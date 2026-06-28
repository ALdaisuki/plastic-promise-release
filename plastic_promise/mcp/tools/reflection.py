"""MCP Reflection tool handlers — 3 tools for introspection and evolution.

工具列表:
- scarf_reflect   : 执行 SCARF 五维度自省 (stub)
- inertia_check   : 惯性抑制检测 — 检查任务是否过于相似 (stub)
- feedback_apply  : 手动应用反馈到记忆或上下文条目
"""

import json
from typing import Any

from mcp.types import TextContent


# ---------------------------------------------------------------------------
# scarf_reflect (stub)
# ---------------------------------------------------------------------------

async def handle_scarf_reflect(engine: Any, args: dict) -> list[TextContent]:
    """Execute SCARF five-dimension self-reflection (stub).

    Args:
        engine: ContextEngine instance.
        args: {"context": str, "dimensions"?: list[str]}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        from plastic_promise.reflection.soul_scarf import SCARFReflector
        context = args.get("context", "")
        dimensions = args.get("dimensions")
        reflector = SCARFReflector()
        result = reflector.reflect(context)
        if dimensions:
            result = {d: result.get(d) for d in dimensions if d in result}
        return [TextContent(type="text", text=json.dumps({
            "tool": "scarf_reflect",
            "reflection": result,
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "scarf_reflect"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# inertia_check (stub)
# ---------------------------------------------------------------------------

async def handle_inertia_check(engine: Any, args: dict) -> list[TextContent]:
    """Inertia suppression detection: check if recent tasks are too similar (stub).

    Args:
        engine: ContextEngine instance.
        args: {"recent_tasks"?: list[str]}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        from plastic_promise.reflection.soul_proprioception import ProprioceptionManager
        recent_tasks = args.get("recent_tasks", [])
        pm = ProprioceptionManager()
        for task in recent_tasks:
            pm.record_task(task)
        result = pm.check_inertia()
        return [TextContent(type="text", text=json.dumps({
            "tool": "inertia_check",
            "inertia": result,
        }, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "inertia_check"}, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# feedback_apply
# ---------------------------------------------------------------------------

async def handle_feedback_apply(engine: Any, args: dict) -> list[TextContent]:
    """Apply feedback to a memory, updating its worth counters.

    Manually applies feedback to a memory or context item (adopted / ignored /
    rejected). Updates the worth counter and self-evolution weights, then
    persists the updated record back to the engine's storage backend.

    Args:
        engine: ContextEngine instance (must provide get_memory + store_memory).
        args: {"item_id": str, "feedback_type": str, "task_context"?: str}.

    Returns:
        list[TextContent]: MCP response with updated worth score and observation count.
    """
    try:
        item_id: str = args["item_id"]
        feedback_type: str = args["feedback_type"]  # adopted / ignored / rejected

        # Get the memory record
        record = engine.get_memory(item_id)
        if record is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Memory {item_id} not found"}, ensure_ascii=False))]

        # Apply feedback
        if feedback_type == "adopted":
            record.record_adopted()
        elif feedback_type == "rejected":
            record.record_rejected()
        elif feedback_type == "ignored":
            record.record_ignored()

        # Persist updated record
        engine.store_memory(record)

        return [TextContent(type="text", text=json.dumps({
            "updated": True,
            "item_id": item_id,
            "feedback_type": feedback_type,
            "new_worth_score": record.worth_score(),
            "observations": record.total_observations,
        }, ensure_ascii=False))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "feedback_apply"}, ensure_ascii=False))]
