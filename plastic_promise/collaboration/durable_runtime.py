"""Durable server-owned collaboration lifecycle for PR5.

This module is deliberately an adapter behind the collaboration contracts.  It
does not make edge/compute callers authoritative and it never adopts or
updates canonical memory.  The supplied SQLite connection is the single
writer owned by ``pp-server-backend/pp-core``; all lifecycle state is stored in
that connection so a fresh adapter instance can recover it after restart.

The adapter is intentionally narrow.  It provides the durable seam that MCP,
Hook, and Maintenance handlers can call, while leaving transport/auth policy
and migration/LanceDB orchestration to their respective PR5 adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import TYPE_CHECKING, Any

from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
    trusted_memory_origin,
)

from .acceptance_receipt import (
    AcceptanceReceipt,
    AcceptanceReceiptAuthority,
    ServerAcceptanceSourceRegistry,
)
from .canonical_time import canonical_text, parse_utc, server_now, server_now_text
from .contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    EventCursor,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from .event_log import CollaborationEventLog
from .lease_contract import LeaseHeartbeat, WorkItem, WorkLease
from .passive_bridge import PromotionCandidate
from .policy_binding import AGENT_POLICY_REVISION, AgentPolicyBindingAuthority
from .role_assignment import (
    RESULT_SUBMISSION_USE,
    WORK_SUBMITTER_ROLE,
    RoleAssignmentAuthority,
    RoleAssignmentBasis,
    RoleAssignmentRepository,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


DURABLE_COLLABORATION_SCHEMA = "collaboration-runtime/sqlite-v2"
DURABLE_COLLABORATION_REVISION = "pr5-durable-lifecycle-v2"

_WORK_STATES = frozenset(
    {
        "proposed",
        "ready",
        "leased",
        "in_progress",
        "submitted",
        "reviewing",
        "accepted",
        "rework",
        "expired",
    }
)
_WORK_INITIAL_STATES = frozenset({"proposed", "ready"})
_WORK_CLAIM_TARGET_STATES = frozenset({"leased", "in_progress"})
_WORK_RESULT_TARGET_STATES = frozenset({"submitted"})
_WORK_CLAIMABLE_STATES = frozenset({"proposed", "ready", "rework"})
_WORK_RESULT_SOURCE_STATES = frozenset({"leased", "in_progress"})
_AGENT_SESSION_STATES = frozenset({"registered", "active", "idle", "stale", "closed"})
_ACTIVE_AGENT_SESSION_STATES = frozenset({"active"})
_HEARTBEAT_AGENT_SESSION_STATES = frozenset({"active", "idle"})
_WORKFLOW_STAGE_LIFECYCLES = frozenset({"started", "blocked"})
_WORKFLOW_STAGE_BLOCK_REASONS = frozenset(
    {
        "skill_execution_failed",
        "composite_tracking_failed",
        "workflow_transition_conflict",
        "stage_finalization_failed",
        "stage_execution_cancelled",
    }
)

# ``step-closure`` does not accept a caller-selected workflow stage.  Formal
# result submission is therefore issued under one server-owned implementation
# stage.  The role-assignment authority still validates the work/session/lease
# binding and the stage is part of the durable intent event, but it cannot be
# forged by an MCP payload.
_RESULT_SUBMISSION_WORKFLOW_STAGE = "implement"

_SESSION_INIT_EVENT_LIMIT = 20
_SESSION_INIT_WORK_LIMIT = 32
_SESSION_INIT_OBJECTIVE_CHARS = 512


class DurableCollaborationError(CollaborationContractError):
    """Stable, content-free failure from the durable lifecycle adapter."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DurableCollaborationError(code)
    return result


def _bounded_state(value: object, allowed: frozenset[str], code: str) -> str:
    state = str(value or "").strip().casefold()
    if state not in allowed:
        raise DurableCollaborationError(code)
    return state


def _workflow_attempt_id(
    *,
    agent_session_id: str,
    project_id: str,
    coordination_session_id: str,
    execution_receipt_id: str,
    route_id: str,
    stage: str,
    step_index: int,
) -> str:
    """Derive one server-stable identity for a workflow execution attempt."""

    digest = hashlib.sha256(
        _canonical_json(
            {
                "agent_session_id": agent_session_id,
                "project_id": project_id,
                "coordination_session_id": coordination_session_id,
                "execution_receipt_id": execution_receipt_id,
                "route_id": route_id,
                "stage": stage,
                "step_index": step_index,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"workflow-attempt:{digest}"


def _workflow_stage_event_id(lifecycle: str, attempt_id: str) -> str:
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:40]
    return f"event:workflow-stage-{lifecycle}:{digest}"


def _identity_from_dict(value: object) -> AgentIdentity:
    if not isinstance(value, Mapping):
        raise DurableCollaborationError("agent_identity_projection_invalid")
    capabilities = value.get("capabilities", ())
    if not isinstance(capabilities, (list, tuple)):
        raise DurableCollaborationError("agent_identity_projection_invalid")
    return AgentIdentity(
        agent_id=str(value.get("agent_id") or ""),
        role=str(value.get("role") or ""),
        parent_agent_id=(
            None if value.get("parent_agent_id") is None else str(value["parent_agent_id"])
        ),
        capabilities=tuple(str(item) for item in capabilities),
    )


def _work_receipt_from_projection(value: object) -> WorkReceipt:
    """Rebuild a stored WorkReceipt without trusting caller-supplied fields."""

    if not isinstance(value, Mapping):
        raise DurableCollaborationError("work_receipt_projection_invalid")
    assigned = value.get("assigned_agent")
    if not isinstance(assigned, Mapping):
        raise DurableCollaborationError("work_receipt_projection_invalid")
    try:
        return WorkReceipt(
            receipt_id=str(value.get("receipt_id") or ""),
            work_item_id=str(value.get("work_item_id") or ""),
            project=ProjectScope(str(value.get("project_id") or "")),
            coordination_session_id=str(value.get("coordination_session_id") or ""),
            assigned_agent=_identity_from_dict(assigned),
            objective=str(value.get("objective") or ""),
            fencing_generation=int(value.get("fencing_generation") or 0),
            issued_at=str(value.get("issued_at") or ""),
            expires_at=str(value.get("expires_at") or ""),
            dependency_work_ids=tuple(
                str(item) for item in (value.get("dependency_work_ids") or ())
            ),
        )
    except (TypeError, ValueError, CollaborationContractError) as exc:
        raise DurableCollaborationError("work_receipt_projection_invalid") from exc


def _work_item_from_projection(value: object) -> WorkItem:
    if not isinstance(value, Mapping):
        raise DurableCollaborationError("work_item_projection_invalid")
    try:
        return WorkItem(
            work_item_id=str(value.get("work_item_id") or ""),
            project=ProjectScope(str(value.get("project_id") or "")),
            owner_kind=str(value.get("owner_kind") or ""),
            policy_kind=str(value.get("policy_kind") or ""),
            operation_kind=str(value.get("operation_kind") or ""),
            input_sha256=str(value.get("input_sha256") or ""),
            result_schema=str(value.get("result_schema") or ""),
            created_at=str(value.get("created_at") or ""),
            max_attempts=int(value.get("max_attempts") or 0),
            coordination_session_id=(
                None
                if value.get("coordination_session_id") is None
                else str(value.get("coordination_session_id") or "")
            ),
        )
    except (TypeError, ValueError, CollaborationContractError) as exc:
        raise DurableCollaborationError("work_item_projection_invalid") from exc


def _work_lease_from_projection(value: object) -> WorkLease:
    if not isinstance(value, Mapping):
        raise DurableCollaborationError("work_lease_projection_invalid")
    raw_identity = value.get("owner_identity")
    owner_identity = None if raw_identity is None else _identity_from_dict(raw_identity)
    try:
        return WorkLease(
            lease_id=str(value.get("lease_id") or ""),
            work_item=_work_item_from_projection(value.get("work_item")),
            owner_kind=str(value.get("owner_kind") or ""),
            policy_kind=str(value.get("policy_kind") or ""),
            owner_id=str(value.get("owner_id") or ""),
            fencing_generation=int(value.get("fencing_generation") or 0),
            attempt=int(value.get("attempt") or 0),
            issued_at=str(value.get("issued_at") or ""),
            expires_at=str(value.get("expires_at") or ""),
            result_binding_sha256=str(value.get("result_binding_sha256") or ""),
            idempotency_key_sha256=str(value.get("idempotency_key_sha256") or ""),
            owner_identity=owner_identity,
        )
    except (TypeError, ValueError, CollaborationContractError) as exc:
        raise DurableCollaborationError("work_lease_projection_invalid") from exc


def _agent_session_from_projection(value: object) -> AgentSession:
    if not isinstance(value, Mapping):
        raise DurableCollaborationError("agent_session_projection_invalid")
    try:
        return AgentSession(
            session_id=str(value.get("session_id") or ""),
            identity=_identity_from_dict(value.get("identity")),
            project=ProjectScope(str(value.get("project_id") or "")),
            coordination_session_id=str(value.get("coordination_session_id") or ""),
            state=str(value.get("state") or ""),
            started_at=str(value.get("started_at") or ""),
            last_heartbeat_at=str(value.get("last_heartbeat_at") or ""),
            expires_at=(
                None if value.get("expires_at") is None else str(value.get("expires_at") or "")
            ),
        )
    except (TypeError, ValueError, CollaborationContractError) as exc:
        raise DurableCollaborationError("agent_session_projection_invalid") from exc


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    raw = cursor.fetchone()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))


def _write_boundary(method):
    """Apply the injected server single-writer transaction to one method."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._write():
            return method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class SessionInitResult:
    """Bound server response returned by the session-init adapter."""

    session: AgentSession
    policy: Mapping[str, object]
    working_set_summary: Mapping[str, object]
    cursor: EventCursor
    next_cursor: EventCursor
    source_head_sequence: int
    cursor_has_more: bool
    assigned_work: tuple[Mapping[str, object], ...]
    peer_delta: tuple[Mapping[str, object], ...] = ()
    state: str = "active"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "session-init-response-v2",
            "session": self.session.to_dict(),
            "policy": dict(self.policy),
            "working_set_summary": dict(self.working_set_summary),
            "cursor": {
                "stored": self.cursor.to_dict(),
                "next": self.next_cursor.to_dict(),
                "source_head_sequence": self.source_head_sequence,
                "has_more": self.cursor_has_more,
                "ack_required": self.next_cursor.sequence > self.cursor.sequence,
            },
            "assigned_work": [dict(item) for item in self.assigned_work],
            "peer_delta": [dict(item) for item in self.peer_delta],
            "state": self.state,
            "authority": "pp-server-backend/pp-core",
            "persistent": True,
        }


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of one server-clocked presence/lease/retention reconcile."""

    stale_session_ids: tuple[str, ...] = ()
    abandoned_lease_ids: tuple[str, ...] = ()
    expired_work_ids: tuple[str, ...] = ()
    retained_event_ids: tuple[str, ...] = ()
    retried_promotion_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "collaboration-reconcile/v1",
            "stale_session_ids": list(self.stale_session_ids),
            "abandoned_lease_ids": list(self.abandoned_lease_ids),
            "expired_work_ids": list(self.expired_work_ids),
            "retained_event_ids": list(self.retained_event_ids),
            "retried_promotion_ids": list(self.retried_promotion_ids),
        }


@dataclass(frozen=True, slots=True)
class PendingPromotionResult:
    """Outcome of the accepted-work to pending-proposal adapter."""

    status: str
    candidate_id: str
    proposal_id: str | None = None
    reason: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "collaboration-promotion-result/v1",
            "status": self.status,
            "candidate_id": self.candidate_id,
            "proposal_id": self.proposal_id,
            "reason": self.reason,
            "attempts": self.attempts,
            "canonical_memory_effect": "none",
            "adoption": "separate-governed-action",
        }


class DurableCollaborationRuntime:
    """SQLite-backed AgentRegistry, ProjectWorkBoard, and lifecycle adapter.

    ``connection`` must be the server's canonical single-writer connection.
    The adapter does not open a second connection and does not expose the
    connection to edge/compute callers.  ``clock`` is injected for deterministic
    tests but is expected to be the server UTC clock in production.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] | None = None,
        policy_authority: AgentPolicyBindingAuthority | None = None,
        role_assignment_authority: RoleAssignmentAuthority | None = None,
        role_assignment_repository: RoleAssignmentRepository | None = None,
        acceptance_authority: AcceptanceReceiptAuthority | None = None,
        acceptance_source_registry: ServerAcceptanceSourceRegistry | None = None,
        policy_revision: str = AGENT_POLICY_REVISION,
        presence_timeout_seconds: int = 120,
        event_retention_seconds: int = 7 * 24 * 60 * 60,
        lease_grace_seconds: int = 0,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise DurableCollaborationError("durable_connection_invalid")
        if not callable(transaction_factory):
            raise DurableCollaborationError("durable_writer_required")
        if policy_authority is not None and not isinstance(
            policy_authority,
            AgentPolicyBindingAuthority,
        ):
            raise DurableCollaborationError("durable_policy_authority_invalid")
        if role_assignment_authority is not None and not isinstance(
            role_assignment_authority,
            RoleAssignmentAuthority,
        ):
            raise DurableCollaborationError("durable_role_assignment_authority_invalid")
        if role_assignment_repository is not None and not isinstance(
            role_assignment_repository,
            RoleAssignmentRepository,
        ):
            raise DurableCollaborationError("durable_role_assignment_repository_invalid")
        if (role_assignment_authority is None) != (role_assignment_repository is None):
            raise DurableCollaborationError("durable_role_assignment_composition_incomplete")
        if acceptance_authority is not None and not isinstance(
            acceptance_authority,
            AcceptanceReceiptAuthority,
        ):
            raise DurableCollaborationError("durable_acceptance_authority_invalid")
        if (
            acceptance_source_registry is not None
            and type(acceptance_source_registry) is not ServerAcceptanceSourceRegistry
        ):
            raise DurableCollaborationError("durable_acceptance_source_registry_invalid")
        if str(policy_revision or "").strip() != AGENT_POLICY_REVISION:
            raise DurableCollaborationError("durable_policy_revision_not_server_current")
        if (
            isinstance(presence_timeout_seconds, bool)
            or not isinstance(presence_timeout_seconds, int)
            or presence_timeout_seconds <= 0
        ):
            raise DurableCollaborationError("presence_timeout_invalid")
        if (
            isinstance(event_retention_seconds, bool)
            or not isinstance(event_retention_seconds, int)
            or event_retention_seconds <= 0
        ):
            raise DurableCollaborationError("event_retention_invalid")
        if (
            isinstance(lease_grace_seconds, bool)
            or not isinstance(lease_grace_seconds, int)
            or lease_grace_seconds < 0
        ):
            raise DurableCollaborationError("lease_grace_invalid")
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._policy_authority = policy_authority
        self._role_assignment_authority = role_assignment_authority
        self._role_assignment_repository = role_assignment_repository
        self._acceptance_authority = acceptance_authority
        self._acceptance_source_registry = acceptance_source_registry
        self._policy_revision = _text(policy_revision, "policy_revision_required")
        self._presence_timeout_seconds = presence_timeout_seconds
        self._event_retention_seconds = event_retention_seconds
        self._lease_grace_seconds = lease_grace_seconds
        self._verify_schema()

    @property
    def role_assignment_authority(self) -> RoleAssignmentAuthority | None:
        """Return the exact server-owned authority injected at composition.

        The handle is process-local and non-serializable.  It is exposed only
        so trusted server adapters can prove that role issuance, acceptance,
        and durable lifecycle checks share one authority instance; callers
        cannot construct or replace it through an MCP payload.
        """

        return self._role_assignment_authority

    @property
    def role_assignment_repository(self) -> RoleAssignmentRepository | None:
        """Return the paired server-owned role-assignment repository seam."""

        return self._role_assignment_repository

    def _fetchone(self, sql: str, parameters: tuple[object, ...] = ()) -> dict[str, Any] | None:
        return _row(self._connection.execute(sql, parameters))

    def _fetchall(self, sql: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        return _rows(self._connection.execute(sql, parameters))

    def _write(self) -> AbstractContextManager[None]:
        return self._transaction_factory()

    def _has_table(self, name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            is not None
        )

    def _now(self) -> tuple[datetime, str]:
        try:
            now = server_now(self._clock)
        except ValueError as exc:
            raise DurableCollaborationError("server_clock_invalid") from exc
        return now, canonical_text(now)

    def _install_schema_components(
        self,
        *,
        grant_id: str,
        plan_revision: str,
        backup_receipt_sha256: str,
    ) -> None:
        """Install base lifecycle DDL for the deployment-owned schema module.

        This is deliberately private. Runtime construction is verify-only;
        the complete transaction, typed backup receipt, manifest validation,
        component markers, and installation receipt belong exclusively to
        ``deployment.CollaborationSchemaMigration``.
        """

        grant_id = _text(grant_id, "schema_grant_id_required")
        plan_revision = _text(plan_revision, "schema_plan_revision_required")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", backup_receipt_sha256) is None:
            raise DurableCollaborationError("schema_backup_receipt_invalid")

        statements = (
            # The append-only event source is part of the same explicit PR5
            # migration.  ``CollaborationEventLog`` remains a read/append
            # adapter, but runtime construction must fail closed when this
            # schema has not been installed by the migration grant.
            """
            CREATE TABLE IF NOT EXISTS collaboration_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                actor_agent_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                causal_parent_event_id TEXT,
                audience_roles_json TEXT NOT NULL DEFAULT '[]',
                audience_agent_ids_json TEXT NOT NULL DEFAULT '[]',
                event_json TEXT NOT NULL,
                event_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_events_scope_cursor
                ON collaboration_events(project_id, coordination_session_id, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_events_parent
                ON collaboration_events(causal_parent_event_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_update
            BEFORE UPDATE ON collaboration_events
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_delete
            BEFORE DELETE ON collaboration_events
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_replace
            BEFORE INSERT ON collaboration_events
            WHEN EXISTS (
                SELECT 1
                  FROM collaboration_events
                 WHERE event_id = NEW.event_id
                    OR (NEW.sequence > 0 AND sequence = NEW.sequence)
            )
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_runtime_schema (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_revision TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                plan_revision TEXT NOT NULL,
                backup_receipt_sha256 TEXT NOT NULL,
                installed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_agents (
                project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                parent_agent_id TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                identity_json TEXT NOT NULL,
                policy_json TEXT NOT NULL DEFAULT '{}',
                policy_revision TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'registered',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, agent_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_agent_sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                session_json TEXT NOT NULL,
                session_sha256 TEXT NOT NULL,
                policy_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                expires_at TEXT,
                closed_at TEXT NOT NULL DEFAULT '',
                cursor_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_sessions_scope
                ON collaboration_agent_sessions(project_id, coordination_session_id, state)
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_work_items (
                work_item_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                work_receipt_json TEXT NOT NULL,
                work_receipt_sha256 TEXT NOT NULL,
                assigned_agent_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'proposed',
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_work_scope_state
                ON collaboration_work_items(project_id, coordination_session_id, state)
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_work_leases (
                lease_id TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                owner_kind TEXT NOT NULL,
                policy_kind TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                lease_json TEXT NOT NULL,
                lease_sha256 TEXT NOT NULL,
                fencing_generation INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                heartbeat_sequence INTEGER NOT NULL DEFAULT 0,
                last_heartbeat_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                released_at TEXT NOT NULL DEFAULT '',
                release_reason TEXT NOT NULL DEFAULT '',
                UNIQUE(work_item_id, fencing_generation)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_leases_active
                ON collaboration_work_leases(project_id, state, expires_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_leases_owner_session
                ON collaboration_work_leases(
                    project_id, coordination_session_id, owner_id, owner_session_id, state
                )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_results (
                receipt_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_cursors (
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                sequence INTEGER NOT NULL DEFAULT 0,
                source_head_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, coordination_session_id, consumer_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_event_retention (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                retention_state TEXT NOT NULL DEFAULT 'retained',
                cleaned_at TEXT NOT NULL DEFAULT '',
                audit_digest TEXT NOT NULL,
                UNIQUE(project_id, coordination_session_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS collaboration_promotion_outbox (
                candidate_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                candidate_sha256 TEXT NOT NULL,
                idempotency_sha256 TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                proposal_id TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_promotion_status
                ON collaboration_promotion_outbox(status, updated_at)
            """,
        )
        for statement in statements:
            self._connection.execute(statement)
        installed_at = server_now_text(self._clock)
        self._connection.execute(
            """
            INSERT INTO collaboration_runtime_schema (
                singleton, schema_revision, grant_id, plan_revision,
                backup_receipt_sha256, installed_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_revision=excluded.schema_revision,
                grant_id=excluded.grant_id,
                plan_revision=excluded.plan_revision,
                backup_receipt_sha256=excluded.backup_receipt_sha256,
                installed_at=excluded.installed_at
            """,
            (
                DURABLE_COLLABORATION_REVISION,
                grant_id,
                plan_revision,
                backup_receipt_sha256,
                installed_at,
            ),
        )

    def _verify_schema(self) -> None:
        required = {
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
        }
        existing = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not required.issubset(existing):
            raise DurableCollaborationError("durable_collaboration_schema_missing")
        required_columns: dict[str, frozenset[str]] = {
            "collaboration_runtime_schema": frozenset(
                {
                    "singleton",
                    "schema_revision",
                    "grant_id",
                    "plan_revision",
                    "backup_receipt_sha256",
                    "installed_at",
                }
            ),
            "collaboration_events": frozenset(
                {
                    "sequence",
                    "event_id",
                    "project_id",
                    "coordination_session_id",
                    "actor_agent_id",
                    "actor_role",
                    "event_type",
                    "causal_parent_event_id",
                    "audience_roles_json",
                    "audience_agent_ids_json",
                    "event_json",
                    "event_sha256",
                    "created_at",
                    "expires_at",
                }
            ),
            "collaboration_agents": frozenset(
                {
                    "project_id",
                    "agent_id",
                    "role",
                    "parent_agent_id",
                    "capabilities_json",
                    "identity_json",
                    "policy_json",
                    "policy_revision",
                    "state",
                    "first_seen_at",
                    "last_seen_at",
                    "updated_at",
                }
            ),
            "collaboration_agent_sessions": frozenset(
                {
                    "session_id",
                    "project_id",
                    "agent_id",
                    "coordination_session_id",
                    "identity_json",
                    "session_json",
                    "session_sha256",
                    "policy_json",
                    "state",
                    "started_at",
                    "last_heartbeat_at",
                    "expires_at",
                    "closed_at",
                    "cursor_sequence",
                    "updated_at",
                }
            ),
            "collaboration_work_items": frozenset(
                {
                    "work_item_id",
                    "project_id",
                    "coordination_session_id",
                    "work_receipt_json",
                    "work_receipt_sha256",
                    "assigned_agent_id",
                    "state",
                    "attempt",
                    "max_attempts",
                    "created_at",
                    "updated_at",
                    "last_error",
                }
            ),
            "collaboration_work_leases": frozenset(
                {
                    "lease_id",
                    "work_item_id",
                    "project_id",
                    "coordination_session_id",
                    "owner_kind",
                    "policy_kind",
                    "owner_id",
                    "owner_session_id",
                    "lease_json",
                    "lease_sha256",
                    "fencing_generation",
                    "attempt",
                    "issued_at",
                    "expires_at",
                    "heartbeat_sequence",
                    "last_heartbeat_at",
                    "state",
                    "released_at",
                    "release_reason",
                }
            ),
            "collaboration_results": frozenset(
                {
                    "receipt_id",
                    "project_id",
                    "coordination_session_id",
                    "work_item_id",
                    "result_json",
                    "result_sha256",
                    "submitted_at",
                    "outcome",
                    "created_at",
                }
            ),
            "collaboration_cursors": frozenset(
                {
                    "project_id",
                    "coordination_session_id",
                    "consumer_id",
                    "sequence",
                    "source_head_sequence",
                    "updated_at",
                }
            ),
            "collaboration_event_retention": frozenset(
                {
                    "event_id",
                    "project_id",
                    "coordination_session_id",
                    "sequence",
                    "expires_at",
                    "retention_state",
                    "cleaned_at",
                    "audit_digest",
                }
            ),
            "collaboration_promotion_outbox": frozenset(
                {
                    "candidate_id",
                    "project_id",
                    "candidate_json",
                    "candidate_sha256",
                    "idempotency_sha256",
                    "status",
                    "proposal_id",
                    "attempts",
                    "failure_reason",
                    "created_at",
                    "updated_at",
                }
            ),
        }
        for table, columns in required_columns.items():
            actual = {
                str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            if not columns.issubset(actual):
                raise DurableCollaborationError("durable_collaboration_schema_stale")
        expected_indexes = {
            "idx_collaboration_events_scope_cursor",
            "idx_collaboration_events_parent",
            "idx_collaboration_sessions_scope",
            "idx_collaboration_work_scope_state",
            "idx_collaboration_leases_active",
            "idx_collaboration_leases_owner_session",
            "idx_collaboration_promotion_status",
        }
        actual_indexes = {
            str(row[1])
            for table in required
            for row in self._connection.execute(f"PRAGMA index_list({table})")
        }
        if not expected_indexes.issubset(actual_indexes):
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        trigger_rows = self._connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        trigger_sql = {str(row[0]): str(row[1] or "").lower() for row in trigger_rows}
        for trigger_name in (
            "collaboration_events_no_update",
            "collaboration_events_no_delete",
            "collaboration_events_no_replace",
        ):
            if trigger_name not in trigger_sql:
                raise DurableCollaborationError("durable_collaboration_schema_stale")
        if "raise(abort" not in trigger_sql["collaboration_events_no_update"]:
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        if "raise(abort" not in trigger_sql["collaboration_events_no_delete"]:
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        if "raise(abort" not in trigger_sql["collaboration_events_no_replace"]:
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        revision = self._connection.execute(
            "SELECT schema_revision, grant_id, plan_revision, backup_receipt_sha256, installed_at "
            "FROM collaboration_runtime_schema WHERE singleton=1"
        ).fetchone()
        if revision is None or str(revision[0]) != DURABLE_COLLABORATION_REVISION:
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        if not str(revision[1]).strip() or not str(revision[2]).strip():
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(revision[3])):
            raise DurableCollaborationError("durable_collaboration_schema_stale")
        try:
            parse_utc(str(revision[4]))
        except Exception as exc:
            raise DurableCollaborationError("durable_collaboration_schema_stale") from exc

    def _require_agent_session(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_id: str,
        identity: AgentIdentity | None = None,
        session_id: str | None = None,
        allowed_states: frozenset[str] = _ACTIVE_AGENT_SESSION_STATES,
    ) -> dict[str, Any]:
        """Resolve one exact server-owned Agent session and its policy binding.

        ``AgentIdentity`` is a public declaration and therefore is not an
        authority on its own.  Every state-changing caller reaches this one
        seam while holding the server writer transaction.  The seam resolves
        ambiguity, distinguishes terminal lifecycle states, and validates the
        exact session-owned policy projection so transport adapters do not
        each invent a weaker version of Agent authority.
        """

        project_id = _text(project_id, "work_project_required")
        coordination_session_id = _text(
            coordination_session_id,
            "coordination_session_required",
        )
        agent_id = _text(agent_id, "agent_id_required")
        if not isinstance(allowed_states, frozenset) or not allowed_states:
            raise DurableCollaborationError("agent_session_state_requirement_invalid")
        if not allowed_states.issubset(_AGENT_SESSION_STATES):
            raise DurableCollaborationError("agent_session_state_requirement_invalid")
        sql = """
            SELECT sessions.*, agents.state AS agent_state,
                   agents.policy_revision AS agent_policy_revision,
                   agents.policy_json AS agent_policy_json,
                   agents.identity_json AS agent_identity_json
              FROM collaboration_agent_sessions AS sessions
              JOIN collaboration_agents AS agents
                ON agents.project_id = sessions.project_id
               AND agents.agent_id = sessions.agent_id
             WHERE sessions.project_id=?
               AND sessions.coordination_session_id=?
               AND sessions.agent_id=?
        """
        parameters: tuple[object, ...] = (project_id, coordination_session_id, agent_id)
        if session_id is not None:
            session_id = _text(session_id, "session_id_required")
            sql += " AND sessions.session_id=?"
            parameters += (session_id,)
        sql += " ORDER BY sessions.updated_at DESC, sessions.session_id"
        rows = self._fetchall(sql, parameters)
        if not rows:
            raise DurableCollaborationError("agent_session_not_registered")
        if session_id is None:
            eligible = [row for row in rows if str(row["state"]) in allowed_states]
            if len(eligible) > 1:
                raise DurableCollaborationError("agent_session_ambiguous")
            row = eligible[0] if eligible else rows[0]
        else:
            row = rows[0]
        state = _bounded_state(
            row["state"],
            _AGENT_SESSION_STATES,
            "agent_session_state_corrupt",
        )
        if state == "stale":
            raise DurableCollaborationError("agent_session_stale")
        if state == "closed":
            raise DurableCollaborationError("agent_session_closed")
        if state not in allowed_states:
            raise DurableCollaborationError("agent_session_not_active")
        agent_state = _bounded_state(
            row["agent_state"],
            _AGENT_SESSION_STATES,
            "agent_registry_state_corrupt",
        )
        if agent_state == "stale":
            raise DurableCollaborationError("agent_session_stale")
        if agent_state == "closed":
            raise DurableCollaborationError("agent_session_closed")
        if agent_state not in {"active", "idle"}:
            raise DurableCollaborationError("agent_session_not_active")
        if str(row["agent_policy_revision"]) != self._policy_revision:
            raise DurableCollaborationError("agent_session_policy_unbound")
        try:
            agent_policy = json.loads(str(row["agent_policy_json"]))
            session_policy = json.loads(str(row["policy_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("agent_session_policy_corrupt") from exc
        if not isinstance(agent_policy, Mapping) or not isinstance(session_policy, Mapping):
            raise DurableCollaborationError("agent_session_policy_corrupt")
        if str(agent_policy.get("policy_revision") or "") != self._policy_revision:
            raise DurableCollaborationError("agent_session_policy_unbound")
        if str(session_policy.get("policy_revision") or "") != self._policy_revision:
            raise DurableCollaborationError("agent_session_policy_unbound")
        expected_binding = {
            "agent_session_id": str(row["session_id"]),
            "agent_id": agent_id,
            "project_id": project_id,
            "coordination_session_id": coordination_session_id,
        }
        if any(
            str(session_policy.get(field) or "") != expected
            for field, expected in expected_binding.items()
        ):
            raise DurableCollaborationError("agent_session_policy_scope_mismatch")
        _, now_text = self._now()
        expires_at = row.get("expires_at")
        if expires_at:
            try:
                if parse_utc(str(expires_at)) <= parse_utc(now_text):
                    raise DurableCollaborationError("agent_session_expired")
            except DurableCollaborationError:
                raise
            except (TypeError, ValueError) as exc:
                raise DurableCollaborationError("agent_session_expiry_invalid") from exc
        try:
            stored_identity = _identity_from_dict(json.loads(str(row["identity_json"])))
            registered_identity = _identity_from_dict(json.loads(str(row["agent_identity_json"])))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("agent_session_identity_corrupt") from exc
        if stored_identity != registered_identity:
            raise DurableCollaborationError("agent_registry_identity_mismatch")
        if identity is not None and stored_identity != identity:
            raise DurableCollaborationError("agent_session_identity_mismatch")
        return row

    def _require_active_agent_session(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_id: str,
        identity: AgentIdentity | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for callers that require the active state."""

        return self._require_agent_session(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            agent_id=agent_id,
            identity=identity,
            session_id=session_id,
            allowed_states=_ACTIVE_AGENT_SESSION_STATES,
        )

    def _refresh_agent_registry_state(
        self,
        *,
        project_id: str,
        agent_id: str,
        observed_at: str,
    ) -> str:
        """Project the aggregate Agent state from all of its durable sessions."""

        states = {
            _bounded_state(row["state"], _AGENT_SESSION_STATES, "agent_session_state_corrupt")
            for row in self._fetchall(
                "SELECT state FROM collaboration_agent_sessions WHERE project_id=? AND agent_id=?",
                (project_id, agent_id),
            )
        }
        if states.intersection({"registered", "active"}):
            aggregate = "active"
        elif "idle" in states:
            aggregate = "idle"
        elif "stale" in states:
            aggregate = "stale"
        else:
            aggregate = "closed"
        self._connection.execute(
            "UPDATE collaboration_agents SET state=?, updated_at=? "
            "WHERE project_id=? AND agent_id=?",
            (aggregate, observed_at, project_id, agent_id),
        )
        return aggregate

    def _require_lease_owner_session(
        self,
        lease: Mapping[str, Any],
        session: Mapping[str, Any],
        *,
        binding_error: str,
    ) -> None:
        """Verify that a durable lease belongs to the exact live session."""

        owner_session_id = str(lease.get("owner_session_id") or "").strip()
        session_id = str(session.get("session_id") or "").strip()
        if not owner_session_id or not session_id or owner_session_id != session_id:
            raise DurableCollaborationError(binding_error)

    # ------------------------------------------------------------------
    # Agent/session registry and session-init lifecycle
    # ------------------------------------------------------------------

    def register_session(
        self,
        session: AgentSession,
        *,
        policy: Mapping[str, object] | None = None,
        peer_delta: tuple[Mapping[str, object], ...] = (),
    ) -> SessionInitResult:
        """Register an authenticated session idempotently and recover state."""

        if not isinstance(session, AgentSession):
            raise DurableCollaborationError("agent_session_required")
        if self._policy_authority is None:
            raise DurableCollaborationError("durable_policy_authority_required")
        if policy is not None:
            raise DurableCollaborationError("durable_policy_caller_policy_forbidden")
        project_id = session.project.project_id
        identity = session.identity
        policy_value: Mapping[str, object]
        server_session: AgentSession
        with self._write():
            # This re-read is the authority boundary.  A pre-transaction
            # lookup is merely a diagnostic hint; identity/scope/state checks
            # must happen after the single writer is held so a competing
            # session cannot be overwritten by an idempotent replay.
            now, now_text = self._now()
            existing = self._fetchone(
                "SELECT session_json, state, started_at, expires_at "
                "FROM collaboration_agent_sessions WHERE session_id = ?",
                (session.session_id,),
            )
            register_acceptance_session_source = existing is None
            if existing is None:
                server_session = AgentSession(
                    session_id=session.session_id,
                    identity=identity,
                    project=session.project,
                    coordination_session_id=session.coordination_session_id,
                    state="active",
                    started_at=now_text,
                    last_heartbeat_at=now_text,
                    expires_at=canonical_text(
                        now + timedelta(seconds=max(self._presence_timeout_seconds, 24 * 60 * 60))
                    ),
                )
            else:
                try:
                    stored = json.loads(str(existing["session_json"]))
                    stored_identity = (
                        stored.get("identity") if isinstance(stored, Mapping) else None
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise DurableCollaborationError("agent_session_projection_corrupt") from exc
                if (
                    not isinstance(stored_identity, Mapping)
                    or stored.get("project_id") != project_id
                    or stored.get("coordination_session_id") != session.coordination_session_id
                    or stored_identity != identity.to_dict()
                ):
                    raise DurableCollaborationError("agent_session_identity_conflict")
                state = _bounded_state(
                    existing["state"],
                    _AGENT_SESSION_STATES,
                    "agent_session_state_corrupt",
                )
                if state == "closed":
                    raise DurableCollaborationError("agent_session_closed")
                if state == "stale":
                    raise DurableCollaborationError("agent_session_stale")
                try:
                    server_session = AgentSession(
                        session_id=session.session_id,
                        identity=identity,
                        project=session.project,
                        coordination_session_id=session.coordination_session_id,
                        state="active",
                        started_at=str(existing["started_at"]),
                        last_heartbeat_at=now_text,
                        expires_at=str(existing["expires_at"]),
                    )
                except CollaborationContractError as exc:
                    raise DurableCollaborationError("agent_session_projection_corrupt") from exc
            # Persist only the server-issued projection.  Caller timestamps
            # remain diagnostic input and never participate in durable state.
            session_json = _canonical_json(server_session.to_dict())
            session_digest = _sha256(server_session.to_dict())
            try:
                policy_binding = self._policy_authority.issue(
                    server_session,
                    policy_revision=self._policy_revision,
                )
            except Exception as exc:
                raise DurableCollaborationError(_stable_reason(exc)) from exc
            policy_value = policy_binding.to_dict()
            policy_json = _canonical_json(policy_value)
            self._connection.execute(
                """
            INSERT INTO collaboration_agents (
                project_id, agent_id, role, parent_agent_id, capabilities_json,
                identity_json, policy_json, policy_revision, state,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(project_id, agent_id) DO UPDATE SET
                role = excluded.role,
                parent_agent_id = excluded.parent_agent_id,
                capabilities_json = excluded.capabilities_json,
                identity_json = excluded.identity_json,
                policy_json = excluded.policy_json,
                state = CASE
                    WHEN collaboration_agents.state = 'closed' THEN 'active'
                    ELSE 'active'
                END,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    identity.agent_id,
                    identity.role,
                    identity.parent_agent_id,
                    _canonical_json(list(identity.capabilities)),
                    _canonical_json(identity.to_dict()),
                    policy_json,
                    # ``AgentPolicyBinding`` deliberately names this field
                    # ``policy_revision``.  Persisting a legacy ``revision``
                    # lookup would silently erase the server-issued policy
                    # authority from the durable agent registry.
                    str(policy_value.get("policy_revision") or ""),
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            if self._acceptance_source_registry is not None and register_acceptance_session_source:
                try:
                    self._acceptance_source_registry.register(server_session)
                except Exception as exc:
                    raise DurableCollaborationError(
                        "durable_acceptance_source_registration_failed"
                    ) from exc
            if existing is None:
                self._connection.execute(
                    """
                INSERT INTO collaboration_agent_sessions (
                    session_id, project_id, agent_id, coordination_session_id,
                    identity_json, session_json, session_sha256, policy_json,
                    state, started_at, last_heartbeat_at, expires_at,
                    closed_at, cursor_sequence, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, '', 0, ?)
                    """,
                    (
                        session.session_id,
                        project_id,
                        identity.agent_id,
                        session.coordination_session_id,
                        _canonical_json(identity.to_dict()),
                        session_json,
                        session_digest,
                        policy_json,
                        server_session.started_at,
                        now_text,
                        server_session.expires_at,
                        now_text,
                    ),
                )
                self._append_event_in_transaction(
                    CollaborationEvent(
                        event_id=(
                            "event:agent-joined:"
                            + hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
                        ),
                        project=session.project,
                        coordination_session_id=session.coordination_session_id,
                        actor=identity,
                        event_type="agent.joined",
                        summary="Agent joined collaboration session",
                        created_at=now_text,
                        subject_refs=(session.session_id,),
                        payload={},
                    ),
                    actor_session_id=session.session_id,
                )
            else:
                updated = self._connection.execute(
                    "UPDATE collaboration_agent_sessions SET state='active', "
                    "last_heartbeat_at=?, policy_json=?, updated_at=? "
                    "WHERE session_id=? AND state IN ('registered','active','idle')",
                    (now_text, policy_json, now_text, session.session_id),
                )
                if updated.rowcount != 1:
                    # The transaction owns the writer lock, so this is a
                    # durable corruption/state mismatch rather than a retry.
                    raise DurableCollaborationError("agent_session_not_active")
            self._refresh_agent_registry_state(
                project_id=project_id,
                agent_id=identity.agent_id,
                observed_at=now_text,
            )
        cursor = self.load_cursor(
            project=session.project,
            coordination_session_id=session.coordination_session_id,
            consumer_id=session.session_id,
        )
        assigned_work = tuple(
            self.assigned_work_projection(
                session.project,
                session.session_id,
                limit=_SESSION_INIT_WORK_LIMIT,
            )
        )
        peer_page = self.peer_delta_page(
            session=self._session_with_server_state(server_session, now_text),
            after=cursor,
            limit=_SESSION_INIT_EVENT_LIMIT,
        )
        return SessionInitResult(
            session=self._session_with_server_state(server_session, now_text),
            policy=policy_value,
            working_set_summary=self.working_set_summary(
                session=self._session_with_server_state(server_session, now_text)
            ),
            cursor=cursor,
            next_cursor=peer_page["next_cursor"],
            source_head_sequence=int(peer_page["source_head_sequence"]),
            cursor_has_more=bool(peer_page["has_more"]),
            assigned_work=assigned_work,
            # Never echo a caller-supplied peer projection.  The page is read
            # from the canonical append-only log for the exact registered
            # session and does not advance the stored cursor until an explicit
            # acknowledgement is verified.
            peer_delta=tuple(peer_page["items"]),
        )

    # Explicit alias used by MCP session-init adapters.
    session_init = register_session

    def _session_with_server_state(self, session: AgentSession, heartbeat_at: str) -> AgentSession:
        expires_at = session.expires_at
        if expires_at is not None and parse_utc(expires_at) < parse_utc(heartbeat_at):
            expires_at = canonical_text(
                parse_utc(heartbeat_at) + timedelta(seconds=self._presence_timeout_seconds)
            )
        return AgentSession(
            session_id=session.session_id,
            identity=session.identity,
            project=session.project,
            coordination_session_id=session.coordination_session_id,
            state="active",
            started_at=session.started_at,
            last_heartbeat_at=heartbeat_at,
            expires_at=expires_at,
        )

    def heartbeat(self, session_id: str) -> dict[str, object]:
        """Record a server-clocked heartbeat and reconcile stale lifecycle rows."""

        session_id = _text(session_id, "session_id_required")
        with self._write():
            row = self._fetchone(
                "SELECT * FROM collaboration_agent_sessions WHERE session_id = ?",
                (session_id,),
            )
            if row is None:
                raise DurableCollaborationError("agent_session_not_found")
            self._require_agent_session(
                project_id=str(row["project_id"]),
                coordination_session_id=str(row["coordination_session_id"]),
                agent_id=str(row["agent_id"]),
                session_id=session_id,
                allowed_states=_HEARTBEAT_AGENT_SESSION_STATES,
            )
            _, now_text = self._now()
            updated = self._connection.execute(
                "UPDATE collaboration_agent_sessions SET state='active', last_heartbeat_at=?, "
                "updated_at=? WHERE session_id=? AND state IN ('active','idle')",
                (now_text, now_text, session_id),
            )
            if updated.rowcount != 1:
                # The exact lifecycle cause was read immediately above.  An
                # update miss under the held writer is therefore corruption,
                # not an invitation to resurrect a stale/closed session.
                raise DurableCollaborationError("agent_session_not_active")
            self._connection.execute(
                "UPDATE collaboration_agents SET last_seen_at=?, updated_at=? "
                "WHERE project_id=? AND agent_id=?",
                (now_text, now_text, row["project_id"], row["agent_id"]),
            )
            self._refresh_agent_registry_state(
                project_id=str(row["project_id"]),
                agent_id=str(row["agent_id"]),
                observed_at=now_text,
            )
        report = self.reconcile()
        return {
            "schema_version": "collaboration-heartbeat/v1",
            "session_id": session_id,
            "observed_at": now_text,
            "state": "active",
            "reconcile": report.to_dict(),
            "assigned_work": self.assigned_work(ProjectScope(row["project_id"]), session_id),
            "cursor": self.load_cursor(
                project=ProjectScope(row["project_id"]),
                coordination_session_id=row["coordination_session_id"],
                consumer_id=session_id,
            ).to_dict(),
        }

    def mark_idle(self, session_id: str) -> dict[str, object]:
        """Mark a live Agent session idle without fabricating a heartbeat.

        Idle is a recoverable presence state: the next authenticated server
        heartbeat resumes it to ``active``.  A stale or closed session is not
        recoverable through this path and must use a fresh server-bound
        session identity instead.
        """

        session_id = _text(session_id, "session_id_required")
        with self._write():
            row = self._fetchone(
                "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
                (session_id,),
            )
            if row is None:
                raise DurableCollaborationError("agent_session_not_found")
            self._require_agent_session(
                project_id=str(row["project_id"]),
                coordination_session_id=str(row["coordination_session_id"]),
                agent_id=str(row["agent_id"]),
                session_id=session_id,
                allowed_states=_HEARTBEAT_AGENT_SESSION_STATES,
            )
            _, now_text = self._now()
            updated = self._connection.execute(
                "UPDATE collaboration_agent_sessions SET state='idle', updated_at=? "
                "WHERE session_id=? AND state IN ('active','idle')",
                (now_text, session_id),
            )
            if updated.rowcount != 1:
                raise DurableCollaborationError("agent_session_not_active")
            self._refresh_agent_registry_state(
                project_id=str(row["project_id"]),
                agent_id=str(row["agent_id"]),
                observed_at=now_text,
            )
        return {
            "schema_version": "collaboration-session-idle/v1",
            "session_id": session_id,
            "observed_at": now_text,
            "state": "idle",
            "authority": "pp-server-backend/pp-core",
        }

    @_write_boundary
    def heartbeat_lease(
        self,
        heartbeat: LeaseHeartbeat,
        *,
        agent_session_id: str | None = None,
    ) -> dict[str, object]:
        """Persist one server-observed heartbeat for an active Agent lease.

        The owner-supplied ``sent_at`` is diagnostic only.  Lease freshness,
        sequence ordering, and expiry are decided by the server clock and by
        the exact durable lease digest/fencing generation.  The optional
        ``agent_session_id`` is an exact server-bound lifecycle selector; it
        is never inferred from an Agent identity supplied by a caller.

        The durable lease row binds the lease to the exact session that
        claimed it.  Concurrent transports for the same Agent therefore
        cannot extend each other's leases.
        """

        if not isinstance(heartbeat, LeaseHeartbeat):
            raise DurableCollaborationError("lease_heartbeat_required")
        if heartbeat.owner_kind != "agent":
            raise DurableCollaborationError("compute_lease_collaboration_forbidden")
        if agent_session_id is not None:
            agent_session_id = _text(agent_session_id, "agent_session_id_required")
        row = self._fetchone(
            "SELECT * FROM collaboration_work_leases WHERE lease_id=?",
            (heartbeat.lease_id,),
        )
        if row is None:
            raise DurableCollaborationError("lease_not_found")
        session = self._require_active_agent_session(
            project_id=heartbeat.project.project_id,
            coordination_session_id=str(row["coordination_session_id"]),
            agent_id=heartbeat.owner_id,
            session_id=agent_session_id,
        )
        self._require_lease_owner_session(
            row,
            session,
            binding_error="lease_heartbeat_binding_mismatch",
        )
        if (
            str(row["project_id"]) != heartbeat.project.project_id
            or str(row["work_item_id"]) != heartbeat.work_item_id
            or str(row["owner_kind"]) != heartbeat.owner_kind
            or str(row["policy_kind"]) != heartbeat.policy_kind
            or str(row["owner_id"]) != heartbeat.owner_id
            or str(row["lease_sha256"]) != heartbeat.lease_sha256
            or int(row["fencing_generation"]) != heartbeat.fencing_generation
        ):
            raise DurableCollaborationError("lease_heartbeat_binding_mismatch")
        if str(row["state"]) != "active":
            raise DurableCollaborationError("lease_heartbeat_not_active")
        now, now_text = self._now()
        if parse_utc(str(row["issued_at"])) > now:
            raise DurableCollaborationError("lease_issued_in_future")
        if parse_utc(str(row["expires_at"])) <= now:
            raise DurableCollaborationError("lease_expired")
        if heartbeat.sequence <= int(row["heartbeat_sequence"]):
            raise DurableCollaborationError("lease_heartbeat_stale")
        self._connection.execute(
            "UPDATE collaboration_work_leases SET heartbeat_sequence=?, last_heartbeat_at=? "
            "WHERE lease_id=? AND state='active'",
            (heartbeat.sequence, now_text, heartbeat.lease_id),
        )
        return {
            "schema_version": "lease-heartbeat-receipt/v1",
            "lease_id": heartbeat.lease_id,
            "work_item_id": heartbeat.work_item_id,
            "project_id": heartbeat.project.project_id,
            "owner_id": heartbeat.owner_id,
            "fencing_generation": heartbeat.fencing_generation,
            "heartbeat_sequence": heartbeat.sequence,
            "observed_at": now_text,
            "state": "active",
            "authority": "pp-server-backend/pp-core",
        }

    def end_session(self, session_id: str, *, reason: str = "session_end") -> dict[str, object]:
        """Close one session, append ``agent.closed``, and release its leases."""

        session_id = _text(session_id, "session_id_required")
        reason = _text(reason, "session_end_reason_required")
        result: dict[str, object]
        with self._write():
            row = self._fetchone(
                "SELECT * FROM collaboration_agent_sessions WHERE session_id = ?",
                (session_id,),
            )
            if row is None:
                raise DurableCollaborationError("agent_session_not_found")
            project = ProjectScope(row["project_id"])
            coordination_session_id = str(row["coordination_session_id"])
            event_key = hashlib.sha256(
                f"{project.project_id}:{coordination_session_id}:{session_id}".encode()
            ).hexdigest()[:40]
            event_id = f"event:session-closed:{event_key}"
            if str(row["state"]) == "closed":
                cursor = self.load_cursor(
                    project=project,
                    coordination_session_id=coordination_session_id,
                    consumer_id=session_id,
                )
                result = {
                    "schema_version": "session-end-response-v1",
                    "session_id": session_id,
                    "state": "closed",
                    "released_lease_ids": [],
                    "event_id": event_id
                    if self._fetchone(
                        "SELECT event_id FROM collaboration_events WHERE event_id=?",
                        (event_id,),
                    )
                    else "",
                    "cursor": cursor.to_dict(),
                }
            else:
                actor = _identity_from_dict(json.loads(str(row["identity_json"])))
                self._require_active_agent_session(
                    project_id=str(row["project_id"]),
                    coordination_session_id=coordination_session_id,
                    agent_id=str(row["agent_id"]),
                    identity=actor,
                    session_id=session_id,
                )
                _, observed_now = self._now()
                closed_at = str(row["closed_at"] or "") or observed_now
                # Release before closing so an ambiguity or release failure
                # rolls back without losing the active-session authority.
                released = self._release_session_leases(
                    session_id=session_id,
                    project_id=row["project_id"],
                    now_text=observed_now,
                    reason=reason,
                )
                event = CollaborationEvent(
                    event_id=event_id,
                    project=project,
                    coordination_session_id=coordination_session_id,
                    actor=actor,
                    event_type="agent.closed",
                    summary="Agent session closed",
                    created_at=closed_at,
                    payload={"reason": reason, "released_lease_count": len(released)},
                )
                # Keep event append, cursor advancement, and the final state
                # transition in one writer transaction.  append_event and
                # record_cursor have private in-transaction variants so this
                # path never attempts to reopen a closed session.
                event_cursor = self._append_event_in_transaction(
                    event,
                    actor_session_id=session_id,
                )
                self._record_cursor_in_transaction(
                    event_cursor,
                    consumer_id=session_id,
                    source_head_sequence=event_cursor.sequence,
                )
                self._connection.execute(
                    "UPDATE collaboration_agent_sessions SET state='closed', closed_at=?, "
                    "updated_at=? WHERE session_id=? AND state IN ('registered','active','idle')",
                    (closed_at, observed_now, session_id),
                )
                if self._connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise DurableCollaborationError("agent_session_not_active")
                aggregate = self._refresh_agent_registry_state(
                    project_id=str(row["project_id"]),
                    agent_id=str(row["agent_id"]),
                    observed_at=observed_now,
                )
                result = {
                    "schema_version": "session-end-response-v1",
                    "session_id": session_id,
                    "state": "closed",
                    "released_lease_ids": released,
                    "event_id": event_id,
                    "cursor": event_cursor.to_dict(),
                    "agent_state": aggregate,
                }
        return result

    # Compatibility name for Hook SessionEnd integrations.
    session_end = end_session

    # ------------------------------------------------------------------
    # ProjectWorkBoard and durable lease/result records
    # ------------------------------------------------------------------

    def register_work(
        self,
        receipt: WorkReceipt,
        *,
        state: str = "proposed",
        max_attempts: int = 1,
        agent_session_id: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(receipt, WorkReceipt):
            raise DurableCollaborationError("work_receipt_required")
        state = _bounded_state(state, _WORK_STATES, "work_state_invalid")
        if state not in _WORK_INITIAL_STATES:
            raise DurableCollaborationError("work_initial_state_invalid")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise DurableCollaborationError("work_max_attempts_invalid")
        now, now_text = self._now()
        if parse_utc(receipt.issued_at) > now:
            raise DurableCollaborationError("work_receipt_issued_in_future")
        if parse_utc(receipt.expires_at) <= now:
            raise DurableCollaborationError("work_receipt_expired")
        if agent_session_id is not None:
            agent_session_id = _text(agent_session_id, "agent_session_id_required")
        existing = self._fetchone(
            "SELECT work_receipt_sha256, state FROM collaboration_work_items WHERE work_item_id=?",
            (receipt.work_item_id,),
        )
        if existing is not None and str(existing["work_receipt_sha256"]) != receipt.content_sha256:
            raise DurableCollaborationError("work_receipt_conflict")
        with self._write():
            # A receipt may be structurally valid while still being an
            # unauthorised caller declaration.  Resolve it through the exact
            # durable server-issued session only after the writer opens; the
            # preflight query above deliberately has no authority effect.
            self._require_active_agent_session(
                project_id=receipt.project.project_id,
                coordination_session_id=receipt.coordination_session_id,
                agent_id=receipt.assigned_agent.agent_id,
                identity=receipt.assigned_agent,
                session_id=agent_session_id,
            )
            locked_existing = self._fetchone(
                "SELECT work_receipt_sha256 FROM collaboration_work_items WHERE work_item_id=?",
                (receipt.work_item_id,),
            )
            if (
                locked_existing is not None
                and str(locked_existing["work_receipt_sha256"]) != receipt.content_sha256
            ):
                raise DurableCollaborationError("work_receipt_conflict")
            self._connection.execute(
                """
            INSERT INTO collaboration_work_items (
                work_item_id, project_id, coordination_session_id,
                work_receipt_json, work_receipt_sha256, assigned_agent_id,
                state, attempt, max_attempts, created_at, updated_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '')
            ON CONFLICT(work_item_id) DO NOTHING
                """,
                (
                    receipt.work_item_id,
                    receipt.project.project_id,
                    receipt.coordination_session_id,
                    _canonical_json(receipt.to_dict()),
                    receipt.content_sha256,
                    receipt.assigned_agent.agent_id,
                    state,
                    max_attempts,
                    receipt.issued_at,
                    now_text,
                ),
            )
            if self._acceptance_source_registry is not None:
                try:
                    self._acceptance_source_registry.register(receipt)
                except Exception as exc:
                    raise DurableCollaborationError(
                        "durable_acceptance_source_registration_failed"
                    ) from exc
        return self.get_work(receipt.work_item_id) or {}

    def claim_work(
        self,
        lease: WorkLease,
        *,
        state: str = "leased",
        agent_session_id: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(lease, WorkLease):
            raise DurableCollaborationError("work_lease_required")
        state = _bounded_state(state, _WORK_STATES, "work_state_invalid")
        if state not in _WORK_CLAIM_TARGET_STATES:
            raise DurableCollaborationError("work_claim_state_invalid")
        work = lease.work_item
        now, now_text = self._now()
        if parse_utc(lease.issued_at) > now:
            raise DurableCollaborationError("lease_issued_in_future")
        if parse_utc(lease.expires_at) <= now:
            raise DurableCollaborationError("lease_expired")
        existing = self._fetchone(
            "SELECT project_id, coordination_session_id, work_receipt_sha256, assigned_agent_id, "
            "state, attempt, max_attempts "
            "FROM collaboration_work_items WHERE work_item_id=?",
            (work.work_item_id,),
        )
        if existing is None:
            raise DurableCollaborationError("work_not_registered")
        if lease.owner_kind != "agent":
            raise DurableCollaborationError("compute_lease_collaboration_forbidden")
        if agent_session_id is not None:
            agent_session_id = _text(agent_session_id, "agent_session_id_required")
        if (
            str(existing["project_id"]) != work.project.project_id
            or str(existing["coordination_session_id"]) != (work.coordination_session_id or "")
            or str(existing["assigned_agent_id"]) != lease.owner_id
            or str(existing["work_receipt_sha256"]) != lease.result_binding_sha256
        ):
            raise DurableCollaborationError("work_lease_binding_mismatch")
        existing_lease = self._fetchone(
            "SELECT lease_sha256,state,expires_at,owner_id,owner_session_id "
            "FROM collaboration_work_leases WHERE lease_id=?",
            (lease.lease_id,),
        )
        if existing_lease is not None:
            if str(existing_lease["lease_sha256"]) != lease.content_sha256:
                raise DurableCollaborationError("work_lease_conflict")
            # A released/expired lease is a spent fence.  Never turn a replay
            # of the same immutable lease id back into an active lease.
            if str(existing_lease["state"]) != "active":
                raise DurableCollaborationError("work_lease_replay_forbidden")
            if parse_utc(str(existing_lease["expires_at"])) <= now:
                raise DurableCollaborationError("lease_expired")
            with self._write():
                session = self._require_active_agent_session(
                    project_id=work.project.project_id,
                    coordination_session_id=work.coordination_session_id or "",
                    agent_id=lease.owner_id,
                    identity=lease.owner_identity,
                    session_id=agent_session_id,
                )
                if str(existing_lease["owner_id"]) != lease.owner_id:
                    raise DurableCollaborationError("work_lease_binding_mismatch")
                self._require_lease_owner_session(
                    existing_lease,
                    session,
                    binding_error="work_lease_binding_mismatch",
                )
                return self.get_work(work.work_item_id) or {}
        current_state = _bounded_state(existing["state"], _WORK_STATES, "work_state_corrupt")
        if current_state not in _WORK_CLAIMABLE_STATES:
            raise DurableCollaborationError("work_state_transition_invalid")
        current_attempt = int(existing["attempt"])
        max_attempts = int(existing["max_attempts"])
        if lease.work_item.max_attempts != max_attempts:
            raise DurableCollaborationError("work_max_attempts_mismatch")
        if lease.attempt != current_attempt + 1:
            raise DurableCollaborationError("work_attempt_not_monotonic")
        if lease.attempt > max_attempts:
            raise DurableCollaborationError("work_attempt_exhausted")
        latest_generation = self._connection.execute(
            "SELECT MAX(fencing_generation) FROM collaboration_work_leases WHERE work_item_id=?",
            (work.work_item_id,),
        ).fetchone()[0]
        if latest_generation is not None and lease.fencing_generation <= int(latest_generation):
            raise DurableCollaborationError("work_fencing_stale")
        active_lease = self._fetchone(
            "SELECT lease_id FROM collaboration_work_leases "
            "WHERE work_item_id=? AND state='active'",
            (work.work_item_id,),
        )
        if active_lease is not None:
            raise DurableCollaborationError("work_active_lease_exists")
        with self._write():
            # The preflight reads above are only hints.  Re-read every fence
            # that controls admission after the writer transaction is held;
            # otherwise two callers can both pass the active-lease check
            # before either INSERT becomes visible.
            session = self._require_active_agent_session(
                project_id=work.project.project_id,
                coordination_session_id=work.coordination_session_id or "",
                agent_id=lease.owner_id,
                identity=lease.owner_identity,
                session_id=agent_session_id,
            )
            locked_work = self._fetchone(
                "SELECT state, attempt, max_attempts FROM collaboration_work_items "
                "WHERE work_item_id=?",
                (work.work_item_id,),
            )
            if locked_work is None:
                raise DurableCollaborationError("work_not_registered")
            locked_state = _bounded_state(locked_work["state"], _WORK_STATES, "work_state_corrupt")
            if locked_state not in _WORK_CLAIMABLE_STATES:
                raise DurableCollaborationError("work_state_transition_invalid")
            if int(locked_work["max_attempts"]) != max_attempts:
                raise DurableCollaborationError("work_max_attempts_mismatch")
            if lease.attempt != int(locked_work["attempt"]) + 1:
                raise DurableCollaborationError("work_attempt_not_monotonic")
            locked_generation = self._connection.execute(
                "SELECT MAX(fencing_generation) FROM collaboration_work_leases "
                "WHERE work_item_id=?",
                (work.work_item_id,),
            ).fetchone()[0]
            if locked_generation is not None and lease.fencing_generation <= int(locked_generation):
                raise DurableCollaborationError("work_fencing_stale")
            locked_active = self._fetchone(
                "SELECT lease_id FROM collaboration_work_leases "
                "WHERE work_item_id=? AND state='active'",
                (work.work_item_id,),
            )
            if locked_active is not None:
                raise DurableCollaborationError("work_active_lease_exists")
            try:
                self._connection.execute(
                    """
            INSERT INTO collaboration_work_leases (
                lease_id, work_item_id, project_id, coordination_session_id,
                owner_kind, policy_kind, owner_id, owner_session_id, lease_json, lease_sha256,
                fencing_generation, attempt, issued_at, expires_at,
                heartbeat_sequence, last_heartbeat_at, state, released_at, release_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', '', '')
                """,
                    (
                        lease.lease_id,
                        work.work_item_id,
                        work.project.project_id,
                        work.coordination_session_id or "",
                        lease.owner_kind,
                        lease.policy_kind,
                        lease.owner_id,
                        str(session["session_id"]),
                        _canonical_json(lease.to_dict()),
                        lease.content_sha256,
                        lease.fencing_generation,
                        lease.attempt,
                        lease.issued_at,
                        lease.expires_at,
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DurableCollaborationError("work_lease_conflict") from exc
            self._connection.execute(
                "UPDATE collaboration_work_items SET state=?, attempt=?, max_attempts=?, updated_at=? "
                "WHERE work_item_id=?",
                (
                    state,
                    lease.attempt,
                    lease.work_item.max_attempts,
                    now_text,
                    work.work_item_id,
                ),
            )
        return self.get_work(work.work_item_id) or {}

    def _record_result_in_transaction(
        self,
        result: ResultReceipt,
        *,
        state: str = "submitted",
        lease_id: str | None = None,
        fencing_generation: int | None = None,
        lease_sha256: str | None = None,
        result_binding_sha256: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict[str, object]:
        """Persist a result while the caller owns the writer transaction.

        Lease identity is intentionally explicit at this internal boundary.  Looking
        up "whatever lease is active for this work" would allow a delayed
        result from an older generation to attach to a newer retry.  The
        optional ``agent_session_id`` selects the exact server-bound Agent
        session.  The lease row stores that exact session relation, so a peer
        transport with the same Agent identity still fails closed.
        """

        if not isinstance(result, ResultReceipt):
            raise DurableCollaborationError("result_receipt_required")
        state = _bounded_state(state, _WORK_STATES, "work_state_invalid")
        if state not in _WORK_RESULT_TARGET_STATES:
            raise DurableCollaborationError("work_result_state_invalid")
        if lease_id is None or fencing_generation is None:
            raise DurableCollaborationError("result_lease_binding_required")
        lease_id = _text(lease_id, "result_lease_id_required")
        if agent_session_id is not None:
            agent_session_id = _text(agent_session_id, "agent_session_id_required")
        if (
            isinstance(fencing_generation, bool)
            or not isinstance(fencing_generation, int)
            or fencing_generation < 1
        ):
            raise DurableCollaborationError("result_fencing_generation_invalid")
        if (
            result_binding_sha256 is not None
            and result_binding_sha256 != result.work_receipt_sha256
        ):
            raise DurableCollaborationError("result_binding_digest_mismatch")
        work = self._fetchone(
            "SELECT project_id, coordination_session_id, work_receipt_sha256, assigned_agent_id, "
            "state FROM "
            "collaboration_work_items WHERE work_item_id=?",
            (result.work_item_id,),
        )
        if work is None:
            raise DurableCollaborationError("work_not_registered")
        if (
            str(work["project_id"]) != result.project.project_id
            or str(work["coordination_session_id"]) != result.coordination_session_id
        ):
            raise DurableCollaborationError("result_scope_mismatch")
        if str(work["work_receipt_sha256"]) != result.work_receipt_sha256:
            raise DurableCollaborationError("result_work_receipt_mismatch")
        if str(work["assigned_agent_id"]) != result.submitted_by.agent_id:
            raise DurableCollaborationError("result_submitter_not_assignee")
        session = self._require_active_agent_session(
            project_id=result.project.project_id,
            coordination_session_id=result.coordination_session_id,
            agent_id=result.submitted_by.agent_id,
            identity=result.submitted_by,
            session_id=agent_session_id,
        )
        current_state = _bounded_state(work["state"], _WORK_STATES, "work_state_corrupt")
        existing_result = self._fetchone(
            "SELECT result_sha256,result_json FROM collaboration_results WHERE receipt_id=?",
            (result.receipt_id,),
        )
        if (
            existing_result is not None
            and str(existing_result["result_sha256"]) != result.content_sha256
        ):
            raise DurableCollaborationError("result_receipt_conflict")
        if existing_result is not None:
            try:
                stored_result = json.loads(str(existing_result["result_json"]))
                binding = (
                    stored_result.get("lease_binding")
                    if isinstance(stored_result, Mapping)
                    else None
                )
            except (TypeError, json.JSONDecodeError) as exc:
                raise DurableCollaborationError("result_receipt_corrupt") from exc
            if not isinstance(binding, Mapping):
                raise DurableCollaborationError("result_lease_binding_missing")
            try:
                stored_generation = int(binding.get("fencing_generation") or 0)
            except (TypeError, ValueError) as exc:
                raise DurableCollaborationError("result_lease_binding_corrupt") from exc
            if (
                str(binding.get("lease_id") or "") != lease_id
                or stored_generation != fencing_generation
                or (
                    lease_sha256 is not None
                    and str(binding.get("lease_sha256") or "") != lease_sha256
                )
                or str(binding.get("result_binding_sha256") or "") != result.work_receipt_sha256
            ):
                raise DurableCollaborationError("result_lease_binding_mismatch")
            replay_lease = self._fetchone(
                "SELECT * FROM collaboration_work_leases WHERE lease_id=?",
                (lease_id,),
            )
            if replay_lease is None:
                raise DurableCollaborationError("result_lease_source_unverified")
            if (
                str(replay_lease["work_item_id"]) != result.work_item_id
                or str(replay_lease["owner_id"]) != result.submitted_by.agent_id
                or int(replay_lease["fencing_generation"]) != fencing_generation
            ):
                raise DurableCollaborationError("result_lease_binding_mismatch")
            self._require_lease_owner_session(
                replay_lease,
                session,
                binding_error="result_lease_binding_mismatch",
            )
            return self.get_work(result.work_item_id) or {}
        if current_state not in _WORK_RESULT_SOURCE_STATES:
            raise DurableCollaborationError("work_result_transition_invalid")
        active_lease = self._fetchone(
            "SELECT * FROM collaboration_work_leases WHERE lease_id=?",
            (lease_id,),
        )
        if active_lease is None:
            raise DurableCollaborationError("work_active_lease_required")
        if str(active_lease["state"]) != "active":
            raise DurableCollaborationError("result_lease_not_active")
        if (
            str(active_lease["work_item_id"]) != result.work_item_id
            or str(active_lease["project_id"]) != result.project.project_id
            or str(active_lease["coordination_session_id"]) != result.coordination_session_id
        ):
            raise DurableCollaborationError("result_lease_binding_mismatch")
        if str(active_lease["owner_id"]) != result.submitted_by.agent_id:
            raise DurableCollaborationError("result_lease_owner_mismatch")
        self._require_lease_owner_session(
            active_lease,
            session,
            binding_error="result_lease_binding_mismatch",
        )
        if int(active_lease["fencing_generation"]) != fencing_generation:
            raise DurableCollaborationError("result_fencing_generation_stale")
        if lease_sha256 is not None and str(active_lease["lease_sha256"]) != lease_sha256:
            raise DurableCollaborationError("result_lease_digest_mismatch")
        now, now_text = self._now()
        try:
            lease_projection = json.loads(str(active_lease["lease_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("result_lease_corrupt") from exc
        if (
            not isinstance(lease_projection, Mapping)
            or str(lease_projection.get("result_binding_sha256") or "")
            != result.work_receipt_sha256
        ):
            raise DurableCollaborationError("result_lease_binding_mismatch")
        if parse_utc(str(active_lease["issued_at"])) > now:
            raise DurableCollaborationError("result_lease_issued_in_future")
        if parse_utc(str(active_lease["expires_at"])) <= now:
            raise DurableCollaborationError("result_lease_expired")
        if parse_utc(result.submitted_at) > now:
            raise DurableCollaborationError("result_submitted_in_future")
        result_projection = result.to_dict()
        result_projection["lease_binding"] = {
            "lease_id": lease_id,
            "lease_sha256": str(active_lease["lease_sha256"]),
            "fencing_generation": fencing_generation,
            "result_binding_sha256": result.work_receipt_sha256,
        }
        self._connection.execute(
            """
            INSERT INTO collaboration_results (
                receipt_id, project_id, coordination_session_id, work_item_id,
                result_json, result_sha256, submitted_at, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(receipt_id) DO NOTHING
            """,
            (
                result.receipt_id,
                result.project.project_id,
                result.coordination_session_id,
                result.work_item_id,
                _canonical_json(result_projection),
                result.content_sha256,
                result.submitted_at,
                result.outcome,
                now_text,
            ),
        )
        self._connection.execute(
            "UPDATE collaboration_work_items SET state=?, updated_at=? WHERE work_item_id=?",
            (state, now_text, result.work_item_id),
        )
        # A result consumes this exact lease generation.  It may never be
        # replayed as a fresh claim; retries must mint a higher fence.
        self._connection.execute(
            "UPDATE collaboration_work_leases SET state='completed', released_at=?, "
            "release_reason='result_recorded' WHERE lease_id=? AND state='active'",
            (now_text, lease_id),
        )
        event_key = hashlib.sha256(
            _canonical_json(
                {
                    "project_id": result.project.project_id,
                    "coordination_session_id": result.coordination_session_id,
                    "work_item_id": result.work_item_id,
                    "result_receipt_id": result.receipt_id,
                    "result_receipt_sha256": result.content_sha256,
                }
            ).encode("utf-8")
        ).hexdigest()[:40]
        submitted_payload: dict[str, object] = {
            "result_receipt_id": result.receipt_id,
            "result_receipt_sha256": result.content_sha256,
            "work_receipt_sha256": result.work_receipt_sha256,
            "lease_id": lease_id,
            "lease_sha256": str(active_lease["lease_sha256"]),
            "fencing_generation": fencing_generation,
            "outcome": result.outcome,
            "artifact_refs": list(result.artifact_refs),
            "evidence_refs": list(result.evidence_refs),
            "canonical_memory_effect": "none",
        }
        # Legacy/internal record_result callers may submit a receipt before
        # the formal MCP path has issued a role assignment.  Empty public
        # digest fields are rejected by CollaborationEvent; formal closures
        # still include the server-issued assignment digest below.
        if result.role_assignment_sha256:
            submitted_payload["role_assignment_sha256"] = result.role_assignment_sha256
        submitted_event = CollaborationEvent(
            event_id=f"event:work-submitted:{event_key}",
            project=result.project,
            coordination_session_id=result.coordination_session_id,
            actor=result.submitted_by,
            event_type="work.submitted",
            summary="Work result submitted",
            created_at=now_text,
            work_item_id=result.work_item_id,
            subject_refs=(result.receipt_id,),
            evidence_refs=(result.content_sha256,),
            payload=submitted_payload,
        )
        self._append_event_in_transaction(
            submitted_event,
            actor_session_id=str(session["session_id"]),
        )
        # Register the canonical source only after both the durable result row
        # and its append-only event have succeeded. The registry is
        # process-local and cannot be rolled back with SQLite; registering it
        # earlier would leave a ghost acceptance source after an event-write
        # failure. ServerAcceptanceSourceRegistry stages its own changes, so a
        # registration error here remains all-or-nothing as well.
        if self._acceptance_source_registry is not None:
            try:
                self._acceptance_source_registry.register(result)
            except Exception as exc:
                raise DurableCollaborationError(
                    "durable_acceptance_source_registration_failed"
                ) from exc
        return self.get_work(result.work_item_id) or {}

    @_write_boundary
    def record_result(
        self,
        result: ResultReceipt,
        *,
        state: str = "submitted",
        lease_id: str | None = None,
        fencing_generation: int | None = None,
        lease_sha256: str | None = None,
        result_binding_sha256: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict[str, object]:
        """Persist a result bound to one exact active lease generation."""

        return self._record_result_in_transaction(
            result,
            state=state,
            lease_id=lease_id,
            fencing_generation=fencing_generation,
            lease_sha256=lease_sha256,
            result_binding_sha256=result_binding_sha256,
            agent_session_id=agent_session_id,
        )

    @_write_boundary
    def record_step_closure_result(
        self,
        *,
        work_item_id: str,
        outcome: str,
        summary: str,
        artifact_refs: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        result: Mapping[str, object] | None = None,
        agent_session_id: str,
    ) -> dict[str, object]:
        """Create and persist a server-bound ResultReceipt for formal closure.

        The caller may identify work and bounded result content, but cannot
        choose the submitter identity or lease authority.  The exact active
        lease is resolved from ``work_item_id + owner_session_id`` while the
        single writer is held.  This prevents a delayed result from choosing
        a newer retry or a peer transport's lease.
        """

        work_item_id = _text(work_item_id, "work_item_id_required")
        agent_session_id = _text(agent_session_id, "agent_session_id_required")
        normalized_outcome = str(outcome or "").strip().casefold()
        normalized_summary = str(summary or "").strip()
        # Keep the deterministic receipt identity stable for semantically
        # equivalent caller input. ResultReceipt performs the same contract
        # normalization; doing it before hashing prevents whitespace/case
        # differences from minting a second result for one closure.
        outcome = normalized_outcome
        summary = normalized_summary
        work = self._fetchone(
            "SELECT work_receipt_json,work_receipt_sha256,project_id,"
            "coordination_session_id,assigned_agent_id,state "
            "FROM collaboration_work_items WHERE work_item_id=?",
            (work_item_id,),
        )
        if work is None:
            raise DurableCollaborationError("work_not_registered")
        receipt = _work_receipt_from_projection(json.loads(str(work["work_receipt_json"])))
        if receipt.content_sha256 != str(work["work_receipt_sha256"]):
            raise DurableCollaborationError("work_receipt_projection_conflict")
        session = self._require_active_agent_session(
            project_id=str(work["project_id"]),
            coordination_session_id=str(work["coordination_session_id"]),
            agent_id=str(work["assigned_agent_id"]),
            identity=receipt.assigned_agent,
            session_id=agent_session_id,
        )
        if result is None:
            result_projection: Mapping[str, object] = {}
        elif isinstance(result, Mapping):
            result_projection = result
        else:
            raise DurableCollaborationError("result_projection_invalid")
        now, now_text = self._now()
        closure_request = {
            "work_item_id": work_item_id,
            "agent_session_id": agent_session_id,
            "outcome": normalized_outcome,
            "summary": normalized_summary,
            "artifact_refs": list(artifact_refs),
            "evidence_refs": list(evidence_refs),
            "result": result_projection,
        }
        receipt_id = (
            "result:step-closure:"
            + hashlib.sha256(_canonical_json(closure_request).encode("utf-8")).hexdigest()[:40]
        )

        # A repeated formal closure must be a repair/replay, not a new claim.
        # The first submission consumes its lease, so resolve the original
        # lease binding from the immutable result row before looking for a
        # currently-active lease.
        existing_result = self._fetchone(
            "SELECT result_sha256,result_json FROM collaboration_results WHERE receipt_id=?",
            (receipt_id,),
        )
        if existing_result is not None:
            try:
                stored_projection = json.loads(str(existing_result["result_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise DurableCollaborationError("result_receipt_corrupt") from exc
            if not isinstance(stored_projection, Mapping):
                raise DurableCollaborationError("result_receipt_corrupt")
            stored_binding = stored_projection.get("lease_binding")
            if not isinstance(stored_binding, Mapping):
                raise DurableCollaborationError("result_lease_binding_missing")
            stored_receipt = dict(stored_projection)
            stored_receipt.pop("lease_binding", None)
            try:
                if _sha256(stored_receipt) != str(existing_result["result_sha256"]):
                    raise DurableCollaborationError("result_receipt_corrupt")
                submitted_by = stored_receipt.get("submitted_by")
                if not isinstance(submitted_by, Mapping):
                    raise DurableCollaborationError("result_receipt_corrupt")
                replay_receipt = ResultReceipt(
                    receipt_id=str(stored_receipt["receipt_id"]),
                    work_item_id=str(stored_receipt["work_item_id"]),
                    work_receipt_sha256=str(stored_receipt["work_receipt_sha256"]),
                    project=ProjectScope(str(stored_receipt["project_id"])),
                    coordination_session_id=str(stored_receipt["coordination_session_id"]),
                    submitted_by=_identity_from_dict(submitted_by),
                    outcome=str(stored_receipt["outcome"]),
                    summary=str(stored_receipt["summary"]),
                    submitted_at=str(stored_receipt["submitted_at"]),
                    role_assignment_sha256=str(stored_receipt.get("role_assignment_sha256") or ""),
                    artifact_refs=tuple(stored_receipt.get("artifact_refs") or ()),
                    evidence_refs=tuple(stored_receipt.get("evidence_refs") or ()),
                    result=(
                        stored_receipt.get("result")
                        if isinstance(stored_receipt.get("result"), Mapping)
                        else {}
                    ),
                )
                stored_generation = int(stored_binding.get("fencing_generation") or 0)
                stored_lease_id = _text(
                    stored_binding.get("lease_id"),
                    "result_lease_id_required",
                )
                stored_lease_sha256 = _text(
                    stored_binding.get("lease_sha256"),
                    "result_lease_digest_required",
                )
            except DurableCollaborationError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise DurableCollaborationError("result_receipt_corrupt") from exc
            if (
                replay_receipt.receipt_id != receipt_id
                or replay_receipt.work_item_id != work_item_id
                or replay_receipt.project.project_id != str(work["project_id"])
                or replay_receipt.coordination_session_id != str(work["coordination_session_id"])
                or replay_receipt.submitted_by != receipt.assigned_agent
                or replay_receipt.outcome != normalized_outcome
                or replay_receipt.summary != normalized_summary
                or replay_receipt.artifact_refs != tuple(artifact_refs)
                or replay_receipt.evidence_refs != tuple(evidence_refs)
                or dict(replay_receipt.result) != dict(result_projection)
            ):
                raise DurableCollaborationError("result_receipt_conflict")
            persisted = self._record_result_in_transaction(
                replay_receipt,
                lease_id=stored_lease_id,
                fencing_generation=stored_generation,
                lease_sha256=stored_lease_sha256,
                agent_session_id=str(session["session_id"]),
            )
            return {
                "schema_version": "step-closure-result-receipt/v1",
                "result_receipt": replay_receipt.to_dict(),
                "result_receipt_sha256": replay_receipt.content_sha256,
                "work": persisted,
                "memory_proposal": None,
                "canonical_memory_effect": "none",
                "replayed": True,
            }

        leases = self._fetchall(
            "SELECT lease_id, fencing_generation, lease_sha256, state, expires_at "
            "FROM collaboration_work_leases "
            "WHERE work_item_id=? AND owner_session_id=? AND state='active' "
            "ORDER BY fencing_generation DESC",
            (work_item_id, str(session["session_id"])),
        )
        if not leases:
            raise DurableCollaborationError("work_active_lease_required")
        if len(leases) != 1:
            raise DurableCollaborationError("work_active_lease_ambiguous")
        lease = leases[0]
        lease_id = _text(lease["lease_id"], "result_lease_id_required")
        try:
            fencing_generation = int(lease["fencing_generation"])
        except (TypeError, ValueError) as exc:
            raise DurableCollaborationError("result_fencing_generation_invalid") from exc
        lease_sha256 = _text(lease["lease_sha256"], "result_lease_digest_required")

        # Formal closure is the only MCP path that can create a ResultReceipt
        # from a work id.  Before constructing that receipt, the server issues
        # a short-lived submitter assignment from the exact session/work/lease
        # tuple.  The intent event and assignment basis are written through the
        # same single-writer transaction, so a failure cannot leave a role
        # binding that points at a result which was rolled back.
        if self._role_assignment_authority is None or self._role_assignment_repository is None:
            raise DurableCollaborationError("result_submitter_assignment_required")
        try:
            work_projection = json.loads(str(work["work_receipt_json"]))
            lease_projection = json.loads(
                str(
                    self._fetchone(
                        "SELECT lease_json FROM collaboration_work_leases WHERE lease_id=?",
                        (lease_id,),
                    )["lease_json"]
                )
            )
            session_projection = json.loads(str(session["session_json"]))
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("result_submitter_assignment_source_corrupt") from exc
        stored_work = _work_receipt_from_projection(work_projection)
        stored_lease = _work_lease_from_projection(lease_projection)
        stored_session = _agent_session_from_projection(session_projection)
        if (
            stored_work.content_sha256 != str(work["work_receipt_sha256"])
            or stored_lease.content_sha256 != lease_sha256
            or stored_lease.work_item.work_item_id != work_item_id
        ):
            # ``owner_session_id`` is server-only lease-row state, not part of
            # the portable WorkLease.  The exact row/session binding was
            # already checked by the active-lease query; keep this guard
            # limited to portable digest and scope invariants.
            raise DurableCollaborationError("result_submitter_assignment_source_mismatch")
        intent_key = hashlib.sha256(
            _canonical_json(
                {
                    "project_id": stored_work.project.project_id,
                    "coordination_session_id": stored_work.coordination_session_id,
                    "work_item_id": stored_work.work_item_id,
                    "lease_id": stored_lease.lease_id,
                    "agent_session_id": stored_session.session_id,
                    "use": RESULT_SUBMISSION_USE,
                    "workflow_stage": _RESULT_SUBMISSION_WORKFLOW_STAGE,
                }
            ).encode("utf-8")
        ).hexdigest()[:40]
        intent_event_id = f"event:agent-intent:result-submission:{intent_key}"
        intent_event = CollaborationEvent(
            event_id=intent_event_id,
            project=stored_work.project,
            coordination_session_id=stored_work.coordination_session_id,
            actor=stored_session.identity,
            event_type="agent.intent_declared",
            summary="Result submission intent declared",
            created_at=now_text,
            work_item_id=stored_work.work_item_id,
            payload={
                "requested_use": RESULT_SUBMISSION_USE,
                "requested_role": WORK_SUBMITTER_ROLE,
                "workflow_stage": _RESULT_SUBMISSION_WORKFLOW_STAGE,
                "authority_effect": "none",
            },
        )
        self._append_event_in_transaction(
            intent_event,
            actor_session_id=stored_session.session_id,
        )
        stored_intent_row = self._fetchone(
            "SELECT event_json FROM collaboration_events WHERE event_id=?",
            (intent_event_id,),
        )
        if stored_intent_row is None:
            raise DurableCollaborationError("result_submitter_intent_persistence_missing")
        try:
            stored_intent = CollaborationEvent.from_dict(
                json.loads(str(stored_intent_row["event_json"]))
            )
        except (TypeError, json.JSONDecodeError, CollaborationContractError) as exc:
            raise DurableCollaborationError("result_submitter_intent_projection_corrupt") from exc
        basis = RoleAssignmentBasis(
            session=stored_session,
            work=stored_work,
            lease=stored_lease,
            intent_event=stored_intent,
            workflow_stage=_RESULT_SUBMISSION_WORKFLOW_STAGE,
            work_state=str(work["state"]),
            lease_state="active",
        )
        try:
            self._role_assignment_repository.register_basis(
                use=RESULT_SUBMISSION_USE,
                basis=basis,
            )
            assignment = self._role_assignment_authority.issue(
                use=RESULT_SUBMISSION_USE,
                agent_session_id=stored_session.session_id,
                work_item_id=stored_work.work_item_id,
                lease_id=stored_lease.lease_id,
                intent_event_id=stored_intent.event_id,
            )
        except Exception as exc:
            reason = _stable_reason(exc)
            raise DurableCollaborationError(
                reason
                if reason.startswith("role_assignment_")
                else "result_submitter_assignment_unavailable"
            ) from exc
        result_receipt = ResultReceipt.for_work(
            receipt,
            receipt_id=receipt_id,
            submitted_by=receipt.assigned_agent,
            outcome=outcome,
            summary=summary,
            submitted_at=canonical_text(now),
            artifact_refs=artifact_refs,
            evidence_refs=evidence_refs,
            result=result_projection,
            role_assignment_sha256=assignment.assignment_sha256,
        )
        persisted = self._record_result_in_transaction(
            result_receipt,
            lease_id=lease_id,
            fencing_generation=fencing_generation,
            lease_sha256=lease_sha256,
            agent_session_id=str(session["session_id"]),
        )
        return {
            "schema_version": "step-closure-result-receipt/v1",
            "result_receipt": result_receipt.to_dict(),
            "result_receipt_sha256": result_receipt.content_sha256,
            "work": persisted,
            "memory_proposal": None,
            "canonical_memory_effect": "none",
        }

    def review_work(
        self,
        work_item_id: str,
        *,
        reviewer_session_id: str,
    ) -> dict[str, object]:
        """Move submitted work into reviewing under an active peer session."""

        return self._transition_review_state(
            work_item_id,
            reviewer_session_id=reviewer_session_id,
            target_state="reviewing",
        )

    @_write_boundary
    def accept_work(
        self,
        work_item_id: str,
        *,
        reviewer_session_id: str,
        acceptance_receipt: AcceptanceReceipt,
    ) -> dict[str, object]:
        """Move reviewed work into accepted under an independent reviewer.

        A portable receipt is only evidence.  The injected server authority
        must resolve it to its own issuance record before this state-changing
        boundary can run.  A caller-supplied digest (or a structurally valid
        but self-issued receipt) never grants acceptance authority.
        """

        if self._acceptance_authority is None:
            raise DurableCollaborationError("work_acceptance_authority_required")
        if not isinstance(acceptance_receipt, AcceptanceReceipt):
            raise DurableCollaborationError("work_acceptance_receipt_required")
        work = self._transition_review_state(
            work_item_id,
            reviewer_session_id=reviewer_session_id,
            target_state="accepted",
            acceptance_receipt=acceptance_receipt,
        )
        promotion = self._enqueue_accepted_work_promotion(
            work_item_id,
            acceptance_receipt=acceptance_receipt,
        )
        return {**work, "promotion": promotion.to_dict()}

    def _enqueue_accepted_work_promotion(
        self,
        work_item_id: str,
        *,
        acceptance_receipt: AcceptanceReceipt,
    ) -> PendingPromotionResult:
        """Create the durable promotion job from canonical accepted-work rows."""

        if self._acceptance_authority is None:
            raise DurableCollaborationError("promotion_acceptance_authority_required")
        try:
            canonical_acceptance = self._acceptance_authority.verify_issued(acceptance_receipt)
        except Exception as exc:
            raise DurableCollaborationError("promotion_acceptance_receipt_unverified") from exc
        work = self._fetchone(
            "SELECT work_receipt_sha256,project_id,coordination_session_id "
            "FROM collaboration_work_items WHERE work_item_id=? AND state='accepted'",
            (work_item_id,),
        )
        if work is None:
            raise DurableCollaborationError("promotion_source_not_accepted")
        result = self._fetchone(
            "SELECT result_json,result_sha256 FROM collaboration_results WHERE receipt_id=?",
            (canonical_acceptance.result_receipt_id,),
        )
        if (
            result is None
            or str(result["result_sha256"]) != canonical_acceptance.result_receipt_sha256
        ):
            raise DurableCollaborationError("promotion_result_digest_mismatch")
        try:
            result_projection = json.loads(str(result["result_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("promotion_result_corrupt") from exc
        if not isinstance(result_projection, Mapping):
            raise DurableCollaborationError("promotion_result_corrupt")
        result_projection = dict(result_projection)
        result_projection.pop("lease_binding", None)
        if _sha256(result_projection) != str(result["result_sha256"]):
            raise DurableCollaborationError("promotion_result_digest_mismatch")
        expected_event = self._accepted_event(
            {
                "project_id": str(work["project_id"]),
                "coordination_session_id": str(work["coordination_session_id"]),
                "work_item_id": work_item_id,
                "work_receipt_sha256": str(work["work_receipt_sha256"]),
            },
            reviewer_session_id=canonical_acceptance.reviewer_agent_session_id,
            acceptance=canonical_acceptance,
        )
        event = self._fetchone(
            "SELECT event_sha256 FROM collaboration_events WHERE event_id=? AND event_type='work.accepted'",
            (expected_event.event_id,),
        )
        if event is None:
            raise DurableCollaborationError("promotion_source_event_missing")
        evidence_refs = tuple(
            str(item) for item in result_projection.get("evidence_refs", ()) if str(item).strip()
        )
        combined_evidence = tuple(
            dict.fromkeys((*evidence_refs, *canonical_acceptance.evidence_refs))
        )
        idempotency_sha256 = _sha256(
            {
                "event_id": expected_event.event_id,
                "event_sha256": str(event["event_sha256"]),
                "work_receipt_sha256": str(work["work_receipt_sha256"]),
                "result_receipt_sha256": canonical_acceptance.result_receipt_sha256,
                "acceptance_receipt_sha256": canonical_acceptance.content_sha256,
            }
        )
        candidate = PromotionCandidate(
            candidate_id=f"promotion-candidate:{idempotency_sha256.removeprefix('sha256:')[:40]}",
            project=ProjectScope(str(work["project_id"])),
            coordination_session_id=str(work["coordination_session_id"]),
            work_item_id=work_item_id,
            source_event_id=expected_event.event_id,
            source_event_sha256=str(event["event_sha256"]),
            work_receipt_sha256=str(work["work_receipt_sha256"]),
            result_receipt_sha256=canonical_acceptance.result_receipt_sha256,
            acceptance_receipt_sha256=canonical_acceptance.content_sha256,
            summary=str(result_projection.get("summary") or "").strip(),
            evidence_refs=combined_evidence,
            idempotency_sha256=idempotency_sha256,
        )
        return self.enqueue_promotion(
            candidate,
            conflict_checked=canonical_acceptance.conflict_state != "unresolved",
            acceptance_receipt=canonical_acceptance,
        )

    @_write_boundary
    def _transition_review_state(
        self,
        work_item_id: str,
        *,
        reviewer_session_id: str,
        target_state: str,
        acceptance_receipt: AcceptanceReceipt | None = None,
    ) -> dict[str, object]:
        work_item_id = _text(work_item_id, "work_item_id_required")
        reviewer_session_id = _text(reviewer_session_id, "reviewer_session_id_required")
        target_state = _bounded_state(target_state, _WORK_STATES, "work_state_invalid")
        if target_state not in {"reviewing", "accepted"}:
            raise DurableCollaborationError("work_review_state_invalid")
        work = self._fetchone(
            "SELECT * FROM collaboration_work_items WHERE work_item_id=?",
            (work_item_id,),
        )
        if work is None:
            raise DurableCollaborationError("work_not_registered")
        current_state = _bounded_state(work["state"], _WORK_STATES, "work_state_corrupt")
        expected = "submitted" if target_state == "reviewing" else "reviewing"
        if target_state == "accepted" and current_state == "accepted":
            replayed_acceptance = self._verify_acceptance_receipt(
                work,
                reviewer_session_id=reviewer_session_id,
                acceptance_receipt=acceptance_receipt,
                allow_inactive_sessions=True,
            )
            event = self._accepted_event(
                work,
                reviewer_session_id=reviewer_session_id,
                acceptance=replayed_acceptance,
            )
            self._require_existing_accepted_event(event)
            return self.get_work(work_item_id) or {}
        if current_state != expected:
            raise DurableCollaborationError("work_review_state_transition_invalid")
        reviewer = self._fetchone(
            "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
            (reviewer_session_id,),
        )
        if reviewer is None:
            raise DurableCollaborationError("reviewer_session_not_found")
        if str(reviewer["project_id"]) != str(work["project_id"]) or str(
            reviewer["coordination_session_id"]
        ) != str(work["coordination_session_id"]):
            raise DurableCollaborationError("reviewer_session_scope_mismatch")
        if str(reviewer["agent_id"]) == str(work["assigned_agent_id"]):
            raise DurableCollaborationError("independent_reviewer_required")
        actor = _identity_from_dict(json.loads(str(reviewer["identity_json"])))
        canonical_acceptance: AcceptanceReceipt | None = None
        if target_state == "accepted":
            canonical_acceptance = self._verify_acceptance_receipt(
                work,
                reviewer_session_id=reviewer_session_id,
                acceptance_receipt=acceptance_receipt,
                reviewer=reviewer,
                reviewer_identity=actor,
                allow_inactive_sessions=False,
            )
        self._require_active_agent_session(
            project_id=str(work["project_id"]),
            coordination_session_id=str(work["coordination_session_id"]),
            agent_id=actor.agent_id,
            identity=actor,
            session_id=reviewer_session_id,
        )
        _, now_text = self._now()
        self._connection.execute(
            "UPDATE collaboration_work_items SET state=?, updated_at=?, last_error='' "
            "WHERE work_item_id=? AND state=?",
            (target_state, now_text, work_item_id, expected),
        )
        payload: dict[str, object] = {
            "stage": "review",
            "decision": target_state,
            "reviewer_session_id": reviewer_session_id,
            "work_receipt_sha256": str(work["work_receipt_sha256"]),
        }
        event_type = "work.accepted" if target_state == "accepted" else "workflow.stage_completed"
        if target_state == "accepted":
            if canonical_acceptance is None:  # pragma: no cover - defensive narrowing
                raise DurableCollaborationError("work_acceptance_receipt_required")
            event = self._accepted_event(
                work,
                reviewer_session_id=reviewer_session_id,
                acceptance=canonical_acceptance,
            )
        else:
            event_key = hashlib.sha256(
                f"{work['project_id']}:{work['coordination_session_id']}:{work_item_id}:"
                f"{target_state}:{reviewer_session_id}".encode()
            ).hexdigest()[:40]
            event = CollaborationEvent(
                event_id=f"event:work-{target_state}:{event_key}",
                project=ProjectScope(str(work["project_id"])),
                coordination_session_id=str(work["coordination_session_id"]),
                actor=actor,
                event_type=event_type,
                summary=f"Work {target_state}",
                created_at=now_text,
                work_item_id=work_item_id,
                payload=payload,
            )
        # ``append_event`` re-validates the active server session and uses the
        # canonical event-log writer; the nested transaction is intentional.
        self.append_event(event, actor_session_id=reviewer_session_id)
        return self.get_work(work_item_id) or {}

    def _verify_acceptance_receipt(
        self,
        work: Mapping[str, Any],
        *,
        reviewer_session_id: str,
        acceptance_receipt: AcceptanceReceipt | None,
        reviewer: Mapping[str, Any] | None = None,
        reviewer_identity: AgentIdentity | None = None,
        allow_inactive_sessions: bool,
    ) -> AcceptanceReceipt:
        """Resolve a portable receipt through the injected server authority.

        The acceptance authority owns issuance and role/policy checks.  This
        adapter adds the durable source checks that the process-local v2
        authority cannot perform: exact persisted work/result/session rows,
        reviewer argument binding, and accepted-event lineage.
        """

        if self._acceptance_authority is None:
            raise DurableCollaborationError("work_acceptance_authority_required")
        if not isinstance(acceptance_receipt, AcceptanceReceipt):
            raise DurableCollaborationError("work_acceptance_receipt_required")
        try:
            proof = self._acceptance_authority.verify_for_consumption(acceptance_receipt)
            canonical = self._acceptance_authority.verify_consumption_proof(proof)
        except Exception as exc:
            raise DurableCollaborationError(_stable_reason(exc)) from exc
        if canonical.decision != "accepted":
            raise DurableCollaborationError("work_acceptance_decision_invalid")
        if canonical.conflict_state == "unresolved":
            raise DurableCollaborationError("work_acceptance_conflict_unresolved")
        project_id = str(work["project_id"])
        coordination_session_id = str(work["coordination_session_id"])
        work_item_id = str(work["work_item_id"])
        work_digest = str(work["work_receipt_sha256"])
        try:
            stored_work = json.loads(str(work["work_receipt_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("work_receipt_corrupt") from exc
        if not isinstance(stored_work, Mapping) or _sha256(stored_work) != work_digest:
            raise DurableCollaborationError("work_receipt_digest_mismatch")
        if (
            canonical.project.project_id != project_id
            or canonical.coordination_session_id != coordination_session_id
            or canonical.work_item_id != work_item_id
            or canonical.work_receipt_sha256 != work_digest
            or canonical.work_receipt_id != str(stored_work.get("receipt_id") or "")
        ):
            raise DurableCollaborationError("work_acceptance_work_scope_mismatch")
        if canonical.reviewer_agent_session_id != reviewer_session_id:
            raise DurableCollaborationError("work_acceptance_reviewer_session_mismatch")
        if reviewer is None:
            reviewer = self._fetchone(
                "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
                (reviewer_session_id,),
            )
        if reviewer is None:
            raise DurableCollaborationError("work_acceptance_reviewer_session_missing")
        try:
            stored_reviewer = json.loads(str(reviewer["session_json"]))
            resolved_reviewer = _identity_from_dict(json.loads(str(reviewer["identity_json"])))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("work_acceptance_reviewer_session_corrupt") from exc
        if not isinstance(stored_reviewer, Mapping) or _sha256(stored_reviewer) != str(
            reviewer["session_sha256"]
        ):
            raise DurableCollaborationError("work_acceptance_reviewer_session_digest_mismatch")
        if (
            str(reviewer["project_id"]) != project_id
            or str(reviewer["coordination_session_id"]) != coordination_session_id
            or canonical.reviewer_agent_session_sha256 != str(reviewer["session_sha256"])
            or canonical.accepted_by != resolved_reviewer
        ):
            raise DurableCollaborationError("work_acceptance_reviewer_scope_mismatch")
        if reviewer_identity is not None and reviewer_identity != resolved_reviewer:
            raise DurableCollaborationError("work_acceptance_reviewer_identity_mismatch")
        if not allow_inactive_sessions and str(reviewer["state"]) != "active":
            raise DurableCollaborationError("agent_session_not_active")

        result = self._fetchone(
            "SELECT * FROM collaboration_results WHERE receipt_id=?",
            (canonical.result_receipt_id,),
        )
        if result is None:
            raise DurableCollaborationError("work_acceptance_result_missing")
        if (
            str(result["project_id"]) != project_id
            or str(result["coordination_session_id"]) != coordination_session_id
            or str(result["work_item_id"]) != work_item_id
            or str(result["result_sha256"]) != canonical.result_receipt_sha256
        ):
            raise DurableCollaborationError("work_acceptance_result_scope_mismatch")
        try:
            stored_result = json.loads(str(result["result_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("work_acceptance_result_corrupt") from exc
        if not isinstance(stored_result, Mapping):
            raise DurableCollaborationError("work_acceptance_result_corrupt")
        result_projection = dict(stored_result)
        result_projection.pop("lease_binding", None)
        if _sha256(result_projection) != str(result["result_sha256"]):
            raise DurableCollaborationError("work_acceptance_result_digest_mismatch")
        if (
            str(stored_result.get("receipt_id") or "") != canonical.result_receipt_id
            or str(stored_result.get("work_item_id") or "") != work_item_id
            or str(stored_result.get("work_receipt_sha256") or "") != work_digest
            or str(stored_result.get("outcome") or "") != "completed"
        ):
            raise DurableCollaborationError("work_acceptance_result_scope_mismatch")
        submitted_by = stored_result.get("submitted_by")
        if not isinstance(submitted_by, Mapping):
            raise DurableCollaborationError("work_acceptance_result_corrupt")
        if str(submitted_by.get("agent_id") or "") != str(work["assigned_agent_id"]):
            raise DurableCollaborationError("work_acceptance_result_submitter_mismatch")
        submitter = self._fetchone(
            "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
            (canonical.submitter_agent_session_id,),
        )
        if submitter is None:
            raise DurableCollaborationError("work_acceptance_submitter_session_missing")
        try:
            stored_submitter = json.loads(str(submitter["session_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("work_acceptance_submitter_session_corrupt") from exc
        if not isinstance(stored_submitter, Mapping) or _sha256(stored_submitter) != str(
            submitter["session_sha256"]
        ):
            raise DurableCollaborationError("work_acceptance_submitter_session_digest_mismatch")
        if (
            str(submitter["project_id"]) != project_id
            or str(submitter["coordination_session_id"]) != coordination_session_id
            or canonical.submitter_agent_session_sha256 != str(submitter["session_sha256"])
            or str(submitter["agent_id"]) != str(work["assigned_agent_id"])
        ):
            raise DurableCollaborationError("work_acceptance_submitter_scope_mismatch")
        if canonical.accepted_by.agent_id == str(work["assigned_agent_id"]):
            raise DurableCollaborationError("independent_reviewer_required")
        return canonical

    def _accepted_event(
        self,
        work: Mapping[str, Any],
        *,
        reviewer_session_id: str,
        acceptance: AcceptanceReceipt,
    ) -> CollaborationEvent:
        event_key = hashlib.sha256(
            f"{work['project_id']}:{work['coordination_session_id']}:{work['work_item_id']}:"
            f"accepted:{acceptance.content_sha256}".encode()
        ).hexdigest()[:40]
        payload = {
            "stage": "review",
            "decision": "accepted",
            "reviewer_session_id": reviewer_session_id,
            "work_receipt_sha256": str(work["work_receipt_sha256"]),
            "acceptance_receipt_id": acceptance.acceptance_receipt_id,
            "acceptance_receipt_sha256": acceptance.content_sha256,
            "bridge_kind": "accepted",
            "result_receipt_id": acceptance.result_receipt_id,
            "result_receipt_sha256": acceptance.result_receipt_sha256,
            "role_assignment_sha256": acceptance.reviewer_assignment_sha256,
            "reviewer_assignment_sha256": acceptance.reviewer_assignment_sha256,
            "submitter_assignment_sha256": acceptance.submitter_assignment_sha256,
        }
        return CollaborationEvent(
            event_id=f"event:work-accepted:{event_key}",
            project=ProjectScope(str(work["project_id"])),
            coordination_session_id=str(work["coordination_session_id"]),
            actor=acceptance.accepted_by,
            event_type="work.accepted",
            summary="Work accepted",
            created_at=acceptance.issued_at_utc,
            work_item_id=str(work["work_item_id"]),
            evidence_refs=acceptance.evidence_refs,
            payload=payload,
        )

    def _require_existing_accepted_event(self, expected: CollaborationEvent) -> None:
        row = self._fetchone(
            "SELECT * FROM collaboration_events WHERE event_id=?",
            (expected.event_id,),
        )
        if row is None:
            raise DurableCollaborationError("work_acceptance_event_missing")
        try:
            stored = json.loads(str(row["event_json"]))
            audience_roles = json.loads(str(row["audience_roles_json"]))
            audience_agent_ids = json.loads(str(row["audience_agent_ids_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("work_acceptance_event_corrupt") from exc
        if not isinstance(stored, Mapping):
            raise DurableCollaborationError("work_acceptance_event_corrupt")
        try:
            stored_event = CollaborationEvent.from_dict(stored)
        except CollaborationContractError as exc:
            raise DurableCollaborationError("work_acceptance_event_corrupt") from exc
        if stored_event.content_sha256 != str(row["event_sha256"]):
            raise DurableCollaborationError("work_acceptance_event_digest_mismatch")
        if (
            str(row["event_id"]) != stored_event.event_id
            or str(row["project_id"]) != stored_event.project.project_id
            or str(row["coordination_session_id"]) != stored_event.coordination_session_id
            or str(row["actor_agent_id"]) != stored_event.actor.agent_id
            or str(row["actor_role"]) != stored_event.actor.role
            or str(row["event_type"]) != stored_event.event_type
            or row["causal_parent_event_id"] != stored_event.causal_parent_event_id
            or audience_roles != list(stored_event.audience_roles)
            or audience_agent_ids != list(stored_event.audience_agent_ids)
            or str(row["created_at"]) != stored_event.created_at
            or row["expires_at"] != stored_event.expires_at
        ):
            raise DurableCollaborationError("work_acceptance_event_corrupt")
        stored_payload = dict(stored_event.payload)
        diagnostics = stored_payload.pop("_server_time_diagnostics", None)
        if not isinstance(diagnostics, Mapping):
            raise DurableCollaborationError("work_acceptance_event_conflict")
        if dict(diagnostics) != {
            "source_event_sha256": expected.content_sha256,
            "created_at": expected.created_at,
            "expires_at": expected.expires_at,
        }:
            raise DurableCollaborationError("work_acceptance_event_conflict")
        stored_projection = stored_event.to_dict()
        stored_projection["created_at"] = expected.created_at
        stored_projection["expires_at"] = expected.expires_at
        stored_projection["payload"] = stored_payload
        if stored_projection != expected.to_dict():
            raise DurableCollaborationError("work_acceptance_event_conflict")

    def get_work(self, work_item_id: str) -> dict[str, object] | None:
        row = self._fetchone(
            "SELECT * FROM collaboration_work_items WHERE work_item_id=?",
            (str(work_item_id or "").strip(),),
        )
        return row

    def assigned_work(self, project: ProjectScope, session_id: str) -> list[dict[str, object]]:
        if not isinstance(project, ProjectScope):
            raise DurableCollaborationError("work_project_required")
        session_id = _text(session_id, "session_id_required")
        rows = self._fetchall(
            """
            SELECT work.* FROM collaboration_work_items AS work
            JOIN collaboration_agent_sessions AS sessions
              ON sessions.agent_id = work.assigned_agent_id
             AND sessions.project_id = work.project_id
             AND sessions.coordination_session_id = work.coordination_session_id
            WHERE work.project_id=? AND sessions.session_id=?
              AND sessions.state = 'active'
              AND work.state IN ('proposed','ready','leased','in_progress','submitted','reviewing')
            ORDER BY work.updated_at, work.work_item_id
            """,
            (project.project_id, session_id),
        )
        return rows

    def assigned_work_projection(
        self,
        project: ProjectScope,
        session_id: str,
        *,
        limit: int = _SESSION_INIT_WORK_LIMIT,
    ) -> list[dict[str, object]]:
        """Return a bounded, non-authoritative work inbox projection.

        Stored WorkReceipt JSON is revalidated against its normalized columns
        and digest before any allowlisted display fields are returned.  Raw
        receipt/lease JSON, owner session ids, idempotency material and result
        payloads never cross this interface.
        """

        if not isinstance(project, ProjectScope):
            raise DurableCollaborationError("work_project_required")
        session_id = _text(session_id, "session_id_required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise DurableCollaborationError("work_projection_limit_invalid")
        session = self._fetchone(
            "SELECT project_id,coordination_session_id,agent_id,state "
            "FROM collaboration_agent_sessions WHERE session_id=?",
            (session_id,),
        )
        if session is None:
            raise DurableCollaborationError("agent_session_not_found")
        if str(session["project_id"]) != project.project_id:
            raise DurableCollaborationError("agent_session_scope_mismatch")
        self._require_agent_session(
            project_id=project.project_id,
            coordination_session_id=str(session["coordination_session_id"]),
            agent_id=str(session["agent_id"]),
            session_id=session_id,
            allowed_states=_HEARTBEAT_AGENT_SESSION_STATES,
        )
        rows = self._fetchall(
            """
            SELECT work.work_item_id,work.project_id,work.coordination_session_id,
                   work.work_receipt_json,work.work_receipt_sha256,
                   work.assigned_agent_id,work.state,work.attempt,
                   work.max_attempts,work.created_at,work.updated_at,
                   leases.fencing_generation AS lease_fencing_generation,
                   leases.expires_at AS lease_expires_at,
                   leases.last_heartbeat_at AS lease_last_heartbeat_at,
                   leases.state AS lease_state
              FROM collaboration_work_items AS work
              LEFT JOIN collaboration_work_leases AS leases
                ON leases.work_item_id=work.work_item_id
               AND leases.owner_session_id=?
               AND leases.state='active'
             WHERE work.project_id=?
               AND work.coordination_session_id=?
               AND work.assigned_agent_id=?
               AND work.state IN (
                    'proposed','ready','leased','in_progress','submitted','reviewing'
               )
             ORDER BY work.updated_at,work.work_item_id
             LIMIT ?
            """,
            (
                session_id,
                project.project_id,
                str(session["coordination_session_id"]),
                str(session["agent_id"]),
                limit,
            ),
        )
        projection: list[dict[str, object]] = []
        for row in rows:
            try:
                receipt_value = json.loads(str(row["work_receipt_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise DurableCollaborationError("work_receipt_projection_corrupt") from exc
            receipt = _work_receipt_from_projection(receipt_value)
            if (
                receipt.content_sha256 != str(row["work_receipt_sha256"])
                or receipt.work_item_id != str(row["work_item_id"])
                or receipt.project.project_id != str(row["project_id"])
                or receipt.coordination_session_id != str(row["coordination_session_id"])
                or receipt.assigned_agent.agent_id != str(row["assigned_agent_id"])
            ):
                raise DurableCollaborationError("work_receipt_projection_corrupt")
            objective = receipt.objective
            if len(objective) > _SESSION_INIT_OBJECTIVE_CHARS:
                objective = objective[: _SESSION_INIT_OBJECTIVE_CHARS - 1].rstrip() + "…"
            lease_state = str(row.get("lease_state") or "")
            projection.append(
                {
                    "work_item_id": receipt.work_item_id,
                    "state": _bounded_state(row["state"], _WORK_STATES, "work_state_corrupt"),
                    "objective_summary": objective,
                    "assigned_agent_id": receipt.assigned_agent.agent_id,
                    "attempt": int(row["attempt"]),
                    "max_attempts": int(row["max_attempts"]),
                    "dependency_work_ids": list(receipt.dependency_work_ids[:32]),
                    "fencing_generation": receipt.fencing_generation,
                    "expires_at": receipt.expires_at,
                    "receipt_sha256": receipt.content_sha256,
                    "updated_at": str(row["updated_at"]),
                    "lease": (
                        {
                            "state": lease_state,
                            "fencing_generation": int(row["lease_fencing_generation"]),
                            "expires_at": str(row["lease_expires_at"]),
                            "last_heartbeat_at": str(row["lease_last_heartbeat_at"]),
                        }
                        if lease_state
                        else None
                    ),
                    "authority_effect": "none",
                }
            )
        return projection

    def working_set_summary(self, *, session: AgentSession) -> dict[str, object]:
        """Summarize one exact project/coordination scope without raw rows."""

        if not isinstance(session, AgentSession):
            raise DurableCollaborationError("agent_session_required")
        self._require_agent_session(
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
            agent_id=session.identity.agent_id,
            session_id=session.session_id,
            allowed_states=_HEARTBEAT_AGENT_SESSION_STATES,
        )
        agent_counts = dict.fromkeys(("active", "idle", "stale", "closed"), 0)
        for row in self._fetchall(
            "SELECT state,COUNT(*) AS count FROM collaboration_agent_sessions "
            "WHERE project_id=? AND coordination_session_id=? GROUP BY state",
            (session.project.project_id, session.coordination_session_id),
        ):
            state = _bounded_state(
                row["state"], _AGENT_SESSION_STATES, "agent_session_state_corrupt"
            )
            if state == "registered":
                state = "active"
            agent_counts[state] = agent_counts.get(state, 0) + int(row["count"])

        work_counts = dict.fromkeys(sorted(_WORK_STATES), 0)
        for row in self._fetchall(
            "SELECT state,COUNT(*) AS count FROM collaboration_work_items "
            "WHERE project_id=? AND coordination_session_id=? GROUP BY state",
            (session.project.project_id, session.coordination_session_id),
        ):
            state = _bounded_state(row["state"], _WORK_STATES, "work_state_corrupt")
            work_counts[state] = int(row["count"])

        _, now_text = self._now()
        visibility_parameters = (
            session.project.project_id,
            session.coordination_session_id,
            session.identity.agent_id,
            session.identity.role,
            session.identity.agent_id,
            now_text,
        )
        event_counts: dict[str, int] = {}
        try:
            for event_type in ("blocker.raised", "conflict.detected"):
                row = self._fetchone(
                    """
                    SELECT COUNT(*) AS count
                      FROM collaboration_events
                     WHERE project_id=? AND coordination_session_id=?
                       AND (
                            (audience_roles_json='[]' AND audience_agent_ids_json='[]')
                            OR actor_agent_id=?
                            OR EXISTS (
                                SELECT 1 FROM json_each(audience_roles_json) WHERE value=?
                            )
                            OR EXISTS (
                                SELECT 1 FROM json_each(audience_agent_ids_json) WHERE value=?
                            )
                       )
                       AND (expires_at IS NULL OR expires_at>?)
                       AND event_type=?
                    """,
                    (*visibility_parameters, event_type),
                )
                event_counts[event_type] = int((row or {}).get("count") or 0)
        except sqlite3.OperationalError as exc:
            if "malformed json" not in str(exc).casefold():
                raise
            raise DurableCollaborationError("collaboration_event_projection_mismatch") from exc
        return {
            "schema_version": "collaboration-working-set-summary/v1",
            "agents": agent_counts,
            "work": work_counts,
            "blockers": event_counts.get("blocker.raised", 0),
            "conflicts": event_counts.get("conflict.detected", 0),
            "observed_at": now_text,
            "authority_effect": "none",
            "canonical_memory_effect": "none",
        }

    def peer_delta_page(
        self,
        *,
        session: AgentSession,
        after: EventCursor | None = None,
        limit: int = _SESSION_INIT_EVENT_LIMIT,
    ) -> dict[str, object]:
        """Read one redacted peer-event page without acknowledging its cursor."""

        if not isinstance(session, AgentSession):
            raise DurableCollaborationError("agent_session_required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise DurableCollaborationError("peer_delta_limit_invalid")
        self._require_agent_session(
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
            agent_id=session.identity.agent_id,
            session_id=session.session_id,
            allowed_states=_HEARTBEAT_AGENT_SESSION_STATES,
        )
        start = after or self.load_cursor(
            project=session.project,
            coordination_session_id=session.coordination_session_id,
            consumer_id=session.session_id,
        )
        event_log = CollaborationEventLog(
            connection=self._connection,
            clock=self._clock,
            ensure_schema=False,
        )
        try:
            page = event_log.read(
                project=session.project,
                coordination_session_id=session.coordination_session_id,
                audience=session.identity,
                after=start,
                limit=limit,
            )
        finally:
            event_log.close()
        source_head = self._connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM collaboration_events "
            "WHERE project_id=? AND coordination_session_id=?",
            (session.project.project_id, session.coordination_session_id),
        ).fetchone()
        items = tuple(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor": {
                    "agent_id": event.actor.agent_id,
                    "role": event.actor.role,
                },
                "summary": event.summary,
                "created_at": event.created_at,
                "expires_at": event.expires_at,
                "causal_parent_event_id": event.causal_parent_event_id,
                "work_item_id": event.work_item_id,
                "subject_refs": list(event.subject_refs[:32]),
                "evidence_refs": list(event.evidence_refs[:32]),
                "payload": "redacted",
                "authority_effect": "none",
            }
            for event in page.events
        )
        return {
            "schema_version": "collaboration-peer-delta/v1",
            "items": items,
            "after_cursor": start,
            "next_cursor": page.next_cursor,
            "source_head_sequence": int(source_head[0] if source_head else 0),
            "has_more": page.has_more,
            "limit": limit,
            "ack_required": page.next_cursor.sequence > start.sequence,
        }

    # ------------------------------------------------------------------
    # Event/cursor persistence and restart recovery
    # ------------------------------------------------------------------

    @_write_boundary
    def publish_workflow_stage_lifecycle_event(
        self,
        *,
        agent_session_id: str,
        execution_receipt_id: str,
        route_id: str,
        stage: str,
        step_index: int,
        lifecycle: str,
        reason_code: str = "",
    ) -> dict[str, object]:
        """Persist a bounded workflow ``started`` or ``blocked`` event.

        ``execution_receipt_id`` is deliberately named as a candidate on the
        started path: the official workflow receipt is not canonical until
        the later receipt/cursor transaction succeeds.  The event therefore
        carries only the deterministic attempt identity and the candidate
        receipt id; it never copies receipt evidence or caller narrative.
        ``blocked`` is fail-closed and requires the exact started event to be
        present before it can be emitted.
        """

        agent_session_id = _text(agent_session_id, "agent_session_id_required")
        execution_receipt_id = _text(
            execution_receipt_id,
            "workflow_execution_receipt_id_required",
        )
        route_id = _text(route_id, "workflow_route_id_required")
        stage = _text(stage, "workflow_stage_required")
        lifecycle = _bounded_state(
            lifecycle,
            _WORKFLOW_STAGE_LIFECYCLES,
            "workflow_stage_lifecycle_invalid",
        )
        if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
            raise DurableCollaborationError("workflow_step_index_invalid")
        reason_code = str(reason_code or "").strip().casefold()
        if lifecycle == "blocked":
            if reason_code not in _WORKFLOW_STAGE_BLOCK_REASONS:
                raise DurableCollaborationError("workflow_stage_block_reason_invalid")
        elif reason_code:
            raise DurableCollaborationError("workflow_stage_started_reason_forbidden")

        session = self._fetchone(
            "SELECT project_id,coordination_session_id,agent_id,identity_json "
            "FROM collaboration_agent_sessions WHERE session_id=?",
            (agent_session_id,),
        )
        if session is None:
            raise DurableCollaborationError("agent_session_not_found")
        actor = _identity_from_dict(json.loads(str(session["identity_json"])))
        project = ProjectScope(str(session["project_id"]))
        coordination_session_id = str(session["coordination_session_id"])
        self._require_active_agent_session(
            project_id=project.project_id,
            coordination_session_id=coordination_session_id,
            agent_id=str(session["agent_id"]),
            identity=actor,
            session_id=agent_session_id,
        )

        attempt_id = _workflow_attempt_id(
            agent_session_id=agent_session_id,
            project_id=project.project_id,
            coordination_session_id=coordination_session_id,
            execution_receipt_id=execution_receipt_id,
            route_id=route_id,
            stage=stage,
            step_index=step_index,
        )
        event_id = _workflow_stage_event_id(lifecycle, attempt_id)
        _, now_text = self._now()
        common_payload: dict[str, object] = {
            "route_id": route_id,
            "stage": stage,
            "step_index": step_index,
            "workflow_attempt_id": attempt_id,
            "candidate_execution_receipt_id": execution_receipt_id,
            "canonical_memory_effect": "none",
        }
        if lifecycle == "started":
            event = CollaborationEvent(
                event_id=event_id,
                project=project,
                coordination_session_id=coordination_session_id,
                actor=actor,
                event_type="workflow.stage_started",
                summary="Workflow stage started",
                created_at=now_text,
                subject_refs=(execution_receipt_id,),
                payload={**common_payload, "status": "started"},
            )
            existing = self._fetchone(
                "SELECT event_id FROM collaboration_events WHERE event_id=?",
                (event_id,),
            )
            cursor = self._append_event_in_transaction(
                event,
                actor_session_id=agent_session_id,
            )
            return {
                "schema_version": "workflow-stage-lifecycle/v1",
                "state": "durable",
                "persistent": True,
                "lifecycle": lifecycle,
                "workflow_attempt_id": attempt_id,
                "event_id": event_id,
                "event_type": event.event_type,
                "cursor": cursor.to_dict(),
                "replayed": existing is not None,
                "canonical_memory_effect": "none",
            }

        started_event_id = _workflow_stage_event_id("started", attempt_id)
        if (
            self._fetchone(
                "SELECT event_id FROM collaboration_events WHERE event_id=?",
                (started_event_id,),
            )
            is None
        ):
            raise DurableCollaborationError("workflow_stage_started_missing")
        event = CollaborationEvent(
            event_id=event_id,
            project=project,
            coordination_session_id=coordination_session_id,
            actor=actor,
            event_type="workflow.stage_blocked",
            summary="Workflow stage blocked",
            created_at=now_text,
            causal_parent_event_id=started_event_id,
            subject_refs=(execution_receipt_id,),
            payload={
                **common_payload,
                "status": "blocked",
                "reason_code": reason_code,
            },
        )
        existing = self._fetchone(
            "SELECT event_id FROM collaboration_events WHERE event_id=?",
            (event_id,),
        )
        cursor = self._append_event_in_transaction(
            event,
            actor_session_id=agent_session_id,
        )
        return {
            "schema_version": "workflow-stage-lifecycle/v1",
            "state": "durable",
            "persistent": True,
            "lifecycle": lifecycle,
            "workflow_attempt_id": attempt_id,
            "event_id": event_id,
            "event_type": event.event_type,
            "reason_code": reason_code,
            "causal_parent_event_id": started_event_id,
            "cursor": cursor.to_dict(),
            "replayed": existing is not None,
            "canonical_memory_effect": "none",
        }

    @_write_boundary
    def publish_workflow_receipt_events(
        self,
        *,
        agent_session_id: str,
        execution_receipt_id: str,
        route_id: str,
        stage: str,
        step_index: int,
    ) -> dict[str, object]:
        """Project one canonical workflow receipt into collaboration events.

        The official workflow receipt must already exist in the canonical
        SQLite store.  This method deliberately accepts only the exact durable
        AgentSession plus the bounded receipt coordinates: project, workflow
        scope, actor identity, receipt evidence, and server time are resolved
        again under the pp-core single-writer transaction.

        The handoff is receipt-first rather than cross-module exactly-once.
        Deterministic event ids and the append-only event log make replay a
        repair operation: a later identical call fills any missing event while
        a conflicting immutable event fails closed.
        """

        agent_session_id = _text(agent_session_id, "agent_session_id_required")
        execution_receipt_id = _text(
            execution_receipt_id,
            "workflow_execution_receipt_id_required",
        )
        route_id = _text(route_id, "workflow_route_id_required")
        stage = _text(stage, "workflow_stage_required")
        if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
            raise DurableCollaborationError("workflow_step_index_invalid")

        session = self._fetchone(
            "SELECT project_id,coordination_session_id,agent_id,identity_json "
            "FROM collaboration_agent_sessions WHERE session_id=?",
            (agent_session_id,),
        )
        if session is None:
            raise DurableCollaborationError("agent_session_not_found")
        actor = _identity_from_dict(json.loads(str(session["identity_json"])))
        project = ProjectScope(str(session["project_id"]))
        coordination_session_id = str(session["coordination_session_id"])
        self._require_active_agent_session(
            project_id=project.project_id,
            coordination_session_id=coordination_session_id,
            agent_id=str(session["agent_id"]),
            identity=actor,
            session_id=agent_session_id,
        )

        if not self._has_table("official_workflow_receipts"):
            raise DurableCollaborationError("workflow_receipt_store_missing")
        receipt = self._fetchone(
            """
            SELECT receipt_id,scope_id,route_id,step_index,stage,
                   upstream_revision,content_sha256,evidence_json
              FROM official_workflow_receipts
             WHERE receipt_id=?
            """,
            (execution_receipt_id,),
        )
        if receipt is None:
            raise DurableCollaborationError("workflow_receipt_not_found")
        if str(receipt["scope_id"]) != coordination_session_id:
            raise DurableCollaborationError("workflow_receipt_scope_mismatch")
        if str(receipt["route_id"]) != route_id:
            raise DurableCollaborationError("workflow_receipt_route_mismatch")
        if str(receipt["stage"]) != stage:
            raise DurableCollaborationError("workflow_receipt_stage_mismatch")
        if int(receipt["step_index"]) != step_index:
            raise DurableCollaborationError("workflow_receipt_step_mismatch")

        try:
            evidence = json.loads(str(receipt["evidence_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableCollaborationError("workflow_receipt_evidence_corrupt") from exc
        if not isinstance(evidence, Mapping):
            raise DurableCollaborationError("workflow_receipt_evidence_corrupt")

        receipt_sha256 = _sha256(
            {
                "schema_version": "official-workflow-receipt-handoff/v1",
                "execution_receipt_id": execution_receipt_id,
                "scope_id": coordination_session_id,
                "route_id": route_id,
                "step_index": step_index,
                "stage": stage,
                "upstream_revision": str(receipt["upstream_revision"]),
                "content_sha256": str(receipt["content_sha256"]),
                # Collaboration events bind the evidence without copying its
                # body into the peer-visible event stream.
                "evidence_sha256": _sha256(evidence),
            }
        )
        event_key = hashlib.sha256(
            _canonical_json(
                {
                    "project_id": project.project_id,
                    "coordination_session_id": coordination_session_id,
                    "execution_receipt_id": execution_receipt_id,
                    "receipt_sha256": receipt_sha256,
                }
            ).encode("utf-8")
        ).hexdigest()[:40]
        receipt_event_id = f"event:workflow-receipt:{event_key}"
        completed_event_id = f"event:workflow-completed:{event_key}"
        attempt_id = _workflow_attempt_id(
            agent_session_id=agent_session_id,
            project_id=project.project_id,
            coordination_session_id=coordination_session_id,
            execution_receipt_id=execution_receipt_id,
            route_id=route_id,
            stage=stage,
            step_index=step_index,
        )
        started_event_id = _workflow_stage_event_id("started", attempt_id)
        started_event = self._fetchone(
            "SELECT event_id FROM collaboration_events WHERE event_id=?",
            (started_event_id,),
        )
        if started_event is None:
            raise DurableCollaborationError("workflow_stage_started_missing")
        existing_event_ids = {
            event_id
            for event_id in (receipt_event_id, completed_event_id)
            if self._fetchone(
                "SELECT event_id FROM collaboration_events WHERE event_id=?",
                (event_id,),
            )
            is not None
        }
        _, now_text = self._now()
        common_payload: dict[str, object] = {
            "route_id": route_id,
            "stage": stage,
            "step_index": step_index,
            "execution_receipt_id": execution_receipt_id,
            "receipt_sha256": receipt_sha256,
            "canonical_memory_effect": "none",
        }
        receipt_event = CollaborationEvent(
            event_id=receipt_event_id,
            project=project,
            coordination_session_id=coordination_session_id,
            actor=actor,
            event_type="workflow.receipt_submitted",
            summary="Workflow receipt submitted",
            created_at=now_text,
            causal_parent_event_id=started_event_id if started_event is not None else None,
            subject_refs=(execution_receipt_id,),
            evidence_refs=(receipt_sha256,),
            payload={**common_payload, "status": "submitted"},
        )
        completed_event = CollaborationEvent(
            event_id=completed_event_id,
            project=project,
            coordination_session_id=coordination_session_id,
            actor=actor,
            event_type="workflow.stage_completed",
            summary="Workflow stage completed",
            created_at=now_text,
            causal_parent_event_id=receipt_event_id,
            subject_refs=(execution_receipt_id,),
            evidence_refs=(receipt_sha256,),
            payload={**common_payload, "status": "completed"},
        )
        receipt_cursor = self._append_event_in_transaction(
            receipt_event,
            actor_session_id=agent_session_id,
        )
        completed_cursor = self._append_event_in_transaction(
            completed_event,
            actor_session_id=agent_session_id,
        )
        return {
            "schema_version": "workflow-receipt-collaboration-handoff/v1",
            "state": "durable",
            "persistent": True,
            "execution_receipt_id": execution_receipt_id,
            "receipt_sha256": receipt_sha256,
            "event_ids": [receipt_event_id, completed_event_id],
            "event_types": [
                "workflow.receipt_submitted",
                "workflow.stage_completed",
            ],
            "cursor": completed_cursor.to_dict(),
            "receipt_cursor": receipt_cursor.to_dict(),
            "replayed": len(existing_event_ids) == 2,
            "canonical_memory_effect": "none",
        }

    @_write_boundary
    def append_event(
        self,
        event: CollaborationEvent,
        *,
        actor_session_id: str | None = None,
    ) -> EventCursor:
        return self._append_event_in_transaction(
            event,
            actor_session_id=actor_session_id,
        )

    def _append_event_in_transaction(
        self,
        event: CollaborationEvent,
        *,
        actor_session_id: str | None = None,
    ) -> EventCursor:
        """Append an event while the caller owns the single-writer transaction."""

        if not isinstance(event, CollaborationEvent):
            raise DurableCollaborationError("collaboration_event_required")
        if actor_session_id is not None:
            actor_session_id = _text(actor_session_id, "agent_session_id_required")
        self._require_active_agent_session(
            project_id=event.project.project_id,
            coordination_session_id=event.coordination_session_id,
            agent_id=event.actor.agent_id,
            identity=event.actor,
            session_id=actor_session_id,
        )
        event_log = CollaborationEventLog(
            connection=self._connection,
            clock=self._clock,
            ensure_schema=False,
        )
        try:
            cursor = event_log.append(
                event,
                retention_seconds=self._event_retention_seconds,
            )
        except TypeError as exc:
            # Keep the adapter compatible with older in-process event-log
            # doubles that only implement append(event).  Only this exact
            # signature mismatch is safe to retry; TypeError from event
            # validation or SQLite must propagate.
            if "retention_seconds" not in str(exc):
                raise
            cursor = event_log.append(event)
        stored = self._fetchone(
            "SELECT expires_at,event_sha256 FROM collaboration_events WHERE event_id=?",
            (event.event_id,),
        )
        if stored is None:
            raise DurableCollaborationError("collaboration_event_persistence_missing")
        _, now_text = self._now()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO collaboration_event_retention (
                event_id, project_id, coordination_session_id, sequence,
                expires_at, retention_state, cleaned_at, audit_digest
            ) VALUES (?, ?, ?, ?, ?, 'retained', '', ?)
            """,
            (
                event.event_id,
                event.project.project_id,
                event.coordination_session_id,
                cursor.sequence,
                stored["expires_at"]
                or canonical_text(
                    self._now()[0] + timedelta(seconds=self._event_retention_seconds)
                ),
                _sha256({"event_id": event.event_id, "event_sha256": stored["event_sha256"]}),
            ),
        )
        return cursor

    def load_cursor(
        self,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        consumer_id: str,
    ) -> EventCursor:
        if not isinstance(project, ProjectScope):
            raise DurableCollaborationError("cursor_project_required")
        coordination_session_id = _text(coordination_session_id, "coordination_session_required")
        consumer_id = _text(consumer_id, "cursor_consumer_required")
        row = self._fetchone(
            "SELECT sequence FROM collaboration_cursors WHERE project_id=? AND "
            "coordination_session_id=? AND consumer_id=?",
            (project.project_id, coordination_session_id, consumer_id),
        )
        sequence = 0 if row is None else int(row["sequence"])
        return EventCursor(project, coordination_session_id, sequence)

    @_write_boundary
    def record_cursor(
        self,
        cursor: EventCursor,
        *,
        consumer_id: str,
        source_head_sequence: int | None = None,
    ) -> EventCursor:
        return self._record_cursor_in_transaction(
            cursor,
            consumer_id=consumer_id,
            source_head_sequence=source_head_sequence,
        )

    def _record_cursor_in_transaction(
        self,
        cursor: EventCursor,
        *,
        consumer_id: str,
        source_head_sequence: int | None = None,
    ) -> EventCursor:
        """Record a cursor while the caller owns the single-writer transaction."""

        if not isinstance(cursor, EventCursor):
            raise DurableCollaborationError("cursor_required")
        consumer_id = _text(consumer_id, "cursor_consumer_required")
        session = self._fetchone(
            "SELECT project_id, coordination_session_id, agent_id, state FROM "
            "collaboration_agent_sessions WHERE session_id=?",
            (consumer_id,),
        )
        if session is None:
            raise DurableCollaborationError("cursor_consumer_session_missing")
        if (
            str(session["project_id"]) != cursor.project.project_id
            or str(session["coordination_session_id"]) != cursor.coordination_session_id
        ):
            raise DurableCollaborationError("cursor_consumer_scope_mismatch")
        self._require_active_agent_session(
            project_id=cursor.project.project_id,
            coordination_session_id=cursor.coordination_session_id,
            agent_id=str(session["agent_id"]),
            # ``consumer_id`` is the exact durable AgentSession that owns
            # this cursor.  Falling back to agent id alone would turn a
            # legitimate concurrent-session cursor update into ambiguity (or
            # worse, a future accidental "most recent" selection).
            session_id=consumer_id,
        )
        current = self.load_cursor(
            project=cursor.project,
            coordination_session_id=cursor.coordination_session_id,
            consumer_id=consumer_id,
        )
        if cursor.sequence < current.sequence:
            raise DurableCollaborationError("cursor_regression")
        head = cursor.sequence if source_head_sequence is None else int(source_head_sequence)
        if head < cursor.sequence or head < 0:
            raise DurableCollaborationError("cursor_head_invalid")
        actual_head = int(
            self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM collaboration_events "
                "WHERE project_id=? AND coordination_session_id=?",
                (cursor.project.project_id, cursor.coordination_session_id),
            ).fetchone()[0]
        )
        if head > actual_head:
            raise DurableCollaborationError("cursor_head_unknown")
        if cursor.sequence > 0:
            known = self._connection.execute(
                "SELECT 1 FROM collaboration_events WHERE project_id=? "
                "AND coordination_session_id=? AND sequence=?",
                (
                    cursor.project.project_id,
                    cursor.coordination_session_id,
                    cursor.sequence,
                ),
            ).fetchone()
            if known is None:
                raise DurableCollaborationError("cursor_sequence_unknown")
        _, now_text = self._now()
        self._connection.execute(
            """
            INSERT INTO collaboration_cursors (
                project_id, coordination_session_id, consumer_id,
                sequence, source_head_sequence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, coordination_session_id, consumer_id) DO UPDATE SET
                sequence=excluded.sequence,
                source_head_sequence=excluded.source_head_sequence,
                updated_at=excluded.updated_at
            """,
            (
                cursor.project.project_id,
                cursor.coordination_session_id,
                consumer_id,
                cursor.sequence,
                head,
                now_text,
            ),
        )
        self._connection.execute(
            "UPDATE collaboration_agent_sessions SET cursor_sequence=?, updated_at=? "
            "WHERE session_id=? AND project_id=? AND coordination_session_id=?",
            (
                cursor.sequence,
                now_text,
                consumer_id,
                cursor.project.project_id,
                cursor.coordination_session_id,
            ),
        )
        return cursor

    # ------------------------------------------------------------------
    # Maintenance / reconcile / SessionEnd lease release
    # ------------------------------------------------------------------

    @_write_boundary
    def reconcile(self) -> ReconcileReport:
        now, now_text = self._now()
        stale_before = now - timedelta(seconds=self._presence_timeout_seconds)
        stale_rows = self._fetchall(
            "SELECT session_id, project_id, agent_id, coordination_session_id "
            "FROM collaboration_agent_sessions "
            "WHERE state IN ('registered','active','idle') AND last_heartbeat_at <= ?",
            (canonical_text(stale_before),),
        )
        stale_ids = tuple(str(row["session_id"]) for row in stale_rows)
        abandoned: list[str] = []
        expired_work: list[str] = []
        for row in stale_rows:
            self._connection.execute(
                "UPDATE collaboration_agent_sessions SET state='stale', updated_at=? WHERE session_id=?",
                (now_text, row["session_id"]),
            )
            active_count = self._connection.execute(
                "SELECT COUNT(*) FROM collaboration_agent_sessions "
                "WHERE project_id=? AND coordination_session_id=? AND agent_id=? AND session_id<>? "
                "AND state IN ('registered','active','idle')",
                (
                    row["project_id"],
                    row["coordination_session_id"],
                    row["agent_id"],
                    row["session_id"],
                ),
            ).fetchone()[0]
            self._connection.execute(
                "UPDATE collaboration_agents SET state=?, updated_at=? "
                "WHERE project_id=? AND agent_id=?",
                (
                    "active" if int(active_count) else "stale",
                    now_text,
                    row["project_id"],
                    row["agent_id"],
                ),
            )
            stale_leases = self._fetchall(
                "SELECT leases.lease_id, leases.work_item_id, leases.attempt, "
                "work.max_attempts FROM collaboration_work_leases AS leases "
                "JOIN collaboration_work_items AS work ON work.work_item_id=leases.work_item_id "
                "WHERE leases.project_id=? AND leases.coordination_session_id=? "
                "AND leases.owner_id=? AND leases.owner_session_id=? AND leases.state='active'",
                (
                    row["project_id"],
                    row["coordination_session_id"],
                    row["agent_id"],
                    row["session_id"],
                ),
            )
            for lease in stale_leases:
                attempt = int(lease["attempt"])
                max_attempts = int(lease["max_attempts"] or attempt)
                next_state = "expired" if attempt >= max_attempts else "rework"
                self._connection.execute(
                    "UPDATE collaboration_work_leases SET state='abandoned', released_at=?, "
                    "release_reason='session_stale' WHERE lease_id=? AND state='active'",
                    (now_text, lease["lease_id"]),
                )
                self._connection.execute(
                    "UPDATE collaboration_work_items SET state=?, updated_at=?, last_error=? "
                    "WHERE work_item_id=?",
                    (next_state, now_text, "session_stale", lease["work_item_id"]),
                )
                abandoned.append(str(lease["lease_id"]))
                if next_state == "expired":
                    expired_work.append(str(lease["work_item_id"]))

        lease_cutoff = now - timedelta(seconds=self._lease_grace_seconds)
        leases = self._fetchall(
            "SELECT * FROM collaboration_work_leases WHERE state='active' AND expires_at <= ?",
            (canonical_text(lease_cutoff),),
        )
        for lease in leases:
            lease_id = str(lease["lease_id"])
            work_id = str(lease["work_item_id"])
            work = self._fetchone(
                "SELECT attempt, max_attempts FROM collaboration_work_items WHERE work_item_id=?",
                (work_id,),
            )
            attempt = int(work["attempt"] if work else lease["attempt"])
            max_attempts = int(work["max_attempts"] if work else lease["attempt"])
            next_state = "expired" if attempt >= max_attempts else "rework"
            lease_state = "expired" if next_state == "expired" else "abandoned"
            self._connection.execute(
                "UPDATE collaboration_work_leases SET state=?, released_at=?, release_reason=? "
                "WHERE lease_id=? AND state='active'",
                (lease_state, now_text, "heartbeat_timeout", lease_id),
            )
            self._connection.execute(
                "UPDATE collaboration_work_items SET state=?, updated_at=?, last_error=? WHERE work_item_id=?",
                (next_state, now_text, "lease_expired", work_id),
            )
            abandoned.append(lease_id)
            if next_state == "expired":
                expired_work.append(work_id)

        retained = self._retain_expired_events(now, now_text)
        retry_ids = self._retry_failed_promotions(now_text)
        return ReconcileReport(
            stale_session_ids=stale_ids,
            abandoned_lease_ids=tuple(abandoned),
            expired_work_ids=tuple(expired_work),
            retained_event_ids=tuple(retained),
            retried_promotion_ids=tuple(retry_ids),
        )

    def maintenance(self) -> dict[str, object]:
        """Run the bounded cleanup pass; no proposal adoption or memory delete."""

        report = self.reconcile()
        persisted = self.reconcile_promotions()
        return {
            "schema_version": "collaboration-maintenance/v1",
            "reconcile": report.to_dict(),
            "promotion": [item.to_dict() for item in persisted],
            "canonical_memory_mutation": False,
            "event_delete": False,
        }

    def _retain_expired_events(self, now: datetime, now_text: str) -> list[str]:
        if not self._has_table("collaboration_events"):
            return []
        cutoff = canonical_text(now)
        rows = self._fetchall(
            "SELECT retention.sequence,retention.event_id,retention.project_id,"
            "retention.coordination_session_id,retention.expires_at,events.event_sha256 "
            "FROM collaboration_event_retention AS retention "
            "JOIN collaboration_events AS events ON events.event_id=retention.event_id "
            "WHERE retention.expires_at IS NOT NULL AND retention.expires_at <= ? "
            "AND retention.cleaned_at=''",
            (cutoff,),
        )
        retained: list[str] = []
        for row in rows:
            event_id = str(row["event_id"])
            audit = _sha256(
                {
                    "event_id": event_id,
                    "event_sha256": row["event_sha256"],
                    "retained_at": now_text,
                }
            )
            self._connection.execute(
                """
                INSERT INTO collaboration_event_retention (
                    event_id, project_id, coordination_session_id, sequence,
                    expires_at, retention_state, cleaned_at, audit_digest
                ) VALUES (?, ?, ?, ?, ?, 'retained', '', ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    retention_state='retained', audit_digest=excluded.audit_digest
                """,
                (
                    event_id,
                    row["project_id"],
                    row["coordination_session_id"],
                    row["sequence"],
                    row["expires_at"],
                    audit,
                ),
            )
            # Append-only source remains intact; cleanup records only a
            # retention disposition for read-side filtering/audit.
            self._connection.execute(
                "UPDATE collaboration_event_retention SET cleaned_at=?, retention_state='released' "
                "WHERE event_id=? AND cleaned_at=''",
                (now_text, event_id),
            )
            retained.append(event_id)
        return retained

    def _release_session_leases(
        self,
        *,
        session_id: str,
        project_id: str,
        now_text: str,
        reason: str,
    ) -> list[str]:
        session_row = self._fetchone(
            "SELECT project_id,coordination_session_id,agent_id "
            "FROM collaboration_agent_sessions WHERE session_id=?",
            (session_id,),
        )
        if session_row is None:
            raise DurableCollaborationError("agent_session_not_found")
        if str(session_row["project_id"]) != project_id:
            raise DurableCollaborationError("agent_session_scope_mismatch")
        coordination_session_id = str(session_row["coordination_session_id"])
        agent_id = str(session_row["agent_id"])
        rows = self._fetchall(
            "SELECT leases.lease_id,leases.work_item_id,leases.attempt,work.max_attempts "
            "FROM collaboration_work_leases AS leases "
            "JOIN collaboration_work_items AS work ON work.work_item_id=leases.work_item_id "
            "WHERE leases.project_id=? AND leases.owner_id=? "
            "AND leases.owner_session_id=? AND leases.coordination_session_id=? "
            "AND leases.state='active'",
            (project_id, agent_id, session_id, coordination_session_id),
        )
        released: list[str] = []
        for row in rows:
            next_state = (
                "expired"
                if int(row["attempt"]) >= int(row["max_attempts"] or row["attempt"])
                else "rework"
            )
            self._connection.execute(
                "UPDATE collaboration_work_leases SET state='released', released_at=?, release_reason=? "
                "WHERE lease_id=? AND state='active'",
                (now_text, reason, row["lease_id"]),
            )
            self._connection.execute(
                "UPDATE collaboration_work_items SET state=?, updated_at=?, last_error=? WHERE work_item_id=?",
                (next_state, now_text, reason, row["work_item_id"]),
            )
            released.append(str(row["lease_id"]))
        return released

    # ------------------------------------------------------------------
    # Accepted result -> pending proposal (never adoption)
    # ------------------------------------------------------------------

    @_write_boundary
    def enqueue_promotion(
        self,
        candidate: PromotionCandidate,
        *,
        conflict_checked: bool = False,
        acceptance_receipt: AcceptanceReceipt | None = None,
    ) -> PendingPromotionResult:
        """Persist a verified candidate in the promotion outbox.

        ``conflict_checked`` is an explicit server-side gate.  The candidate
        itself carries evidence digests but cannot self-assert that conflict
        review happened.
        """

        if not isinstance(candidate, PromotionCandidate):
            raise DurableCollaborationError("promotion_candidate_required")
        if not conflict_checked:
            return PendingPromotionResult(
                status="rejected",
                candidate_id=candidate.candidate_id,
                reason="promotion_conflict_check_required",
            )
        if self._acceptance_authority is None or not isinstance(
            acceptance_receipt,
            AcceptanceReceipt,
        ):
            return PendingPromotionResult(
                status="rejected",
                candidate_id=candidate.candidate_id,
                reason="promotion_acceptance_authority_required",
            )
        try:
            canonical_acceptance = self._acceptance_authority.verify_issued(acceptance_receipt)
        except Exception:
            return PendingPromotionResult(
                status="rejected",
                candidate_id=candidate.candidate_id,
                reason="promotion_acceptance_receipt_unverified",
            )
        if canonical_acceptance.content_sha256 != candidate.acceptance_receipt_sha256:
            return PendingPromotionResult(
                status="rejected",
                candidate_id=candidate.candidate_id,
                reason="promotion_acceptance_digest_mismatch",
            )
        if (
            canonical_acceptance.project != candidate.project
            or canonical_acceptance.coordination_session_id != candidate.coordination_session_id
            or canonical_acceptance.work_item_id != candidate.work_item_id
            or canonical_acceptance.work_receipt_sha256 != candidate.work_receipt_sha256
            or canonical_acceptance.result_receipt_sha256 != candidate.result_receipt_sha256
            or canonical_acceptance.decision != "accepted"
            or canonical_acceptance.conflict_state == "unresolved"
        ):
            return PendingPromotionResult(
                status="rejected",
                candidate_id=candidate.candidate_id,
                reason="promotion_acceptance_binding_invalid",
            )
        self._verify_candidate_source(candidate)
        _, now_text = self._now()
        # Persist only the bounded candidate plus a server-verification marker;
        # the receipt itself is not a credential and can be revalidated by the
        # authority before this boundary is called again.
        payload = {
            "candidate": candidate.to_dict(),
            "acceptance_receipt_verified": True,
            "acceptance_receipt_sha256": canonical_acceptance.content_sha256,
        }
        candidate_json = _canonical_json(payload)
        digest = _sha256(payload)
        existing = self._fetchone(
            "SELECT candidate_sha256,status,proposal_id,attempts,failure_reason FROM "
            "collaboration_promotion_outbox WHERE candidate_id=?",
            (candidate.candidate_id,),
        )
        if existing is not None:
            if str(existing["candidate_sha256"]) != digest:
                raise DurableCollaborationError("promotion_candidate_conflict")
            return PendingPromotionResult(
                status=str(existing["status"]),
                candidate_id=candidate.candidate_id,
                proposal_id=str(existing["proposal_id"]) or None,
                reason=str(existing["failure_reason"] or ""),
                attempts=int(existing["attempts"]),
            )
        self._connection.execute(
            """
            INSERT INTO collaboration_promotion_outbox (
                candidate_id, project_id, candidate_json, candidate_sha256,
                idempotency_sha256, status, proposal_id, attempts,
                failure_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', '', 0, '', ?, ?)
            """,
            (
                candidate.candidate_id,
                candidate.project.project_id,
                candidate_json,
                digest,
                candidate.idempotency_sha256,
                now_text,
                now_text,
            ),
        )
        return PendingPromotionResult(status="pending", candidate_id=candidate.candidate_id)

    @_write_boundary
    def reconcile_promotions(self, *, limit: int = 100) -> tuple[PendingPromotionResult, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 1000:
            raise DurableCollaborationError("promotion_limit_invalid")
        rows = self._fetchall(
            "SELECT * FROM collaboration_promotion_outbox WHERE status IN ('pending','failed') "
            "AND attempts < 5 ORDER BY updated_at,candidate_id LIMIT ?",
            (limit,),
        )
        results: list[PendingPromotionResult] = []
        for row in rows:
            candidate_id = str(row["candidate_id"])
            attempts = int(row["attempts"] or 0) + 1
            try:
                candidate = self._candidate_from_dict(json.loads(row["candidate_json"]))
                self._verify_promotion_schema()
                proposal_store = MemoryProposalStore(self._connection, ensure_schema=False)
                # This is a server-side internal route, not a user-originated
                # memory write.  Keep the proposal policy gate active while
                # explicitly proving that the accepted-work bridge is a
                # trusted producer.  The resulting row remains pending and
                # adoption is still a separate governed operation.
                with trusted_memory_origin("collaboration-promotion"):
                    proposal_rows = proposal_store.create_many(
                        [
                            ProposalCandidate(
                                content=candidate.summary,
                                category="fact",
                                project_id=candidate.project.project_id,
                                visibility="project",
                                origin_role="system",
                                origin_turn_hash=candidate.idempotency_sha256,
                                origin_call_id=candidate.source_event_id,
                                metadata={
                                    "collaboration_candidate_id": candidate.candidate_id,
                                    "source_event_sha256": candidate.source_event_sha256,
                                    "work_receipt_sha256": candidate.work_receipt_sha256,
                                    "result_receipt_sha256": candidate.result_receipt_sha256,
                                    "acceptance_receipt_sha256": candidate.acceptance_receipt_sha256,
                                    "evidence_refs": list(candidate.evidence_refs),
                                },
                            )
                        ]
                    )
                proposal_id = str(proposal_rows[0]["proposal_id"])
                _, now_text = self._now()
                self._connection.execute(
                    "UPDATE collaboration_promotion_outbox SET status='persisted', proposal_id=?, "
                    "attempts=?, failure_reason='', updated_at=? WHERE candidate_id=?",
                    (proposal_id, attempts, now_text, candidate_id),
                )
                results.append(
                    PendingPromotionResult(
                        status="persisted",
                        candidate_id=candidate_id,
                        proposal_id=proposal_id,
                        attempts=attempts,
                    )
                )
            except Exception as exc:
                _, now_text = self._now()
                reason = _stable_reason(exc)
                self._connection.execute(
                    "UPDATE collaboration_promotion_outbox SET status='failed', attempts=?, "
                    "failure_reason=?, updated_at=? WHERE candidate_id=?",
                    (attempts, reason, now_text, candidate_id),
                )
                results.append(
                    PendingPromotionResult(
                        status="failed",
                        candidate_id=candidate_id,
                        reason=reason,
                        attempts=attempts,
                    )
                )
        return tuple(results)

    def _retry_failed_promotions(self, now_text: str) -> list[str]:
        rows = self._fetchall(
            "SELECT candidate_id FROM collaboration_promotion_outbox WHERE status='failed' "
            "AND attempts < 5 ORDER BY updated_at LIMIT 100"
        )
        return [str(row["candidate_id"]) for row in rows]

    def _verify_promotion_schema(self) -> None:
        """Fail closed without creating or rebuilding the canonical proposal table."""

        if not self._has_table("memory_proposals"):
            raise DurableCollaborationError("promotion_schema_missing")
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(memory_proposals)").fetchall()
        }
        required = {
            "proposal_id",
            "project_id",
            "visibility",
            "origin_visibility",
            "content",
            "content_hash",
            "category",
            "origin_role",
            "origin_turn_hash",
            "origin_call_id",
            "status",
            "metadata_json",
            "expires_at",
            "created_at",
            "updated_at",
        }
        if not required.issubset(columns):
            raise DurableCollaborationError("promotion_schema_stale")
        table_row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_proposals'"
        ).fetchone()
        normalized = " ".join(str(table_row[0] or "").lower().split()) if table_row else ""
        required_fragments = (
            "origin_visibility",
            "unique(project_id, origin_turn_hash, content_hash)",
            "origin_role in ('user', 'system')",
            "status not in ('adopted', 'rejected')",
            "status != 'adopted' or length(trim(promoted_memory_id)) > 0",
            "between 1 and 500",
        )
        if not all(fragment in normalized for fragment in required_fragments):
            raise DurableCollaborationError("promotion_schema_stale")

    def _verify_candidate_source(self, candidate: PromotionCandidate) -> None:
        row = self._fetchone(
            "SELECT event_json,event_sha256,event_type,project_id,coordination_session_id FROM "
            "collaboration_events WHERE event_id=?",
            (candidate.source_event_id,),
        )
        if row is None:
            raise DurableCollaborationError("promotion_source_event_missing")
        canonical_event_sha256 = str(row["event_sha256"])
        source_event_sha256 = ""
        try:
            stored_event = json.loads(str(row["event_json"]))
            stored_payload = (
                stored_event.get("payload", {}) if isinstance(stored_event, Mapping) else {}
            )
            diagnostics = (
                stored_payload.get("_server_time_diagnostics", {})
                if isinstance(stored_payload, Mapping)
                else {}
            )
            source_event_sha256 = (
                str(diagnostics.get("source_event_sha256") or "")
                if isinstance(diagnostics, Mapping)
                else ""
            )
        except (TypeError, json.JSONDecodeError):
            raise DurableCollaborationError("promotion_source_event_corrupt") from None
        if candidate.source_event_sha256 not in {
            canonical_event_sha256,
            source_event_sha256,
        }:
            raise DurableCollaborationError("promotion_source_event_digest_mismatch")
        if str(row["event_type"]) != "work.accepted":
            raise DurableCollaborationError("promotion_source_not_accepted")
        if (
            str(row["project_id"]) != candidate.project.project_id
            or str(row["coordination_session_id"]) != candidate.coordination_session_id
        ):
            raise DurableCollaborationError("promotion_source_scope_mismatch")
        payload = stored_event.get("payload", {}) if isinstance(stored_event, Mapping) else {}
        if not isinstance(payload, Mapping) or payload.get("bridge_kind") != "accepted":
            raise DurableCollaborationError("promotion_source_acceptance_missing")
        if payload.get("result_receipt_sha256") != candidate.result_receipt_sha256:
            raise DurableCollaborationError("promotion_result_digest_mismatch")
        if payload.get("acceptance_receipt_sha256") != candidate.acceptance_receipt_sha256:
            raise DurableCollaborationError("promotion_acceptance_digest_mismatch")

    @staticmethod
    def _candidate_from_dict(value: Mapping[str, object]) -> PromotionCandidate:
        try:
            candidate_value = (
                value.get("candidate") if isinstance(value.get("candidate"), Mapping) else value
            )
            return PromotionCandidate(
                candidate_id=str(candidate_value["candidate_id"]),
                project=ProjectScope(str(candidate_value["project_id"])),
                coordination_session_id=str(candidate_value["coordination_session_id"]),
                work_item_id=str(candidate_value["work_item_id"]),
                source_event_id=str(candidate_value["source_event_id"]),
                source_event_sha256=str(candidate_value["source_event_sha256"]),
                work_receipt_sha256=str(candidate_value["work_receipt_sha256"]),
                result_receipt_sha256=str(candidate_value["result_receipt_sha256"]),
                acceptance_receipt_sha256=str(candidate_value["acceptance_receipt_sha256"]),
                summary=str(candidate_value["summary"]),
                evidence_refs=tuple(str(item) for item in candidate_value.get("evidence_refs", ())),
                idempotency_sha256=str(candidate_value["idempotency_sha256"]),
                reason_code=str(
                    candidate_value.get("reason_code")
                    or "passive_bridge_accepted_work_pending_proposal"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DurableCollaborationError("promotion_candidate_corrupt") from exc


class CollaborationMemoryPromoter:
    """Named PR5 promoter façade; adoption remains outside this class."""

    def __init__(self, runtime: DurableCollaborationRuntime) -> None:
        if not isinstance(runtime, DurableCollaborationRuntime):
            raise DurableCollaborationError("promoter_runtime_required")
        self.runtime = runtime

    def submit(
        self,
        candidate: PromotionCandidate,
        *,
        conflict_checked: bool = False,
        acceptance_receipt: AcceptanceReceipt | None = None,
    ) -> PendingPromotionResult:
        return self.runtime.enqueue_promotion(
            candidate,
            conflict_checked=conflict_checked,
            acceptance_receipt=acceptance_receipt,
        )

    def reconcile(self, *, limit: int = 100) -> tuple[PendingPromotionResult, ...]:
        return self.runtime.reconcile_promotions(limit=limit)


def _stable_reason(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = exc.__class__.__name__.strip().casefold()
    return text or "promotion_failed"


__all__ = [
    "CollaborationMemoryPromoter",
    "DURABLE_COLLABORATION_REVISION",
    "DURABLE_COLLABORATION_SCHEMA",
    "DurableCollaborationError",
    "DurableCollaborationRuntime",
    "PendingPromotionResult",
    "ReconcileReport",
    "SessionInitResult",
]
