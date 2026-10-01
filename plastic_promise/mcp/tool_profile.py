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
        # 真实使用面（16）
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
        # 零调用但属核心能力（保留可用）
        "memory_recall",
        "memory_list",
        "issue_list",
        "issue_transition",
        "task_enqueue",
        "task_claim",
        "task_complete",
        "task_verify",
        "task_inbox",
        "task_heartbeat",
        "task_abandon",
        "smart-remember",
    }
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
