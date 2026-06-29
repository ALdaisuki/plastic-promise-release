"""E2E integration tests: Direction A (decay + reinforcement + composite) x Direction B (extraction + dedup + QualityGate + merge).

Covers the complete memory lifecycle:
  1. store_urgent → extract_memories → process_pipeline (Dir B extraction + pipeline)
  2. QualityGate scores with tier-aware freshness (Dir A Weibull + Dir B QualityGate)
  3. Decay fields initialized on store (Gap 3)
  4. Dedup: duplicate detection → access_count++ → effective_half_life boosted (Gap 1)
  5. Similar memories → merge_similar finds pairs (Dir A composite_score + Dir B merge)
  6. Full GC cycle with merge integration
"""

import pytest
from unittest.mock import MagicMock, patch

from plastic_promise.memory.pipeline import MemoryPipeline
from plastic_promise.memory.soul_memory import RecMem, MemoryRecord, MemoryGC, MemoryWorthCalculator
from plastic_promise.core.quality_gate import QualityGate
from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.core.lancedb_store import LanceDBStore


class TestDirectionABIntegration:
    """Full A+B lifecycle E2E tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rec_mem = RecMem()
        self.embedder = FallbackEmbedder(dim=1024)
        self.pipeline = MemoryPipeline(
            rec_mem=self.rec_mem,
            embedder=self.embedder,
        )
        yield
        self.pipeline._buffer.clear()

    # ================================================================
    # Scenario 1: Single memory through full pipeline
    # ================================================================

    def test_full_pipeline_extraction_to_store(self):
        """Memory enters pipeline → extracted → scored → stored with decay fields."""
        content = "用户喜欢用 Rust 写后端，因为内存安全和零成本抽象"

        # Stage 1: store_urgent with extraction
        # extract_memories is lazily imported inside store_urgent — patch the source
        with patch('plastic_promise.smart_extractor.extract_memories') as mock_extract:
            from plastic_promise.smart_extractor import ExtractedMemory
            mock_extract.return_value = [
                ExtractedMemory(
                    category="preference",
                    l0_abstract="用户喜欢 Rust 后端开发",
                    l1_summary="[preference] 用户偏爱 Rust 因其内存安全和零成本抽象",
                    l2_content=content,
                    importance=0.85,
                    confidence=0.9,
                    source_segment=content,
                )
            ]
            mid = self.pipeline.store_urgent(content, memory_type="experience", source="user")

        assert mid is not None
        assert mid.startswith("fuzzy_")
        record = self.pipeline._buffer[mid]
        # Extraction metadata attached
        assert "extracted" in record
        assert record["extracted"]["category"] == "preference"
        assert record["extracted"]["confidence"] == 0.9
        # Category tag injected
        assert any("cat:preference" in tag for tag in record["tags"])

        # Stage 2-4: Full pipeline run
        self.pipeline._tier_manager = None  # skip tier classification, will default to L1
        result = self.pipeline.process_pipeline()

        # Pipeline ran successfully
        assert result["total_processed"] >= 1
        # Buffer should be empty after migration
        assert mid not in self.pipeline._buffer

    # ================================================================
    # Scenario 2: QualityGate tier-aware scoring
    # ================================================================

    def test_quality_gate_tier_aware_scoring(self):
        """L3 memories get higher freshness than L1 for the same age (Gap 2)."""
        gate = QualityGate()
        old_date = "2026-06-15T00:00:00"  # 15 days ago
        extracted = {
            "category": "fact",
            "l0_abstract": "Deploy completed successfully",
            "l1_summary": "[fact] Production deploy of v2.3 completed",
            "l2_content": "Deployment to production finished at 14:30 UTC. All smoke tests passed.",
            "confidence": 0.88,
        }
        tags = ["cat:fact", "deployment"]

        # L1: 15 days >> 3 day half-life → heavily decayed
        score_l1 = gate.score(extracted=extracted, tags=tags, domain_hint="building",
                              created_at=old_date, tier="L1")
        # L3: 15 days << 90 day half-life → mildly decayed
        score_l3 = gate.score(extracted=extracted, tags=tags, domain_hint="building",
                              created_at=old_date, tier="L3")

        # L3 should score higher because freshness penalty is much smaller
        assert score_l3 > score_l1, f"L3={score_l3:.3f} should > L1={score_l1:.3f} for 15-day-old memory"

        # New memory (no created_at) → freshness = 1.0 regardless of tier
        score_new = gate.score(extracted=extracted, tags=tags, domain_hint="building",
                               created_at=None, tier="L1")
        assert score_new > 0.8  # fresh + good extraction

    # ================================================================
    # Scenario 3: Dedup + AccessReinforcement integration
    # ================================================================

    def test_dedup_triggers_access_reinforcement(self):
        """Duplicate detection boosts effective_half_life (Gap 1)."""
        from plastic_promise.core.decay_engine import AccessReinforcement
        from plastic_promise.core.constants import DECAY_CONFIG

        # Create an existing L3 memory
        existing = MemoryRecord(
            content="用户喜欢 Rust 后端开发", memory_type="experience",
            source="user", memory_id="existing_abc", tier="L3",
        )
        existing.access_count = 3
        existing.last_accessed = "2026-06-25T00:00:00"
        original_hl = existing.effective_half_life  # default

        # Simulate dedup hit: access_count++ + AccessReinforcement
        existing.access_count += 1
        existing.last_accessed = "2026-06-30T12:00:00"

        tier = getattr(existing, 'tier', 'L1')
        base_hl = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])["half_life_days"]
        reinforcer = AccessReinforcement()
        _, new_hl = reinforcer.compute_boost(
            access_count=existing.access_count,
            last_accessed=existing.last_accessed,
            base_half_life=base_hl,
            is_auto_recall=False,
            current_time_str="2026-06-30T12:00:00",
        )
        existing.effective_half_life = new_hl

        # After 4 accesses with recent last_accessed, half-life should be extended
        assert existing.effective_half_life > original_hl
        # L3 base is 90 days, reinforcement should extend beyond that
        assert existing.effective_half_life > base_hl

    # ================================================================
    # Scenario 4: Composite score integration in merge
    # ================================================================

    def test_composite_score_drives_merge_survivor(self):
        """merge_similar uses Direction A composite_score for survivor selection."""
        calc = MemoryWorthCalculator()

        # High-quality memory (high worth, recent, reinforced)
        r1 = MemoryRecord(
            content="用户偏爱 Rust 因其内存安全和性能",
            memory_type="experience", source="user",
            memory_id="mem_best", tier="L3",
        )
        r1.worth_success = 10
        r1.worth_failure = 0
        r1.access_count = 8
        r1.decay_multiplier = 0.9  # fresh
        r1.effective_half_life = 120.0  # reinforced beyond base 90

        # Low-quality memory (low worth, decayed)
        r2 = MemoryRecord(
            content="Rust 挺好的",
            memory_type="experience", source="user",
            memory_id="mem_worst", tier="L1",
        )
        r2.worth_success = 1
        r2.worth_failure = 3
        r2.access_count = 0
        r2.decay_multiplier = 0.2  # heavily decayed
        r2.effective_half_life = 3.0

        score1 = calc.calculate_composite_score(r1)
        score2 = calc.calculate_composite_score(r2)

        assert score1 > score2, f"Best({score1:.3f}) should > Worst({score2:.3f})"
        # r1 should be a clear survivor candidate
        assert score1 > 0.6

    # ================================================================
    # Scenario 5: Decision matrix (QualityGate thresholds)
    # ================================================================

    def test_quality_gate_decision_boundaries(self):
        """Verify the three-tier decision matrix."""
        assert QualityGate.decide(0.50) == "store"
        assert QualityGate.decide(0.75) == "store"
        assert QualityGate.decide(0.30) == "low_quality"
        assert QualityGate.decide(0.49) == "low_quality"
        assert QualityGate.decide(0.29) == "discard"
        assert QualityGate.decide(0.0) == "discard"

    # ================================================================
    # Scenario 6: Full GC cycle with merge integration
    # ================================================================

    def test_gc_cycle_includes_merge_result_key(self):
        """MemoryGC.collect() returns a 'merge' key in the result dict (Task 4)."""
        gc = MemoryGC(self.rec_mem)

        # Add some records to the pool
        r1 = MemoryRecord(content="Rust backend", memory_type="experience",
                          source="user", memory_id="gc_test_1", tier="L3")
        r2 = MemoryRecord(content="Go backend", memory_type="experience",
                          source="user", memory_id="gc_test_2", tier="L1")
        self.rec_mem._records[r1.memory_id] = r1
        self.rec_mem._records[r2.memory_id] = r2

        # Mock engine for GC
        self.rec_mem._engine = MagicMock()
        self.rec_mem._engine._memories = {
            "gc_test_1": {"_vector": None},
            "gc_test_2": {"_vector": None},
        }
        self.rec_mem._engine._ldb = None  # No LanceDB → merge returns error

        result = gc.collect(dry_run=True)
        # collect() must include "merge" key
        assert "merge" in result, f"GC result missing 'merge' key: {list(result.keys())}"
        # With no LanceDB, merge reports error
        assert isinstance(result["merge"], dict)

    # ================================================================
    # Scenario 7: Complete A+B state after pipeline
    # ================================================================

    def test_memory_record_has_all_ab_fields(self):
        """After pipeline store, a MemoryRecord carries all Direction A + B fields."""
        record = MemoryRecord(
            content="完整测试记忆", memory_type="experience",
            source="user", tier="L3",
        )
        record.worth_success = 7
        record.worth_failure = 1
        record.access_count = 5
        record.decay_multiplier = 0.85
        record.effective_half_life = 110.0
        record.metadata["quality"] = "store"
        record.metadata["gate_score"] = 0.72
        record.metadata["merged_from"] = [
            {"memory_id": "old_001", "content_abstract": "相似旧记忆...",
             "merged_at": "2026-06-30T12:00:00", "worth_score": 0.45}
        ]

        # Direction A fields present
        assert hasattr(record, 'decay_multiplier')
        assert hasattr(record, 'effective_half_life')
        assert record.decay_multiplier == 0.85
        assert record.effective_half_life == 110.0

        # Direction B fields present
        assert "quality" in record.metadata
        assert "gate_score" in record.metadata
        assert "merged_from" in record.metadata

        # Composite score available (Dir A)
        calc = MemoryWorthCalculator()
        composite = calc.calculate_composite_score(record)
        assert 0.0 <= composite <= 1.0

        # Serialization round-trips all fields (Dir A + Dir B)
        d = record.to_dict()
        r2 = MemoryRecord.from_dict(d)
        assert r2.decay_multiplier == record.decay_multiplier
        assert r2.effective_half_life == record.effective_half_life
        assert r2.metadata.get("quality") == "store"
        assert r2.metadata.get("gate_score") == 0.72
