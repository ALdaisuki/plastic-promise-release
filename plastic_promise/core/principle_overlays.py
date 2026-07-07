"""Project-level overlays for global core principle activation."""

from __future__ import annotations

from typing import Any

from plastic_promise.core.traceability import utc_now

VALID_OVERLAY_ACTIONS = {"boost", "suppress", "tag"}


def ensure_project_principle_overlay_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_principle_overlays (
            overlay_id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            principle_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            weight_delta REAL NOT NULL DEFAULT 0,
            tag TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, principle_id, action, tag)
        )
        """
    )
    conn.commit()


def upsert_project_principle_overlay(
    conn,
    *,
    project_id: str,
    principle_id: int,
    action: str,
    weight_delta: float = 0.0,
    tag: str = "",
    reason: str = "",
    enabled: bool = True,
) -> int:
    if action not in VALID_OVERLAY_ACTIONS:
        raise ValueError(f"invalid project principle overlay action: {action}")
    ensure_project_principle_overlay_schema(conn)
    cursor = conn.execute(
        """
        INSERT INTO project_principle_overlays (
            project_id, principle_id, action, weight_delta, tag, reason, enabled, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, principle_id, action, tag) DO UPDATE SET
            weight_delta = excluded.weight_delta,
            reason = excluded.reason,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """,
        (
            project_id,
            int(principle_id),
            action,
            float(weight_delta),
            tag,
            reason,
            int(enabled),
            utc_now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def load_project_principle_overlays(conn, project_id: str) -> dict[int, dict[str, Any]]:
    if not project_id:
        return {}
    ensure_project_principle_overlay_schema(conn)
    rows = conn.execute(
        """
        SELECT principle_id, action, weight_delta, tag, reason
        FROM project_principle_overlays
        WHERE project_id = ? AND enabled = 1
        ORDER BY overlay_id ASC
        """,
        (project_id,),
    ).fetchall()

    overlays: dict[int, dict[str, Any]] = {}
    for principle_id, action, weight_delta, tag, reason in rows:
        if action not in VALID_OVERLAY_ACTIONS:
            continue
        pid = int(principle_id)
        entry = overlays.setdefault(
            pid,
            {
                "boost": 0.0,
                "suppress": False,
                "tags": [],
                "actions": [],
                "reasons": [],
            },
        )
        if action == "boost":
            entry["boost"] += float(weight_delta or 0.0)
        elif action == "suppress":
            entry["suppress"] = True
        elif action == "tag" and tag:
            entry["tags"].append(str(tag))
        if action not in entry["actions"]:
            entry["actions"].append(action)
        if reason:
            entry["reasons"].append(str(reason))
    return overlays
