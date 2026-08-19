from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import plastic_promise.collaboration as collaboration_api
from plastic_promise.collaboration.acceptance_receipt import (
    REVIEW_CHANNELS,
    AcceptanceReceiptAuthority,
    ReviewReceipt,
    open_server_acceptance_receipt_authority,
    open_server_acceptance_source_registry,
)
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.durable_role_store import (
    DurableRoleAssignmentRepository,
)
from plastic_promise.collaboration.durable_runtime import (
    DurableCollaborationError,
    DurableCollaborationRuntime,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    LeaseHeartbeat,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.passive_bridge import PromotionCandidate
from plastic_promise.collaboration.policy_binding import (
    ACCEPTANCE_REVIEW_POLICY_REVISION,
    AGENT_POLICY_REVISION,
    AgentPolicyBindingAuthority,
    open_server_agent_policy_binding_authority,
)
from plastic_promise.collaboration.role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    InMemoryRoleAssignmentRepository,
    RoleAssignmentAuthority,
    RoleAssignmentBasis,
    open_server_role_assignment_authority,
)
from plastic_promise.core.memory_proposals import ensure_memory_proposal_schema
from plastic_promise.core.workflow_state import commit_workflow_transition
from tests.pr5_schema_fixture import install_pr5_collaboration_schema

SERVER_NOW = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime = SERVER_NOW

    def __call__(self) -> datetime:
        return self.value


class ReentrantWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.depth = 0

    @contextmanager
    def transaction(self):
        outer = self.depth == 0 and not self.connection.in_transaction
        self.depth += 1
        if outer:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if outer:
                self.connection.rollback()
            raise
        else:
            if outer:
                self.connection.commit()
        finally:
            self.depth -= 1


class InjectingWriter(ReentrantWriter):
    """Run one deterministic competing write after the outer transaction opens."""

    def __init__(self, connection: sqlite3.Connection, before_outer_write) -> None:
        super().__init__(connection)
        self._before_outer_write = before_outer_write

    @contextmanager
    def transaction(self):
        outer = self.depth == 0 and not self.connection.in_transaction
        self.depth += 1
        if outer:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            if outer and self._before_outer_write is not None:
                callback = self._before_outer_write
                self._before_outer_write = None
                callback()
            yield
        except BaseException:
            if outer:
                self.connection.rollback()
            raise
        else:
            if outer:
                self.connection.commit()
        finally:
            self.depth -= 1


class FailOnceWriter(ReentrantWriter):
    """Abort one outer transaction after its body has run.

    This models a transient commit/connection failure without weakening the
    append-only schema.  The official workflow receipt is written by a
    separate transaction, so a later replay must be able to repair the
    collaboration-event handoff.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.fail_next = True

    @contextmanager
    def transaction(self):
        outer = self.depth == 0 and not self.connection.in_transaction
        self.depth += 1
        if outer:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            if outer and self.fail_next:
                self.fail_next = False
                raise sqlite3.OperationalError("transient collaboration writer failure")
        except BaseException:
            if outer:
                self.connection.rollback()
            raise
        else:
            if outer:
                self.connection.commit()
        finally:
            self.depth -= 1


def _runtime() -> tuple[sqlite3.Connection, DurableCollaborationRuntime, MutableClock]:
    connection = sqlite3.connect(":memory:")
    writer = ReentrantWriter(connection)
    # The durable runtime must never create or mutate canonical memory rows,
    # but this fixture still needs a minimal canonical table so that the
    # pending-only assertion below can prove the table remains empty.
    connection.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, project_id TEXT NOT NULL)"
    )
    install_pr5_collaboration_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=lambda: SERVER_NOW,
        suffix="pr5-runtime",
    )
    clock = MutableClock()
    policy_authority = open_server_agent_policy_binding_authority(clock=clock)
    role_repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=clock,
    )
    runtime = DurableCollaborationRuntime(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        policy_authority=policy_authority,
        role_assignment_repository=role_repository,
        role_assignment_authority=role_authority,
        presence_timeout_seconds=7200,
        event_retention_seconds=60,
    )
    return connection, runtime, clock


def test_runtime_schema_installation_is_deployment_owned() -> None:
    assert not hasattr(DurableCollaborationRuntime, "install_schema")
    assert not hasattr(collaboration_api, "DurableCollaborationSchemaGrant")
    assert not hasattr(collaboration_api, "DurableCollaborationSchemaAuthority")
    assert not hasattr(collaboration_api, "open_durable_collaboration_schema_authority")


def _official_workflow_receipt(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    stage: str = "codebase-design",
    route_id: str = "codebase-design",
    step_index: int = 0,
) -> str:
    return commit_workflow_transition(
        connection,
        scope_id=scope_id,
        route_id=route_id,
        step_index=step_index,
        receipt={
            "skill": stage,
            "upstream_revision": "official-skills:test",
            "content_sha256": "a" * 64,
            "status": "completed",
            "evidence": {
                "verification": "bounded source-level evidence",
                "private_body": "must never enter collaboration payloads",
            },
        },
        current_stage=stage,
    )


def test_workflow_receipt_handoff_is_bounded_durable_and_idempotent() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt_id = _official_workflow_receipt(
        connection,
        scope_id=session.coordination_session_id,
    )

    started = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )

    first = runtime.publish_workflow_receipt_events(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )
    replay = runtime.publish_workflow_receipt_events(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )

    assert first["state"] == "durable"
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["event_ids"] == first["event_ids"]
    assert started["event_type"] == "workflow.stage_started"
    rows = connection.execute(
        "SELECT event_type,event_id,event_json FROM collaboration_events "
        "WHERE event_type LIKE 'workflow.%' ORDER BY sequence"
    ).fetchall()
    assert [row[0] for row in rows] == [
        "workflow.stage_started",
        "workflow.receipt_submitted",
        "workflow.stage_completed",
    ]
    assert rows[1][2]
    assert rows[1][1] == first["event_ids"][0]
    assert rows[2][1] == first["event_ids"][1]
    event_projection = "\n".join(str(row[2]) for row in rows)
    assert "bounded source-level evidence" not in event_projection
    assert "must never enter collaboration payloads" not in event_projection
    assert receipt_id in event_projection
    assert first["receipt_sha256"] in event_projection
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type LIKE 'workflow.%'"
    ).fetchone() == (3,)
    connection.close()


def test_workflow_stage_lifecycle_is_ordered_bounded_and_idempotent() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    started = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )
    replay = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )
    blocked = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="blocked",
        reason_code="skill_execution_failed",
    )
    blocked_replay = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="blocked",
        reason_code="skill_execution_failed",
    )

    assert started["event_type"] == "workflow.stage_started"
    assert started["replayed"] is False
    assert replay["replayed"] is True
    assert blocked["event_type"] == "workflow.stage_blocked"
    assert blocked["causal_parent_event_id"] == started["event_id"]
    assert blocked["replayed"] is False
    assert blocked_replay["replayed"] is True
    rows = connection.execute(
        "SELECT event_type,event_id,event_json,causal_parent_event_id "
        "FROM collaboration_events WHERE event_type LIKE 'workflow.%' ORDER BY sequence"
    ).fetchall()
    assert [row[0] for row in rows] == [
        "workflow.stage_started",
        "workflow.stage_blocked",
    ]
    assert rows[1][3] == rows[0][1]
    projection = "\n".join(str(row[2]) for row in rows)
    assert "skill_execution_failed" in projection
    assert "candidate" in projection
    assert "task_description" not in projection
    connection.close()


def test_workflow_stage_blocked_requires_prior_started_and_fixed_reason() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    with pytest.raises(DurableCollaborationError, match="workflow_stage_started_missing"):
        runtime.publish_workflow_stage_lifecycle_event(
            agent_session_id=session.session_id,
            execution_receipt_id="workflow-receipt:no-start",
            route_id="codebase-design",
            stage="codebase-design",
            step_index=0,
            lifecycle="blocked",
            reason_code="skill_execution_failed",
        )
    with pytest.raises(DurableCollaborationError, match="workflow_stage_block_reason_invalid"):
        runtime.publish_workflow_stage_lifecycle_event(
            agent_session_id=session.session_id,
            execution_receipt_id="workflow-receipt:no-start",
            route_id="codebase-design",
            stage="codebase-design",
            step_index=0,
            lifecycle="blocked",
            reason_code="raw exception text",
        )
    connection.close()


def test_workflow_receipt_handoff_links_to_existing_started_event() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt_id = _official_workflow_receipt(
        connection,
        scope_id=session.coordination_session_id,
    )

    started = runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )
    handoff = runtime.publish_workflow_receipt_events(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )
    rows = connection.execute(
        "SELECT event_type,event_id,causal_parent_event_id FROM collaboration_events "
        "WHERE event_type LIKE 'workflow.%' ORDER BY sequence"
    ).fetchall()
    assert [row[0] for row in rows] == [
        "workflow.stage_started",
        "workflow.receipt_submitted",
        "workflow.stage_completed",
    ]
    assert rows[1][2] == started["event_id"]
    assert rows[2][2] == rows[1][1]
    assert handoff["event_types"] == [
        "workflow.receipt_submitted",
        "workflow.stage_completed",
    ]
    connection.close()


def test_workflow_receipt_handoff_rejects_scope_and_coordinate_drift() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt_id = _official_workflow_receipt(
        connection,
        scope_id="coord:other",
    )

    with pytest.raises(DurableCollaborationError, match="workflow_receipt_scope_mismatch"):
        runtime.publish_workflow_receipt_events(
            agent_session_id=session.session_id,
            execution_receipt_id=receipt_id,
            route_id="codebase-design",
            stage="codebase-design",
            step_index=0,
        )

    bound_receipt_id = _official_workflow_receipt(
        connection,
        scope_id=session.coordination_session_id,
        route_id="bug-onramp",
        stage="diagnosing-bugs",
    )
    with pytest.raises(DurableCollaborationError, match="workflow_receipt_route_mismatch"):
        runtime.publish_workflow_receipt_events(
            agent_session_id=session.session_id,
            execution_receipt_id=bound_receipt_id,
            route_id="codebase-design",
            stage="diagnosing-bugs",
            step_index=0,
        )
    connection.close()


def test_workflow_receipt_handoff_retries_after_transaction_failure() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt_id = _official_workflow_receipt(
        connection,
        scope_id=session.coordination_session_id,
    )

    runtime.publish_workflow_stage_lifecycle_event(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )

    # The canonical receipt is already durable.  A transient failure in the
    # collaboration writer rolls back both event appends while leaving that
    # receipt untouched; the next identical call must repair the handoff.
    failing_writer = FailOnceWriter(connection)
    runtime._transaction_factory = failing_writer.transaction  # noqa: SLF001
    with pytest.raises(sqlite3.OperationalError, match="transient collaboration writer failure"):
        runtime.publish_workflow_receipt_events(
            agent_session_id=session.session_id,
            execution_receipt_id=receipt_id,
            route_id="codebase-design",
            stage="codebase-design",
            step_index=0,
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM official_workflow_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type LIKE 'workflow.%'"
    ).fetchone() == (1,)

    runtime._transaction_factory = ReentrantWriter(connection).transaction  # noqa: SLF001
    repair = runtime.publish_workflow_receipt_events(
        agent_session_id=session.session_id,
        execution_receipt_id=receipt_id,
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )
    assert repair["replayed"] is False
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type LIKE 'workflow.%'"
    ).fetchone() == (3,)
    assert connection.execute(
        "SELECT COUNT(*) FROM official_workflow_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone() == (1,)
    connection.close()


def _session() -> AgentSession:
    return AgentSession(
        session_id="agent-session:builder",
        identity=AgentIdentity("agent:builder", "participant"),
        project=ProjectScope("project:pr5-test"),
        coordination_session_id="coord:pr5",
        state="active",
        started_at="2026-08-11T00:00:00.000000Z",
        last_heartbeat_at="2026-08-11T00:00:00.000000Z",
        expires_at="2026-08-11T02:00:00.000000Z",
    )


def _work() -> tuple[WorkReceipt, WorkLease]:
    project = ProjectScope("project:pr5-test")
    actor = AgentIdentity("agent:builder", "participant")
    receipt = WorkReceipt(
        receipt_id="receipt:pr5-work",
        work_item_id="work:pr5",
        project=project,
        coordination_session_id="coord:pr5",
        assigned_agent=actor,
        objective="Persist the collaboration runtime",
        fencing_generation=1,
        issued_at="2026-08-11T00:00:00.000000Z",
        expires_at="2026-08-11T02:00:00.000000Z",
    )
    item = WorkItem(
        work_item_id="work:pr5",
        project=project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="build",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-v1",
        created_at="2026-08-11T00:00:00.000000Z",
        max_attempts=2,
        coordination_session_id="coord:pr5",
    )
    lease = WorkLease(
        lease_id="lease:pr5",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=actor.agent_id,
        owner_identity=actor,
        fencing_generation=1,
        attempt=1,
        issued_at="2026-08-11T00:00:01.000000Z",
        expires_at="2026-08-11T01:30:00.000000Z",
        result_binding_sha256=receipt.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )
    return receipt, lease


def _peer_work() -> tuple[WorkReceipt, WorkLease]:
    receipt, lease = _work()
    peer_receipt = replace(
        receipt,
        receipt_id="receipt:pr5-work:peer",
        work_item_id="work:pr5:peer",
        objective="Persist the peer collaboration runtime",
    )
    peer_item = replace(
        lease.work_item,
        work_item_id=peer_receipt.work_item_id,
        input_sha256="sha256:" + "3" * 64,
    )
    peer_lease = replace(
        lease,
        lease_id="lease:pr5:peer",
        work_item=peer_item,
        result_binding_sha256=peer_receipt.content_sha256,
        idempotency_key_sha256="sha256:" + "4" * 64,
    )
    return peer_receipt, peer_lease


def _formal_closure(
    runtime: DurableCollaborationRuntime,
    *,
    work_item_id: str = "work:pr5",
    agent_session_id: str = "agent-session:builder",
    outcome: str = "completed",
    summary: str = "Formal closure persisted",
) -> dict[str, object]:
    return runtime.record_step_closure_result(
        work_item_id=work_item_id,
        outcome=outcome,
        summary=summary,
        artifact_refs=("artifact:pr5",),
        evidence_refs=("evidence:pr5",),
        result={"verification": "focused durable runtime tests"},
        agent_session_id=agent_session_id,
    )


def _step_closure_receipt_id(
    *,
    work_item_id: str,
    agent_session_id: str,
    outcome: str,
    summary: str,
) -> str:
    request = {
        "work_item_id": work_item_id,
        "agent_session_id": agent_session_id,
        "outcome": outcome.strip().casefold(),
        "summary": summary.strip(),
        "artifact_refs": ["artifact:pr5"],
        "evidence_refs": ["evidence:pr5"],
        "result": {"verification": "focused durable runtime tests"},
    }
    encoded = json.dumps(
        request,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "result:step-closure:" + hashlib.sha256(encoded).hexdigest()[:40]


def _role_intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
    use: str,
    role: str,
    stage: str,
    created_at: str,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=f"event:intent:{session.session_id}:{work.work_item_id}:{use}",
        project=work.project,
        coordination_session_id=work.coordination_session_id,
        actor=session.identity,
        event_type="agent.intent_declared",
        summary=f"Intent for {use}",
        created_at=created_at,
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _issue_role_assignment(
    *,
    repository: InMemoryRoleAssignmentRepository,
    authority: RoleAssignmentAuthority,
    use: str,
    basis: RoleAssignmentBasis,
) -> str:
    repository.register_basis(use=use, basis=basis)
    return authority.issue(
        use=use,
        agent_session_id=basis.session.session_id,
        work_item_id=basis.work.work_item_id,
        lease_id=basis.lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
        ttl_seconds=1800,
    ).assignment_sha256


def test_runtime_fails_closed_without_migration_schema() -> None:
    connection = sqlite3.connect(":memory:")

    @contextmanager
    def no_tx():
        yield

    with pytest.raises(DurableCollaborationError, match="durable_collaboration_schema_missing"):
        DurableCollaborationRuntime(
            connection,
            transaction_factory=no_tx,
        )
    connection.close()


def test_session_registration_requires_server_policy_authority_and_revision() -> None:
    connection, runtime, clock = _runtime()
    writer = ReentrantWriter(connection)
    unbound = DurableCollaborationRuntime(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    with pytest.raises(DurableCollaborationError, match="durable_policy_authority_required"):
        unbound.register_session(_session())
    with pytest.raises(DurableCollaborationError, match="durable_policy_caller_policy_forbidden"):
        runtime.register_session(_session(), policy={"revision": "caller-forged"})
    with pytest.raises(
        DurableCollaborationError,
        match="durable_policy_revision_not_server_current",
    ):
        DurableCollaborationRuntime(
            connection,
            transaction_factory=writer.transaction,
            clock=clock,
            policy_revision="caller-forged",
        )
    connection.close()


def test_register_session_appends_one_bounded_agent_joined_event() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    expected_event_id = (
        "event:agent-joined:"
        + hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
    )

    runtime.register_session(session)
    runtime.register_session(session)

    rows = connection.execute(
        "SELECT event_id,event_json FROM collaboration_events WHERE event_type='agent.joined'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == expected_event_id
    projection = json.loads(rows[0][1])
    assert projection["subject_refs"] == [session.session_id]
    assert set(projection["payload"]) == {"_server_time_diagnostics"}
    assert "policy" not in projection["payload"]
    assert "capabilities" not in projection["payload"]
    connection.close()


def test_register_session_event_failure_rolls_back_agent_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, runtime, _ = _runtime()
    from plastic_promise.collaboration import durable_runtime as module

    def fail_append(self, event, **kwargs):
        raise RuntimeError("event_writer_unavailable")

    monkeypatch.setattr(module.CollaborationEventLog, "append", fail_append)
    with pytest.raises(RuntimeError, match="event_writer_unavailable"):
        runtime.register_session(_session())

    assert connection.execute("SELECT COUNT(*) FROM collaboration_agents").fetchone() == (0,)
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_agent_sessions"
    ).fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_events").fetchone() == (0,)
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_event_retention"
    ).fetchone() == (0,)
    connection.close()


def test_collaboration_state_recovers_after_sqlite_reopen(tmp_path) -> None:
    database_path = tmp_path / "pr5-collaboration-restart.sqlite3"
    connection = sqlite3.connect(database_path)
    writer = ReentrantWriter(connection)
    connection.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, project_id TEXT NOT NULL)"
    )
    install_pr5_collaboration_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=lambda: SERVER_NOW,
        suffix="pr5-runtime-restart",
    )
    clock = MutableClock()
    role_repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    runtime = DurableCollaborationRuntime(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        policy_authority=open_server_agent_policy_binding_authority(clock=clock),
        role_assignment_repository=role_repository,
        role_assignment_authority=open_server_role_assignment_authority(
            repository=role_repository,
            clock=clock,
        ),
        presence_timeout_seconds=7200,
        event_retention_seconds=60,
    )
    session = _session()
    initialized = runtime.register_session(session)
    assert initialized.policy["policy_revision"] == AGENT_POLICY_REVISION
    stored_policy_revision = connection.execute(
        "SELECT policy_revision FROM collaboration_agents WHERE project_id=? AND agent_id=?",
        (session.project.project_id, session.identity.agent_id),
    ).fetchone()[0]
    assert stored_policy_revision == AGENT_POLICY_REVISION
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    runtime.claim_work(lease, agent_session_id=session.session_id)
    cursor = initialized.next_cursor
    for index in range(4):
        cursor = runtime.append_event(
            CollaborationEvent(
                event_id=f"event:cursor:{index}",
                project=session.project,
                coordination_session_id=session.coordination_session_id,
                actor=session.identity,
                event_type="work.progressed",
                summary=f"cursor event {index}",
                created_at="2026-08-11T00:00:01.000000Z",
            ),
            actor_session_id=session.session_id,
        )
    runtime.record_cursor(
        cursor,
        consumer_id=session.session_id,
        source_head_sequence=cursor.sequence,
    )
    expected_event_count = connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE project_id=? "
        "AND coordination_session_id=?",
        (session.project.project_id, session.coordination_session_id),
    ).fetchone()[0]
    connection.close()

    connection = sqlite3.connect(database_path)
    writer = ReentrantWriter(connection)
    restarted_clock = MutableClock()
    restarted_role_repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=restarted_clock,
    )
    restarted = DurableCollaborationRuntime(
        connection,
        transaction_factory=writer.transaction,
        clock=restarted_clock,
        policy_authority=open_server_agent_policy_binding_authority(clock=restarted_clock),
        role_assignment_repository=restarted_role_repository,
        role_assignment_authority=open_server_role_assignment_authority(
            repository=restarted_role_repository,
            clock=restarted_clock,
        ),
        presence_timeout_seconds=7200,
        event_retention_seconds=60,
    )
    recovered = restarted.register_session(session)
    assert recovered.cursor.sequence == cursor.sequence
    assert recovered.session.identity == session.identity
    assert recovered.assigned_work[0]["work_item_id"] == receipt.work_item_id
    assert connection.execute(
        "SELECT policy_revision,state FROM collaboration_agents WHERE project_id=? AND agent_id=?",
        (session.project.project_id, session.identity.agent_id),
    ).fetchone() == (AGENT_POLICY_REVISION, "active")
    assert connection.execute(
        "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
        (session.session_id,),
    ).fetchone() == ("active",)
    assert restarted.get_work(receipt.work_item_id)["state"] == "leased"
    assert connection.execute(
        "SELECT owner_session_id,state FROM collaboration_work_leases WHERE lease_id=?",
        (lease.lease_id,),
    ).fetchone() == (session.session_id, "active")
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE project_id=? "
        "AND coordination_session_id=?",
        (session.project.project_id, session.coordination_session_id),
    ).fetchone()[0] == expected_event_count
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.joined'"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT event_type FROM collaboration_events WHERE event_id='event:cursor:3'"
    ).fetchone() == ("work.progressed",)
    assert connection.execute(
        "SELECT sequence FROM collaboration_cursors WHERE consumer_id=?",
        (session.session_id,),
    ).fetchone() == (cursor.sequence,)
    connection.close()


def test_register_session_rechecks_identity_after_the_writer_transaction_opens() -> None:
    """A competing durable session cannot be mistaken for an idempotent replay."""

    connection, runtime, _ = _runtime()
    rival = replace(
        _session(),
        identity=AgentIdentity("agent:rival", "participant"),
    )
    runtime.register_session(rival)
    agent_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(collaboration_agents)")
    )
    agent_values = tuple(
        connection.execute(
            "SELECT * FROM collaboration_agents WHERE project_id=? AND agent_id=?",
            (rival.project.project_id, rival.identity.agent_id),
        ).fetchone()
    )
    session_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(collaboration_agent_sessions)")
    )
    session_values = tuple(
        connection.execute(
            "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
            (rival.session_id,),
        ).fetchone()
    )
    connection.execute(
        "DELETE FROM collaboration_agent_sessions WHERE session_id=?",
        (rival.session_id,),
    )
    connection.execute(
        "DELETE FROM collaboration_agents WHERE project_id=? AND agent_id=?",
        (rival.project.project_id, rival.identity.agent_id),
    )
    connection.commit()

    def inject_rival_session() -> None:
        agent_marks = ", ".join("?" for _ in agent_columns)
        session_marks = ", ".join("?" for _ in session_columns)
        connection.execute(
            f"INSERT INTO collaboration_agents ({', '.join(agent_columns)}) VALUES ({agent_marks})",
            agent_values,
        )
        connection.execute(
            "INSERT INTO collaboration_agent_sessions "
            f"({', '.join(session_columns)}) VALUES ({session_marks})",
            session_values,
        )

    writer = InjectingWriter(connection, inject_rival_session)
    runtime._transaction_factory = writer.transaction

    with pytest.raises(DurableCollaborationError, match="agent_session_identity_conflict"):
        runtime.register_session(_session())
    connection.close()


def test_work_admission_rechecks_session_state_inside_writer_transaction() -> None:
    """A session made stale after preflight cannot create or claim Agent work."""

    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt, lease = _work()

    def mark_session_stale() -> None:
        connection.execute(
            "UPDATE collaboration_agent_sessions SET state='stale' WHERE session_id=?",
            (session.session_id,),
        )

    runtime._transaction_factory = InjectingWriter(connection, mark_session_stale).transaction
    with pytest.raises(DurableCollaborationError, match="agent_session_stale"):
        runtime.register_work(receipt, max_attempts=2)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_work_items").fetchone()[0] == 0

    connection.execute(
        "UPDATE collaboration_agent_sessions SET state='active' WHERE session_id=?",
        (session.session_id,),
    )
    connection.commit()
    runtime._transaction_factory = ReentrantWriter(connection).transaction
    runtime.register_work(receipt, max_attempts=2)

    runtime._transaction_factory = InjectingWriter(connection, mark_session_stale).transaction
    with pytest.raises(DurableCollaborationError, match="agent_session_stale"):
        runtime.claim_work(lease)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_work_leases").fetchone()[0] == 0
    connection.close()


def test_heartbeat_cannot_revive_a_session_marked_stale_after_preflight() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)

    def mark_session_stale() -> None:
        connection.execute(
            "UPDATE collaboration_agent_sessions SET state='stale' WHERE session_id=?",
            (session.session_id,),
        )

    runtime._transaction_factory = InjectingWriter(connection, mark_session_stale).transaction
    with pytest.raises(DurableCollaborationError, match="agent_session_stale"):
        runtime.heartbeat(session.session_id)
    connection.close()


def test_idle_session_can_resume_but_stale_session_cannot_resume() -> None:
    connection, runtime, clock = _runtime()
    session = _session()
    runtime.register_session(session)

    idle = runtime.mark_idle(session.session_id)
    assert idle["state"] == "idle"
    assert runtime.heartbeat(session.session_id)["state"] == "active"

    runtime.mark_idle(session.session_id)
    clock.value = SERVER_NOW + timedelta(hours=3)
    assert runtime.reconcile().stale_session_ids == (session.session_id,)
    with pytest.raises(DurableCollaborationError, match="agent_session_stale"):
        runtime.heartbeat(session.session_id)
    connection.close()


def test_work_admission_requires_an_exact_session_when_identity_is_ambiguous() -> None:
    connection, runtime, _ = _runtime()
    first = _session()
    second = replace(first, session_id="agent-session:builder:second")
    runtime.register_session(first)
    runtime.register_session(second)
    receipt, lease = _work()

    with pytest.raises(DurableCollaborationError, match="agent_session_ambiguous"):
        runtime.register_work(receipt, max_attempts=2)
    runtime.register_work(
        receipt,
        max_attempts=2,
        agent_session_id=first.session_id,
    )
    with pytest.raises(DurableCollaborationError, match="agent_session_ambiguous"):
        runtime.claim_work(lease)
    runtime.claim_work(lease, agent_session_id=first.session_id)
    connection.close()


def test_lease_heartbeat_and_result_require_the_exact_owner_session() -> None:
    connection, runtime, _ = _runtime()
    first = _session()
    second = replace(first, session_id="agent-session:builder:second")
    runtime.register_session(first)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=first.session_id)
    runtime.claim_work(lease, agent_session_id=first.session_id)
    runtime.register_session(second)

    heartbeat = LeaseHeartbeat.for_lease(
        lease,
        heartbeat_id="heartbeat:pr5:ambiguous",
        sequence=1,
        sent_at="2026-08-11T01:00:10.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="agent_session_ambiguous"):
        runtime.heartbeat_lease(heartbeat)
    with pytest.raises(DurableCollaborationError, match="lease_heartbeat_binding_mismatch"):
        runtime.heartbeat_lease(heartbeat, agent_session_id=second.session_id)
    heartbeat_receipt = runtime.heartbeat_lease(
        heartbeat,
        agent_session_id=first.session_id,
    )
    assert heartbeat_receipt["heartbeat_sequence"] == 1

    result = ResultReceipt.for_work(
        receipt,
        receipt_id="result:pr5:ambiguous-session",
        submitted_by=receipt.assigned_agent,
        outcome="completed",
        summary="must not cross a peer transport",
        submitted_at="2026-08-11T00:59:20.000000Z",
    )
    binding = {
        "lease_id": lease.lease_id,
        "fencing_generation": lease.fencing_generation,
        "lease_sha256": lease.content_sha256,
    }
    with pytest.raises(DurableCollaborationError, match="agent_session_ambiguous"):
        runtime.record_result(result, **binding)
    with pytest.raises(DurableCollaborationError, match="result_lease_binding_mismatch"):
        runtime.record_result(result, agent_session_id=second.session_id, **binding)
    recorded = runtime.record_result(
        result,
        agent_session_id=first.session_id,
        **binding,
    )

    assert connection.execute(
        "SELECT state,heartbeat_sequence FROM collaboration_work_leases WHERE lease_id=?",
        (lease.lease_id,),
    ).fetchone() == ("completed", 1)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (1,)
    assert recorded["state"] == "submitted"
    connection.close()


def test_reconcile_reclaims_only_the_stale_owner_session_lease() -> None:
    connection, runtime, clock = _runtime()
    first = _session()
    second = replace(first, session_id="agent-session:builder:second")
    runtime.register_session(first)
    receipt, lease = _work()
    long_lease = replace(lease, expires_at="2026-08-11T06:00:00.000000Z")
    runtime.register_work(receipt, max_attempts=2, agent_session_id=first.session_id)
    runtime.claim_work(long_lease, agent_session_id=first.session_id)
    runtime.register_session(second)
    connection.execute(
        "UPDATE collaboration_agent_sessions SET last_heartbeat_at=? WHERE session_id=?",
        ("2026-08-10T22:00:00.000000Z", first.session_id),
    )
    connection.commit()
    clock.value = SERVER_NOW + timedelta(minutes=1)

    report = runtime.reconcile()

    assert report.stale_session_ids == (first.session_id,)
    assert report.abandoned_lease_ids == (long_lease.lease_id,)
    assert connection.execute(
        "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
        (first.session_id,),
    ).fetchone() == ("stale",)
    assert connection.execute(
        "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
        (second.session_id,),
    ).fetchone() == ("active",)
    assert connection.execute(
        "SELECT owner_session_id,state,release_reason "
        "FROM collaboration_work_leases WHERE lease_id=?",
        (long_lease.lease_id,),
    ).fetchone() == (first.session_id, "abandoned", "session_stale")
    assert connection.execute(
        "SELECT state FROM collaboration_work_items WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == ("rework",)
    heartbeat = LeaseHeartbeat.for_lease(
        long_lease,
        heartbeat_id="heartbeat:pr5:stale-owner-peer",
        sequence=1,
        sent_at="2026-08-11T01:01:10.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="lease_heartbeat_binding_mismatch"):
        runtime.heartbeat_lease(heartbeat, agent_session_id=second.session_id)
    connection.close()


def test_session_end_closes_the_exact_session_and_releases_only_its_lease() -> None:
    connection, runtime, _ = _runtime()
    first = _session()
    second = replace(first, session_id="agent-session:builder:second")
    runtime.register_session(first)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=first.session_id)
    runtime.claim_work(lease, agent_session_id=first.session_id)
    runtime.register_session(second)
    peer_receipt, peer_lease = _peer_work()
    runtime.register_work(
        peer_receipt,
        max_attempts=2,
        agent_session_id=second.session_id,
    )
    runtime.claim_work(peer_lease, agent_session_id=second.session_id)

    closed = runtime.end_session(first.session_id)

    assert closed["released_lease_ids"] == [lease.lease_id]
    assert dict(
        connection.execute(
            "SELECT session_id,state FROM collaboration_agent_sessions WHERE session_id IN (?,?)",
            (first.session_id, second.session_id),
        ).fetchall()
    ) == {first.session_id: "closed", second.session_id: "active"}
    assert dict(
        connection.execute(
            "SELECT lease_id,state FROM collaboration_work_leases WHERE lease_id IN (?,?)",
            (lease.lease_id, peer_lease.lease_id),
        ).fetchall()
    ) == {lease.lease_id: "released", peer_lease.lease_id: "active"}
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.closed'"
    ).fetchone() == (1,)
    connection.close()


def test_formal_step_closure_records_once_and_replays_the_same_receipt() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    runtime.claim_work(lease, agent_session_id=session.session_id)

    first = _formal_closure(runtime)
    replay = _formal_closure(runtime)

    assert first["schema_version"] == "step-closure-result-receipt/v1"
    assert first.get("replayed") is None
    assert replay["replayed"] is True
    assert replay["result_receipt_sha256"] == first["result_receipt_sha256"]
    assert replay["result_receipt"]["receipt_id"] == first["result_receipt"]["receipt_id"]
    assert first["memory_proposal"] is None
    assert first["canonical_memory_effect"] == "none"
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (1,)
    assert connection.execute(
        "SELECT state,release_reason FROM collaboration_work_leases WHERE lease_id=?",
        (lease.lease_id,),
    ).fetchone() == ("completed", "result_recorded")
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.submitted'"
    ).fetchone() == (1,)
    connection.close()


def test_formal_step_closure_rejects_conflicting_existing_receipt() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    runtime.claim_work(lease, agent_session_id=session.session_id)
    receipt_id = _step_closure_receipt_id(
        work_item_id=receipt.work_item_id,
        agent_session_id=session.session_id,
        outcome="completed",
        summary="Formal closure persisted",
    )
    conflicting = ResultReceipt.for_work(
        receipt,
        receipt_id=receipt_id,
        submitted_by=receipt.assigned_agent,
        outcome="failed",
        summary="conflicting preexisting result",
        submitted_at="2026-08-11T00:59:00.000000Z",
    )
    runtime.record_result(
        conflicting,
        lease_id=lease.lease_id,
        fencing_generation=lease.fencing_generation,
        lease_sha256=lease.content_sha256,
        agent_session_id=session.session_id,
    )

    with pytest.raises(DurableCollaborationError, match="result_receipt_conflict"):
        _formal_closure(runtime)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (1,)
    connection.close()


def test_formal_step_closure_rejects_missing_work_or_lease() -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    with pytest.raises(DurableCollaborationError, match="work_not_registered"):
        _formal_closure(runtime, work_item_id="work:pr5:missing")

    receipt, _ = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    with pytest.raises(DurableCollaborationError, match="work_active_lease_required"):
        _formal_closure(runtime)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (0,)
    connection.close()


def test_formal_step_closure_rejects_wrong_or_ambiguous_owner_session() -> None:
    connection, runtime, _ = _runtime()
    owner = _session()
    peer = replace(owner, session_id="agent-session:builder:second")
    runtime.register_session(owner)
    runtime.register_session(peer)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=owner.session_id)
    runtime.claim_work(lease, agent_session_id=owner.session_id)

    with pytest.raises(DurableCollaborationError, match="work_active_lease_required"):
        _formal_closure(runtime, agent_session_id=peer.session_id)

    duplicate_lease = replace(
        lease,
        lease_id="lease:pr5:duplicate-owner-session",
        fencing_generation=2,
        attempt=2,
        idempotency_key_sha256="sha256:" + "5" * 64,
    )
    connection.execute(
        """
        INSERT INTO collaboration_work_leases (
            lease_id, work_item_id, project_id, coordination_session_id,
            owner_kind, policy_kind, owner_id, owner_session_id, lease_json, lease_sha256,
            fencing_generation, attempt, issued_at, expires_at,
            heartbeat_sequence, last_heartbeat_at, state, released_at, release_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', '', '')
        """,
        (
            duplicate_lease.lease_id,
            receipt.work_item_id,
            receipt.project.project_id,
            receipt.coordination_session_id,
            duplicate_lease.owner_kind,
            duplicate_lease.policy_kind,
            duplicate_lease.owner_id,
            owner.session_id,
            json.dumps(duplicate_lease.to_dict(), sort_keys=True, separators=(",", ":")),
            duplicate_lease.content_sha256,
            duplicate_lease.fencing_generation,
            duplicate_lease.attempt,
            duplicate_lease.issued_at,
            duplicate_lease.expires_at,
            SERVER_NOW.isoformat().replace("+00:00", "Z"),
        ),
    )
    connection.commit()

    with pytest.raises(DurableCollaborationError, match="work_active_lease_ambiguous"):
        _formal_closure(runtime)
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (0,)
    connection.close()


def test_formal_step_closure_event_append_failure_rolls_back_result_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    runtime.claim_work(lease, agent_session_id=session.session_id)
    from plastic_promise.collaboration import durable_runtime as module

    def fail_append(self, event, **kwargs):
        raise RuntimeError("event_writer_unavailable")

    monkeypatch.setattr(module.CollaborationEventLog, "append", fail_append)
    with pytest.raises(RuntimeError, match="event_writer_unavailable"):
        _formal_closure(runtime)

    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (0,)
    assert connection.execute(
        "SELECT state,release_reason FROM collaboration_work_leases WHERE lease_id=?",
        (lease.lease_id,),
    ).fetchone() == ("active", "")
    assert connection.execute(
        "SELECT state FROM collaboration_work_items WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == ("leased",)
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.submitted'"
    ).fetchone() == (0,)
    connection.close()


def test_ordinary_step_closure_does_not_create_a_collaboration_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, runtime, _ = _runtime()
    session = _session()
    runtime.register_session(session)
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2, agent_session_id=session.session_id)
    runtime.claim_work(lease, agent_session_id=session.session_id)
    from plastic_promise.loop import soul_loop
    from plastic_promise.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(runtime=runtime, session=session),
    )
    monkeypatch.setattr(mcp_server, "_closure_history", [])
    monkeypatch.setattr(
        soul_loop,
        "post_task",
        lambda *_a, **_k: {
            "scarf": {"summary": {"overall_score": 0.8}},
            "trust": {"score": 0.8},
            "cei": {"score": 0.8, "tier": "stable"},
            "hormone": {"trust_delta": 0.0},
            "reflection": {"step_id": "step:ordinary"},
        },
    )

    response = asyncio.run(
        mcp_server.call_tool(
            "step-closure",
            {"task_description": "ordinary closure", "mode": "light"},
        )
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is True
    assert payload["collaboration_result"] is None
    assert connection.execute("SELECT COUNT(*) FROM collaboration_results").fetchone() == (0,)
    assert connection.execute(
        "SELECT state FROM collaboration_work_leases WHERE lease_id=?",
        (lease.lease_id,),
    ).fetchone() == ("active",)
    connection.close()


def _patch_step_closure_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from plastic_promise.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_server, "_closure_history", [])


def test_mcp_formal_step_closure_rejects_partial_fields_without_work_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plastic_promise.mcp import server as mcp_server

    _patch_step_closure_server(monkeypatch)
    response = asyncio.run(
        mcp_server.call_tool(
            "step-closure",
            {
                "task_description": "partial formal closure",
                "outcome": "completed",
            },
        )
    )
    payload = json.loads(response[0].text)

    assert payload == {
        "success": False,
        "error": "formal_work_item_required",
        "closure": None,
        "collaboration_result": None,
        "memory_proposal": None,
    }


def test_mcp_formal_step_closure_rejects_non_object_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plastic_promise.mcp import server as mcp_server

    _patch_step_closure_server(monkeypatch)
    session = SimpleNamespace(session_id="agent-session:handler")
    runtime = SimpleNamespace()
    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(runtime=runtime, session=session),
    )
    response = asyncio.run(
        mcp_server.call_tool(
            "step-closure",
            {
                "task_description": "invalid formal result",
                "work_item_id": "work:handler",
                "outcome": "completed",
                "summary": "The result must be an object",
                "result": ["not", "an", "object"],
            },
        )
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is False
    assert payload["error"] == "result_projection_invalid"


def test_mcp_formal_replay_does_not_repeat_reflection_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plastic_promise.loop import soul_loop
    from plastic_promise.mcp import server as mcp_server

    _patch_step_closure_server(monkeypatch)
    session = SimpleNamespace(session_id="agent-session:handler")
    calls: list[tuple[object, ...]] = []

    class ReplayRuntime:
        def record_step_closure_result(self, **kwargs):
            calls.append(tuple(kwargs.values()))
            return {
                "schema_version": "step-closure-result-receipt/v1",
                "result_receipt_sha256": "sha256:" + "a" * 64,
                "result_receipt": {"receipt_id": "result:handler"},
                "work": {"state": "submitted"},
                "memory_proposal": None,
                "canonical_memory_effect": "none",
                "replayed": True,
            }

    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(runtime=ReplayRuntime(), session=session),
    )
    monkeypatch.setattr(
        soul_loop,
        "post_task",
        lambda *_a, **_k: pytest.fail("replayed formal closure must not reflect again"),
    )
    response = asyncio.run(
        mcp_server.call_tool(
            "step-closure",
            {
                "task_description": "replay formal closure",
                "work_item_id": "work:handler",
                "outcome": "completed",
                "summary": "Already persisted",
                "result": {"verified": True},
            },
        )
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is True
    assert payload["closure"]["state"] == "replayed"
    assert payload["collaboration_result"]["persistent"] is True
    assert len(calls) == 1


def test_mcp_formal_result_survives_reflection_failure_as_degraded_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plastic_promise.loop import soul_loop
    from plastic_promise.mcp import server as mcp_server

    _patch_step_closure_server(monkeypatch)
    session = SimpleNamespace(session_id="agent-session:handler")

    class DurableRuntime:
        def record_step_closure_result(self, **_kwargs):
            return {
                "schema_version": "step-closure-result-receipt/v1",
                "result_receipt_sha256": "sha256:" + "b" * 64,
                "result_receipt": {"receipt_id": "result:handler"},
                "work": {"state": "submitted"},
                "memory_proposal": None,
                "canonical_memory_effect": "none",
            }

    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(runtime=DurableRuntime(), session=session),
    )
    monkeypatch.setattr(
        soul_loop,
        "post_task",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reflection backend unavailable")),
    )
    response = asyncio.run(
        mcp_server.call_tool(
            "step-closure",
            {
                "task_description": "durable result before reflection",
                "work_item_id": "work:handler",
                "outcome": "completed",
                "summary": "Durable result was committed",
                "result": {"verified": True},
            },
        )
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is True
    assert payload["closure"]["state"] == "degraded"
    assert payload["closure"]["reason"] == "reflection_unavailable"
    assert payload["collaboration_result"]["state"] == "submitted"
    assert payload["collaboration_result"]["persistent"] is True
    assert payload["memory_proposal"] is None
    assert payload["canonical_memory_effect"] == "none"


def test_lease_and_result_replays_are_idempotent_but_digest_conflicts_fail() -> None:
    connection, runtime, _ = _runtime()
    runtime.register_session(_session())
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2)
    runtime.claim_work(lease)
    runtime.claim_work(lease)
    conflicting_lease = WorkLease(
        lease_id=lease.lease_id,
        work_item=lease.work_item,
        owner_kind=lease.owner_kind,
        policy_kind=lease.policy_kind,
        owner_id=lease.owner_id,
        owner_identity=lease.owner_identity,
        fencing_generation=lease.fencing_generation,
        attempt=lease.attempt,
        issued_at=lease.issued_at,
        expires_at="2026-08-11T01:30:01.000000Z",
        result_binding_sha256=lease.result_binding_sha256,
        idempotency_key_sha256=lease.idempotency_key_sha256,
    )
    with pytest.raises(DurableCollaborationError, match="work_lease_conflict"):
        runtime.claim_work(conflicting_lease)

    result = ResultReceipt.for_work(
        receipt,
        receipt_id="result:pr5",
        submitted_by=receipt.assigned_agent,
        outcome="completed",
        summary="completed",
        submitted_at="2026-08-11T00:59:10.000000Z",
    )
    binding = {
        "lease_id": lease.lease_id,
        "fencing_generation": lease.fencing_generation,
        "lease_sha256": lease.content_sha256,
    }
    runtime.record_result(result, **binding)
    runtime.record_result(result, **binding)
    conflicting_result = ResultReceipt.for_work(
        receipt,
        receipt_id=result.receipt_id,
        submitted_by=receipt.assigned_agent,
        outcome="failed",
        summary="different result",
        submitted_at="2026-08-11T00:59:10.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="result_receipt_conflict"):
        runtime.record_result(conflicting_result, **binding)
    connection.close()


def test_lease_fence_and_result_state_transitions_are_monotonic() -> None:
    connection, runtime, clock = _runtime()
    runtime.register_session(_session())
    receipt, lease = _work()
    runtime.register_work(receipt, max_attempts=2)
    runtime.claim_work(lease)
    with pytest.raises(DurableCollaborationError, match="work_claim_state_invalid"):
        runtime.claim_work(lease, state="accepted")

    clock.value = SERVER_NOW + timedelta(minutes=31)
    runtime.reconcile()
    stale = replace(
        lease,
        lease_id="lease:pr5:stale-generation",
        attempt=2,
        issued_at="2026-08-11T01:31:00.000000Z",
        expires_at="2026-08-11T01:40:00.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="work_fencing_stale"):
        runtime.claim_work(stale)

    current = replace(stale, lease_id="lease:pr5:current-generation", fencing_generation=2)
    runtime.claim_work(current)
    forged_result = ResultReceipt(
        receipt_id="result:pr5:forged-owner",
        work_item_id=receipt.work_item_id,
        work_receipt_sha256=receipt.content_sha256,
        project=receipt.project,
        coordination_session_id=receipt.coordination_session_id,
        submitted_by=AgentIdentity("agent:other", "implementer"),
        outcome="completed",
        summary="forged owner",
        submitted_at="2026-08-11T01:31:10.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="result_submitter_not_assignee"):
        runtime.record_result(
            forged_result,
            lease_id=current.lease_id,
            fencing_generation=current.fencing_generation,
            lease_sha256=current.content_sha256,
        )
    valid_result = ResultReceipt.for_work(
        receipt,
        receipt_id="result:pr5:state-jump",
        submitted_by=receipt.assigned_agent,
        outcome="completed",
        summary="state jump",
        submitted_at="2026-08-11T01:31:10.000000Z",
    )
    with pytest.raises(DurableCollaborationError, match="work_result_state_invalid"):
        runtime.record_result(
            valid_result,
            state="accepted",
            lease_id=current.lease_id,
            fencing_generation=current.fencing_generation,
            lease_sha256=current.content_sha256,
        )
    connection.close()


def test_session_end_retries_event_append_without_losing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, runtime, _ = _runtime()
    runtime.register_session(_session())
    from plastic_promise.collaboration import durable_runtime as module

    original = module.CollaborationEventLog.append
    attempts = {"count": 0}

    def fail_once(self, event):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("event_writer_unavailable")
        return original(self, event)

    monkeypatch.setattr(module.CollaborationEventLog, "append", fail_once)
    with pytest.raises(RuntimeError, match="event_writer_unavailable"):
        runtime.end_session(_session().session_id)
    row = connection.execute(
        "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
        (_session().session_id,),
    ).fetchone()
    assert row[0] == "active"
    closed = runtime.end_session(_session().session_id)
    assert closed["state"] == "closed"
    assert attempts["count"] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.closed'"
    ).fetchone() == (1,)
    event_sequence = connection.execute(
        "SELECT sequence FROM collaboration_events WHERE event_id=?",
        (closed["event_id"],),
    ).fetchone()[0]
    assert closed["cursor"]["sequence"] == event_sequence
    assert connection.execute(
        "SELECT sequence FROM collaboration_cursors WHERE consumer_id=?",
        (_session().session_id,),
    ).fetchone() == (event_sequence,)
    assert connection.execute(
        "SELECT state,cursor_sequence FROM collaboration_agent_sessions WHERE session_id=?",
        (_session().session_id,),
    ).fetchone() == ("closed", event_sequence)
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_event_retention WHERE event_id=?",
        (closed["event_id"],),
    ).fetchone() == (1,)
    connection.close()


def test_maintenance_releases_retained_events_without_deleting_audit_source() -> None:
    connection, runtime, clock = _runtime()
    runtime.register_session(_session())
    event = CollaborationEvent(
        event_id="event:expired",
        project=ProjectScope("project:pr5-test"),
        coordination_session_id="coord:pr5",
        actor=AgentIdentity("agent:builder", "participant"),
        event_type="work.progressed",
        summary="old progress",
        created_at="2026-08-11T00:00:00.000000Z",
        expires_at="2026-08-11T00:00:10.000000Z",
    )
    runtime.append_event(event)
    clock.value = datetime(2026, 8, 11, 1, 2, tzinfo=timezone.utc)
    report = runtime.reconcile()
    assert event.event_id in report.retained_event_ids
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_id=?",
        (event.event_id,),
    ).fetchone() == (1,)
    row = connection.execute(
        "SELECT retention_state,cleaned_at FROM collaboration_event_retention WHERE event_id=?",
        (event.event_id,),
    ).fetchone()
    assert row[0] == "released"
    assert row[1]
    clock.value = datetime(2026, 8, 11, 1, 2, tzinfo=timezone.utc)
    assert runtime.reconcile().retained_event_ids == ()
    connection.close()


def test_accepted_candidate_requires_server_acceptance_receipt() -> None:
    connection, runtime, _ = _runtime()
    runtime.register_session(_session())
    reviewer = replace(
        _session(),
        session_id="agent-session:reviewer",
        identity=AgentIdentity("agent:reviewer", "deepsec_reviewer"),
    )
    runtime.register_session(reviewer)
    event = CollaborationEvent(
        event_id="event:accepted:without-authority",
        project=ProjectScope("project:pr5-test"),
        coordination_session_id="coord:pr5",
        actor=reviewer.identity,
        event_type="work.accepted",
        summary="accepted work",
        created_at="2026-08-11T00:00:10.000000Z",
        payload={
            "bridge_kind": "accepted",
            "result_receipt_sha256": "sha256:" + "2" * 64,
            "acceptance_receipt_sha256": "sha256:" + "3" * 64,
        },
    )
    runtime.append_event(event)
    source_event_sha256 = connection.execute(
        "SELECT event_sha256 FROM collaboration_events WHERE event_id=?",
        (event.event_id,),
    ).fetchone()[0]
    candidate = PromotionCandidate(
        candidate_id="promotion-candidate:without-authority",
        project=event.project,
        coordination_session_id=event.coordination_session_id,
        work_item_id="work:pr5",
        source_event_id=event.event_id,
        source_event_sha256=source_event_sha256,
        work_receipt_sha256="sha256:" + "1" * 64,
        result_receipt_sha256="sha256:" + "2" * 64,
        acceptance_receipt_sha256="sha256:" + "3" * 64,
        summary="accepted work without server receipt",
        evidence_refs=("evidence:review",),
        idempotency_sha256="sha256:" + "5" * 64,
    )
    assert runtime.enqueue_promotion(candidate, conflict_checked=True).reason == (
        "promotion_acceptance_authority_required"
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM collaboration_promotion_outbox").fetchone()[0] == 0
    )
    connection.close()


def _runtime_with_acceptance() -> tuple[
    sqlite3.Connection,
    DurableCollaborationRuntime,
    MutableClock,
    AgentPolicyBindingAuthority,
    AcceptanceReceiptAuthority,
    InMemoryRoleAssignmentRepository,
    RoleAssignmentAuthority,
]:
    connection, _, clock = _runtime()
    writer = ReentrantWriter(connection)
    policy_authority = open_server_agent_policy_binding_authority(clock=clock)
    source_registry = open_server_acceptance_source_registry()
    role_repository = InMemoryRoleAssignmentRepository()
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=clock,
    )
    acceptance_authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        source_registry=source_registry,
        current_review_policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
        current_source_revision="a" * 40,
        clock=clock,
    )
    runtime = DurableCollaborationRuntime(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        policy_authority=policy_authority,
        role_assignment_repository=role_repository,
        role_assignment_authority=role_authority,
        acceptance_authority=acceptance_authority,
        acceptance_source_registry=source_registry,
        presence_timeout_seconds=7200,
        event_retention_seconds=60,
    )
    assert runtime.role_assignment_authority is role_authority
    return (
        connection,
        runtime,
        clock,
        policy_authority,
        acceptance_authority,
        role_repository,
        role_authority,
    )


def test_accepted_candidate_only_creates_pending_proposal_with_verified_receipt() -> None:
    """A verified receipt reaches pending proposals but never canonical memory."""

    (
        connection,
        runtime,
        clock,
        policy_authority,
        acceptance_authority,
        role_repository,
        role_authority,
    ) = _runtime_with_acceptance()
    submitter = runtime.register_session(_session()).session
    reviewer_input = replace(
        _session(),
        session_id="agent-session:reviewer",
        identity=AgentIdentity("agent:reviewer", "deepsec_reviewer"),
    )
    reviewer = runtime.register_session(reviewer_input).session
    work_receipt, lease = _work()
    runtime.register_work(work_receipt, max_attempts=2)
    runtime.claim_work(lease)
    clock.value = datetime(2026, 8, 11, 1, 0, 6, tzinfo=timezone.utc)
    submitter_assignment_sha256 = _issue_role_assignment(
        repository=role_repository,
        authority=role_authority,
        use=RESULT_SUBMISSION_USE,
        basis=RoleAssignmentBasis(
            session=submitter,
            work=work_receipt,
            lease=lease,
            intent_event=_role_intent(
                session=submitter,
                work=work_receipt,
                use=RESULT_SUBMISSION_USE,
                role=WORK_SUBMITTER_ROLE,
                stage="implement",
                created_at="2026-08-11T01:00:05.000000Z",
            ),
            workflow_stage="implement",
            work_state="in_progress",
            lease_state="active",
        ),
    )
    result = ResultReceipt.for_work(
        work_receipt,
        receipt_id="result:pr5:accepted",
        submitted_by=work_receipt.assigned_agent,
        outcome="completed",
        summary="completed result",
        submitted_at="2026-08-11T01:00:10.000000Z",
        role_assignment_sha256=submitter_assignment_sha256,
    )
    clock.value = datetime(2026, 8, 11, 1, 0, 15, tzinfo=timezone.utc)
    runtime.record_result(
        result,
        lease_id=lease.lease_id,
        fencing_generation=lease.fencing_generation,
        lease_sha256=lease.content_sha256,
    )
    runtime.review_work(work_receipt.work_item_id, reviewer_session_id=reviewer.session_id)
    clock.value = datetime(2026, 8, 11, 1, 0, 19, tzinfo=timezone.utc)
    reviewer_assignment_sha256 = _issue_role_assignment(
        repository=role_repository,
        authority=role_authority,
        use=ACCEPTANCE_REVIEW_USE,
        basis=RoleAssignmentBasis(
            session=reviewer,
            work=work_receipt,
            lease=lease,
            intent_event=_role_intent(
                session=reviewer,
                work=work_receipt,
                use=ACCEPTANCE_REVIEW_USE,
                role=WORK_REVIEWER_ROLE,
                stage="code-review",
                created_at="2026-08-11T01:00:18.000000Z",
            ),
            workflow_stage="code-review",
            work_state="reviewing",
            lease_state="completed",
            result=result,
            submitter_agent_session_id=submitter.session_id,
        ),
    )
    reviews = tuple(
        ReviewReceipt.for_result(
            work_receipt,
            result,
            review_receipt_id=f"review:pr5:accepted:{channel}",
            reviewer_assignment_sha256=reviewer_assignment_sha256,
            reviewer_agent_session_id=reviewer.session_id,
            review_policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
            source_revision="a" * 40,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc="2026-08-11T01:00:20.000000Z",
            evidence_refs=(f"evidence:review:{channel}",),
            review_channel=channel,
            diff_digest="sha256:" + "a" * 64,
            requirement_set_digest="sha256:" + "b" * 64,
            union_contract_revision="2026-08-11.3",
        )
        for channel in REVIEW_CHANNELS
    )
    reviewer_binding = policy_authority.issue(
        reviewer,
        policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
    )
    clock.value = datetime(2026, 8, 11, 1, 0, 30, tzinfo=timezone.utc)
    acceptance = acceptance_authority.issue(
        work_receipt,
        result,
        reviews,
        submitter_session=submitter,
        reviewer_session=reviewer,
        reviewer_policy_binding=reviewer_binding,
    )
    assert acceptance.submitter_assignment_sha256 == submitter_assignment_sha256
    assert acceptance.reviewer_assignment_sha256 == reviewer_assignment_sha256
    runtime.accept_work(
        work_receipt.work_item_id,
        reviewer_session_id=reviewer.session_id,
        acceptance_receipt=acceptance,
    )
    source_event = connection.execute(
        "SELECT event_id,event_sha256 FROM collaboration_events "
        "WHERE event_type='work.accepted' ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert source_event is not None
    candidate = PromotionCandidate(
        candidate_id="promotion-candidate:verified",
        project=work_receipt.project,
        coordination_session_id=work_receipt.coordination_session_id,
        work_item_id=work_receipt.work_item_id,
        source_event_id=str(source_event[0]),
        source_event_sha256=str(source_event[1]),
        work_receipt_sha256=work_receipt.content_sha256,
        result_receipt_sha256=result.content_sha256,
        acceptance_receipt_sha256=acceptance.content_sha256,
        summary="accepted work becomes reviewable",
        evidence_refs=("evidence:review",),
        idempotency_sha256="sha256:" + "4" * 64,
    )
    queued = runtime.enqueue_promotion(
        candidate,
        conflict_checked=True,
        acceptance_receipt=acceptance,
    )
    assert queued.status == "pending"
    ensure_memory_proposal_schema(connection)
    persisted = runtime.reconcile_promotions()
    assert persisted[0].status == "persisted"
    assert (
        connection.execute(
            "SELECT status FROM memory_proposals WHERE proposal_id=?",
            (persisted[0].proposal_id,),
        ).fetchone()[0]
        == "pending"
    )
    assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    connection.close()
