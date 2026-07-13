"""Tests for MemoryGC.merge_similar() — batch similar memory merging."""

import json
from unittest.mock import MagicMock, patch

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.core.synthesis_retrieval import _source_is_available
from plastic_promise.memory.soul_memory import MemoryGC, MemoryRecord, RecMem


class TestMemoryMerge:
    """Test suite for merge_similar behavior."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create MemoryGC with mocked RecMem and LanceDB."""
        self.rec_mem = MagicMock(spec=RecMem)
        self.rec_mem._records = {}
        self.rec_mem.health_ratio = 0.9
        self.gc = MemoryGC(self.rec_mem)
        yield

    def _make_record(self, mid, content, tier="L3", created_at="2026-06-30T12:00:00"):
        """Helper: create a MemoryRecord and add to rec_mem._records."""
        record = MemoryRecord(
            content=content,
            memory_type="experience",
            source="user",
            memory_id=mid,
            worth_success=5,
            worth_failure=0,
            tier=tier,
        )
        record.created_at = created_at
        self.rec_mem._records[mid] = record
        return record

    @staticmethod
    def _register_source(
        engine,
        memory_id,
        content,
        *,
        worth_success,
        worth_failure,
        project_id="project:merge-test",
    ):
        assert (
            engine.register_memory(
                {
                    "id": memory_id,
                    "content": content,
                    "memory_type": "experience",
                    "source": "test",
                    "source_class": "experience",
                    "project_id": project_id,
                    "visibility": "project",
                    "tags": ["status:current"],
                    "worth_success": worth_success,
                    "worth_failure": worth_failure,
                    "raw_content": content,
                    "l0_abstract": content,
                    "l1_summary": content,
                    "l2_content": content,
                    "embedding_text": content,
                    "embedding_hash": f"sha256:{memory_id}",
                    "search_text": content,
                    "metadata_json": {"quality": {"status": "current"}},
                }
            )
            == memory_id
        )

    def test_merge_similar_dry_run_reports_candidates(self):
        """dry_run=True should report candidates without modifying records."""
        # Setup: two records with vectors
        self._make_record("mem_001", "User likes Rust for backend")
        self._make_record("mem_002", "User prefers Rust for server development")

        # Mock LanceDB — returns high similarity between mem_001 and mem_002
        mock_ldb = MagicMock()
        # First call (mem_001) returns mem_002 as similar
        # Second call (mem_002) returns mem_001 as similar
        mock_ldb.search_similar.side_effect = [
            [("mem_002", 0.82), ("mem_003", 0.45)],  # mem_001 → similar to mem_002
            [("mem_001", 0.82)],  # mem_002 → similar to mem_001
        ]

        # Mock engine
        engine = MagicMock()
        engine._memories = {
            "mem_001": {
                "_vector": [0.1] * 1024,
                "access_count": 0,
                "worth_success": 0,
                "project_id": "project:merge-test",
            },
            "mem_002": {
                "_vector": [0.2] * 1024,
                "access_count": 0,
                "worth_success": 0,
                "project_id": "project:merge-test",
            },
        }
        engine._ldb = mock_ldb
        engine._sqlite = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=True)

        assert result["dry_run"] is True
        assert result["candidates_found"] >= 1
        assert len(result["merged_pairs"]) >= 1
        # No records should be removed in dry_run
        assert "mem_001" in self.rec_mem._records
        assert "mem_002" in self.rec_mem._records
        engine.mutate_ordinary_source.assert_not_called()
        engine.update_memory_fields.assert_not_called()

    @pytest.mark.parametrize(
        ("project_a", "project_b"),
        [
            ("", "project:a"),
            ("project:a", ""),
            ("project:a", "project:b"),
        ],
    )
    def test_merge_similar_requires_nonempty_equal_candidate_projects(
        self,
        project_a,
        project_b,
    ):
        self._make_record("project-a-memory", "first project evidence")
        self._make_record("project-b-memory", "second project evidence")
        ldb = MagicMock()
        ldb.search_similar.side_effect = [
            [("project-b-memory", 0.99)],
            [("project-a-memory", 0.99)],
        ]
        engine = MagicMock()
        engine._memories = {
            "project-a-memory": {
                "_vector": [0.1] * 1024,
                "project_id": project_a,
            },
            "project-b-memory": {
                "_vector": [0.2] * 1024,
                "project_id": project_b,
            },
        }
        engine._ldb = ldb
        engine._sqlite = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=False)

        assert result["candidates_found"] == 0
        assert result["would_merge"] == 0
        assert result["merged_pairs"] == []
        engine.mutate_ordinary_source.assert_not_called()
        assert set(self.rec_mem._records) == {
            "project-a-memory",
            "project-b-memory",
        }

    def test_merge_similar_persists_loser_tombstone_and_stales_dependents(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A completed merge owns availability through the canonical coordinator."""
        monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "merge.db"))
        monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
        monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
        engine = ContextEngine(use_sqlite=True)
        try:
            engine.ensure_heavy_init = lambda: None
            self._register_source(
                engine,
                "merge-survivor",
                "High-value evidence about canonical merge behavior.",
                worth_success=10,
                worth_failure=0,
            )
            self._register_source(
                engine,
                "merge-loser",
                "Lower-value duplicate evidence about canonical merge behavior.",
                worth_success=0,
                worth_failure=10,
            )
            self._register_source(
                engine,
                "merge-counterpart",
                "Independent evidence supporting the verified synthesis.",
                worth_success=5,
                worth_failure=0,
            )

            store = SynthesisStore(engine._sqlite._conn, engine=engine)
            draft = store.create_draft(
                "The independent sources support one verified merge conclusion.",
                ["merge-loser", "merge-counterpart"],
                synthesis_key="memory-gc:merge-loser",
                validity_scope="project:merge-test",
                project_id="project:merge-test",
                visibility="project",
                actor="test",
                call_id="call-create-merge-dependent",
            )
            verified = store.verify(
                draft.memory_id,
                "reviewer",
                "call-verify-merge-dependent",
                draft.revision,
            )

            rec_mem = RecMem(engine)
            gc = MemoryGC(rec_mem)
            engine._memories["merge-survivor"]["_vector"] = [0.1] * 1024
            engine._memories["merge-loser"]["_vector"] = [0.2] * 1024
            ldb = MagicMock()
            ldb.search_similar.side_effect = [
                [("merge-loser", 0.92)],
                [("merge-survivor", 0.92)],
            ]
            engine._ldb = ldb
            runtime_before = engine._memories

            with (
                patch.object(
                    engine,
                    "mutate_ordinary_source",
                    wraps=engine.mutate_ordinary_source,
                ) as mutate,
                patch.object(
                    engine,
                    "get_memory_dict_for_review",
                    wraps=engine.get_memory_dict_for_review,
                ) as get_canonical,
            ):
                result = gc.merge_similar(threshold=0.70, dry_run=False)

            assert result["merged_pairs"] == [
                {
                    "survivor": "merge-survivor",
                    "merged": ["merge-loser"],
                    "similarity": 0.92,
                }
            ]
            mutate.assert_called_once()
            get_canonical.assert_any_call("merge-survivor")
            mutation = mutate.call_args
            assert mutation.args == ("merge-loser",)
            assert mutation.kwargs["operation"] == "forgotten"
            assert "merge-survivor" in mutation.kwargs["reason"]
            assert mutation.kwargs["actor"] == "memory_gc"
            assert mutation.kwargs["call_id"].startswith("internal:memory_gc:merge:")
            assert mutation.kwargs["expected_project_id"] == "project:merge-test"
            assert mutation.kwargs["expected_content_hash"]
            assert mutation.kwargs["expected_source_snapshot"]["category"]
            assert mutation.kwargs["require_source_available"] is True
            assert set(mutation.kwargs["expected_peer_snapshots"]) == {"merge-survivor"}
            peer_metadata = mutation.kwargs["peer_metadata_replacements"]
            assert [
                item["memory_id"] for item in peer_metadata["merge-survivor"]["merged_from"]
            ] == ["merge-loser"]

            loser = engine._sqlite.get("merge-loser")
            assert loser is not None
            assert _source_is_available(loser) is False
            assert loser["metadata_json"]["quality"]["status"] == "forgotten"
            assert "merge-survivor" in loser["metadata_json"]["quality"]["reason"]
            assert "merge-loser" in engine._memories
            assert "merge-loser" in runtime_before
            assert store.get(verified.memory_id).status == "stale"
            assert store.get(verified.memory_id).stale_reason == "source_forgotten"
            survivor = engine._sqlite.get("merge-survivor")
            assert [item["memory_id"] for item in survivor["metadata_json"]["merged_from"]] == [
                "merge-loser"
            ]
            memory_jobs = [
                json.loads(row[0])
                for row in engine._sqlite._conn.execute(
                    "SELECT payload_json FROM store_outbox WHERE tool_name = 'memory_index'"
                ).fetchall()
            ]
            assert any(
                payload["memory_id"] == "merge-survivor" and payload["action"] == "upsert"
                for payload in memory_jobs
            )

            reopened = ContextEngine(use_sqlite=True)
            try:
                persisted = reopened._sqlite.get("merge-survivor")
                assert [
                    item["memory_id"] for item in persisted["metadata_json"]["merged_from"]
                ] == ["merge-loser"]
            finally:
                reopened._sqlite._conn.close()
        finally:
            engine._sqlite._conn.close()

    def test_merge_similar_peer_metadata_failure_rolls_back_loser_and_jobs(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "merge-rollback.db"))
        engine = ContextEngine(use_sqlite=True)
        try:
            self._register_source(
                engine,
                "merge-rollback-survivor",
                "Higher-value evidence selected as the durable survivor.",
                worth_success=10,
                worth_failure=0,
            )
            self._register_source(
                engine,
                "merge-rollback-loser",
                "Lower-value evidence selected as the merge loser.",
                worth_success=0,
                worth_failure=10,
            )
            rec_mem = RecMem(engine)
            gc = MemoryGC(rec_mem)
            engine._memories["merge-rollback-survivor"]["_vector"] = [0.1] * 1024
            engine._memories["merge-rollback-loser"]["_vector"] = [0.2] * 1024
            ldb = MagicMock()
            ldb.search_similar.side_effect = [
                [("merge-rollback-loser", 0.93)],
                [("merge-rollback-survivor", 0.93)],
            ]
            engine._ldb = ldb
            before = {
                table: engine._sqlite._conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("memories", "memory_lineage", "store_outbox", "memory_version")
            }
            original_patch = engine._sqlite.patch_ordinary

            def fail_peer_patch(memory_id, **kwargs):
                if memory_id == "merge-rollback-survivor":
                    raise RuntimeError("injected peer metadata patch failure")
                return original_patch(memory_id, **kwargs)

            monkeypatch.setattr(engine._sqlite, "patch_ordinary", fail_peer_patch)

            result = gc.merge_similar(threshold=0.70, dry_run=False)

            assert result["merged_pairs"] == []
            assert result["error"] == "injected peer metadata patch failure"
            after = {
                table: engine._sqlite._conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("memories", "memory_lineage", "store_outbox", "memory_version")
            }
            assert after == before
            loser = engine._sqlite.get("merge-rollback-loser")
            survivor = engine._sqlite.get("merge-rollback-survivor")
            assert _source_is_available(loser) is True
            assert "merged_from" not in survivor["metadata_json"]
        finally:
            engine._sqlite._conn.close()

    def test_merge_similar_mutation_failure_is_not_reported_as_merged(self):
        """A failed canonical mutation must leave the pair out of the result."""
        survivor = self._make_record("merge-success", "higher value")
        loser = self._make_record("merge-failure", "lower value")
        survivor.worth_success = 10
        survivor.worth_failure = 0
        loser.worth_success = 0
        loser.worth_failure = 10

        ldb = MagicMock()
        ldb.search_similar.side_effect = [
            [("merge-failure", 0.91)],
            [("merge-success", 0.91)],
        ]
        engine = MagicMock()
        engine._memories = {
            "merge-success": {
                **survivor.to_dict(),
                "_vector": [0.1] * 1024,
                "project_id": "project:merge-test",
            },
            "merge-failure": {
                **loser.to_dict(),
                "_vector": [0.2] * 1024,
                "project_id": "project:merge-test",
            },
        }
        engine._ldb = ldb
        engine._sqlite = None
        engine.mutate_ordinary_source.side_effect = RuntimeError("injected failure")
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=False)

        assert result["would_merge"] == 0
        assert result["would_free"] == 0
        assert result["merged_pairs"] == []
        assert result["error"] == "injected failure"
        assert "merged_from" not in survivor.metadata
        assert "merged_into" not in loser.metadata
        assert "merge-failure" in engine._memories
        engine.update_memory_fields.assert_not_called()

    def test_merge_similar_survivor_keeps_higher_worth(self):
        """Survivor should be the record with higher worth_score."""
        r1 = self._make_record(
            "mem_high", "Rust is great", created_at="2026-06-01T00:00:00"
        )  # older but higher score
        r2 = self._make_record(
            "mem_low", "Rust is excellent", created_at="2026-06-30T00:00:00"
        )  # newer but lower score
        # Force different worth_success/failure so computed worth_score differs
        r1.worth_success = 10
        r1.worth_failure = 0
        r2.worth_success = 0
        r2.worth_failure = 5

        mock_ldb = MagicMock()
        mock_ldb.search_similar.side_effect = [
            [("mem_low", 0.78)],
            [("mem_high", 0.78)],
        ]

        engine = MagicMock()
        engine._memories = {
            "mem_high": {
                "_vector": [0.1] * 1024,
                "project_id": "project:merge-test",
            },
            "mem_low": {
                "_vector": [0.2] * 1024,
                "project_id": "project:merge-test",
            },
        }
        engine._ldb = mock_ldb
        engine._sqlite = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar(threshold=0.70, dry_run=True)
        pairs = result["merged_pairs"]
        if pairs:
            # mem_high (0.85) should survive over mem_low (0.45)
            pair = pairs[0]
            assert pair["survivor"] == "mem_high"
            assert "mem_low" in pair["merged"]

    def test_merge_similar_no_lancedb_returns_error(self):
        """When LanceDB is unavailable, merge returns error dict."""
        self._make_record("mem_a", "content A")
        self._make_record("mem_b", "content B")

        engine = MagicMock()
        engine._memories = {
            "mem_a": {"_vector": None},
            "mem_b": {"_vector": None},
        }
        engine._ldb = None  # No LanceDB
        self.rec_mem._engine = engine

        result = self.gc.merge_similar()
        assert "error" in result or result["candidates_found"] == 0

    def test_merge_similar_empty_pool(self):
        """Empty memory pool returns zero candidates."""
        engine = MagicMock()
        engine._memories = {}
        engine._ldb = None
        self.rec_mem._engine = engine

        result = self.gc.merge_similar()
        assert result["candidates_found"] == 0
        assert result["would_merge"] == 0
