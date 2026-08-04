import asyncio
import json
import time

from mcp.types import TextContent


def test_official_stage_defs_use_entity_only_tracking():
    from plastic_promise.skills.official_workflow_stages import SKILL_DEFS

    assert SKILL_DEFS
    for stage_name, skill_def in SKILL_DEFS.items():
        assert skill_def.track_start_memory is False, stage_name


async def _call_full_closure():
    from plastic_promise.skills.official_workflow_stages import _governance_step_closure_full

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


def test_official_stage_closure_returns_promptly_when_post_task_blocks(monkeypatch):
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
    time.sleep(0.30)


def test_stage_handler_includes_official_bug_guidance():
    from plastic_promise.skills.official_workflow_stages import _stage_handler

    atom_results = {
        "defense": [TextContent(type="text", text=json.dumps({"trust": 0.6}))],
        "step_closure_full": [
            TextContent(type="text", text=json.dumps({"closed": True, "mode": "full"}))
        ],
    }

    result = asyncio.run(
        _stage_handler(
            None,
            {"route": "bug-onramp"},
            atom_results,
            "diagnosing-bugs",
        )
    )
    guidance = result.data["stage_guidance"]

    assert guidance["stage_summary"]["stage"] == "diagnosing-bugs"
    assert guidance["stage_summary"]["invocation_authority"] == "model"
    assert guidance["route_summary"]["route_id"] == "bug-onramp"
    assert guidance["route_summary"]["next_stage"] == "tdd"
    assert guidance["closure_reminder"]["sp_stage_closed"] is True


def test_tdd_guidance_requires_verified_vertical_slice():
    from plastic_promise.skills.official_workflow_stages import build_stage_guidance

    guidance = build_stage_guidance("tdd", closed=False, route_id="bug-onramp")

    assert guidance["stage_summary"]["layer"] == "testing"
    assert guidance["required_artifacts"][0]["path"] == (
        "A red-green vertical slice at an agreed test seam."
    )
    assert guidance["closure_reminder"]["mode"] == "full"
    assert guidance["closure_reminder"]["sp_stage_closed"] is False
    assert guidance["route_summary"]["stages"] == [
        "diagnosing-bugs",
        "tdd",
        "code-review",
    ]


def test_user_only_stage_guidance_never_claims_model_authority():
    from plastic_promise.skills.official_workflow_stages import build_stage_guidance

    guidance = build_stage_guidance("grill-with-docs", route_id="idea-to-ship")

    assert guidance["stage_summary"]["invocation_authority"] == "user"
    assert guidance["route_summary"]["stage_authority"]["grill-with-docs"] == "user"
    assert guidance["route_summary"]["stage_authority"]["implement"] == "user"
    assert "tdd" not in guidance["route_summary"]["stage_authority"]


def test_setup_stage_is_registered_without_restoring_old_chain():
    from plastic_promise.skills.official_workflow_stages import SKILL_DEFS, build_stage_guidance

    assert "setup-matt-pocock-skills" in SKILL_DEFS
    guidance = build_stage_guidance("setup-matt-pocock-skills")
    assert guidance["stage_summary"]["layer"] == "bootstrap"
    assert guidance["stage_summary"]["invocation_authority"] == "user"
