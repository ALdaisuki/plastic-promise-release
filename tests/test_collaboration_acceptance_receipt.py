from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.acceptance_receipt import (
    ACCEPTANCE_RECEIPT_ISSUER,
    ACCEPTANCE_RECEIPT_SCHEMA,
    REVIEW_CHANNELS,
    REVIEW_RECEIPT_ISSUER,
    REVIEW_RECEIPT_SCHEMA,
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
    CollaborationEvent,
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
from plastic_promise.collaboration.policy_binding import (
    AgentPolicyBinding,
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

NOW = datetime(2026, 8, 11, 1, 20, tzinfo=timezone.utc)
WORK_ISSUED = "2026-08-11T01:00:00Z"
SUBMITTED = "2026-08-11T01:10:00Z"
REVIEWED = "2026-08-11T01:15:00Z"
EXPIRES = "2026-08-11T02:00:00Z"
POLICY_REVISION = "acceptance-review-policy/v1"
SOURCE_REVISION = "e" * 40
DIFF_DIGEST = "sha256:" + "a" * 64
REQUIREMENT_SET_DIGEST = "sha256:" + "b" * 64
UNION_CONTRACT_REVISION = "2026-08-11.3"


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


@dataclass
class AcceptanceHarness:
    clock: MutableClock
    role_clock: MutableClock
    project: ProjectScope
    submitter: AgentIdentity
    reviewer: AgentIdentity
    work: WorkReceipt
    lease: WorkLease
    result: ResultReceipt
    submitter_session: AgentSession
    reviewer_session: AgentSession
    reviews: tuple[ReviewReceipt, ...]
    policy_authority: AgentPolicyBindingAuthority
    reviewer_binding: AgentPolicyBinding
    source_registry: ServerAcceptanceSourceRegistry
    role_repository: InMemoryRoleAssignmentRepository
    role_authority: RoleAssignmentAuthority
    submitter_assignment_sha256: str
    reviewer_assignment_sha256: str
    authority: AcceptanceReceiptAuthority

    @property
    def review(self) -> ReviewReceipt:
        return self.reviews[0]

    def issue(
        self,
        *,
        reviews: tuple[ReviewReceipt, ...] | None = None,
        review: ReviewReceipt | None = None,
        submitter_session: AgentSession | None = None,
        reviewer_session: AgentSession | None = None,
        reviewer_binding: AgentPolicyBinding | None | object = ...,  # sentinel
        acceptance_receipt_id: str | None = "acceptance:exact-contract",
        result: ResultReceipt | None = None,
    ) -> AcceptanceReceipt:
        if reviews is not None and review is not None:
            raise AssertionError("pass either reviews or review")
        binding = self.reviewer_binding if reviewer_binding is ... else reviewer_binding
        assert binding is None or isinstance(binding, AgentPolicyBinding)
        effective_reviews = reviews or self.reviews
        if review is not None:
            effective_reviews = (review, *self.reviews[1:])
        return self.authority.issue(
            self.work,
            result or self.result,
            effective_reviews,
            submitter_session=submitter_session or self.submitter_session,
            reviewer_session=reviewer_session or self.reviewer_session,
            reviewer_policy_binding=binding,
            acceptance_receipt_id=acceptance_receipt_id,
        )


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _registry_for(
    *sources: WorkReceipt | ResultReceipt | AgentSession,
) -> ServerAcceptanceSourceRegistry:
    registry = open_server_acceptance_source_registry()
    if sources:
        registry.register(*sources)
    return registry


def _replace_reviews(
    reviews: tuple[ReviewReceipt, ...],
    **changes: object,
) -> tuple[ReviewReceipt, ...]:
    return tuple(replace(review, **changes) for review in reviews)


def _reviews_for_result(
    work: WorkReceipt,
    result: ResultReceipt,
    *,
    reviewer_assignment_sha256: str,
    reviewer_agent_session_id: str,
    review_policy_revision: str = POLICY_REVISION,
    source_revision: str = SOURCE_REVISION,
    decision: str = "accepted",
    conflict_state: str = "none",
    reviewed_at_utc: str = REVIEWED,
    id_prefix: str = "review-receipt:acceptance",
    evidence_prefix: str = "review:independent",
    diff_digest: str = DIFF_DIGEST,
    requirement_set_digest: str = REQUIREMENT_SET_DIGEST,
    union_contract_revision: str = UNION_CONTRACT_REVISION,
) -> tuple[ReviewReceipt, ...]:
    return tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"{id_prefix}:{channel}",
            reviewer_assignment_sha256=reviewer_assignment_sha256,
            reviewer_agent_session_id=reviewer_agent_session_id,
            review_policy_revision=review_policy_revision,
            source_revision=source_revision,
            decision=decision,
            conflict_state=conflict_state,
            reviewed_at_utc=reviewed_at_utc,
            evidence_refs=(f"{evidence_prefix}:{channel}",),
            review_channel=channel,
            diff_digest=diff_digest,
            requirement_set_digest=requirement_set_digest,
            union_contract_revision=union_contract_revision,
        )
        for channel in REVIEW_CHANNELS
    )


def _session(
    identity: AgentIdentity,
    project: ProjectScope,
    *,
    session_id: str,
    coordination_session_id: str = "coord:acceptance",
    heartbeat: datetime = NOW,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=identity,
        project=project,
        coordination_session_id=coordination_session_id,
        state="active",
        started_at="2026-08-11T00:50:00Z",
        last_heartbeat_at=_text(heartbeat),
        expires_at=EXPIRES,
    )


def _lease(work: WorkReceipt) -> WorkLease:
    item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:acceptance",
        created_at=WORK_ISSUED,
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    return WorkLease(
        lease_id="lease:acceptance",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=work.assigned_agent.agent_id,
        owner_identity=work.assigned_agent,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at="2026-08-11T01:01:00Z",
        expires_at="2026-08-11T01:45:00Z",
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )


def _intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
    use: str,
    role: str,
    stage: str,
    created_at: str,
    suffix: str,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=(f"event:intent:{session.session_id}:{work.work_item_id}:{use}:{suffix}"),
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


def _submitter_assignment(
    *,
    repository: InMemoryRoleAssignmentRepository,
    authority: RoleAssignmentAuthority,
    clock: MutableClock,
    work: WorkReceipt,
    lease: WorkLease,
    submitter_session: AgentSession,
) -> str:
    basis = RoleAssignmentBasis(
        session=submitter_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=submitter_session,
            work=work,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
            created_at="2026-08-11T01:04:00Z",
            suffix="submitter",
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )
    repository.register_basis(use=RESULT_SUBMISSION_USE, basis=basis)
    clock.value = datetime(2026, 8, 11, 1, 5, tzinfo=timezone.utc)
    assignment = authority.issue(
        use=RESULT_SUBMISSION_USE,
        agent_session_id=submitter_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    clock.value = NOW
    return assignment.assignment_sha256


def _reviewer_assignment(
    *,
    repository: InMemoryRoleAssignmentRepository,
    authority: RoleAssignmentAuthority,
    clock: MutableClock,
    work: WorkReceipt,
    lease: WorkLease,
    result: ResultReceipt,
    submitter_session: AgentSession,
    reviewer_session: AgentSession,
    suffix: str,
    issued_at: datetime = datetime(2026, 8, 11, 1, 13, tzinfo=timezone.utc),
) -> str:
    intent_at = min(issued_at - timedelta(seconds=30), NOW - timedelta(seconds=30))
    basis = RoleAssignmentBasis(
        session=reviewer_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=reviewer_session,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
            created_at=_text(intent_at),
            suffix=suffix,
        ),
        workflow_stage="code-review",
        work_state="reviewing",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter_session.session_id,
    )
    repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=basis)
    clock.value = issued_at
    assignment = authority.issue(
        use=ACCEPTANCE_REVIEW_USE,
        agent_session_id=reviewer_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    clock.value = max(NOW, issued_at + timedelta(minutes=10))
    return assignment.assignment_sha256


def _harness(*, clock: MutableClock | None = None) -> AcceptanceHarness:
    effective_clock = clock or MutableClock()
    role_clock = MutableClock()
    project = ProjectScope("project:plastic-promise")
    submitter = AgentIdentity("agent:implementer", "participant")
    reviewer = AgentIdentity("agent:reviewer", "deepsec_reviewer")
    work = WorkReceipt(
        receipt_id="work-receipt:acceptance",
        work_item_id="work:acceptance",
        project=project,
        coordination_session_id="coord:acceptance",
        assigned_agent=submitter,
        objective="Implement the exact AcceptanceReceipt contract",
        fencing_generation=4,
        issued_at=WORK_ISSUED,
        expires_at=EXPIRES,
    )
    lease = _lease(work)
    submitter_session = _session(
        submitter,
        project,
        session_id="agent-session:submitter",
        heartbeat=datetime(2026, 8, 11, 1, 10, tzinfo=timezone.utc),
    )
    reviewer_session = _session(
        reviewer,
        project,
        session_id="agent-session:reviewer",
        heartbeat=min(effective_clock.value, NOW),
    )
    role_repository = InMemoryRoleAssignmentRepository()
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=role_clock,
    )
    submitter_assignment_sha256 = _submitter_assignment(
        repository=role_repository,
        authority=role_authority,
        clock=role_clock,
        work=work,
        lease=lease,
        submitter_session=submitter_session,
    )
    result = ResultReceipt.for_work(
        work,
        receipt_id="result-receipt:acceptance",
        submitted_by=submitter,
        outcome="completed",
        summary="Exact acceptance contract implemented",
        submitted_at=SUBMITTED,
        role_assignment_sha256=submitter_assignment_sha256,
        artifact_refs=("artifact:acceptance-diff",),
        evidence_refs=("test:acceptance-contract",),
        result={"status": "focused-pass"},
    )
    reviewer_assignment_sha256 = _reviewer_assignment(
        repository=role_repository,
        authority=role_authority,
        clock=role_clock,
        work=work,
        lease=lease,
        result=result,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
        suffix="reviewer",
    )
    policy_authority = open_server_agent_policy_binding_authority(clock=effective_clock)
    reviewer_binding = policy_authority.issue(
        reviewer_session,
        binding_id="binding:acceptance-reviewer",
        policy_revision=POLICY_REVISION,
        ttl_seconds=1800,
    )
    reviews = _reviews_for_result(
        work,
        result,
        reviewer_assignment_sha256=reviewer_assignment_sha256,
        reviewer_agent_session_id=reviewer_session.session_id,
    )
    source_registry = _registry_for(
        work,
        result,
        submitter_session,
        reviewer_session,
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        source_registry=source_registry,
        clock=effective_clock,
    )
    return AcceptanceHarness(
        clock=effective_clock,
        role_clock=role_clock,
        project=project,
        submitter=submitter,
        reviewer=reviewer,
        work=work,
        lease=lease,
        result=result,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
        reviews=reviews,
        policy_authority=policy_authority,
        reviewer_binding=reviewer_binding,
        source_registry=source_registry,
        role_repository=role_repository,
        role_authority=role_authority,
        submitter_assignment_sha256=submitter_assignment_sha256,
        reviewer_assignment_sha256=reviewer_assignment_sha256,
        authority=authority,
    )


def test_server_issues_exact_secret_free_digest_bound_receipt() -> None:
    harness = _harness()
    receipt = harness.issue()

    assert harness.authority.verify_issued(receipt) is receipt
    verified = harness.authority.verify_for_consumption(receipt)
    assert isinstance(verified, VerifiedAcceptanceReceipt)
    assert verified.content_sha256 == receipt.content_sha256
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_verified_receipt_authority_required$",
    ):
        VerifiedAcceptanceReceipt(receipt)
    projection = receipt.to_dict()
    assert projection["schema_version"] == ACCEPTANCE_RECEIPT_SCHEMA
    assert projection["authority"] == "server-authenticated-decision"
    assert projection["issuer"] == ACCEPTANCE_RECEIPT_ISSUER
    assert projection["acceptance_receipt_id"] == "acceptance:exact-contract"
    assert projection["project_id"] == harness.project.project_id
    assert projection["coordination_session_id"] == harness.work.coordination_session_id
    assert projection["work_item_id"] == harness.work.work_item_id
    assert projection["work_receipt_sha256"] == harness.work.content_sha256
    assert projection["result_receipt_sha256"] == harness.result.content_sha256
    assert projection["submitter_agent_session_id"] == harness.submitter_session.session_id
    assert projection["reviewer_agent_session_id"] == harness.reviewer_session.session_id
    assert projection["submitter_assignment_sha256"] == (harness.submitter_assignment_sha256)
    assert projection["reviewer_assignment_sha256"] == (harness.reviewer_assignment_sha256)
    assert projection["review_policy_revision"] == POLICY_REVISION
    assert projection["source_revision"] == SOURCE_REVISION
    assert projection["decision"] == "accepted"
    assert projection["conflict_state"] == "none"
    assert projection["issued_at_utc"] == _text(NOW)
    assert projection["digests"] == {
        "work": harness.work.content_sha256,
        "result": harness.result.content_sha256,
        "submitter_assignment": harness.submitter_assignment_sha256,
        "reviewer_assignment": harness.reviewer_assignment_sha256,
        "evidence": receipt.evidence_sha256,
    }
    serialized = str(projection).casefold()
    assert not any(
        secret_key in serialized
        for secret_key in ("api_key", "authorization", "password", "private_key", "token")
    )


def test_review_assertion_and_caller_reported_role_do_not_grant_authority() -> None:
    harness = _harness()
    caller_reviewer = AgentIdentity("agent:caller-reviewer", "reviewer")
    caller_session = _session(
        caller_reviewer,
        harness.project,
        session_id="agent-session:caller-reviewer",
    )
    caller_assignment_sha256 = _reviewer_assignment(
        repository=harness.role_repository,
        authority=harness.role_authority,
        clock=harness.role_clock,
        work=harness.work,
        lease=harness.lease,
        result=harness.result,
        submitter_session=harness.submitter_session,
        reviewer_session=caller_session,
        suffix="caller-reviewer",
    )
    reviews = _reviews_for_result(
        harness.work,
        harness.result,
        reviewer_assignment_sha256=caller_assignment_sha256,
        reviewer_agent_session_id=caller_session.session_id,
        id_prefix="review-receipt:caller-claim",
        evidence_prefix="review:caller-claim",
    )
    harness.source_registry.register(caller_session)

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_reviewer_authority_invalid$",
    ):
        harness.issue(
            reviews=reviews,
            reviewer_session=caller_session,
            reviewer_binding=None,
        )

    forged_authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=open_server_agent_policy_binding_authority(clock=harness.clock),
        role_assignment_authority=harness.role_authority,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        source_registry=_registry_for(),
        clock=harness.clock,
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_not_server_issued$",
    ):
        forged_authority.verify_issued(harness.issue())
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_verified_receipt_authority_mismatch$",
    ):
        forged_authority.verify_consumption_proof(
            harness.authority.verify_for_consumption(harness.issue())
        )


@pytest.mark.parametrize(
    ("missing_source", "error"),
    [
        ("work", "acceptance_work_source_unverified"),
        ("result", "acceptance_result_source_unverified"),
        ("submitter_session", "acceptance_submitter_session_source_unverified"),
        ("reviewer_session", "acceptance_reviewer_session_source_unverified"),
    ],
)
def test_canonical_source_registry_requires_exact_registered_sources(
    missing_source: str,
    error: str,
) -> None:
    harness = _harness()
    sources: list[WorkReceipt | ResultReceipt | AgentSession] = []
    if missing_source != "work":
        sources.append(harness.work)
        if missing_source != "result":
            sources.append(harness.result)
    if missing_source != "submitter_session":
        sources.append(harness.submitter_session)
    if missing_source != "reviewer_session":
        sources.append(harness.reviewer_session)
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=harness.policy_authority,
        role_assignment_authority=harness.role_authority,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        source_registry=_registry_for(*sources),
        clock=harness.clock,
    )

    with pytest.raises(AcceptanceReceiptError, match=f"^{error}$"):
        authority.issue(
            harness.work,
            harness.result,
            harness.reviews,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            reviewer_policy_binding=harness.reviewer_binding,
        )


def test_source_registry_is_closed_exact_and_scope_bound() -> None:
    harness = _harness()
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_source_registry_server_required$",
    ):
        ServerAcceptanceSourceRegistry()

    registry = _registry_for(
        harness.work,
        harness.result,
        harness.submitter_session,
        harness.reviewer_session,
    )
    registry.register(
        harness.work,
        harness.result,
        harness.submitter_session,
        harness.reviewer_session,
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_work_source_conflict$",
    ):
        registry.register(replace(harness.work, objective="Drifted canonical work"))
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_result_source_conflict$",
    ):
        registry.register(replace(harness.result, summary="Drifted canonical result"))
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_session_source_conflict$",
    ):
        registry.register(
            replace(
                harness.submitter_session,
                last_heartbeat_at="2026-08-11T01:11:00Z",
            )
        )

    other_project_work = replace(
        harness.work,
        project=ProjectScope("project:other"),
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_result_work_source_unregistered$",
    ):
        _registry_for().register(
            replace(
                harness.result,
                project=other_project_work.project,
            )
        )

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_source_registry_invalid$",
    ):
        open_server_acceptance_receipt_authority(
            reviewer_policy_authority=harness.policy_authority,
            role_assignment_authority=harness.role_authority,
            current_review_policy_revision=POLICY_REVISION,
            current_source_revision=SOURCE_REVISION,
            source_registry=object(),  # type: ignore[arg-type]
            clock=harness.clock,
        )


def test_self_review_fails_even_when_the_review_assertion_names_that_session() -> None:
    harness = _harness()
    reviews = _replace_reviews(
        harness.reviews,
        reviewer_agent_session_id=harness.submitter_session.session_id,
    )

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_independent_reviewer_required$",
    ):
        harness.issue(
            reviews=reviews,
            reviewer_session=harness.submitter_session,
            reviewer_binding=None,
        )


@pytest.mark.parametrize(
    ("review", "error"),
    [
        (
            lambda value: replace(value, project=ProjectScope("project:other")),
            "acceptance_review_project_mismatch",
        ),
        (
            lambda value: replace(value, coordination_session_id="coord:other"),
            "acceptance_review_session_mismatch",
        ),
        (
            lambda value: replace(value, work_item_id="work:other"),
            "acceptance_review_work_item_mismatch",
        ),
        (
            lambda value: replace(value, work_receipt_sha256="sha256:" + "0" * 64),
            "acceptance_review_work_digest_mismatch",
        ),
        (
            lambda value: replace(value, result_receipt_sha256="sha256:" + "0" * 64),
            "acceptance_review_result_digest_mismatch",
        ),
    ],
)
def test_cross_scope_and_receipt_digest_drift_fail_closed(review, error: str) -> None:
    harness = _harness()
    with pytest.raises(AcceptanceReceiptError, match=f"^{error}$"):
        harness.issue(review=review(harness.review))


def test_evidence_tampering_and_secret_bearing_fields_fail_closed() -> None:
    harness = _harness()
    tampered = replace(harness.review, evidence_sha256="sha256:" + "0" * 64)
    with pytest.raises(
        AcceptanceReceiptError,
        match="^review_receipt_evidence_digest_mismatch$",
    ):
        harness.issue(review=tampered)

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_secret_value_forbidden$",
    ):
        ReviewReceipt.for_result(
            harness.work,
            harness.result,
            review_receipt_id="review-receipt:secret",
            reviewer_assignment_sha256=harness.reviewer_assignment_sha256,
            reviewer_agent_session_id=harness.reviewer_session.session_id,
            review_policy_revision=POLICY_REVISION,
            source_revision=SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=REVIEWED,
            evidence_refs=("ghp_" + "a" * 36,),
        )

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_source_revision_not_pinned$",
    ):
        replace(harness.review, source_revision="main")


def test_stale_policy_source_and_unresolved_conflict_fail_closed() -> None:
    harness = _harness()
    with pytest.raises(AcceptanceReceiptError, match="^acceptance_review_policy_stale$"):
        harness.issue(
            reviews=_replace_reviews(
                harness.reviews,
                review_policy_revision="acceptance-review-policy/v0",
            )
        )
    with pytest.raises(AcceptanceReceiptError, match="^acceptance_source_revision_stale$"):
        harness.issue(reviews=_replace_reviews(harness.reviews, source_revision="d" * 40))
    with pytest.raises(AcceptanceReceiptError, match="^acceptance_conflict_unresolved$"):
        harness.issue(reviews=_replace_reviews(harness.reviews, conflict_state="unresolved"))


def test_issue_and_review_must_follow_result_submission() -> None:
    early_clock = MutableClock(datetime(2026, 8, 11, 1, 5, tzinfo=timezone.utc))
    harness = _harness(clock=early_clock)
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_issued_before_submission$",
    ):
        harness.issue()

    normal = _harness()
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_review_before_submission$",
    ):
        normal.issue(
            reviews=_replace_reviews(
                normal.reviews,
                reviewed_at_utc="2026-08-11T01:05:00Z",
            )
        )


def test_accepted_decision_requires_completed_result() -> None:
    harness = _harness()
    failed = ResultReceipt.for_work(
        harness.work,
        receipt_id="result-receipt:failed",
        submitted_by=harness.submitter,
        outcome="failed",
        summary="Failed result",
        submitted_at=SUBMITTED,
        role_assignment_sha256=harness.submitter_assignment_sha256,
        evidence_refs=("test:failed",),
    )
    failed_reviewer_assignment_sha256 = _reviewer_assignment(
        repository=harness.role_repository,
        authority=harness.role_authority,
        clock=harness.role_clock,
        work=harness.work,
        lease=harness.lease,
        result=failed,
        submitter_session=harness.submitter_session,
        reviewer_session=harness.reviewer_session,
        suffix="failed-result",
    )
    reviews = _reviews_for_result(
        harness.work,
        failed,
        reviewer_assignment_sha256=failed_reviewer_assignment_sha256,
        reviewer_agent_session_id=harness.reviewer_session.session_id,
        id_prefix="review-receipt:failed",
        evidence_prefix="review:failed",
    )
    harness.source_registry.register(failed)
    with pytest.raises(AcceptanceReceiptError, match="^acceptance_result_not_completed$"):
        harness.issue(result=failed, reviews=reviews)


def test_replay_is_idempotent_but_conflicting_or_ambiguous_replay_fails() -> None:
    harness = _harness()
    first = harness.issue()
    harness.clock.value += timedelta(seconds=60)
    assert harness.issue() is first

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_replay_ambiguous$",
    ):
        harness.issue(acceptance_receipt_id="acceptance:second-id")

    conflicting_reviews = _replace_reviews(harness.reviews, decision="rejected")
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_decision_conflict$",
    ):
        harness.issue(reviews=conflicting_reviews)

    other_result = ResultReceipt.for_work(
        harness.work,
        receipt_id="result-receipt:other",
        submitted_by=harness.submitter,
        outcome="completed",
        summary="Different result",
        submitted_at="2026-08-11T01:21:00Z",
        role_assignment_sha256=harness.submitter_assignment_sha256,
        evidence_refs=("test:other",),
    )
    other_assignment_issued_at = datetime(2026, 8, 11, 1, 21, 30, tzinfo=timezone.utc)
    other_reviewer_assignment_sha256 = _reviewer_assignment(
        repository=harness.role_repository,
        authority=harness.role_authority,
        clock=harness.role_clock,
        work=harness.work,
        lease=harness.lease,
        result=other_result,
        submitter_session=harness.submitter_session,
        reviewer_session=harness.reviewer_session,
        suffix="other-result",
        issued_at=other_assignment_issued_at,
    )
    other_reviews = _reviews_for_result(
        harness.work,
        other_result,
        reviewer_assignment_sha256=other_reviewer_assignment_sha256,
        reviewer_agent_session_id=harness.reviewer_session.session_id,
        reviewed_at_utc="2026-08-11T01:22:00Z",
        id_prefix="review-receipt:other",
        evidence_prefix="review:other",
    )
    harness.source_registry.register(other_result)
    harness.clock.value = datetime(2026, 8, 11, 1, 23, tzinfo=timezone.utc)
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_replay_conflict$",
    ):
        harness.issue(
            result=other_result,
            reviews=other_reviews,
            acceptance_receipt_id=first.acceptance_receipt_id,
        )


def test_post_issue_tampering_is_rejected_by_issuing_authority() -> None:
    harness = _harness()
    receipt = harness.issue()
    tampered = replace(receipt, evidence_sha256="sha256:" + "0" * 64)

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_evidence_digest_mismatch$",
    ):
        harness.authority.verify_issued(tampered)


def test_portable_receipt_codecs_are_exact_canonical_and_digest_bound() -> None:
    harness = _harness()
    receipt = harness.issue()
    review_json = json.dumps(
        harness.review.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    acceptance_json = json.dumps(
        receipt.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert ReviewReceipt.from_dict(harness.review.to_dict()) == harness.review
    assert (
        ReviewReceipt.from_canonical_json(
            review_json,
            expected_sha256=harness.review.content_sha256,
        )
        == harness.review
    )
    assert AcceptanceReceipt.from_dict(receipt.to_dict()) == receipt
    assert (
        AcceptanceReceipt.from_canonical_json(
            acceptance_json,
            expected_sha256=receipt.content_sha256,
        )
        == receipt
    )
    assert harness.review.to_dict()["schema_version"] == REVIEW_RECEIPT_SCHEMA
    assert harness.review.to_dict()["issuer"] == REVIEW_RECEIPT_ISSUER


@pytest.mark.parametrize(
    ("kind", "mutation", "error"),
    [
        (
            "review",
            lambda value: {**value, "unexpected": "field"},
            "review_receipt_projection_invalid",
        ),
        (
            "review",
            lambda value: {**value, "schema_version": "collaboration-review/v0"},
            "review_receipt_schema_invalid",
        ),
        (
            "review",
            lambda value: {**value, "issuer": "caller"},
            "review_receipt_issuer_invalid",
        ),
        (
            "acceptance",
            lambda value: {**value, "unexpected": "field"},
            "acceptance_receipt_projection_invalid",
        ),
        (
            "acceptance",
            lambda value: {**value, "issuer": "caller"},
            "acceptance_receipt_issuer_invalid",
        ),
        (
            "acceptance",
            lambda value: {
                **value,
                "digests": {**value["digests"], "work": "sha256:" + "0" * 64},
            },
            "acceptance_receipt_digest_projection_mismatch",
        ),
    ],
)
def test_portable_receipt_codecs_reject_shape_schema_issuer_and_digest_drift(
    kind: str,
    mutation,
    error: str,
) -> None:
    harness = _harness()
    receipt = harness.issue()
    value = harness.review.to_dict() if kind == "review" else receipt.to_dict()
    decoder = ReviewReceipt.from_dict if kind == "review" else AcceptanceReceipt.from_dict

    with pytest.raises(AcceptanceReceiptError, match=f"^{error}$"):
        decoder(mutation(value))


def test_canonical_json_codecs_reject_noncanonical_or_wrong_external_digest() -> None:
    harness = _harness()
    receipt = harness.issue()
    pretty = json.dumps(receipt.to_dict(), indent=2, sort_keys=True)
    canonical = json.dumps(
        receipt.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    with pytest.raises(AcceptanceReceiptError, match="^acceptance_receipt_json_invalid$"):
        AcceptanceReceipt.from_canonical_json(pretty)
    with pytest.raises(AcceptanceReceiptError, match="^acceptance_receipt_digest_mismatch$"):
        AcceptanceReceipt.from_canonical_json(
            canonical,
            expected_sha256="sha256:" + "0" * 64,
        )
