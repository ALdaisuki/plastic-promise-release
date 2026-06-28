"""Principle domain MCP tool handlers — 4 tools for principle activation and inheritance.

工具列表:
- handle_principle_activate : 根据任务类型自动激活相关核心原则
- handle_principle_inherit  : 触发原则单向扩散 (work→all / life→all)
- handle_principle_diffuse  : 查询原则在域间的传播状态
- handle_principle_evaluate : 反事实评估 — 「如果违反会怎样」的预演
"""

import json
from typing import Any

from mcp.types import TextContent


async def handle_principle_activate(engine: Any, args: dict) -> list[TextContent]:
    """Activate core principles based on task type + keyword matching.

    Auto-activates relevant core principles based on task type, with optional
    keyword matching from the task description for additional coverage.
    Returns the list of activated principles with their metadata.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {"task_type": str, "task_description"?: str,
               "max_principles"?: int}.

    Returns:
        list[TextContent]: MCP response with activated principles array.
    """
    try:
        from plastic_promise.core.constants import CORE_PRINCIPLES

        task_type = args["task_type"]
        task_description = args.get("task_description", "")

        # Task type -> principle ID mapping
        recommendations: dict[str, list[int]] = {
            "code_generation": [1, 3, 8, 10],
            "code_review": [1, 5, 6, 9],
            "debugging": [1, 5, 10],
            "architecture": [2, 7, 8],
            "refactoring": [5, 6, 7],
            "learning": [1, 10, 11],
            "collaboration": [2, 7, 9],
            "general": [1, 2, 3, 4],
        }
        ids: list[int] = recommendations.get(task_type, [1, 2, 3, 4])

        # Keyword matching: add extra principles when description keywords hit
        for p in CORE_PRINCIPLES:
            if p["id"] not in ids:
                for kw in p.get("keywords", []):
                    if kw in task_description:
                        ids.append(p["id"])
                        break

        max_p = args.get("max_principles", 5)
        ids = list(dict.fromkeys(ids))[:max_p]  # deduplicate, limit
        principles = [p for p in CORE_PRINCIPLES if p["id"] in ids]

        return [TextContent(type="text", text=json.dumps({
            "task_type": task_type,
            "activated": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "content": p["content"],
                    "domain": p["domain"],
                }
                for p in principles
            ],
            "count": len(principles),
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "principle_activate"}, ensure_ascii=False))]


async def handle_principle_inherit(engine: Any, args: dict) -> list[TextContent]:
    """Trigger principle diffusion: work->all or life->all with decay factor.

    Triggers one-way principle diffusion from a source domain to a target
    domain, applying the PRINCIPLE_INHERITANCE_DECAY factor (0.70) to
    weights during propagation.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {"source_domain": str, "target_domain"?: str,
               "principle_ids"?: list[int]}.

    Returns:
        list[TextContent]: MCP response with inherited principles and decay info.
    """
    try:
        from plastic_promise.core.constants import (
            CORE_PRINCIPLES,
            PRINCIPLE_INHERITANCE_DECAY,
        )

        source_domain = args["source_domain"]  # "work" or "life"
        target_domain = args.get("target_domain", "all")
        principle_ids: list[int] = args.get("principle_ids") or []  # None/empty = all in source

        # Filter principles by source domain
        source_principles = [
            p for p in CORE_PRINCIPLES
            if p["domain"] == source_domain
            and (not principle_ids or p["id"] in principle_ids)
        ]

        # Apply decay
        inherited = [
            {
                "id": p["id"],
                "name": p["name"],
                "source_domain": source_domain,
                "target_domain": target_domain,
                "original_weight": 1.0,
                "decayed_weight": round(PRINCIPLE_INHERITANCE_DECAY, 2),
            }
            for p in source_principles
        ]

        return [TextContent(type="text", text=json.dumps({
            "direction": f"{source_domain} -> {target_domain}",
            "decay_factor": PRINCIPLE_INHERITANCE_DECAY,
            "inherited_count": len(inherited),
            "principles": inherited,
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "principle_inherit"}, ensure_ascii=False))]


async def handle_principle_diffuse(engine: Any, args: dict) -> list[TextContent]:
    """Query principle propagation state across domains.

    Queries the propagation state of principles across domains: current
    activation domain, propagation path, and whether a principle can diffuse
    to other domains.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {"principle_id"?: int} — None returns all principles.

    Returns:
        list[TextContent]: MCP response mapping principle IDs to propagation state.
    """
    try:
        from plastic_promise.core.constants import CORE_PRINCIPLES

        principle_id = args.get("principle_id")  # None = all

        if principle_id is not None:
            found = next(
                (p for p in CORE_PRINCIPLES if p["id"] == principle_id), None
            )
            principles = [found] if found else []
        else:
            principles = CORE_PRINCIPLES

        result: dict[str, dict] = {}
        for p in principles:
            result[str(p["id"])] = {
                "name": p["name"],
                "active_domain": p["domain"],
                "propagation_path": (
                    f"{p['domain']} -> all" if p["domain"] != "all" else "all"
                ),
                "can_diffuse_to": ["all"] if p["domain"] != "all" else [],
            }

        return [TextContent(type="text", text=json.dumps(
            result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "principle_diffuse"}, ensure_ascii=False))]


async def handle_principle_evaluate(engine: Any, args: dict) -> list[TextContent]:
    """Counterfactual evaluation: what if this principle were violated?

    Performs a "what if violated" walkthrough for a specified principle,
    providing a non-coercive but sufficient decision basis for the Agent
    by describing the concrete consequences of violation.

    Args:
        engine: ContextEngine instance (unused in stateless implementation).
        args: {"principle_id": int, "scenario"?: str}.

    Returns:
        list[TextContent]: MCP response with violation consequence and recommendation.
    """
    try:
        from plastic_promise.core.constants import CORE_PRINCIPLES

        principle_id = args["principle_id"]
        scenario = args.get("scenario", "")

        principle = next(
            (p for p in CORE_PRINCIPLES if p["id"] == principle_id), None
        )
        if not principle:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Principle {principle_id} not found"}, ensure_ascii=False))]

        # Counterfactual: what happens if violated
        consequences: dict[int, str] = {
            1: "指标失真，系统健康度不可信，小问题积累成大故障",
            2: "Agent执行规则失去内在动机，行为退化为最小合规",
            3: "记忆系统退化为被动档案库，上下文供应枯竭",
            4: "原则形同虚设，Agent行为与核心约定脱节",
            5: "虚假安全感，机制存在但不产生实际效果",
            6: "系统间数据流断裂，各自为战",
            7: "单点故障扩散，一个模块崩溃引发连锁故障",
            8: "LLM失去感官输入，决策退化为纯粹的文本补全",
            9: "自主权错配：高分时过于冒险，低分时寸步难行",
            10: "反馈信号丢失，系统行为逐渐漂移偏离约定",
            11: "核心约定无法跨代传递，新Agent需从零训练",
        }

        consequence = consequences.get(principle_id, "未知后果")

        return [TextContent(type="text", text=json.dumps({
            "principle_id": principle_id,
            "name": principle["name"],
            "content": principle["content"],
            "scenario": scenario,
            "violation_consequence": consequence,
            "recommendation": (
                f"保持对原则 {principle_id} 的遵守，避免: {consequence}"
            ),
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "principle_evaluate"}, ensure_ascii=False))]
