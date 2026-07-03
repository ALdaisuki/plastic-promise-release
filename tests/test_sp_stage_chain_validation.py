"""Tests for sp-stage chain validation behavior."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _FakeSkillEngine:
    async def exec(self, skill_name, params, caller=None):
        return SimpleNamespace(
            success=True,
            data={
                "skill_name": skill_name,
                "params": params,
                "caller": caller,
            },
            errors=[],
        )


@pytest.mark.asyncio
async def test_sp_stage_allows_root_stage_to_start_new_chain():
    """A review flow must not block another Agent from starting debugging."""
    from plastic_promise.mcp.server import call_tool

    with patch(
        "plastic_promise.mcp.tools.skill_tracking.get_current_stage",
        return_value="requesting-code-review",
    ):
        with patch("plastic_promise.mcp.server.get_skill_engine", return_value=_FakeSkillEngine()):
            result = await call_tool(
                "sp-stage",
                {
                    "stage": "systematic-debugging",
                    "task_description": "debug a separate Agent failure",
                },
            )

    data = json.loads(result[0].text)
    assert data["success"] is True
    assert data["stage"] == "systematic-debugging"
    assert data["data"]["skill_name"] == "sp-systematic-debugging"


@pytest.mark.asyncio
async def test_sp_stage_still_rejects_invalid_non_root_transition():
    """Only root stages may bypass the current chain; non-root stages stay guarded."""
    from plastic_promise.mcp.server import call_tool

    with patch(
        "plastic_promise.mcp.tools.skill_tracking.get_current_stage",
        return_value="requesting-code-review",
    ):
        with patch("plastic_promise.mcp.server.get_skill_engine", return_value=_FakeSkillEngine()):
            result = await call_tool(
                "sp-stage",
                {
                    "stage": "test-driven-development",
                    "task_description": "attempt an invalid non-root transition",
                },
            )

    data = json.loads(result[0].text)
    assert data["error"] == "chain_violation"
    assert data["current_stage"] == "requesting-code-review"
    assert data["valid_next"] == ["receiving-code-review"]
