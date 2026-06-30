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

# Task-oriented keywords that always warrant retrieval (high concurrency, intensive ops)
TASK_KEYWORDS = [
    "优化", "性能", "并发", "缓存", "数据库", "索引", "查询",
    "初始化", "启动", "加载", "读取", "写入", "存储", "检索",
    "架构", "设计", "重构", "修复", "bug", "错误", "异常",
    "配置", "部署", "测试", "监控", "日志", "安全",
    "optimize", "performance", "concurrent", "cache", "database",
    "index", "query", "init", "startup", "load", "read", "write",
    "architecture", "design", "refactor", "fix", "error", "config",
    "deploy", "test", "monitor", "log", "security",
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

    # 2. Force-retrieve keywords (memory-specific)
    for pattern in FORCE_RETRIEVE_PATTERNS:
        if pattern.lower() in q_lower:
            return True

    # 3. Task-oriented keywords — always retrieve (engineering queries)
    for kw in TASK_KEYWORDS:
        if kw.lower() in q_lower:
            return True

    # 4. Greetings (short only)
    if any(q_lower.startswith(g) for g in GREETINGS) and len(q) <= 15:
        return False

    # 5. Short affirmations
    stripped = q_lower.rstrip("!.,; :)！，。；：）")
    if stripped in AFFIRMATIONS:
        return False

    # 6. Default: check question marks + length
    has_question = "?" in q or "？" in q
    cjk_chars = sum(1 for c in q if "一" <= c <= "鿿" or "぀" <= c <= "ゟ")
    ascii_chars = sum(1 for c in q if c.isascii() and c.isalpha())

    if has_question:
        return True
    # Lowered CJK threshold: 4 chars (2-3 word Chinese phrase) is enough for a meaningful query
    if cjk_chars >= 4:
        return True
    if ascii_chars >= 12:
        return True

    return False
