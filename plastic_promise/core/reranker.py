"""Cross-encoder reranker — LLM-based relevance scoring for retrieval results.

Uses local Ollama LLM to pairwise compare query against candidates.
Blends: final_score = 0.6 * ce_score + 0.4 * original_score.
Graceful fallback: returns original order on any failure.
"""

import json
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

    passages = "\n\n".join(
        f"[{i}] {c[:300]}"
        for i, (_, c, _) in enumerate(candidates[:top_k * 2])
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
    return reranked[:top_k]
