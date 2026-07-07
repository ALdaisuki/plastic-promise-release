"""Startup recovery helpers for Hunter Guild task state."""

from __future__ import annotations

import datetime
from pathlib import Path
import sqlite3
from typing import Any

from plastic_promise.core.paths import get_db_path
from plastic_promise.core.task_queue_schema import ensure_task_tables


def release_stale_claims(
    db_path: str | Path | None = None,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Release claimed/executing tasks whose heartbeat exceeded timeout."""
    path = str(db_path or get_db_path())
    current = now or datetime.datetime.now()
    current_iso = current.isoformat()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)

    rows = conn.execute(
        "SELECT * FROM task_queue WHERE status IN ('claimed', 'executing')"
    ).fetchall()

    released_ids: list[str] = []
    escalated_ids: list[str] = []

    for row in rows:
        last_seen = _parse_timestamp(
            row["heartbeat_at"] or row["claimed_at"] or row["updated_at"] or row["created_at"]
        )
        timeout_seconds = _timeout_seconds(row["timeout_seconds"])
        if last_seen is None or (current - last_seen).total_seconds() <= timeout_seconds:
            continue

        new_escalation_count = int(row["escalation_count"] or 0) + 1
        max_escalations = int(row["max_escalations"] or 3)
        escalated = new_escalation_count >= max_escalations
        next_agent = "claude" if escalated else row["to_agent"]

        conn.execute(
            "UPDATE task_queue SET status='pending', to_agent=?, claimed_by=NULL, "
            "claimed_at=NULL, heartbeat_at=NULL, escalation_count=?, "
            "last_escalation_at=?, updated_at=? WHERE id=?",
            (next_agent, new_escalation_count, current_iso, current_iso, row["id"]),
        )
        conn.execute(
            "INSERT INTO hunter_failure_log "
            "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
            "VALUES (?, ?, ?, 'timeout', NULL, NULL, ?)",
            (row["claimed_by"] or "", row["id"], row["task_type"], -0.01),
        )
        released_ids.append(row["id"])
        if escalated:
            escalated_ids.append(row["id"])

    conn.commit()
    conn.close()

    return {
        "success": True,
        "released_count": len(released_ids),
        "released_task_ids": released_ids,
        "escalated_count": len(escalated_ids),
        "escalated_task_ids": escalated_ids,
    }


def _parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def _timeout_seconds(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = 300
    return max(1, timeout)
