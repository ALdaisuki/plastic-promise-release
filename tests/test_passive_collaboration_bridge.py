from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

import pytest

from plastic_promise.collaboration.acceptance_receipt import (
    REVIEW_CHANNELS,
    AcceptanceReceipt,
    AcceptanceReceiptAuthority,
    AcceptanceReceiptError,
    ReviewReceipt,
    ServerAcceptanceSourceRegistry,
    VerifiedAcceptanceReceipt,
    open_server_acceptance_receipt_authority,
    open_server_acceptance_source_registry,
)
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    EventCursor,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.passive_bridge import (
    PassiveCollaborationBridgeError,
    PassiveCollaborationInput,
    PassiveCollaborationSource,
    compile_passive_collaboration,
    open_server_passive_collaboration_runtime,
)
from plastic_promise.collaboration.policy_binding import (
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

NOW = "2026-08-11T01:00:00Z"
SUBMITTED = "2026-08-11T01:10:00Z"
ACCEPTED = "2026-08-11T01:20:00Z"
SERVER_OBSERVED = "2026-08-11T01:30:00Z"
EXPIRES = "2026-08-11T02:00:00Z"
POLICY_REVISION = "acceptance-review-policy/v1"
SOURCE_REVISION = "e" * 40


def _registry_for(
    work: WorkReceipt,
    result: ResultReceipt,
    *sessions: AgentSession,
) -> ServerAcceptanceSourceRegistry:
    registry = open_server_acceptance_source_registry()
    registry.register(work, result, *sessions)
    return registry


def _session(
    identity: AgentIdentity,
    project: ProjectScope,
    *,
    session_id: str,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=identity,
        project=project,
        coordination_session_id="coord:pr3",
        state="active",
        started_at="2026-08-11T00:50:00Z",
        last_heartbeat_at=ACCEPTED,
        expires_at=EXPIRES,
    )


def _issue_acceptance(
    work: WorkReceipt,
    result: ResultReceipt,
    *,
    implementer: AgentIdentity,
    reviewer: AgentIdentity,
    evidence_refs: tuple[str, ...] = ("review:passive-bridge-accepted",),
    decision: str = "accepted",
    conflict_state: str = "none",
    verify_for_consumption: bool = True,
) -> tuple[
    AcceptanceReceipt | VerifiedAcceptanceReceipt,
    AcceptanceReceiptAuthority,
]:
    submitter_session = _session(
        implementer,
        work.project,
        session_id="agent-session:passive-submitter",
    )
    reviewer_session = _session(
        reviewer,
        work.project,
        session_id="agent-session:passive-reviewer",
    )
    now = datetime.fromisoformat(ACCEPTED.replace("Z", "+00:00"))
    policy_authority = open_server_agent_policy_binding_authority(clock=lambda: now)
    binding = policy_authority.issue(
        reviewer_session,
        policy_revision=POLICY_REVISION,
    )
    role_repository = InMemoryRoleAssignmentRepository()
    role_clock = [datetime.fromisoformat("2026-08-11T01:05:00+00:00")]
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=lambda: role_clock[0],
    )
    work_item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:passive-bridge",
        created_at=NOW,
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    lease = WorkLease(
        lease_id="lease:passive-bridge",
        work_item=work_item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=implementer.agent_id,
        owner_identity=implementer,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at="2026-08-11T01:01:00Z",
        expires_at=EXPIRES,
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )

    def _intent(
        session: AgentSession,
        *,
        use: str,
        role: str,
        stage: str,
        suffix: str,
    ) -> CollaborationEvent:
        return CollaborationEvent(
            event_id=f"event:intent:{session.session_id}:{work.work_item_id}:{use}:{suffix}",
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            actor=session.identity,
            event_type="agent.intent_declared",
            summary=f"Intent for {use}",
            created_at="2026-08-11T01:04:00Z",
            work_item_id=work.work_item_id,
            payload={
                "requested_use": use,
                "requested_role": role,
                "workflow_stage": stage,
                "authority_effect": "none",
            },
        )

    submitter_basis = RoleAssignmentBasis(
        session=submitter_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            submitter_session,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
            suffix="submitter",
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )
    role_repository.register_basis(use=RESULT_SUBMISSION_USE, basis=submitter_basis)
    submitter_assignment = role_authority.issue(
        use=RESULT_SUBMISSION_USE,
        agent_session_id=submitter_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=submitter_basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    role_clock[0] = datetime.fromisoformat("2026-08-11T01:13:00+00:00")
    result = replace(
        result,
        role_assignment_sha256=submitter_assignment.assignment_sha256,
    )
    reviewer_basis = RoleAssignmentBasis(
        session=reviewer_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            reviewer_session,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
            suffix="reviewer",
        ),
        workflow_stage="code-review",
        work_state="reviewing",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter_session.session_id,
    )
    role_repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=reviewer_basis)
    reviewer_assignment = role_authority.issue(
        use=ACCEPTANCE_REVIEW_USE,
        agent_session_id=reviewer_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=reviewer_basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    role_clock[0] = now
    reviews = tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"review-receipt:passive-bridge:{channel}",
            reviewer_agent_session_id=reviewer_session.session_id,
            reviewer_assignment_sha256=reviewer_assignment.assignment_sha256,
            review_policy_revision=POLICY_REVISION,
            source_revision=SOURCE_REVISION,
            decision=decision,
            conflict_state=conflict_state,
            reviewed_at_utc=ACCEPTED,
            evidence_refs=evidence_refs,
            review_channel=channel,
        )
        for channel in REVIEW_CHANNELS
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        source_registry=_registry_for(
            work,
            result,
            submitter_session,
            reviewer_session,
        ),
        clock=lambda: now,
    )
    receipt = authority.issue(
        work,
        result,
        reviews,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
        reviewer_policy_binding=binding,
        acceptance_receipt_id="acceptance-receipt:passive-bridge",
    )
    if not verify_for_consumption:
        return receipt, authority
    return authority.verify_for_consumption(receipt), authority


def _fixture() -> tuple[
    ProjectScope,
    AgentIdentity,
    AgentIdentity,
    WorkReceipt,
    ResultReceipt,
    VerifiedAcceptanceReceipt,
    AcceptanceReceiptAuthority,
]:
    project = ProjectScope("project:plastic-promise")
    implementer = AgentIdentity("agent:implementer", "implementer")
    reviewer = AgentIdentity("agent:reviewer", "deepsec_reviewer")
    work = WorkReceipt(
        receipt_id="work-receipt:passive-bridge",
        work_item_id="work:passive-memory-bridge",
        project=project,
        coordination_session_id="coord:pr3",
        assigned_agent=implementer,
        objective="Connect accepted collaboration work to pending proposals",
        fencing_generation=3,
        issued_at=NOW,
        expires_at=EXPIRES,
    )
    result = ResultReceipt.for_work(
        work,
        receipt_id="result-receipt:passive-bridge",
        submitted_by=implementer,
        outcome="completed",
        summary="Passive collaboration bridge implemented",
        submitted_at=SUBMITTED,
        artifact_refs=("artifact:passive-bridge-diff",),
        evidence_refs=("test:passive-collaboration-bridge",),
        result={"tests": "focused-pass"},
    )
    acceptance, authority = _issue_acceptance(
        work,
        result,
        implementer=implementer,
        reviewer=reviewer,
    )
    assert isinstance(acceptance, VerifiedAcceptanceReceipt)
    result = replace(
        result,
        role_assignment_sha256=acceptance._receipt.submitter_assignment_sha256,
    )
    return project, implementer, reviewer, work, result, acceptance, authority


def test_progress_is_ephemeral_event_only_and_deterministically_idempotent() -> None:
    project, implementer, _, work, _, _, _ = _fixture()
    request = PassiveCollaborationInput(
        kind="progress",
        source="stop_hook",
        project=project,
        coordination_session_id="coord:pr3",
        actor=implementer,
        work=work,
        server_observed_at_utc=SUBMITTED,
        summary="Implemented the pure adapter; focused tests remain",
        evidence_refs=("diff:passive-bridge",),
        audience_roles=("coordinator", "reviewer"),
    )

    first = compile_passive_collaboration(request)
    replay = compile_passive_collaboration(request)

    assert first == replay
    assert first.idempotency_sha256.startswith("sha256:")
    assert first.event.event_id == replay.event.event_id
    assert first.event.event_type == "work.progressed"
    assert first.event.work_item_id == work.work_item_id
    assert first.event.payload["bridge_kind"] == "progress"
    assert first.event.payload["canonical_memory_effect"] == "none"
    assert first.promotion_candidate is None
    assert first.reason_code == "passive_bridge_progress_event_only"
    assert first.to_dict()["canonical_memory_effect"] == "none"


def test_submitted_result_is_bound_but_does_not_create_promotion_candidate() -> None:
    project, implementer, _, work, result, _, _ = _fixture()
    plan = compile_passive_collaboration(
        PassiveCollaborationInput(
            kind="submitted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=implementer,
            work=work,
            result=result,
            server_observed_at_utc=SERVER_OBSERVED,
        )
    )

    assert plan.event.event_type == "work.submitted"
    assert plan.event.summary == result.summary
    assert plan.event.payload["result_receipt_sha256"] == result.content_sha256
    assert plan.promotion_candidate is None
    assert plan.reason_code == "passive_bridge_submitted_event_only"


def test_receipt_backed_event_identity_ignores_later_server_observation_time() -> None:
    project, implementer, _, work, result, _, _ = _fixture()
    canonical = compile_passive_collaboration(
        PassiveCollaborationInput(
            kind="submitted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=implementer,
            work=work,
            result=result,
            server_observed_at_utc=ACCEPTED,
        )
    )
    spoofed = compile_passive_collaboration(
        PassiveCollaborationInput(
            kind="submitted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=implementer,
            work=work,
            result=result,
            server_observed_at_utc=SERVER_OBSERVED,
        )
    )

    assert spoofed.event.created_at == result.submitted_at
    assert spoofed.event.event_id == canonical.event.event_id
    assert spoofed.idempotency_sha256 == canonical.idempotency_sha256


def test_independently_accepted_result_creates_pending_only_promotion_command() -> None:
    project, _, reviewer, work, result, acceptance, authority = _fixture()
    plan = compile_passive_collaboration(
        PassiveCollaborationInput(
            kind="accepted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=reviewer,
            work=work,
            result=result,
            acceptance=acceptance,
            server_observed_at_utc=SERVER_OBSERVED,
        ),
        acceptance_authority=authority,
    )

    assert plan.event.event_type == "work.accepted"
    assert plan.event.payload["bridge_kind"] == "accepted"
    assert plan.event.payload["acceptance_receipt_sha256"] == acceptance.content_sha256
    candidate = plan.promotion_candidate
    assert candidate is not None
    assert candidate.work_receipt_sha256 == work.content_sha256
    assert candidate.result_receipt_sha256 == result.content_sha256
    assert candidate.acceptance_receipt_sha256 == acceptance.content_sha256
    projection = candidate.to_dict()
    assert projection["command"] == "create_pending_memory_proposal"
    assert projection["target_state"] == "pending"
    assert projection["canonical_memory_effect"] == "none"
    assert projection["direct_memory_write_allowed"] is False
    assert projection["requires_server_receipt_verification"] is True
    assert "canonical" not in str(projection["target_state"])


def test_accepted_result_requires_the_exact_issuing_authority() -> None:
    project, implementer, reviewer, work, result, acceptance, authority = _fixture()
    request = PassiveCollaborationInput(
        kind="accepted",
        source="stop_hook",
        project=project,
        coordination_session_id="coord:pr3",
        actor=reviewer,
        work=work,
        result=result,
        acceptance=acceptance,
        server_observed_at_utc=SERVER_OBSERVED,
    )
    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="^passive_bridge_acceptance_authority_required$",
    ):
        compile_passive_collaboration(request)

    foreign_proof, foreign_authority = _issue_acceptance(
        work,
        result,
        implementer=implementer,
        reviewer=reviewer,
    )
    assert isinstance(foreign_proof, VerifiedAcceptanceReceipt)
    assert foreign_authority is not authority
    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="^passive_bridge_acceptance_authority_rejected$",
    ):
        compile_passive_collaboration(
            replace(request, acceptance=foreign_proof),
            acceptance_authority=authority,
        )


def test_result_and_acceptance_must_bind_project_session_actor_work_and_digests() -> None:
    project, implementer, reviewer, work, result, _, _ = _fixture()
    other_project = ProjectScope("project:other")

    with pytest.raises(PassiveCollaborationBridgeError, match="passive_bridge_project_mismatch"):
        compile_passive_collaboration(
            PassiveCollaborationInput(
                kind="progress",
                source="stop_hook",
                project=other_project,
                coordination_session_id="coord:pr3",
                actor=implementer,
                work=work,
                server_observed_at_utc=SERVER_OBSERVED,
                summary="Progress",
            )
        )
    with pytest.raises(PassiveCollaborationBridgeError, match="passive_bridge_session_mismatch"):
        compile_passive_collaboration(
            PassiveCollaborationInput(
                kind="submitted",
                source="stop_hook",
                project=project,
                coordination_session_id="coord:other",
                actor=implementer,
                work=work,
                result=result,
                server_observed_at_utc=SERVER_OBSERVED,
            )
        )
    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="passive_bridge_actor_work_mismatch",
    ):
        compile_passive_collaboration(
            PassiveCollaborationInput(
                kind="submitted",
                source="stop_hook",
                project=project,
                coordination_session_id="coord:pr3",
                actor=reviewer,
                work=work,
                result=result,
                server_observed_at_utc=SERVER_OBSERVED,
            )
        )

    raw_acceptance, _ = _issue_acceptance(
        work,
        result,
        implementer=implementer,
        reviewer=reviewer,
        verify_for_consumption=False,
    )
    assert isinstance(raw_acceptance, AcceptanceReceipt)
    forged_acceptance = replace(
        raw_acceptance,
        accepted_by=AgentIdentity("agent:forged-reviewer", "deepsec_reviewer"),
    )
    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="passive_bridge_acceptance",
    ):
        PassiveCollaborationInput(
            kind="accepted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=forged_acceptance.accepted_by,
            work=work,
            result=result,
            acceptance=forged_acceptance,
            server_observed_at_utc=SERVER_OBSERVED,
        )


def test_accepted_work_requires_independent_evidenced_acceptance_and_completed_result() -> None:
    project, implementer, reviewer, work, result, _, _ = _fixture()
    submitter_session = _session(
        implementer,
        project,
        session_id="agent-session:self-review",
    )
    now = datetime.fromisoformat(ACCEPTED.replace("Z", "+00:00"))
    policy_authority = open_server_agent_policy_binding_authority(clock=lambda: now)
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=open_server_role_assignment_authority(
            repository=InMemoryRoleAssignmentRepository(),
        ),
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        source_registry=_registry_for(
            work,
            result,
            submitter_session,
        ),
        clock=lambda: now,
    )
    self_review = tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"review-receipt:self:{channel}",
            reviewer_agent_session_id=submitter_session.session_id,
            reviewer_assignment_sha256="sha256:" + "0" * 64,
            review_policy_revision=POLICY_REVISION,
            source_revision=SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=ACCEPTED,
            evidence_refs=("review:self",),
            review_channel=channel,
        )
        for channel in REVIEW_CHANNELS
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="acceptance_independent_reviewer_required",
    ):
        authority.issue(
            work,
            result,
            self_review,
            submitter_session=submitter_session,
            reviewer_session=submitter_session,
            reviewer_policy_binding=None,
        )
    with pytest.raises(
        AcceptanceReceiptError,
        match="review_receipt_evidence_required",
    ):
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id="review-receipt:no-evidence",
            reviewer_agent_session_id="agent-session:reviewer",
            reviewer_assignment_sha256="sha256:" + "0" * 64,
            review_policy_revision=POLICY_REVISION,
            source_revision=SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=ACCEPTED,
            evidence_refs=(),
        )

    failed = ResultReceipt.for_work(
        work,
        receipt_id="result-receipt:failed",
        submitted_by=implementer,
        outcome="failed",
        summary="Implementation failed",
        submitted_at=SUBMITTED,
        evidence_refs=("test:failed",),
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="acceptance_result_not_completed",
    ):
        _issue_acceptance(
            work,
            failed,
            implementer=implementer,
            reviewer=reviewer,
            evidence_refs=("review:invalid-acceptance",),
        )


def test_secret_or_semantic_channels_cannot_cross_the_bridge() -> None:
    project, implementer, _, work, _, _, _ = _fixture()
    with pytest.raises(CollaborationContractError, match="secret_value_forbidden"):
        PassiveCollaborationInput(
            kind="progress",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=implementer,
            work=work,
            server_observed_at_utc=SERVER_OBSERVED,
            summary="ghp_" + "a" * 36,
        )

    # The bridge exposes no arbitrary payload input, and it does not copy the
    # work objective or result mapping into peer events or promotion commands.
    _, _, reviewer, work, result, acceptance, authority = _fixture()
    plan = compile_passive_collaboration(
        PassiveCollaborationInput(
            kind="accepted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=reviewer,
            work=work,
            result=result,
            acceptance=acceptance,
            server_observed_at_utc=SERVER_OBSERVED,
        ),
        acceptance_authority=authority,
    )
    serialized = json_text = str(plan.to_dict())
    assert work.objective not in serialized
    assert str(result.result) not in json_text
    assert "focused-pass" not in serialized


def test_summary_cannot_drift_from_the_bound_result() -> None:
    project, implementer, _, work, result, _, _ = _fixture()
    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="passive_bridge_summary_result_mismatch",
    ):
        PassiveCollaborationInput(
            kind="submitted",
            source="stop_hook",
            project=project,
            coordination_session_id="coord:pr3",
            actor=implementer,
            work=work,
            result=result,
            server_observed_at_utc=SERVER_OBSERVED,
            summary="A materially different claim",
        )


def test_server_runtime_derives_progress_submitted_and_accepted_from_typed_sources() -> None:
    project, implementer, _, work, result, acceptance, authority = _fixture()
    appended = []

    def append_event(event):
        appended.append(event)
        return EventCursor(project, event.coordination_session_id, len(appended))

    runtime = open_server_passive_collaboration_runtime(
        append_event=append_event,
        clock=lambda: datetime.fromisoformat(SERVER_OBSERVED.replace("Z", "+00:00")),
    )
    progress_ref = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:pr3",
            hook_turn_id="turn:progress",
            work=work,
            progress_summary="Focused implementation is still in progress",
        )
    )
    submitted_ref = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:pr3",
            hook_turn_id="turn:submitted",
            work=work,
            result=result,
        )
    )
    accepted_ref = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:pr3",
            hook_turn_id="turn:accepted",
            work=work,
            result=result,
            acceptance=acceptance,
        ),
        acceptance_authority=authority,
    )

    progress = runtime.publish_stop(
        collaboration_ref=progress_ref,
        project=project,
        hook_session_id="hook-session:pr3",
        hook_turn_id="turn:progress",
    )
    submitted = runtime.publish_stop(
        collaboration_ref=submitted_ref,
        project=project,
        hook_session_id="hook-session:pr3",
        hook_turn_id="turn:submitted",
    )
    accepted = runtime.publish_stop(
        collaboration_ref=accepted_ref,
        project=project,
        hook_session_id="hook-session:pr3",
        hook_turn_id="turn:accepted",
    )

    assert [event.event_type for event in appended] == [
        "work.progressed",
        "work.submitted",
        "work.accepted",
    ]
    assert [progress.status, submitted.status, accepted.status] == [
        "recorded",
        "recorded",
        "recorded",
    ]
    assert [event.created_at for event in appended] == [
        SERVER_OBSERVED.replace("Z", ".000000Z"),
        SUBMITTED.replace("Z", ".000000Z"),
        ACCEPTED.replace("Z", ".000000Z"),
    ]
    assert accepted.promotion_status == "source-only-deferred"
    serialized = str(appended[-1].to_dict())
    assert work.objective not in serialized
    assert str(result.result) not in serialized
    assert "lease_token" not in serialized
    assert implementer.agent_id not in str(accepted.to_dict().get("reason"))


def test_server_runtime_retries_same_plan_and_then_reports_duplicate() -> None:
    project, _, _, work, result, _, _ = _fixture()
    attempts = []

    def append_event(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("temporary database outage")
        return EventCursor(project, event.coordination_session_id, 7)

    runtime = open_server_passive_collaboration_runtime(
        append_event=append_event,
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:retry",
            hook_turn_id="turn:retry",
            work=work,
            result=result,
        )
    )
    arguments = {
        "collaboration_ref": reference,
        "project": project,
        "hook_session_id": "hook-session:retry",
        "hook_turn_id": "turn:retry",
    }

    first = runtime.publish_stop(**arguments)
    second = runtime.publish_stop(**arguments)
    replay = runtime.publish_stop(**arguments)

    assert first.status == "retry"
    assert second.status == "recorded"
    assert replay.status == "duplicate"
    assert len(attempts) == 2
    assert attempts[0].event_id == attempts[1].event_id
    assert attempts[0].content_sha256 == attempts[1].content_sha256


def test_server_runtime_does_not_hold_registry_lock_during_event_append() -> None:
    project, _, _, work, _, _, _ = _fixture()
    append_started = threading.Event()
    release_append = threading.Event()
    publications = []

    def append_event(event):
        append_started.set()
        assert release_append.wait(timeout=2)
        return EventCursor(project, event.coordination_session_id, 1)

    runtime = open_server_passive_collaboration_runtime(
        append_event=append_event,
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:concurrent",
            hook_turn_id="turn:concurrent",
            work=work,
            progress_summary="Append is in flight",
        )
    )
    arguments = {
        "collaboration_ref": reference,
        "project": project,
        "hook_session_id": "hook-session:concurrent",
        "hook_turn_id": "turn:concurrent",
    }

    first = threading.Thread(target=lambda: publications.append(runtime.publish_stop(**arguments)))
    first.start()
    assert append_started.wait(timeout=1)
    concurrent = runtime.publish_stop(**arguments)
    release_append.set()
    first.join(timeout=2)

    assert not first.is_alive()
    assert concurrent.status == "retry"
    assert concurrent.reason == "passive_collaboration_event_append_in_progress"
    assert publications[0].status == "recorded"
    assert runtime.publish_stop(**arguments).status == "duplicate"


def test_server_runtime_never_records_a_foreign_event_cursor() -> None:
    project, _, _, work, _, _, _ = _fixture()
    attempts = []

    def append_event(event):
        attempts.append(event)
        return EventCursor(
            ProjectScope("project:foreign"),
            event.coordination_session_id,
            len(attempts),
        )

    runtime = open_server_passive_collaboration_runtime(
        append_event=append_event,
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:cursor",
            hook_turn_id="turn:cursor",
            work=work,
            progress_summary="Validate append cursor",
        )
    )
    arguments = {
        "collaboration_ref": reference,
        "project": project,
        "hook_session_id": "hook-session:cursor",
        "hook_turn_id": "turn:cursor",
    }

    first = runtime.publish_stop(**arguments)
    second = runtime.publish_stop(**arguments)

    assert first.status == second.status == "retry"
    assert first.reason == second.reason == "passive_collaboration_event_cursor_invalid"
    assert len(attempts) == 2


def test_server_runtime_never_records_a_zero_sequence_cursor() -> None:
    project, _, _, work, _, _, _ = _fixture()
    attempts = []

    def append_event(event):
        attempts.append(event)
        return EventCursor(project, event.coordination_session_id, 0)

    runtime = open_server_passive_collaboration_runtime(
        append_event=append_event,
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:zero-cursor",
            hook_turn_id="turn:zero-cursor",
            work=work,
            progress_summary="Validate append advancement",
        )
    )
    arguments = {
        "collaboration_ref": reference,
        "project": project,
        "hook_session_id": "hook-session:zero-cursor",
        "hook_turn_id": "turn:zero-cursor",
    }

    first = runtime.publish_stop(**arguments)
    second = runtime.publish_stop(**arguments)

    assert first.status == second.status == "retry"
    assert first.reason == second.reason == "passive_collaboration_event_cursor_invalid"
    assert len(attempts) == 2


def test_source_transition_cannot_widen_audience() -> None:
    project, _, _, work, result, _, _ = _fixture()
    runtime = open_server_passive_collaboration_runtime(
        append_event=lambda event: EventCursor(project, event.coordination_session_id, 1),
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    restricted = PassiveCollaborationSource(
        hook_session_id="hook-session:audience",
        hook_turn_id="turn:audience",
        work=work,
        result=result,
        audience_roles=("deepsec_reviewer",),
    )
    runtime.register_source(restricted)

    with pytest.raises(
        PassiveCollaborationBridgeError,
        match="passive_collaboration_source_visibility_widening",
    ):
        runtime.register_source(replace(restricted, audience_roles=()))


def test_server_runtime_skips_missing_context_and_rejects_cross_scope_reference() -> None:
    project, _, _, work, _, _, _ = _fixture()
    runtime = open_server_passive_collaboration_runtime(
        append_event=lambda event: EventCursor(project, event.coordination_session_id, 1),
        clock=lambda: datetime.fromisoformat(SUBMITTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:bound",
            hook_turn_id="turn:bound",
            work=work,
            progress_summary="Bounded progress",
        )
    )

    absent = runtime.publish_stop(
        collaboration_ref=None,
        project=project,
        hook_session_id="hook-session:bound",
        hook_turn_id="turn:bound",
    )
    unavailable = runtime.publish_stop(
        collaboration_ref="collaboration-ref:" + "A" * 32,
        project=project,
        hook_session_id="hook-session:bound",
        hook_turn_id="turn:bound",
    )
    foreign = runtime.publish_stop(
        collaboration_ref=reference,
        project=ProjectScope("project:foreign"),
        hook_session_id="hook-session:bound",
        hook_turn_id="turn:bound",
    )

    assert absent.status == "skipped"
    assert unavailable.status == "skipped"
    assert foreign.status == "rejected"
    assert foreign.reason == "passive_collaboration_scope_mismatch"


def test_server_runtime_rejects_acceptance_proof_from_foreign_authority() -> None:
    project, implementer, reviewer, work, result, _, canonical_authority = _fixture()
    foreign_proof, _foreign_authority = _issue_acceptance(
        work,
        result,
        implementer=implementer,
        reviewer=reviewer,
    )
    assert isinstance(foreign_proof, VerifiedAcceptanceReceipt)
    runtime = open_server_passive_collaboration_runtime(
        append_event=lambda event: EventCursor(project, event.coordination_session_id, 1),
        clock=lambda: datetime.fromisoformat(ACCEPTED.replace("Z", "+00:00")),
    )
    reference = runtime.register_source(
        PassiveCollaborationSource(
            hook_session_id="hook-session:foreign",
            hook_turn_id="turn:foreign",
            work=work,
            result=result,
            acceptance=foreign_proof,
        ),
        acceptance_authority=canonical_authority,
    )

    publication = runtime.publish_stop(
        collaboration_ref=reference,
        project=project,
        hook_session_id="hook-session:foreign",
        hook_turn_id="turn:foreign",
    )

    assert publication.status == "rejected"
    assert publication.reason == "passive_bridge_acceptance_authority_rejected"
