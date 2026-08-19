"""Side-effect-free, operation-bound deployment planning contracts.

Every mutating deployment operation is bound to a deterministic plan hash.  A
plan records a non-secret fingerprint of the relevant local SQLite assets and
installer state, so the controller rejects a plan if its selected action,
canonical database, restore source, or installer state changes after review.
Planning only reads local files; it never creates a directory, database,
backup, service asset, or temporary SQLite file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from .manifest import ResolvedDeployment, ResourceBudget, ResourceLocations

DEPLOYMENT_PLAN_SCHEMA_VERSION = "plastic-promise/deployment-plan/v2"
_DEPLOYMENT_STATE_SCHEMA_VERSION = "plastic-promise/deployment-state/v1"
_BOOTSTRAP_MUTABLE_BYTES = 512 * 1024**2
_MIGRATION_SCRATCH_MINIMUM_BYTES = 64 * 1024**2
_HASH_CHUNK_BYTES = 1024 * 1024

DEPLOYMENT_OPERATIONS = frozenset(
    {
        "install",
        "upgrade",
        "backup",
        "module-disable",
        "module-enable",
        "module-install",
        "module-remove",
        "remove",
        "purge",
        "restore",
    }
)
_MIGRATION_OPERATIONS = frozenset({"install", "upgrade", "restore"})
_TARGET_BACKUP_OPERATIONS = frozenset({"install", "upgrade", "backup", "purge", "restore"})


@dataclass(frozen=True)
class DeploymentTarget:
    """Explicit local paths a future deployment operation may manage."""

    state_root: Path
    database_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "state_root": str(self.state_root),
            "database_path": str(self.database_path),
        }


@dataclass(frozen=True)
class SQLiteAssetSnapshot:
    """Non-secret fingerprint and bounded size estimate for one SQLite asset set."""

    primary_state: str
    primary_bytes: int
    wal_bytes: int
    shm_bytes: int
    fingerprint: str

    @property
    def exists(self) -> bool:
        return self.primary_state == "file"

    @property
    def total_bytes(self) -> int:
        return self.primary_bytes + self.wal_bytes + self.shm_bytes

    @property
    def online_backup_bytes(self) -> int:
        """Conservative write reservation for an SQLite online backup.

        A WAL-backed source can materialise changes beyond the primary file, so
        reserving the complete observed SQLite asset set is safer than treating
        the original bootstrap estimate as the whole write set.
        """

        return max(self.primary_bytes, self.total_bytes)

    def as_dict(self) -> dict[str, object]:
        return {
            "primary_state": self.primary_state,
            "primary_bytes": self.primary_bytes,
            "wal_bytes": self.wal_bytes,
            "shm_bytes": self.shm_bytes,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class DeploymentPlan:
    """A deterministic, non-executable plan for exactly one local operation."""

    plan_id: str
    deployment_id: str
    profile_id: str
    module_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    target: DeploymentTarget
    operation: str
    module_id: str | None
    target_snapshot: SQLiteAssetSnapshot
    state_fingerprint: str
    source_path: Path | None
    source_snapshot: SQLiteAssetSnapshot | None
    resource_budget: ResourceBudget | None
    resource_locations: ResourceLocations | None
    estimated_write_bytes: int
    high_risk_steps: tuple[str, ...]
    installation_hash: str
    plan_hash: str

    def as_dict(self) -> dict[str, object]:
        source: dict[str, object] | None = None
        if self.source_snapshot is not None:
            source = {
                "fingerprint": self.source_snapshot.fingerprint,
            }
        return {
            "schema": DEPLOYMENT_PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "operation": self.operation,
            "deployment_id": self.deployment_id,
            "profile": self.profile_id,
            "modules": list(self.module_ids),
            "nodes": list(self.node_ids),
            "module": self.module_id,
            "target": self.target.as_dict(),
            "preconditions": {
                "database": self.target_snapshot.as_dict(),
                "deployment_state_fingerprint": self.state_fingerprint,
                "restore_source": source,
            },
            "resource_budget": (
                self.resource_budget.as_dict()
                if self.resource_budget is not None
                else {"status": "missing"}
            ),
            "resource_locations": (
                self.resource_locations.as_dict()
                if self.resource_locations is not None
                else {"status": "missing"}
            ),
            "estimated_write_bytes": self.estimated_write_bytes,
            "high_risk_steps": list(self.high_risk_steps),
            "installation_hash": self.installation_hash,
            "plan_hash": self.plan_hash,
        }


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _file_digest(path: Path, *, label: str) -> tuple[str, int, str]:
    """Fingerprint one expected file without creating or opening SQLite."""

    if not path.exists():
        return ("missing", 0, _sha256(f"{label}:missing".encode()))
    if not path.is_file():
        return ("non-file", 0, _sha256(f"{label}:non-file".encode()))
    digest = hashlib.sha256()
    digest.update(label.encode("utf-8"))
    digest.update(b"\0")
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return ("file", path.stat().st_size, f"sha256:{digest.hexdigest()}")


def sqlite_asset_snapshot(database_path: Path) -> SQLiteAssetSnapshot:
    """Capture a deterministic fingerprint for a primary SQLite file and sidecars."""

    primary_state, primary_bytes, primary_hash = _file_digest(database_path, label="primary")
    _, wal_bytes, wal_hash = _file_digest(
        database_path.with_name(f"{database_path.name}-wal"), label="wal"
    )
    _, shm_bytes, shm_hash = _file_digest(
        database_path.with_name(f"{database_path.name}-shm"), label="shm"
    )
    return SQLiteAssetSnapshot(
        primary_state=primary_state,
        primary_bytes=primary_bytes,
        wal_bytes=wal_bytes,
        shm_bytes=shm_bytes,
        fingerprint=_sha256(
            json.dumps(
                {
                    "primary": primary_hash,
                    "wal": wal_hash,
                    "shm": shm_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    )


def deployment_state_fingerprint(state_root: Path) -> str:
    """Fingerprint only the installer-owned state record, if any."""

    state_path = state_root / "deployment-state.json"
    state, _, digest = _file_digest(state_path, label="deployment-state")
    if state == "non-file":
        return "invalid"
    return digest


def _installation_payload(
    resolved: ResolvedDeployment, *, target: DeploymentTarget
) -> dict[str, object]:
    return {
        "schema": _DEPLOYMENT_STATE_SCHEMA_VERSION,
        "deployment_id": resolved.deployment_id,
        "profile": resolved.profile_id,
        "modules": list(resolved.module_ids),
        "nodes": list(resolved.node_ids),
        "resource_locations": (
            resolved.resource_locations.as_dict()
            if resolved.resource_locations is not None
            else None
        ),
        "target": target.as_dict(),
    }


def _estimated_write_bytes(
    *,
    operation: str,
    target_snapshot: SQLiteAssetSnapshot,
    source_snapshot: SQLiteAssetSnapshot | None,
    resource_budget: ResourceBudget | None,
) -> int:
    """Bound the files this controller can actually write for one operation."""

    total = _BOOTSTRAP_MUTABLE_BYTES if operation == "install" and not target_snapshot.exists else 0
    if operation in _TARGET_BACKUP_OPERATIONS and target_snapshot.exists:
        total += target_snapshot.online_backup_bytes
    if operation == "restore" and source_snapshot is not None:
        total += source_snapshot.online_backup_bytes
    if operation in _MIGRATION_OPERATIONS:
        migration_base = max(
            target_snapshot.primary_bytes,
            source_snapshot.primary_bytes if source_snapshot is not None else 0,
        )
        total += max(_MIGRATION_SCRATCH_MINIMUM_BYTES, migration_base // 10)
    if resource_budget is not None:
        total += resource_budget.planned_write_bytes
    return total


def _high_risk_steps(
    *,
    operation: str,
    target_snapshot: SQLiteAssetSnapshot,
) -> tuple[str, ...]:
    """Describe review-sensitive state changes separately from the plan hash."""

    steps: list[str] = []
    if operation == "install" and not target_snapshot.exists:
        steps.append("canonical_sqlite_bootstrap")
    if operation in _TARGET_BACKUP_OPERATIONS and target_snapshot.exists:
        steps.append("verified_online_sqlite_backup")
    if operation in _MIGRATION_OPERATIONS:
        steps.append("versioned_sqlite_migration")
    if operation == "restore":
        steps.append("verified_sqlite_replace")
    if operation == "purge":
        steps.append("physical_canonical_sqlite_delete")
    return tuple(steps)


def _operation_payload(
    *,
    operation: str,
    module_id: str | None,
    target_snapshot: SQLiteAssetSnapshot,
    state_fingerprint: str,
    source_path: Path | None,
    source_snapshot: SQLiteAssetSnapshot | None,
) -> dict[str, object]:
    return {
        "operation": operation,
        "module": module_id,
        "target_database": target_snapshot.as_dict(),
        "deployment_state_fingerprint": state_fingerprint,
        "restore_source": (
            {
                "path": str(source_path) if source_path is not None else None,
                "fingerprint": source_snapshot.fingerprint if source_snapshot is not None else None,
            }
            if source_snapshot is not None
            else None
        ),
    }


def create_deployment_plan(
    resolved: ResolvedDeployment,
    *,
    state_root: Path,
    operation: str = "install",
    module_id: str | None = None,
    source: Path | None = None,
) -> DeploymentPlan:
    """Build a read-only, action-bound deployment plan.

    ``restore`` binds the exact source SQLite asset. The controller later
    requires matching, hash-verified backup evidence for the selected profile.
    A cross-profile transfer needs its own one-way migration contract instead
    of being smuggled through a generic database replacement command.
    """

    if operation not in DEPLOYMENT_OPERATIONS:
        raise ValueError("deployment_plan_operation_unsupported")
    if operation.startswith("module-") and not module_id:
        raise ValueError("deployment_plan_module_required")
    if operation == "restore":
        if source is None:
            raise ValueError("restore_source_required")
    elif source is not None:
        raise ValueError("deployment_plan_source_not_allowed")

    normalized_state_root = state_root.expanduser().resolve(strict=False)
    target = DeploymentTarget(
        state_root=normalized_state_root,
        database_path=normalized_state_root / "data" / "plastic-promise.sqlite3",
    )
    normalized_source = source.expanduser().resolve(strict=False) if source is not None else None
    target_snapshot = sqlite_asset_snapshot(target.database_path)
    state_fingerprint = deployment_state_fingerprint(target.state_root)
    source_snapshot = (
        sqlite_asset_snapshot(normalized_source) if normalized_source is not None else None
    )
    if source_snapshot is not None and not source_snapshot.exists:
        raise ValueError("restore_source_missing")
    installation_payload = _installation_payload(resolved, target=target)
    installation_hash = _sha256(
        json.dumps(
            installation_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    estimated_write_bytes = _estimated_write_bytes(
        operation=operation,
        target_snapshot=target_snapshot,
        source_snapshot=source_snapshot,
        resource_budget=resolved.resource_budget,
    )
    high_risk_steps = _high_risk_steps(
        operation=operation,
        target_snapshot=target_snapshot,
    )
    plan_payload: dict[str, Any] = {
        "schema": DEPLOYMENT_PLAN_SCHEMA_VERSION,
        "installation_hash": installation_hash,
        **_operation_payload(
            operation=operation,
            module_id=module_id,
            target_snapshot=target_snapshot,
            state_fingerprint=state_fingerprint,
            source_path=normalized_source,
            source_snapshot=source_snapshot,
        ),
        "resource_budget": (
            resolved.resource_budget.as_dict() if resolved.resource_budget is not None else None
        ),
        "resource_locations": (
            resolved.resource_locations.as_dict()
            if resolved.resource_locations is not None
            else None
        ),
        "estimated_write_bytes": estimated_write_bytes,
        "high_risk_steps": high_risk_steps,
    }
    plan_hash = _sha256(
        json.dumps(
            plan_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return DeploymentPlan(
        plan_id=f"plan-{plan_hash.removeprefix('sha256:')[:20]}",
        deployment_id=resolved.deployment_id,
        profile_id=resolved.profile_id,
        module_ids=resolved.module_ids,
        node_ids=resolved.node_ids,
        target=target,
        operation=operation,
        module_id=module_id,
        target_snapshot=target_snapshot,
        state_fingerprint=state_fingerprint,
        source_path=normalized_source,
        source_snapshot=source_snapshot,
        resource_budget=resolved.resource_budget,
        resource_locations=resolved.resource_locations,
        estimated_write_bytes=estimated_write_bytes,
        high_risk_steps=high_risk_steps,
        installation_hash=installation_hash,
        plan_hash=plan_hash,
    )


def plan_drift_code(plan: DeploymentPlan) -> str | None:
    """Return the precise non-secret reason an operation plan is no longer current."""

    if sqlite_asset_snapshot(plan.target.database_path) != plan.target_snapshot:
        return "database_state_drift"
    if deployment_state_fingerprint(plan.target.state_root) != plan.state_fingerprint:
        return "deployment_state_drift"
    if (
        plan.source_path is not None
        and sqlite_asset_snapshot(plan.source_path) != plan.source_snapshot
    ):
        return "restore_source_drift"
    return None


def create_install_plan(resolved: ResolvedDeployment, *, state_root: Path) -> DeploymentPlan:
    """Backward-compatible helper for the default ``install`` plan."""

    return create_deployment_plan(resolved, state_root=state_root)


__all__ = [
    "DEPLOYMENT_OPERATIONS",
    "DEPLOYMENT_PLAN_SCHEMA_VERSION",
    "DeploymentPlan",
    "DeploymentTarget",
    "SQLiteAssetSnapshot",
    "create_deployment_plan",
    "create_install_plan",
    "deployment_state_fingerprint",
    "plan_drift_code",
    "sqlite_asset_snapshot",
]
