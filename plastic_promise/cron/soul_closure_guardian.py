"""Closure guardian — detect unclosed tasks and alert.

Runs every 60 minutes. Queries SQLite for working-tier memories
that haven't been accessed in 24+ hours.
"""

import datetime
from typing import Any


def run(engine: Any = None) -> dict:
    """Check for unclosed tasks.

    Args:
        engine: ContextEngine instance (optional).

    Returns:
        dict with {stale_count, stale_ids, alert, timestamp}.
    """
    now = datetime.datetime.now().isoformat()
    stale_count = 0
    stale_ids: list[str] = []

    if engine is not None:
        try:
            all_mems = engine.list_memories(
                memory_type=None, source=None, min_worth=None, limit=1000
            )
            cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
            for mem in all_mems:
                if mem.tier == "working" and mem.last_accessed_at:
                    try:
                        last = datetime.datetime.fromisoformat(mem.last_accessed_at)
                        if last < cutoff:
                            stale_count += 1
                            stale_ids.append(mem.id)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    return {
        "timestamp": now,
        "stale_count": stale_count,
        "stale_ids": stale_ids[:20],
        "alert": stale_count > 5,
        "action": "manual_review_needed" if stale_count > 5 else "ok",
    }
