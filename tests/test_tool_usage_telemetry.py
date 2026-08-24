"""Tool usage telemetry: table creation, row recording, and CLI report.

Covers the call_tool telemetry primitives in server.py
(_ensure_tool_usage_table / _record_tool_usage) against a temp SQLite db,
then verifies scripts/tool_usage_report.py can aggregate the same db via
--db and emit a TOTAL line.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

from plastic_promise.mcp.server import (
    _ensure_tool_usage_table,
    _record_tool_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = REPO_ROOT / "scripts" / "tool_usage_report.py"


def test_ensure_and_record_tool_usage_roundtrip(tmp_path):
    db_path = str(tmp_path / "telemetry.db")
    conn = sqlite3.connect(db_path)
    try:
        _ensure_tool_usage_table(conn)
        # Idempotent create.
        _ensure_tool_usage_table(conn)

        _record_tool_usage(
            conn,
            "memory_recall",
            "claude",
            duration_ms=12.5,
            ok=1,
            response_bytes=1024,
            error="",
        )
        _record_tool_usage(
            conn,
            "context_supply",
            "pi_builder",
            duration_ms=40.0,
            ok=0,
            response_bytes=64,
            error="ValueError: bad scope",
        )
        rows = conn.execute(
            "SELECT ts, tool, actor, duration_ms, ok, response_bytes, error"
            " FROM tool_usage_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    first, second = rows
    assert first[1] == "memory_recall"
    assert first[2] == "claude"
    assert first[3] == 12.5
    assert first[4] == 1
    assert first[5] == 1024
    assert first[6] == ""
    assert second[1] == "context_supply"
    assert second[2] == "pi_builder"
    assert second[3] == 40.0
    assert second[4] == 0
    assert second[5] == 64
    assert second[6] == "ValueError: bad scope"
    for row in rows:
        assert isinstance(row[0], str) and row[0].endswith("Z")


def test_report_script_outputs_total_for_same_db(tmp_path):
    db_path = str(tmp_path / "telemetry.db")
    conn = sqlite3.connect(db_path)
    try:
        _ensure_tool_usage_table(conn)
        _record_tool_usage(conn, "memory_recall", "claude", 10.0, 1, 100, "")
        _record_tool_usage(conn, "memory_recall", "pi_fixer", 30.0, 0, 50, "RuntimeError: x")
    finally:
        conn.close()

    proc = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), "--db", db_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TOTAL | 2 | 1 |" in proc.stdout
