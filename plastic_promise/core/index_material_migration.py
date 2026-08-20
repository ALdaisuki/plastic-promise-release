"""Offline, fail-closed migration of canonical memory index material."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plastic_promise.core.index_outbox_reconciliation import (
    canonical_source_fingerprint,
    snapshot_index_outbox,
)
from plastic_promise.core.memory_index import (
    COMPACT_V2_POLICY,
    LEGACY_POLICY,
    IndexMaterial,
    build_index_material,
    effective_embedding_model_name,
    metadata_with_index_material,
    read_persisted_index_material,
)
from plastic_promise.core.synthesis import (
    SYNTHESIS_STATUSES,
    canonical_synthesis_binding,
    synthesis_binding_hash,
)

_TARGET_POLICIES = frozenset({LEGACY_POLICY, COMPACT_V2_POLICY})
_MIGRATABLE_SYNTHESIS_STATUSES = SYNTHESIS_STATUSES & frozenset({"verified", "stale", "contested"})
_DERIVED_MEMORY_COLUMNS = frozenset({"embedding_text", "search_text", "embedding_hash"})
_PROTECTED_EXCLUDED_TABLES = frozenset({"memory_version", "sqlite_sequence", "store_outbox"})
_REQUIRED_OUTBOX_COLUMNS = frozenset(
    {
        "outbox_id",
        "tool_name",
        "project_id",
        "call_id",
        "status",
        "payload_json",
        "error_class",
        "error_message",
        "metadata_json",
        "created_at",
        "dedupe_key",
        "attempt_count",
        "updated_at",
        "next_attempt_at",
    }
)


class IndexMaterialMigrationError(RuntimeError):
    """Stable fail-closed diagnostic for an offline canonical migration."""


@dataclass(frozen=True)
class _PreparedRow:
    memory_id: str
    project_id: str
    memory_type: str
    previous_embedding_hash: str
    material: IndexMaterial
    metadata: dict[str, Any]
    changed: bool
    synthesis_status: str | None = None
    synthesis_revision: int | None = None
    synthesis_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class MigrationPlan:
    row_count: int
    changed_row_count: int
    ordinary_changed_count: int
    synthesis_changed_count: int
    memory_version: int
    target_policy: str
    target_model_identity: str
    target_model_sha256: str
    policy_counts: dict[str, int]
    model_counts: dict[str, int]
    source_fingerprint: str
    protected_fingerprint: str
    index_outbox: dict[str, Any]
    rows: tuple[_PreparedRow, ...]

    def public_report(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "changed_row_count": self.changed_row_count,
            "ordinary_changed_count": self.ordinary_changed_count,
            "synthesis_changed_count": self.synthesis_changed_count,
            "memory_version": self.memory_version,
            "target_policy": self.target_policy,
            "target_model_identity": self.target_model_identity,
            "target_model_sha256": self.target_model_sha256,
            "current_policy_counts": self.policy_counts,
            "current_model_counts": self.model_counts,
            "source_fingerprint": self.source_fingerprint,
            "protected_fingerprint": self.protected_fingerprint,
            "index_outbox": self.index_outbox,
        }


def _utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _database_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise IndexMaterialMigrationError("database_must_be_existing_regular_file")
    return path.resolve(strict=True)


def _backup_directory(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise IndexMaterialMigrationError("backup_directory_must_be_existing_directory")
    return path.resolve(strict=True)


def _connect(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if not writable:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _parse_environment_file(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise IndexMaterialMigrationError("environment_file_must_be_existing_regular_file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise IndexMaterialMigrationError("environment_file_unreadable") from exc
    environment: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if (
            not separator
            or not name
            or not (name[0].isalpha() or name[0] == "_")
            or any(not (character.isalnum() or character == "_") for character in name)
            or name in environment
            or any(character.isspace() for character in value)
        ):
            raise IndexMaterialMigrationError("environment_file_invalid")
        environment[name] = value
    return environment


@contextmanager
def configured_environment(paths: Sequence[str | Path]) -> Iterator[None]:
    """Apply generated EnvironmentFiles without exposing their values."""

    updates: dict[str, str] = {}
    for raw_path in paths:
        updates.update(_parse_environment_file(Path(raw_path).expanduser()))
    previous = {name: os.environ.get(name) for name in updates}
    missing = {name for name in updates if name not in os.environ}
    try:
        os.environ.update(updates)
        yield
    finally:
        for name, value in previous.items():
            if name in missing:
                os.environ.pop(name, None)
            elif value is not None:
                os.environ[name] = value


def _json_object(value: object, *, reason: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise IndexMaterialMigrationError(reason)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise IndexMaterialMigrationError(reason) from exc
    if not isinstance(parsed, dict):
        raise IndexMaterialMigrationError(reason)
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_fingerprint_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(value).hex()}
    return {"__repr__": repr(value)}


def _protected_metadata(table: str, value: object) -> object:
    if not isinstance(value, str):
        return _normalized_fingerprint_value(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return {"__invalid_json__": value}
    if not isinstance(parsed, dict):
        return parsed
    result = dict(parsed)
    if table == "memories":
        result.pop("memory_index", None)
        result.pop("synthesis_binding", None)
        result.pop("synthesis_binding_hash", None)
    elif table == "synthesis_artifacts":
        result.pop("synthesis_binding", None)
        result.pop("synthesis_binding_hash", None)
    return result


def protected_database_fingerprint(connection: sqlite3.Connection) -> str:
    """Hash every table/field that the migration is not allowed to change."""

    digest = hashlib.sha256()
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in _PROTECTED_EXCLUDED_TABLES and not str(row[0]).startswith("sqlite_")
    ]
    for table in tables:
        quoted_table = table.replace('"', '""')
        columns = [
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
        ]
        selected = [
            column
            for column in columns
            if not (table == "memories" and column in _DERIVED_MEMORY_COLUMNS)
        ]
        quoted_columns = [f'"{column.replace(chr(34), chr(34) * 2)}"' for column in selected]
        rows: list[str] = []
        if quoted_columns:
            for raw in connection.execute(
                f'SELECT {", ".join(quoted_columns)} FROM "{quoted_table}"'
            ).fetchall():
                record: dict[str, object] = {}
                for column, value in zip(selected, raw, strict=True):
                    if column == "metadata_json" and table in {
                        "memories",
                        "synthesis_artifacts",
                    }:
                        record[column] = _protected_metadata(table, value)
                    else:
                        record[column] = _normalized_fingerprint_value(value)
                rows.append(_canonical_json(record))
        rows.sort()
        encoded = _canonical_json({"table": table, "columns": selected, "rows": rows}).encode(
            "utf-8"
        )
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    quoted = table.replace('"', '""')
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()}


def _preflight_schema(
    connection: sqlite3.Connection,
    *,
    require_quiescent_outbox: bool,
) -> int:
    required_memory = {
        "id",
        "content",
        "memory_type",
        "project_id",
        "visibility",
        "source_class",
        "origin_kind",
        "origin_hash",
        "metadata_json",
        "embedding_text",
        "embedding_hash",
        "search_text",
    }
    if not required_memory.issubset(_table_columns(connection, "memories")):
        raise IndexMaterialMigrationError("memory_schema_incompatible")
    if not _REQUIRED_OUTBOX_COLUMNS.issubset(_table_columns(connection, "store_outbox")):
        raise IndexMaterialMigrationError("outbox_schema_incompatible")
    version_rows = connection.execute(
        "SELECT singleton, version, typeof(version) FROM memory_version"
    ).fetchall()
    if (
        len(version_rows) != 1
        or int(version_rows[0][0]) != 1
        or str(version_rows[0][2]) != "integer"
        or int(version_rows[0][1]) < 0
    ):
        raise IndexMaterialMigrationError("memory_version_invalid")
    unresolved = connection.execute(
        "SELECT status, COUNT(*) FROM store_outbox "
        "WHERE tool_name IN ('memory_index', 'synthesis_index') AND status <> 'done' "
        "GROUP BY status ORDER BY status"
    ).fetchall()
    if require_quiescent_outbox and unresolved:
        raise IndexMaterialMigrationError("index_outbox_not_quiescent")
    return int(version_rows[0][1])


def _integrity(connection: sqlite3.Connection) -> tuple[str, str]:
    quick = connection.execute("PRAGMA quick_check").fetchone()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if quick is None or str(quick[0]) != "ok":
        raise IndexMaterialMigrationError("database_quick_check_failed")
    if integrity is None or str(integrity[0]) != "ok":
        raise IndexMaterialMigrationError("database_integrity_check_failed")
    return "ok", "ok"


def _prepare_synthesis(
    connection: sqlite3.Connection,
    memory: dict[str, Any],
    material: IndexMaterial,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], str, int, dict[str, Any]]:
    row = connection.execute(
        "SELECT status, revision, synthesis_key, metadata_json "
        "FROM synthesis_artifacts WHERE memory_id = ?",
        (str(memory["id"]),),
    ).fetchone()
    if row is None:
        raise IndexMaterialMigrationError("synthesis_control_missing")
    status = str(row[0] or "")
    if status not in _MIGRATABLE_SYNTHESIS_STATUSES:
        raise IndexMaterialMigrationError("synthesis_control_not_terminal")
    if type(row[1]) is not int or row[1] < 1:
        raise IndexMaterialMigrationError("synthesis_control_revision_invalid")
    control_metadata = _json_object(row[3], reason="synthesis_control_metadata_invalid")
    candidate = {
        **memory,
        "embedding_text": material.vector_text,
        "search_text": material.search_text,
        "embedding_hash": material.embedding_hash,
        "metadata_json": metadata,
    }
    binding = canonical_synthesis_binding(candidate, material)
    binding_hash = synthesis_binding_hash(binding)
    metadata["synthesis_binding"] = binding
    metadata["synthesis_binding_hash"] = binding_hash
    control_metadata["synthesis_binding"] = binding
    control_metadata["synthesis_binding_hash"] = binding_hash
    if binding.get("synthesis_key") != str(row[2] or ""):
        raise IndexMaterialMigrationError("synthesis_binding_key_mismatch")
    return metadata, status, int(row[1]), control_metadata


def build_migration_plan(
    connection: sqlite3.Connection,
    *,
    target_policy: str,
    require_quiescent_outbox: bool = True,
    target_model_name: str | None = None,
) -> MigrationPlan:
    target_policy = str(target_policy or "").strip().casefold()
    if target_policy not in _TARGET_POLICIES:
        raise IndexMaterialMigrationError("target_policy_invalid")
    _integrity(connection)
    memory_version = _preflight_schema(
        connection,
        require_quiescent_outbox=require_quiescent_outbox,
    )
    index_outbox = snapshot_index_outbox(connection)
    status_counts = index_outbox.get("status_counts")
    if not isinstance(status_counts, dict):
        raise IndexMaterialMigrationError("index_outbox_snapshot_invalid")
    unknown_statuses = set(status_counts) - {
        "pending",
        "processing",
        "blocked",
        "failed",
        "done",
    }
    if unknown_statuses:
        raise IndexMaterialMigrationError("index_outbox_status_unknown")
    if not require_quiescent_outbox and int(status_counts.get("processing", 0) or 0) > 0:
        raise IndexMaterialMigrationError("index_outbox_processing_job_active")
    target_model = (
        str(target_model_name).strip()
        if target_model_name is not None
        else effective_embedding_model_name()
    )
    if not target_model:
        raise IndexMaterialMigrationError("target_model_identity_invalid")
    target_model_sha256 = hashlib.sha256(target_model.encode("utf-8")).hexdigest()
    cursor = connection.execute("SELECT * FROM memories ORDER BY id")
    memories = [dict(row) for row in cursor.fetchall()]
    if not memories:
        raise IndexMaterialMigrationError("canonical_memories_empty")
    policy_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    prepared: list[_PreparedRow] = []
    synthesis_ids: set[str] = set()
    for memory in memories:
        memory_id = str(memory.get("id") or "").strip()
        project_id = str(memory.get("project_id") or "").strip()
        memory_type = str(memory.get("memory_type") or "").strip().casefold()
        if not memory_id or not project_id:
            raise IndexMaterialMigrationError("canonical_memory_identity_invalid")
        persisted = read_persisted_index_material(memory)
        if persisted is None:
            raise IndexMaterialMigrationError("source_index_material_unverifiable")
        policy_counts[persisted.policy] = policy_counts.get(persisted.policy, 0) + 1
        model_counts[persisted.model_name] = model_counts.get(persisted.model_name, 0) + 1
        material = build_index_material(
            memory,
            policy=target_policy,
            model_name=target_model,
        )
        metadata = metadata_with_index_material(memory.get("metadata_json"), material)
        revision: int | None = None
        synthesis_status: str | None = None
        control_metadata: dict[str, Any] | None = None
        if memory_type == "synthesis":
            synthesis_ids.add(memory_id)
            metadata, synthesis_status, revision, control_metadata = _prepare_synthesis(
                connection,
                memory,
                material,
                metadata,
            )
        elif (
            connection.execute(
                "SELECT 1 FROM synthesis_artifacts WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            is not None
        ):
            raise IndexMaterialMigrationError("ordinary_memory_has_synthesis_control")
        metadata_json = _canonical_json(metadata)
        row_changed = any(
            (
                str(memory.get("embedding_text") or "") != material.vector_text,
                str(memory.get("search_text") or "") != material.search_text,
                str(memory.get("embedding_hash") or "") != material.embedding_hash,
                _canonical_json(
                    _json_object(
                        memory.get("metadata_json"),
                        reason="memory_metadata_invalid",
                    )
                )
                != metadata_json,
            )
        )
        prepared.append(
            _PreparedRow(
                memory_id=memory_id,
                project_id=project_id,
                memory_type=memory_type,
                previous_embedding_hash=str(memory.get("embedding_hash") or ""),
                material=material,
                metadata=metadata,
                changed=row_changed,
                synthesis_status=synthesis_status,
                synthesis_revision=revision,
                synthesis_metadata=control_metadata,
            )
        )
    controlled_ids = {
        str(row[0])
        for row in connection.execute("SELECT memory_id FROM synthesis_artifacts").fetchall()
    }
    if controlled_ids != synthesis_ids:
        raise IndexMaterialMigrationError("synthesis_control_coverage_mismatch")
    changed_rows = [row for row in prepared if row.changed]
    return MigrationPlan(
        row_count=len(prepared),
        changed_row_count=len(changed_rows),
        ordinary_changed_count=sum(row.memory_type != "synthesis" for row in changed_rows),
        synthesis_changed_count=sum(row.memory_type == "synthesis" for row in changed_rows),
        memory_version=memory_version,
        target_policy=target_policy,
        target_model_identity=target_model,
        target_model_sha256=target_model_sha256,
        policy_counts=dict(sorted(policy_counts.items())),
        model_counts=dict(sorted(model_counts.items())),
        source_fingerprint=canonical_source_fingerprint(connection),
        protected_fingerprint=protected_database_fingerprint(connection),
        index_outbox=index_outbox,
        rows=tuple(prepared),
    )


def inspect_database(
    database: str | Path,
    *,
    target_policy: str,
    allow_unresolved_index_outbox: bool = False,
    target_model_name: str | None = None,
) -> MigrationPlan:
    path = _database_path(database)
    connection = _connect(path, writable=False)
    try:
        return build_migration_plan(
            connection,
            target_policy=target_policy,
            require_quiescent_outbox=not allow_unresolved_index_outbox,
            target_model_name=target_model_name,
        )
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_online_backup(source: Path, backup_directory: Path) -> tuple[Path, str, str]:
    name = f"plastic_memory.pre-index-material-migration.{_utc_compact()}.{secrets.token_hex(4)}.db"
    backup = backup_directory / name
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source_connection: sqlite3.Connection | None = None
    backup_connection: sqlite3.Connection | None = None
    try:
        source_connection = _connect(source, writable=False)
        backup_connection = sqlite3.connect(backup)
        source_connection.backup(backup_connection)
        backup_connection.commit()
        backup_connection.close()
        backup_connection = None
        os.chmod(backup, 0o600)
        verification = _connect(backup, writable=False)
        try:
            _integrity(verification)
            fingerprint = canonical_source_fingerprint(verification)
        finally:
            verification.close()
        return backup, _sha256_file(backup), fingerprint
    except BaseException:
        with suppress(OSError):
            backup.unlink(missing_ok=True)
        raise
    finally:
        if backup_connection is not None:
            backup_connection.close()
        if source_connection is not None:
            source_connection.close()


def _insert_memory_job(
    connection: sqlite3.Connection,
    *,
    row: _PreparedRow,
    memory_version: int,
    call_id: str,
    now: str,
) -> None:
    payload = {
        "action": "upsert",
        "expected_embedding_hash": row.material.embedding_hash,
        "material_revision": row.material.embedding_hash,
        "memory_id": row.memory_id,
        "memory_version": memory_version,
        "project_id": row.project_id,
    }
    dedupe_key = "memory-index:" + _canonical_json(
        {
            "action": "upsert",
            "expected_embedding_hash": row.material.embedding_hash,
            "memory_id": row.memory_id,
            "memory_version": memory_version,
            "project_id": row.project_id,
        }
    )
    connection.execute(
        "INSERT INTO store_outbox ("
        "outbox_id, tool_name, project_id, call_id, status, payload_json, "
        "error_class, error_message, metadata_json, created_at, dedupe_key, "
        "attempt_count, updated_at, next_attempt_at"
        ") VALUES (?, 'memory_index', ?, ?, 'pending', ?, '', '', ?, ?, ?, 0, ?, '')",
        (
            f"outbox_{secrets.token_hex(8)}",
            row.project_id,
            call_id,
            _canonical_json(payload),
            _canonical_json({"job_schema": "memory-index/v3"}),
            now,
            dedupe_key,
            now,
        ),
    )


def _insert_synthesis_job(
    connection: sqlite3.Connection,
    *,
    row: _PreparedRow,
    call_id: str,
    now: str,
) -> None:
    if row.synthesis_revision is None:
        raise IndexMaterialMigrationError("synthesis_revision_missing")
    if row.synthesis_status not in _MIGRATABLE_SYNTHESIS_STATUSES:
        raise IndexMaterialMigrationError("synthesis_status_missing")
    action = "upsert" if row.synthesis_status == "verified" else "delete"
    payload = {
        "action": action,
        "memory_id": row.memory_id,
        "revision": row.synthesis_revision,
    }
    connection.execute(
        "INSERT INTO store_outbox ("
        "outbox_id, tool_name, project_id, call_id, status, payload_json, "
        "error_class, error_message, metadata_json, created_at, dedupe_key, "
        "attempt_count, updated_at, next_attempt_at"
        ") VALUES (?, 'synthesis_index', ?, ?, 'pending', ?, '', '', ?, ?, ?, 0, ?, '')",
        (
            f"outbox_{secrets.token_hex(8)}",
            row.project_id,
            call_id,
            _canonical_json(payload),
            _canonical_json({"job_schema": "synthesis-index/v1"}),
            now,
            f"synthesis-index:{row.memory_id}:{row.synthesis_revision}:{action}",
            now,
        ),
    )


def _apply_plan(connection: sqlite3.Connection, plan: MigrationPlan) -> tuple[int, str, str]:
    changed = [row for row in plan.rows if row.changed]
    if not changed:
        return plan.memory_version, plan.source_fingerprint, plan.protected_fingerprint
    next_version = plan.memory_version + 1
    now = _utc_now()
    call_id = f"index-material-migration:{_utc_compact()}:{secrets.token_hex(4)}"
    for row in changed:
        cursor = connection.execute(
            "UPDATE memories SET embedding_text = ?, search_text = ?, embedding_hash = ?, "
            "metadata_json = ? WHERE id = ? AND embedding_hash = ?",
            (
                row.material.vector_text,
                row.material.search_text,
                row.material.embedding_hash,
                _canonical_json(row.metadata),
                row.memory_id,
                row.previous_embedding_hash,
            ),
        )
        if cursor.rowcount != 1:
            raise IndexMaterialMigrationError("canonical_memory_changed_during_migration")
        if row.memory_type == "synthesis":
            if (
                row.synthesis_status not in _MIGRATABLE_SYNTHESIS_STATUSES
                or row.synthesis_revision is None
                or row.synthesis_metadata is None
            ):
                raise IndexMaterialMigrationError("synthesis_control_missing")
            control = connection.execute(
                "UPDATE synthesis_artifacts SET metadata_json = ? "
                "WHERE memory_id = ? AND status = ? AND revision = ?",
                (
                    _canonical_json(row.synthesis_metadata),
                    row.memory_id,
                    row.synthesis_status,
                    row.synthesis_revision,
                ),
            )
            if control.rowcount != 1:
                raise IndexMaterialMigrationError("synthesis_control_changed_during_migration")
    version = connection.execute(
        "UPDATE memory_version SET version = ? WHERE singleton = 1 AND version = ?",
        (next_version, plan.memory_version),
    )
    if version.rowcount != 1:
        raise IndexMaterialMigrationError("memory_version_changed_during_migration")
    for row in changed:
        if row.memory_type == "synthesis":
            _insert_synthesis_job(connection, row=row, call_id=call_id, now=now)
        else:
            _insert_memory_job(
                connection,
                row=row,
                memory_version=next_version,
                call_id=call_id,
                now=now,
            )
    protected = protected_database_fingerprint(connection)
    if protected != plan.protected_fingerprint:
        raise IndexMaterialMigrationError("protected_state_changed_during_migration")
    source = canonical_source_fingerprint(connection)
    if source == plan.source_fingerprint:
        raise IndexMaterialMigrationError("canonical_source_fingerprint_unchanged")
    return next_version, source, protected


def apply_migration(
    database: str | Path,
    *,
    backup_directory: str | Path,
    target_policy: str,
    expected_row_count: int,
    expected_source_fingerprint: str,
    expected_target_model_sha256: str,
    allow_unresolved_index_outbox: bool = False,
    expected_index_outbox_watermark: int | None = None,
    expected_index_outbox_immutable_digest: str | None = None,
    expected_index_outbox_job_count: int | None = None,
    expected_index_outbox_active_count: int | None = None,
    target_model_name: str | None = None,
) -> dict[str, object]:
    source = _database_path(database)
    backups = _backup_directory(backup_directory)
    initial = inspect_database(
        source,
        target_policy=target_policy,
        allow_unresolved_index_outbox=allow_unresolved_index_outbox,
        target_model_name=target_model_name,
    )
    if initial.row_count != expected_row_count:
        raise IndexMaterialMigrationError("expected_row_count_mismatch")
    if initial.source_fingerprint != expected_source_fingerprint:
        raise IndexMaterialMigrationError("expected_source_fingerprint_mismatch")
    if initial.target_model_sha256 != expected_target_model_sha256:
        raise IndexMaterialMigrationError("expected_target_model_mismatch")
    expected_outbox = (
        expected_index_outbox_watermark,
        expected_index_outbox_immutable_digest,
        expected_index_outbox_job_count,
        expected_index_outbox_active_count,
    )
    if allow_unresolved_index_outbox:
        if any(value is None for value in expected_outbox):
            raise IndexMaterialMigrationError("recovery_outbox_expectations_required")
        observed_outbox = (
            initial.index_outbox.get("watermark"),
            initial.index_outbox.get("immutable_digest"),
            initial.index_outbox.get("job_count"),
            initial.index_outbox.get("active_snapshot_jobs"),
        )
        if observed_outbox != expected_outbox:
            raise IndexMaterialMigrationError("expected_index_outbox_mismatch")
    elif any(value is not None for value in expected_outbox):
        raise IndexMaterialMigrationError("recovery_outbox_flag_required")

    backup, backup_sha256, backup_fingerprint = _create_online_backup(source, backups)
    if backup_fingerprint != initial.source_fingerprint:
        raise IndexMaterialMigrationError("backup_source_fingerprint_mismatch")
    backup_connection = _connect(backup, writable=False)
    try:
        backup_outbox = snapshot_index_outbox(backup_connection)
    finally:
        backup_connection.close()
    if backup_outbox != initial.index_outbox:
        raise IndexMaterialMigrationError("backup_index_outbox_snapshot_mismatch")

    connection = _connect(source, writable=True)
    committed = False
    try:
        connection.execute("BEGIN EXCLUSIVE")
        locked = build_migration_plan(
            connection,
            target_policy=target_policy,
            require_quiescent_outbox=not allow_unresolved_index_outbox,
            target_model_name=target_model_name,
        )
        if (
            locked.source_fingerprint != initial.source_fingerprint
            or locked.protected_fingerprint != initial.protected_fingerprint
            or locked.target_model_sha256 != initial.target_model_sha256
            or locked.row_count != initial.row_count
            or locked.index_outbox != initial.index_outbox
        ):
            raise IndexMaterialMigrationError("canonical_source_changed_after_backup")
        next_version, source_after, protected_after = _apply_plan(connection, locked)
        connection.commit()
        committed = True
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    verification = _connect(source, writable=False)
    try:
        quick, integrity = _integrity(verification)
        final = build_migration_plan(
            verification,
            target_policy=target_policy,
            require_quiescent_outbox=False,
            target_model_name=target_model_name,
        )
    finally:
        verification.close()
    if not committed or final.changed_row_count != 0:
        raise IndexMaterialMigrationError("post_migration_material_verification_failed")
    if final.protected_fingerprint != protected_after or final.source_fingerprint != source_after:
        raise IndexMaterialMigrationError("post_migration_fingerprint_mismatch")
    if final.memory_version != next_version:
        raise IndexMaterialMigrationError("post_migration_version_mismatch")
    return {
        "applied": True,
        **initial.public_report(),
        "memory_version_after": next_version,
        "source_fingerprint_after": source_after,
        "protected_fingerprint_after": protected_after,
        "backup_path": str(backup),
        "backup_sha256": backup_sha256,
        "backup_source_fingerprint": backup_fingerprint,
        "quick_check": quick,
        "integrity_check": integrity,
    }


__all__ = [
    "IndexMaterialMigrationError",
    "MigrationPlan",
    "apply_migration",
    "build_migration_plan",
    "configured_environment",
    "inspect_database",
    "protected_database_fingerprint",
]
