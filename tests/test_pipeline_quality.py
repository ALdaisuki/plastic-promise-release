"""Tests for pipeline quality features — extraction, dedup, QualityGate integration."""

import pytest
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
        with patch('plastic_promise.smart_extractor.extract_memories') as mock_extract:
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
        with patch('plastic_promise.smart_extractor.extract_memories') as mock_extract:
            mock_extract.return_value = []
            result = self.pipeline.store_urgent("   ")
            assert result is None

    def test_store_urgent_extraction_error_fallback(self):
        """extract_memories raises → fall back to raw content, no extracted field."""
        with patch('plastic_promise.smart_extractor.extract_memories') as mock_extract:
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
            "created_at": "2026-06-30T12:00:00",
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
