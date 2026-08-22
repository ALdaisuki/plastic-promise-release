"""MCP Task Queue tools — Hunter Guild dispatch board (Phase 1 complete: 7 tools).

Tools: task_enqueue, task_claim, task_complete, task_verify,
       task_inbox, task_heartbeat, task_abandon
"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import suppress
from datetime import datetime
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.hunter_rank import can_claim, trust_to_rank
from plastic_promise.core.paths import get_db_path
from plastic_promise.core.project_identity import canonical_project_id

_TASK_REVIEWER_ACTORS = frozenset({"claude", "codex"})
_TASK_ACTIVE_STATUSES = ("claimed", "executing")


def _get_db_path() -> str:
    return get_db_path()


def _generate_task_id() -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"t_{datetime.now().strftime('%Y%m%d%H%M%S')}_{suffix}"


def _compute_payload_hash(payload: dict) -> str:
    """Compute a deterministic hash for dedup based on payload content.

    Serializes payload with sorted keys to produce a canonical form,
    then returns SHA256 first 8 hex chars. Excludes 'payload_hash' from
    the computation so that injecting the hash into the stored payload
    does not change the hash value.

    Works for any payload shape. Returns empty string if payload is
    None, empty, or contains only 'payload_hash'.
    """
    if not payload:
        return ""
    clean = {k: v for k, v in payload.items() if k != "payload_hash"}
    if not clean:
        return ""
    canonical = json.dumps(clean, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


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
    # Ensure Hunter Guild tables exist (idempotent — CREATE TABLE IF NOT EXISTS)
    from plastic_promise.core.task_queue_schema import ensure_task_tables

    ensure_task_tables(conn)
    return conn


def _canonical_project_id(args: dict[str, Any]) -> str:
    """Return the explicit project authority or an empty fail-closed marker."""

    return canonical_project_id(args.get("project_id"))


def _project_scope_rejected(args: dict[str, Any]) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": False,
                    "status": "rejected",
                    "project_id": str(args.get("project_id") or "").strip(),
                    "reason": "canonical project_id is required",
                },
                ensure_ascii=False,
            ),
        )
    ]


def _task_authority_rejected(
    args: dict[str, Any],
    reason: str,
    *,
    runtime_project_id: str = "",
) -> list[TextContent]:
    """Return a stable rejection without treating caller declarations as authority."""

    requested_project_id = str(args.get("project_id") or "").strip()
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": False,
                    "status": "rejected",
                    "project_id": runtime_project_id or requested_project_id,
                    "requested_project_id": requested_project_id,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
    ]


def _coerce_trust_score(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_task_authority(
    tool_name: str,
    args: dict[str, Any],
    runtime_context: dict[str, Any] | None,
    *,
    actor_field: str | None = None,
    trust_field: str | None = None,
    internal_default_actor: str = "",
    require_reviewer: bool = False,
) -> tuple[dict[str, Any] | None, list[TextContent] | None]:
    """Resolve effective Task authority.

    ``runtime_context is None`` is deliberately reserved for trusted in-process
    Python callers such as scanners.  Every public MCP dispatch supplies a
    private runtime context, so network callers cannot reach this compatibility
    path by omitting fields.
    """

    requested_project_id = _canonical_project_id(args)
    if runtime_context is None:
        if not requested_project_id:
            return None, _project_scope_rejected(args)
        actor = (
            str(args.get(actor_field) or internal_default_actor).strip()
            if actor_field
            else internal_default_actor
        )
        trust_score = _coerce_trust_score(args.get(trust_field)) if trust_field else None
        authority = {
            "actor": actor,
            "call_id": "",
            "project_id": requested_project_id,
            "project_policy": "balanced",
            "trust_score": trust_score,
            "trust_tier": str(args.get("trust_tier") or ""),
            "defense_decision": "allow",
            "authority_source": "trusted_internal",
            "tool_name": tool_name,
        }
        return authority, None

    if (
        not isinstance(runtime_context, dict)
        or runtime_context.get("authority_source") != "server_runtime_session"
    ):
        return None, _task_authority_rejected(
            args,
            "task_runtime_authorization_required",
            runtime_project_id=str((runtime_context or {}).get("project_id") or ""),
        )

    runtime_project_id = _canonical_project_id({"project_id": runtime_context.get("project_id")})
    if not runtime_project_id or requested_project_id != runtime_project_id:
        return None, _task_authority_rejected(
            args,
            "task_project_scope_mismatch",
            runtime_project_id=runtime_project_id,
        )

    runtime_tool = str(runtime_context.get("tool_name") or "").strip()
    if runtime_tool and runtime_tool != tool_name:
        return None, _task_authority_rejected(
            args,
            "task_runtime_authorization_required",
            runtime_project_id=runtime_project_id,
        )

    actor = str(runtime_context.get("actor") or "").strip()
    if not actor or actor == "mcp":
        return None, _task_authority_rejected(
            args,
            "task_runtime_authorization_required",
            runtime_project_id=runtime_project_id,
        )
    if str(runtime_context.get("defense_decision") or "deny") != "allow":
        return None, _task_authority_rejected(
            args,
            "task_runtime_authorization_denied",
            runtime_project_id=runtime_project_id,
        )

    if actor_field and actor_field in args:
        declared_actor = str(args.get(actor_field) or "").strip()
        if declared_actor != actor:
            return None, _task_authority_rejected(
                args,
                "task_actor_mismatch",
                runtime_project_id=runtime_project_id,
            )

    trust_score = _coerce_trust_score(runtime_context.get("trust_score"))
    if trust_score is None:
        return None, _task_authority_rejected(
            args,
            "task_runtime_authorization_denied",
            runtime_project_id=runtime_project_id,
        )
    if trust_field and trust_field in args:
        declared_trust = _coerce_trust_score(args.get(trust_field))
        if declared_trust is None or abs(declared_trust - trust_score) > 1e-9:
            return None, _task_authority_rejected(
                args,
                "task_trust_declaration_mismatch",
                runtime_project_id=runtime_project_id,
            )

    if require_reviewer and actor not in _TASK_REVIEWER_ACTORS:
        return None, _task_authority_rejected(
            args,
            "task_reviewer_authority_required",
            runtime_project_id=runtime_project_id,
        )

    authority = {
        **runtime_context,
        "actor": actor,
        "project_id": runtime_project_id,
        "trust_score": trust_score,
        "tool_name": tool_name,
    }
    return authority, None


def _runtime_event_args(args: dict[str, Any], authority: dict[str, Any]) -> dict[str, Any]:
    """Keep caller workflow metadata while replacing authority-bearing fields."""

    return {
        **args,
        "project_id": authority["project_id"],
        "trust_score": authority.get("trust_score"),
        "trust_tier": authority.get("trust_tier", ""),
        "defense_decision": authority.get("defense_decision", ""),
    }


def _task_state_conflict(
    project_id: str,
    task_id: str,
    *,
    current_status: str = "",
) -> list[TextContent]:
    payload: dict[str, Any] = {
        "success": False,
        "status": "rejected",
        "project_id": project_id,
        "task_id": task_id,
        "reason": "task_state_conflict",
    }
    if current_status:
        payload["current_status"] = current_status
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _parent_scope_rejected(project_id: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": False,
                    "status": "rejected",
                    "project_id": project_id,
                    "reason": "parent task is unavailable in project scope",
                },
                ensure_ascii=False,
            ),
        )
    ]


def _parent_task_is_in_project(project_id: str, parent_task_id: object) -> bool:
    if parent_task_id in {None, ""}:
        return True
    if not isinstance(parent_task_id, str) or parent_task_id != parent_task_id.strip():
        return False
    conn = _get_conn()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM task_queue WHERE project_id = ? AND id = ?",
                (project_id, parent_task_id),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def _record_task_runtime_event(
    conn,
    *,
    event_name: str,
    status: str,
    args: dict[str, Any],
    task_id: str,
    actor: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    from plastic_promise.core.event_protocol import record_runtime_event
    from plastic_promise.mcp.tools.request_scope import build_request_scope

    project_id = _canonical_project_id(args)
    scope = build_request_scope({**args, "project_id": project_id}, event_name)
    record_runtime_event(
        conn,
        event_kind="task",
        event_name=event_name,
        status=status,
        request_scope_id=scope["request_scope_id"],
        stage_session_id=scope["stage_session_id"],
        flow_line_id=scope["flow_line_id"],
        project_id=project_id,
        actor=actor,
        trust_tier=str(args.get("trust_tier") or ""),
        defense_decision=str(args.get("defense_decision") or ""),
        audit_trace={"task_id": task_id},
        metadata={"task_id": task_id, **(metadata or {})},
    )


# ═══════════════════════════════════════════════════════════════
# task_enqueue
# ═══════════════════════════════════════════════════════════════


async def handle_task_enqueue(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Enqueue a task onto the guild board.

    Validates the submitter's trust score and enforces rank-based
    submission rules.
    """
    authority, rejected = _resolve_task_authority(
        "task_enqueue",
        args,
        _runtime_context,
        actor_field="from_agent",
        trust_field="from_trust_score",
        internal_default_actor="daemon",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]
    if not _parent_task_is_in_project(project_id, args.get("parent_task_id")):
        return _parent_scope_rejected(project_id)

    from_agent = authority["actor"] or "daemon"
    from_trust_score = authority.get("trust_score")
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
                            "project_id": project_id,
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
            review_task_id = _generate_task_id()
            conn = _get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
                    "from_agent, status, description, domain, memory_id, principle_id, "
                    "source_scan, parent_task_id, timeout_seconds, max_escalations, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        project_id,
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
                # The approval task is part of the same state transition.  A
                # crash cannot leave a pending_review parent without its review work.
                conn.execute(
                    "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
                    "from_agent, status, description, parent_task_id, payload) "
                    "VALUES (?, ?, 'notify_review', ?, 'claude', 2, 'system', 'pending', ?, ?, ?)",
                    (
                        review_task_id,
                        project_id,
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
                _record_task_runtime_event(
                    conn,
                    event_name="task_enqueue",
                    status="pending",
                    args=_runtime_event_args(args, authority),
                    task_id=task_id,
                    actor=from_agent,
                    metadata={
                        "task_type": args["task_type"],
                        "task_status": "pending_review",
                        "to_agent": args["to_agent"],
                        "priority": priority,
                        "review_task_id": review_task_id,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "project_id": project_id,
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

    # ── Dedup check (all scanner-generated tasks) ─────────
    # For any task with a source_scan (auto-generated by scanners),
    # check if a pending task with the same payload_hash already exists.
    # Pending scanner findings represent unresolved work and do not become
    # unique again merely because they crossed a wall-clock boundary.
    source_scan = args.get("source_scan")
    conn = None
    if source_scan is not None:
        payload = args.get("payload")
        if payload:
            phash = _compute_payload_hash(payload)
            if phash:
                conn = _get_conn()
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT id FROM task_queue "
                    "WHERE project_id = ? AND task_type = ? AND status = 'pending' "
                    "AND source_scan IS NOT NULL "
                    "AND json_extract(payload, '$.payload_hash') = ? "
                    "LIMIT 1",
                    (project_id, args["task_type"], phash),
                ).fetchone()
                if existing:
                    conn.rollback()
                    conn.close()
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "project_id": project_id,
                                    "status": "duplicate",
                                    "existing_task_id": existing["id"],
                                    "reason": (
                                        f"Pending {args['task_type']} from {source_scan} "
                                        "already exists for this payload"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    ]

    # ── Normal enqueue ─────────────────────────────────────
    task_id = _generate_task_id()
    conn = conn or _get_conn()
    try:
        conn.execute(
            "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
            "from_agent, status, description, domain, memory_id, principle_id, "
            "source_scan, parent_task_id, timeout_seconds, max_escalations, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                project_id,
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
        _record_task_runtime_event(
            conn,
            event_name="task_enqueue",
            status="pending",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=from_agent,
            metadata={
                "task_type": args["task_type"],
                "to_agent": args["to_agent"],
                "priority": priority,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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

    # SSE broadcast — fire-and-forget, never blocks task creation
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        bus = get_event_bus()
        sse_notified = await bus.broadcast_task_event(
            "task:new",
            {
                "project_id": project_id,
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
                    "project_id": project_id,
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


async def handle_task_claim(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Claim a task from the guild board. Atomic — first-come-first-served."""
    authority, rejected = _resolve_task_authority(
        "task_claim",
        args,
        _runtime_context,
        actor_field="agent_name",
        trust_field="trust_score",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]

    agent_name = authority["actor"]
    task_id = args["task_id"]
    trust_score = authority["trust_score"]
    force = args.get("force", False)
    if (
        force
        and _runtime_context is not None
        and (agent_name not in _TASK_REVIEWER_ACTORS or trust_score is None or trust_score < 0.80)
    ):
        return _task_authority_rejected(
            args,
            "task_force_claim_authority_required",
            runtime_project_id=project_id,
        )

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Read task
    task = conn.execute(
        "SELECT * FROM task_queue WHERE project_id = ? AND id = ?",
        (project_id, task_id),
    ).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "project_id": project_id, "reason": "委托不存在"},
                    ensure_ascii=False,
                ),
            )
        ]

    if task["status"] != "pending":
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "project_id": project_id,
                        "reason": f"委托已被揭榜 (status={task['status']})",
                    },
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
                        "project_id": project_id,
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
        msg = f"!!! 越级揭榜(已记录): {msg}"

    # Atomic claim
    now = datetime.now().isoformat()
    try:
        result = conn.execute(
            "UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=?, "
            "heartbeat_at=?, updated_at=? WHERE project_id=? AND id=? AND status='pending'",
            (agent_name, now, now, now, project_id, task_id),
        )
        if result.rowcount == 0:
            conn.rollback()
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "project_id": project_id,
                            "reason": "揭榜失败: 委托已被其他猎人抢先揭榜",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        _record_task_runtime_event(
            conn,
            event_name="task_claim",
            status="running",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=agent_name,
            metadata={
                "task_type": task["task_type"],
                "priority": task["priority"],
                "force": force,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # SSE broadcast — notify submitter that task was claimed
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        sse_notified = await get_event_bus().broadcast_task_event(
            "task:claimed",
            {
                "project_id": project_id,
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
                    "project_id": project_id,
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


async def handle_task_complete(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Submit a completed task for verification."""
    authority, rejected = _resolve_task_authority(
        "task_complete",
        args,
        _runtime_context,
        actor_field="agent_name",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]

    task_id = args["task_id"]
    agent_name = authority["actor"]
    result_text = args["result"]
    artifacts = args.get("artifacts", [])

    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    task = conn.execute(
        "SELECT * FROM task_queue WHERE project_id = ? AND id = ?",
        (project_id, task_id),
    ).fetchone()
    if not task:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "project_id": project_id, "reason": "委托不存在"},
                    ensure_ascii=False,
                ),
            )
        ]

    if task["claimed_by"] != agent_name:
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "project_id": project_id,
                        "reason": f"委托由 {task['claimed_by']} 揭榜，不是你",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    verify_task_id = None
    try:
        now = datetime.now().isoformat()
        transition = conn.execute(
            "UPDATE task_queue SET status='done', done_at=?, result=?, updated_at=? "
            "WHERE project_id=? AND id=? AND claimed_by=? "
            "AND status IN ('claimed','executing')",
            (now, result_text, now, project_id, task_id, agent_name),
        )
        if transition.rowcount != 1:
            current_status = str(task["status"] or "")
            conn.rollback()
            return _task_state_conflict(
                project_id,
                task_id,
                current_status=current_status,
            )

        # Auto-create verification subtask for Claude (unless task is already for Claude)
        if task["to_agent"] != "claude":
            verify_task_id = _generate_task_id()
            conn.execute(
                "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, 'verify_task', ?, 'claude', ?, 'system', 'pending', ?, ?, ?)",
                (
                    verify_task_id,
                    project_id,
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

        _record_task_runtime_event(
            conn,
            event_name="task_complete",
            status="completed",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=agent_name,
            metadata={
                "task_type": task["task_type"],
                "verification_task_id": verify_task_id,
                "artifacts": artifacts,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # SSE broadcast — notify submitter that task is done
    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        sse_notified = await get_event_bus().broadcast_task_event(
            "task:done",
            {
                "project_id": project_id,
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
                    "project_id": project_id,
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


async def handle_task_verify(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Verify a completed task using reviewer authority and a status CAS."""
    authority, rejected = _resolve_task_authority(
        "task_verify",
        args,
        _runtime_context,
        actor_field="verified_by",
        internal_default_actor="claude",
        require_reviewer=_runtime_context is not None,
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]
    verified_by = authority["actor"] or "claude"

    task_id = args["task_id"]
    verdict = str(args["verdict"] or "").strip().casefold()
    comment = str(args.get("comment") or "")
    if verdict not in {"accepted", "rejected", "reassigned"}:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "project_id": project_id,
                        "reason": f"无效的verdict: {verdict}",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    task = conn.execute(
        "SELECT * FROM task_queue WHERE project_id = ? AND id = ?",
        (project_id, task_id),
    ).fetchone()
    if not task:
        conn.rollback()
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "project_id": project_id, "reason": "委托不存在"},
                    ensure_ascii=False,
                ),
            )
        ]
    # A verification subtask is created 'pending' for the elder and has no
    # meaningful claim/complete loop of its own; a server-owned reviewer may
    # verify it directly.  Regular tasks still require the hunter's 'done'.
    is_pending_verification_subtask = (
        str(task["status"] or "") == "pending"
        and str(task["task_type"] or "") == "verify_task"
    )
    if task["status"] != "done" and not is_pending_verification_subtask:
        current_status = str(task["status"] or "")
        conn.rollback()
        conn.close()
        return _task_state_conflict(
            project_id,
            task_id,
            current_status=current_status,
        )

    now = datetime.now().isoformat()
    new_task_id = None
    new_esc = int(task["escalation_count"] or 0)
    max_escalations = int(task["max_escalations"] or 3)
    reassign_to = str(args.get("reassign_to_agent") or task["to_agent"])

    # Trust attribution falls back to the original hunter recorded on the
    # verification subtask's payload when claimed_by is absent (auto-created
    # subtasks are never claimed).
    trust_target = task["claimed_by"]
    if not trust_target:
        try:
            _trust_payload = json.loads(task["payload"]) if task["payload"] else {}
            if isinstance(_trust_payload, dict):
                trust_target = _trust_payload.get("original_agent") or None
        except (TypeError, ValueError):
            trust_target = None
    trust_skipped = trust_target is None

    if verdict == "accepted":
        # CAS over both legal pre-verify states: 'done' for regular tasks,
        # 'pending' for elder-direct verification subtasks.
        transition = conn.execute(
            "UPDATE task_queue SET status='verified', verified_at=?, verified_by=?, "
            "verify_verdict='accepted', updated_at=? "
            "WHERE project_id=? AND id=? AND status IN ('done','pending')",
            (now, verified_by, now, project_id, task_id),
        )
        new_status = "verified"
        delta = 0.02
        event_type = "task:verified"
    else:
        new_esc += 1
        # Rejection/reassignment of a pending verification subtask must also pass;
        # the pre-check already guarantees regular tasks arrive here only 'done'.
        transition = conn.execute(
            "UPDATE task_queue SET status='reassigned', verified_at=?, verified_by=?, "
            "verify_verdict=?, escalation_count=?, last_escalation_at=?, updated_at=? "
            "WHERE project_id=? AND id=? AND status IN ('done','pending')",
            (now, verified_by, verdict, new_esc, now, now, project_id, task_id),
        )
        new_status = "reassigned"
        delta = -0.03
        event_type = "task:reassigned"

    if transition.rowcount != 1:
        conn.rollback()
        conn.close()
        return _task_state_conflict(project_id, task_id)

    if verdict != "accepted":
        try:
            existing_payload = json.loads(task["payload"]) if task["payload"] else {}
        except (TypeError, ValueError):
            existing_payload = {}
        if not isinstance(existing_payload, dict):
            existing_payload = {}
        new_payload = {
            **existing_payload,
            "original_claimed_by": task["claimed_by"],
        }
        new_task_id = _generate_task_id()
        if new_esc >= max_escalations:
            new_payload.update({"verdict": verdict, "comment": comment})
            conn.execute(
                "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, ?, 'claude', 1, ?, 'pending', ?, ?, ?)",
                (
                    new_task_id,
                    project_id,
                    task["task_type"],
                    f"[S级升级] {task['title']}",
                    verified_by,
                    f"升级原因: {new_esc}次失败/超时, 长老{verified_by}",
                    task_id,
                    json.dumps(new_payload),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    new_task_id,
                    project_id,
                    task["task_type"],
                    f"[重派] {task['title']}",
                    reassign_to,
                    max(1, int(task["priority"] or 3) - 1),
                    verified_by,
                    f"长老{verified_by}打回重做。原因: {comment[:200]}",
                    task_id,
                    json.dumps(new_payload),
                ),
            )

    try:
        _record_task_runtime_event(
            conn,
            event_name="task_verify",
            status="completed",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=verified_by,
            metadata={
                "task_type": task["task_type"],
                "verdict": verdict,
                "claimed_by": task["claimed_by"],
                "new_task_id": new_task_id,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        if not trust_skipped:
            from plastic_promise.defense.soul_enforcer import TrustManager

            tm = TrustManager()
            if delta > 0:
                tm.boost(delta, f"委托验收通过: {task_id}", target=trust_target)
            else:
                tm.decay(
                    delta,
                    f"委托被打回: {task_id} — {comment[:100]}",
                    target=trust_target,
                )
    except Exception:
        pass

    sse_notified = 0
    try:
        from plastic_promise.core.task_event_bus import get_event_bus

        sse_notified = await get_event_bus().broadcast_task_event(
            event_type,
            {
                "project_id": project_id,
                "task_id": task_id,
                "task_type": task["task_type"],
                "title": task["title"],
                "from_agent": verified_by,
                "to_agent": (
                    reassign_to
                    if verdict != "accepted" and new_esc < max_escalations
                    else "claude"
                    if verdict != "accepted"
                    else task["to_agent"]
                ),
                "priority": task["priority"],
                "claimed_by": task["claimed_by"],
            },
        )
    except Exception:
        pass

    trust_reason = "委托验收通过" if verdict == "accepted" else f"委托被打回: {comment[:80]}"
    payload: dict[str, Any] = {
        "success": True,
        "project_id": project_id,
        "new_status": new_status,
        "sse_notified": sse_notified,
        "trust_adjustment": {
            "agent": trust_target,
            "delta": delta,
            "reason": trust_reason,
            **({"skipped_reason": "no_trust_target"} if trust_skipped else {}),
        },
    }
    if verdict != "accepted":
        payload.update(
            {
                "new_task_id": new_task_id,
                "escalation_count": new_esc,
                "escalated_to_claude": new_esc >= max_escalations,
            }
        )
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


# ═══════════════════════════════════════════════════════════════
# task_inbox
# ═══════════════════════════════════════════════════════════════


async def handle_task_inbox(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """View the guild board — default shows only claimable tasks."""
    authority, rejected = _resolve_task_authority(
        "task_inbox",
        args,
        _runtime_context,
        actor_field="agent_name",
        trust_field="trust_score",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]

    agent_name = authority["actor"]
    trust_score = authority["trust_score"]
    filter_status = args.get("filter_status", "pending")
    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Stats
    my_active = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE project_id=? AND claimed_by=? "
        "AND status IN ('claimed','executing')",
        (project_id, agent_name),
    ).fetchone()[0]

    available = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE project_id=? AND status='pending'",
        (project_id,),
    ).fetchone()[0]

    # Task list
    if filter_status == "my_active":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE project_id=? AND claimed_by=? "
            "AND status IN ('claimed','executing','done') "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (project_id, agent_name, limit),
        ).fetchall()
    elif filter_status == "pending_review":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE project_id=? AND status='pending_review' "
            "ORDER BY created_at ASC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    elif filter_status == "all":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE project_id=? "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE project_id=? AND status='pending' "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    conn.close()

    tasks = []
    for row in rows:
        ok, msg = can_claim(trust_score, row["priority"])
        tasks.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
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
                    "project_id": project_id,
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


async def handle_task_heartbeat(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Send heartbeat for a claimed task."""
    authority, rejected = _resolve_task_authority(
        "task_heartbeat",
        args,
        _runtime_context,
        actor_field="agent_name",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]

    task_id = args["task_id"]
    agent_name = authority["actor"]

    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    task = conn.execute(
        "SELECT * FROM task_queue WHERE project_id=? AND id=?",
        (project_id, task_id),
    ).fetchone()
    if not task:
        conn.rollback()
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "project_id": project_id,
                        "reason": "委托不存在或非你揭榜",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    now = datetime.now().isoformat()
    transition = conn.execute(
        "UPDATE task_queue SET status=CASE WHEN status='claimed' THEN 'executing' ELSE status END, "
        "heartbeat_at=?, updated_at=? WHERE project_id=? AND id=? AND claimed_by=? "
        "AND status IN ('claimed','executing')",
        (now, now, project_id, task_id, agent_name),
    )
    if transition.rowcount != 1:
        current_status = str(task["status"] or "")
        conn.rollback()
        conn.close()
        return _task_state_conflict(
            project_id,
            task_id,
            current_status=current_status,
        )

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

    try:
        _record_task_runtime_event(
            conn,
            event_name="task_heartbeat",
            status="running",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=agent_name,
            metadata={
                "task_type": task["task_type"],
                "previous_status": task["status"],
                "overdue": overdue,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "project_id": project_id,
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


async def handle_task_abandon(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Abandon a claimed task — trust penalty applies."""
    authority, rejected = _resolve_task_authority(
        "task_abandon",
        args,
        _runtime_context,
        actor_field="agent_name",
    )
    if rejected is not None:
        return rejected
    assert authority is not None
    project_id = authority["project_id"]

    task_id = args["task_id"]
    agent_name = authority["actor"]
    reason = args.get("reason", "")

    # Penalty: -0.02 base
    delta = -0.02
    tm = None
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager

        tm = TrustManager()
        current = tm.get(agent_name)
    except Exception:
        current = 0.50  # fallback if TrustManager unavailable

    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    task = conn.execute(
        "SELECT * FROM task_queue WHERE project_id=? AND id=?",
        (project_id, task_id),
    ).fetchone()
    if not task:
        conn.rollback()
        conn.close()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "project_id": project_id, "reason": "委托不存在"},
                    ensure_ascii=False,
                ),
            )
        ]

    transition = conn.execute(
        "UPDATE task_queue SET status='pending', claimed_by=NULL, claimed_at=NULL, "
        "heartbeat_at=NULL, updated_at=? WHERE project_id=? AND id=? AND claimed_by=? "
        "AND status IN ('claimed','executing')",
        (datetime.now().isoformat(), project_id, task_id, agent_name),
    )
    if transition.rowcount != 1:
        current_status = str(task["status"] or "")
        conn.rollback()
        conn.close()
        return _task_state_conflict(
            project_id,
            task_id,
            current_status=current_status,
        )

    # State release, failure evidence, and the runtime event are one transition.
    # Trust is adjusted only after the transaction commits, so a racing
    # complete/abandon cannot double-penalize.
    try:
        conn.execute(
            "INSERT INTO hunter_failure_log "
            "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
            "VALUES (?, ?, ?, 'abandoned', ?, ?, ?)",
            (agent_name, task_id, task["task_type"], current, current + delta, delta),
        )
        abandon_count = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log AS failure "
            "JOIN task_queue AS task ON task.id = failure.task_id "
            "WHERE task.project_id=? AND failure.agent_name=? "
            "AND failure.failure_type='abandoned'",
            (project_id, agent_name),
        ).fetchone()[0]
        _record_task_runtime_event(
            conn,
            event_name="task_abandon",
            status="completed",
            args=_runtime_event_args(args, authority),
            task_id=task_id,
            actor=agent_name,
            metadata={
                "task_type": task["task_type"],
                "previous_status": task["status"],
                "reason": str(reason)[:200],
                "penalty": delta,
                "repeat_count": abandon_count,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if tm is not None:
        with suppress(Exception):
            tm.decay(delta, f"主动弃单: {task_id} — {reason[:80]}", target=agent_name)

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "project_id": project_id,
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
