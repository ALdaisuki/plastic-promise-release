from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import AgentIdentity, AgentSession, ProjectScope
from plastic_promise.collaboration.coordination_plan import (
    TOKEN_AUTHORITY_PROVIDER,
    CoordinationPlan,
    CoordinationPlanAuthority,
    CoordinationPlanError,
    DelegationEdge,
    ResourceAllocation,
    ResponsibilityNode,
    TopLevelAgentBindingAuthority,
    UserMandate,
)
from plastic_promise.collaboration.durable_coordination_plan_store import (
    DURABLE_COORDINATION_PLAN_REQUIRED_TABLES,
    DURABLE_COORDINATION_PLAN_SCHEMA_DDL,
    DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,
    DurableCoordinationPlanRepository,
)

NOW = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:coordination-plan")


def _time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _session(session_id: str, *, role: str = "participant") -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=AgentIdentity(
            agent_id=session_id.replace("agent-session", "agent"),
            role=role,
        ),
        project=PROJECT,
        coordination_session_id="coord:one",
        state="active",
        started_at=_time(NOW - timedelta(minutes=1)),
        last_heartbeat_at=_time(NOW),
        expires_at=_time(NOW + timedelta(hours=2)),
    )


def _allocation(slots: int, tokens: int) -> ResourceAllocation:
    return ResourceAllocation(
        agent_slots=slots,
        token_budget=tokens,
        token_budget_authority=TOKEN_AUTHORITY_PROVIDER,
    )


def _node(
    suffix: str,
    *,
    allocation: ResourceAllocation,
    can_delegate: bool,
) -> ResponsibilityNode:
    return ResponsibilityNode(
        node_id=f"node:{suffix}",
        work_item_id=f"work:{suffix}",
        role_intent=f"role.{suffix}",
        scope=f"Bounded {suffix} responsibility",
        allowed_paths=("plastic_promise/",),
        allowed_tools=("git.read",),
        acceptance_conditions=(f"{suffix} accepted",),
        allocation=allocation,
        can_delegate=can_delegate,
    )


def _plan() -> CoordinationPlan:
    mandate = UserMandate(
        mandate_id="mandate:one",
        project=PROJECT,
        coordination_session_id="coord:one",
        user_instruction_sha256="sha256:" + "1" * 64,
        objective="Complete one bounded collaboration program",
        constraints=("Only the Top-Level Agent may activate a successor plan",),
        issued_at_utc=_time(NOW),
        expires_at_utc=_time(NOW + timedelta(hours=4)),
    )
    root = _node("root", allocation=_allocation(2, 100), can_delegate=True)
    child = _node("worker", allocation=_allocation(1, 20), can_delegate=False)
    return CoordinationPlan(
        plan_id="plan:one",
        plan_revision=1,
        mandate=mandate,
        top_level_agent_session_id="agent-session:root",
        root_node_id=root.node_id,
        nodes=(root, child),
        edges=(DelegationEdge(root.node_id, child.node_id),),
        total_allocation=root.allocation,
        created_at_utc=_time(NOW + timedelta(seconds=1)),
        expires_at_utc=_time(NOW + timedelta(hours=3)),
    )


class _TrustedTopLevelAuthorizationVerifier:
    """Test-only replacement for the server-owned user authorization verifier."""

    def authorize(self, *, mandate: UserMandate, session: AgentSession) -> None:
        if (
            session.session_id != "agent-session:root"
            or session.project != mandate.project
            or session.coordination_session_id != mandate.coordination_session_id
        ):
            raise CoordinationPlanError("coordination_plan_top_level_user_authorization_rejected")


def _binding_authority(
    repository: DurableCoordinationPlanRepository,
) -> TopLevelAgentBindingAuthority:
    return TopLevelAgentBindingAuthority(
        repository=repository,
        authorization_verifier=_TrustedTopLevelAuthorizationVerifier(),
        clock=lambda: NOW + timedelta(seconds=5),
    )


def _base_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE collaboration_agents (
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
        );
        CREATE TABLE collaboration_agent_sessions (
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
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id, agent_id)
                REFERENCES collaboration_agents(project_id, agent_id)
        );
        """
    )
    for session in (_session("agent-session:root"), _session("agent-session:worker")):
        identity_json = json.dumps(
            session.identity.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        session_json = json.dumps(
            session.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = _time(NOW)
        connection.execute(
            """
            INSERT INTO collaboration_agents (
                project_id, agent_id, role, parent_agent_id, capabilities_json,
                identity_json, state, first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                session.project.project_id,
                session.identity.agent_id,
                session.identity.role,
                session.identity.parent_agent_id,
                json.dumps(list(session.identity.capabilities), separators=(",", ":")),
                identity_json,
                now,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO collaboration_agent_sessions (
                session_id, project_id, agent_id, coordination_session_id,
                identity_json, session_json, session_sha256, state,
                started_at, last_heartbeat_at, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.project.project_id,
                session.identity.agent_id,
                session.coordination_session_id,
                identity_json,
                session_json,
                session.content_sha256,
                session.started_at,
                session.last_heartbeat_at,
                session.expires_at,
                now,
            ),
        )
    for statement in DURABLE_COORDINATION_PLAN_SCHEMA_DDL:
        connection.execute(statement)
    connection.execute(
        f"INSERT INTO {DURABLE_COORDINATION_PLAN_REQUIRED_TABLES[0]} "
        "(singleton, schema_revision, installed_at_utc) VALUES (1, ?, ?)",
        (DURABLE_COORDINATION_PLAN_SCHEMA_REVISION, _time(NOW)),
    )
    connection.commit()
    _binding_authority(_repository(connection)).bind_top_level_session(
        _plan().mandate,
        agent_session_id="agent-session:root",
    )
    return connection


@contextmanager
def _writer(connection: sqlite3.Connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _repository(connection: sqlite3.Connection) -> DurableCoordinationPlanRepository:
    return DurableCoordinationPlanRepository(
        connection,
        transaction_factory=lambda: _writer(connection),
        clock=lambda: NOW + timedelta(seconds=5),
    )


def test_adapter_is_verify_only_and_rejects_missing_or_weakened_schema() -> None:
    connection = sqlite3.connect(":memory:")
    with pytest.raises(
        CoordinationPlanError,
        match="coordination_plan_durable_foreign_keys_required",
    ):
        _repository(connection)

    connection = _base_connection()
    connection.execute(
        "DROP TRIGGER collaboration_coordination_plan_plans_no_update"
    ) if False else None
    # A migration-owned table may exist but a missing append-only trigger is
    # still a stale schema; runtime does not repair it.
    connection.execute("DROP TRIGGER collaboration_coordination_plans_no_update")
    with pytest.raises(CoordinationPlanError, match="coordination_plan_durable_schema_stale"):
        _repository(connection)


def test_activation_rehydrates_after_restart_and_replays_exactly() -> None:
    connection = _base_connection()
    repository = _repository(connection)
    authority = CoordinationPlanAuthority(
        repository=repository, clock=lambda: NOW + timedelta(seconds=5)
    )
    plan = _plan()

    first = authority.activate(plan, actor_session_id="agent-session:root")
    restarted = _repository(connection)
    restarted_authority = CoordinationPlanAuthority(
        repository=restarted,
        clock=lambda: NOW + timedelta(seconds=5),
    )
    assert (
        restarted_authority.verify_current(
            plan_sha256=plan.content_sha256,
            activation_sha256=first.activation.activation_sha256,
        )
        == first
    )
    assert restarted_authority.activate(plan, actor_session_id="agent-session:root") == first


def test_successor_uses_current_head_cas_and_preserves_historical_verify() -> None:
    connection = _base_connection()
    authority = CoordinationPlanAuthority(
        repository=_repository(connection),
        clock=lambda: NOW + timedelta(seconds=5),
    )
    previous = _plan()
    first = authority.activate(previous, actor_session_id="agent-session:root")
    successor = replace(
        previous,
        plan_revision=2,
        created_at_utc=_time(NOW + timedelta(seconds=2)),
        supersedes_plan_sha256=previous.content_sha256,
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_generation_conflict"):
        authority.activate(successor, actor_session_id="agent-session:root")
    current = authority.activate(
        successor,
        actor_session_id="agent-session:root",
        expected_current_activation_sha256=first.activation.activation_sha256,
    )
    assert authority.verify_issued(
        plan_sha256=previous.content_sha256,
        activation_sha256=first.activation.activation_sha256,
    ) == (previous, first.activation)
    with pytest.raises(CoordinationPlanError, match="coordination_plan_not_current"):
        authority.verify_current(
            plan_sha256=previous.content_sha256,
            activation_sha256=first.activation.activation_sha256,
        )
    assert current.plan == successor


def test_top_level_binding_and_canonical_rows_fail_closed_on_substitution_or_tamper() -> None:
    connection = _base_connection()
    authority = CoordinationPlanAuthority(
        repository=_repository(connection),
        clock=lambda: NOW + timedelta(seconds=5),
    )
    plan = _plan()
    verified = authority.activate(plan, actor_session_id="agent-session:root")

    foreign_top_level = replace(plan, top_level_agent_session_id="agent-session:worker")
    with pytest.raises(CoordinationPlanError, match="coordination_plan_top_level_session_mismatch"):
        authority.activate(foreign_top_level, actor_session_id="agent-session:worker")

    connection.execute("DROP TRIGGER collaboration_coordination_plan_top_level_bindings_no_update")
    connection.execute(
        "UPDATE collaboration_coordination_plan_top_level_bindings "
        "SET mandate_sha256=? WHERE project_id=? AND coordination_session_id=?",
        ("sha256:" + "f" * 64, PROJECT.project_id, "coord:one"),
    )
    connection.execute(
        """
        CREATE TRIGGER collaboration_coordination_plan_top_level_bindings_no_update
        BEFORE UPDATE ON collaboration_coordination_plan_top_level_bindings
        BEGIN
            SELECT RAISE(ABORT, 'coordination_plan_top_level_binding_append_only');
        END
        """
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_top_level_binding_corrupt"):
        _repository(connection).load_top_level_binding(
            project_id=PROJECT.project_id,
            coordination_session_id="coord:one",
        )

    connection.execute("DROP TRIGGER collaboration_coordination_plans_no_update")
    connection.execute(
        "UPDATE collaboration_coordination_plans SET project_id=? WHERE plan_sha256=?",
        ("project:tampered", plan.content_sha256),
    )
    connection.execute(
        """
        CREATE TRIGGER collaboration_coordination_plans_no_update
        BEFORE UPDATE ON collaboration_coordination_plans
        BEGIN
            SELECT RAISE(ABORT, 'coordination_plan_plan_append_only');
        END
        """
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_durable_record_corrupt"):
        _repository(connection).load_plan_by_digest(plan.content_sha256)

    # Restore the plan row and tamper the activation JSON; both the canonical
    # JSON digest and the denormalized activation projection are verified.
    connection.execute("DROP TRIGGER collaboration_coordination_plans_no_update")
    connection.execute(
        "UPDATE collaboration_coordination_plans SET project_id=? WHERE plan_sha256=?",
        (PROJECT.project_id, plan.content_sha256),
    )
    connection.execute(
        """
        CREATE TRIGGER collaboration_coordination_plans_no_update
        BEFORE UPDATE ON collaboration_coordination_plans
        BEGIN
            SELECT RAISE(ABORT, 'coordination_plan_plan_append_only');
        END
        """
    )
    connection.execute("DROP TRIGGER collaboration_coordination_plan_activations_no_update")
    connection.execute(
        "UPDATE collaboration_coordination_plan_activations SET activation_json=? WHERE activation_sha256=?",
        (
            verified.activation.canonical_json().replace('plan_revision":1', 'plan_revision":2'),
            verified.activation.activation_sha256,
        ),
    )
    connection.execute(
        """
        CREATE TRIGGER collaboration_coordination_plan_activations_no_update
        BEFORE UPDATE ON collaboration_coordination_plan_activations
        BEGIN
            SELECT RAISE(ABORT, 'coordination_plan_activation_append_only');
        END
        """
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_durable_record_corrupt"):
        _repository(connection).load_activation_by_digest(verified.activation.activation_sha256)


def test_provider_budget_is_enforced_but_agent_estimate_is_not_a_hard_ceiling() -> None:
    connection = _base_connection()
    authority = CoordinationPlanAuthority(
        repository=_repository(connection),
        clock=lambda: NOW + timedelta(seconds=5),
    )
    verified = authority.activate(_plan(), actor_session_id="agent-session:root")
    provider = authority.record_usage(
        verified,
        receipt_id="usage:provider",
        responsibility_node_id="node:worker",
        agent_session_id="agent-session:worker",
        token_usage=20,
        token_measurement=TOKEN_AUTHORITY_PROVIDER,
        measurement_evidence_sha256="sha256:" + "a" * 64,
    )
    assert provider.token_usage == 20
    with pytest.raises(CoordinationPlanError, match="resource_usage_token_budget_exceeded"):
        authority.record_usage(
            verified,
            receipt_id="usage:provider-over",
            responsibility_node_id="node:worker",
            agent_session_id="agent-session:worker",
            token_usage=1,
            token_measurement=TOKEN_AUTHORITY_PROVIDER,
            measurement_evidence_sha256="sha256:" + "b" * 64,
        )
    estimate = authority.record_usage(
        verified,
        receipt_id="usage:estimate",
        responsibility_node_id="node:worker",
        agent_session_id="agent-session:worker",
        token_usage=999999,
        token_measurement="agent-estimate",
    )
    assert estimate.token_measurement == "agent-estimate"
    assert (
        _repository(connection).total_provider_token_usage(
            plan_sha256=verified.plan.content_sha256,
            responsibility_node_id="node:worker",
        )
        == 20
    )


def test_current_only_usage_rejects_superseded_verified_plan() -> None:
    connection = _base_connection()
    authority = CoordinationPlanAuthority(
        repository=_repository(connection),
        clock=lambda: NOW + timedelta(seconds=5),
    )
    previous = _plan()
    first = authority.activate(previous, actor_session_id="agent-session:root")
    successor = replace(
        previous,
        plan_revision=2,
        created_at_utc=_time(NOW + timedelta(seconds=2)),
        supersedes_plan_sha256=previous.content_sha256,
    )
    authority.activate(
        successor,
        actor_session_id="agent-session:root",
        expected_current_activation_sha256=first.activation.activation_sha256,
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_not_current"):
        authority.record_usage(
            first,
            receipt_id="usage:stale",
            responsibility_node_id="node:worker",
            agent_session_id="agent-session:worker",
            token_usage=1,
            token_measurement="agent-estimate",
        )
