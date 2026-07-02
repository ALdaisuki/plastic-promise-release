"""域 1: Session Lifecycle skills — 会话生命周期管理"""

import json

from plastic_promise.skills.engine import SkillDef, SkillResult


async def _session_init_handler(ctx, params, atom_results):
    """session-init handler: assemble atom results into a unified context pack.

    Atoms called before this handler:
    - principle_activate: {activated: [...], count: N}
    - scarf_reflect: {overall_score, dimensions: {Status, Certainty, ...}}
    - context_supply: ContextPack JSON (core/related/divergent)
    - memory_store: {stored: true, memory_id: "..."}
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
    context_data = {}
    context_raw = atom_results.get("context_supply")
    if context_raw and hasattr(context_raw[0], "text"):
        context_data = {
            "prompt": context_raw[0].text
        }  # ContextPack.to_prompt() returns formatted text
    memory_data = parse(atom_results.get("memory_store"))
    domain_data = parse(atom_results.get("domain"))
    system_data = parse(atom_results.get("system"))
    defense_data = parse(atom_results.get("defense"))
    gc_data = parse(atom_results.get("memory_gc"))

    # ── Chain state: report current SKILL_CHAIN_MAP position ──
    try:
        from plastic_promise.mcp.tools.skill_tracking import get_current_stage
        from plastic_promise.core.constants import SKILL_CHAIN_MAP as _CHAIN_MAP

        current_stage = get_current_stage()
        chain_state = None
        if current_stage:
            lookup = current_stage.replace("sp-", "")
            chain = _CHAIN_MAP.get(lookup) or _CHAIN_MAP.get(f"sp-{lookup}", {})
            chain_state = {
                "current_stage": current_stage,
                "valid_next": chain.get("successors", []),
                "predecessors": chain.get("predecessors", []),
            }
    except Exception:
        chain_state = None

    return SkillResult(
        skill_name="session-init",
        success=True,
        data={
            "principles": principle_data.get("activated", []),
            "scarf_baseline": scarf_data,
            "context": context_data,
            "inject_memory_id": memory_data.get("memory_id", ""),
            "domain_health": domain_data,
            "system_stats": system_data,
            "trust": defense_data,
            "gc_preview": gc_data,
            "chain_state": chain_state,
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
        "context_supply",
        "memory_store",
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
    concurrent=True,  # 性能优化：8个原子并行执行，将串行耗时降低为单次最长耗时
)
