"""Deterministic shadow comparison between fusion strategies.

Phase A of the Three-Librarians alignment: quantify how the equal-vote RRF
default reorders results against the legacy cross-currency weighted-max
blend on identical channel rankings. Pure functions only, no environment,
no I/O, so runs are reproducible and CI-safe. Metrics describe ranking
behavior; they never gate releases by themselves.
"""
from __future__ import annotations
import math
from collections.abc import Mapping, Sequence
from typing import Any
from plastic_promise.core.fusion_policy import FusionConfig, weighted_rrf

DEFAULT_TOP_K = 8
_BM25_BYPASS_HIGH = 0.75
_BM25_BYPASS_EXACT = 0.90
_FTS_BYPASS = 0.85
_LEGACY_DEFAULT_VECTOR_WEIGHT = 0.70


def _legacy_weighted_scores(
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    vector_weight: float = _LEGACY_DEFAULT_VECTOR_WEIGHT,
) -> dict[str, float]:
    """Approximate the legacy hybrid blend (score-weighted with bypasses).

    Mirrors the pre-RRF semantics of the Python fallback path so shadow
    runs answer what the old currency would have said on the same inputs."""
    text_weight = 1.0 - vector_weight
    combined: dict[str, float] = {}
    for memory_id, score in rankings.get("vector", ()):
        combined[memory_id] = max(combined.get(memory_id, 0.0), score * vector_weight)
    for channel in ("bm25", "fts"):
        for memory_id, score in rankings.get(channel, ()):
            weight = score * text_weight
            if channel == "bm25":
                if score >= _BM25_BYPASS_EXACT:
                    weight = score
                elif score >= _BM25_BYPASS_HIGH:
                    weight = max(weight, score * 0.90)
            elif score >= _FTS_BYPASS:
                weight = score
            combined[memory_id] = max(combined.get(memory_id, 0.0), weight)
    return combined


def _kendall_tau(left: Sequence[str], right: Sequence[str]) -> float:
    """Kendall tau over the shared-id intersection with deterministic order."""
    right_set = set(right)
    left_set = set(left)
    shared = [item for item in left if item in right_set]
    other = [item for item in right if item in left_set]
    rank_right = {item: index for index, item in enumerate(other)}
    n = len(shared)
    if n < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            delta = rank_right[shared[i]] - rank_right[shared[j]]
            if delta == 0:
                continue
            if delta > 0:
                discordant += 1
            else:
                concordant += 1
    return (concordant - discordant) / (n * (n - 1) / 2)


def compare_fusion_strategies(
    rankings: Mapping[str, Sequence[tuple[str, float]]],
    config: FusionConfig,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Rank the same channels with RRF and the legacy blend; report deltas.

    Bounded JSON-safe metrics: rrf_order, legacy_order, top_k_overlap,
    kendall_tau on the shared pool, and the derived fused ceiling
    sum(weights)/(k+1). Deterministic for identical inputs."""
    fused = weighted_rrf(rankings, config)
    rrf_order = [memory_id for memory_id, _score in fused]
    legacy_scores = _legacy_weighted_scores(rankings)
    legacy_order = [
        memory_id
        for memory_id, _score in sorted(
            legacy_scores.items(), key=lambda row: (-row[1], row[0])
        )
    ]
    rrf_top = set(rrf_order[:top_k])
    legacy_top = set(legacy_order[:top_k])
    overlap = len(rrf_top & legacy_top) / top_k if top_k > 0 else 1.0
    tau = _kendall_tau(rrf_order, legacy_order)
    ceiling = sum(config.weights.values()) / (config.k + 1)
    finite_ceiling = ceiling if math.isfinite(ceiling) else 0.0
    return {
        "rrf_order": rrf_order,
        "legacy_order": legacy_order,
        "top_k_overlap": overlap,
        "kendall_tau": tau,
        "fused_ceiling": finite_ceiling,
        "top_k": top_k,
    }
