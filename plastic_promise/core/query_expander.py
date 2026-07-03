"""Query expansion — local synonym dictionary for BM25 text retrieval.

Zero API calls. Chinese substring match + English word-boundary regex.
Domain-aware filtering. Used ONLY for BM25 text search, not vector search.
"""

import os as _os
import re

# ── Synonym dictionary ──────────────────────────────────────────
# Each entry: cn=[Chinese triggers], en=[English triggers],
#             expansions=[terms to append], domains=[applicable domains]

SYNONYM_MAP = [
    # ── Fixing domain ──
    {
        "cn": ["挂了", "炸了", "宕机", "崩溃"],
        "en": ["crashed", "down", "crash"],
        "expansions": ["崩溃", "crash", "error", "报错"],
        "domains": ["fixing", "reflecting"],
    },
    {
        "cn": ["卡住", "卡死", "挂起", "没反应"],
        "en": ["stuck", "frozen", "hung", "hanging"],
        "expansions": ["卡住", "hang", "freeze", "timeout", "超时"],
        "domains": ["fixing"],
    },
    {
        "cn": ["踩坑", "坑", "陷阱"],
        "en": ["pitfall", "trap"],
        "expansions": ["bug", "问题", "教训", "troubleshoot"],
        "domains": ["fixing", "reflecting"],
    },
    # ── Building domain ──
    {
        "cn": ["配置", "设置"],
        "en": ["config", "configuration", "settings"],
        "expansions": ["config", "configuration", "settings"],
        "domains": ["building"],
    },
    {
        "cn": ["部署", "上线", "发布"],
        "en": ["deploy", "release", "ship"],
        "expansions": ["deploy", "发布", "上线", "release"],
        "domains": ["building", "governing"],
    },
    {
        "cn": ["慢", "卡顿", "性能"],
        "en": ["slow", "performance", "lag"],
        "expansions": ["性能", "performance", "优化", "optimize", "slow"],
        "domains": ["building", "fixing"],
    },
    {
        "cn": ["重构", "重写"],
        "en": ["refactor", "rewrite"],
        "expansions": ["refactor", "重写", "rewrite", "清理", "cleanup"],
        "domains": ["building"],
    },
    # ── Designing domain ──
    {
        "cn": ["架构", "设计"],
        "en": ["architecture", "design"],
        "expansions": ["架构", "architecture", "设计", "design", "结构"],
        "domains": ["designing"],
    },
    # ── Memory/Knowledge domain ──
    {
        "cn": ["忘了", "不记得", "记不清"],
        "en": ["forgot", "remember", "recall"],
        "expansions": ["记忆", "memory", "recall", "之前", "previously"],
        "domains": ["designing", "fixing", "reflecting"],
    },
    {
        "cn": ["记忆", "回忆"],
        "en": ["memory", "recall", "memories"],
        "expansions": ["记忆", "memory", "recall", "检索", "retrieval"],
        "domains": ["designing", "reflecting"],
    },
    # ── Governing domain ──
    {
        "cn": ["信任", "信任分"],
        "en": ["trust", "trust_score"],
        "expansions": ["信任", "trust", "trust_score", "自主", "autonomy"],
        "domains": ["governing"],
    },
    {
        "cn": ["原则", "约定"],
        "en": ["principle", "commitment"],
        "expansions": ["原则", "principle", "约定", "commitment", "治理", "governance"],
        "domains": ["governing", "designing"],
    },
    # ── Reflecting domain ──
    {
        "cn": ["审计", "审查", "复盘"],
        "en": ["audit", "review", "postmortem"],
        "expansions": ["审计", "audit", "审查", "review", "复盘", "retrospective"],
        "domains": ["reflecting", "governing"],
    },
    {
        "cn": ["教训", "经验"],
        "en": ["lesson", "experience"],
        "expansions": ["教训", "lesson", "经验", "experience", "学到的"],
        "domains": ["reflecting"],
    },
    # ── General ──
    {
        "cn": ["测试", "验证"],
        "en": ["test", "verify", "validate"],
        "expansions": ["测试", "test", "验证", "verify", "检查", "check"],
        "domains": ["building", "fixing"],
    },
    {
        "cn": ["权限", "认证", "授权"],
        "en": ["auth", "permission", "access"],
        "expansions": ["权限", "permission", "认证", "auth", "安全", "security"],
        "domains": ["governing", "fixing"],
    },
]

_MAX_EXPANSION_TERMS = int(_os.environ.get("PP_QUERY_EXPANSION_MAX", "3"))


def expand_query(query: str, domain_hint: str = None) -> str:
    """Expand query with domain-relevant synonyms for BM25 text retrieval.

    Chinese: exact substring match (no word boundaries in CJK).
    English: \\b word-boundary regex to avoid false positives.
    Max 3 expansion terms. Already-present terms skipped (idempotent).
    Short queries (<2 chars) pass through unchanged.
    Domain filtering: when domain_hint is set, only matching domains activate.

    Args:
        query: Raw user query text.
        domain_hint: Optional domain scope (building/fixing/designing/reflecting/governing).

    Returns:
        Expanded query string (original + up to 3 synonyms), or original if no match.
    """
    if not query or len(query.strip()) < 2:
        return query

    q = query.strip()
    added: set[str] = set()
    tokens_lower = set(q.lower().split())

    for entry in SYNONYM_MAP:
        # Domain filter: skip entries not matching the current domain context
        if domain_hint and domain_hint not in entry.get("domains", []):
            continue

        matched = False

        # Chinese: exact substring match
        for cn_term in entry.get("cn", []):
            if cn_term in q:
                matched = True
                break

        # English: word-boundary regex
        if not matched:
            for en_term in entry.get("en", []):
                try:
                    if re.search(r"\b" + re.escape(en_term) + r"\b", q, re.IGNORECASE):
                        matched = True
                        break
                except re.error:
                    pass

        if not matched:
            continue

        # Add expansion terms not already in the query
        for exp in entry.get("expansions", []):
            exp_lower = exp.lower()
            if exp_lower not in tokens_lower and exp not in q:
                added.add(exp)
                if len(added) >= _MAX_EXPANSION_TERMS:
                    break

        if len(added) >= _MAX_EXPANSION_TERMS:
            break

    if added:
        return q + " " + " ".join(added)
    return q
