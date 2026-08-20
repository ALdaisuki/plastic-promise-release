"""Server-safe reranking seam.

The canonical backend may reorder already-scored candidates or blend scores
returned by a registered compute node, but it must not own a provider adapter.
In developer/compute processes this module delegates to the legacy reranker;
the server artifact excludes that provider implementation and uses the
truthful original-order fallback below.
"""

from __future__ import annotations

import os
from typing import Any

_provider_module: Any | None = None
if os.environ.get("PP_ENDPOINT_ROLE", "").strip() != "pp-server-backend":
    try:
        from plastic_promise.core import reranker as _provider_module
    except (ImportError, ModuleNotFoundError):
        _provider_module = None


if _provider_module is not None:
    MultiProviderReranker = _provider_module.MultiProviderReranker
else:

    class MultiProviderReranker:
        """Provider-neutral original-order reranker used by pp-server-backend."""

        def __init__(self, providers: tuple[str, ...] | None = None) -> None:
            self._providers = providers or ("original",)
            self._last_diagnostics: dict[str, object] = {
                "provider": "original",
                "status": "not_run",
                "degraded": False,
                "reason": "",
                "candidate_count": 0,
                "reranked_count": 0,
            }

        @property
        def last_diagnostics(self) -> dict[str, object]:
            return dict(self._last_diagnostics)

        @staticmethod
        def _candidate_relevance(candidate: object) -> float:
            if isinstance(candidate, tuple):
                return float(candidate[2]) if len(candidate) > 2 else 0.0
            return float(getattr(candidate, "relevance", 0.0))

        @staticmethod
        def _set_relevance(candidates: list, index: int, value: float) -> None:
            candidate = candidates[index]
            if isinstance(candidate, tuple):
                candidates[index] = (candidate[0], candidate[1], value)
            else:
                candidate.relevance = value

        def _apply_rerank_scores(
            self,
            candidates: list,
            provider_scores: dict[int, float],
            top_k: int | None = None,
        ) -> list:
            for index, item in enumerate(candidates):
                if index not in provider_scores:
                    continue
                original = self._candidate_relevance(item)
                score = float(provider_scores[index])
                self._set_relevance(
                    candidates,
                    index,
                    min(1.0, max(original * 0.5, 0.6 * score + 0.4 * original)),
                )
            candidates.sort(key=self._candidate_relevance, reverse=True)
            return candidates[:top_k] if top_k else candidates

        def rerank(self, query: str, candidates: list, top_k: int | None = None) -> list:
            del query
            self._last_diagnostics = {
                "provider": "original",
                "status": "skipped",
                "degraded": False,
                "reason": "original_configured",
                "candidate_count": len(candidates),
                "reranked_count": 0,
            }
            return candidates[:top_k] if top_k else candidates
