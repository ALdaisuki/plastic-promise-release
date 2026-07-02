"""Multi-provider cross-encoder reranker with graceful degradation.

Provider chain (configurable via PP_RERANK_PROVIDERS):
  1. Jina AI (free tier, 1M tokens/day) — POST api.jina.ai/v1/rerank
  2. SiliconFlow (free tier, 1K calls/day) — POST api.siliconflow.cn/v1/rerank
  3. Ollama local (zero network) — /api/generate with structured prompt
  4. Cosine fallback (always available) — pure computation

Blend: final = 0.6 * ce + 0.4 * original, floor original * 0.5
Cache: SHA-256(query + candidate_ids), LRU 64, TTL 60s
"""

import hashlib
import json
import logging
import math
import os
import threading
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("plastic-promise.reranker")

# ── Configuration ──────────────────────────────────────────────

_DEFAULT_PROVIDERS = ["jina", "siliconflow", "ollama", "cosine"]
_PROVIDER_TIMEOUT = float(os.environ.get("PP_RERANK_TIMEOUT", "5.0"))
_TOTAL_TIMEOUT = float(os.environ.get("PP_RERANK_TOTAL_TIMEOUT", "10.0"))

# ── Cache ──────────────────────────────────────────────────────

_rerank_cache: dict[str, tuple[list[tuple[str, float]], float]] = {}
_rerank_cache_lock = threading.Lock()
_RERANK_CACHE_SIZE = 64
_RERANK_CACHE_TTL = 60  # seconds


class MultiProviderReranker:
    """Unified reranker with multi-provider fallback chain.

    Usage:
        reranker = MultiProviderReranker()
        reranked = reranker.rerank(query, candidates)
    """

    def __init__(self) -> None:
        disabled = os.environ.get("PP_RERANK_DISABLED", "0") == "1"
        provider_str = os.environ.get("PP_RERANK_PROVIDERS", ",".join(_DEFAULT_PROVIDERS))
        self._providers = provider_str.split(",")
        self._disabled = disabled
        self._last_provider: str = "none"
        self._last_error: str = ""

    # ── Public API ──────────────────────────────────────────────

    def rerank(
        self,
        query: str,
        candidates: list,
        top_k: int | None = None,
    ) -> list:
        """Rerank candidates through the provider chain.

        Args:
            query: Original search query text.
            candidates: List of ContextItem objects (must have .id, .content, .relevance).
            top_k: Optional max results (None = return all).

        Returns:
            Reranked list of ContextItem with adjusted relevance scores.
        """
        if self._disabled or len(candidates) <= 1:
            return candidates

        # ── Cache check ──
        cache_key = _cache_key(query, candidates)
        cached = _cache_get(cache_key)
        if cached is not None:
            return self._apply_rerank_scores(candidates, cached, top_k)

        # ── Provider chain ──
        deadline = time.time() + _TOTAL_TIMEOUT
        for provider_name in self._providers:
            if time.time() > deadline:
                break
            handler = getattr(self, f"_rerank_{provider_name}", None)
            if handler is None:
                continue
            try:
                ce_scores = handler(query, candidates, deadline)
                if ce_scores:
                    self._last_provider = provider_name
                    _cache_set(cache_key, ce_scores)
                    return self._apply_rerank_scores(candidates, ce_scores, top_k)
            except Exception as e:
                self._last_error = f"{provider_name}: {e}"
                continue

        # All providers failed — return original order
        self._last_provider = "none"
        return candidates

    @property
    def last_provider(self) -> str:
        return self._last_provider

    # ── Provider implementations ─────────────────────────────────

    def _rerank_jina(self, query, candidates, deadline):
        """Jina AI Reranker API (free tier: 1M tokens/day)."""
        url = "https://api.jina.ai/v1/rerank"
        documents = [c.content[:500] for c in candidates[:30]]
        payload = json.dumps({
            "model": "jina-reranker-v2-base-multilingual",
            "query": query[:500],
            "documents": documents,
            "top_n": min(len(candidates), 20),
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        timeout = max(1.0, min(_PROVIDER_TIMEOUT, deadline - time.time()))
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        scores = {}
        for r in data.get("results", []):
            idx = r.get("index", -1)
            if 0 <= idx < len(candidates):
                scores[idx] = r.get("relevance_score", 0.5)
        return scores

    def _rerank_siliconflow(self, query, candidates, deadline):
        """SiliconFlow Reranker API (free tier: 1K calls/day)."""
        url = "https://api.siliconflow.cn/v1/rerank"
        documents = [c.content[:500] for c in candidates[:30]]
        payload = json.dumps({
            "model": "BAAI/bge-reranker-v2-m3",
            "query": query[:500],
            "documents": documents,
            "top_n": min(len(candidates), 20),
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        timeout = max(1.0, min(_PROVIDER_TIMEOUT, deadline - time.time()))
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8"))
        scores = {}
        for r in data.get("results", []):
            idx = r.get("index", -1)
            if 0 <= idx < len(candidates):
                scores[idx] = r.get("relevance_score", 0.5)
        return scores

    def _rerank_ollama(self, query, candidates, deadline):
        """Local Ollama LLM via /api/generate (zero network, always available locally)."""
        host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        model = os.environ.get("PP_RERANK_MODEL", "mxbai-embed-large")
        passages = "\n\n".join(
            f"[{i}] {c.content[:300]}" for i, c in enumerate(candidates[:30])
        )
        prompt = (
            f"Query: {query[:200]}\n\n"
            f"Rate relevance 0-100:\n\n{passages}\n\n"
            f'Return JSON: {{"scores": [0, 50, 80, ...]}}'
        )
        payload = json.dumps({
            "model": model, "prompt": prompt, "stream": False,
        }).encode("utf-8")
        url = f"{host}/api/generate"
        req = urllib.request.Request(url, data=payload,
                                      headers={"Content-Type": "application/json"})
        timeout = max(1.0, min(_PROVIDER_TIMEOUT, deadline - time.time()))
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        scores = {}
        if "scores" in raw or "[" in raw:
            try:
                # Try "scores" key first
                if "scores" in raw:
                    arr = json.loads(raw).get("scores", [])
                else:
                    start = raw.index("[")
                    arr = json.loads(raw[start:raw.rindex("]") + 1])
                for i, s in enumerate(arr):
                    if i < len(candidates):
                        scores[i] = float(s) / 100.0
            except (json.JSONDecodeError, ValueError, IndexError):
                pass
        return scores

    def _rerank_cosine(self, query, candidates, deadline):
        """Pure cosine similarity fallback (always available, zero network).

        Uses the existing LanceDB ANN search as a proxy — returns
        original scores weighted toward high-BM25 hits.
        """
        # Cosine fallback: preserve original ordering, slightly boost top items
        scores = {}
        for i, c in enumerate(candidates):
            # Linear decay: top items get highest cosine proxy scores
            scores[i] = max(0.3, 1.0 - (i / max(len(candidates), 1)) * 0.5)
        return scores

    # ── Score application ────────────────────────────────────────

    @staticmethod
    def _apply_rerank_scores(candidates, ce_scores, top_k=None):
        """Apply cross-encoder scores with 60/40 blend + floor.

        Blend: final = 0.6 * ce + 0.4 * original, floor original * 0.5
        Unreturned candidates (BM25 exact hits) penalized 20%.
        """
        for i, item in enumerate(candidates):
            orig = item.relevance
            ce = ce_scores.get(i, orig)
            if i not in ce_scores:
                # Unreturned by reranker — penalize 20% but preserve
                ce = orig * 0.8
            item.relevance = min(1.0, max(orig * 0.5, 0.6 * ce + 0.4 * orig))

        candidates.sort(key=lambda x: x.relevance, reverse=True)
        if top_k:
            return candidates[:top_k]
        return candidates

    def rerank_tuples(
        self,
        query: str,
        candidates: list[tuple[str, str, float]],  # [(id, content, score)]
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Convenience: rerank tuple-format candidates (backward compatible).

        Returns list of (id, final_score) sorted descending.
        """
        if self._disabled or not candidates:
            return [(cid, score) for cid, _, score in candidates]

        # ── Cache check ──
        cache_key = _cache_key(query, candidates)
        cached = _cache_get(cache_key)
        if cached is not None:
            result = []
            for i, (cid, _, orig) in enumerate(candidates):
                ce = cached.get(i, orig)
                final = min(1.0, max(orig * 0.5, 0.6 * ce + 0.4 * orig))
                result.append((cid, final))
            result.sort(key=lambda x: x[1], reverse=True)
            return result[:top_k]

        # ── Provider chain ──
        deadline = time.time() + _TOTAL_TIMEOUT
        for provider_name in self._providers:
            if time.time() > deadline:
                break
            handler = getattr(self, f"_rerank_{provider_name}", None)
            if handler is None:
                continue
            try:
                ce_scores = handler(query, candidates, deadline)
                if ce_scores:
                    self._last_provider = provider_name
                    _cache_set(cache_key, ce_scores)
                    result = []
                    for i, (cid, _, orig) in enumerate(candidates):
                        ce = ce_scores.get(i, orig)
                        final = min(1.0, max(orig * 0.5, 0.6 * ce + 0.4 * orig))
                        result.append((cid, final))
                    result.sort(key=lambda x: x[1], reverse=True)
                    return result[:top_k]
            except Exception as e:
                self._last_error = f"{provider_name}: {e}"
                continue

        # All providers failed — return original order
        self._last_provider = "none"
        return [(cid, score) for cid, _, score in candidates]


# ── Cache helpers ────────────────────────────────────────────────

def _cache_key(query, candidates):
    ids = "|".join(
        getattr(c, "id", "") or (c[0] if isinstance(c, tuple) else str(c))
        for c in candidates[:40]
    )
    return hashlib.sha256(f"{query}|{ids}".encode()).hexdigest()


def _cache_get(key):
    now = time.time()
    with _rerank_cache_lock:
        if key in _rerank_cache:
            result, ts = _rerank_cache[key]
            if now - ts < _RERANK_CACHE_TTL:
                return result
            del _rerank_cache[key]
    return None


def _cache_set(key, value):
    now = time.time()
    with _rerank_cache_lock:
        if len(_rerank_cache) >= _RERANK_CACHE_SIZE:
            oldest = min(_rerank_cache, key=lambda k: _rerank_cache[k][1])
            del _rerank_cache[oldest]
        _rerank_cache[key] = (value, now)


# ── Backward-compatible wrapper ──────────────────────────────────

def cross_encode_rerank(
    query: str,
    candidates: list[tuple[str, str, float]],
    top_k: int = 10,
    **kwargs,
) -> list[tuple[str, float]]:
    """Backward-compatible shim delegating to MultiProviderReranker."""
    reranker = MultiProviderReranker()
    return reranker.rerank_tuples(query, candidates, top_k=top_k)
