from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plastic_promise.core.fusion_policy import FUSION_CHANNEL_ORDER

VALID_RETRIEVAL_MODES = {
    "local",
    "global",
    "hybrid",
    "mix",
    "project",
    "code",
    "audit",
    "principle",
}

HIGH_IMPACT_SYNTHESIS_RETRIEVAL = frozenset(
    {
        "code",
        "audit",
        "principle",
        "governing",
        "correction",
        "code_review",
        "debugging",
    }
)


MODE_BUDGETS: dict[str, dict[str, int]] = {
    "local": {"core": 5, "related": 8, "divergent": 3, "raw_evidence": 6},
    "global": {"core": 6, "related": 10, "divergent": 6, "raw_evidence": 8},
    "hybrid": {"core": 7, "related": 12, "divergent": 6, "raw_evidence": 10},
    "mix": {"core": 8, "related": 14, "divergent": 8, "raw_evidence": 12},
    "project": {"core": 6, "related": 10, "divergent": 2, "raw_evidence": 8},
    "code": {"core": 8, "related": 12, "divergent": 4, "raw_evidence": 12},
    "audit": {"core": 10, "related": 10, "divergent": 2, "raw_evidence": 14},
    "principle": {"core": 6, "related": 8, "divergent": 4, "raw_evidence": 8},
}


@dataclass(frozen=True)
class RetrievalPlan:
    mode: str
    budget: dict[str, int]
    channels: list[str] = field(default_factory=list)
    fusion_channels: tuple[str, ...] = field(default_factory=tuple)
    channel_windows: dict[str, int] = field(default_factory=dict)
    task_type: str = "general"
    scope: str = "global"
    project_policy: str = "balanced"
    reason: str = "task_type"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "budget": dict(self.budget),
            "channels": list(self.channels),
            "fusion_channels": list(self.fusion_channels),
            "channel_windows": dict(self.channel_windows),
            "task_type": self.task_type,
            "scope": self.scope,
            "project_policy": self.project_policy,
            "reason": self.reason,
        }


def requires_synthesis_source_expansion(
    plan: RetrievalPlan,
    *,
    task_type: str | None = None,
) -> bool:
    """Return whether selected synthesis must carry current raw source evidence."""
    normalized_mode = str(plan.mode or "").strip().casefold()
    normalized_task = str(task_type or plan.task_type or "").strip().casefold()
    return bool({normalized_mode, normalized_task} & HIGH_IMPACT_SYNTHESIS_RETRIEVAL)


def plan_retrieval(
    *,
    task_type: str = "general",
    scope: str = "global",
    project_policy: str = "balanced",
    retrieval_mode: str | None = None,
    has_vector: bool = True,
    has_graph: bool = True,
    has_fts: bool = True,
) -> RetrievalPlan:
    normalized_task = (task_type or "general").lower()
    normalized_scope = scope or "global"
    normalized_policy = project_policy or "balanced"

    if retrieval_mode:
        mode = retrieval_mode.strip().lower()
        if mode not in VALID_RETRIEVAL_MODES:
            raise ValueError(f"Unknown retrieval_mode {retrieval_mode!r}")
        reason = "caller_override"
    elif normalized_task in {"code_generation", "code_review", "debugging", "refactoring"}:
        mode = "code"
        reason = "task_type"
    elif "audit" in normalized_task or normalized_task in {"review", "code_audit"}:
        mode = "audit"
        reason = "task_type"
    elif "principle" in normalized_task or normalized_task == "governing":
        mode = "principle"
        reason = "task_type"
    elif normalized_task == "architecture":
        mode = "mix"
        reason = "task_type"
    elif normalized_scope != "global":
        mode = "local"
        reason = "scope"
    elif normalized_policy == "strict":
        mode = "project"
        reason = "project_policy"
    elif has_vector and has_graph:
        mode = "hybrid"
        reason = "available_backends"
    else:
        mode = "global"
        reason = "fallback"

    channels = ["bm25"]
    if has_vector:
        channels.append("vector")
    if has_fts:
        channels.append("fts")
    if has_graph:
        channels.append("graph")
    if mode == "principle":
        channels.append("principle")
    if mode == "audit":
        channels.append("audit")
    if mode == "code":
        channels.append("code")

    fusion_channels = tuple(channel for channel in FUSION_CHANNEL_ORDER if channel in channels)
    channel_windows = dict.fromkeys(fusion_channels, 32)

    return RetrievalPlan(
        mode=mode,
        budget=dict(MODE_BUDGETS[mode]),
        channels=channels,
        fusion_channels=fusion_channels,
        channel_windows=channel_windows,
        task_type=normalized_task,
        scope=normalized_scope,
        project_policy=normalized_policy,
        reason=reason,
    )
