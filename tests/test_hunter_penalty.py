"""Tests for HunterPenaltyEngine — pure-function penalty computation and integration."""

import pytest
import sqlite3
from plastic_promise.core.task_queue_schema import ensure_task_tables
from plastic_promise.core.hunter_penalty import HunterPenaltyEngine


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    ensure_task_tables(conn)
    return conn


def test_penalty_timeout():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "timeout", 1)
    assert result["base_penalty"] == -0.01
    assert result["upgrade_triggered"] is False


def test_penalty_timeout_upgrade_on_third():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "timeout", 3)
    assert result["base_penalty"] == -0.01
    assert result["upgrade_triggered"] is True
    assert result["upgrade_penalty"] == -0.03


def test_penalty_abandoned():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "abandoned", 5)
    assert result["upgrade_triggered"] is True
    assert result["action"] == "demote_to_D"


def test_penalty_overreach():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_builder", "overreach", 1)
    assert result["action"] == "lock_rank_30d"
