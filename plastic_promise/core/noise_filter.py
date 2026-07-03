"""Noise filter — prevent low-quality content from entering memory.

Filters: agent denials, meta-questions, short boilerplate, emoji-only messages.
English and Chinese patterns supported.
"""

import re

try:
    import emoji as _emoji

    def _is_emoji_only(text: str) -> bool:
        """Return True if text is predominantly emoji characters."""
        stripped = text.strip()
        if not stripped:
            return False
        total = len(stripped)
        emoji_count = _emoji.emoji_count(stripped)
        # Each emoji may be multiple code points; check if >50% are emoji
        return emoji_count > 0 and (emoji_count * 2) >= total
except ImportError:
    # Fallback: broad Unicode emoji range check (no external dependency)
    import unicodedata as _unicodedata

    def _is_emoji_only(text: str) -> bool:
        """Return True if text is predominantly emoji-like characters."""
        stripped = text.strip()
        if not stripped:
            return False
        emoji_chars = sum(1 for c in stripped if _unicodedata.category(c) in ("So", "Sk"))
        return emoji_chars > 0 and emoji_chars >= len(stripped) * 0.5


DENIAL_PATTERNS = [
    r"i (do ?n[o\']?t|don'?t) have.*(information|data|memory|record)",
    r"我没有(任何)?(相关)?(信息|数据|记忆|记录)",
    r"无法提供",
    r"cannot (provide|find|locate)",
    r"抱歉.*(无法|不能)",
]

META_QUESTION_PATTERNS = [
    r"你(还)?记得吗",
    r"do you (remember|recall|know about)",
    r"你有.*记忆",
    r"can you remember",
]

SHORT_BOILERPLATE = [
    "好的",
    "好吧",
    "行",
    "可以",
    "没问题",
    "收到",
    "明白",
    "了解",
    "知道了",
    "谢谢",
    "感谢",
    "多谢",
    "谢啦",
    "ok",
    "thanks",
    "thx",
    "got it",
]

LOW_INFORMATION_SNIPPETS = {
    "no file edits",
    "no edits",
    "md files only",
    "markdown files only",
    "read-only",
}

PARTIAL_URL_PATTERNS = [
    r"^https?://[^\s./]+$",
    r"^(com|org|net|io|dev|cn|ai)/[\w./-]+$",
]

TELEMETRY_PATTERNS = [
    r"^audit\s+trust=",
    r"^\[?skill (start|complete|abandoned)\]?",
]

BOILERPLATE_MAX_LENGTH = 10


def is_noise(text: str) -> bool:
    """Return True if text is low-quality and should not be stored as memory.

    Checks: emoji-only → length < 5 → denial patterns → meta-questions → short boilerplate.

    Args:
        text: Raw text to evaluate.

    Returns:
        True if the text should be filtered out.
    """
    t = text.strip()

    # Emoji-only detection: flag before length check
    if _is_emoji_only(t):
        return True

    if len(t) < 5:
        return True

    t_lower = t.lower()

    if t_lower in LOW_INFORMATION_SNIPPETS:
        return True

    for pattern in PARTIAL_URL_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    for pattern in TELEMETRY_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    for pattern in DENIAL_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    for pattern in META_QUESTION_PATTERNS:
        if re.search(pattern, t_lower):
            return True

    if len(t) <= BOILERPLATE_MAX_LENGTH:
        for phrase in SHORT_BOILERPLATE:
            if t_lower.startswith(phrase) and len(t) - len(phrase) <= 3:
                return True

    return False
