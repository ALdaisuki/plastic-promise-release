"""Exemplar Gap Detector — knowledge-gap detection middleware.

Detects when context_supply returns empty/low-quality results for
technical queries, signaling that exemplar research is needed.

This module does NOT perform searches or produce side effects.
It only builds a GapSignal that consumers (sp-stage, Claude) may act on.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GapSignal:
    """Signal emitted when context_supply detects a knowledge gap."""
    type: str              # "exemplar_needed"
    problem: str           # Original query text
    suggested_search: list[str]  # 2-3 search keywords
    auto_task: bool        # Whether to auto-create a Hunter Guild task
    severity: str          # "high" | "medium" | "low"


# Technology keywords that indicate a query may benefit from exemplar research.
# These are kept intentionally broad — false positives are cheap (a signal is
# shown but ignored), while false negatives mean missed knowledge gaps.
TECH_KEYWORDS = {
    "storage", "engine", "agent", "memory", "retrieval",
    "api", "schema", "protocol", "distributed", "consensus",
    "replication", "caching", "queue", "stream", "index",
    "embedding", "vector", "pipeline", "router", "gateway",
    "proxy", "cache", "lock", "transaction", "snapshot",
    "database", "sql", "nosql", "lance", "sqlite",
    "rust", "python", "golang", "typescript", "compiler",
    "serialize", "deserialize", "encoding", "encryption",
    "auth", "oauth", "jwt", "token", "session",
    "wal", "lsm", "btree", "hash", "bloom",
    "rpc", "grpc", "http", "websocket", "sse",
    "scheduler", "daemon", "worker", "dispatcher",
    "rag", "llm", "embedder", "reranker", "tokenizer",
}


def _is_tech_query(query: str) -> bool:
    """Check if a query contains technology-related keywords.

    The check is case-insensitive and matches substrings within words
    (e.g. "embedding" matches "embedder"). This is intentional: false
    positives produce a harmless signal; false negatives miss gaps.
    """
    query_lower = query.lower()
    return any(kw in query_lower for kw in TECH_KEYWORDS)


# English stop words (subset — full list of ~150 common words)
STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "having", "do", "does", "did",
    "doing", "will", "would", "could", "should", "may", "might",
    "can", "shall", "to", "of", "in", "for", "on", "with", "at",
    "by", "from", "as", "into", "through", "during", "before",
    "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "if", "then", "else", "when",
    "where", "why", "how", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "only", "own",
    "same", "than", "too", "very", "just", "about", "also",
    "this", "that", "these", "those", "it", "its", "he", "she",
    "they", "them", "we", "you", "i", "me", "my", "your", "our",
    "what", "which", "who", "whom", "whose",
    # Chinese stop words
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
    "它", "们", "那", "些", "什么", "怎么", "如何", "为什么",
    "可以", "这个", "那个", "还是", "只是", "但是", "因为", "所以",
}


def _extract_keywords(query: str) -> list[str]:
    """Extract 2-3 search keywords from a query using simple heuristics.

    Algorithm (no external dependencies):
    1. Normalize: lowercase, strip punctuation except hyphens
    2. Tokenize: split on whitespace for English; treat CJK chars as tokens
    3. Filter stop words
    4. Score remaining tokens: CAP-cased English nouns > tech keywords > rest
    5. Merge adjacent scored tokens into compound phrases
    6. Return top 3, ordered by priority

    Does NOT use spacy/nltk — keeps the dependency footprint zero.
    """
    import re

    # Normalize
    cleaned = re.sub(r'[^\w\s\-]', ' ', query)
    tokens = cleaned.split()

    # Separate English tokens from CJK
    en_tokens = []
    cjk_tokens = []

    for token in tokens:
        # Skip stop words (case-insensitive for English)
        if token.lower() in STOP_WORDS:
            continue
        # Detect CJK: if the token has any CJK character, treat separately
        if any('一' <= c <= '鿿' for c in token):
            # For CJK, each character is a "word", but pairs are more useful
            chars = [c for c in token if '一' <= c <= '鿿']
            # Generate bigrams (compound CJK phrases)
            for i in range(len(chars) - 1):
                cjk_tokens.append(chars[i] + chars[i + 1])
            # Also include single chars as fallback
            cjk_tokens.extend(chars)
        else:
            en_tokens.append(token)

    # Score English tokens: CAP-cased > lowercase tech > lowercase
    scored = []
    for token in en_tokens:
        score = 0
        # Heuristic: tokens starting with uppercase are likely proper nouns
        if token[0].isupper():
            score += 3
        # Tokens that match tech keywords get bonus
        if token.lower() in TECH_KEYWORDS:
            score += 2
        # Longer tokens are more specific
        if len(token) > 3:
            score += 1
        scored.append((score, token.lower()))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Build compound phrases: merge adjacent high-score tokens
    phrases = []
    i = 0
    en_result = [t for _, t in scored]
    while i < len(en_result):
        # Try 2-word compound
        if i + 1 < len(en_result):
            phrases.append(f"{en_result[i]} {en_result[i+1]}")
        # Try 3-word compound
        if i + 2 < len(en_result):
            phrases.append(f"{en_result[i]} {en_result[i+1]} {en_result[i+2]}")
        phrases.append(en_result[i])
        i += 1

    # Combine: compound phrases first, then single tokens, then CJK bigrams
    all_keywords = phrases[:3] + en_result[:3] + cjk_tokens[:2]

    # Deduplicate preserving order
    seen = set()
    result = []
    for kw in all_keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            result.append(kw)

    return result[:3]


def detect_gap(query: str, pack: "ContextPack") -> Optional[GapSignal]:
    """Detect knowledge gaps in context_supply results.

    Called as middleware in context_supply's return path.
    Returns None (no gap) or a GapSignal for consumers to act on.

    Three-tier detection:
    1. core layer has results → information is sufficient, no gap
    2. core is empty but related has >=3 items all with relevance > 0.45
       → associated knowledge is adequate, no gap
    3. core is empty and related is insufficient → gap detected

    Only triggers for queries containing technology keywords.
    Does NOT perform searches. Does NOT modify the pack.
    """
    # Guard: only technical queries can trigger
    if not _is_tech_query(query):
        return None

    # Tier 1: core layer populated → sufficient info
    if pack.core:
        return None

    # Tier 2: related layer has enough high-quality items
    related_with_relevance = [
        item for item in pack.related
        if getattr(item, 'relevance', 0) > 0.45
    ]
    if len(related_with_relevance) >= 3:
        return None

    # Tier 3: genuine knowledge gap
    return GapSignal(
        type="exemplar_needed",
        problem=query,
        suggested_search=_extract_keywords(query),
        auto_task=True,
        severity="medium",
    )
