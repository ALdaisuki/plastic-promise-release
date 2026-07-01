"""MCP Memory 工具 — 记忆域

公开工具:
- memory_recall, memory_store, memory_update, memory_forget
- memory_list, memory_gc, memory_correct
- memory_sync_files, memory_reclassify
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
        strict = args.get("strict", False)
        pack = args.get("pack", None)

        # Check query cache
        cached = _cache_get(query, task_type, max_results, scope)
        if cached is not None:
            return [TextContent(type="text", text=cached)]

        from plastic_promise.core.embedder import get_embedder, FallbackEmbedder
        domain_hint = args.get("domain_hint", None)
        federation = args.get("federation", True)

        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = await embedder.aembed(query)
        except Exception:
            embedder = FallbackEmbedder()
            vec = await embedder.aembed(query)
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

        # strict mode: return empty on no core matches
        if strict and not pack.core:
            return [TextContent(type="text", text=json.dumps(
                {"strict": True, "core": [], "message": "no matches in strict mode"},
                ensure_ascii=False))]

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
        content = args.get("content", "")
        if not content or (isinstance(content, str) and not content.strip()):
            return [TextContent(type="text", text=json.dumps(
                {"stored": False, "reason": "empty_content",
                 "note": "memory_store requires non-empty 'content'"},
                ensure_ascii=False))]
        if is_noise(content):
            return [TextContent(type="text", text=json.dumps(
                {"stored": False, "reason": "noise_filtered",
                 "content_preview": content[:100]},
                ensure_ascii=False))]

        # Health check: detect MCP server availability (non-blocking, cached)
        server_ok = getattr(engine, '_server_alive', True)

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
        _max_llm = args.get("max_llm_calls", 3)
        fuzzy_id = fb.store_urgent(content, memory_type, source, entity_ids=all_entities, custom_tags=custom_tags,
                                    max_llm_calls=_max_llm, skip_embed=(_max_llm == 0))

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


# ═══════════════════════════════════════════════════════════════
# memory_reclassify — 批量重跑分类管线
# ═══════════════════════════════════════════════════════════════

async def handle_memory_reclassify(engine: Any, args: dict) -> list[TextContent]:
    """原地重分类存量记忆 — 只走规则分类(大类tier/domain/category)，不创建新记录。

    使用 fuzzy_buffer 的 Stage 2 分类能力（tagged→classified），
    但直接更新现有 SQLite 记录，不经过 Stage 4 的 store() 创建副本。
    """
    batch_size = args.get("batch_size", 50)
    dry_run = args.get("dry_run", False)

    # Import classification components
    from plastic_promise.smart_extractor import _classify_by_rules
    from plastic_promise.memory.soul_memory import MemoryRecord, MemoryTierManager
    from plastic_promise.core.domain_manager import DomainManager

    fb = _get_fuzzy_buffer(engine)
    tier_mgr = fb._tier_manager
    dm = getattr(engine, '_dm', None)

    sqlite = getattr(engine, '_sqlite', None)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    reclassified = 0
    skipped = 0
    errors = 0
    by_category = {}
    by_domain = {}

    pending = []
    for mid, mem in engine._memories.items():
        if not isinstance(mem, dict):
            continue
        tags = list(mem.get("tags", []))
        if "status:replaced" in tags:
            skipped += 1
            continue
        pending.append((mid, mem, tags))

    batch = pending[:batch_size]
    remaining = max(0, len(pending) - batch_size)

    for mid, mem, old_tags in batch:
        try:
            content = mem.get("content", "")
            if not content.strip():
                skipped += 1
                continue

            # ── 1. Category: rule-based keyword matching ──
            cat, conf = _classify_by_rules(content)
            new_category = cat if cat else mem.get("category", "other")

            # ── 2. Tier: MemoryTierManager.classify_tier ──
            new_tier = "L1"
            if tier_mgr is not None:
                try:
                    mr = MemoryRecord(
                        content=content,
                        memory_type=mem.get("memory_type", "experience"),
                        source=mem.get("source", "user"),
                    )
                    mr.access_count = mem.get("access_count", 0)
                    mr.worth_success = mem.get("worth_success", 0)
                    mr.worth_failure = mem.get("worth_failure", 0)
                    new_tier = tier_mgr.classify_tier(mr)
                except Exception:
                    pass

            # ── 3. Domain: DomainManager.assign ──
            new_domain = mem.get("domain", "uncategorized")
            new_tags = list(old_tags)
            if cat and f"cat:{cat}" not in new_tags:
                new_tags.append(f"cat:{cat}")

            # ── 3.5. LLM pending: tag uncertain classifications for background refinement ──
            if new_category == "other" or conf < 0.5:
                if "llm_pending:true" not in new_tags:
                    new_tags.append("llm_pending:true")
            else:
                # Remove llm_pending if category is now confident
                if "llm_pending:true" in new_tags:
                    new_tags.remove("llm_pending:true")

            if dm is not None and (new_domain == "uncategorized" or new_domain is None):
                try:
                    assigned = dm.assign(new_tags, agent_id="system")
                    if assigned and assigned != "uncategorized":
                        new_domain = assigned
                except Exception:
                    pass

            # ── 4. Apply changes in-place ──
            changed = (
                new_category != mem.get("category", "other")
                or new_tier != mem.get("tier", "L1")
                or new_domain != mem.get("domain", "uncategorized")
                or set(new_tags) != set(old_tags)
            )

            if changed and not dry_run:
                # Update in-memory engine dict
                engine._memories[mid]["category"] = new_category
                engine._memories[mid]["tier"] = new_tier
                engine._memories[mid]["domain"] = new_domain
                engine._memories[mid]["tags"] = new_tags

                # Update SQLite
                if sqlite is not None:
                    try:
                        sqlite._conn.execute(
                            "UPDATE memories SET category = ?, tier = ?, domain = ?, tags = ? WHERE id = ?",
                            (new_category, new_tier, new_domain, json.dumps(new_tags), mid)
                        )
                        sqlite._conn.commit()
                    except Exception:
                        pass

                by_category[new_category] = by_category.get(new_category, 0) + 1
                by_domain[new_domain] = by_domain.get(new_domain, 0) + 1
                reclassified += 1
            elif changed and dry_run:
                reclassified += 1
                by_category[new_category] = by_category.get(new_category, 0) + 1
                by_domain[new_domain] = by_domain.get(new_domain, 0) + 1
            else:
                skipped += 1

        except Exception:
            errors += 1

    return [TextContent(type="text", text=json.dumps({
        "reclassified": reclassified,
        "remaining": remaining,
        "skipped": skipped,
        "errors": errors,
        "batch_size": batch_size,
        "dry_run": dry_run,
        "total": len(engine._memories),
        "last_id": batch[-1][0] if batch else None,
        "category_distribution": by_category,
        "domain_distribution": by_domain,
    }, ensure_ascii=False))]


# ═══════════════════════════════════════════════════════════════
# memory_sync_files — 存量 .md 文件同步到 MCP 管道
# ═══════════════════════════════════════════════════════════════

def _parse_frontmatter(content: str) -> dict:
    """使用 yaml 标准库解析 frontmatter。失败时降级返回空 dict。"""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml
        result = yaml.safe_load(parts[1])
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


async def handle_memory_sync_files(engine: Any, args: dict) -> list[TextContent]:
    """同步文件系统 .md 记忆到 MCP 管道。"""
    source_dir = args.get("source_dir", "")
    dry_run = args.get("dry_run", False)

    if not source_dir or not os.path.isdir(source_dir):
        return [TextContent(type="text", text=json.dumps({
            "error": f"Invalid source_dir: {source_dir}",
            "synced": 0, "skipped": 0, "errors": 0
        }, ensure_ascii=False))]

    synced = 0
    skipped = 0
    errors = 0

    for fname in sorted(os.listdir(source_dir)):
        if fname == "MEMORY.md" or not fname.endswith(".md"):
            continue

        fpath = os.path.join(source_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()

        if "[[synced-to-mcp]]" in content or "[[memory-system-primary-channel]]" in content:
            skipped += 1
            continue

        fm = _parse_frontmatter(content)
        name = fm.get("name", fname.replace(".md", ""))
        metadata_fm = fm.get("metadata", {})
        mem_type = metadata_fm.get("type", "reference") if isinstance(metadata_fm, dict) else "reference"
        description = fm.get("description", "")

        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[-1].strip() if len(parts) >= 3 else content

        tags = [f"cat:{mem_type}", "source:file-sync", f"file:{fname}"]
        entity_id = f"memory:file:{name}"

        if dry_run:
            synced += 1
            continue

        try:
            result = await handle_memory_store(engine, {
                "content": f"[FILE SYNC] {name}: {description}\n\n{body}",
                "memory_type": "experience",
                "source": "file_sync",
                "entity_ids": [entity_id],
                "tags": tags,
            })
            data = json.loads(result[0].text)
            if data.get("stored"):
                synced += 1
                new_content = content.rstrip() + "\n\n[[synced-to-mcp]]\n"
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content)
            else:
                errors += 1
        except Exception:
            errors += 1

    return [TextContent(type="text", text=json.dumps({
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "source_dir": source_dir,
    }, ensure_ascii=False))]
