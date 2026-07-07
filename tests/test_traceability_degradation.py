import asyncio
import sqlite3
import json

from plastic_promise.core import traceability
from plastic_promise.core.traceability import (
    build_envelope,
    ensure_traceability_schema,
    new_call_id,
    record_call_span,
    record_degradation_event,
)


def test_new_call_id_has_call_prefix():
    assert new_call_id().startswith("call_")


def test_envelope_marks_degraded_from_warnings():
    payload = build_envelope(
        data={"ok": True},
        trace={"call_id": "call_x", "project_id": "project:test"},
        warnings=["embedding failed; used text retrieval"],
        fallback_used=["text_retrieval"],
        minimum_result="text_context",
    )

    assert payload["success"] is True
    assert payload["degraded"] is True
    assert payload["degrade_level"] == "warning"
    assert payload["fallback_used"] == ["text_retrieval"]
    assert payload["minimum_result"] == "text_context"
    assert payload["data"] == {"ok": True}


def test_traceability_schema_records_span_and_degradation(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)

    record_call_span(
        conn,
        call_id="call_one",
        parent_call_id="call_parent",
        request_scope_id="scope_one",
        stage_session_id="stage_one",
        flow_line_id="flow_one",
        project_id="project:test",
        tool_name="memory_recall",
        status="success",
        degraded=True,
        metadata={"route": "normal-development"},
    )
    record_degradation_event(
        conn,
        call_id="call_one",
        request_scope_id="scope_one",
        project_id="project:test",
        tool_name="memory_recall",
        link_name="embedding",
        policy="best_effort",
        level="warning",
        error_class="RuntimeError",
        error_message="embedder unavailable",
        fallback_used="text_retrieval",
        minimum_result="text_context",
    )

    span = conn.execute("SELECT project_id, tool_name, degraded FROM call_spans").fetchone()
    event = conn.execute(
        "SELECT project_id, link_name, fallback_used FROM degradation_events"
    ).fetchone()

    assert span == ("project:test", "memory_recall", 1)
    assert event == ("project:test", "embedding", "text_retrieval")
    conn.close()


def test_record_call_span_updates_without_replacing_started_at(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)
    timestamps = iter(["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"])
    monkeypatch.setattr(traceability, "utc_now", lambda: next(timestamps))

    record_call_span(
        conn,
        call_id="call_repeat",
        parent_call_id="call_parent",
        request_scope_id="scope_one",
        stage_session_id="stage_one",
        flow_line_id="flow_one",
        project_id="project:test",
        tool_name="memory_recall",
        status="running",
        degraded=False,
        metadata={"route": "first"},
    )
    record_call_span(
        conn,
        call_id="call_repeat",
        parent_call_id="call_parent_updated",
        request_scope_id="scope_two",
        stage_session_id="stage_two",
        flow_line_id="flow_two",
        project_id="project:test",
        tool_name="memory_recall",
        status="success",
        degraded=True,
        metadata={"route": "second"},
    )

    row_count = conn.execute(
        "SELECT COUNT(*) FROM call_spans WHERE call_id = ?",
        ("call_repeat",),
    ).fetchone()[0]
    span = conn.execute(
        """
        SELECT started_at, ended_at, status, degraded, parent_call_id, metadata_json
        FROM call_spans
        WHERE call_id = ?
        """,
        ("call_repeat",),
    ).fetchone()

    assert row_count == 1
    assert span[0] == "2026-01-01T00:00:00Z"
    assert span[1] == "2026-01-01T00:01:00Z"
    assert span[2:5] == ("success", 1, "call_parent_updated")
    assert json.loads(span[5]) == {"route": "second"}
    conn.close()


def test_schema_migrates_legacy_call_span_timestamps(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    conn.execute(
        """
        CREATE TABLE call_spans (
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    ensure_traceability_schema(conn)
    record_call_span(
        conn,
        call_id="call_legacy",
        project_id="project:test",
        tool_name="memory_recall",
        status="success",
    )

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(call_spans)").fetchall()
    }
    row_count = conn.execute(
        "SELECT COUNT(*) FROM call_spans WHERE call_id = ?",
        ("call_legacy",),
    ).fetchone()[0]

    assert "started_at" in columns
    assert "ended_at" in columns
    assert row_count == 1
    conn.close()


def test_context_supply_prompt_includes_project_warning(monkeypatch):
    from plastic_promise.mcp.tools.context import handle_context_supply

    class FakePack:
        def __init__(self):
            self.audit_metadata = {}

        def to_prompt(self):
            return str(self.audit_metadata)

    class FakeEngine:
        def supply(self, task_description, task_vector, task_type, scope):
            return FakePack()

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0] * 4

    monkeypatch.setattr(
        "plastic_promise.core.embedder.get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )

    result = asyncio.run(
        handle_context_supply(FakeEngine(), {"task_description": "review", "scope": "building"})
    )
    text = result[0].text

    assert "project_id unresolved" in text
    assert "project:unknown" in text
    assert "project_restricted_context" in text


def test_memory_recall_records_call_span(tmp_path, monkeypatch):
    from plastic_promise.core.context_engine import ContextEngine, ContextPack
    from plastic_promise.mcp.tools.memory import handle_memory_recall

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0] * 4

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(
        "plastic_promise.core.embedder.get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )
    monkeypatch.setattr("plastic_promise.adaptive_retrieval.should_retrieve", lambda query: True)
    engine = ContextEngine()
    engine.supply = lambda query, vec, task_type, scope, debug=False: ContextPack()

    asyncio.run(
        handle_memory_recall(
            engine,
            {
                "query": "release trace",
                "project_id": "project:test-app",
                "call_id": "call_recall_trace",
                "request_id": "req:recall-trace",
            },
        )
    )

    row = engine._sqlite._conn.execute(
        "SELECT project_id, tool_name, status, degraded FROM call_spans WHERE call_id = ?",
        ("call_recall_trace",),
    ).fetchone()
    assert row == ("project:test-app", "memory_recall", "success", 0)
    engine._sqlite._conn.close()


def test_context_supply_unknown_project_records_degradation_event(tmp_path, monkeypatch):
    from plastic_promise.core.context_engine import ContextEngine, ContextPack
    from plastic_promise.mcp.tools.context import handle_context_supply

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0] * 4

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(
        "plastic_promise.core.embedder.get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )
    engine = ContextEngine()
    engine.supply = lambda task_description, task_vector, task_type, scope: ContextPack()

    asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "release trace",
                "scope": "building",
                "call_id": "call_context_trace",
                "request_id": "req:context-trace",
            },
        )
    )

    span = engine._sqlite._conn.execute(
        "SELECT project_id, tool_name, status, degraded FROM call_spans WHERE call_id = ?",
        ("call_context_trace",),
    ).fetchone()
    event = engine._sqlite._conn.execute(
        """
        SELECT project_id, tool_name, link_name, fallback_used, minimum_result
        FROM degradation_events
        WHERE call_id = ?
        """,
        ("call_context_trace",),
    ).fetchone()

    assert span == ("project:unknown", "context_supply", "success", 1)
    assert event == (
        "project:unknown",
        "context_supply",
        "project_context",
        "project_restricted_context",
        "project_restricted_context",
    )
    engine._sqlite._conn.close()
