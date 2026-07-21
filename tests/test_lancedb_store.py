"""Tests for LanceDBStore — vector storage with ANN + FTS."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import plastic_promise.core.lancedb_store as lancedb_store_module
from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.core.lancedb_store import EMB_DIM, LanceDBStore


class VectorTestEmbedder:
    model_name = "vector-test"
    dim = EMB_DIM

    def embed(self, text):
        return [0.1] * EMB_DIM

    def embed_batch(self, texts):
        return [[0.1] * EMB_DIM for _ in texts]


class RecordingVectorEmbedder(VectorTestEmbedder):
    def __init__(self):
        self.texts = []

    def embed(self, text):
        self.texts.append(text)
        return super().embed(text)


class StructuredVectorEmbedder(RecordingVectorEmbedder):
    index_model_name = (
        "mxbai-embed-large|chunking=structure-v1|target_chars=512|"
        "hard_chars=512|max_chunks=64|max_source_chars=2000000|budget=characters-fallback"
    )


class PreparingVectorEmbedder(RecordingVectorEmbedder):
    index_model_name = "vector-test"

    def __init__(self):
        super().__init__()
        self.prepared = []

    def prepare_index_text(self, text):
        self.prepared.append(text)
        return f"PLAN::{text}"


class RepairEngine:
    def __init__(self, memories, sqlite_store=None):
        self._memories = memories
        self._sqlite = sqlite_store

    @property
    def memory_count(self):
        return len(self._memories)

    def update_memory_fields(self, memory_id, **fields):
        self._memories[memory_id].update(fields)
        if self._sqlite is not None:
            self._sqlite.upsert(memory_id, self._memories[memory_id])
        return True


class TestLanceDBStore:
    """Full test suite for LanceDBStore."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a fresh temp directory for each test."""
        self._tmpdir = tempfile.mkdtemp(prefix="lancedb_test_")
        self._db_path = os.path.join(self._tmpdir, "test.lancedb")
        self._embedder = FallbackEmbedder(dim=EMB_DIM)
        self.store = LanceDBStore(self._db_path, self._embedder)
        yield
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _require_vectors(self):
        """Skip the current test if vector operations are disabled (FallbackEmbedder)."""
        if self.store._vectors_disabled:
            pytest.skip("Vector operations disabled (FallbackEmbedder)")

    def test_init_creates_table(self):
        """Table should be created automatically."""
        assert self.store._table is not None
        assert self.store.count_rows() == 0

    def test_init_uses_supported_fts_index_api(self, tmp_path, recwarn):
        LanceDBStore(str(tmp_path / "supported-fts-api.lancedb"), VectorTestEmbedder())

        assert not [warning for warning in recwarn if "create_fts_index" in str(warning.message)]

    def test_insert_and_count(self):
        """Insert a row and verify count increases."""
        self._require_vectors()
        vec = [0.1] * EMB_DIM
        self.store.insert("mem_001", vec, "hello world")
        assert self.store.count_rows() == 1

    def test_insert_duplicate_noop(self):
        """Inserting the same memory_id twice should be a no-op."""
        self._require_vectors()
        vec = [0.1] * EMB_DIM
        self.store.insert("mem_001", vec, "hello world")
        self.store.insert("mem_001", [0.2] * EMB_DIM, "duplicate")
        assert self.store.count_rows() == 1

    def test_replace_checked_updates_existing_row_exactly(self, tmp_path):
        store = LanceDBStore(
            str(tmp_path / "checked-replace.lancedb"),
            VectorTestEmbedder(),
        )
        vector_v1 = [0.1] * EMB_DIM
        vector_v2 = [0.9] * EMB_DIM
        store.insert_checked(
            "revisioned-memory",
            vector_v1,
            "revision one search text",
            tier="L1",
            category="fact",
            scope="global",
        )

        store.replace_checked(
            "revisioned-memory",
            vector_v2,
            "revision two exact search text",
            tier="L2",
            category="decision",
            scope="project:test",
        )

        rows = (
            store._table.search()
            .where(
                "memory_id = 'revisioned-memory'",
                prefilter=True,
            )
            .limit(2)
            .to_list()
        )
        assert len(rows) == 1
        assert rows[0]["text"] == "revision two exact search text"
        assert rows[0]["tier"] == "L2"
        assert rows[0]["category"] == "decision"
        assert rows[0]["scope"] == "project:test"
        assert list(rows[0]["vector"]) == pytest.approx(vector_v2)

    def test_replace_checked_failure_propagates_and_retry_recovers(self):
        class ReplaceTable:
            def __init__(self):
                self.rows = [
                    {
                        "memory_id": "revisioned-memory",
                        "vector": [0.1] * EMB_DIM,
                        "text": "revision one",
                        "tier": "L1",
                        "category": "fact",
                        "scope": "global",
                    }
                ]
                self.fail_add_once = True
                self.add_calls = 0
                self.delete_calls = 0

            def search(self):
                return self

            def where(self, *_args, **_kwargs):
                return self

            def limit(self, _limit):
                return self

            def to_list(self):
                return list(self.rows)

            def delete(self, _predicate):
                self.delete_calls += 1
                self.rows.clear()

            def add(self, rows):
                self.add_calls += 1
                if self.fail_add_once:
                    self.fail_add_once = False
                    raise RuntimeError("replacement add failed")
                self.rows.extend(rows)

        table = ReplaceTable()
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._table = table
        vector_v2 = [0.9] * EMB_DIM
        replacement = (
            "revisioned-memory",
            vector_v2,
            "revision two",
            "L2",
            "decision",
            "project:test",
        )

        with pytest.raises(RuntimeError, match="replacement add failed"):
            store.replace_checked(*replacement)
        assert table.rows == []

        store.replace_checked(*replacement)
        assert table.rows[0]["text"] == "revision two"
        assert table.rows[0]["vector"] == vector_v2
        writes_after_retry = (table.delete_calls, table.add_calls)

        store.replace_checked(*replacement)
        assert (table.delete_calls, table.add_calls) == writes_after_retry

    def test_insert_with_tier_and_scope(self):
        """Insert with custom tier, category, and scope."""
        self._require_vectors()
        vec = [0.1] * EMB_DIM
        self.store.insert(
            "mem_002", vec, "scoped memory", tier="L3", category="test", scope="private"
        )
        assert self.store.count_rows() == 1

    def test_search_returns_results(self):
        """Vector search should return inserted rows."""
        self._require_vectors()
        vec = [0.5] * EMB_DIM
        self.store.insert("mem_001", vec, "test vector")
        results = self.store.search([0.5] * EMB_DIM, k=10)
        assert len(results) == 1
        mid, score, text, tier, scope = results[0]
        assert mid == "mem_001"
        assert score >= 0.0 and score <= 1.0
        assert text == "test vector"

    def test_search_empty_table(self):
        """Search on empty table returns empty list."""
        results = self.store.search([0.5] * EMB_DIM, k=10)
        assert results == []

    def test_get_vectors_returns_only_requested_rows(self, tmp_path):
        store = LanceDBStore(str(tmp_path / "bulk-vectors.lancedb"), VectorTestEmbedder())
        vectors = {
            "mem_001": [0.1] * EMB_DIM,
            "mem_002": [0.2] * EMB_DIM,
            "mem_003": [0.3] * EMB_DIM,
        }
        for memory_id, vector in vectors.items():
            store.insert(memory_id, vector, memory_id)

        result = store.get_vectors(["mem_001", "mem_003", "missing", "mem_001"])

        assert set(result) == {"mem_001", "mem_003"}
        assert result["mem_001"] == pytest.approx(vectors["mem_001"])
        assert result["mem_003"] == pytest.approx(vectors["mem_003"])

    def test_search_scope_filter(self):
        """Scope filter should exclude non-matching rows."""
        self._require_vectors()
        self.store.insert("mem_001", [0.1] * EMB_DIM, "global doc", scope="global")
        self.store.insert("mem_002", [0.1] * EMB_DIM, "private doc", scope="private")
        results = self.store.search([0.1] * EMB_DIM, k=10, scope="private")
        assert len(results) == 1
        assert results[0][0] == "mem_002"

    def test_search_tier_filter(self):
        """Tier filter should exclude non-matching rows."""
        self._require_vectors()
        self.store.insert("mem_001", [0.1] * EMB_DIM, "L1 doc", tier="L1")
        self.store.insert("mem_002", [0.1] * EMB_DIM, "L3 doc", tier="L3")
        results = self.store.search([0.1] * EMB_DIM, k=10, tier="L3")
        assert len(results) == 1
        assert results[0][0] == "mem_002"

    def test_search_fts(self):
        """Full-text search should find matching text."""
        self._require_vectors()
        self.store.insert("mem_001", [0.0] * EMB_DIM, "the quick brown fox")
        self.store.insert("mem_002", [0.0] * EMB_DIM, "lazy dog sleeps")
        results = self.store.search_fts("quick", k=10)
        assert len(results) >= 1
        mids = {r[0] for r in results}
        assert "mem_001" in mids

    def test_search_fts_projects_non_vector_columns_and_records_failure(self):
        class BrokenQuery:
            def __init__(self):
                self.selected = None

            def select(self, columns):
                self.selected = columns
                return self

            def limit(self, _limit):
                return self

            def to_list(self):
                raise RuntimeError("native fts failed")

        class BrokenTable:
            def __init__(self):
                self.query = BrokenQuery()

            def search(self, *_args, **_kwargs):
                return self.query

        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._fts_ready = True
        store._table = BrokenTable()
        store._last_search_diagnostics = []

        assert store.search_fts("query") == []
        assert "vector" not in store._table.query.selected
        assert "_score" in store._table.query.selected
        assert store.consume_search_diagnostics() == [
            {
                "channel": "fts",
                "reason": "lancedb_fts_query_failed",
                "error_class": "RuntimeError",
            }
        ]
        assert store.consume_search_diagnostics() == []

    @pytest.mark.parametrize(
        "query",
        [
            "\u672a\u7ecf\u786e\u8ba4\u7684\u673a\u5668\u5f52\u7eb3\u4e0d\u80fd\u5f53\u4f5c\u4e8b\u5b9e",
            "\u6765\u6e90\u53d8\u5316\u540e\u65e7\u7684\u673a\u5668\u7ed3\u8bba\u4e0d\u80fd\u8fd4\u56de",
        ],
    )
    def test_incremental_native_fts_seed_does_not_degrade_on_cjk_queries(self, tmp_path, query):
        fixture_path = Path(__file__).parent / "fixtures" / "recall_quality" / "v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        store = LanceDBStore(str(tmp_path / "incremental-fts.lancedb"), VectorTestEmbedder())

        for memory in fixture["corpus"]:
            store.insert_checked(
                memory["memory_id"],
                [0.01] * EMB_DIM,
                memory["content"],
                tier="L1",
                category=memory.get("category", "other"),
                scope=memory.get("project_id", "global"),
            )

        index_stats = store._table.index_stats("text_idx")
        assert index_stats is not None
        assert index_stats.num_unindexed_rows > 0
        assert isinstance(store.search_fts(query, k=20), list)
        assert store.consume_search_diagnostics() == []

    def test_update_replaces_row(self):
        """Update should replace the row (delete + insert)."""
        self._require_vectors()
        self.store.insert("mem_001", [0.1] * EMB_DIM, "original")
        self.store.update("mem_001", [0.9] * EMB_DIM, "updated")
        assert self.store.count_rows() == 1
        results = self.store.search([0.9] * EMB_DIM, k=10)
        assert len(results) == 1
        assert results[0][2] == "updated"

    def test_delete_removes_row(self):
        """Delete should remove the row by memory_id."""
        self._require_vectors()
        self.store.insert("mem_001", [0.1] * EMB_DIM, "to be deleted")
        assert self.store.count_rows() == 1
        self.store.delete("mem_001")
        assert self.store.count_rows() == 0

    def test_delete_nonexistent(self):
        """Deleting a non-existent memory_id should not error."""
        self.store.delete("mem_nonexistent")  # should not raise

    def test_count_rows_empty(self):
        """Fresh table should have 0 rows."""
        assert self.store.count_rows() == 0

    def test_count_rows_after_inserts(self):
        """Count should reflect multiple inserts."""
        self._require_vectors()
        for i in range(5):
            self.store.insert(f"mem_{i:03d}", [float(i) / 10.0] * EMB_DIM, f"text {i}")
        assert self.store.count_rows() == 5

    def test_search_k_respected(self):
        """k parameter should limit results."""
        self._require_vectors()
        for i in range(10):
            self.store.insert(f"mem_{i:03d}", [0.5] * EMB_DIM, f"text {i}")
        results = self.store.search([0.5] * EMB_DIM, k=3)
        assert len(results) == 3

    def test_backfill_skips_when_full(self):
        """backfill should return 0 when LanceDB already has >= rows than engine."""
        self._require_vectors()
        # Insert enough rows to match mock engine count
        for i in range(3):
            self.store.insert(f"mem_{i:03d}", [0.0] * EMB_DIM, f"text {i}")

        class MockEngine:
            memory_count = 3

        count = self.store.backfill(MockEngine())
        assert count == 0  # already full, skip

    def test_backfill_inserts_missing(self):
        """backfill should insert memories not yet in LanceDB."""
        self._require_vectors()
        # LanceDB has 2 rows, SQLite has 5 with 2 overlapping
        for i in range(2):
            self.store.insert(f"mem_{i:03d}", [0.0] * EMB_DIM, f"existing {i}")

        class MockRecord:
            def __init__(self, mid, content, tier="L1", category="other", scope="global"):
                self.id = mid
                self.content = content
                self.tier = tier
                self.category = category
                self.scope = scope

        class MockEngine:
            memory_count = 5
            _memories = {
                "mem_000": {
                    "content": "existing 0",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
                "mem_001": {
                    "content": "existing 1",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
                "mem_002": {
                    "content": "new memory 2",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
                "mem_003": {
                    "content": "new memory 3",
                    "tier": "L3",
                    "category": "other",
                    "scope": "private",
                },
                "mem_004": {
                    "content": "new memory 4",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
            }

        count = self.store.backfill(MockEngine())
        assert count == 3  # 5 sqlite - 2 already in lancedb
        assert self.store.count_rows() == 5

    def test_list_memory_ids_returns_all_ids(self):
        """list_memory_ids should expose the vector table truth set."""
        store = LanceDBStore(self._db_path, VectorTestEmbedder())
        store.insert("mem_keep", [0.1] * EMB_DIM, "keep")
        store.insert("mem_other", [0.2] * EMB_DIM, "other")

        assert store.list_memory_ids() == {"mem_keep", "mem_other"}

    def test_sync_with_engine_repairs_same_count_id_mismatch(self):
        """sync_with_engine should handle orphan+missing rows even when counts match."""
        store = LanceDBStore(self._db_path, VectorTestEmbedder())
        store.insert("mem_keep", [0.1] * EMB_DIM, "keep")
        store.insert("mem_orphan", [0.2] * EMB_DIM, "orphan")

        class MockEngine:
            memory_count = 2
            _memories = {
                "mem_keep": {
                    "content": "keep",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
                "mem_missing": {
                    "content": "missing",
                    "tier": "L1",
                    "category": "other",
                    "scope": "global",
                },
            }

        result = store.sync_with_engine(MockEngine())

        assert result == {
            "orphan_deleted": 1,
            "missing_backfilled": 1,
            "missing_skipped": 0,
            "orphan_ids": ["mem_orphan"],
            "missing_ids": ["mem_missing"],
        }
        assert store.list_memory_ids() == {"mem_keep", "mem_missing"}

    def test_sync_reindexes_existing_rows_when_chunking_model_changes(
        self, tmp_path, monkeypatch
    ):
        from plastic_promise.core.memory_index import build_index_material, index_metadata

        monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
        embedder = StructuredVectorEmbedder()
        old_material = build_index_material(
            {"content": "long memory body"},
            model_name="mxbai-embed-large",
        )
        memory = {
            "id": "migrated-memory",
            "content": "long memory body",
            "embedding_text": old_material.vector_text,
            "search_text": old_material.search_text,
            "embedding_hash": old_material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(old_material)},
            "memory_type": "experience",
            "tier": "L1",
            "category": "other",
            "scope": "global",
        }
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / "chunking-migration.lancedb"), embedder)
        store.insert_checked(memory["id"], [0.1] * EMB_DIM, old_material.search_text)

        result = store.sync_with_engine(engine)

        assert result["stale_reindexed"] == 1
        assert result["stale_ids"] == [memory["id"]]
        assert embedder.texts == [old_material.vector_text]
        assert engine._memories[memory["id"]]["metadata_json"]["memory_index"]["model_name"] == (
            embedder.index_model_name
        )

    def test_sync_removes_existing_row_with_invalid_v2_chunk_manifest(
        self, tmp_path, monkeypatch
    ):
        from plastic_promise.core.memory_index import build_index_material, index_metadata

        monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
        embedder = StructuredVectorEmbedder()
        material = build_index_material(
            {"content": "canonical memory with required chunk evidence"},
            model_name=embedder.index_model_name,
        )
        metadata = {"memory_index": index_metadata(material)}
        metadata["memory_index"].pop("chunk_manifest")
        metadata["memory_index"].pop("chunk_manifest_hash")
        memory = {
            "id": "invalid-structured-memory",
            "content": "canonical memory with required chunk evidence",
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": metadata,
            "memory_type": "experience",
            "tier": "L1",
            "category": "other",
            "scope": "global",
        }
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / "invalid-v2.lancedb"), embedder)
        store.insert_checked(memory["id"], [0.1] * EMB_DIM, material.search_text)

        result = store.sync_with_engine(engine)

        assert result["invalid_material_removed"] == 1
        assert result["invalid_material_ids"] == [memory["id"]]
        assert result["diagnostics"] == [
            {"memory_id": memory["id"], "reason": "index_material_invalid_removed"}
        ]
        assert store.list_memory_ids() == set()

    def test_synthesis_chunking_model_migration_is_deferred_without_lancedb_churn(
        self, tmp_path, monkeypatch
    ):
        from plastic_promise.core.memory_index import build_index_material, index_metadata

        embedder = StructuredVectorEmbedder()
        old_material = build_index_material(
            {"content": "governed synthesis body"},
            model_name="mxbai-embed-large",
        )
        memory = {
            "id": "stale-synthesis",
            "content": "governed synthesis body",
            "embedding_text": old_material.vector_text,
            "search_text": old_material.search_text,
            "embedding_hash": old_material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(old_material)},
            "memory_type": "synthesis",
            "tier": "L1",
            "category": "decision",
            "scope": "global",
        }
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / "synthesis-migration.lancedb"), embedder)
        monkeypatch.setattr(store, "_memory_is_index_eligible", lambda *_args: True)
        store.insert_checked(memory["id"], [0.1] * EMB_DIM, old_material.search_text)
        replace_calls = []
        delete_calls = []

        original_replace = store.replace_checked
        original_delete = store.delete_checked

        def record_replace(*args, **kwargs):
            replace_calls.append((args, kwargs))
            return original_replace(*args, **kwargs)

        def record_delete(*args, **kwargs):
            delete_calls.append((args, kwargs))
            return original_delete(*args, **kwargs)

        store.replace_checked = record_replace
        store.delete_checked = record_delete

        assert (
            store._insert_engine_memory(engine, memory["id"], memory, replace_existing=True)
            is False
        )
        assert replace_calls == []
        assert delete_calls == []
        assert embedder.texts == []
        assert store.index_diagnostics == (
            {
                "memory_id": memory["id"],
                "reason": "synthesis_index_material_migration_deferred",
            },
        )
        assert store.list_memory_ids() == {memory["id"]}

    def test_sync_does_not_count_failed_backend_insert_as_backfilled(self, tmp_path, monkeypatch):
        store = LanceDBStore(str(tmp_path / "failed-insert.lancedb"), VectorTestEmbedder())
        engine = RepairEngine(
            {
                "mem-missing": {
                    "id": "mem-missing",
                    "content": "missing canonical memory",
                    "memory_type": "experience",
                }
            }
        )

        def fail_insert(*_args, **_kwargs):
            raise RuntimeError("backend add failed")

        monkeypatch.setattr(store, "insert_checked", fail_insert)

        result = store.sync_with_engine(engine)

        assert result["missing_backfilled"] == 0
        assert result["missing_skipped"] == 1
        assert result["diagnostics"] == [
            {"memory_id": "mem-missing", "reason": "lancedb_insert_failed"}
        ]
        assert store.list_memory_ids() == set()

    def test_sync_does_not_count_failed_backend_delete_as_repaired(self, tmp_path, monkeypatch):
        store = LanceDBStore(str(tmp_path / "failed-delete.lancedb"), VectorTestEmbedder())
        store.insert_checked("mem-orphan", [0.1] * EMB_DIM, "orphan row")
        engine = RepairEngine({})

        def fail_delete(*_args, **_kwargs):
            raise RuntimeError("backend delete failed")

        monkeypatch.setattr(store, "delete_checked", fail_delete)

        result = store.sync_with_engine(engine)

        assert result["orphan_deleted"] == 0
        assert result["diagnostics"] == [
            {"memory_id": "mem-orphan", "reason": "lancedb_delete_failed"}
        ]
        assert store.list_memory_ids() == {"mem-orphan"}

    def test_rebuild_aborts_when_backend_clear_fails(self, tmp_path, monkeypatch):
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / "failed-clear.lancedb"), embedder)
        store.insert_checked("stale-row", [0.1] * EMB_DIM, "stale vector text")
        engine = RepairEngine(
            {
                "stale-row": {
                    "id": "stale-row",
                    "content": "new canonical text",
                    "memory_type": "experience",
                }
            }
        )

        def fail_clear():
            raise RuntimeError("backend clear failed")

        monkeypatch.setattr(store, "clear_all_checked", fail_clear)

        assert store.rebuild_all(engine) == 0
        assert store.index_diagnostics == (
            {"memory_id": "__table__", "reason": "lancedb_clear_failed"},
        )
        assert embedder.texts == []
        row = store._table.search().where("memory_id = 'stale-row'").limit(1).to_list()[0]
        assert row["text"] == "stale vector text"

    @pytest.mark.parametrize("repair", ["sync_with_engine", "rebuild_all", "backfill"])
    def test_repair_reuses_persisted_index_material_byte_for_byte(
        self, tmp_path, monkeypatch, repair
    ):
        from plastic_promise.core.memory_index import (
            SUMMARY_POLICY,
            build_index_material,
            index_metadata,
        )

        raw = "RAW-L2-MUST-NOT-BE-EMBEDDED"
        vector_text = "L0: compact vector text\nL1: - governed summary"
        search_text = "compact search text"
        embedder = RecordingVectorEmbedder()
        material = build_index_material(
            {
                "content": raw,
                "embedding_text": vector_text,
                "search_text": search_text,
            },
            policy=SUMMARY_POLICY,
            model_name=embedder.model_name,
        )
        memory = {
            "id": "compact-memory",
            "content": raw,
            "memory_type": "experience",
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(material)},
            "tier": "L1",
            "category": "fact",
            "scope": "global",
        }
        engine = RepairEngine({"compact-memory": memory})
        store = LanceDBStore(str(tmp_path / f"{repair}.lancedb"), embedder)

        # The environment may choose a different policy for future writes only.
        monkeypatch.delenv("PP_MEMORY_SUMMARY_INDEX", raising=False)
        getattr(store, repair)(engine)

        assert embedder.texts == [vector_text]
        assert raw not in embedder.texts
        row = store._table.search().where("memory_id = 'compact-memory'").limit(1).to_list()[0]
        assert row["text"] == search_text
        assert memory["metadata_json"]["memory_index"]["policy"] == SUMMARY_POLICY

    def test_legacy_fallback_is_explicit_persisted_and_env_independent(self, tmp_path, monkeypatch):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore
        from plastic_promise.core.memory_index import (
            LEGACY_FALLBACK_POLICY,
            build_index_material,
        )

        sqlite_store = _SQLiteMemoryStore(str(tmp_path / "fallback.db"))
        sqlite_store.upsert(
            "legacy-memory",
            {
                "content": "legacy full content",
                "memory_type": "experience",
                "source": "test",
                "embedding_text": "unused compact summary",
                "embedding_hash": "unrelated-old-hash",
                "metadata_json": {
                    "memory_index": {
                        "embedding_hash": "unrelated-old-hash",
                        "summary_index_enabled": False,
                    }
                },
            },
        )
        engine = RepairEngine(dict(sqlite_store.iter_all()), sqlite_store)
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / "fallback.lancedb"), embedder)

        monkeypatch.setenv("PP_MEMORY_SUMMARY_INDEX", "1")
        assert store.sync_with_engine(engine)["missing_backfilled"] == 1

        persisted = sqlite_store.get("legacy-memory")
        assert persisted["embedding_text"] == "legacy full content"
        assert persisted["search_text"] == "legacy full content"
        assert persisted["metadata_json"]["memory_index"]["policy"] == LEGACY_FALLBACK_POLICY
        expected = build_index_material(
            {"content": "legacy full content"},
            policy=LEGACY_FALLBACK_POLICY,
            model_name=embedder.model_name,
        )
        assert persisted["embedding_hash"] == expected.embedding_hash
        assert persisted["embedding_hash"] != "unrelated-old-hash"

        monkeypatch.setenv("PP_MEMORY_SUMMARY_INDEX", "0")
        assert store.rebuild_all(engine) == 1
        assert embedder.texts == ["legacy full content", "legacy full content"]
        row = store._table.search().where("memory_id = 'legacy-memory'").limit(1).to_list()[0]
        assert row["text"] == "legacy full content"

    def test_pre_v2_repair_prepares_exact_document_material_before_persisting(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
        embedder = PreparingVectorEmbedder()
        memory = {
            "id": "pre-v2-memory",
            "content": "legacy full content",
            "memory_type": "experience",
            "tier": "L1",
            "category": "fact",
            "scope": "global",
        }
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / "pre-v2-prepared.lancedb"), embedder)

        result = store.sync_with_engine(engine)

        assert result["missing_backfilled"] == 1
        assert embedder.prepared == ["legacy full content"]
        assert embedder.texts == ["PLAN::legacy full content"]
        assert memory["embedding_text"] == "PLAN::legacy full content"

    def test_partial_compact_material_never_falls_back_to_raw_content(self, tmp_path):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore
        from plastic_promise.core.memory_index import SUMMARY_POLICY

        sqlite_store = _SQLiteMemoryStore(str(tmp_path / "partial-compact.db"))
        sqlite_store.upsert(
            "partial-compact",
            {
                "content": "RAW-L2-DO-NOT-EMBED",
                "memory_type": "experience",
                "embedding_text": "COMPACT-VECTOR",
                "embedding_hash": "legacy-unverified-hash",
                "l0_abstract": "COMPACT-SEARCH",
            },
        )
        engine = RepairEngine(
            {"partial-compact": {"content": "STALE-RUNTIME"}},
            sqlite_store,
        )
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / "partial-compact.lancedb"), embedder)

        assert store.sync_with_engine(engine)["missing_backfilled"] == 1

        persisted = sqlite_store.get("partial-compact")
        assert embedder.texts == ["COMPACT-VECTOR"]
        assert persisted["embedding_text"] == "COMPACT-VECTOR"
        assert persisted["search_text"] == "COMPACT-SEARCH"
        assert persisted["metadata_json"]["memory_index"]["policy"] == SUMMARY_POLICY
        assert persisted["embedding_hash"] != "legacy-unverified-hash"
        row = store._table.search().where("memory_id = 'partial-compact'").limit(1).to_list()[0]
        assert row["text"] == "COMPACT-SEARCH"

    @pytest.mark.parametrize(
        ("kind", "expected_reason"),
        [
            ("missing", "index_material_incomplete"),
            ("hash", "index_material_hash_mismatch"),
            ("model", "index_material_model_mismatch"),
        ],
    )
    def test_repair_skips_invalid_material_with_explicit_diagnostic(
        self, tmp_path, monkeypatch, kind, expected_reason
    ):
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            build_index_material,
            index_metadata,
        )

        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
        embedder = RecordingVectorEmbedder()
        material = build_index_material(
            {
                "domain": "building",
                "category": "fact",
                "l0_abstract": "Persisted compact evidence",
                "l1_summary": "Keep Identifier_X1",
            },
            policy=COMPACT_V2_POLICY,
            model_name=("Other-Model" if kind == "model" else embedder.model_name),
        )
        memory = {
            "id": f"invalid-{kind}",
            "content": "RAW-MUST-NOT-BE-REINTERPRETED",
            "memory_type": "experience",
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(material)},
            "tier": "L1",
            "category": "fact",
            "scope": "global",
        }
        if kind == "missing":
            memory["embedding_text"] = ""
        elif kind == "hash":
            memory["embedding_hash"] = "tampered-hash"
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / f"invalid-{kind}.lancedb"), embedder)

        result = store.sync_with_engine(engine)

        assert result["missing_backfilled"] == 0
        assert result["missing_skipped"] == 1
        assert result["diagnostics"] == [{"memory_id": memory["id"], "reason": expected_reason}]
        assert embedder.texts == []
        assert store.list_memory_ids() == set()

    def test_repair_uses_persisted_compact_material_despite_environment_change(
        self, tmp_path, monkeypatch
    ):
        from plastic_promise.core.memory_index import (
            COMPACT_V2_POLICY,
            build_index_material,
            index_metadata,
        )

        embedder = RecordingVectorEmbedder()
        material = build_index_material(
            {
                "domain": "building",
                "category": "decision",
                "l0_abstract": "SQLite truth",
                "l1_summary": "Use Identifier_X1",
                "content": "RAW-MUST-NOT-BE-USED",
            },
            policy=COMPACT_V2_POLICY,
            model_name=embedder.model_name,
        )
        memory = {
            "id": "persisted-compact-v2",
            "content": "RAW-MUST-NOT-BE-USED",
            "memory_type": "experience",
            "embedding_text": material.vector_text,
            "search_text": material.search_text,
            "embedding_hash": material.embedding_hash,
            "metadata_json": {"memory_index": index_metadata(material)},
            "tier": "L1",
            "category": "decision",
            "scope": "global",
        }
        engine = RepairEngine({memory["id"]: memory})
        store = LanceDBStore(str(tmp_path / "persisted-compact-v2.lancedb"), embedder)
        monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "legacy")

        assert store.backfill(engine) == 1
        assert embedder.texts == [material.vector_text]
        assert "RAW-MUST-NOT-BE-USED" not in embedder.texts
        row = (
            store._table.search().where("memory_id = 'persisted-compact-v2'").limit(1).to_list()[0]
        )
        assert row["text"] == material.search_text

    @pytest.mark.parametrize("repair", ["sync_with_engine", "rebuild_all", "backfill"])
    def test_repair_uses_canonical_sqlite_material_and_refreshes_runtime(self, tmp_path, repair):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore
        from plastic_promise.core.memory_index import (
            SUMMARY_POLICY,
            build_index_material,
            index_metadata,
        )

        embedder = RecordingVectorEmbedder()
        canonical = build_index_material(
            {
                "content": "canonical display",
                "embedding_text": "CANONICAL VECTOR",
                "search_text": "CANONICAL SEARCH",
            },
            policy=SUMMARY_POLICY,
            model_name=embedder.model_name,
        )
        sqlite_store = _SQLiteMemoryStore(str(tmp_path / "canonical.db"))
        sqlite_store.upsert(
            "canonical-memory",
            {
                "content": "canonical display",
                "memory_type": "experience",
                "embedding_text": canonical.vector_text,
                "search_text": canonical.search_text,
                "embedding_hash": canonical.embedding_hash,
                "metadata_json": {"memory_index": index_metadata(canonical)},
            },
        )
        engine = RepairEngine(
            {
                "canonical-memory": {
                    "id": "canonical-memory",
                    "content": "STALE RAW",
                    "memory_type": "experience",
                    "embedding_text": "STALE VECTOR",
                    "search_text": "STALE SEARCH",
                    "embedding_hash": "stale-hash",
                    "metadata_json": {
                        "memory_index": {
                            "policy": SUMMARY_POLICY,
                            "embedding_hash": "stale-hash",
                        }
                    },
                }
            },
            sqlite_store,
        )
        store = LanceDBStore(str(tmp_path / f"canonical-{repair}.lancedb"), embedder)

        result = getattr(store, repair)(engine)
        if repair == "sync_with_engine":
            assert result["missing_backfilled"] == 1
        else:
            assert result == 1

        assert embedder.texts == ["CANONICAL VECTOR"]
        row = store._table.search().where("memory_id = 'canonical-memory'").limit(1).to_list()[0]
        assert row["text"] == "CANONICAL SEARCH"
        assert engine._memories["canonical-memory"]["embedding_text"] == "CANONICAL VECTOR"
        assert sqlite_store.get("canonical-memory")["content"] == "canonical display"

    def test_backfill_compares_eligible_ids_not_raw_row_counts(self, tmp_path, monkeypatch):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
        sqlite_store = _SQLiteMemoryStore(str(tmp_path / "eligible.db"))
        sqlite_store.upsert(
            "ordinary-missing",
            {"content": "ordinary should be indexed", "memory_type": "experience"},
        )
        sqlite_store.upsert(
            "draft-existing",
            {"content": "uncontrolled synthesis", "memory_type": "synthesis"},
        )
        engine = RepairEngine(dict(sqlite_store.iter_all()), sqlite_store)
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / "eligible.lancedb"), embedder)
        store.insert("draft-existing", [0.1] * EMB_DIM, "must not mask ordinary")
        store.insert("orphan", [0.1] * EMB_DIM, "count padding")

        assert store.count_rows() == engine.memory_count
        assert store.backfill(engine) == 1
        assert "ordinary-missing" in store.list_memory_ids()

        sync_result = store.sync_with_engine(engine)
        assert "draft-existing" in sync_result["orphan_ids"]
        assert store.list_memory_ids() == {"ordinary-missing"}

    @pytest.mark.parametrize("repair", ["sync_with_engine", "rebuild_all", "backfill"])
    def test_control_associated_type_drift_is_never_indexed(self, tmp_path, monkeypatch, repair):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
        sqlite_store = _SQLiteMemoryStore(str(tmp_path / f"controlled-{repair}.db"))
        sqlite_store.upsert(
            "controlled-type-drift",
            {
                "id": "controlled-type-drift",
                "content": "A governed synthesis row drifted to an ordinary type.",
                "memory_type": "experience",
            },
        )
        sqlite_store._conn.execute(
            "INSERT INTO synthesis_artifacts "
            "(memory_id, synthesis_key, status, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', 'now', 'now')",
            ("controlled-type-drift", "key:controlled-type-drift", "verified"),
        )
        sqlite_store._conn.commit()
        memory = sqlite_store.get("controlled-type-drift")
        engine = RepairEngine({"controlled-type-drift": memory}, sqlite_store)
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / f"controlled-{repair}.lancedb"), embedder)

        assert store._memory_is_index_eligible(engine, "controlled-type-drift", memory) is False
        getattr(store, repair)(engine)

        assert "controlled-type-drift" not in store.list_memory_ids()
        assert embedder.texts == []

    @pytest.mark.parametrize("repair", ["sync_with_engine", "rebuild_all", "backfill"])
    @pytest.mark.parametrize(
        "state",
        ["forgotten", "wrong", "deprecated", "replaced", "conflict"],
    )
    def test_repair_never_reindexes_unavailable_ordinary_memory(
        self,
        tmp_path,
        repair,
        state,
    ):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        memory_id = f"ordinary-{state}"
        sqlite_store = _SQLiteMemoryStore(str(tmp_path / f"{repair}-{state}.db"))
        sqlite_store.upsert(
            memory_id,
            {
                "id": memory_id,
                "content": "Unavailable ordinary memory must stay out of the derived index.",
                "memory_type": "experience",
                "tags": [f"status:{state}"],
                "metadata_json": {
                    "lifecycle_status": state,
                    "quality": {"status": state},
                },
                "decay_multiplier": 0.0,
            },
        )
        memory = sqlite_store.get(memory_id)
        engine = RepairEngine({memory_id: memory}, sqlite_store)
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / f"{repair}-{state}.lancedb"), embedder)
        if repair != "backfill":
            store.insert(memory_id, [0.1] * EMB_DIM, "stale unavailable vector")

        getattr(store, repair)(engine)

        assert memory_id not in store.list_memory_ids()
        assert embedder.texts == []

    def test_sync_keeps_canonically_available_ordinary_status_index_eligible(self, tmp_path):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        sqlite_store = _SQLiteMemoryStore(str(tmp_path / "available-status.db"))
        sqlite_store.upsert(
            "reviewed-task",
            {
                "id": "reviewed-task",
                "content": "Reviewed is an explicitly available ordinary-memory state.",
                "memory_type": "task",
                "tags": ["status:reviewed"],
                "metadata_json": {"quality": {"status": "reviewed"}},
            },
        )
        memory = sqlite_store.get("reviewed-task")
        engine = RepairEngine({"reviewed-task": memory}, sqlite_store)
        embedder = RecordingVectorEmbedder()
        store = LanceDBStore(str(tmp_path / "available-status.lancedb"), embedder)

        result = store.sync_with_engine(engine)

        assert result["missing_backfilled"] == 1
        assert store.list_memory_ids() == {"reviewed-task"}
        assert embedder.texts == ["Reviewed is an explicitly available ordinary-memory state."]

    def test_control_lookup_error_fails_index_eligibility_closed(self, tmp_path):
        class BrokenConnection:
            def execute(self, *_args, **_kwargs):
                raise RuntimeError("control lookup unavailable")

        class BrokenSQLite:
            _conn = BrokenConnection()

        engine = RepairEngine(
            {
                "unknown-governance": {
                    "id": "unknown-governance",
                    "content": "Unknown governance state must fail closed.",
                    "memory_type": "experience",
                }
            },
            BrokenSQLite(),
        )
        store = LanceDBStore(str(tmp_path / "broken-control.lancedb"), RecordingVectorEmbedder())

        assert (
            store._memory_is_index_eligible(
                engine,
                "unknown-governance",
                engine._memories["unknown-governance"],
            )
            is False
        )

    @pytest.mark.parametrize("mutation_point", ["material", "embed"])
    def test_repair_rechecks_canonical_eligibility_before_vector_insert(
        self,
        tmp_path,
        monkeypatch,
        mutation_point,
    ):
        from plastic_promise.core.context_engine import _SQLiteMemoryStore

        sqlite_store = _SQLiteMemoryStore(str(tmp_path / f"recheck-{mutation_point}.db"))
        sqlite_store.upsert(
            "eligibility-race",
            {
                "id": "eligibility-race",
                "content": "ordinary candidate before the indexing race",
                "memory_type": "experience",
            },
        )
        engine = RepairEngine(dict(sqlite_store.iter_all()), sqlite_store)

        def mutate_to_synthesis_orphan():
            current = sqlite_store.get("eligibility-race")
            current["memory_type"] = "synthesis"
            current["source_class"] = "synthesis"
            sqlite_store.upsert("eligibility-race", current)

        class MutatingEmbedder(RecordingVectorEmbedder):
            def embed(self, text):
                if mutation_point == "embed":
                    mutate_to_synthesis_orphan()
                return super().embed(text)

        embedder = MutatingEmbedder()
        store = LanceDBStore(str(tmp_path / f"recheck-{mutation_point}.lancedb"), embedder)
        if mutation_point == "material":
            original_resolve = lancedb_store_module.resolve_index_material
            mutated = False

            def resolve_and_mutate(*args, **kwargs):
                nonlocal mutated
                result = original_resolve(*args, **kwargs)
                if not mutated:
                    mutated = True
                    mutate_to_synthesis_orphan()
                return result

            monkeypatch.setattr(
                lancedb_store_module,
                "resolve_index_material",
                resolve_and_mutate,
            )

        assert store.backfill(engine) == 0
        assert store.list_memory_ids() == set()
        assert sqlite_store.get("eligibility-race")["memory_type"] == "synthesis"
        sqlite_store._conn.close()

    def test_reopen_persists_data(self):
        """Data should persist across LanceDBStore instances on the same path."""
        self._require_vectors()
        vec = [0.3] * EMB_DIM
        self.store.insert("mem_001", vec, "persistent record")
        assert self.store.count_rows() == 1

        # Reopen with a new instance
        store2 = LanceDBStore(self._db_path, self._embedder)
        assert store2.count_rows() == 1
        results = store2.search(vec, k=10)
        assert len(results) == 1
        assert results[0][0] == "mem_001"
        # LanceDB connection is auto-closed on garbage collection

    def test_search_similar_returns_top_k(self):
        """search_similar returns top-k matches sorted by similarity descending."""
        self._require_vectors()
        import random

        # Insert 5 memories with known vectors
        base = [0.5] * EMB_DIM
        for i in range(5):
            vec = [v + random.uniform(-0.01, 0.01) for v in base]
            self.store.insert(f"sim_{i}", vec, f"similar memory {i}")
        # Search with a near-identical vector
        query = [0.51] * EMB_DIM
        results = self.store.search_similar(query, k=3)
        assert len(results) == 3
        # First result should be most similar
        assert results[0][1] >= results[1][1] >= results[2][1]
        # All scores in [0, 1]
        for _mid, score in results:
            assert 0.0 <= score <= 1.0

    def test_search_similar_empty_table(self):
        """search_similar on empty table returns empty list."""
        results = self.store.search_similar([0.5] * EMB_DIM, k=3)
        assert results == []

    def test_check_duplicate_finds_match(self):
        """check_duplicate returns memory_id when similarity >= threshold."""
        self._require_vectors()
        vec = [0.7] * EMB_DIM
        self.store.insert("dup_target", vec, "target text")
        # Search with near-identical vector
        query = [0.71] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result == "dup_target"

    def test_check_duplicate_no_match(self):
        """check_duplicate returns None when no vector is similar enough."""
        self._require_vectors()
        # Uniform vectors are colinear (cosine sim ~1.0). Use orthogonal vector.
        self.store.insert("far_away", [0.1] * EMB_DIM, "distant text")
        query = [1.0] + [0.0] * (EMB_DIM - 1)  # orthogonal to uniform
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result is None

    def test_check_duplicate_skips_self(self):
        """check_duplicate should not match the same memory_id (self-exclusion later in pipeline)."""
        self._require_vectors()
        # This tests the LanceDB layer only — self-exclusion is pipeline's job
        vec = [0.5] * EMB_DIM
        self.store.insert("self_test", vec, "self")
        # A different but very similar vector
        query = [0.51] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        # May match "self_test" — pipeline handles self-exclusion
        # This test just verifies the method doesn't crash
        assert result is not None

    def test_check_duplicate_blocks_unsafe_unindexed_fragment_scan(self, monkeypatch):
        class FragmentedDataset:
            def get_fragments(self):
                return [object(), object(), object()]

        class FragmentedTable:
            def __init__(self):
                self.search_calls = 0

            def count_rows(self):
                return 3

            def list_indices(self):
                return []

            def to_lance(self):
                return FragmentedDataset()

            def search(self, *_args, **_kwargs):
                self.search_calls += 1
                return self

        monkeypatch.setenv("LDB_MAX_UNINDEXED_VECTOR_SCAN_FRAGMENTS", "2")
        table = FragmentedTable()
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._table = table
        store._last_search_diagnostics = []

        assert store.check_duplicate([0.1] * EMB_DIM) is None
        assert table.search_calls == 0
        assert store.consume_search_diagnostics() == [
            {
                "channel": "vector",
                "reason": "lancedb_unindexed_fragment_scan_blocked",
                "fragment_count": "3",
                "limit": "2",
            }
        ]

    def test_vector_index_allows_fragmented_duplicate_scan(self, monkeypatch):
        class FragmentedDataset:
            def get_fragments(self):
                return [object(), object(), object()]

        class VectorIndex:
            columns = ["vector"]
            index_type = "IVF_FLAT"

        class IndexedTable:
            def __init__(self):
                self.search_calls = 0

            def count_rows(self):
                return 3

            def list_indices(self):
                return [VectorIndex()]

            def to_lance(self):
                return FragmentedDataset()

            def search(self, *_args, **_kwargs):
                self.search_calls += 1
                return self

            def metric(self, _metric):
                return self

            def limit(self, _limit):
                return self

            def to_list(self):
                return [{"memory_id": "indexed-target", "_distance": 0.0}]

        monkeypatch.setenv("LDB_MAX_UNINDEXED_VECTOR_SCAN_FRAGMENTS", "2")
        table = IndexedTable()
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._table = table
        store._last_search_diagnostics = []

        assert store.check_duplicate([0.1] * EMB_DIM) == "indexed-target"
        assert table.search_calls == 1

    def test_insert_checked_auto_compacts_small_fragment_backlog(self, monkeypatch):
        class MutableDataset:
            def __init__(self, table):
                self.table = table

            def get_fragments(self):
                return [object()] * self.table.fragment_count

        class MutableTable:
            def __init__(self):
                self.fragment_count = 1
                self.optimize_calls = 0

            def count_rows(self):
                return self.fragment_count

            def list_indices(self):
                return []

            def to_lance(self):
                return MutableDataset(self)

            def search(self, *_args, **_kwargs):
                return self

            def where(self, *_args, **_kwargs):
                return self

            def limit(self, _limit):
                return self

            def to_list(self):
                return []

            def add(self, _rows):
                self.fragment_count += 1

            def optimize(self):
                self.optimize_calls += 1
                self.fragment_count = 1

        monkeypatch.setenv("LDB_AUTO_COMPACT_FRAGMENT_THRESHOLD", "1")
        monkeypatch.setenv("LDB_AUTO_COMPACT_MAX_FRAGMENTS", "8")
        table = MutableTable()
        store = object.__new__(LanceDBStore)
        store._vectors_disabled = False
        store._table = table

        store.insert_checked("compact-me", [0.1] * EMB_DIM, "compact text")

        assert table.optimize_calls == 1
        assert table.fragment_count == 1
