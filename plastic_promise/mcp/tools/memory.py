"""MCP Memory 工具 — 记忆域

公开工具:
- memory_recall, memory_store, memory_update, memory_forget
- memory_list, memory_gc, memory_correct
- memory_sync_files, memory_reclassify
"""

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalPolicyError,
    classify_proposal_candidates,
    has_trusted_internal_origin,
    proposal_mode,
    trusted_memory_origin,
)
from plastic_promise.core.project_context import infer_project_context
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import (
    engine_memory_is_governed_synthesis,
)
from plastic_promise.core.traceability import (
    build_envelope,
    new_call_id,
    record_outbox_event,
    safe_record_call_span,
    safe_record_degradation_event,
)

_trusted_memory_origin = trusted_memory_origin

# ---- Query result cache for memory_recall ----
_query_cache: dict[str, tuple[str, float]] = {}  # hash -> (json_result, timestamp)
_query_cache_lock = threading.Lock()
_QUERY_CACHE_SIZE = int(os.environ.get("PP_QUERY_CACHE_SIZE", "32"))
_QUERY_CACHE_TTL = float(os.environ.get("PP_QUERY_CACHE_TTL", "30"))  # seconds


def _cache_key(
    query: str,
    task_type: str,
    max_results: int,
    scope: str,
    debug: bool = False,
    strict: bool = False,
    request_scope_id: str = "",
    project_id: str = "",
    project_policy: str = "",
    retrieval_mode: str = "",
    memory_version: int | str | None = None,
    fusion_policy: str = "",
) -> str:
    raw = (
        f"{query}|{task_type}|{max_results}|{scope}|"
        f"debug={int(bool(debug))}|strict={int(bool(strict))}|"
        f"request_scope={request_scope_id}|project_id={project_id}|"
        f"project_policy={project_policy}|retrieval_mode={retrieval_mode}|"
        f"memory_version={memory_version}|fusion_policy={fusion_policy}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(
    query: str,
    task_type: str,
    max_results: int,
    scope: str,
    debug: bool = False,
    strict: bool = False,
    request_scope_id: str = "",
    project_id: str = "",
    project_policy: str = "",
    retrieval_mode: str = "",
    memory_version: int | str | None = None,
    fusion_policy: str = "",
) -> str | None:
    key = _cache_key(
        query,
        task_type,
        max_results,
        scope,
        debug,
        strict,
        request_scope_id,
        project_id,
        project_policy,
        retrieval_mode,
        memory_version,
        fusion_policy,
    )
    now = time.time()
    with _query_cache_lock:
        if key in _query_cache:
            result, ts = _query_cache[key]
            if now - ts < _QUERY_CACHE_TTL:
                return result
            del _query_cache[key]
    return None


def _cache_set(
    query: str,
    task_type: str,
    max_results: int,
    scope: str,
    result: str,
    debug: bool = False,
    strict: bool = False,
    request_scope_id: str = "",
    project_id: str = "",
    project_policy: str = "",
    retrieval_mode: str = "",
    memory_version: int | str | None = None,
    fusion_policy: str = "",
):
    key = _cache_key(
        query,
        task_type,
        max_results,
        scope,
        debug,
        strict,
        request_scope_id,
        project_id,
        project_policy,
        retrieval_mode,
        memory_version,
        fusion_policy,
    )
    now = time.time()
    with _query_cache_lock:
        if len(_query_cache) >= _QUERY_CACHE_SIZE:
            oldest = min(_query_cache, key=lambda k: _query_cache[k][1])
            del _query_cache[oldest]
        _query_cache[key] = (result, now)


def _current_memory_version(engine: Any) -> int | str:
    conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if conn is None:
        return "unavailable"
    try:
        from plastic_promise.core.synthesis_retrieval import read_memory_version

        return read_memory_version(conn)
    except Exception:
        return "invalid"


def _cached_context_item(row: dict[str, Any], layer: str):
    from plastic_promise.core.context_engine import ContextItem

    return ContextItem(
        id=str(row.get("id", "")),
        content=str(row.get("content", "")),
        relevance=float(row.get("relevance", 0.0) or 0.0),
        source=str(row.get("source", "")),
        freshness=str(row.get("freshness", "valid")),
        layer=layer,
        is_principle=bool(row.get("is_principle", False)),
        worth_score=float(row.get("worth_score", 0.0) or 0.0),
    )


def _cached_retrieval_plan(payload: dict[str, Any], *, task_type: str, scope: str):
    from plastic_promise.core.retrieval_planner import RetrievalPlan

    audit = payload.get("audit")
    audit = audit if isinstance(audit, dict) else {}
    plan_data = audit.get("retrieval_plan")
    plan_data = plan_data if isinstance(plan_data, dict) else {}
    budget = plan_data.get("budget") or payload.get("budget") or audit.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("cached_retrieval_plan_invalid")
    normalized_budget = {
        layer: max(0, int(budget.get(layer, 0)))
        for layer in ("core", "related", "divergent", "raw_evidence")
    }
    mode = str(plan_data.get("mode") or payload.get("mode") or audit.get("mode") or "")
    if not mode:
        raise ValueError("cached_retrieval_plan_invalid")
    channels = plan_data.get("channels")
    return RetrievalPlan(
        mode=mode,
        budget=normalized_budget,
        channels=list(channels) if isinstance(channels, list) else [],
        task_type=str(plan_data.get("task_type") or task_type),
        scope=str(plan_data.get("scope") or scope),
        project_policy=str(plan_data.get("project_policy") or "balanced"),
        reason=str(plan_data.get("reason") or "cache_revalidation"),
    )


def _revalidate_cached_recall(
    engine: Any,
    cached: str,
    *,
    task_type: str,
    scope: str,
    project_ctx: Any,
) -> str:
    from plastic_promise.core.context_engine import ContextPack

    payload = json.loads(cached)
    if not isinstance(payload, dict):
        raise ValueError("cached_payload_invalid")
    finalizer = getattr(engine, "_finalize_supply_pack", None)
    if not callable(finalizer):
        raise ValueError("cached_gate_unavailable")

    original_layers: dict[str, list[dict[str, Any]]] = {}
    pack = ContextPack()
    for layer in ("core", "related", "divergent"):
        rows = payload.get(layer, [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("cached_payload_invalid")
        original_layers[layer] = rows
        setattr(pack, layer, [_cached_context_item(row, layer) for row in rows])

    audit = payload.get("audit")
    pack.audit_metadata = dict(audit) if isinstance(audit, dict) else {}
    recommendations = payload.get("context_recommendations")
    if isinstance(recommendations, list):
        pack.audit_metadata["context_recommender"] = {
            "task_type": task_type,
            "recommendations": recommendations,
            "hard_constraints": "preserved_before_ranking",
        }
    stats = payload.get("per_item_stats")
    pack.per_item_stats = list(stats) if isinstance(stats, list) else []
    pipeline_stats = payload.get("pipeline_stats")
    pack.pipeline_stats = dict(pipeline_stats) if isinstance(pipeline_stats, dict) else {}
    if "gap_signal" in payload:
        pack.gap_signal = payload.get("gap_signal")

    retrieval_plan = _cached_retrieval_plan(payload, task_type=task_type, scope=scope)
    pack = finalizer(
        pack,
        retrieval_plan,
        task_type=task_type,
        project_id=project_ctx.project_id,
        project_policy=project_ctx.project_policy,
    )
    pack = _sanitize_pack_for_project(
        pack,
        project_ctx,
        engine,
        task_type=task_type,
    )

    for layer in ("core", "related", "divergent"):
        original_by_id = {str(row.get("id", "")): row for row in original_layers[layer]}
        payload[layer] = [
            original_by_id[item.id] for item in getattr(pack, layer) if item.id in original_by_id
        ]
    payload["audit"] = pack.audit_metadata
    payload["raw_evidence"] = pack.audit_metadata.get("raw_evidence", [])
    recommender = pack.audit_metadata.get("context_recommender")
    payload["context_recommendations"] = (
        recommender.get("recommendations", []) if isinstance(recommender, dict) else []
    )
    if "per_item_stats" in payload:
        payload["per_item_stats"] = pack.per_item_stats
    if "pipeline_stats" in payload:
        payload["pipeline_stats"] = pack.pipeline_stats
    if "gap_signal" in payload:
        payload["gap_signal"] = pack.gap_signal
    payload["total_items"] = pack.total_items

    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in (
            "core",
            "related",
            "divergent",
            "audit",
            "raw_evidence",
            "context_recommendations",
            "total_items",
        ):
            nested[key] = payload[key]
        if "per_item_stats" in nested:
            nested["per_item_stats"] = pack.per_item_stats
        if "pipeline_stats" in nested:
            nested["pipeline_stats"] = pack.pipeline_stats
        if "gap_signal" in nested:
            nested["gap_signal"] = pack.gap_signal

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_federation_signals(pack, domain_hint, engine, federation):
    """Generate cross-domain federation signals on retrieval."""
    if not federation or not domain_hint or domain_hint == "all":
        return []
    dm = getattr(engine, "_dm", None)
    if dm is None:
        return []
    signals = []
    seen = set()
    for item in pack.core + pack.related:
        item_domain = getattr(item, "domain", "") or ""
        if item_domain and item_domain != domain_hint and item_domain != "all":
            key = (item_domain, domain_hint)
            if key not in seen:
                seen.add(key)
                signals.append(
                    {
                        "source": item_domain,
                        "target": domain_hint,
                        "signal": dm.generate_signal(
                            item_domain,
                            domain_hint,
                            getattr(item, "id", "?"),
                            agent_id=getattr(engine, "_agent_owner", "")
                            or os.environ.get("AGENT_OWNER", ""),
                        ),
                    }
                )
    return signals


def _canonical_project_record(engine, item_id: str) -> tuple[dict | None, str]:
    from plastic_promise.core.context_engine import resolve_project_metadata

    return resolve_project_metadata(engine, item_id)


def _item_meta(item, engine=None) -> dict:
    item_meta = getattr(item, "metadata", None)
    merged = dict(item_meta) if isinstance(item_meta, dict) else {}
    memory, state = _canonical_project_record(engine, str(getattr(item, "id", "") or ""))
    if state in {"canonical", "runtime"} and isinstance(memory, dict):
        merged.update(memory)
        return merged

    merged.update(
        {
            key: getattr(item, key)
            for key in ("project_id", "visibility", "source_class")
            if hasattr(item, key)
        }
    )
    return merged


def _project_allowed(item, project_ctx, layer: str, engine=None) -> bool:
    item_id = str(getattr(item, "id", "") or "")
    canonical_record, state = _canonical_project_record(engine, item_id)
    if state == "error":
        return False
    is_noncanonical = item_id.startswith(
        ("principle:", "code:", "mcp_tool:", "task_state:", "bilingual_synonym:")
    )
    if state == "canonical_missing":
        return is_noncanonical
    if state == "runtime_missing" and is_noncanonical:
        return True
    meta = _item_meta(item, engine)
    if state in {"canonical", "runtime"} and canonical_record is not None:
        meta = {**meta, **canonical_record}
    item_project = meta.get("project_id", "project:legacy-global")
    visibility = meta.get("visibility", "project")
    source_class = meta.get("source_class", "experience")

    if source_class in {"telemetry", "prompt"} and layer in {"core", "related"}:
        return False
    if project_ctx.degraded and layer in {"core", "related"}:
        return visibility == "global"
    if layer == "divergent" and project_ctx.project_policy != "strict":
        return visibility in {"shared", "global"} or item_project == project_ctx.project_id
    return item_project == project_ctx.project_id or visibility == "global"


def _project_value_mentions(
    value: Any, blocked_ids: set[str], blocked_content: tuple[str, ...]
) -> bool:
    if isinstance(value, str):
        return value in blocked_ids or any(
            content and content in value for content in blocked_content
        )
    if isinstance(value, dict):
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id in blocked_ids:
            return True
        return any(
            _project_value_mentions(item, blocked_ids, blocked_content) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_project_value_mentions(item, blocked_ids, blocked_content) for item in value)
    return False


def _sanitize_project_value(
    value: Any,
    blocked_ids: set[str],
    blocked_content: tuple[str, ...],
) -> Any:
    if isinstance(value, list):
        return [
            _sanitize_project_value(item, blocked_ids, blocked_content)
            for item in value
            if not _project_value_mentions(item, blocked_ids, blocked_content)
        ]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_project_value(item, blocked_ids, blocked_content)
            for item in value
            if not _project_value_mentions(item, blocked_ids, blocked_content)
        )
    if isinstance(value, dict):
        return {
            key: _sanitize_project_value(item, blocked_ids, blocked_content)
            for key, item in value.items()
            if isinstance(item, (dict, list, tuple))
            or not _project_value_mentions(item, blocked_ids, blocked_content)
        }
    return value


def _sanitize_pack_for_project(pack, project_ctx, engine=None, *, task_type: str = ""):
    """Apply MCP project visibility to every public pack surface."""
    blocked_items = []
    for layer in ("core", "related", "divergent"):
        original = list(getattr(pack, layer, []) or [])
        admitted = [item for item in original if _project_allowed(item, project_ctx, layer, engine)]
        admitted_ids = {str(getattr(item, "id", "") or "") for item in admitted}
        blocked_items.extend(
            item for item in original if str(getattr(item, "id", "") or "") not in admitted_ids
        )
        setattr(pack, layer, admitted)

    blocked_ids = {
        str(getattr(item, "id", "") or "") for item in blocked_items if getattr(item, "id", "")
    }
    blocked_content = tuple(str(getattr(item, "content", "") or "") for item in blocked_items)
    visible_items = [
        *list(getattr(pack, "core", []) or []),
        *list(getattr(pack, "related", []) or []),
        *list(getattr(pack, "divergent", []) or []),
    ]
    visible_ids = {str(getattr(item, "id", "") or "") for item in visible_items}

    original_audit = dict(getattr(pack, "audit_metadata", {}) or {})
    original_raw = original_audit.get("raw_evidence")
    raw_rows = original_raw if isinstance(original_raw, list) else []
    raw_by_id = {
        str(row.get("id", "")): row for row in raw_rows if isinstance(row, dict) and row.get("id")
    }
    audit = dict(original_audit)
    audit = _sanitize_project_value(audit, blocked_ids, blocked_content)
    budget = audit.get("budget")
    if not isinstance(budget, dict):
        retrieval_plan = audit.get("retrieval_plan")
        budget = retrieval_plan.get("budget", {}) if isinstance(retrieval_plan, dict) else {}
    try:
        raw_limit = max(0, int(budget.get("raw_evidence", 10)))
    except (AttributeError, TypeError, ValueError):
        raw_limit = 10
    audit["raw_evidence"] = [
        {
            "id": str(getattr(item, "id", "") or ""),
            "source": str(
                raw_by_id.get(str(getattr(item, "id", "") or ""), {}).get("source")
                or getattr(item, "source", "")
                or ""
            ),
            "score": round(float(getattr(item, "relevance", 0.0) or 0.0), 6),
            "content": str(getattr(item, "content", "") or "")[:300],
        }
        for item in sorted(
            visible_items,
            key=lambda candidate: float(getattr(candidate, "relevance", 0.0) or 0.0),
            reverse=True,
        )[:raw_limit]
    ]

    from plastic_promise.core.context_recommender import recommend_context_items

    recommendations = recommend_context_items(visible_items, task_type=task_type)
    audit["context_recommender"] = {
        "task_type": task_type,
        "recommendations": recommendations,
        "hard_constraints": "preserved_before_ranking",
    }
    pack.audit_metadata = audit
    pack.per_item_stats = [
        row
        for row in list(getattr(pack, "per_item_stats", []) or [])
        if isinstance(row, dict) and str(row.get("id", "")) in visible_ids
    ]
    pack.pipeline_stats = _sanitize_project_value(
        dict(getattr(pack, "pipeline_stats", {}) or {}),
        blocked_ids,
        blocked_content,
    )
    if getattr(pack, "gap_signal", None) is not None:
        gap_signal = pack.gap_signal
        if is_dataclass(gap_signal):
            gap_type = type(gap_signal)
            sanitized_gap = _sanitize_project_value(
                asdict(gap_signal),
                blocked_ids,
                blocked_content,
            )
            try:
                pack.gap_signal = gap_type(**sanitized_gap)
            except Exception:
                pack.gap_signal = sanitized_gap
        else:
            pack.gap_signal = _sanitize_project_value(
                gap_signal,
                blocked_ids,
                blocked_content,
            )
    return pack


def _origin_scope(meta: dict, project_ctx) -> str:
    item_project = meta.get("project_id", "project:legacy-global")
    visibility = meta.get("visibility", "project")
    if visibility == "global":
        return "global"
    if visibility == "shared":
        return "shared"
    if item_project == project_ctx.project_id:
        return "project"
    return "cross_project"


def _serialize_context_item(item, project_ctx, engine=None, include_life: bool = False) -> dict:
    meta = _item_meta(item, engine)
    payload = {
        "id": item.id,
        "content": item.content[:500],
        "relevance": item.relevance,
        "source": getattr(item, "source", ""),
        "project_id": meta.get("project_id", "project:legacy-global"),
        "visibility": meta.get("visibility", "project"),
        "origin_scope": _origin_scope(meta, project_ctx),
    }
    if include_life:
        payload.update(
            {
                "freshness": getattr(item, "freshness", "valid"),
                "worth_score": getattr(item, "worth_score", 0.0),
            }
        )
    return payload


def _parent_call_id(args: dict) -> str:
    return str(args.get("parent_call_id") or args.get("parent_call") or "")


# ---- memory_recall ----
async def handle_memory_recall(engine: Any, args: dict) -> list[TextContent]:
    """Hybrid memory retrieval: embed query -> ContextEngine.supply() -> ContextPack JSON.

    Calls ContextEngine.supply() for hybrid retrieval (vector + BM25 + RRF fusion
    + symbolic rules + graph traversal), returns three-layer context pack.

    Uses a short-lived query cache (PP_QUERY_CACHE_SIZE=32, PP_QUERY_CACHE_TTL=30s)
    to avoid redundant embedding + retrieval for repeated queries.
    """
    try:
        from plastic_promise.adaptive_retrieval import should_retrieve
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        query = args["query"]
        request_scope = build_request_scope(args, "memory_recall")
        request_scope_id = request_scope["request_scope_id"]
        if not should_retrieve(query):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "skipped": True,
                            "reason": "adaptive_retrieval",
                            "query": query[:100],
                            "request_scope": request_scope,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        task_type = args.get("task_type", "general")
        max_results = args.get("max_results", 20)
        scope = args.get("scope", "global")
        strict = args.get("strict", False)
        debug = bool(args.get("debug", False))
        retrieval_mode = str(args.get("retrieval_mode") or "")
        fusion_policy = str(args.get("fusion_policy") or "").strip()
        project_ctx = infer_project_context(args)
        memory_version = _current_memory_version(engine)
        call_id = args.get("call_id") or new_call_id()
        pack = args.get("pack")

        # Check query cache
        cached = _cache_get(
            query,
            task_type,
            max_results,
            scope,
            debug,
            strict,
            request_scope_id,
            project_ctx.project_id,
            project_ctx.project_policy,
            retrieval_mode,
            memory_version,
            fusion_policy,
        )
        if cached is not None:
            try:
                cached = _revalidate_cached_recall(
                    engine,
                    cached,
                    task_type=task_type,
                    scope=scope,
                    project_ctx=project_ctx,
                )
            except Exception:
                cached = None
        if cached is not None:
            degraded = False
            try:
                degraded = bool(json.loads(cached).get("degraded", False))
            except Exception:
                degraded = False
            safe_record_call_span(
                engine,
                call_id=call_id,
                parent_call_id=_parent_call_id(args),
                request_scope_id=request_scope_id,
                stage_session_id=request_scope["stage_session_id"],
                flow_line_id=request_scope["flow_line_id"],
                project_id=project_ctx.project_id,
                tool_name="memory_recall",
                status="success",
                degraded=degraded,
                metadata={
                    "cache_hit": True,
                    "task_type": task_type,
                    "scope": scope,
                    "project_policy": project_ctx.project_policy,
                },
            )
            return [TextContent(type="text", text=cached)]

        from plastic_promise.core.embedder import FallbackEmbedder, get_embedder

        domain_hint = args.get("domain_hint")
        federation = args.get("federation", True)

        try:
            embedder = get_embedder(fallback_on_error=False)
            vec = await embedder.aembed(query)
        except Exception:
            embedder = FallbackEmbedder()
            vec = await embedder.aembed(query)
        try:
            pack = engine.supply(
                query,
                vec,
                task_type,
                scope,
                debug=debug,
                project_id=project_ctx.project_id,
                project_policy=project_ctx.project_policy,
                project_degraded=project_ctx.degraded,
                retrieval_mode=retrieval_mode or None,
                fusion_policy=fusion_policy or None,
            )
        except TypeError:
            pack = engine.supply(query, vec, task_type, scope, debug=debug)

        pack = _sanitize_pack_for_project(
            pack,
            project_ctx,
            engine,
            task_type=task_type,
        )
        audit_metadata = dict(getattr(pack, "audit_metadata", {}) or {})
        audit_metadata["request_scope"] = request_scope
        trace = {
            "call_id": call_id,
            "request_scope_id": request_scope_id,
            "project_id": project_ctx.project_id,
        }
        core_items = list(pack.core)
        related_items = list(pack.related)
        divergent_items = list(pack.divergent)
        recommender = audit_metadata.get("context_recommender")
        context_recommendations = (
            recommender.get("recommendations", []) if isinstance(recommender, dict) else []
        )

        response_payload = {
            "core": [
                _serialize_context_item(i, project_ctx, engine, include_life=True)
                for i in core_items[:max_results]
            ],
            "related": [
                _serialize_context_item(i, project_ctx, engine) for i in related_items[:max_results]
            ],
            "divergent": [
                _serialize_context_item(i, project_ctx, engine)
                for i in divergent_items[:max_results]
            ],
            "activated_principles": pack.activated_principles,
            "domain_hint": domain_hint,
            "project_id": project_ctx.project_id,
            "project_policy": project_ctx.project_policy,
            "project_context": project_ctx.to_dict(),
            "trace": trace,
            "request_scope_id": request_scope_id,
            "request_scope": request_scope,
            "mode": audit_metadata.get("mode")
            or (audit_metadata.get("retrieval_plan") or {}).get("mode"),
            "budget": audit_metadata.get("budget")
            or (audit_metadata.get("retrieval_plan") or {}).get("budget", {}),
            "raw_evidence": audit_metadata.get("raw_evidence", []),
            "context_recommendations": context_recommendations,
            "federation_signals": _generate_federation_signals(
                pack, domain_hint, engine, federation
            ),
            "total_items": pack.total_items,
            "audit": audit_metadata,
        }
        if debug:
            channel_rankings, channel_states = _serialize_channel_evidence(pack)
            response_payload["pipeline_stats"] = pack.pipeline_stats
            response_payload["per_item_stats"] = pack.per_item_stats[:max_results]
            response_payload["channel_rankings"] = channel_rankings
            response_payload["channel_states"] = channel_states

        project_warnings = project_ctx.warning_list()
        envelope = build_envelope(
            data=dict(response_payload),
            trace=trace,
            warnings=project_warnings,
            minimum_result="project_restricted_context" if project_ctx.degraded else "",
        )
        response_payload.update(envelope)
        if strict and not core_items:
            response_payload.update(
                {
                    "strict": True,
                    "core": [],
                    "data": {
                        **response_payload["data"],
                        "core": [],
                    },
                    "message": "no matches in strict mode",
                }
            )

        result_json = json.dumps(
            response_payload,
            ensure_ascii=False,
            indent=2,
        )

        safe_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=_parent_call_id(args),
            request_scope_id=request_scope_id,
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="memory_recall",
            status="success",
            degraded=bool(response_payload.get("degraded", False)),
            metadata={
                "cache_hit": False,
                "task_type": task_type,
                "scope": scope,
                "strict": bool(strict),
                "debug": bool(debug),
                "max_results": max_results,
                "project_policy": project_ctx.project_policy,
                "retrieval_mode": retrieval_mode,
                "fusion_policy": fusion_policy,
                "warnings": project_warnings,
            },
        )
        if project_warnings:
            safe_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope_id,
                project_id=project_ctx.project_id,
                tool_name="memory_recall",
                link_name="project_context",
                policy="project_restricted",
                level="warning",
                fallback_used="project_restricted_context",
                minimum_result="project_restricted_context",
                metadata={"warnings": project_warnings},
            )

        # Cache the result
        _cache_set(
            query,
            task_type,
            max_results,
            scope,
            result_json,
            debug,
            strict,
            request_scope_id,
            project_ctx.project_id,
            project_ctx.project_policy,
            retrieval_mode,
            _current_memory_version(engine),
            fusion_policy,
        )

        return [TextContent(type="text", text=result_json)]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_recall"}, ensure_ascii=False),
            )
        ]


# ---- memory_store ----
def _canonical_sqlite_connection(engine: Any):
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    return getattr(sqlite, "_conn", None)


def _proposal_trace(call_id: str, request_scope: dict, project_id: str) -> dict[str, str]:
    return {
        "call_id": call_id,
        "request_scope_id": str(request_scope.get("request_scope_id") or ""),
        "project_id": project_id,
    }


def _proposal_response(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _route_user_memory_proposal(
    engine: Any,
    args: dict,
    *,
    content: str,
    project_ctx: Any,
    call_id: str,
    request_scope: dict,
) -> tuple[list[TextContent] | None, dict[str, Any] | None]:
    """Apply off/shadow/on before any generic write or fallback outbox."""
    try:
        mode = proposal_mode()
    except ProposalPolicyError as exc:
        return _proposal_response(
            {
                "stored": False,
                "status": "rejected",
                "reason": str(exc),
                "trace": _proposal_trace(call_id, request_scope, project_ctx.project_id),
            }
        ), None
    if mode == "off" or has_trusted_internal_origin(args):
        return None, None

    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    classification = classify_proposal_candidates(
        content,
        project_id=project_ctx.project_id,
        visibility=project_ctx.visibility,
        origin_role="user",
        origin_turn_hash=str(args.get("origin_turn_hash") or digest),
        origin_call_id=call_id,
        origin_visibility=project_ctx.visibility,
        metadata={},
    )
    shadow = {
        "categories": [candidate.category for candidate in classification.candidates],
        "content_hash": digest,
        "reason_codes": list(classification.reason_codes),
        "secret_detected": "secret_detected" in classification.reason_codes,
        "visibility_allowed": classification.decision == "propose",
        "would_propose": classification.decision == "propose",
    }
    if mode == "shadow":
        return None, shadow
    if classification.decision != "propose":
        reason = classification.reason_codes[0]
        return _proposal_response(
            {
                "stored": False,
                "status": "rejected",
                "reason": reason,
                "trace": _proposal_trace(call_id, request_scope, project_ctx.project_id),
            }
        ), None

    try:
        conn = _canonical_sqlite_connection(engine)
        if conn is None:
            raise ProposalPolicyError("canonical_store_unavailable")
        rows = MemoryProposalStore(conn).create_many(classification.candidates)
        if not rows or any(row.get("status") != "pending" for row in rows):
            raise ProposalPolicyError("proposal_not_pending")
        return _proposal_response(
            {
                "stored": False,
                "status": "pending",
                "proposal_ids": [row["proposal_id"] for row in rows],
                "categories": [row["category"] for row in rows],
                "trace": _proposal_trace(call_id, request_scope, project_ctx.project_id),
            }
        ), None
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ProposalPolicyError) else "proposal_store_failed"
        return _proposal_response(
            {
                "stored": False,
                "status": "rejected",
                "reason": reason,
                "trace": _proposal_trace(call_id, request_scope, project_ctx.project_id),
            }
        ), None


def _synthesis_artifact_payload(artifact: Any) -> dict[str, Any]:
    return {
        "memory_id": artifact.memory_id,
        "status": artifact.status,
        "revision": artifact.revision,
        "support_count": artifact.support_count,
        "source_fingerprint": artifact.source_fingerprint,
        "validity_scope": artifact.validity_scope,
        "project_id": artifact.project_id,
        "visibility": artifact.visibility,
    }


def _synthesis_store_trace(call_id: str, request_scope: dict, project_id: str) -> dict:
    return {
        "call_id": call_id,
        "request_scope_id": request_scope["request_scope_id"],
        "stage_session_id": request_scope["stage_session_id"],
        "flow_line_id": request_scope["flow_line_id"],
        "project_id": project_id,
    }


def _handle_governed_synthesis_store(
    engine: Any,
    args: dict,
    *,
    project_ctx: Any,
    call_id: str,
    request_scope: dict,
) -> list[TextContent]:
    """Route synthesis lifecycle writes without touching the generic pipeline/outbox."""
    trace = _synthesis_store_trace(call_id, request_scope, project_ctx.project_id)
    try:
        from plastic_promise.core.synthesis import SynthesisConflict, SynthesisStore

        conn = _canonical_sqlite_connection(engine)
        if conn is None:
            raise SynthesisConflict("canonical_store_unavailable")
        store = SynthesisStore(conn, engine=engine)
        expected_revision = args.get("expected_revision")
        source_ids = args.get("source_ids") or []
        synthesis_key = str(args.get("synthesis_key") or "")
        metadata = args.get("metadata_json")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        common = {
            "validity_scope": str(args.get("validity_scope") or ""),
            "project_id": project_ctx.project_id,
            "visibility": project_ctx.visibility,
            "actor": str(args.get("actor") or ""),
            "call_id": call_id,
            "automatic": bool(args.get("automatic", False)),
            "reuse_signal": bool(args.get("reuse_signal", False)),
            "audit_synthesis": bool(metadata.get("audit_synthesis", False)),
            "metadata": metadata,
        }
        if expected_revision is None:
            artifact = store.create_draft(
                str(args.get("content") or ""),
                source_ids,
                synthesis_key=synthesis_key,
                **common,
            )
        else:
            if type(expected_revision) is not int or expected_revision < 1:
                raise SynthesisConflict("invalid_expected_revision")
            row = conn.execute(
                "SELECT memory_id FROM synthesis_artifacts WHERE synthesis_key = ?",
                (synthesis_key.strip(),),
            ).fetchone()
            if row is None:
                raise SynthesisConflict("synthesis_not_found")
            artifact = store.refresh(
                str(row[0]),
                str(args.get("content") or ""),
                source_ids,
                expected_revision,
                **common,
            )

        data = {
            "stored": artifact is not None,
            "memory_type": "synthesis",
            "status": "shadow" if artifact is None else artifact.status,
            "trace": trace,
        }
        if artifact is not None:
            data.update(_synthesis_artifact_payload(artifact))
        response = dict(data)
        response.update(
            build_envelope(
                data=data,
                trace=trace,
                warnings=project_ctx.warning_list(),
                fallback_used=[],
            )
        )
        safe_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=_parent_call_id(args),
            request_scope_id=request_scope["request_scope_id"],
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="memory_store",
            status="success",
            degraded=bool(project_ctx.warning_list()),
            metadata={
                "memory_type": "synthesis",
                "status": data["status"],
                "revision": data.get("revision"),
            },
        )
        return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]
    except SynthesisConflict as exc:
        data = {
            "stored": False,
            "memory_type": "synthesis",
            "reason": str(exc),
            "trace": trace,
        }
        response = dict(data)
        response.update(
            build_envelope(
                data=data,
                trace=trace,
                success=False,
                warnings=[],
                fallback_used=[],
            )
        )
        safe_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=_parent_call_id(args),
            request_scope_id=request_scope["request_scope_id"],
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="memory_store",
            status="rejected",
            degraded=False,
            metadata={"memory_type": "synthesis", "reason": str(exc)},
        )
        return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]
    except Exception as exc:
        data = {
            "stored": False,
            "memory_type": "synthesis",
            "reason": "synthesis_operation_failed",
            "error_class": exc.__class__.__name__,
            "trace": trace,
        }
        warnings = ["governed synthesis operation failed without fallback"]
        response = dict(data)
        response.update(
            build_envelope(
                data=data,
                trace=trace,
                success=False,
                warnings=warnings,
                fallback_used=[],
            )
        )
        safe_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=_parent_call_id(args),
            request_scope_id=request_scope["request_scope_id"],
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="memory_store",
            status="error",
            degraded=True,
            metadata={
                "memory_type": "synthesis",
                "reason": "synthesis_operation_failed",
                "error_class": exc.__class__.__name__,
            },
        )
        return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]


async def handle_memory_store(engine: Any, args: dict) -> list[TextContent]:
    """Store a memory: create MemoryRecord, persist to SQLite, embed and index.

    Args:
        engine: ContextEngine instance.
        args: {"content": str, "memory_type": str, "source"?: str,
               "scope"?: str, "entity_ids"?: list[str]}.

    Returns:
        list[TextContent]: MCP response with stored memory metadata.
    """
    try:
        from plastic_promise.core.noise_filter import is_noise
        from plastic_promise.mcp.tools.request_scope import build_request_scope

        content = args.get("content", "")
        if not content or (isinstance(content, str) and not content.strip()):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "stored": False,
                            "reason": "empty_content",
                            "note": "memory_store requires non-empty 'content'",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        # Health check: detect MCP server availability (non-blocking, cached)
        server_ok = getattr(engine, "_server_alive", True)

        memory_type = args.get("memory_type", "experience")
        source = args.get("source", "user")
        scope = args.get("scope", "global")
        entity_ids = args.get("entity_ids", [])
        project_ctx = infer_project_context(args)
        call_id = args.get("call_id") or new_call_id()
        request_scope = build_request_scope(args, "memory_store")
        custom_tags = args.get("tags", [])  # 用户指定的标签 (task:pending 等)

        # Auto-extract entity links from content (原则 #6 数据流驱动)
        if str(memory_type).strip().casefold() == "synthesis":
            return _handle_governed_synthesis_store(
                engine,
                args,
                project_ctx=project_ctx,
                call_id=call_id,
                request_scope=request_scope,
            )

        proposal_terminal, proposal_shadow = _route_user_memory_proposal(
            engine,
            args,
            content=str(content),
            project_ctx=project_ctx,
            call_id=call_id,
            request_scope=request_scope,
        )
        if proposal_terminal is not None:
            return proposal_terminal

        if is_noise(content):
            payload = {
                "stored": False,
                "reason": "noise_filtered",
                "content_preview": content[:100],
            }
            if proposal_shadow is not None:
                payload["proposal_shadow"] = proposal_shadow
            return [
                TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False),
                )
            ]

        extracted = _extract_entity_ids(content, engine)
        all_entities = list(set(entity_ids + extracted))

        # ALL memories go through fuzzy buffer pipeline first:
        #   raw → tagged(关键词) → classified(大类分L1/L3) → embedded(细分向量) → 迁移主池
        # This is the standard path, not a fallback. (原则 #4 上下文驱动, #10 自演化闭环)
        fb = _get_fuzzy_buffer(engine)
        _max_llm = args.get("max_llm_calls", 3)
        fuzzy_id = fb.store_urgent(
            content,
            memory_type,
            source,
            entity_ids=all_entities,
            custom_tags=custom_tags,
            max_llm_calls=_max_llm,
            skip_embed=(_max_llm == 0),
            project_id=project_ctx.project_id,
            visibility=project_ctx.visibility,
            source_class=project_ctx.source_class,
            created_by_call_id=call_id,
            origin_kind=args.get("origin_kind", "tool_call"),
            origin_uri=args.get("origin_uri", "mcp://memory_store"),
            origin_ref=args.get("origin_ref", ""),
            origin_hash=args.get("origin_hash", ""),
            parent_memory_ids=args.get("parent_memory_ids", []),
            metadata_json=args.get("metadata_json", {}),
        )

        # Process through pipeline immediately (同步处理——大类分完就入池)
        result = fb.process_pipeline()
        pipeline_counts = result.get("pipeline", {}) if isinstance(result, dict) else {}
        migration_outcomes = (
            result.get("migration_outcomes", {}) if isinstance(result, dict) else {}
        )
        outcomes_reported = isinstance(result, dict) and "migration_outcomes" in result
        submitted_outcome = (
            migration_outcomes.get(fuzzy_id, {})
            if isinstance(migration_outcomes, dict)
            else {}
        )
        canonical_memory_id = str(
            submitted_outcome.get("canonical_memory_id") or fuzzy_id or ""
        )
        deduplicated = submitted_outcome.get("status") == "deduplicated"
        migrated_count = int(pipeline_counts.get("embedded→migrated", 0) or 0)
        stored_in_engine = bool(
            canonical_memory_id
            and hasattr(engine, "memory_exists")
            and engine.memory_exists(canonical_memory_id)
        )
        durable_submission = (
            stored_in_engine if outcomes_reported else stored_in_engine or migrated_count > 0
        )

        canonical_record = None
        if durable_submission and stored_in_engine:
            get_memory_dict = getattr(engine, "get_memory_dict", None)
            if callable(get_memory_dict):
                canonical_record = get_memory_dict(canonical_memory_id)
            if not isinstance(canonical_record, dict):
                storage = getattr(engine, "_sqlite", None)
                get_canonical = getattr(storage, "get", None)
                if callable(get_canonical):
                    canonical_record = get_canonical(canonical_memory_id)
            if not isinstance(canonical_record, dict):
                raise RuntimeError("canonical_memory_snapshot_missing")

        canonical_content = str((canonical_record or {}).get("content") or content)
        canonical_memory_type = str(
            (canonical_record or {}).get("memory_type") or memory_type
        )
        canonical_scope = str((canonical_record or {}).get("scope") or scope)
        canonical_project_id = str(
            (canonical_record or {}).get("project_id") or project_ctx.project_id
        )
        canonical_visibility = str(
            (canonical_record or {}).get("visibility") or project_ctx.visibility
        )
        canonical_source_class = str(
            (canonical_record or {}).get("source_class") or project_ctx.source_class
        )
        canonical_domain = str((canonical_record or {}).get("domain") or "")
        canonical_entity_ids = (
            list(canonical_record.get("entity_ids") or [])
            if isinstance(canonical_record, dict)
            else list(all_entities)
        )

        # Bind graph and response identity to the durable canonical survivor.
        if durable_submission:
            for eid in canonical_entity_ids:
                engine.add_graph_edge(
                    source=canonical_memory_id,
                    target=eid,
                    relation="references",
                    weight=0.5,
                )

        # Push SSE notification for real-time multi-agent awareness
        if durable_submission:
            try:
                from plastic_promise.mcp.server import notify_issue_change

                notify_issue_change(
                    {
                        "type": "memory_stored",
                        "memory_id": canonical_memory_id,
                        "submitted_memory_id": fuzzy_id,
                        "deduplicated": deduplicated,
                        "created": not deduplicated,
                        "content_preview": canonical_content[:200],
                        "memory_type": canonical_memory_type,
                        "domain": canonical_domain,
                        "project_id": canonical_project_id,
                        "visibility": canonical_visibility,
                        "source_class": canonical_source_class,
                        "timestamp": __import__("datetime").datetime.now().isoformat(),
                    }
                )
            except Exception:
                pass

        if not durable_submission:
            warnings = project_ctx.warning_list() + [
                "memory_store pipeline completed without durable migration"
            ]
            trace = {"call_id": call_id, "project_id": project_ctx.project_id}
            safe_record_call_span(
                engine,
                call_id=call_id,
                parent_call_id=_parent_call_id(args),
                request_scope_id=request_scope["request_scope_id"],
                stage_session_id=request_scope["stage_session_id"],
                flow_line_id=request_scope["flow_line_id"],
                project_id=project_ctx.project_id,
                tool_name="memory_store",
                status="degraded",
                degraded=True,
                metadata={
                    "memory_type": memory_type,
                    "scope": scope,
                    "source": source,
                    "project_policy": project_ctx.project_policy,
                    "visibility": project_ctx.visibility,
                    "source_class": project_ctx.source_class,
                    "pipeline": pipeline_counts,
                },
            )
            safe_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope["request_scope_id"],
                project_id=project_ctx.project_id,
                tool_name="memory_store",
                link_name="memory_pipeline",
                policy="required",
                level="warning",
                error_class="QualityGate",
                error_message="pipeline produced no durable memory",
                fallback_used="none",
                minimum_result="quality_filtered",
                metadata={"pipeline": pipeline_counts},
            )
            payload = {
                "stored": False,
                "memory_id": fuzzy_id,
                "content_preview": content[:200],
                "memory_type": memory_type,
                "scope": scope,
                "project_id": project_ctx.project_id,
                "visibility": project_ctx.visibility,
                "source_class": project_ctx.source_class,
                "trace": trace,
                "warnings": warnings,
                "entity_ids": all_entities,
                "pipeline": pipeline_counts,
            }
            if proposal_shadow is not None:
                payload["proposal_shadow"] = proposal_shadow
            payload.update(
                build_envelope(
                    data=dict(payload),
                    trace=trace,
                    warnings=warnings,
                    fallback_used=[],
                    minimum_result="quality_filtered",
                )
            )
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        safe_record_call_span(
            engine,
            call_id=call_id,
            parent_call_id=_parent_call_id(args),
            request_scope_id=request_scope["request_scope_id"],
            stage_session_id=request_scope["stage_session_id"],
            flow_line_id=request_scope["flow_line_id"],
            project_id=project_ctx.project_id,
            tool_name="memory_store",
            status="success",
            degraded=bool(project_ctx.warning_list()),
            metadata={
                "memory_type": memory_type,
                "scope": scope,
                "source": source,
                "project_policy": project_ctx.project_policy,
                "visibility": project_ctx.visibility,
                "source_class": project_ctx.source_class,
            },
        )

        response_payload = {
            "stored": True,
            "memory_id": canonical_memory_id,
            "submitted_memory_id": fuzzy_id,
            "deduplicated": deduplicated,
            "created": not deduplicated,
            "content_preview": canonical_content[:200],
            "memory_type": canonical_memory_type,
            "scope": canonical_scope,
            "project_id": canonical_project_id,
            "visibility": canonical_visibility,
            "source_class": canonical_source_class,
            "trace": {"call_id": call_id, "project_id": canonical_project_id},
            "warnings": project_ctx.warning_list(),
            "entity_ids": canonical_entity_ids,
            "pipeline": pipeline_counts,
            "note": "必经流水线: raw→tagged→classified(大类)→embedded(细分)→主池",
            "server_ok": server_ok,
        }
        if proposal_shadow is not None:
            response_payload["proposal_shadow"] = proposal_shadow
        return [
            TextContent(
                type="text",
                text=json.dumps(response_payload, ensure_ascii=False),
            )
        ]
    except Exception as e:
        try:
            from plastic_promise.mcp.tools.request_scope import build_request_scope

            project_ctx = infer_project_context(args)
            call_id = args.get("call_id") or new_call_id()
            request_scope = build_request_scope(args, "memory_store")
            request_scope_id = request_scope["request_scope_id"]
            trace = {
                "call_id": call_id,
                "request_scope_id": request_scope_id,
                "project_id": project_ctx.project_id,
            }
            payload = {
                "content": args.get("content", ""),
                "memory_type": args.get("memory_type", "experience"),
                "source": args.get("source", "user"),
                "scope": args.get("scope", "global"),
                "entity_ids": args.get("entity_ids", []),
                "tags": args.get("tags", []),
                "visibility": project_ctx.visibility,
                "source_class": project_ctx.source_class,
                "origin_kind": args.get("origin_kind", "tool_call"),
                "origin_uri": args.get("origin_uri", "mcp://memory_store"),
                "origin_ref": args.get("origin_ref", ""),
                "origin_hash": args.get("origin_hash", ""),
                "parent_memory_ids": args.get("parent_memory_ids", []),
                "metadata_json": args.get("metadata_json", {}),
            }
            sqlite = getattr(engine, "_sqlite", None)
            conn = getattr(sqlite, "_conn", None)
            outbox_id = record_outbox_event(
                conn,
                tool_name="memory_store",
                project_id=project_ctx.project_id,
                call_id=call_id,
                status="pending",
                payload=payload,
                error_class=e.__class__.__name__,
                error_message=str(e),
                metadata={"fallback": "store_outbox"},
            )
            warnings = project_ctx.warning_list() + [
                "memory_store failed; payload persisted to store_outbox"
            ]
            response_payload = {
                "stored": False,
                "tool": "memory_store",
                "outbox_id": outbox_id,
                "content_preview": str(args.get("content", ""))[:200],
                "memory_type": args.get("memory_type", "experience"),
                "project_id": project_ctx.project_id,
                "visibility": project_ctx.visibility,
                "source_class": project_ctx.source_class,
                "trace": trace,
                "warnings": warnings,
                "error_class": e.__class__.__name__,
                "error": str(e),
            }
            envelope = build_envelope(
                data=dict(response_payload),
                trace=trace,
                warnings=warnings,
                fallback_used=["store_outbox"],
                minimum_result="outbox_record",
            )
            response_payload.update(envelope)
            safe_record_call_span(
                engine,
                call_id=call_id,
                parent_call_id=_parent_call_id(args),
                request_scope_id=request_scope_id,
                stage_session_id=request_scope["stage_session_id"],
                flow_line_id=request_scope["flow_line_id"],
                project_id=project_ctx.project_id,
                tool_name="memory_store",
                status="degraded",
                degraded=True,
                metadata={
                    "memory_type": args.get("memory_type", "experience"),
                    "scope": args.get("scope", "global"),
                    "project_policy": project_ctx.project_policy,
                    "visibility": project_ctx.visibility,
                    "source_class": project_ctx.source_class,
                    "outbox_id": outbox_id,
                },
            )
            safe_record_degradation_event(
                engine,
                call_id=call_id,
                request_scope_id=request_scope_id,
                project_id=project_ctx.project_id,
                tool_name="memory_store",
                link_name="store_outbox",
                policy="best_effort",
                level="warning",
                error_class=e.__class__.__name__,
                error_message=str(e),
                fallback_used="store_outbox",
                minimum_result="outbox_record",
                metadata={"outbox_id": outbox_id},
            )
            return [TextContent(type="text", text=json.dumps(response_payload, ensure_ascii=False))]
        except Exception as fallback_error:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": str(e),
                            "fallback_error": str(fallback_error),
                            "tool": "memory_store",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]


def _ordinary_mutation_failure(
    outcome_key: str,
    memory_id: str,
    reason: str,
    *,
    operation: str = "",
    status: str | None = None,
) -> list[TextContent]:
    payload: dict[str, Any] = {
        outcome_key: False,
        "committed": False,
        "memory_id": memory_id,
        "operation": operation,
        "reason": reason,
        "stale_dependents": [],
        "ordinary_index_job_id": "",
        "synthesis_index_job_ids": [],
        "pending_job_ids": [],
        "completed_job_ids": [],
    }
    if status is not None:
        payload["status"] = status
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _ordinary_record_value(record: Any, field: str) -> Any:
    if isinstance(record, dict):
        return record.get(field)
    return getattr(record, field, None)


def _ordinary_authority_record(engine: Any, memory_id: str, fallback: Any) -> Any:
    """Read canonical project evidence without trusting a hydrated public record."""
    getter = getattr(engine, "get_memory_dict_for_review", None)
    if not callable(getter):
        return fallback
    try:
        canonical = getter(memory_id)
    except Exception:
        return None
    return canonical if isinstance(canonical, dict) else None


def _ordinary_mutation_authority(
    runtime_context: dict[str, Any] | None,
    *,
    record: Any,
) -> tuple[tuple[str, str] | None, str]:
    if not isinstance(runtime_context, dict):
        return None, "ordinary_mutation_runtime_authorization_required"

    actor = str(runtime_context.get("actor") or "").strip()
    call_id = str(runtime_context.get("call_id") or "").strip()
    project_id = str(runtime_context.get("project_id") or "").strip()
    if not actor or not call_id or project_id in {"", "project:unknown"}:
        return None, "ordinary_mutation_runtime_authorization_required"
    try:
        trust_score = float(runtime_context.get("trust_score"))
    except (TypeError, ValueError):
        return None, "ordinary_mutation_runtime_authorization_denied"
    if runtime_context.get("defense_decision") != "allow" or not (0.0 <= trust_score <= 1.0):
        return None, "ordinary_mutation_runtime_authorization_denied"

    source_project_id = str(_ordinary_record_value(record, "project_id") or "").strip()
    if source_project_id in {"", "project:unknown"}:
        return None, "ordinary_mutation_source_project_required"
    if project_id != source_project_id:
        return None, "ordinary_mutation_project_mismatch"
    return (actor, call_id), ""


def _memory_tool_runtime_authority(
    runtime_context: dict[str, Any] | None,
    *,
    tool_name: str,
) -> tuple[tuple[str, str, str] | None, str]:
    """Validate server-owned authority for public bulk memory mutations."""
    if not isinstance(runtime_context, dict):
        return None, f"{tool_name}_runtime_authorization_required"
    actor = str(runtime_context.get("actor") or "").strip()
    call_id = str(runtime_context.get("call_id") or "").strip()
    project_id = str(runtime_context.get("project_id") or "").strip()
    try:
        trust_score = float(runtime_context.get("trust_score"))
        from plastic_promise.core.tool_manifest import manifest_for_tool

        required_trust = manifest_for_tool(tool_name).trust_requirement
    except (TypeError, ValueError):
        return None, f"{tool_name}_runtime_authorization_denied"
    if (
        not actor
        or not call_id
        or project_id in {"", "project:unknown"}
        or runtime_context.get("defense_decision") != "allow"
        or not math.isfinite(trust_score)
        or trust_score < required_trust
        or trust_score > 1.0
    ):
        return None, f"{tool_name}_runtime_authorization_denied"
    return (actor, call_id, project_id), ""


def _ordinary_mutation_payload(
    engine: Any,
    result: Any,
    *,
    outcome_key: str,
    status: str | None = None,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    ordinary_job_id = str(result.ordinary_index_job_id or "")
    synthesis_job_ids = [str(job_id) for job_id in result.synthesis_index_job_ids]
    job_ids = [job_id for job_id in (ordinary_job_id, *synthesis_job_ids) if job_id]
    statuses: dict[str, str] = {}
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    conn = getattr(sqlite, "_conn", None)
    if conn is not None:
        for job_id in job_ids:
            try:
                row = conn.execute(
                    "SELECT status FROM store_outbox WHERE outbox_id = ?",
                    (job_id,),
                ).fetchone()
            except Exception:
                row = None
            statuses[job_id] = str(row[0]) if row is not None else "pending"
    else:
        statuses = dict.fromkeys(job_ids, "pending")

    payload: dict[str, Any] = {
        outcome_key: True,
        "committed": True,
        "memory_id": str(result.memory_id),
        "operation": str(result.operation),
        "reason": "",
        "committed_memory_version": int(result.committed_memory_version),
        "stale_dependents": list(result.stale_synthesis_ids),
        "ordinary_index_job_id": ordinary_job_id,
        "synthesis_index_job_ids": synthesis_job_ids,
        "pending_job_ids": [job_id for job_id in job_ids if statuses.get(job_id) != "done"],
        "completed_job_ids": [job_id for job_id in job_ids if statuses.get(job_id) == "done"],
    }
    if status is not None:
        payload["status"] = status
    if actions is not None:
        payload["actions"] = actions
    return payload


def _serialize_channel_evidence(pack: Any) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    rankings: dict[str, list[dict]] = {}
    for channel, raw_rows in dict(getattr(pack, "channel_rankings", {}) or {}).items():
        rows: list[dict] = []
        for raw_row in list(raw_rows or []):
            if is_dataclass(raw_row):
                row = asdict(raw_row)
            elif isinstance(raw_row, dict):
                row = dict(raw_row)
            else:
                row = {
                    "id": getattr(raw_row, "id", getattr(raw_row, "memory_id", "")),
                    "score": getattr(raw_row, "score", None),
                    "rank": getattr(raw_row, "rank", None),
                }
            memory_id = str(row.get("id", row.get("memory_id", "")) or "").strip()
            if memory_id:
                rows.append(
                    {
                        "id": memory_id,
                        "score": row.get("score"),
                        "rank": row.get("rank"),
                    }
                )
        rankings[str(channel)] = rows
    states = {
        str(channel): dict(state)
        for channel, state in dict(getattr(pack, "channel_states", {}) or {}).items()
        if isinstance(state, dict)
    }
    return rankings, states


def _ordinary_metadata_payload(memory_id: str, success: bool) -> dict[str, Any]:
    return {
        "updated": success,
        "committed": success,
        "memory_id": memory_id,
        "operation": "metadata_patch",
        "reason": "" if success else "ordinary_metadata_update_failed",
        "stale_dependents": [],
        "ordinary_index_job_id": "",
        "synthesis_index_job_ids": [],
        "pending_job_ids": [],
        "completed_job_ids": [],
    }


def _trigger_memory_evolution(engine: Any) -> None:
    try:
        from plastic_promise.memory.soul_memory import EvolveR, RecMem

        EvolveR(RecMem(engine)).evolve_cycle()
    except Exception:
        pass


def _coordinated_memory_correction(
    engine: Any,
    *,
    memory_id: str,
    record: Any,
    new_content: Any,
    mark_as: Any,
    reason: Any,
    runtime_context: dict[str, Any],
) -> list[TextContent]:
    normalized_mark = str(mark_as or "").strip().casefold()
    if normalized_mark not in {"", "corrected", "wrong", "deprecated"}:
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            "correction_operation_invalid",
            operation=normalized_mark,
        )
    if normalized_mark == "corrected" and new_content is None:
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            "correction_content_required",
            operation="replace_content",
        )
    if normalized_mark in {"wrong", "deprecated"} and new_content is not None:
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            f"{normalized_mark}_content_not_allowed",
            operation=normalized_mark,
        )
    if new_content is None and not normalized_mark:
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            "correction_operation_required",
        )

    if new_content is not None:
        operation = "replace_content"
        result_action = "content_replaced"
        current_content = str(_ordinary_record_value(record, "content") or "")
        if str(new_content) == current_content:
            return _ordinary_mutation_failure(
                "corrected",
                memory_id,
                "correction_content_unchanged",
                operation=operation,
            )
    else:
        operation = normalized_mark
        result_action = f"marked_{normalized_mark}"

    authority_record = _ordinary_authority_record(engine, memory_id, record)
    authority, authority_reason = _ordinary_mutation_authority(
        runtime_context,
        record=authority_record,
    )
    if authority is None:
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            authority_reason,
            operation=operation,
        )
    actor, call_id = authority
    expected_project_id = str(_ordinary_record_value(authority_record, "project_id") or "").strip()
    mutate = getattr(engine, "mutate_ordinary_source", None)
    if not callable(mutate):
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            "ordinary_source_mutation_api_required",
            operation=operation,
        )

    mutation_kwargs = {
        "operation": operation,
        "reason": str(reason or f"public:memory_correct:{operation}"),
        "actor": actor,
        "call_id": call_id,
        "expected_project_id": expected_project_id,
        "expected_content_hash": synthesis_content_hash(
            _ordinary_record_value(authority_record, "content")
        ),
        "require_source_available": True,
    }
    if operation == "replace_content":
        mutation_kwargs["content"] = str(new_content)
    try:
        result = mutate(memory_id, **mutation_kwargs)
    except Exception as exc:
        from plastic_promise.core.ordinary_memory_mutation import (
            OrdinaryMemoryMutationError,
        )

        stable_reason = (
            str(exc)
            if isinstance(exc, OrdinaryMemoryMutationError)
            else "ordinary_source_mutation_failed"
        )
        return _ordinary_mutation_failure(
            "corrected",
            memory_id,
            stable_reason,
            operation=operation,
        )

    _trigger_memory_evolution(engine)
    payload = _ordinary_mutation_payload(
        engine,
        result,
        outcome_key="corrected",
        actions=[result_action],
    )
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


# ---- memory_update ----
async def handle_memory_update(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Update a memory's content or metadata.

    Delegates to ContextEngine.update_memory() which builds UpdateFields
    from the provided keyword arguments.

    Args:
        engine: ContextEngine instance.
        args: {"memory_id": str, "content"?: str, "importance"?: float,
               "category"?: str, "reset_worth"?: bool}.

    Returns:
        list[TextContent]: MCP response confirming update.
    """
    try:
        memory_id = args["memory_id"]
        content = args.get("content")
        importance = args.get("importance")
        category = args.get("category")
        record = engine.get_memory(memory_id)
        if record is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": False,
                            "memory_id": memory_id,
                            "reason": "not_found",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        if engine_memory_is_governed_synthesis(
            engine,
            memory_id,
            memory_type=getattr(record, "memory_type", None),
        ):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": False,
                            "memory_id": memory_id,
                            "reason": "governed_synthesis",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        if content is not None and _runtime_context is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": False,
                            "memory_id": memory_id,
                            "reason": "ordinary_content_requires_coordinator",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        if content is not None:
            if any(value is not None for value in (importance, category)) or bool(
                args.get("reset_worth")
            ):
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    "ordinary_content_metadata_combination_not_allowed",
                    operation="replace_content",
                )
            current_content = str(_ordinary_record_value(record, "content") or "")
            if str(content) == current_content:
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    "ordinary_source_content_unchanged",
                    operation="replace_content",
                )
            authority_record = _ordinary_authority_record(engine, memory_id, record)
            authority, authority_reason = _ordinary_mutation_authority(
                _runtime_context,
                record=authority_record,
            )
            if authority is None:
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    authority_reason,
                    operation="replace_content",
                )
            actor, call_id = authority
            expected_project_id = str(
                _ordinary_record_value(authority_record, "project_id") or ""
            ).strip()
            mutate = getattr(engine, "mutate_ordinary_source", None)
            if not callable(mutate):
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    "ordinary_source_mutation_api_required",
                    operation="replace_content",
                )
            try:
                result = mutate(
                    memory_id,
                    operation="replace_content",
                    content=str(content),
                    reason=str(args.get("reason") or "public:memory_update_content"),
                    actor=actor,
                    call_id=call_id,
                    expected_project_id=expected_project_id,
                    expected_content_hash=synthesis_content_hash(
                        _ordinary_record_value(authority_record, "content")
                    ),
                    require_source_available=True,
                )
            except Exception as exc:
                from plastic_promise.core.ordinary_memory_mutation import (
                    OrdinaryMemoryMutationError,
                )

                stable_reason = (
                    str(exc)
                    if isinstance(exc, OrdinaryMemoryMutationError)
                    else "ordinary_source_mutation_failed"
                )
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    stable_reason,
                    operation="replace_content",
                )
            payload = _ordinary_mutation_payload(
                engine,
                result,
                outcome_key="updated",
            )
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        fields = {
            key: value
            for key, value in {"importance": importance, "category": category}.items()
            if value is not None
        }
        success = bool(fields) or bool(args.get("reset_worth"))
        expected_project_id = ""
        if success and _runtime_context is not None:
            authority_record = _ordinary_authority_record(engine, memory_id, record)
            authority, authority_reason = _ordinary_mutation_authority(
                _runtime_context,
                record=authority_record,
            )
            if authority is None:
                return _ordinary_mutation_failure(
                    "updated",
                    memory_id,
                    authority_reason,
                    operation="metadata_patch",
                )
            expected_project_id = str(
                _ordinary_record_value(authority_record, "project_id") or ""
            ).strip()
        if success and _runtime_context is not None:
            patch = getattr(engine, "patch_ordinary_memory", None)
            replacements = dict(fields)
            if args.get("reset_worth"):
                replacements.update(
                    {
                        "worth_success": 0,
                        "worth_failure": 0,
                    }
                )
            try:
                success = bool(
                    callable(patch)
                    and patch(
                        memory_id,
                        replacements=replacements,
                        expected_project_id=expected_project_id,
                        require_source_available=True,
                        bump_memory_version=(True if args.get("reset_worth") else None),
                    )
                )
            except Exception:
                success = False
        elif fields and args.get("reset_worth"):
            patch = getattr(engine, "patch_ordinary_memory", None)
            success = bool(
                callable(patch)
                and patch(
                    memory_id,
                    replacements={
                        **fields,
                        "worth_success": 0,
                        "worth_failure": 0,
                    },
                    bump_memory_version=True,
                )
            )
        elif fields:
            update_fields = getattr(engine, "update_memory_fields", None)
            success = bool(callable(update_fields) and update_fields(memory_id, **fields))
        elif success and args.get("reset_worth"):
            reset_worth = getattr(engine, "reset_ordinary_worth", None)
            success = bool(callable(reset_worth) and reset_worth(memory_id))

        if _runtime_context is not None:
            payload = _ordinary_metadata_payload(memory_id, success)
        else:
            payload = {
                "updated": success,
                "memory_id": memory_id,
            }
        return [
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_update"}, ensure_ascii=False),
            )
        ]


# ---- memory_forget ----
async def handle_memory_forget(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Soft-delete a memory while preserving its audit record.

    The SQLite record is retained and marked as forgotten. Checked ordinary
    and dependent-synthesis delete jobs durably remove derived index state.

    Args:
        engine: ContextEngine instance.
        args: {"memory_id": str, "reason"?: str}.

    Returns:
        list[TextContent]: MCP response confirming deletion.
    """
    try:
        memory_id = args["memory_id"]
        reason = args.get("reason", "")

        record = engine.get_memory(memory_id)
        if record is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "forgotten": False,
                            "status": "not_found",
                            "memory_id": memory_id,
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        if engine_memory_is_governed_synthesis(
            engine,
            memory_id,
            memory_type=getattr(record, "memory_type", None),
        ):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "forgotten": False,
                            "status": "governed",
                            "memory_id": memory_id,
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        authority_record = _ordinary_authority_record(engine, memory_id, record)
        authority, authority_reason = _ordinary_mutation_authority(
            _runtime_context,
            record=authority_record,
        )
        if authority is None:
            return _ordinary_mutation_failure(
                "forgotten",
                memory_id,
                authority_reason,
                operation="forgotten",
                status="rejected",
            )
        actor, call_id = authority
        expected_project_id = str(
            _ordinary_record_value(authority_record, "project_id") or ""
        ).strip()
        mutate = getattr(engine, "mutate_ordinary_source", None)
        if not callable(mutate):
            return _ordinary_mutation_failure(
                "forgotten",
                memory_id,
                "ordinary_source_mutation_api_required",
                operation="forgotten",
                status="rejected",
            )
        try:
            result = mutate(
                memory_id,
                operation="forgotten",
                reason=str(reason or "public:memory_forget"),
                actor=actor,
                call_id=call_id,
                expected_project_id=expected_project_id,
                expected_content_hash=synthesis_content_hash(
                    _ordinary_record_value(authority_record, "content")
                ),
                require_source_available=True,
            )
        except Exception as exc:
            from plastic_promise.core.ordinary_memory_mutation import (
                OrdinaryMemoryMutationError,
            )

            stable_reason = (
                str(exc)
                if isinstance(exc, OrdinaryMemoryMutationError)
                else "ordinary_source_mutation_failed"
            )
            return _ordinary_mutation_failure(
                "forgotten",
                memory_id,
                stable_reason,
                operation="forgotten",
                status="rejected",
            )

        payload = _ordinary_mutation_payload(
            engine,
            result,
            outcome_key="forgotten",
            status="soft_deleted",
        )
        state = getattr(engine, "__dict__", {})
        sqlite = state.get("_sqlite") if isinstance(state, dict) else None
        try:
            canonical = sqlite.get(memory_id) if sqlite is not None else None
        except Exception:
            canonical = None
        payload["tags"] = list(canonical.get("tags") or []) if canonical else []
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_forget"}, ensure_ascii=False),
            )
        ]


# ---- memory_stats ----
async def handle_memory_stats(engine: Any, args: dict) -> list[TextContent]:
    """Return memory pool statistics.

    Delegates to ContextEngine.memory_stats_json() which computes
    aggregate statistics from the SQLite backend.

    Args:
        engine: ContextEngine instance.
        args: {"scope"?: str} (optional namespace filter).

    Returns:
        list[TextContent]: MCP response with memory pool statistics.
    """
    try:
        scope = args.get("scope")

        stats_json = engine.memory_stats_json(scope)
        stats = json.loads(stats_json)

        result = {
            "total": stats.get("total", 0),
            "healthy": stats.get("healthy", 0),
            "decaying": stats.get("decaying", 0),
            "by_tier": stats.get("by_tier", {}),
            "by_type": stats.get("by_type", {}),
            "by_category": stats.get("by_category", {}),
            "average_worth": stats.get("average_worth", 0.0),
        }

        # 追加 fuzzy buffer 积压信息
        try:
            fb = _get_fuzzy_buffer(engine)
            if fb:
                result["fuzzy_buffer"] = fb.stats()
        except Exception:
            pass

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_stats"}, ensure_ascii=False),
            )
        ]


# ---- memory_list ----
async def handle_memory_list(engine: Any, args: dict) -> list[TextContent]:
    """List memories by filter criteria.

    Delegates to ContextEngine.list_memories() with optional filters
    for memory_type, source, min_worth, limit, and scope.

    Args:
        engine: ContextEngine instance.
        args: {"memory_type"?: str, "source"?: str, "min_worth"?: float,
               "limit"?: int, "scope"?: str}.

    Returns:
        list[TextContent]: MCP response with filtered memory items.
    """
    try:
        memory_type = args.get("memory_type")
        source = args.get("source")
        min_worth = args.get("min_worth")
        limit = args.get("limit", 50)
        scope = args.get("scope")

        results = engine.list_memories(
            memory_type=memory_type,
            source=source,
            min_worth=min_worth,
            limit=limit,
            scope=scope,
        )

        items = [
            {
                "id": r.id,
                "content": r.content[:300],
                "memory_type": r.memory_type,
                "source": r.source,
                "tier": r.tier,
                "worth_score": r.worth_score(),
                "created_at": r.created_at,
            }
            for r in results
        ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"items": items, "count": len(items)}, ensure_ascii=False, indent=2
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_list"}, ensure_ascii=False),
            )
        ]


# ---- memory_gc ----
async def handle_memory_gc(engine: Any, args: dict) -> list[TextContent]:
    """Run garbage collection on decaying memories.

    Delegates to MemoryGC.collect() which performs:
    1. Mark decaying candidates (worth_score < threshold)
    2. Merge similar memories (cosine similarity >= 0.70)
    3. Remove decayed records and persist merged metadata to SQLite

    Args:
        engine: ContextEngine instance.
        args: {"dry_run"?: bool, "force"?: bool}.

    Returns:
        list[TextContent]: MCP response with GC results from MemoryGC.
    """
    try:
        dry_run = args.get("dry_run", True)
        force = args.get("force", False)

        if dry_run:
            from plastic_promise.core.constants import MEMORY_DECAY_THRESHOLD

            candidates: list[tuple[str, float]] = []
            iter_memories = getattr(engine, "iter_memories", None)
            try:
                public_memories = list(iter_memories()) if callable(iter_memories) else []
            except Exception:
                public_memories = []
            for mem in public_memories:
                try:
                    if not isinstance(mem, dict):
                        continue
                    mid = str(mem.get("id") or "")
                    if not mid:
                        continue
                    worth = mem.get("worth_score")
                    decay = mem.get("decay_multiplier")
                    score = worth if worth is not None else decay
                    if score is not None and score < MEMORY_DECAY_THRESHOLD:
                        candidates.append((mid, float(score)))
                except Exception:
                    continue
            candidates.sort(key=lambda item: item[1])
            result = {
                "dry_run": True,
                "candidates_count": len(candidates),
                "candidates": [mid for mid, _score in candidates[:50]],
                "removed": 0,
                "health_before": 1.0,
                "health_after": 1.0,
                "freed_slots": 0,
                "merge": {
                    "dry_run": True,
                    "candidates_found": 0,
                    "would_merge": 0,
                    "would_free": 0,
                    "merged_pairs": [],
                    "skipped": "lightweight dry_run does not initialize LanceDB",
                },
                "context_status": "lightweight",
            }
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        engine.ensure_heavy_init()  # actual GC needs LanceDB/decay components

        from plastic_promise.memory.soul_memory import MemoryGC, RecMem

        rm = RecMem(engine)
        rm.update_all_decay()  # NEW: update Weibull decay values first
        gc = MemoryGC(rm)
        result = gc.collect(dry_run=dry_run, force=force)

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "memory_gc"}, ensure_ascii=False),
            )
        ]


# ---- _extract_entity_ids (internal helper) ----
def _extract_entity_ids(content: str, engine: Any) -> list[str]:
    """Auto-extract entity references from memory content.

    Matches known principle names and graph node names against content.
    Serves principle #6 (data-flow driven — actual content → actual links).
    """
    entity_ids = []
    try:
        from plastic_promise.core.constants import CORE_PRINCIPLES

        # Match principle names
        for p in CORE_PRINCIPLES:
            if p["name"][:4] in content or p["name"][-4:] in content:
                pid = f"principle:{p['id']}"
                if pid not in entity_ids:
                    entity_ids.append(pid)
        # Match existing graph nodes
        for node in engine.list_graph_nodes():
            nid = node.get("id", "")
            name = node.get("name", "")
            if name and len(name) >= 3 and name in content and nid not in entity_ids:
                entity_ids.append(nid)
    except Exception:
        pass
    return entity_ids


# ---- _get_fuzzy_buffer (internal helper) ----
# Module-level caches for engine fuzzy buffers (avoids private-attr violations)
_fuzzy_buffers: dict[int, Any] = {}
_rec_mem_cache: dict[int, Any] = {}


def _get_fuzzy_buffer(engine: Any):
    """Get or create a FuzzyBuffer attached to the engine."""
    eid = id(engine)
    if eid not in _fuzzy_buffers:
        from plastic_promise.core.embedder import get_embedder
        from plastic_promise.memory.pipeline import MemoryPipeline
        from plastic_promise.memory.soul_memory import MemoryTierManager, RecMem

        rec_mem = _rec_mem_cache.get(eid, RecMem(engine))
        try:
            embedder = get_embedder()
        except Exception:
            from plastic_promise.core.embedder import FallbackEmbedder

            embedder = FallbackEmbedder()
        tier_mgr = MemoryTierManager(rec_mem)
        _fuzzy_buffers[eid] = MemoryPipeline(
            rec_mem=rec_mem,
            embedder=embedder,
            tier_manager=tier_mgr,
            domain_manager=getattr(engine, "_dm", None),
            lancedb=getattr(engine, "_ldb", None),
        )
        _rec_mem_cache[eid] = rec_mem
    return _fuzzy_buffers[eid]


# ---- fuzzy_status (internal — not exposed as MCP tool) ----
async def handle_fuzzy_status(engine: Any, args: dict) -> list[TextContent]:
    """Query fuzzy buffer statistics — items per stage, total, oldest pending."""
    try:
        fb = _get_fuzzy_buffer(engine)
        stats = fb.stats()
        return [TextContent(type="text", text=json.dumps(stats, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "fuzzy_status"}, ensure_ascii=False),
            )
        ]


# ---- fuzzy_process (internal — not exposed as MCP tool) ----
async def handle_fuzzy_process(engine: Any, args: dict) -> list[TextContent]:
    """Trigger fuzzy buffer pipeline processing (raw→tagged→embedded→classified→migrate)."""
    try:
        fb = _get_fuzzy_buffer(engine)
        result = fb.process_pipeline()
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "fuzzy_process"}, ensure_ascii=False),
            )
        ]


# ---- memory_correct ----
async def handle_memory_correct(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """Human-in-the-loop memory correction — edit, deprecate, or mark memory quality.

    Serves principles #2 (transparency) and #3 (audit closure) by giving
    users explicit control over AI memories.
    """
    try:
        memory_id = args["memory_id"]
        new_content = args.get("content")
        mark_as = args.get("mark_as")  # "corrected" | "deprecated" | "wrong"
        reason = args.get("reason", "")

        record = engine.get_memory(memory_id)
        if record is None:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Memory {memory_id} not found"}, ensure_ascii=False),
                )
            ]
        if engine_memory_is_governed_synthesis(
            engine,
            memory_id,
            memory_type=getattr(record, "memory_type", None),
        ):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "corrected": False,
                            "memory_id": memory_id,
                            "reason": "governed_synthesis",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        # Direct handler calls lack server-owned authority. Keep their legacy
        # metadata-only compatibility behavior and reject content/lifecycle
        # writes; public MCP dispatch supplies the coordinator context below.
        if _runtime_context is None and (new_content is not None or mark_as == "deprecated"):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "corrected": False,
                            "memory_id": memory_id,
                            "reason": "ordinary_content_requires_coordinator",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        actions = []

        # Quality feedback is metadata-only. Apply it through the canonical
        # counter patch so private/index/provenance columns are never rebuilt
        # from a hydrated MemoryRecord.
        if _runtime_context is None and mark_as in {"wrong", "corrected"}:
            feedback_type = "rejected" if mark_as == "wrong" else "adopted"
            try:
                engine.apply_ordinary_feedback(memory_id, feedback_type)
            except Exception:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "corrected": False,
                                "memory_id": memory_id,
                                "reason": "ordinary_feedback_update_failed",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            record = engine.get_memory(memory_id) or record
            actions.append("marked_wrong" if mark_as == "wrong" else "marked_corrected")

        if _runtime_context is not None:
            return _coordinated_memory_correction(
                engine,
                memory_id=memory_id,
                record=record,
                new_content=new_content,
                mark_as=mark_as,
                reason=reason,
                runtime_context=_runtime_context,
            )

        # Trigger EvolveR after correction — 自演化闭环
        try:
            from plastic_promise.memory.soul_memory import EvolveR, RecMem

            rm = RecMem(engine)
            evolver = EvolveR(rm)
            evolver.evolve_cycle()
        except Exception:
            pass

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "corrected": True,
                        "memory_id": memory_id,
                        "actions": actions,
                        "reason": reason,
                        "worth_score": record.worth_score()
                        if hasattr(record, "worth_score")
                        else None,
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
                text=json.dumps({"error": str(e), "tool": "memory_correct"}, ensure_ascii=False),
            )
        ]


# ═══════════════════════════════════════════════════════════════
# memory_reclassify — 批量重跑分类管线
# ═══════════════════════════════════════════════════════════════


async def handle_memory_reclassify(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """原地重分类存量记忆 — 只走规则分类(大类tier/domain/category)，不创建新记录。

    使用 fuzzy_buffer 的 Stage 2 分类能力（tagged→classified），
    但直接更新现有 SQLite 记录，不经过 Stage 4 的 store() 创建副本。
    """
    authority, authority_reason = _memory_tool_runtime_authority(
        _runtime_context,
        tool_name="memory_reclassify",
    )
    if authority is None:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "reclassified": 0,
                        "remaining": 0,
                        "skipped": 0,
                        "errors": 0,
                        "committed": False,
                        "reason": authority_reason,
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    _actor, _call_id, authority_project_id = authority

    batch_size = args.get("batch_size", 50)
    if type(batch_size) is not int or not (1 <= batch_size <= 1000):
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "reclassified": 0,
                        "remaining": 0,
                        "skipped": 0,
                        "errors": 0,
                        "committed": False,
                        "reason": "memory_reclassify_batch_size_invalid",
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    dry_run = args.get("dry_run", False)
    target_id = str(args.get("memory_id") or "")
    resume_from = args.get("resume_from", 0)
    if type(resume_from) is not int or resume_from < 0:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "reclassified": 0,
                        "remaining": 0,
                        "skipped": 0,
                        "errors": 0,
                        "committed": False,
                        "reason": "memory_reclassify_resume_from_invalid",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    # Import classification components
    from plastic_promise.memory.soul_memory import MemoryRecord
    from plastic_promise.smart_extractor import _classify_by_rules

    fb = _get_fuzzy_buffer(engine)
    tier_mgr = fb._tier_manager
    dm = getattr(engine, "_dm", None)

    reclassified = 0
    skipped = 0
    errors = 0
    by_category = {}
    by_domain = {}

    pending = []
    for mem in engine.iter_memories():
        mid = mem["id"]
        if target_id and mid != target_id:
            continue
        if engine_memory_is_governed_synthesis(
            engine,
            mid,
            memory_type=mem.get("memory_type"),
        ):
            skipped += 1
            continue
        canonical = _ordinary_authority_record(engine, mid, mem)
        if not isinstance(canonical, dict):
            skipped += 1
            continue
        if str(canonical.get("project_id") or "").strip() != authority_project_id:
            skipped += 1
            continue
        mem = canonical
        tags = list(mem.get("tags", []))
        if "status:replaced" in tags:
            skipped += 1
            continue
        pending.append((mid, mem, tags))

    pending.sort(key=lambda item: item[0])
    start = 0 if target_id else resume_from
    batch = pending[start : start + batch_size]
    remaining = max(0, len(pending) - start - len(batch))

    for mid, mem, old_tags in batch:
        try:
            content = mem.get("content", "")
            if not content.strip():
                skipped += 1
                continue

            # ── 1. Category: rule-based keyword matching ──
            cat, conf = _classify_by_rules(content)
            new_category = cat if cat else mem.get("category", "other")

            # ── 2. Tier: MemoryTierManager.classify_tier ──
            new_tier = "L1"
            if tier_mgr is not None:
                try:
                    mr = MemoryRecord(
                        content=content,
                        memory_type=mem.get("memory_type", "experience"),
                        source=mem.get("source", "user"),
                    )
                    mr.access_count = mem.get("access_count", 0)
                    mr.worth_success = mem.get("worth_success", 0)
                    mr.worth_failure = mem.get("worth_failure", 0)
                    new_tier = tier_mgr.classify_tier(mr)
                except Exception:
                    pass

            # ── 3. Domain: DomainManager.assign ──
            new_domain = mem.get("domain", "uncategorized")
            new_tags = list(old_tags)
            if cat and f"cat:{cat}" not in new_tags:
                new_tags.append(f"cat:{cat}")

            # ── 3.5. LLM pending: tag uncertain classifications for background refinement ──
            if new_category == "other" or conf < 0.5:
                if "llm_pending:true" not in new_tags:
                    new_tags.append("llm_pending:true")
            else:
                # Remove llm_pending if category is now confident
                if "llm_pending:true" in new_tags:
                    new_tags.remove("llm_pending:true")

            if dm is not None and (new_domain == "uncategorized" or new_domain is None):
                try:
                    assigned = dm.assign(new_tags, agent_id="system")
                    if assigned and assigned != "uncategorized":
                        new_domain = assigned
                except Exception:
                    pass

            # ── 4. Apply changes in-place ──
            changed = (
                new_category != mem.get("category", "other")
                or new_tier != mem.get("tier", "L1")
                or new_domain != mem.get("domain", "uncategorized")
                or set(new_tags) != set(old_tags)
            )

            if changed and not dry_run:
                from plastic_promise.core.synthesis import synthesis_content_hash

                expected_project_id = str(mem.get("project_id") or "").strip() or None
                replacements = {
                    "category": new_category,
                    "tier": new_tier,
                    "domain": new_domain,
                    "tags": new_tags,
                }
                patch = getattr(engine, "patch_ordinary_memory", None)
                if not callable(patch):
                    errors += 1
                    continue
                canonical = patch(
                    mid,
                    replacements=replacements,
                    expected_project_id=expected_project_id,
                    expected_content_hash=synthesis_content_hash(content),
                    expected_tags=old_tags,
                    expected_category=str(mem.get("category") or "other"),
                    require_source_available=True,
                    index_upsert_call_id=f"memory-reclassify:{time.time_ns()}:{mid}",
                )
                if isinstance(canonical, dict):
                    by_category[new_category] = by_category.get(new_category, 0) + 1
                    by_domain[new_domain] = by_domain.get(new_domain, 0) + 1
                    reclassified += 1
                else:
                    skipped += 1
            elif changed and dry_run:
                reclassified += 1
                by_category[new_category] = by_category.get(new_category, 0) + 1
                by_domain[new_domain] = by_domain.get(new_domain, 0) + 1
            else:
                skipped += 1

        except Exception:
            errors += 1

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "reclassified": reclassified,
                    "remaining": remaining,
                    "skipped": skipped,
                    "errors": errors,
                    "batch_size": batch_size,
                    "dry_run": dry_run,
                    "committed": not dry_run and reclassified > 0,
                    "reason": "",
                    "project_id": authority_project_id,
                    "total": engine.memory_count,
                    "last_id": batch[-1][0] if batch else None,
                    "next_resume_from": start + len(batch),
                    "category_distribution": by_category,
                    "domain_distribution": by_domain,
                },
                ensure_ascii=False,
            ),
        )
    ]


# ═══════════════════════════════════════════════════════════════
# memory_sync_files — 存量 .md 文件同步到 MCP 管道
# ═══════════════════════════════════════════════════════════════


def _parse_frontmatter(content: str) -> dict:
    """使用 yaml 标准库解析 frontmatter。失败时降级返回空 dict。"""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml

        result = yaml.safe_load(parts[1])
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _canonical_memory_sync_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _memory_sync_path_allowed(path: str, roots: list[Any]) -> bool:
    candidate = _canonical_memory_sync_path(path)
    for raw_root in roots:
        if not isinstance(raw_root, (str, os.PathLike)) or not os.fspath(raw_root).strip():
            continue
        root = _canonical_memory_sync_path(raw_root)
        try:
            if os.path.commonpath((candidate, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _memory_sync_failure(source_dir: Any, reason: str, *, error: str = "") -> list[TextContent]:
    payload = {
        "synced": 0,
        "skipped": 0,
        "errors": 0,
        "committed": False,
        "reason": reason,
        "source_dir": str(source_dir or ""),
    }
    if error:
        payload["error"] = error
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def handle_memory_sync_files(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict[str, Any] | None = None,
) -> list[TextContent]:
    """同步文件系统 .md 记忆到 MCP 管道。"""
    source_dir = args.get("source_dir", "")
    dry_run = args.get("dry_run", False)

    authority, authority_reason = _memory_tool_runtime_authority(
        _runtime_context,
        tool_name="memory_sync_files",
    )
    if authority is None:
        return _memory_sync_failure(source_dir, authority_reason)
    _actor, authority_call_id, authority_project_id = authority

    allowed_roots = _runtime_context.get("allowed_source_roots")
    if not isinstance(allowed_roots, list) or not _memory_sync_path_allowed(
        str(source_dir or ""),
        allowed_roots,
    ):
        return _memory_sync_failure(source_dir, "memory_sync_source_not_allowed")

    canonical_source_dir = _canonical_memory_sync_path(str(source_dir))
    if not source_dir or not os.path.isdir(canonical_source_dir):
        return _memory_sync_failure(
            source_dir,
            "memory_sync_invalid_source_dir",
            error=f"Invalid source_dir: {source_dir}",
        )

    synced = 0
    stored = 0
    skipped = 0
    errors = 0

    for fname in sorted(os.listdir(canonical_source_dir)):
        if fname == "MEMORY.md" or not fname.endswith(".md"):
            continue

        fpath = _canonical_memory_sync_path(os.path.join(canonical_source_dir, fname))
        if not _memory_sync_path_allowed(fpath, [canonical_source_dir]) or not os.path.isfile(
            fpath
        ):
            errors += 1
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeError):
            errors += 1
            continue

        if "[[synced-to-mcp]]" in content or "[[memory-system-primary-channel]]" in content:
            skipped += 1
            continue

        fm = _parse_frontmatter(content)
        name = fm.get("name", fname.replace(".md", ""))
        metadata_fm = fm.get("metadata", {})
        mem_type = (
            metadata_fm.get("type", "reference") if isinstance(metadata_fm, dict) else "reference"
        )
        description = fm.get("description", "")

        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            body = parts[-1].strip() if len(parts) >= 3 else content

        tags = [f"cat:{mem_type}", "source:file-sync", f"file:{fname}"]
        entity_id = f"memory:file:{name}"

        if dry_run:
            synced += 1
            continue

        try:
            with trusted_memory_origin("memory_sync_files"):
                result = await handle_memory_store(
                    engine,
                    {
                        "content": f"[FILE SYNC] {name}: {description}\n\n{body}",
                        "memory_type": "experience",
                        "source": "file_sync",
                        "entity_ids": [entity_id],
                        "tags": tags,
                        "max_llm_calls": args.get("max_llm_calls", 0),
                        "project_id": authority_project_id,
                        "project_policy": str(_runtime_context.get("project_policy") or "balanced"),
                        "visibility": "project",
                        "parent_call_id": authority_call_id,
                    },
                )
            data = json.loads(result[0].text)
            if data.get("stored"):
                stored += 1
                new_content = content.rstrip() + "\n\n[[synced-to-mcp]]\n"
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                synced += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    partial = not dry_run and errors > 0 and (stored > 0 or synced > 0)
    reason = (
        "memory_sync_files_partial"
        if partial
        else "memory_sync_files_failed"
        if not dry_run and errors > 0
        else ""
    )

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "synced": synced,
                    "stored": stored,
                    "skipped": skipped,
                    "errors": errors,
                    "source_dir": source_dir,
                    "dry_run": dry_run,
                    "committed": not dry_run and stored > 0,
                    "partial": partial,
                    "reason": reason,
                    "project_id": authority_project_id,
                },
                ensure_ascii=False,
            ),
        )
    ]
