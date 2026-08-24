#!/usr/bin/env python3
"""Aggregate MCP tool usage telemetry (tool_usage_events table).

Usage: python scripts/tool_usage_report.py [--db PATH] [--days N]
Read-only. Pairs with the call_tool telemetry wrapper in server.py.
"""

import argparse
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _usage_section(conn: sqlite3.Connection, days: int) -> None:
    try:
        rows = conn.execute(
            """
            SELECT tool,
                   COUNT(*)                AS calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors,
                   ROUND(AVG(duration_ms), 1) AS avg_ms
            FROM tool_usage_events
            WHERE ts >= datetime('now', ?)
            GROUP BY tool
            ORDER BY calls DESC
            """,
            (f"-{days} days",),
        ).fetchall()
    except sqlite3.OperationalError:
        print(f"no usage events in the last {days} days")
        return
    if not rows:
        print(f"no usage events in the last {days} days")
        return
    print(f"tool | calls | errors | avg_ms   (last {days} days)")
    for tool, calls, errors, avg_ms in rows:
        print(f"{tool} | {calls} | {errors} | {avg_ms}")
    total = sum(r[1] for r in rows)
    errs = sum(r[2] for r in rows)
    print(f"TOTAL | {total} | {errs} |")


def _injection_section(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(DISTINCT session_id),
                   SUM(CASE WHEN referenced > 0 THEN 1 ELSE 0 END)
            FROM context_injection_events
            """
        ).fetchone()
    except sqlite3.OperationalError:
        print("no injection events")
        return
    injections, sessions, referenced_rows = row or (0, 0, 0)
    if not injections:
        print("no injection events")
        return
    rate = (referenced_rows or 0) / injections * 100
    print("surface=context_injections | injections | sessions | referenced_rows | rate")
    print(f"context_injections | {injections} | {sessions} | {referenced_rows or 0} | {rate:.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--days", type=int, default=14)
    ns = ap.parse_args()

    if ns.db:
        db_path = ns.db
    else:
        from plastic_promise.core.paths import get_db_path

        db_path = get_db_path()

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        _usage_section(conn, ns.days)
        _injection_section(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
