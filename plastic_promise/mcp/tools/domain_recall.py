"""MCP domain_recall 工具 — 按域读取管道

按行为域 (building/designing/reflecting/fixing/governing/connecting)
检索记忆，返回域标签匹配 + 衰减加权 + 文本关键词过滤 + worth 排序的结果。

与 memory_recall 的区别：
  - memory_recall: 语义向量检索 (embedding → LanceDB + BM25 → RRF 融合)
  - domain_recall:  域标签精确匹配 + 文本关键词 + 衰减/价值加权 (不需要 embedding)
"""

import asyncio
import datetime
import json
import re
from typing import Any

from mcp.types import TextContent


# 7 个有效行为域 (all 不参与分配)
VALID_DOMAINS = {"building", "designing", "reflecting", "fixing", "governing", "connecting", "all"}


def _compute_freshness(mem: dict, decay_calc=None) -> float:
    """Compute freshness score for a memory using Weibull decay.

    Returns 1.0 for new memories (no created_at), exponentially decaying
    towards 0.05 for old ones.
    """
    created_at = mem.get("created_at", "")
    if not created_at:
        return 1.0
    if decay_calc is None:
        from plastic_promise.core.decay_engine import WeibullDecayCalculator

        decay_calc = WeibullDecayCalculator()
    tier = mem.get("tier", "L2")
    effective_tier = tier if tier in ("L1", "L3") else "default"
    return decay_calc.compute_decay(effective_tier, created_at)


def _compute_worth(mem: dict) -> float:
    """Compute normalised worth score [0, 1] from worth_success/worth_failure counters."""
    success = mem.get("worth_success", 0)
    fail = mem.get("worth_failure", 0)
    total = success + fail
    if total == 0:
        return 0.5  # neutral
    return success / total


def _score(
    mem: dict, text_relevance: float, freshness: float, worth: float, domain_match_type: str
) -> float:
    """Composite score: text relevance + freshness + worth + domain bonus.

    Weights:
      - text_relevance: 0.35 (0 if no query)
      - freshness:      0.30
      - worth:          0.35
      - domain_bonus:   exact match ×1.2, tag overlap ×1.1
    """
    base = text_relevance * 0.35 + freshness * 0.30 + worth * 0.35
    if domain_match_type == "exact":
        base = min(base * 1.2, 1.0)
    elif domain_match_type == "tag":
        base = min(base * 1.1, 1.0)
    return base


async def handle_domain_recall(engine: Any, args: dict) -> list[TextContent]:
    """Domain-aware memory retrieval: filters by domain tags, applies
    decay + worth scoring, optional text query matching.

    Args:
        engine: ContextEngine instance.
        args:
            domain: str (required) — domain name: building|designing|reflecting|fixing|governing|connecting|all
            query: str (optional) — text keywords for filtering within domain
            max_results: int — max results (default 20)
            min_worth: float — minimum worth_score [0,1] (default 0.0)
            min_freshness: float — minimum freshness [0,1] (default 0.05)
            source: str — filter by source (optional)

    Returns:
        list[TextContent]: ranked memory items with domain, freshness, worth scores.
    """
    try:
        domain = args.get("domain", "")
        if not domain or domain not in VALID_DOMAINS:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Invalid or missing domain. Valid: {', '.join(sorted(VALID_DOMAINS))}",
                            "domain": domain,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        query = args.get("query", "")
        max_results = min(args.get("max_results", 20), 100)
        min_worth = args.get("min_worth", 0.0)
        min_freshness = args.get("min_freshness", 0.05)
        source_filter = args.get("source", "")

        # Ensure DomainManager is loaded
        engine.ensure_heavy_init()

        dm = getattr(engine, "_dm", None)
        domain_config = dm.domains.get(domain) if dm else None
        domain_tags: set = domain_config.tags if domain_config else set()

        # Build query bigrams for text matching
        has_query = bool(query and query.strip())
        query_bigrams: set = set()
        if has_query:
            has_cjk = bool(re.search(r"[一-鿿]", query))
            if has_cjk:
                for i in range(len(query) - 1):
                    bg = query[i : i + 2]
                    if not re.search(r"[\s，。！？、；：,.!?;:\s]", bg):
                        query_bigrams.add(bg)
            else:
                query_bigrams = set(query.lower().split())

        # Init decay calculator once
        from plastic_promise.core.decay_engine import WeibullDecayCalculator

        decay_calc = WeibullDecayCalculator()

        current_owner = __import__("os").environ.get("AGENT_OWNER", "")

        scored: list[dict] = []

        for mid, mem in engine.iter_memories():
            # Owner filter
            mem_owner = mem.get("owner", "")
            if current_owner and mem_owner not in (current_owner, "shared", ""):
                continue

            # Source filter
            if source_filter and mem.get("source", "") != source_filter:
                continue

            # —— Domain matching ——
            mem_domain = mem.get("domain", "")
            domain_match_type = "none"

            if domain == "all":
                domain_match_type = "exact"  # "all" matches everything
            elif mem_domain == domain:
                domain_match_type = "exact"
            elif domain_tags:
                mem_tags = set(mem.get("tags", []))
                if mem_tags & domain_tags:
                    domain_match_type = "tag"

            if domain_match_type == "none":
                continue

            # —— Text relevance (optional query) ——
            text_relevance = 0.5  # neutral when no query
            if has_query and query_bigrams:
                content = mem.get("content", "")
                if has_cjk:
                    hits = sum(1.0 for bg in query_bigrams if bg in content)
                    text_relevance = hits / len(query_bigrams)
                else:
                    hits = sum(1.0 for w in query_bigrams if w.lower() in content.lower())
                    text_relevance = hits / len(query_bigrams)
                if text_relevance == 0:
                    continue  # no text match when query is given

            # —— Freshness ——
            freshness = _compute_freshness(mem, decay_calc)
            if freshness < min_freshness:
                continue

            # —— Worth ——
            worth = _compute_worth(mem)
            if worth < min_worth:
                continue

            # —— Composite score ——
            composite = _score(mem, text_relevance, freshness, worth, domain_match_type)

            scored.append(
                {
                    "id": mid,
                    "content": mem.get("content", "")[:300],
                    "source": mem.get("source", "?"),
                    "tier": mem.get("tier", "L2"),
                    "domain": mem_domain or "uncategorized",
                    "domain_match": domain_match_type,
                    "text_relevance": round(text_relevance, 4),
                    "freshness": round(freshness, 4),
                    "worth_score": round(worth, 4),
                    "composite_score": round(composite, 4),
                    "tags": mem.get("tags", [])[:8],
                    "created_at": mem.get("created_at", ""),
                }
            )

        # Sort by composite score descending
        scored.sort(key=lambda x: x["composite_score"], reverse=True)

        # Top N
        results = scored[:max_results]

        # Domain stats
        total_in_domain = sum(
            1
            for mem in engine.iter_memories()
            if mem.get("domain") == domain
            or (domain == "all")
            or (domain_tags and set(mem.get("tags", [])) & domain_tags)
        )

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "domain": domain,
                        "query": query,
                        "results": results,
                        "total_in_domain": total_in_domain,
                        "total_results": len(results),
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": str(e),
                        "domain": args.get("domain", ""),
                    },
                    ensure_ascii=False,
                ),
            )
        ]
