"""Operational semantics for MCP tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class ToolManifest:
    name: str
    domain: str = "system"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    risk_level: str = "medium"
    side_effects: tuple[str, ...] = field(default_factory=tuple)
    trust_requirement: float = 0.60
    fallbacks: tuple[str, ...] = field(default_factory=tuple)
    evidence_required: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "capabilities": list(self.capabilities),
            "risk_level": self.risk_level,
            "side_effects": list(self.side_effects),
            "trust_requirement": self.trust_requirement,
            "fallbacks": list(self.fallbacks),
            "evidence_required": list(self.evidence_required),
            "description": self.description,
        }


CURATED_MANIFESTS: dict[str, dict[str, Any]] = {
    "memory_recall": {
        "domain": "memory",
        "capabilities": ("memory.search", "context.retrieve"),
        "risk_level": "low",
        "side_effects": ("read", "access_count"),
        "trust_requirement": 0.0,
        "fallbacks": ("text_retrieval", "empty_context"),
    },
    "context_supply": {
        "domain": "context",
        "capabilities": ("context.supply", "memory.search", "principle.activate"),
        "risk_level": "low",
        "side_effects": ("read", "access_count"),
        "trust_requirement": 0.0,
        "fallbacks": ("text_retrieval", "code_memory_skip", "empty_context"),
    },
    "memory_store": {
        "domain": "memory",
        "capabilities": ("memory.write", "quality_gate.run"),
        "risk_level": "high",
        "side_effects": ("memory_write", "vector_write", "graph_write"),
        "trust_requirement": 0.50,
        "fallbacks": ("store_outbox",),
        "evidence_required": ("content", "source"),
    },
    "memory_update": {
        "domain": "memory",
        "capabilities": ("memory.update",),
        "risk_level": "high",
        "side_effects": ("memory_mutation",),
        "trust_requirement": 0.60,
        "fallbacks": ("no_op_error",),
        "evidence_required": ("memory_id",),
    },
    "memory_forget": {
        "domain": "memory",
        "capabilities": ("memory.forget", "memory.lifecycle"),
        "risk_level": "critical",
        "side_effects": ("memory_mutation", "soft_delete"),
        "trust_requirement": 0.80,
        "fallbacks": ("dry_run", "no_op_error"),
        "evidence_required": ("memory_id", "reason"),
    },
    "memory_correct": {
        "domain": "memory",
        "capabilities": ("memory.correct", "memory.lifecycle"),
        "risk_level": "high",
        "side_effects": ("memory_mutation", "graph_write"),
        "trust_requirement": 0.60,
        "fallbacks": ("no_op_error",),
    },
    "memory_gc": {
        "domain": "memory",
        "capabilities": ("memory.gc",),
        "risk_level": "critical",
        "side_effects": ("memory_mutation", "delete"),
        "trust_requirement": 0.80,
        "fallbacks": ("dry_run",),
    },
    "memory_reclassify": {
        "domain": "memory",
        "capabilities": ("memory.reclassify", "memory.update"),
        "risk_level": "high",
        "side_effects": ("memory_mutation", "vector_write"),
        "trust_requirement": 0.60,
        "fallbacks": ("dry_run", "no_op_error"),
    },
    "memory_sync_files": {
        "domain": "memory",
        "capabilities": ("memory.sync_files", "memory.write"),
        "risk_level": "high",
        "side_effects": ("memory_mutation", "file_write"),
        "trust_requirement": 0.60,
        "fallbacks": ("dry_run", "no_op_error"),
        "evidence_required": ("source_dir", "project_id"),
    },
    "feedback_apply": {
        "domain": "reflection",
        "capabilities": ("memory.feedback", "memory.review"),
        "risk_level": "high",
        "side_effects": ("memory_mutation", "review_state"),
        "trust_requirement": 0.60,
        "fallbacks": ("no_op_error",),
        "evidence_required": ("item_id", "feedback_type"),
    },
    "defense": {
        "domain": "defense",
        "capabilities": ("trust.read", "trust.adjust", "tool.evaluate"),
        "risk_level": "high",
        "side_effects": ("trust_mutation", "read"),
        "trust_requirement": 0.50,
        "fallbacks": ("read_only_status",),
    },
    "audit_pre_check": {
        "domain": "audit",
        "capabilities": ("defense.pre_check",),
        "risk_level": "low",
        "side_effects": ("read",),
        "trust_requirement": 0.0,
        "fallbacks": ("deny_on_error",),
    },
    "audit_rollover": {
        "domain": "audit",
        "capabilities": ("audit.rollover", "memory.lifecycle"),
        "risk_level": "high",
        "side_effects": ("memory_mutation", "soft_delete", "memory_write"),
        "trust_requirement": 0.60,
        "fallbacks": ("no_op_error",),
        "evidence_required": ("content", "project_id"),
    },
    "runtime_mode": {
        "domain": "system",
        "capabilities": ("runtime.read", "runtime.configure"),
        "risk_level": "high",
        "side_effects": ("runtime_mutation", "process_config"),
        "trust_requirement": 0.60,
        "fallbacks": ("status_only",),
    },
    "task_enqueue": {
        "domain": "task",
        "capabilities": ("task.dispatch",),
        "risk_level": "medium",
        "side_effects": ("task_write", "event_emit"),
        "trust_requirement": 0.50,
        "fallbacks": ("local_plan_record",),
    },
    "task_claim": {
        "domain": "task",
        "capabilities": ("task.claim",),
        "risk_level": "medium",
        "side_effects": ("task_state", "event_emit"),
        "trust_requirement": 0.30,
        "fallbacks": ("no_op_error",),
    },
    "task_complete": {
        "domain": "task",
        "capabilities": ("task.complete",),
        "risk_level": "medium",
        "side_effects": ("task_state", "event_emit"),
        "trust_requirement": 0.30,
        "fallbacks": ("no_op_error",),
    },
    "task_verify": {
        "domain": "task",
        "capabilities": ("task.verify", "trust.adjust"),
        "risk_level": "high",
        "side_effects": ("task_state", "trust_mutation", "event_emit"),
        "trust_requirement": 0.60,
        "fallbacks": ("pending_review",),
    },
}


def _infer_domain(name: str) -> str:
    prefix = name.split("_", 1)[0].split("-", 1)[0]
    return {
        "memory": "memory",
        "context": "context",
        "principle": "principle",
        "audit": "audit",
        "defense": "defense",
        "task": "task",
        "skill": "skill",
        "runtime": "system",
        "system": "system",
        "market": "market",
        "pack": "pack",
    }.get(prefix, "system")


def _infer_capabilities(name: str) -> tuple[str, ...]:
    domain = _infer_domain(name)
    return (f"{domain}.{name.replace('-', '_')}",)


def _infer_side_effects(name: str) -> tuple[str, ...]:
    if any(token in name for token in ("store", "update", "correct", "forget", "gc")):
        return ("write",)
    if any(token in name for token in ("enqueue", "claim", "complete", "verify", "transition")):
        return ("task_state",)
    if any(token in name for token in ("install", "upgrade", "remove", "enable", "disable")):
        return ("system_mutation",)
    return ("read",)


def _infer_fallbacks(name: str) -> tuple[str, ...]:
    if name.startswith(("memory_recall", "context_supply")):
        return ("text_retrieval",)
    if "export" in name:
        return ("empty_export",)
    if any(token in name for token in ("store", "update", "forget", "correct")):
        return ("no_op_error",)
    return ()


def _trust_for_risk(risk_level: str) -> float:
    return {"low": 0.0, "medium": 0.50, "high": 0.60, "critical": 0.80}[risk_level]


def _manifest(name: str, **kwargs: Any) -> ToolManifest:
    risk = kwargs.get("risk_level", "medium")
    if risk not in RISK_ORDER:
        raise ValueError(f"Unknown tool risk level: {risk}")
    return ToolManifest(
        name=name,
        domain=kwargs.get("domain", _infer_domain(name)),
        capabilities=tuple(kwargs.get("capabilities", _infer_capabilities(name))),
        risk_level=risk,
        side_effects=tuple(kwargs.get("side_effects", _infer_side_effects(name))),
        trust_requirement=float(kwargs.get("trust_requirement", _trust_for_risk(risk))),
        fallbacks=tuple(kwargs.get("fallbacks", _infer_fallbacks(name))),
        evidence_required=tuple(kwargs.get("evidence_required", ())),
        description=kwargs.get("description", ""),
    )


def manifest_for_tool(name: str, description: str = "") -> ToolManifest:
    overlay = dict(CURATED_MANIFESTS.get(name, {}))
    if description and "description" not in overlay:
        overlay["description"] = description
    if "risk_level" not in overlay:
        side_effects = _infer_side_effects(name)
        if "system_mutation" in side_effects:
            overlay["risk_level"] = "high"
        elif side_effects == ("read",):
            overlay["risk_level"] = "low"
    return _manifest(name, **overlay)


def build_tool_manifest_registry(
    tools: Iterable[str] | Mapping[str, str],
) -> dict[str, ToolManifest]:
    pairs = tools.items() if isinstance(tools, Mapping) else ((str(name), "") for name in tools)
    return {name: manifest_for_tool(name, description) for name, description in pairs}


def evaluate_tool_decision(
    manifest: ToolManifest,
    trust_score: float,
    *,
    trust_tier: str = "",
) -> dict[str, Any]:
    reasons: list[str] = []
    required = manifest.trust_requirement

    if trust_score < 0.15:
        decision = "deny"
        reasons.append("trust_below_hard_minimum")
    elif trust_score + 1e-9 >= required:
        if manifest.risk_level == "critical" and trust_score < 0.90:
            decision = "ask"
            reasons.append("critical_tool_requires_confirmation")
        else:
            decision = "allow"
            reasons.append("trust_satisfies_requirement")
    elif trust_score < max(0.15, required - 0.25):
        decision = "deny"
        reasons.append("trust_far_below_requirement")
    else:
        decision = "ask"
        reasons.append("trust_below_requirement")

    if manifest.side_effects:
        reasons.append("side_effects:" + ",".join(manifest.side_effects))
    if trust_tier:
        reasons.append(f"trust_tier:{trust_tier}")

    return {
        "tool_name": manifest.name,
        "decision": decision,
        "trust_score": round(float(trust_score), 4),
        "trust_tier": trust_tier,
        "required_trust": required,
        "risk_level": manifest.risk_level,
        "reasons": reasons,
        "fallbacks": list(manifest.fallbacks),
        "manifest": manifest.to_dict(),
    }


def register_tool_manifest_graph(engine: Any, manifests: Iterable[ToolManifest]) -> dict[str, int]:
    tools_registered = 0
    nodes_registered = 0
    edges_created = 0

    for manifest in manifests:
        tool_node = f"mcp_tool:{manifest.name}"
        result = engine.register_entity(
            entity_type="mcp_tool",
            entity_id=manifest.name,
            entity_name=manifest.name,
            entity_description=manifest.description,
            metadata=manifest.to_dict(),
            source_kind="tool_manifest",
        )
        tools_registered += 1
        nodes_registered += int(bool(result.get("is_new")))

        for capability in manifest.capabilities:
            cap_node = f"tool_capability:{capability}"
            engine.register_entity(
                entity_type="tool_capability",
                entity_id=capability,
                entity_name=capability,
                metadata={"capability": capability},
                source_kind="tool_manifest",
            )
            if engine.add_graph_edge(
                tool_node,
                cap_node,
                relation="has_capability",
                weight=0.8,
                metadata={"tool": manifest.name},
                source_kind="tool_manifest",
            ):
                edges_created += 1

        risk_node = f"tool_risk:{manifest.risk_level}"
        engine.register_entity(
            entity_type="tool_risk",
            entity_id=manifest.risk_level,
            entity_name=manifest.risk_level,
            metadata={"risk_level": manifest.risk_level},
            source_kind="tool_manifest",
        )
        for relation in ("has_risk", "requires_trust"):
            if engine.add_graph_edge(
                tool_node,
                risk_node,
                relation=relation,
                weight=manifest.trust_requirement,
                metadata={"trust_requirement": manifest.trust_requirement},
                source_kind="tool_manifest",
            ):
                edges_created += 1

        for fallback in manifest.fallbacks:
            fallback_node = f"tool_fallback:{fallback}"
            engine.register_entity(
                entity_type="tool_fallback",
                entity_id=fallback,
                entity_name=fallback,
                metadata={"fallback": fallback},
                source_kind="tool_manifest",
            )
            if engine.add_graph_edge(
                tool_node,
                fallback_node,
                relation="has_fallback",
                weight=0.5,
                metadata={"tool": manifest.name},
                source_kind="tool_manifest",
            ):
                edges_created += 1

    return {
        "tools_registered": tools_registered,
        "nodes_registered": nodes_registered,
        "edges_created": edges_created,
    }
