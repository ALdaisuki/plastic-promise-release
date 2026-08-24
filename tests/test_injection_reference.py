"""Injection→reference tracking unit tests (temp DB, no real store touched)."""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plastic_promise.mcp.tools import injection_tracking as it  # noqa: E402


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    monkeypatch.setattr(it, "get_db_path", lambda: str(db))
    return str(db)


def test_record_and_mark_roundtrip(temp_db):
    conn = sqlite3.connect(temp_db)
    it.ensure_tables(conn)
    conn.close()

    it.record_injection("sess-1", "session_brief", ["m-aaa", "m-bbb"], 120)
    it.record_injection("sess-1", "session_brief", ["m-ccc"], 60)

    marked = it.mark_references("sess-1", ["m-bbb"])
    assert marked >= 1

    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT referenced FROM context_injection_events ORDER BY id").fetchall()
    assert rows[0][0] == 1  # m-aaa,m-bbb 行：m-bbb 被引用一次
    assert rows[1][0] == 0  # m-ccc 行：未被引用
    conn.close()


def test_mark_references_session_scoped(temp_db):
    conn = sqlite3.connect(temp_db)
    it.ensure_tables(conn)
    conn.close()

    it.record_injection("sess-A", "session_brief", ["m-shared"], 10)
    it.record_injection("sess-B", "session_brief", ["m-shared"], 10)

    it.mark_references("sess-B", ["m-shared"])

    conn = sqlite3.connect(temp_db)
    a = conn.execute(
        "SELECT referenced FROM context_injection_events WHERE session_id='sess-A'"
    ).fetchone()[0]
    b = conn.execute(
        "SELECT COALESCE(referenced,0) FROM context_injection_events WHERE session_id='sess-B'"
    ).fetchone()[0]
    assert a == 0
    assert b == 1
    conn.close()


def test_tracking_failures_are_silent(temp_db, monkeypatch):
    monkeypatch.setattr(it, "get_db_path", lambda: "/nonexistent/dir/x.db")
    # 不抛异常即通过：坏路径下 record/mark 均静默
    it.record_injection("s", "session_brief", ["m"], 1)
    assert it.mark_references("s", ["m"]) == 0


def test_report_script_totals(temp_db, capsys):
    conn = sqlite3.connect(temp_db)
    it.ensure_tables(conn)
    conn.close()
    it.record_injection("sess-R", "session_brief", ["m-1"], 50)

    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "tool_usage_report.py"),
            "--db",
            temp_db,
            "--days",
            "14",
        ],
        capture_output=True,
        text=True,
    )
    # 只注入未引用：referenced_rows=0, rate=0.0%
    assert "context_injections | 1 | 1 | 0" in out.stdout
