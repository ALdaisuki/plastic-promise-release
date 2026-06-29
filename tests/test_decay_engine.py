"""Tests for WeibullDecayCalculator + AccessReinforcement + composite scoring."""
import datetime
import pytest
from plastic_promise.core.decay_engine import WeibullDecayCalculator, AccessReinforcement
from plastic_promise.memory.soul_memory import MemoryRecord, MemoryWorthCalculator
from plastic_promise.core.constants import DECAY_CONFIG, REINFORCEMENT_CONFIG


class TestWeibullDecay:
    def test_brand_new_memory_decay_1(self):
        w = WeibullDecayCalculator()
        now = datetime.datetime.now().isoformat()
        assert w.compute_decay("L1", now, current_time_str=now) == 1.0

    def test_l1_3day_decay_approx_half(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
        dm = w.compute_decay("L1", old)
        assert 0.4 <= dm <= 0.6, f"expected ~0.5, got {dm:.3f}"

    def test_l3_90day_decay_approx_half(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
        dm = w.compute_decay("L3", old)
        assert 0.4 <= dm <= 0.6, f"expected ~0.5, got {dm:.3f}"

    def test_decay_lower_bound_0_05(self):
        w = WeibullDecayCalculator()
        very_old = "2020-01-01T00:00:00"
        dm = w.compute_decay("L1", very_old)
        assert dm >= 0.05

    def test_unknown_tier_uses_default(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=14)).isoformat()
        dm = w.compute_decay("unknown_tier", old)
        assert 0.4 <= dm <= 0.6

    def test_effective_half_life_overrides_lambda(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
        dm_default = w.compute_decay("L1", old)
        dm_extended = w.compute_decay("L1", old, effective_half_life=90.0)
        assert dm_extended > dm_default  # longer half-life = slower decay


class TestAccessReinforcement:
    def test_auto_recall_returns_zero_boost(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(3, now, 3.0, is_auto_recall=True, current_time_str=now)
        assert score == 0.0
        assert hl == 3.0

    def test_no_access_returns_zero_boost(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(0, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert score == 0.0
        assert hl == 3.0

    def test_active_recall_extends_half_life(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(3, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert hl > 3.0
        assert score > 0.0

    def test_reinforcement_score_normalized_0_to_1(self):
        a = AccessReinforcement()
        assert a.compute_reinforcement_score(3.0, 3.0) == 0.0
        assert a.compute_reinforcement_score(3.0, 9.0) == 1.0

    def test_old_access_is_discounted(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        old_access = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
        _, hl_old = a.compute_boost(3, old_access, 3.0, is_auto_recall=False, current_time_str=now)
        _, hl_new = a.compute_boost(3, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert hl_old < hl_new  # old access is worth less


class TestCompositeScore:
    def test_brand_new_memory(self):
        r = MemoryRecord("test", tier="L1", worth_success=5, worth_failure=1)
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.59, freshness=0.0, reinforcement=0.0 -> ~0.35
        assert 0.30 <= score <= 0.40

    def test_fully_decayed_memory(self):
        r = MemoryRecord("old", tier="L1", worth_success=5, worth_failure=1)
        r.decay_multiplier = 0.05  # almost fully decayed
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.83, freshness=0.95, reinforcement=0.0 -> ~0.74
        assert score > 0.5  # freshness compensates

    def test_heavily_reinforced_memory(self):
        r = MemoryRecord("reinforced", tier="L1", worth_success=5, worth_failure=1)
        r.effective_half_life = 9.0  # max reinforcement
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.83, freshness=0.0, reinforcement=1.0 -> ~0.65
        assert score > 0.5

    def test_graceful_degradation_on_missing_fields(self):
        r = MemoryRecord("no_fields", tier="L1")
        # These fields should not exist on a brand new record without explicit set
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        assert 0.0 <= score <= 1.0  # should not crash
