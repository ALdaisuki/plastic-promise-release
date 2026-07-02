"""MCP Task Queue tools — Hunter Guild dispatch board (Phase 1 complete: 7 tools).

Tools: task_enqueue, task_claim, task_complete, task_verify,
       task_inbox, task_heartbeat, task_abandon
"""

import hashlib
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


def _compute_payload_hash(payload: dict) -> str:
    """Compute a deterministic hash for dedup based on payload content.

    Uses SHA256 first 8 hex chars of problem + sorted search_hints.
    Returns empty string if payload is None or missing required fields.
    """
    if not payload:
        return ""
    problem = payload.get("problem", "") or payload.get("gap_signal", {}).get("problem", "")
    search_hint = payload.get("search_hint", [])
    if not problem:
        return ""
    seed = f"{problem}|{'|'.join(sorted(search_hint))}"
    return hashlib.sha256(seed.encode()).hexdigest()[:8]


def _inject_payload_hash(payload: dict) -> dict:
    """Inject payload_hash into payload dict for later dedup queries."""
    if not payload:
        return payload
    result = dict(payload)
    result["payload_hash"] = _compute_payload_hash(payload)
    return result


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
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "rejected",
                            "reason": f"降级猎人（{rank['title']}）无权挂委托，信任分={from_trust_score:.2f}",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
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
                    task_id,
                    args["task_type"],
                    args["title"],
                    args["to_agent"],
                    priority,
                    from_agent,
                    args.get("description", ""),
                    args.get("domain"),
                    args.get("memory_id"),
                    args.get("principle_id"),
                    args.get("source_scan"),
                    args.get("parent_task_id"),
                    args.get("timeout_seconds", 300),
                    max_escalations,
                    json.dumps(_inject_payload_hash(args.get("payload")))
                    if args.get("payload")
                    else None,
                ),
            )
            conn.commit()
            conn.close()
            # Auto-notify Claude by creating a review sub-task
            review_task_id = _generate_task_id()
            conn2 = _get_conn()
            conn2.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, 'notify_review', ?, 'claude', 2, 'system', 'pending', ?, ?, ?)",
                (
                    review_task_id,
                    f"[审批] {args['title']}",
                    f"C级猎人 {from_agent}（{rank['title']}）挂委托需审批。原始委托: {task_id}",
                    task_id,
                    json.dumps(
                        {
                            "original_task_id": task_id,
                            "submitter": from_agent,
                            "submitter_rank": rank["rank"],
                        }
                    ),
                ),
            )
            conn2.commit()
            conn2.close()
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "task_id": task_id,
                            "status": "pending_review",
                            "sse_broadcast": False,
                            "matched_subscribers": 1,
                            "review_required": True,
                            "review_task_id": review_task_id,
                            "reason": f"C级猎人（{rank['title']}）挂A/B级委托需Claude审批",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

    # ── Dedup check (research_exemplar / verify_exemplar) ───
    # For research-oriented task types, check if a pending task
    # with the same payload_hash already exists.
    if args["task_type"] in ("research_exemplar", "verify_exemplar"):
        payload = args.get("payload")
        if payload:
            phash = _compute_payload_hash(payload)
            if phash:
                dedup_conn = _get_conn()
                existing = dedup_conn.execute(
                    "SELECT id FROM task_queue "
                    "WHERE task_type = ? AND status = 'pending' "
                    "AND json_extract(payload, '$.payload_hash') = ? "
                    "LIMIT 1",
                    (args["task_type"], phash),
                ).fetchone()
                dedup_conn.close()
                if existing:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "duplicate",
                                    "existing_task_id": existing["id"],
                                    "reason": f"Pending {args['task_type']} for this problem already exists",
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]

    # ── Normal enqueue ─────────────────────────────────────
    task_id = _generate_task_id()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
        "from_agent, status, description, domain, memory_id, principle_id, "
        "source_scan, parent_task_id, timeout_seconds, max_escalations, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            args["task_type"],
            args["title"],
            args["to_agent"],
            priority,
            from_agent,
            args.get("description", ""),
            args.get("domain"),
            args.get("memory_id"),
            args.get("principle_id"),
            args.get("source_scan"),
            args.get("parent_task_id"),
            args.get("timeout_seconds", 300),
            max_escalations,
            json.dumps(_inject_payload_hash(args.get("payload"))) if args.get("payload") else None,
        ),
    )
    conn.commit()

    # Use match_subscribers() for accurate counting (keywords respected)
    try:
        from plastic_promise.core.task_subscriptions import match_subscribers

        matched = len(
            match_subscribers(
                {
                    "task_type": args["task_type"],
                    "to_agent": args["to_agent"],
                    "priority": priority,
                    "title": args["title"],
                    "description": args.get("description", ""),
                }
            )
        )
    except ImportError:
        matched = 0  # Phase 3 not yet implemented
    conn.close()

    # SSE broadcast — fire-and-forget, never blocks task creation
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        bus = get_event_bus()
        sse_notified = await bus.broadcast_task_event(
            "task:new",
            {
                "task_id": task_id,
                "task_type": args["task_type"],
                "priority": priority,
                "to_agent": args["to_agent"],
                "title": args["title"],
                "from_agent": from_agent,
                "description": args.get("description", ""),
            },
        )
    except Exception:
        pass

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "task_id": task_id,
                    "status": "pending",
                    "sse_broadcast": sse_notified > 0,
                    "sse_notified": sse_notified,
                    "matched_subscribers": matched,
                    "review_required": False,
                },
                ensure_ascii=False,
            ),
        )
    ]


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
    task = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "reason": "委托不存在"}, ensure_ascii=False),
            )
        ]

    if task["status"] != "pending":
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "reason": f"委托已被揭榜 (status={task['status']})"},
                    ensure_ascii=False,
                ),
            )
        ]

    # Rank check
    ok, msg = can_claim(trust_score, task["priority"])
    if not ok and not force:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "reason": "等级不足",
                        "rank": rank_info,
                        "task_priority": task["priority"],
                        "match": msg,
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    if not ok and force:
        msg = f"⚠️ 越级揭榜(已记录): {msg}"

    # Atomic claim
    now = datetime.now().isoformat()
    result = conn.execute(
        "UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=?, "
        "heartbeat_at=?, updated_at=? WHERE id=? AND status='pending'",
        (agent_name, now, now, now, task_id),
    )
    conn.commit()

    if result.rowcount == 0:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "reason": "揭榜失败: 委托已被其他猎人抢先揭榜"},
                    ensure_ascii=False,
                ),
            )
        ]

    conn.close()

    # SSE broadcast — notify submitter that task was claimed
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        sse_notified = await get_event_bus().broadcast_task_event(
            "task:claimed",
            {
                "task_id": task_id,
                "task_type": task["task_type"],
                "title": task["title"],
                "from_agent": agent_name,
                "to_agent": task["to_agent"],
                "priority": task["priority"],
                "claimed_by": agent_name,
            },
        )
    except Exception:
        pass

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "rank": rank_info,
                    "task_priority": task["priority"],
                    "match": msg,
                    "force_claimed": force and not ok,
                    "sse_notified": sse_notified,
                },
                ensure_ascii=False,
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# task_complete
# ═══════════════════════════════════════════════════════════════


async def handle_task_complete(engine: Any, args: dict) -> list[TextContent]:
    """Submit a completed task for verification."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]
    result_text = args["result"]
    artifacts = args.get("artifacts", [])

    conn = _get_conn()
    task = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "reason": "委托不存在"}, ensure_ascii=False),
            )
        ]

    if task["claimed_by"] != agent_name:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "reason": f"委托由 {task['claimed_by']} 揭榜，不是你"},
                    ensure_ascii=False,
                ),
            )
        ]

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE task_queue SET status='done', done_at=?, result=?, updated_at=? WHERE id=?",
        (now, result_text, now, task_id),
    )
    conn.commit()

    # Auto-create verification subtask for Claude (unless task is already for Claude)
    verify_task_id = None
    if task["to_agent"] != "claude":
        verify_task_id = _generate_task_id()
        conn.execute(
            "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
            "from_agent, status, description, parent_task_id, payload) "
            "VALUES (?, 'verify_task', ?, 'claude', ?, 'system', 'pending', ?, ?, ?)",
            (
                verify_task_id,
                f"验收委托: {task['title']}",
                task["priority"],
                f"猎人 {agent_name} 已完成委托 {task_id}，请验收。\n结果: {result_text[:500]}",
                task_id,
                json.dumps(
                    {
                        "original_task_id": task_id,
                        "original_agent": agent_name,
                        "original_result": result_text[:1000],
                        "artifacts": artifacts,
                    }
                ),
            ),
        )
        conn.commit()

    conn.close()

    # SSE broadcast — notify submitter that task is done
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        sse_notified = await get_event_bus().broadcast_task_event(
            "task:done",
            {
                "task_id": task_id,
                "task_type": task["task_type"],
                "title": task["title"],
                "from_agent": agent_name,
                "to_agent": task["to_agent"],
                "priority": task["priority"],
                "claimed_by": agent_name,
            },
        )
    except Exception:
        pass

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "sse_notified": sse_notified,
                    "status": "done",
                    "verification_task_id": verify_task_id,
                    "waiting_for": "verification by claude" if verify_task_id else "self-verified",
                },
                ensure_ascii=False,
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# task_verify
# ═══════════════════════════════════════════════════════════════


async def handle_task_verify(engine: Any, args: dict) -> list[TextContent]:
    """Verify a completed task (长老验收)."""
    task_id = args["task_id"]
    verdict = args["verdict"]  # "accepted" | "rejected" | "reassigned"
    verified_by = args.get("verified_by", "claude")
    comment = args.get("comment", "")

    conn = _get_conn()
    task = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "reason": "委托不存在"}, ensure_ascii=False),
            )
        ]

    now = datetime.now().isoformat()

    if verdict == "accepted":
        conn.execute(
            "UPDATE task_queue SET status='verified', verified_at=?, verified_by=?, "
            "verify_verdict='accepted', updated_at=? WHERE id=?",
            (now, verified_by, now, task_id),
        )
        conn.commit()

        # Trust boost for the hunter
        delta = 0.02
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager

            tm = TrustManager()
            tm.boost(delta, f"委托验收通过: {task_id}", target=task["claimed_by"])
        except Exception:
            pass

        conn.close()

        # SSE broadcast — notify hunter of verification result
        sse_notified = 0
        try:
            from plastic_promise.core.task_event_bus import get_event_bus

            sse_notified = await get_event_bus().broadcast_task_event(
                "task:verified",
                {
                    "task_id": task_id,
                    "task_type": task["task_type"],
                    "title": task["title"],
                    "from_agent": verified_by,
                    "to_agent": task["to_agent"],
                    "priority": task["priority"],
                    "claimed_by": task["claimed_by"],
                },
            )
        except Exception:
            pass

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "new_status": "verified",
                        "sse_notified": sse_notified,
                        "trust_adjustment": {
                            "agent": task["claimed_by"],
                            "delta": delta,
                            "reason": "委托验收通过",
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    elif verdict in ("rejected", "reassigned"):
        new_esc = task["escalation_count"] + 1
        conn.execute(
            "UPDATE task_queue SET status='reassigned', verified_at=?, verified_by=?, "
            "verify_verdict=?, escalation_count=?, last_escalation_at=?, updated_at=? "
            "WHERE id=?",
            (now, verified_by, verdict, new_esc, now, now, task_id),
        )
        conn.commit()

        # Trust penalty
        delta = -0.03
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager

            tm = TrustManager()
            tm.decay(delta, f"委托被打回: {task_id} — {comment[:100]}", target=task["claimed_by"])
        except Exception:
            pass

        # Auto-create reassigned subtask
        reassign_to = args.get("reassign_to_agent", task["to_agent"])
        existing_payload = json.loads(task["payload"]) if task["payload"] else {}
        new_payload = {
            **existing_payload,
            "original_claimed_by": task["claimed_by"],
        }

        new_task_id = None
        if new_esc >= task["max_escalations"]:
            # Escalate to Claude
            new_task_id = _generate_task_id()
            new_payload.update(
                {
                    "verdict": verdict,
                    "comment": comment,
                }
            )
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, 'claude', 1, ?, 'pending', ?, ?, ?)",
                (
                    new_task_id,
                    task["task_type"],
                    f"[S级升级] {task['title']}",
                    verified_by,
                    f"升级原因: {new_esc}次失败/超时, 长老{verified_by}",
                    task_id,
                    json.dumps(new_payload),
                ),
            )
        else:
            new_task_id = _generate_task_id()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    new_task_id,
                    task["task_type"],
                    f"[重派] {task['title']}",
                    reassign_to,
                    max(1, task["priority"] - 1),  # Upgrade priority
                    verified_by,
                    f"长老{verified_by}打回重做。原因: {comment[:200]}",
                    task_id,
                    json.dumps(new_payload),
                ),
            )
        conn.commit()
        conn.close()

        # SSE broadcast — notify hunter of reassignment
        sse_notified = 0
        try:
            from plastic_promise.core.task_event_bus import get_event_bus

            sse_notified = await get_event_bus().broadcast_task_event(
                "task:reassigned",
                {
                    "task_id": task_id,
                    "task_type": task["task_type"],
                    "title": task["title"],
                    "from_agent": verified_by,
                    "to_agent": reassign_to if new_esc < task["max_escalations"] else "claude",
                    "priority": task["priority"],
                    "claimed_by": task["claimed_by"],
                },
            )
        except Exception:
            pass

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "new_status": "reassigned",
                        "sse_notified": sse_notified,
                        "new_task_id": new_task_id,
                        "escalation_count": new_esc,
                        "escalated_to_claude": new_esc >= task["max_escalations"],
                        "trust_adjustment": {
                            "agent": task["claimed_by"],
                            "delta": delta,
                            "reason": f"委托被打回: {comment[:80]}",
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    conn.close()
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {"success": False, "reason": f"无效的verdict: {verdict}"}, ensure_ascii=False
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# task_inbox
# ═══════════════════════════════════════════════════════════════


async def handle_task_inbox(engine: Any, args: dict) -> list[TextContent]:
    """View the guild board — default shows only claimable tasks."""
    agent_name = args["agent_name"]
    trust_score = args["trust_score"]
    filter_status = args.get("filter_status", "pending")
    limit = args.get("limit", 20)

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Stats
    my_active = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE claimed_by=? AND status IN ('claimed','executing')",
        (agent_name,),
    ).fetchone()[0]

    available = conn.execute("SELECT COUNT(*) FROM task_queue WHERE status='pending'").fetchone()[0]

    # Task list
    if filter_status == "my_active":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE claimed_by=? "
            "AND status IN ('claimed','executing','done') "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (agent_name, limit),
        ).fetchall()
    elif filter_status == "pending_review":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status='pending_review' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    elif filter_status == "all":
        rows = conn.execute(
            "SELECT * FROM task_queue ORDER BY priority ASC, created_at ASC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status='pending' "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()

    tasks = []
    for row in rows:
        ok, msg = can_claim(trust_score, row["priority"])
        tasks.append(
            {
                "id": row["id"],
                "task_type": row["task_type"],
                "title": row["title"],
                "priority": row["priority"],
                "recommended_rank": {1: "S", 2: "A", 3: "B", 4: "C"}.get(row["priority"], "C"),
                "status": row["status"],
                "from_agent": row["from_agent"],
                "created_at": row["created_at"],
                "match": msg,
                "can_claim": ok and row["status"] == "pending",
                "parent_task_id": row["parent_task_id"] or None,
            }
        )

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "agent_name": agent_name,
                    "rank": rank_info,
                    "stats": {
                        "my_active": my_active,
                        "available": available,
                    },
                    "tasks": tasks,
                },
                ensure_ascii=False,
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# task_heartbeat
# ═══════════════════════════════════════════════════════════════


async def handle_task_heartbeat(engine: Any, args: dict) -> list[TextContent]:
    """Send heartbeat for a claimed task."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]

    conn = _get_conn()
    task = conn.execute(
        "SELECT * FROM task_queue WHERE id=? AND claimed_by=?", (task_id, agent_name)
    ).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "reason": "委托不存在或非你揭榜"}, ensure_ascii=False
                ),
            )
        ]

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE task_queue SET heartbeat_at=?, updated_at=? WHERE id=?", (now, now, task_id)
    )
    conn.commit()

    # Check if overdue
    overdue = False
    if task["heartbeat_at"] and task["timeout_seconds"]:
        try:
            last_hb = datetime.fromisoformat(task["heartbeat_at"])
            elapsed = (datetime.now() - last_hb).total_seconds()
            if elapsed > task["timeout_seconds"]:
                overdue = True
        except (ValueError, TypeError):
            pass

    conn.close()
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "overdue": overdue,
                    "next_heartbeat_in": 60,
                },
                ensure_ascii=False,
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# task_abandon
# ═══════════════════════════════════════════════════════════════


async def handle_task_abandon(engine: Any, args: dict) -> list[TextContent]:
    """Abandon a claimed task — trust penalty applies."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]
    reason = args.get("reason", "")

    conn = _get_conn()
    task = conn.execute(
        "SELECT * FROM task_queue WHERE id=? AND claimed_by=? "
        "AND status IN ('claimed','executing')",
        (task_id, agent_name),
    ).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "reason": "委托不存在或非你揭榜或已提交"}, ensure_ascii=False
                ),
            )
        ]

    # Penalty: -0.02 base
    delta = -0.02
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager

        tm = TrustManager()
        current = tm.get(agent_name)
    except Exception:
        current = 0.50  # fallback if TrustManager unavailable

    # Issue 4 fix: Log failure FIRST (durable even if TrustManager fails)
    conn.execute(
        "INSERT INTO hunter_failure_log "
        "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
        "VALUES (?, ?, ?, 'abandoned', ?, ?, ?)",
        (agent_name, task_id, task["task_type"], current, current + delta, delta),
    )
    conn.commit()

    try:
        tm.decay(delta, f"主动弃单: {task_id} — {reason[:80]}", target=agent_name)
    except Exception:
        pass

    # Release task back to pending
    conn.execute(
        "UPDATE task_queue SET status='pending', claimed_by=NULL, claimed_at=NULL, "
        "heartbeat_at=NULL, updated_at=? WHERE id=?",
        (datetime.now().isoformat(), task_id),
    )
    conn.commit()

    # Count repeat abandons
    abandon_count = conn.execute(
        "SELECT COUNT(*) FROM hunter_failure_log WHERE agent_name=? AND failure_type='abandoned'",
        (agent_name,),
    ).fetchone()[0]
    conn.close()

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "penalty": {
                        "type": "abandoned",
                        "trust_delta": delta,
                        "repeat_count": abandon_count,
                        "warning": f"累计弃单{abandon_count}次，再弃{5 - abandon_count}次将降级到D"
                        if abandon_count < 5
                        else "已触发降级审查",
                    },
                },
                ensure_ascii=False,
            ),
        )
    ]
