import asyncio
import json
import time

from mcp.types import TextContent


def test_sp_stage_defs_use_entity_only_tracking():
    from plastic_promise.skills.superpowers_stages import SKILL_DEFS

    assert SKILL_DEFS
    for stage_name, skill_def in SKILL_DEFS.items():
        assert skill_def.track_start_memory is False, stage_name


async def _call_full_closure():
    from plastic_promise.skills.superpowers_stages import _governance_step_closure_full

    result = await _governance_step_closure_full(
        None,
        {
            "task_description": "blocked closure regression",
            "lesson": "exercise timeout path",
            "improvement": "return promptly",
            "root_cause": "blocking post_task",
            "optimization": "bound closure latency",
        },
    )
    return json.loads(result[0].text)


def test_sp_stage_closure_returns_promptly_when_post_task_blocks(monkeypatch):
    def blocking_post_task(**kwargs):
        time.sleep(0.25)
        return {"alignment": {"checked": 1}}

    monkeypatch.setenv("PP_STEP_CLOSURE_TIMEOUT_SEC", "0.01")
    monkeypatch.setattr("plastic_promise.loop.soul_loop.post_task", blocking_post_task)

    started = time.perf_counter()
    payload = asyncio.run(_call_full_closure())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert payload["closed"] is False
    assert payload["timed_out"] is True

    # Let the background post_task finish before the event loop shuts down fully
    # in slower Python runtimes.
    time.sleep(0.30)


def test_generic_stage_handler_includes_brainstorming_guidance():
    from plastic_promise.skills.superpowers_stages import _stage_handler

    atom_results = {
        "defense": [TextContent(type="text", text=json.dumps({"trust": 0.6}))],
        "step_closure_light": [
            TextContent(type="text", text=json.dumps({"closed": True, "mode": "light"}))
        ],
    }

    result = asyncio.run(_stage_handler(None, {}, atom_results, "brainstorming"))
    guidance = result.data["stage_guidance"]

    assert guidance["stage_summary"]["stage"] == "brainstorming"
    assert guidance["required_artifacts"][0]["path"] == (
        "docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md"
    )
    assert guidance["closure_reminder"]["current_stage"] == "brainstorming"
    assert guidance["closure_reminder"]["mode"] == "light"
    assert guidance["closure_reminder"]["sp_stage_closed"] is True


def test_writing_plans_guidance_requires_plan_artifact():
    from plastic_promise.skills.superpowers_stages import build_stage_guidance

    guidance = build_stage_guidance("writing-plans", closed=False)

    assert guidance["stage_summary"]["layer"] == "planning"
    assert guidance["required_artifacts"][0]["path"] == (
        "docs/superpowers/plans/YYYY-MM-DD-<feature>.md"
    )
    assert "step-closure" in guidance["closure_reminder"]["message"]
    assert guidance["closure_reminder"]["sp_stage_closed"] is False
    assert "official_skill" not in guidance
    assert "skill_authority" not in guidance["stage_summary"]
    assert "next_actions" not in guidance
    assert "handoff_summary" not in guidance
    assert set(guidance) == {
        "stage_summary",
        "route_summary",
        "required_artifacts",
        "closure_reminder",
    }
    assert set(guidance["stage_summary"]) == {"stage", "layer", "summary"}
    assert set(guidance["route_summary"]) == {
        "route_id",
        "label",
        "summary",
        "stages",
        "current_stage",
        "current_index",
        "session_isolation",
    }


def test_bug_hunt_route_guidance_keeps_programmatic_session_contract():
    from plastic_promise.skills.superpowers_stages import build_stage_guidance

    guidance = build_stage_guidance("systematic-debugging")

    assert guidance["route_summary"]["route_id"] == "bug-hunt"
    assert guidance["route_summary"]["stages"][0] == "systematic-debugging"
    assert "stage_session_id" in guidance["route_summary"]["session_isolation"]
    assert "flow_line_id" in guidance["route_summary"]["session_isolation"]
    serialized = json.dumps(guidance)
    assert "official_skill" not in serialized
    assert "skill_authority" not in serialized
    assert "load/read" not in serialized


def test_custom_route_guidance_keeps_explicit_route_id():
    from plastic_promise.skills.superpowers_stages import build_stage_guidance

    guidance = build_stage_guidance("writing-plans", route_id="release-hardening")

    assert guidance["route_summary"]["route_id"] == "release-hardening"
    assert guidance["route_summary"]["label"] == "Custom route"
    assert guidance["route_summary"]["stages"] == ["writing-plans"]


def test_using_superpowers_guidance_is_bootstrap_stage():
    from plastic_promise.skills.superpowers_stages import build_stage_guidance

    guidance = build_stage_guidance("using-superpowers")

    assert guidance["stage_summary"]["layer"] == "bootstrap"
    assert guidance["route_summary"]["route_id"] == "superpowers-bootstrap"
    assert guidance["route_summary"]["stages"][0] == "using-superpowers"
    assert guidance["required_artifacts"] == [
        {
            "kind": "workflow_scope",
            "path": "stage_session_id + route + flow_line_id",
            "required": True,
        }
    ]
    assert "official" not in json.dumps(guidance).lower()


def test_writing_skills_guidance_uses_skill_authoring_route():
    from plastic_promise.skills.superpowers_stages import build_stage_guidance

    guidance = build_stage_guidance("writing-skills")

    assert guidance["stage_summary"]["layer"] == "skill-authoring"
    assert guidance["route_summary"]["route_id"] == "skill-authoring"
    assert guidance["route_summary"]["stages"] == ["writing-skills"]
    assert guidance["closure_reminder"]["mode"] == "light"
    assert "official" not in json.dumps(guidance).lower()
