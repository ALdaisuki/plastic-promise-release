import datetime
import sqlite3

from plastic_promise.core.task_queue_schema import ensure_task_tables


def _insert_task(conn, task_id, *, status, claimed_by, heartbeat_at, timeout_seconds=60):
    conn.execute(
        "INSERT INTO task_queue "
        "(id, task_type, title, to_agent, status, claimed_by, claimed_at, heartbeat_at, "
        "timeout_seconds, escalation_count, max_escalations, updated_at) "
        "VALUES (?, 'fix_memory', ?, 'pi_fixer', ?, ?, ?, ?, ?, 0, 3, ?)",
        (
            task_id,
            task_id,
            status,
            claimed_by,
            heartbeat_at,
            heartbeat_at,
            timeout_seconds,
            heartbeat_at,
        ),
    )


def test_release_stale_claims_returns_timed_out_tasks_to_pending(tmp_path):
    from plastic_promise.core.task_recovery import release_stale_claims

    db_path = tmp_path / "plastic.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    now = datetime.datetime(2026, 7, 7, 12, 0, 0)
    stale = (now - datetime.timedelta(seconds=120)).isoformat()
    fresh = (now - datetime.timedelta(seconds=10)).isoformat()
    _insert_task(conn, "stale_claimed", status="claimed", claimed_by="pi_fixer", heartbeat_at=stale)
    _insert_task(conn, "stale_executing", status="executing", claimed_by="pi_builder", heartbeat_at=stale)
    _insert_task(conn, "fresh_claimed", status="claimed", claimed_by="pi_reviewer", heartbeat_at=fresh)
    conn.commit()
    conn.close()

    result = release_stale_claims(db_path, now=now)

    assert result["released_count"] == 2
    assert set(result["released_task_ids"]) == {"stale_claimed", "stale_executing"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stale_claimed = conn.execute(
        "SELECT status, claimed_by, escalation_count FROM task_queue WHERE id='stale_claimed'"
    ).fetchone()
    fresh_claimed = conn.execute(
        "SELECT status, claimed_by, escalation_count FROM task_queue WHERE id='fresh_claimed'"
    ).fetchone()
    failures = conn.execute(
        "SELECT agent_name, task_id, failure_type, penalty_applied "
        "FROM hunter_failure_log ORDER BY task_id"
    ).fetchall()
    conn.close()

    assert dict(stale_claimed) == {
        "status": "pending",
        "claimed_by": None,
        "escalation_count": 1,
    }
    assert dict(fresh_claimed) == {
        "status": "claimed",
        "claimed_by": "pi_reviewer",
        "escalation_count": 0,
    }
    assert [row["task_id"] for row in failures] == ["stale_claimed", "stale_executing"]
    assert {row["failure_type"] for row in failures} == {"timeout"}
    assert {row["penalty_applied"] for row in failures} == {-0.01}


def test_release_stale_claims_escalates_to_claude_after_max_timeouts(tmp_path):
    from plastic_promise.core.task_recovery import release_stale_claims

    db_path = tmp_path / "plastic.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    now = datetime.datetime(2026, 7, 7, 12, 0, 0)
    stale = (now - datetime.timedelta(seconds=120)).isoformat()
    _insert_task(conn, "repeat_timeout", status="claimed", claimed_by="pi_fixer", heartbeat_at=stale)
    conn.execute(
        "UPDATE task_queue SET escalation_count=2, max_escalations=3 WHERE id='repeat_timeout'"
    )
    conn.commit()
    conn.close()

    result = release_stale_claims(db_path, now=now)

    assert result["released_count"] == 1
    assert result["escalated_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT status, to_agent, claimed_by, escalation_count FROM task_queue WHERE id='repeat_timeout'"
    ).fetchone()
    conn.close()

    assert dict(task) == {
        "status": "pending",
        "to_agent": "claude",
        "claimed_by": None,
        "escalation_count": 3,
    }
