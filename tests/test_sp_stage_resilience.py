import asyncio
import json
import time


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
