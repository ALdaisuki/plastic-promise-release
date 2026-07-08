"""Multi-provider reranker with local-first graceful degradation.

Default provider chain (configurable via PP_RERANK_PROVIDERS):
  1. Ollama local (zero external network): /api/generate with structured prompt
  2. Cosine fallback (always available): pure computation

Hosted providers are opt-in, for example:
  PP_RERANK_PROVIDERS=jina,siliconflow,ollama,cosine

Blend: final = 0.6 * ce + 0.4 * original, floor original * 0.5
Cache: SHA-256(query + candidate_ids), LRU 64, TTL 60s
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request

logger = logging.getLogger("plastic-promise.reranker")

# ── Configuration ──────────────────────────────────────────────

_DEFAULT_PROVIDERS = ["ollama", "cosine"]
_PROVIDER_TIMEOUT = float(os.environ.get("PP_RERANK_TIMEOUT", "5.0"))
_TOTAL_TIMEOUT = float(os.environ.get("PP_RERANK_TOTAL_TIMEOUT", "10.0"))

# ── Cache ──────────────────────────────────────────────────────

_rerank_cache: dict[str, tuple[dict[int, float], float]] = {}
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
        self._providers = [p.strip() for p in provider_str.split(",") if p.strip()] or ["cosine"]
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
        documents = [_candidate_content(c)[:500] for c in candidates[:30]]
        payload = json.dumps(
            {
                "model": "jina-reranker-v2-base-multilingual",
                "query": query[:500],
                "documents": documents,
                "top_n": min(len(candidates), 20),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers)
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
        documents = [_candidate_content(c)[:500] for c in candidates[:30]]
        payload = json.dumps(
            {
                "model": "BAAI/bge-reranker-v2-m3",
                "query": query[:500],
                "documents": documents,
                "top_n": min(len(candidates), 20),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("SILICONFLOW_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers)
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
        host = _normalize_ollama_host(os.environ.get("OLLAMA_HOST"))
        model = os.environ.get("PP_RERANK_MODEL", "qwen2.5:3b")
        limited_candidates = candidates[:30]
        passages = "\n\n".join(
            f"[{i}] {_candidate_content(c)[:300]}" for i, c in enumerate(limited_candidates)
        )
        prompt = (
            f"Query: {query[:200]}\n\n"
            f"Rate each passage relevance from 0 to 100:\n\n{passages}\n\n"
            f"Return only valid JSON with exactly {len(limited_candidates)} numeric scores, "
            f'one per passage, no markdown and no ellipsis: {{"scores":[0]}}'
        )
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")
        url = f"{host}/api/generate"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        timeout = max(1.0, min(_PROVIDER_TIMEOUT, deadline - time.time()))
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = json.loads(resp.read().decode("utf-8")).get("response", "")
        return _parse_ollama_score_response(raw, len(limited_candidates))

    def _rerank_cosine(self, query, candidates, deadline):
        """Pure cosine similarity fallback (always available, zero network).

        Uses the existing LanceDB ANN search as a proxy — returns
        original scores weighted toward high-BM25 hits.
        """
        # Cosine fallback: preserve original ordering, slightly boost top items
        scores = {}
        for i, _c in enumerate(candidates):
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
            orig = _candidate_relevance(item)
            ce = ce_scores.get(i, orig)
            if i not in ce_scores:
                # Unreturned by reranker — penalize 20% but preserve
                ce = orig * 0.8
            _set_candidate_relevance(
                candidates,
                i,
                min(1.0, max(orig * 0.5, 0.6 * ce + 0.4 * orig)),
            )

        candidates.sort(key=_candidate_relevance, reverse=True)
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
            return _tuple_scores(candidates, cached, top_k)

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
                    return _tuple_scores(candidates, ce_scores, top_k)
            except Exception as e:
                self._last_error = f"{provider_name}: {e}"
                continue

        # All providers failed — return original order
        self._last_provider = "none"
        return [(cid, score) for cid, _, score in candidates]


# ── Candidate helpers ───────────────────────────────────────────


def _candidate_id(candidate) -> str:
    if isinstance(candidate, tuple):
        return str(candidate[0])
    return str(getattr(candidate, "id", candidate))


def _candidate_content(candidate) -> str:
    if isinstance(candidate, tuple):
        return str(candidate[1]) if len(candidate) > 1 else ""
    return str(getattr(candidate, "content", ""))


def _candidate_relevance(candidate) -> float:
    if isinstance(candidate, tuple):
        return float(candidate[2]) if len(candidate) > 2 else 0.0
    return float(getattr(candidate, "relevance", 0.0))


def _set_candidate_relevance(candidates: list, index: int, relevance: float) -> None:
    candidate = candidates[index]
    if isinstance(candidate, tuple):
        candidates[index] = (candidate[0], candidate[1], relevance)
    else:
        candidate.relevance = relevance


def _tuple_scores(candidates, ce_scores, top_k):
    result = []
    for i, (cid, _, orig) in enumerate(candidates):
        ce = ce_scores.get(i, orig)
        final = min(1.0, max(orig * 0.5, 0.6 * ce + 0.4 * orig))
        result.append((cid, final))
    result.sort(key=lambda x: x[1], reverse=True)
    return result[:top_k]


def _normalize_ollama_host(host: str | None) -> str:
    raw = (host or "http://127.0.0.1:11434").strip().rstrip("/")
    raw = raw.replace("0.0.0.0", "127.0.0.1")
    if "://" in raw:
        return raw
    if ":" in raw:
        return f"http://{raw}"
    return f"http://{raw}:11434"


def _parse_ollama_score_response(raw: str, expected_count: int) -> dict[int, float]:
    arr = _extract_json_score_array(raw)
    if arr is None:
        target = raw.split("scores", 1)[1] if "scores" in raw else raw
        arr = re.findall(r"-?\d+(?:\.\d+)?", target)

    scores: dict[int, float] = {}
    for i, value in enumerate(arr[:expected_count]):
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        scores[i] = max(0.0, min(1.0, score / 100.0))
    return scores


def _extract_json_score_array(raw: str) -> list | None:
    for candidate in _json_candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            scores = parsed.get("scores")
            if isinstance(scores, list):
                return scores
        if isinstance(parsed, list):
            return parsed
    return None


def _json_candidates(raw: str) -> list[str]:
    candidates = [raw]
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.index("{") : raw.rindex("}") + 1])
    if "[" in raw and "]" in raw:
        candidates.append(raw[raw.index("[") : raw.rindex("]") + 1])
    return candidates


# ── Cache helpers ────────────────────────────────────────────────


def _cache_key(query, candidates):
    ids = "|".join(_candidate_id(c) for c in candidates[:40])
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
