"""Test that Weibull decay is actually applied to memories."""

import datetime
from plastic_promise.core.decay_engine import WeibullDecayCalculator
from plastic_promise.memory.soul_memory import MemoryRecord


def test_decay_applied_to_old_memory():
    """A 5-day-old L1 memory should have decay < 0.5 (half-life=3d)."""
    r = MemoryRecord("test_decay", "old content", tier="L1")
    r.created_at = (datetime.datetime.now() - datetime.timedelta(days=5)).isoformat()
    r.effective_half_life = 3.0

    calc = WeibullDecayCalculator()
    dm = calc.compute_decay("L1", r.created_at, effective_half_life=3.0)

    assert dm < 0.5, f"5-day-old L1 memory (half-life=3d) should have decay < 0.5, got {dm:.4f}"


def test_l2_decay_slower_than_l1():
    """L2 (half-life=7d) should decay slower than L1 (3d) at same age."""
    calc = WeibullDecayCalculator()
    age = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()

    dm_l1 = calc.compute_decay("L1", age, effective_half_life=3.0)
    dm_l2 = calc.compute_decay("L2", age, effective_half_life=7.0)

    assert dm_l2 > dm_l1, (
        f"L2 decay={dm_l2:.4f} should be > L1 decay={dm_l1:.4f} at same age (3 days)"
    )


def test_l3_decay_minimal():
    """L3 (half-life=90d) at 3 days should barely decay."""
    calc = WeibullDecayCalculator()
    age = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()

    dm_l3 = calc.compute_decay("L3", age, effective_half_life=90.0)

    assert dm_l3 > 0.9, (
        f"L3 memory at 3 days should have decay > 0.9 (half-life=90d), got {dm_l3:.4f}"
    )
