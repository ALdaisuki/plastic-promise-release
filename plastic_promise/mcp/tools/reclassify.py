"""MCP tool: memory_reclassify — bulk re-run classification pipeline on existing memories"""

import json
from datetime import datetime, timezone
from typing import Any
from mcp.types import TextContent


async def handle_memory_reclassify(engine: Any, args: dict) -> list[TextContent]:
    """Force existing memories through the classification pipeline (tier + domain + category).

    Iterates engine._memories, extracting content/entity_ids/tags/source,
    re-processes through MemoryPipeline, preserving worth history in new memory metadata.

    Args:
        engine: ContextEngine instance
        args:
            batch_size: int — entries per run (default 50)
            resume_from: str | None — continue from this memory_id (checkpoint resume)

    Returns:
        list[TextContent]: reclassified, remaining, skipped, errors counts
    """
    from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

    batch_size = args.get("batch_size", 50)
    resume_from = args.get("resume_from", None)

    fb = _get_fuzzy_buffer(engine)
    now = datetime.now(timezone.utc).isoformat()

    reclassified = 0
    skipped = 0
    errors = 0
    remaining = 0

    # Collect pending memories (exclude already marked replaced)
    pending = []
    for mid, mem in engine._memories.items():
        if not isinstance(mem, dict):
            continue
        tags = mem.get("tags", [])
        if "status:replaced" in tags:
            skipped += 1
            continue
        # resume_from: skip already-processed
        if resume_from and mid <= resume_from:
            skipped += 1
            continue
        pending.append((mid, dict(mem)))  # shallow copy

    remaining = max(0, len(pending) - batch_size)
    batch = pending[:batch_size]

    for mid, mem in batch:
        try:
            content = mem.get("content", "")
            if not content.strip():
                skipped += 1
                continue

            old_tags = list(mem.get("tags", []))
            old_eids = list(mem.get("entity_ids", []))
            old_source = mem.get("source", "user")
            old_worth_s = mem.get("worth_success", 0)
            old_worth_f = mem.get("worth_failure", 0)
            old_access = mem.get("access_count", 0)

            # Re-classify through pipeline
            fb.store_urgent(
                content=content,
                memory_type=mem.get("memory_type", "experience"),
                source=old_source,
                entity_ids=old_eids,
                custom_tags=old_tags,
                domain_hint=None,  # let DomainManager reassign
            )
            fb.process_pipeline()

            # Find the newly created memory (most recent in engine._memories not in batch)
            new_mid = None
            for check_mid in engine._memories:
                if (check_mid not in dict(batch)  # not in current batch
                        and "status:replaced" not in engine._memories[check_mid].get("tags", [])
                        and content[:50] in engine._memories[check_mid].get("content", "")):
                    new_mid = check_mid
                    break

            # Preserve worth history on the new memory
            if new_mid and new_mid in engine._memories:
                new_mem = engine._memories[new_mid]
                if "metadata" not in new_mem or not isinstance(new_mem.get("metadata"), dict):
                    new_mem["metadata"] = {}
                new_mem["metadata"]["worth_history"] = {
                    "previous": {"success": old_worth_s, "failure": old_worth_f},
                    "previous_access_count": old_access,
                    "reclassified_at": now,
                }

            # Mark old memory as replaced
            engine._memories[mid]["metadata"] = engine._memories[mid].get("metadata", {})
            if not isinstance(engine._memories[mid]["metadata"], dict):
                engine._memories[mid]["metadata"] = {}
            engine._memories[mid]["metadata"]["replaced_by"] = new_mid
            old_tags_replaced = list(engine._memories[mid].get("tags", []))
            if "status:replaced" not in old_tags_replaced:
                old_tags_replaced.append("status:replaced")
            engine._memories[mid]["tags"] = old_tags_replaced

            # SQLite sync
            sqlite = getattr(engine, '_sqlite', None)
            if sqlite is not None:
                try:
                    sqlite._conn.execute(
                        "UPDATE memories SET tags = ?, metadata = ? WHERE id = ?",
                        (json.dumps(old_tags_replaced),
                         json.dumps(engine._memories[mid]["metadata"]), mid)
                    )
                    if new_mid:
                        import json as _json
                        sqlite._conn.execute(
                            "UPDATE memories SET metadata = ? WHERE id = ?",
                            (_json.dumps(new_mem["metadata"]), new_mid)
                        )
                    sqlite._conn.commit()
                except Exception:
                    pass

            reclassified += 1
        except Exception:
            errors += 1

    return [TextContent(type="text", text=json.dumps({
        "reclassified": reclassified,
        "remaining": remaining,
        "skipped": skipped,
        "errors": errors,
        "batch_size": batch_size,
        "last_id": batch[-1][0] if batch else None,
        "total": len(engine._memories),
    }, ensure_ascii=False))]
