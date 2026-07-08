import pytest

from plastic_promise.core.behavior_graph import (
    VALID_EDGE_TYPES,
    VALID_NODE_TYPES,
    graph_edge,
    graph_node,
)
from plastic_promise.core.context_engine import ContextEngine


def test_behavior_graph_schema_accepts_p0_node_and_edge_types():
    assert {
        "memory",
        "principle",
        "tool",
        "task",
        "audit_span",
        "code_symbol",
        "evidence",
        "mcp_tool",
    }.issubset(VALID_NODE_TYPES)
    assert {"calls", "imports", "tests", "documents", "exposes_tool"}.issubset(
        VALID_EDGE_TYPES
    )

    node = graph_node(
        node_id="code_symbol:plastic_promise.core.context_engine.ContextEngine.supply",
        node_type="code_symbol",
        name="ContextEngine.supply",
        description="Context supply entrypoint",
        metadata={"symbol_kind": "function"},
    )
    edge = graph_edge(
        source=node["id"],
        target="mcp_tool:context_supply",
        relation="exposes_tool",
        weight=0.9,
        metadata={"read_only": True},
    )

    assert node["schema_version"] == "behavior-graph/v1"
    assert node["type"] == "code_symbol"
    assert node["metadata"]["symbol_kind"] == "function"
    assert edge["relation"] == "exposes_tool"
    assert edge["metadata"]["read_only"] is True


def test_behavior_graph_schema_rejects_unknown_types():
    with pytest.raises(ValueError, match="Unknown behavior graph node type"):
        graph_node("unknown:x", "unknown", "x")

    with pytest.raises(ValueError, match="Unknown behavior graph edge relation"):
        graph_edge("a", "b", "unknown_relation")


def test_context_engine_registers_typed_graph_records():
    engine = ContextEngine(use_sqlite=False)

    result = engine.register_entity(
        entity_type="code_symbol",
        entity_id="plastic_promise.core.context_engine.ContextEngine.supply",
        entity_name="ContextEngine.supply",
        entity_description="Context supply entrypoint",
        related_entities=["mcp_tool:context_supply"],
        metadata={"symbol_kind": "function"},
    )
    added = engine.add_graph_edge(
        "code_symbol:plastic_promise.core.context_engine.ContextEngine.supply",
        "mcp_tool:context_supply",
        relation="exposes_tool",
        weight=0.9,
        metadata={"read_only": True},
    )

    graph = engine.query_graph("full_graph")
    node = graph["nodes"][result["node_id"]]

    assert added is True
    assert node["type"] == "code_symbol"
    assert node["metadata"]["symbol_kind"] == "function"
    assert any(edge["relation"] == "exposes_tool" for edge in graph["edges"])
