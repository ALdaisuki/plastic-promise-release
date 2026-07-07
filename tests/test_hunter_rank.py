"""Tests for Hunter Rank System - trust_to_rank, priority_to_rank, can_claim."""

from plastic_promise.core.hunter_rank import can_claim, priority_to_rank, trust_to_rank


def test_trust_to_rank_s():
    assert trust_to_rank(0.85) == {"rank": "S", "title": "传奇猎人", "icon": "[S]"}


def test_trust_to_rank_a():
    assert trust_to_rank(0.72) == {"rank": "A", "title": "资深猎人", "icon": "[A]"}


def test_trust_to_rank_b():
    assert trust_to_rank(0.55) == {"rank": "B", "title": "正式猎人", "icon": "[B]"}


def test_trust_to_rank_c():
    assert trust_to_rank(0.40) == {"rank": "C", "title": "见习猎人", "icon": "[C]"}


def test_trust_to_rank_d():
    assert trust_to_rank(0.10) == {"rank": "D", "title": "降级猎人", "icon": "[D]"}


def test_trust_to_rank_boundaries():
    # Exact thresholds: S >= 0.80, A >= 0.65
    assert trust_to_rank(0.80)["rank"] == "S"
    assert trust_to_rank(0.799)["rank"] == "A"


def test_priority_to_rank():
    assert priority_to_rank(1) == "S"
    assert priority_to_rank(2) == "A"
    assert priority_to_rank(3) == "B"
    assert priority_to_rank(4) == "C"


def test_can_claim_match():
    ok, msg = can_claim(0.72, 2)
    assert ok is True
    assert "[OK]" in msg


def test_can_claim_overreach():
    ok, msg = can_claim(0.55, 2)
    assert ok is False
    assert "!!!" in msg


def test_can_claim_s_rank_anything():
    ok, msg = can_claim(0.90, 4)
    assert ok is True
