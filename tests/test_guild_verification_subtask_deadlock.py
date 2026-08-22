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

# ═══════════════════════════════════════════════════════════════
# CAS relaxation + trust attribution fallback regression tests
# ═══════════════════════════════════════════════════════════════


def _make_pending_verification_subtask(e2e_db_path, agent_name="pi_fixer"):
    """enqueue -> claim -> complete; returns the auto-created verify_task id."""
    engine = MockEngine()
    enq = _run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT,
                "task_type": "fix_memory",
                "title": "主委托",
                "to_agent": agent_name,
                "from_agent": "claude",
                "priority": 3,
            },
        )
    )
    main_id = __import__("json").loads(enq[0].text)["task_id"]
    _run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT,
                "task_id": main_id,
                "agent_name": agent_name,
                "trust_score": 0.60,
            },
        )
    )
    comp = _run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT,
                "task_id": main_id,
                "agent_name": agent_name,
                "result": "done",
            },
        )
    )
    sub_id = __import__("json").loads(comp[0].text)["verification_task_id"]
    assert sub_id, "complete must auto-create the verification subtask"
    return main_id, sub_id


def _verify(engine, sub_id, verdict):
    return _run(
        handle_task_verify(
            engine,
            {
                "project_id": PROJECT,
                "task_id": sub_id,
                "verdict": verdict,
                "verified_by": "claude",
                "comment": "长老复核",
            },
        )
    )


def test_rejected_pending_verification_subtask_reassigns(e2e_db_path):
    engine = MockEngine()
    _, sub_id = _make_pending_verification_subtask(e2e_db_path)

    result = _verify(engine, sub_id, "rejected")
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is True, payload
    assert payload["new_status"] == "reassigned"

    import sqlite3

    conn = sqlite3.connect(e2e_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM task_queue WHERE parent_task_id=? ORDER BY created_at DESC, id",
        (sub_id,),
    ).fetchall()
    conn.close()
    assert len(rows) == 1, f"expected exactly one reassigned child, got {len(rows)}"
    child = rows[0]
    assert child["status"] == "pending"
    assert child["id"] == payload["new_task_id"]
    assert child["title"].startswith("[重派]")


def test_second_accept_conflicts(e2e_db_path):
    engine = MockEngine()
    _, sub_id = _make_pending_verification_subtask(e2e_db_path)

    first = __import__("json").loads(_verify(engine, sub_id, "accepted")[0].text)
    assert first["success"] is True and first["new_status"] == "verified"

    # Second accept hits the pre-check CAS: status is no longer done/pending.
    result = _verify(engine, sub_id, "accepted")
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is False
    assert payload["status"] == "rejected"
    assert payload["reason"] == "task_state_conflict"
    assert payload["current_status"] == "verified"


def test_accept_trust_target_falls_back_to_original_agent(e2e_db_path):
    engine = MockEngine()
    _, sub_id = _make_pending_verification_subtask(
        e2e_db_path, agent_name="pi_hunter_a"
    )

    result = _verify(engine, sub_id, "accepted")
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is True, payload
    trust_adjustment = payload["trust_adjustment"]
    assert trust_adjustment["agent"] == "pi_hunter_a"
    assert "skipped_reason" not in trust_adjustment


def test_accept_without_trust_target_marks_skipped(e2e_db_path):
    import sqlite3

    engine = MockEngine()
    # Hand-plant a pending verification subtask whose payload carries no
    # original_agent (and nothing was ever claimed).
    sub_id = "t_test_no_target"
    conn = sqlite3.connect(e2e_db_path)
    conn.execute(
        "INSERT INTO task_queue (id, project_id, task_type, title, to_agent, "
        "priority, from_agent, status, description, payload) "
        "VALUES (?, ?, 'verify_task', ?, 'claude', 3, 'system', 'pending', ?, ?)",
        (
            sub_id,
            PROJECT,
            "验收委托: 无归属",
            "手工植入的无 original_agent 验收子委托",
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    result = _verify(engine, sub_id, "accepted")
    payload = __import__("json").loads(result[0].text)
    assert payload["success"] is True, payload
    trust_adjustment = payload["trust_adjustment"]
    assert trust_adjustment["skipped_reason"] == "no_trust_target"
    assert trust_adjustment["agent"] is None
