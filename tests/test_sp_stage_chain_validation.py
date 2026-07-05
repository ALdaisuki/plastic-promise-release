import asyncio
import json
from types import SimpleNamespace

import plastic_promise.mcp.server as mcp_server
import plastic_promise.mcp.tools.skill_tracking as skill_tracking
from plastic_promise.core.constants import normalize_stage_name


class _FakeSkillEngine:
    def __init__(self):
        self.calls = []

    async def exec(self, skill_name, params, caller="claude"):
        self.calls.append((skill_name, params, caller))
        return SimpleNamespace(success=True, data={"skill_name": skill_name}, errors=[])


def _set_current_stage(stage):
    with skill_tracking._skill_state_lock:
        skill_tracking._current_stage = stage
        skill_tracking._current_skill = None
        skill_tracking._current_entity_id = None


def _run_sp_stage(monkeypatch, stage, current="review"):
    fake = _FakeSkillEngine()
    _set_current_stage(current)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)
    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            {"stage": stage, "task_description": "chain validation regression"},
        )
    )
    return json.loads(result[0].text), fake


def test_normalize_review_alias_to_receiving_code_review():
    assert normalize_stage_name("review") == "receiving-code-review"
    assert normalize_stage_name("sp-review") == "receiving-code-review"
    assert normalize_stage_name("superpowers:receive-review") == "receiving-code-review"


def test_sp_stage_allows_audit_after_review_alias(monkeypatch):
    data, fake = _run_sp_stage(monkeypatch, "audit", current="review")

    assert data["success"] is True
    assert fake.calls[0][0] == "sp-audit"


def test_sp_stage_allows_root_stage_from_stale_review(monkeypatch):
    data, fake = _run_sp_stage(monkeypatch, "systematic-debugging", current="review")

    assert data["success"] is True
    assert fake.calls[0][0] == "sp-systematic-debugging"


def test_sp_stage_allows_root_stage_to_start_new_chain(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "systematic-debugging",
        current="requesting-code-review",
    )

    assert data["success"] is True
    assert data["stage"] == "systematic-debugging"
    assert fake.calls[0][0] == "sp-systematic-debugging"


def test_sp_stage_rejects_invalid_non_root_transition(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "test-driven-development",
        current="requesting-code-review",
    )

    assert data["error"] == "chain_violation"
    assert data["current_stage"] == "requesting-code-review"
    assert data["valid_next"] == ["receiving-code-review"]
    assert fake.calls == []


def test_sp_stage_chain_validation_uses_stage_session_scope(monkeypatch):
    fake = _FakeSkillEngine()
    _set_current_stage("brainstorming")
    skill_tracking.set_current_stage(
        "using-git-worktrees",
        stage_session_id="stage:agent-b:existing",
    )
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)

    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            {
                "stage": "writing-plans",
                "task_description": "agent B continues its own chain",
                "stage_session_id": "stage:agent-b:existing",
            },
        )
    )
    data = json.loads(result[0].text)

    assert data["success"] is True
    assert data["stage_session_id"] == "stage:agent-b:existing"
    assert fake.calls[0][0] == "sp-writing-plans"
    assert fake.calls[0][1]["stage_session_id"] == "stage:agent-b:existing"
