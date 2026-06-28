"""Adaptive retrieval — decide whether a query warrants memory lookup.

Saves embedding API calls by skipping greetings, commands, and trivial input.
Force-retrieves when memory-related keywords are detected.
"""

import re


FORCE_RETRIEVE_PATTERNS = [
    "记得", "recall", "之前", "上次", "去年", "以前",
    "上次", "memory", "回忆", "previously", "last time",
    "历史", "history", "记录", "record",
]

SKIP_PATTERNS = [
    r"^/",
    r"^[:\w]+:$",
    r"^\?+$",
]

GREETINGS = ["hi", "hey", "hello", "你好", "早上好", "晚安", "good morning", "good evening"]
AFFIRMATIONS = ["ok", "okay", "好", "行", "可以", "thanks", "谢谢", "thx", "收到", "明白"]


def should_retrieve(query: str) -> bool:
    """Return True if the query warrants memory retrieval.

    Priority: skip patterns → force keywords → greetings → length check.

    Args:
        query: Raw user query text.

    Returns:
        True if memory retrieval should be performed.
    """
    q = query.strip()
    if not q:
        return False

    q_lower = q.lower()

    # 1. Skip patterns (regex) — commands/syntax always skip
    for pattern in SKIP_PATTERNS:
        if re.match(pattern, q):
            return False

    # 2. Force-retrieve keywords
    for pattern in FORCE_RETRIEVE_PATTERNS:
        if pattern.lower() in q_lower:
            return True

    # 3. Greetings (short only)
    if any(q_lower.startswith(g) for g in GREETINGS) and len(q) <= 15:
        return False

    # 4. Short affirmations
    stripped = q_lower.rstrip("!.,; :)！，。；：）")
    if stripped in AFFIRMATIONS:
        return False

    # 5. Default: check question marks + length
    has_question = "?" in q or "？" in q
    cjk_chars = sum(1 for c in q if "一" <= c <= "鿿" or "぀" <= c <= "ゟ")
    ascii_chars = sum(1 for c in q if c.isascii() and c.isalpha())

    if has_question:
        return True
    if cjk_chars >= 8:
        return True
    if ascii_chars >= 20:
        return True

    return False
