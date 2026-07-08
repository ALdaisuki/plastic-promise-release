from __future__ import annotations

from hashlib import sha1
from typing import Any

SCHEMA_VERSION = "behavior-graph/v1"

VALID_NODE_TYPES = {
    "memory",
    "principle",
    "tool",
    "task",
    "audit_span",
    "code_symbol",
    "file",
    "class",
    "function",
    "method",
    "test",
    "doc",
    "mcp_tool",
    "evidence",
    "document_chunk",
    "skill_session",
    "code_module",
}

VALID_EDGE_TYPES = {
    "references",
    "supports",
    "governs",
    "embodies",
    "activates",
    "contains",
    "imports",
    "calls",
    "inherits",
    "tests",
    "documents",
    "exposes_tool",
    "uses_tool",
    "produced_evidence",
    "cites",
    "parent_of",
    "blocks",
    "blocked_by",
    "related_to",
}


def validate_node_type(node_type: str) -> str:
    if node_type not in VALID_NODE_TYPES:
        raise ValueError(
            f"Unknown behavior graph node type {node_type!r}. "
            f"Valid: {', '.join(sorted(VALID_NODE_TYPES))}"
        )
    return node_type


def validate_edge_type(relation: str) -> str:
    if relation not in VALID_EDGE_TYPES:
        raise ValueError(
            f"Unknown behavior graph edge relation {relation!r}. "
            f"Valid: {', '.join(sorted(VALID_EDGE_TYPES))}"
        )
    return relation


def graph_node(
    node_id: str,
    node_type: str,
    name: str,
    description: str = "",
    *,
    source_kind: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_node_type(node_type)
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "description": description or "",
        "schema_version": SCHEMA_VERSION,
        "source_kind": source_kind,
        "metadata": dict(metadata or {}),
    }


def graph_edge(
    source: str,
    target: str,
    relation: str,
    weight: float = 0.5,
    *,
    source_kind: str = "",
    evidence_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_edge_type(relation)
    edge_key = f"{source}|{relation}|{target}"
    return {
        "id": f"edge:{sha1(edge_key.encode('utf-8')).hexdigest()[:16]}",
        "from": source,
        "to": target,
        "relation": relation,
        "weight": float(weight),
        "schema_version": SCHEMA_VERSION,
        "source_kind": source_kind,
        "evidence_id": evidence_id,
        "metadata": dict(metadata or {}),
    }
