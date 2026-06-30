"""MCP Memory 工具 — 记忆域 8 个公开工具

公开工具:
- memory_recall   : 混合检索记忆，返回三层上下文包
- memory_store    : 存储一条记忆到 Plastic Promise 记忆池
- memory_update   : 更新已有记忆的内容或元数据
- memory_forget   : 软删除记忆（硬删除，标记为衰退待 GC 清理）
- memory_stats    : 获取记忆池统计信息（含 fuzzy buffer 积压）
- memory_list     : 按条件列出记忆
- memory_gc       : 手动触发垃圾回收
- memory_correct  : 人类纠正记忆

内部处理器 (not exposed as MCP tools):
- handle_fuzzy_status  : 查看模糊缓存区统计
- handle_fuzzy_process : 触发模糊缓存区处理流水线
"""

import hashlib
import json
import os
import datetime
import threading
import time
from typing import Any

from mcp.types import TextContent


# ---- Query result cache for memory_recall ----
_query_cache: dict[str, tuple[str, float]] = {}  # hash -> (json_result, timestamp)
_query_cache_lock = threading.Lock()
_QUERY_CACHE_SIZE = int(os.environ.get("PP_QUERY_CACHE_SIZE", "32"))
_QUERY_CACHE_TTL = float(os.environ.get("PP_QUERY_CACHE_TTL", "30"))  # seconds


def _cache_key(query: str, task_type: str, max_results: int, scope: str) -> str:
    raw = f"{query}|{task_type}|{max_results}|{scope}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(query: str, task_type: str, max_results: int, scope: str) -> str | None:
    key = _cache_key(query, task_type, max_results, scope)
    now = time.time()
    with _query_cache_lock:
        if key in _query_cache:
            result, ts = _query_cache[key]
            if now - ts < _QUERY_CACHE_TTL:
                return result
            del _query_cache[key]
    return None


def _cache_set(query: str, task_type: str, max_results: int, scope: str, result: str):
    key = _cache_key(query, task_type, max_results, scope)
    now = time.time()
    with _query_cache_lock:
        if len(_query_cache) >= _QUERY_CACHE_SIZE:
            oldest = min(_query_cache, key=lambda k: _query_cache[k][1])
            del _query_cache[oldest]
        _query_cache[key] = (result, now)


def _generate_federation_signals(pack, domain_hint, engine, federation):
    """Generate cross-domain federation signals on retrieval."""
    if not federation or not domain_hint or domain_hint == "all":
        return []
    dm = getattr(engine, '_dm', None)
    if dm is None:
        return []
    signals = []
    seen = set()
    for item in (pack.core + pack.related):
        item_domain = getattr(item, 'domain', '') or ''
        if item_domain and item_domain != domain_hint and item_domain != "all":
            key = (item_domain, domain_hint)
            if key not in seen:
                seen.add(key)
                signals.append({
                    "source": item_domain,
                    "target": domain_hint,
                    "signal": dm.generate_signal(item_domain, domain_hint,
                                                  getattr(item, 'id', '?'),
                                                  agent_id=getattr(engine, '_agent_owner', '')
                                                    or os.environ.get("AGENT_OWNER", ""))
                })
    return signals


# ---- memory_recall ----
async def handle_memory_recall(engine: Any, args: dict) -> list[TextContent]:
    """Hybrid memory retrieval: embed query -> ContextEngine.supply() -> ContextPack JSON.

    Calls ContextEngine.supply() for hybrid retrieval (vector + BM25 + RRF fusion
    + symbolic rules + graph traversal), returns three-layer context pack.

    Uses a short-lived query cache (PP_QUERY_CACHE_SIZE=32, PP_QUERY_CACHE_TTL=30s)
    to avoid redundant embedding + retrieval for repeated queries.
    """
    try:
        from plastic_promise.adaptive_retrieval import should_retrieve
        query = args["query"]
        if not should_retrieve(query):
            return [TextContent(type="text", text=json.dumps(
                {"skipped": True, "reason": "adaptive_retrieval",
                 "query": query[:100]},
                ensure_ascii=False))]

        task_type = args.get("task_type", "general")
        max_results = args.get("max_results", 20)
        scope = args.get("scope", "global")

        # Check query cache
        cached = _cache_get(query, task_type, max_results, scope)
        if cached is not None:
            return [TextContent(type="text", text=cached)]

        from plastic_promise.core.embedder import get_embedder, FallbackEmbedder
        domain_hint = args.get("domain_hint", None)
        federation = args.get("federation", True)

        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = embedder.embed(query)
        except Exception:
            embedder = FallbackEmbedder()
            vec = embedder.embed(query)
        pack = engine.supply(query, vec, task_type, scope)

        result_json = json.dumps({
            "core": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance,
                      "source": i.source, "freshness": i.freshness, "worth_score": i.worth_score}
                     for i in pack.core[:max_results]],
            "related": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance}
                        for i in pack.related[:max_results]],
            "divergent": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance}
                          for i in pack.divergent[:max_results]],
            "activated_principles": pack.activated_principles,
            "domain_hint": domain_hint,
            "federation_signals": _generate_federation_signals(pack, domain_hint, engine, federation),
            "total_items": pack.total_items,
            "audit": pack.audit_metadata,
        }, ensure_ascii=False, indent=2)

        # Cache the result
        _cache_set(query, task_type, max_results, scope, result_json)

        return [TextContent(type="text", text=result_json)]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_recall"}, ensure_ascii=False))]


# ---- memory_store ----
async def handle_memory_store(engine: Any, args: dict) -> list[TextContent]:
    """Store a memory: create MemoryRecord, persist to SQLite, embed and index.

    Args:
        engine: ContextEngine instance.
        args: {"content": str, "memory_type": str, "source"?: str,
               "scope"?: str, "entity_ids"?: list[str]}.

    Returns:
        list[TextContent]: MCP response with stored memory metadata.
    """
    try:
        from plastic_promise.core.noise_filter import is_noise
        content = args["content"]
        if is_noise(content):
            return [TextContent(type="text", text=json.dumps(
                {"stored": False, "reason": "noise_filtered",
                 "content_preview": content[:100]},
                ensure_ascii=False))]

        # Health check: 异步检测 MCP 服务器可用性（不阻塞事件循环）
        server_ok = True
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.get("http://127.0.0.1:9020/health", timeout=2.0)
        except Exception:
            server_ok = False

        memory_type = args.get("memory_type", "experience")
        source = args.get("source", "user")
        scope = args.get("scope", "global")
        entity_ids = args.get("entity_ids", [])
        custom_tags = args.get("tags", [])  # 用户指定的标签 (task:pending 等)

        owner = args.get("owner", "")

        # Auto-extract entity links from content (原则 #6 数据流驱动)
        extracted = _extract_entity_ids(content, engine)
        all_entities = list(set(entity_ids + extracted))

        # ALL memories go through fuzzy buffer pipeline first:
        #   raw → tagged(关键词) → classified(大类分L1/L3) → embedded(细分向量) → 迁移主池
        # This is the standard path, not a fallback. (原则 #4 上下文驱动, #10 自演化闭环)
        fb = _get_fuzzy_buffer(engine)
        fuzzy_id = fb.store_urgent(content, memory_type, source, entity_ids=all_entities, custom_tags=custom_tags)

        # Auto-link extracted entities to graph immediately
        for eid in all_entities:
            edge = {"from": fuzzy_id, "to": eid, "relation": "references", "weight": 0.5}
            if edge not in engine._graph_edges:
                engine._graph_edges.append(edge)

        # Process through pipeline immediately (同步处理——大类分完就入池)
        result = fb.process_pipeline()

        # Push SSE notification for real-time multi-agent awareness
        try:
            from plastic_promise.mcp.server import notify_issue_change
            notify_issue_change({
                "type": "memory_stored",
                "memory_id": fuzzy_id,
                "content_preview": content[:200],
                "memory_type": memory_type,
                "domain": getattr(engine, '_domain_hint', ''),
                "timestamp": __import__('datetime').datetime.now().isoformat(),
            })
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps({
            "stored": True,
            "memory_id": fuzzy_id,
            "content_preview": content[:200],
            "memory_type": memory_type,
            "scope": scope,
            "entity_ids": all_entities,
            "pipeline": result["pipeline"],
            "note": "必经流水线: raw→tagged→classified(大类)→embedded(细分)→主池",
            "server_ok": server_ok,
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_store"}, ensure_ascii=False))]


# ---- memory_update ----
async def handle_memory_update(engine: Any, args: dict) -> list[TextContent]:
    """Update a memory's content or metadata.

    Delegates to ContextEngine.update_memory() which builds UpdateFields
    from the provided keyword arguments.

    Args:
        engine: ContextEngine instance.
        args: {"memory_id": str, "content"?: str, "importance"?: float,
               "category"?: str, "reset_worth"?: bool}.

    Returns:
        list[TextContent]: MCP response confirming update.
    """
    try:
        memory_id = args["memory_id"]
        content = args.get("content")
        importance = args.get("importance")
        category = args.get("category")

        success = engine.update_memory(
            memory_id,
            content=content,
            importance=importance,
            category=category,
        )

        # If reset_worth is requested, zero out the worth counters
        if args.get("reset_worth"):
            engine.update_memory(
                memory_id,
                content=None,
                importance=None,
                category=None,
            )
            # Access and reset worth counters on the record object
            record = engine.get_memory(memory_id)
            if record is not None:
                record.worth_success = 0
                record.worth_failure = 0
                engine.store_memory(record)  # re-store to persist

        return [TextContent(type="text", text=json.dumps({
            "updated": success,
            "memory_id": memory_id,
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_update"}, ensure_ascii=False))]


# ---- memory_forget ----
async def handle_memory_forget(engine: Any, args: dict) -> list[TextContent]:
    """Delete a memory (hard delete from SQLite).

    The memory is permanently removed from the storage backend.
    For soft-delete semantics, reduce importance to near-zero instead.

    Args:
        engine: ContextEngine instance.
        args: {"memory_id": str, "reason"?: str}.

    Returns:
        list[TextContent]: MCP response confirming deletion.
    """
    try:
        memory_id = args["memory_id"]
        reason = args.get("reason", "")

        # Clean LanceDB vector store first (prevents orphan vector entries)
        ldb = getattr(engine, '_ldb', None)
        if ldb is not None:
            try:
                ldb.delete(memory_id)
            except Exception:
                pass

        success = engine.delete_memory(memory_id)

        return [TextContent(type="text", text=json.dumps({
            "forgotten": success,
            "memory_id": memory_id,
            "reason": reason,
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_forget"}, ensure_ascii=False))]


# ---- memory_stats ----
async def handle_memory_stats(engine: Any, args: dict) -> list[TextContent]:
    """Return memory pool statistics.

    Delegates to ContextEngine.memory_stats_json() which computes
    aggregate statistics from the SQLite backend.

    Args:
        engine: ContextEngine instance.
        args: {"scope"?: str} (optional namespace filter).

    Returns:
        list[TextContent]: MCP response with memory pool statistics.
    """
    try:
        scope = args.get("scope")

        stats_json = engine.memory_stats_json(scope)
        stats = json.loads(stats_json)

        result = {
            "total": stats.get("total", 0),
            "healthy": stats.get("healthy", 0),
            "decaying": stats.get("decaying", 0),
            "by_tier": stats.get("by_tier", {}),
            "by_type": stats.get("by_type", {}),
            "by_category": stats.get("by_category", {}),
            "average_worth": stats.get("average_worth", 0.0),
        }

        # 追加 fuzzy buffer 积压信息
        try:
            fb = _get_fuzzy_buffer(engine)
            if fb:
                result["fuzzy_buffer"] = fb.stats()
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_stats"}, ensure_ascii=False))]


# ---- memory_list ----
async def handle_memory_list(engine: Any, args: dict) -> list[TextContent]:
    """List memories by filter criteria.

    Delegates to ContextEngine.list_memories() with optional filters
    for memory_type, source, min_worth, limit, and scope.

    Args:
        engine: ContextEngine instance.
        args: {"memory_type"?: str, "source"?: str, "min_worth"?: float,
               "limit"?: int, "scope"?: str}.

    Returns:
        list[TextContent]: MCP response with filtered memory items.
    """
    try:
        memory_type = args.get("memory_type")
        source = args.get("source")
        min_worth = args.get("min_worth")
        limit = args.get("limit", 50)
        scope = args.get("scope")

        results = engine.list_memories(
            memory_type=memory_type,
            source=source,
            min_worth=min_worth,
            limit=limit,
            scope=scope,
        )

        items = [{"id": r.id, "content": r.content[:300], "memory_type": r.memory_type,
                  "source": r.source, "tier": r.tier, "worth_score": r.worth_score(),
                  "created_at": r.created_at}
                 for r in results]

        return [TextContent(type="text", text=json.dumps(
            {"items": items, "count": len(items)}, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_list"}, ensure_ascii=False))]


# ---- memory_gc ----
async def handle_memory_gc(engine: Any, args: dict) -> list[TextContent]:
    """Run garbage collection on decaying memories.

    Delegates to MemoryGC.collect() which performs:
    1. Mark decaying candidates (worth_score < threshold)
    2. Merge similar memories (cosine similarity >= 0.70)
    3. Remove decayed records and persist merged metadata to SQLite

    Args:
        engine: ContextEngine instance.
        args: {"dry_run"?: bool, "force"?: bool}.

    Returns:
        list[TextContent]: MCP response with GC results from MemoryGC.
    """
    try:
        dry_run = args.get("dry_run", True)
        force = args.get("force", False)

        from plastic_promise.memory.soul_memory import RecMem, MemoryGC
        rm = RecMem(engine)
        gc = MemoryGC(rm)
        result = gc.collect(dry_run=dry_run, force=force)

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_gc"}, ensure_ascii=False))]


# ---- _extract_entity_ids (internal helper) ----
def _extract_entity_ids(content: str, engine: Any) -> list[str]:
    """Auto-extract entity references from memory content.

    Matches known principle names and graph node names against content.
    Serves principle #6 (data-flow driven — actual content → actual links).
    """
    entity_ids = []
    try:
        from plastic_promise.core.constants import CORE_PRINCIPLES
        # Match principle names
        for p in CORE_PRINCIPLES:
            if p["name"][:4] in content or p["name"][-4:] in content:
                pid = f"principle:{p['id']}"
                if pid not in entity_ids:
                    entity_ids.append(pid)
        # Match existing graph nodes
        for nid in engine._graph_nodes:
            name = engine._graph_nodes[nid].get("name", "")
            if name and len(name) >= 3 and name in content:
                if nid not in entity_ids:
                    entity_ids.append(nid)
    except Exception:
        pass
    return entity_ids


# ---- _get_fuzzy_buffer (internal helper) ----
def _get_fuzzy_buffer(engine: Any):
    """Get or create a FuzzyBuffer attached to the engine."""
    if not hasattr(engine, '_fuzzy_buffer') or engine._fuzzy_buffer is None:
        from plastic_promise.memory.pipeline import MemoryPipeline
        from plastic_promise.memory.soul_memory import MemoryTierManager, RecMem
        from plastic_promise.core.embedder import get_embedder

        rec_mem = engine._rec_mem if hasattr(engine, '_rec_mem') else RecMem(engine)
        try:
            embedder = get_embedder()
        except Exception:
            from plastic_promise.core.embedder import FallbackEmbedder
            embedder = FallbackEmbedder()
        tier_mgr = MemoryTierManager(rec_mem)
        engine._fuzzy_buffer = MemoryPipeline(
            rec_mem=rec_mem, embedder=embedder, tier_manager=tier_mgr,
            domain_manager=getattr(engine, '_dm', None),
            lancedb=getattr(engine, '_ldb', None),
        )
        engine._rec_mem = rec_mem
    return engine._fuzzy_buffer


# ---- fuzzy_status (internal — not exposed as MCP tool) ----
async def handle_fuzzy_status(engine: Any, args: dict) -> list[TextContent]:
    """Query fuzzy buffer statistics — items per stage, total, oldest pending."""
    try:
        fb = _get_fuzzy_buffer(engine)
        stats = fb.stats()
        return [TextContent(type="text", text=json.dumps(stats, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "fuzzy_status"}, ensure_ascii=False))]


# ---- fuzzy_process (internal — not exposed as MCP tool) ----
async def handle_fuzzy_process(engine: Any, args: dict) -> list[TextContent]:
    """Trigger fuzzy buffer pipeline processing (raw→tagged→embedded→classified→migrate)."""
    try:
        fb = _get_fuzzy_buffer(engine)
        result = fb.process_pipeline()
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "fuzzy_process"}, ensure_ascii=False))]


# ---- memory_correct ----
async def handle_memory_correct(engine: Any, args: dict) -> list[TextContent]:
    """Human-in-the-loop memory correction — edit, deprecate, or mark memory quality.

    Serves principles #2 (transparency) and #3 (audit closure) by giving
    users explicit control over AI memories.
    """
    try:
        memory_id = args["memory_id"]
        new_content = args.get("content")
        mark_as = args.get("mark_as")  # "corrected" | "deprecated" | "wrong"
        reason = args.get("reason", "")

        record = engine.get_memory(memory_id)
        if record is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Memory {memory_id} not found"}, ensure_ascii=False))]

        actions = []

        # Content update
        if new_content is not None and new_content != record.content:
            engine.update_memory(memory_id, content=new_content)
            # Refresh record after update, then reset worth counters
            record = engine.get_memory(memory_id)
            if record is not None:
                record.worth_success = 0
                record.worth_failure = 0
                engine.store_memory(record)
            actions.append("content_updated")

        # Quality marking
        if mark_as == "wrong":
            record.record_rejected()
            engine.store_memory(record)
            actions.append("marked_wrong")
        elif mark_as == "deprecated":
            engine.delete_memory(memory_id)
            actions.append("deprecated")
        elif mark_as == "corrected":
            record.record_adopted()
            engine.store_memory(record)
            actions.append("marked_corrected")

        # Trigger EvolveR after correction — 自演化闭环
        try:
            from plastic_promise.memory.soul_memory import RecMem, EvolveR
            rm = RecMem(engine)
            evolver = EvolveR(rm)
            evolver.evolve_cycle()
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps({
            "corrected": True,
            "memory_id": memory_id,
            "actions": actions,
            "reason": reason,
            "worth_score": record.worth_score() if hasattr(record, 'worth_score') else None,
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_correct"}, ensure_ascii=False))]
