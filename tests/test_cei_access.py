"""Test that CEI can be accessed without internal knowledge."""
import pytest
from plastic_promise.loop.soul_loop import get_cei


def test_cei_accessible():
    """CEI should be readable via get_cei() without .fget hack."""
    cei = get_cei()
    assert isinstance(cei, float), f"CEI should be float, got {type(cei)}"


def test_cei_in_range():
    """CEI should be in [0, 1] range."""
    cei = get_cei()
    assert 0.0 <= cei <= 1.0, f"CEI={cei} out of [0,1] range"


def test_cei_has_default():
    """Before any step-closure, CEI should be the default 0.5."""
    cei = get_cei()
    assert cei == 0.5, f"Default CEI should be 0.5, got {cei}"
