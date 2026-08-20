"""Startup recovery helpers for Hunter Guild task state."""

from __future__ import annotations

import datetime
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from plastic_promise.core.paths import get_db_path
from plastic_promise.core.task_queue_schema import (
    LEGACY_TASK_PROJECT_ID,
    ensure_task_tables,
)


def release_stale_claims(
    db_path: str | Path | None = None,
    *,
    project_id: str | None = None,
    system_authority: bool = False,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Release stale claims within one project or under explicit system authority.

    Omitting ``project_id`` never grants an implicit global scan.  The startup
    launcher is the only current system-wide caller and must opt in with
    ``system_authority=True``.
    """
    normalized_project = str(project_id or "").strip()
    if system_authority and normalized_project:
        raise ValueError("project_id and system_authority are mutually exclusive")
    if not system_authority and normalized_project in {
        "",
        "project:unknown",
        LEGACY_TASK_PROJECT_ID,
    }:
        raise ValueError("valid project_id is required without system_authority")

    path = str(db_path or get_db_path())
    current = now or datetime.datetime.now()
    current_iso = current.isoformat()

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        ensure_task_tables(conn)

        if system_authority:
            rows = conn.execute(
                "SELECT * FROM task_queue WHERE status IN ('claimed', 'executing')"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM task_queue "
                "WHERE project_id = ? AND status IN ('claimed', 'executing')",
                (normalized_project,),
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

            transition = conn.execute(
                "UPDATE task_queue SET status='pending', to_agent=?, claimed_by=NULL, "
                "claimed_at=NULL, heartbeat_at=NULL, escalation_count=?, "
                "last_escalation_at=?, updated_at=? WHERE id=? AND project_id=? "
                "AND status=? AND claimed_by IS ? AND heartbeat_at IS ?",
                (
                    next_agent,
                    new_escalation_count,
                    current_iso,
                    current_iso,
                    row["id"],
                    row["project_id"],
                    row["status"],
                    row["claimed_by"],
                    row["heartbeat_at"],
                ),
            )
            if transition.rowcount != 1:
                # Heartbeat/complete/abandon won the race after the scan read.
                # Do not reset fresher state or record a false timeout penalty.
                continue
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
    finally:
        conn.close()

    return {
        "success": True,
        "authority_scope": "system" if system_authority else "project",
        "project_id": None if system_authority else normalized_project,
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
