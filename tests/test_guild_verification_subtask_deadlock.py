"""Elder direct-verification of pending verification subtasks.

task_complete auto-creates a verification subtask in 'pending' state, but
task_verify only accepted 'done' rows -- a deadlock: nobody can meaningfully
claim and complete their own verification request. A server-owned reviewer
authority may verify a pending verify_task subtask directly.
"""
import asyncio

import pytest

from plastic_promise.mcp.tools.task_queue import (
    handle_task_claim,
    handle_task_complete,
    handle_task_enqueue,
    handle_task_verify,
)


PROJECT = "project:plastic-promise"


@pytest.fixture()
def e2e_db_path(tmp_path, monkeypatch):
    import sqlite3

    from plastic_promise.core.task_queue_schema import ensure_task_tables

    db_path = str(tmp_path / "test_plastic.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    conn.close()
    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    return db_path


class MockEngine:
    pass


def _run(coro):
    return asyncio.run(coro)


def test_elder_can_directly_verify_pending_verification_subtask(
    e2e_db_path, monkeypatch
):
    engine = MockEngine()
    hunter_trust = 0.60

    enq = _run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT,
                "task_type": "fix_memory",
                "title": "主委托",
                "to_agent": "pi_fixer",
                "from_agent": "claude",
                "priority": 3,
            },
        )
    )
    main_id = enq[0].text and __import__("json").loads(enq[0].text)["task_id"]

    _run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT,
                "task_id": main_id,
                "agent_name": "pi_fixer",
                "trust_score": hunter_trust,
            },
        )
    )
    comp = _run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT,
                "task_id": main_id,
                "agent_name": "pi_fixer",
                "result": "done",
            },
        )
    )
    comp_payload = __import__("json").loads(comp[0].text)
    sub_id = comp_payload.get("verification_task_id")
    assert sub_id, "complete must auto-create the verification subtask"

    import sqlite3

    conn = sqlite3.connect(e2e_db_path)
    conn.row_factory = sqlite3.Row
    sub = conn.execute(
        "SELECT status, task_type FROM task_queue WHERE id=?", (sub_id,)
    ).fetchone()
    conn.close()
    assert sub["status"] == "pending", "subtask is created pending"
    assert sub["task_type"] == "verify_task"

    # Before the fix this returned task_state_conflict(current_status=pending)
    result = _run(
        handle_task_verify(
            engine,
            {
                "project_id": PROJECT,
                "task_id": sub_id,
                "verdict": "accepted",
                "verified_by": "claude",
                "comment": "elder direct verification",
            },
        )
    )
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is True, payload
    assert payload["new_status"] == "verified"


def test_regular_pending_task_still_rejects_direct_verification(
    e2e_db_path, monkeypatch
):
    engine = MockEngine()
    enq = _run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT,
                "task_type": "fix_memory",
                "title": "普通任务未完成不可验收",
                "to_agent": "pi_fixer",
                "from_agent": "daemon",
            },
        )
    )
    task_id = __import__("json").loads(enq[0].text)["task_id"]
    result = _run(
        handle_task_verify(
            engine,
            {
                "project_id": PROJECT,
                "task_id": task_id,
                "verdict": "accepted",
                "verified_by": "claude",
            },
        )
    )
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is False
    assert payload["reason"] == "task_state_conflict"
