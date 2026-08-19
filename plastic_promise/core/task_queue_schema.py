"""SQL DDL for hunter guild task queue system.

All tables live in the existing plastic_memory.db alongside trust_scores.
Schema creation is idempotent (IF NOT EXISTS).
"""

import sqlite3

from plastic_promise.core.project_identity import LEGACY_QUARANTINE_PROJECT_ID

LEGACY_TASK_PROJECT_ID = LEGACY_QUARANTINE_PROJECT_ID
TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID = "20260810_task_queue_project_scope_v1"


def _canonical_project_id_sql(column: str) -> str:
    """Return the SQLite predicate matching the shared canonical ID grammar."""

    return f"""
        length({column}) BETWEEN 9 AND 256
        AND {column} = trim({column})
        AND substr({column}, 1, 8) = 'project:'
        AND lower({column}) != 'project:unknown'
        AND substr({column}, 9, 1) GLOB '[A-Za-z0-9]'
        AND substr({column}, 9) NOT GLOB '*[^A-Za-z0-9_.:/-]*'
    """.strip()


def _writable_task_project_id_sql(column: str) -> str:
    """Return the stricter predicate for new ordinary Task Queue ownership."""

    return f"""
        ({_canonical_project_id_sql(column)})
        AND lower({column}) NOT IN ('project:legacy-global', '{LEGACY_TASK_PROJECT_ID}')
    """.strip()


TASK_QUEUE_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS task_queue (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL DEFAULT '{LEGACY_TASK_PROJECT_ID}'
                    CHECK({_canonical_project_id_sql("project_id")}),
    task_type       TEXT NOT NULL,
    priority        INTEGER DEFAULT 3,
    status          TEXT DEFAULT 'pending',
    title           TEXT NOT NULL,
    description     TEXT,
    payload         TEXT,
    from_agent      TEXT DEFAULT 'daemon',
    to_agent        TEXT NOT NULL,
    domain          TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT,
    heartbeat_at    TEXT,
    done_at         TEXT,
    verified_at     TEXT,
    verified_by     TEXT,
    verify_verdict  TEXT,
    result          TEXT,
    escalation_count INTEGER DEFAULT 0,
    max_escalations  INTEGER DEFAULT 3,
    last_escalation_at TEXT,
    timeout_seconds  INTEGER DEFAULT 300,
    memory_id       TEXT,
    principle_id    TEXT,
    source_scan     TEXT,
    parent_task_id  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

TASK_QUEUE_PROJECT_ID_VALIDATION_DDL = f"""
DROP TRIGGER IF EXISTS trg_task_project_id_insert;
DROP TRIGGER IF EXISTS trg_task_project_id_update;

CREATE TRIGGER trg_task_project_id_insert
BEFORE INSERT ON task_queue
WHEN NOT ({_writable_task_project_id_sql("NEW.project_id")})
BEGIN
    SELECT RAISE(ABORT, 'invalid canonical task project_id');
END;

CREATE TRIGGER trg_task_project_id_update
BEFORE UPDATE OF project_id ON task_queue
WHEN NOT ({_writable_task_project_id_sql("NEW.project_id")})
BEGIN
    SELECT RAISE(ABORT, 'invalid canonical task project_id');
END;
"""

TASK_QUEUE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_to_agent ON task_queue(to_agent);
CREATE INDEX IF NOT EXISTS idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_parent ON task_queue(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_dedup ON task_queue(task_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_claimed ON task_queue(claimed_by, status);
CREATE INDEX IF NOT EXISTS idx_task_project_status
    ON task_queue(project_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_project_agent
    ON task_queue(project_id, claimed_by, status);
CREATE INDEX IF NOT EXISTS idx_task_project_parent
    ON task_queue(project_id, parent_task_id);
"""

# Existing duplicates are grandfathered.  Guard inserts only: controlled task
# updates advance lifecycle state and must be able to return an existing task
# to ``pending`` without rewriting or deleting historical rows.
TASK_QUEUE_DEDUPLICATION_DDL = f"""
DROP INDEX IF EXISTS uq_task_project_pending_payload;
DROP TRIGGER IF EXISTS trg_task_project_pending_payload_insert;
DROP TRIGGER IF EXISTS trg_task_project_pending_payload_update;

CREATE TRIGGER trg_task_project_pending_payload_insert
BEFORE INSERT ON task_queue
WHEN NEW.project_id != '{LEGACY_TASK_PROJECT_ID}'
  AND NEW.status = 'pending'
  AND NEW.source_scan IS NOT NULL
  AND COALESCE(
        CASE WHEN json_valid(NEW.payload)
             THEN json_extract(NEW.payload, '$.payload_hash') END,
        ''
      ) != ''
  AND EXISTS (
        SELECT 1
        FROM task_queue AS existing
        WHERE existing.project_id = NEW.project_id
          AND existing.task_type = NEW.task_type
          AND existing.status = 'pending'
          AND existing.source_scan IS NOT NULL
          AND CASE WHEN json_valid(existing.payload)
                   THEN json_extract(existing.payload, '$.payload_hash') END
              = CASE WHEN json_valid(NEW.payload)
                     THEN json_extract(NEW.payload, '$.payload_hash') END
      )
BEGIN
    SELECT RAISE(ABORT, 'duplicate pending scanner task payload');
END;
"""

# Backwards-compatible export for callers that only need the fresh-schema DDL.
TASK_QUEUE_DDL = TASK_QUEUE_TABLE_DDL + TASK_QUEUE_INDEX_DDL + TASK_QUEUE_DEDUPLICATION_DDL

TASK_QUEUE_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS task_queue_schema_migrations (
    migration_id     TEXT PRIMARY KEY,
    applied_at       TEXT NOT NULL DEFAULT (datetime('now')),
    quarantined_rows INTEGER NOT NULL DEFAULT 0,
    details          TEXT NOT NULL
);
"""

TASK_SUBSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS task_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    task_type_filter TEXT,
    priority_min    INTEGER DEFAULT 3,
    keywords        TEXT,
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_name, task_type_filter)
);
"""

HUNTER_FAILURE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS hunter_failure_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    failure_type    TEXT NOT NULL,
    trust_before    REAL,
    trust_after     REAL,
    penalty_applied REAL,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES task_queue(id)
);

CREATE INDEX IF NOT EXISTS idx_failure_agent ON hunter_failure_log(agent_name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_failure_type ON hunter_failure_log(agent_name, task_type, failure_type);
"""

METRIC_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS metric_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    metric_value REAL NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    computed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_metric_name_time ON metric_history(metric_name, computed_at);
"""

DEFAULT_SUBSCRIPTIONS = [
    ("pi_fixer", "fix_*", 3, '["fix","memory","orphan","stale","gc","decay"]'),
    ("pi_fixer", "gc_*", 3, '["cleanup","decay","zombie"]'),
    ("pi_builder", "build_*", 3, '["build","implement","scaffold","refactor"]'),
    ("pi_builder", "refactor_*", 3, '["decouple","module","optimize"]'),
    ("pi_reviewer", "review_*", 3, '["review","audit","quality","trend"]'),
    ("pi_reviewer", "investigate_*", 2, '["recurrence","trust","anomaly"]'),
    ("claude", "audit_*", 1, '["architecture","coupling","security"]'),
    ("claude", "investigate_*", 1, '["trust","drop","escalation"]'),
    ("claude", None, 1, None),
]


def _execute_ddl(conn, script: str) -> None:
    """Execute one DDL statement at a time without ``executescript`` commits."""

    statement_lines: list[str] = []
    for line in script.splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines).strip()
        if statement and sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement_lines.clear()

    if any(line.strip() for line in statement_lines):
        raise ValueError("incomplete task queue DDL statement")


def _apply_task_table_schema(conn) -> None:
    """Apply the idempotent schema inside the caller's active transaction."""

    # Create the table before its project-scoped indexes.  An older production
    # table can already exist without ``project_id``; running the indexes first
    # would make the additive migration impossible.
    _execute_ddl(conn, TASK_QUEUE_TABLE_DDL)
    _execute_ddl(conn, TASK_QUEUE_MIGRATIONS_DDL)

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
    quarantined_rows = 0
    migration_details = "project_id already present; no legacy rows changed"
    if "project_id" not in columns:
        quarantined_rows = int(conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0])
        conn.execute(
            "ALTER TABLE task_queue ADD COLUMN project_id TEXT NOT NULL "
            f"DEFAULT '{LEGACY_TASK_PROJECT_ID}' "
            f"CHECK({_canonical_project_id_sql('project_id')})"
        )
        migration_details = (
            "added project_id additively; rows without canonical ownership were "
            f"assigned to {LEGACY_TASK_PROJECT_ID}"
        )

    conn.execute(
        "INSERT OR IGNORE INTO task_queue_schema_migrations "
        "(migration_id, quarantined_rows, details) VALUES (?, ?, ?)",
        (
            TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID,
            quarantined_rows,
            migration_details,
        ),
    )
    _execute_ddl(conn, TASK_QUEUE_PROJECT_ID_VALIDATION_DDL)
    _execute_ddl(conn, TASK_QUEUE_INDEX_DDL)
    _execute_ddl(conn, TASK_QUEUE_DEDUPLICATION_DDL)
    _execute_ddl(conn, TASK_SUBSCRIPTIONS_DDL)
    _execute_ddl(conn, HUNTER_FAILURE_LOG_DDL)
    _execute_ddl(conn, METRIC_HISTORY_DDL)

    # Seed default subscriptions
    # NOTE: SQLite UNIQUE treats NULLs as distinct, so for NULL task_type_filter
    # we must check existence explicitly to avoid duplicates on re-run.

    for agent, filt, prio, keywords in DEFAULT_SUBSCRIPTIONS:
        if filt is None:
            existing = conn.execute(
                "SELECT COUNT(*) FROM task_subscriptions WHERE agent_name = ? AND task_type_filter IS NULL",
                (agent,),
            ).fetchone()[0]
            if existing == 0:
                conn.execute(
                    "INSERT INTO task_subscriptions "
                    "(agent_name, task_type_filter, priority_min, keywords) "
                    "VALUES (?, ?, ?, ?)",
                    (agent, filt, prio, keywords),
                )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO task_subscriptions "
                "(agent_name, task_type_filter, priority_min, keywords) "
                "VALUES (?, ?, ?, ?)",
                (agent, filt, prio, keywords),
            )


def migrate_task_tables_on_startup(conn) -> None:
    """Run the task schema migration under explicit startup transaction authority.

    Startup migration owns an otherwise-idle connection and therefore owns the
    matching commit or rollback.  It must never be used to borrow authority from
    a caller transaction.
    """

    if conn.in_transaction:
        raise RuntimeError("startup task migration requires an idle connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        _apply_task_table_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_task_tables(conn) -> None:
    """Ensure task tables without committing a transaction owned by the caller.

    Existing transactions retain full commit/rollback authority.  Callers that
    provide an idle connection delegate the bounded transaction to the explicit
    startup migration authority so existing runtime initialization remains
    backwards compatible.
    """

    if conn.in_transaction:
        _apply_task_table_schema(conn)
        return
    migrate_task_tables_on_startup(conn)
