"""Tests for skill_tracking MCP tools -- Skill Tracking Task 3."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import TextContent


class TestSkillSessionStart:
    """Tests for handle_skill_session_start."""

    def test_start_creates_entity_with_correct_id_format(self):
        """Entity ID follows skill:<name>:<ISO timestamp> pattern."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:brainstorming:2026-06-30T14:23:01.123456",
            "type": "skill_session",
            "name": "brainstorming",
            "is_new": True,
            "edges_created": 0,
        }

        with patch(
            "plastic_promise.mcp.tools.skill_tracking._activate_skill_principles"
        ) as mock_principles:
            mock_principles.return_value = [{"id": 2, "name": "全过程可查可透明"}]
            with patch(
                "plastic_promise.mcp.tools.skill_tracking._recall_skill_memories"
            ) as mock_recall:
                mock_recall.return_value = ["mem_abc"]
                with patch(
                    "plastic_promise.mcp.tools.skill_tracking._store_skill_start"
                ) as mock_store:
                    mock_store.return_value = "mem_skill_xyz"

                    result = asyncio.run(
                        handle_skill_session_start(
                            engine,
                            {
                                "skill_name": "brainstorming",
                                "task_description": "Design the skill tracking module",
                                "parent_entity_id": None,
                            },
                        )
                    )

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["skill_name"] == "brainstorming"
        assert data["status"] == "active"
        assert data["domain"] == "designing"
        assert data["chain_warning"] is None
        assert "skill:brainstorming:" in data["entity_id"]
        assert data["memory_id"] == "mem_skill_xyz"

    def test_start_returns_chain_warning_for_illegal_parent(self):
        """Parent validation returns warning but does not block creation."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:writing-plans:2026-06-30T15:00:00",
            "type": "skill_session",
            "name": "writing-plans",
            "is_new": True,
            "edges_created": 1,
        }

        with (
            patch(
                "plastic_promise.mcp.tools.skill_tracking._activate_skill_principles",
                return_value=[],
            ),
            patch(
                "plastic_promise.mcp.tools.skill_tracking._recall_skill_memories",
                return_value=[],
            ),
            patch(
                "plastic_promise.mcp.tools.skill_tracking._store_skill_start",
                return_value="mem_xyz",
            ),
        ):
            result = asyncio.run(
                handle_skill_session_start(
                    engine,
                    {
                        "skill_name": "writing-plans",
                        "task_description": "Plan the module",
                        "parent_entity_id": ("skill:test-driven-development:2026-06-30T14:00:00"),
                    },
                )
            )

        data = json.loads(result[0].text)
        # writing-plans expects predecessor "brainstorming", not "test-driven-development"
        assert data["chain_warning"] is not None
        assert "not a legal predecessor" in data["chain_warning"]
        # Despite warning, the session IS created
        assert data["status"] == "active"
        assert data["skill_name"] == "writing-plans"
        assert "skill:writing-plans:" in data["entity_id"]

    def test_start_without_parent_no_warning(self):
        """Null parent is always valid -- no chain_warning."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:brainstorming:2026-06-30T14:23:01",
            "type": "skill_session",
            "name": "brainstorming",
            "is_new": True,
            "edges_created": 0,
        }

        with (
            patch(
                "plastic_promise.mcp.tools.skill_tracking._activate_skill_principles",
                return_value=[],
            ),
            patch(
                "plastic_promise.mcp.tools.skill_tracking._recall_skill_memories",
                return_value=[],
            ),
            patch(
                "plastic_promise.mcp.tools.skill_tracking._store_skill_start",
                return_value="mem_xyz",
            ),
        ):
            result = asyncio.run(
                handle_skill_session_start(
                    engine,
                    {
                        "skill_name": "brainstorming",
                        "task_description": "Design something",
                        "parent_entity_id": None,
                    },
                )
            )

        data = json.loads(result[0].text)
        assert data["chain_warning"] is None
        assert data["status"] == "active"

    def test_start_rejects_non_string_memory_create_result(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.create_ordinary_if_absent.return_value = MagicMock()

        with pytest.raises(TypeError, match="skill_start_memory_id_invalid"):
            asyncio.run(
                handle_skill_session_start(
                    engine,
                    {
                        "skill_name": "brainstorming",
                        "task_description": "Reject malformed creation results",
                    },
                )
            )

    def test_start_propagates_memory_create_failure(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.create_ordinary_if_absent.side_effect = RuntimeError("injected create failure")

        with pytest.raises(RuntimeError, match="injected create failure"):
            asyncio.run(
                handle_skill_session_start(
                    engine,
                    {
                        "skill_name": "brainstorming",
                        "task_description": "Surface persistence failures",
                    },
                )
            )

    def test_start_unknown_skill_name_errors(self):
        """Unknown skill name should return an error response."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()

        result = asyncio.run(
            handle_skill_session_start(
                engine,
                {
                    "skill_name": "nonexistent-skill",
                    "task_description": "Test",
                },
            )
        )

        data = json.loads(result[0].text)
        assert "error" in data
        assert "Unknown skill_name" in data["error"]
        assert data["tool"] == "skill_session_start"


class TestSkillSessionComplete:
    """Tests for handle_skill_session_complete."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_engine_with_memory(
        entity_id="skill:brainstorming:2026-06-30T14:00:00.000000",
        content="[SKILL START] brainstorming: Design something",
        tags=None,
        created_at=None,
    ):
        """Build a mock engine whose _memories dict contains one skill-start entry."""
        if created_at is None:
            # Use an ISO timestamp ~1 hour ago so duration_ms > 0
            import datetime as _dt

            created_at = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)).isoformat()
        if tags is None:
            tags = ["task:active", "skill:brainstorming", "domain:designing"]

        memory_id = "mem_test_001"
        mem = {
            "id": memory_id,
            "content": content,
            "memory_type": "experience",
            "source": "superpowers",
            "project_id": "project:test",
            "entity_ids": [entity_id],
            "tags": tags,
            "metadata_json": {},
            "created_at": created_at,
            "domain": "designing",
            "worth_success": 0,
            "worth_failure": 0,
        }
        engine = MagicMock()
        engine._memories = {memory_id: mem}
        engine.source_mutations = []
        # Wire up public API methods
        engine.iter_memories = lambda: iter(engine._memories.values())

        def _mutate_ordinary_source(mid, **mutation):
            engine.source_mutations.append({"memory_id": mid, **mutation})
            engine._memories[mid]["content"] = mutation["content"]
            return {"stale_synthesis_ids": ("dependent-synthesis",)}

        engine.mutate_ordinary_source = _mutate_ordinary_source

        def _update_memory_fields(mid, **fields):
            if mid in engine._memories:
                engine._memories[mid].update(fields)
                return True
            return False

        engine.update_memory_fields = _update_memory_fields

        def _patch_ordinary_memory(mid, *, replacements, **_preconditions):
            return _update_memory_fields(mid, **replacements)

        engine.patch_ordinary_memory = _patch_ordinary_memory
        # Wire up get_memory / store_memory so feedback_apply works
        engine.get_memory.return_value = MagicMock(
            id=memory_id,
            content=content,
            worth_success=0,
            worth_failure=0,
            total_observations=0,
        )
        engine.get_memory.return_value.worth_score.return_value = 0.5
        return engine, memory_id, mem

    # ------------------------------------------------------------------
    # Test 1: Normal completion → done
    # ------------------------------------------------------------------

    def test_complete_marks_artifact_store_as_trusted(self, monkeypatch):
        from plastic_promise.core.memory_proposals import has_trusted_internal_origin
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        monkeypatch.setenv("PP_MEMORY_PROPOSALS", "on")
        entity_id = "skill:brainstorming:2026-06-30T14:00:00.000000"
        engine, memory_id, _mem = self._make_engine_with_memory(
            entity_id=entity_id,
            content="[SKILL START] brainstorming: Design something\n[SKILL COMPLETE] duration_ms=1",
        )
        observed = []

        async def capture_store(_engine, args):
            trusted = has_trusted_internal_origin(args)
            observed.append(trusted)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "stored": trusted,
                            "status": "canonical" if trusted else "pending",
                            "memory_id": "mem_skill_artifact" if trusted else None,
                        }
                    ),
                )
            ]

        with (
            patch(
                "plastic_promise.mcp.tools.reflection.handle_feedback_apply",
                return_value=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "updated": True,
                                "item_id": memory_id,
                                "new_worth_score": 0.52,
                            }
                        ),
                    )
                ],
            ),
            patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
                side_effect=capture_store,
            ),
        ):
            result = asyncio.run(
                handle_skill_session_complete(
                    engine,
                    {
                        "entity_id": entity_id,
                        "artifacts": ["docs/design.md"],
                    },
                )
            )

        payload = json.loads(result[0].text)
        assert observed == [True]
        assert payload["artifact_memory_ids"] == ["mem_skill_artifact"]
        assert has_trusted_internal_origin({}) is False

    def test_complete_transitions_status_to_done(self):
        """Normal outcome: status=done, next_skills populated, worth_update ~0.02."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:brainstorming:2026-06-30T14:00:00.000000"
        engine, memory_id, mem = self._make_engine_with_memory(
            entity_id=entity_id,
            content="[SKILL START] brainstorming: Design something\n[SKILL COMPLETE] duration_ms=1",
        )

        with patch(
            "plastic_promise.mcp.tools.reflection.handle_feedback_apply",
        ) as mock_fb:
            # feedback_apply returns MCP TextContent list
            mock_fb.return_value = [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "updated": True,
                            "item_id": memory_id,
                            "new_worth_score": 0.52,
                        }
                    ),
                )
            ]

            with patch(
                "plastic_promise.mcp.tools.memory.handle_memory_store",
            ) as mock_store:
                mock_store.return_value = [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "memory_id": "mem_artifact_xyz",
                            }
                        ),
                    )
                ]

                result = asyncio.run(
                    handle_skill_session_complete(
                        engine,
                        {
                            "entity_id": entity_id,
                            # no outcome → normal completion
                            "artifacts": ["docs/design.md"],
                        },
                    )
                )

        data = json.loads(result[0].text)
        assert data["status"] == "done"
        assert data["skill_name"] == "brainstorming"
        assert data["entity_id"] == entity_id
        assert data["memory_id"] == memory_id

        # next_skills from SKILL_CHAIN_MAP (brainstorming → exemplar-research)
        assert "exemplar-research" in data["next_skills"]

        # worth_update should reflect the feedback_apply delta
        assert data["worth_update"] is not None
        assert data["worth_update"] == 0.52

        # duration should be calculated
        assert data["duration_ms"] is not None
        assert data["duration_ms"] > 0

        # artifact storage called
        assert len(data["artifact_memory_ids"]) == 1
        assert data["artifact_memory_ids"][0] == "mem_artifact_xyz"

        # Engine tags and content updated
        engine_mem = engine._memories[memory_id]
        assert "task:done" in engine_mem["tags"]
        assert "task:active" not in engine_mem["tags"]
        assert "[SKILL COMPLETE]" in engine_mem["content"]

    def test_complete_skips_feedback_when_metadata_patch_fails(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

        entity_id = "skill:brainstorming:2026-06-30T14:00:00.000000"
        engine, memory_id, _mem = self._make_engine_with_memory(
            entity_id=entity_id,
            content="[SKILL START] brainstorming: Design something\n[SKILL COMPLETE] duration_ms=1",
        )
        engine.patch_ordinary_memory = MagicMock(return_value=False)

        with patch("plastic_promise.mcp.tools.reflection.handle_feedback_apply") as feedback:
            result = asyncio.run(handle_skill_session_complete(engine, {"entity_id": entity_id}))

        data = json.loads(result[0].text)
        assert data == {
            "updated": False,
            "entity_id": entity_id,
            "skill_name": "brainstorming",
            "memory_id": memory_id,
            "reason": "ordinary_metadata_update_failed",
            "tool": "skill_session_complete",
        }
        feedback.assert_not_called()

    def test_complete_reports_partial_when_content_commits_before_metadata_failure(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

        entity_id = "skill:brainstorming:2026-06-30T14:00:00.000000"
        engine, memory_id, _mem = self._make_engine_with_memory(entity_id=entity_id)
        engine.mutate_ordinary_source = MagicMock(
            return_value={
                "operation": "corrected",
                "stale_synthesis_ids": ("dependent-synthesis",),
                "ordinary_index_job_id": "ordinary-job",
                "synthesis_index_job_ids": ("synthesis-job",),
            }
        )
        engine.patch_ordinary_memory = MagicMock(return_value=False)

        with patch("plastic_promise.mcp.tools.reflection.handle_feedback_apply") as feedback:
            result = asyncio.run(handle_skill_session_complete(engine, {"entity_id": entity_id}))

        data = json.loads(result[0].text)
        assert data == {
            "updated": False,
            "entity_id": entity_id,
            "skill_name": "brainstorming",
            "memory_id": memory_id,
            "reason": "ordinary_metadata_update_failed",
            "tool": "skill_session_complete",
            "committed": True,
            "partial": True,
            "operation": "corrected",
            "stale_dependents": ["dependent-synthesis"],
            "ordinary_index_job_id": "ordinary-job",
            "synthesis_index_job_ids": ["synthesis-job"],
        }
        feedback.assert_not_called()

    # ------------------------------------------------------------------
    # Test 2: still_in_progress resets timer
    # ------------------------------------------------------------------

    def test_still_in_progress_routes_content_through_coordinator(self):
        """A renewal marker carries generated internal mutation evidence."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:systematic-debugging:2026-06-30T15:00:00.111111"
        engine, memory_id, mem = self._make_engine_with_memory(
            entity_id=entity_id,
            content="[SKILL START] systematic-debugging: Debug issue #42",
            tags=["task:active", "skill:systematic-debugging", "domain:fixing"],
        )
        result = asyncio.run(
            handle_skill_session_complete(
                engine,
                {
                    "entity_id": entity_id,
                    "outcome": "still_in_progress",
                },
            )
        )

        data = json.loads(result[0].text)
        assert data["status"] == "still_active"
        assert data["memory_id"] == memory_id
        assert data["renewal_count"] == 1
        assert "[still_in_progress]" in engine._memories[memory_id]["content"]
        assert len(engine.source_mutations) == 1
        mutation = engine.source_mutations[0]
        assert mutation["operation"] == "replace_content"
        assert mutation["reason"] == "skill_session:still_in_progress"
        assert mutation["actor"] == "skill_tracking"
        assert mutation["call_id"].startswith("internal:skill_tracking:still_in_progress:")
        assert mutation["expected_content_hash"]
        assert mutation["expected_project_id"] == "project:test"
        assert mutation["expected_source_snapshot"] == {"tags": mem["tags"]}
        assert mutation["require_source_available"] is True

    # ------------------------------------------------------------------
    # Test 3: still_in_progress exceeds max renewals → overdue
    # ------------------------------------------------------------------

    def test_still_in_progress_adds_overdue_after_coordinated_content(self):
        """The metadata patch follows a successful coordinated marker write."""
        from plastic_promise.mcp.tools.skill_tracking import (
            MAX_STILL_IN_PROGRESS_RENEWALS,
            handle_skill_session_complete,
        )

        entity_id = "skill:writing-plans:2026-06-30T12:00:00.222222"
        # Pre-populate content with 3 [still_in_progress] markers
        content = "[SKILL START] writing-plans: Plan the module\n" + "\n".join(
            ["[still_in_progress]"] * MAX_STILL_IN_PROGRESS_RENEWALS
        )
        engine, memory_id, mem = self._make_engine_with_memory(
            entity_id=entity_id,
            content=content,
            tags=["task:active", "skill:writing-plans", "domain:designing"],
        )
        result = asyncio.run(
            handle_skill_session_complete(
                engine,
                {
                    "entity_id": entity_id,
                    "outcome": "still_in_progress",
                },
            )
        )

        data = json.loads(result[0].text)
        assert data["status"] == "still_active"
        assert data["overdue"] is True
        assert "task:overdue" in engine._memories[memory_id]["tags"]
        assert len(engine.source_mutations) == 1

    # ------------------------------------------------------------------
    # Test 4: Abandoned outcome
    # ------------------------------------------------------------------

    def test_abandoned_outcome_routes_content_before_tag_transition(self):
        """Abandonment appends content through the coordinator before metadata."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:brainstorming:2026-06-30T13:00:00.333333"
        engine, memory_id, mem = self._make_engine_with_memory(
            entity_id=entity_id,
        )
        result = asyncio.run(
            handle_skill_session_complete(
                engine,
                {
                    "entity_id": entity_id,
                    "outcome": "abandoned: requirement changed",
                },
            )
        )

        data = json.loads(result[0].text)
        assert data["status"] == "abandoned"
        assert data["memory_id"] == memory_id
        assert data["reason"] == "requirement changed"
        assert "task:abandoned" in engine._memories[memory_id]["tags"]
        assert "task:active" not in engine._memories[memory_id]["tags"]
        assert "[SKILL ABANDONED] requirement changed" in engine._memories[memory_id]["content"]
        mutation = engine.source_mutations[0]
        assert mutation["operation"] == "replace_content"
        assert mutation["reason"] == "skill_session:abandoned"
        assert mutation["actor"] == "skill_tracking"
        assert mutation["call_id"].startswith("internal:skill_tracking:abandoned:")
        assert mutation["expected_content_hash"]
        assert mutation["expected_project_id"] == "project:test"
        assert mutation["require_source_available"] is True

    def test_normal_completion_content_cannot_bypass_source_invalidation(self):
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:brainstorming:2026-06-30T13:00:00.333333"
        engine, memory_id, _mem = self._make_engine_with_memory(entity_id=entity_id)

        with patch(
            "plastic_promise.mcp.tools.reflection.handle_feedback_apply",
            return_value=[
                TextContent(
                    type="text",
                    text=json.dumps({"updated": True, "new_worth_score": 0.52}),
                )
            ],
        ):
            result = asyncio.run(handle_skill_session_complete(engine, {"entity_id": entity_id}))

        data = json.loads(result[0].text)
        assert data["status"] == "done"
        assert "[SKILL COMPLETE]" in engine._memories[memory_id]["content"]
        mutation = engine.source_mutations[0]
        assert mutation["operation"] == "replace_content"
        assert mutation["reason"] == "skill_session:complete"
        assert mutation["actor"] == "skill_tracking"
        assert mutation["call_id"].startswith("internal:skill_tracking:complete:")
        assert mutation["expected_content_hash"]
        assert mutation["expected_project_id"] == "project:test"
        assert mutation["require_source_available"] is True

    def test_skill_content_append_rejects_concurrent_source_change(self):
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:brainstorming:2026-06-30T13:00:00.333333"
        engine, memory_id, mem = self._make_engine_with_memory(entity_id=entity_id)
        observed_content = mem["content"]
        before_tags = list(mem["tags"])

        def reject_stale_append(mid, **mutation):
            assert mid == memory_id
            assert mutation["expected_content_hash"]
            assert mutation["expected_project_id"] == "project:test"
            assert mutation["expected_source_snapshot"] == {"tags": before_tags}
            assert mutation["require_source_available"] is True
            engine._memories[mid]["content"] = observed_content + "\nconcurrent update"
            raise RuntimeError("ordinary_source_precondition_mismatch")

        engine.mutate_ordinary_source = reject_stale_append
        engine.patch_ordinary_memory = MagicMock(return_value=True)

        result = asyncio.run(
            handle_skill_session_complete(
                engine,
                {"entity_id": entity_id, "outcome": "still_in_progress"},
            )
        )

        payload = json.loads(result[0].text)
        assert payload["updated"] is False
        assert payload["reason"] == "ordinary_source_precondition_mismatch"
        assert engine._memories[memory_id]["content"].endswith("concurrent update")
        assert engine._memories[memory_id]["tags"] == before_tags
        engine.patch_ordinary_memory.assert_not_called()

    def test_skill_content_failure_does_not_apply_metadata(self):
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_complete,
        )

        entity_id = "skill:brainstorming:2026-06-30T13:00:00.333333"
        engine, memory_id, mem = self._make_engine_with_memory(entity_id=entity_id)
        before_tags = list(mem["tags"])
        engine.mutate_ordinary_source = MagicMock(
            side_effect=RuntimeError("ordinary_source_cas_mismatch")
        )
        engine.update_memory_fields = MagicMock(return_value=True)

        result = asyncio.run(
            handle_skill_session_complete(
                engine,
                {"entity_id": entity_id, "outcome": "still_in_progress"},
            )
        )

        payload = json.loads(result[0].text)
        assert payload["updated"] is False
        assert payload["reason"] == "ordinary_source_cas_mismatch"
        assert engine._memories[memory_id]["tags"] == before_tags
        engine.update_memory_fields.assert_not_called()


class TestSkillSessionTrace:
    """Tests for handle_skill_session_trace."""

    def test_trace_returns_sessions_with_chain_validation(self):
        """Mock 2 sessions (brainstorming done, finishing-a-development-branch done),
        verify chain_valid=True, 2 sessions returned."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()

        entity_b = "skill:brainstorming:2026-06-30T14:00:00.000000"
        entity_f = "skill:finishing-a-development-branch:2026-06-30T15:00:00.111111"

        # Graph nodes — keyed by "skill_session:" + raw entity_id
        engine._graph_nodes = {
            f"skill_session:{entity_b}": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Design the module",
            },
            f"skill_session:{entity_f}": {
                "type": "skill_session",
                "name": "finishing-a-development-branch",
                "description": "Finish the development branch",
            },
        }

        # Graph edges — brainstorming is parent_of finishing-a-development-branch
        engine._graph_edges = [
            {
                "from": f"skill_session:{entity_b}",
                "to": f"skill_session:{entity_f}",
                "relation": "parent_of",
                "weight": 0.7,
            },
        ]

        engine._memories = {
            "mem_b": {
                "id": "mem_b",
                "content": (
                    "[SKILL START] brainstorming: Design the module\n"
                    "[SKILL COMPLETE] duration_ms=3600000"
                ),
                "entity_ids": [entity_b],
                "tags": ["task:done", "skill:brainstorming", "domain:designing"],
                "created_at": "2026-06-30T14:00:00.000000",
            },
            "mem_f": {
                "id": "mem_f",
                "content": (
                    "[SKILL START] finishing-a-development-branch: Finish branch\n"
                    "[SKILL COMPLETE] duration_ms=1800000"
                ),
                "entity_ids": [entity_f],
                "tags": ["task:done", "skill:finishing-a-development-branch", "domain:governing"],
                "created_at": "2026-06-30T15:00:00.111111",
            },
        }

        # Wire up public API methods
        engine.list_graph_nodes = lambda: [{"id": k, **v} for k, v in engine._graph_nodes.items()]
        engine.list_graph_edges = lambda: engine._graph_edges
        engine.iter_memories = lambda: iter(engine._memories.values())

        result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                },
            )
        )

        assert len(result) == 1
        data = json.loads(result[0].text)

        assert data["total_count"] == 2
        assert data["chain_valid"] is True
        assert data["chain_complete"] is True
        assert len(data["gaps"]) == 0
        assert len(data["chain_warnings"]) == 0

        # Sessions are returned
        assert len(data["sessions"]) == 2
        skills = {s["skill_name"] for s in data["sessions"]}
        assert skills == {"brainstorming", "finishing-a-development-branch"}

        # Brainstorming should have a child
        b_sess = next(s for s in data["sessions"] if s["skill_name"] == "brainstorming")
        assert b_sess["status"] == "done"
        assert entity_f in b_sess["child_skills"]
        assert b_sess["parent_skill"] is None

        # Finishing branch should have a parent (brainstorming), no children
        f_sess = next(
            s for s in data["sessions"] if s["skill_name"] == "finishing-a-development-branch"
        )
        assert f_sess["status"] == "done"
        assert entity_b in f_sess["parent_skill"]
        assert f_sess["child_skills"] == []

    def test_trace_never_falls_back_to_raw_memories_when_public_iterator_is_generator(self):
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        entity_id = "skill:brainstorming:2026-07-10T12:00:00.000000"
        engine._memories = {
            "draft-synthesis": {
                "id": "draft-synthesis",
                "content": "[SKILL COMPLETE] DRAFT-SYNTHESIS-SECRET duration_ms=99",
                "entity_ids": [entity_id],
                "tags": ["task:done", "skill:brainstorming"],
                "memory_type": "synthesis",
            }
        }
        engine.list_graph_nodes = lambda: [
            {
                "id": f"skill_session:{entity_id}",
                "type": "skill_session",
                "name": "brainstorming",
            }
        ]
        engine.list_graph_edges = lambda: []
        engine.iter_memories = lambda: iter([])

        result = asyncio.run(handle_skill_session_trace(engine, {"session_scope": "all"}))
        payload = json.loads(result[0].text)

        assert "DRAFT-SYNTHESIS-SECRET" not in json.dumps(payload)
        assert payload["sessions"][0]["tracking_persistence"] == "entity_only"

    def test_trace_detects_orphan_active(self):
        """Mock 1 active session with last_accessed 45 min ago,
        verify gaps contain orphan_active."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()

        entity_id = "skill:brainstorming:2026-06-30T14:00:00.000000"

        engine._graph_nodes = {
            f"skill_session:{entity_id}": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Design the module",
            },
        }
        engine._graph_edges = []

        import datetime as _dt

        # last_accessed = 45 minutes ago
        la_ts = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=45)).isoformat()

        engine._memories = {
            "mem_orphan": {
                "id": "mem_orphan",
                "content": "[SKILL START] brainstorming: Design the module",
                "entity_ids": [entity_id],
                "tags": ["task:active", "skill:brainstorming", "domain:designing"],
                "created_at": "2026-06-30T14:00:00.000000",
                "last_accessed": la_ts,
            },
        }

        engine.list_graph_nodes = lambda: [{"id": k, **v} for k, v in engine._graph_nodes.items()]
        engine.list_graph_edges = lambda: engine._graph_edges
        engine.iter_memories = lambda: iter(engine._memories.values())

        result = asyncio.run(
            handle_skill_session_trace(
                engine,
                {
                    "session_scope": "all",
                },
            )
        )

        assert len(result) == 1
        data = json.loads(result[0].text)

        assert data["total_count"] == 1
        # chain_complete should be False because orphan_active is a gap
        assert data["chain_complete"] is False
        assert data["chain_valid"] is True  # no chain_warnings expected

        assert len(data["gaps"]) == 1
        gap = data["gaps"][0]
        assert gap["type"] == "orphan_active"
        assert gap["entity_id"] == entity_id
        assert gap["skill_name"] == "brainstorming"
        assert gap["idle_minutes"] >= 44  # allow small clock drift

        sessions = data["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["status"] == "active"

    def test_trace_ignores_non_start_memory_for_orphan_detection(self):
        """Graph-only/entity-only stage tracking should not create an
        orphan_active gap that skill_session_complete cannot repair."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        entity_id = "skill:sp-writing-plans:2026-07-03T02:07:11.763360"
        engine._graph_nodes = {
            f"skill_session:{entity_id}": {
                "type": "skill_session",
                "name": "sp-writing-plans",
                "description": "entity-only stage trace",
            },
        }
        engine._graph_edges = []

        import datetime as _dt

        la_ts = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=45)).isoformat()
        engine._memories = {
            "mem_entity_only": {
                "id": "mem_entity_only",
                "content": "legacy stage note without a skill start marker",
                "entity_ids": [entity_id],
                "tags": ["task:active", "skill:sp-writing-plans", "domain:designing"],
                "created_at": "2026-07-03T02:07:11.763360",
                "last_accessed": la_ts,
            },
        }

        engine.list_graph_nodes = lambda: [{"id": k, **v} for k, v in engine._graph_nodes.items()]
        engine.list_graph_edges = lambda: engine._graph_edges
        engine.iter_memories = lambda: iter(engine._memories.values())

        result = asyncio.run(handle_skill_session_trace(engine, {"session_scope": "all"}))
        data = json.loads(result[0].text)

        assert data["chain_complete"] is True
        assert data["gaps"] == []
        assert data["sessions"][0]["tracking_persistence"] == "entity_only"


class TestSkillSessionAudit:
    """Tests for handle_skill_session_audit."""

    def test_audit_with_no_memories(self):
        """Empty memory pool returns 0 scanned sessions and 0 gaps."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_audit,
        )

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._memories = {}
        engine.list_graph_nodes = lambda: []
        engine.iter_memories = lambda: iter([])

        result = asyncio.run(handle_skill_session_audit(engine, {}))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["scanned_sessions"] == 0
        assert len(data["gaps_found"]) == 0
        assert len(data["auto_fixed"]) == 0

    def test_audit_detects_missing_starts(self):
        """Memory mentions 'brainstorming' but no graph node exists --
        reports a gap with type='missing_start'."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_audit,
        )

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._memories = {
            "mem_1": {
                "id": "mem_1",
                "content": "I used brainstorming to design the module",
                "entity_ids": [],
                "tags": [],
            },
        }
        engine.list_graph_nodes = lambda: []
        engine.iter_memories = lambda: iter(engine._memories.values())

        result = asyncio.run(handle_skill_session_audit(engine, {}))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["scanned_sessions"] == 0
        assert len(data["gaps_found"]) >= 1

        # Find the brainstorming gap
        gap = next(
            (g for g in data["gaps_found"] if g["skill_name"] == "brainstorming"),
            None,
        )
        assert gap is not None, f"Expected a gap for 'brainstorming', got: {data['gaps_found']}"
        assert gap["type"] == "missing_start"
        assert gap["domain"] == "designing"
        assert len(data["auto_fixed"]) == 0
