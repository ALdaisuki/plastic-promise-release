"""Principle domain MCP tool handlers — 2 tools for principle activation and evaluation."""

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

        # domain_hint filter — optionally narrow to a behavior domain
        # All-domain principles are always included regardless of hint.
        domain_hint = args.get("domain_hint")
        if domain_hint and domain_hint != "all":
            principles = [p for p in principles if p["domain"] in (domain_hint, "all")]

        # consequences and recommendations now come from CORE_PRINCIPLES fields
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "task_type": task_type,
                        "activated": [
                            {
                                "id": p["id"],
                                "name": p["name"],
                                "content": p["content"],
                                "consequence": p.get("consequence", ""),
                                "recommendation": p.get("recommendation", ""),
                                "domain": p["domain"],
                            }
                            for p in principles
                        ],
                        "count": len(principles),
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
                text=json.dumps(
                    {"error": str(e), "tool": "principle_activate"}, ensure_ascii=False
                ),
            )
        ]


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

        principle = next((p for p in CORE_PRINCIPLES if p["id"] == principle_id), None)
        if not principle:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Principle {principle_id} not found"}, ensure_ascii=False
                    ),
                )
            ]

        # Counterfactual: what happens if violated — reads from CORE_PRINCIPLES
        consequence = principle.get("consequence", "未知后果")

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "principle_id": principle_id,
                        "name": principle["name"],
                        "content": principle["content"],
                        "scenario": scenario,
                        "violation_consequence": consequence,
                        "recommendation": (
                            f"保持对原则 {principle_id} 的遵守，避免: {consequence}"
                        ),
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
                text=json.dumps(
                    {"error": str(e), "tool": "principle_evaluate"}, ensure_ascii=False
                ),
            )
        ]
