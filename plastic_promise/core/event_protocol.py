"""Unified runtime event protocol for tool, task, and agent operations."""

from __future__ import annotations

import json
import secrets
from typing import Any

from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    utc_now,
)

VALID_EVENT_KINDS = {"tool", "task", "agent"}
VALID_EVENT_STATUSES = {"pending", "running", "completed", "error"}


def new_event_id() -> str:
    return f"evt_{secrets.token_hex(8)}"


def _json(data: dict[str, Any] | None) -> str:
    return json.dumps(data or {}, ensure_ascii=False)


def record_runtime_event(
    conn,
    *,
    event_kind: str,
    event_name: str,
    status: str,
    request_scope_id: str = "",
    stage_session_id: str = "",
    flow_line_id: str = "",
    project_id: str = "",
    actor: str = "",
    trust_tier: str = "",
    defense_decision: str = "",
    audit_trace: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> str:
    if event_kind not in VALID_EVENT_KINDS:
        raise ValueError(f"Unknown runtime event kind: {event_kind}")
    if status not in VALID_EVENT_STATUSES:
        raise ValueError(f"Unknown runtime event status: {status}")

    event_id = event_id or new_event_id()
    ensure_traceability_schema(conn)
    conn.execute(
        """
        INSERT INTO runtime_events (
            event_id,
            event_kind,
            event_name,
            status,
            request_scope_id,
            stage_session_id,
            flow_line_id,
            project_id,
            actor,
            trust_tier,
            defense_decision,
            audit_trace_json,
            metadata_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_kind,
            event_name,
            status,
            request_scope_id,
            stage_session_id,
            flow_line_id,
            project_id,
            actor,
            trust_tier,
            defense_decision,
            _json(audit_trace),
            _json(metadata),
            utc_now(),
        ),
    )
    conn.commit()
    return event_id


def _engine_conn(engine: Any):
    sqlite = getattr(engine, "_sqlite", None)
    return getattr(sqlite, "_conn", None)


def safe_record_runtime_event(engine: Any, **kwargs: Any) -> bool:
    """Best-effort runtime event write; never blocks the user path."""
    conn = _engine_conn(engine)
    if conn is None:
        return False
    try:
        record_runtime_event(conn, **kwargs)
        return True
    except Exception:
        return False
