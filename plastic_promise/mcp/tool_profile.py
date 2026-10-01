"""Tool surface profiles — core | full.

Audit 2026-10-02: 62 declared tools, 16 ever called, 46 zero-call over 40 days.
Profile gating hides dead surface from list_tools without deleting any
handler: archived tools stay dispatchable when explicitly called (dispatch
layer stays fail-open) and reappear in the full profile.

Environment:
    PP_TOOL_PROFILE=core|full   (default: core)
"""

from __future__ import annotations

import os
from typing import Iterable

PROFILE_ENV = "PP_TOOL_PROFILE"
PROFILE_CORE = "core"
PROFILE_FULL = "full"
DEFAULT_PROFILE = PROFILE_CORE

# Tools kept visible in the core profile. Two sources: the 16 names with real
# 40-day telemetry, plus zero-call names that belong to a live capability lane
# (retrieval, guild lifecycle, Issue flow, context supply).
CORE_KEEP: frozenset[str] = frozenset(
    {
        # ── A 档：40 天真实使用面（16）─────────────────────────────
        "auto_context_inject",
        "audit_pre_check",
        "step-closure",
        "audit_run",
        "session-init",
        "memory_store",
        "context_supply",
        "defense",
        "sp-stage",
        "scarf_reflect",
        "principle_evaluate",
        "issue_create",
        "collaboration_work_register",
        "collaboration_work_claim",
        "collaboration_work_review",
        "market_list",
        # ── B 档：零遥测但属活跃能力通道（18，T1 分档 KEEP-CORE）────
        "memory_recall",
        "memory_update",
        "memory_forget",
        "memory_list",
        "issue_transition",
        "issue_list",
        "task_enqueue",
        "task_claim",
        "task_complete",
        "task_verify",
        "task_inbox",
        "task_heartbeat",
        "task_abandon",
        "collaboration_work_list",
        "collaboration_lease_heartbeat",
        "collaboration_work_accept",
        "review_run",
        "smart-remember",
    }
)

# Hidden from list_tools but invoked by the engine/daemons directly, so their
# zero telemetry does NOT mean dead code (T1 classification, INTERNAL-ONLY 13).
# Keeping them out of the visible surface trims agent confusion without
# removing capability: dispatch stays fail-open and full profile lists them.
INTERNAL_LIVE: frozenset[str] = frozenset(
    {
        "memory_gc",
        "memory_reclassify",
        "memory_sync_files",
        "principle_activate",
        "context_inject",
        "feedback_apply",
        "system",
        "domain",
        "skill_session_start",
        "skill_session_complete",
        "skill_session_trace",
        "skill_auto_track",
        "audit_rollover",
    }
)

# Overlapping or paired surfaces collapsed out of the default view
# (T1 classification, ALIAS-MERGE 4).
ALIAS_MERGE_HIDDEN: frozenset[str] = frozenset(
    {"memory_correct", "market_enable", "market_disable", "market_status"}
)

# Aliases collapse to a canonical entry; the alias keeps working at dispatch.
ALIAS_TO_CANONICAL: dict[str, str] = {
    "session_init": "session-init",
    "step_closure": "step-closure",
    "smart_remember": "smart-remember",
    "sp_stage": "sp-stage",
}


def active_profile(environ: dict | None = None) -> str:
    """Return the configured profile, defaulting to core."""

    env = environ if environ is not None else os.environ
    raw = str(env.get(PROFILE_ENV) or "").strip().casefold()
    return PROFILE_FULL if raw == PROFILE_FULL else DEFAULT_PROFILE


def filter_tools(names: Iterable[str], profile: str | None = None) -> list[str]:
    """Apply profile + alias collapse to a declared name list."""

    resolved = profile or active_profile()
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        canonical = ALIAS_TO_CANONICAL.get(name, name)
        if canonical in seen:
            continue
        if resolved == PROFILE_FULL or canonical in CORE_KEEP:
            seen.add(canonical)
            kept.append(canonical)
    return kept


def is_hidden(name: str, profile: str | None = None) -> bool:
    """True when a declared name is hidden by the active profile."""

    resolved = profile or active_profile()
    if resolved == PROFILE_FULL:
        return False
    return ALIAS_TO_CANONICAL.get(name, name) not in CORE_KEEP
