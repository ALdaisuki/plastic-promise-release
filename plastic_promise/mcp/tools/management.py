"""MCP Management tool handlers — 3 tools for system administration.

工具列表:
- system_stats   : 获取 Plastic Promise 系统整体统计
- system_backup  : 导出 Plastic Promise 完整状态 (stub)
- system_migrate : 从其他记忆系统迁移数据到 Plastic Promise (stub)
"""

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
        mem_stats = (
            json.loads(mem_stats_str) if isinstance(mem_stats_str, str) else {}
        )

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

        return [TextContent(type="text", text=json.dumps({
            "memory": mem_stats,
            "graph": graph_stats,
            "digital_body_systems": systems,
            "engine_version": "0.1.0",
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "system_stats"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# system_backup (stub)
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
        return [TextContent(type="text", text=json.dumps({
            "tool": "system_backup",
            "status": "not_implemented",
            "message": "System backup/export is not yet wired.",
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "system_backup"}, ensure_ascii=False))]


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
        return [TextContent(type="text", text=json.dumps({
            "tool": "system_migrate",
            "status": "not_implemented",
            "message": "System migration is not yet wired.",
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "system_migrate"}, ensure_ascii=False))]
