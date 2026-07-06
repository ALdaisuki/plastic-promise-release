"""Request-scope helpers for heavy MCP calls."""

from __future__ import annotations

import uuid


def _clean(value: object) -> str:
    return str(value or "").strip()


def build_request_scope(args: dict | None, tool_name: str) -> dict[str, str]:
    """Return explicit request identity for long-running MCP operations."""
    args = args or {}
    stage_session_id = _clean(args.get("stage_session_id") or args.get("stage_id"))
    flow_line_id = _clean(args.get("flow_line_id") or args.get("flow_id"))
    request_id = _clean(args.get("request_id"))

    if not stage_session_id:
        stage_session_id = f"session:{tool_name}:default"
    if not flow_line_id:
        flow_line_id = "default"
    if not request_id:
        request_id = f"req:{uuid.uuid4().hex[:12]}"

    return {
        "stage_session_id": stage_session_id,
        "flow_line_id": flow_line_id,
        "request_id": request_id,
        "request_scope_id": f"{stage_session_id}::flow:{flow_line_id}::req:{request_id}",
    }
