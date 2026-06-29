"""Tests for auto_context_inject MCP tool."""
import asyncio
import json
import pytest
from unittest.mock import MagicMock, patch, ANY
from mcp.types import TextContent


class TestAutoContextInject:
    """Tests for handle_auto_context_inject."""

    def test_inject_creates_session_and_returns_context(self):
        """Full inject flow: start -> supply -> store -> complete."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-07-01T18:00:00",
            "type": "skill_session",
            "name": "auto_inject:claude_code",
            "is_new": True,
            "edges_created": 0,
        }

        with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.to_prompt.return_value = "# Context Pack"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.memory.handle_memory_store') as mock_store:
                mock_store.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"memory_id": "mem_inject_001", "stored": True})
                )]

                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(
                        type="text",
                        text=json.dumps({
                            "entity_id": "skill:auto_inject:claude_code:2026-07-01T18:00:00",
                            "skill_name": "auto_inject:claude_code",
                            "status": "active",
                            "domain": "reflecting",
                            "activated_principles": [{"id": 2, "name": "全过程可查可透明"}],
                            "related_memories": [],
                            "tags_applied": ["task:active", "skill:auto_inject:claude_code", "domain:reflecting"],
                            "chain_warning": None,
                        })
                    )]

                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(
                            type="text",
                            text=json.dumps({
                                "entity_id": "skill:auto_inject:claude_code:2026-07-01T18:00:00",
                                "status": "done",
                            })
                        )]

                        result = asyncio.run(handle_auto_context_inject(engine, {
                            "task_description": "修复 JWT 认证 bug",
                            "task_type": "code_generation",
                            "source": "claude_code",
                        }))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["skill_name"] == "auto_inject:claude_code"
        assert "entity_id" in data
        assert data["inject_memory_id"] == "mem_inject_001"
        assert "principles" in data

    def test_inject_graceful_degradation_when_start_fails(self):
        """Skill session start failure does not block inject."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start',
                   side_effect=Exception("MCP unavailable")):
            with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
                mock_loop = MagicMock()
                mock_pack = MagicMock()
                mock_pack.to_prompt.return_value = "# Context Pack"
                mock_pack.activated_principles = []
                mock_loop.pre_task_v2.return_value = mock_pack
                mock_loop_class.return_value = mock_loop

                with patch('plastic_promise.mcp.tools.memory.handle_memory_store') as mock_store:
                    mock_store.return_value = [TextContent(
                        type="text",
                        text=json.dumps({"memory_id": "mem_inject_fallback", "stored": True})
                    )]

                    result = asyncio.run(handle_auto_context_inject(engine, {
                        "task_description": "修复 bug",
                        "source": "manual",
                    }))

        data = json.loads(result[0].text)
        # Should still return context even though tracking failed
        assert "context_pack" in data or "partial" in str(data)

    def test_inject_stores_full_task_description_in_content(self):
        """Content preserves full task_description for self-feedback retrieval."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}
        task_desc = "修复 JWT 认证 bug — token 过期后 refresh 流程异常"

        stored_content = []

        async def capture_store(eng, args):
            stored_content.append(args.get("content", ""))
            return [TextContent(type="text", text=json.dumps({"memory_id": "mem_cap", "stored": True}))]

        with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.core = []
            mock_pack.related = []
            mock_pack.divergent = []
            mock_pack.activated_principles = []
            mock_pack.to_prompt.return_value = "# Context Pack"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.memory.handle_memory_store', side_effect=capture_store):
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "activated_principles": [],
                        "related_memories": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        asyncio.run(handle_auto_context_inject(engine, {
                            "task_description": task_desc,
                            "source": "manual",
                        }))

        assert len(stored_content) == 1
        assert task_desc in stored_content[0]
        assert "[AUTO INJECT]" in stored_content[0]

    def test_inject_principle_fallback_when_supply_fails(self):
        """When pre_task_v2 fails, principle_activate is called as safety net."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            # Simulate pre_task_v2 failure
            mock_loop.pre_task_v2.side_effect = Exception("Embedding service down")
            mock_loop_class.return_value = mock_loop

            fallback_principles = [{"id": 1, "name": "奥卡姆剃刀"}, {"id": 2, "name": "全过程可查可透明"}]
            with patch('plastic_promise.mcp.tools.principles.handle_principle_activate') as mock_pa:
                mock_pa.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"activated": fallback_principles})
                )]

                with patch('plastic_promise.mcp.tools.memory.handle_memory_store') as mock_store:
                    mock_store.return_value = [TextContent(
                        type="text",
                        text=json.dumps({"memory_id": "mem_fallback", "stored": True})
                    )]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                        mock_start.return_value = [TextContent(type="text", text=json.dumps({
                            "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                            "skill_name": "auto_inject:manual",
                            "status": "active",
                            "domain": "reflecting",
                            "activated_principles": [],
                            "chain_warning": None,
                        }))]
                        with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                            mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                            result = asyncio.run(handle_auto_context_inject(engine, {
                                "task_description": "修复 bug",
                                "source": "manual",
                            }))

        data = json.loads(result[0].text)
        # Should have fallback principles
        assert "奥卡姆剃刀" in str(data["principles"])
        assert "errors" in data or "partial" in str(data)

    def test_inject_memory_store_failure_does_not_block(self):
        """memory_store failure returns context_pack anyway."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.core = []
            mock_pack.related = []
            mock_pack.divergent = []
            mock_pack.activated_principles = []
            mock_pack.to_prompt.return_value = "# Context"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.memory.handle_memory_store',
                       side_effect=Exception("Memory store down")) as mock_store:
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "activated_principles": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        result = asyncio.run(handle_auto_context_inject(engine, {
                            "task_description": "修复 bug",
                            "source": "manual",
                        }))

        data = json.loads(result[0].text)
        # Should still have context_pack even though store failed
        assert data.get("partial") == True
        assert data["inject_memory_id"] is None

    def test_self_feedback_loop_second_inject_hits_first(self):
        """Second inject with similar task_description retrieves first inject record."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        # Simulate existing inject memory in the pool
        first_inject_memory = {
            "id": "mem_first",
            "content": "[AUTO INJECT] 修复 JWT 认证 bug\ncore_items: 3\nactivated_principles: 奥卡姆剃刀, 全过程可查可透明",
            "memory_type": "experience",
            "tags": ["auto_inject", "source:manual", "task:done"],
            "worth_score": 0.72,
        }
        engine._memories = {"mem_first": first_inject_memory}

        # The supply() should find the first inject record
        pack_with_hit = MagicMock()
        core_item = MagicMock()
        core_item.id = "mem_first"
        core_item.content = first_inject_memory["content"]
        core_item.relevance = 0.85
        pack_with_hit.core = [core_item]
        pack_with_hit.related = []
        pack_with_hit.divergent = []
        pack_with_hit.activated_principles = []
        pack_with_hit.to_prompt.return_value = "# Context with hit"

        with patch('plastic_promise.loop.soul_loop.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.return_value = pack_with_hit
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.memory.handle_memory_store') as mock_store:
                mock_store.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"memory_id": "mem_second", "stored": True})
                )]
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:02:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "related_memories": ["mem_first"],  # Self-feedback hit!
                        "activated_principles": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        result = asyncio.run(handle_auto_context_inject(engine, {
                            "task_description": "修复 OAuth 认证 bug",  # Similar task
                            "source": "manual",
                        }))

        data = json.loads(result[0].text)
        # Second inject's context_pack should have the first inject record in core
        assert data["context_pack"]["core"][0]["id"] == "mem_first"
        assert "JWT" in data["context_pack"]["core"][0]["content"]
