import asyncio
import json
import sqlite3
from types import SimpleNamespace

from plastic_promise.core import traceability
from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    record_call_span,
    record_degradation_event,
)


def _conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)
    return conn


def test_otlp_exporter_noops_without_endpoint(tmp_path, monkeypatch):
    from plastic_promise.core.otlp_exporter import export_traceability_to_otlp

    monkeypatch.delenv("PP_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    conn = _conn(tmp_path)

    result = export_traceability_to_otlp(conn, project_id="project:test-app")

    assert result == {
        "enabled": False,
        "success": True,
        "endpoint": "",
        "spans_exported": 0,
        "error": "",
    }
    conn.close()


def test_otlp_exporter_builds_trace_payload_from_traceability_tables(tmp_path, monkeypatch):
    from plastic_promise.core.otlp_exporter import export_traceability_to_otlp

    conn = _conn(tmp_path)
    timestamps = iter(["2026-07-07T00:00:00Z", "2026-07-07T00:00:01Z"])
    monkeypatch.setattr(traceability, "utc_now", lambda: next(timestamps))
    record_call_span(
        conn,
        call_id="call_app",
        request_scope_id="scope_app",
        stage_session_id="stage_one",
        flow_line_id="flow_one",
        project_id="project:test-app",
        tool_name="memory_store",
        status="degraded",
        degraded=True,
        metadata={"route": "release"},
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

    sent = {}

    def sender(url, body, headers, timeout_s):
        sent["url"] = url
        sent["payload"] = json.loads(body.decode("utf-8"))
        sent["headers"] = headers
        sent["timeout_s"] = timeout_s
        return {"status": 200}

    result = export_traceability_to_otlp(
        conn,
        project_id="project:test-app",
        endpoint="http://collector:4318",
        sender=sender,
    )

    assert result["enabled"] is True
    assert result["success"] is True
    assert result["endpoint"] == "http://collector:4318/v1/traces"
    assert result["spans_exported"] == 1
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["url"] == "http://collector:4318/v1/traces"

    resource_spans = sent["payload"]["resourceSpans"]
    attrs = resource_spans[0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": "plastic-promise"}} in attrs
    span = resource_spans[0]["scopeSpans"][0]["spans"][0]
    assert span["name"] == "memory_store"
    assert span["attributes"]
    assert span["events"][0]["name"] == "degradation"
    assert span["status"]["code"] == 2
    conn.close()


def test_otlp_exporter_reports_sender_failure_without_raising(tmp_path):
    from plastic_promise.core.otlp_exporter import export_traceability_to_otlp

    conn = _conn(tmp_path)
    record_call_span(
        conn,
        call_id="call_app",
        project_id="project:test-app",
        tool_name="memory_recall",
        status="success",
    )

    def sender(url, body, headers, timeout_s):
        raise RuntimeError("collector unavailable")

    result = export_traceability_to_otlp(
        conn,
        project_id="project:test-app",
        endpoint="http://collector:4318/v1/traces",
        sender=sender,
    )

    assert result["enabled"] is True
    assert result["success"] is False
    assert result["spans_exported"] == 1
    assert result["error_class"] == "RuntimeError"
    assert "collector unavailable" in result["error"]
    conn.close()


def test_commercial_audit_export_can_attach_otlp_result(tmp_path, monkeypatch):
    from plastic_promise.mcp.tools.commercial_audit import handle_commercial_audit_export

    conn = _conn(tmp_path)
    record_call_span(
        conn,
        call_id="call_app",
        project_id="project:test-app",
        tool_name="memory_store",
        status="success",
    )

    def fake_export(conn_arg, **kwargs):
        assert conn_arg is conn
        assert kwargs["project_id"] == "project:test-app"
        assert kwargs["endpoint"] == "http://collector:4318"
        return {
            "enabled": True,
            "success": True,
            "endpoint": "http://collector:4318/v1/traces",
            "spans_exported": 1,
            "error": "",
        }

    monkeypatch.setattr(
        "plastic_promise.core.otlp_exporter.export_traceability_to_otlp",
        fake_export,
    )
    engine = SimpleNamespace(_sqlite=SimpleNamespace(_conn=conn))

    result = asyncio.run(
        handle_commercial_audit_export(
            engine,
            {
                "project_id": "project:test-app",
                "export_otlp": True,
                "otlp_endpoint": "http://collector:4318",
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["success"] is True
    assert payload["otlp_export"]["spans_exported"] == 1
    conn.close()
