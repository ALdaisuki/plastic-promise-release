import asyncio
import json

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp import server as mcp_server
from plastic_promise.mcp.tools.audit_defense import handle_defense


def _tool_names() -> list[str]:
    return [tool.name for tool in asyncio.run(mcp_server.list_tools())]


def test_tool_manifest_registry_covers_every_mcp_tool():
    from plastic_promise.core.tool_manifest import build_tool_manifest_registry

    names = _tool_names()
    registry = build_tool_manifest_registry(names)

    assert set(names).issubset(registry)
    assert registry["memory_store"].risk_level == "high"
    assert "memory_write" in registry["memory_store"].side_effects
    assert registry["context_supply"].fallbacks
    assert registry["defense"].trust_requirement >= 0.5


def test_tool_manifest_graph_registers_semantic_nodes_and_edges():
    from plastic_promise.core.behavior_graph import VALID_EDGE_TYPES, VALID_NODE_TYPES
    from plastic_promise.core.tool_manifest import (
        build_tool_manifest_registry,
        register_tool_manifest_graph,
    )

    assert {"tool_capability", "tool_risk", "tool_fallback"}.issubset(VALID_NODE_TYPES)
    assert {"has_capability", "has_risk", "requires_trust", "has_fallback"}.issubset(
        VALID_EDGE_TYPES
    )

    engine = ContextEngine(use_sqlite=False)
    registry = build_tool_manifest_registry(["memory_store", "context_supply"])
    result = register_tool_manifest_graph(engine, registry.values())
    graph = engine.query_graph("full_graph")

    assert result["tools_registered"] == 2
    assert "mcp_tool:memory_store" in graph["nodes"]
    assert graph["nodes"]["mcp_tool:memory_store"]["metadata"]["risk_level"] == "high"
    assert any(edge["relation"] == "has_capability" for edge in graph["edges"])
    assert any(edge["relation"] == "has_fallback" for edge in graph["edges"])


def test_defense_evaluate_tool_returns_allow_ask_deny():
    engine = ContextEngine(use_sqlite=False)

    allow = asyncio.run(
        handle_defense(
            engine,
            {"action": "evaluate_tool", "tool_name": "context_supply", "trust_score": 0.58},
        )
    )
    ask = asyncio.run(
        handle_defense(
            engine,
            {"action": "evaluate_tool", "tool_name": "memory_forget", "trust_score": 0.58},
        )
    )
    deny = asyncio.run(
        handle_defense(
            engine,
            {"action": "evaluate_tool", "tool_name": "memory_forget", "trust_score": 0.05},
        )
    )

    allow_payload = json.loads(allow[0].text)
    ask_payload = json.loads(ask[0].text)
    deny_payload = json.loads(deny[0].text)

    assert allow_payload["decision"] == "allow"
    assert ask_payload["decision"] == "ask"
    assert deny_payload["decision"] == "deny"
    assert allow_payload["manifest"]["name"] == "context_supply"
    assert ask_payload["required_trust"] > ask_payload["trust_score"]
    assert "reasons" in deny_payload


def test_audit_rollover_is_narrowly_allowed_at_fresh_install_trust():
    from plastic_promise.core.constants import TRUST_INITIAL
    from plastic_promise.core.tool_manifest import (
        evaluate_tool_decision,
        manifest_for_tool,
    )

    rollover = manifest_for_tool("audit_rollover")
    forget = manifest_for_tool("memory_forget")

    assert rollover.domain == "audit"
    assert rollover.risk_level == "high"
    assert rollover.trust_requirement == TRUST_INITIAL
    assert "audit.rollover" in rollover.capabilities
    assert evaluate_tool_decision(rollover, TRUST_INITIAL)["decision"] == "allow"

    assert forget.risk_level == "critical"
    assert forget.trust_requirement > TRUST_INITIAL
    assert evaluate_tool_decision(forget, TRUST_INITIAL)["decision"] == "ask"


def test_public_memory_maintenance_tools_require_mutation_authority():
    from plastic_promise.core.tool_manifest import manifest_for_tool

    reclassify = manifest_for_tool("memory_reclassify")
    sync_files = manifest_for_tool("memory_sync_files")

    assert reclassify.trust_requirement == 0.60
    assert "memory_mutation" in reclassify.side_effects
    assert sync_files.trust_requirement == 0.60
    assert {"memory_mutation", "file_write"}.issubset(sync_files.side_effects)
    assert sync_files.evidence_required == ("source_dir", "project_id")


def test_defense_schema_exposes_evaluate_tool_action():
    tool = next(tool for tool in asyncio.run(mcp_server.list_tools()) if tool.name == "defense")
    props = tool.inputSchema["properties"]

    assert "evaluate_tool" in props["action"]["enum"]
    assert {"tool_name", "trust_score"}.issubset(props)
