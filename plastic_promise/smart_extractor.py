"""Smart memory extraction — rules + LLM hybrid extraction into 6 categories.

Categories: preference, fact, decision, entity, event, pattern.
Three-layer storage: L0 (one-liner), L1 (summary), L2 (full text).
Two-stage dedup: vector similarity pre-filter + category-aware MERGE/SKIP.
"""

import re
import json
import requests
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedMemory:
    """A structured memory extracted from conversation."""
    category: str           # preference|fact|decision|entity|event|pattern
    l0_abstract: str        # one-sentence index (≤80 chars)
    l1_summary: str         # structured summary (≤300 chars)
    l2_content: str         # full original text
    importance: float       # 0.0-1.0
    confidence: float       # 0.0-1.0 extraction confidence
    source_segment: str = ""  # the text segment that triggered extraction


# Category → keyword patterns
CATEGORY_KEYWORDS = {
    "preference": ["喜欢", "不喜欢", "prefer", "讨厌", "习惯", "偏好", "favorite", "倾向于", "prefer"],
    "fact": ["是", "was", "位于", "has", "知道", "了解", "属于", "包含", "版本", "version"],
    "decision": ["决定", "decided", "选择", "chose", "确定", "定下来", "最终", "敲定", "改为"],
    "entity": ["项目", "project", "代码", "repo", "文件", "file", "模块", "module", "仓库", "repository"],
    "event": ["完成了", "finished", "部署了", "deployed", "发布了", "released", "提交了", "committed", "修复了", "fixed"],
    "pattern": ["总是", "always", "通常", "usually", "每次", "每次", "经常", "often", "从不", "never"],
}


def _classify_by_rules(text: str) -> tuple[Optional[str], float]:
    """Classify text into a category using keyword matching.

    Returns:
        (category, confidence) where category is None if no match.
    """
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in text.lower())
        if hits > 0:
            scores[cat] = hits / len(keywords)

    if not scores:
        return (None, 0.0)

    best = max(scores, key=scores.get)
    return (best, scores[best])


def _generate_l0_l1(text: str, category: str) -> tuple[str, str]:
    """Generate L0 (one-liner) and L1 (summary) from raw text.

    Uses simple heuristics — LLM fallback in future version.
    """
    # L0: first sentence, truncated
    first_sentence = re.split(r"[。！？.!?\n]", text)[0].strip()
    l0 = first_sentence[:80]

    # L1: key extraction
    l1 = f"[{category}] {text[:300]}"

    return (l0, l1)


def extract_memories(
    conversation: str,
    ollama_host: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:3b",
    llm_fallback_threshold: float = 0.7,
) -> list[ExtractedMemory]:
    """Extract structured memories from conversation text.

    Pipeline:
    1. Split into sentences
    2. Rule-based classification per sentence
    3. If confidence < threshold, attempt LLM fallback (graceful on failure)
    4. Build ExtractedMemory with L0/L1/L2 layers

    Args:
        conversation: Raw conversation text.
        ollama_host: Ollama API host.
        ollama_model: Ollama model for LLM fallback classification.
        llm_fallback_threshold: Min confidence to skip LLM fallback.

    Returns:
        List of ExtractedMemory objects.
    """
    sentences = re.split(r"[。！？.!?\n]+", conversation)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 10]

    results: list[ExtractedMemory] = []

    for sent in sentences:
        cat, conf = _classify_by_rules(sent)

        # LLM fallback for low-confidence
        if conf < llm_fallback_threshold:
            llm_cat = _llm_classify(sent, ollama_host, ollama_model)
            if llm_cat and llm_cat in CATEGORY_KEYWORDS:
                cat = llm_cat
                conf = max(conf, 0.5)  # LLM overrides with base confidence

        if cat is None:
            continue

        l0, l1 = _generate_l0_l1(sent, cat)

        results.append(ExtractedMemory(
            category=cat,
            l0_abstract=l0,
            l1_summary=l1,
            l2_content=sent,
            importance=0.5 + 0.5 * conf,  # scale confidence to importance
            confidence=conf,
            source_segment=sent,
        ))

    return results


def _llm_classify(
    text: str,
    ollama_host: str,
    ollama_model: str,
    timeout: int = 10,
) -> Optional[str]:
    """Use Ollama LLM to classify text into one of 6 categories.

    Returns None on any failure (network, timeout, bad response).
    """
    prompt = f"""Classify this text into exactly ONE category. Reply with ONLY the category word.

Categories: preference, fact, decision, entity, event, pattern

Text: {text[:500]}

Category:"""

    try:
        resp = requests.post(
            f"{ollama_host}/api/generate",
            json={"model": ollama_model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip().lower()
        # Extract first matching category
        for cat in CATEGORY_KEYWORDS:
            if cat in raw:
                return cat
        return None
    except Exception:
        return None
