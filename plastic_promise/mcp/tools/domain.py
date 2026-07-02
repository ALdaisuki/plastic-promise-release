"""Domain MCP 工具 — 域联邦统一入口

公开工具:
- domain : 统一入口，action=stats|merge|unmerge|rename|rebuild

内部处理器 (保留供直接调用):
- handle_domain_stats   : 查看所有域的统计信息
- handle_domain_merge   : 手动合并两个域
- handle_domain_unmerge : 手动解除合并
- handle_domain_rename  : 重命名域
"""

import json
import os
from typing import Any
from mcp.types import TextContent


async def handle_domain_stats(engine: Any, args: dict) -> list[TextContent]:
    """查看所有域统计: 标签数、记忆数、原则数、得分、谱系、最后活跃时间。"""
    try:
        dm = getattr(engine, "_dm", None)
        if dm is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "DomainManager not initialized"}, ensure_ascii=False),
                )
            ]
        return [TextContent(type="text", text=json.dumps(dm.stats(), ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "domain_stats"}, ensure_ascii=False),
            )
        ]


async def handle_domain_merge(engine: Any, args: dict) -> list[TextContent]:
    """手动合并两个域（覆盖自动阈值）。"""
    try:
        dm = getattr(engine, "_dm", None)
        if dm is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "DomainManager not initialized"}, ensure_ascii=False),
                )
            ]
        source = args.get("source", "")
        target = args.get("target", "")
        if not source or not target:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "source and target required"}, ensure_ascii=False),
                )
            ]
        ok = dm.merge(source, target)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"merged": ok, "source": source, "target": target}, ensure_ascii=False
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "domain_merge"}, ensure_ascii=False),
            )
        ]


async def handle_domain_unmerge(engine: Any, args: dict) -> list[TextContent]:
    """从 merged_from 谱系恢复被合并的域。"""
    try:
        dm = getattr(engine, "_dm", None)
        if dm is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "DomainManager not initialized"}, ensure_ascii=False),
                )
            ]
        source = args.get("source", "")
        if not source:
            return [
                TextContent(
                    type="text", text=json.dumps({"error": "source required"}, ensure_ascii=False)
                )
            ]
        ok = dm.unmerge(source)
        return [
            TextContent(
                type="text", text=json.dumps({"unmerged": ok, "source": source}, ensure_ascii=False)
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "domain_unmerge"}, ensure_ascii=False),
            )
        ]


async def handle_domain_rename(engine: Any, args: dict) -> list[TextContent]:
    """重命名域，自动更新记忆和原则的 domain 字段。旧名保留为别名 30 天。"""
    try:
        dm = getattr(engine, "_dm", None)
        if dm is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "DomainManager not initialized"}, ensure_ascii=False),
                )
            ]
        old_name = args.get("old_name", "")
        new_name = args.get("new_name", "")
        if not old_name or not new_name:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "old_name and new_name required"}, ensure_ascii=False
                    ),
                )
            ]
        ok = dm.rename(old_name, new_name)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "renamed": ok,
                        "old_name": old_name,
                        "new_name": new_name,
                        "note": f"旧名 '{old_name}' 保留为别名 30 天",
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "domain_rename"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# domain — 统一入口 (replaces domain_stats/merge/unmerge/rename as MCP tools)
# ---------------------------------------------------------------------------


def _get_agent_id(engine: Any) -> str:
    """从 engine 或环境变量提取当前 Agent 标识。空串 = 单 Agent 模式。"""
    return getattr(engine, "_agent_owner", "") or os.environ.get("AGENT_OWNER", "")


async def handle_domain(engine: Any, args: dict) -> list[TextContent]:
    """域联邦统一入口。action: stats|merge|unmerge|rename|rebuild|reset_throttle"""
    action = args.get("action", "stats")
    engine.ensure_heavy_init()  # ensure DomainManager is initialized before access
    dm = getattr(engine, "_dm", None)
    if dm is None:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "DomainManager not available (_dm_ok=False)"}, ensure_ascii=False
                ),
            )
        ]

    agent_id = _get_agent_id(engine)

    try:
        if action == "stats":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(dm.stats(agent_id=agent_id), ensure_ascii=False, indent=2),
                )
            ]
        elif action == "merge":
            ok = dm.merge(args["source"], args["target"], agent_id=agent_id)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"merged": ok, "source": args["source"], "target": args["target"]},
                        ensure_ascii=False,
                    ),
                )
            ]
        elif action == "unmerge":
            ok = dm.unmerge(args["source"], agent_id=agent_id)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"unmerged": ok, "source": args["source"]}, ensure_ascii=False),
                )
            ]
        elif action == "rename":
            ok = dm.rename(args["old_name"], args["new_name"], agent_id=agent_id)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"renamed": ok, "old_name": args["old_name"], "new_name": args["new_name"]},
                        ensure_ascii=False,
                    ),
                )
            ]
        elif action == "rebuild":
            result = dm.rebuild_from_memories(agent_id=agent_id)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif action == "reset_throttle":
            scanner = args.get("scanner", "")
            if not scanner:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": "scanner parameter required for reset_throttle"},
                            ensure_ascii=False,
                        ),
                    )
                ]
            try:
                from daemons.maintenance_daemon import _scanner_throttles

                throttle = _scanner_throttles.get(scanner)
                if throttle is None:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": f"unknown scanner: {scanner}. "
                                    f"Known: {list(_scanner_throttles.keys())}"
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]
                old_interval = throttle.current
                throttle.current = throttle.base
                throttle.empty_streak = 0
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "reset_throttle": scanner,
                                "old_interval": old_interval,
                                "new_interval": throttle.base,
                                "status": "ok",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            except ImportError:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": "daemon module not importable — is the daemon running?"},
                            ensure_ascii=False,
                        ),
                    )
                ]
        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"unknown action: {action}"}, ensure_ascii=False),
                )
            ]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]
