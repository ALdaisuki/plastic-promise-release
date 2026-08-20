"""Deployment-owned installation seam for the complete PR5 collaboration schema.

Runtime adapters are deliberately verify-only.  This module is the single
installation interface: it binds one canonical-backup receipt to the current
migration lease, installs every collaboration schema participant in one
caller-owned ``BEGIN IMMEDIATE`` transaction, verifies the compiled manifest,
and writes the installation receipt only after the full structure is valid.

The implementation delegates component DDL to the durable collaboration and
role-assignment modules where those facts already live.  Activity, acceptance,
coordinator, and coordination-plan runtime adapters remain verify-only; this
deployment module is their sole schema installer.  The small interface keeps
migration authority local while avoiding synonymous runtime tables.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from plastic_promise.collaboration.canonical_time import canonical_text, parse_utc, server_now
from plastic_promise.collaboration.durable_acceptance_store import (
    DURABLE_ACCEPTANCE_SCHEMA_REVISION,
    DurableAcceptanceAuthorityRepository,
)
from plastic_promise.collaboration.durable_activity_store import DurableActivityRepository
from plastic_promise.collaboration.durable_coordination_plan_store import (
    DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS,
    DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES,
    DURABLE_COORDINATION_PLAN_REQUIRED_TABLES,
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS,
    DURABLE_COORDINATION_PLAN_SCHEMA_DDL,
    DURABLE_COORDINATION_PLAN_SCHEMA_MANIFEST_SHA256,
    DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,
    DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_DDL,
    DurableCoordinationPlanRepository,
)
from plastic_promise.collaboration.durable_coordinator_store import (
    DURABLE_COORDINATOR_REQUIRED_INDEXES,
    DURABLE_COORDINATOR_REQUIRED_TABLES,
    DURABLE_COORDINATOR_REQUIRED_TRIGGERS,
    DURABLE_COORDINATOR_SCHEMA_REVISION,
    DurableCoordinatorRepository,
)
from plastic_promise.collaboration.durable_role_store import (
    _MIGRATION_SCHEMA_AUTHORITY,
    DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION,
    DurableRoleAssignmentRepository,
)
from plastic_promise.collaboration.durable_role_store import (
    install_schema as install_role_assignment_schema,
)
from plastic_promise.collaboration.durable_runtime import (
    DURABLE_COLLABORATION_REVISION,
    DurableCollaborationError,
    DurableCollaborationRuntime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager


COLLABORATION_SCHEMA_MIGRATION_VERSION = "plastic-promise-collaboration-schema-migration/v2"
COLLABORATION_SCHEMA_MANIFEST_VERSION = "plastic-promise-collaboration-schema-manifest/v2"
COLLABORATION_SCHEMA_INSTALL_RECEIPT_VERSION = (
    "plastic-promise-collaboration-schema-install-receipt/v1"
)
CANONICAL_BACKUP_MIGRATION_RECEIPT_VERSION = "plastic-promise-canonical-backup-migration-receipt/v1"
COLLABORATION_ACTIVITY_SCHEMA_REVISION = "collaboration-activity/sqlite-v1"
COLLABORATION_ACCEPTANCE_SCHEMA_REVISION = DURABLE_ACCEPTANCE_SCHEMA_REVISION
COLLABORATION_SCHEMA_INSTALL_PHASE = "collaboration-schema-install"

_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SAFE_REF = re.compile(r"\A[a-z][a-z0-9_.:/-]{1,255}\Z")
_RECEIPT_TOKEN = object()
_COORDINATION_PLAN_V1_REVISION = "collaboration-coordination-plan/sqlite-v1"
_COORDINATION_PLAN_BINDING_TABLE = "collaboration_coordination_plan_top_level_bindings"
_COLLABORATION_RUNTIME_V1_REVISION = "pr5-durable-lifecycle-v1"
_COLLABORATION_WORK_LEASE_TABLE = "collaboration_work_leases"


class CollaborationSchemaMigrationError(ValueError):
    """Stable, secret-free failure from the deployment schema seam."""

    def __init__(self, code: str) -> None:
        if _SAFE_REF.fullmatch(code) is None:
            raise ValueError("collaboration_schema_error_code_invalid")
        self.code = code
        super().__init__(code)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(payload: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()}"


def _require_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CollaborationSchemaMigrationError(code)
    return value


def _require_ref(value: object, code: str) -> str:
    if not isinstance(value, str) or _SAFE_REF.fullmatch(value) is None:
        raise CollaborationSchemaMigrationError(code)
    return value


def _require_generation(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CollaborationSchemaMigrationError(code)
    return value


def _canonical_timestamp(value: object, code: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CollaborationSchemaMigrationError(code)
        return canonical_text(value.astimezone(timezone.utc))
    if not isinstance(value, str):
        raise CollaborationSchemaMigrationError(code)
    try:
        parsed = parse_utc(value)
    except Exception as exc:
        raise CollaborationSchemaMigrationError(code) from exc
    rendered = canonical_text(parsed)
    if rendered != value:
        raise CollaborationSchemaMigrationError(code)
    return rendered


_ACTIVITY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS collaboration_activity_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_agent_activity (
        activity_update_sha256 TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        agent_session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL DEFAULT '',
        role_assignment_sha256 TEXT NOT NULL DEFAULT '',
        cursor INTEGER NOT NULL,
        update_json TEXT NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        UNIQUE(project_id, coordination_session_id, agent_session_id, work_item_id, cursor),
        CHECK(cursor >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_agent_activity_scope_cursor
        ON collaboration_agent_activity(
            project_id, coordination_session_id, agent_session_id, work_item_id, cursor
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_activity_audits (
        audit_receipt_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        activity_update_sha256 TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        agent_session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL DEFAULT '',
        cursor INTEGER NOT NULL,
        validated_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        FOREIGN KEY(activity_update_sha256)
            REFERENCES collaboration_agent_activity(activity_update_sha256) ON DELETE RESTRICT,
        CHECK(cursor >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_activity_audits_scope_cursor
        ON collaboration_activity_audits(
            project_id, coordination_session_id, agent_session_id, work_item_id, cursor
        )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_agent_activity_no_update
    BEFORE UPDATE ON collaboration_agent_activity
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_agent_activity_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_agent_activity_no_delete
    BEFORE DELETE ON collaboration_agent_activity
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_agent_activity_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_activity_audits_no_update
    BEFORE UPDATE ON collaboration_activity_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_activity_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_activity_audits_no_delete
    BEFORE DELETE ON collaboration_activity_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_activity_audit_append_only');
    END
    """,
)

_ACCEPTANCE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS collaboration_acceptance_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_review_receipts (
        review_receipt_sha256 TEXT PRIMARY KEY,
        review_receipt_id TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        work_receipt_sha256 TEXT NOT NULL,
        result_receipt_sha256 TEXT NOT NULL,
        reviewer_agent_session_id TEXT NOT NULL,
        reviewer_assignment_sha256 TEXT NOT NULL,
        review_policy_revision TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        review_channel TEXT NOT NULL,
        diff_digest TEXT NOT NULL,
        requirement_set_digest TEXT NOT NULL,
        union_contract_revision TEXT NOT NULL,
        decision TEXT NOT NULL,
        conflict_state TEXT NOT NULL,
        reviewed_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        UNIQUE(
            project_id, coordination_session_id, work_item_id,
            result_receipt_sha256, reviewer_assignment_sha256, review_channel
        ),
        CHECK(review_channel IN ('standards', 'spec', 'deepsec')),
        CHECK(decision IN ('accepted', 'rejected')),
        CHECK(conflict_state IN ('none', 'resolved', 'unresolved'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_review_receipts_scope
        ON collaboration_review_receipts(
            project_id, coordination_session_id, work_item_id, reviewed_at_utc
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_acceptance_receipts (
        acceptance_receipt_sha256 TEXT PRIMARY KEY,
        acceptance_receipt_id TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        work_receipt_id TEXT NOT NULL,
        work_receipt_sha256 TEXT NOT NULL,
        result_receipt_id TEXT NOT NULL,
        result_receipt_sha256 TEXT NOT NULL,
        review_receipt_id TEXT NOT NULL,
        review_receipt_sha256 TEXT NOT NULL,
        submitter_agent_session_id TEXT NOT NULL,
        submitter_agent_session_sha256 TEXT NOT NULL,
        reviewer_agent_session_id TEXT NOT NULL,
        reviewer_agent_session_sha256 TEXT NOT NULL,
        submitter_assignment_sha256 TEXT NOT NULL,
        reviewer_assignment_sha256 TEXT NOT NULL,
        assignment_policy_revision TEXT NOT NULL,
        review_policy_revision TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        diff_digest TEXT NOT NULL,
        requirement_set_digest TEXT NOT NULL,
        union_contract_revision TEXT NOT NULL,
        decision TEXT NOT NULL,
        conflict_state TEXT NOT NULL,
        issued_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        UNIQUE(project_id, coordination_session_id, work_item_id, result_receipt_sha256),
        FOREIGN KEY(review_receipt_sha256)
            REFERENCES collaboration_review_receipts(review_receipt_sha256) ON DELETE RESTRICT,
        CHECK(decision IN ('accepted', 'rejected')),
        CHECK(conflict_state IN ('none', 'resolved'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_acceptance_receipts_scope
        ON collaboration_acceptance_receipts(
            project_id, coordination_session_id, work_item_id, issued_at_utc
        )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_review_receipts_no_update
    BEFORE UPDATE ON collaboration_review_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_review_receipt_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_review_receipts_no_delete
    BEFORE DELETE ON collaboration_review_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_review_receipt_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_acceptance_receipts_no_update
    BEFORE UPDATE ON collaboration_acceptance_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_acceptance_receipt_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_acceptance_receipts_no_delete
    BEFORE DELETE ON collaboration_acceptance_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_acceptance_receipt_append_only');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_acceptance_review_bindings (
        acceptance_receipt_sha256 TEXT NOT NULL,
        review_channel TEXT NOT NULL,
        review_receipt_sha256 TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        diff_digest TEXT NOT NULL,
        requirement_set_digest TEXT NOT NULL,
        union_contract_revision TEXT NOT NULL,
        PRIMARY KEY(acceptance_receipt_sha256, review_channel),
        UNIQUE(acceptance_receipt_sha256, review_receipt_sha256),
        FOREIGN KEY(acceptance_receipt_sha256)
            REFERENCES collaboration_acceptance_receipts(acceptance_receipt_sha256)
            ON DELETE RESTRICT,
        FOREIGN KEY(review_receipt_sha256)
            REFERENCES collaboration_review_receipts(review_receipt_sha256)
            ON DELETE RESTRICT,
        CHECK(review_channel IN ('standards', 'spec', 'deepsec'))
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_acceptance_review_bindings_no_update
    BEFORE UPDATE ON collaboration_acceptance_review_bindings
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_acceptance_review_binding_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_acceptance_review_bindings_no_delete
    BEFORE DELETE ON collaboration_acceptance_review_bindings
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_acceptance_review_binding_append_only');
    END
    """,
)

_COORDINATOR_DDL = (
    """
    CREATE TABLE IF NOT EXISTS collaboration_coordinator_audit_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_coordinator_audits (
        coordinator_audit_receipt_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        authority_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        activity_update_sha256 TEXT NOT NULL,
        activity_receipt_sha256 TEXT NOT NULL,
        audit_generation INTEGER NOT NULL,
        status TEXT NOT NULL,
        completion_verified INTEGER NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        FOREIGN KEY(activity_receipt_sha256)
            REFERENCES collaboration_activity_audits(audit_receipt_sha256) ON DELETE RESTRICT,
        UNIQUE(project_id, coordination_session_id, activity_update_sha256, audit_generation),
        CHECK(audit_generation >= 1),
        CHECK(status IN ('verified', 'mismatch', 'overlap', 'stale', 'blocked')),
        CHECK(completion_verified IN (0, 1))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_coordinator_audits_scope_generation
        ON collaboration_coordinator_audits(
            project_id, coordination_session_id, activity_update_sha256, audit_generation
        )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_coordinator_audit_heads (
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        activity_update_sha256 TEXT NOT NULL,
        current_generation INTEGER NOT NULL,
        current_receipt_id TEXT NOT NULL UNIQUE,
        current_receipt_sha256 TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY(project_id, coordination_session_id, activity_update_sha256),
        FOREIGN KEY(current_receipt_id)
            REFERENCES collaboration_coordinator_audits(receipt_id) ON DELETE RESTRICT,
        CHECK(current_generation >= 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collaboration_coordinator_audit_consumptions (
        activity_update_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        receipt_sha256 TEXT NOT NULL,
        audit_generation INTEGER NOT NULL,
        consumed_at_utc TEXT NOT NULL,
        FOREIGN KEY(receipt_id)
            REFERENCES collaboration_coordinator_audits(receipt_id) ON DELETE RESTRICT,
        CHECK(audit_generation >= 1)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_coordinator_consumptions_receipt
        ON collaboration_coordinator_audit_consumptions(receipt_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_audits_no_update
    BEFORE UPDATE ON collaboration_coordinator_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_audits_no_delete
    BEFORE DELETE ON collaboration_coordinator_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_audit_heads_no_delete
    BEFORE DELETE ON collaboration_coordinator_audit_heads
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_no_delete');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_audit_heads_identity_immutable
    BEFORE UPDATE ON collaboration_coordinator_audit_heads
    WHEN OLD.project_id IS NOT NEW.project_id
      OR OLD.coordination_session_id IS NOT NEW.coordination_session_id
      OR OLD.activity_update_sha256 IS NOT NEW.activity_update_sha256
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_identity_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_audit_heads_generation_step
    BEFORE UPDATE ON collaboration_coordinator_audit_heads
    WHEN NEW.current_generation != OLD.current_generation + 1
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_generation_invalid');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_consumptions_no_update
    BEFORE UPDATE ON collaboration_coordinator_audit_consumptions
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_consumption_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_coordinator_consumptions_no_delete
    BEFORE DELETE ON collaboration_coordinator_audit_consumptions
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_consumption_append_only');
    END
    """,
)

_INSTALL_RECEIPT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS collaboration_schema_install_receipts (
        operation_id TEXT PRIMARY KEY,
        installation_ref TEXT NOT NULL,
        operation_ref TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        grant_id TEXT NOT NULL,
        fencing_generation INTEGER NOT NULL,
        phase_manifest_sha256 TEXT NOT NULL,
        schema_manifest_sha256 TEXT NOT NULL,
        backup_receipt_sha256 TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL UNIQUE,
        CHECK(fencing_generation >= 1)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_collaboration_schema_install_receipts_time
        ON collaboration_schema_install_receipts(installed_at_utc, operation_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_schema_install_receipts_no_update
    BEFORE UPDATE ON collaboration_schema_install_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_schema_install_receipt_append_only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS collaboration_schema_install_receipts_no_delete
    BEFORE DELETE ON collaboration_schema_install_receipts
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_schema_install_receipt_append_only');
    END
    """,
)

_OWN_DDL = (
    _ACTIVITY_DDL
    + _ACCEPTANCE_DDL
    + _COORDINATOR_DDL
    + DURABLE_COORDINATION_PLAN_SCHEMA_DDL
    + _INSTALL_RECEIPT_DDL
)

_BASE_TABLES = (
    "collaboration_runtime_schema",
    "collaboration_events",
    "collaboration_agents",
    "collaboration_agent_sessions",
    "collaboration_work_items",
    "collaboration_work_leases",
    "collaboration_results",
    "collaboration_cursors",
    "collaboration_event_retention",
    "collaboration_promotion_outbox",
)
_ROLE_TABLES = (
    "collaboration_role_assignment_schema",
    "collaboration_role_assignment_basis_snapshots",
    "collaboration_role_assignment_basis_current",
    "collaboration_role_assignment_receipts",
    "collaboration_role_assignment_bindings",
    "collaboration_role_assignment_revocations",
)
_OWN_TABLES = (
    "collaboration_activity_schema",
    "collaboration_agent_activity",
    "collaboration_activity_audits",
    "collaboration_acceptance_schema",
    "collaboration_review_receipts",
    "collaboration_acceptance_receipts",
    "collaboration_acceptance_review_bindings",
    *DURABLE_COORDINATOR_REQUIRED_TABLES,
    *DURABLE_COORDINATION_PLAN_REQUIRED_TABLES,
    "collaboration_schema_install_receipts",
)
_REQUIRED_INDEXES = (
    "idx_collaboration_events_scope_cursor",
    "idx_collaboration_events_parent",
    "idx_collaboration_sessions_scope",
    "idx_collaboration_work_scope_state",
    "idx_collaboration_leases_active",
    "idx_collaboration_leases_owner_session",
    "idx_collaboration_promotion_status",
    "idx_role_assignment_basis_scope",
    "idx_role_assignment_active_scope",
    "idx_collaboration_agent_activity_scope_cursor",
    "idx_collaboration_activity_audits_scope_cursor",
    "idx_collaboration_review_receipts_scope",
    "idx_collaboration_acceptance_receipts_scope",
    *DURABLE_COORDINATOR_REQUIRED_INDEXES,
    *DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES,
    "idx_collaboration_schema_install_receipts_time",
)
_REQUIRED_TRIGGERS = (
    "collaboration_events_no_update",
    "collaboration_events_no_delete",
    "collaboration_events_no_replace",
    "role_assignment_basis_snapshot_no_update",
    "role_assignment_basis_snapshot_no_delete",
    "role_assignment_receipt_no_update",
    "role_assignment_receipt_no_delete",
    "role_assignment_revocation_no_update",
    "role_assignment_revocation_no_delete",
    "role_assignment_bindings_immutable_identity",
    "role_assignment_bindings_no_delete",
    "collaboration_agent_activity_no_update",
    "collaboration_agent_activity_no_delete",
    "collaboration_activity_audits_no_update",
    "collaboration_activity_audits_no_delete",
    "collaboration_review_receipts_no_update",
    "collaboration_review_receipts_no_delete",
    "collaboration_acceptance_receipts_no_update",
    "collaboration_acceptance_receipts_no_delete",
    "collaboration_acceptance_review_bindings_no_update",
    "collaboration_acceptance_review_bindings_no_delete",
    *DURABLE_COORDINATOR_REQUIRED_TRIGGERS,
    *DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS,
    "collaboration_schema_install_receipts_no_update",
    "collaboration_schema_install_receipts_no_delete",
)

_REQUIRED_OWN_COLUMNS: dict[str, frozenset[str]] = {
    "collaboration_activity_schema": frozenset(
        {"singleton", "schema_revision", "installed_at_utc"}
    ),
    "collaboration_agent_activity": frozenset(
        {
            "activity_update_sha256",
            "project_id",
            "coordination_session_id",
            "agent_session_id",
            "agent_id",
            "work_item_id",
            "role_assignment_sha256",
            "cursor",
            "update_json",
            "recorded_at_utc",
        }
    ),
    "collaboration_activity_audits": frozenset(
        {
            "audit_receipt_sha256",
            "receipt_id",
            "activity_update_sha256",
            "project_id",
            "coordination_session_id",
            "agent_session_id",
            "agent_id",
            "work_item_id",
            "cursor",
            "validated_at_utc",
            "receipt_json",
        }
    ),
    "collaboration_acceptance_schema": frozenset(
        {"singleton", "schema_revision", "installed_at_utc"}
    ),
    "collaboration_review_receipts": frozenset(
        {
            "review_receipt_sha256",
            "review_receipt_id",
            "project_id",
            "coordination_session_id",
            "work_item_id",
            "work_receipt_sha256",
            "result_receipt_sha256",
            "reviewer_agent_session_id",
            "reviewer_assignment_sha256",
            "review_policy_revision",
            "source_revision",
            "review_channel",
            "diff_digest",
            "requirement_set_digest",
            "union_contract_revision",
            "decision",
            "conflict_state",
            "reviewed_at_utc",
            "receipt_json",
        }
    ),
    "collaboration_acceptance_receipts": frozenset(
        {
            "acceptance_receipt_sha256",
            "acceptance_receipt_id",
            "project_id",
            "coordination_session_id",
            "work_item_id",
            "work_receipt_id",
            "work_receipt_sha256",
            "result_receipt_id",
            "result_receipt_sha256",
            "review_receipt_id",
            "review_receipt_sha256",
            "submitter_agent_session_id",
            "submitter_agent_session_sha256",
            "reviewer_agent_session_id",
            "reviewer_agent_session_sha256",
            "submitter_assignment_sha256",
            "reviewer_assignment_sha256",
            "assignment_policy_revision",
            "review_policy_revision",
            "source_revision",
            "diff_digest",
            "requirement_set_digest",
            "union_contract_revision",
            "decision",
            "conflict_state",
            "issued_at_utc",
            "receipt_json",
        }
    ),
    "collaboration_acceptance_review_bindings": frozenset(
        {
            "acceptance_receipt_sha256",
            "review_channel",
            "review_receipt_sha256",
            "source_revision",
            "diff_digest",
            "requirement_set_digest",
            "union_contract_revision",
        }
    ),
    "collaboration_coordinator_audit_schema": frozenset(
        {"singleton", "schema_revision", "installed_at_utc"}
    ),
    "collaboration_coordinator_audits": frozenset(
        {
            "coordinator_audit_receipt_sha256",
            "receipt_id",
            "authority_id",
            "project_id",
            "coordination_session_id",
            "activity_update_sha256",
            "activity_receipt_sha256",
            "audit_generation",
            "status",
            "completion_verified",
            "recorded_at_utc",
            "receipt_json",
        }
    ),
    "collaboration_coordinator_audit_heads": frozenset(
        {
            "project_id",
            "coordination_session_id",
            "activity_update_sha256",
            "current_generation",
            "current_receipt_id",
            "current_receipt_sha256",
            "updated_at_utc",
        }
    ),
    "collaboration_coordinator_audit_consumptions": frozenset(
        {
            "activity_update_sha256",
            "receipt_id",
            "receipt_sha256",
            "audit_generation",
            "consumed_at_utc",
        }
    ),
    **{
        table: frozenset(columns)
        for table, columns in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS.items()
    },
    "collaboration_schema_install_receipts": frozenset(
        {
            "operation_id",
            "installation_ref",
            "operation_ref",
            "plan_hash",
            "grant_id",
            "fencing_generation",
            "phase_manifest_sha256",
            "schema_manifest_sha256",
            "backup_receipt_sha256",
            "installed_at_utc",
            "receipt_json",
            "receipt_sha256",
        }
    ),
}


def _ddl_manifest_entries(statements: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for statement in statements:
        compact = " ".join(statement.split())
        match = re.search(
            r"CREATE (?:TABLE|INDEX|TRIGGER)(?: IF NOT EXISTS)? ([a-z0-9_]+)",
            compact,
            re.IGNORECASE,
        )
        if match is None:
            raise RuntimeError("collaboration_schema_manifest_ddl_unrecognized")
        result.append(f"ddl:{match.group(1).lower()}:{_sha256(compact)}")
    return tuple(result)


COLLABORATION_SCHEMA_MANIFEST: tuple[str, ...] = (
    f"manifest:{COLLABORATION_SCHEMA_MANIFEST_VERSION.replace('/', ':')}",
    f"component:durable-collaboration:{DURABLE_COLLABORATION_REVISION}",
    f"component:durable-role-assignment:{DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION.replace('/', ':')}",
    f"component:durable-acceptance:{DURABLE_ACCEPTANCE_SCHEMA_REVISION.replace('/', ':')}",
    f"component:durable-coordinator:{DURABLE_COORDINATOR_SCHEMA_REVISION.replace('/', ':')}",
    f"component:durable-coordination-plan:{DURABLE_COORDINATION_PLAN_SCHEMA_REVISION.replace('/', ':')}",
    f"component:durable-coordination-plan-manifest:{DURABLE_COORDINATION_PLAN_SCHEMA_MANIFEST_SHA256}",
    *(f"table:{name}" for name in _BASE_TABLES + _ROLE_TABLES),
    *_ddl_manifest_entries(_OWN_DDL),
)
COLLABORATION_SCHEMA_MANIFEST_SHA256 = _sha256(list(COLLABORATION_SCHEMA_MANIFEST))


@dataclass(frozen=True, slots=True, init=False)
class CanonicalBackupMigrationReceipt:
    """Typed proof that phase 3 completed under the exact migration fence."""

    operation_id: str
    installation_ref: str
    operation_ref: str
    plan_hash: str
    grant_id: str
    fencing_generation: int
    phase_manifest_sha256: str
    schema_manifest_sha256: str
    backup_receipt_sha256: str
    completed_at_utc: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationSchemaMigrationError("canonical_backup_receipt_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        binding: Mapping[str, object],
        backup_receipt_sha256: str,
        completed_at_utc: str,
        _token: object,
    ) -> CanonicalBackupMigrationReceipt:
        if _token is not _RECEIPT_TOKEN:
            raise CollaborationSchemaMigrationError("canonical_backup_receipt_factory_required")
        instance = object.__new__(cls)
        values = {
            **binding,
            "backup_receipt_sha256": _require_digest(
                backup_receipt_sha256,
                "canonical_backup_receipt_digest_invalid",
            ),
            "completed_at_utc": _canonical_timestamp(
                completed_at_utc,
                "canonical_backup_receipt_timestamp_invalid",
            ),
        }
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        instance.validate_integrity()
        return instance

    def validate_integrity(self) -> None:
        _validate_binding(self.to_dict())
        _require_digest(self.backup_receipt_sha256, "canonical_backup_receipt_digest_invalid")
        _canonical_timestamp(
            self.completed_at_utc,
            "canonical_backup_receipt_timestamp_invalid",
        )

    def validate_for(self, context: object) -> None:
        self.validate_integrity()
        if _execution_binding(context) != _receipt_binding(self):
            raise CollaborationSchemaMigrationError("canonical_backup_receipt_binding_mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CANONICAL_BACKUP_MIGRATION_RECEIPT_VERSION,
            **_receipt_binding(self),
            "backup_receipt_sha256": self.backup_receipt_sha256,
            "completed_at_utc": self.completed_at_utc,
        }

    @property
    def receipt_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True, init=False)
class CollaborationSchemaInstallReceipt:
    """Immutable deployment receipt for one fully verified schema install."""

    operation_id: str
    installation_ref: str
    operation_ref: str
    plan_hash: str
    grant_id: str
    fencing_generation: int
    phase_manifest_sha256: str
    schema_manifest_sha256: str
    backup_receipt_sha256: str
    installed_at_utc: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationSchemaMigrationError("collaboration_schema_receipt_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        binding: Mapping[str, object],
        backup_receipt_sha256: str,
        installed_at_utc: str,
        _token: object,
    ) -> CollaborationSchemaInstallReceipt:
        if _token is not _RECEIPT_TOKEN:
            raise CollaborationSchemaMigrationError("collaboration_schema_receipt_factory_required")
        instance = object.__new__(cls)
        values = {
            **binding,
            "backup_receipt_sha256": _require_digest(
                backup_receipt_sha256,
                "collaboration_schema_backup_receipt_invalid",
            ),
            "installed_at_utc": _canonical_timestamp(
                installed_at_utc,
                "collaboration_schema_receipt_timestamp_invalid",
            ),
        }
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        instance.validate_integrity()
        return instance

    def validate_integrity(self) -> None:
        _validate_binding(self.to_dict())
        _require_digest(
            self.backup_receipt_sha256,
            "collaboration_schema_backup_receipt_invalid",
        )
        _canonical_timestamp(
            self.installed_at_utc,
            "collaboration_schema_receipt_timestamp_invalid",
        )

    def validate_for(
        self,
        context: object,
        backup_receipt: CanonicalBackupMigrationReceipt,
    ) -> None:
        self.validate_integrity()
        backup_receipt.validate_for(context)
        if _execution_binding(context) != _receipt_binding(self):
            raise CollaborationSchemaMigrationError("collaboration_schema_receipt_binding_mismatch")
        if self.backup_receipt_sha256 != backup_receipt.backup_receipt_sha256:
            raise CollaborationSchemaMigrationError("collaboration_schema_backup_receipt_mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_SCHEMA_INSTALL_RECEIPT_VERSION,
            **_receipt_binding(self),
            "backup_receipt_sha256": self.backup_receipt_sha256,
            "installed_at_utc": self.installed_at_utc,
        }

    @property
    def receipt_sha256(self) -> str:
        return _sha256(self.to_dict())


def _receipt_binding(receipt: object) -> dict[str, object]:
    return {
        "operation_id": getattr(receipt, "operation_id", None),
        "installation_ref": getattr(receipt, "installation_ref", None),
        "operation_ref": getattr(receipt, "operation_ref", None),
        "plan_hash": getattr(receipt, "plan_hash", None),
        "grant_id": getattr(receipt, "grant_id", None),
        "fencing_generation": getattr(receipt, "fencing_generation", None),
        "phase_manifest_sha256": getattr(receipt, "phase_manifest_sha256", None),
        "schema_manifest_sha256": getattr(receipt, "schema_manifest_sha256", None),
    }


def _validate_binding(binding: Mapping[str, object]) -> dict[str, object]:
    validated = {
        "operation_id": _require_ref(binding.get("operation_id"), "schema_operation_id_invalid"),
        "installation_ref": _require_ref(
            binding.get("installation_ref"),
            "schema_installation_ref_invalid",
        ),
        "operation_ref": _require_ref(
            binding.get("operation_ref"),
            "schema_operation_ref_invalid",
        ),
        "plan_hash": _require_digest(binding.get("plan_hash"), "schema_plan_hash_invalid"),
        "grant_id": _require_ref(binding.get("grant_id"), "schema_grant_id_invalid"),
        "fencing_generation": _require_generation(
            binding.get("fencing_generation"),
            "schema_fencing_generation_invalid",
        ),
        "phase_manifest_sha256": _require_digest(
            binding.get("phase_manifest_sha256"),
            "schema_phase_manifest_digest_invalid",
        ),
        "schema_manifest_sha256": _require_digest(
            binding.get("schema_manifest_sha256"),
            "schema_manifest_digest_invalid",
        ),
    }
    if validated["schema_manifest_sha256"] != COLLABORATION_SCHEMA_MANIFEST_SHA256:
        raise CollaborationSchemaMigrationError("schema_manifest_digest_mismatch")
    return validated


def _execution_binding(context: object) -> dict[str, object]:
    plan = getattr(context, "plan", None)
    lease = getattr(context, "lease", None)
    if plan is None or lease is None:
        raise CollaborationSchemaMigrationError("schema_execution_context_invalid")
    phase_manifest = tuple(getattr(plan, "phase_manifest", ()) or ())
    schema_manifest = tuple(getattr(plan, "schema_manifest", ()) or ())
    if not phase_manifest or not schema_manifest:
        raise CollaborationSchemaMigrationError("schema_execution_manifest_missing")
    if schema_manifest != COLLABORATION_SCHEMA_MANIFEST:
        raise CollaborationSchemaMigrationError("schema_manifest_mismatch")
    binding = {
        "operation_id": getattr(lease, "operation_id", None),
        "installation_ref": getattr(plan, "installation_ref", None),
        "operation_ref": getattr(plan, "operation_ref", None),
        "plan_hash": getattr(plan, "plan_hash", None),
        "grant_id": getattr(lease, "grant_id", None),
        "fencing_generation": getattr(lease, "fencing_generation", None),
        "phase_manifest_sha256": getattr(plan, "phase_manifest_sha256", None),
        "schema_manifest_sha256": getattr(plan, "schema_manifest_sha256", None),
    }
    validated = _validate_binding(binding)
    if getattr(lease, "plan_hash", None) != validated["plan_hash"]:
        raise CollaborationSchemaMigrationError("schema_execution_plan_mismatch")
    if getattr(lease, "phase_manifest_sha256", None) != validated["phase_manifest_sha256"]:
        raise CollaborationSchemaMigrationError("schema_execution_phase_manifest_mismatch")
    if getattr(lease, "schema_manifest_sha256", None) != validated["schema_manifest_sha256"]:
        raise CollaborationSchemaMigrationError("schema_execution_schema_manifest_mismatch")
    return validated


def bind_canonical_backup_receipt(
    context: object,
    *,
    backup_receipt_sha256: str,
    completed_at: datetime | str,
) -> CanonicalBackupMigrationReceipt:
    """Bind canonical backup evidence to one live typed execution context."""

    return CanonicalBackupMigrationReceipt._issue(
        binding=_execution_binding(context),
        backup_receipt_sha256=backup_receipt_sha256,
        completed_at_utc=_canonical_timestamp(
            completed_at,
            "canonical_backup_receipt_timestamp_invalid",
        ),
        _token=_RECEIPT_TOKEN,
    )


@contextmanager
def _borrowed_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        raise CollaborationSchemaMigrationError("collaboration_schema_transaction_required")
    yield


class CollaborationSchemaMigration:
    """Deep deployment module for installing and verifying the PR5 schema."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise CollaborationSchemaMigrationError("collaboration_schema_connection_invalid")
        if not callable(transaction_factory):
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_transaction_factory_invalid"
            )
        if clock is not None and not callable(clock):
            raise CollaborationSchemaMigrationError("collaboration_schema_clock_invalid")
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def schema_manifest(self) -> tuple[str, ...]:
        return COLLABORATION_SCHEMA_MANIFEST

    @property
    def schema_manifest_sha256(self) -> str:
        return COLLABORATION_SCHEMA_MANIFEST_SHA256

    def install(
        self,
        context: object,
        backup_receipt: CanonicalBackupMigrationReceipt,
    ) -> CollaborationSchemaInstallReceipt:
        """Install and verify every schema participant in one caller transaction."""

        if not isinstance(backup_receipt, CanonicalBackupMigrationReceipt):
            raise CollaborationSchemaMigrationError("canonical_backup_receipt_required")
        # PR5 durable adapters require connection-scoped referential
        # integrity. Set this on the deployment-owned canonical connection
        # before BEGIN IMMEDIATE; runtime adapters only verify it later.
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise CollaborationSchemaMigrationError(
                    "collaboration_schema_foreign_keys_required"
                )
        except CollaborationSchemaMigrationError:
            raise
        except Exception as exc:
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_foreign_keys_required"
            ) from exc
        binding = _execution_binding(context)
        backup_receipt.validate_for(context)
        existing = self._existing_install_receipt(str(binding["operation_id"]))
        if existing is not None:
            existing.validate_for(context, backup_receipt)
            self._verify_structure(marker_required=True)
            self._verify_install_receipt(existing)
            return existing
        if self._connection.in_transaction:
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_outer_transaction_forbidden"
            )
        try:
            installed_at = canonical_text(server_now(self._clock))
        except Exception as exc:
            raise CollaborationSchemaMigrationError("collaboration_schema_clock_invalid") from exc
        receipt = CollaborationSchemaInstallReceipt._issue(
            binding=binding,
            backup_receipt_sha256=backup_receipt.backup_receipt_sha256,
            installed_at_utc=installed_at,
            _token=_RECEIPT_TOKEN,
        )
        try:
            with self._transaction_factory():
                if not self._connection.in_transaction:
                    raise CollaborationSchemaMigrationError(
                        "collaboration_schema_begin_immediate_required"
                    )
                self._install_component_schemas(binding, backup_receipt, installed_at)
                self._verify_structure(marker_required=False)
                self._write_component_markers(installed_at)
                self._verify_structure(marker_required=True)
                self._append_install_receipt(receipt)
                self._verify_install_receipt(receipt)
        except CollaborationSchemaMigrationError:
            raise
        except Exception as exc:
            raise CollaborationSchemaMigrationError("collaboration_schema_install_failed") from exc
        if self._connection.in_transaction:
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_transaction_not_committed"
            )
        persisted = self.verify(operation_id=receipt.operation_id)
        if persisted != receipt:
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_restart_verification_failed"
            )
        return receipt

    def _existing_install_receipt(
        self,
        operation_id: str,
    ) -> CollaborationSchemaInstallReceipt | None:
        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='collaboration_schema_install_receipts'"
        ).fetchone()
        if table is None:
            return None
        row = self._connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM collaboration_schema_install_receipts "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        receipt = _receipt_from_json(str(row[0]))
        if receipt.receipt_sha256 != str(row[1]):
            raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_stale")
        return receipt

    def verify(self, *, operation_id: str | None = None) -> CollaborationSchemaInstallReceipt:
        """Verify the compiled schema and return its persisted typed receipt."""

        self._verify_structure(marker_required=True)
        parameters: tuple[object, ...] = ()
        where = ""
        if operation_id is not None:
            where = "WHERE operation_id=?"
            parameters = (_require_ref(operation_id, "schema_operation_id_invalid"),)
        row = self._connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM collaboration_schema_install_receipts "
            f"{where} ORDER BY installed_at_utc DESC, operation_id DESC LIMIT 1",
            parameters,
        ).fetchone()
        if row is None:
            raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_missing")
        receipt = _receipt_from_json(str(row[0]))
        if receipt.receipt_sha256 != str(row[1]):
            raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_stale")
        self._verify_install_receipt(receipt)
        return receipt

    def _install_component_schemas(
        self,
        binding: Mapping[str, object],
        backup_receipt: CanonicalBackupMigrationReceipt,
        installed_at: str,
    ) -> None:
        runtime_installer = object.__new__(DurableCollaborationRuntime)
        runtime_installer._connection = self._connection
        runtime_installer._clock = self._clock
        self._upgrade_collaboration_runtime_v1_to_v2()
        runtime_installer._install_schema_components(
            grant_id=str(binding["grant_id"]),
            plan_revision=COLLABORATION_SCHEMA_MIGRATION_VERSION,
            backup_receipt_sha256=backup_receipt.backup_receipt_sha256,
        )
        install_role_assignment_schema(
            self._connection,
            transaction_factory=lambda: _borrowed_transaction(self._connection),
            clock=self._clock,
            _migration_authority=_MIGRATION_SCHEMA_AUTHORITY,
        )
        self._upgrade_coordination_plan_schema_v1_to_v2()
        for statement in _OWN_DDL:
            self._connection.execute(statement)
        # ``installed_at`` is intentionally unused until every table/index/
        # trigger is verified; component markers are written in the next step.
        del installed_at

    def _upgrade_collaboration_runtime_v1_to_v2(self) -> None:
        """Bind legacy Agent leases to one exact durable AgentSession.

        The v1 table stored only ``owner_id``.  Backfill is permitted only
        when the canonical session registry proves exactly one matching
        session in the lease's project and coordination scope.  Zero or
        multiple candidates are not guessed: the deployment transaction
        aborts and leaves the v1 schema untouched.
        """

        marker_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collaboration_runtime_schema'"
        ).fetchone()
        lease_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_COLLABORATION_WORK_LEASE_TABLE,),
        ).fetchone()
        if marker_table is None or lease_table is None:
            return

        marker = self._connection.execute(
            "SELECT schema_revision FROM collaboration_runtime_schema WHERE singleton=1"
        ).fetchone()
        if marker is None:
            return
        revision = str(marker[0])
        columns = tuple(
            str(row[1])
            for row in self._connection.execute(
                f"PRAGMA table_info({_COLLABORATION_WORK_LEASE_TABLE})"
            ).fetchall()
        )
        if "owner_session_id" in columns:
            if revision == DURABLE_COLLABORATION_REVISION:
                return
            raise CollaborationSchemaMigrationError(
                "collaboration_runtime_upgrade_revision_unsupported"
            )
        if revision != _COLLABORATION_RUNTIME_V1_REVISION:
            raise CollaborationSchemaMigrationError(
                "collaboration_runtime_upgrade_revision_unsupported"
            )

        self._connection.execute(
            f"ALTER TABLE {_COLLABORATION_WORK_LEASE_TABLE} "
            "ADD COLUMN owner_session_id TEXT NOT NULL DEFAULT ''"
        )
        rows = self._connection.execute(
            f"SELECT lease_id,project_id,coordination_session_id,owner_id "
            f"FROM {_COLLABORATION_WORK_LEASE_TABLE} ORDER BY lease_id"
        ).fetchall()
        for lease_id, project_id, coordination_session_id, owner_id in rows:
            candidates = self._connection.execute(
                "SELECT session_id FROM collaboration_agent_sessions "
                "WHERE project_id=? AND coordination_session_id=? AND agent_id=? "
                "ORDER BY session_id",
                (project_id, coordination_session_id, owner_id),
            ).fetchall()
            if len(candidates) != 1:
                raise CollaborationSchemaMigrationError(
                    "collaboration_runtime_upgrade_lease_owner_ambiguous"
                )
            self._connection.execute(
                f"UPDATE {_COLLABORATION_WORK_LEASE_TABLE} "
                "SET owner_session_id=? WHERE lease_id=? AND owner_session_id=''",
                (str(candidates[0][0]), str(lease_id)),
            )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_collaboration_leases_owner_session "
            f"ON {_COLLABORATION_WORK_LEASE_TABLE}("
            "project_id, coordination_session_id, owner_id, owner_session_id, state)"
        )

    def _upgrade_coordination_plan_schema_v1_to_v2(self) -> None:
        """Add mandate lineage to the only coordination-plan v1 table that changed.

        The upgrade runs inside the caller's existing ``BEGIN IMMEDIATE``
        transaction.  It never issues a new Top-Level Agent binding: the
        immutable v1 JSON already contains ``mandate_sha256`` and is copied
        byte-for-byte with its original digest and timestamps.  Missing or
        inconsistent evidence aborts the whole migration.
        """

        marker_table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='collaboration_coordination_plan_schema'"
        ).fetchone()
        if marker_table is None:
            # Fresh installations have not created the marker yet.  The
            # current v2 DDL is installed by the caller later in this same
            # transaction, and the marker is written only after the full
            # structure has been verified.
            return

        marker = self._connection.execute(
            "SELECT schema_revision FROM collaboration_coordination_plan_schema WHERE singleton=1"
        ).fetchone()
        if marker is None:
            return
        revision = str(marker[0])
        if revision == DURABLE_COORDINATION_PLAN_SCHEMA_REVISION:
            return
        if revision != _COORDINATION_PLAN_V1_REVISION:
            raise CollaborationSchemaMigrationError(
                "collaboration_coordination_plan_upgrade_revision_unsupported"
            )
        columns = tuple(
            str(row[1])
            for row in self._connection.execute(
                f"PRAGMA table_info({_COORDINATION_PLAN_BINDING_TABLE})"
            ).fetchall()
        )
        expected_v1 = tuple(
            column
            for column in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS[
                _COORDINATION_PLAN_BINDING_TABLE
            ]
            if column != "mandate_sha256"
        )
        if columns != expected_v1:
            raise CollaborationSchemaMigrationError(
                "collaboration_coordination_plan_upgrade_schema_stale"
            )

        rows = self._connection.execute(
            f"SELECT * FROM {_COORDINATION_PLAN_BINDING_TABLE} "
            "ORDER BY project_id, coordination_session_id"
        ).fetchall()
        migrated: list[tuple[object, ...]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[7]))
            except (TypeError, ValueError) as exc:
                raise CollaborationSchemaMigrationError(
                    "collaboration_coordination_plan_upgrade_binding_invalid"
                ) from exc
            if (
                not isinstance(payload, dict)
                or _canonical_json(payload) != str(row[7])
                or set(payload)
                != {
                    "schema_version",
                    "project_id",
                    "coordination_session_id",
                    "top_level_agent_session_id",
                    "top_level_agent_id",
                    "agent_session_sha256",
                    "mandate_sha256",
                    "binding_generation",
                    "bound_at_utc",
                    "authority_effect",
                }
                or payload.get("schema_version") != "collaboration-top-level-agent-binding/v1"
                or payload.get("project_id") != row[0]
                or payload.get("coordination_session_id") != row[1]
                or payload.get("top_level_agent_session_id") != row[2]
                or payload.get("top_level_agent_id") != row[3]
                or payload.get("agent_session_sha256") != row[4]
                or payload.get("binding_generation") != row[5]
                or payload.get("bound_at_utc") != row[6]
                or payload.get("authority_effect") != "server-repository-required"
                or _sha256(payload) != row[8]
            ):
                raise CollaborationSchemaMigrationError(
                    "collaboration_coordination_plan_upgrade_binding_invalid"
                )
            mandate_sha256 = payload.get("mandate_sha256")
            _require_digest(
                mandate_sha256,
                "collaboration_coordination_plan_upgrade_binding_invalid",
            )
            migrated.append(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    mandate_sha256,
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                )
            )

        for trigger in DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[:2]:
            self._connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in (DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[0],):
            self._connection.execute(f"DROP INDEX IF EXISTS {index}")
        self._connection.execute(
            f"ALTER TABLE {_COORDINATION_PLAN_BINDING_TABLE} RENAME TO "
            "collaboration_coordination_plan_top_level_bindings_v1"
        )
        self._connection.execute(DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_DDL)
        self._connection.executemany(
            f"""
            INSERT INTO {_COORDINATION_PLAN_BINDING_TABLE} (
                project_id, coordination_session_id,
                top_level_agent_session_id, top_level_agent_id,
                agent_session_sha256, mandate_sha256,
                binding_generation, bound_at_utc,
                binding_json, binding_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            migrated,
        )
        self._connection.execute("DROP TABLE collaboration_coordination_plan_top_level_bindings_v1")

    def _write_component_markers(self, installed_at: str) -> None:
        self._connection.execute(
            """
            INSERT INTO collaboration_activity_schema(singleton, schema_revision, installed_at_utc)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_revision=excluded.schema_revision,
                installed_at_utc=excluded.installed_at_utc
            """,
            (COLLABORATION_ACTIVITY_SCHEMA_REVISION, installed_at),
        )
        self._connection.execute(
            """
            INSERT INTO collaboration_acceptance_schema(singleton, schema_revision, installed_at_utc)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_revision=excluded.schema_revision,
                installed_at_utc=excluded.installed_at_utc
            """,
            (COLLABORATION_ACCEPTANCE_SCHEMA_REVISION, installed_at),
        )
        self._connection.execute(
            """
            INSERT INTO collaboration_coordinator_audit_schema(
                singleton, schema_revision, installed_at_utc
            )
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_revision=excluded.schema_revision,
                installed_at_utc=excluded.installed_at_utc
            """,
            (DURABLE_COORDINATOR_SCHEMA_REVISION, installed_at),
        )
        self._connection.execute(
            """
            INSERT INTO collaboration_coordination_plan_schema(
                singleton, schema_revision, installed_at_utc
            ) VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_revision=excluded.schema_revision,
                installed_at_utc=excluded.installed_at_utc
            """,
            (DURABLE_COORDINATION_PLAN_SCHEMA_REVISION, installed_at),
        )

    def _append_install_receipt(self, receipt: CollaborationSchemaInstallReceipt) -> None:
        payload = _canonical_json(receipt.to_dict())
        existing = self._connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM collaboration_schema_install_receipts "
            "WHERE operation_id=?",
            (receipt.operation_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != payload or str(existing[1]) != receipt.receipt_sha256:
                raise CollaborationSchemaMigrationError(
                    "collaboration_schema_install_receipt_conflict"
                )
            return
        self._connection.execute(
            """
            INSERT INTO collaboration_schema_install_receipts(
                operation_id, installation_ref, operation_ref, plan_hash, grant_id,
                fencing_generation, phase_manifest_sha256, schema_manifest_sha256,
                backup_receipt_sha256, installed_at_utc, receipt_json, receipt_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.operation_id,
                receipt.installation_ref,
                receipt.operation_ref,
                receipt.plan_hash,
                receipt.grant_id,
                receipt.fencing_generation,
                receipt.phase_manifest_sha256,
                receipt.schema_manifest_sha256,
                receipt.backup_receipt_sha256,
                receipt.installed_at_utc,
                payload,
                receipt.receipt_sha256,
            ),
        )

    def _verify_structure(self, *, marker_required: bool) -> None:
        try:
            runtime = object.__new__(DurableCollaborationRuntime)
            runtime._connection = self._connection
            runtime._verify_schema()
            DurableRoleAssignmentRepository(
                self._connection,
                transaction_factory=lambda: _borrowed_transaction(self._connection),
                clock=self._clock,
            )
        except (DurableCollaborationError, Exception) as exc:
            # Re-raise this module's own errors unchanged; component exceptions
            # are intentionally normalized at the deployment seam.
            if isinstance(exc, CollaborationSchemaMigrationError):
                raise
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_component_verification_failed"
            ) from exc
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not set(_BASE_TABLES + _ROLE_TABLES + _OWN_TABLES).issubset(tables):
            raise CollaborationSchemaMigrationError("collaboration_schema_table_missing")
        for table, required in _REQUIRED_OWN_COLUMNS.items():
            actual = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required.issubset(actual):
                raise CollaborationSchemaMigrationError("collaboration_schema_column_missing")
        indexes = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        if not set(_REQUIRED_INDEXES).issubset(indexes):
            raise CollaborationSchemaMigrationError("collaboration_schema_index_missing")
        trigger_rows = self._connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        triggers = {str(row[0]): str(row[1] or "").casefold() for row in trigger_rows}
        if not set(_REQUIRED_TRIGGERS).issubset(triggers):
            raise CollaborationSchemaMigrationError("collaboration_schema_trigger_missing")
        if any("raise(abort" not in triggers[name] for name in _REQUIRED_TRIGGERS):
            raise CollaborationSchemaMigrationError("collaboration_schema_trigger_stale")
        if not marker_required:
            return
        markers = (
            (
                "collaboration_activity_schema",
                COLLABORATION_ACTIVITY_SCHEMA_REVISION,
            ),
            (
                "collaboration_acceptance_schema",
                COLLABORATION_ACCEPTANCE_SCHEMA_REVISION,
            ),
            (
                "collaboration_coordinator_audit_schema",
                DURABLE_COORDINATOR_SCHEMA_REVISION,
            ),
            (
                "collaboration_coordination_plan_schema",
                DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,
            ),
        )
        for table, revision in markers:
            row = self._connection.execute(
                f"SELECT schema_revision, installed_at_utc FROM {table} WHERE singleton=1"
            ).fetchone()
            if row is None or str(row[0]) != revision:
                raise CollaborationSchemaMigrationError("collaboration_schema_marker_stale")
            _canonical_timestamp(row[1], "collaboration_schema_marker_timestamp_invalid")
        try:
            DurableAcceptanceAuthorityRepository(
                self._connection,
                transaction_factory=lambda: _borrowed_transaction(self._connection),
            )
            activity_repository = DurableActivityRepository(
                self._connection,
                transaction_factory=lambda: _borrowed_transaction(self._connection),
            )
            DurableCoordinatorRepository(
                self._connection,
                activity_repository=activity_repository,
                transaction_factory=lambda: _borrowed_transaction(self._connection),
            )
            DurableCoordinationPlanRepository(
                self._connection,
                transaction_factory=lambda: _borrowed_transaction(self._connection),
                clock=self._clock,
            )
        except Exception as exc:
            raise CollaborationSchemaMigrationError(
                "collaboration_schema_component_verification_failed"
            ) from exc

    def _verify_install_receipt(self, receipt: CollaborationSchemaInstallReceipt) -> None:
        receipt.validate_integrity()
        row = self._connection.execute(
            "SELECT * FROM collaboration_schema_install_receipts WHERE operation_id=?",
            (receipt.operation_id,),
        ).fetchone()
        if row is None:
            raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_missing")
        names = [
            str(item[1])
            for item in self._connection.execute(
                "PRAGMA table_info(collaboration_schema_install_receipts)"
            ).fetchall()
        ]
        values = dict(zip(names, row, strict=True))
        expected = {
            **_receipt_binding(receipt),
            "backup_receipt_sha256": receipt.backup_receipt_sha256,
            "installed_at_utc": receipt.installed_at_utc,
            "receipt_json": _canonical_json(receipt.to_dict()),
            "receipt_sha256": receipt.receipt_sha256,
        }
        if any(values.get(name) != value for name, value in expected.items()):
            raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_stale")


def _receipt_from_json(payload: str) -> CollaborationSchemaInstallReceipt:
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CollaborationSchemaMigrationError(
            "collaboration_schema_install_receipt_stale"
        ) from exc
    if not isinstance(data, dict):
        raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_stale")
    if data.get("schema_version") != COLLABORATION_SCHEMA_INSTALL_RECEIPT_VERSION:
        raise CollaborationSchemaMigrationError("collaboration_schema_install_receipt_stale")
    binding = _validate_binding(data)
    return CollaborationSchemaInstallReceipt._issue(
        binding=binding,
        backup_receipt_sha256=str(data.get("backup_receipt_sha256", "")),
        installed_at_utc=str(data.get("installed_at_utc", "")),
        _token=_RECEIPT_TOKEN,
    )


def collaboration_schema_present(connection: sqlite3.Connection) -> bool:
    """Return whether the complete manifest and a valid install receipt exist."""

    if not isinstance(connection, sqlite3.Connection):
        return False
    try:
        # The transaction factory is intentionally unusable for this read-only
        # probe; ``verify`` never enters it.
        verifier = CollaborationSchemaMigration(
            connection,
            transaction_factory=lambda: _borrowed_transaction(connection),
        )
        verifier.verify()
    except Exception:
        return False
    return True


__all__ = [
    "CANONICAL_BACKUP_MIGRATION_RECEIPT_VERSION",
    "COLLABORATION_ACCEPTANCE_SCHEMA_REVISION",
    "COLLABORATION_ACTIVITY_SCHEMA_REVISION",
    "COLLABORATION_SCHEMA_INSTALL_PHASE",
    "COLLABORATION_SCHEMA_INSTALL_RECEIPT_VERSION",
    "COLLABORATION_SCHEMA_MANIFEST",
    "COLLABORATION_SCHEMA_MANIFEST_SHA256",
    "COLLABORATION_SCHEMA_MANIFEST_VERSION",
    "COLLABORATION_SCHEMA_MIGRATION_VERSION",
    "DURABLE_COORDINATION_PLAN_SCHEMA_REVISION",
    "DURABLE_COORDINATOR_SCHEMA_REVISION",
    "CanonicalBackupMigrationReceipt",
    "CollaborationSchemaInstallReceipt",
    "CollaborationSchemaMigration",
    "CollaborationSchemaMigrationError",
    "bind_canonical_backup_receipt",
    "collaboration_schema_present",
]
