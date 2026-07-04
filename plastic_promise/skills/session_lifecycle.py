"""域 1: Session Lifecycle skills — 会话生命周期管理"""

import asyncio
import json
import time

from plastic_promise.skills.engine import SkillDef, SkillResult


_CONTEXT_MODES = {"none", "light", "full"}
_LIGHT_CONTEXT_LIMIT = 2
_LIGHT_CONTEXT_TIMEOUT_S = 1.5
_FULL_CONTEXT_TIMEOUT_S = 10.0


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
        from plastic_promise.core.embedder import get_embedder

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
    memories = getattr(ctx, "_memories", None)
    if not isinstance(memories, dict):
        return {
            "status": "deferred",
            "mode": "light",
            "reason": "memory pool unavailable; call context_supply before material decisions",
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
    for mid, mem in memories.items():
        if time.monotonic() > deadline:
            timed_out = True
            break
        if not isinstance(mem, dict) or _is_deleted_or_forgotten(mem):
            continue
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
                "worth_score": round(worth, 4),
            }
        )

    items = sorted(scored, key=lambda item: item["relevance"], reverse=True)[
        :_LIGHT_CONTEXT_LIMIT
    ]
    status = "ready" if items else ("degraded" if timed_out else "deferred")
    reason = (
        "light lexical memory preview; call context_supply before material decisions"
        if items
        else "no relevant light-context memory found; call context_supply before material decisions"
    )
    if timed_out:
        reason += "; light preview hit its timeout"

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

    # ── Chain state: report current SKILL_CHAIN_MAP position ──
    try:
        from plastic_promise.core.constants import (
            SKILL_CHAIN_MAP as _CHAIN_MAP,
            normalize_stage_name,
        )
        from plastic_promise.mcp.tools.skill_tracking import get_current_stage

        current_stage = normalize_stage_name(get_current_stage())
        chain_state = None
        if current_stage:
            chain = _CHAIN_MAP.get(current_stage) or _CHAIN_MAP.get(f"sp-{current_stage}", {})
            chain_state = {
                "current_stage": current_stage,
                "valid_next": [normalize_stage_name(s) for s in chain.get("successors", [])],
                "predecessors": [normalize_stage_name(s) for s in chain.get("predecessors", [])],
            }
    except Exception:
        chain_state = None

    component_health = _compile_component_health(ctx)

    return SkillResult(
        skill_name="session-init",
        success=True,
        data={
            "principles": principle_data.get("activated", []),
            "scarf_baseline": scarf_data,
            "context": context_data,
            "context_status": context_data,
            "inject_memory_id": memory_data.get("memory_id", ""),
            "memory_injection_status": memory_data,
            "domain_health": domain_data,
            "system_stats": system_data,
            "trust": defense_data,
            "gc_preview": gc_data,
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
