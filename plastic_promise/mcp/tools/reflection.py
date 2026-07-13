"""MCP Reflection tool handlers — 2 tools for introspection and evolution.

公开工具:
- scarf_reflect  : SCARF 五维自省 (mode=standard|inertia)
- feedback_apply : 手动应用反馈到记忆或上下文条目

内部处理器:
- handle_inertia_check : 惯性抑制检测 (由 scarf_reflect mode=inertia 调用)
"""

import json
from typing import Any

from mcp.types import TextContent

# ---------------------------------------------------------------------------
# scarf_reflect (stub)
# ---------------------------------------------------------------------------


async def handle_scarf_reflect(engine: Any, args: dict) -> list[TextContent]:
    """Execute SCARF five-dimension self-reflection or inertia check.

    Args:
        engine: ContextEngine instance.
        args: {"context": str, "dimensions"?: list[str], "mode"?: "standard"|"inertia",
               "recent_tasks"?: list[str]}.

    Returns:
        list[TextContent]: MCP response.
    """
    mode = args.get("mode", "standard")
    if mode == "inertia":
        return await handle_inertia_check(engine, args)

    # standard SCARF reflection
    try:
        from plastic_promise.reflection.soul_scarf import SCARFReflector

        context = args.get("context") or args.get("task_description", "")
        dimensions = args.get("dimensions")
        reflector = SCARFReflector()
        # Offload to thread: reflect() does 15 sync embedder.embed() HTTP calls
        import asyncio as _asyncio

        result = await _asyncio.to_thread(reflector.reflect, context)
        if dimensions:
            result = {d: result.get(d) for d in dimensions if d in result}
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tool": "scarf_reflect",
                        "reflection": result,
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
                text=json.dumps({"error": str(e), "tool": "scarf_reflect"}, ensure_ascii=False),
            )
        ]


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
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tool": "inertia_check",
                        "inertia": result,
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
                text=json.dumps({"error": str(e), "tool": "inertia_check"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# feedback_apply
# ---------------------------------------------------------------------------


def _feedback_canonical_connection(engine: Any):
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    return getattr(sqlite, "_conn", None)


_MIN_GOVERNED_FEEDBACK_TRUST = 0.60


def _governed_feedback_authority(
    runtime_context: dict[str, Any] | None,
    *,
    artifact_project_id: object,
) -> tuple[tuple[str, str] | None, str]:
    if not isinstance(runtime_context, dict):
        return None, "feedback_runtime_authorization_required"

    actor = str(runtime_context.get("actor") or "").strip()
    call_id = str(runtime_context.get("call_id") or "").strip()
    runtime_project_id = str(runtime_context.get("project_id") or "").strip()
    if (
        actor in {"", "mcp"}
        or not call_id
        or runtime_project_id
        in {
            "",
            "project:unknown",
        }
    ):
        return None, "feedback_runtime_authorization_required"

    try:
        trust_score = float(runtime_context.get("trust_score"))
    except (TypeError, ValueError):
        return None, "feedback_runtime_authorization_denied"
    if runtime_context.get("defense_decision") != "allow" or not (
        _MIN_GOVERNED_FEEDBACK_TRUST <= trust_score <= 1.0
    ):
        return None, "feedback_runtime_authorization_denied"

    if runtime_project_id != str(artifact_project_id or "").strip():
        return None, "feedback_project_mismatch"
    return (actor, call_id), ""


def _governed_feedback_failure(
    item_id: str,
    feedback_type: str,
    reason: str,
) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "updated": False,
                    "item_id": item_id,
                    "feedback_type": feedback_type,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
    ]


def _governed_feedback_payload(artifact: Any, feedback_type: str) -> dict[str, Any]:
    return {
        "updated": True,
        "item_id": artifact.memory_id,
        "feedback_type": feedback_type,
        "status": artifact.status,
        "revision": artifact.revision,
        "support_count": artifact.support_count,
        "source_fingerprint": artifact.source_fingerprint,
        "stale_reason": artifact.stale_reason,
        "verified_by_actor": artifact.verified_by_actor,
        "verified_by_call_id": artifact.verified_by_call_id,
    }


def _handle_memory_proposal_feedback(
    engine: Any,
    args: dict,
    *,
    proposal: dict[str, Any],
    actor: str,
    call_id: str,
) -> list[TextContent]:
    from plastic_promise.core.memory_proposals import (
        ProposalPolicyError,
        promote_memory_proposal,
        reject_memory_proposal,
    )

    item_id = str(proposal["proposal_id"])
    feedback_type = str(args.get("feedback_type") or "")
    if feedback_type == "ignored":
        payload = {
            "updated": False,
            "item_id": item_id,
            "feedback_type": feedback_type,
            "reason": "proposal_feedback_ignored_not_allowed",
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    try:
        if feedback_type == "adopted":
            result = promote_memory_proposal(
                engine,
                item_id,
                actor=actor,
                call_id=call_id,
            )
            payload = {
                "updated": True,
                "item_id": item_id,
                "feedback_type": feedback_type,
                "status": result.status,
                "memory_id": result.memory_id,
                "index_job_id": result.index_job_id,
            }
        elif feedback_type == "rejected":
            rejected = reject_memory_proposal(
                engine,
                item_id,
                actor=actor,
                call_id=call_id,
                reason=str(
                    args.get("rejection_reason") or args.get("task_context") or "reviewer_rejected"
                ),
            )
            payload = {
                "updated": True,
                "item_id": item_id,
                "feedback_type": feedback_type,
                "status": rejected["status"],
            }
        else:
            raise ProposalPolicyError("unsupported_proposal_feedback")
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    except ProposalPolicyError as exc:
        payload = {
            "updated": False,
            "item_id": item_id,
            "feedback_type": feedback_type,
            "reason": str(exc),
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    except Exception as exc:
        payload = {
            "updated": False,
            "item_id": item_id,
            "feedback_type": feedback_type,
            "reason": "proposal_feedback_failed",
            "error_class": exc.__class__.__name__,
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _handle_governed_synthesis_feedback(
    engine: Any,
    args: dict,
    *,
    conn: Any,
    actor: str,
    call_id: str,
) -> list[TextContent]:
    from plastic_promise.core.synthesis import SynthesisConflict, SynthesisStore

    item_id = str(args.get("item_id") or "")
    feedback_type = str(args.get("feedback_type") or "")
    if feedback_type == "ignored":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "updated": False,
                        "item_id": item_id,
                        "feedback_type": feedback_type,
                        "reason": "synthesis_feedback_ignored_not_allowed",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    try:
        expected_revision = args.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 1:
            raise SynthesisConflict("invalid_expected_revision")
        store = SynthesisStore(conn, engine=engine)
        if feedback_type == "adopted":
            artifact = store.verify(item_id, actor, call_id, expected_revision)
        elif feedback_type == "rejected":
            reason = str(
                args.get("rejection_reason") or args.get("task_context") or "reviewer_rejected"
            )
            artifact = store.mark_contested(
                item_id,
                reason,
                expected_revision,
                actor=actor,
                call_id=call_id,
            )
        else:
            raise SynthesisConflict("unsupported_synthesis_feedback")
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    _governed_feedback_payload(artifact, feedback_type),
                    ensure_ascii=False,
                ),
            )
        ]
    except SynthesisConflict as exc:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "updated": False,
                        "item_id": item_id,
                        "feedback_type": feedback_type,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as exc:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "updated": False,
                        "item_id": item_id,
                        "feedback_type": feedback_type,
                        "reason": "synthesis_feedback_failed",
                        "error_class": exc.__class__.__name__,
                    },
                    ensure_ascii=False,
                ),
            )
        ]


async def handle_feedback_apply(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Apply feedback to a memory, updating its worth counters.

    Manually applies feedback to a memory or context item (adopted / ignored /
    rejected). Updates the worth counter and self-evolution weights, then
    persists the updated record back to the engine's storage backend.

    Args:
        engine: ContextEngine instance (must provide get_memory + store_memory).
        args: {"item_id": str, "feedback_type": str, "task_context"?: str}.
        _runtime_context: Server-owned reviewer authority for governed artifacts.

    Returns:
        list[TextContent]: MCP response with updated worth score and observation count.
    """
    try:
        item_id: str = args["item_id"]
        feedback_type: str = args["feedback_type"]  # adopted / ignored / rejected

        conn = _feedback_canonical_connection(engine)
        if conn is not None:
            try:
                from plastic_promise.core.memory_proposals import MemoryProposalStore

                proposal = MemoryProposalStore(conn).get(item_id)
            except Exception as exc:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "updated": False,
                                "item_id": item_id,
                                "feedback_type": feedback_type,
                                "reason": "proposal_gate_unavailable",
                                "error_class": exc.__class__.__name__,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if proposal is not None:
                actor = call_id = ""
                if feedback_type != "ignored":
                    authority, reason = _governed_feedback_authority(
                        _runtime_context,
                        artifact_project_id=proposal.get("project_id"),
                    )
                    if authority is None:
                        return _governed_feedback_failure(
                            item_id,
                            feedback_type,
                            reason,
                        )
                    actor, call_id = authority
                return _handle_memory_proposal_feedback(
                    engine,
                    args,
                    proposal=proposal,
                    actor=actor,
                    call_id=call_id,
                )
            try:
                synthesis_row = conn.execute(
                    "SELECT m.project_id FROM synthesis_artifacts AS sa "
                    "LEFT JOIN memories AS m ON m.id = sa.memory_id "
                    "WHERE sa.memory_id = ?",
                    (item_id,),
                ).fetchone()
            except Exception as exc:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "updated": False,
                                "item_id": item_id,
                                "feedback_type": feedback_type,
                                "reason": "synthesis_gate_unavailable",
                                "error_class": exc.__class__.__name__,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            if synthesis_row is not None:
                actor = call_id = ""
                if feedback_type != "ignored":
                    authority, reason = _governed_feedback_authority(
                        _runtime_context,
                        artifact_project_id=synthesis_row[0],
                    )
                    if authority is None:
                        return _governed_feedback_failure(
                            item_id,
                            feedback_type,
                            reason,
                        )
                    actor, call_id = authority
                return _handle_governed_synthesis_feedback(
                    engine,
                    args,
                    conn=conn,
                    actor=actor,
                    call_id=call_id,
                )

        apply_feedback = getattr(engine, "apply_ordinary_feedback", None)
        if not callable(apply_feedback):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": False,
                            "item_id": item_id,
                            "feedback_type": feedback_type,
                            "reason": "ordinary_feedback_api_required",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        feedback_kwargs: dict[str, Any] = {}
        if _runtime_context is not None:
            if conn is None:
                return _governed_feedback_failure(
                    item_id,
                    feedback_type,
                    "ordinary_feedback_gate_unavailable",
                )
            try:
                ordinary_row = conn.execute(
                    "SELECT project_id FROM memories WHERE id = ?",
                    (item_id,),
                ).fetchone()
            except Exception:
                return _governed_feedback_failure(
                    item_id,
                    feedback_type,
                    "ordinary_feedback_gate_unavailable",
                )
            if ordinary_row is None:
                return _governed_feedback_failure(
                    item_id,
                    feedback_type,
                    "ordinary_feedback_target_not_found",
                )
            expected_project_id = str(ordinary_row[0] or "").strip()
            authority, authority_reason = _governed_feedback_authority(
                _runtime_context,
                artifact_project_id=expected_project_id,
            )
            if authority is None:
                return _governed_feedback_failure(
                    item_id,
                    feedback_type,
                    authority_reason,
                )
            feedback_kwargs = {
                "expected_project_id": expected_project_id,
                "require_source_available": True,
            }
        try:
            canonical = apply_feedback(item_id, feedback_type, **feedback_kwargs)
        except Exception as exc:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": False,
                            "item_id": item_id,
                            "feedback_type": feedback_type,
                            "reason": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        success = float(canonical.get("worth_success", 0) or 0)
        failure = float(canonical.get("worth_failure", 0) or 0)
        observations = success + failure
        worth_score = 0.5 if observations == 0 else (success + 1.0) / (observations + 2.0)
        payload: dict[str, Any] = {
            "updated": True,
            "item_id": item_id,
            "feedback_type": feedback_type,
            "new_worth_score": worth_score,
            "observations": observations,
        }

        # Graph weights are a derived projection of the committed worth counters.
        try:
            engine.apply_edge_feedback_for_memory(item_id)
        except Exception as exc:
            payload.update(
                {
                    "committed": True,
                    "partial": True,
                    "graph_sync_pending": True,
                    "degraded": ["graph_feedback_sync"],
                    "error_class": exc.__class__.__name__,
                }
            )

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "feedback_apply"}, ensure_ascii=False),
            )
        ]
