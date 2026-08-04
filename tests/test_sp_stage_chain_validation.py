import asyncio
import json
from types import SimpleNamespace

import plastic_promise.mcp.server as mcp_server
import plastic_promise.mcp.tools.skill_tracking as skill_tracking
from plastic_promise.core.constants import normalize_stage_name
from plastic_promise.core.official_workflow import (
    OFFICIAL_SKILLS,
    UPSTREAM_SKILLS_REVISION,
)


class _FakeSkillEngine:
    def __init__(self):
        self.calls = []

    async def exec(self, skill_name, params, caller="claude"):
        self.calls.append((skill_name, params, caller))
        stage = skill_name.removeprefix("sp-")
        return SimpleNamespace(
            success=True,
            data={"skill_name": skill_name, "stage": stage},
            errors=[],
            audit_trail={},
        )


def _set_current_stage(stage):
    with skill_tracking._skill_state_lock:
        skill_tracking._current_stage = stage
        skill_tracking._current_skill = None
        skill_tracking._current_entity_id = None
        skill_tracking._stage_sessions.clear()


def _execution_receipt(stage, **overrides):
    receipt = {
        "skill": stage,
        "upstream_revision": UPSTREAM_SKILLS_REVISION,
        "content_sha256": OFFICIAL_SKILLS[stage].content_sha256,
        "status": "completed",
        "evidence": {"verification": "focused regression passed"},
    }
    receipt.update(overrides)
    return receipt


def _run_sp_stage(monkeypatch, stage, current=None, extra_args=None):
    fake = _FakeSkillEngine()
    _set_current_stage(current)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)
    payload = {"stage": stage, "task_description": "official workflow regression"}
    if extra_args:
        payload.update(extra_args)
    result = asyncio.run(mcp_server.call_tool("sp-stage", payload))
    return json.loads(result[0].text), fake


def test_sp_stage_rejects_removed_legacy_superpowers_stage(monkeypatch):
    data, fake = _run_sp_stage(monkeypatch, "brainstorming")

    assert data["error"] == "unknown_stage"
    assert data["requested_stage"] == "brainstorming"
    assert "brainstorming" not in data["available_stages"]
    assert "grill-with-docs" in data["available_stages"]
    assert fake.calls == []


def test_normalize_official_stage_aliases():
    assert normalize_stage_name("review") == "code-review"
    assert normalize_stage_name("sp-review") == "code-review"
    assert normalize_stage_name("mattpocock:tdd") == "tdd"
    assert normalize_stage_name("superpowers:receive-review") == "superpowers:receive-review"


def test_sp_stage_allows_model_bug_onramp(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "diagnosing-bugs",
        current="code-review",
        extra_args={
            "route": "bug-onramp",
            "invocation_source": "model",
            "execution_receipt": _execution_receipt("diagnosing-bugs"),
        },
    )

    assert data["success"] is True
    assert data["stage"] == "diagnosing-bugs"
    assert fake.calls[0][0] == "sp-diagnosing-bugs"
    assert fake.calls[0][1]["route"] == "bug-onramp"


def test_sp_stage_advances_inside_scoped_official_route(monkeypatch):
    fake = _FakeSkillEngine()
    _set_current_stage(None)
    scope = "stage:agent-b:existing::flow:bug-fix"
    skill_tracking.set_current_stage("diagnosing-bugs", stage_session_id=scope)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)

    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            {
                "stage": "tdd",
                "task_description": "repair the reproduced defect",
                "stage_session_id": "stage:agent-b:existing",
                "flow_line_id": "bug-fix",
                "route": "bug-onramp",
                "invocation_source": "model",
                "execution_receipt": _execution_receipt("tdd"),
            },
        )
    )
    data = json.loads(result[0].text)

    assert data["success"] is True
    assert data["stage_session_id"] == "stage:agent-b:existing"
    assert data["flow_scope_id"] == scope
    assert fake.calls[0][0] == "sp-tdd"
    assert fake.calls[0][1]["stage_session_id"] == scope


def test_sp_stage_rejects_skipped_stage_in_official_route(monkeypatch):
    fake = _FakeSkillEngine()
    _set_current_stage(None)
    scope = "stage:shared::flow:delivery"
    skill_tracking.set_current_stage("grill-with-docs", stage_session_id=scope)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: fake)

    result = asyncio.run(
        mcp_server.call_tool(
            "sp-stage",
            {
                "stage": "to-tickets",
                "task_description": "skip directly to tickets",
                "stage_session_id": "stage:shared",
                "flow_line_id": "delivery",
                "route": "idea-to-ship",
                "invocation_source": "user",
            },
        )
    )
    data = json.loads(result[0].text)

    assert data["error"] == "chain_violation"
    assert data["current_stage"] == "grill-with-docs"
    assert data["valid_next"] == ["to-spec"]
    assert fake.calls == []


def test_sp_stage_rejects_unknown_official_route(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "diagnosing-bugs",
        extra_args={"route": "normal-development"},
    )

    assert data["error"] == "unknown_route"
    assert data["requested_route"] == "normal-development"
    assert "bug-onramp" in data["available_routes"]
    assert fake.calls == []


def test_sp_stage_rejects_stage_outside_selected_route(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "tdd",
        extra_args={"route": "review", "invocation_source": "model"},
    )

    assert data["error"] == "stage_not_in_route"
    assert data["route_stages"] == ["code-review"]
    assert fake.calls == []


def test_sp_stage_wrapper_adds_official_guidance(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "diagnosing-bugs",
        extra_args={
            "route": "bug-onramp",
            "invocation_source": "model",
            "execution_receipt": _execution_receipt("diagnosing-bugs"),
        },
    )

    guidance = data["data"]["stage_guidance"]
    assert guidance["stage_summary"]["stage"] == "diagnosing-bugs"
    assert guidance["stage_summary"]["invocation_authority"] == "model"
    assert guidance["route_summary"]["route_id"] == "bug-onramp"
    assert guidance["route_summary"]["next_stage"] == "tdd"
    assert guidance["closure_reminder"]["mode"] == "full"
    assert fake.calls[0][0] == "sp-diagnosing-bugs"


def test_sp_stage_rejects_model_invocation_of_user_only_skill(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "implement",
        extra_args={"route": "idea-to-ship", "invocation_source": "model"},
    )

    assert data["error"] == "invocation_not_allowed"
    assert data["required_source"] == "user"
    assert fake.calls == []


def test_sp_stage_without_execution_receipt_only_returns_guidance(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "diagnosing-bugs",
        extra_args={"route": "bug-onramp", "invocation_source": "model"},
    )

    assert data["success"] is True
    assert data["execution_status"] == "awaiting_receipt"
    assert data["receipt_required"] is True
    assert skill_tracking.get_current_stage() is None
    assert fake.calls == []


def test_sp_stage_rejects_receipt_with_wrong_pinned_content_hash(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "diagnosing-bugs",
        extra_args={
            "route": "bug-onramp",
            "invocation_source": "model",
            "execution_receipt": _execution_receipt("diagnosing-bugs", content_sha256="0" * 64),
        },
    )

    assert data["error"] == "invalid_execution_receipt"
    assert data["reason"] == "content_sha256_mismatch"
    assert skill_tracking.get_current_stage() is None
    assert fake.calls == []


def test_sp_stage_user_declaration_cannot_bypass_route_predecessors(monkeypatch):
    data, fake = _run_sp_stage(
        monkeypatch,
        "implement",
        extra_args={"route": "idea-to-ship", "invocation_source": "user"},
    )

    assert data["error"] == "chain_violation"
    assert data["current_stage"] is None
    assert data["valid_next"] == ["grill-with-docs"]
    assert fake.calls == []
