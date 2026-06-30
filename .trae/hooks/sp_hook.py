#!/usr/bin/env python
"""sp_hook.py — Trae Hooks 桥接 skill_auto_track

通过 Trae hooks.json 配置调用，接收 stdin JSON 事件，
调用 MCP 服务器的 /api/skill-track 端点，与 Claude Code 走同一追踪管道。

配置方式 (hooks.json):
{
  "hooks": {
    "PreToolUse": [{"name": "sp-pre-tool", "enabled": true,
      "matcher": "run_mcp", "command": "python .trae/hooks/sp_hook.py"}],
    "PostToolUse": [{"name": "sp-post-tool", "enabled": true,
      "matcher": "run_mcp", "command": "python .trae/hooks/sp_hook.py"}]
  }
}
"""

import json
import sys
import urllib.request
import traceback

MCP_URL = "http://127.0.0.1:9020/api/skill-track"


def _extract_mcp_call(payload):
    """从 Trae hook payload 中提取 MCP 工具调用信息。

    返回 (server_name, tool_name, args) 或 (None, None, None)。
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    if tool_name == "run_mcp":
        return (
            tool_input.get("server_name", ""),
            tool_input.get("tool_name", ""),
            tool_input.get("args", {}),
        )
    return (None, tool_name, tool_input if isinstance(tool_input, dict) else {})


def _call_skill_auto_track(phase, skill_name):
    """调用 MCP 服务器的 /api/skill-track 端点。"""
    try:
        data = json.dumps({"phase": phase, "skill_name": skill_name}).encode("utf-8")
        req = urllib.request.Request(
            MCP_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def handle_pre_tool_use(payload):
    """PreToolUse: 检测 sp-stage 调用，触发 skill_auto_track(phase="start")。"""
    server, tool_name, args = _extract_mcp_call(payload)

    if server != "mcp_plastic-promise" or tool_name != "sp-stage":
        return {"continue": True}

    stage = args.get("stage", "")
    if stage:
        _call_skill_auto_track("start", stage)

    return {"continue": True}


def handle_post_tool_use(payload):
    """PostToolUse: 检测 sp-stage 完成，触发 skill_auto_track(phase="complete")。"""
    server, tool_name, args = _extract_mcp_call(payload)

    if server != "mcp_plastic-promise" or tool_name != "sp-stage":
        return {"continue": True}

    stage = args.get("stage", "")
    if stage:
        _call_skill_auto_track("complete", stage)

    return {"continue": True}


_HANDLERS = {
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
}


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"continue": True}))
            return 0

        payload = json.loads(raw)
        event_name = payload.get("hook_event_name", "")

        handler = _HANDLERS.get(event_name)
        if handler:
            result = handler(payload)
        else:
            result = {"continue": True}

        print(json.dumps(result, ensure_ascii=False))
        return 0

    except Exception:
        print(json.dumps({"continue": True}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(main())