import asyncio
import json
from types import SimpleNamespace

import jsonschema

import plastic_promise.mcp.server as mcp_server


def _tools_by_name():
    return {tool.name: tool for tool in asyncio.run(mcp_server.list_tools())}


def _validate(tool_name, payload):
    tool = _tools_by_name()[tool_name]
    jsonschema.validate(instance=payload, schema=tool.inputSchema)


def test_claude_code_payloads_validate_against_exposed_schemas():
    payloads = {
        "principle_activate": {
            "task_type": "architecture",
            "task_description": "Git 仓库治理：补充 .gitignore 黑名单并清理不必要测试文件",
            "max_principles": 5,
            "domain_hint": "governing",
        },
        "memory_recall": {
            "query": "Git 仓库治理 .gitignore 黑名单 测试文件 清理",
            "task_type": "debugging",
            "max_results": 5,
            "min_relevance": 0.2,
            "include_principles": True,
            "strict": False,
            "debug": False,
            "stage_session_id": "stage:codex:schema",
            "flow_line_id": "bug-hunt",
            "request_id": "req:schema-memory",
        },
        "context_supply": {
            "task_description": "审计 MCP 参数校验失败",
            "task_type": "debugging",
            "scope": "governing",
            "stage_session_id": "stage:codex:schema",
            "flow_line_id": "bug-hunt",
            "request_id": "req:schema-context",
        },
        "defense": {"action": "get"},
        "runtime_mode": {"action": "set", "mode": "rust-full"},
        "audit_pre_check": {
            "action_description": "写入 MCP schema validation hardening spec/plan",
            "action_type": "write",
        },
        "session-init": {
            "task_description": "会话启动",
            "task_type": "debugging",
        },
    }

    for tool_name, payload in payloads.items():
        _validate(tool_name, payload)


def test_handler_read_optional_fields_are_declared():
    tools = _tools_by_name()

    memory_props = set(tools["memory_recall"].inputSchema["properties"])
    assert {
        "scope",
        "domain_hint",
        "federation",
        "pack",
        "stage_session_id",
        "flow_line_id",
        "request_id",
    }.issubset(memory_props)

    context_props = set(tools["context_supply"].inputSchema["properties"])
    assert {"stage_session_id", "flow_line_id", "request_id"}.issubset(context_props)

    defense_props = set(tools["defense"].inputSchema["properties"])
    assert "target" in defense_props

    runtime_mode_props = set(tools["runtime_mode"].inputSchema["properties"])
    assert {"action", "mode"}.issubset(runtime_mode_props)

    context_graph = tools["context_graph"].inputSchema["properties"]["query_type"]
    assert set(context_graph["enum"]) == {"node_info", "traverse", "full_graph", "neighbors"}

    session_props = set(tools["session-init"].inputSchema["properties"])
    assert {"context_mode", "context_timeout_s", "scope", "route", "flow_line_id"}.issubset(
        session_props
    )


def test_hyphenated_skill_aliases_are_exposed_with_matching_required_fields():
    tools = _tools_by_name()
    aliases = {
        "session_init": "session-init",
        "smart_remember": "smart-remember",
        "step_closure": "step-closure",
        "sp_stage": "sp-stage",
    }

    for alias, canonical in aliases.items():
        assert alias in tools
        assert canonical in tools
        assert tools[alias].inputSchema.get("required", []) == tools[canonical].inputSchema.get(
            "required", []
        )


def test_sp_stage_schema_exposes_full_superpowers_skill_surface():
    tools = _tools_by_name()
    enum_values = set(tools["sp-stage"].inputSchema["properties"]["stage"]["enum"])

    assert {"using-superpowers", "writing-skills"}.issubset(enum_values)


def test_sp_stage_description_lists_every_exposed_stage():
    tool = _tools_by_name()["sp-stage"]
    enum_values = set(tool.inputSchema["properties"]["stage"]["enum"])
    missing = sorted(stage for stage in enum_values if stage not in (tool.description or ""))

    assert not missing


def test_codex_deferred_tool_discovery_keywords_are_exposed():
    tools = _tools_by_name()
    expected_keywords = {
        "session-init": ["Plastic Promise MCP", "Codex", "tool_search", "bootstrap"],
        "sp-stage": ["Plastic Promise MCP", "Codex", "tool_search", "SuperPowers"],
        "memory_recall": ["Plastic Promise MCP", "Codex", "tool_search", "memory recall"],
        "context_supply": ["Plastic Promise MCP", "Codex", "tool_search", "context supply"],
        "defense": ["Plastic Promise MCP", "Codex", "tool_search", "trust"],
        "runtime_mode": ["Plastic Promise MCP", "Codex", "tool_search", "runtime mode"],
        "session_init": ["Plastic Promise MCP", "Codex", "tool_search", "bootstrap"],
        "sp_stage": ["Plastic Promise MCP", "Codex", "tool_search", "SuperPowers"],
        "step_closure": ["Plastic Promise MCP", "Codex", "tool_search", "step closure"],
    }

    for tool_name, keywords in expected_keywords.items():
        description = tools[tool_name].description or ""
        missing = [keyword for keyword in keywords if keyword not in description]
        assert not missing, f"{tool_name} description is missing discovery keywords: {missing}"


def test_normalized_schemas_reject_unknown_fields():
    tool = _tools_by_name()["defense"]
    try:
        jsonschema.validate(instance={"action": "get", "unexpected": True}, schema=tool.inputSchema)
    except jsonschema.ValidationError:
        return
    raise AssertionError("defense schema should reject unknown fields")


def test_lightweight_router_calls_accept_valid_payloads(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())

    principle = asyncio.run(
        mcp_server.call_tool(
            "principle_activate",
            {"task_type": "debugging", "task_description": "参数校验", "max_principles": 3},
        )
    )
    assert json.loads(principle[0].text)["count"] >= 1

    async def fake_defense(engine, args):
        from mcp.types import TextContent

        return [TextContent(type="text", text=json.dumps({"trust": 0.6, "tier": "standard"}))]

    import plastic_promise.mcp.tools.audit_defense as audit_defense

    monkeypatch.setattr(audit_defense, "handle_defense", fake_defense)
    defense = asyncio.run(mcp_server.call_tool("defense", {"action": "get"}))
    assert json.loads(defense[0].text)["tier"] == "standard"


def test_session_init_alias_routes_to_canonical_skill(monkeypatch):
    calls = []

    class FakeSkillEngine:
        async def exec(self, skill_name, params, caller="claude"):
            calls.append((skill_name, params, caller))
            return SimpleNamespace(
                skill_name=skill_name,
                success=True,
                data={"ok": True},
                degrade_log=[],
                errors=[],
                audit_trail={},
            )

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: FakeSkillEngine())

    result = asyncio.run(
        mcp_server.call_tool("session_init", {"task_description": "alias route"})
    )
    data = json.loads(result[0].text)
    assert data["success"] is True
    assert calls == [("session-init", {"task_description": "alias route"}, "claude")]


def test_sse_app_constructs_with_installed_starlette(monkeypatch):
    route_paths = []

    async def fake_serve(self):
        route_paths.extend(route.path for route in self.config.app.routes)
        return None

    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    asyncio.run(mcp_server.run_sse(0))
    assert "/mcp" in route_paths
