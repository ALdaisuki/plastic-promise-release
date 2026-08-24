"""Context injection tracking: record injections, mark references.

Enables the injection->reference-rate observability metric: every
session-brief injection writes one row to context_injection_events;
when the same scope later recalls those memories via memory_recall,
matching rows are marked. scripts/tool_usage_report.py aggregates the
reference rate.

Stdlib only. All writers are best-effort: telemetry failures must never
break the memory or context paths they observe.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from plastic_promise.core.paths import get_db_path


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the injection-event table and its session index if missing."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS context_injection_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT,"
        " session_id TEXT,"
        " surface TEXT,"
        " memory_ids TEXT,"
        " chars INTEGER,"
        " referenced INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cie_session ON context_injection_events(session_id)"
    )
    conn.commit()


def record_injection(session_id: str, surface: str, memory_ids: list, chars: int) -> None:
    """Record one injection event. Best-effort: never raises."""
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            ensure_tables(conn)
            conn.execute(
                "INSERT INTO context_injection_events"
                " (ts, session_id, surface, memory_ids, chars)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    str(session_id or ""),
                    str(surface or ""),
                    ",".join(str(mid) for mid in (memory_ids or [])),
                    int(chars or 0),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def mark_references(session_id: str, memory_ids: list) -> int:
    """Increment referenced on injection rows containing each mid.

    Returns the total number of rows updated. Best-effort: never raises.
    """
    updated = 0
    try:
        conn = sqlite3.connect(get_db_path())
        try:
            ensure_tables(conn)
            for mid in memory_ids or []:
                if not mid:
                    continue
                cur = conn.execute(
                    "UPDATE context_injection_events"
                    " SET referenced = COALESCE(referenced, 0) + 1"
                    " WHERE session_id = ? AND memory_ids LIKE '%' || ? || '%'",
                    (str(session_id or ""), str(mid)),
                )
                updated += int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return updated
