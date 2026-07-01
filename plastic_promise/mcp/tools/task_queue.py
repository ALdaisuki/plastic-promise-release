"""MCP Task Queue tools — Hunter Guild dispatch board.

Tools: task_enqueue, task_claim, task_complete, task_verify,
       task_inbox, task_heartbeat, task_abandon
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.hunter_rank import trust_to_rank, can_claim
from plastic_promise.core.constants import RANK_ORDER


def _get_db_path() -> str:
    return os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")


def _generate_task_id() -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"t_{datetime.now().strftime('%Y%m%d%H%M%S')}_{suffix}"


def _get_conn():
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════════════════════
# task_enqueue
# ═══════════════════════════════════════════════════════════════

async def handle_task_enqueue(engine: Any, args: dict) -> list[TextContent]:
    """Enqueue a task onto the guild board.

    Validates the submitter's trust score and enforces rank-based
    submission rules.
    """
    from_agent = args.get("from_agent", "daemon")
    from_trust_score = args.get("from_trust_score", None)
    priority = args.get("priority", 3)
    max_escalations = args.get("max_escalations", 3)

    # ── Submitter validation ──────────────────────────────
    if from_agent not in ("daemon", "claude") and from_trust_score is not None:
        rank = trust_to_rank(from_trust_score)
        if rank["rank"] == "D":
            return [TextContent(type="text", text=json.dumps({
                "status": "rejected",
                "reason": f"降级猎人（{rank['title']}）无权挂委托，信任分={from_trust_score:.2f}",
            }, ensure_ascii=False))]
        if rank["rank"] == "C" and priority <= 2:
            # Needs Claude review
            task_id = _generate_task_id()
            conn = _get_conn()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, domain, memory_id, principle_id, "
                "source_scan, parent_task_id, timeout_seconds, max_escalations, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, args["task_type"], args["title"], args["to_agent"],
                    priority, from_agent,
                    args.get("description", ""), args.get("domain"),
                    args.get("memory_id"), args.get("principle_id"),
                    args.get("source_scan"), args.get("parent_task_id"),
                    args.get("timeout_seconds", 300), max_escalations,
                    json.dumps(args.get("payload")) if args.get("payload") else None,
                ))
            conn.commit()
            conn.close()
            # Auto-notify Claude by creating a review sub-task
            review_task_id = _generate_task_id()
            conn2 = _get_conn()
            conn2.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, 'notify_review', ?, 'claude', 2, 'system', 'pending', ?, ?, ?)",
                (review_task_id,
                 f"[审批] {args['title']}",
                 f"C级猎人 {from_agent}（{rank['title']}）挂委托需审批。"
                 f"原始委托: {task_id}",
                 task_id,
                 json.dumps({"original_task_id": task_id, "submitter": from_agent,
                             "submitter_rank": rank["rank"]}),
                ))
            conn2.commit()
            conn2.close()
            return [TextContent(type="text", text=json.dumps({
                "task_id": task_id,
                "status": "pending_review",
                "sse_broadcast": False,
                "matched_subscribers": 1,
                "review_required": True,
                "review_task_id": review_task_id,
                "reason": f"C级猎人（{rank['title']}）挂A/B级委托需Claude审批",
            }, ensure_ascii=False))]

    # ── Normal enqueue ─────────────────────────────────────
    task_id = _generate_task_id()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
        "from_agent, status, description, domain, memory_id, principle_id, "
        "source_scan, parent_task_id, timeout_seconds, max_escalations, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, args["task_type"], args["title"], args["to_agent"],
            priority, from_agent,
            args.get("description", ""), args.get("domain"),
            args.get("memory_id"), args.get("principle_id"),
            args.get("source_scan"), args.get("parent_task_id"),
            args.get("timeout_seconds", 300), max_escalations,
            json.dumps(args.get("payload")) if args.get("payload") else None,
        ))
    conn.commit()

    # Use match_subscribers() for accurate counting (keywords respected)
    try:
        from plastic_promise.core.task_subscriptions import match_subscribers
        matched = len(match_subscribers({
            "task_type": args["task_type"],
            "to_agent": args["to_agent"],
            "priority": priority,
            "title": args["title"],
            "description": args.get("description", ""),
        }))
    except ImportError:
        matched = 0  # Phase 3 not yet implemented
    conn.close()

    return [TextContent(type="text", text=json.dumps({
        "task_id": task_id,
        "status": "pending",
        "sse_broadcast": False,  # Phase 3
        "matched_subscribers": matched,
        "review_required": False,
    }, ensure_ascii=False))]


# ═══════════════════════════════════════════════════════════════
# task_claim
# ═══════════════════════════════════════════════════════════════

async def handle_task_claim(engine: Any, args: dict) -> list[TextContent]:
    """Claim a task from the guild board. Atomic — first-come-first-served."""
    agent_name = args["agent_name"]
    task_id = args["task_id"]
    trust_score = args["trust_score"]
    force = args.get("force", False)

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Read task
    task = conn.execute(
        "SELECT * FROM task_queue WHERE id = ?", (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在"
        }, ensure_ascii=False))]

    if task["status"] != "pending":
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": f"委托已被揭榜 (status={task['status']})"
        }, ensure_ascii=False))]

    # Rank check
    ok, msg = can_claim(trust_score, task["priority"])
    if not ok and not force:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "等级不足",
            "rank": rank_info, "task_priority": task["priority"], "match": msg,
        }, ensure_ascii=False))]

    if not ok and force:
        msg = f"⚠️ 越级揭榜(已记录): {msg}"

    # Atomic claim
    now = datetime.now().isoformat()
    result = conn.execute(
        "UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=?, "
        "heartbeat_at=?, updated_at=? WHERE id=? AND status='pending'",
        (agent_name, now, now, now, task_id)
    )
    conn.commit()

    if result.rowcount == 0:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "揭榜失败: 委托已被其他猎人抢先揭榜"
        }, ensure_ascii=False))]

    conn.close()
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "rank": rank_info,
        "task_priority": task["priority"],
        "match": msg,
        "force_claimed": force and not ok,
    }, ensure_ascii=False))]
