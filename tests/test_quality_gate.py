"""Tests for QualityGate — multi-feature memory entry scoring."""

import pytest
from plastic_promise.core.quality_gate import QualityGate


class TestQualityGate:
    """Full test suite for QualityGate scoring and decision logic."""

    def test_score_perfect_extraction(self):
        """Full extracted data + tags + domain -> high score."""
        gate = QualityGate()
        extracted = {
            "category": "preference",
            "l0_abstract": "User prefers Rust for backend development",
            "l1_summary": "[preference] User prefers Rust because of memory safety and zero-cost abstractions",
            "confidence": 0.9,
        }
        tags = ["cat:preference", "rust", "backend"]
        score = gate.score(extracted=extracted, tags=tags, domain_hint="building")
        # ~0.9 conf *0.25 + ~0.8 relevance *0.25 + 1.0 freshness *0.25 + 1.0 density *0.25 approx 0.9+
        assert score >= 0.75

    def test_score_no_extracted_defaults(self):
        """Missing extracted field -> generous defaults, should pass store threshold."""
        gate = QualityGate()
        score = gate.score(extracted={}, tags=[], domain_hint=None)
        # 0.5*0.25 + 0.5*0.25 + 1.0*0.25 + 0.5*0.25 = 0.625
        assert 0.60 <= score <= 0.65

    def test_score_low_confidence_no_structure(self):
        """Low confidence, no tags, no L0/L1 -> borderline low."""
        gate = QualityGate()
        extracted = {
            "category": "fact",
            "confidence": 0.3,
        }
        tags = []
        score = gate.score(extracted=extracted, tags=tags, domain_hint=None)
        # 0.3*0.25 + 0.5*0.25 + 1.0*0.25 + 0.0*0.25 = 0.45
        assert 0.40 <= score < 0.50

    def test_score_info_density_full_structure(self):
        """L0+L1+L2 all present + category -> max info_density."""
        gate = QualityGate()
        extracted = {
            "category": "event",
            "l0_abstract": "Deployed v2.3.1 to production at 14:30 UTC",
            "l1_summary": "[event] Production deployment of v2.3.1 -- includes memory pipeline fixes and LanceDB backfill",
            "l2_content": "Completed deployment of version 2.3.1 to the production cluster. The release includes three patches: memory pipeline dedup, LanceDB backfill optimization, and dashboard refresh fix. All 47 integration tests passed. Rollback plan verified.",
            "confidence": 0.88,
        }
        tags = ["cat:event", "deployment", "production"]
        score = gate.score(extracted=extracted, tags=tags, domain_hint="building")
        # L0=0.3 + L1=0.3 + L2=0.2 + structure=0.2 = 1.0 info_density
        assert score >= 0.80

    def test_decide_store(self):
        """gate_score >= 0.5 -> 'store'."""
        assert QualityGate.decide(0.55) == "store"
        assert QualityGate.decide(0.50) == "store"
        assert QualityGate.decide(1.0) == "store"

    def test_decide_low_quality(self):
        """gate_score 0.3-0.5 -> 'low_quality'."""
        assert QualityGate.decide(0.30) == "low_quality"
        assert QualityGate.decide(0.49) == "low_quality"
        assert QualityGate.decide(0.35) == "low_quality"

    def test_decide_discard(self):
        """gate_score < 0.3 -> 'discard'."""
        assert QualityGate.decide(0.29) == "discard"
        assert QualityGate.decide(0.0) == "discard"
        assert QualityGate.decide(0.10) == "discard"

    def test_score_edge_case_empty_tags_long_content(self):
        """Long content with no tags but good extraction -> respectable score."""
        gate = QualityGate()
        extracted = {
            "category": "pattern",
            "l0_abstract": "User consistently uses TDD workflow",
            "l1_summary": "[pattern] Across 12 coding sessions, user always writes failing tests first",
            "l2_content": "Observed pattern across 12 consecutive coding sessions: user writes a failing test, runs it to confirm failure, then writes minimal implementation, confirms pass, then refactors -- classic TDD red-green-refactor cycle.",
            "confidence": 0.82,
        }
        score = gate.score(extracted=extracted, tags=[], domain_hint=None)
        # info_density should be 1.0 (full L0/L1/L2 + category), relevance 0.5 (no tags)
        assert 0.65 <= score <= 0.80

    def test_freshness_tier_aware_decay(self):
        """Gap 2 fix: L1 (3d half-life) decays faster than L3 (90d half-life)."""
        gate = QualityGate()
        old_date = "2026-06-20T00:00:00"  # 10 days ago from 2026-06-30
        extracted = {"category": "fact", "confidence": 0.8}

        # L1: 10 days > 3d half-life → significant decay
        score_l1 = gate.score(extracted=extracted, tags=[], domain_hint=None,
                              created_at=old_date, tier="L1")
        # L3: 10 days << 90d half-life → mild decay
        score_l3 = gate.score(extracted=extracted, tags=[], domain_hint=None,
                              created_at=old_date, tier="L3")
        # L3 should score higher (fresher) than L1
        assert score_l3 > score_l1, f"L3({score_l3:.3f}) should be > L1({score_l1:.3f})"

    def test_freshness_unknown_tier_uses_default(self):
        """Gap 2 fix: unknown tier falls back to 'default' decay params."""
        gate = QualityGate()
        old_date = "2026-06-20T00:00:00"
        # Both should work without error
        score_none = gate.score(extracted={"confidence": 0.8}, tags=[],
                                created_at=old_date, tier=None)
        score_garbage = gate.score(extracted={"confidence": 0.8}, tags=[],
                                   created_at=old_date, tier="garbage")
        assert 0.0 <= score_none <= 1.0
        assert 0.0 <= score_garbage <= 1.0
