"""Cross-encoder reranker — LLM-based relevance scoring for retrieval results.

Uses local Ollama LLM to pairwise compare query against candidates.
Blends: final_score = 0.6 * ce_score + 0.4 * original_score.
Graceful fallback: returns original order on any failure.

Query result cache: SHA-256(query + candidate_ids) → reranked list, LRU 64 / 60s.
"""

import hashlib
import json
import threading
import time
import requests
from typing import Optional


def cross_encode_rerank(
    query: str,
    candidates: list[tuple[str, str, float]],  # [(id, content, original_score)]
    ollama_host: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    top_k: int = 10,
    timeout: int = 5,
) -> list[tuple[str, float]]:
    """Rerank candidates using LLM-based relevance scoring.

    Args:
        query: The search query.
        candidates: List of (id, content, original_score) tuples.
        ollama_host: Ollama API host.
        ollama_model: Ollama model name.
        top_k: Maximum results to return.
        timeout: Seconds before fallback to original order.

    Returns:
        List of (id, final_score) sorted descending.
    """
    if not candidates:
        return []

    # ── Cache check ──
    cache_key = hashlib.sha256(
        f"{query}|{'|'.join(cid for cid, _, _ in candidates[: top_k * 2])}".encode()
    ).hexdigest()
    now = time.time()
    with _rerank_cache_lock:
        if cache_key in _rerank_cache:
            cached_result, cached_ts = _rerank_cache[cache_key]
            if now - cached_ts < _RERANK_CACHE_TTL:
                return cached_result
            del _rerank_cache[cache_key]

    passages = "\n\n".join(
        f"[{i}] {c[:300]}" for i, (_, c, _) in enumerate(candidates[: top_k * 2])
    )
    prompt = f"""Rate each passage's relevance to the query on a scale of 0-100.
Query: {query[:200]}

{passages}

Reply as JSON: {{"scores": [{{"passage": 0, "score": 50}}, ...]}}"""

    ce_scores: dict[int, float] = {}

    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        if "{" in raw:
            json_start = raw.index("{")
            parsed = json.loads(raw[json_start:])
            for entry in parsed.get("scores", []):
                idx = entry.get("passage", -1)
                score = entry.get("score", 50) / 100.0
                if 0 <= idx < len(candidates):
                    ce_scores[idx] = score
    except Exception:
        pass

    reranked = []
    for i, (cid, _, orig) in enumerate(candidates):
        ce = ce_scores.get(i, orig)
        final = 0.6 * ce + 0.4 * orig
        reranked.append((cid, final))

    reranked.sort(key=lambda x: x[1], reverse=True)
    result = reranked[:top_k]

    # ── Cache store ──
    with _rerank_cache_lock:
        if len(_rerank_cache) >= _RERANK_CACHE_SIZE:
            oldest = min(_rerank_cache, key=lambda k: _rerank_cache[k][1])
            del _rerank_cache[oldest]
        _rerank_cache[cache_key] = (result, now)

    return result


# ── Reranker cache ──
_rerank_cache: dict[str, tuple[list[tuple[str, float]], float]] = {}
_rerank_cache_lock = threading.Lock()
_RERANK_CACHE_SIZE = 64
_RERANK_CACHE_TTL = 60  # seconds (shorter TTL — relevance decays faster than classification)
