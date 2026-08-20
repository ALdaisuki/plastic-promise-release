"""Transaction-boundary tests for the canonical task-queue schema."""

import sqlite3

import pytest

from plastic_promise.core.task_queue_schema import (
    LEGACY_TASK_PROJECT_ID,
    TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID,
    TASK_QUEUE_TABLE_DDL,
    ensure_task_tables,
    migrate_task_tables_on_startup,
)


def _create_legacy_task_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE task_queue ("
        "id TEXT PRIMARY KEY, task_type TEXT NOT NULL, priority INTEGER DEFAULT 3, "
        "status TEXT DEFAULT 'pending', title TEXT NOT NULL, description TEXT, payload TEXT, "
        "from_agent TEXT DEFAULT 'daemon', to_agent TEXT NOT NULL, domain TEXT, "
        "claimed_by TEXT, claimed_at TEXT, heartbeat_at TEXT, done_at TEXT, verified_at TEXT, "
        "verified_by TEXT, verify_verdict TEXT, result TEXT, escalation_count INTEGER DEFAULT 0, "
        "max_escalations INTEGER DEFAULT 3, last_escalation_at TEXT, "
        "timeout_seconds INTEGER DEFAULT 300, memory_id TEXT, principle_id TEXT, "
        "source_scan TEXT, parent_task_id TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )


def _insert_pending_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    project_id: str,
    source_scan: str | None,
) -> None:
    conn.execute(
        "INSERT INTO task_queue "
        "(id, project_id, task_type, title, to_agent, source_scan, payload) "
        "VALUES (?, ?, 'fix_memory', ?, 'pi_fixer', ?, ?)",
        (
            task_id,
            project_id,
            task_id,
            source_scan,
            '{"payload_hash":"shared-payload"}',
        ),
    )


def test_ensure_task_tables_preserves_caller_rollback_for_legacy_migration(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "legacy.db")
    _create_legacy_task_queue(conn)
    conn.execute("CREATE TABLE caller_state (value TEXT NOT NULL)")
    conn.commit()

    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_state (value) VALUES ('must-roll-back')")

    ensure_task_tables(conn)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM caller_state").fetchone()[0] == 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)")}
    assert "project_id" not in columns
    conn.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_startup_migration_authority_is_idempotent_for_fresh_and_legacy_schema(
    tmp_path, legacy
) -> None:
    conn = sqlite3.connect(tmp_path / f"{'legacy' if legacy else 'fresh'}.db")
    if legacy:
        _create_legacy_task_queue(conn)
        conn.execute(
            "INSERT INTO task_queue (id, task_type, title, to_agent) "
            "VALUES ('legacy-task', 'fix_memory', 'legacy', 'pi_fixer')"
        )
        conn.commit()

    migrate_task_tables_on_startup(conn)
    migrate_task_tables_on_startup(conn)

    assert conn.in_transaction is False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)")}
    assert "project_id" in columns
    migration_count = conn.execute(
        "SELECT COUNT(*) FROM task_queue_schema_migrations WHERE migration_id = ?",
        (TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID,),
    ).fetchone()[0]
    assert migration_count == 1
    if legacy:
        project_id = conn.execute(
            "SELECT project_id FROM task_queue WHERE id = 'legacy-task'"
        ).fetchone()[0]
        assert project_id == LEGACY_TASK_PROJECT_ID
    conn.close()


def test_startup_migration_authority_refuses_caller_transaction(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "borrowed.db")
    conn.execute("BEGIN")

    with pytest.raises(RuntimeError, match="requires an idle connection"):
        migrate_task_tables_on_startup(conn)

    assert conn.in_transaction is True
    conn.rollback()
    conn.close()


@pytest.mark.parametrize(
    "project_id",
    [
        "project:has space",
        "project:control\ncharacter",
        "project:非canonical",
        f"project:{'a' * 249}",
        "project:unknown",
        "project:legacy-global",
        LEGACY_TASK_PROJECT_ID,
    ],
)
def test_task_schema_rejects_new_noncanonical_project_ids(tmp_path, project_id) -> None:
    conn = sqlite3.connect(tmp_path / "canonical-project-id.db")
    migrate_task_tables_on_startup(conn)

    with pytest.raises(sqlite3.IntegrityError, match="canonical task project_id"):
        _insert_pending_task(
            conn,
            task_id="invalid-project",
            project_id=project_id,
            source_scan=None,
        )

    conn.rollback()
    _insert_pending_task(
        conn,
        task_id="valid-project",
        project_id="project:alpha",
        source_scan=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="canonical task project_id"):
        conn.execute(
            "UPDATE task_queue SET project_id = ? WHERE id = 'valid-project'",
            (project_id,),
        )
    conn.rollback()
    conn.close()


def test_task_schema_rejects_new_rows_without_explicit_project_id(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "missing-project-id.db")
    migrate_task_tables_on_startup(conn)

    with pytest.raises(sqlite3.IntegrityError, match="canonical task project_id"):
        conn.execute(
            "INSERT INTO task_queue (id, task_type, title, to_agent) "
            "VALUES ('missing-project', 'fix_memory', 'missing', 'pi_fixer')"
        )

    conn.rollback()
    conn.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_payload_deduplication_is_scanner_scoped_after_migration(tmp_path, legacy) -> None:
    conn = sqlite3.connect(tmp_path / f"dedupe-{'legacy' if legacy else 'fresh'}.db")
    if legacy:
        _create_legacy_task_queue(conn)
        conn.commit()

    migrate_task_tables_on_startup(conn)

    _insert_pending_task(
        conn,
        task_id="ordinary-one",
        project_id="project:alpha",
        source_scan=None,
    )
    _insert_pending_task(
        conn,
        task_id="ordinary-two",
        project_id="project:alpha",
        source_scan=None,
    )
    _insert_pending_task(
        conn,
        task_id="scanner-alpha",
        project_id="project:alpha",
        source_scan="scan_duplicates",
    )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_pending_task(
            conn,
            task_id="scanner-alpha-duplicate",
            project_id="project:alpha",
            source_scan="scan_duplicates_again",
        )

    _insert_pending_task(
        conn,
        task_id="scanner-beta",
        project_id="project:beta",
        source_scan="scan_duplicates",
    )
    conn.commit()

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE project_id = 'project:alpha' "
            "AND source_scan IS NULL"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM task_queue WHERE source_scan IS NOT NULL").fetchone()[0]
        == 2
    )
    conn.close()


def test_startup_migration_preserves_existing_scanner_duplicates(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "pre-dedupe.db")
    conn.execute(TASK_QUEUE_TABLE_DDL)
    _insert_pending_task(
        conn,
        task_id="historical-duplicate-one",
        project_id="project:alpha",
        source_scan="scan_duplicates",
    )
    _insert_pending_task(
        conn,
        task_id="historical-duplicate-two",
        project_id="project:alpha",
        source_scan="scan_duplicates",
    )
    conn.commit()

    migrate_task_tables_on_startup(conn)

    historical_rows = conn.execute(
        "SELECT id, project_id, task_type, status, source_scan, payload "
        "FROM task_queue WHERE project_id = 'project:alpha' ORDER BY id"
    ).fetchall()
    assert historical_rows == [
        (
            "historical-duplicate-one",
            "project:alpha",
            "fix_memory",
            "pending",
            "scan_duplicates",
            '{"payload_hash":"shared-payload"}',
        ),
        (
            "historical-duplicate-two",
            "project:alpha",
            "fix_memory",
            "pending",
            "scan_duplicates",
            '{"payload_hash":"shared-payload"}',
        ),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        _insert_pending_task(
            conn,
            task_id="new-duplicate",
            project_id="project:alpha",
            source_scan="scan_duplicates_again",
        )

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 2
    conn.close()
