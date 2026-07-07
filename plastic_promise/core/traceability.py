"""Local traceability helpers for call spans and degradation events."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_call_id() -> str:
    return f"call_{secrets.token_hex(8)}"


def _metadata_json(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, ensure_ascii=False)


def _table_columns(conn, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_traceability_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_spans (
            call_id TEXT PRIMARY KEY,
            parent_call_id TEXT NOT NULL DEFAULT '',
            request_scope_id TEXT NOT NULL DEFAULT '',
            stage_session_id TEXT NOT NULL DEFAULT '',
            flow_line_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL,
            stage_name TEXT NOT NULL DEFAULT '',
            caller TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'success',
            degraded INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT NOT NULL DEFAULT '',
            output_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_lineage (
            lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            relation TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS degradation_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            link_name TEXT NOT NULL,
            policy TEXT NOT NULL,
            level TEXT NOT NULL,
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            fallback_used TEXT NOT NULL DEFAULT '',
            minimum_result TEXT NOT NULL DEFAULT '',
            user_visible INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_outbox (
            outbox_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    call_span_columns = _table_columns(conn, "call_spans")
    if "started_at" not in call_span_columns:
        conn.execute(
            "ALTER TABLE call_spans ADD COLUMN started_at TEXT NOT NULL DEFAULT ''"
        )
        call_span_columns.add("started_at")
    if "ended_at" not in call_span_columns:
        conn.execute("ALTER TABLE call_spans ADD COLUMN ended_at TEXT")
        call_span_columns.add("ended_at")
    if "created_at" in call_span_columns:
        conn.execute(
            """
            UPDATE call_spans
            SET started_at = created_at
            WHERE started_at = '' AND created_at IS NOT NULL
            """
        )
    if "updated_at" in call_span_columns:
        conn.execute(
            """
            UPDATE call_spans
            SET ended_at = updated_at
            WHERE (ended_at IS NULL OR ended_at = '') AND updated_at IS NOT NULL
            """
        )
    conn.commit()


def _default_outbox_path() -> Path:
    try:
        from plastic_promise.core.paths import get_db_path

        return Path(get_db_path()).with_name("store_outbox.jsonl")
    except Exception:
        return Path("store_outbox.jsonl")


def record_outbox_event(
    conn,
    *,
    tool_name: str,
    project_id: str,
    call_id: str,
    status: str,
    payload: dict[str, Any],
    error_class: str = "",
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
    fallback_path: str | Path | None = None,
) -> str:
    outbox_id = f"outbox_{secrets.token_hex(8)}"
    created_at = utc_now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    metadata_json = _metadata_json(metadata)

    try:
        ensure_traceability_schema(conn)
        conn.execute(
            """
            INSERT INTO store_outbox (
                outbox_id,
                tool_name,
                project_id,
                call_id,
                status,
                payload_json,
                error_class,
                error_message,
                metadata_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                tool_name,
                project_id,
                call_id,
                status,
                payload_json,
                error_class,
                error_message,
                metadata_json,
                created_at,
            ),
        )
        conn.commit()
        return outbox_id
    except Exception:
        path = Path(fallback_path) if fallback_path is not None else _default_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "outbox_id": outbox_id,
            "tool_name": tool_name,
            "project_id": project_id,
            "call_id": call_id,
            "status": status,
            "payload": payload or {},
            "error_class": error_class,
            "error_message": error_message,
            "metadata": metadata or {},
            "created_at": created_at,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return outbox_id


def record_call_span(
    conn,
    *,
    call_id: str,
    parent_call_id: str = "",
    request_scope_id: str = "",
    stage_session_id: str = "",
    flow_line_id: str = "",
    project_id: str = "",
    tool_name: str,
    stage_name: str = "",
    caller: str = "",
    status: str = "success",
    degraded: bool = False,
    input_hash: str = "",
    output_hash: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    columns = [
        "call_id",
        "parent_call_id",
        "request_scope_id",
        "stage_session_id",
        "flow_line_id",
        "project_id",
        "tool_name",
        "stage_name",
        "caller",
        "status",
        "degraded",
        "input_hash",
        "output_hash",
        "metadata_json",
        "started_at",
        "ended_at",
    ]
    values = [
        call_id,
        parent_call_id,
        request_scope_id,
        stage_session_id,
        flow_line_id,
        project_id,
        tool_name,
        stage_name,
        caller,
        status,
        int(degraded),
        input_hash,
        output_hash,
        _metadata_json(metadata),
        now,
        now,
    ]
    call_span_columns = _table_columns(conn, "call_spans")
    update_assignments = [
        "parent_call_id = excluded.parent_call_id",
        "request_scope_id = excluded.request_scope_id",
        "stage_session_id = excluded.stage_session_id",
        "flow_line_id = excluded.flow_line_id",
        "project_id = excluded.project_id",
        "tool_name = excluded.tool_name",
        "stage_name = excluded.stage_name",
        "caller = excluded.caller",
        "ended_at = excluded.ended_at",
        "status = excluded.status",
        "degraded = excluded.degraded",
        "input_hash = excluded.input_hash",
        "output_hash = excluded.output_hash",
        "metadata_json = excluded.metadata_json",
    ]
    if "created_at" in call_span_columns:
        columns.append("created_at")
        values.append(now)
    if "updated_at" in call_span_columns:
        columns.append("updated_at")
        values.append(now)
        update_assignments.append("updated_at = excluded.updated_at")

    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""
        INSERT INTO call_spans ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(call_id) DO UPDATE SET
            {", ".join(update_assignments)}
        """,
        values,
    )
    conn.commit()


def record_degradation_event(
    conn,
    *,
    call_id: str,
    request_scope_id: str,
    project_id: str,
    tool_name: str,
    link_name: str,
    policy: str,
    level: str,
    error_class: str = "",
    error_message: str = "",
    fallback_used: str = "",
    minimum_result: str = "",
    user_visible: bool = True,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO degradation_events (
            call_id,
            request_scope_id,
            project_id,
            tool_name,
            link_name,
            policy,
            level,
            error_class,
            error_message,
            fallback_used,
            minimum_result,
            user_visible,
            metadata_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            request_scope_id,
            project_id,
            tool_name,
            link_name,
            policy,
            level,
            error_class,
            error_message,
            fallback_used,
            minimum_result,
            int(user_visible),
            _metadata_json(metadata),
            utc_now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _engine_conn(engine: Any):
    sqlite = getattr(engine, "_sqlite", None)
    return getattr(sqlite, "_conn", None)


def safe_record_call_span(engine: Any, **kwargs: Any) -> bool:
    """Best-effort handler trace write; never block the user path."""
    conn = _engine_conn(engine)
    if conn is None:
        return False
    try:
        ensure_traceability_schema(conn)
        record_call_span(conn, **kwargs)
        return True
    except Exception:
        return False


def safe_record_degradation_event(engine: Any, **kwargs: Any) -> bool:
    """Best-effort degradation event write; never block the user path."""
    conn = _engine_conn(engine)
    if conn is None:
        return False
    try:
        ensure_traceability_schema(conn)
        record_degradation_event(conn, **kwargs)
        return True
    except Exception:
        return False


def build_envelope(
    *,
    data: Any,
    trace: dict[str, Any],
    success: bool = True,
    warnings: list[str] | None = None,
    fallback_used: list[str] | None = None,
    minimum_result: str = "",
    degrade_level: str = "",
) -> dict[str, Any]:
    warnings = warnings or []
    fallback_used = fallback_used or []
    degraded = bool(warnings or fallback_used or minimum_result)
    if degraded and not degrade_level:
        degrade_level = "warning"
    if not degraded and not degrade_level:
        degrade_level = "none"

    return {
        "success": success,
        "degraded": degraded,
        "degrade_level": degrade_level,
        "warnings": warnings,
        "fallback_used": fallback_used,
        "minimum_result": minimum_result,
        "trace": trace,
        "data": data,
    }
