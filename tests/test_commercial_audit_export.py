import asyncio
import json
import sqlite3
from types import SimpleNamespace

from plastic_promise.core import traceability
from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    record_call_span,
    record_degradation_event,
    record_outbox_event,
)


def _engine_with_conn(conn):
    return SimpleNamespace(_sqlite=SimpleNamespace(_conn=conn))


def test_commercial_audit_export_filters_project_and_includes_outbox(tmp_path, monkeypatch):
    from plastic_promise.mcp.tools.commercial_audit import handle_commercial_audit_export

    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)
    timestamps = iter(
        [
            "2026-07-07T00:00:00Z",
            "2026-07-07T00:01:00Z",
            "2026-07-07T00:02:00Z",
            "2026-07-07T00:03:00Z",
            "2026-07-07T00:04:00Z",
            "2026-07-07T00:05:00Z",
        ]
    )
    monkeypatch.setattr(traceability, "utc_now", lambda: next(timestamps))

    record_call_span(
        conn,
        call_id="call_app",
        request_scope_id="scope_app",
        project_id="project:test-app",
        tool_name="memory_store",
        status="degraded",
        degraded=True,
        metadata={"release": "task3"},
    )
    record_degradation_event(
        conn,
        call_id="call_app",
        request_scope_id="scope_app",
        project_id="project:test-app",
        tool_name="memory_store",
        link_name="store_outbox",
        policy="best_effort",
        level="warning",
        fallback_used="store_outbox",
        minimum_result="outbox_record",
    )
    outbox_id = record_outbox_event(
        conn,
        tool_name="memory_store",
        project_id="project:test-app",
        call_id="call_app",
        status="pending",
        payload={"content": "durable minimum payload"},
    )
    record_call_span(
        conn,
        call_id="call_other",
        project_id="project:other",
        tool_name="memory_recall",
        status="success",
    )

    result = asyncio.run(
        handle_commercial_audit_export(
            _engine_with_conn(conn),
            {"project_id": "project:test-app", "include_outbox": True},
        )
    )
    payload = json.loads(result[0].text)

    assert payload["success"] is True
    assert payload["project_id"] == "project:test-app"
    assert payload["counts"] == {
        "call_spans": 1,
        "degradation_events": 1,
        "store_outbox": 1,
    }
    assert payload["call_spans"][0]["call_id"] == "call_app"
    assert payload["call_spans"][0]["metadata"] == {"release": "task3"}
    assert payload["degradation_events"][0]["fallback_used"] == "store_outbox"
    assert payload["store_outbox"][0]["outbox_id"] == outbox_id
    assert payload["store_outbox"][0]["payload"]["content"] == "durable minimum payload"
    conn.close()


def test_commercial_audit_export_time_window_filters_rows(tmp_path, monkeypatch):
    from plastic_promise.mcp.tools.commercial_audit import handle_commercial_audit_export

    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)
    timestamps = iter(["2026-07-07T00:00:00Z", "2026-07-07T01:00:00Z"])
    monkeypatch.setattr(traceability, "utc_now", lambda: next(timestamps))

    record_call_span(
        conn,
        call_id="call_old",
        project_id="project:test-app",
        tool_name="memory_recall",
        status="success",
    )
    record_call_span(
        conn,
        call_id="call_new",
        project_id="project:test-app",
        tool_name="context_supply",
        status="success",
    )

    result = asyncio.run(
        handle_commercial_audit_export(
            _engine_with_conn(conn),
            {
                "project_id": "project:test-app",
                "since": "2026-07-07T00:30:00Z",
                "include_outbox": False,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert [span["call_id"] for span in payload["call_spans"]] == ["call_new"]
    assert payload["store_outbox"] == []
    conn.close()
