"""Tests for Task Queue MCP tools — task_enqueue."""
import json
import sqlite3
import os
import asyncio
import pytest
from plastic_promise.core.task_queue_schema import ensure_task_tables
from plastic_promise.mcp.tools.task_queue import (
    handle_task_enqueue, handle_task_claim,
    handle_task_complete, handle_task_verify,
    _generate_task_id,
)


@pytest.fixture
def test_db_path(tmp_path):
    """Create a temp database with task queue tables for isolated testing."""
    db_path = str(tmp_path / "test_plastic.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    conn.close()
    return db_path


def test_generate_task_id():
    tid = _generate_task_id()
    assert tid.startswith("t_")
    assert len(tid) > 4


def test_task_enqueue_basic(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    result = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory",
        "title": "测试委托: 修复重复记忆",
        "to_agent": "pi_fixer",
        "priority": 3,
        "from_agent": "daemon",
        "description": "单元测试创建的委托",
        "source_scan": "test",
    }))

    text = json.loads(result[0].text)
    assert text["status"] == "pending"
    assert text["task_id"].startswith("t_")
    assert text["sse_broadcast"] is False  # No SSE in Phase 1
    assert text["review_required"] is False


def test_task_enqueue_d_rank_rejected(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    result = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory",
        "title": "D级猎人尝试挂委托",
        "to_agent": "pi_fixer",
        "from_agent": "unknown_agent",
        "from_trust_score": 0.20,  # D级
        "priority": 3,
    }))

    text = json.loads(result[0].text)
    assert text["status"] == "rejected"
    assert "降级猎人" in text["reason"]


# ═══════════════════════════════════════════════════════════════
# task_claim tests
# ═══════════════════════════════════════════════════════════════

def test_task_claim_success(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    # First enqueue a task
    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "待揭榜委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]

    # Now claim it
    r2 = asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer",
        "task_id": task_id,
        "trust_score": 0.60,
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert "✅" in data["match"]
    assert data["rank"]["rank"] == "B"


def test_task_claim_rank_mismatch(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "audit_architecture", "title": "A级委托",
        "to_agent": "claude", "priority": 2,  # priority=2 → rank A
    }))
    task_id = json.loads(r[0].text)["task_id"]

    r2 = asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer",
        "task_id": task_id,
        "trust_score": 0.55,  # B级接A级 → 越级
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "⚠️" in data["match"]


def test_task_claim_double_prevented(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "单次委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]

    # First claim succeeds
    asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60,
    }))
    # Second claim must fail (already claimed)
    r2 = asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_reviewer", "task_id": task_id, "trust_score": 0.70,
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "已被揭榜" in data["reason"]


# ═══════════════════════════════════════════════════════════════
# task_complete tests
# ═══════════════════════════════════════════════════════════════

def test_task_complete_creates_verify_subtask(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "可完成委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60,
    }))

    r2 = asyncio.run(handle_task_complete(engine, {
        "task_id": task_id,
        "agent_name": "pi_fixer",
        "result": "修复完成：移除3条重复记忆",
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["status"] == "done"
    assert data["verification_task_id"] is not None  # Auto-created verify task for Claude


def test_task_complete_wrong_agent(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "我的委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60,
    }))

    r2 = asyncio.run(handle_task_complete(engine, {
        "task_id": task_id,
        "agent_name": "pi_builder",  # Different agent!
        "result": "不是我揭的",
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is False


# ═══════════════════════════════════════════════════════════════
# task_verify tests
# ═══════════════════════════════════════════════════════════════

def test_task_verify_accepted_boosts_trust(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "验收测试委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60,
    }))
    asyncio.run(handle_task_complete(engine, {
        "task_id": task_id, "agent_name": "pi_fixer", "result": "done",
    }))

    r2 = asyncio.run(handle_task_verify(engine, {
        "task_id": task_id,
        "verdict": "accepted",
        "verified_by": "claude",
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "verified"
    assert data["trust_adjustment"]["delta"] == 0.02


def test_task_verify_rejected_deducts(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "打回测试委托",
        "to_agent": "pi_fixer", "priority": 3,
    }))
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(handle_task_claim(engine, {
        "agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60,
    }))
    asyncio.run(handle_task_complete(engine, {
        "task_id": task_id, "agent_name": "pi_fixer", "result": "done",
    }))

    r2 = asyncio.run(handle_task_verify(engine, {
        "task_id": task_id,
        "verdict": "rejected",
        "verified_by": "claude",
        "comment": "修复不彻底",
    }))
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "reassigned"
    assert data["trust_adjustment"]["delta"] == -0.03
