"""Explainable context recommendation over already eligible context items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

LAYER_BONUS = {"core": 0.14, "related": 0.06, "divergent": 0.0}


def _float_attr(item: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(item, name, default) or default)
    except Exception:
        return default


def _text(item: Any) -> str:
    return str(getattr(item, "content", "") or "").lower()


def _source(item: Any) -> str:
    return str(getattr(item, "source", "") or "")


def _item_id(item: Any) -> str:
    return str(getattr(item, "id", "") or "")


def recommend_context_items(
    items: Iterable[Any],
    *,
    task_type: str = "",
    hard_excluded_ids: set[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    excluded = set(hard_excluded_ids or set())
    task = str(task_type or "").lower()
    recommendations: list[dict[str, Any]] = []

    for item in items:
        item_id = _item_id(item)
        if item_id in excluded:
            continue

        relevance = _float_attr(item, "relevance")
        worth = _float_attr(item, "worth_score")
        adoption = _float_attr(item, "adoption_count")
        rejection = _float_attr(item, "rejection_count")
        layer = str(getattr(item, "layer", "") or "")
        source = _source(item)
        reasons: list[str] = []

        score = relevance * 0.62 + worth * 0.18 + LAYER_BONUS.get(layer, 0.0)
        if relevance >= 0.75:
            reasons.append("high_relevance")
        elif relevance >= 0.40:
            reasons.append("relevant")

        if worth >= 0.60:
            reasons.append("positive_worth")

        if adoption > rejection:
            score += 0.06
            reasons.append("positive_history")
        elif rejection > adoption:
            score -= 0.06
            reasons.append("negative_history")

        if layer == "core":
            reasons.append("core_context")

        if source.startswith("project:"):
            score += 0.05
            reasons.append("project_scope_match")
        elif source == "global":
            reasons.append("global_context")

        if task and task in _text(item):
            score += 0.05
            reasons.append("task_type_match")

        if not reasons:
            reasons.append("baseline_candidate")

        recommendations.append(
            {
                "id": item_id,
                "score": round(max(0.0, min(score, 1.0)), 4),
                "reasons": reasons,
                "layer": layer,
                "source": source,
            }
        )

    recommendations.sort(key=lambda rec: rec["score"], reverse=True)
    return recommendations[:limit]


def attach_context_recommendations(
    pack: Any,
    *,
    task_type: str = "",
    hard_excluded_ids: set[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    items = list(getattr(pack, "core", []) or [])
    items.extend(getattr(pack, "related", []) or [])
    items.extend(getattr(pack, "divergent", []) or [])
    recommendations = recommend_context_items(
        items,
        task_type=task_type,
        hard_excluded_ids=hard_excluded_ids,
        limit=limit,
    )
    pack.audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
    pack.audit_metadata["context_recommender"] = {
        "task_type": task_type,
        "recommendations": recommendations,
        "hard_constraints": "preserved_before_ranking",
    }
    return recommendations
