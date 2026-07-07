"""Tests for pipeline quality features — extraction, dedup, QualityGate integration."""

import pytest
import datetime
from unittest.mock import MagicMock, patch
from plastic_promise.memory.pipeline import MemoryPipeline
from plastic_promise.memory.soul_memory import MemoryRecord, RecMem
from plastic_promise.core.embedder import FallbackEmbedder


class TestPipelineQuality:
    """Integration tests for quality pipeline features."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create pipeline with mocked dependencies."""
        self.rec_mem = MagicMock(spec=RecMem)
        self.rec_mem._records = {}
        self.embedder = FallbackEmbedder(dim=1024)
        # Mock LanceDB for dedup
        self.lancedb = MagicMock()
        self.lancedb.check_duplicate.return_value = None  # no dup by default
        self.pipeline = MemoryPipeline(
            rec_mem=self.rec_mem,
            embedder=self.embedder,
        )
        self.pipeline._lancedb = self.lancedb
        yield
        # Clean up buffer
        self.pipeline._buffer.clear()

    def test_store_urgent_extracts_memories(self):
        """store_urgent calls extract_memories and stores extracted fields."""
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            from plastic_promise.smart_extractor import ExtractedMemory

            mock_extract.return_value = [
                ExtractedMemory(
                    category="preference",
                    l0_abstract="User likes Rust",
                    l1_summary="[preference] User prefers Rust for backend",
                    l2_content="User likes Rust for backend development",
                    importance=0.8,
                    confidence=0.9,
                    source_segment="User likes Rust for backend development",
                )
            ]
            mid = self.pipeline.store_urgent("User likes Rust for backend development")
            assert isinstance(mid, str)
            assert mid.startswith("fuzzy_")
            # Buffer record should have extracted field
            record = self.pipeline._buffer[mid]
            assert record["stage"] == "raw"
            assert "extracted" in record
            assert record["extracted"]["category"] == "preference"
            assert record["extracted"]["confidence"] == 0.9
            # Tags should include cat:preference
            assert any("cat:preference" in tag for tag in record["tags"])

    def test_store_urgent_no_extraction_returns_none(self):
        """extract_memories returns empty and content is whitespace → returns None."""
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            mock_extract.return_value = []
            result = self.pipeline.store_urgent("   ")
            assert result is None

    def test_store_urgent_extraction_error_fallback(self):
        """extract_memories raises → fall back to raw content, no extracted field."""
        with patch("plastic_promise.smart_extractor.extract_memories") as mock_extract:
            mock_extract.side_effect = RuntimeError("Ollama not running")
            mid = self.pipeline.store_urgent("Important memory about deployment")
            assert isinstance(mid, str)
            record = self.pipeline._buffer[mid]
            assert "extracted" not in record
            assert record["content"] == "Important memory about deployment"

    def test_migrate_skips_duplicate(self):
        """When check_duplicate returns a match, buffer entry is removed without store."""
        self.lancedb.check_duplicate.return_value = "existing_001"

        # Manually create a buffer entry at embedded stage with a vector
        mid = "fuzzy_testdup"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "duplicate content",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": "uncategorized",
            "vector": [0.5] * 1024,
            "extracted": {"category": "fact", "confidence": 0.8},
            "entity_ids": [],
            "created_at": datetime.datetime.now().isoformat(),
        }
        # Mock engine internals
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {"existing_001": {}}
        self.pipeline.rec_mem._engine._sqlite = None

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry should be removed
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()

    def test_migrate_discards_low_quality(self):
        """QualityGate score < 0.3 → buffer entry discarded."""
        # Create entry with intentionally terrible extraction data
        mid = "fuzzy_testlow"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "ok",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": None,
            "vector": [0.5] * 1024,
            "extracted": {
                # No category, no L0/L1/L2 — minimal info density
                "confidence": 0.0,  # zero confidence
            },
            "entity_ids": [],
            "created_at": "2026-06-17T12:00:00",  # 13 days old → freshness < 0.3
        }
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {}

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry should be removed (discarded)
        assert mid not in self.pipeline._buffer
        # rec_mem.store should NOT have been called
        self.rec_mem.store.assert_not_called()

    def test_migrate_dedup_updates_effective_half_life(self):
        """Gap 1 fix: Dedup hit recomputes effective_half_life via AccessReinforcement."""
        self.lancedb.check_duplicate.return_value = "existing_002"

        mid = "fuzzy_testboost"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "reinforced duplicate",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": [],
            "domain": "uncategorized",
            "vector": [0.5] * 1024,
            "entity_ids": [],
            "extracted": {"category": "fact", "confidence": 0.8},
            "created_at": datetime.datetime.now().isoformat(),
        }

        # Create a Python-side record with known tier and baseline half-life
        from plastic_promise.memory.soul_memory import MemoryRecord

        py_rec = MemoryRecord(
            content="existing content",
            memory_type="experience",
            source="user",
            memory_id="existing_002",
            tier="L3",
        )
        py_rec.access_count = 2
        py_rec.last_accessed = "2026-06-25T00:00:00"
        original_hl = py_rec.effective_half_life  # should be default 90.0 for L3

        self.pipeline.rec_mem._records["existing_002"] = py_rec
        self.pipeline.rec_mem._engine = MagicMock()
        self.pipeline.rec_mem._engine._memories = {
            "existing_002": {
                "access_count": 2,
                "worth_success": 1,
                "last_accessed": "2026-06-25T00:00:00",
            }
        }
        self.pipeline.rec_mem._engine._sqlite = None

        with patch("plastic_promise.memory.pipeline.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = __import__("datetime").datetime.fromisoformat(
                "2026-06-30T12:00:00"
            )
            mock_dt.datetime.now.isoformat = lambda: "2026-06-30T12:00:00"
            self.pipeline._process_embedded_to_migrate()

        # Buffer entry removed (dedup skip)
        assert mid not in self.pipeline._buffer
        # access_count incremented
        assert py_rec.access_count == 3
        # effective_half_life should be recomputed (boosted from access + recency)
        assert py_rec.effective_half_life != original_hl

    def test_migrate_store_initializes_decay_fields(self):
        """Gap 3 fix: After RecMem.store(), decay_multiplier and effective_half_life are set."""
        self.lancedb.check_duplicate.return_value = None  # no dedup

        mid = "fuzzy_testdecayinit"
        self.pipeline._buffer[mid] = {
            "memory_id": mid,
            "content": "memory that needs decay init",
            "memory_type": "experience",
            "source": "user",
            "stage": "embedded",
            "tags": ["test"],
            "domain": "building",
            "tier": "L3",
            "vector": [0.5] * 1024,
            "entity_ids": [],
            "extracted": {
                "category": "fact",
                "confidence": 0.85,
                "l0_abstract": "Test memory for decay initialization",
                "l1_summary": "[fact] Test memory should get decay fields set",
                "l2_content": "A sufficiently long content string that provides enough information density to pass the quality gate threshold comfortably.",
            },
            "created_at": datetime.datetime.now().isoformat(),
        }

        # Mock RecMem.store to return a real-like record and track it
        real_store = self.rec_mem.store
        stored_records = {}

        def mock_store(**kwargs):
            from plastic_promise.memory.soul_memory import MemoryRecord

            mr = MemoryRecord(**kwargs)
            mr.memory_id = "stored_decay_init"
            stored_records[mr.memory_id] = mr
            self.pipeline.rec_mem._records[mr.memory_id] = mr
            return mr

        self.rec_mem.store = mock_store
        self.pipeline.rec_mem._engine = MagicMock()
        # RecMem.store() normally registers to engine — our mock bypasses that,
        # so pre-populate the engine dict with the expected ID
        self.pipeline.rec_mem._engine._memories = {"stored_decay_init": {}}
        self.pipeline.rec_mem._engine._sqlite = None

        self.pipeline._process_embedded_to_migrate()

        # Buffer entry removed (successful store)
        assert mid not in self.pipeline._buffer
        # The stored record should have decay_multiplier set (not default 1.0 for old dates)
        stored = stored_records.get("stored_decay_init")
        assert stored is not None
        # For a just-created memory, decay_multiplier should be close to 1.0
        assert stored.decay_multiplier > 0.9
        # effective_half_life should be the L3 base (90 days), not the default 3.0
        assert stored.effective_half_life > 3.0
