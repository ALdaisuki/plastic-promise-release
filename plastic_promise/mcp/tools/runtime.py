"""MCP runtime mode tool."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from plastic_promise.launcher.runtime_mode import (
    apply_runtime_mode,
    runtime_mode_status,
)


async def handle_runtime_mode(engine: Any, args: dict) -> list[TextContent]:
    """Get or hot-update the current MCP process runtime mode."""
    action = args.get("action", "get")
    if action == "get":
        return [
            TextContent(
                type="text",
                text=json.dumps(runtime_mode_status(), ensure_ascii=False, indent=2),
            )
        ]

    if action != "set":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "action must be 'get' or 'set'", "tool": "runtime_mode"},
                    ensure_ascii=False,
                ),
            )
        ]

    requested_mode = args.get("mode")
    if not requested_mode:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "mode is required when action='set'", "tool": "runtime_mode"},
                    ensure_ascii=False,
                ),
            )
        ]

    try:
        mode = apply_runtime_mode(requested_mode)
    except ValueError as exc:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(exc), "tool": "runtime_mode"}, ensure_ascii=False),
            )
        ]

    refresh = {"called": False, "initialize_heavy": mode.depth in {"normal", "full"}}
    try:
        if hasattr(engine, "refresh_runtime_mode"):
            engine.refresh_runtime_mode(initialize_heavy=refresh["initialize_heavy"])
            refresh["called"] = True
        else:
            if hasattr(engine, "reset_rust_health"):
                engine.reset_rust_health()
            if refresh["initialize_heavy"] and hasattr(engine, "ensure_heavy_init"):
                engine.ensure_heavy_init()
            refresh["called"] = True
    except Exception as exc:
        refresh["error"] = str(exc)

    status = runtime_mode_status()
    status["action"] = "set"
    status["refresh"] = refresh
    return [TextContent(type="text", text=json.dumps(status, ensure_ascii=False, indent=2))]
