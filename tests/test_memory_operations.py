import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from mcp.types import TextContent

from plastic_promise.skills.engine import SkillEngine, SkillDef, SkillResult
from plastic_promise.skills.memory_operations import skill_smart_remember


class TestSmartRemember:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "memory_recall", "memory_store",
            "skill_session_start", "skill_session_complete",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _mock_response(self, data: dict) -> list:
        return [TextContent(type="text", text=json.dumps(data))]

    @pytest.mark.asyncio
    async def test_smart_remember_new_memory(self, mock_engine):
        """When no duplicate is found, a new memory must be stored."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_principle_activate(engine, args):
            call_order.append("principle_activate")
            return self._mock_response({"activated": [{"id": 1, "name": "奥卡姆剃刀"}]})

        async def mock_memory_recall(engine, args):
            call_order.append("memory_recall")
            # No duplicates found
            return self._mock_response({"core": [], "related": [], "divergent": []})

        async def mock_memory_store(engine, args):
            call_order.append("memory_store")
            return self._mock_response({
                "stored": True, "memory_id": "mem_new_001",
                "content_preview": args["content"][:50],
            })

        async def mock_session(engine, args):
            return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["memory_store"] = mock_memory_store
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session

        se.register(skill_smart_remember)
        result = await se.exec("smart-remember", params={
            "content": "The user prefers tabs over spaces",
            "memory_type": "experience",
            "source": "user",
        }, caller="claude")

        assert result.success is True
        assert result.data.get("action") == "stored"
        assert result.data.get("memory_id") == "mem_new_001"
        assert call_order == ["principle_activate", "memory_recall", "memory_store"]

    @pytest.mark.asyncio
    async def test_smart_remember_duplicate_found(self, mock_engine):
        """When a duplicate is found (cos >= 0.85), update existing instead of creating new.
        The update is done by calling handle_memory_update directly (not as an engine atom)."""
        with patch(
            "plastic_promise.skills.memory_operations.handle_memory_update",
            new_callable=AsyncMock,
        ) as mock_update:
            mock_update.return_value = [TextContent(
                type="text",
                text=json.dumps({"updated": True, "memory_id": "mem_existing_042"}),
            )]

            se = SkillEngine(mock_engine)
            call_order = []

            async def mock_principle_activate(engine, args):
                call_order.append("principle_activate")
                return self._mock_response({"activated": []})

            async def mock_memory_recall(engine, args):
                call_order.append("memory_recall")
                # Duplicate found — one existing memory with high relevance
                return self._mock_response({
                    "core": [
                        {"id": "mem_existing_042", "content": "User prefers tabs over spaces", "relevance": 0.92}
                    ],
                    "related": [], "divergent": [],
                })

            async def mock_session(engine, args):
                return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

            se._atoms["principle_activate"] = mock_principle_activate
            se._atoms["memory_recall"] = mock_memory_recall
            se._atoms["skill_session_start"] = mock_session
            se._atoms["skill_session_complete"] = mock_session

            se.register(skill_smart_remember)
            result = await se.exec("smart-remember", params={
                "content": "User prefers tabs over spaces",
                "memory_type": "experience",
                "source": "user",
            }, caller="claude")

            assert result.success is True
            assert result.data.get("action") == "updated"
            assert result.data.get("memory_id") == "mem_existing_042"
            assert call_order == ["principle_activate", "memory_recall"]
            assert "memory_store" not in call_order  # did not create duplicate
            mock_update.assert_awaited_once_with(
                mock_engine, {"memory_id": "mem_existing_042", "content": "User prefers tabs over spaces"},
            )


def _make_mock_tool(name: str):
    """Create a minimal mock tool with a .name attribute."""
    tool = MagicMock()
    tool.name = name
    return tool
