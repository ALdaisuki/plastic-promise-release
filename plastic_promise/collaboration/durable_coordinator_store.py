"""Verify-only SQLite adapter for durable coordinator audit authority.

Deployment owns the schema.  This module accepts the canonical server SQLite
connection and its single-writer transaction factory, verifies the complete
table/index/trigger manifest without mutating it, and persists append-only
coordinator audit generations, their CAS-updated current head, and one durable
consumption record per source activity update.

The adapter never opens another connection and never installs or migrates
schema.  Canonical JSON and denormalized columns are verified on every read so
a portable receipt cannot become dispatch evidence by shape alone.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .activity_update import ActivityAuditRepository, ActivityContractError
from .canonical_time import canonical_text
from .contracts import ProjectScope
from .coordinator_supervisor import (
    _COORDINATOR_RECEIPT_ISSUER,
    _COORDINATOR_RECEIPT_SCHEMA,
    _SERVER_COORDINATOR_AUTHORITY_TOKEN,
    AUDIT_STATUSES,
    EVIDENCE_KINDS,
    CoordinatorActivityAuditReceipt,
    CoordinatorAuditConsumption,
    CoordinatorAuditError,
    CoordinatorAuditRecord,
    _coordinator_authority_id,
    _digest,
    _identifier,
    _require_exact_coordinator_record,
    _require_same_coordinator_receipt,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


DURABLE_COORDINATOR_SCHEMA_REVISION = "collaboration-coordinator-audit/sqlite-v1"

DURABLE_COORDINATOR_REQUIRED_TABLES = (
    "collaboration_coordinator_audit_schema",
    "collaboration_coordinator_audits",
    "collaboration_coordinator_audit_heads",
    "collaboration_coordinator_audit_consumptions",
)
DURABLE_COORDINATOR_REQUIRED_INDEXES = (
    "idx_collaboration_coordinator_audits_scope_generation",
    "idx_collaboration_coordinator_consumptions_receipt",
)
DURABLE_COORDINATOR_REQUIRED_TRIGGERS = (
    "collaboration_coordinator_audits_no_update",
    "collaboration_coordinator_audits_no_delete",
    "collaboration_coordinator_audit_heads_no_delete",
    "collaboration_coordinator_audit_heads_identity_immutable",
    "collaboration_coordinator_audit_heads_generation_step",
    "collaboration_coordinator_consumptions_no_update",
    "collaboration_coordinator_consumptions_no_delete",
)

(
    _SCHEMA_TABLE,
    _AUDIT_TABLE,
    _HEAD_TABLE,
    _CONSUMPTION_TABLE,
) = DURABLE_COORDINATOR_REQUIRED_TABLES
(
    _AUDIT_SCOPE_INDEX,
    _CONSUMPTION_RECEIPT_INDEX,
) = DURABLE_COORDINATOR_REQUIRED_INDEXES

_TABLE_COLUMNS: dict[
    str,
    tuple[tuple[str, str, bool, object, int], ...],
] = {
    _SCHEMA_TABLE: (
        ("singleton", "INTEGER", False, None, 1),
        ("schema_revision", "TEXT", True, None, 0),
        ("installed_at_utc", "TEXT", True, None, 0),
    ),
    _AUDIT_TABLE: (
        ("coordinator_audit_receipt_sha256", "TEXT", False, None, 1),
        ("receipt_id", "TEXT", True, None, 0),
        ("authority_id", "TEXT", True, None, 0),
        ("project_id", "TEXT", True, None, 0),
        ("coordination_session_id", "TEXT", True, None, 0),
        ("activity_update_sha256", "TEXT", True, None, 0),
        ("activity_receipt_sha256", "TEXT", True, None, 0),
        ("audit_generation", "INTEGER", True, None, 0),
        ("status", "TEXT", True, None, 0),
        ("completion_verified", "INTEGER", True, None, 0),
        ("recorded_at_utc", "TEXT", True, None, 0),
        ("receipt_json", "TEXT", True, None, 0),
    ),
    _HEAD_TABLE: (
        ("project_id", "TEXT", True, None, 1),
        ("coordination_session_id", "TEXT", True, None, 2),
        ("activity_update_sha256", "TEXT", True, None, 3),
        ("current_generation", "INTEGER", True, None, 0),
        ("current_receipt_id", "TEXT", True, None, 0),
        ("current_receipt_sha256", "TEXT", True, None, 0),
        ("updated_at_utc", "TEXT", True, None, 0),
    ),
    _CONSUMPTION_TABLE: (
        ("activity_update_sha256", "TEXT", False, None, 1),
        ("receipt_id", "TEXT", True, None, 0),
        ("receipt_sha256", "TEXT", True, None, 0),
        ("audit_generation", "INTEGER", True, None, 0),
        ("consumed_at_utc", "TEXT", True, None, 0),
    ),
}

_INDEXES = {
    _AUDIT_SCOPE_INDEX: (
        _AUDIT_TABLE,
        (
            "project_id",
            "coordination_session_id",
            "activity_update_sha256",
            "audit_generation",
        ),
    ),
    _CONSUMPTION_RECEIPT_INDEX: (
        _CONSUMPTION_TABLE,
        ("receipt_id",),
    ),
}

_TRIGGERS = {
    "collaboration_coordinator_audits_no_update": (
        _AUDIT_TABLE,
        "before update on",
        "collaboration_coordinator_audit_append_only",
    ),
    "collaboration_coordinator_audits_no_delete": (
        _AUDIT_TABLE,
        "before delete on",
        "collaboration_coordinator_audit_append_only",
    ),
    "collaboration_coordinator_audit_heads_no_delete": (
        _HEAD_TABLE,
        "before delete on",
        "collaboration_coordinator_audit_head_no_delete",
    ),
    "collaboration_coordinator_audit_heads_identity_immutable": (
        _HEAD_TABLE,
        "before update on",
        "collaboration_coordinator_audit_head_identity_immutable",
    ),
    "collaboration_coordinator_audit_heads_generation_step": (
        _HEAD_TABLE,
        "before update on",
        "collaboration_coordinator_audit_head_generation_invalid",
    ),
    "collaboration_coordinator_consumptions_no_update": (
        _CONSUMPTION_TABLE,
        "before update on",
        "collaboration_coordinator_consumption_append_only",
    ),
    "collaboration_coordinator_consumptions_no_delete": (
        _CONSUMPTION_TABLE,
        "before delete on",
        "collaboration_coordinator_consumption_append_only",
    ),
}

_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "issuer",
        "receipt_id",
        "authority_id",
        "audit_generation",
        "activity_receipt",
        "activity_receipt_sha256",
        "activity_narrative",
        "status",
        "evidence_lineage",
        "reason_codes",
        "completion_verified",
        "authority_effect",
        "tool_policy_effect",
        "canonical_memory_effect",
        "merge_effect",
        "deploy_effect",
        "persistence_effect",
        "promotion_effect",
        "verification",
    }
)


class DurableCoordinatorRepository:
    """Canonical SQLite adapter satisfying ``CoordinatorAuditRepository``."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        activity_repository: ActivityAuditRepository,
        transaction_factory: Callable[[], AbstractContextManager[None]],
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise CoordinatorAuditError("coordinator_durable_connection_invalid")
        if not isinstance(activity_repository, ActivityAuditRepository):
            raise CoordinatorAuditError("coordinator_durable_activity_repository_invalid")
        if not callable(transaction_factory):
            raise CoordinatorAuditError("coordinator_durable_writer_required")
        self._connection = connection
        self._activity_repository = activity_repository
        self._transaction_factory = transaction_factory
        self._verify_schema()

    def load_by_receipt_id(
        self,
        receipt_id: str,
    ) -> CoordinatorAuditRecord | None:
        normalized = _coordinator_receipt_id(receipt_id)
        row = self._fetchone(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE receipt_id=?",
            (normalized,),
        )
        if row is None:
            return None
        record = self._record_from_row(row)
        if record.receipt.receipt_id != normalized:
            raise CoordinatorAuditError("coordinator_durable_lookup_mismatch")
        return record

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        activity_update_sha256: str,
    ) -> CoordinatorAuditRecord | None:
        project, coordination, update_digest = _head_key(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            activity_update_sha256=activity_update_sha256,
        )
        head = self._fetchone(
            f"""
            SELECT * FROM {_HEAD_TABLE}
             WHERE project_id=?
               AND coordination_session_id=?
               AND activity_update_sha256=?
            """,
            (project, coordination, update_digest),
        )
        if head is None:
            return None
        record = self.load_by_receipt_id(str(head["current_receipt_id"]))
        if record is None:
            raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        updated_at = _canonical_timestamp(
            head["updated_at_utc"],
            "coordinator_durable_record_corrupt",
        )
        expected = {
            "project_id": record.project.project_id,
            "coordination_session_id": record.coordination_session_id,
            "activity_update_sha256": record.receipt.activity_update_sha256,
            "current_generation": record.receipt.audit_generation,
            "current_receipt_id": record.receipt.receipt_id,
            "current_receipt_sha256": record.receipt.content_sha256,
        }
        for column, value in expected.items():
            if head[column] != value:
                raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        if updated_at != record.recorded_at_utc:
            raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        return record

    def append_generation(
        self,
        record: CoordinatorAuditRecord,
        *,
        expected_generation: int,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditRecord:
        if _authority_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_audit_repository_write_authority_required")
        record.validate_integrity()
        prior_generation = _prior_generation(expected_generation)
        if record.receipt.audit_generation != prior_generation + 1:
            raise CoordinatorAuditError("coordinator_audit_generation_invalid")

        existing = self.load_by_receipt_id(record.receipt.receipt_id)
        if existing is not None:
            _require_same_coordinator_receipt(existing, record)
            return existing

        receipt = record.receipt
        try:
            with self._transaction_factory():
                self._connection.execute(
                    f"""
                    INSERT INTO {_AUDIT_TABLE} (
                        coordinator_audit_receipt_sha256, receipt_id, authority_id,
                        project_id, coordination_session_id,
                        activity_update_sha256, activity_receipt_sha256,
                        audit_generation, status, completion_verified,
                        recorded_at_utc, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.content_sha256,
                        receipt.receipt_id,
                        receipt.authority_id,
                        record.project.project_id,
                        record.coordination_session_id,
                        receipt.activity_update_sha256,
                        receipt.activity_receipt.content_sha256,
                        receipt.audit_generation,
                        receipt.status,
                        int(receipt.completion_verified),
                        record.recorded_at_utc,
                        receipt.canonical_json(),
                    ),
                )
                changed = self._advance_head(
                    record,
                    expected_generation=prior_generation,
                )
                if changed != 1:
                    raise CoordinatorAuditError("coordinator_audit_generation_conflict")
        except sqlite3.IntegrityError as exc:
            winner = self.load_by_receipt_id(receipt.receipt_id)
            if winner is not None:
                _require_same_coordinator_receipt(winner, record)
                return winner
            raise CoordinatorAuditError("coordinator_durable_append_conflict") from exc

        stored = self.load_by_receipt_id(receipt.receipt_id)
        if stored is None:
            raise CoordinatorAuditError("coordinator_durable_append_missing")
        _require_exact_coordinator_record(stored, record)
        current = self.load_current(
            project_id=record.project.project_id,
            coordination_session_id=record.coordination_session_id,
            activity_update_sha256=receipt.activity_update_sha256,
        )
        if current is None or current.receipt.receipt_id != receipt.receipt_id:
            raise CoordinatorAuditError("coordinator_durable_head_missing")
        return stored

    def load_consumption(
        self,
        activity_update_sha256: str,
    ) -> CoordinatorAuditConsumption | None:
        update_digest = _digest(
            activity_update_sha256,
            "coordinator_consumption_activity_digest_invalid",
        )
        row = self._fetchone(
            f"SELECT * FROM {_CONSUMPTION_TABLE} WHERE activity_update_sha256=?",
            (update_digest,),
        )
        if row is None:
            return None
        try:
            consumption = CoordinatorAuditConsumption(
                receipt_id=str(row["receipt_id"]),
                receipt_sha256=str(row["receipt_sha256"]),
                activity_update_sha256=str(row["activity_update_sha256"]),
                audit_generation=int(row["audit_generation"]),
                consumed_at_utc=str(row["consumed_at_utc"]),
            )
            consumption.validate_integrity()
        except (TypeError, ValueError, CoordinatorAuditError) as exc:
            raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc
        if not hmac.compare_digest(consumption.activity_update_sha256, update_digest):
            raise CoordinatorAuditError("coordinator_durable_lookup_mismatch")
        record = self.load_by_receipt_id(consumption.receipt_id)
        if record is None or (
            not hmac.compare_digest(
                record.receipt.activity_update_sha256,
                consumption.activity_update_sha256,
            )
            or not hmac.compare_digest(
                record.receipt.content_sha256,
                consumption.receipt_sha256,
            )
            or record.receipt.audit_generation != consumption.audit_generation
        ):
            raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        return consumption

    def consume_current(
        self,
        record: CoordinatorAuditRecord,
        *,
        consumed_at_utc: str,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditConsumption:
        if _authority_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_consumption_write_authority_required")
        record.validate_integrity()
        consumed_at = _canonical_timestamp(
            consumed_at_utc,
            "coordinator_consumption_time_invalid",
        )
        existing = self.load_consumption(record.receipt.activity_update_sha256)
        if existing is not None:
            raise CoordinatorAuditError("coordinator_audit_receipt_replayed")
        try:
            with self._transaction_factory():
                current = self.load_current(
                    project_id=record.project.project_id,
                    coordination_session_id=record.coordination_session_id,
                    activity_update_sha256=record.receipt.activity_update_sha256,
                )
                if current is None or current.receipt.receipt_id != record.receipt.receipt_id:
                    raise CoordinatorAuditError("coordinator_audit_receipt_superseded")
                _require_exact_coordinator_record(current, record)
                self._connection.execute(
                    f"""
                    INSERT INTO {_CONSUMPTION_TABLE} (
                        activity_update_sha256, receipt_id, receipt_sha256,
                        audit_generation, consumed_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.receipt.activity_update_sha256,
                        record.receipt.receipt_id,
                        record.receipt.content_sha256,
                        record.receipt.audit_generation,
                        consumed_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if self.load_consumption(record.receipt.activity_update_sha256) is not None:
                raise CoordinatorAuditError("coordinator_audit_receipt_replayed") from exc
            raise CoordinatorAuditError("coordinator_durable_consumption_conflict") from exc
        stored = self.load_consumption(record.receipt.activity_update_sha256)
        if stored is None:
            raise CoordinatorAuditError("coordinator_durable_consumption_missing")
        return stored

    def _advance_head(
        self,
        record: CoordinatorAuditRecord,
        *,
        expected_generation: int,
    ) -> int:
        receipt = record.receipt
        if expected_generation == 0:
            cursor = self._connection.execute(
                f"""
                INSERT OR IGNORE INTO {_HEAD_TABLE} (
                    project_id, coordination_session_id, activity_update_sha256,
                    current_generation, current_receipt_id,
                    current_receipt_sha256, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.project.project_id,
                    record.coordination_session_id,
                    receipt.activity_update_sha256,
                    receipt.audit_generation,
                    receipt.receipt_id,
                    receipt.content_sha256,
                    record.recorded_at_utc,
                ),
            )
            return cursor.rowcount
        cursor = self._connection.execute(
            f"""
            UPDATE {_HEAD_TABLE}
               SET current_generation=?, current_receipt_id=?,
                   current_receipt_sha256=?, updated_at_utc=?
             WHERE project_id=?
               AND coordination_session_id=?
               AND activity_update_sha256=?
               AND current_generation=?
            """,
            (
                receipt.audit_generation,
                receipt.receipt_id,
                receipt.content_sha256,
                record.recorded_at_utc,
                record.project.project_id,
                record.coordination_session_id,
                receipt.activity_update_sha256,
                expected_generation,
            ),
        )
        return cursor.rowcount

    def _record_from_row(self, row: Mapping[str, object]) -> CoordinatorAuditRecord:
        try:
            receipt = _receipt_from_json(
                str(row["receipt_json"]),
                activity_repository=self._activity_repository,
            )
            project = ProjectScope(row["project_id"])  # type: ignore[arg-type]
            coordination = _identifier(
                row["coordination_session_id"],
                "coordinator_durable_record_corrupt",
            )
            recorded_at = _canonical_timestamp(
                row["recorded_at_utc"],
                "coordinator_durable_record_corrupt",
            )
            expected = {
                "coordinator_audit_receipt_sha256": receipt.content_sha256,
                "receipt_id": receipt.receipt_id,
                "authority_id": receipt.authority_id,
                "project_id": receipt.activity_receipt.scope.project.project_id,
                "coordination_session_id": receipt.activity_receipt.scope.coordination_session_id,
                "activity_update_sha256": receipt.activity_update_sha256,
                "activity_receipt_sha256": receipt.activity_receipt.content_sha256,
                "audit_generation": receipt.audit_generation,
                "status": receipt.status,
                "completion_verified": int(receipt.completion_verified),
            }
            for column, value in expected.items():
                if row[column] != value:
                    raise CoordinatorAuditError("coordinator_durable_record_corrupt")
            if receipt.authority_id != _coordinator_authority_id(
                project,
                coordination,
            ):
                raise CoordinatorAuditError("coordinator_durable_record_corrupt")
            record = CoordinatorAuditRecord(
                project=project,
                coordination_session_id=coordination,
                receipt=receipt,
                recorded_at_utc=recorded_at,
            )
            record.validate_integrity()
            return record
        except CoordinatorAuditError as exc:
            if exc.code.startswith("coordinator_durable_"):
                raise
            raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc
        except (KeyError, TypeError, ValueError, ActivityContractError) as exc:
            raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc

    def _verify_schema(self) -> None:
        foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CoordinatorAuditError("coordinator_durable_foreign_keys_required")
        existing_tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not set(DURABLE_COORDINATOR_REQUIRED_TABLES).issubset(existing_tables):
            raise CoordinatorAuditError("coordinator_durable_schema_missing")
        for table, expected_columns in _TABLE_COLUMNS.items():
            actual = tuple(
                (
                    str(row[1]),
                    str(row[2]).upper(),
                    bool(row[3]),
                    row[4],
                    int(row[5]),
                )
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if actual != expected_columns:
                raise CoordinatorAuditError("coordinator_durable_schema_stale")
        _verify_table_sql(
            self._connection,
            _SCHEMA_TABLE,
            ("check(singleton=1)",),
        )
        _verify_table_sql(
            self._connection,
            _AUDIT_TABLE,
            (
                "check(audit_generation >= 1)",
                "check(status in ('verified', 'mismatch', 'overlap', 'stale', 'blocked'))",
                "check(completion_verified in (0, 1))",
            ),
        )
        _verify_table_sql(
            self._connection,
            _HEAD_TABLE,
            ("check(current_generation >= 1)",),
        )
        _verify_table_sql(
            self._connection,
            _CONSUMPTION_TABLE,
            ("check(audit_generation >= 1)",),
        )
        _verify_primary_key(
            self._connection,
            _AUDIT_TABLE,
            "coordinator_audit_receipt_sha256",
        )
        _verify_composite_primary_key(
            self._connection,
            _HEAD_TABLE,
            ("project_id", "coordination_session_id", "activity_update_sha256"),
        )
        _verify_primary_key(
            self._connection,
            _CONSUMPTION_TABLE,
            "activity_update_sha256",
        )
        _verify_unique_index(self._connection, _AUDIT_TABLE, ("receipt_id",))
        _verify_unique_index(
            self._connection,
            _AUDIT_TABLE,
            (
                "project_id",
                "coordination_session_id",
                "activity_update_sha256",
                "audit_generation",
            ),
        )
        _verify_unique_index(
            self._connection,
            _HEAD_TABLE,
            ("current_receipt_id",),
        )
        _verify_unique_index(
            self._connection,
            _CONSUMPTION_TABLE,
            ("receipt_id",),
        )
        for name, (table, columns) in _INDEXES.items():
            _verify_named_index(self._connection, name, table, columns)
        _verify_foreign_key(
            self._connection,
            table=_AUDIT_TABLE,
            expected=(
                "collaboration_activity_audits",
                "activity_receipt_sha256",
                "audit_receipt_sha256",
                "RESTRICT",
            ),
        )
        _verify_foreign_key(
            self._connection,
            table=_HEAD_TABLE,
            expected=(
                _AUDIT_TABLE,
                "current_receipt_id",
                "receipt_id",
                "RESTRICT",
            ),
        )
        _verify_foreign_key(
            self._connection,
            table=_CONSUMPTION_TABLE,
            expected=(_AUDIT_TABLE, "receipt_id", "receipt_id", "RESTRICT"),
        )
        for name, (table, action, failure) in _TRIGGERS.items():
            _verify_trigger(self._connection, name, table, action, failure)
        _verify_head_identity_trigger(self._connection)
        _verify_head_generation_trigger(self._connection)
        marker = self._fetchone(
            f"SELECT schema_revision, installed_at_utc FROM {_SCHEMA_TABLE} WHERE singleton=1"
        )
        if marker is None:
            raise CoordinatorAuditError("coordinator_durable_schema_missing")
        if marker["schema_revision"] != DURABLE_COORDINATOR_SCHEMA_REVISION:
            raise CoordinatorAuditError("coordinator_durable_schema_stale")
        _canonical_timestamp(
            marker["installed_at_utc"],
            "coordinator_durable_schema_stale",
        )

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> dict[str, Any] | None:
        return _row(self._connection.execute(sql, parameters))


def _receipt_from_json(
    raw: str,
    *,
    activity_repository: object,
) -> CoordinatorActivityAuditReceipt:
    payload = _canonical_mapping(raw)
    if set(payload) != _RECEIPT_FIELDS:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    if (
        payload.get("schema_version") != _COORDINATOR_RECEIPT_SCHEMA
        or payload.get("issuer") != _COORDINATOR_RECEIPT_ISSUER
    ):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    activity_payload = payload.get("activity_receipt")
    if not isinstance(activity_payload, Mapping):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    activity_receipt_id = activity_payload.get("receipt_id")
    if not isinstance(activity_receipt_id, str):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    try:
        activity_record_receipt = _load_activity_receipt(
            activity_repository,
            activity_receipt_id,
        )
    except ActivityContractError as exc:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc
    if activity_record_receipt.to_dict() != dict(activity_payload):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    if payload.get("activity_receipt_sha256") != activity_record_receipt.content_sha256:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    lineage = _lineage(payload.get("evidence_lineage"))
    reasons = _string_tuple(payload.get("reason_codes"))
    status = payload.get("status")
    if status not in AUDIT_STATUSES:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    completion = payload.get("completion_verified")
    if not isinstance(completion, bool):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    receipt = CoordinatorActivityAuditReceipt._rehydrate(
        receipt_id=payload.get("receipt_id"),  # type: ignore[arg-type]
        authority_id=payload.get("authority_id"),  # type: ignore[arg-type]
        audit_generation=payload.get("audit_generation"),  # type: ignore[arg-type]
        activity_receipt=activity_record_receipt,
        status=status,  # type: ignore[arg-type]
        evidence_lineage=lineage,
        reason_codes=reasons,
        completion_verified=completion,
        _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
    )
    if receipt.canonical_json() != raw:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    return receipt


def _load_activity_receipt(
    repository: object,
    receipt_id: str,
):
    load = getattr(repository, "load_by_receipt_id", None)
    if not callable(load):
        raise ActivityContractError("activity_audit_repository_invalid")
    record = load(receipt_id)
    if record is None:
        raise ActivityContractError("activity_audit_receipt_not_server_issued")
    record.validate_integrity()
    return record.receipt


def _lineage(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    lineage: list[tuple[str, str]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"kind", "evidence_sha256"}:
            raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        kind = entry.get("kind")
        digest = entry.get("evidence_sha256")
        if kind not in EVIDENCE_KINDS or not isinstance(digest, str):
            raise CoordinatorAuditError("coordinator_durable_record_corrupt")
        lineage.append((kind, digest))
    return tuple(lineage)  # type: ignore[return-value]


def _head_key(
    *,
    project_id: object,
    coordination_session_id: object,
    activity_update_sha256: object,
) -> tuple[str, str, str]:
    try:
        project = ProjectScope(project_id).project_id
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError("coordinator_project_invalid") from exc
    return (
        project,
        _identifier(
            coordination_session_id,
            "coordinator_coordination_session_invalid",
        ),
        _digest(
            activity_update_sha256,
            "coordinator_activity_update_digest_invalid",
        ),
    )


def _coordinator_receipt_id(value: object) -> str:
    receipt_id = _identifier(value, "coordinator_audit_receipt_id_invalid")
    if not receipt_id.startswith("coordinator-audit:"):
        raise CoordinatorAuditError("coordinator_audit_receipt_id_invalid")
    return receipt_id


def _prior_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoordinatorAuditError("coordinator_audit_expected_generation_invalid")
    return value


def _canonical_timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CoordinatorAuditError(code)
    try:
        rendered = canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError(code) from exc
    if rendered != value:
        raise CoordinatorAuditError(code)
    return rendered


def _canonical_mapping(raw: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != raw:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    return payload


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError("coordinator_durable_record_corrupt") from exc


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CoordinatorAuditError("coordinator_durable_record_corrupt")
    return tuple(value)


def _verify_primary_key(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    matches = [row for row in rows if str(row[1]) == column]
    if len(matches) != 1 or int(matches[0][5]) != 1:
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_table_sql(
    connection: sqlite3.Connection,
    table: str,
    required_tokens: tuple[str, ...],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    normalized = " ".join(str(row[0]).casefold().split())
    if any(token not in normalized for token in required_tokens):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_composite_primary_key(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    actual = tuple(
        name
        for _position, name in sorted((int(row[5]), str(row[1])) for row in rows if int(row[5]) > 0)
    )
    if actual != columns:
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_unique_index(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(row[2]):
            continue
        actual = tuple(
            str(item[2]) for item in connection.execute(f"PRAGMA index_info({row[1]})").fetchall()
        )
        if actual == columns:
            return
    raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_named_index(
    connection: sqlite3.Connection,
    name: str,
    table: str,
    columns: tuple[str, ...],
) -> None:
    row = connection.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    if row is None or str(row[0]) != table:
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    listed = [
        item
        for item in connection.execute(f"PRAGMA index_list({table})").fetchall()
        if str(item[1]) == name
    ]
    if len(listed) != 1 or bool(listed[0][2]):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    actual = tuple(
        str(item[2]) for item in connection.execute(f"PRAGMA index_info({name})").fetchall()
    )
    if actual != columns:
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_foreign_key(
    connection: sqlite3.Connection,
    *,
    table: str,
    expected: tuple[str, str, str, str],
) -> None:
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        actual = (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        if actual == expected:
            return
    raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_trigger(
    connection: sqlite3.Connection,
    name: str,
    table: str,
    action: str,
    failure: str,
) -> None:
    row = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    if row is None or str(row[0]) != table or not isinstance(row[1], str):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    normalized = " ".join(str(row[1]).casefold().split())
    required = (action, table.casefold(), f"raise(abort, '{failure.casefold()}')")
    if any(token not in normalized for token in required):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_head_identity_trigger(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='collaboration_coordinator_audit_heads_identity_immutable'"
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    normalized = " ".join(str(row[0]).casefold().split())
    for column in ("project_id", "coordination_session_id", "activity_update_sha256"):
        if f"old.{column} is not new.{column}" not in normalized:
            raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _verify_head_generation_trigger(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='collaboration_coordinator_audit_heads_generation_step'"
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise CoordinatorAuditError("coordinator_durable_schema_stale")
    normalized = " ".join(str(row[0]).casefold().split())
    if "new.current_generation != old.current_generation + 1" not in normalized:
        raise CoordinatorAuditError("coordinator_durable_schema_stale")


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    raw = cursor.fetchone()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))


__all__ = [
    "DURABLE_COORDINATOR_REQUIRED_INDEXES",
    "DURABLE_COORDINATOR_REQUIRED_TABLES",
    "DURABLE_COORDINATOR_REQUIRED_TRIGGERS",
    "DURABLE_COORDINATOR_SCHEMA_REVISION",
    "DurableCoordinatorRepository",
]
