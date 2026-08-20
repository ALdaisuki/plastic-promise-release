"""Focused PR5 acceptance-state authority and idempotency checks."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.acceptance_receipt import (
    REVIEW_CHANNELS,
    AcceptanceReceipt,
    AcceptanceReceiptAuthority,
    ReviewReceipt,
    open_server_acceptance_receipt_authority,
    open_server_acceptance_source_registry,
)
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.durable_runtime import (
    DurableCollaborationError,
    DurableCollaborationRuntime,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.passive_bridge import PromotionCandidate
from plastic_promise.collaboration.policy_binding import (
    ACCEPTANCE_REVIEW_POLICY_REVISION,
    AgentPolicyBindingAuthority,
    open_server_agent_policy_binding_authority,
)
from plastic_promise.collaboration.role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    InMemoryRoleAssignmentRepository,
    RoleAssignmentBasis,
    open_server_role_assignment_authority,
)
from tests.pr5_schema_fixture import install_pr5_collaboration_schema

BASE = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
DIFF_DIGEST = "sha256:" + "a" * 64
REQUIREMENT_SET_DIGEST = "sha256:" + "b" * 64
UNION_CONTRACT_REVISION = "2026-08-11.3"


@dataclass
class MutableClock:
    value: datetime = BASE

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


@dataclass
class GateHarness:
    connection: sqlite3.Connection
    clock: MutableClock
    runtime: DurableCollaborationRuntime
    policy_authority: AgentPolicyBindingAuthority
    acceptance_authority: AcceptanceReceiptAuthority
    work: WorkReceipt
    lease: WorkLease
    result: ResultReceipt
    submitter: AgentSession
    reviewer: AgentSession
    acceptance: AcceptanceReceipt


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _session(
    *,
    session_id: str,
    identity: AgentIdentity,
    project: ProjectScope,
    coordination_session_id: str,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=identity,
        project=project,
        coordination_session_id=coordination_session_id,
        state="active",
        started_at=_text(BASE),
        last_heartbeat_at=_text(BASE),
        expires_at=_text(BASE + timedelta(hours=2)),
    )


def _install_schema(
    connection: sqlite3.Connection,
    writer: ReentrantWriter,
    clock: MutableClock,
) -> None:
    install_pr5_collaboration_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        suffix="pr5-acceptance-gate",
    )


def _intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
    use: str,
    role: str,
    stage: str,
    created_at: datetime,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=f"event:intent:{session.session_id}:{work.work_item_id}:{use}",
        project=work.project,
        coordination_session_id=work.coordination_session_id,
        actor=session.identity,
        event_type="agent.intent_declared",
        summary=f"Intent for {use}",
        created_at=_text(created_at),
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _work(*, project: ProjectScope, assigned_agent: AgentIdentity, suffix: str) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=f"work-receipt:acceptance-gate-{suffix}",
        work_item_id=f"work:acceptance-gate-{suffix}",
        project=project,
        coordination_session_id="coord:acceptance-gate",
        assigned_agent=assigned_agent,
        objective="Exercise one exact durable acceptance transition",
        fencing_generation=1,
        issued_at=_text(BASE),
        expires_at=_text(BASE + timedelta(hours=1)),
    )


def _lease(work: WorkReceipt, *, suffix: str) -> WorkLease:
    item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:acceptance-gate",
        created_at=_text(BASE),
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    return WorkLease(
        lease_id=f"lease:acceptance-gate-{suffix}",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=work.assigned_agent.agent_id,
        owner_identity=work.assigned_agent,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at=_text(BASE + timedelta(minutes=1)),
        expires_at=_text(BASE + timedelta(minutes=30)),
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + ("2" if suffix == "one" else "3") * 64,
    )


def _submitter_assignment(
    *,
    repository: InMemoryRoleAssignmentRepository,
    authority,
    work: WorkReceipt,
    lease: WorkLease,
    submitter: AgentSession,
) -> str:
    basis = RoleAssignmentBasis(
        session=submitter,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=submitter,
            work=work,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
            created_at=BASE + timedelta(minutes=1, seconds=30),
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )
    repository.register_basis(use=RESULT_SUBMISSION_USE, basis=basis)
    assignment = authority.issue(
        use=RESULT_SUBMISSION_USE,
        agent_session_id=submitter.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
    )
    return assignment.assignment_sha256


def _reviewer_assignment(
    *,
    repository: InMemoryRoleAssignmentRepository,
    authority,
    work: WorkReceipt,
    lease: WorkLease,
    result: ResultReceipt,
    submitter: AgentSession,
    reviewer: AgentSession,
) -> str:
    basis = RoleAssignmentBasis(
        session=reviewer,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=reviewer,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
            created_at=BASE + timedelta(minutes=4, seconds=30),
        ),
        workflow_stage="code-review",
        work_state="reviewing",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter.session_id,
    )
    repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=basis)
    assignment = authority.issue(
        use=ACCEPTANCE_REVIEW_USE,
        agent_session_id=reviewer.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
    )
    return assignment.assignment_sha256


def _harness() -> GateHarness:
    connection = sqlite3.connect(":memory:")
    writer = ReentrantWriter(connection)
    clock = MutableClock()
    _install_schema(connection, writer, clock)
    project = ProjectScope("project:acceptance-gate")
    # A durable session owns only its least-privilege baseline identity.
    # ``work.submitter`` is issued later from the server-derived work/lease
    # basis; it is deliberately not a long-lived ``AgentIdentity.role``.
    submitter_identity = AgentIdentity("agent:acceptance-builder", "participant")
    reviewer_identity = AgentIdentity("agent:acceptance-reviewer", "deepsec_reviewer")
    submitter_input = _session(
        session_id="agent-session:acceptance-builder",
        identity=submitter_identity,
        project=project,
        coordination_session_id="coord:acceptance-gate",
    )
    reviewer_input = _session(
        session_id="agent-session:acceptance-reviewer",
        identity=reviewer_identity,
        project=project,
        coordination_session_id="coord:acceptance-gate",
    )
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
        current_source_revision="c" * 40,
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
        event_retention_seconds=3600,
    )
    assert runtime.role_assignment_authority is role_authority
    submitter = runtime.register_session(submitter_input).session
    reviewer = runtime.register_session(reviewer_input).session
    work = _work(project=project, assigned_agent=submitter.identity, suffix="one")
    runtime.register_work(work, max_attempts=2)
    lease = _lease(work, suffix="one")
    clock.value = BASE + timedelta(minutes=1)
    runtime.claim_work(lease)
    clock.value = BASE + timedelta(minutes=2)
    submitter_assignment_sha256 = _submitter_assignment(
        repository=role_repository,
        authority=role_authority,
        work=work,
        lease=lease,
        submitter=submitter,
    )
    result = ResultReceipt.for_work(
        work,
        receipt_id="result:acceptance-gate-one",
        submitted_by=submitter.identity,
        outcome="completed",
        summary="Acceptance gate result",
        submitted_at=_text(BASE + timedelta(minutes=3)),
        role_assignment_sha256=submitter_assignment_sha256,
        evidence_refs=("evidence:acceptance-gate",),
    )
    clock.value = BASE + timedelta(minutes=3)
    runtime.record_result(
        result,
        lease_id=lease.lease_id,
        fencing_generation=lease.fencing_generation,
        lease_sha256=lease.content_sha256,
    )
    clock.value = BASE + timedelta(minutes=4)
    runtime.review_work(work.work_item_id, reviewer_session_id=reviewer.session_id)
    clock.value = BASE + timedelta(minutes=5)
    reviewer_assignment_sha256 = _reviewer_assignment(
        repository=role_repository,
        authority=role_authority,
        work=work,
        lease=lease,
        result=result,
        submitter=submitter,
        reviewer=reviewer,
    )
    reviews = tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"review:acceptance-gate-one:{channel}",
            reviewer_assignment_sha256=reviewer_assignment_sha256,
            reviewer_agent_session_id=reviewer.session_id,
            review_policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
            source_revision="c" * 40,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=_text(BASE + timedelta(minutes=5)),
            evidence_refs=(f"evidence:acceptance-review:{channel}",),
            review_channel=channel,
            diff_digest=DIFF_DIGEST,
            requirement_set_digest=REQUIREMENT_SET_DIGEST,
            union_contract_revision=UNION_CONTRACT_REVISION,
        )
        for channel in REVIEW_CHANNELS
    )
    reviewer_binding = policy_authority.issue(
        reviewer,
        policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
        binding_id="binding:acceptance-gate-reviewer",
    )
    clock.value = BASE + timedelta(minutes=6)
    acceptance = acceptance_authority.issue(
        work,
        result,
        reviews,
        submitter_session=submitter,
        reviewer_session=reviewer,
        reviewer_policy_binding=reviewer_binding,
        acceptance_receipt_id="acceptance:acceptance-gate-one",
    )
    return GateHarness(
        connection=connection,
        clock=clock,
        runtime=runtime,
        policy_authority=policy_authority,
        acceptance_authority=acceptance_authority,
        work=work,
        lease=lease,
        result=result,
        submitter=submitter,
        reviewer=reviewer,
        acceptance=acceptance,
    )


def _prepare_other_reviewing_work(gate: GateHarness) -> WorkReceipt:
    work = _work(
        project=gate.work.project,
        assigned_agent=gate.submitter.identity,
        suffix="two",
    )
    gate.runtime.register_work(work, max_attempts=2)
    lease = _lease(work, suffix="two")
    gate.runtime.claim_work(lease)
    result = ResultReceipt.for_work(
        work,
        receipt_id="result:acceptance-gate-two",
        submitted_by=gate.submitter.identity,
        outcome="completed",
        summary="Other result must not accept with the first receipt",
        submitted_at=_text(BASE + timedelta(minutes=7)),
    )
    gate.clock.value = BASE + timedelta(minutes=7)
    gate.runtime.record_result(
        result,
        lease_id=lease.lease_id,
        fencing_generation=lease.fencing_generation,
        lease_sha256=lease.content_sha256,
    )
    gate.clock.value = BASE + timedelta(minutes=8)
    gate.runtime.review_work(
        work.work_item_id,
        reviewer_session_id=gate.reviewer.session_id,
    )
    return work


@pytest.fixture
def gate() -> GateHarness:
    harness = _harness()
    try:
        yield harness
    finally:
        harness.connection.close()


def test_accept_work_rejects_digest_strings_and_self_issued_receipts(
    gate: GateHarness,
) -> None:
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_receipt_required$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance.content_sha256,  # type: ignore[arg-type]
        )
    forged = replace(
        gate.acceptance,
        acceptance_receipt_id="acceptance:acceptance-gate-forged",
    )
    with pytest.raises(
        DurableCollaborationError,
        match="^acceptance_receipt_not_server_issued$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=forged,
        )
    work = gate.runtime.get_work(gate.work.work_item_id)
    assert work is not None and work["state"] == "reviewing"
    assert (
        gate.connection.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.accepted'"
        ).fetchone()[0]
        == 0
    )


def test_accept_work_enforces_exact_work_result_and_reviewer_scope(
    gate: GateHarness,
) -> None:
    alternate_reviewer = replace(
        gate.reviewer,
        session_id="agent-session:acceptance-reviewer-alt",
        identity=AgentIdentity(
            "agent:acceptance-reviewer-alt",
            "deepsec_reviewer",
        ),
    )
    gate.runtime.register_session(alternate_reviewer)
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_reviewer_session_mismatch$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=alternate_reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )

    reviewer_row = gate.connection.execute(
        "SELECT session_json FROM collaboration_agent_sessions WHERE session_id=?",
        (gate.reviewer.session_id,),
    ).fetchone()
    assert reviewer_row is not None
    gate.connection.execute(
        "UPDATE collaboration_agent_sessions SET session_json='{}' WHERE session_id=?",
        (gate.reviewer.session_id,),
    )
    gate.connection.commit()
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_reviewer_session_digest_mismatch$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )
    gate.connection.execute(
        "UPDATE collaboration_agent_sessions SET session_json=? WHERE session_id=?",
        (reviewer_row[0], gate.reviewer.session_id),
    )
    gate.connection.commit()

    work_row = gate.connection.execute(
        "SELECT work_receipt_json FROM collaboration_work_items WHERE work_item_id=?",
        (gate.work.work_item_id,),
    ).fetchone()
    assert work_row is not None
    gate.connection.execute(
        "UPDATE collaboration_work_items SET work_receipt_json='{}' WHERE work_item_id=?",
        (gate.work.work_item_id,),
    )
    gate.connection.commit()
    with pytest.raises(
        DurableCollaborationError,
        match="^work_receipt_digest_mismatch$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )
    gate.connection.execute(
        "UPDATE collaboration_work_items SET work_receipt_json=? WHERE work_item_id=?",
        (work_row[0], gate.work.work_item_id),
    )
    gate.connection.commit()

    other_work = _prepare_other_reviewing_work(gate)
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_work_scope_mismatch$",
    ):
        gate.runtime.accept_work(
            other_work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )

    submitter_row = gate.connection.execute(
        "SELECT session_json FROM collaboration_agent_sessions WHERE session_id=?",
        (gate.submitter.session_id,),
    ).fetchone()
    assert submitter_row is not None
    gate.connection.execute(
        "UPDATE collaboration_agent_sessions SET session_json='{}' WHERE session_id=?",
        (gate.submitter.session_id,),
    )
    gate.connection.commit()
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_submitter_session_digest_mismatch$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )
    gate.connection.execute(
        "UPDATE collaboration_agent_sessions SET session_json=? WHERE session_id=?",
        (submitter_row[0], gate.submitter.session_id),
    )
    gate.connection.commit()

    gate.connection.execute(
        "UPDATE collaboration_results SET result_sha256=? WHERE receipt_id=?",
        ("sha256:" + "f" * 64, gate.result.receipt_id),
    )
    gate.connection.commit()
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_result_scope_mismatch$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )
    work = gate.runtime.get_work(gate.work.work_item_id)
    assert work is not None and work["state"] == "reviewing"


def test_accept_work_persists_one_idempotent_accepted_event(
    gate: GateHarness,
) -> None:
    gate.runtime.register_session(
        replace(
            gate.reviewer,
            session_id="agent-session:acceptance-reviewer-second-active",
        )
    )
    accepted = gate.runtime.accept_work(
        gate.work.work_item_id,
        reviewer_session_id=gate.reviewer.session_id,
        acceptance_receipt=gate.acceptance,
    )
    assert accepted["state"] == "accepted"
    assert accepted["promotion"]["status"] == "pending"
    assert gate.connection.execute(
        "SELECT COUNT(*) FROM collaboration_promotion_outbox WHERE status='pending'"
    ).fetchone() == (1,)
    first_event = gate.connection.execute(
        "SELECT event_id,event_json FROM collaboration_events WHERE event_type='work.accepted'"
    ).fetchone()
    assert first_event is not None
    gate.clock.value += timedelta(minutes=1)
    replayed = gate.runtime.accept_work(
        gate.work.work_item_id,
        reviewer_session_id=gate.reviewer.session_id,
        acceptance_receipt=gate.acceptance,
    )
    assert replayed["state"] == "accepted"
    assert replayed["promotion"]["candidate_id"] == accepted["promotion"]["candidate_id"]
    assert gate.connection.execute(
        "SELECT COUNT(*) FROM collaboration_promotion_outbox"
    ).fetchone() == (1,)
    second_event = gate.connection.execute(
        "SELECT event_id,event_json FROM collaboration_events WHERE event_type='work.accepted'"
    ).fetchone()
    assert second_event == first_event
    assert (
        gate.connection.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.accepted'"
        ).fetchone()[0]
        == 1
    )
    stored_projection = json.loads(str(first_event[1]))
    payload = stored_projection["payload"]
    assert payload["acceptance_receipt_id"] == gate.acceptance.acceptance_receipt_id
    assert payload["acceptance_receipt_sha256"] == gate.acceptance.content_sha256
    assert payload["result_receipt_sha256"] == gate.result.content_sha256
    diagnostics = payload["_server_time_diagnostics"]
    assert diagnostics["created_at"] == gate.acceptance.issued_at_utc
    source_projection = dict(stored_projection)
    source_payload = dict(payload)
    source_payload.pop("_server_time_diagnostics")
    source_projection["created_at"] = diagnostics["created_at"]
    source_projection["expires_at"] = diagnostics["expires_at"]
    source_projection["payload"] = source_payload
    source_event = CollaborationEvent.from_dict(source_projection)
    assert source_event.content_sha256 == diagnostics["source_event_sha256"]
    assert source_event.actor == gate.acceptance.accepted_by
    assert source_event.evidence_refs == gate.acceptance.evidence_refs

    tampered_projection = dict(stored_projection)
    tampered_projection["summary"] = "Tampered accepted event"
    tampered_event = CollaborationEvent.from_dict(tampered_projection)
    gate.connection.execute("DROP TRIGGER collaboration_events_no_update")
    gate.connection.execute(
        "UPDATE collaboration_events SET event_json=?,event_sha256=? WHERE event_id=?",
        (
            tampered_event.canonical_json(),
            tampered_event.content_sha256,
            first_event[0],
        ),
    )
    gate.connection.commit()
    with pytest.raises(
        DurableCollaborationError,
        match="^work_acceptance_event_conflict$",
    ):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )


def test_accept_work_rolls_back_acceptance_when_promotion_enqueue_fails(
    gate: GateHarness,
    monkeypatch,
) -> None:
    def fail_promotion(*_args, **_kwargs):
        raise DurableCollaborationError("promotion_enqueue_test_failure")

    monkeypatch.setattr(gate.runtime, "_enqueue_accepted_work_promotion", fail_promotion)
    with pytest.raises(DurableCollaborationError, match="^promotion_enqueue_test_failure$"):
        gate.runtime.accept_work(
            gate.work.work_item_id,
            reviewer_session_id=gate.reviewer.session_id,
            acceptance_receipt=gate.acceptance,
        )
    work = gate.runtime.get_work(gate.work.work_item_id)
    assert work is not None and work["state"] == "reviewing"
    assert gate.connection.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.accepted'"
    ).fetchone() == (0,)
    assert gate.connection.execute(
        "SELECT COUNT(*) FROM collaboration_promotion_outbox"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("case_id", "event_type", "payload", "result_outcome", "expected_rejection"),
    (
        pytest.param(
            "progress",
            "work.progressed",
            {"bridge_kind": "progress"},
            None,
            "promotion_source_not_accepted",
            id="progress",
        ),
        pytest.param(
            "assumption",
            "assumption.published",
            {"bridge_kind": "assumption"},
            None,
            "promotion_source_not_accepted",
            id="assumption",
        ),
        pytest.param(
            "peer-agreement",
            "finding.published",
            {"bridge_kind": "peer_agreement"},
            None,
            "promotion_source_not_accepted",
            id="peer-agreement",
        ),
        pytest.param(
            "semantic-capture",
            "finding.published",
            {"bridge_kind": "semantic_capture"},
            None,
            "promotion_source_not_accepted",
            id="semantic-capture",
        ),
        pytest.param(
            "self-acceptance",
            "work.accepted",
            {"bridge_kind": "accepted"},
            "self-accepted",
            "promotion_acceptance_receipt_unverified",
            id="self-acceptance",
        ),
        pytest.param(
            "raw-prompt",
            "finding.published",
            {"rawPrompt": "verbatim user content must never enter collaboration"},
            None,
            "collaboration_semantic_channel_forbidden",
            id="raw-prompt",
        ),
        pytest.param(
            "nonaccepted-result",
            "work.submitted",
            {"bridge_kind": "submitted", "result_outcome": "completed"},
            "completed",
            "promotion_source_not_accepted",
            id="nonaccepted-result",
        ),
        pytest.param(
            "failed-result",
            "work.submitted",
            {"bridge_kind": "submitted", "result_outcome": "failed"},
            "failed",
            "promotion_acceptance_binding_invalid",
            id="failed-result",
        ),
        pytest.param(
            "blocked-result",
            "work.submitted",
            {"bridge_kind": "submitted", "result_outcome": "blocked"},
            "blocked",
            "promotion_acceptance_binding_invalid",
            id="blocked-result",
        ),
    ),
)
def test_pr5_e04_nonaccepted_sources_cannot_enqueue_collaboration_promotion(
    gate: GateHarness,
    case_id: str,
    event_type: str,
    payload: dict[str, str],
    result_outcome: str | None,
    expected_rejection: str,
) -> None:
    if case_id == "raw-prompt":
        with pytest.raises(
            CollaborationContractError,
            match=f"^{expected_rejection}$",
        ):
            CollaborationEvent(
                event_id=f"event:promotion-negative:{case_id}",
                project=gate.work.project,
                coordination_session_id=gate.work.coordination_session_id,
                actor=gate.submitter.identity,
                event_type=event_type,
                summary=f"Bounded PR5-E04 negative source: {case_id}",
                created_at=_text(gate.clock.value),
                work_item_id=gate.work.work_item_id,
                payload=payload,
            )
        assert gate.connection.execute(
            "SELECT COUNT(*) FROM collaboration_promotion_outbox"
        ).fetchone() == (0,)
        return

    source_result = gate.result
    acceptance = gate.acceptance
    if result_outcome in {"failed", "blocked"}:
        source_result = ResultReceipt.for_work(
            gate.work,
            receipt_id=f"result:promotion-negative:{case_id}",
            submitted_by=gate.submitter.identity,
            outcome=result_outcome,
            summary=f"PR5-E04 {result_outcome} result",
            submitted_at=gate.result.submitted_at,
            role_assignment_sha256=gate.result.role_assignment_sha256,
            evidence_refs=(f"evidence:promotion-negative:{case_id}",),
        )
    elif result_outcome == "self-accepted":
        acceptance = replace(
            gate.acceptance,
            acceptance_receipt_id="acceptance:promotion-negative:self-acceptance",
            accepted_by=gate.submitter.identity,
        )

    source = CollaborationEvent(
        event_id=f"event:promotion-negative:{case_id}",
        project=gate.work.project,
        coordination_session_id=gate.work.coordination_session_id,
        actor=gate.submitter.identity,
        event_type=event_type,
        summary=f"Bounded PR5-E04 negative source: {case_id}",
        created_at=_text(gate.clock.value),
        work_item_id=gate.work.work_item_id,
        evidence_refs=(f"evidence:promotion-negative:{case_id}",),
        payload={
            **payload,
            "result_receipt_sha256": source_result.content_sha256,
            "acceptance_receipt_sha256": acceptance.content_sha256,
        },
    )
    gate.runtime.append_event(source, actor_session_id=gate.submitter.session_id)
    source_row = gate.connection.execute(
        "SELECT event_sha256 FROM collaboration_events WHERE event_id=?",
        (source.event_id,),
    ).fetchone()
    assert source_row is not None
    candidate = PromotionCandidate(
        candidate_id=f"promotion-candidate:negative:{case_id}",
        project=gate.work.project,
        coordination_session_id=gate.work.coordination_session_id,
        work_item_id=gate.work.work_item_id,
        source_event_id=source.event_id,
        source_event_sha256=str(source_row[0]),
        work_receipt_sha256=gate.work.content_sha256,
        result_receipt_sha256=source_result.content_sha256,
        acceptance_receipt_sha256=acceptance.content_sha256,
        summary=f"Must not enqueue: {case_id}",
        evidence_refs=(f"evidence:promotion-negative:{case_id}",),
        idempotency_sha256="sha256:" + "9" * 64,
    )

    if expected_rejection == "promotion_source_not_accepted":
        with pytest.raises(DurableCollaborationError, match=f"^{expected_rejection}$"):
            gate.runtime.enqueue_promotion(
                candidate,
                conflict_checked=True,
                acceptance_receipt=acceptance,
            )
    else:
        rejected = gate.runtime.enqueue_promotion(
            candidate,
            conflict_checked=True,
            acceptance_receipt=acceptance,
        )
        assert rejected.status == "rejected"
        assert rejected.reason == expected_rejection

    assert gate.connection.execute(
        "SELECT COUNT(*) FROM collaboration_promotion_outbox"
    ).fetchone() == (0,)
