import asyncio
import json
import sqlite3

from plastic_promise.core.traceability import ensure_traceability_schema
from plastic_promise.mcp import server as mcp_server
from plastic_promise.mcp.tools.task_queue import handle_task_enqueue


def test_runtime_events_schema_records_status_transitions(tmp_path):
    from plastic_promise.core.event_protocol import record_runtime_event

    conn = sqlite3.connect(tmp_path / "events.db")
    ensure_traceability_schema(conn)

    event_id = record_runtime_event(
        conn,
        event_kind="tool",
        event_name="memory_recall",
        status="pending",
        request_scope_id="scope_one",
        stage_session_id="stage_one",
        flow_line_id="flow_one",
        project_id="project:test",
        actor="codex",
        trust_tier="medium",
        defense_decision="allow",
        audit_trace={"call_id": "call_one"},
        metadata={"tool_name": "memory_recall"},
    )

    row = conn.execute(
        """
        SELECT event_id, event_kind, event_name, status, request_scope_id,
               trust_tier, defense_decision, audit_trace_json, metadata_json
        FROM runtime_events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()

    assert row[0] == event_id
    assert row[1:7] == (
        "tool",
        "memory_recall",
        "pending",
        "scope_one",
        "medium",
        "allow",
    )
    assert json.loads(row[7]) == {"call_id": "call_one"}
    assert json.loads(row[8]) == {"tool_name": "memory_recall"}
    conn.close()


def test_mcp_call_tool_records_completed_and_error_events(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "plastic.db"))
    monkeypatch.setattr(mcp_server, "_engine", None)

    asyncio.run(
        mcp_server.call_tool(
            "runtime_mode",
            {
                "action": "get",
                "stage_session_id": "stage_evt",
                "flow_line_id": "flow_evt",
                "request_id": "ok",
                "trust_score": 0.58,
            },
        )
    )
    missing = asyncio.run(
        mcp_server.call_tool(
            "__missing_tool__",
            {
                "stage_session_id": "stage_evt",
                "flow_line_id": "flow_evt",
                "request_id": "missing",
            },
        )
    )

    assert "Unknown tool" in missing[0].text
    conn = sqlite3.connect(tmp_path / "plastic.db")
    rows = conn.execute(
        """
        SELECT event_name, status, request_scope_id, defense_decision
        FROM runtime_events
        WHERE event_kind = 'tool'
        ORDER BY event_rowid
        """
    ).fetchall()
    conn.close()

    assert ("runtime_mode", "pending", "stage_evt::flow:flow_evt::req:ok", "ask") in rows
    assert ("runtime_mode", "running", "stage_evt::flow:flow_evt::req:ok", "ask") in rows
    assert ("runtime_mode", "completed", "stage_evt::flow:flow_evt::req:ok", "ask") in rows
    assert ("__missing_tool__", "error", "stage_evt::flow:flow_evt::req:missing", "allow") in rows


def test_task_enqueue_records_task_runtime_event(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "tasks.db"))

    class MockEngine:
        pass

    result = asyncio.run(
        handle_task_enqueue(
            MockEngine(),
            {
                "task_type": "build_feature",
                "title": "Build runtime event test",
                "to_agent": "pi_builder",
                "priority": 3,
                "from_agent": "claude",
                "description": "Record a task event",
                "source_scan": "test",
                "stage_session_id": "stage_task",
                "flow_line_id": "flow_task",
                "request_id": "enqueue",
            },
        )
    )
    task_id = json.loads(result[0].text)["task_id"]

    conn = sqlite3.connect(tmp_path / "tasks.db")
    row = conn.execute(
        """
        SELECT event_kind, event_name, status, actor, metadata_json
        FROM runtime_events
        WHERE event_kind = 'task' AND event_name = 'task_enqueue'
        """
    ).fetchone()
    conn.close()

    assert row[0:4] == ("task", "task_enqueue", "pending", "claude")
    assert json.loads(row[4])["task_id"] == task_id
