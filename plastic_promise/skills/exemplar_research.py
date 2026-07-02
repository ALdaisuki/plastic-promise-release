"""Exemplar Research -- sp-stage handler for exemplar-research phase.

Part of the SuperPowers 12-stage pipeline. Sits between brainstorming
and using-git-worktrees in the main chain.

Execution flow:
  1. Read gap_signal (if present) or extract search intent from task_description
  2. WebSearch for mature implementations
  3. Three-question analysis (problem / pattern / constraints)
  4. Write analysis doc to engineering-patterns/ (status=draft)
  5. Quality review via verify_exemplar task
  6. On approval -> smart-remember dual-write to memory pool
  7. Complete -> exemplar available in subsequent context_supply calls
"""

import json
import os
from datetime import datetime

from plastic_promise.skills.engine import SkillResult


async def _exemplar_research_handler(ctx, params, atom_results):
    """Handler for sp-stage exemplar-research.

    ctx: ContextEngine instance
    params: dict with task_description and optional gap_signal
    atom_results: dict with principle_activate and memory_store results
    """
    task_desc = params.get("task_description", "exemplar research")
    gap_signal = params.get("gap_signal", None)

    # -- 1. Determine search target ---------------------------------
    if gap_signal and isinstance(gap_signal, dict):
        problem = gap_signal.get("problem", task_desc)
        search_hints = gap_signal.get("suggested_search", [])
    else:
        problem = task_desc
        # Extract search hints from task_description
        try:
            from plastic_promise.core.exemplar_gap_detector import _extract_keywords
            search_hints = _extract_keywords(task_desc)
        except ImportError:
            search_hints = []

    search_query = " ".join(search_hints) if search_hints else problem

    # -- 2. Parse atom results --------------------------------------
    def parse(result):
        if result and hasattr(result[0], 'text'):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    store_data = parse(atom_results.get("memory_store"))

    # -- 3. Return SkillResult with exemplar context -----------------
    # The actual WebSearch + analysis + doc writing + review
    # is performed by Claude in the conversation after receiving
    # this result. The handler provides the structured context
    # and instructions.

    return SkillResult(
        skill_name="sp-exemplar-research",
        success=True,
        data={
            "stage": "exemplar-research",
            "domain": "designing",
            "tags": ["stage:exemplar-research", "domain:designing", "task:research"],
            "principles": principle_data.get("activated", []),
            "memory_id": store_data.get("memory_id", ""),
            "exemplar": {
                "problem": problem,
                "search_query": search_query,
                "search_hints": search_hints,
                "gap_signal": gap_signal,
                "instructions": (
                    "1. Use WebSearch to find mature implementations for: " + search_query + "\n"
                    "2. Apply three-question analysis:\n"
                    "   a. What problem does it solve?\n"
                    "   b. How does it solve it? (algorithm / data structure / flow)\n"
                    "   c. What parts cannot be used directly? (language / architecture / constraints)\n"
                    "3. Write analysis doc to docs/superpowers/specs/engineering-patterns/YYYY-MM-DD-<project>.md\n"
                    "4. Set status=draft in frontmatter\n"
                    "5. Create verify_exemplar task for quality review\n"
                    "6. On approval -> smart-remember(memory_type='exemplar') to memory pool\n"
                    "7. Update INDEX.md with new entry and status marking"
                ),
            },
            "transition": "-> exemplar-research -> using-git-worktrees",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ============================================================
# SkillDef registration
# ============================================================

EXEMPLAR_RESEARCH_SKILL_DEF = {
    "name": "sp-exemplar-research",
    "domain": "superpowers_stages",
    "description": "SuperPowers stage: exemplar research -- search mature implementations, three-question analysis, write analysis doc, quality review then store",
    "tier": "P0",
    "atoms": ["principle_activate", "memory_store"],
    "degrade_map": {
        "principle_activate": "skip",
        "memory_store": "warn",
    },
    "handler": _exemplar_research_handler,
    "allowed_callers": ["claude", "pi", "trae"],
}
