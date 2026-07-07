"""Commercial audit export over persisted traceability tables."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.traceability import ensure_traceability_schema, utc_now


def _conn(engine: Any):
    sqlite = getattr(engine, "_sqlite", None)
    return getattr(sqlite, "_conn", None)


def _json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _filters(args: dict[str, Any], timestamp_col: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    project_id = str(args.get("project_id") or "").strip()
    since = str(args.get("since") or "").strip()
    until = str(args.get("until") or "").strip()

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


def _call_spans(conn, args: dict[str, Any]) -> list[dict[str, Any]]:
    where, params = _filters(args, "started_at")
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
            "metadata": _json(row[13], {}),
            "started_at": row[14],
            "ended_at": row[15],
        }
        for row in rows
    ]


def _degradation_events(conn, args: dict[str, Any]) -> list[dict[str, Any]]:
    where, params = _filters(args, "created_at")
    rows = conn.execute(
        f"""
        SELECT event_id, call_id, request_scope_id, project_id, tool_name,
               link_name, policy, level, error_class, error_message,
               fallback_used, minimum_result, user_visible, metadata_json,
               created_at
        FROM degradation_events
        {where}
        ORDER BY created_at ASC, event_id ASC
        """,
        params,
    ).fetchall()
    return [
        {
            "event_id": row[0],
            "call_id": row[1],
            "request_scope_id": row[2],
            "project_id": row[3],
            "tool_name": row[4],
            "link_name": row[5],
            "policy": row[6],
            "level": row[7],
            "error_class": row[8],
            "error_message": row[9],
            "fallback_used": row[10],
            "minimum_result": row[11],
            "user_visible": bool(row[12]),
            "metadata": _json(row[13], {}),
            "created_at": row[14],
        }
        for row in rows
    ]


def _store_outbox(conn, args: dict[str, Any]) -> list[dict[str, Any]]:
    if not args.get("include_outbox", False):
        return []
    where, params = _filters(args, "created_at")
    rows = conn.execute(
        f"""
        SELECT outbox_id, tool_name, project_id, call_id, status,
               payload_json, error_class, error_message, metadata_json,
               created_at
        FROM store_outbox
        {where}
        ORDER BY created_at ASC, outbox_id ASC
        """,
        params,
    ).fetchall()
    return [
        {
            "outbox_id": row[0],
            "tool_name": row[1],
            "project_id": row[2],
            "call_id": row[3],
            "status": row[4],
            "payload": _json(row[5], {}),
            "error_class": row[6],
            "error_message": row[7],
            "metadata": _json(row[8], {}),
            "created_at": row[9],
        }
        for row in rows
    ]


async def handle_commercial_audit_export(engine: Any, args: dict) -> list[TextContent]:
    """Export a project-filterable audit bundle from SQLite truth tables."""
    try:
        conn = _conn(engine)
        if conn is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "tool": "commercial_audit_export",
                            "error": "traceability database unavailable",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        ensure_traceability_schema(conn)
        spans = _call_spans(conn, args)
        events = _degradation_events(conn, args)
        outbox = _store_outbox(conn, args)
        payload = {
            "success": True,
            "tool": "commercial_audit_export",
            "generated_at": utc_now(),
            "project_id": str(args.get("project_id") or ""),
            "filters": {
                "project_id": str(args.get("project_id") or ""),
                "since": str(args.get("since") or ""),
                "until": str(args.get("until") or ""),
                "include_outbox": bool(args.get("include_outbox", False)),
            },
            "counts": {
                "call_spans": len(spans),
                "degradation_events": len(events),
                "store_outbox": len(outbox),
            },
            "call_spans": spans,
            "degradation_events": events,
            "store_outbox": outbox,
        }
        if args.get("export_otlp", False):
            from plastic_promise.core.otlp_exporter import export_traceability_to_otlp

            payload["otlp_export"] = export_traceability_to_otlp(
                conn,
                project_id=str(args.get("project_id") or ""),
                since=str(args.get("since") or ""),
                until=str(args.get("until") or ""),
                endpoint=str(args.get("otlp_endpoint") or "") or None,
            )
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "tool": "commercial_audit_export",
                        "error": str(e),
                        "error_class": e.__class__.__name__,
                    },
                    ensure_ascii=False,
                ),
            )
        ]
