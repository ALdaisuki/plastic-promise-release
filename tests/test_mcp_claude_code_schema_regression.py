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
            "project_id": "project:test-app",
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
            "retrieval_mode": "mix",
            "debug": True,
            "stage_session_id": "stage:codex:schema",
            "flow_line_id": "bug-hunt",
            "request_id": "req:schema-context",
        },
        "defense": {"action": "get"},
        "runtime_mode": {"action": "set", "mode": "rust-full"},
        "system": {
            "action": "benchmark",
            "run": False,
            "limit": 5,
            "gate": True,
            "baseline_name": "release",
            "max_p95_ms": 1000.0,
        },
        "audit_pre_check": {
            "action_description": "写入 MCP schema validation hardening spec/plan",
            "action_type": "write",
        },
        "session-init": {
            "task_description": "会话启动",
            "task_type": "debugging",
        },
        "commercial_audit_export": {
            "project_id": "project:test-app",
            "include_outbox": True,
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
        "project_id",
        "project_policy",
    }.issubset(memory_props)

    context_props = set(tools["context_supply"].inputSchema["properties"])
    assert {
        "stage_session_id",
        "flow_line_id",
        "request_id",
        "project_id",
        "project_policy",
        "retrieval_mode",
        "debug",
    }.issubset(context_props)

    memory_store_props = set(tools["memory_store"].inputSchema["properties"])
    assert {
        "project_id",
        "project_policy",
        "visibility",
        "source_class",
        "origin_kind",
        "origin_uri",
        "parent_memory_ids",
    }.issubset(memory_store_props)

    review_props = set(tools["review_run"].inputSchema["properties"])
    assert {"project_id", "project_policy"}.issubset(review_props)

    commercial_audit_props = set(tools["commercial_audit_export"].inputSchema["properties"])
    assert {"project_id", "since", "until", "include_outbox", "export_otlp", "otlp_endpoint"}.issubset(
        commercial_audit_props
    )

    principle_props = set(tools["principle_activate"].inputSchema["properties"])
    assert "project_id" in principle_props

    system_props = set(tools["system"].inputSchema["properties"])
    assert {
        "action",
        "run",
        "queries",
        "repeat",
        "benchmark_name",
        "limit",
        "baseline_name",
        "set_baseline",
        "gate",
        "tolerance_ratio",
        "max_p50_ms",
        "max_p95_ms",
        "max_p99_ms",
    }.issubset(system_props)
    assert "benchmark" in tools["system"].inputSchema["properties"]["action"]["enum"]

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


def test_context_supply_debug_returns_structured_metadata(monkeypatch):
    from plastic_promise.core.context_engine import ContextItem, ContextPack
    import plastic_promise.core.embedder as embedder_mod
    from plastic_promise.mcp.tools.context import handle_context_supply

    class FakeEmbedder:
        async def aembed(self, _text):
            return [0.0] * 1024

    class FakeEngine:
        def __init__(self):
            self.kwargs = {}

        def supply(self, *_args, **kwargs):
            self.kwargs = kwargs
            return ContextPack(
                core=[
                    ContextItem(
                        id="m1",
                        content="debug context item",
                        relevance=0.9,
                        source="test",
                        layer="core",
                    )
                ],
                audit_metadata={"canonical_hot": {"enabled": True}},
                pipeline_stats={"canonical_hot_count": 1},
                per_item_stats=[{"id": "m1", "gate_decision": "core"}],
            )

    monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())
    engine = FakeEngine()

    result = asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "debug context supply",
                "task_type": "debugging",
                "retrieval_mode": "mix",
                "debug": True,
            },
        )
    )

    data = json.loads(result[0].text)
    assert engine.kwargs["debug"] is True
    assert engine.kwargs["retrieval_mode"] == "mix"
    assert data["pipeline_stats"]["canonical_hot_count"] == 1
    assert data["per_item_stats"][0]["gate_decision"] == "core"
    assert data["audit_metadata"]["canonical_hot"]["enabled"] is True
    assert data["prompt"]


def test_context_supply_debug_tolerates_pack_without_audit_metadata(monkeypatch):
    from plastic_promise.core.context_engine import ContextItem
    import plastic_promise.core.embedder as embedder_mod
    from plastic_promise.mcp.tools.context import handle_context_supply

    class FakeEmbedder:
        async def aembed(self, _text):
            return [0.0] * 1024

    class MinimalPack:
        core = [
            ContextItem(
                id="m1",
                content="minimal debug context item",
                relevance=0.8,
                source="test",
                layer="core",
            )
        ]
        related = []
        divergent = []
        activated_principles = []
        pipeline_stats = {}
        per_item_stats = []

        def to_prompt(self):
            return "minimal prompt"

    class FakeEngine:
        def supply(self, *_args, **_kwargs):
            return MinimalPack()

    monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

    result = asyncio.run(
        handle_context_supply(
            FakeEngine(),
            {
                "task_description": "debug context supply",
                "task_type": "debugging",
                "debug": True,
            },
        )
    )

    data = json.loads(result[0].text)
    assert data["audit_metadata"]["trace"]["call_id"]
    assert data["trace"]["request_scope_id"]
    assert data["prompt"] == "minimal prompt"


def test_public_tool_descriptions_do_not_expose_mojibake_markers():
    markers = ["\u951b", "\u9225", "\u00c3", "\u00e6", "\ufffd"]

    offenders = []
    for tool in _tools_by_name().values():
        haystack = " ".join(
            [
                tool.description or "",
                json.dumps(tool.inputSchema, ensure_ascii=False),
            ]
        )
        if any(marker in haystack for marker in markers):
            offenders.append(tool.name)

    assert offenders == []


def test_server_initialization_instructions_expose_codex_bootstrap_contract():
    instructions = mcp_server.server.create_initialization_options().instructions or ""

    assert "Plastic Promise MCP" in instructions
    assert "session-init" in instructions
    assert "sp-stage" in instructions
    assert "context_supply" in instructions
    assert "debug=true only for diagnostics" in instructions
    assert len(instructions[:512]) == len(instructions)


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
        "commercial_audit_export": [
            "Plastic Promise MCP",
            "Codex",
            "tool_search",
            "commercial audit export",
        ],
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


def test_streamable_http_app_constructs_with_installed_starlette(monkeypatch):
    route_paths = []

    async def fake_serve(self):
        route_paths.extend(route.path for route in self.config.app.routes)
        return None

    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    asyncio.run(mcp_server.run_streamable_http(0))
    assert "/mcp" in route_paths


def test_legacy_run_sse_alias_constructs_same_app(monkeypatch):
    route_paths = []

    async def fake_serve(self):
        route_paths.extend(route.path for route in self.config.app.routes)
        return None

    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    asyncio.run(mcp_server.run_sse(0))
    assert "/mcp" in route_paths
    assert "/sse" in route_paths


def test_windows_client_disconnect_filter_suppresses_proactor_noise(monkeypatch):
    class FakeHandle:
        def __repr__(self):
            return "<Handle _ProactorBasePipeTransport._call_connection_lost()>"

    async def runner():
        delegated = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: delegated.append(context))
        mcp_server._install_windows_client_disconnect_filter(
            mcp_server.logging.getLogger("test")
        )
        handler = loop.get_exception_handler()

        handler(
            loop,
            {
                "exception": ConnectionResetError(10054, "client closed"),
                "handle": FakeHandle(),
            },
        )
        handler(loop, {"exception": RuntimeError("real failure"), "message": "boom"})

        assert len(delegated) == 1
        assert isinstance(delegated[0]["exception"], RuntimeError)

    monkeypatch.setattr(mcp_server.sys, "platform", "win32")
    asyncio.run(runner())


def test_windows_client_disconnect_filter_ignores_non_disconnect(monkeypatch):
    monkeypatch.setattr(mcp_server.sys, "platform", "win32")

    assert not mcp_server._is_windows_client_disconnect(
        {"exception": RuntimeError("boom"), "message": "_call_connection_lost"}
    )
