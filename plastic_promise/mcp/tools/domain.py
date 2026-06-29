"""Domain MCP 工具 — 域联邦管理 4 个工具

工具列表:
- domain_stats   : 查看所有域的统计信息
- domain_merge   : 手动合并两个域
- domain_unmerge : 手动解除合并
- domain_rename  : 重命名域
"""

import json
from typing import Any
from mcp.types import TextContent


async def handle_domain_stats(engine: Any, args: dict) -> list[TextContent]:
    """查看所有域统计: 标签数、记忆数、原则数、得分、谱系、最后活跃时间。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        return [TextContent(type="text", text=json.dumps(
            dm.stats(), ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_stats"}, ensure_ascii=False))]


async def handle_domain_merge(engine: Any, args: dict) -> list[TextContent]:
    """手动合并两个域（覆盖自动阈值）。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        source = args.get("source", "")
        target = args.get("target", "")
        if not source or not target:
            return [TextContent(type="text", text=json.dumps(
                {"error": "source and target required"}, ensure_ascii=False))]
        ok = dm.merge(source, target)
        return [TextContent(type="text", text=json.dumps(
            {"merged": ok, "source": source, "target": target}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_merge"}, ensure_ascii=False))]


async def handle_domain_unmerge(engine: Any, args: dict) -> list[TextContent]:
    """从 merged_from 谱系恢复被合并的域。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        source = args.get("source", "")
        if not source:
            return [TextContent(type="text", text=json.dumps(
                {"error": "source required"}, ensure_ascii=False))]
        ok = dm.unmerge(source)
        return [TextContent(type="text", text=json.dumps(
            {"unmerged": ok, "source": source}, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_unmerge"}, ensure_ascii=False))]


async def handle_domain_rename(engine: Any, args: dict) -> list[TextContent]:
    """重命名域，自动更新记忆和原则的 domain 字段。旧名保留为别名 30 天。"""
    try:
        dm = getattr(engine, '_dm', None)
        if dm is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": "DomainManager not initialized"}, ensure_ascii=False))]
        old_name = args.get("old_name", "")
        new_name = args.get("new_name", "")
        if not old_name or not new_name:
            return [TextContent(type="text", text=json.dumps(
                {"error": "old_name and new_name required"}, ensure_ascii=False))]
        ok = dm.rename(old_name, new_name)
        return [TextContent(type="text", text=json.dumps(
            {"renamed": ok, "old_name": old_name, "new_name": new_name,
             "note": f"旧名 '{old_name}' 保留为别名 30 天"},
            ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "domain_rename"}, ensure_ascii=False))]
