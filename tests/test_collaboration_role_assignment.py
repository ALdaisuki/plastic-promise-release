from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

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
from plastic_promise.collaboration.role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    InMemoryRoleAssignmentRepository,
    RoleAssignmentBasis,
    RoleAssignmentBindingState,
    RoleAssignmentError,
    RoleAssignmentReceipt,
    VerifiedRoleAssignment,
    open_server_role_assignment_authority,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


NOW = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:role-assignment")


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(agent_id: str) -> AgentIdentity:
    # The stable identity role is deliberately unrelated to the work role.
    return AgentIdentity(agent_id=agent_id, role="participant")


def _session(agent_id: str, suffix: str = "1") -> AgentSession:
    identity = _identity(agent_id)
    return AgentSession(
        session_id=f"agent-session:{agent_id.removeprefix('agent:')}:{suffix}",
        identity=identity,
        project=PROJECT,
        coordination_session_id="coord:role-assignment",
        state="active",
        started_at=_text(NOW - timedelta(minutes=5)),
        last_heartbeat_at=_text(NOW - timedelta(seconds=5)),
        expires_at=_text(NOW + timedelta(minutes=30)),
    )


def _work(*, work_item_id: str, assigned: AgentIdentity, generation: int = 1) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=f"work-receipt:{work_item_id.removeprefix('work:')}",
        work_item_id=work_item_id,
        project=PROJECT,
        coordination_session_id="coord:role-assignment",
        assigned_agent=assigned,
        objective="Exercise one exact dynamic work-role scope",
        fencing_generation=generation,
        issued_at=_text(NOW - timedelta(minutes=2)),
        expires_at=_text(NOW + timedelta(minutes=20)),
    )


def _lease(work: WorkReceipt, *, owner: AgentIdentity) -> WorkLease:
    work_item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:role-assignment",
        created_at=_text(NOW - timedelta(minutes=3)),
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    return WorkLease(
        lease_id=f"lease:{work.work_item_id.removeprefix('work:')}",
        work_item=work_item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=owner.agent_id,
        owner_identity=owner,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at=_text(NOW - timedelta(minutes=1)),
        expires_at=_text(NOW + timedelta(minutes=10)),
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
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=f"event:intent:{session.session_id.removeprefix('agent-session:')}:{work.work_item_id.removeprefix('work:')}:{use}",
        project=work.project,
        coordination_session_id=work.coordination_session_id,
        actor=session.identity,
        event_type="agent.intent_declared",
        summary=f"Intent to enter {use}",
        created_at=_text(NOW - timedelta(seconds=30)),
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _submitter_basis(
    *,
    session: AgentSession,
    work_item_id: str,
) -> RoleAssignmentBasis:
    work = _work(work_item_id=work_item_id, assigned=session.identity)
    lease = _lease(work, owner=session.identity)
    return RoleAssignmentBasis(
        session=session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=session,
            work=work,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )


def _reviewer_basis(
    *,
    reviewer_session: AgentSession,
    submitter_session: AgentSession,
    work_item_id: str,
) -> RoleAssignmentBasis:
    work = _work(work_item_id=work_item_id, assigned=submitter_session.identity)
    lease = _lease(work, owner=submitter_session.identity)
    result = ResultReceipt.for_work(
        work,
        receipt_id=f"result:{work_item_id.removeprefix('work:')}",
        submitted_by=submitter_session.identity,
        outcome="completed",
        summary="Completed by the independently assigned submitter",
        submitted_at=_text(NOW - timedelta(seconds=20)),
        evidence_refs=("evidence:role-assignment",),
    )
    return RoleAssignmentBasis(
        session=reviewer_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=reviewer_session,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
        ),
        workflow_stage="code-review",
        work_state="submitted",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter_session.session_id,
    )


def _register(
    repository: InMemoryRoleAssignmentRepository,
    use: str,
    basis: RoleAssignmentBasis,
) -> None:
    repository.register_basis(use=use, basis=basis)


def _issue(authority, use: str, basis: RoleAssignmentBasis):
    return authority.issue(
        use=use,
        agent_session_id=basis.session.session_id,
        work_item_id=basis.work.work_item_id,
        lease_id=basis.lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
    )


def test_one_stable_agent_identity_can_hold_different_roles_on_different_work() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    shared_agent = _session("agent:codex")
    submitter = _submitter_basis(session=shared_agent, work_item_id="work:a")
    other_submitter = _session("agent:peer")
    reviewer = _reviewer_basis(
        reviewer_session=shared_agent,
        submitter_session=other_submitter,
        work_item_id="work:b",
    )
    _register(repository, RESULT_SUBMISSION_USE, submitter)
    _register(repository, ACCEPTANCE_REVIEW_USE, reviewer)

    submitter_assignment = _issue(authority, RESULT_SUBMISSION_USE, submitter)
    reviewer_assignment = _issue(authority, ACCEPTANCE_REVIEW_USE, reviewer)

    assert submitter_assignment.agent_id == reviewer_assignment.agent_id == "agent:codex"
    assert submitter_assignment.agent_session_sha256 == reviewer_assignment.agent_session_sha256
    assert submitter_assignment.assignment_role == WORK_SUBMITTER_ROLE
    assert reviewer_assignment.assignment_role == WORK_REVIEWER_ROLE
    assert submitter_assignment.work_item_id == "work:a"
    assert reviewer_assignment.work_item_id == "work:b"
    assert submitter_assignment.to_dict()["tool_policy_effect"] == "none"
    assert reviewer_assignment.to_dict()["authority_effect"] == "none"


def test_role_intent_and_actor_name_never_create_authority_by_themselves() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(session=_session("agent:pi_reviewer"), work_item_id="work:intent")

    with pytest.raises(RoleAssignmentError, match="^role_assignment_basis_not_found$"):
        _issue(authority, RESULT_SUBMISSION_USE, basis)

    forged = RoleAssignmentReceipt(
        assignment_id="role-assignment:forged",
        project=basis.work.project,
        coordination_session_id=basis.work.coordination_session_id,
        work_item_id=basis.work.work_item_id,
        work_receipt_sha256=basis.work.content_sha256,
        lease_id=basis.lease.lease_id,
        lease_sha256=basis.lease.content_sha256,
        fencing_generation=basis.lease.fencing_generation,
        agent_session_id=basis.session.session_id,
        agent_session_sha256=basis.session.content_sha256,
        agent_id=basis.session.identity.agent_id,
        assignment_role=WORK_SUBMITTER_ROLE,
        use=RESULT_SUBMISSION_USE,
        workflow_stage="implement",
        intent_event_id=basis.intent_event.event_id,
        intent_event_sha256=basis.intent_event.content_sha256,
        result_receipt_sha256="",
        assignment_policy_revision="work-role-assignment-policy/v1",
        issued_at_utc=_text(NOW),
        expires_at_utc=_text(NOW + timedelta(minutes=5)),
        idempotency_sha256="sha256:" + "9" * 64,
    )
    with pytest.raises(RoleAssignmentError, match="^role_assignment_not_server_issued$"):
        authority.verify_for_use(
            forged.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(NOW + timedelta(seconds=1)),
        )


def test_public_identity_roles_do_not_participate_in_submitter_authorization() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(
        session=_session("agent:reviewer-named-submit-agent"),
        work_item_id="work:stable-agent-id",
    )
    assigned = AgentIdentity(
        agent_id=basis.session.identity.agent_id,
        role="reviewer",
        capabilities=("public.work.claim",),
    )
    work = replace(basis.work, assigned_agent=assigned)
    lease_identity = AgentIdentity(
        agent_id=basis.session.identity.agent_id,
        role="observer",
        capabilities=("public.lease.observe",),
    )
    lease = replace(
        basis.lease,
        owner_identity=lease_identity,
        result_binding_sha256=work.content_sha256,
    )
    event_actor = AgentIdentity(
        agent_id=basis.session.identity.agent_id,
        role="coordinator",
    )
    intent = replace(basis.intent_event, actor=event_actor)
    basis = replace(basis, work=work, lease=lease, intent_event=intent)
    _register(repository, RESULT_SUBMISSION_USE, basis)

    assignment = _issue(authority, RESULT_SUBMISSION_USE, basis)

    assert assignment.agent_id == "agent:reviewer-named-submit-agent"
    assert assignment.assignment_role == WORK_SUBMITTER_ROLE


def test_same_agent_cannot_review_own_result_even_with_a_fresh_session() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    submitter = _session("agent:codex", "submit")
    reviewer = _session("agent:codex", "review")
    basis = _reviewer_basis(
        reviewer_session=reviewer,
        submitter_session=submitter,
        work_item_id="work:self-review",
    )
    _register(repository, ACCEPTANCE_REVIEW_USE, basis)

    with pytest.raises(RoleAssignmentError, match="^role_assignment_self_review_forbidden$"):
        _issue(authority, ACCEPTANCE_REVIEW_USE, basis)


def test_public_identity_roles_do_not_participate_in_reviewer_authorization() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    reviewer_session = _session("agent:independent-reviewer")
    submitter_session = _session("agent:submitter")
    basis = _reviewer_basis(
        reviewer_session=reviewer_session,
        submitter_session=submitter_session,
        work_item_id="work:stable-review-agent-id",
    )
    assigned = AgentIdentity(
        agent_id=submitter_session.identity.agent_id,
        role="reviewer",
    )
    work = replace(basis.work, assigned_agent=assigned)
    result_submitter = AgentIdentity(
        agent_id=submitter_session.identity.agent_id,
        role="observer",
    )
    result = replace(
        basis.result,
        submitted_by=result_submitter,
        work_receipt_sha256=work.content_sha256,
    )
    lease = replace(basis.lease, result_binding_sha256=work.content_sha256)
    basis = replace(basis, work=work, lease=lease, result=result)
    _register(repository, ACCEPTANCE_REVIEW_USE, basis)

    assignment = _issue(authority, ACCEPTANCE_REVIEW_USE, basis)

    assert assignment.agent_id == reviewer_session.identity.agent_id
    assert assignment.assignment_role == WORK_REVIEWER_ROLE


def test_assignment_rejects_role_intent_mismatch_and_stale_fence() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(session=_session("agent:codex"), work_item_id="work:fence")
    wrong_intent = replace(
        basis.intent_event,
        payload={
            "requested_use": RESULT_SUBMISSION_USE,
            "requested_role": WORK_REVIEWER_ROLE,
            "workflow_stage": "implement",
            "authority_effect": "none",
        },
    )
    mismatched = replace(basis, intent_event=wrong_intent)
    _register(repository, RESULT_SUBMISSION_USE, mismatched)

    with pytest.raises(RoleAssignmentError, match="^role_assignment_intent_role_mismatch$"):
        _issue(authority, RESULT_SUBMISSION_USE, mismatched)

    stale = replace(
        basis,
        intent_event=replace(
            basis.intent_event,
            event_id="event:intent:stale-fence",
        ),
        lease=replace(basis.lease, fencing_generation=2),
    )
    _register(repository, RESULT_SUBMISSION_USE, stale)
    with pytest.raises(RoleAssignmentError, match="^role_assignment_fencing_stale$"):
        _issue(authority, RESULT_SUBMISSION_USE, stale)


@pytest.mark.parametrize(
    ("mutate_intent", "error_code"),
    (
        (
            lambda event: replace(
                event,
                created_at=_text(NOW + timedelta(seconds=1)),
            ),
            "role_assignment_intent_not_observed",
        ),
        (
            lambda event: replace(event, expires_at=_text(NOW)),
            "role_assignment_intent_expired",
        ),
    ),
)
def test_assignment_requires_a_current_observed_intent_event(
    mutate_intent,
    error_code: str,
) -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id=f"work:{error_code.removeprefix('role_assignment_')}",
    )
    basis = replace(basis, intent_event=mutate_intent(basis.intent_event))
    _register(repository, RESULT_SUBMISSION_USE, basis)

    with pytest.raises(RoleAssignmentError, match=f"^{error_code}$"):
        _issue(authority, RESULT_SUBMISSION_USE, basis)


def test_verify_for_use_is_authority_bound_and_time_bounded() -> None:
    repository = InMemoryRoleAssignmentRepository()
    clock = MutableClock(NOW)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis(session=_session("agent:codex"), work_item_id="work:verify")
    _register(repository, RESULT_SUBMISSION_USE, basis)
    assignment = _issue(authority, RESULT_SUBMISSION_USE, basis)

    verified = authority.verify_for_use(
        assignment.assignment_sha256,
        use=RESULT_SUBMISSION_USE,
        used_at=_text(NOW),
    )
    assert isinstance(verified, VerifiedRoleAssignment)
    assert verified.assignment_role == WORK_SUBMITTER_ROLE
    assert verified.work_receipt_sha256 == basis.work.content_sha256

    with pytest.raises(RoleAssignmentError, match="^role_assignment_verified_authority_required$"):
        VerifiedRoleAssignment(assignment)
    with pytest.raises(TypeError, match="verified_role_assignment_not_serializable"):
        verified.__reduce__()
    with pytest.raises(RoleAssignmentError, match="^role_assignment_use_mismatch$"):
        authority.verify_for_use(
            assignment.assignment_sha256,
            use=ACCEPTANCE_REVIEW_USE,
            used_at=_text(NOW),
        )
    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_use_time_in_future$",
    ):
        authority.verify_for_use(
            assignment.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(NOW + timedelta(seconds=1)),
        )
    clock.value = NOW + timedelta(minutes=5)
    with pytest.raises(RoleAssignmentError, match="^role_assignment_expired$"):
        authority.verify_for_use(
            assignment.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=assignment.expires_at_utc,
        )


def test_verify_for_use_rejects_backdating_after_server_expiry() -> None:
    repository = InMemoryRoleAssignmentRepository()
    clock = MutableClock(NOW)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:no-backdating",
    )
    _register(repository, RESULT_SUBMISSION_USE, basis)
    assignment = _issue(authority, RESULT_SUBMISSION_USE, basis)
    clock.value = NOW + timedelta(minutes=6)

    with pytest.raises(RoleAssignmentError, match="^role_assignment_expired$"):
        authority.verify_for_use(
            assignment.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(NOW + timedelta(seconds=1)),
        )


def test_binding_revocation_is_generation_bound_idempotent_and_fail_closed() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:revocation",
    )
    _register(repository, RESULT_SUBMISSION_USE, basis)
    assignment = _issue(authority, RESULT_SUBMISSION_USE, basis)

    assert assignment.binding_generation == 1

    revoked = authority.revoke(
        assignment.assignment_sha256,
        expected_generation=assignment.binding_generation,
    )

    assert revoked.assignment_sha256 == assignment.assignment_sha256
    assert revoked.binding_generation == assignment.binding_generation
    assert revoked.revoked is True
    assert (
        authority.revoke(
            assignment.assignment_sha256,
            expected_generation=assignment.binding_generation,
        )
        == revoked
    )
    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_binding_generation_mismatch$",
    ):
        authority.revoke(
            assignment.assignment_sha256,
            expected_generation=assignment.binding_generation + 1,
        )
    with pytest.raises(RoleAssignmentError, match="^role_assignment_binding_revoked$"):
        authority.verify_for_use(
            assignment.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(NOW + timedelta(seconds=1)),
        )
    with pytest.raises(RoleAssignmentError, match="^role_assignment_binding_revoked$"):
        _issue(authority, RESULT_SUBMISSION_USE, basis)


def test_duplicate_issue_returns_the_exact_existing_binding_without_refreshing_it() -> None:
    repository = InMemoryRoleAssignmentRepository()
    clock = MutableClock(NOW)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:idempotent-issue",
    )
    _register(repository, RESULT_SUBMISSION_USE, basis)
    first = _issue(authority, RESULT_SUBMISSION_USE, basis)
    clock.value = NOW + timedelta(seconds=1)

    replay = _issue(authority, RESULT_SUBMISSION_USE, basis)

    assert replay is first
    assert replay.assignment_sha256 == first.assignment_sha256
    assert replay.issued_at_utc == _text(NOW)
    assert replay.binding_generation == first.binding_generation == 1


def test_concurrent_duplicate_issue_returns_the_canonical_winner() -> None:
    class RacingRepository(InMemoryRoleAssignmentRepository):
        def load_by_idempotency(self, idempotency_sha256: str):
            del idempotency_sha256
            return None

    repository = RacingRepository()
    clock = MutableClock(NOW)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:concurrent-idempotency",
    )
    _register(repository, RESULT_SUBMISSION_USE, basis)
    winner = _issue(authority, RESULT_SUBMISSION_USE, basis)
    clock.value = NOW + timedelta(seconds=1)

    replay = _issue(authority, RESULT_SUBMISSION_USE, basis)

    assert replay is winner
    assert replay.assignment_sha256 == winner.assignment_sha256


def test_verify_rejects_a_repository_row_from_the_wrong_digest_key() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    requested_basis = _submitter_basis(
        session=_session("agent:codex", "requested"),
        work_item_id="work:requested-digest",
    )
    wrong_basis = _submitter_basis(
        session=_session("agent:peer", "wrong"),
        work_item_id="work:wrong-digest",
    )
    _register(repository, RESULT_SUBMISSION_USE, requested_basis)
    _register(repository, RESULT_SUBMISSION_USE, wrong_basis)
    requested = _issue(authority, RESULT_SUBMISSION_USE, requested_basis)
    wrong = _issue(authority, RESULT_SUBMISSION_USE, wrong_basis)

    class MisindexedRepository:
        def register_basis(self, **kwargs):
            return repository.register_basis(**kwargs)

        def resolve_issue_basis(self, **kwargs):
            return repository.resolve_issue_basis(**kwargs)

        def append_exact(self, receipt, **kwargs):
            return repository.append_exact(receipt, **kwargs)

        def load_by_digest(self, assignment_sha256):
            del assignment_sha256
            return wrong

        def load_by_idempotency(self, idempotency_sha256):
            return repository.load_by_idempotency(idempotency_sha256)

        def load_binding_state(self, assignment_sha256):
            del assignment_sha256
            return repository.load_binding_state(wrong.assignment_sha256)

        def revoke_exact(self, assignment_sha256, **kwargs):
            return repository.revoke_exact(assignment_sha256, **kwargs)

    misindexed = open_server_role_assignment_authority(
        repository=MisindexedRepository(),
        clock=MutableClock(NOW),
    )

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_repository_lookup_mismatch$",
    ):
        misindexed.verify_for_use(
            requested.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(NOW),
        )


def test_issue_rejects_a_repository_row_from_the_wrong_idempotency_key() -> None:
    canonical_repository = InMemoryRoleAssignmentRepository()
    canonical_authority = open_server_role_assignment_authority(
        repository=canonical_repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:wrong-idempotency-key",
    )
    _register(canonical_repository, RESULT_SUBMISSION_USE, basis)
    canonical = _issue(canonical_authority, RESULT_SUBMISSION_USE, basis)
    canonical_state = canonical_repository.load_binding_state(canonical.assignment_sha256)
    assert canonical_state is not None
    foreign = replace(
        canonical,
        idempotency_sha256="sha256:" + "3" * 64,
    )
    foreign_state = RoleAssignmentBindingState(
        assignment_sha256=foreign.assignment_sha256,
        server_basis_sha256=canonical_state.server_basis_sha256,
        binding_generation=foreign.binding_generation,
    )

    class MisindexedRepository:
        def register_basis(self, **kwargs):
            del kwargs

        def resolve_issue_basis(self, **kwargs):
            del kwargs
            return basis

        def append_exact(self, receipt, **kwargs):
            del receipt, kwargs
            raise AssertionError("not reached")

        def load_by_digest(self, assignment_sha256):
            del assignment_sha256
            return None

        def load_by_idempotency(self, idempotency_sha256):
            del idempotency_sha256
            return foreign

        def load_binding_state(self, assignment_sha256):
            assert assignment_sha256 == foreign.assignment_sha256
            return foreign_state

        def revoke_exact(self, assignment_sha256, **kwargs):
            del assignment_sha256, kwargs
            raise AssertionError("not reached")

    authority = open_server_role_assignment_authority(
        repository=MisindexedRepository(),
        clock=MutableClock(NOW),
    )

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_repository_lookup_mismatch$",
    ):
        _issue(authority, RESULT_SUBMISSION_USE, basis)


def test_repository_binding_writes_are_server_only() -> None:
    repository = InMemoryRoleAssignmentRepository()
    authority = open_server_role_assignment_authority(
        repository=repository,
        clock=MutableClock(NOW),
    )
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:server-write-boundary",
    )
    _register(repository, RESULT_SUBMISSION_USE, basis)
    assignment = _issue(authority, RESULT_SUBMISSION_USE, basis)

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_repository_write_authority_required$",
    ):
        repository.append_exact(assignment)
    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_repository_write_authority_required$",
    ):
        repository.revoke_exact(
            assignment.assignment_sha256,
            expected_generation=assignment.binding_generation,
            revoked_at_utc=_text(NOW),
        )


def test_repository_contract_requires_durable_binding_state_and_revocation_seams() -> None:
    class LegacyRepository:
        def resolve_issue_basis(self, **kwargs):
            del kwargs
            raise AssertionError("not reached")

        def append_exact(self, receipt):
            return receipt

        def load_by_digest(self, assignment_sha256):
            del assignment_sha256
            return None

    with pytest.raises(RoleAssignmentError, match="^role_assignment_repository_invalid$"):
        open_server_role_assignment_authority(
            repository=LegacyRepository(),
            clock=MutableClock(NOW),
        )


def test_repository_missing_current_binding_state_fails_closed() -> None:
    basis = _submitter_basis(
        session=_session("agent:codex"),
        work_item_id="work:missing-binding-state",
    )

    class MissingBindingStateRepository:
        def register_basis(self, **kwargs):
            del kwargs

        def resolve_issue_basis(self, **kwargs):
            del kwargs
            return basis

        def append_exact(self, receipt, **kwargs):
            del kwargs
            return receipt

        def load_by_digest(self, assignment_sha256):
            del assignment_sha256
            return None

        def load_by_idempotency(self, idempotency_sha256):
            del idempotency_sha256
            return None

        def load_binding_state(self, assignment_sha256):
            del assignment_sha256
            return None

        def revoke_exact(self, assignment_sha256, **kwargs):
            del assignment_sha256, kwargs
            raise AssertionError("not reached")

    authority = open_server_role_assignment_authority(
        repository=MissingBindingStateRepository(),
        clock=MutableClock(NOW),
    )

    with pytest.raises(RoleAssignmentError, match="^role_assignment_binding_state_missing$"):
        _issue(authority, RESULT_SUBMISSION_USE, basis)
