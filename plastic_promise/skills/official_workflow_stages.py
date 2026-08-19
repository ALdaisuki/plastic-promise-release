"""Programmatic adapter for the pinned Matt Pocock engineering workflows.

The public MCP tool remains ``sp-stage`` for client compatibility. Its stage
names, invocation authority, route catalog, and handoffs come exclusively from
the pinned upstream engineering skills.
"""

from __future__ import annotations

import json

from mcp.types import TextContent

from plastic_promise.core.agent_tool_policy import policy_receipt
from plastic_promise.core.official_workflow import OFFICIAL_SKILLS
from plastic_promise.skills.closure_runner import run_post_task_best_effort
from plastic_promise.skills.engine import SkillDef, SkillResult
from plastic_promise.skills.tool_routing import (
    OFFICIAL_WORKFLOW_ROUTES,
    invocation_policy,
)

ENGINEERING_STAGE_CONFIG = {
    name: (
        skill.domain,
        skill.layer,
        skill.artifact,
        skill.closure_mode,
    )
    for name, skill in OFFICIAL_SKILLS.items()
}

STAGE_ROUTE_MAP = {
    route_id: {
        "label": route["label"],
        "summary": route["summary"],
        "stages": list(route["stages"]),
        "branches": dict(route.get("branches") or {}),
    }
    for route_id, route in OFFICIAL_WORKFLOW_ROUTES.items()
}

STAGE_DEFAULT_ROUTE_MAP = {
    "setup-matt-pocock-skills": "setup",
    "ask-matt": "routing",
    "grill-with-docs": "idea-to-ship",
    "grill-me": "grill-me",
    "grilling": "grilling",
    "to-spec": "spec-to-ship",
    "to-tickets": "tickets-to-ship",
    "implement": "implement-to-review",
    "tdd": "tdd-to-review",
    "diagnosing-bugs": "bug-onramp",
    "research": "research-feed",
    "prototype": "prototype",
    "resolving-merge-conflicts": "merge-conflict",
    "code-review": "review",
    "triage": "triage-to-ship",
    "wayfinder": "wayfinder-to-ship",
    "improve-codebase-architecture": "architecture-feed",
    "domain-modeling": "domain-modeling",
    "codebase-design": "codebase-design",
    "handoff": "handoff",
    "teach": "teach",
    "writing-great-skills": "writing-great-skills",
}

STAGE_DOMAIN_MAP = {name: values[0] for name, values in ENGINEERING_STAGE_CONFIG.items()}
STAGE_TAGS_MAP = {
    name: [f"stage:{name}", f"domain:{values[0]}", "workflow:mattpocock"]
    for name, values in ENGINEERING_STAGE_CONFIG.items()
}
STAGE_DESCRIPTIONS = {
    name: f"Official engineering skill: {values[2]}"
    for name, values in ENGINEERING_STAGE_CONFIG.items()
}


def resolve_stage_route(stage_name: str, route_id: str | None = None) -> str:
    requested = str(route_id or "").strip()
    if requested in STAGE_ROUTE_MAP:
        return requested
    if stage_name in STAGE_DEFAULT_ROUTE_MAP:
        return STAGE_DEFAULT_ROUTE_MAP[stage_name]
    return "idea-to-ship"


def _build_route_summary(stage_name: str, route_id: str | None = None) -> dict:
    resolved_route = resolve_stage_route(stage_name, route_id)
    route = STAGE_ROUTE_MAP[resolved_route]
    stages = list(route["stages"])
    current_index = stages.index(stage_name) if stage_name in stages else None
    next_stage = (
        stages[current_index + 1]
        if current_index is not None and current_index + 1 < len(stages)
        else None
    )
    return {
        "route_id": resolved_route,
        "label": route["label"],
        "summary": route["summary"],
        "stages": stages,
        "stage_authority": {stage: invocation_policy(stage) for stage in stages},
        "branches": dict(route.get("branches") or {}),
        "current_stage": stage_name,
        "current_index": current_index,
        "next_stage": next_stage,
        "session_isolation": (
            "Use stage_session_id plus flow_line_id to isolate concurrent workflow lines."
        ),
    }


def build_stage_guidance(
    stage_name: str,
    closed: bool | None = None,
    route_id: str | None = None,
) -> dict:
    if stage_name not in ENGINEERING_STAGE_CONFIG:
        raise ValueError(f"unknown official engineering stage: {stage_name}")
    domain, layer, artifact, closure_mode = ENGINEERING_STAGE_CONFIG[stage_name]
    delegated_roles = (
        ["deepsec_reviewer"]
        if stage_name == "code-review"
        else ["research_reader"]
        if stage_name == "research"
        else []
    )
    return {
        "stage_summary": {
            "stage": stage_name,
            "layer": layer,
            "summary": STAGE_DESCRIPTIONS[stage_name],
            "invocation_authority": invocation_policy(stage_name),
        },
        "route_summary": _build_route_summary(stage_name, route_id=route_id),
        "required_artifacts": [
            {
                "kind": "engineering_workflow_evidence",
                "path": artifact,
                "required": True,
            }
        ],
        "closure_reminder": {
            "tool": "step-closure",
            "mode": closure_mode,
            "current_stage": stage_name,
            "required": closure_mode == "full",
            "sp_stage_closed": closed,
            "message": (
                f"Run step-closure(mode='{closure_mode}') after substantive verified output."
            ),
        },
        "delegation_policy": {
            "roles": delegated_roles,
            "receipts": [policy_receipt(role) for role in delegated_roles],
            "enforcement": {
                "required_argument": "agent_role",
                "transport_boundary": "mcp.call_tool",
                "fail_closed": True,
                "message": (
                    "Delegated calls may include agent_role; the MCP dispatcher "
                    "authorizes the allowlisted tool and action before handler execution. "
                    "Transport-level identity binding is not available on this MCP surface."
                ),
            },
            "default": "no delegated role; the caller keeps its existing tool boundary",
        },
    }


def attach_stage_guidance(
    data: dict,
    stage_name: str,
    closed: bool | None = None,
    route_id: str | None = None,
) -> dict:
    result = data if isinstance(data, dict) else {}
    result.setdefault(
        "stage_guidance",
        build_stage_guidance(stage_name, closed=closed, route_id=route_id),
    )
    return result


async def _governance_step_closure_light(ctx, params: dict):
    closure = await run_post_task_best_effort(
        task_description=params.get("task_description", "official-workflow-light"),
        mode="light",
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "closed": closure.completed,
                    "mode": "light",
                    "timed_out": closure.timed_out,
                    "skipped": closure.skipped,
                    "reason": closure.reason,
                },
                ensure_ascii=False,
            ),
        )
    ]


async def _governance_step_closure_full(ctx, params: dict):
    task_desc = params.get("task_description", "official-workflow-full")
    closure = await run_post_task_best_effort(
        task_description=task_desc,
        git_commit=params.get("git_commit", ""),
        mode="full",
        lesson=params.get("lesson") or f"official workflow: {task_desc[:100]}",
        improvement=params.get("improvement") or "Follow the selected official workflow handoff.",
        root_cause=params.get("root_cause") or "Stage completed normally.",
        optimization=params.get("optimization")
        or "Continue to the next applicable official skill.",
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "closed": closure.completed,
                    "mode": "full",
                    "timed_out": closure.timed_out,
                    "skipped": closure.skipped,
                    "reason": closure.reason,
                },
                ensure_ascii=False,
            ),
        )
    ]


def _parse_atom(result) -> dict:
    if result and hasattr(result[0], "text"):
        try:
            return json.loads(result[0].text)
        except (json.JSONDecodeError, TypeError):
            return {"raw": result[0].text}
    return {}


async def _stage_handler(ctx, params, atom_results, stage_name):
    closure = _parse_atom(
        atom_results.get("step_closure_light") or atom_results.get("step_closure_full")
    )
    return SkillResult(
        skill_name=f"sp-{stage_name}",
        success=True,
        data={
            "stage": stage_name,
            "domain": STAGE_DOMAIN_MAP[stage_name],
            "tags": STAGE_TAGS_MAP[stage_name],
            "trust": _parse_atom(atom_results.get("defense")) or "unchecked",
            "closed": closure.get("closed") if closure else None,
            "stage_guidance": build_stage_guidance(
                stage_name,
                closed=closure.get("closed") if closure else None,
                route_id=params.get("route"),
            ),
            "transition": f"-> {stage_name}",
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


def _make_handler(stage_name):
    async def handler(ctx, params, atom_results):
        return await _stage_handler(ctx, params, atom_results, stage_name)

    return handler


STAGE_ATOMS = {}
for _stage_name, (_domain, _layer, _artifact, _closure_mode) in ENGINEERING_STAGE_CONFIG.items():
    STAGE_ATOMS[_stage_name] = [
        "defense",
        "principle_activate",
    ]

STAGE_DEGRADE = {
    "principle_activate": "skip",
    "defense": "warn",
    "step_closure_light": "skip",
    "step_closure_full": "warn",
}

SKILL_DEFS = {
    stage_name: SkillDef(
        name=f"sp-{stage_name}",
        domain=STAGE_DOMAIN_MAP[stage_name],
        description=STAGE_DESCRIPTIONS[stage_name],
        tier="P0",
        atoms=atoms,
        degrade_map=STAGE_DEGRADE,
        handler=_make_handler(stage_name),
        allowed_callers=["claude", "pi", "trae"],
        atom_timeout_seconds=5.0,
        track_start_memory=False,
    )
    for stage_name, atoms in STAGE_ATOMS.items()
}


def trigger_plugin_hooks(stage_name: str, params: dict) -> list[dict]:
    """Trigger optional extension hooks without blocking the official flow."""
    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        loader.activate_all()
        results = loader.trigger_hooks(
            f"on_before_{stage_name.replace('-', '_')}",
            {"task_description": params.get("task_description", ""), "to_stage": stage_name},
        )
        return [result for result in results if result]
    except Exception:
        return []


def transition_plugin_hooks(from_stage: str, to_stage: str, params: dict) -> list[dict]:
    """Trigger optional transition hooks without blocking the official flow."""
    try:
        from plastic_promise.extensions.loader import PluginLoader

        loader = PluginLoader()
        loader.discover()
        loader.activate_all()
        results = loader.trigger_hooks(
            f"on_transition_{from_stage.replace('-', '_')}_{to_stage.replace('-', '_')}",
            {
                "task_description": params.get("task_description", ""),
                "from_stage": from_stage,
                "to_stage": to_stage,
            },
        )
        return [result for result in results if result]
    except Exception:
        return []
