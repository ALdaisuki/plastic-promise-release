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
        stage = skill_name.removeprefix("sp-")
        return SimpleNamespace(
            success=True, data={"skill_name": skill_name, "stage": stage}, errors=[]
        )


def _set_current_stage(stage):
    with skill_tracking._skill_state_lock:
        skill_tracking._current_stage = stage
        skill_tracking._current_skill = None
        skill_tracking._current_entity_id = None


def _run_sp_stage(monkeypatch, stage, current="review", extra_args=None):
    fake = _FakeSkillEngine()
    _set_current_stage(current)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)
    payload = {"stage": stage, "task_description": "chain validation regression"}
    if extra_args:
        payload.update(extra_args)
    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            payload,
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


def test_sp_stage_wrapper_adds_guidance_to_skill_result(monkeypatch):
    data, fake = _run_sp_stage(monkeypatch, "audit", current="review")

    guidance = data["data"]["stage_guidance"]
    assert guidance["stage_summary"]["stage"] == "audit"
    assert guidance["route_summary"]["route_id"] == "audit-review"
    assert guidance["closure_reminder"]["mode"] == "full"
    assert guidance["required_artifacts"][0]["path"] == "audit_run(action='full')"
    assert "stage_session_id" in guidance["route_summary"]["session_isolation"]
    assert "official_skill" not in json.dumps(guidance)
    assert "skill_authority" not in json.dumps(guidance)
    assert fake.calls[0][0] == "sp-audit"


def test_sp_stage_new_flow_line_rejects_non_root_entry(monkeypatch):
    fake = _FakeSkillEngine()
    _set_current_stage(None)
    skill_tracking.set_current_stage(
        "requesting-code-review",
        stage_session_id="stage:shared",
    )
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)

    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            {
                "stage": "writing-plans",
                "task_description": "dev flow should not inherit review flow",
                "stage_session_id": "stage:shared",
                "flow_line_id": "dev",
                "route": "normal-development",
            },
        )
    )
    data = json.loads(result[0].text)

    assert data["error"] == "chain_violation"
    assert data["current_stage"] is None
    assert "brainstorming" in data["valid_next"]
    assert "requesting-code-review" in data["valid_next"]
    assert "systematic-debugging" in data["valid_next"]
    assert fake.calls == []


def test_sp_stage_new_scopes_reject_non_root_entrypoints(monkeypatch):
    for stage in (
        "writing-plans",
        "test-driven-development",
        "finishing-a-development-branch",
    ):
        data, fake = _run_sp_stage(
            monkeypatch,
            stage,
            current=None,
            extra_args={
                "stage_session_id": f"stage:fresh:{stage}",
                "route": "normal-development",
            },
        )

        assert data["error"] == "chain_violation"
        assert data["current_stage"] is None
        assert "brainstorming" in data["valid_next"]
        assert fake.calls == []


def test_sp_stage_new_scope_allows_root_entrypoint(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "brainstorming",
        current=None,
        extra_args={
            "stage_session_id": "stage:fresh:design",
            "route": "normal-development",
        },
    )

    assert data["success"] is True
    assert data["stage"] == "brainstorming"
    assert fake.calls[0][0] == "sp-brainstorming"


def test_sp_stage_allows_meta_root_stages_from_stale_chain(monkeypatch):
    expectations = {
        "using-superpowers": ("sp-using-superpowers", "superpowers-bootstrap"),
        "writing-skills": ("sp-writing-skills", "skill-authoring"),
    }

    for stage, (skill_name, route_id) in expectations.items():
        data, fake = _run_sp_stage(
            monkeypatch,
            stage,
            current="requesting-code-review",
        )

        assert data["success"] is True
        assert fake.calls[0][0] == skill_name
        assert data["data"]["stage_guidance"]["route_summary"]["route_id"] == route_id
