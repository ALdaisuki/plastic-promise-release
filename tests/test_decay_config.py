"""Test that DECAY_CONFIG covers all active tiers."""
import pytest
from plastic_promise.core.constants import DECAY_CONFIG


def test_decay_config_has_l2():
    """L2 tier must exist — 146 of 186 memories are L2."""
    assert "L2" in DECAY_CONFIG, (
        "DECAY_CONFIG missing L2 entry. 146 memories fall through to 'default' "
        "instead of getting L2-specific decay parameters."
    )


def test_decay_config_has_all_tiers():
    """Every tier that appears in the memory pool needs a decay config."""
    required = {"L1", "L2", "L3"}
    missing = required - set(DECAY_CONFIG.keys())
    assert not missing, f"DECAY_CONFIG missing tiers: {missing}"


def test_l2_params_sane():
    """L2 half-life should be between L1 (3d) and L3 (90d)."""
    cfg = DECAY_CONFIG.get("L2", {})
    hl = cfg.get("half_life_days", 0)
    assert 3 < hl < 90, f"L2 half_life={hl} not between L1(3) and L3(90)"
    beta = cfg.get("beta", 0)
    assert 0.5 < beta < 2.0, f"L2 beta={beta} out of sane range [0.5, 2.0]"
