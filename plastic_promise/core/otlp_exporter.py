"""Best-effort OTLP/HTTP JSON export for local traceability tables."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from plastic_promise import __version__
from plastic_promise.core.traceability import ensure_traceability_schema

Sender = Callable[[str, bytes, dict[str, str], float], dict[str, Any] | None]


def _configured_endpoint(endpoint: str | None = None) -> str:
    value = (
        endpoint
        or os.environ.get("PP_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    ).strip()
    if not value:
        return ""
    if value.endswith("/v1/traces"):
        return value
    return value.rstrip("/") + "/v1/traces"


def _hex_id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def _unix_nano(value: str) -> str:
    if not value:
        return "0"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp() * 1_000_000_000))
    except Exception:
        return "0"


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": "" if value is None else str(value)}}


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _filters(project_id: str = "", since: str = "", until: str = "", timestamp_col: str = ""):
    clauses: list[str] = []
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if since:
        clauses.append(f"{timestamp_col} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{timestamp_col} <= ?")
        params.append(until)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _load_spans(conn, project_id: str, since: str, until: str) -> list[dict[str, Any]]:
    where, params = _filters(project_id, since, until, "started_at")
    rows = conn.execute(
        f"""
        SELECT call_id, parent_call_id, request_scope_id, stage_session_id,
               flow_line_id, project_id, tool_name, stage_name, caller,
               status, degraded, input_hash, output_hash, metadata_json,
               started_at, ended_at
        FROM call_spans
        {where}
        ORDER BY started_at ASC, call_id ASC
        """,
        params,
    ).fetchall()
    return [
        {
            "call_id": row[0],
            "parent_call_id": row[1],
            "request_scope_id": row[2],
            "stage_session_id": row[3],
            "flow_line_id": row[4],
            "project_id": row[5],
            "tool_name": row[6],
            "stage_name": row[7],
            "caller": row[8],
            "status": row[9],
            "degraded": bool(row[10]),
            "input_hash": row[11],
            "output_hash": row[12],
            "metadata": _json(row[13]),
            "started_at": row[14],
            "ended_at": row[15],
        }
        for row in rows
    ]


def _load_events(conn, project_id: str, since: str, until: str) -> dict[str, list[dict[str, Any]]]:
    where, params = _filters(project_id, since, until, "created_at")
    rows = conn.execute(
        f"""
        SELECT call_id, link_name, policy, level, error_class, error_message,
               fallback_used, minimum_result, user_visible, metadata_json, created_at
        FROM degradation_events
        {where}
        ORDER BY created_at ASC, event_id ASC
        """,
        params,
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(
            {
                "link_name": row[1],
                "policy": row[2],
                "level": row[3],
                "error_class": row[4],
                "error_message": row[5],
                "fallback_used": row[6],
                "minimum_result": row[7],
                "user_visible": bool(row[8]),
                "metadata": _json(row[9]),
                "created_at": row[10],
            }
        )
    return grouped


def _span_attributes(span: dict[str, Any]) -> list[dict[str, Any]]:
    attrs = [
        _attr("call.id", span["call_id"]),
        _attr("project.id", span["project_id"]),
        _attr("tool.name", span["tool_name"]),
        _attr("request_scope.id", span["request_scope_id"]),
        _attr("stage_session.id", span["stage_session_id"]),
        _attr("flow_line.id", span["flow_line_id"]),
        _attr("stage.name", span["stage_name"]),
        _attr("caller", span["caller"]),
        _attr("status", span["status"]),
        _attr("degraded", span["degraded"]),
    ]
    for key, value in span["metadata"].items():
        attrs.append(_attr(f"metadata.{key}", value))
    return attrs


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    attrs = [
        _attr("link.name", event["link_name"]),
        _attr("policy", event["policy"]),
        _attr("level", event["level"]),
        _attr("fallback.used", event["fallback_used"]),
        _attr("minimum.result", event["minimum_result"]),
        _attr("user_visible", event["user_visible"]),
    ]
    if event["error_class"]:
        attrs.append(_attr("error.class", event["error_class"]))
    if event["error_message"]:
        attrs.append(_attr("error.message", event["error_message"]))
    for key, value in event["metadata"].items():
        attrs.append(_attr(f"metadata.{key}", value))
    return {
        "timeUnixNano": _unix_nano(event["created_at"]),
        "name": "degradation",
        "attributes": attrs,
    }


def _build_payload(
    spans: list[dict[str, Any]],
    events_by_call: dict[str, list[dict[str, Any]]],
    project_id: str,
) -> dict[str, Any]:
    otlp_spans = []
    for span in spans:
        trace_seed = span["request_scope_id"] or span["call_id"]
        item = {
            "traceId": _hex_id(f"trace:{trace_seed}", 32),
            "spanId": _hex_id(f"span:{span['call_id']}", 16),
            "name": span["tool_name"],
            "kind": 1,
            "startTimeUnixNano": _unix_nano(span["started_at"]),
            "endTimeUnixNano": _unix_nano(span["ended_at"]),
            "attributes": _span_attributes(span),
            "events": [_event_payload(event) for event in events_by_call.get(span["call_id"], [])],
            "status": {"code": 2 if span["degraded"] or span["status"] != "success" else 1},
        }
        if span["parent_call_id"]:
            item["parentSpanId"] = _hex_id(f"span:{span['parent_call_id']}", 16)
        otlp_spans.append(item)

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", "plastic-promise"),
                        _attr("service.version", __version__),
                        _attr("project.id", project_id),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "plastic-promise.traceability",
                            "version": __version__,
                        },
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }


def _default_sender(url: str, body: bytes, headers: dict[str, str], timeout_s: float):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return {"status": getattr(response, "status", 0)}


def export_traceability_to_otlp(
    conn,
    *,
    project_id: str = "",
    since: str = "",
    until: str = "",
    endpoint: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 3.0,
    sender: Sender | None = None,
) -> dict[str, Any]:
    """Export local traceability rows to OTLP/HTTP JSON.

    The function is intentionally best-effort. Missing endpoint is a
    successful no-op; network and serialization errors are returned as
    structured status instead of being raised into MCP handlers.
    """
    url = _configured_endpoint(endpoint)
    if not url:
        return {
            "enabled": False,
            "success": True,
            "endpoint": "",
            "spans_exported": 0,
            "error": "",
        }

    try:
        ensure_traceability_schema(conn)
        spans = _load_spans(conn, project_id, since, until)
        events_by_call = _load_events(conn, project_id, since, until)
        if not spans:
            return {
                "enabled": True,
                "success": True,
                "endpoint": url,
                "spans_exported": 0,
                "error": "",
            }

        body = json.dumps(
            _build_payload(spans, events_by_call, project_id),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        (sender or _default_sender)(url, body, request_headers, timeout_s)
        return {
            "enabled": True,
            "success": True,
            "endpoint": url,
            "spans_exported": len(spans),
            "error": "",
        }
    except Exception as e:
        return {
            "enabled": True,
            "success": False,
            "endpoint": url,
            "spans_exported": len(spans) if "spans" in locals() else 0,
            "error": str(e),
            "error_class": e.__class__.__name__,
        }
