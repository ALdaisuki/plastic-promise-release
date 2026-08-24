"""域 1: Session Lifecycle skills — 会话生命周期管理"""

import asyncio
import json
import time

from plastic_promise.skills.engine import SkillDef, SkillResult

_CONTEXT_MODES = {"none", "light", "full"}
_LIGHT_CONTEXT_LIMIT = 2
_LIGHT_CONTEXT_TIMEOUT_S = 1.5
_FULL_CONTEXT_TIMEOUT_S = 10.0
_DEFAULT_WORKFLOW_ROUTE = "idea-to-ship"
_DEFAULT_WORKFLOW_ENTRY_STAGE = "grill-with-docs"
_VALID_WORKFLOW_ENTRYPOINTS = [
    "grill-with-docs",
    "diagnosing-bugs",
    "research",
    "prototype",
    "resolving-merge-conflicts",
    "code-review",
]
_WORKFLOW_GOVERNANCE_CONTRACT = (
    "Use the pinned official Matt Pocock flow and its invocation authority. "
    "Call sp-stage only for a model-invoked skill or an explicitly user-invoked skill; "
    "the server isolates state by stage_session_id and flow_line_id."
)


def _nonempty_text(value, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _workflow_route_catalog() -> dict:
    try:
        from plastic_promise.skills.tool_routing import OFFICIAL_WORKFLOW_ROUTES

        catalog = {}
        for route_id, route in OFFICIAL_WORKFLOW_ROUTES.items():
            stages = list(route.get("stages") or [])
            catalog[route_id] = {
                "label": route.get("label", route_id),
                "summary": route.get("summary", ""),
                "entry_stage": stages[0] if stages else _DEFAULT_WORKFLOW_ENTRY_STAGE,
                "stages": stages,
                "branches": dict(route.get("branches") or {}),
            }
        if catalog:
            return catalog
    except Exception:
        pass

    return {
        _DEFAULT_WORKFLOW_ROUTE: {
            "label": "Idea to ship",
            "summary": "Official Matt Pocock engineering flow.",
            "entry_stage": _DEFAULT_WORKFLOW_ENTRY_STAGE,
            "stages": [_DEFAULT_WORKFLOW_ENTRY_STAGE],
        }
    }


def _build_workflow_contract(params: dict, stage_session_id: str, ctx=None) -> dict:
    requested_route = _nonempty_text(
        params.get("route") or params.get("workflow_route"),
        _DEFAULT_WORKFLOW_ROUTE,
    )
    route_catalog = _workflow_route_catalog()
    route = requested_route if requested_route in route_catalog else _DEFAULT_WORKFLOW_ROUTE
    flow_line_id = _nonempty_text(
        params.get("flow_line_id") or params.get("flow_id"),
        route,
    )
    public_stage_session_id = _nonempty_text(
        stage_session_id or params.get("stage_session_id") or params.get("stage_id"),
        "default",
    )
    from plastic_promise.core.workflow_state import compose_flow_scope

    project_id = _nonempty_text(params.get("project_id"), "")
    flow_scope_id = compose_flow_scope(public_stage_session_id, flow_line_id, project_id)
    persisted_state = {}
    try:
        from plastic_promise.mcp.tools.skill_tracking import get_stage_chain_state

        persisted_state = get_stage_chain_state(flow_scope_id, engine=ctx)
    except Exception:
        pass
    persisted_route = str(persisted_state.get("route_id") or "")
    if persisted_route in route_catalog:
        route = persisted_route
    route_profile = route_catalog.get(route)
    if route_profile is None:
        route = _DEFAULT_WORKFLOW_ROUTE
        route_profile = route_catalog[route]
    stages = list((route_profile or {}).get("stages") or [_DEFAULT_WORKFLOW_ENTRY_STAGE])
    entry_stage = stages[0] if stages else _DEFAULT_WORKFLOW_ENTRY_STAGE
    from plastic_promise.skills.tool_routing import invocation_policy

    entry_authority = invocation_policy(entry_stage)
    current_step_index = int(persisted_state.get("current_step_index", -1))
    current_stage = str(persisted_state.get("current_stage") or "")
    if not (0 <= current_step_index < len(stages)) or stages[current_step_index] != current_stage:
        current_step_index = stages.index(current_stage) if current_stage in stages else -1
    next_step_index = current_step_index + 1
    next_stage = stages[next_step_index] if next_step_index < len(stages) else ""
    next_authority = invocation_policy(next_stage) if next_stage else "unknown"

    return {
        "default_route": _DEFAULT_WORKFLOW_ROUTE,
        "route": route,
        "route_id": route,
        "flow_line_id": flow_line_id,
        "stage_session_id": public_stage_session_id,
        "project_id": project_id,
        "flow_scope_id": flow_scope_id,
        "entry_stage": entry_stage,
        "entry_authority": entry_authority,
        "current_stage": current_stage or None,
        "current_step_index": current_step_index,
        "next_stage": next_stage or None,
        "stages": stages,
        "branches": dict((route_profile or {}).get("branches") or {}),
        "valid_root_entrypoints": sorted(
            {
                str(profile.get("entry_stage") or "")
                for profile in route_catalog.values()
                if profile.get("entry_stage")
            }
        )
        or list(_VALID_WORKFLOW_ENTRYPOINTS),
        "available_routes": route_catalog,
        "custom_route_policy": "Only pinned official route ids are accepted.",
        "governance_contract": _WORKFLOW_GOVERNANCE_CONTRACT,
        "next_call": {
            "tool": "sp-stage",
            "stage": next_stage or None,
            "invocation_source": next_authority if next_stage else None,
            "auto_invoke": next_authority == "model",
            "task_description": params.get("task_description", ""),
            "stage_session_id": public_stage_session_id,
            "route": route,
            "flow_line_id": flow_line_id,
            "project_id": project_id,
        },
    }


def _compile_component_health(ctx) -> dict:
    """Compile health status for all four session-init components."""
    health = {}

    # domain_manager
    health["domain_manager"] = "healthy" if getattr(ctx, "_dm_ok", False) else "degraded_no_init"

    # lancedb
    ldb = getattr(ctx, "_ldb", None)
    if ldb is None:
        health["lancedb"] = "unavailable"
    elif getattr(ldb, "_vectors_disabled", False):
        health["lancedb"] = "degraded_vectors"
    else:
        health["lancedb"] = "healthy"

    # embedder
    try:
        from plastic_promise.core.server_embedder import get_embedder

        emb = get_embedder()
        health["embedder"] = (
            "fallback_zero" if getattr(emb, "model_name", "") == "fallback-zero" else "healthy"
        )
    except Exception:
        health["embedder"] = "fallback_zero"

    # scarf — degraded if embedder is zero-vector
    health["scarf"] = "degraded_text_only" if health["embedder"] == "fallback_zero" else "healthy"

    return health


def _context_mode(params: dict) -> tuple[str, str | None]:
    requested = str(params.get("context_mode", "light") or "light").lower()
    if requested in _CONTEXT_MODES:
        return requested, None
    return "light", requested


def _context_timeout(params: dict, default: float) -> float:
    raw = params.get("context_timeout_s", default)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = default
    return max(0.1, min(timeout, 30.0))


def _tokenize_light(text: str) -> list[str]:
    try:
        from plastic_promise.core.context_engine import ContextEngine

        return ContextEngine._tokenize(text)
    except Exception:
        return [part.lower() for part in text.split() if len(part) >= 3]


def _memory_worth(mem: dict) -> float:
    explicit = mem.get("worth_score")
    if explicit is not None:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    success = float(mem.get("worth_success") or 0)
    failure = float(mem.get("worth_failure") or 0)
    total = success + failure
    return (success + 1.0) / (total + 2.0) if total > 0 else 0.5


def _is_deleted_or_forgotten(mem: dict) -> bool:
    tags = mem.get("tags", []) or []
    return bool(set(tags) & {"status:forgotten", "status:deleted", "decay:pending"})


def _light_context_status(ctx, params: dict) -> dict:
    """Return a bounded lexical memory preview without embedding or rerank."""
    timeout_s = _context_timeout(params, _LIGHT_CONTEXT_TIMEOUT_S)
    deadline = time.monotonic() + timeout_s
    iter_memories = getattr(ctx, "iter_memories", None)
    if not callable(iter_memories):
        return {
            "status": "deferred",
            "mode": "light",
            "reason": "public memory iterator unavailable; call context_supply before material decisions",
            "items": [],
            "item_count": 0,
            "timeout_s": timeout_s,
            "requires_full_context_before_action": True,
        }
    try:
        memories = list(iter_memories())
    except Exception:
        return {
            "status": "deferred",
            "mode": "light",
            "reason": "public memory admission failed; call context_supply before material decisions",
            "items": [],
            "item_count": 0,
            "timeout_s": timeout_s,
            "requires_full_context_before_action": True,
        }

    query = str(params.get("task_description", "") or "")
    task_type = str(params.get("task_type", "general") or "general")
    query_terms = set(_tokenize_light(f"{query} {task_type}"))
    if not query_terms:
        return {
            "status": "deferred",
            "mode": "light",
            "reason": "empty task description; call context_supply once the task is concrete",
            "items": [],
            "item_count": 0,
            "timeout_s": timeout_s,
            "requires_full_context_before_action": True,
        }

    scored: list[dict] = []
    scanned = 0
    timed_out = False
    for index, mem in enumerate(memories):
        if time.monotonic() > deadline:
            timed_out = True
            break
        if not isinstance(mem, dict) or _is_deleted_or_forgotten(mem):
            continue
        # adoption-audit: light preview must not surface code_memory echoes
        if (
            str(mem.get("source_kind", "") or "") == "code_memory"
            or str(mem.get("source", "") or "") == "code_memory"
        ):
            continue
        mid = str(mem.get("id") or f"memory:{index}")
        content = str(mem.get("content", "") or "")
        if not content.strip():
            continue
        tags = " ".join(str(tag) for tag in (mem.get("tags", []) or []))
        searchable = " ".join(
            [
                content,
                str(mem.get("memory_type", "") or ""),
                str(mem.get("source", "") or ""),
                str(mem.get("domain", "") or ""),
                str(mem.get("category", "") or ""),
                tags,
            ]
        )
        doc_terms = set(_tokenize_light(searchable))
        overlap = query_terms & doc_terms
        if not overlap:
            continue
        scanned += 1
        lexical = len(overlap) / max(len(query_terms), 1)
        worth = _memory_worth(mem)
        try:
            importance = float(mem.get("importance", 0.5) or 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        tier = str(mem.get("tier", "") or "")
        tier_boost = 0.08 if tier == "L1" else (-0.04 if tier == "L3" else 0.0)
        relevance = min(1.0, (0.65 * lexical) + (0.25 * worth) + (0.10 * importance) + tier_boost)
        scored.append(
            {
                "id": str(mem.get("id", mid)),
                "content": content[:500],
                "relevance": round(relevance, 4),
                "source": str(mem.get("source", "") or ""),
                "memory_type": str(mem.get("memory_type", "") or ""),
                "worth_score": round(worth, 4),
            }
        )

    def _preview_rank(item: dict) -> tuple:
        # adoption-audit: prefer experience/reflection memories, worth descending
        preferred = item.get("memory_type") in {"experience", "reflection"}
        return (1 if preferred else 0, item["worth_score"], item["relevance"])

    items = sorted(scored, key=_preview_rank, reverse=True)[:_LIGHT_CONTEXT_LIMIT]
    status = "ready" if items else ("degraded" if timed_out else "deferred")
    reason = (
        "light lexical memory preview; call context_supply before material decisions"
        if items
        else "no relevant light-context memory found; call context_supply before material decisions"
    )
    if timed_out:
        reason += "; light preview hit its timeout"

    # injection tracking: record what the brief actually surfaced
    try:
        from plastic_promise.mcp.tools.injection_tracking import record_injection

        record_injection(
            str(params.get("stage_session_id") or "adhoc"),
            "session_brief",
            [str(item.get("id") or "") for item in items],
            sum(len(str(item.get("content", "") or "")) for item in items),
        )
    except Exception:
        pass

    return {
        "status": status,
        "mode": "light",
        "reason": reason,
        "items": items,
        "item_count": len(items),
        "scanned_matches": scanned,
        "timeout_s": timeout_s,
        "timed_out": timed_out,
        "requires_full_context_before_action": True,
    }


async def _full_context_status(ctx, params: dict) -> dict:
    """Run full context_supply only when callers explicitly request it."""
    timeout_s = _context_timeout(params, _FULL_CONTEXT_TIMEOUT_S)
    task_description = str(params.get("task_description", "") or "")
    task_type = str(params.get("task_type", "general") or "general")
    scope = str(params.get("scope", "global") or "global")
    try:
        from plastic_promise.mcp.tools.context import handle_context_supply

        result = await asyncio.wait_for(
            handle_context_supply(
                ctx,
                {
                    "task_description": task_description,
                    "task_type": task_type,
                    "scope": scope,
                },
            ),
            timeout=timeout_s,
        )
        payload = json.loads(result[0].text) if result and hasattr(result[0], "text") else {}
        if isinstance(payload, dict) and payload.get("error"):
            return {
                "status": "degraded",
                "mode": "full",
                "reason": payload.get("error", "context_supply failed"),
                "timeout_s": timeout_s,
                "requires_full_context_before_action": True,
            }
        return {
            "status": "ready",
            "mode": "full",
            "reason": "full context_supply completed because context_mode=full was requested",
            "timeout_s": timeout_s,
            "context_pack": payload,
            "requires_full_context_before_action": False,
        }
    except TimeoutError:
        return {
            "status": "degraded",
            "mode": "full",
            "reason": f"context_supply timed out after {timeout_s:.1f}s",
            "timeout_s": timeout_s,
            "requires_full_context_before_action": True,
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "mode": "full",
            "reason": f"context_supply failed: {exc}",
            "timeout_s": timeout_s,
            "requires_full_context_before_action": True,
        }


async def _session_init_handler(ctx, params, atom_results):
    """session-init handler: assemble atom results into a unified bootstrap pack.

    session-init must stay lightweight. Task-specific retrieval and memory
    injection are explicit follow-up steps (`context_supply` / `memory_store`),
    not mandatory startup atoms.

    Atoms called before this handler:
    - principle_activate: {activated: [...], count: N}
    - scarf_reflect: {overall_score, dimensions: {Status, Certainty, ...}}
    - domain: {domains: {...}}
    - system: {memory: {...}, fuzzy_buffer: {...}}
    - defense: {trust: float, tier: str}
    - memory_gc: {dry_run: true, candidates_count: N}
    """

    def parse(result):
        """Extract parsed JSON dict from atom result list[TextContent]."""
        if result and hasattr(result[0], "text"):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    scarf_data = parse(atom_results.get("scarf_reflect"))
    mode, invalid_mode = _context_mode(params)
    if mode == "none":
        context_data = {
            "status": "deferred",
            "mode": "none",
            "reason": "context preload disabled; call context_supply for task-specific context",
            "requires_full_context_before_action": True,
        }
    elif mode == "full":
        context_data = await _full_context_status(ctx, params)
    else:
        context_data = _light_context_status(ctx, params)
    if invalid_mode is not None:
        context_data["requested_mode"] = invalid_mode
        context_data["mode_warning"] = "unknown context_mode; fell back to light"
    memory_data = {
        "stored": False,
        "status": "deferred",
        "reason": "session-init no longer writes startup memories synchronously",
    }
    domain_data = parse(atom_results.get("domain"))
    system_data = parse(atom_results.get("system"))
    defense_data = parse(atom_results.get("defense"))
    gc_data = parse(atom_results.get("memory_gc"))

    # ── Chain state: resume the exact persisted session + flow cursor ──
    stage_session_id = ""
    try:
        from plastic_promise.mcp.tools.skill_tracking import (
            get_stage_chain_state,
            resolve_stage_session_id,
        )

        stage_session_id = resolve_stage_session_id(params)
        workflow_contract = _build_workflow_contract(params, stage_session_id, ctx)
        chain_state = get_stage_chain_state(workflow_contract["flow_scope_id"], engine=ctx)
        chain_state["stage_session_id"] = stage_session_id
        chain_state["valid_next"] = (
            [workflow_contract["next_stage"]] if workflow_contract["next_stage"] else []
        )
        chain_state["predecessors"] = []
    except Exception:
        chain_state = None
        workflow_contract = None

    if not stage_session_id:
        stage_session_id = _nonempty_text(
            params.get("stage_session_id") or params.get("stage_id"),
            "default",
        )
    if workflow_contract is None:
        workflow_contract = _build_workflow_contract(params, stage_session_id, ctx)
    if chain_state is not None:
        chain_state.update(
            {
                "default_route": workflow_contract["default_route"],
                "route": workflow_contract["route"],
                "route_id": workflow_contract["route_id"],
                "flow_line_id": workflow_contract["flow_line_id"],
                "flow_scope_id": workflow_contract["flow_scope_id"],
                "entry_stage": workflow_contract["entry_stage"],
                "valid_root_entrypoints": workflow_contract["valid_root_entrypoints"],
                "governance_contract": workflow_contract["governance_contract"],
            }
        )

    component_health = _compile_component_health(ctx)

    return SkillResult(
        skill_name="session-init",
        success=True,
        data={
            "principles": principle_data.get("activated", []),
            "scarf_baseline": scarf_data,
            "context_status": context_data,
            "inject_memory_id": memory_data.get("memory_id", ""),
            "memory_injection_status": memory_data,
            "domain_health": domain_data,
            "system_stats": system_data,
            "trust": defense_data,
            "gc_preview": gc_data,
            "stage_session_id": stage_session_id,
            "workflow_contract": workflow_contract,
            "chain_state": chain_state,
            "component_health": component_health,
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ── Skill Definition ──

skill_session_init = SkillDef(
    name="session-init",
    domain="session_lifecycle",
    description="会话启动 — 封装 CLAUDE.md 步骤 0-5",
    tier="P0",
    atoms=[
        "principle_activate",
        "scarf_reflect",
        "domain",
        "system",
        "defense",
        "memory_gc",
    ],
    degrade_map={
        "domain": "skip",
        "system": "skip",
        "memory_gc": "skip",
        "defense": "warn",
        "scarf_reflect": "warn",
    },
    handler=_session_init_handler,
    allowed_callers=["claude", "pi"],
    atom_timeout_seconds=2.0,
    track_start_memory=False,
    concurrent=True,  # 性能优化：8个原子并行执行，将串行耗时降低为单次最长耗时
)
