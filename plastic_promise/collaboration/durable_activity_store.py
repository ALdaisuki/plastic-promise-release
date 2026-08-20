"""Verify-only SQLite adapter for durable Agent activity audit records.

The deployment migration owns every table, index, trigger, and schema marker.
This module owns only the runtime persistence seam: it verifies that the
canonical server connection already has the frozen schema, appends one exact
``AgentActivityUpdate``/``ActivityAuditReceipt`` pair through the caller's
single-writer transaction factory, and reconstructs typed values on reads.

Construction is deliberately read-only.  There is no schema installer or
implicit migration fallback here.  Missing, stale, or weakened schema fails
closed before a portable receipt can be treated as server-issued evidence.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .activity_update import (
    _SERVER_AUTHORITY_TOKEN,
    ACTIVITY_AUDIT_ISSUER,
    ACTIVITY_AUDIT_RECEIPT_SCHEMA,
    ACTIVITY_REDACTION_POLICY_REVISION,
    ACTIVITY_SCOPE_SCHEMA,
    ACTIVITY_SLICE_SCHEMA,
    AGENT_ACTIVITY_UPDATE_SCHEMA,
    ActivityAuditReceipt,
    ActivityAuditRecord,
    ActivityContractError,
    ActivityScope,
    ActivitySlice,
    AgentActivityUpdate,
    _digest,
    _identifier,
    _optional_identifier,
)
from .canonical_time import canonical_text
from .contracts import ProjectScope

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


DURABLE_ACTIVITY_SCHEMA_REVISION = "collaboration-activity/sqlite-v1"

DURABLE_ACTIVITY_REQUIRED_TABLES = (
    "collaboration_activity_schema",
    "collaboration_agent_activity",
    "collaboration_activity_audits",
)
DURABLE_ACTIVITY_REQUIRED_INDEXES = (
    "idx_collaboration_agent_activity_scope_cursor",
    "idx_collaboration_activity_audits_scope_cursor",
)
DURABLE_ACTIVITY_REQUIRED_TRIGGERS = (
    "collaboration_agent_activity_no_update",
    "collaboration_agent_activity_no_delete",
    "collaboration_activity_audits_no_update",
    "collaboration_activity_audits_no_delete",
)

_SCHEMA_TABLE, _ACTIVITY_TABLE, _AUDIT_TABLE = DURABLE_ACTIVITY_REQUIRED_TABLES
_ACTIVITY_SCOPE_INDEX, _AUDIT_SCOPE_INDEX = DURABLE_ACTIVITY_REQUIRED_INDEXES
(
    _ACTIVITY_NO_UPDATE_TRIGGER,
    _ACTIVITY_NO_DELETE_TRIGGER,
    _AUDIT_NO_UPDATE_TRIGGER,
    _AUDIT_NO_DELETE_TRIGGER,
) = DURABLE_ACTIVITY_REQUIRED_TRIGGERS

_TABLE_COLUMNS = {
    _SCHEMA_TABLE: (
        "singleton",
        "schema_revision",
        "installed_at_utc",
    ),
    _ACTIVITY_TABLE: (
        "activity_update_sha256",
        "project_id",
        "coordination_session_id",
        "agent_session_id",
        "agent_id",
        "work_item_id",
        "role_assignment_sha256",
        "cursor",
        "update_json",
        "recorded_at_utc",
    ),
    _AUDIT_TABLE: (
        "audit_receipt_sha256",
        "receipt_id",
        "activity_update_sha256",
        "project_id",
        "coordination_session_id",
        "agent_session_id",
        "agent_id",
        "work_item_id",
        "cursor",
        "validated_at_utc",
        "receipt_json",
    ),
}
_INDEXES = {
    _ACTIVITY_SCOPE_INDEX: (
        _ACTIVITY_TABLE,
        (
            "project_id",
            "coordination_session_id",
            "agent_session_id",
            "work_item_id",
            "cursor",
        ),
    ),
    _AUDIT_SCOPE_INDEX: (
        _AUDIT_TABLE,
        (
            "project_id",
            "coordination_session_id",
            "agent_session_id",
            "work_item_id",
            "cursor",
        ),
    ),
}
_TRIGGERS = {
    _ACTIVITY_NO_UPDATE_TRIGGER: (
        _ACTIVITY_TABLE,
        "before update on",
        "collaboration_agent_activity_append_only",
    ),
    _ACTIVITY_NO_DELETE_TRIGGER: (
        _ACTIVITY_TABLE,
        "before delete on",
        "collaboration_agent_activity_append_only",
    ),
    _AUDIT_NO_UPDATE_TRIGGER: (
        _AUDIT_TABLE,
        "before update on",
        "collaboration_activity_audit_append_only",
    ),
    _AUDIT_NO_DELETE_TRIGGER: (
        _AUDIT_TABLE,
        "before delete on",
        "collaboration_activity_audit_append_only",
    ),
}

_SCOPE_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "coordination_session_id",
        "agent_session_id",
        "agent_id",
        "authority_effect",
        "canonical_memory_effect",
    }
)
_SLICE_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "paths",
        "summary",
        "authority_effect",
        "tool_policy_effect",
        "canonical_memory_effect",
    }
)
_UPDATE_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "role",
        "summary",
        "previous",
        "current",
        "next",
        "blockers",
        "work_item_id",
        "role_assignment_sha256",
        "cursor",
        "redaction_policy_revision",
        "role_effect",
        "role_assignment_effect",
        "authority_effect",
        "tool_policy_effect",
        "canonical_memory_effect",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "issuer",
        "receipt_id",
        "scope",
        "role",
        "work_item_id",
        "role_assignment_sha256",
        "cursor",
        "activity_update_sha256",
        "activity_scope_sha256",
        "validated_at_utc",
        "redaction",
        "role_effect",
        "role_assignment_effect",
        "authority_effect",
        "tool_policy_effect",
        "canonical_memory_effect",
        "verification",
    }
)
_MAX_CURSOR = (1 << 63) - 1


class DurableActivityRepository:
    """Canonical SQLite adapter satisfying ``ActivityAuditRepository``.

    ``connection`` must be the already-open server-owned SQLite connection.
    Every append borrows the server's single-writer transaction factory; the
    adapter never opens a second database and never installs schema.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
    ) -> None:
        _require_connection(connection)
        _require_transaction_factory(transaction_factory)
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._verify_schema()

    def load_by_update_digest(
        self,
        update_sha256: str,
    ) -> ActivityAuditRecord | None:
        digest = _digest(update_sha256, "activity_audit_update_digest_invalid")
        activity = self._fetchone(
            f"SELECT * FROM {_ACTIVITY_TABLE} WHERE activity_update_sha256=?",
            (digest,),
        )
        if activity is None:
            return None
        record = self._record_from_activity_row(activity)
        if not hmac.compare_digest(record.update.update_sha256, digest):
            raise ActivityContractError("activity_durable_lookup_mismatch")
        return record

    def load_by_receipt_id(self, receipt_id: str) -> ActivityAuditRecord | None:
        normalized = _identifier(receipt_id, "activity_audit_receipt_id_invalid")
        if not normalized.startswith("activity-audit:"):
            raise ActivityContractError("activity_audit_receipt_id_invalid")
        audit = self._fetchone(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE receipt_id=?",
            (normalized,),
        )
        if audit is None:
            return None
        activity = self._fetchone(
            f"SELECT * FROM {_ACTIVITY_TABLE} WHERE activity_update_sha256=?",
            (audit["activity_update_sha256"],),
        )
        if activity is None:
            raise ActivityContractError("activity_durable_record_corrupt")
        record = self._record_from_rows(activity, audit)
        if record.receipt.receipt_id != normalized:
            raise ActivityContractError("activity_durable_lookup_mismatch")
        return record

    def load_by_stream_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
        cursor: int,
    ) -> ActivityAuditRecord | None:
        stream = _stream_key(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            agent_session_id=agent_session_id,
            work_item_id=work_item_id,
        )
        position = _cursor(cursor)
        activity = self._fetchone(
            f"""
            SELECT * FROM {_ACTIVITY_TABLE}
             WHERE project_id=?
               AND coordination_session_id=?
               AND agent_session_id=?
               AND work_item_id=?
               AND cursor=?
            """,
            (*stream, position),
        )
        return None if activity is None else self._record_from_activity_row(activity)

    def highest_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
    ) -> int | None:
        stream = _stream_key(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            agent_session_id=agent_session_id,
            work_item_id=work_item_id,
        )
        row = self._fetchone(
            f"""
            SELECT MAX(cursor) AS highest_cursor
              FROM {_ACTIVITY_TABLE}
             WHERE project_id=?
               AND coordination_session_id=?
               AND agent_session_id=?
               AND work_item_id=?
            """,
            stream,
        )
        if row is None or row["highest_cursor"] is None:
            return None
        return _cursor(row["highest_cursor"])

    def append_exact(
        self,
        update: AgentActivityUpdate,
        receipt: ActivityAuditReceipt,
        *,
        recorded_at_utc: str,
        _authority_token: object | None = None,
    ) -> ActivityAuditRecord:
        """Atomically append one exact pair or return its canonical replay."""

        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise ActivityContractError("activity_audit_repository_write_authority_required")
        if not isinstance(update, AgentActivityUpdate):
            raise ActivityContractError("activity_update_invalid")
        if not isinstance(receipt, ActivityAuditReceipt):
            raise ActivityContractError("activity_audit_receipt_invalid")
        update.validate_integrity()
        receipt.validate_integrity(update)
        recorded_at = _canonical_timestamp(
            recorded_at_utc,
            "activity_audit_recorded_at_invalid",
        )
        update_json = update.canonical_json()
        receipt_json = receipt.canonical_json()

        existing = self.load_by_update_digest(update.update_sha256)
        if existing is not None:
            _require_exact_record(existing, update=update, receipt=receipt)
            return existing
        if self.load_by_receipt_id(receipt.receipt_id) is not None:
            raise ActivityContractError("activity_audit_receipt_id_conflict")
        at_cursor = self.load_by_stream_cursor(
            project_id=update.project.project_id,
            coordination_session_id=update.coordination_session_id,
            agent_session_id=update.agent_session_id,
            work_item_id=update.work_item_id,
            cursor=update.cursor,
        )
        if at_cursor is not None:
            raise ActivityContractError("activity_audit_cursor_conflict")
        highest = self.highest_cursor(
            project_id=update.project.project_id,
            coordination_session_id=update.coordination_session_id,
            agent_session_id=update.agent_session_id,
            work_item_id=update.work_item_id,
        )
        if highest is not None and update.cursor < highest:
            raise ActivityContractError("activity_audit_cursor_regression")

        try:
            with self._transaction_factory():
                self._connection.execute(
                    f"""
                    INSERT INTO {_ACTIVITY_TABLE} (
                        activity_update_sha256, project_id,
                        coordination_session_id, agent_session_id, agent_id,
                        work_item_id, role_assignment_sha256, cursor,
                        update_json, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        update.update_sha256,
                        update.project.project_id,
                        update.coordination_session_id,
                        update.agent_session_id,
                        update.agent_id,
                        update.work_item_id,
                        update.role_assignment_sha256,
                        update.cursor,
                        update_json,
                        recorded_at,
                    ),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO {_AUDIT_TABLE} (
                        audit_receipt_sha256, receipt_id,
                        activity_update_sha256, project_id,
                        coordination_session_id, agent_session_id, agent_id,
                        work_item_id, cursor, validated_at_utc, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_sha256,
                        receipt.receipt_id,
                        update.update_sha256,
                        update.project.project_id,
                        update.coordination_session_id,
                        update.agent_session_id,
                        update.agent_id,
                        update.work_item_id,
                        update.cursor,
                        receipt.validated_at_utc,
                        receipt_json,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            winner = self.load_by_update_digest(update.update_sha256)
            if winner is not None:
                _require_exact_record(winner, update=update, receipt=receipt)
                return winner
            if self.load_by_receipt_id(receipt.receipt_id) is not None:
                raise ActivityContractError("activity_audit_receipt_id_conflict") from exc
            at_cursor = self.load_by_stream_cursor(
                project_id=update.project.project_id,
                coordination_session_id=update.coordination_session_id,
                agent_session_id=update.agent_session_id,
                work_item_id=update.work_item_id,
                cursor=update.cursor,
            )
            if at_cursor is not None:
                raise ActivityContractError("activity_audit_cursor_conflict") from exc
            raise ActivityContractError("activity_durable_append_conflict") from exc

        stored = self.load_by_update_digest(update.update_sha256)
        if stored is None:
            raise ActivityContractError("activity_durable_append_missing")
        _require_exact_record(stored, update=update, receipt=receipt)
        return stored

    def _record_from_activity_row(
        self,
        activity: Mapping[str, object],
    ) -> ActivityAuditRecord:
        audit = self._fetchone(
            f"SELECT * FROM {_AUDIT_TABLE} WHERE activity_update_sha256=?",
            (activity["activity_update_sha256"],),
        )
        if audit is None:
            raise ActivityContractError("activity_durable_record_corrupt")
        return self._record_from_rows(activity, audit)

    def _record_from_rows(
        self,
        activity: Mapping[str, object],
        audit: Mapping[str, object],
    ) -> ActivityAuditRecord:
        try:
            update = _update_from_json(str(activity["update_json"]))
            receipt = _receipt_from_json(str(audit["receipt_json"]), update)
            recorded_at = _canonical_timestamp(
                activity["recorded_at_utc"],
                "activity_durable_record_corrupt",
            )
            expected_activity = {
                "activity_update_sha256": update.update_sha256,
                "project_id": update.project.project_id,
                "coordination_session_id": update.coordination_session_id,
                "agent_session_id": update.agent_session_id,
                "agent_id": update.agent_id,
                "work_item_id": update.work_item_id,
                "role_assignment_sha256": update.role_assignment_sha256,
                "cursor": update.cursor,
            }
            expected_audit = {
                "audit_receipt_sha256": receipt.receipt_sha256,
                "receipt_id": receipt.receipt_id,
                "activity_update_sha256": update.update_sha256,
                "project_id": update.project.project_id,
                "coordination_session_id": update.coordination_session_id,
                "agent_session_id": update.agent_session_id,
                "agent_id": update.agent_id,
                "work_item_id": update.work_item_id,
                "cursor": update.cursor,
                "validated_at_utc": receipt.validated_at_utc,
            }
            for column, expected in expected_activity.items():
                if activity[column] != expected:
                    raise ActivityContractError("activity_durable_record_corrupt")
            for column, expected in expected_audit.items():
                if audit[column] != expected:
                    raise ActivityContractError("activity_durable_record_corrupt")
            record = ActivityAuditRecord(
                update=update,
                receipt=receipt,
                recorded_at_utc=recorded_at,
            )
            record.validate_integrity()
            return record
        except ActivityContractError as exc:
            if exc.code.startswith("activity_durable_"):
                raise
            raise ActivityContractError("activity_durable_record_corrupt") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ActivityContractError("activity_durable_record_corrupt") from exc

    def _verify_schema(self) -> None:
        existing_tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not set(DURABLE_ACTIVITY_REQUIRED_TABLES).issubset(existing_tables):
            raise ActivityContractError("activity_durable_schema_missing")

        for table, expected_columns in _TABLE_COLUMNS.items():
            actual = tuple(
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if actual != expected_columns:
                raise ActivityContractError("activity_durable_schema_stale")

        _verify_primary_key(self._connection, _ACTIVITY_TABLE, "activity_update_sha256")
        _verify_primary_key(self._connection, _AUDIT_TABLE, "audit_receipt_sha256")
        _verify_unique_index(
            self._connection,
            _ACTIVITY_TABLE,
            (
                "project_id",
                "coordination_session_id",
                "agent_session_id",
                "work_item_id",
                "cursor",
            ),
        )
        _verify_unique_index(self._connection, _AUDIT_TABLE, ("receipt_id",))
        _verify_unique_index(
            self._connection,
            _AUDIT_TABLE,
            ("activity_update_sha256",),
        )
        for name, (table, columns) in _INDEXES.items():
            _verify_named_index(self._connection, name, table, columns)
        _verify_activity_foreign_key(self._connection)
        for name, (table, action, failure) in _TRIGGERS.items():
            _verify_trigger(self._connection, name, table, action, failure)

        marker = self._fetchone(
            f"SELECT schema_revision, installed_at_utc FROM {_SCHEMA_TABLE} WHERE singleton=1"
        )
        if marker is None:
            raise ActivityContractError("activity_durable_schema_missing")
        if marker["schema_revision"] != DURABLE_ACTIVITY_SCHEMA_REVISION:
            raise ActivityContractError("activity_durable_schema_stale")
        _canonical_timestamp(
            marker["installed_at_utc"],
            "activity_durable_schema_stale",
        )

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> dict[str, Any] | None:
        return _row(self._connection.execute(sql, parameters))


def _update_from_json(raw: str) -> AgentActivityUpdate:
    payload = _canonical_mapping(raw)
    _require_exact_fields(payload, _UPDATE_FIELDS)
    if payload.get("schema_version") != AGENT_ACTIVITY_UPDATE_SCHEMA:
        raise ActivityContractError("activity_durable_record_corrupt")
    if payload.get("redaction_policy_revision") != ACTIVITY_REDACTION_POLICY_REVISION:
        raise ActivityContractError("activity_durable_record_corrupt")
    update = AgentActivityUpdate(
        scope=_scope_from_mapping(_mapping(payload.get("scope"))),
        role=payload.get("role"),  # type: ignore[arg-type]
        summary=payload.get("summary"),  # type: ignore[arg-type]
        previous=_slice_from_value(payload.get("previous")),
        current=_slice_from_mapping(_mapping(payload.get("current"))),
        next=_slice_from_value(payload.get("next")),
        blockers=_string_tuple(payload.get("blockers")),
        work_item_id=payload.get("work_item_id"),  # type: ignore[arg-type]
        role_assignment_sha256=payload.get("role_assignment_sha256"),  # type: ignore[arg-type]
        cursor=payload.get("cursor"),  # type: ignore[arg-type]
    )
    if update.canonical_json() != raw:
        raise ActivityContractError("activity_durable_record_corrupt")
    return update


def _receipt_from_json(raw: str, update: AgentActivityUpdate) -> ActivityAuditReceipt:
    payload = _canonical_mapping(raw)
    _require_exact_fields(payload, _RECEIPT_FIELDS)
    if (
        payload.get("schema_version") != ACTIVITY_AUDIT_RECEIPT_SCHEMA
        or payload.get("issuer") != ACTIVITY_AUDIT_ISSUER
    ):
        raise ActivityContractError("activity_durable_record_corrupt")
    receipt = ActivityAuditReceipt._rehydrate(
        receipt_id=payload.get("receipt_id"),  # type: ignore[arg-type]
        update=update,
        validated_at_utc=payload.get("validated_at_utc"),  # type: ignore[arg-type]
        _authority_token=_SERVER_AUTHORITY_TOKEN,
    )
    if receipt.canonical_json() != raw:
        raise ActivityContractError("activity_durable_record_corrupt")
    return receipt


def _scope_from_mapping(payload: Mapping[str, object]) -> ActivityScope:
    _require_exact_fields(payload, _SCOPE_FIELDS)
    if payload.get("schema_version") != ACTIVITY_SCOPE_SCHEMA:
        raise ActivityContractError("activity_durable_record_corrupt")
    return ActivityScope(
        project=ProjectScope(payload.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=payload.get("coordination_session_id"),  # type: ignore[arg-type]
        agent_session_id=payload.get("agent_session_id"),  # type: ignore[arg-type]
        agent_id=payload.get("agent_id"),  # type: ignore[arg-type]
    )


def _slice_from_value(value: object) -> ActivitySlice | None:
    if value is None:
        return None
    return _slice_from_mapping(_mapping(value))


def _slice_from_mapping(payload: Mapping[str, object]) -> ActivitySlice:
    _require_exact_fields(payload, _SLICE_FIELDS)
    if payload.get("schema_version") != ACTIVITY_SLICE_SCHEMA:
        raise ActivityContractError("activity_durable_record_corrupt")
    return ActivitySlice(
        scope=payload.get("scope"),  # type: ignore[arg-type]
        paths=_string_tuple(payload.get("paths")),
        summary=payload.get("summary"),  # type: ignore[arg-type]
    )


def _require_exact_record(
    record: ActivityAuditRecord,
    *,
    update: AgentActivityUpdate,
    receipt: ActivityAuditReceipt,
) -> None:
    if not isinstance(record, ActivityAuditRecord):
        raise ActivityContractError("activity_audit_repository_record_invalid")
    record.validate_integrity()
    if not hmac.compare_digest(record.update.update_sha256, update.update_sha256):
        raise ActivityContractError("activity_audit_repository_lookup_mismatch")
    if record.receipt.receipt_id != receipt.receipt_id:
        raise ActivityContractError("activity_audit_replay_ambiguous")
    if not hmac.compare_digest(record.receipt.receipt_sha256, receipt.receipt_sha256):
        raise ActivityContractError("activity_audit_repository_replay_conflict")


def _stream_key(
    *,
    project_id: object,
    coordination_session_id: object,
    agent_session_id: object,
    work_item_id: object,
) -> tuple[str, str, str, str]:
    try:
        project = ProjectScope(project_id).project_id
    except (TypeError, ValueError) as exc:
        raise ActivityContractError("activity_project_invalid") from exc
    return (
        project,
        _identifier(
            coordination_session_id,
            "activity_scope_coordination_session_invalid",
        ),
        _identifier(agent_session_id, "activity_scope_agent_session_invalid"),
        _optional_identifier(work_item_id, "activity_work_item_invalid"),
    )


def _cursor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_CURSOR:
        raise ActivityContractError("activity_cursor_invalid")
    return value


def _canonical_timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ActivityContractError(code)
    try:
        rendered = canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise ActivityContractError(code) from exc
    if rendered != value:
        raise ActivityContractError(code)
    return rendered


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
        raise ActivityContractError("activity_durable_record_corrupt") from exc


def _canonical_mapping(raw: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ActivityContractError("activity_durable_record_corrupt") from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != raw:
        raise ActivityContractError("activity_durable_record_corrupt")
    return payload


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ActivityContractError("activity_durable_record_corrupt")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ActivityContractError("activity_durable_record_corrupt")
    return tuple(value)


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
) -> None:
    if set(payload) != expected:
        raise ActivityContractError("activity_durable_record_corrupt")


def _require_connection(connection: object) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise ActivityContractError("activity_durable_connection_invalid")


def _require_transaction_factory(value: object) -> None:
    if not callable(value):
        raise ActivityContractError("activity_durable_writer_required")


def _verify_primary_key(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    matches = [row for row in rows if str(row[1]) == column]
    if len(matches) != 1 or int(matches[0][5]) != 1:
        raise ActivityContractError("activity_durable_schema_stale")


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
    raise ActivityContractError("activity_durable_schema_stale")


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
        raise ActivityContractError("activity_durable_schema_stale")
    listed = [
        item
        for item in connection.execute(f"PRAGMA index_list({table})").fetchall()
        if str(item[1]) == name
    ]
    if len(listed) != 1 or bool(listed[0][2]):
        raise ActivityContractError("activity_durable_schema_stale")
    actual = tuple(
        str(item[2]) for item in connection.execute(f"PRAGMA index_info({name})").fetchall()
    )
    if actual != columns:
        raise ActivityContractError("activity_durable_schema_stale")


def _verify_activity_foreign_key(connection: sqlite3.Connection) -> None:
    expected = (
        _ACTIVITY_TABLE,
        "activity_update_sha256",
        "activity_update_sha256",
        "RESTRICT",
    )
    for row in connection.execute(f"PRAGMA foreign_key_list({_AUDIT_TABLE})").fetchall():
        actual = (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        if actual == expected:
            return
    raise ActivityContractError("activity_durable_schema_stale")


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
        raise ActivityContractError("activity_durable_schema_stale")
    normalized = " ".join(str(row[1]).casefold().split())
    required = (action, table.casefold(), f"raise(abort, '{failure.casefold()}')")
    if any(token not in normalized for token in required):
        raise ActivityContractError("activity_durable_schema_stale")


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    raw = cursor.fetchone()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))
