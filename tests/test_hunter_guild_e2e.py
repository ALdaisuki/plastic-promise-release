"""End-to-end integration test for the Hunter Guild dispatch system.

Exercises the full lifecycle:
  daemon discovers → enqueues → hunter browses inbox → claims →
  heartbeat + completes → verification sub-task auto-created →
  Claude verifies (accept) → trust boost → task off active board.
"""

import json
import asyncio
import pytest
from plastic_promise.mcp.tools.task_queue import (
    handle_task_enqueue,
    handle_task_claim,
    handle_task_complete,
    handle_task_verify,
    handle_task_inbox,
    handle_task_heartbeat,
)


@pytest.fixture
def e2e_db_path(tmp_path):
    """Create a temp database with task queue tables for isolated E2E testing."""
    from plastic_promise.core.task_queue_schema import ensure_task_tables
    import sqlite3

    db_path = str(tmp_path / "test_plastic.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    conn.close()
    return db_path


def test_full_hunter_guild_lifecycle(e2e_db_path, monkeypatch):
    """End-to-end: daemon discovers → enqueues → hunter claims → completes → verified."""
    monkeypatch.setenv("PLASTIC_DB_PATH", e2e_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    # ── Step 1: Daemon discovers a memory issue and enqueues ──────
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "task_type": "fix_memory",
                "title": "修复重复记忆集群 #DUP_042",
                "to_agent": "pi_fixer",
                "priority": 3,
                "from_agent": "daemon",
                "source_scan": "scan_duplicate_clusters",
                "description": "发现3条完全重复的记忆，保留worth最高的一条",
                "payload": {"memory_ids": ["m_001", "m_002", "m_003"]},
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["status"] == "pending"
    task_id = data["task_id"]
    assert task_id.startswith("t_")

    # ── Step 2: pi_fixer checks inbox and sees the task ────────────
    r = asyncio.run(
        handle_task_inbox(
            engine,
            {
                "agent_name": "pi_fixer",
                "trust_score": 0.60,
                "filter_status": "pending",
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["rank"]["rank"] == "B"
    tasks = [t for t in data["tasks"] if t["id"] == task_id]
    assert len(tasks) == 1
    assert tasks[0]["can_claim"] is True

    # ── Step 3: pi_fixer claims the task ──────────────────────────
    r = asyncio.run(
        handle_task_claim(
            engine,
            {
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["success"] is True

    # ── Step 4: Another hunter tries to claim the same task — blocked
    r = asyncio.run(
        handle_task_claim(
            engine,
            {
                "agent_name": "pi_reviewer",
                "task_id": task_id,
                "trust_score": 0.70,
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["success"] is False
    assert "已被揭榜" in data.get("reason", "") or "揭榜失败" in data.get("reason", "")

    # ── Step 5: pi_fixer sends heartbeat, then completes ──────────
    r = asyncio.run(
        handle_task_heartbeat(
            engine,
            {
                "task_id": task_id,
                "agent_name": "pi_fixer",
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["success"] is True
    assert data["overdue"] is False

    r = asyncio.run(
        handle_task_complete(
            engine,
            {
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "已清理2条重复记忆，保留 m_001 (worth=0.78)",
                "artifacts": ["memory_id:m_001"],
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["success"] is True
    assert data["status"] == "done"
    verify_task_id = data["verification_task_id"]
    assert verify_task_id is not None

    # ── Step 6: Claude verifies — accept ──────────────────────────
    r = asyncio.run(
        handle_task_verify(
            engine,
            {
                "task_id": task_id,
                "verdict": "accepted",
                "verified_by": "claude",
                "comment": "清理正确，LGTM",
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["success"] is True
    assert data["new_status"] == "verified"
    assert data["trust_adjustment"]["delta"] == 0.02

    # ── Step 7: Verified task is off the active board ─────────────
    r = asyncio.run(
        handle_task_inbox(
            engine,
            {
                "agent_name": "pi_fixer",
                "trust_score": 0.62,
                "filter_status": "my_active",
            },
        )
    )
    data = json.loads(r[0].text)
    my_ids = [t["id"] for t in data["tasks"]]
    assert task_id not in my_ids, "Verified task must not appear in my_active"
