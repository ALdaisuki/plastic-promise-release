"""Subscription matching -- determine which agents should be notified.

Queries the task_subscriptions table (defined in task_queue_schema.py)
and filters by agent_name, task_type GLOB pattern, priority minimum,
and keyword presence in title/description.
"""

import fnmatch
import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def match_subscribers(task: dict) -> list[str]:
    """Find subscribers matching a task. Returns list of agent names.

    Matching rules (all must pass):
      1. subscription.agent_name == task.to_agent
      2. task_type matches task_type_filter (fnmatch GLOB) if filter is set
      3. task.priority <= subscription.priority_min (lower number = higher priority)
      4. ALL keywords present in title+description if keywords are set

    Args:
        task: dict with keys task_type, to_agent, priority, title, description

    Returns:
        List of matching agent_name strings.
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if not os.path.exists(db_path):
        logger.debug("Database not found at %s, no subscribers", db_path)
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM task_subscriptions WHERE enabled = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist yet -- schema not initialized
        conn.close()
        return []

    matched: list[str] = []
    for sub in rows:
        # agent match: subscription targets the task's destination
        if sub["agent_name"] != task.get("to_agent", ""):
            continue

        # task_type filter (GLOB)
        if sub["task_type_filter"]:
            task_type = task.get("task_type", "")
            if not fnmatch.fnmatch(task_type, sub["task_type_filter"]):
                continue

        # priority filter: task must be at least as important as min
        task_priority = task.get("priority", 4)
        if task_priority > sub["priority_min"]:
            continue

        # keyword match: all keywords must appear in title+description
        if sub["keywords"]:
            try:
                keywords = json.loads(sub["keywords"])
            except (json.JSONDecodeError, TypeError):
                keywords = []
            if keywords:
                title = task.get("title", "")
                desc = task.get("description", "")
                text = f"{title} {desc}".lower()
                if not any(kw.lower() in text for kw in keywords):
                    continue

        matched.append(sub["agent_name"])

    conn.close()
    return matched
