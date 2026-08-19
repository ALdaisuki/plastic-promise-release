from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.acceptance_receipt import (
    REVIEW_CHANNELS,
    AcceptanceReceipt,
    AcceptanceReceiptError,
    ReviewReceipt,
    ServerAcceptanceSourceRegistry,
    open_server_acceptance_receipt_authority,
    open_server_acceptance_source_registry,
)
from plastic_promise.collaboration.awareness import (
    AgentAwarenessProjection,
    ProjectWorkingSet,
)
from plastic_promise.collaboration.context_projection import (
    COLLABORATION_CONTEXT_SCHEMA,
    COLLABORATION_PROJECTION_FACTORY_REVISION,
    COLLABORATION_SOURCE_AUTHORITY,
    COLLABORATION_SOURCE_KIND,
    AuthenticatedCollaborationFeed,
    CollaborationContextAuthority,
    CollaborationContextBudget,
    CollaborationContextProjectionError,
    CollaborationPolicyClaim,
    CollaborationSourceTuple,
    ServerCollaborationContextBinding,
    SourcePageLineageClaim,
    VerifiedCausalDistanceClaim,
    compose_context_projection,
    open_server_collaboration_context_authority,
)
from plastic_promise.collaboration.contracts import (
    COLLABORATION_EVENT_SCHEMA,
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    CoordinationSession,
    EventCursor,
    EventPage,
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

EARLIER = "2026-08-11T00:00:00Z"
NOW = "2026-08-11T01:00:00Z"
LATER = "2026-08-11T02:00:00Z"
TEST_POLICY_DIGEST = "sha256:" + "1" * 64
TEST_POLICY_BINDING_DIGEST = "sha256:" + "2" * 64
TEST_SOURCE_RECEIPT_DIGEST = "sha256:" + "3" * 64
TEST_SOURCE_ANCHOR_DIGEST = "sha256:" + "4" * 64
TEST_EVENT_LOG_REVISION = "event-log:pr4-test"
TEST_ACCEPTANCE_POLICY_REVISION = "acceptance-review-policy/v1"
TEST_SOURCE_REVISION = "e" * 40


def _acceptance_registry(
    work: WorkReceipt,
    result: ResultReceipt,
    *sessions: AgentSession,
) -> ServerAcceptanceSourceRegistry:
    registry = open_server_acceptance_source_registry()
    registry.register(work, result, *sessions)
    return registry


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class AuthorityState:
    policy_valid: bool = True
    source_valid: bool = True
    projection_valid: bool = True
    current_source_head_sequence: int = 0
    authorized_source_lineages: set[str] = field(default_factory=set)
    authorized_awareness_digests: set[str] = field(default_factory=set)
    accepted_digests: set[str] = field(default_factory=set)


@dataclass
class AuthorityHarness:
    authority: CollaborationContextAuthority
    clock: MutableClock
    state: AuthorityState

    def bind(
        self,
        *,
        memory: dict[str, object],
        awareness: AgentAwarenessProjection,
        event_page: EventPage,
        project: ProjectScope,
        audience_session: AgentSession,
        read_after_cursor: EventCursor,
        acceptance_receipts: tuple[AcceptanceReceipt, ...],
        source_head_cursor: EventCursor | None = None,
        causal_distances: tuple[VerifiedCausalDistanceClaim, ...] = (),
        source_kind: str = COLLABORATION_SOURCE_KIND,
        source_authority: str = COLLABORATION_SOURCE_AUTHORITY,
        event_schema_revision: str = COLLABORATION_EVENT_SCHEMA,
        event_log_revision: str = TEST_EVENT_LOG_REVISION,
        projection_factory_revision: str = COLLABORATION_PROJECTION_FACTORY_REVISION,
        generated_at_utc: str | None = None,
        source_audience_session: AgentSession | None = None,
        source_policy_revision: str | None = None,
        ttl_seconds: int = 60,
    ) -> AuthenticatedCollaborationFeed:
        head = source_head_cursor or event_page.next_cursor
        source_session = source_audience_session or audience_session
        self.state.current_source_head_sequence = max(
            self.state.current_source_head_sequence,
            head.sequence,
        )
        policy_claim = CollaborationPolicyClaim(
            binding_id="policy-binding:pr4-test",
            binding_sha256=TEST_POLICY_BINDING_DIGEST,
            policy_revision="policy:collaboration-context:v1",
            policy_digest=TEST_POLICY_DIGEST,
            audience_session_sha256=audience_session.content_sha256,
            expires_at=LATER,
        )
        source_lineage = SourcePageLineageClaim(
            source_receipt_id="source-receipt:pr4-test",
            source_receipt_sha256=TEST_SOURCE_RECEIPT_DIGEST,
            source_anchor_sha256=TEST_SOURCE_ANCHOR_DIGEST,
            source_tuple=CollaborationSourceTuple(
                source_kind=source_kind,
                source_authority=source_authority,
                project=project,
                coordination_session_id="coord:pr4",
                audience_agent_id=source_session.identity.agent_id,
                audience_role=source_session.identity.role,
                audience_session_id=source_session.session_id,
                audience_session_sha256=source_session.content_sha256,
                agent_session_policy_revision=(
                    source_policy_revision or policy_claim.policy_revision
                ),
                event_schema_revision=event_schema_revision,
                event_log_revision=event_log_revision,
                cursor_from=read_after_cursor,
                cursor_to=event_page.next_cursor,
                source_page_digest=event_page.content_sha256,
                projection_factory_revision=projection_factory_revision,
                generated_at_utc=generated_at_utc or _clock_text(self.clock()),
            ),
            source_head_cursor=head,
            event_page_sha256=event_page.content_sha256,
            awareness_sha256=awareness.content_sha256,
            working_set_sha256=awareness.working_set_sha256,
            visible_event_count=len(event_page.events),
            causal_distances=causal_distances,
        )
        self.state.authorized_source_lineages.add(source_lineage.content_sha256)
        self.state.authorized_awareness_digests.add(awareness.content_sha256)
        return self.authority.bind_sources(
            memory_context=memory,
            awareness=awareness,
            event_page=event_page,
            authenticated_project=project,
            authenticated_session_id="coord:pr4",
            authenticated_audience_session=audience_session,
            read_after_cursor=read_after_cursor,
            policy_claim=policy_claim,
            source_lineage=source_lineage,
            acceptance_receipts=acceptance_receipts,
            ttl_seconds=ttl_seconds,
        )


def _authority_harness(
    *,
    session_freshness_seconds: int = 300,
    snapshot_freshness_seconds: int = 300,
) -> AuthorityHarness:
    clock = MutableClock(datetime.fromisoformat(NOW.replace("Z", "+00:00")))
    state = AuthorityState()
    authority = open_server_collaboration_context_authority(
        clock=clock,
        policy_verifier=lambda _session, claim: (
            state.policy_valid
            and claim.policy_revision == "policy:collaboration-context:v1"
            and claim.policy_digest == TEST_POLICY_DIGEST
            and claim.binding_sha256 == TEST_POLICY_BINDING_DIGEST
        ),
        source_lineage_verifier=lambda claim: (
            state.source_valid
            and claim.source_receipt_sha256 == TEST_SOURCE_RECEIPT_DIGEST
            and claim.source_anchor_sha256 == TEST_SOURCE_ANCHOR_DIGEST
            and claim.source_tuple.event_log_revision == TEST_EVENT_LOG_REVISION
            and claim.source_head_cursor.sequence <= state.current_source_head_sequence
            and claim.content_sha256 in state.authorized_source_lineages
        ),
        projection_verifier=lambda awareness, claim: (
            state.projection_valid
            and awareness.content_sha256 in state.authorized_awareness_digests
            and claim.awareness_sha256 == awareness.content_sha256
            and claim.working_set_sha256 == awareness.working_set_sha256
        ),
        acceptance_verifier=lambda receipt: receipt.content_sha256 in state.accepted_digests,
        session_freshness_seconds=session_freshness_seconds,
        snapshot_freshness_seconds=snapshot_freshness_seconds,
    )
    return AuthorityHarness(authority=authority, clock=clock, state=state)


def _agent(agent_id: str, role: str) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, role=role, capabilities=("code.read",))


def _work(project: ProjectScope, agent: AgentIdentity, work_id: str) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=f"receipt:{work_id}",
        work_item_id=work_id,
        project=project,
        coordination_session_id="coord:pr4",
        assigned_agent=agent,
        objective=f"Private objective for {work_id}",
        fencing_generation=1,
        issued_at=EARLIER,
        expires_at=LATER,
    )


def _presence(
    project: ProjectScope,
    agent: AgentIdentity,
    *,
    state: str = "active",
    heartbeat_at: str = NOW,
    expires_at: str | None = LATER,
) -> AgentSession:
    return AgentSession(
        session_id=f"session:{agent.agent_id}",
        identity=agent,
        project=project,
        coordination_session_id="coord:pr4",
        state=state,
        started_at=EARLIER,
        last_heartbeat_at=heartbeat_at,
        expires_at=expires_at,
    )


def _role_lease(work: WorkReceipt) -> WorkLease:
    item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:context-projection",
        created_at=EARLIER,
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    return WorkLease(
        lease_id=f"lease:{work.work_item_id}",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=work.assigned_agent.agent_id,
        owner_identity=work.assigned_agent,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at=EARLIER,
        expires_at=LATER,
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )


def _role_intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
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
        created_at=EARLIER,
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _issue_acceptance(
    work: WorkReceipt,
    result: ResultReceipt,
    *,
    submitter_session: AgentSession,
    reviewer_session: AgentSession,
    acceptance_receipt_id: str,
    verified_digest_sink: set[str],
) -> tuple[AcceptanceReceipt, ResultReceipt]:
    clock_value = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    role_repository = InMemoryRoleAssignmentRepository()
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=lambda: clock_value,
    )
    lease = _role_lease(work)
    submitter_basis = RoleAssignmentBasis(
        session=submitter_session,
        work=work,
        lease=lease,
        intent_event=_role_intent(
            session=submitter_session,
            work=work,
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
    )
    bound_result = replace(
        result,
        role_assignment_sha256=submitter_assignment.assignment_sha256,
    )
    reviewer_basis = RoleAssignmentBasis(
        session=reviewer_session,
        work=work,
        lease=lease,
        intent_event=_role_intent(
            session=reviewer_session,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
            suffix="reviewer",
        ),
        workflow_stage="code-review",
        work_state="submitted",
        lease_state="completed",
        result=bound_result,
        submitter_agent_session_id=submitter_session.session_id,
    )
    role_repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=reviewer_basis)
    reviewer_assignment = role_authority.issue(
        use=ACCEPTANCE_REVIEW_USE,
        agent_session_id=reviewer_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=reviewer_basis.intent_event.event_id,
    )
    policy_authority = open_server_agent_policy_binding_authority(clock=lambda: clock_value)
    binding = policy_authority.issue(
        reviewer_session,
        policy_revision=TEST_ACCEPTANCE_POLICY_REVISION,
    )
    reviews = tuple(
        ReviewReceipt.for_result(
            work,
            bound_result,
            review_receipt_id=f"review:{acceptance_receipt_id}:{channel}",
            reviewer_assignment_sha256=reviewer_assignment.assignment_sha256,
            reviewer_agent_session_id=reviewer_session.session_id,
            review_policy_revision=TEST_ACCEPTANCE_POLICY_REVISION,
            source_revision=TEST_SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=NOW,
            evidence_refs=(f"evidence:independent-review:{channel}",),
            review_channel=channel,
        )
        for channel in REVIEW_CHANNELS
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        current_review_policy_revision=TEST_ACCEPTANCE_POLICY_REVISION,
        current_source_revision=TEST_SOURCE_REVISION,
        source_registry=_acceptance_registry(
            work,
            bound_result,
            submitter_session,
            reviewer_session,
        ),
        clock=lambda: clock_value,
    )
    receipt = authority.issue(
        work,
        bound_result,
        reviews,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
        reviewer_policy_binding=binding,
        acceptance_receipt_id=acceptance_receipt_id,
    )
    verified = authority.verify_for_consumption(receipt)
    verified_digest_sink.add(verified.content_sha256)
    return receipt, bound_result


def _event(
    project: ProjectScope,
    actor: AgentIdentity,
    event_id: str,
    event_type: str,
    *,
    work_item_id: str | None = None,
    summary: str | None = None,
    created_at: str = NOW,
    causal_parent_event_id: str | None = None,
    subject_refs: tuple[str, ...] = (),
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=event_id,
        project=project,
        coordination_session_id="coord:pr4",
        actor=actor,
        event_type=event_type,
        summary=summary or f"Summary for {event_id}",
        created_at=created_at,
        causal_parent_event_id=causal_parent_event_id,
        work_item_id=work_item_id,
        subject_refs=subject_refs,
        payload={"private_adapter_payload": "redacted upstream"},
    )


def _memory(project_id: str = "project:plastic-promise") -> dict[str, object]:
    return {
        "core": [{"id": "memory:core", "content": "canonical decision"}],
        "related": [{"id": "memory:related", "content": "related history"}],
        "divergent": [{"id": "memory:divergent", "content": "possible analogy"}],
        "activated_principles": ["context first"],
        "project_id": project_id,
        "trace": {"call_id": "call:memory"},
    }


def _tamper_feed(
    feed: AuthenticatedCollaborationFeed,
    **changes: object,
) -> AuthenticatedCollaborationFeed:
    """Bypass the closed factory only to exercise compose-time drift checks."""

    forged = object.__new__(AuthenticatedCollaborationFeed)
    for descriptor in fields(feed):
        object.__setattr__(
            forged,
            descriptor.name,
            changes.get(descriptor.name, getattr(feed, descriptor.name)),
        )
    return forged


def _tamper_awareness(
    awareness: AgentAwarenessProjection,
    **changes: object,
) -> AgentAwarenessProjection:
    forged = object.__new__(AgentAwarenessProjection)
    for descriptor in fields(awareness):
        object.__setattr__(
            forged,
            descriptor.name,
            changes.get(descriptor.name, getattr(awareness, descriptor.name)),
        )
    return forged


def _content_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _clock_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _rebind_feed(
    memory: dict[str, object],
    feed: AuthenticatedCollaborationFeed,
    harness: AuthorityHarness,
    *,
    audience_session: AgentSession | None = None,
    acceptance_receipts: tuple[AcceptanceReceipt, ...] | None = None,
    event_page: EventPage | None = None,
) -> AuthenticatedCollaborationFeed:
    receipts = feed.acceptance_receipts if acceptance_receipts is None else acceptance_receipts
    return harness.bind(
        memory=memory,
        awareness=feed.awareness,
        event_page=event_page or feed.event_page,
        project=feed.project,
        audience_session=audience_session or feed.audience_session,
        read_after_cursor=feed.read_after_cursor,
        acceptance_receipts=receipts,
    )


def _sources(
    *,
    role: str = "implementer",
    progress_count: int = 2,
    blocker_summary: str | None = None,
    session_freshness_seconds: int = 300,
    snapshot_freshness_seconds: int = 300,
    observed_at: str = NOW,
    progress_created_at: tuple[str, ...] | None = None,
    causal_cycle: bool = False,
    external_causal_parent: bool = False,
) -> tuple[
    dict[str, object],
    AuthenticatedCollaborationFeed,
    AuthorityHarness,
    AgentSession,
    AgentIdentity,
]:
    project = ProjectScope("project:plastic-promise")
    audience = _agent("agent:builder", role)
    peer = _agent("agent:peer", "implementer")
    reviewer = _agent("agent:reviewer", "deepsec_reviewer")
    audience_session = _presence(project, audience)
    peer_session = _presence(project, peer)
    reviewer_session = _presence(project, reviewer)
    harness = _authority_harness(
        session_freshness_seconds=session_freshness_seconds,
        snapshot_freshness_seconds=snapshot_freshness_seconds,
    )
    own_work = _work(project, audience, "work:own")
    peer_work = _work(project, peer, "work:peer")
    accepted = ResultReceipt.for_work(
        peer_work,
        receipt_id="result:peer",
        submitted_by=peer,
        outcome="completed",
        summary="Accepted result supplied by the authenticated feed",
        submitted_at=NOW,
        artifact_refs=("artifact:peer",),
        evidence_refs=("evidence:review",),
    )
    acceptance, accepted = _issue_acceptance(
        peer_work,
        accepted,
        submitter_session=peer_session,
        reviewer_session=reviewer_session,
        acceptance_receipt_id="acceptance:peer",
        verified_digest_sink=harness.state.accepted_digests,
    )
    conflict = _event(
        project,
        peer,
        "event:conflict",
        "conflict.detected",
        work_item_id="work:peer",
        subject_refs=("module:collaboration", "decision:strict-project-scope"),
    )
    blocker = _event(
        project,
        audience,
        "event:blocker",
        "blocker.raised",
        work_item_id="work:own",
        summary=blocker_summary,
        subject_refs=("module:collaboration", "symbol:compose_context_projection"),
    )
    progress = [
        _event(
            project,
            audience if index == 0 else peer,
            f"event:progress:{index}",
            "work.progressed",
            work_item_id="work:own" if index == 0 else "work:peer",
            created_at=(progress_created_at[index] if progress_created_at is not None else NOW),
            causal_parent_event_id=(
                "event:progress:1"
                if causal_cycle and index == 0
                else "event:previous-page-root"
                if external_causal_parent and index == 1
                else "event:progress:0"
                if index > 0
                else None
            ),
            subject_refs=(
                "module:collaboration",
                "symbol:compose_context_projection",
                "artifact:context-projection",
                "decision:strict-project-scope",
            ),
        )
        for index in range(progress_count)
    ]
    page_events = (conflict, blocker, *progress)
    after = EventCursor(project, "coord:pr4", 10)
    page = EventPage(
        project=project,
        coordination_session_id="coord:pr4",
        events=page_events,
        after_cursor=after,
        next_cursor=EventCursor(project, "coord:pr4", 10 + len(page_events)),
        has_more=False,
    )
    working_set = ProjectWorkingSet(
        coordination=CoordinationSession(
            session_id="coord:pr4",
            project=project,
            objective="Deliver isolated collaboration context",
            created_at=EARLIER,
            expires_at=LATER,
        ),
        goal_summary="Deliver isolated collaboration context",
        plan_revision="plan:4",
        observed_at=observed_at,
        agent_sessions=(audience_session, peer_session, reviewer_session),
        leased_work=(own_work, peer_work),
        accepted_results=(accepted,),
        blockers=(blocker,),
        conflicts=(conflict,),
    )
    awareness = working_set.project_for(audience=audience_session, deltas=page)
    memory = _memory()
    causal_distances = (
        (
            VerifiedCausalDistanceClaim(
                event_id="event:progress:1",
                root_event_id="event:previous-page-root",
                distance=1,
                source_receipt_sha256=TEST_SOURCE_RECEIPT_DIGEST,
            ),
        )
        if external_causal_parent
        else ()
    )
    feed = harness.bind(
        memory=memory,
        awareness=awareness,
        event_page=page,
        project=project,
        audience_session=audience_session,
        read_after_cursor=after,
        acceptance_receipts=(acceptance,),
        causal_distances=causal_distances,
    )
    return memory, feed, harness, audience_session, peer


def test_feed_requires_the_verified_source_factory() -> None:
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_feed_factory_required",
    ):
        AuthenticatedCollaborationFeed()


def test_server_authority_and_binding_require_their_closed_factories() -> None:
    def unreachable_verifier(*_args: object) -> bool:
        raise AssertionError("constructor must reject before calling verifiers")

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_server_authority_required",
    ):
        CollaborationContextAuthority(
            clock=lambda: datetime(2026, 8, 11, 1, tzinfo=timezone.utc),
            policy_verifier=unreachable_verifier,
            source_lineage_verifier=unreachable_verifier,
            projection_verifier=unreachable_verifier,
            acceptance_verifier=unreachable_verifier,
        )
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_authority_binding_factory_required",
    ):
        ServerCollaborationContextBinding()


def test_composer_keeps_memory_layers_separate_and_ranks_collaboration() -> None:
    memory, feed, harness, _, _ = _sources()
    original_layers = {name: memory[name] for name in ("core", "related", "divergent")}

    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )

    for layer, original in original_layers.items():
        assert projected[layer] == original
        assert all("event_id" not in item for item in projected[layer])  # type: ignore[union-attr]
    collaboration = projected["collaboration"]
    assert collaboration["schema_version"] == COLLABORATION_CONTEXT_SCHEMA  # type: ignore[index]
    assert collaboration["canonical_memory_effect"] == "none"  # type: ignore[index]
    assert collaboration["authority"] == "non-authoritative-context-projection"  # type: ignore[index]
    assert [item["kind"] for item in collaboration["items"]] == [  # type: ignore[index]
        "conflict",
        "blocker",
        "accepted_result",
        "progress",
        "progress",
    ]
    assert collaboration["cursor"] == {  # type: ignore[index]
        "after": {
            "project_id": "project:plastic-promise",
            "coordination_session_id": "coord:pr4",
            "sequence": 10,
        },
        "next": {
            "project_id": "project:plastic-promise",
            "coordination_session_id": "coord:pr4",
            "sequence": 14,
        },
        "has_more": False,
        "source_event_count": 4,
    }
    assert collaboration["source_digests"]["memory_context"] == feed.memory_context_sha256  # type: ignore[index]
    assert collaboration["source_digests"]["awareness"] == feed.awareness_sha256  # type: ignore[index]
    assert collaboration["source_digests"]["event_page"] == feed.event_page_sha256  # type: ignore[index]
    assert collaboration["source_digests"]["audience_session"] == (  # type: ignore[index]
        feed.audience_session_sha256
    )
    assert collaboration["source_digests"]["acceptance_receipts"] == list(  # type: ignore[index]
        feed.acceptance_receipt_sha256s
    )
    source_tuple = collaboration["source_tuple"]  # type: ignore[index]
    assert source_tuple["source_kind"] == COLLABORATION_SOURCE_KIND
    assert source_tuple["source_authority"] == COLLABORATION_SOURCE_AUTHORITY
    assert source_tuple["agent_session_policy_revision"] == ("policy:collaboration-context:v1")
    assert source_tuple["event_schema_revision"] == COLLABORATION_EVENT_SCHEMA
    assert source_tuple["event_log_revision"] == TEST_EVENT_LOG_REVISION
    assert source_tuple["projection_factory_revision"] == (
        COLLABORATION_PROJECTION_FACTORY_REVISION
    )
    assert source_tuple["generated_at_utc"] == _clock_text(harness.clock())
    assert source_tuple["acceptance_receipts"] == [
        {
            "acceptance_receipt_id": "acceptance:peer",
            "receipt_sha256": feed.acceptance_receipts[0].content_sha256,
        }
    ]
    runtime_authority = collaboration["runtime_authority"]  # type: ignore[index]
    assert runtime_authority["server_feed_binding"] == "pr4-process-local-server-bound"
    assert runtime_authority["security_boundary"] == "pr4-authenticated-process-local"
    assert runtime_authority["persistent_head_authority"] == (
        "pr5-durable-canonical-adapter"
    )
    assert runtime_authority["restart_recovery"] == "deferred-to-pr5"
    assert (
        runtime_authority["python_factory_is_validation_seam_only"]
        is True
    )
    accepted_result = collaboration["items"][2]  # type: ignore[index]
    assert accepted_result["acceptance_receipt_id"] == "acceptance:peer"
    assert accepted_result["acceptance_receipt_sha256"] == (
        feed.acceptance_receipts[0].content_sha256
    )
    assert accepted_result["accepted_by"] == {
        "agent_id": "agent:reviewer",
        "role": "deepsec_reviewer",
    }
    rendered = str(projected)
    assert "private_adapter_payload" not in rendered
    assert "Private objective" not in rendered


def test_role_aware_relevance_prefers_owned_progress_within_same_kind() -> None:
    memory, feed, harness, _, _ = _sources()
    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )
    progress = [
        item
        for item in projected["collaboration"]["items"]  # type: ignore[index]
        if item["kind"] == "progress"
    ]
    assert [item["event_id"] for item in progress] == [
        "event:progress:0",
        "event:progress:1",
    ]
    assert progress[0]["relevance"] > progress[1]["relevance"]
    assert "audience-work" in progress[0]["relevance_reasons"]
    assert all(0.0 <= item["relevance"] <= 1.0 for item in progress)


def test_relevance_explains_refs_freshness_severity_and_causal_distance() -> None:
    memory, feed, harness, _, _ = _sources(
        progress_count=3,
        progress_created_at=(NOW, EARLIER, NOW),
    )
    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )
    items = {
        item["event_id"]: item
        for item in projected["collaboration"]["items"]  # type: ignore[index]
        if "event_id" in item
    }
    old_peer = items["event:progress:1"]
    new_peer = items["event:progress:2"]
    for reason in (
        "same-module",
        "same-symbol",
        "same-artifact",
        "same-decision",
        "causal-distance:1",
    ):
        assert reason in new_peer["relevance_reasons"]
    assert old_peer["relevance_signals"]["causal_distance"] == 1
    assert new_peer["relevance_signals"]["causal_distance"] == 1
    assert new_peer["relevance_signals"]["freshness"] > old_peer["relevance_signals"]["freshness"]
    assert new_peer["relevance"] > old_peer["relevance"]
    assert (
        items["event:conflict"]["relevance_signals"]["severity"]
        > new_peer["relevance_signals"]["severity"]
    )
    assert new_peer["relevance_signals"]["reference_overlap"] == {
        "module": 1,
        "symbol": 1,
        "artifact": 1,
        "decision": 1,
    }


def test_causal_cycle_fails_closed() -> None:
    memory, feed, harness, _, _ = _sources(causal_cycle=True)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_causal_cycle",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=harness.authority,
        )


def test_budget_preserves_strict_risk_order_and_reports_omissions() -> None:
    memory, feed, harness, _, _ = _sources(progress_count=4)
    budget = CollaborationContextBudget(
        max_items=2,
        max_collaboration_bytes=12_000,
        max_response_bytes=20_000,
    )
    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
        budget=budget,
    )
    collaboration = projected["collaboration"]
    assert [item["kind"] for item in collaboration["items"]] == [  # type: ignore[index]
        "conflict",
        "blocker",
    ]
    assert collaboration["budget"]["emitted_by_kind"] == {  # type: ignore[index]
        "conflict": 1,
        "blocker": 1,
        "accepted_result": 0,
        "progress": 0,
    }
    assert collaboration["budget"]["omitted_by_kind"] == {  # type: ignore[index]
        "conflict": 0,
        "blocker": 0,
        "accepted_result": 1,
        "progress": 4,
    }
    assert len(json.dumps(collaboration, sort_keys=True).encode("utf-8")) <= 12_000
    assert len(json.dumps(projected, sort_keys=True).encode("utf-8")) <= 20_000


def test_byte_pressure_only_emits_a_prefix_of_strict_priority_order() -> None:
    memory, feed, harness, _, _ = _sources(
        progress_count=4,
        blocker_summary="B" * 512,
    )
    full = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )
    expected_items = full["collaboration"]["items"]  # type: ignore[index]

    def compose_at(byte_limit: int):
        try:
            return compose_context_projection(
                memory_context=memory,
                collaboration_feed=feed,
                authority=harness.authority,
                budget=CollaborationContextBudget(
                    max_items=12,
                    max_collaboration_bytes=byte_limit,
                    max_response_bytes=20_000,
                ),
            )
        except CollaborationContextProjectionError as exc:
            assert exc.code == "context_response_budget_exceeded"
            return None

    low, high = 1_024, 12_000
    while low < high:
        midpoint = (low + high) // 2
        projected = compose_at(midpoint)
        emitted_count = (
            0 if projected is None else len(projected["collaboration"]["items"])  # type: ignore[index]
        )
        if emitted_count >= 2:
            high = midpoint
        else:
            low = midpoint + 1

    projected = compose_at(low)
    assert projected is not None
    emitted = projected["collaboration"]["items"]  # type: ignore[index]
    assert 2 <= len(emitted) < len(expected_items)
    assert emitted == expected_items[: len(emitted)]


def test_completed_artifacts_without_acceptance_are_not_accepted_results() -> None:
    memory, feed, harness, _, _ = _sources()
    rebound = _rebind_feed(memory, feed, harness, acceptance_receipts=())

    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=rebound,
        authority=harness.authority,
    )

    kinds = [item["kind"] for item in projected["collaboration"]["items"]]  # type: ignore[index]
    assert "accepted_result" not in kinds
    assert (
        projected["collaboration"]["budget"]["requested_by_kind"][  # type: ignore[index]
            "accepted_result"
        ]
        == 0
    )


def test_caller_supplied_acceptance_does_not_seed_context_authority() -> None:
    memory, feed, harness, _, _ = _sources()
    harness.state.accepted_digests.clear()

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_acceptance_authority_rejected",
    ):
        _rebind_feed(
            memory,
            feed,
            harness,
            acceptance_receipts=feed.acceptance_receipts,
        )


def test_acceptance_requires_an_independent_reviewer() -> None:
    project = ProjectScope("project:plastic-promise")
    peer = _agent("agent:peer", "implementer")
    peer_session = _presence(project, peer)
    work = _work(project, peer, "work:self-review")
    result = ResultReceipt.for_work(
        work,
        receipt_id="result:self-review",
        submitted_by=peer,
        outcome="completed",
        summary="Self reviewed result",
        submitted_at=NOW,
        artifact_refs=("artifact:self-review",),
    )

    reviews = tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"review:self-review:{channel}",
            reviewer_assignment_sha256="sha256:" + "0" * 64,
            reviewer_agent_session_id=peer_session.session_id,
            review_policy_revision=TEST_ACCEPTANCE_POLICY_REVISION,
            source_revision=TEST_SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=NOW,
            evidence_refs=(f"evidence:self-review:{channel}",),
            review_channel=channel,
        )
        for channel in REVIEW_CHANNELS
    )
    clock_value = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    policy_authority = open_server_agent_policy_binding_authority(clock=lambda: clock_value)
    role_authority = open_server_role_assignment_authority(
        repository=InMemoryRoleAssignmentRepository(),
        clock=lambda: clock_value,
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        current_review_policy_revision=TEST_ACCEPTANCE_POLICY_REVISION,
        current_source_revision=TEST_SOURCE_REVISION,
        source_registry=_acceptance_registry(
            work,
            result,
            peer_session,
        ),
        clock=lambda: clock_value,
    )

    with pytest.raises(
        AcceptanceReceiptError,
        match="acceptance_independent_reviewer_required",
    ):
        authority.issue(
            work,
            result,
            reviews,
            submitter_session=peer_session,
            reviewer_session=peer_session,
            reviewer_policy_binding=None,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt, feed: replace(
            receipt,
            project=ProjectScope("project:other"),
        ),
        lambda receipt, feed: replace(
            receipt,
            coordination_session_id="coord:other",
        ),
        lambda receipt, feed: replace(
            receipt,
            work_item_id="work:other",
        ),
        lambda receipt, feed: replace(
            receipt,
            work_receipt_sha256="sha256:" + "0" * 64,
        ),
        lambda receipt, feed: replace(
            receipt,
            result_receipt_sha256="sha256:" + "0" * 64,
        ),
        lambda receipt, feed: replace(
            receipt,
            accepted_by=_agent("agent:unknown-reviewer", "reviewer"),
        ),
        lambda receipt, feed: replace(
            receipt,
            accepted_by=feed.audience_session.identity,
        ),
        lambda receipt, feed: replace(
            receipt,
            accepted_by=_agent("agent:peer", "reviewer"),
        ),
    ],
)
def test_acceptance_receipts_fail_closed_on_scope_result_work_and_reviewer_drift(
    mutate,
) -> None:
    memory, feed, harness, _, _ = _sources()
    mutated = mutate(feed.acceptance_receipts[0], feed)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_acceptance_authority_rejected",
    ):
        _rebind_feed(
            memory,
            feed,
            harness,
            acceptance_receipts=(mutated,),
        )


def test_acceptance_receipt_tuple_and_digest_drift_fail_closed() -> None:
    memory, feed, harness, _, _ = _sources()
    drifted = _tamper_feed(feed, acceptance_receipts=())

    with pytest.raises(
        CollaborationContextProjectionError,
        match="acceptance_receipt_digest_mismatch",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=drifted,
            authority=harness.authority,
        )


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(feed, memory_context_sha256="sha256:" + "0" * 64),
            ),
            "memory_context_digest_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(feed, awareness_sha256="sha256:" + "0" * 64),
            ),
            "awareness_digest_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(feed, working_set_sha256="sha256:" + "0" * 64),
            ),
            "working_set_digest_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(feed, event_page_sha256="sha256:" + "0" * 64),
            ),
            "event_page_digest_mismatch",
        ),
        (
            lambda memory, feed: ({**memory, "project_id": "project:other"}, feed),
            "context_project_scope_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(feed, coordination_session_id="coord:other"),
            ),
            "collaboration_session_scope_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(
                    feed,
                    audience_session=(
                        other := _presence(
                            feed.project,
                            _agent("agent:other", "implementer"),
                        )
                    ),
                    audience_session_sha256=other.content_sha256,
                ),
            ),
            "collaboration_audience_mismatch",
        ),
        (
            lambda memory, feed: (
                memory,
                _tamper_feed(
                    feed,
                    read_after_cursor=EventCursor(feed.project, "coord:pr4", 9),
                ),
            ),
            "collaboration_after_cursor_mismatch",
        ),
    ],
)
def test_composer_fails_closed_on_scope_identity_cursor_and_digest_drift(
    mutate,
    error: str,
) -> None:
    memory, feed, harness, _, _ = _sources()
    mutated_memory, mutated_feed = mutate(memory, feed)
    with pytest.raises(CollaborationContextProjectionError, match=error):
        compose_context_projection(
            memory_context=mutated_memory,
            collaboration_feed=mutated_feed,
            authority=harness.authority,
        )


@pytest.mark.parametrize("state", ["idle", "stale", "closed"])
def test_composer_rejects_non_active_audience_session(state: str) -> None:
    memory, feed, harness, _, _ = _sources()
    inactive = replace(feed.audience_session, state=state)
    rebound = _tamper_feed(
        feed,
        audience_session=inactive,
        audience_session_sha256=inactive.content_sha256,
    )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_audience_session_inactive",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=rebound,
            authority=harness.authority,
        )


def test_composer_rejects_expired_or_future_heartbeat_audience_session() -> None:
    memory, feed, harness, _, _ = _sources()
    expired = replace(feed.audience_session, expires_at=NOW)
    expired_feed = _tamper_feed(
        feed,
        audience_session=expired,
        audience_session_sha256=expired.content_sha256,
    )
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_audience_session_expired",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=expired_feed,
            authority=harness.authority,
        )

    future = replace(
        feed.audience_session,
        last_heartbeat_at="2026-08-11T01:30:00Z",
    )
    future_feed = _tamper_feed(
        feed,
        audience_session=future,
        audience_session_sha256=future.content_sha256,
    )
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_audience_session_from_future",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=future_feed,
            authority=harness.authority,
        )


def test_composer_rejects_audience_session_content_drift_with_same_ids() -> None:
    memory, feed, harness, _, _ = _sources()
    drifted_session = replace(
        feed.audience_session,
        last_heartbeat_at="2026-08-11T00:59:00Z",
    )
    drifted = _tamper_feed(feed, audience_session=drifted_session)

    with pytest.raises(
        CollaborationContextProjectionError,
        match="audience_session_digest_mismatch",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=drifted,
            authority=harness.authority,
        )


def test_composer_rejects_rebound_page_that_does_not_match_awareness_source() -> None:
    memory, feed, harness, audience_session, _ = _sources()
    replacement_page = EventPage(
        project=feed.project,
        coordination_session_id="coord:pr4",
        events=(feed.event_page.events[-1],),
        after_cursor=feed.read_after_cursor,
        next_cursor=feed.awareness.next_cursor,
        has_more=False,
    )
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_source_binding_mismatch",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=replacement_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
        )


def test_composer_rejects_collaboration_collision_and_oversized_memory_response() -> None:
    memory, feed, harness, _, _ = _sources()
    with pytest.raises(
        CollaborationContextProjectionError,
        match="memory_context_collaboration_collision",
    ):
        compose_context_projection(
            memory_context={**memory, "collaboration": {"untrusted": True}},
            collaboration_feed=feed,
            authority=harness.authority,
        )

    large_memory = {**memory, "audit": {"blob": "x" * 8_000}}
    rebound = _rebind_feed(large_memory, feed, harness)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_response_budget_exceeded",
    ):
        compose_context_projection(
            memory_context=large_memory,
            collaboration_feed=rebound,
            authority=harness.authority,
            budget=CollaborationContextBudget(
                max_items=2,
                max_collaboration_bytes=3_000,
                max_response_bytes=4_000,
            ),
        )


def test_feed_cannot_be_replayed_through_an_unrelated_authority() -> None:
    memory, feed, _, _, _ = _sources()
    unrelated = _authority_harness()

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_authority_instance_mismatch",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=unrelated.authority,
        )


def test_authority_rejects_expired_binding_and_stale_session_replay() -> None:
    memory, feed, harness, _, _ = _sources()
    harness.clock.advance(61)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_authority_binding_expired",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=harness.authority,
        )

    memory, feed, harness, _, _ = _sources(session_freshness_seconds=30)
    harness.clock.advance(31)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_audience_session_stale",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=harness.authority,
        )


@pytest.mark.parametrize(
    ("revoke", "error"),
    [
        (
            lambda harness, feed: setattr(harness.state, "policy_valid", False),
            "context_policy_authority_rejected",
        ),
        (
            lambda harness, feed: setattr(harness.state, "source_valid", False),
            "context_source_lineage_authority_rejected",
        ),
        (
            lambda harness, feed: setattr(harness.state, "projection_valid", False),
            "context_projection_authority_rejected",
        ),
        (
            lambda harness, feed: harness.state.accepted_digests.clear(),
            "context_acceptance_authority_rejected",
        ),
    ],
)
def test_compose_rechecks_revocable_server_authority(revoke, error: str) -> None:
    memory, feed, harness, _, _ = _sources()
    revoke(harness, feed)

    with pytest.raises(CollaborationContextProjectionError, match=error):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=harness.authority,
        )


def test_historical_page_receipt_survives_append_only_head_advance() -> None:
    memory, feed, harness, _, _ = _sources()
    harness.state.current_source_head_sequence += 1

    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )
    assert projected["collaboration"]["cursor"]["next"]["sequence"] == 14  # type: ignore[index]


def test_full_feed_binding_rejects_memory_rehash_attack() -> None:
    memory, feed, harness, _, _ = _sources()
    forged_memory = {
        **memory,
        "trace": {"call_id": "call:forged-but-self-consistent"},
    }
    forged = _tamper_feed(
        feed,
        memory_context_sha256=_content_sha256(forged_memory),
    )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_feed_payload_digest_mismatch",
    ):
        compose_context_projection(
            memory_context=forged_memory,
            collaboration_feed=forged,
            authority=harness.authority,
        )


def test_awareness_drift_cannot_reuse_the_original_lineage() -> None:
    memory, feed, harness, _, _ = _sources()
    forged_awareness = _tamper_awareness(
        feed.awareness,
        goal_summary="Caller-forged goal summary",
    )
    forged = _tamper_feed(
        feed,
        awareness=forged_awareness,
        awareness_sha256=forged_awareness.content_sha256,
    )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_lineage_mismatch",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=forged,
            authority=harness.authority,
        )


def test_matching_forged_awareness_requires_both_canonical_verifiers() -> None:
    memory, feed, harness, audience_session, _ = _sources()
    forged_awareness = _tamper_awareness(
        feed.awareness,
        goal_summary="Caller-forged goal summary",
    )
    forged_feed = harness.bind(
        memory=memory,
        awareness=forged_awareness,
        event_page=feed.event_page,
        project=feed.project,
        audience_session=audience_session,
        read_after_cursor=feed.read_after_cursor,
        acceptance_receipts=feed.acceptance_receipts,
    )

    harness.state.authorized_source_lineages.discard(forged_feed.source_lineage.content_sha256)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_lineage_authority_rejected",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=forged_feed,
            authority=harness.authority,
        )

    harness.state.authorized_source_lineages.add(forged_feed.source_lineage.content_sha256)
    harness.state.authorized_awareness_digests.discard(forged_awareness.content_sha256)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_projection_authority_rejected",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=forged_feed,
            authority=harness.authority,
        )


def test_authority_rejects_future_and_stale_awareness_snapshots() -> None:
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_snapshot_from_future",
    ):
        _sources(observed_at="2026-08-11T01:30:00Z")

    memory, feed, harness, _, _ = _sources(snapshot_freshness_seconds=30)
    harness.clock.advance(31)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="collaboration_snapshot_stale",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=feed,
            authority=harness.authority,
        )


def test_verified_cross_page_causal_distance_enriches_relevance() -> None:
    memory, feed, harness, _, _ = _sources(external_causal_parent=True)
    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=feed,
        authority=harness.authority,
    )
    progress = next(
        item
        for item in projected["collaboration"]["items"]  # type: ignore[index]
        if item.get("event_id") == "event:progress:1"
    )

    assert progress["relevance_signals"]["causal_distance"] == 1
    assert "causal-distance:1" in progress["relevance_reasons"]


def test_binding_ids_are_server_generated_and_expired_bindings_stay_invalid() -> None:
    memory, feed, harness, audience_session, _ = _sources()
    short_lived = harness.bind(
        memory=memory,
        awareness=feed.awareness,
        event_page=feed.event_page,
        project=feed.project,
        audience_session=audience_session,
        read_after_cursor=feed.read_after_cursor,
        acceptance_receipts=feed.acceptance_receipts,
        ttl_seconds=1,
    )
    assert short_lived.authority_binding.binding_id != feed.authority_binding.binding_id

    harness.clock.advance(2)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_authority_binding_expired",
    ):
        compose_context_projection(
            memory_context=memory,
            collaboration_feed=short_lived,
            authority=harness.authority,
        )

    replacement = harness.bind(
        memory=memory,
        awareness=feed.awareness,
        event_page=feed.event_page,
        project=feed.project,
        audience_session=audience_session,
        read_after_cursor=feed.read_after_cursor,
        acceptance_receipts=feed.acceptance_receipts,
    )
    projected = compose_context_projection(
        memory_context=memory,
        collaboration_feed=replacement,
        authority=harness.authority,
    )
    assert projected["collaboration"]["runtime_authority"]["binding_id"] == (  # type: ignore[index]
        replacement.authority_binding.binding_id
    )
    assert replacement.authority_binding.binding_id != short_lived.authority_binding.binding_id


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {"source_kind": "caller-built-source"},
            "context_source_kind_invalid",
        ),
        (
            {"source_authority": "caller"},
            "context_source_authority_invalid",
        ),
        (
            {"event_schema_revision": "collaboration-event/v0"},
            "context_source_event_schema_revision_stale",
        ),
        (
            {"projection_factory_revision": "projection-factory/v0"},
            "context_source_projection_factory_revision_stale",
        ),
    ],
)
def test_source_tuple_rejects_substitution_and_stale_fixed_revisions(
    changes: dict[str, object],
    error: str,
) -> None:
    _, feed, _, _, _ = _sources()

    with pytest.raises(CollaborationContextProjectionError, match=error):
        replace(feed.source_lineage.source_tuple, **changes)


def test_authority_rejects_stale_log_revision_and_invalid_source_generation() -> None:
    memory, feed, harness, audience_session, _ = _sources()

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_lineage_authority_rejected",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=feed.event_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
            event_log_revision="event-log:stale",
        )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_generated_from_future",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=feed.event_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
            generated_at_utc="2026-08-11T01:00:01Z",
        )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_generated_before_snapshot",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=feed.event_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
            generated_at_utc=EARLIER,
        )


def test_source_tuple_rejects_audience_and_policy_self_assertion() -> None:
    memory, feed, harness, audience_session, _ = _sources()
    forged_session = _presence(
        feed.project,
        _agent("agent:forged-coordinator", "coordinator"),
    )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_audience_mismatch",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=feed.event_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
            source_audience_session=forged_session,
        )

    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_source_policy_revision_mismatch",
    ):
        harness.bind(
            memory=memory,
            awareness=feed.awareness,
            event_page=feed.event_page,
            project=feed.project,
            audience_session=audience_session,
            read_after_cursor=feed.read_after_cursor,
            acceptance_receipts=feed.acceptance_receipts,
            source_policy_revision="policy:forged",
        )


def test_lineage_claim_rejects_a_cursor_beyond_the_server_head() -> None:
    project = ProjectScope("project:plastic-promise")
    after = EventCursor(project, "coord:pr4", 10)
    with pytest.raises(
        CollaborationContextProjectionError,
        match="context_lineage_cursor_beyond_head",
    ):
        SourcePageLineageClaim(
            source_receipt_id="source-receipt:forged",
            source_receipt_sha256=TEST_SOURCE_RECEIPT_DIGEST,
            source_anchor_sha256=TEST_SOURCE_ANCHOR_DIGEST,
            source_tuple=CollaborationSourceTuple(
                source_kind=COLLABORATION_SOURCE_KIND,
                source_authority=COLLABORATION_SOURCE_AUTHORITY,
                project=project,
                coordination_session_id="coord:pr4",
                audience_agent_id="agent:builder",
                audience_role="implementer",
                audience_session_id="session:agent:builder",
                audience_session_sha256="sha256:" + "8" * 64,
                agent_session_policy_revision="policy:collaboration-context:v1",
                event_schema_revision=COLLABORATION_EVENT_SCHEMA,
                event_log_revision=TEST_EVENT_LOG_REVISION,
                cursor_from=after,
                cursor_to=EventCursor(project, "coord:pr4", 999),
                source_page_digest="sha256:" + "5" * 64,
                projection_factory_revision=COLLABORATION_PROJECTION_FACTORY_REVISION,
                generated_at_utc=NOW,
            ),
            source_head_cursor=EventCursor(project, "coord:pr4", 14),
            event_page_sha256="sha256:" + "5" * 64,
            awareness_sha256="sha256:" + "6" * 64,
            working_set_sha256="sha256:" + "7" * 64,
            visible_event_count=4,
        )
