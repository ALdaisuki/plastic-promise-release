"""QualityGate — multi-feature entry scoring for memory pipeline gating.

Four dimensions x equal weight (0.25 each):
  confidence      — from smart_extractor classification confidence
  relevance       — domain/tag matching via DomainManager
  freshness       — time-decay via Direction A Weibull logic
  info_density    — L0/L1/L2 completeness + structural metadata

Decision matrix:
  gate_score >= 0.5  -> store
  0.3 <= score < 0.5 -> store with low_quality tag
  score < 0.3       -> discard
"""

from plastic_promise.core.constants import (
    QUALITY_GATE_THRESHOLD_LOW,
    QUALITY_GATE_THRESHOLD_STORE,
    QUALITY_GATE_WEIGHTS,
)


class QualityGate:
    """Composite gating scorer for memory entry quality."""

    WEIGHTS = QUALITY_GATE_WEIGHTS
    THRESHOLD_STORE = QUALITY_GATE_THRESHOLD_STORE
    THRESHOLD_LOW = QUALITY_GATE_THRESHOLD_LOW

    def score(
        self,
        extracted: dict | None = None,
        tags: list[str] | None = None,
        domain_hint: str | None = None,
        created_at: str | None = None,
        tier: str | None = None,
    ) -> float:
        """Compute composite gate_score from four dimensions.

        Args:
            extracted: dict from smart_extractor with keys:
                category, l0_abstract, l1_summary, l2_content, confidence
            tags: semantic tags from pipeline
            domain_hint: domain assigned during classified stage
            created_at: ISO timestamp for freshness calculation (None = now)
            tier: memory tier (L1/L3) for tier-aware freshness decay (None = default)

        Returns:
            float in [0.0, 1.0] — weighted sum of four dimensions.
        """
        extracted = extracted or {}
        tags = tags or []

        confidence = self._compute_confidence(extracted)
        relevance = self._compute_relevance(tags, domain_hint)
        freshness = self._compute_freshness(created_at, tier)
        info_density = self._compute_info_density(extracted, tags)

        return (
            confidence * self.WEIGHTS["confidence"]
            + relevance * self.WEIGHTS["relevance"]
            + freshness * self.WEIGHTS["freshness"]
            + info_density * self.WEIGHTS["info_density"]
        )

    @staticmethod
    def decide(gate_score: float) -> str:
        """Map gate_score to action: 'store' | 'low_quality' | 'discard'."""
        if gate_score >= QUALITY_GATE_THRESHOLD_STORE:
            return "store"
        elif gate_score >= QUALITY_GATE_THRESHOLD_LOW:
            return "low_quality"
        else:
            return "discard"

    # ---- Private dimension calculators ----

    @staticmethod
    def _compute_confidence(extracted: dict) -> float:
        """Confidence from smart_extractor, default 0.5."""
        return extracted.get("confidence", 0.5)

    @staticmethod
    def _compute_relevance(tags: list[str], domain_hint: str | None) -> float:
        """Relevance based on tag-to-domain matching.

        With domain_hint: ratio of matched tags x 1.5, capped at 1.0.
        Without domain_hint: neutral 0.5.
        """
        if not domain_hint or not tags:
            return 0.5
        try:
            from plastic_promise.core.domain_manager import PREDEFINED_DOMAINS

            domain_config = PREDEFINED_DOMAINS.get(domain_hint, {})
            domain_tags = domain_config.get("tags", set())
            if not domain_tags:
                return 0.5
            matched = sum(1 for tag in tags if tag in domain_tags)
            ratio = matched / max(len(tags), 1)
            return max(0.5, min(1.0, ratio * 1.5))
        except Exception:
            return 0.5

    @staticmethod
    def _compute_freshness(created_at: str | None = None, tier: str | None = None) -> float:
        """Time-decay freshness via Direction A Weibull engine.

        Delegates to WeibullDecayCalculator for consistency with composite_score.
        New memories (created_at=None) → 1.0.
        Uses memory's actual tier when available, falls back to "default".
        """
        if created_at is None:
            return 1.0
        try:
            from plastic_promise.core.decay_engine import WeibullDecayCalculator

            wdc = WeibullDecayCalculator()
            effective_tier = tier if tier in ("L1", "L3") else "default"
            decay = wdc.compute_decay(effective_tier, created_at)
            return decay
        except Exception:
            return 1.0

    @staticmethod
    def _compute_info_density(extracted: dict, tags: list[str]) -> float:
        """Information density from L0/L1/L2 completeness + structure.

        L0 score (0.3): l0_abstract present and len > 10
        L1 score (0.3): l1_summary present and len > 20
        L2 score (0.2): l2_content present and len > 50
        Structure score (0.2): has category AND tags

        Returns 0.5 when extracted is empty (generous default for direct writes).
        """
        if not extracted:
            return 0.5

        l0_score = (
            0.3
            if extracted.get("l0_abstract") and len(extracted.get("l0_abstract", "")) > 10
            else 0.0
        )
        l1_score = (
            0.3
            if extracted.get("l1_summary") and len(extracted.get("l1_summary", "")) > 20
            else 0.0
        )
        l2_score = (
            0.2
            if extracted.get("l2_content") and len(extracted.get("l2_content", "")) > 50
            else 0.0
        )
        structure_score = 0.2 if extracted.get("category") and tags else 0.0

        return l0_score + l1_score + l2_score + structure_score
