import json

import pytest
from unittest.mock import MagicMock
from mcp.types import TextContent

from plastic_promise.skills.engine import SkillEngine, SkillDef, SkillResult
from plastic_promise.skills.session_lifecycle import skill_session_init


def _make_mock_tool(name: str):
    """Create a minimal mock tool with a .name attribute."""
    tool = MagicMock()
    tool.name = name
    return tool


class TestSessionInit:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "context_supply", "memory_store",
            "domain", "system", "defense", "memory_gc",
            "skill_session_start", "skill_session_complete",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    @pytest.mark.asyncio
    async def test_session_init_success(self, mock_engine):
        """session-init must call all 7 atoms in order and return context pack."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def record_call(name, data):
            async def handler(engine, args):
                call_order.append(name)
                return [TextContent(type="text", text=json.dumps(data))]
            return handler

        se._atoms["principle_activate"] = await record_call("principle_activate", {
            "task_type": "general", "activated": [{"id": 1, "name": "奥卡姆剃刀"}], "count": 1
        })
        se._atoms["context_supply"] = await record_call("context_supply", {
            "core": [{"id": "m1", "content": "test"}], "related": [], "divergent": []
        })
        se._atoms["memory_store"] = await record_call("memory_store", {
            "stored": True, "memory_id": "mem_001"
        })
        se._atoms["domain"] = await record_call("domain", {
            "domains": {"building": {"score": 0.8}}
        })
        se._atoms["system"] = await record_call("system", {
            "memory": {"total": 42, "healthy": 40, "decaying": 2}
        })
        se._atoms["defense"] = await record_call("defense", {
            "trust": 0.75, "tier": "standard"
        })
        se._atoms["memory_gc"] = await record_call("memory_gc", {
            "dry_run": True, "candidates_count": 3
        })
        se._atoms["skill_session_start"] = await record_call("skill_session_start", {
            "entity_id": "skill:session-init:2026-01-01T00:00:00"
        })
        se._atoms["skill_session_complete"] = await record_call("skill_session_complete", {
            "status": "done"
        })

        se.register(skill_session_init)
        result = await se.exec("session-init", params={
            "task_description": "test task",
            "task_type": "general",
        }, caller="claude")

        assert result.success is True
        assert result.skill_name == "session-init"
        # Verify all 7 atoms called in order (index 0 is skill_session_start, called internally by engine)
        assert call_order[1:8] == [
            "principle_activate", "context_supply", "memory_store",
            "domain", "system", "defense", "memory_gc"
        ]
        # Verify handler assembled the data
        assert "context" in result.data
        assert "domain_health" in result.data
        assert "system_stats" in result.data
        assert "trust" in result.data

    @pytest.mark.asyncio
    async def test_session_init_degraded_domain_skip(self, mock_engine):
        """When domain fails with degrade='skip', session-init must continue and note the skip."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def ok_atom(name, data):
            async def handler(engine, args):
                call_order.append(name)
                return [TextContent(type="text", text=json.dumps(data))]
            return handler

        async def failing_atom(name):
            async def handler(engine, args):
                call_order.append(name)
                raise RuntimeError("DomainManager not available")
            return handler

        se._atoms["principle_activate"] = await ok_atom("principle_activate", {"activated": []})
        se._atoms["context_supply"] = await ok_atom("context_supply", {"core": []})
        se._atoms["memory_store"] = await ok_atom("memory_store", {"stored": True})
        se._atoms["domain"] = await failing_atom("domain")  # This will fail
        se._atoms["system"] = await ok_atom("system", {"memory": {"total": 0}})
        se._atoms["defense"] = await ok_atom("defense", {"trust": 0.5})
        se._atoms["memory_gc"] = await ok_atom("memory_gc", {"candidates_count": 0})
        se._atoms["skill_session_start"] = await ok_atom("skill_session_start", {"entity_id": "skill:test:..."})
        se._atoms["skill_session_complete"] = await ok_atom("skill_session_complete", {"status": "done"})

        se.register(skill_session_init)
        result = await se.exec("session-init", params={
            "task_description": "test",
        }, caller="claude")

        assert result.success is True
        assert "system" in call_order  # continued after domain failure
        assert any("domain" in log and "skip" in log for log in result.degrade_log)
