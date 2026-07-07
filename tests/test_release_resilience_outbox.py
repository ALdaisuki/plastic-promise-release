import json
import sqlite3

import asyncio

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    record_outbox_event,
)
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.memory import handle_memory_store


def test_record_outbox_event_persists_to_sqlite(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)

    outbox_id = record_outbox_event(
        conn,
        tool_name="memory_store",
        project_id="project:test-app",
        call_id="call_store",
        status="stored",
        payload={"content": "minimum durable payload"},
        error_class="OperationalError",
        error_message="database is locked",
        metadata={"fallback": "sqlite"},
    )

    row = conn.execute(
        """
        SELECT outbox_id, tool_name, project_id, call_id, status,
               payload_json, error_class, error_message, metadata_json
        FROM store_outbox
        WHERE outbox_id = ?
        """,
        (outbox_id,),
    ).fetchone()

    assert row[:5] == (
        outbox_id,
        "memory_store",
        "project:test-app",
        "call_store",
        "stored",
    )
    assert json.loads(row[5]) == {"content": "minimum durable payload"}
    assert row[6:8] == ("OperationalError", "database is locked")
    assert json.loads(row[8]) == {"fallback": "sqlite"}
    conn.close()


def test_record_outbox_event_falls_back_to_jsonl(tmp_path):
    class BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    fallback_path = tmp_path / "store_outbox.jsonl"

    outbox_id = record_outbox_event(
        BrokenConn(),
        tool_name="memory_store",
        project_id="project:test-app",
        call_id="call_store",
        status="stored",
        payload={"content": "minimum durable payload"},
        error_class="OperationalError",
        error_message="database is locked",
        fallback_path=fallback_path,
    )

    lines = fallback_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outbox_id"] == outbox_id
    assert record["project_id"] == "project:test-app"
    assert record["payload"]["content"] == "minimum durable payload"
    assert record["error_class"] == "OperationalError"


def test_memory_store_failure_returns_outbox_minimum_result(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    engine = ContextEngine()

    def fail_fuzzy_buffer(_engine):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", fail_fuzzy_buffer)

    result = asyncio.run(
        handle_memory_store(
            engine,
            {
                "content": "release outbox fallback memory",
                "memory_type": "experience",
                "source": "codex",
                "project_id": "project:test-app",
                "call_id": "call_outbox",
                "max_llm_calls": 0,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["stored"] is False
    assert payload["degraded"] is True
    assert payload["fallback_used"] == ["store_outbox"]
    assert payload["minimum_result"] == "outbox_record"
    assert payload["trace"]["call_id"] == "call_outbox"
    assert payload["project_id"] == "project:test-app"
    assert payload["outbox_id"].startswith("outbox_")

    row = engine._sqlite._conn.execute(
        "SELECT project_id, call_id, status, payload_json FROM store_outbox WHERE outbox_id = ?",
        (payload["outbox_id"],),
    ).fetchone()
    span = engine._sqlite._conn.execute(
        "SELECT project_id, tool_name, status, degraded FROM call_spans WHERE call_id = ?",
        ("call_outbox",),
    ).fetchone()
    event = engine._sqlite._conn.execute(
        """
        SELECT project_id, tool_name, link_name, fallback_used, minimum_result
        FROM degradation_events
        WHERE call_id = ?
        """,
        ("call_outbox",),
    ).fetchone()
    assert row[:3] == ("project:test-app", "call_outbox", "pending")
    assert json.loads(row[3])["content"] == "release outbox fallback memory"
    assert span == ("project:test-app", "memory_store", "degraded", 1)
    assert event == (
        "project:test-app",
        "memory_store",
        "store_outbox",
        "store_outbox",
        "outbox_record",
    )
    engine._sqlite._conn.close()
