from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

import plastic_promise.collaboration as collaboration_root
from plastic_promise.collaboration import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    CoordinationSession,
    EventCursor,
    EventPage,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.awareness import (
    AGENT_AWARENESS_SCHEMA,
    PROJECT_WORKING_SET_SCHEMA,
    AgentAwarenessProjection,
    ProjectWorkingSet,
)

NOW = "2026-08-11T01:00:00Z"
EARLIER = "2026-08-11T00:00:00Z"
LATER = "2026-08-11T02:00:00Z"
PUBLIC_GOAL = "Ship a bounded, reviewable awareness projection"


def _agent(agent_id: str, role: str) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, role=role, capabilities=("code.read",))


def _coordination(project: ProjectScope) -> CoordinationSession:
    return CoordinationSession(
        session_id="coord:pr4",
        project=project,
        objective="Private coordination objective must never enter awareness output",
        created_at=EARLIER,
        expires_at=LATER,
    )


def _presence(
    project: ProjectScope,
    agent: AgentIdentity,
    *,
    state: str = "active",
) -> AgentSession:
    return AgentSession(
        session_id=f"session:{agent.agent_id}",
        identity=agent,
        project=project,
        coordination_session_id="coord:pr4",
        state=state,
        started_at=EARLIER,
        last_heartbeat_at=NOW,
        expires_at=LATER,
    )


def _work(
    project: ProjectScope,
    agent: AgentIdentity,
    work_item_id: str,
) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=f"receipt:{work_item_id}",
        work_item_id=work_item_id,
        project=project,
        coordination_session_id="coord:pr4",
        assigned_agent=agent,
        objective=f"Private assignment details for {work_item_id}",
        fencing_generation=1,
        issued_at=EARLIER,
        expires_at=LATER,
    )


def _event(
    project: ProjectScope,
    actor: AgentIdentity,
    event_id: str,
    event_type: str,
    *,
    work_item_id: str | None = None,
    audience_roles: tuple[str, ...] = (),
    summary: str | None = None,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=event_id,
        project=project,
        coordination_session_id="coord:pr4",
        actor=actor,
        event_type=event_type,
        summary=summary if summary is not None else f"Public summary for {event_id}",
        created_at=NOW,
        work_item_id=work_item_id,
        audience_roles=audience_roles,
        payload={"status": "bounded"},
    )


def _page(
    project: ProjectScope,
    events: tuple[CollaborationEvent, ...],
    *,
    after_sequence: int = 0,
    next_sequence: int = 2,
    has_more: bool = False,
) -> EventPage:
    after = EventCursor(project, "coord:pr4", after_sequence)
    return EventPage(
        project=project,
        coordination_session_id="coord:pr4",
        events=events,
        next_cursor=EventCursor(project, "coord:pr4", next_sequence),
        has_more=has_more,
        after_cursor=after,
    )


def _working_set(
    project: ProjectScope,
    *,
    agent_sessions: tuple[AgentSession, ...] = (),
    leased_work: tuple[WorkReceipt, ...] = (),
    accepted_results: tuple[ResultReceipt, ...] = (),
    blockers: tuple[CollaborationEvent, ...] = (),
    conflicts: tuple[CollaborationEvent, ...] = (),
) -> ProjectWorkingSet:
    return ProjectWorkingSet(
        coordination=_coordination(project),
        goal_summary=PUBLIC_GOAL,
        plan_revision="plan:4",
        observed_at=NOW,
        agent_sessions=agent_sessions,
        leased_work=leased_work,
        accepted_results=accepted_results,
        blockers=blockers,
        conflicts=conflicts,
    )


def test_pr4_read_models_are_owned_by_the_awareness_module() -> None:
    assert ProjectWorkingSet.__module__ == "plastic_promise.collaboration.awareness"
    assert AgentAwarenessProjection.__module__ == "plastic_promise.collaboration.awareness"
    assert not hasattr(collaboration_root, "ProjectWorkingSet")
    assert not hasattr(collaboration_root, "AgentAwarenessProjection")


def test_role_projection_is_bounded_redacted_and_non_authoritative() -> None:
    project = ProjectScope("project:plastic-promise")
    reviewer = _agent("agent:reviewer", "reviewer")
    builder = _agent("agent:builder", "implementer")
    reviewer_presence = _presence(project, reviewer)
    builder_presence = _presence(project, builder)
    reviewer_work = _work(project, reviewer, "work:review")
    builder_work = _work(project, builder, "work:build")
    accepted = ResultReceipt.for_work(
        reviewer_work,
        receipt_id="result:review",
        submitted_by=reviewer,
        outcome="completed",
        summary="Reviewed artifact accepted by the supplied source",
        submitted_at=NOW,
        artifact_refs=("artifact:review-report",),
        evidence_refs=("evidence:review",),
    )
    reviewer_blocker = _event(
        project,
        reviewer,
        "event:blocker",
        "blocker.raised",
        work_item_id=reviewer_work.work_item_id,
        audience_roles=("reviewer",),
    )
    public_conflict = _event(
        project,
        builder,
        "event:conflict",
        "conflict.detected",
        work_item_id=builder_work.work_item_id,
    )
    reviewer_delta = _event(
        project,
        reviewer,
        "event:review-progress",
        "work.progressed",
        work_item_id=reviewer_work.work_item_id,
        audience_roles=("reviewer",),
    )
    public_delta = _event(
        project,
        builder,
        "event:build-progress",
        "work.progressed",
        work_item_id=builder_work.work_item_id,
    )
    working_set = _working_set(
        project,
        agent_sessions=(builder_presence, reviewer_presence),
        leased_work=(builder_work, reviewer_work),
        accepted_results=(accepted,),
        blockers=(reviewer_blocker,),
        conflicts=(public_conflict,),
    )

    reviewer_page = _page(project, (reviewer_delta, public_delta))
    reviewer_view = working_set.project_for(
        audience=reviewer_presence,
        deltas=reviewer_page,
    ).to_dict()
    builder_page = _page(project, (public_delta,))
    builder_view = working_set.project_for(
        audience=builder_presence,
        deltas=builder_page,
    ).to_dict()

    assert reviewer_view["schema_version"] == AGENT_AWARENESS_SCHEMA
    assert reviewer_view["authority"] == "non-authoritative-projection"
    assert reviewer_view["canonical_memory_effect"] == "none"
    assert reviewer_view["server_feed_binding"] == "pr4-process-local-server-bound"
    assert reviewer_view["persistent_source"] == "deferred-to-pr5"
    assert reviewer_view["goal_summary"] == PUBLIC_GOAL
    assert reviewer_view["source_bindings"] == {
        "working_set_sha256": working_set.content_sha256,
        "event_page_sha256": reviewer_page.content_sha256,
    }
    working_set_view = working_set.to_dict()
    assert working_set_view["authority"] == "non-authoritative-rebuildable-source"
    assert working_set_view["canonical_memory_effect"] == "none"
    assert working_set_view["server_feed_binding"] == "pr4-process-local-server-bound"
    assert working_set_view["persistent_source"] == "deferred-to-pr5"
    assert len(reviewer_view["projection"]["leased_work"]) == 2  # type: ignore[index]
    assert len(reviewer_view["projection"]["accepted_artifacts"]) == 1  # type: ignore[index]
    assert len(reviewer_view["projection"]["blockers"]) == 1  # type: ignore[index]

    assert [
        item["work_item_id"]
        for item in builder_view["projection"]["leased_work"]  # type: ignore[index]
    ] == ["work:build"]
    assert builder_view["projection"]["accepted_artifacts"] == []  # type: ignore[index]
    assert builder_view["projection"]["blockers"] == []  # type: ignore[index]
    assert len(builder_view["projection"]["conflicts"]) == 1  # type: ignore[index]
    rendered = json.dumps(
        {
            "working_set": working_set_view,
            "reviewer": reviewer_view,
            "builder": builder_view,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert '"objective"' not in rendered
    assert '"payload"' not in rendered
    assert '"capabilities"' not in rendered
    assert "Private assignment details" not in rendered
    assert "Private coordination objective" not in rendered
    normalized_rendered = rendered.casefold()
    for authority_surface in ('"persistence', '"hook', '"mcp', '"runtime_authority'):
        assert authority_surface not in normalized_rendered


def test_cursor_and_audience_scope_fail_closed_while_valid_resume_advances() -> None:
    project = ProjectScope("project:plastic-promise")
    builder = _agent("agent:builder", "implementer")
    reviewer = _agent("agent:reviewer", "reviewer")
    builder_presence = _presence(project, builder)
    working_set = _working_set(
        project,
        agent_sessions=(builder_presence,),
    )
    hidden = _event(
        project,
        reviewer,
        "event:hidden",
        "work.progressed",
        audience_roles=("reviewer",),
    )
    page = _page(
        project,
        (_event(project, builder, "event:visible", "work.progressed"),),
        after_sequence=2,
        next_sequence=4,
        has_more=True,
    )

    projection = working_set.project_for(audience=builder_presence, deltas=page)
    assert projection.after_cursor.sequence == 2
    assert projection.next_cursor.sequence == 4
    assert projection.has_more is True
    assert projection.to_dict()["cursor"]["next"]["sequence"] == 4  # type: ignore[index]

    resumed_page = EventPage(
        project=project,
        coordination_session_id="coord:pr4",
        events=(_event(project, builder, "event:resumed", "work.progressed"),),
        next_cursor=EventCursor(project, "coord:pr4", 5),
        has_more=False,
        after_cursor=projection.next_cursor,
    )
    resumed = working_set.project_for(
        audience=builder_presence,
        deltas=resumed_page,
    )
    assert resumed.after_cursor == projection.next_cursor
    assert resumed.next_cursor.sequence == 5
    assert resumed.content_sha256 != projection.content_sha256

    hidden_page = _page(project, (hidden,), next_sequence=1)
    with pytest.raises(CollaborationContractError, match="awareness_delta_audience_mismatch"):
        working_set.project_for(audience=builder_presence, deltas=hidden_page)

    other = ProjectScope("project:other")
    other_page = EventPage(
        project=other,
        coordination_session_id="coord:pr4",
        events=(),
        next_cursor=EventCursor(other, "coord:pr4", 0),
        has_more=False,
        after_cursor=EventCursor(other, "coord:pr4", 0),
    )
    with pytest.raises(CollaborationContractError, match="awareness_scope_mismatch"):
        working_set.project_for(
            audience=builder_presence,
            deltas=other_page,
        )

    with pytest.raises(CollaborationContractError, match="event_page_empty_cursor_gap"):
        EventPage(
            project=project,
            coordination_session_id="coord:pr4",
            events=(),
            next_cursor=EventCursor(project, "coord:pr4", 5),
            has_more=False,
            after_cursor=EventCursor(project, "coord:pr4", 4),
        )

    unbound_page = EventPage(
        project=project,
        coordination_session_id="coord:pr4",
        events=(),
        next_cursor=EventCursor(project, "coord:pr4", 0),
        has_more=False,
    )
    with pytest.raises(CollaborationContractError, match="awareness_delta_page_unbound"):
        working_set.project_for(audience=builder_presence, deltas=unbound_page)

    with pytest.raises(TypeError):
        working_set.project_for(  # type: ignore[call-arg]
            audience=builder_presence,
            after=EventCursor.start(project, "coord:pr4"),
            deltas=page,
        )


def test_projection_response_is_bounded_by_delta_count_and_total_bytes() -> None:
    project = ProjectScope("project:plastic-promise")
    builder = _agent("agent:builder", "implementer")
    builder_presence = _presence(project, builder)
    working_set = _working_set(
        project,
        agent_sessions=(builder_presence,),
    )

    too_many = _page(
        project,
        tuple(_event(project, builder, f"event:{index}", "work.progressed") for index in range(21)),
        next_sequence=21,
    )
    with pytest.raises(
        CollaborationContractError,
        match="awareness_delta_page_too_large",
    ):
        working_set.project_for(audience=builder_presence, deltas=too_many)

    oversized = _page(
        project,
        tuple(
            _event(
                project,
                builder,
                f"event:large:{index}",
                "work.progressed",
                summary=f"{index:02d}:" + ("x" * 3_900),
            )
            for index in range(20)
        ),
        next_sequence=20,
    )
    with pytest.raises(CollaborationContractError, match="awareness_projection_too_large"):
        working_set.project_for(audience=builder_presence, deltas=oversized)


def test_working_set_rejects_unaccepted_cross_scope_future_and_unbounded_sources() -> None:
    project = ProjectScope("project:plastic-promise")
    other = ProjectScope("project:other")
    builder = _agent("agent:builder", "implementer")
    work = _work(project, builder, "work:build")
    failed = ResultReceipt.for_work(
        work,
        receipt_id="result:failed",
        submitted_by=builder,
        outcome="failed",
        summary="Not accepted",
        artifact_refs=("artifact:failed",),
    )

    with pytest.raises(CollaborationContractError, match="working_set_accepted_result_invalid"):
        _working_set(
            project,
            accepted_results=(failed,),
        )
    with pytest.raises(CollaborationContractError, match="working_set_scope_mismatch"):
        _working_set(
            project,
            leased_work=(_work(other, builder, "work:other"),),
        )
    with pytest.raises(CollaborationContractError, match="working_set_agent_from_future"):
        _working_set(
            project,
            agent_sessions=(
                AgentSession(
                    session_id="session:future",
                    identity=builder,
                    project=project,
                    coordination_session_id="coord:pr4",
                    state="active",
                    started_at=EARLIER,
                    last_heartbeat_at="2026-08-11T01:30:00Z",
                    expires_at=LATER,
                ),
            ),
        )
    with pytest.raises(
        CollaborationContractError,
        match="working_set_agent_sessions_too_many",
    ):
        _working_set(
            project,
            agent_sessions=tuple(
                AgentSession(
                    session_id=f"session:{index}",
                    identity=_agent(f"agent:{index}", "implementer"),
                    project=project,
                    coordination_session_id="coord:pr4",
                    state="active",
                    started_at=EARLIER,
                    last_heartbeat_at=NOW,
                    expires_at=LATER,
                )
                for index in range(65)
            ),
        )


def test_projection_requires_registered_active_unexpired_agent_session() -> None:
    project = ProjectScope("project:plastic-promise")
    reviewer = _agent("agent:reviewer", "reviewer")
    reviewer_presence = _presence(project, reviewer)
    page = _page(project, (), next_sequence=0)
    working_set = _working_set(project, agent_sessions=(reviewer_presence,))

    with pytest.raises(CollaborationContractError, match="awareness_audience_session_invalid"):
        working_set.project_for(audience=reviewer, deltas=page)  # type: ignore[arg-type]

    impersonator = AgentSession(
        session_id="session:impersonator",
        identity=reviewer,
        project=project,
        coordination_session_id="coord:pr4",
        state="active",
        started_at=EARLIER,
        last_heartbeat_at=NOW,
        expires_at=LATER,
    )
    with pytest.raises(CollaborationContractError, match="awareness_audience_not_registered"):
        working_set.project_for(audience=impersonator, deltas=page)

    idle_presence = _presence(project, reviewer, state="idle")
    idle_working_set = _working_set(project, agent_sessions=(idle_presence,))
    with pytest.raises(CollaborationContractError, match="awareness_audience_inactive"):
        idle_working_set.project_for(audience=idle_presence, deltas=page)

    expired_presence = AgentSession(
        session_id="session:expired",
        identity=reviewer,
        project=project,
        coordination_session_id="coord:pr4",
        state="active",
        started_at=EARLIER,
        last_heartbeat_at=NOW,
        expires_at=NOW,
    )
    expired_working_set = _working_set(project, agent_sessions=(expired_presence,))
    with pytest.raises(CollaborationContractError, match="awareness_audience_expired"):
        expired_working_set.project_for(audience=expired_presence, deltas=page)


def test_accepted_result_must_bind_one_to_one_to_the_exact_work_receipt() -> None:
    project = ProjectScope("project:plastic-promise")
    builder = _agent("agent:builder", "implementer")
    reviewer = _agent("agent:reviewer", "reviewer")
    work = _work(project, builder, "work:build")
    accepted = ResultReceipt.for_work(
        work,
        receipt_id="result:build",
        submitted_by=builder,
        outcome="completed",
        summary="Built artifact",
        submitted_at=NOW,
        artifact_refs=("artifact:build",),
    )

    valid = _working_set(project, leased_work=(work,), accepted_results=(accepted,))
    assert valid.accepted_results == (accepted,)

    with pytest.raises(
        CollaborationContractError,
        match="working_set_accepted_result_work_missing",
    ):
        _working_set(project, accepted_results=(accepted,))

    forged_digest = replace(accepted, work_receipt_sha256="sha256:" + ("0" * 64))
    with pytest.raises(
        CollaborationContractError,
        match="working_set_accepted_result_digest_mismatch",
    ):
        _working_set(project, leased_work=(work,), accepted_results=(forged_digest,))

    forged_submitter = replace(accepted, submitted_by=reviewer)
    with pytest.raises(
        CollaborationContractError,
        match="working_set_accepted_result_submitter_mismatch",
    ):
        _working_set(project, leased_work=(work,), accepted_results=(forged_submitter,))

    for submitted_at in ("2026-08-10T23:59:59Z", "2026-08-11T02:00:01Z"):
        with pytest.raises(
            CollaborationContractError,
            match="working_set_accepted_result_time_invalid",
        ):
            _working_set(
                project,
                leased_work=(work,),
                accepted_results=(replace(accepted, submitted_at=submitted_at),),
            )

    duplicate_result = replace(accepted, receipt_id="result:build:duplicate")
    with pytest.raises(
        CollaborationContractError,
        match="working_set_result_work_duplicate",
    ):
        _working_set(
            project,
            leased_work=(work,),
            accepted_results=(accepted, duplicate_result),
        )


def test_awareness_projection_has_no_public_arbitrary_mapping_constructor() -> None:
    project = ProjectScope("project:plastic-promise")
    reviewer = _agent("agent:reviewer", "reviewer")
    cursor = EventCursor.start(project, "coord:pr4")

    with pytest.raises(CollaborationContractError, match="awareness_factory_required"):
        AgentAwarenessProjection(
            project=project,
            coordination_session_id="coord:pr4",
            audience=reviewer,
            observed_at=NOW,
            goal_summary=PUBLIC_GOAL,
            plan_revision="plan:4",
            after_cursor=cursor,
            next_cursor=cursor,
            has_more=False,
            projection={"escaped_channel": "caller-controlled content"},
        )
    with pytest.raises(CollaborationContractError, match="awareness_factory_required"):
        AgentAwarenessProjection()


def test_projection_digest_binds_exact_working_set_and_event_page_sources() -> None:
    project = ProjectScope("project:plastic-promise")
    builder = _agent("agent:builder", "implementer")
    builder_presence = _presence(project, builder)
    working_set = _working_set(project, agent_sessions=(builder_presence,))
    page = _page(
        project,
        (_event(project, builder, "event:one", "work.progressed"),),
        next_sequence=1,
    )

    projection = working_set.project_for(audience=builder_presence, deltas=page)
    bindings = projection.to_dict()["source_bindings"]
    assert bindings == {
        "working_set_sha256": working_set.content_sha256,
        "event_page_sha256": page.content_sha256,
    }

    later_page = _page(project, (), after_sequence=1, next_sequence=1)
    assert later_page.content_sha256 != page.content_sha256
    later_projection = working_set.project_for(audience=builder_presence, deltas=later_page)
    assert later_projection.event_page_sha256 == later_page.content_sha256
    assert later_projection.content_sha256 != projection.content_sha256


def test_working_set_is_immutable_deterministic_and_reports_only_counts() -> None:
    project = ProjectScope("project:plastic-promise")
    reviewer = _agent("agent:reviewer", "reviewer")
    builder = _agent("agent:builder", "implementer")
    left = ProjectWorkingSet(
        coordination=_coordination(project),
        goal_summary=PUBLIC_GOAL,
        plan_revision="plan:4",
        observed_at=NOW,
        agent_sessions=(_presence(project, reviewer), _presence(project, builder)),
        leased_work=(
            _work(project, reviewer, "work:review"),
            _work(project, builder, "work:build"),
        ),
    )
    right = ProjectWorkingSet(
        coordination=_coordination(project),
        goal_summary=PUBLIC_GOAL,
        plan_revision="plan:4",
        observed_at=NOW,
        agent_sessions=(_presence(project, builder), _presence(project, reviewer)),
        leased_work=(
            _work(project, builder, "work:build"),
            _work(project, reviewer, "work:review"),
        ),
    )

    assert left.content_sha256 == right.content_sha256
    assert left.to_dict()["schema_version"] == PROJECT_WORKING_SET_SCHEMA
    assert left.to_dict()["counts"] == {
        "agent_sessions": 2,
        "leased_work": 2,
        "accepted_results": 0,
        "blockers": 0,
        "conflicts": 0,
    }
    assert left.to_dict()["source_digests"] == {
        "coordination": left.coordination.content_sha256,
        "agent_sessions": [item.content_sha256 for item in left.agent_sessions],
        "leased_work": [item.content_sha256 for item in left.leased_work],
        "accepted_results": [],
        "blockers": [],
        "conflicts": [],
    }
    revised = ProjectWorkingSet(
        coordination=left.coordination,
        goal_summary=left.goal_summary,
        plan_revision="plan:5",
        observed_at=NOW,
        agent_sessions=left.agent_sessions,
        leased_work=left.leased_work,
    )
    assert revised.content_sha256 != left.content_sha256
    assert "Private assignment details" not in left.canonical_json()
    assert "Private coordination objective" not in left.canonical_json()
    with pytest.raises(FrozenInstanceError):
        left.plan_revision = "plan:5"  # type: ignore[misc]
