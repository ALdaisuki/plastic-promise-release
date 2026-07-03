"""Test that MemoryRecord gets tier-appropriate half-life."""

from plastic_promise.memory.soul_memory import MemoryRecord


def test_l1_half_life_is_3_days():
    r = MemoryRecord("test1", tier="L1")
    assert r.effective_half_life == 3.0, (
        f"L1 half-life should be 3 days, got {r.effective_half_life}"
    )


def test_l2_half_life_is_7_days():
    r = MemoryRecord("test2", tier="L2")
    assert r.effective_half_life == 7.0, (
        f"L2 half-life should be 7 days, got {r.effective_half_life}"
    )


def test_l3_half_life_is_90_days():
    r = MemoryRecord("test3", tier="L3")
    assert r.effective_half_life == 90.0, (
        f"L3 half-life should be 90 days, got {r.effective_half_life}"
    )


def test_explicit_half_life_override():
    """Explicit half_life parameter should override tier default."""
    r = MemoryRecord("test4", tier="L1", effective_half_life=42.0)
    assert r.effective_half_life == 42.0, "Explicit override should be respected"


def test_category_round_trip_for_pipeline_store():
    """MemoryRecord accepts category passed by MemoryPipeline and preserves it."""
    r = MemoryRecord("test5", category="fact")

    assert r.category == "fact"
    assert r.to_dict()["category"] == "fact"
    assert MemoryRecord.from_dict(r.to_dict()).category == "fact"
