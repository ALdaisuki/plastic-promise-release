"""SQL DDL for hunter guild task queue system.

All tables live in the existing plastic_memory.db alongside trust_scores.
Schema creation is idempotent (IF NOT EXISTS).
"""

TASK_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS task_queue (
    id              TEXT PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_to_agent ON task_queue(to_agent);
CREATE INDEX IF NOT EXISTS idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_parent ON task_queue(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_claimed ON task_queue(claimed_by, status);
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


def ensure_task_tables(conn):
    """Create all task queue tables if they don't exist. Idempotent."""
    conn.executescript(TASK_QUEUE_DDL)
    conn.executescript(TASK_SUBSCRIPTIONS_DDL)
    conn.executescript(HUNTER_FAILURE_LOG_DDL)
    conn.executescript(METRIC_HISTORY_DDL)

    # Seed default subscriptions
    # NOTE: SQLite UNIQUE treats NULLs as distinct, so for NULL task_type_filter
    # we must check existence explicitly to avoid duplicates on re-run.
    import json

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
    conn.commit()
