"""Explicit, backup-gated canonical SQLite migrations owned by deployment.

Runtime code may inspect the node-governance schema but cannot apply it.  This
module is intentionally standard-library-only so a deploy controller can
perform a versioned migration without importing MCP or service runtime code.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .migration_journal import apply_migration_journal_schema, migration_journal_schema_present

NODE_GOVERNANCE_SCHEMA_VERSION = 2
NODE_GOVERNANCE_V2_MIGRATION_ID = "node-governance-v2"
MIGRATION_EXECUTION_JOURNAL_MIGRATION_ID = "migration-execution-journal-v2"
PRODUCTION_READINESS_SCHEMA_MIGRATION_ID = "production-readiness-schema-v1"
PROMOTION_TASK_SCHEMA_MIGRATION_ID = "memory-proposal-promotion-tasks-v1"

_REQUIRED_TABLES = frozenset(
    {
        "inference_nodes",
        "inference_node_reservations",
        "inference_node_latency_samples",
        "inference_node_audit_events",
        "inference_node_identity_receipts",
        "derived_work_accelerator_audit_events",
    }
)
_REQUIRED_COLUMNS = {
    "inference_nodes": frozenset(
        {
            "node_id",
            "node_kind",
            "transport_id",
            "transport_evidence",
            "expected_identity_json",
            "declared_capabilities_json",
            "max_concurrency",
            "state",
            "observed_identity_json",
            "observed_capabilities_json",
            "queue_depth",
            "reported_available_slots",
            "registration_source",
            "registration_reference",
            "verification_receipt",
            "quarantine_reason",
            "last_health_at",
            "created_at",
            "updated_at",
        }
    ),
    "inference_node_reservations": frozenset(
        {"job_id", "fencing_generation", "project_id", "node_id", "lease_expires_at", "created_at"}
    ),
    "inference_node_latency_samples": frozenset(
        {"sample_id", "node_id", "operation", "required_identity", "latency_ms", "succeeded_at"}
    ),
    "inference_node_audit_events": frozenset(
        {"event_sequence", "event_id", "node_id", "event_name", "created_at"}
    ),
    "inference_node_identity_receipts": frozenset(
        {
            "receipt_id",
            "node_id",
            "config_revision",
            "required_identity",
            "observed_identity",
            "profile_digest",
            "issued_at",
        }
    ),
    "derived_work_accelerator_audit_events": frozenset(
        {
            "event_id",
            "event_kind",
            "task_kind",
            "decision",
            "reason_code",
            "day_utc",
            "occurred_at",
        }
    ),
}
_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS inference_nodes (
        node_id TEXT PRIMARY KEY,
        node_kind TEXT NOT NULL,
        transport_id TEXT NOT NULL,
        transport_evidence TEXT NOT NULL,
        expected_identity_json TEXT NOT NULL,
        declared_capabilities_json TEXT NOT NULL,
        max_concurrency INTEGER NOT NULL,
        state TEXT NOT NULL,
        observed_identity_json TEXT,
        observed_capabilities_json TEXT,
        queue_depth INTEGER NOT NULL DEFAULT 0,
        reported_available_slots INTEGER NOT NULL DEFAULT 0,
        registration_source TEXT NOT NULL,
        registration_reference TEXT NOT NULL,
        verification_receipt TEXT NOT NULL,
        quarantine_reason TEXT,
        last_health_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (node_kind IN ('remote-node', 'ollama', 'cloud')),
        CHECK (state IN ('registered', 'active', 'quarantined'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inference_node_latency_samples (
        sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        required_identity TEXT NOT NULL,
        latency_ms REAL NOT NULL,
        succeeded_at TEXT NOT NULL,
        FOREIGN KEY (node_id) REFERENCES inference_nodes(node_id) ON DELETE RESTRICT,
        CHECK (operation IN ('embedding', 'rerank'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inference_node_reservations (
        job_id TEXT NOT NULL,
        fencing_generation INTEGER NOT NULL,
        project_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, fencing_generation),
        FOREIGN KEY (node_id) REFERENCES inference_nodes(node_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inference_node_audit_events (
        event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        node_id TEXT NOT NULL,
        event_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (node_id) REFERENCES inference_nodes(node_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inference_node_identity_receipts (
        receipt_id TEXT PRIMARY KEY,
        node_id TEXT NOT NULL,
        config_revision TEXT NOT NULL,
        required_identity TEXT NOT NULL,
        observed_identity TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        FOREIGN KEY (node_id) REFERENCES inference_nodes(node_id) ON DELETE RESTRICT,
        UNIQUE(node_id, config_revision, required_identity, profile_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS derived_work_accelerator_audit_events (
        event_id TEXT PRIMARY KEY,
        event_kind TEXT NOT NULL CHECK(event_kind IN ('admission', 'scheduler')),
        task_kind TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('denied', 'deferred')),
        reason_code TEXT NOT NULL,
        day_utc TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(event_kind, task_kind, decision, reason_code, day_utc)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS inference_node_latency_idx
        ON inference_node_latency_samples
           (node_id, operation, required_identity, sample_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS inference_node_reservation_expiry_idx
        ON inference_node_reservations (lease_expires_at, node_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS derived_work_accelerator_audit_recent_idx
        ON derived_work_accelerator_audit_events (occurred_at DESC, event_id DESC)
    """,
)

# These tables are deliberately installed by the explicit deployment
# migration rather than by a read path.  Production readiness only trusts an
# attestation written by a separate verifier, while promotion work is
# durable even when the optional automation queue is disabled at runtime.
_PRODUCTION_READINESS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS production_evidence_attestations (
        attestation_id TEXT PRIMARY KEY,
        subject_path_sha256 TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL,
        issuer TEXT NOT NULL,
        signature TEXT NOT NULL,
        status TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_production_attestations_expiry
        ON production_evidence_attestations (status, expires_at)
    """,
)

_PROMOTION_TASK_DDL = (
    """
    CREATE TABLE IF NOT EXISTS memory_proposal_promotion_tasks (
        task_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        risk_tier TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 4,
        lease_token_hash TEXT NOT NULL DEFAULT '',
        fencing_generation INTEGER NOT NULL DEFAULT 0,
        lease_expires_at TEXT NOT NULL DEFAULT '',
        next_attempt_at TEXT NOT NULL,
        last_failure_code TEXT NOT NULL DEFAULT '',
        last_failure_detail TEXT NOT NULL DEFAULT '',
        memory_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, proposal_id, idempotency_key),
        CHECK(status IN ('queued', 'leased', 'retry_wait', 'completed', 'failed')),
        CHECK(risk_tier IN ('low', 'medium', 'high', 'critical')),
        CHECK(attempt_count >= 0 AND max_attempts >= 1),
        CHECK(project_id != 'project:unknown')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_promotion_tasks_claim
        ON memory_proposal_promotion_tasks(project_id, status, next_attempt_at, task_id)
    """,
)


def node_governance_schema_present(connection: sqlite3.Connection) -> bool:
    """Return whether the explicit node-governance schema is fully present."""

    if not isinstance(connection, sqlite3.Connection):
        return False
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.Error:
        return False
    names = {str(row[0]) for row in rows}
    if not _REQUIRED_TABLES.issubset(names):
        return False
    try:
        return all(
            columns.issubset(
                {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            )
            for table, columns in _REQUIRED_COLUMNS.items()
        )
    except sqlite3.Error:
        return False


def apply_node_governance_schema(connection: sqlite3.Connection) -> None:
    """Apply additive v2 DDL inside a caller-owned SQLite transaction."""

    if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
        raise ValueError("node_governance_migration_transaction_required")
    for statement in _DDL_STATEMENTS:
        connection.execute(statement)


def apply_production_readiness_schema(connection: sqlite3.Connection) -> None:
    """Install the additive tables used by production readiness and promotion.

    The caller owns the transaction and backup/lease protocol.  This function
    only emits idempotent DDL; it never inserts attestations or queue work.
    """

    if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
        raise ValueError("production_readiness_migration_transaction_required")
    for statement in (*_PRODUCTION_READINESS_DDL, *_PROMOTION_TASK_DDL):
        connection.execute(statement)


def apply_deployment_migrations(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Apply each known migration at most once in the current transaction."""

    if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
        raise ValueError("deployment_migration_transaction_required")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS deployment_migration_journal (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied_migrations: list[str] = []
    node_governance_applied = connection.execute(
        "SELECT 1 FROM deployment_migration_journal WHERE migration_id = ?",
        (NODE_GOVERNANCE_V2_MIGRATION_ID,),
    ).fetchone()
    if node_governance_applied is None:
        apply_node_governance_schema(connection)
        connection.execute(
            "INSERT INTO deployment_migration_journal (migration_id, applied_at) VALUES (?, ?)",
            (
                NODE_GOVERNANCE_V2_MIGRATION_ID,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            ),
        )
        applied_migrations.append(NODE_GOVERNANCE_V2_MIGRATION_ID)

    migration_journal_applied = connection.execute(
        "SELECT 1 FROM deployment_migration_journal WHERE migration_id = ?",
        (MIGRATION_EXECUTION_JOURNAL_MIGRATION_ID,),
    ).fetchone()
    # The v2 journal is additive.  It persists the exact phase/schema manifests
    # bound into each operation and repairs v1 tables only inside this explicit
    # deployment migration, never from runtime construction.
    if migration_journal_applied is None or not migration_journal_schema_present(connection):
        apply_migration_journal_schema(connection)
        if migration_journal_applied is None:
            connection.execute(
                "INSERT INTO deployment_migration_journal (migration_id, applied_at) VALUES (?, ?)",
                (
                    MIGRATION_EXECUTION_JOURNAL_MIGRATION_ID,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                ),
            )
            applied_migrations.append(MIGRATION_EXECUTION_JOURNAL_MIGRATION_ID)

    production_readiness_applied = connection.execute(
        "SELECT 1 FROM deployment_migration_journal WHERE migration_id = ?",
        (PRODUCTION_READINESS_SCHEMA_MIGRATION_ID,),
    ).fetchone()
    promotion_tasks_applied = connection.execute(
        "SELECT 1 FROM deployment_migration_journal WHERE migration_id = ?",
        (PROMOTION_TASK_SCHEMA_MIGRATION_ID,),
    ).fetchone()
    if production_readiness_applied is None or promotion_tasks_applied is None:
        apply_production_readiness_schema(connection)
        applied_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if production_readiness_applied is None:
            connection.execute(
                "INSERT INTO deployment_migration_journal (migration_id, applied_at) VALUES (?, ?)",
                (PRODUCTION_READINESS_SCHEMA_MIGRATION_ID, applied_at),
            )
            applied_migrations.append(PRODUCTION_READINESS_SCHEMA_MIGRATION_ID)
        if promotion_tasks_applied is None:
            connection.execute(
                "INSERT INTO deployment_migration_journal (migration_id, applied_at) VALUES (?, ?)",
                (PROMOTION_TASK_SCHEMA_MIGRATION_ID, applied_at),
            )
            applied_migrations.append(PROMOTION_TASK_SCHEMA_MIGRATION_ID)
    return tuple(applied_migrations)


__all__ = [
    "NODE_GOVERNANCE_SCHEMA_VERSION",
    "NODE_GOVERNANCE_V2_MIGRATION_ID",
    "MIGRATION_EXECUTION_JOURNAL_MIGRATION_ID",
    "PRODUCTION_READINESS_SCHEMA_MIGRATION_ID",
    "PROMOTION_TASK_SCHEMA_MIGRATION_ID",
    "apply_deployment_migrations",
    "apply_node_governance_schema",
    "apply_production_readiness_schema",
    "node_governance_schema_present",
]
