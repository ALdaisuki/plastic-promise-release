from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from plastic_promise.collaboration import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    CollaborationEventLog,
    CoordinationSession,
    EventCursor,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)

NOW = "2026-08-10T12:00:00Z"
LATER = "2026-08-10T13:00:00Z"
PAST = "2026-08-10T11:00:00Z"


def _agent(agent_id: str, role: str) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, role=role)


def _event(
    event_id: str,
    project: ProjectScope,
    actor: AgentIdentity,
    *,
    session: str = "coord:pr1",
    event_type: str = "work.progressed",
    summary: str = "bounded progress",
    **kwargs: object,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=event_id,
        project=project,
        coordination_session_id=session,
        actor=actor,
        event_type=event_type,
        summary=summary,
        created_at=NOW,
        **kwargs,
    )


def test_contracts_are_immutable_bounded_non_secret_and_stably_hashed() -> None:
    project = ProjectScope("project:repo:github.com/aldaisuki/plastic-promise")
    agent = AgentIdentity(
        "agent:reviewer",
        "reviewer",
        capabilities=("code.read", "finding.publish"),
    )
    left = _event(
        "event:stable",
        project,
        agent,
        payload={"severity": "p0", "evidence": {"line": 17, "valid": True}},
    )
    right = _event(
        "event:stable",
        project,
        agent,
        payload={"evidence": {"valid": True, "line": 17}, "severity": "p0"},
    )

    assert left.canonical_json() == right.canonical_json()
    assert left.content_sha256 == right.content_sha256
    assert left.content_sha256.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        project.project_id = "project:other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        left.payload["severity"] = "p1"  # type: ignore[index]
    with pytest.raises(CollaborationContractError, match="project_unknown_forbidden"):
        ProjectScope("project:unknown")
    for invalid_project_id in (
        "project:has space",
        "project:control\ncharacter",
        "project:非canonical",
        f"project:{'a' * 249}",
        "project:legacy-quarantine",
    ):
        with pytest.raises(CollaborationContractError, match="project_id_invalid"):
            ProjectScope(invalid_project_id)
    with pytest.raises(CollaborationContractError, match="secret_field_forbidden"):
        _event(
            "event:secret",
            project,
            agent,
            payload={"provider": {"api_key": "must-not-enter-the-log"}},
        )
    with pytest.raises(CollaborationContractError, match="secret_field_forbidden"):
        _event(
            "event:camel-secret",
            project,
            agent,
            payload={"provider": {"accessToken": "synthetic-value"}},
        )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_semantic_channel_forbidden",
    ):
        _event(
            "event:private-reasoning",
            project,
            agent,
            payload={"review": {"privateReasoning": "hidden transcript"}},
        )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_semantic_channel_forbidden",
    ):
        _event(
            "event:lease-capability",
            project,
            agent,
            payload={"assignment": {"lease_capability": "opaque-capability"}},
        )
    for event_id, forbidden_field in (
        ("event:raw-prompt", "rawPrompt"),
        ("event:full-prompt", "full_prompt"),
        ("event:prompt-transcript", "promptTranscript"),
    ):
        with pytest.raises(
            CollaborationContractError,
            match="collaboration_semantic_channel_forbidden",
        ):
            _event(
                event_id,
                project,
                agent,
                payload={"capture": {forbidden_field: "verbatim user content"}},
            )

    allowed = _event(
        "event:near-miss-public-field",
        project,
        agent,
        event_type="work.released",
        payload={"release_handle": "public-release-reference"},
    )
    assert allowed.payload["release_handle"] == "public-release-reference"
    with pytest.raises(CollaborationContractError, match="secret_value_forbidden"):
        _event(
            "event:private-key",
            project,
            agent,
            summary="-----BEGIN OPENSSH PRIVATE KEY-----",
        )
    with pytest.raises(CollaborationContractError, match="secret_value_forbidden"):
        _event(
            "event:token-value",
            project,
            agent,
            summary="ghp_" + "a" * 36,
        )


def test_contracts_reject_overdeep_overnoded_and_lossy_json_projections() -> None:
    project = ProjectScope("project:plastic-promise")
    agent = _agent("agent:reviewer", "reviewer")
    nested: object = "leaf"
    for _ in range(9):
        nested = {"child": nested}
    with pytest.raises(CollaborationContractError, match="collaboration_json_too_complex"):
        _event("event:deep", project, agent, payload={"nested": nested})

    wide = {f"group-{group}": {f"item-{item}": item for item in range(8)} for group in range(64)}
    with pytest.raises(CollaborationContractError, match="collaboration_json_too_complex"):
        _event("event:wide", project, agent, payload=wide)
    with pytest.raises(CollaborationContractError, match="collaboration_json_number_invalid"):
        _event("event:huge-int", project, agent, payload={"value": 1 << 63})
    with pytest.raises(CollaborationContractError, match="event_cursor_sequence_invalid"):
        EventCursor(project, "coord:pr1", 1 << 63)
    with pytest.raises(CollaborationContractError, match="audience_roles_duplicate"):
        _event(
            "event:duplicate-role",
            project,
            agent,
            audience_roles=("reviewer", "reviewer"),
        )
    with pytest.raises(CollaborationContractError, match="audience_roles_too_many"):
        _event(
            "event:too-many-roles",
            project,
            agent,
            audience_roles=tuple("reviewer" for _ in range(33)),
        )

    valid = _event("event:projection", project, agent)
    extra_field = valid.to_dict()
    extra_field["unexpected"] = True
    with pytest.raises(CollaborationContractError, match="collaboration_event_projection_invalid"):
        CollaborationEvent.from_dict(extra_field)

    lossy_sequence = valid.to_dict()
    lossy_sequence["actor"]["capabilities"] = "code.read"  # type: ignore[index]
    with pytest.raises(CollaborationContractError, match="actor_capabilities_invalid"):
        CollaborationEvent.from_dict(lossy_sequence)


def test_session_work_and_result_receipts_remain_project_bound() -> None:
    project = ProjectScope("project:plastic-promise")
    builder = _agent("agent:builder", "implementer")
    coordination = CoordinationSession(
        session_id="coord:pr1",
        project=project,
        objective="Implement the collaboration contracts",
        created_at=NOW,
        expires_at=LATER,
    )
    presence = AgentSession(
        session_id="agent-session:builder",
        identity=builder,
        project=project,
        coordination_session_id=coordination.session_id,
        state="active",
        started_at=NOW,
        last_heartbeat_at=NOW,
        expires_at=LATER,
    )
    work = WorkReceipt(
        receipt_id="work-receipt:1",
        work_item_id="work:contracts",
        project=project,
        coordination_session_id=coordination.session_id,
        assigned_agent=builder,
        objective="Add immutable contracts",
        fencing_generation=1,
        issued_at=NOW,
        expires_at=LATER,
    )
    result = ResultReceipt.for_work(
        work,
        receipt_id="result-receipt:1",
        submitted_by=builder,
        outcome="completed",
        summary="Contracts implemented",
        submitted_at=NOW,
        evidence_refs=("test:project-collaboration",),
        result={"tests": 4},
    )

    assert presence.project == coordination.project == work.project == result.project
    assert result.work_receipt_sha256 == work.content_sha256
    assert result.to_dict()["submitted_by"]["agent_id"] == builder.agent_id  # type: ignore[index]
    with pytest.raises(CollaborationContractError, match="result_submitter_not_assignee"):
        ResultReceipt.for_work(
            work,
            receipt_id="result-receipt:forged",
            submitted_by=_agent("agent:other", "implementer"),
            outcome="completed",
            summary="forged",
        )
    with pytest.raises(CollaborationContractError, match="result_submitter_not_assignee"):
        ResultReceipt.for_work(
            work,
            receipt_id="result-receipt:identity-drift",
            submitted_by=_agent(builder.agent_id, "reviewer"),
            outcome="completed",
            summary="same id, different public identity",
        )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_semantic_channel_forbidden",
    ):
        ResultReceipt.for_work(
            work,
            receipt_id="result-receipt:private-reasoning",
            submitted_by=builder,
            outcome="completed",
            summary="result with a forbidden private channel",
            result={"chain_of_thought": "hidden transcript"},
        )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_semantic_channel_forbidden",
    ):
        ResultReceipt.for_work(
            work,
            receipt_id="result-receipt:prompt-transcript",
            submitted_by=builder,
            outcome="completed",
            summary="result with a forbidden prompt channel",
            result={"audit": {"prompt_transcript": "verbatim user content"}},
        )
    with pytest.raises(CollaborationContractError, match="agent_session_heartbeat_after_expiry"):
        AgentSession(
            session_id="agent-session:expired",
            identity=builder,
            project=project,
            coordination_session_id=coordination.session_id,
            state="stale",
            started_at=NOW,
            last_heartbeat_at="2026-08-10T14:00:00Z",
            expires_at=LATER,
        )


def test_event_log_preserves_injected_connection_transaction_and_lifetime() -> None:
    connection = sqlite3.connect(":memory:")
    log = CollaborationEventLog(connection=connection)
    connection.execute("CREATE TABLE canonical_marker (value TEXT NOT NULL)")
    connection.commit()

    connection.execute("INSERT INTO canonical_marker VALUES ('uncommitted')")
    log.append(
        _event(
            "event:uncommitted",
            ProjectScope("project:a"),
            _agent("agent:publisher", "coordinator"),
        )
    )
    assert connection.in_transaction is True
    connection.rollback()

    assert connection.execute("SELECT COUNT(*) FROM canonical_marker").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM collaboration_events").fetchone()[0] == 0
    log.close()
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_event_log_owned_path_commits_and_owns_its_connection(tmp_path) -> None:
    db_path = tmp_path / "canonical.db"
    log = CollaborationEventLog(db_path=db_path)
    project = ProjectScope("project:a")
    actor = _agent("agent:publisher", "coordinator")
    log.append(_event("event:owned", project, actor))

    observer = sqlite3.connect(db_path)
    assert observer.execute("SELECT COUNT(*) FROM collaboration_events").fetchone()[0] == 1
    observer.close()
    log.close()
    with pytest.raises(sqlite3.ProgrammingError):
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=actor,
        )


def test_event_log_isolates_project_session_cursor_and_audience() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE canonical_marker (value TEXT NOT NULL)")
    connection.execute("INSERT INTO canonical_marker VALUES ('same-connection')")
    log = CollaborationEventLog(connection=connection)
    project_a = ProjectScope("project:a")
    project_b = ProjectScope("project:b")
    publisher = _agent("agent:publisher", "coordinator")
    reviewer = _agent("agent:reviewer", "reviewer")
    implementer = _agent("agent:implementer", "implementer")

    log.append(_event("event:broadcast", project_a, publisher))
    log.append(
        _event(
            "event:reviewers",
            project_a,
            publisher,
            audience_roles=("reviewer",),
        )
    )
    log.append(
        _event(
            "event:implementer",
            project_a,
            publisher,
            audience_agent_ids=(implementer.agent_id,),
        )
    )
    log.append(_event("event:other-project", project_b, publisher))
    log.append(_event("event:other-session", project_a, publisher, session="coord:other"))

    first = log.read(
        project=project_a,
        coordination_session_id="coord:pr1",
        audience=reviewer,
        limit=1,
    )
    second = log.read(
        project=project_a,
        coordination_session_id="coord:pr1",
        audience=reviewer,
        after=first.next_cursor,
        limit=1,
    )
    implementer_page = log.read(
        project=project_a,
        coordination_session_id="coord:pr1",
        audience=implementer,
    )
    project_b_page = log.read(
        project=project_b,
        coordination_session_id="coord:pr1",
        audience=reviewer,
    )

    assert [event.event_id for event in first.events] == ["event:broadcast"]
    assert first.after_cursor == EventCursor.start(project_a, "coord:pr1")
    assert first.has_more is True
    assert [event.event_id for event in second.events] == ["event:reviewers"]
    assert second.after_cursor == first.next_cursor
    assert second.has_more is False
    assert [event.event_id for event in implementer_page.events] == [
        "event:broadcast",
        "event:implementer",
    ]
    assert [event.event_id for event in project_b_page.events] == ["event:other-project"]
    assert (
        connection.execute("SELECT value FROM canonical_marker").fetchone()[0] == "same-connection"
    )
    with pytest.raises(CollaborationContractError, match="collaboration_cursor_scope_mismatch"):
        log.read(
            project=project_b,
            coordination_session_id="coord:pr1",
            audience=reviewer,
            after=EventCursor.start(project_a, "coord:pr1"),
        )
    connection.close()


def test_event_log_enforces_causality_expiry_idempotency_and_append_only_storage() -> None:
    connection = sqlite3.connect(":memory:")
    clock_value = [datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)]
    log = CollaborationEventLog(
        connection=connection,
        clock=lambda: clock_value[0],
    )
    project = ProjectScope("project:a")
    other_project = ProjectScope("project:b")
    actor = _agent("agent:builder", "implementer")
    parent = _event("event:parent", project, actor)
    child = _event(
        "event:child",
        project,
        actor,
        causal_parent_event_id=parent.event_id,
    )
    expired = CollaborationEvent(
        event_id="event:expired",
        project=project,
        coordination_session_id="coord:pr1",
        actor=actor,
        event_type="work.progressed",
        summary="expired progress",
        created_at="2026-08-10T10:00:00Z",
        expires_at=PAST,
    )

    parent_cursor = log.append(parent)
    assert log.append(parent) == parent_cursor
    log.append(child)
    log.append(expired, retention_seconds=3600)
    # The source absolute expiry (11:00) is diagnostic only.  The server owns
    # the one-hour retention policy and re-stamps it from its 12:00 observation.
    assert (
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=actor,
            include_expired=True,
        ).events[-1].expires_at
        == "2026-08-10T13:00:00.000000Z"
    )
    clock_value[0] = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    current = log.read(
        project=project,
        coordination_session_id="coord:pr1",
        audience=actor,
    )
    all_events = log.read(
        project=project,
        coordination_session_id="coord:pr1",
        audience=actor,
        include_expired=True,
    )

    assert [event.event_id for event in current.events] == ["event:parent", "event:child"]
    assert [event.event_id for event in all_events.events] == [
        "event:parent",
        "event:child",
        "event:expired",
    ]
    with pytest.raises(CollaborationContractError, match="collaboration_parent_scope_mismatch"):
        log.append(
            _event(
                "event:cross-project-child",
                other_project,
                actor,
                causal_parent_event_id=parent.event_id,
            )
        )
    with pytest.raises(CollaborationContractError, match="collaboration_parent_scope_mismatch"):
        log.append(
            _event(
                "event:cross-session-child",
                project,
                actor,
                session="coord:other",
                causal_parent_event_id=parent.event_id,
            )
        )
    with pytest.raises(CollaborationContractError, match="collaboration_event_id_conflict"):
        log.append(
            _event(
                parent.event_id,
                project,
                actor,
                summary="same ID, different immutable content",
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="collaboration_event_append_only"):
        connection.execute(
            "UPDATE collaboration_events SET event_type = 'agent.closed' WHERE event_id = ?",
            (parent.event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="collaboration_event_append_only"):
        connection.execute(
            "DELETE FROM collaboration_events WHERE event_id = ?",
            (parent.event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="collaboration_event_append_only"):
        connection.execute(
            "INSERT OR REPLACE INTO collaboration_events "
            "SELECT * FROM collaboration_events WHERE event_id = ?",
            (parent.event_id,),
        )
    connection.close()


def test_event_log_uses_server_time_and_keeps_source_time_diagnostic_only() -> None:
    connection = sqlite3.connect(":memory:")
    log = CollaborationEventLog(
        connection=connection,
        clock=lambda: datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    project = ProjectScope("project:a")
    actor = _agent("agent:builder", "implementer")
    source = CollaborationEvent(
        event_id="event:skewed-source-clock",
        project=project,
        coordination_session_id="coord:pr1",
        actor=actor,
        event_type="work.progressed",
        summary="source clock is diagnostic only",
        created_at="2036-08-10T10:00:00Z",
        expires_at="2036-08-10T10:30:00Z",
    )

    cursor = log.append(source)
    stored = log.read(
        project=project,
        coordination_session_id="coord:pr1",
        audience=actor,
        include_expired=True,
    ).events[0]

    assert stored.created_at == "2026-08-10T12:00:00.000000Z"
    assert stored.expires_at is None
    diagnostics = stored.payload["_server_time_diagnostics"]
    assert isinstance(diagnostics, Mapping)
    assert diagnostics["created_at"] == "2036-08-10T10:00:00.000000Z"
    assert diagnostics["expires_at"] == "2036-08-10T10:30:00.000000Z"
    assert diagnostics["source_event_sha256"] == source.content_sha256
    # A replay cannot move the canonical timestamp, even when the source wall
    # clock changed between attempts.
    replay = CollaborationEvent(
        event_id=source.event_id,
        project=project,
        coordination_session_id="coord:pr1",
        actor=actor,
        event_type=source.event_type,
        summary=source.summary,
        created_at="2020-01-01T00:00:00Z",
        expires_at="2020-01-01T00:01:00Z",
    )
    assert log.append(replay) == cursor
    assert (
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=actor,
            include_expired=True,
        ).events[0].created_at
        == "2026-08-10T12:00:00.000000Z"
    )
    connection.close()


def test_event_log_rejects_naive_server_clock() -> None:
    connection = sqlite3.connect(":memory:")
    log = CollaborationEventLog(
        connection=connection,
        clock=lambda: datetime(2026, 8, 10, 12, 0),
    )
    project = ProjectScope("project:a")
    actor = _agent("agent:builder", "implementer")
    with pytest.raises(CollaborationContractError, match="collaboration_clock_invalid"):
        log.append(_event("event:naive-server-clock", project, actor))
    connection.close()


def test_event_log_detects_hash_corruption() -> None:
    connection = sqlite3.connect(":memory:")
    log = CollaborationEventLog(connection=connection)
    project = ProjectScope("project:a")
    actor = _agent("agent:publisher", "coordinator")
    log.append(_event("event:hash", project, actor))
    connection.commit()

    connection.execute("DROP TRIGGER collaboration_events_no_update")
    connection.execute(
        "UPDATE collaboration_events SET event_sha256 = ? WHERE event_id = ?",
        ("sha256:" + "0" * 64, "event:hash"),
    )
    with pytest.raises(CollaborationContractError, match="collaboration_event_hash_mismatch"):
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=actor,
        )
    connection.close()


def test_event_log_fails_closed_when_audience_projection_drifts() -> None:
    connection = sqlite3.connect(":memory:")
    log = CollaborationEventLog(connection=connection)
    project = ProjectScope("project:a")
    publisher = _agent("agent:publisher", "coordinator")
    intruder = _agent("agent:builder", "implementer")
    log.append(
        _event(
            "event:restricted",
            project,
            publisher,
            audience_roles=("reviewer",),
        )
    )
    connection.commit()

    connection.execute("DROP TRIGGER collaboration_events_no_update")
    connection.execute(
        "UPDATE collaboration_events SET audience_roles_json = '[]' WHERE event_id = ?",
        ("event:restricted",),
    )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_event_projection_mismatch",
    ):
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=intruder,
        )

    connection.execute(
        "UPDATE collaboration_events SET audience_roles_json = 'not-json' WHERE event_id = ?",
        ("event:restricted",),
    )
    with pytest.raises(
        CollaborationContractError,
        match="collaboration_event_projection_mismatch",
    ):
        log.read(
            project=project,
            coordination_session_id="coord:pr1",
            audience=intruder,
        )
    connection.close()
