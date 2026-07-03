"""MCP Management + Domain 工具 — 管理域"""

import json
from typing import Any

from mcp.types import TextContent

# ---------------------------------------------------------------------------
# system_stats
# ---------------------------------------------------------------------------


async def handle_system_stats(engine: Any, args: dict) -> list[TextContent]:
    """Aggregate system-wide statistics.

    Retrieves overall Plastic Promise system statistics: memory pool status,
    entity graph scale, and nine-system digital-body health snapshot.

    Args:
        engine: ContextEngine instance (must provide memory_stats_json + get_graph).
        args: {} (no arguments required).

    Returns:
        list[TextContent]: MCP response with memory, graph, and system stats.
    """
    try:
        from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS

        # Memory stats
        mem_stats_str = engine.memory_stats_json()
        mem_stats = json.loads(mem_stats_str) if isinstance(mem_stats_str, str) else {}

        # EntityGraph stats (handles both Rust object and Python GraphInfo/dict)
        graph = engine.get_graph()
        if isinstance(graph, dict):
            graph_stats = {
                "nodes": len(graph.get("nodes", {})),
                "edges": len(graph.get("edges", [])),
            }
        else:
            graph_stats = {
                "nodes": getattr(graph, "node_count", 0),
                "edges": getattr(graph, "edge_count", 0),
            }

        # Digital body system snapshot
        systems = {}
        for key, sys in DIGITAL_BODY_SYSTEMS.items():
            systems[key] = {
                "name": sys["name"],
                "maturity": sys["maturity"],
            }

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "memory": mem_stats,
                        "graph": graph_stats,
                        "digital_body_systems": systems,
                        "engine_version": "0.1.0",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "system_stats"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# system — 统一入口 (replaces system_stats/system_backup/system_migrate as MCP tools)
# ---------------------------------------------------------------------------


def _get_fuzzy_buffer(engine: Any):
    """Get or create FuzzyBuffer / MemoryPipeline attached to the engine."""
    fb = engine.get_fuzzy_buffer()
    if fb is None:
        from plastic_promise.core.embedder import get_embedder
        from plastic_promise.memory.pipeline import MemoryPipeline
        from plastic_promise.memory.soul_memory import MemoryTierManager, RecMem

        rec_mem = engine.get_rec_mem() or RecMem(engine)
        try:
            embedder = get_embedder()
        except Exception:
            from plastic_promise.core.embedder import FallbackEmbedder

            embedder = FallbackEmbedder()
        tier_mgr = MemoryTierManager(rec_mem)
        fb = MemoryPipeline(rec_mem=rec_mem, embedder=embedder, tier_manager=tier_mgr)
        engine.set_fuzzy_buffer(fb)
        engine.set_rec_mem(rec_mem)
    return fb


async def handle_system(engine: Any, args: dict) -> list[TextContent]:
    """系统工具统一入口。action: stats|backup|migrate"""
    try:
        engine.ensure_heavy_init()  # ensure DomainManager + embedder are initialized
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "system"}, ensure_ascii=False),
            )
        ]
    action = args.get("action", "stats")
    if action == "backup":
        return await handle_system_backup(engine, args)
    elif action == "migrate":
        return await handle_system_migrate(engine, args)
    else:
        # stats 模式: 合并 system_stats + fuzzy 积压计数
        result = await handle_system_stats(engine, args)
        # 追加 fuzzy buffer 积压信息
        try:
            fb = _get_fuzzy_buffer(engine)
            if fb:
                buf_stats = fb.stats()
                parsed = json.loads(result[0].text) if result else {}
                parsed["fuzzy_buffer"] = buf_stats
                result = [
                    TextContent(type="text", text=json.dumps(parsed, ensure_ascii=False, indent=2))
                ]
        except Exception:
            pass
        return result


# ---------------------------------------------------------------------------
# system_backup (stub) — internal, called by handle_system
# ---------------------------------------------------------------------------


async def handle_system_backup(engine: Any, args: dict) -> list[TextContent]:
    """Export complete Plastic Promise state (stub).

    Args:
        engine: ContextEngine instance.
        args: {"format"?: str, "include_audit_history"?: bool}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        fmt = args.get("format", "json")
        include_audit = args.get("include_audit_history", False)

        # Collect all memories
        memories = engine.list_memories(limit=10000) if engine else []
        mem_list = []
        for m in memories:
            mem_list.append(
                {
                    "id": m.id,
                    "content": m.content,
                    "memory_type": m.memory_type,
                    "source": m.source,
                    "tier": m.tier,
                    "created_at": m.created_at,
                }
            )

        # Collect graph
        graph = engine.get_graph() if engine else {}
        if hasattr(graph, "_nodes"):
            graph_data = {"nodes": dict(graph._nodes), "edges": list(graph._edges)}
        elif isinstance(graph, dict):
            graph_data = graph
        else:
            graph_data = {"nodes": {}, "edges": []}

        backup = {
            "version": "0.1.0",
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "memories": mem_list,
            "graph": graph_data,
            "memory_count": len(mem_list),
        }
        if include_audit:
            from plastic_promise.defense.soul_audit import SoulAuditor

            auditor = SoulAuditor()
            backup["audit_report"] = (
                auditor.get_report().to_dict()
                if hasattr(auditor.get_report(), "to_dict")
                else str(auditor.get_report())
            )

        return [TextContent(type="text", text=json.dumps(backup, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "system_backup"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# system_migrate (stub)
# ---------------------------------------------------------------------------


async def handle_system_migrate(engine: Any, args: dict) -> list[TextContent]:
    """Migrate data from another memory system into Plastic Promise (stub).

    Args:
        engine: ContextEngine instance.
        args: {"source_path": str, "source_type": str, "dry_run"?: bool}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        source_path = args.get("source_path", "")
        dry_run = args.get("dry_run", True)

        if not source_path:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "source_path is required"}, ensure_ascii=False),
                )
            ]

        import os

        if not os.path.exists(source_path):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Source file not found: {source_path}"}, ensure_ascii=False
                    ),
                )
            ]

        with open(source_path, encoding="utf-8") as f:
            source_data = json.load(f)

        memories = source_data.get("memories", [])
        imported = 0
        skipped = 0

        for mem in memories:
            if dry_run:
                imported += 1
                continue
            try:
                if isinstance(mem, dict):
                    engine.register_memory(
                        {
                            "id": mem.get("id", ""),
                            "content": mem.get("content", ""),
                            "memory_type": mem.get("memory_type", "experience"),
                            "source": mem.get("source", "migration"),
                            "tier": mem.get("tier", "L1"),
                        }
                    )
                    imported += 1
            except Exception:
                skipped += 1

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tool": "system_migrate",
                        "source_path": source_path,
                        "dry_run": dry_run,
                        "total_found": len(memories),
                        "imported": imported,
                        "skipped": skipped,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "system_migrate"}, ensure_ascii=False),
            )
        ]


# ---- issue_create ----
async def handle_issue_create(engine: Any, args: dict) -> list[TextContent]:
    """Create a new Issue with optional principle and dependency links."""
    try:
        im = engine.get_issue_manager()
        iid = im.create(
            title=args.get("title", "Untitled"),
            description=args.get("description", ""),
            principle_id=args.get("principle_id"),
            memory_ids=args.get("memory_ids", []),
            blocks=args.get("blocks", []),
            blocked_by=args.get("blocked_by", []),
            owner=args.get("owner", ""),
        )
        return [
            TextContent(
                type="text", text=json.dumps({"created": True, "issue_id": iid}, ensure_ascii=False)
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "issue_create"}, ensure_ascii=False),
            )
        ]


# ---- issue_transition ----
async def handle_issue_transition(engine: Any, args: dict) -> list[TextContent]:
    """Transition an Issue to a new state."""
    try:
        im = engine.get_issue_manager()
        result = im.transition(
            iid=args["issue_id"],
            new_state=args["state"],
            reason=args.get("reason", ""),
        )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "issue_transition"}, ensure_ascii=False),
            )
        ]


# ---- issue_list ----
async def handle_issue_list(engine: Any, args: dict) -> list[TextContent]:
    """List Issues, optionally filtered by state or owner."""
    try:
        im = engine.get_issue_manager()
        issues = im.list(
            state=args.get("state"),
            owner=args.get("owner"),
        )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "issues": issues,
                        "count": len(issues),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "issue_list"}, ensure_ascii=False),
            )
        ]


# ---- pack_export ----
async def handle_pack_export(engine: Any, args: dict) -> list[TextContent]:
    """Export memories as a shareable JSON experience pack (streaming gzip)."""
    try:
        from plastic_promise.core.pack_index import pack_export_streaming

        name = args["name"]
        path = args.get("path", f"{name}.json.gz")
        tags = args.get("tags")
        result = pack_export_streaming(name, path, engine, tags)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "pack_export"}, ensure_ascii=False),
            )
        ]


# ---- pack_import ----
async def handle_pack_import(engine: Any, args: dict) -> list[TextContent]:
    """Import a JSON experience pack into the memory pool with optional strategy."""
    try:
        from plastic_promise.core.pack_index import pack_import_with_strategy

        path = args["path"]
        strategy = args.get("strategy", "skip")
        owner = args.get("owner", "")
        result = pack_import_with_strategy(path, engine, strategy, owner)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "pack_import"}, ensure_ascii=False),
            )
        ]
