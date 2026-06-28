"""MCP Memory 工具 — 记忆域 7 个工具

工具列表:
- memory_recall   : 混合检索记忆，返回三层上下文包
- memory_store    : 存储一条记忆到 Plastic Promise 记忆池
- memory_update   : 更新已有记忆的内容或元数据
- memory_forget   : 软删除记忆（硬删除，标记为衰退待 GC 清理）
- memory_stats    : 获取记忆池统计信息
- memory_list     : 按条件列出记忆
- memory_gc       : 手动触发垃圾回收
"""

import json
import datetime
from typing import Any

from mcp.types import TextContent


# ---- memory_recall ----
async def handle_memory_recall(engine: Any, args: dict) -> list[TextContent]:
    """Hybrid memory retrieval: embed query -> ContextEngine.supply() -> ContextPack JSON.

    Calls ContextEngine.supply() for hybrid retrieval (vector + BM25 + RRF fusion
    + symbolic rules + graph traversal), returns three-layer context pack.

    Args:
        engine: ContextEngine instance.
        args: {"query": str, "task_type"?: str, "max_results"?: int,
               "scope"?: str}.

    Returns:
        list[TextContent]: MCP response with core/related/divergent layers.
    """
    try:
        from plastic_promise.adaptive_retrieval import should_retrieve
        query = args["query"]
        if not should_retrieve(query):
            return [TextContent(type="text", text=json.dumps(
                {"skipped": True, "reason": "adaptive_retrieval",
                 "query": query[:100]},
                ensure_ascii=False))]

        from plastic_promise.embedder import get_embedder, FallbackEmbedder
        task_type = args.get("task_type", "general")
        max_results = args.get("max_results", 20)
        scope = args.get("scope", "global")

        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = embedder.embed(query)
        except Exception:
            embedder = FallbackEmbedder()
            vec = embedder.embed(query)
        pack = engine.supply(query, vec, task_type, scope)

        return [TextContent(type="text", text=json.dumps({
            "core": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance,
                      "source": i.source, "freshness": i.freshness, "worth_score": i.worth_score}
                     for i in pack.core[:max_results]],
            "related": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance}
                        for i in pack.related[:max_results]],
            "divergent": [{"id": i.id, "content": i.content[:500], "relevance": i.relevance}
                          for i in pack.divergent[:max_results]],
            "activated_principles": pack.activated_principles,
            "total_items": pack.total_items,
            "audit": pack.audit_metadata,
        }, ensure_ascii=False, indent=2))]
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
        from plastic_promise.noise_filter import is_noise
        content = args["content"]
        if is_noise(content):
            return [TextContent(type="text", text=json.dumps(
                {"stored": False, "reason": "noise_filtered",
                 "content_preview": content[:100]},
                ensure_ascii=False))]

        memory_type = args.get("memory_type", "experience")
        source = args.get("source", "user")
        scope = args.get("scope", "global")
        entity_ids = args.get("entity_ids", [])

        memory_id = f"mem_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        # Create MemoryRecord — Rust PyO3 first, Python fallback
        try:
            import context_engine_core
            record = context_engine_core.MemoryRecord(
                memory_id, content, memory_type, source
            )
        except ImportError:
            from plastic_promise.core.context_engine import MemoryRecord
            record = MemoryRecord(
                memory_id, content, memory_type, source
            )
        record.scope = scope
        record.category = "other"
        record.importance = 0.7
        record.entity_ids = entity_ids
        record.created_at = datetime.datetime.now().isoformat()

        # Persist via ContextEngine delegation
        stored_id = engine.store_memory(record)

        # Embed and prepare for vector indexing (embedding computed for future use)
        from plastic_promise.embedder import get_embedder, FallbackEmbedder
        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = embedder.embed(content)
        except Exception:
            embedder = FallbackEmbedder()
            vec = embedder.embed(content)

        return [TextContent(type="text", text=json.dumps({
            "stored": True,
            "memory_id": stored_id,
            "content_preview": content[:200],
            "memory_type": memory_type,
            "scope": scope,
            "vector_dim": len(vec),
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

        return [TextContent(type="text", text=json.dumps({
            "total": stats.get("total", 0),
            "healthy": stats.get("healthy", 0),
            "decaying": stats.get("decaying", 0),
            "by_tier": stats.get("by_tier", {}),
            "by_type": stats.get("by_type", {}),
            "by_category": stats.get("by_category", {}),
            "average_worth": stats.get("average_worth", 0.0),
        }, ensure_ascii=False))]
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

    Lists all memories, identifies candidates with worth_score < 0.15
    and zero access count, then deletes them unless dry_run is True.

    Args:
        engine: ContextEngine instance.
        args: {"dry_run"?: bool, "force"?: bool}.

    Returns:
        list[TextContent]: MCP response with GC results.
    """
    try:
        dry_run = args.get("dry_run", True)

        # Fetch all memories (up to a high limit)
        all_mems = engine.list_memories(limit=10000)

        # Identify decaying candidates: low worth and never accessed
        decaying = [m for m in all_mems
                    if m.worth_score() < 0.15 and m.access_count == 0]

        freed_ids = []
        if not dry_run:
            for m in decaying:
                engine.delete_memory(m.id)
                freed_ids.append(m.id)

        return [TextContent(type="text", text=json.dumps({
            "dry_run": dry_run,
            "candidates": len(decaying),
            "deleted": 0 if dry_run else len(decaying),
            "freed_ids": freed_ids,
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "memory_gc"}, ensure_ascii=False))]
