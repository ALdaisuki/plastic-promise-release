"""Memory decay engine — Weibull stretched-exponential decay + access reinforcement.

WeibullDecayCalculator: time-based decay with per-tier beta and half-life.
AccessReinforcement: spaced-repetition half-life extension on active recall.

Formulas adapted from memory-lancedb-pro's decay-engine.ts and
access-tracker.ts, reimplemented in Python for Plastic Promise.
"""
import math
import datetime
import logging
from typing import Optional

logger = logging.getLogger("plastic-promise.decay")


class WeibullDecayCalculator:
    """Compute Weibull stretched-exponential decay for memory records.

    Formula: raw_decay = exp(-lambda x days_since_created^beta)
             lambda = ln(2) / half_life_days^beta    (so decay = 0.5 at t=half_life)
             decay_multiplier = clamp(raw_decay, 0.05, 1.0)

    Per-tier configuration controls decay speed:
      L1 (working):  beta=1.5, half-life=3d  -> super-exponential, fast fade
      L3 (long-term): beta=0.7, half-life=90d -> sub-exponential, slow fade
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        from plastic_promise.core.constants import DECAY_CONFIG
        self._config = config or DECAY_CONFIG

    def _get_params(self, tier: str) -> tuple[float, float]:
        """Return (beta, half_life_days) for a tier, defaulting if unknown."""
        cfg = self._config.get(tier, self._config["default"])
        return cfg["beta"], cfg["half_life_days"]

    def _days_since(self, created_at: str, current_time_str: str) -> float:
        """Compute fractional days between two ISO timestamps."""
        try:
            created = datetime.datetime.fromisoformat(created_at)
            current = datetime.datetime.fromisoformat(current_time_str)
            return (current - created).total_seconds() / 86400.0
        except Exception:
            return 0.0

    def compute_decay(self, tier: str, created_at: str,
                      effective_half_life: Optional[float] = None,
                      current_time_str: Optional[str] = None) -> float:
        """Compute decay_multiplier for a single memory.

        Args:
            tier: Memory tier (L1/L3).
            created_at: ISO timestamp of memory creation.
            effective_half_life: Optional override for half-life (from access
                reinforcement). When provided, lambda is recomputed from it.
            current_time_str: ISO timestamp for "now". Defaults to now().

        Returns:
            decay_multiplier in [0.05, 1.0]. 1.0 = brand new, 0.05 = fully decayed.
        """
        beta, half_life = self._get_params(tier)
        if effective_half_life is not None and effective_half_life > 0:
            half_life = effective_half_life
        # lambda = ln(2) / half_life^beta  ensures decay = 0.5 at t=half_life
        lam = math.log(2) / (half_life ** beta)

        now = current_time_str or datetime.datetime.now().isoformat()
        days = self._days_since(created_at, now)
        if days <= 0:
            return 1.0

        raw = math.exp(-lam * (days ** beta))
        return max(0.05, min(1.0, raw))

    def evaluate_all(self, records: list, current_time_str: Optional[str] = None
                     ) -> list[tuple[str, float]]:
        """Batch-evaluate decay for multiple MemoryRecord objects.

        Args:
            records: List of MemoryRecord objects (must have .memory_id, .tier,
                     .created_at, .effective_half_life attributes).
            current_time_str: ISO timestamp for "now". Defaults to now().

        Returns:
            List of (memory_id, decay_multiplier) tuples for all records.
        """
        now = current_time_str or datetime.datetime.now().isoformat()
        results = []
        for r in records:
            try:
                dm = self.compute_decay(
                    tier=getattr(r, 'tier', 'L1'),
                    created_at=getattr(r, 'created_at', now),
                    effective_half_life=getattr(r, 'effective_half_life', None),
                    current_time_str=now,
                )
                results.append((r.memory_id, dm))
            except Exception as e:
                logger.warning("Decay eval failed for %s: %s", getattr(r, 'memory_id', '?'), e)
                results.append((getattr(r, 'memory_id', ''), 1.0))
        return results
