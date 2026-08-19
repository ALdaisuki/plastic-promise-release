"""Safe, local deployment state transitions.

The controller renders selected runtime activation assets but never starts a
service, creates an account, touches a remote host, or discovers a node. It
owns only an explicitly selected local state root and keeps destructive
database operations behind separate, affirmative operations. Planning and
preflight remain side-effect free.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ._json_io import write_json_atomically
from .backup_evidence import (
    BackupEvidenceError,
    backup_evidence_path,
    load_verified_backup_evidence,
    write_backup_evidence,
)
from .catalog import module_by_id, profile_by_id
from .migration_journal import migration_journal_schema_present
from .plan import plan_drift_code
from .runtime_assets import materialize_runtime_assets, remove_runtime_assets
from .sqlite_migrations import apply_deployment_migrations, node_governance_schema_present

if TYPE_CHECKING:
    from collections.abc import Callable

    from .plan import DeploymentPlan


@dataclass(frozen=True)
class HostDiskUsage:
    """The only disk facts the preflight policy needs from a host."""

    total_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class DeploymentPreflight:
    """A bounded, read-only preflight result for one deterministic plan."""

    ok: bool
    failure_codes: tuple[str, ...]
    total_bytes: int
    free_bytes: int
    estimated_write_bytes: int
    external_write_bytes: int
    existing_artifact_bytes: int
    resource_evidence_complete: bool
    post_install_free_bytes: int
    required_free_bytes: int
    volumes: tuple[ResourceVolumePreflight, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "checked": True,
            "ok": self.ok,
            "failure_codes": list(self.failure_codes),
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "estimated_write_bytes": self.estimated_write_bytes,
            "external_write_bytes": self.external_write_bytes,
            "existing_artifact_bytes": self.existing_artifact_bytes,
            "resource_evidence_complete": self.resource_evidence_complete,
            "post_install_free_bytes": self.post_install_free_bytes,
            "required_free_bytes": self.required_free_bytes,
            "volumes": [volume.as_dict() for volume in self.volumes],
        }


@dataclass(frozen=True)
class ResourceVolumePreflight:
    """One physical filesystem observed by a no-side-effect resource gate."""

    purposes: tuple[str, ...]
    total_bytes: int
    free_bytes: int
    planned_write_bytes: int
    existing_artifact_bytes: int
    post_install_free_bytes: int
    required_free_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "purposes": list(self.purposes),
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "planned_write_bytes": self.planned_write_bytes,
            "existing_artifact_bytes": self.existing_artifact_bytes,
            "post_install_free_bytes": self.post_install_free_bytes,
            "required_free_bytes": self.required_free_bytes,
        }


@dataclass
class _ResourceVolumeGroup:
    """Mutable aggregation of all selected write sets on one filesystem."""

    usage: HostDiskUsage
    purposes: list[str]
    planned_write_bytes: int = 0
    existing_artifact_bytes: int = 0


class DeploymentApplyError(RuntimeError):
    """A stable reason why an explicit local apply request was refused."""


@dataclass(frozen=True)
class DeploymentApplyResult:
    """The minimal non-sensitive result of a local state transition."""

    changed: bool
    database_action: str
    plan_hash: str
    backup_path: Path | None
    migrations_applied: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "database_action": self.database_action,
            "plan_hash": self.plan_hash,
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "migrations_applied": list(self.migrations_applied),
        }


@dataclass(frozen=True)
class DeploymentStatus:
    """Non-sensitive observation of a locally managed deployment."""

    installed: bool
    database_exists: bool
    database_integrity: str
    plan_hash: str | None
    profile_id: str | None
    module_ids: tuple[str, ...]
    disabled_modules: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "plastic-promise/deployment-status/v1",
            "installed": self.installed,
            "database_exists": self.database_exists,
            "database_integrity": self.database_integrity,
            "plan_hash": self.plan_hash,
            "profile": self.profile_id,
            "modules": list(self.module_ids),
            "disabled_modules": list(self.disabled_modules),
            "service_management": "external",
        }


def _nearest_existing_directory(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else candidate.parent


def _read_disk_usage(path: Path) -> HostDiskUsage:
    observed = shutil.disk_usage(_nearest_existing_directory(path))
    return HostDiskUsage(total_bytes=observed.total, free_bytes=observed.free)


def _filesystem_device(path: Path) -> int:
    """Identify the existing filesystem without creating a configured root."""

    return _nearest_existing_directory(path).stat().st_dev


def _directory_size(path: Path) -> int:
    """Count existing regular files without following symlinks or mutating the path."""

    if not path.exists():
        return 0
    if not path.is_dir():
        raise OSError("resource_location_not_directory")
    total = 0
    errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        errors.append(error)

    for root, _directories, filenames in os.walk(path, followlinks=False, onerror=onerror):
        for filename in filenames:
            candidate = Path(root) / filename
            try:
                metadata = candidate.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(exc)
                continue
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
    if errors:
        raise OSError("resource_location_unreadable") from errors[0]
    return total


def _sqlite_uri(path: Path) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode=rw"


def _sqlite_readonly_uri(path: Path) -> str:
    return f"{path.resolve(strict=True).as_uri()}?mode=ro"


def _sqlite_integrity(path: Path) -> str:
    """Return a bounded integrity result without creating a SQLite file."""

    if not path.is_file():
        return "missing"
    try:
        with sqlite3.connect(_sqlite_readonly_uri(path), uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error:
        return "unreadable"
    return "ok" if result is not None and result[0] == "ok" else "failed"


def _verified_online_backup(
    source_path: Path,
    *,
    backup_directory: Path,
    profile_id: str,
    prefix: str = "backup",
) -> Path:
    """Make a new SQLite online backup without altering the source database."""

    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / f"{prefix}-{uuid4().hex}.sqlite3"
    try:
        with sqlite3.connect(_sqlite_uri(source_path), uri=True) as source:
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DeploymentApplyError("existing_database_integrity_check_failed")
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
                backup_integrity = backup.execute("PRAGMA integrity_check").fetchone()
                if backup_integrity is None or backup_integrity[0] != "ok":
                    raise DeploymentApplyError("backup_integrity_check_failed")
        write_backup_evidence(backup_path, profile_id=profile_id)
    except Exception:
        with suppress(FileNotFoundError):
            backup_path.unlink()
        with suppress(FileNotFoundError):
            backup_evidence_path(backup_path).unlink()
        raise
    return backup_path


def _apply_versioned_migrations(database_path: Path) -> tuple[str, ...]:
    """Apply deployment migrations atomically to an already-opened SQLite file."""

    with sqlite3.connect(_sqlite_uri(database_path), uri=True) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            applied = apply_deployment_migrations(connection)
            if not node_governance_schema_present(connection):
                raise DeploymentApplyError("node_governance_schema_validation_failed")
            if not migration_journal_schema_present(connection):
                raise DeploymentApplyError("migration_journal_schema_validation_failed")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DeploymentApplyError("post_migration_integrity_check_failed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    if not isinstance(applied, tuple) or not all(
        isinstance(migration, str) for migration in applied
    ):
        raise DeploymentApplyError("deployment_migration_result_invalid")
    return applied


def _deployment_state_path(plan: DeploymentPlan) -> Path:
    return Path(plan.target.state_root) / "deployment-state.json"


def _load_state(path: Path) -> dict[str, object] | None:
    """Read an installer-owned state record; malformed records fail closed."""

    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentApplyError("deployment_state_unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != "plastic-promise/deployment-state/v1":
        raise DeploymentApplyError("deployment_state_invalid")
    return value


def _assert_plan_hash(plan: DeploymentPlan, plan_hash: str) -> None:
    if plan_hash != plan.plan_hash:
        raise DeploymentApplyError("plan_hash_mismatch")


def _assert_plan_operation(plan: DeploymentPlan, operation: str) -> None:
    if plan.operation != operation:
        raise DeploymentApplyError("plan_operation_mismatch")


def _assert_plan_current(plan: DeploymentPlan) -> None:
    if drift_code := plan_drift_code(plan):
        raise DeploymentApplyError(drift_code)


def _assert_installed_plan(
    plan: DeploymentPlan, *, plan_hash: str, operation: str
) -> dict[str, object]:
    _assert_plan_hash(plan, plan_hash)
    _assert_plan_operation(plan, operation)
    _assert_plan_current(plan)
    state = _load_state(_deployment_state_path(plan))
    if state is None:
        raise DeploymentApplyError("deployment_not_installed")
    installation_hash = state.get("installation_hash", state.get("plan_hash"))
    if installation_hash != plan.installation_hash:
        raise DeploymentApplyError("installed_plan_hash_mismatch")
    if state.get("database") != str(plan.target.database_path):
        raise DeploymentApplyError("installed_database_target_mismatch")
    return state


def _state_modules(state: dict[str, object]) -> set[str]:
    """Read the installer-owned effective module set and fail closed on corruption."""

    raw = state.get("modules", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DeploymentApplyError("deployment_state_invalid")
    return set(raw)


def _state_disabled_modules(state: dict[str, object]) -> set[str]:
    """Read the installer-owned disabled optional-module set."""

    raw = state.get("disabled_modules", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DeploymentApplyError("deployment_state_invalid")
    return set(raw)


def _runtime_component_directory(plan: DeploymentPlan) -> Path:
    """Return the controller-owned runtime handoff directory for one deployment."""

    return Path(plan.target.state_root) / "runtime-components"


def _runtime_component_path(plan: DeploymentPlan, module_id: str) -> Path:
    return _runtime_component_directory(plan) / f"{module_id}.json"


def _runtime_component_payload(
    plan: DeploymentPlan, module_id: str, *, assets: tuple[Path, ...]
) -> dict[str, object]:
    """Describe a module's non-secret, explicitly activated runtime contract."""

    module = module_by_id(module_id)
    if module is None:
        raise DeploymentApplyError("managed_module_not_selected")
    return {
        "schema": "plastic-promise/runtime-component/v1",
        "module": module_id,
        "profile": plan.profile_id,
        "risk_tier": module.risk_tier,
        "activation": "explicit-platform-asset",
        "assets": [str(asset) for asset in assets],
        "canonical_sqlite_access": "none",
    }


def _materialize_runtime_component(plan: DeploymentPlan, module_id: str) -> None:
    assets = materialize_runtime_assets(plan, module_id)
    try:
        write_json_atomically(
            _runtime_component_path(plan, module_id),
            _runtime_component_payload(plan, module_id, assets=assets),
        )
    except Exception:
        remove_runtime_assets(plan, module_id)
        raise


def _remove_runtime_component(plan: DeploymentPlan, module_id: str) -> None:
    """Delete exact controller-generated files, never user runtime data."""

    with suppress(FileNotFoundError):
        _runtime_component_path(plan, module_id).unlink()
    remove_runtime_assets(plan, module_id)
    with suppress(OSError):
        _runtime_component_directory(plan).rmdir()


def _remove_known_database_files(database_path: Path) -> None:
    """Remove only the primary SQLite file and its exact sidecars."""

    with suppress(FileNotFoundError):
        database_path.unlink()
    _remove_known_database_sidecars(database_path)


def _remove_known_database_sidecars(database_path: Path) -> None:
    """Remove only SQLite sidecars belonging to one explicit primary path."""

    for path in (
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    ):
        with suppress(FileNotFoundError):
            path.unlink()


def _stage_known_database_sidecars(database_path: Path) -> tuple[tuple[Path, Path], ...]:
    """Move exact SQLite sidecars aside so a failed primary replacement can undo it."""

    token = uuid4().hex
    staged: list[tuple[Path, Path]] = []
    try:
        for path in (
            database_path.with_name(f"{database_path.name}-wal"),
            database_path.with_name(f"{database_path.name}-shm"),
        ):
            if not path.exists():
                continue
            staged_path = path.with_name(f".{path.name}.pre-restore-{token}")
            os.replace(path, staged_path)
            staged.append((path, staged_path))
    except Exception:
        _restore_staged_database_sidecars(tuple(staged))
        raise
    return tuple(staged)


def _restore_staged_database_sidecars(staged: tuple[tuple[Path, Path], ...]) -> None:
    """Put staged old sidecars back after an unsuccessful primary replacement."""

    for original, staged_path in reversed(staged):
        if staged_path.exists():
            os.replace(staged_path, original)


def _discard_staged_database_sidecars(staged: tuple[tuple[Path, Path], ...]) -> None:
    """Discard old sidecars only after the replacement primary is durable."""

    for _, staged_path in staged:
        with suppress(FileNotFoundError):
            staged_path.unlink()


def _restore_from_backup(*, source: Path, target: Path) -> None:
    """Stage a verified replacement and discard stale target sidecars.

    The caller has already obtained an explicit service-stopped acknowledgement.
    SQLite has no multi-file atomic replace primitive, so this routine first
    materialises and verifies a standalone candidate, then removes only the
    target's exact WAL/SHM companions immediately before atomically replacing
    the primary file. A restored primary can therefore never reopen with stale
    sidecars from the prior canonical database.
    """

    if not source.is_file():
        raise DeploymentApplyError("restore_source_missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".restore-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    candidate = Path(temporary_name)
    try:
        with sqlite3.connect(_sqlite_readonly_uri(source), uri=True) as original:
            integrity = original.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DeploymentApplyError("restore_source_integrity_check_failed")
            with sqlite3.connect(candidate) as restored:
                original.backup(restored)
                candidate_integrity = restored.execute("PRAGMA integrity_check").fetchone()
                if candidate_integrity is None or candidate_integrity[0] != "ok":
                    raise DeploymentApplyError("restore_candidate_integrity_check_failed")
        staged_sidecars = _stage_known_database_sidecars(target)
        try:
            os.replace(candidate, target)
        except Exception:
            _restore_staged_database_sidecars(staged_sidecars)
            raise
        _discard_staged_database_sidecars(staged_sidecars)
    except Exception:
        with suppress(FileNotFoundError):
            candidate.unlink()
        raise


class DeploymentController:
    """Local deployment controller; service lifecycle stays external."""

    def __init__(self, *, disk_usage: Callable[[Path], HostDiskUsage] | None = None) -> None:
        self._disk_usage = disk_usage or _read_disk_usage

    def preflight(self, plan: DeploymentPlan) -> DeploymentPreflight:
        """Hard-gate every selected local write set without changing target state."""

        profile = profile_by_id(plan.profile_id)
        if profile is None:  # Defensive: public plans are normally manifest-derived.
            raise ValueError("deployment_plan_profile_unsupported")
        budget = plan.resource_budget
        failure_codes: list[str] = []
        state_usage = self.observe_disk_usage(plan.target.state_root)
        state_write_bytes = plan.estimated_write_bytes
        external_write_bytes = 0
        existing_artifact_bytes = 0
        volume_inputs: list[tuple[str, Path, int, int]] = [
            ("canonical-state", plan.target.state_root, state_write_bytes, 0)
        ]
        resource_evidence_complete = budget is not None
        if budget is None:
            failure_codes.append("resource_budget_required")
        else:
            state_write_bytes -= (
                budget.image_layers_bytes + budget.image_unpack_bytes + budget.model_cache_bytes
            )
            external_write_bytes = (
                budget.image_layers_bytes + budget.image_unpack_bytes + budget.model_cache_bytes
            )
            volume_inputs[0] = ("canonical-state", plan.target.state_root, state_write_bytes, 0)
            container_write_bytes = budget.image_layers_bytes + budget.image_unpack_bytes
            if container_write_bytes:
                container_root = (
                    plan.resource_locations.container_store
                    if plan.resource_locations is not None
                    else None
                )
                if container_root is None:
                    failure_codes.append("resource_locations_container_store_required")
                else:
                    try:
                        container_existing = _directory_size(container_root)
                    except OSError:
                        failure_codes.append("container_store_unreadable")
                    else:
                        existing_artifact_bytes += container_existing
                        volume_inputs.append(
                            (
                                "container-store",
                                container_root,
                                container_write_bytes,
                                container_existing,
                            )
                        )
            if budget.model_cache_bytes:
                model_root = (
                    plan.resource_locations.model_cache
                    if plan.resource_locations is not None
                    else None
                )
                if model_root is None:
                    failure_codes.append("resource_locations_model_cache_required")
                else:
                    try:
                        model_existing = _directory_size(model_root)
                    except OSError:
                        failure_codes.append("model_cache_unreadable")
                    else:
                        existing_artifact_bytes += model_existing
                        volume_inputs.append(
                            ("model-cache", model_root, budget.model_cache_bytes, model_existing)
                        )

        grouped_volumes: dict[int, _ResourceVolumeGroup] = {}
        for purpose, root, planned_write, existing_artifacts in volume_inputs:
            try:
                device = _filesystem_device(root)
                usage = self.observe_disk_usage(root)
            except OSError:
                failure_codes.append("resource_location_unreadable")
                continue
            group = grouped_volumes.setdefault(
                device,
                _ResourceVolumeGroup(usage=usage, purposes=[]),
            )
            group.purposes.append(purpose)
            group.planned_write_bytes += planned_write
            group.existing_artifact_bytes += existing_artifacts

        volume_reports: list[ResourceVolumePreflight] = []
        for group in grouped_volumes.values():
            usage = group.usage
            planned_write = group.planned_write_bytes
            required_free = max(
                profile.resource_policy.minimum_free_bytes,
                math.ceil(usage.total_bytes * profile.resource_policy.minimum_free_fraction),
            )
            post_install_free = max(0, usage.free_bytes - planned_write)
            if post_install_free < required_free:
                failure_codes.append("post_install_disk_reserve_unmet")
            volume_reports.append(
                ResourceVolumePreflight(
                    purposes=tuple(sorted(group.purposes)),
                    total_bytes=usage.total_bytes,
                    free_bytes=usage.free_bytes,
                    planned_write_bytes=planned_write,
                    existing_artifact_bytes=group.existing_artifact_bytes,
                    post_install_free_bytes=post_install_free,
                    required_free_bytes=required_free,
                )
            )

        state_report = next(
            (report for report in volume_reports if "canonical-state" in report.purposes),
            None,
        )
        required_free_bytes = (
            state_report.required_free_bytes
            if state_report is not None
            else max(
                profile.resource_policy.minimum_free_bytes,
                math.ceil(state_usage.total_bytes * profile.resource_policy.minimum_free_fraction),
            )
        )
        post_install_free_bytes = (
            state_report.post_install_free_bytes if state_report is not None else 0
        )
        return DeploymentPreflight(
            ok=not failure_codes,
            failure_codes=tuple(dict.fromkeys(failure_codes)),
            total_bytes=state_usage.total_bytes,
            free_bytes=state_usage.free_bytes,
            estimated_write_bytes=plan.estimated_write_bytes,
            external_write_bytes=external_write_bytes,
            existing_artifact_bytes=existing_artifact_bytes,
            resource_evidence_complete=resource_evidence_complete,
            post_install_free_bytes=post_install_free_bytes,
            required_free_bytes=required_free_bytes,
            volumes=tuple(sorted(volume_reports, key=lambda report: report.purposes)),
        )

    def observe_disk_usage(self, path: Path) -> HostDiskUsage:
        """Observe local disk capacity without creating the requested path."""

        usage = self._disk_usage(path)
        if usage.total_bytes <= 0 or usage.free_bytes < 0:
            raise ValueError("deployment_disk_usage_invalid")
        return usage

    def apply(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        dry_run: bool = False,
    ) -> DeploymentApplyResult:
        """Create a new canonical SQLite only after all read-only gates pass."""

        _assert_plan_hash(plan, plan_hash)
        _assert_plan_operation(plan, "install")
        _assert_plan_current(plan)
        report = self.preflight(plan)
        if not report.ok:
            raise DeploymentApplyError(report.failure_codes[0])
        if dry_run:
            return DeploymentApplyResult(
                changed=False,
                database_action="dry_run",
                plan_hash=plan.plan_hash,
                backup_path=None,
                migrations_applied=(),
            )
        database_path = plan.target.database_path
        existing_database = database_path.exists()
        database_created = False
        backup_path: Path | None = None
        migrations_applied: tuple[str, ...] = ()
        try:
            if existing_database:
                backup_path = _verified_online_backup(
                    database_path,
                    backup_directory=plan.target.state_root / "backups",
                    profile_id=plan.profile_id,
                    prefix="pre-apply",
                )
            else:
                database_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(database_path) as connection:
                    database_created = True
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        raise DeploymentApplyError("new_database_integrity_check_failed")
            migrations_applied = _apply_versioned_migrations(database_path)
            for module_id in plan.module_ids:
                _materialize_runtime_component(plan, module_id)
            write_json_atomically(
                plan.target.state_root / "deployment-state.json",
                {
                    "schema": "plastic-promise/deployment-state/v1",
                    "plan_hash": plan.plan_hash,
                    "installation_hash": plan.installation_hash,
                    "profile": plan.profile_id,
                    "modules": list(plan.module_ids),
                    "nodes": list(plan.node_ids),
                    "database": str(database_path),
                    "migrations_applied": list(migrations_applied),
                    "disabled_modules": [],
                },
            )
        except Exception:
            if database_created:
                _remove_known_database_files(database_path)
            raise
        return DeploymentApplyResult(
            changed=True,
            database_action="attached" if existing_database else "created",
            plan_hash=plan.plan_hash,
            backup_path=backup_path,
            migrations_applied=migrations_applied,
        )

    def backup(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        dry_run: bool = False,
    ) -> DeploymentApplyResult:
        """Create a verified online backup of an installed canonical SQLite."""

        _assert_installed_plan(plan, plan_hash=plan_hash, operation="backup")
        report = self.preflight(plan)
        if not report.ok:
            raise DeploymentApplyError(report.failure_codes[0])
        if _sqlite_integrity(plan.target.database_path) != "ok":
            raise DeploymentApplyError("existing_database_integrity_check_failed")
        if dry_run:
            return DeploymentApplyResult(False, "backup_dry_run", plan.plan_hash, None, ())
        backup_path = _verified_online_backup(
            plan.target.database_path,
            backup_directory=plan.target.state_root / "backups",
            profile_id=plan.profile_id,
            prefix="operator-backup",
        )
        return DeploymentApplyResult(True, "backed_up", plan.plan_hash, backup_path, ())

    def upgrade(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        dry_run: bool = False,
    ) -> DeploymentApplyResult:
        """Back up and migrate an installed canonical database in place.

        Unlike ``apply``, upgrade/repair must never interpret a missing primary
        database as permission to initialise a fresh one. Recovery from a
        missing canonical database is deliberately limited to the separately
        confirmed restore/replace-db path.
        """

        _assert_installed_plan(plan, plan_hash=plan_hash, operation="upgrade")
        report = self.preflight(plan)
        if not report.ok:
            raise DeploymentApplyError(report.failure_codes[0])
        integrity = _sqlite_integrity(plan.target.database_path)
        if integrity == "missing":
            raise DeploymentApplyError("database_missing_restore_required")
        if integrity != "ok":
            raise DeploymentApplyError("existing_database_integrity_check_failed")
        if dry_run:
            return DeploymentApplyResult(False, "upgrade_dry_run", plan.plan_hash, None, ())
        backup_path = _verified_online_backup(
            plan.target.database_path,
            backup_directory=plan.target.state_root / "backups",
            profile_id=plan.profile_id,
            prefix="pre-upgrade",
        )
        migrations_applied = _apply_versioned_migrations(plan.target.database_path)
        return DeploymentApplyResult(
            True,
            "upgraded",
            plan.plan_hash,
            backup_path,
            migrations_applied,
        )

    def disable_module(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        module_id: str,
        dry_run: bool = False,
    ) -> bool:
        """Record an optional module as disabled without stopping a service."""

        state = _assert_installed_plan(plan, plan_hash=plan_hash, operation="module-disable")
        if plan.module_id != module_id:
            raise DeploymentApplyError("plan_module_mismatch")
        module = module_by_id(module_id)
        modules = _state_modules(state)
        if module is None or module_id not in modules:
            raise DeploymentApplyError("managed_module_not_selected")
        if module.risk_tier == "core":
            raise DeploymentApplyError("core_module_disable_forbidden")
        disabled = _state_disabled_modules(state)
        changed = module_id not in disabled
        if changed and not dry_run:
            disabled.add(module_id)
            state["disabled_modules"] = sorted(disabled)
            write_json_atomically(_deployment_state_path(plan), state)
        return changed

    def enable_module(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        module_id: str,
        dry_run: bool = False,
    ) -> bool:
        """Record one installed optional module as enabled without starting it."""

        state = _assert_installed_plan(plan, plan_hash=plan_hash, operation="module-enable")
        if plan.module_id != module_id:
            raise DeploymentApplyError("plan_module_mismatch")
        module = module_by_id(module_id)
        modules = _state_modules(state)
        if module is None or module_id not in modules:
            raise DeploymentApplyError("managed_module_not_selected")
        if module.risk_tier == "core":
            raise DeploymentApplyError("core_module_enable_implicit")
        disabled = _state_disabled_modules(state)
        changed = module_id in disabled
        if changed and not dry_run:
            disabled.remove(module_id)
            state["disabled_modules"] = sorted(disabled)
            write_json_atomically(_deployment_state_path(plan), state)
        return changed

    def install_module(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        module_id: str,
        dry_run: bool = False,
    ) -> bool:
        """Install an optional module's controller-owned runtime handoff descriptor."""

        state = _assert_installed_plan(plan, plan_hash=plan_hash, operation="module-install")
        if plan.module_id != module_id:
            raise DeploymentApplyError("plan_module_mismatch")
        module = module_by_id(module_id)
        if module is None:
            raise DeploymentApplyError("managed_module_not_selected")
        if module.risk_tier == "core":
            raise DeploymentApplyError("core_module_install_implicit")
        if plan.profile_id not in module.supported_profiles:
            raise DeploymentApplyError("module_profile_unsupported")
        modules = _state_modules(state)
        if not set(module.requires).issubset(modules):
            raise DeploymentApplyError("module_dependency_missing")
        changed = module_id not in modules
        if changed and not dry_run:
            _materialize_runtime_component(plan, module_id)
            modules.add(module_id)
            state["modules"] = sorted(modules)
            write_json_atomically(_deployment_state_path(plan), state)
        return changed

    def remove_module(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        module_id: str,
        dry_run: bool = False,
    ) -> bool:
        """Remove an optional module descriptor while retaining canonical data and backups."""

        state = _assert_installed_plan(plan, plan_hash=plan_hash, operation="module-remove")
        if plan.module_id != module_id:
            raise DeploymentApplyError("plan_module_mismatch")
        module = module_by_id(module_id)
        modules = _state_modules(state)
        if module is None or module_id not in modules:
            raise DeploymentApplyError("managed_module_not_selected")
        if module.risk_tier == "core":
            raise DeploymentApplyError("core_module_remove_forbidden")
        if any(
            module_id in dependent.requires
            for installed_module_id in modules - {module_id}
            if (dependent := module_by_id(installed_module_id)) is not None
        ):
            raise DeploymentApplyError("module_required_by_installed_module")
        if not dry_run:
            modules.remove(module_id)
            disabled = _state_disabled_modules(state)
            disabled.discard(module_id)
            state["modules"] = sorted(modules)
            state["disabled_modules"] = sorted(disabled)
            write_json_atomically(_deployment_state_path(plan), state)
            _remove_runtime_component(plan, module_id)
        return True

    def remove(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        confirmed: bool,
        dry_run: bool = False,
    ) -> bool:
        """Remove controller-owned runtime descriptors while retaining canonical data."""

        _assert_installed_plan(plan, plan_hash=plan_hash, operation="remove")
        if not confirmed:
            raise DeploymentApplyError("remove_confirmation_required")
        if dry_run:
            return True
        state = _load_state(_deployment_state_path(plan))
        if state is not None:
            for module_id in _state_modules(state):
                _remove_runtime_component(plan, module_id)
        _deployment_state_path(plan).unlink()
        return True

    def purge(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        confirmed: bool,
        service_stopped: bool,
        dry_run: bool = False,
    ) -> DeploymentApplyResult:
        """Physically remove the managed SQLite only after explicit acknowledgements."""

        _assert_installed_plan(plan, plan_hash=plan_hash, operation="purge")
        report = self.preflight(plan)
        if not report.ok:
            raise DeploymentApplyError(report.failure_codes[0])
        if not confirmed:
            raise DeploymentApplyError("purge_confirmation_required")
        if not service_stopped:
            raise DeploymentApplyError("service_stopped_confirmation_required")
        if dry_run:
            return DeploymentApplyResult(False, "purge_dry_run", plan.plan_hash, None, ())
        backup_path = _verified_online_backup(
            plan.target.database_path,
            backup_directory=plan.target.state_root / "backups",
            profile_id=plan.profile_id,
            prefix="pre-purge",
        )
        _remove_known_database_files(plan.target.database_path)
        _deployment_state_path(plan).unlink()
        return DeploymentApplyResult(True, "purged", plan.plan_hash, backup_path, ())

    def restore(
        self,
        plan: DeploymentPlan,
        *,
        plan_hash: str,
        source: Path,
        confirmed: bool,
        service_stopped: bool,
        dry_run: bool = False,
    ) -> DeploymentApplyResult:
        """Replace an installed SQLite from a verified source after an online backup."""

        _assert_installed_plan(plan, plan_hash=plan_hash, operation="restore")
        if (
            plan.source_path is None
            or source.expanduser().resolve(strict=False) != plan.source_path
        ):
            raise DeploymentApplyError("restore_source_plan_mismatch")
        report = self.preflight(plan)
        if not report.ok:
            raise DeploymentApplyError(report.failure_codes[0])
        if not confirmed:
            raise DeploymentApplyError("restore_confirmation_required")
        if not service_stopped:
            raise DeploymentApplyError("service_stopped_confirmation_required")
        if dry_run:
            return DeploymentApplyResult(False, "restore_dry_run", plan.plan_hash, None, ())
        try:
            load_verified_backup_evidence(source, expected_profile_id=plan.profile_id)
        except BackupEvidenceError as error:
            raise DeploymentApplyError(str(error)) from error
        backup_path = _verified_online_backup(
            plan.target.database_path,
            backup_directory=plan.target.state_root / "backups",
            profile_id=plan.profile_id,
            prefix="pre-restore",
        )
        _restore_from_backup(source=source, target=plan.target.database_path)
        try:
            migrations_applied = _apply_versioned_migrations(plan.target.database_path)
        except Exception as migration_error:
            try:
                _restore_from_backup(source=backup_path, target=plan.target.database_path)
            except Exception as rollback_error:
                raise DeploymentApplyError(
                    "restore_migration_failed_recovery_failed"
                ) from rollback_error
            raise DeploymentApplyError("restore_migration_failed_reverted") from migration_error
        return DeploymentApplyResult(
            True, "restored", plan.plan_hash, backup_path, migrations_applied
        )

    def status(self, *, state_root: Path) -> DeploymentStatus:
        """Observe a state root without creating files, services, or databases."""

        state_path = state_root.expanduser().resolve(strict=False) / "deployment-state.json"
        state = _load_state(state_path)
        if state is None:
            return DeploymentStatus(False, False, "missing", None, None, (), ())
        database_raw = state.get("database")
        database = Path(database_raw) if isinstance(database_raw, str) else None
        if database is None:
            raise DeploymentApplyError("deployment_state_invalid")
        modules = tuple(sorted(_state_modules(state)))
        disabled = tuple(sorted(_state_disabled_modules(state)))
        plan_hash_raw = state.get("plan_hash")
        profile_raw = state.get("profile")
        return DeploymentStatus(
            True,
            database.is_file(),
            _sqlite_integrity(database),
            plan_hash_raw if isinstance(plan_hash_raw, str) else None,
            profile_raw if isinstance(profile_raw, str) else None,
            modules,
            disabled,
        )


__all__ = [
    "DeploymentApplyError",
    "DeploymentApplyResult",
    "DeploymentController",
    "DeploymentPreflight",
    "DeploymentStatus",
    "HostDiskUsage",
]
