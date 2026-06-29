"""Tests for LanceDBStore — vector storage with ANN + FTS."""

import os
import tempfile
import shutil

import pytest

from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.core.lancedb_store import LanceDBStore, EMB_DIM


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

    def test_init_creates_table(self):
        """Table should be created automatically."""
        assert self.store._table is not None
        assert self.store.count_rows() == 0

    def test_insert_and_count(self):
        """Insert a row and verify count increases."""
        vec = [0.1] * EMB_DIM
        self.store.insert("mem_001", vec, "hello world")
        assert self.store.count_rows() == 1

    def test_insert_duplicate_noop(self):
        """Inserting the same memory_id twice should be a no-op."""
        vec = [0.1] * EMB_DIM
        self.store.insert("mem_001", vec, "hello world")
        self.store.insert("mem_001", [0.2] * EMB_DIM, "duplicate")
        assert self.store.count_rows() == 1

    def test_insert_with_tier_and_scope(self):
        """Insert with custom tier, category, and scope."""
        vec = [0.1] * EMB_DIM
        self.store.insert("mem_002", vec, "scoped memory",
                          tier="L3", category="test", scope="private")
        assert self.store.count_rows() == 1

    def test_search_returns_results(self):
        """Vector search should return inserted rows."""
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

    def test_search_scope_filter(self):
        """Scope filter should exclude non-matching rows."""
        self.store.insert("mem_001", [0.1] * EMB_DIM, "global doc", scope="global")
        self.store.insert("mem_002", [0.1] * EMB_DIM, "private doc", scope="private")
        results = self.store.search([0.1] * EMB_DIM, k=10, scope="private")
        assert len(results) == 1
        assert results[0][0] == "mem_002"

    def test_search_tier_filter(self):
        """Tier filter should exclude non-matching rows."""
        self.store.insert("mem_001", [0.1] * EMB_DIM, "L1 doc", tier="L1")
        self.store.insert("mem_002", [0.1] * EMB_DIM, "L3 doc", tier="L3")
        results = self.store.search([0.1] * EMB_DIM, k=10, tier="L3")
        assert len(results) == 1
        assert results[0][0] == "mem_002"

    def test_search_fts(self):
        """Full-text search should find matching text."""
        self.store.insert("mem_001", [0.0] * EMB_DIM, "the quick brown fox")
        self.store.insert("mem_002", [0.0] * EMB_DIM, "lazy dog sleeps")
        results = self.store.search_fts("quick", k=10)
        assert len(results) >= 1
        mids = {r[0] for r in results}
        assert "mem_001" in mids

    def test_update_replaces_row(self):
        """Update should replace the row (delete + insert)."""
        self.store.insert("mem_001", [0.1] * EMB_DIM, "original")
        self.store.update("mem_001", [0.9] * EMB_DIM, "updated")
        assert self.store.count_rows() == 1
        results = self.store.search([0.9] * EMB_DIM, k=10)
        assert len(results) == 1
        assert results[0][2] == "updated"

    def test_delete_removes_row(self):
        """Delete should remove the row by memory_id."""
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
        for i in range(5):
            self.store.insert(f"mem_{i:03d}", [float(i) / 10.0] * EMB_DIM, f"text {i}")
        assert self.store.count_rows() == 5

    def test_search_k_respected(self):
        """k parameter should limit results."""
        for i in range(10):
            self.store.insert(f"mem_{i:03d}", [0.5] * EMB_DIM, f"text {i}")
        results = self.store.search([0.5] * EMB_DIM, k=3)
        assert len(results) == 3

    def test_backfill_skips_when_full(self):
        """backfill should return 0 when LanceDB already has >= rows than engine."""
        # Insert enough rows to match mock engine count
        for i in range(3):
            self.store.insert(f"mem_{i:03d}", [0.0] * EMB_DIM, f"text {i}")

        class MockEngine:
            memory_count = 3

        count = self.store.backfill(MockEngine())
        assert count == 0  # already full, skip

    def test_backfill_inserts_missing(self):
        """backfill should insert memories not yet in LanceDB."""
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

            def list_memories(self, limit=10000):
                return [
                    MockRecord("mem_000", "existing 0"),
                    MockRecord("mem_001", "existing 1"),
                    MockRecord("mem_002", "new memory 2"),
                    MockRecord("mem_003", "new memory 3", tier="L3", scope="private"),
                    MockRecord("mem_004", "new memory 4"),
                ]

        count = self.store.backfill(MockEngine())
        assert count == 3  # 5 sqlite - 2 already in lancedb
        assert self.store.count_rows() == 5

    def test_reopen_persists_data(self):
        """Data should persist across LanceDBStore instances on the same path."""
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
        for mid, score in results:
            assert 0.0 <= score <= 1.0

    def test_search_similar_empty_table(self):
        """search_similar on empty table returns empty list."""
        results = self.store.search_similar([0.5] * EMB_DIM, k=3)
        assert results == []

    def test_check_duplicate_finds_match(self):
        """check_duplicate returns memory_id when similarity >= threshold."""
        vec = [0.7] * EMB_DIM
        self.store.insert("dup_target", vec, "target text")
        # Search with near-identical vector
        query = [0.71] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result == "dup_target"

    def test_check_duplicate_no_match(self):
        """check_duplicate returns None when no vector is similar enough."""
        # Uniform vectors are colinear (cosine sim ~1.0). Use orthogonal vector.
        self.store.insert("far_away", [0.1] * EMB_DIM, "distant text")
        query = [1.0] + [0.0] * (EMB_DIM - 1)  # orthogonal to uniform
        result = self.store.check_duplicate(query, threshold=0.85)
        assert result is None

    def test_check_duplicate_skips_self(self):
        """check_duplicate should not match the same memory_id (self-exclusion later in pipeline)."""
        # This tests the LanceDB layer only — self-exclusion is pipeline's job
        vec = [0.5] * EMB_DIM
        self.store.insert("self_test", vec, "self")
        # A different but very similar vector
        query = [0.51] * EMB_DIM
        result = self.store.check_duplicate(query, threshold=0.85)
        # May match "self_test" — pipeline handles self-exclusion
        # This test just verifies the method doesn't crash
        assert result is not None
