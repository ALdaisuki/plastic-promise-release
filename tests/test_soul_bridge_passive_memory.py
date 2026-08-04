from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from plastic_promise.core.soul_bridge import SoulBridge


class _Trust:
    def __init__(self):
        self.score = 0.8

    def boost(self, delta, reason="", *, target=""):
        self.score += delta
        return self.score

    def decay(self, delta, reason="", *, target=""):
        self.score -= delta
        return self.score

    def get(self, target=""):
        return self.score


class _SoulLoop:
    def __init__(self):
        self._engine = object()
        self.calls = []

    def post_task(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


@pytest.fixture
def bridge():
    instance = SoulBridge()
    instance._initialized = True
    instance._trust = _Trust()
    instance._soul_loop = _SoulLoop()
    instance._hormone = SimpleNamespace(apply_feedback=lambda *_args, **_kwargs: None)
    instance._proprioception = SimpleNamespace(record_task=lambda *_args, **_kwargs: None)
    instance._enforcer = SimpleNamespace(pre_check=lambda *_args, **_kwargs: {"blocked": False})
    instance._scarf = SimpleNamespace(reflect=lambda *_args, **_kwargs: {"certainty": 1.0})
    return instance


@pytest.fixture
def passive_calls(monkeypatch):
    calls = []

    def capture(_engine, event):
        calls.append(dict(event))
        return True

    monkeypatch.setattr("plastic_promise.passive_memory.schedule_after_invoke", capture)
    return calls


def _install_context_handler(monkeypatch, *, await_once=False):
    async def handler(_engine, _args):
        if await_once:
            await asyncio.sleep(0)
        return [SimpleNamespace(text=json.dumps({"context_pack": {"core": []}}))]

    monkeypatch.setattr(
        "plastic_promise.mcp.tools.context.handle_auto_context_inject",
        handler,
    )


def test_post_task_schedules_async_audit_and_prefers_explicit_user_text(
    bridge, passive_calls, monkeypatch
):
    _install_context_handler(monkeypatch)
    asyncio.run(bridge.pre_task("Remember that I prefer Rust.", "building"))

    result = bridge.post_task(
        "Assistant result must not become a user fact.",
        "building",
        user_text="Remember that I prefer TypeScript.",
        call_id="call:explicit",
    )

    assert result["passive_memory_scheduled"] is True
    assert passive_calls == [
        {
            "event": "after_invoke",
            "task_description": "Remember that I prefer TypeScript.",
            "task_type": "building",
            "source": "soul_bridge",
            "user_text": "Remember that I prefer TypeScript.",
            "assistant_text": "Assistant result must not become a user fact.",
            "call_id": "call:explicit",
            "metadata": {"success": True},
        }
    ]
    assert bridge._soul_loop.calls == [
        {"task_description": "Assistant result must not become a user fact.", "mode": "full"}
    ]


def test_concurrent_tasks_keep_passive_user_context_isolated(bridge, passive_calls, monkeypatch):
    _install_context_handler(monkeypatch, await_once=True)

    async def run_task(task_text, call_id):
        await bridge.pre_task(task_text, "building")
        await asyncio.sleep(0)
        bridge.post_task("done", "building", call_id=call_id)

    async def run_all():
        await asyncio.gather(
            run_task("Remember that I prefer TypeScript.", "call:a"),
            run_task("Remember that I prefer Rust.", "call:b"),
        )

    asyncio.run(run_all())

    assert {call["call_id"]: call["user_text"] for call in passive_calls} == {
        "call:a": "Remember that I prefer TypeScript.",
        "call:b": "Remember that I prefer Rust.",
    }


def test_thread_local_fallback_survives_asyncio_run_boundary(bridge, passive_calls, monkeypatch):
    _install_context_handler(monkeypatch)

    asyncio.run(bridge.pre_task("Remember that I prefer concise responses.", "general"))
    result = bridge.post_task("done", "general", call_id="call:fallback")

    assert result["passive_memory_scheduled"] is True
    assert passive_calls[0]["user_text"] == "Remember that I prefer concise responses."
