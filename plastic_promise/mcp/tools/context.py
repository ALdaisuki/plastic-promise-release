"""MCP Context 工具 — 上下文域 4 个工具

工具列表:
- context_supply      : 【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包
- context_inject      : 手动向 EntityGraph 注入原则关联边或注册新实体节点
- context_graph       : 查询实体关联图谱数据
- auto_context_inject : 统一自动化上下文注入
"""

import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.recall_quality import (
    LIVE_TRACE_METADATA_KEY,
    evaluate_live_retrieval_quality,
)
from plastic_promise.core.retrieval_explain import (
    METADATA_KEY as RETRIEVAL_EXPLAIN_METADATA_KEY,
)
from plastic_promise.core.retrieval_explain import (
    build_retrieval_explain_snapshot,
)
from plastic_promise.launcher.runtime_mode import runtime_mode_status
from plastic_promise.mcp.response_projection import (
    build_diagnostics,
    compact_context_item,
    resolve_response_mode,
)
from plastic_promise.mcp.tools.supply_runner import (
    float_env,
    run_bounded_engine_supply,
    run_bounded_governed_retrieval_embedding,
)

logger = logging.getLogger(__name__)

_RETRIEVAL_DEGRADATION_REASON_RE = re.compile(r"\A[a-z][a-z0-9_]{0,95}\Z")
_COLLABORATION_CONTEXT_TIMEOUT_MAX_SEC = 1.0


def _collaboration_awareness_mode() -> str:
    mode = os.environ.get("PP_COLLABORATION_AWARENESS", "shadow").strip().casefold()
    return mode if mode in {"off", "shadow", "inject"} else "off"


_CONTEXT_SUPPLY_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("PP_CONTEXT_SUPPLY_MAX_WORKERS", "2"))),
    thread_name_prefix="context-supply",
)


def _stable_retrieval_embedding_reason(exc: Exception) -> str:
    """Return a safe, stable public reason for governed-route degradation."""

    candidate = getattr(exc, "code", "")
    if isinstance(candidate, str) and _RETRIEVAL_DEGRADATION_REASON_RE.fullmatch(candidate):
        return candidate
    return "retrieval_embedding_unavailable"


def _context_supply_item(item: Any) -> dict[str, Any]:
    """Project one memory-plane item into the bounded collaboration input."""

    return compact_context_item(
        {
            "id": getattr(item, "id", ""),
            "content": getattr(item, "content", ""),
            "relevance": getattr(item, "relevance", 0.0),
            "source": getattr(item, "source", ""),
            "freshness": getattr(item, "freshness", ""),
            "worth_score": getattr(item, "worth_score", 0.0),
        },
        content_chars=180,
    )


def _collaboration_memory_context(
    pack: Any,
    *,
    project_id: str,
    request_scope_id: str,
) -> dict[str, object]:
    """Build the bounded memory input used solely for collaboration relevance.

    This is deliberately a projection: the collaboration read plane cannot
    mutate memory scores, content, or canonical state through this seam.
    """

    return {
        "schema_version": "context-supply-memory-context/v1",
        "project_id": project_id,
        "request_scope_id": request_scope_id,
        "core": [_context_supply_item(item) for item in getattr(pack, "core", [])[:4]],
        "related": [_context_supply_item(item) for item in getattr(pack, "related", [])[:4]],
        "divergent": [_context_supply_item(item) for item in getattr(pack, "divergent", [])[:2]],
        "activated_principles": list(getattr(pack, "activated_principles", [])[:8]),
    }


async def _read_collaboration_context(
    runtime: Any | None,
    *,
    memory_context: dict[str, object],
    project_id: str,
    project_degraded: bool,
    request_scope_id: str,
    response_mode: str,
    args: dict[str, Any],
) -> Any | None:
    """Read an optional authenticated collaboration projection fail-open.

    PR4 owns only this injected read seam.  Opening a server runtime, binding
    identities, and advancing a durable feed remain PR5 responsibilities.
    """

    if runtime is None or project_degraded:
        return None

    from plastic_promise.collaboration.context_supply_runtime import (
        CollaborationContextReadRequest,
        CollaborationContextReadResult,
    )
    from plastic_promise.collaboration.contracts import ProjectScope

    try:
        cursor = args.get("collaboration_cursor")
        if cursor is not None and not isinstance(cursor, dict):
            raise ValueError("collaboration_cursor_invalid")
        request = CollaborationContextReadRequest(
            project=ProjectScope(project_id),
            request_scope_id=request_scope_id,
            response_mode=response_mode,
            after_sequence=(cursor or {}).get("after_sequence"),
            limit=args.get("collaboration_limit", 20),
        )
    except Exception:
        return CollaborationContextReadResult(
            state="rejected",
            reason="collaboration_context_request_invalid",
        )

    timeout = min(
        _COLLABORATION_CONTEXT_TIMEOUT_MAX_SEC,
        max(0.01, float_env("PP_COLLABORATION_CONTEXT_TIMEOUT_SEC", 0.25)),
    )
    try:
        return await asyncio.wait_for(
            runtime.compose(memory_context=memory_context, request=request),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        return CollaborationContextReadResult(
            state="degraded",
            reason="collaboration_context_timeout",
            retryable=True,
        )
    except Exception:
        return CollaborationContextReadResult(
            state="degraded",
            reason="collaboration_context_source_unavailable",
            retryable=True,
        )


def _degraded_context_response(
    *,
    reason: str,
    task_description: str,
    task_type: str,
    scope: str,
    request_scope: dict[str, Any],
    project_ctx: Any,
    call_id: str,
    response_mode: str,
    diagnostics_level: str,
) -> list[TextContent]:
    audit_metadata = {
        "engine_version": "context_supply-degraded",
        "task_type": task_type,
        "scope": scope,
        "minimum_result": "degraded_context",
        "degraded": True,
        "reason": reason,
        "request_scope": request_scope,
        "project_context": project_ctx.to_dict(),
        "trace": {
            "call_id": call_id,
            "request_scope_id": request_scope["request_scope_id"],
            "project_id": project_ctx.project_id,
        },
    }
    if response_mode in {"compact", "debug"}:
        payload = {
            "schema_version": "context-supply-response-v1",
            "response_mode": response_mode,
            "ephemeral": True,
            "core": [],
            "related": [],
            "divergent": [],
            "activated_principles": [],
            "request_scope_id": request_scope["request_scope_id"],
            "project_id": project_ctx.project_id,
            "trace": audit_metadata["trace"],
            "degraded": True,
            "minimum_result": "degraded_context",
            "error": reason,
            "diagnostics": build_diagnostics(
                call_id=call_id,
                audit=audit_metadata,
                level=diagnostics_level if response_mode == "debug" else "summary",
                max_bytes=3072,
            ),
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    prompt = "\n".join(
        [
            "## [CONTEXT_SUPPLY_DEGRADED]",
            f"- reason: {reason}",
            f"- task_type: {task_type}",
            f"- scope: {scope}",
            f"- request_scope_id: {request_scope['request_scope_id']}",
            f"- task: {task_description[:200]}",
        ]
    )
    return [TextContent(type="text", text=prompt)]


async def handle_context_supply(
    engine: Any,
    args: dict,
    *,
    _collaboration_runtime: Any | None = None,
) -> list[TextContent]:
    """Handle context_supply tool call.

    Core tool: calls ContextEngine.supply() and returns a three-layer
    structured context pack: Core/Related/Divergent layers.

    Args:
        engine: ContextEngine instance.
        args: {"task_description": str, "task_type"?: str, "scope"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        from plastic_promise.core.project_context import infer_project_context
        from plastic_promise.core.traceability import (
            defer_record_call_span,
            defer_record_degradation_event,
            new_call_id,
            utc_now,
        )
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        trace_started_at = utc_now()
        task_description = args["task_description"]
        task_type = args.get("task_type", "general")
        scope = args.get("scope", "global")
        retrieval_mode = str(args.get("retrieval_mode") or "")
        fusion_policy = str(args.get("fusion_policy") or "").strip()
        response_mode, diagnostics_level = resolve_response_mode(args, default="standard")
        debug = response_mode == "debug"
        request_scope = build_request_scope(args, "context_supply")
        project_ctx = infer_project_context(args)
        call_id = args.get("call_id") or new_call_id()
        embed_timeout = float_env("PP_CONTEXT_EMBED_TIMEOUT_SEC", 3.0)
        supply_timeout = float_env("PP_CONTEXT_SUPPLY_TIMEOUT_SEC", 12.0)

        embedding_degradation_reason = ""
        try:
            task_vector = await run_bounded_governed_retrieval_embedding(
                engine,
                text=task_description,
                project_id=project_ctx.project_id,
                executor=_CONTEXT_SUPPLY_EXECUTOR,
                timeout=embed_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("context_supply embedding timed out after %.2fs", embed_timeout)
            task_vector = []
            embedding_degradation_reason = "retrieval_embedding_timeout"
        except Exception as exc:
            # The ContextEngine-owned route is unavailable.  Pass an empty
            # vector so supply() selects its established text-only fallback;
            # never rediscover a legacy provider from this MCP boundary.
            task_vector = []
            embedding_degradation_reason = _stable_retrieval_embedding_reason(exc)

        try:
            supply_args = {
                "task_description": task_description,
                "task_vector": task_vector,
                "task_type": task_type,
                "scope": scope,
                "project_id": project_ctx.project_id,
                "project_policy": project_ctx.project_policy,
                "project_degraded": project_ctx.degraded,
                "retrieval_mode": retrieval_mode or None,
                "fusion_policy": fusion_policy or None,
                "debug": debug,
            }
            pack = await run_bounded_engine_supply(
                engine,
                supply_args,
                executor=_CONTEXT_SUPPLY_EXECUTOR,
                timeout=supply_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            reason = f"engine.supply timed out after {supply_timeout:.2f}s"
            logger.error("context_supply degraded: %s", reason)
            defer_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope["request_scope_id"],
                project_id=project_ctx.project_id,
                tool_name="context_supply",
                link_name="engine.supply",
                policy="timeout",
                level="error",
                fallback_used="degraded_context",
                minimum_result="degraded_context",
                metadata={
                    "timeout_sec": supply_timeout,
                    "task_type": task_type,
                    "scope": scope,
                    "retrieval_mode": retrieval_mode,
                    "fusion_policy": fusion_policy,
                },
            )
            defer_record_call_span(
                engine,
                call_id=call_id,
                parent_call_id=str(args.get("parent_call_id") or args.get("parent_call") or ""),
                request_scope_id=request_scope["request_scope_id"],
                stage_session_id=request_scope["stage_session_id"],
                flow_line_id=request_scope["flow_line_id"],
                project_id=project_ctx.project_id,
                tool_name="context_supply",
                status="degraded",
                degraded=True,
                metadata={
                    "task_type": task_type,
                    "scope": scope,
                    "retrieval_mode": retrieval_mode,
                    "fusion_policy": fusion_policy,
                    "reason": "engine_supply_timeout",
                },
                started_at=trace_started_at,
            )
            return _degraded_context_response(
                reason=reason,
                task_description=task_description,
                task_type=task_type,
                scope=scope,
                request_scope=request_scope,
                project_ctx=project_ctx,
                call_id=call_id,
                response_mode=response_mode,
                diagnostics_level=diagnostics_level,
            )
        from plastic_promise.mcp.tools.memory import (
            _sanitize_pack_for_project,
            _serialize_channel_evidence,
        )

        pack = _sanitize_pack_for_project(
            pack,
            project_ctx,
            engine,
            task_type=task_type,
        )
        pack.audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
        project_warnings = project_ctx.warning_list()
        embedding_degraded = bool(embedding_degradation_reason)
        # Preserve the established project-context warning behavior while
        # making governed embedding degradation equally observable.
        response_degraded = bool(project_warnings or embedding_degraded)
        retrieval_embedding = {
            "route": "governed" if task_vector else "text-only-degraded",
            "degraded": embedding_degraded,
            "reason": embedding_degradation_reason,
        }
        pack.audit_metadata["request_scope"] = request_scope
        pack.audit_metadata["project_context"] = project_ctx.to_dict()
        pack.audit_metadata["trace"] = {
            "call_id": call_id,
            "request_scope_id": request_scope["request_scope_id"],
            "project_id": project_ctx.project_id,
        }
        pack.audit_metadata["retrieval_embedding"] = retrieval_embedding
        pack.audit_metadata["degraded"] = response_degraded
        if embedding_degraded:
            pack.audit_metadata["fallback_used"] = "text-only-degraded"
            pack.audit_metadata["fallback_reason"] = embedding_degradation_reason
        pack.pipeline_stats = dict(getattr(pack, "pipeline_stats", {}) or {})
        pack.pipeline_stats["degraded"] = response_degraded
        if embedding_degraded:
            pack.pipeline_stats["degradation_state"] = "text-only-degraded"
            pack.pipeline_stats["fallback_reason"] = embedding_degradation_reason
        retrieval_explain = build_retrieval_explain_snapshot(pack)
        if project_warnings:
            pack.audit_metadata["warnings"] = project_warnings
            pack.audit_metadata["minimum_result"] = "project_restricted_context"
        span_metadata = {
            "task_type": task_type,
            "scope": scope,
            "project_policy": project_ctx.project_policy,
            "retrieval_mode": retrieval_mode,
            "fusion_policy": fusion_policy,
            "debug": debug,
            "response_mode": response_mode,
            "diagnostics_level": diagnostics_level,
            "warnings": project_warnings,
            "retrieval_embedding": retrieval_embedding,
            "degradation_reason": embedding_degradation_reason,
        }
        if retrieval_explain is not None:
            span_metadata[RETRIEVAL_EXPLAIN_METADATA_KEY] = retrieval_explain
        retrieval_quality = evaluate_live_retrieval_quality(
            list(getattr(pack, "core", []))
            + list(getattr(pack, "related", []))
            + list(getattr(pack, "divergent", [])),
            args.get("ground_truth"),
            request_scope_id=request_scope["request_scope_id"],
            runtime_mode=str(runtime_mode_status().get("mode") or "unknown"),
            tool_name="context_supply",
            task_type=task_type,
            scope=scope,
            project_id=project_ctx.project_id,
            project_policy=project_ctx.project_policy,
            retrieval_mode=retrieval_mode,
            fusion_policy=fusion_policy,
        )
        if retrieval_quality is not None:
            span_metadata[LIVE_TRACE_METADATA_KEY] = retrieval_quality
        if project_warnings:
            defer_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope["request_scope_id"],
                project_id=project_ctx.project_id,
                tool_name="context_supply",
                link_name="project_context",
                policy="project_restricted",
                level="warning",
                fallback_used="project_restricted_context",
                minimum_result="project_restricted_context",
                metadata={"warnings": project_warnings},
            )
        if embedding_degraded:
            defer_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope["request_scope_id"],
                project_id=project_ctx.project_id,
                tool_name="context_supply",
                link_name="retrieval_embedding",
                policy="text_only_degraded",
                level="warning",
                fallback_used="text-only-degraded",
                minimum_result="text_only_context",
                metadata={"reason": embedding_degradation_reason},
            )

        prompt = pack.to_prompt()
        if embedding_degraded:
            prompt = "\n".join(
                (
                    prompt,
                    "",
                    "## [RETRIEVAL_DEGRADED]",
                    "- route: text-only-degraded",
                    f"- reason: {embedding_degradation_reason}",
                )
            )
        memory_context = _collaboration_memory_context(
            pack,
            project_id=project_ctx.project_id,
            request_scope_id=request_scope["request_scope_id"],
        )
        collaboration_mode = _collaboration_awareness_mode()
        collaboration_result = await _read_collaboration_context(
            _collaboration_runtime if collaboration_mode != "off" else None,
            memory_context=memory_context,
            project_id=project_ctx.project_id,
            project_degraded=project_ctx.degraded,
            request_scope_id=request_scope["request_scope_id"],
            response_mode=response_mode,
            args=args,
        )
        if collaboration_result is not None:
            span_metadata["collaboration"] = {
                "mode": collaboration_mode,
                "state": collaboration_result.state,
                "reason": collaboration_result.reason,
                "retryable": collaboration_result.retryable,
                "replayed": collaboration_result.replayed,
                "canonical_memory_effect": "none",
            }
            if (
                collaboration_mode == "inject"
                and collaboration_result.projection is not None
                and collaboration_result.prompt_section
            ):
                prompt = "\n\n".join((prompt, collaboration_result.prompt_section))
        if response_mode == "standard":
            span_metadata["response_bytes"] = len(prompt.encode("utf-8"))
            defer_record_call_span(
                engine,
                call_id=call_id,
                parent_call_id=str(args.get("parent_call_id") or args.get("parent_call") or ""),
                request_scope_id=request_scope["request_scope_id"],
                stage_session_id=request_scope["stage_session_id"],
                flow_line_id=request_scope["flow_line_id"],
                project_id=project_ctx.project_id,
                tool_name="context_supply",
                status="success",
                degraded=response_degraded,
                metadata=span_metadata,
                started_at=trace_started_at,
            )
            return [TextContent(type="text", text=prompt)]

        channel_rankings, channel_states = _serialize_channel_evidence(pack)
        audit_metadata = getattr(pack, "audit_metadata", {}) or {}

        payload = {
            "schema_version": "context-supply-response-v1",
            "response_mode": response_mode,
            "ephemeral": True,
            "core": memory_context["core"],
            "related": memory_context["related"],
            "divergent": memory_context["divergent"],
            "activated_principles": getattr(pack, "activated_principles", [])[:8],
            "request_scope_id": request_scope["request_scope_id"],
            "project_id": project_ctx.project_id,
            "trace": audit_metadata.get("trace", {}),
            "audit_metadata": audit_metadata,
            "degraded": response_degraded,
            "degradation_reason": embedding_degradation_reason,
            "retrieval_embedding": retrieval_embedding,
            "warnings": project_warnings,
            "minimum_result": (
                "project_restricted_context"
                if project_ctx.degraded
                else "text_only_context"
                if embedding_degraded
                else ""
            ),
            "diagnostics": build_diagnostics(
                call_id=call_id,
                audit=audit_metadata,
                pipeline_stats=getattr(pack, "pipeline_stats", {}),
                per_item_stats=getattr(pack, "per_item_stats", []),
                channel_rankings=channel_rankings,
                channel_states=channel_states,
                level=diagnostics_level if response_mode == "debug" else "summary",
                max_bytes=3072,
            ),
        }
        if collaboration_result is not None:
            payload["collaboration_status"] = {
                "mode": collaboration_mode,
                "state": collaboration_result.state,
                "reason": collaboration_result.reason,
                "retryable": collaboration_result.retryable,
                "replayed": collaboration_result.replayed,
                "canonical_memory_effect": "none",
            }
            if collaboration_mode == "inject" and collaboration_result.projection is not None:
                payload["collaboration"] = collaboration_result.projection
        response_text = json.dumps(payload, ensure_ascii=False)
        span_metadata["response_bytes"] = len(response_text.encode("utf-8"))
        defer_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=str(args.get("parent_call_id") or args.get("parent_call") or ""),
            request_scope_id=request_scope["request_scope_id"],
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="context_supply",
            status="success",
            degraded=response_degraded,
            metadata=span_metadata,
            started_at=trace_started_at,
        )
        return [TextContent(type="text", text=response_text)]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "context_supply"}, ensure_ascii=False),
            )
        ]


async def handle_context_inject(engine: Any, args: dict) -> list[TextContent]:
    """Handle context_inject tool call.

    Manually injects principle-association edges into the EntityGraph,
    or registers new entity nodes (task, memory, code_module).

    Args:
        engine: ContextEngine instance.
        args: {"entity_type": str, "entity_id": str, "entity_name": str,
               "entity_description"?: str, "related_entities"?: list[str]}.

    Returns:
        list[TextContent]: MCP response with injected entity info.
    """
    try:
        entity_type = args.get("entity_type", "")
        entity_id = args.get("entity_id", "")
        entity_name = args.get("entity_name", "")
        entity_description = args.get("entity_description", "")
        related_entities = args.get("related_entities", [])
        metadata = args.get("metadata", {})

        # Validate required fields
        if not entity_type:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "entity_type is required"},
                        ensure_ascii=False,
                    ),
                )
            ]
        if not entity_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "entity_id is required"}, ensure_ascii=False),
                )
            ]

        from plastic_promise.core.behavior_graph import VALID_NODE_TYPES

        valid_types = VALID_NODE_TYPES
        if entity_type not in valid_types:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Unknown entity_type '{entity_type}'. Valid: {', '.join(sorted(valid_types))}"
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        # Route through existing PrincipleManager for principle type
        if entity_type == "principle":
            result = engine.register_entity(
                entity_type="principle",
                entity_id=entity_id,
                entity_name=entity_name,
                entity_description=entity_description,
                related_entities=related_entities,
                metadata=metadata if isinstance(metadata, dict) else {},
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "injected": {
                                "node_id": result["node_id"],
                                "type": entity_type,
                                "name": entity_name,
                                "is_new": result["is_new"],
                                "edges_created": result["edges_created"],
                            }
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

        # All other entity types: use engine.register_entity()
        try:
            result = engine.register_entity(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                entity_description=entity_description,
                related_entities=related_entities,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        except ValueError as ve:
            return [
                TextContent(type="text", text=json.dumps({"error": str(ve)}, ensure_ascii=False))
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "injected": result,
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
                text=json.dumps({"error": str(e), "tool": "context_inject"}, ensure_ascii=False),
            )
        ]


async def handle_context_graph(engine: Any, args: dict) -> list[TextContent]:
    """Handle context_graph tool call.

    Queries entity association graph: node list, edge relationships,
    multi-hop traversal, activation path visualization data.

    Args:
        engine: ContextEngine instance.
        args: {"start_node"?: str, "max_hops"?: int,
               "query_type"?: str}.

    Returns:
        list[TextContent]: MCP response with graph data.
    """
    try:
        query_type = args.get("query_type", "full_graph")
        start_node = args.get("start_node")
        max_hops = args.get("max_hops", 3)

        valid_queries = {"node_info", "traverse", "full_graph", "neighbors"}
        if query_type not in valid_queries:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Unknown query_type '{query_type}'. "
                            f"Valid: {', '.join(sorted(valid_queries))}"
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        result = engine.query_graph(
            query_type=query_type,
            start_node=start_node,
            max_hops=max_hops,
        )

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "context_graph"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# auto_context_inject — 统一自动化上下文注入
# ---------------------------------------------------------------------------


async def handle_auto_context_inject(engine: Any, args: dict) -> list[TextContent]:
    """Adapt MCP calls to provider-neutral before/after passive memory events."""
    from plastic_promise.core.memory_proposals import ProposalPolicyError, proposal_mode
    from plastic_promise.mcp.tools.request_scope import build_request_scope
    from plastic_promise.passive_memory import after_invoke, before_invoke
    from plastic_promise.passive_memory.coordinator import (
        passive_context_mode,
        passive_memory_mode,
    )

    values = dict(args or {})
    durable_lifecycle = values.pop("_durable_collaboration_lifecycle", None)
    if not isinstance(durable_lifecycle, dict):
        durable_lifecycle = {
            "state": "deferred",
            "reason": "durable_collaboration_lifecycle_not_composed",
        }
    event = str(values.get("event") or "before_invoke").strip().casefold()
    source = str(values.get("source") or "manual")
    task_description = str(values.get("task_description") or "")
    skill_name = f"auto_inject:{source}"
    entity_id = None
    tracked_principles: list[dict[str, Any]] = []
    errors: list[str] = []

    # SessionEnd is a transport lifecycle signal, not a passive-memory or
    # skill-tracking invocation.  Return the server-owned lifecycle result
    # directly so disabled capture/context gates cannot suppress cleanup and
    # no proposal, context preload, or synthetic skill session is created.
    if event == "session_end":
        durable = durable_lifecycle.get("state") == "durable"
        payload = {
            "entity_id": None,
            "skill_name": skill_name,
            "event": event,
            "status": "completed" if durable else "deferred",
            "reason": durable_lifecycle.get("reason"),
            "queued": False,
            "inject_memory_id": None,
            "persistent": durable,
            "durable_collaboration_lifecycle": durable_lifecycle,
            "errors": None,
            "partial": False,
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    disabled_reason = ""
    proposal_error = ""
    context_mode = passive_context_mode()
    memory_mode = passive_memory_mode()
    try:
        proposal_setting = proposal_mode()
    except ProposalPolicyError as exc:
        proposal_setting = "invalid"
        proposal_error = str(exc) or "unknown_proposal_mode"
    if event == "before_invoke" and context_mode == "off":
        disabled_reason = "passive_context_disabled"
    elif event == "after_invoke" and proposal_error:
        disabled_reason = "invalid_proposal_mode"
    elif event == "after_invoke" and memory_mode == "off":
        disabled_reason = "passive_memory_disabled"
    elif event == "after_invoke" and proposal_setting == "off":
        disabled_reason = "proposal_gate_closed"
    if disabled_reason:
        request_scope = build_request_scope(
            values,
            "passive_context" if event == "before_invoke" else "passive_memory",
        )
        configuration_errors = (
            [f"proposal_mode: {proposal_error}"]
            if event == "after_invoke" and proposal_error
            else None
        )
        payload: dict[str, Any] = {
            "entity_id": None,
            "skill_name": skill_name,
            "event": event,
            "status": "degraded" if configuration_errors else "skipped",
            "reason": disabled_reason,
            "mode": context_mode if event == "before_invoke" else memory_mode,
            "queued": False,
            "inject_memory_id": None,
            "request_scope_id": request_scope["request_scope_id"],
            "errors": configuration_errors,
            "partial": bool(configuration_errors),
            "durable_collaboration_lifecycle": durable_lifecycle,
        }
        if event == "before_invoke":
            payload.update(
                {
                    "ephemeral": True,
                    "injection": "",
                    "memory_ids": [],
                }
            )
        else:
            payload.update(
                {
                    "proposal_mode": proposal_setting,
                    "worker_scheduled": False,
                    "candidate_count": None,
                    "candidate_hashes": [],
                    "outbox_id": None,
                }
            )
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    try:
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        start_result = await handle_skill_session_start(
            engine,
            {
                "skill_name": skill_name,
                "task_description": task_description or event,
                "parent_entity_id": None,
                "record_memory": False,
            },
        )
        start_data = json.loads(start_result[0].text)
        entity_id = start_data.get("entity_id")
        tracked_principles = list(start_data.get("activated_principles") or [])
    except Exception as exc:
        errors.append(f"skill_session_start: {exc}")

    values["event"] = event
    try:
        if event == "before_invoke":
            payload = await before_invoke(engine, values)
            if not payload.get("principles") and tracked_principles:
                payload["principles"] = tracked_principles
            if not payload.get("principles"):
                try:
                    from plastic_promise.mcp.tools.principles import (
                        handle_principle_activate,
                    )

                    principle_result = await handle_principle_activate(
                        engine,
                        {
                            "task_type": values.get("task_type") or "general",
                            "task_description": task_description,
                            "domain_hint": values.get("domain_hint"),
                            "project_id": values.get("project_id"),
                        },
                    )
                    principle_data = json.loads(principle_result[0].text)
                    payload["principles"] = list(principle_data.get("activated") or [])
                    if principle_data.get("error"):
                        errors.append(f"principle_activate: {principle_data['error']}")
                except Exception as exc:
                    errors.append(f"principle_activate: {exc}")
        elif event == "after_invoke":
            payload = await after_invoke(engine, values)
        else:
            payload = {
                "event": event,
                "status": "rejected",
                "reason": "unknown_event",
                "queued": False,
                "inject_memory_id": None,
            }
            errors.append(f"unknown event: {event}")
    except Exception as exc:
        payload = {
            "event": event,
            "status": "degraded",
            "reason": "passive_memory_coordinator_failed",
            "queued": False,
            "inject_memory_id": None,
        }
        errors.append(f"passive_memory: {exc}")

    if entity_id:
        try:
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

            await handle_skill_session_complete(
                engine,
                {
                    "entity_id": entity_id,
                    "outcome": f"{event} completed",
                    "artifacts": [],
                },
            )
        except Exception as exc:
            errors.append(f"skill_session_complete: {exc}")

    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        **payload,
        "durable_collaboration_lifecycle": durable_lifecycle,
        "inject_memory_id": None,
        "errors": errors or payload.get("errors"),
        "partial": bool(errors or payload.get("partial") or payload.get("status") == "degraded"),
    }
    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]
