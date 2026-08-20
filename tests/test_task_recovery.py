import datetime
import sqlite3

from plastic_promise.core.task_queue_schema import ensure_task_tables


def _insert_task(
    conn,
    task_id,
    *,
    project_id="project:alpha",
    status,
    claimed_by,
    heartbeat_at,
    timeout_seconds=60,
):
    conn.execute(
        "INSERT INTO task_queue "
        "(id, project_id, task_type, title, to_agent, status, claimed_by, claimed_at, heartbeat_at, "
        "timeout_seconds, escalation_count, max_escalations, updated_at) "
        "VALUES (?, ?, 'fix_memory', ?, 'pi_fixer', ?, ?, ?, ?, ?, 0, 3, ?)",
        (
            task_id,
            project_id,
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
    _insert_task(
        conn, "stale_executing", status="executing", claimed_by="pi_builder", heartbeat_at=stale
    )
    _insert_task(
        conn, "fresh_claimed", status="claimed", claimed_by="pi_reviewer", heartbeat_at=fresh
    )
    _insert_task(
        conn,
        "other_project_stale",
        project_id="project:beta",
        status="claimed",
        claimed_by="pi_fixer",
        heartbeat_at=stale,
    )
    conn.commit()
    conn.close()

    result = release_stale_claims(db_path, project_id="project:alpha", now=now)

    assert result["authority_scope"] == "project"
    assert result["project_id"] == "project:alpha"
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
    other_project = conn.execute(
        "SELECT status, claimed_by, escalation_count FROM task_queue WHERE id='other_project_stale'"
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
    assert dict(other_project) == {
        "status": "claimed",
        "claimed_by": "pi_fixer",
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
    _insert_task(
        conn, "repeat_timeout", status="claimed", claimed_by="pi_fixer", heartbeat_at=stale
    )
    conn.execute(
        "UPDATE task_queue SET escalation_count=2, max_escalations=3 WHERE id='repeat_timeout'"
    )
    conn.commit()
    conn.close()

    result = release_stale_claims(db_path, project_id="project:alpha", now=now)

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


def test_release_stale_claims_requires_explicit_authority(tmp_path):
    import pytest

    from plastic_promise.core.task_recovery import release_stale_claims

    with pytest.raises(ValueError, match="valid project_id"):
        release_stale_claims(tmp_path / "plastic.db")


def test_release_stale_claims_system_authority_includes_quarantine(tmp_path):
    from plastic_promise.core.task_queue_schema import LEGACY_TASK_PROJECT_ID
    from plastic_promise.core.task_recovery import release_stale_claims

    db_path = tmp_path / "plastic.db"
    conn = sqlite3.connect(db_path)
    ensure_task_tables(conn)
    now = datetime.datetime(2026, 7, 7, 12, 0, 0)
    stale = (now - datetime.timedelta(seconds=120)).isoformat()
    # Reconstruct a grandfathered pre-validation row. Ordinary post-migration
    # inserts cannot create quarantine ownership; recovery still needs to
    # release historical rows that the additive migration preserved.
    conn.execute("DROP TRIGGER trg_task_project_id_insert")
    _insert_task(
        conn,
        "legacy_stale",
        project_id=LEGACY_TASK_PROJECT_ID,
        status="claimed",
        claimed_by="pi_fixer",
        heartbeat_at=stale,
    )
    conn.commit()
    ensure_task_tables(conn)
    conn.close()

    result = release_stale_claims(db_path, system_authority=True, now=now)

    assert result["authority_scope"] == "system"
    assert result["project_id"] is None
    assert result["released_task_ids"] == ["legacy_stale"]


def test_release_stale_claims_closes_connection_when_schema_check_fails(tmp_path, monkeypatch):
    import pytest

    from plastic_promise.core import task_recovery

    real_connect = sqlite3.connect
    connections = []

    class TrackingConnection:
        def __init__(self, path):
            self._conn = real_connect(path)
            self.closed = False

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            self.closed = True
            self._conn.close()

    def connect(path):
        connection = TrackingConnection(path)
        connections.append(connection)
        return connection

    def fail_schema_check(_connection):
        raise RuntimeError("schema unavailable")

    monkeypatch.setattr(task_recovery.sqlite3, "connect", connect)
    monkeypatch.setattr(task_recovery, "ensure_task_tables", fail_schema_check)

    with pytest.raises(RuntimeError, match="schema unavailable"):
        task_recovery.release_stale_claims(
            tmp_path / "plastic.db",
            project_id="project:alpha",
        )

    assert len(connections) == 1
    assert connections[0].closed is True


def test_release_stale_claims_skips_row_when_heartbeat_wins_snapshot_cas(tmp_path, monkeypatch):
    from plastic_promise.core import task_recovery

    db_path = tmp_path / "plastic.db"
    real_connect = sqlite3.connect
    conn = real_connect(db_path)
    ensure_task_tables(conn)
    now = datetime.datetime(2026, 7, 7, 12, 0, 0)
    stale = (now - datetime.timedelta(seconds=120)).isoformat()
    fresh = (now - datetime.timedelta(seconds=5)).isoformat()
    _insert_task(
        conn,
        "heartbeat-race",
        status="claimed",
        claimed_by="pi_fixer",
        heartbeat_at=stale,
    )
    conn.commit()
    conn.close()

    class RacingConnection:
        def __init__(self, path):
            self._conn = real_connect(path)
            self._injected = False

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._conn.row_factory = value

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, parameters=()):
            if not self._injected and sql.startswith("UPDATE task_queue SET status='pending'"):
                self._injected = True
                racer = real_connect(db_path)
                racer.execute(
                    "UPDATE task_queue SET heartbeat_at=?, updated_at=? WHERE id=?",
                    (fresh, fresh, "heartbeat-race"),
                )
                racer.commit()
                racer.close()
            return self._conn.execute(sql, parameters)

    monkeypatch.setattr(task_recovery.sqlite3, "connect", RacingConnection)

    result = task_recovery.release_stale_claims(
        db_path,
        project_id="project:alpha",
        now=now,
    )

    assert result["released_count"] == 0
    check = real_connect(db_path)
    row = check.execute(
        "SELECT status, claimed_by, heartbeat_at FROM task_queue WHERE id=?",
        ("heartbeat-race",),
    ).fetchone()
    failures = check.execute(
        "SELECT COUNT(*) FROM hunter_failure_log WHERE task_id=?",
        ("heartbeat-race",),
    ).fetchone()[0]
    check.close()
    assert row == ("claimed", "pi_fixer", fresh)
    assert failures == 0
