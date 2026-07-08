"""MGP shadow bridge MCP handler."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.mgp_shadow import MgpShadowBridge

_bridge = MgpShadowBridge()


async def handle_mgp_shadow_bridge(engine: Any, args: dict) -> list[TextContent]:
    action = args.get("action", "status")

    try:
        if action == "status":
            payload = _bridge.status()
        elif action == "set_mode":
            payload = _bridge.set_mode(args.get("mode", "off"))
        elif action == "evaluate":
            bridge = MgpShadowBridge(mode=args.get("mode") or _bridge.mode)
            envelope = {
                "operation": args.get("operation", ""),
                "subject": args.get("subject", ""),
                "content": args.get("content", ""),
                "metadata": args.get("metadata", {}),
                "policy_context": args.get("policy_context", {}),
            }
            payload = bridge.evaluate(envelope, engine=engine)
        else:
            payload = {
                "error": f"Unknown action '{action}'. Valid: status, set_mode, evaluate",
                "tool": "mgp_shadow_bridge",
            }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]
    except Exception as exc:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": str(exc), "tool": "mgp_shadow_bridge"},
                    ensure_ascii=False,
                ),
            )
        ]
