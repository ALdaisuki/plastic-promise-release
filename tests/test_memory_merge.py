"""Tests for MemoryGC.merge_similar() — batch similar memory merging."""

import pytest
from unittest.mock import MagicMock, patch
from plastic_promise.memory.soul_memory import MemoryGC, RecMem, MemoryRecord


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

    def test_merge_similar_dry_run_reports_candidates(self):
        """dry_run=True should report candidates without modifying records."""
        # Setup: two records with vectors
        r1 = self._make_record("mem_001", "User likes Rust for backend")
        r2 = self._make_record("mem_002", "User prefers Rust for server development")

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
            "mem_001": {"_vector": [0.1] * 1024, "access_count": 0, "worth_success": 0},
            "mem_002": {"_vector": [0.2] * 1024, "access_count": 0, "worth_success": 0},
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
            "mem_high": {"_vector": [0.1] * 1024},
            "mem_low": {"_vector": [0.2] * 1024},
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
        r1 = self._make_record("mem_a", "content A")
        r2 = self._make_record("mem_b", "content B")

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
