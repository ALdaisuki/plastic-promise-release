"""Durable grant, lease, fence, and receipt state for migration operations.

The journal owns no deployment actions. It only serializes execution authority
inside the canonical pp-core SQLite database. Mutable adapters receive the
resulting fence through ``MigrationExecutionContext`` and remain responsible
for applying that fence to their own idempotent phase operations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

MIGRATION_JOURNAL_SCHEMA_VERSION = "plastic-promise-migration-journal/v2"
DEFAULT_MIGRATION_LEASE_SECONDS = 900

_SAFE_REF = re.compile(r"^[a-z][a-z0-9:_-]{1,127}$")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")
_MANIFEST_ENTRY = re.compile(r"^[a-z][a-z0-9:._/-]{1,511}$")
_REQUIRED_COLUMNS = {
    "pp_migration_journal_schema": frozenset({"singleton", "schema_version", "installed_at"}),
    "pp_migration_grants": frozenset(
        {
            "grant_id",
            "installation_ref",
            "operation_ref",
            "plan_hash",
            "phase_manifest_json",
            "phase_manifest_sha256",
            "schema_manifest_json",
            "schema_manifest_sha256",
            "issued_at",
            "expires_at",
            "status",
            "operation_id",
            "created_at",
            "updated_at",
        }
    ),
    "pp_migration_operations": frozenset(
        {
            "operation_id",
            "installation_ref",
            "operation_ref",
            "plan_hash",
            "phase_manifest_json",
            "phase_manifest_sha256",
            "schema_manifest_json",
            "schema_manifest_sha256",
            "grant_id",
            "status",
            "fencing_generation",
            "lease_owner_ref",
            "lease_expires_at",
            "receipt_json",
            "created_at",
            "updated_at",
        }
    ),
    "pp_migration_installation_leases": frozenset(
        {
            "installation_ref",
            "operation_id",
            "fencing_generation",
            "lease_owner_ref",
            "lease_expires_at",
            "updated_at",
        }
    ),
    "pp_migration_phase_records": frozenset(
        {
            "operation_id",
            "phase_index",
            "phase",
            "outcome",
            "reason_code",
            "phase_manifest_sha256",
            "schema_manifest_sha256",
            "completed_at",
        }
    ),
}
_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS pp_migration_journal_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version TEXT NOT NULL,
        installed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pp_migration_grants (
        grant_id TEXT PRIMARY KEY,
        installation_ref TEXT NOT NULL,
        operation_ref TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        phase_manifest_json TEXT NOT NULL,
        phase_manifest_sha256 TEXT NOT NULL,
        schema_manifest_json TEXT NOT NULL,
        schema_manifest_sha256 TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'available',
        operation_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(status IN ('available', 'consumed'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pp_migration_operations (
        operation_id TEXT PRIMARY KEY,
        installation_ref TEXT NOT NULL,
        operation_ref TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        phase_manifest_json TEXT NOT NULL,
        phase_manifest_sha256 TEXT NOT NULL,
        schema_manifest_json TEXT NOT NULL,
        schema_manifest_sha256 TEXT NOT NULL,
        grant_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        fencing_generation INTEGER NOT NULL,
        lease_owner_ref TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        receipt_json TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(installation_ref, operation_ref),
        CHECK(status IN ('running', 'applied', 'rejected', 'rolled-back', 'recovery-required')),
        CHECK(fencing_generation >= 1),
        FOREIGN KEY(grant_id) REFERENCES pp_migration_grants(grant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pp_migration_installation_leases (
        installation_ref TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        fencing_generation INTEGER NOT NULL,
        lease_owner_ref TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(fencing_generation >= 1)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pp_migration_operations_status
    ON pp_migration_operations(status, lease_expires_at, operation_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS pp_migration_phase_records (
        operation_id TEXT NOT NULL,
        phase_index INTEGER NOT NULL,
        phase TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        phase_manifest_sha256 TEXT NOT NULL,
        schema_manifest_sha256 TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY(operation_id, phase_index),
        FOREIGN KEY(operation_id) REFERENCES pp_migration_operations(operation_id)
            ON DELETE CASCADE,
        CHECK(phase_index >= 0),
        CHECK(outcome IN ('completed', 'failed', 'skipped'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pp_migration_phase_records_order
    ON pp_migration_phase_records(operation_id, phase_index)
    """,
)


class MigrationJournalError(ValueError):
    """A stable, non-secret persistence error."""

    def __init__(self, code: str) -> None:
        if _SAFE_REF.fullmatch(code) is None:
            raise ValueError("migration_journal_error_code_invalid")
        self.code = code
        super().__init__(code)


def _require_ref(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None:
        raise MigrationJournalError(code)
    return value


def _require_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MigrationJournalError(code)
    return value


def _manifest_json(value: object, code: str) -> str:
    if not isinstance(value, (tuple, list)) or not value:
        raise MigrationJournalError(code)
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or _MANIFEST_ENTRY.fullmatch(entry) is None:
            raise MigrationJournalError(code)
        entries.append(entry)
    if len(set(entries)) != len(entries):
        raise MigrationJournalError(code)
    return json.dumps(entries, ensure_ascii=True, separators=(",", ":"))


def _manifest_tuple(value: object, code: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationJournalError(code) from exc
    payload = _manifest_json(value, code)
    loaded = json.loads(payload)
    return tuple(str(entry) for entry in loaded)


def _manifest_digest(value: object, code: str) -> str:
    payload = _manifest_json(value, code)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _require_utc(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MigrationJournalError(code)
    return value.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return (
        _require_utc(value, "migration_journal_timestamp_invalid")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise MigrationJournalError("migration_journal_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationJournalError("migration_journal_timestamp_invalid") from exc
    return _require_utc(parsed, "migration_journal_timestamp_invalid")


_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|credential|private[_-]?key|api[_-]?key|authorization)",
    re.IGNORECASE,
)
_SECRET_VALUE = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk|rk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def _secret_free_receipt(value: object, *, path: str = "receipt") -> object:
    """Validate a JSON receipt without ever persisting credential material."""

    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                raise MigrationJournalError("migration_receipt_secret_detected")
            result[key] = _secret_free_receipt(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _secret_free_receipt(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_VALUE):
            raise MigrationJournalError("migration_receipt_secret_detected")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise MigrationJournalError("migration_receipt_json_invalid")


def _receipt_json(receipt: object) -> str:
    if not isinstance(receipt, dict):
        raise MigrationJournalError("migration_receipt_json_invalid")
    safe = _secret_free_receipt(receipt)
    try:
        return json.dumps(
            safe, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise MigrationJournalError("migration_receipt_json_invalid") from exc


def _operation_id(identity: MigrationExecutionIdentity) -> str:
    payload = "\x1f".join(
        (
            identity.installation_ref,
            identity.operation_ref,
            identity.plan_hash,
            identity.grant_id,
        )
    )
    return f"migration-operation:{hashlib.sha256(payload.encode()).hexdigest()}"


def migration_journal_schema_present(connection: sqlite3.Connection) -> bool:
    """Return whether all durable migration-journal tables and columns exist."""

    if not isinstance(connection, sqlite3.Connection):
        return False
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        names = {str(row[0]) for row in rows}
        if not _REQUIRED_COLUMNS.keys() <= names:
            return False
        columns_present = all(
            columns.issubset(
                {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            )
            for table, columns in _REQUIRED_COLUMNS.items()
        )
        if not columns_present:
            return False
        marker = connection.execute(
            "SELECT schema_version FROM pp_migration_journal_schema WHERE singleton=1"
        ).fetchone()
        return marker is not None and str(marker[0]) == MIGRATION_JOURNAL_SCHEMA_VERSION
    except sqlite3.Error:
        return False


def apply_migration_journal_schema(connection: sqlite3.Connection) -> None:
    """Apply additive journal DDL inside a caller-owned backup-gated transaction."""

    if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
        raise ValueError("migration_journal_migration_transaction_required")
    for statement in _DDL_STATEMENTS:
        connection.execute(statement)
    empty_manifest_digest = f"sha256:{hashlib.sha256(b'[]').hexdigest()}"
    additions = {
        "pp_migration_grants": (
            ("phase_manifest_json", "TEXT NOT NULL DEFAULT '[]'"),
            (
                "phase_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
            ("schema_manifest_json", "TEXT NOT NULL DEFAULT '[]'"),
            (
                "schema_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
        ),
        "pp_migration_operations": (
            ("phase_manifest_json", "TEXT NOT NULL DEFAULT '[]'"),
            (
                "phase_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
            ("schema_manifest_json", "TEXT NOT NULL DEFAULT '[]'"),
            (
                "schema_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
        ),
        "pp_migration_phase_records": (
            (
                "phase_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
            (
                "schema_manifest_sha256",
                f"TEXT NOT NULL DEFAULT '{empty_manifest_digest}'",
            ),
        ),
    }
    for table, columns in additions.items():
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, declaration in columns:
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    connection.execute(
        """
        INSERT INTO pp_migration_journal_schema(singleton, schema_version, installed_at)
        VALUES (1, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            schema_version=excluded.schema_version,
            installed_at=excluded.installed_at
        """,
        (
            MIGRATION_JOURNAL_SCHEMA_VERSION,
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        ),
    )


@dataclass(frozen=True)
class MigrationExecutionIdentity:
    installation_ref: str
    operation_ref: str
    plan_hash: str
    phase_manifest: tuple[str, ...]
    phase_manifest_sha256: str
    schema_manifest: tuple[str, ...]
    schema_manifest_sha256: str
    grant_id: str
    grant_issued_at: datetime
    grant_expires_at: datetime

    def __post_init__(self) -> None:
        _require_ref(self.installation_ref, "migration_installation_reference_invalid")
        _require_ref(self.operation_ref, "migration_operation_reference_invalid")
        _require_digest(self.plan_hash, "migration_plan_hash_invalid")
        phase_manifest = _manifest_tuple(
            self.phase_manifest,
            "migration_phase_manifest_invalid",
        )
        schema_manifest = _manifest_tuple(
            self.schema_manifest,
            "migration_schema_manifest_invalid",
        )
        if phase_manifest != self.phase_manifest or schema_manifest != self.schema_manifest:
            raise MigrationJournalError("migration_manifest_not_canonical")
        _require_digest(
            self.phase_manifest_sha256,
            "migration_phase_manifest_digest_invalid",
        )
        _require_digest(
            self.schema_manifest_sha256,
            "migration_schema_manifest_digest_invalid",
        )
        if self.phase_manifest_sha256 != _manifest_digest(
            phase_manifest,
            "migration_phase_manifest_invalid",
        ):
            raise MigrationJournalError("migration_phase_manifest_digest_mismatch")
        if self.schema_manifest_sha256 != _manifest_digest(
            schema_manifest,
            "migration_schema_manifest_invalid",
        ):
            raise MigrationJournalError("migration_schema_manifest_digest_mismatch")
        _require_ref(self.grant_id, "migration_grant_id_invalid")
        issued = _require_utc(self.grant_issued_at, "migration_grant_timestamp_invalid")
        expires = _require_utc(self.grant_expires_at, "migration_grant_timestamp_invalid")
        if expires <= issued:
            raise MigrationJournalError("migration_grant_expiry_invalid")


@dataclass(frozen=True)
class MigrationExecutionLease:
    operation_id: str
    installation_ref: str
    operation_ref: str
    plan_hash: str
    phase_manifest: tuple[str, ...]
    phase_manifest_sha256: str
    schema_manifest: tuple[str, ...]
    schema_manifest_sha256: str
    grant_id: str
    owner_ref: str
    fencing_generation: int
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_ref(self.operation_id, "migration_operation_id_invalid")
        _require_ref(self.installation_ref, "migration_installation_reference_invalid")
        _require_ref(self.operation_ref, "migration_operation_reference_invalid")
        _require_digest(self.plan_hash, "migration_plan_hash_invalid")
        phase_manifest = _manifest_tuple(
            self.phase_manifest,
            "migration_phase_manifest_invalid",
        )
        schema_manifest = _manifest_tuple(
            self.schema_manifest,
            "migration_schema_manifest_invalid",
        )
        if phase_manifest != self.phase_manifest or schema_manifest != self.schema_manifest:
            raise MigrationJournalError("migration_manifest_not_canonical")
        if self.phase_manifest_sha256 != _manifest_digest(
            phase_manifest,
            "migration_phase_manifest_invalid",
        ):
            raise MigrationJournalError("migration_phase_manifest_digest_mismatch")
        if self.schema_manifest_sha256 != _manifest_digest(
            schema_manifest,
            "migration_schema_manifest_invalid",
        ):
            raise MigrationJournalError("migration_schema_manifest_digest_mismatch")
        _require_ref(self.grant_id, "migration_grant_id_invalid")
        _require_ref(self.owner_ref, "migration_lease_owner_invalid")
        if isinstance(self.fencing_generation, bool) or self.fencing_generation < 1:
            raise MigrationJournalError("migration_fencing_generation_invalid")
        _require_utc(self.expires_at, "migration_lease_timestamp_invalid")


@dataclass(frozen=True)
class MigrationPhaseReceipt:
    """One durable, ordered, secret-free phase outcome."""

    operation_id: str
    phase_index: int
    phase: str
    outcome: str
    reason_code: str
    phase_manifest_sha256: str
    schema_manifest_sha256: str
    completed_at: datetime

    def __post_init__(self) -> None:
        _require_ref(self.operation_id, "migration_operation_id_invalid")
        if (
            isinstance(self.phase_index, bool)
            or not isinstance(self.phase_index, int)
            or self.phase_index < 0
        ):
            raise MigrationJournalError("migration_phase_index_invalid")
        _require_ref(self.phase, "migration_phase_invalid")
        if self.outcome not in {"completed", "failed", "skipped"}:
            raise MigrationJournalError("migration_phase_outcome_invalid")
        _require_ref(self.reason_code, "migration_phase_reason_invalid")
        _require_digest(
            self.phase_manifest_sha256,
            "migration_phase_manifest_digest_invalid",
        )
        _require_digest(
            self.schema_manifest_sha256,
            "migration_schema_manifest_digest_invalid",
        )
        _require_utc(self.completed_at, "migration_journal_timestamp_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "phase_index": self.phase_index,
            "phase": self.phase,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "phase_manifest_sha256": self.phase_manifest_sha256,
            "schema_manifest_sha256": self.schema_manifest_sha256,
            "completed_at": _utc(self.completed_at),
        }


def _validate_phase_binding(
    lease: MigrationExecutionLease,
    *,
    phase_index: int,
    phase: str,
) -> None:
    if isinstance(phase_index, bool) or not isinstance(phase_index, int) or phase_index < 0:
        raise MigrationJournalError("migration_phase_index_invalid")
    if phase_index >= len(lease.phase_manifest):
        raise MigrationJournalError("migration_phase_index_manifest_mismatch")
    expected = lease.phase_manifest[phase_index]
    if phase != expected:
        raise MigrationJournalError("migration_phase_name_manifest_mismatch")


def _identity_matches_lease(
    identity: MigrationExecutionIdentity,
    lease: MigrationExecutionLease,
) -> bool:
    return (
        identity.installation_ref == lease.installation_ref
        and identity.operation_ref == lease.operation_ref
        and identity.plan_hash == lease.plan_hash
        and identity.phase_manifest == lease.phase_manifest
        and identity.phase_manifest_sha256 == lease.phase_manifest_sha256
        and identity.schema_manifest == lease.schema_manifest
        and identity.schema_manifest_sha256 == lease.schema_manifest_sha256
        and identity.grant_id == lease.grant_id
    )


@runtime_checkable
class MigrationExecutionJournal(Protocol):
    """Persistence seam shared by in-memory tests and canonical SQLite."""

    def register_grant(self, identity: MigrationExecutionIdentity) -> None: ...

    def begin(
        self,
        identity: MigrationExecutionIdentity,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MigrationExecutionLease: ...

    def assert_current(self, lease: MigrationExecutionLease, *, now: datetime) -> None: ...

    def record_phase(
        self,
        lease: MigrationExecutionLease,
        *,
        phase_index: int,
        phase: str,
        outcome: str,
        reason_code: str,
        now: datetime,
    ) -> None: ...

    def list_phase_records(self, operation_id: str) -> tuple[MigrationPhaseReceipt, ...]: ...

    def recover_expired(self, *, now: datetime) -> tuple[str, ...]: ...

    def complete(
        self,
        lease: MigrationExecutionLease,
        *,
        outcome: str,
        receipt: dict[str, object],
        now: datetime,
    ) -> None: ...


class InMemoryMigrationExecutionJournal:
    """Thread-safe adapter used by tests and non-production composition."""

    def __init__(self, *, owner_ref: str = "pp-core-memory") -> None:
        self.owner_ref = _require_ref(owner_ref, "migration_lease_owner_invalid")
        self._lock = Lock()
        self._grants: dict[str, MigrationExecutionIdentity] = {}
        self._operations: dict[tuple[str, str], dict[str, object]] = {}
        self._leases: dict[str, MigrationExecutionLease] = {}
        self._generations: dict[str, int] = {}
        self._phases: dict[str, dict[int, MigrationPhaseReceipt]] = {}

    def register_grant(self, identity: MigrationExecutionIdentity) -> None:
        with self._lock:
            if any(
                operation["grant_id"] == identity.grant_id
                for operation in self._operations.values()
            ):
                raise MigrationJournalError("migration_grant_replayed")
            existing = self._grants.get(identity.grant_id)
            if existing is not None and existing != identity:
                raise MigrationJournalError("migration_grant_registration_conflict")
            if existing is not None:
                return
            self._grants[identity.grant_id] = identity

    def begin(
        self,
        identity: MigrationExecutionIdentity,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MigrationExecutionLease:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        lease_expires_at = _require_utc(lease_expires_at, "migration_lease_timestamp_invalid")
        if lease_expires_at <= now:
            raise MigrationJournalError("migration_lease_expiry_invalid")
        with self._lock:
            registered = self._grants.get(identity.grant_id)
            if registered is None:
                raise MigrationJournalError("migration_grant_unissued")
            if registered != identity:
                raise MigrationJournalError("migration_grant_registration_conflict")
            if any(
                operation.get("installation_ref") == identity.installation_ref
                and operation.get("status") == "recovery-required"
                for operation in self._operations.values()
            ):
                raise MigrationJournalError("migration_operation_recovery_required")
            operation_key = (identity.installation_ref, identity.operation_ref)
            existing_operation = self._operations.get(operation_key)
            if existing_operation is not None:
                status = str(existing_operation["status"])
                if status == "running":
                    existing_lease = self._leases.get(identity.installation_ref)
                    if existing_lease is not None and existing_lease.expires_at <= now:
                        existing_operation["status"] = "recovery-required"
                        raise MigrationJournalError("migration_operation_recovery_required")
                    raise MigrationJournalError("migration_operation_active")
                raise MigrationJournalError("migration_operation_replayed")
            for operation in self._operations.values():
                if operation["grant_id"] == identity.grant_id:
                    raise MigrationJournalError("migration_grant_replayed")
            current_lease = self._leases.get(identity.installation_ref)
            if current_lease is not None and current_lease.expires_at > now:
                raise MigrationJournalError("migration_operation_active")
            generation = self._generations.get(identity.installation_ref, 0) + 1
            self._generations[identity.installation_ref] = generation
            lease = MigrationExecutionLease(
                operation_id=_operation_id(identity),
                installation_ref=identity.installation_ref,
                operation_ref=identity.operation_ref,
                plan_hash=identity.plan_hash,
                phase_manifest=identity.phase_manifest,
                phase_manifest_sha256=identity.phase_manifest_sha256,
                schema_manifest=identity.schema_manifest,
                schema_manifest_sha256=identity.schema_manifest_sha256,
                grant_id=identity.grant_id,
                owner_ref=self.owner_ref,
                fencing_generation=generation,
                expires_at=lease_expires_at,
            )
            self._leases[identity.installation_ref] = lease
            self._operations[operation_key] = {
                "operation_id": lease.operation_id,
                "installation_ref": identity.installation_ref,
                "grant_id": identity.grant_id,
                "identity": identity,
                "status": "running",
                "fencing_generation": generation,
                "receipt": None,
            }
            return lease

    def assert_current(self, lease: MigrationExecutionLease, *, now: datetime) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        with self._lock:
            current = self._leases.get(lease.installation_ref)
            operation = self._operations.get((lease.installation_ref, lease.operation_ref))
            if (
                current != lease
                or current.expires_at <= now
                or operation is None
                or operation["status"] != "running"
                or not isinstance(operation.get("identity"), MigrationExecutionIdentity)
                or not _identity_matches_lease(operation["identity"], lease)
            ):
                raise MigrationJournalError("migration_operation_fence_lost")

    def record_phase(
        self,
        lease: MigrationExecutionLease,
        *,
        phase_index: int,
        phase: str,
        outcome: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        _validate_phase_binding(lease, phase_index=phase_index, phase=phase)
        receipt = MigrationPhaseReceipt(
            operation_id=lease.operation_id,
            phase_index=phase_index,
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            phase_manifest_sha256=lease.phase_manifest_sha256,
            schema_manifest_sha256=lease.schema_manifest_sha256,
            completed_at=now,
        )
        with self._lock:
            current = self._leases.get(lease.installation_ref)
            operation = self._operations.get((lease.installation_ref, lease.operation_ref))
            if (
                current != lease
                or current.expires_at <= now
                or operation is None
                or operation["status"] != "running"
                or not isinstance(operation.get("identity"), MigrationExecutionIdentity)
                or not _identity_matches_lease(operation["identity"], lease)
            ):
                raise MigrationJournalError("migration_operation_fence_lost")
            phases = self._phases.setdefault(lease.operation_id, {})
            existing = phases.get(phase_index)
            if existing is not None and existing != receipt:
                raise MigrationJournalError("migration_phase_registration_conflict")
            phases[phase_index] = receipt

    def list_phase_records(self, operation_id: str) -> tuple[MigrationPhaseReceipt, ...]:
        _require_ref(operation_id, "migration_operation_id_invalid")
        with self._lock:
            return tuple(
                self._phases.get(operation_id, {}).get(index)
                for index in sorted(self._phases.get(operation_id, {}))
            )

    def recover_expired(self, *, now: datetime) -> tuple[str, ...]:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        recovered: list[str] = []
        with self._lock:
            for operation in self._operations.values():
                if operation["status"] != "running":
                    continue
                lease = self._leases.get(str(operation["installation_ref"]))
                if lease is not None and lease.expires_at <= now:
                    operation["status"] = "recovery-required"
                    recovered.append(str(operation["operation_id"]))
        return tuple(sorted(recovered))

    def complete(
        self,
        lease: MigrationExecutionLease,
        *,
        outcome: str,
        receipt: dict[str, object],
        now: datetime,
    ) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        if outcome not in {"applied", "rejected", "rolled-back", "recovery-required"}:
            raise MigrationJournalError("migration_receipt_outcome_invalid")
        with self._lock:
            current = self._leases.get(lease.installation_ref)
            operation = self._operations.get((lease.installation_ref, lease.operation_ref))
            if (
                current != lease
                or lease.expires_at <= now
                or operation is None
                or operation["status"] != "running"
            ):
                raise MigrationJournalError("migration_operation_fence_lost")
            operation["status"] = outcome
            operation["receipt"] = json.loads(_receipt_json(receipt))
            self._leases[lease.installation_ref] = MigrationExecutionLease(
                operation_id=lease.operation_id,
                installation_ref=lease.installation_ref,
                operation_ref=lease.operation_ref,
                plan_hash=lease.plan_hash,
                phase_manifest=lease.phase_manifest,
                phase_manifest_sha256=lease.phase_manifest_sha256,
                schema_manifest=lease.schema_manifest,
                schema_manifest_sha256=lease.schema_manifest_sha256,
                grant_id=lease.grant_id,
                owner_ref=lease.owner_ref,
                fencing_generation=lease.fencing_generation,
                expires_at=now,
            )


class SQLiteMigrationExecutionJournal:
    """Canonical SQLite adapter with transactional lease and receipt CAS."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        owner_ref: str,
        initialize_schema: bool = False,
        require_existing: bool = False,
    ) -> None:
        try:
            self.database_path = Path(database_path)
        except TypeError as exc:
            raise MigrationJournalError("migration_journal_database_path_invalid") from exc
        self.owner_ref = _require_ref(owner_ref, "migration_lease_owner_invalid")
        if not isinstance(require_existing, bool):
            raise MigrationJournalError("migration_journal_existing_flag_invalid")
        self._require_existing = require_existing
        if self._require_existing:
            if not self.database_path.is_absolute():
                raise MigrationJournalError("migration_journal_database_path_not_absolute")
            if not self.database_path.is_file():
                raise MigrationJournalError("migration_journal_database_missing")
        if initialize_schema:
            if self._require_existing:
                raise MigrationJournalError("migration_journal_schema_initialization_forbidden")
            self.initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        database: str | Path = self.database_path
        uri = False
        if self._require_existing:
            database = f"{self.database_path.as_uri()}?mode=rw"
            uri = True
        connection = sqlite3.connect(
            database,
            timeout=30,
            isolation_level=None,
            uri=uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @classmethod
    def open_existing(
        cls,
        database_path: str | Path,
        *,
        owner_ref: str,
    ) -> SQLiteMigrationExecutionJournal:
        """Open an already migrated canonical database without create fallback."""

        journal = cls(
            database_path,
            owner_ref=owner_ref,
            require_existing=True,
        )
        try:
            with closing(journal._connect()) as connection:
                journal._require_schema(connection)
        except MigrationJournalError:
            raise
        except sqlite3.Error as exc:
            raise MigrationJournalError("migration_journal_database_unavailable") from exc
        return journal

    def initialize_schema(self) -> None:
        """Explicitly install journal tables for tests or controlled composition."""

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                apply_migration_journal_schema(connection)
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if not migration_journal_schema_present(connection):
            raise MigrationJournalError("migration_journal_schema_missing")

    def register_grant(self, identity: MigrationExecutionIdentity) -> None:
        now_text = _utc(identity.grant_issued_at)
        phase_manifest_json = _manifest_json(
            identity.phase_manifest,
            "migration_phase_manifest_invalid",
        )
        schema_manifest_json = _manifest_json(
            identity.schema_manifest,
            "migration_schema_manifest_invalid",
        )
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM pp_migration_grants WHERE grant_id = ?",
                    (identity.grant_id,),
                ).fetchone()
                expected = (
                    identity.installation_ref,
                    identity.operation_ref,
                    identity.plan_hash,
                    phase_manifest_json,
                    identity.phase_manifest_sha256,
                    schema_manifest_json,
                    identity.schema_manifest_sha256,
                    _utc(identity.grant_issued_at),
                    _utc(identity.grant_expires_at),
                )
                if existing is not None:
                    actual = (
                        str(existing["installation_ref"]),
                        str(existing["operation_ref"]),
                        str(existing["plan_hash"]),
                        str(existing["phase_manifest_json"]),
                        str(existing["phase_manifest_sha256"]),
                        str(existing["schema_manifest_json"]),
                        str(existing["schema_manifest_sha256"]),
                        str(existing["issued_at"]),
                        str(existing["expires_at"]),
                    )
                    if actual != expected:
                        raise MigrationJournalError("migration_grant_registration_conflict")
                    if str(existing["status"]) != "available":
                        raise MigrationJournalError("migration_grant_replayed")
                else:
                    connection.execute(
                        """
                        INSERT INTO pp_migration_grants (
                            grant_id, installation_ref, operation_ref, plan_hash,
                            phase_manifest_json, phase_manifest_sha256,
                            schema_manifest_json, schema_manifest_sha256,
                            issued_at, expires_at, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?)
                        """,
                        (identity.grant_id, *expected, now_text, now_text),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def begin(
        self,
        identity: MigrationExecutionIdentity,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MigrationExecutionLease:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        lease_expires_at = _require_utc(lease_expires_at, "migration_lease_timestamp_invalid")
        if lease_expires_at <= now:
            raise MigrationJournalError("migration_lease_expiry_invalid")
        now_text = _utc(now)
        operation_id = _operation_id(identity)
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                grant = connection.execute(
                    "SELECT * FROM pp_migration_grants WHERE grant_id = ?",
                    (identity.grant_id,),
                ).fetchone()
                if grant is None:
                    raise MigrationJournalError("migration_grant_unissued")
                if (
                    str(grant["installation_ref"]) != identity.installation_ref
                    or str(grant["operation_ref"]) != identity.operation_ref
                    or str(grant["plan_hash"]) != identity.plan_hash
                    or str(grant["phase_manifest_json"])
                    != _manifest_json(
                        identity.phase_manifest,
                        "migration_phase_manifest_invalid",
                    )
                    or str(grant["phase_manifest_sha256"]) != identity.phase_manifest_sha256
                    or str(grant["schema_manifest_json"])
                    != _manifest_json(
                        identity.schema_manifest,
                        "migration_schema_manifest_invalid",
                    )
                    or str(grant["schema_manifest_sha256"]) != identity.schema_manifest_sha256
                    or str(grant["issued_at"]) != _utc(identity.grant_issued_at)
                    or str(grant["expires_at"]) != _utc(identity.grant_expires_at)
                ):
                    raise MigrationJournalError("migration_grant_registration_conflict")
                if str(grant["status"]) != "available":
                    raise MigrationJournalError("migration_grant_replayed")
                if not (_parse_utc(grant["issued_at"]) <= now < _parse_utc(grant["expires_at"])):
                    raise MigrationJournalError("migration_grant_invalid")

                unresolved = connection.execute(
                    "SELECT operation_id FROM pp_migration_operations "
                    "WHERE installation_ref=? AND status='recovery-required' LIMIT 1",
                    (identity.installation_ref,),
                ).fetchone()
                if unresolved is not None:
                    raise MigrationJournalError("migration_operation_recovery_required")

                operation = connection.execute(
                    "SELECT * FROM pp_migration_operations "
                    "WHERE installation_ref = ? AND operation_ref = ?",
                    (identity.installation_ref, identity.operation_ref),
                ).fetchone()
                if operation is not None:
                    status = str(operation["status"])
                    if status == "running" and _parse_utc(operation["lease_expires_at"]) <= now:
                        connection.execute(
                            "UPDATE pp_migration_operations SET status = 'recovery-required', "
                            "updated_at = ? WHERE operation_id = ? AND status = 'running'",
                            (now_text, str(operation["operation_id"])),
                        )
                        connection.commit()
                        raise MigrationJournalError("migration_operation_recovery_required")
                    if status == "running":
                        raise MigrationJournalError("migration_operation_active")
                    raise MigrationJournalError("migration_operation_replayed")

                current = connection.execute(
                    "SELECT * FROM pp_migration_installation_leases WHERE installation_ref = ?",
                    (identity.installation_ref,),
                ).fetchone()
                generation = 1
                if current is not None:
                    generation = int(current["fencing_generation"]) + 1
                    if _parse_utc(current["lease_expires_at"]) > now:
                        raise MigrationJournalError("migration_operation_active")
                    linked = connection.execute(
                        "SELECT status FROM pp_migration_operations WHERE operation_id = ?",
                        (str(current["operation_id"]),),
                    ).fetchone()
                    if linked is not None and str(linked["status"]) == "running":
                        connection.execute(
                            "UPDATE pp_migration_operations SET status = 'recovery-required', "
                            "updated_at = ? WHERE operation_id = ? AND status = 'running'",
                            (now_text, str(current["operation_id"])),
                        )
                        connection.commit()
                        raise MigrationJournalError("migration_operation_recovery_required")

                connection.execute(
                    """
                    INSERT INTO pp_migration_operations (
                        operation_id, installation_ref, operation_ref, plan_hash,
                        phase_manifest_json, phase_manifest_sha256,
                        schema_manifest_json, schema_manifest_sha256,
                        grant_id, status, fencing_generation, lease_owner_ref,
                        lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        identity.installation_ref,
                        identity.operation_ref,
                        identity.plan_hash,
                        _manifest_json(
                            identity.phase_manifest,
                            "migration_phase_manifest_invalid",
                        ),
                        identity.phase_manifest_sha256,
                        _manifest_json(
                            identity.schema_manifest,
                            "migration_schema_manifest_invalid",
                        ),
                        identity.schema_manifest_sha256,
                        identity.grant_id,
                        generation,
                        self.owner_ref,
                        _utc(lease_expires_at),
                        now_text,
                        now_text,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO pp_migration_installation_leases (
                        installation_ref, operation_id, fencing_generation,
                        lease_owner_ref, lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(installation_ref) DO UPDATE SET
                        operation_id = excluded.operation_id,
                        fencing_generation = excluded.fencing_generation,
                        lease_owner_ref = excluded.lease_owner_ref,
                        lease_expires_at = excluded.lease_expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity.installation_ref,
                        operation_id,
                        generation,
                        self.owner_ref,
                        _utc(lease_expires_at),
                        now_text,
                    ),
                )
                changed = connection.execute(
                    "UPDATE pp_migration_grants SET status = 'consumed', operation_id = ?, "
                    "updated_at = ? WHERE grant_id = ? AND status = 'available'",
                    (operation_id, now_text, identity.grant_id),
                ).rowcount
                if changed != 1:
                    raise MigrationJournalError("migration_grant_replayed")
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return MigrationExecutionLease(
            operation_id=operation_id,
            installation_ref=identity.installation_ref,
            operation_ref=identity.operation_ref,
            plan_hash=identity.plan_hash,
            phase_manifest=identity.phase_manifest,
            phase_manifest_sha256=identity.phase_manifest_sha256,
            schema_manifest=identity.schema_manifest,
            schema_manifest_sha256=identity.schema_manifest_sha256,
            grant_id=identity.grant_id,
            owner_ref=self.owner_ref,
            fencing_generation=generation,
            expires_at=lease_expires_at,
        )

    def assert_current(self, lease: MigrationExecutionLease, *, now: datetime) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            row = connection.execute(
                """
                SELECT operation_id, fencing_generation, lease_owner_ref, lease_expires_at
                FROM pp_migration_installation_leases WHERE installation_ref = ?
                """,
                (lease.installation_ref,),
            ).fetchone()
            operation = connection.execute(
                "SELECT status, plan_hash, phase_manifest_json, phase_manifest_sha256, "
                "schema_manifest_json, schema_manifest_sha256 "
                "FROM pp_migration_operations WHERE operation_id = ?",
                (lease.operation_id,),
            ).fetchone()
        if (
            row is None
            or operation is None
            or str(row["operation_id"]) != lease.operation_id
            or int(row["fencing_generation"]) != lease.fencing_generation
            or str(row["lease_owner_ref"]) != lease.owner_ref
            or _parse_utc(row["lease_expires_at"]) <= now
            or str(operation["status"]) != "running"
            or str(operation["plan_hash"]) != lease.plan_hash
            or str(operation["phase_manifest_json"])
            != _manifest_json(lease.phase_manifest, "migration_phase_manifest_invalid")
            or str(operation["phase_manifest_sha256"]) != lease.phase_manifest_sha256
            or str(operation["schema_manifest_json"])
            != _manifest_json(lease.schema_manifest, "migration_schema_manifest_invalid")
            or str(operation["schema_manifest_sha256"]) != lease.schema_manifest_sha256
        ):
            raise MigrationJournalError("migration_operation_fence_lost")

    def record_phase(
        self,
        lease: MigrationExecutionLease,
        *,
        phase_index: int,
        phase: str,
        outcome: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        _validate_phase_binding(lease, phase_index=phase_index, phase=phase)
        receipt = MigrationPhaseReceipt(
            operation_id=lease.operation_id,
            phase_index=phase_index,
            phase=phase,
            outcome=outcome,
            reason_code=reason_code,
            phase_manifest_sha256=lease.phase_manifest_sha256,
            schema_manifest_sha256=lease.schema_manifest_sha256,
            completed_at=now,
        )
        now_text = _utc(now)
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT operation_id, fencing_generation, lease_owner_ref, lease_expires_at "
                    "FROM pp_migration_installation_leases WHERE installation_ref = ?",
                    (lease.installation_ref,),
                ).fetchone()
                operation = connection.execute(
                    "SELECT status, plan_hash, phase_manifest_json, phase_manifest_sha256, "
                    "schema_manifest_json, schema_manifest_sha256 "
                    "FROM pp_migration_operations WHERE operation_id = ?",
                    (lease.operation_id,),
                ).fetchone()
                if (
                    current is None
                    or operation is None
                    or str(current["operation_id"]) != lease.operation_id
                    or int(current["fencing_generation"]) != lease.fencing_generation
                    or str(current["lease_owner_ref"]) != lease.owner_ref
                    or _parse_utc(current["lease_expires_at"]) <= now
                    or str(operation["status"]) != "running"
                    or str(operation["plan_hash"]) != lease.plan_hash
                    or str(operation["phase_manifest_json"])
                    != _manifest_json(
                        lease.phase_manifest,
                        "migration_phase_manifest_invalid",
                    )
                    or str(operation["phase_manifest_sha256"]) != lease.phase_manifest_sha256
                    or str(operation["schema_manifest_json"])
                    != _manifest_json(
                        lease.schema_manifest,
                        "migration_schema_manifest_invalid",
                    )
                    or str(operation["schema_manifest_sha256"]) != lease.schema_manifest_sha256
                ):
                    raise MigrationJournalError("migration_operation_fence_lost")
                existing = connection.execute(
                    "SELECT * FROM pp_migration_phase_records WHERE operation_id = ? AND phase_index = ?",
                    (lease.operation_id, phase_index),
                ).fetchone()
                if existing is not None:
                    actual = MigrationPhaseReceipt(
                        operation_id=str(existing["operation_id"]),
                        phase_index=int(existing["phase_index"]),
                        phase=str(existing["phase"]),
                        outcome=str(existing["outcome"]),
                        reason_code=str(existing["reason_code"]),
                        phase_manifest_sha256=str(existing["phase_manifest_sha256"]),
                        schema_manifest_sha256=str(existing["schema_manifest_sha256"]),
                        completed_at=_parse_utc(existing["completed_at"]),
                    )
                    if actual != receipt:
                        raise MigrationJournalError("migration_phase_registration_conflict")
                else:
                    connection.execute(
                        "INSERT INTO pp_migration_phase_records "
                        "(operation_id, phase_index, phase, outcome, reason_code, "
                        "phase_manifest_sha256, schema_manifest_sha256, completed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            receipt.operation_id,
                            receipt.phase_index,
                            receipt.phase,
                            receipt.outcome,
                            receipt.reason_code,
                            receipt.phase_manifest_sha256,
                            receipt.schema_manifest_sha256,
                            now_text,
                        ),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def list_phase_records(self, operation_id: str) -> tuple[MigrationPhaseReceipt, ...]:
        _require_ref(operation_id, "migration_operation_id_invalid")
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT operation_id, phase_index, phase, outcome, reason_code, "
                "phase_manifest_sha256, schema_manifest_sha256, completed_at "
                "FROM pp_migration_phase_records WHERE operation_id = ? ORDER BY phase_index",
                (operation_id,),
            ).fetchall()
        return tuple(
            MigrationPhaseReceipt(
                operation_id=str(row["operation_id"]),
                phase_index=int(row["phase_index"]),
                phase=str(row["phase"]),
                outcome=str(row["outcome"]),
                reason_code=str(row["reason_code"]),
                phase_manifest_sha256=str(row["phase_manifest_sha256"]),
                schema_manifest_sha256=str(row["schema_manifest_sha256"]),
                completed_at=_parse_utc(row["completed_at"]),
            )
            for row in rows
        )

    def recover_expired(self, *, now: datetime) -> tuple[str, ...]:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        now_text = _utc(now)
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT operation_id FROM pp_migration_operations "
                    "WHERE status = 'running' AND lease_expires_at <= ? ORDER BY operation_id",
                    (now_text,),
                ).fetchall()
                ids = tuple(str(row["operation_id"]) for row in rows)
                if ids:
                    connection.execute(
                        "UPDATE pp_migration_operations SET status = 'recovery-required', updated_at = ? "
                        "WHERE status = 'running' AND lease_expires_at <= ?",
                        (now_text, now_text),
                    )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return ids

    def complete(
        self,
        lease: MigrationExecutionLease,
        *,
        outcome: str,
        receipt: dict[str, object],
        now: datetime,
    ) -> None:
        now = _require_utc(now, "migration_journal_timestamp_invalid")
        if outcome not in {"applied", "rejected", "rolled-back", "recovery-required"}:
            raise MigrationJournalError("migration_receipt_outcome_invalid")
        receipt_json = _receipt_json(receipt)
        now_text = _utc(now)
        with closing(self._connect()) as connection:
            self._require_schema(connection)
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    """
                    UPDATE pp_migration_operations
                    SET status = ?, receipt_json = ?, lease_expires_at = ?, updated_at = ?
                    WHERE operation_id = ? AND status = 'running'
                      AND fencing_generation = ? AND lease_owner_ref = ?
                      AND plan_hash = ?
                      AND phase_manifest_json = ? AND phase_manifest_sha256 = ?
                      AND schema_manifest_json = ? AND schema_manifest_sha256 = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        outcome,
                        receipt_json,
                        now_text,
                        now_text,
                        lease.operation_id,
                        lease.fencing_generation,
                        lease.owner_ref,
                        lease.plan_hash,
                        _manifest_json(
                            lease.phase_manifest,
                            "migration_phase_manifest_invalid",
                        ),
                        lease.phase_manifest_sha256,
                        _manifest_json(
                            lease.schema_manifest,
                            "migration_schema_manifest_invalid",
                        ),
                        lease.schema_manifest_sha256,
                        now_text,
                    ),
                ).rowcount
                lease_changed = connection.execute(
                    """
                    UPDATE pp_migration_installation_leases
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE installation_ref = ? AND operation_id = ?
                      AND fencing_generation = ? AND lease_owner_ref = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        now_text,
                        now_text,
                        lease.installation_ref,
                        lease.operation_id,
                        lease.fencing_generation,
                        lease.owner_ref,
                        now_text,
                    ),
                ).rowcount
                if changed != 1 or lease_changed != 1:
                    raise MigrationJournalError("migration_operation_fence_lost")
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise


__all__ = [
    "DEFAULT_MIGRATION_LEASE_SECONDS",
    "MIGRATION_JOURNAL_SCHEMA_VERSION",
    "InMemoryMigrationExecutionJournal",
    "MigrationExecutionIdentity",
    "MigrationExecutionJournal",
    "MigrationExecutionLease",
    "MigrationPhaseReceipt",
    "MigrationJournalError",
    "SQLiteMigrationExecutionJournal",
    "apply_migration_journal_schema",
    "migration_journal_schema_present",
]
