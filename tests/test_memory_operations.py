import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from plastic_promise.core.context_engine import (
    ContextEngine,
    OrdinaryMemoryConflict,
    _SQLiteStorage,
)
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.mcp.tools.memory import handle_memory_forget
from plastic_promise.skills.engine import SkillEngine
from plastic_promise.skills.memory_operations import (
    _duplicate_authority,
    _smart_remember_mutation_authority,
    skill_smart_remember,
)


class TestSmartRemember:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "principle_activate",
                "memory_recall",
                "memory_store",
                "skill_session_start",
                "skill_session_complete",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _mock_response(self, data: dict) -> list:
        return [TextContent(type="text", text=json.dumps(data))]

    @staticmethod
    def _canonical_memory(content: str = "User prefers tabs over spaces") -> dict:
        return {
            "id": "mem_existing_042",
            "content": content,
            "project_id": "project:test",
            "tags": ["preference:formatting"],
            "category": "preference",
            "metadata_json": {"quality": {"status": "current"}},
            "worth_success": 3,
            "worth_failure": 1,
            "embedding_hash": "sha256:existing-index",
        }

    @staticmethod
    def _runtime_authority(
        *,
        actor: str = "codex",
        call_id: str = "call:server-smart-remember",
        project_id: str = "project:test",
        trust_score: float = 0.95,
        defense_decision: str = "allow",
    ) -> dict:
        return {
            "actor": actor,
            "call_id": call_id,
            "project_id": project_id,
            "trust_score": trust_score,
            "trust_tier": "high",
            "defense_decision": defense_decision,
        }

    def test_duplicate_authority_binds_top_level_recall_project(self, mock_engine):
        authority, reason = _duplicate_authority(
            mock_engine,
            {"project_id": "project:test"},
            {
                "id": "mem_existing_042",
                "content": "User prefers tabs over spaces",
                "project_id": "project:test",
                "origin_scope": "project",
            },
            "project:other",
        )

        assert authority is None
        assert reason == "ordinary_mutation_project_mismatch"
        mock_engine.get_memory_dict_for_review.assert_not_called()

    def test_smart_remember_authority_rejects_below_manifest_trust_even_if_allow(self):
        authority, reason = _smart_remember_mutation_authority(
            self._runtime_authority(trust_score=0.59, defense_decision="allow"),
            {"project_id": "project:test"},
            {"project_id": "project:test"},
            "project:test",
        )

        assert authority is None
        assert reason == "ordinary_mutation_runtime_authorization_denied"

    @pytest.mark.asyncio
    async def test_smart_remember_new_memory(self, mock_engine):
        """The conditional handler stores exactly once after an empty recall."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_principle_activate(engine, args):
            call_order.append("principle_activate")
            return self._mock_response({"activated": [{"id": 1, "name": "奥卡姆剃刀"}]})

        async def mock_memory_recall(engine, args):
            call_order.append("memory_recall")
            assert args["query"] == "The user prefers tabs over spaces"
            # No duplicates found
            return self._mock_response({"core": [], "related": [], "divergent": []})

        async def mock_memory_store(engine, args):
            call_order.append("memory_store")
            return self._mock_response(
                {
                    "stored": True,
                    "memory_id": "mem_new_001",
                    "content_preview": args["content"][:50],
                }
            )

        async def mock_session(engine, args):
            return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session

        se.register(skill_smart_remember)
        with (
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_recall",
                new=mock_memory_recall,
            ),
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_store",
                new=mock_memory_store,
            ),
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "The user prefers tabs over spaces",
                    "memory_type": "experience",
                    "source": "user",
                },
                caller="claude",
            )

        assert result.success is True
        assert result.data.get("action") == "stored"
        assert result.data.get("memory_id") == "mem_new_001"
        assert call_order == ["principle_activate", "memory_recall", "memory_store"]
        assert skill_smart_remember.atoms == ["principle_activate"]

    @pytest.mark.asyncio
    async def test_smart_remember_exact_duplicate_is_successful_noop(self, mock_engine):
        """Exact canonical content succeeds without store, replace, or reinforcement."""
        canonical = self._canonical_memory()
        mock_engine.get_memory_dict_for_review.return_value = canonical
        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_principle_activate(engine, args):
            call_order.append("principle_activate")
            return self._mock_response({"activated": []})

        async def mock_memory_recall(engine, args):
            call_order.append("memory_recall")
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_existing_042",
                            "content": "User prefers tabs over spaces",
                            "relevance": 0.92,
                            "project_id": "project:test",
                            "origin_scope": "project",
                        }
                    ],
                    "related": [],
                    "divergent": [],
                    "project_id": "project:test",
                }
            )

        async def mock_session(engine, args):
            return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session

        se.register(skill_smart_remember)
        with (
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_recall",
                new=mock_memory_recall,
            ),
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_store",
                new_callable=AsyncMock,
            ) as mock_store,
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "User prefers tabs over spaces",
                    "memory_type": "experience",
                    "source": "user",
                    "project_id": "project:test",
                },
                caller="claude",
            )

        assert result.success is True
        assert result.data.get("action") == "unchanged"
        assert result.data.get("reason") == "exact_duplicate"
        assert result.data.get("memory_id") == "mem_existing_042"
        assert call_order == ["principle_activate", "memory_recall"]
        mock_store.assert_not_awaited()
        mock_engine.mutate_ordinary_source.assert_not_called()
        mock_engine.update_memory_fields.assert_not_called()
        mock_engine.reinforce_ordinary_duplicate.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_remember_similar_duplicate_uses_guarded_coordinator(
        self,
        mock_engine,
    ):
        canonical = self._canonical_memory()
        mock_engine.get_memory_dict_for_review.return_value = canonical
        mock_engine.mutate_ordinary_source.return_value = SimpleNamespace(
            memory_id="mem_existing_042",
            operation="corrected",
            committed_memory_version=7,
            ordinary_index_job_id="ordinary-job",
            stale_synthesis_ids=("synthesis-stale",),
            synthesis_index_job_ids=("synthesis-job",),
        )
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_existing_042",
                            "content": canonical["content"],
                            "relevance": 0.92,
                            "project_id": "project:test",
                            "origin_scope": "project",
                        }
                    ],
                    "project_id": "project:test",
                }
            )

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)

        with (
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_recall",
                new=mock_memory_recall,
            ),
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_store",
                new_callable=AsyncMock,
            ) as mock_store,
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "User strongly prefers tabs over spaces",
                    "memory_type": "experience",
                    "project_id": "project:test",
                    "actor": "forged-client",
                    "call_id": "call:forged",
                    "trust_score": 1.0,
                    "_runtime_context": self._runtime_authority(),
                },
                caller="claude",
            )

        assert result.success is True
        assert result.data["action"] == "updated"
        assert result.data["committed_memory_version"] == 7
        assert result.data["stale_dependents"] == ["synthesis-stale"]
        mock_store.assert_not_awaited()
        mock_engine.update_memory_fields.assert_not_called()
        mock_engine.mutate_ordinary_source.assert_called_once()
        call = mock_engine.mutate_ordinary_source.call_args
        assert call.args == ("mem_existing_042",)
        assert call.kwargs["operation"] == "replace_content"
        assert call.kwargs["content"] == "User strongly prefers tabs over spaces"
        assert call.kwargs["actor"] == "codex"
        assert call.kwargs["call_id"] == "call:server-smart-remember"
        assert call.kwargs["expected_project_id"] == "project:test"
        assert call.kwargs["expected_content_hash"] == synthesis_content_hash(
            canonical["content"]
        )
        assert call.kwargs["expected_source_snapshot"] == {
            "tags": canonical["tags"],
            "category": canonical["category"],
            "metadata_json": canonical["metadata_json"],
            "worth_success": 3,
            "worth_failure": 1,
            "embedding_hash": "sha256:existing-index",
        }
        assert call.kwargs["require_source_available"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "runtime_context,expected_reason",
        [
            (None, "ordinary_mutation_runtime_authorization_required"),
            (
                _runtime_authority.__func__(
                    trust_score=0.59,
                    defense_decision="ask",
                ),
                "ordinary_mutation_runtime_authorization_denied",
            ),
            (
                _runtime_authority.__func__(defense_decision="ask"),
                "ordinary_mutation_runtime_authorization_denied",
            ),
            (
                _runtime_authority.__func__(defense_decision="deny"),
                "ordinary_mutation_runtime_authorization_denied",
            ),
        ],
    )
    async def test_smart_remember_similar_duplicate_requires_server_authority_before_read(
        self,
        mock_engine,
        runtime_context,
        expected_reason,
    ):
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_existing_042",
                            "content": "Existing evidence",
                            "relevance": 0.92,
                            "project_id": "project:test",
                            "origin_scope": "project",
                        }
                    ],
                    "project_id": "project:test",
                }
            )

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)
        params = {
            "content": "Replacement evidence",
            "memory_type": "experience",
            "project_id": "project:test",
        }
        if runtime_context is not None:
            params["_runtime_context"] = runtime_context

        with patch(
            "plastic_promise.skills.memory_operations.handle_memory_recall",
            new=mock_memory_recall,
        ):
            result = await se.exec("smart-remember", params=params, caller="claude")

        assert result.success is False
        assert result.data["reason"] == expected_reason
        mock_engine.get_memory_dict_for_review.assert_not_called()
        mock_engine.mutate_ordinary_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_remember_stale_duplicate_preserves_conflict(self, mock_engine):
        canonical = self._canonical_memory()
        mock_engine.get_memory_dict_for_review.return_value = canonical
        mock_engine.mutate_ordinary_source.side_effect = OrdinaryMemoryConflict(
            "ordinary_patch_cas_mismatch"
        )
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_existing_042",
                            "content": canonical["content"],
                            "relevance": 0.92,
                            "project_id": "project:test",
                            "origin_scope": "project",
                        }
                    ],
                    "project_id": "project:test",
                }
            )

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)

        with patch(
            "plastic_promise.skills.memory_operations.handle_memory_recall",
            new=mock_memory_recall,
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "Replacement evidence",
                    "memory_type": "experience",
                    "project_id": "project:test",
                    "_runtime_context": self._runtime_authority(),
                },
                caller="claude",
            )

        assert result.success is False
        assert result.data["action"] == "update_failed"
        assert result.data["reason"] == "ordinary_patch_cas_mismatch"
        assert result.errors == ["memory_update failed: ordinary_patch_cas_mismatch"]
        mock_engine.update_memory_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_remember_cross_project_duplicate_fails_closed(self, mock_engine):
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_other_project",
                            "content": "Existing evidence",
                            "relevance": 0.92,
                            "project_id": "project:other",
                            "origin_scope": "cross_project",
                        }
                    ],
                    "project_id": "project:test",
                }
            )

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)

        with patch(
            "plastic_promise.skills.memory_operations.handle_memory_recall",
            new=mock_memory_recall,
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "Replacement evidence",
                    "memory_type": "experience",
                    "project_id": "project:test",
                    "_runtime_context": self._runtime_authority(),
                },
                caller="claude",
            )

        assert result.success is False
        assert result.data["reason"] == "ordinary_mutation_project_mismatch"
        mock_engine.get_memory_dict_for_review.assert_not_called()
        mock_engine.mutate_ordinary_source.assert_not_called()
        mock_engine.update_memory_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_remember_missing_duplicate_authority_fails_closed(
        self,
        mock_engine,
    ):
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response(
                {
                    "core": [
                        {
                            "id": "mem_without_authority",
                            "content": "Existing evidence",
                            "relevance": 0.92,
                        }
                    ]
                }
            )

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)

        with (
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_recall",
                new=mock_memory_recall,
            ),
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_store",
                new_callable=AsyncMock,
            ) as mock_store,
        ):
            result = await se.exec(
                "smart-remember",
                params={
                    "content": "Replacement evidence",
                    "memory_type": "experience",
                    "_runtime_context": self._runtime_authority(),
                },
                caller="claude",
            )

        assert result.success is False
        assert result.data["reason"] == "ordinary_mutation_source_project_required"
        mock_store.assert_not_awaited()
        mock_engine.get_memory_dict_for_review.assert_not_called()
        mock_engine.mutate_ordinary_source.assert_not_called()
        mock_engine.update_memory_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_remember_store_failure_preserves_payload(self, mock_engine):
        se = SkillEngine(mock_engine)

        async def mock_principle_activate(_engine, _args):
            return self._mock_response({"activated": []})

        async def mock_memory_recall(_engine, _args):
            return self._mock_response({"core": []})

        async def mock_session(_engine, _args):
            return self._mock_response({"entity_id": "skill:test", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session
        se.register(skill_smart_remember)

        store_payload = {
            "stored": False,
            "reason": "quality_filtered",
            "pipeline": {"embedded->migrated": 0},
        }
        with (
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_recall",
                new=mock_memory_recall,
            ),
            patch(
                "plastic_promise.skills.memory_operations.handle_memory_store",
                new_callable=AsyncMock,
                return_value=self._mock_response(store_payload),
            ) as mock_store,
        ):
            result = await se.exec(
                "smart-remember",
                params={"content": "Candidate evidence", "memory_type": "experience"},
                caller="claude",
            )

        assert result.success is False
        assert result.data == {"action": "store_failed", **store_payload}
        assert result.errors == ["memory_store failed: quality_filtered"]
        mock_store.assert_awaited_once()


class TestMemoryForget:
    @pytest.mark.asyncio
    async def test_memory_forget_keeps_committed_envelope_when_tag_read_fails(self):
        engine = MagicMock()
        engine.get_memory.return_value = SimpleNamespace(memory_type="experience")
        engine.get_memory_dict_for_review.return_value = {
            "id": "mem_001",
            "project_id": "project:test",
            "memory_type": "experience",
        }
        engine.mutate_ordinary_source.return_value = SimpleNamespace(
            memory_id="mem_001",
            operation="forgotten",
            committed_memory_version=4,
            ordinary_index_job_id="ordinary-job",
            stale_synthesis_ids=("synthesis-stale",),
            synthesis_index_job_ids=("synthesis-job",),
        )
        sqlite = MagicMock()
        sqlite._conn = None
        sqlite.get.side_effect = RuntimeError("post-commit canonical read failed")
        engine._sqlite = sqlite

        result = await handle_memory_forget(
            engine,
            {"memory_id": "mem_001", "reason": "authorized"},
            _runtime_context={
                "actor": "codex",
                "call_id": "call:public-forget",
                "project_id": "project:test",
                "trust_score": 0.95,
                "trust_tier": "high",
                "defense_decision": "allow",
            },
        )

        payload = json.loads(result[0].text)
        assert payload["forgotten"] is True
        assert payload["committed"] is True
        assert payload["ordinary_index_job_id"] == "ordinary-job"
        assert payload["synthesis_index_job_ids"] == ["synthesis-job"]
        assert payload["tags"] == []

    @pytest.mark.asyncio
    async def test_memory_forget_persists_tombstone_and_checked_delete(
        self,
        tmp_path,
        monkeypatch,
    ):
        from plastic_promise.core import synthesis_maintenance

        storage = _SQLiteStorage(str(tmp_path / "forget.db"))
        storage.upsert(
            "mem_001",
            {
                "id": "mem_001",
                "content": "Evidence that must remain as an auditable tombstone.",
                "memory_type": "experience",
                "source": "user",
                "project_id": "project:test",
                "visibility": "project",
                "tags": ["existing"],
                "metadata_json": {"quality": {"status": "current"}},
                "raw_content": "Evidence that must remain as an auditable tombstone.",
                "l0_abstract": "Auditable tombstone evidence.",
                "l1_summary": "- auditable tombstone evidence",
                "l2_content": "Evidence that must remain as an auditable tombstone.",
                "embedding_text": "Auditable tombstone evidence.",
                "embedding_hash": "sha256:forget-before",
                "search_text": "auditable tombstone evidence",
            },
        )
        engine = ContextEngine(use_sqlite=False)
        engine._sqlite = storage
        engine._memories = dict(storage.iter_all())
        engine._ldb = MagicMock()
        monkeypatch.setattr(synthesis_maintenance, "replay_memory_index_jobs", lambda *_a, **_k: 0)
        monkeypatch.setattr(
            synthesis_maintenance,
            "replay_synthesis_index_jobs",
            lambda *_a, **_k: 0,
        )

        try:
            result = await handle_memory_forget(
                engine,
                {"memory_id": "mem_001", "reason": "user requested removal"},
                _runtime_context={
                    "actor": "codex",
                    "call_id": "call:public-forget",
                    "project_id": "project:test",
                    "trust_score": 0.95,
                    "trust_tier": "high",
                    "defense_decision": "allow",
                },
            )

            payload = json.loads(result[0].text)
            assert payload["forgotten"] is True
            assert payload["committed"] is True
            assert payload["operation"] == "forgotten"
            assert payload["reason"] == ""
            assert payload["stale_dependents"] == []
            assert payload["ordinary_index_job_id"]
            assert payload["synthesis_index_job_ids"] == []
            assert payload["pending_job_ids"] == [payload["ordinary_index_job_id"]]
            assert payload["completed_job_ids"] == []

            canonical = storage.get("mem_001")
            assert canonical is not None
            assert canonical["metadata_json"]["quality"]["status"] == "forgotten"
            assert engine.get_memory("mem_001") is None
            job = storage._conn.execute(
                "SELECT status, payload_json FROM store_outbox WHERE outbox_id = ?",
                (payload["ordinary_index_job_id"],),
            ).fetchone()
            assert job[0] == "pending"
            assert json.loads(job[1])["action"] == "delete"
            engine._ldb.delete.assert_not_called()
        finally:
            storage._conn.close()


def _make_mock_tool(name: str):
    """Create a minimal mock tool with a .name attribute."""
    tool = MagicMock()
    tool.name = name
    return tool
