"""域 2: Memory Operations skills — 记忆 CRUD 的高层组合"""

import json

from plastic_promise.core.constants import DEDUP_SIMILARITY_THRESHOLD
from plastic_promise.mcp.tools.memory import handle_memory_update
from plastic_promise.skills.engine import SkillDef, SkillResult


async def _smart_remember_handler(ctx, params, atom_results):
    """smart-remember handler: dedup check -> store or update.

    Atoms called before this handler:
    - principle_activate: {activated: [...]}
    - memory_recall: {core: [{id, content, relevance}]}
    - memory_store (if no dupe) OR memory_update (if dupe found)

    Dedup logic:
    - If memory_recall returns any core result with relevance >= DEDUP_SIMILARITY_THRESHOLD
      -> treat as duplicate -> update instead of store
    - Otherwise -> store new memory
    """

    def parse(result):
        if result and hasattr(result[0], "text"):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    recall_data = parse(atom_results.get("memory_recall"))
    core_results = recall_data.get("core", [])

    # Check for duplicates: any core result with relevance >= DEDUP_SIMILARITY_THRESHOLD (0.85)
    duplicate = None
    for item in core_results:
        if item.get("relevance", 0) >= DEDUP_SIMILARITY_THRESHOLD:
            duplicate = item
            break

    if duplicate:
        # Update existing — call handle_memory_update directly (not as an engine atom)
        try:
            update_result = await handle_memory_update(
                ctx,
                {
                    "memory_id": duplicate["id"],
                    "content": params.get("content", ""),
                },
            )
            update_data = parse(update_result)
            memory_id = update_data.get("memory_id", duplicate.get("id", "?"))
        except Exception as e:
            return SkillResult(
                skill_name="smart-remember",
                success=False,
                data={"action": "update_failed", "duplicate_of": duplicate.get("id")},
                atom_results={},
                degrade_log=[f"handle_memory_update: {e}"],
                audit_trail={},
                errors=[f"memory_update failed: {e}"],
            )
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "updated",
                "memory_id": memory_id,
                "duplicate_of": duplicate.get("id"),
                "relevance": duplicate.get("relevance"),
            },
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )
    else:
        # Store new
        store_data = parse(atom_results.get("memory_store"))
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "stored",
                "memory_id": store_data.get("memory_id", "?"),
                "pipeline": store_data.get("pipeline", {}),
            },
            atom_results={},
            degrade_log=[],
            audit_trail={},
            errors=[],
        )


# -- Skill Definition --

skill_smart_remember = SkillDef(
    name="smart-remember",
    domain="memory_operations",
    description="记忆前自动去重 + 质量门控 — 重复的记忆更新而非新增",
    tier="P0",
    atoms=[
        "principle_activate",
        "memory_recall",
        "memory_store",
    ],
    degrade_map={
        "principle_activate": "skip",
        "memory_recall": "fallback:memory_store",  # if recall fails, store anyway (no dedup)
        "memory_store": "abort",  # data integrity: abort on store failure
    },
    handler=_smart_remember_handler,
    allowed_callers=["claude", "pi"],
)
