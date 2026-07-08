from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

GateDecision = Literal["core", "related", "divergent", "raw_only", "block"]


@dataclass(frozen=True)
class CandidateEvidence:
    id: str
    content: str
    source: str
    retrieval_source: str
    base_score: float
    kind: str = "memory"
    project_id: str = "project:legacy-global"
    visibility: str = "project"
    source_class: str = "experience"
    worth_score: float = 0.5
    freshness_score: float = 1.0
    conflict_score: float = 0.0
    canonical_key: str | None = None
    status: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    gate_score: float
    decision: GateDecision
    reasons: tuple[str, ...]
    features: dict[str, float]


def evaluate_context_gate(
    candidate: CandidateEvidence,
    *,
    task_type: str = "general",
    retrieval_mode: str = "global",
    project_id: str = "project:legacy-global",
    project_policy: str = "balanced",
    project_degraded: bool = False,
) -> GateResult:
    """Score whether evidence should be eligible for prompt layers.

    The gate is intentionally deterministic and explainable. In the first
    rollout it is used for debug metadata only; enforcement is a separate
    ContextEngine decision behind feature flags.
    """

    hard_decision, hard_reason = _hard_decision(
        candidate,
        project_id=project_id,
        project_policy=project_policy,
        project_degraded=project_degraded,
    )
    features = {
        "task_match": _task_match(candidate, task_type, retrieval_mode),
        "scope_fit": _scope_fit(candidate, project_id, project_policy),
        "worth": _clamp01(candidate.worth_score),
        "freshness": _clamp01(candidate.freshness_score),
        "source_trust": _source_trust(candidate.source_class, candidate.source),
        "conflict": _clamp01(candidate.conflict_score),
    }
    score = (
        0.25 * features["task_match"]
        + 0.20 * features["scope_fit"]
        + 0.20 * features["worth"]
        + 0.15 * features["freshness"]
        + 0.10 * features["source_trust"]
        + 0.10 * (1.0 - features["conflict"])
    )
    score = round(_clamp01(score), 6)

    reasons = [
        f"task_match={features['task_match']:.2f}",
        f"scope_fit={features['scope_fit']:.2f}",
        f"worth={features['worth']:.2f}",
        f"freshness={features['freshness']:.2f}",
        f"source_trust={features['source_trust']:.2f}",
        f"conflict={features['conflict']:.2f}",
    ]
    if hard_decision:
        reasons.append(hard_reason)
        return GateResult(score, hard_decision, tuple(reasons), features)

    core_threshold = float(os.environ.get("PP_CONTEXT_GATE_CORE_THRESHOLD", "0.72"))
    prompt_threshold = float(os.environ.get("PP_CONTEXT_GATE_PROMPT_THRESHOLD", "0.45"))
    if score >= core_threshold:
        decision: GateDecision = "core"
    elif score >= prompt_threshold:
        decision = "related"
    else:
        decision = "raw_only"
    return GateResult(score, decision, tuple(reasons), features)


def _hard_decision(
    candidate: CandidateEvidence,
    *,
    project_id: str,
    project_policy: str,
    project_degraded: bool,
) -> tuple[GateDecision | None, str]:
    status = (candidate.status or "").strip().lower()
    source_class = (candidate.source_class or "").strip().lower()
    if status in {"forgotten", "expired", "obsolete", "corrected", "deprecated", "rejected"}:
        return "block", f"hard_block:status:{status}"
    if candidate.conflict_score >= 1.0:
        return "block", "hard_block:conflict"
    if _is_cross_project(candidate, project_id) and project_policy == "strict":
        return "block", "hard_block:strict_cross_project"
    if project_degraded and (candidate.visibility or "").lower() != "global":
        return "raw_only", "hard_demote:degraded_non_global"
    if source_class in {"prompt", "telemetry"}:
        return "raw_only", f"hard_demote:source_class:{source_class}"
    return None, ""


def _task_match(candidate: CandidateEvidence, task_type: str, retrieval_mode: str) -> float:
    kind = (candidate.kind or "").lower()
    task = (task_type or "general").lower()
    mode = (retrieval_mode or "global").lower()
    if kind in {"mcp_tool", "code_symbol"}:
        return (
            1.0
            if mode in {"code", "mix", "audit"}
            or task
            in {
                "debugging",
                "code_generation",
                "code_review",
                "refactoring",
                "architecture",
            }
            else 0.80
        )
    if kind == "principle":
        return 1.0 if mode == "principle" or task in {"governing", "architecture"} else 0.85
    if kind == "task_state":
        return 0.90 if task in {"governing", "building", "debugging"} else 0.75
    if candidate.retrieval_source == "canonical_hot":
        return 0.85
    return 0.70


def _scope_fit(candidate: CandidateEvidence, project_id: str, project_policy: str) -> float:
    visibility = (candidate.visibility or "project").lower()
    if visibility == "global":
        return 1.0
    if not candidate.project_id or candidate.project_id == project_id:
        return 1.0
    if candidate.project_id == "project:legacy-global" or project_id == "project:legacy-global":
        return 0.80
    if project_policy == "strict":
        return 0.0
    return 0.45


def _source_trust(source_class: str, source: str) -> float:
    source_key = (source_class or source or "").lower()
    if source_key in {"system", "manual", "principle"}:
        return 1.0
    if source_key in {"code", "code_memory"}:
        return 0.90
    if source_key in {"experience", "user", "agent"}:
        return 0.75
    if source_key in {"maintenance_daemon", "superpowers", "step-closure", "step_closure"}:
        return 0.45
    if source_key in {"prompt", "telemetry"}:
        return 0.10
    return 0.60


def _is_cross_project(candidate: CandidateEvidence, project_id: str) -> bool:
    visibility = (candidate.visibility or "project").lower()
    if visibility == "global":
        return False
    candidate_project = candidate.project_id or "project:legacy-global"
    return candidate_project not in {project_id, "project:legacy-global"}


def _clamp01(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))
