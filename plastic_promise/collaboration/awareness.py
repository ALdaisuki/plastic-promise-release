"""PR4 project working-set and Agent awareness read models.

This module owns the rebuildable, non-authoritative projection policy.  PR1
base contracts remain transport-safe coordination facts; PR4 imports those
facts and derives one bounded, audience-scoped view through the
``ProjectWorkingSet.project_for`` interface.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from .contracts import (
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
    _freeze_json,
    _JsonContract,
    _parse_timestamp,
    _require_identifier,
    _require_public_text,
    _require_type,
    _sequence,
    _thaw_json,
    _timestamp,
)

PROJECT_WORKING_SET_SCHEMA = "project-working-set/v1"
AGENT_AWARENESS_SCHEMA = "agent-awareness/v1"

_MAX_SUMMARY_BYTES = 4 * 1024
_MAX_WORKING_SET_ITEMS = 64
_MAX_AWARENESS_DELTAS = 20
_MAX_AWARENESS_BYTES = 64 * 1024
_FULL_AWARENESS_ROLES = frozenset({"coordinator", "reviewer"})


def _agent_awareness_view(session: AgentSession) -> dict[str, object]:
    return {
        "agent_id": session.identity.agent_id,
        "role": session.identity.role,
        "session_id": session.session_id,
        "state": session.state,
        "last_heartbeat_at": session.last_heartbeat_at,
        "expires_at": session.expires_at,
    }


def _work_awareness_view(work: WorkReceipt) -> dict[str, object]:
    return {
        "work_item_id": work.work_item_id,
        "assigned_agent_id": work.assigned_agent.agent_id,
        "assigned_role": work.assigned_agent.role,
        "fencing_generation": work.fencing_generation,
        "dependency_work_ids": list(work.dependency_work_ids),
        "expires_at": work.expires_at,
        "receipt_sha256": work.content_sha256,
    }


def _artifact_awareness_views(result: ResultReceipt) -> list[dict[str, object]]:
    return [
        {
            "artifact_ref": artifact_ref,
            "work_item_id": result.work_item_id,
            "submitted_by": result.submitted_by.agent_id,
            "submitted_at": result.submitted_at,
            "result_receipt_id": result.receipt_id,
            "result_receipt_sha256": result.content_sha256,
            "evidence_refs": list(result.evidence_refs),
        }
        for artifact_ref in result.artifact_refs
    ]


def _event_awareness_view(event: CollaborationEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "actor": {
            "agent_id": event.actor.agent_id,
            "role": event.actor.role,
        },
        "summary": event.summary,
        "created_at": event.created_at,
        "expires_at": event.expires_at,
        "work_item_id": event.work_item_id,
        "subject_refs": list(event.subject_refs),
        "evidence_refs": list(event.evidence_refs),
    }


@dataclass(frozen=True, slots=True, init=False)
class AgentAwarenessProjection(_JsonContract):
    """Bounded, role-aware coordination view with no canonical authority.

    The projection intentionally omits work objectives, Agent capabilities,
    event payloads, raw prompts, and private reasoning.  A cursor identifies
    the already-consumed collaboration page; it is not a lease or execution
    capability.
    """

    project: ProjectScope
    coordination_session_id: str
    audience_agent_id: str
    audience_role: str
    audience_session_id: str
    observed_at: str
    goal_summary: str
    plan_revision: str
    after_cursor: EventCursor
    next_cursor: EventCursor
    has_more: bool
    working_set_sha256: str
    event_page_sha256: str
    _projection: Mapping[str, object] = field(repr=False)

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationContractError("awareness_factory_required")

    @classmethod
    def _from_sources(
        cls,
        *,
        working_set: ProjectWorkingSet,
        audience: AgentSession,
        deltas: EventPage,
        active_agents: tuple[AgentSession, ...],
        visible_work: tuple[WorkReceipt, ...],
        visible_results: tuple[ResultReceipt, ...],
        visible_blockers: tuple[CollaborationEvent, ...],
        visible_conflicts: tuple[CollaborationEvent, ...],
    ) -> AgentAwarenessProjection:
        """Build a closed projection from source contracts, never caller JSON."""

        _require_type(working_set, ProjectWorkingSet, "awareness_working_set_invalid")
        _require_type(audience, AgentSession, "awareness_audience_session_invalid")
        _require_type(deltas, EventPage, "awareness_delta_page_invalid")
        if deltas.after_cursor is None:
            raise CollaborationContractError("awareness_delta_page_unbound")
        if audience not in working_set.agent_sessions:
            raise CollaborationContractError("awareness_audience_not_registered")
        if audience.state != "active":
            raise CollaborationContractError("awareness_audience_inactive")
        observed = _parse_timestamp(working_set.observed_at)
        if audience.expires_at is not None and _parse_timestamp(audience.expires_at) <= observed:
            raise CollaborationContractError("awareness_audience_expired")
        if (
            deltas.project != working_set.project
            or deltas.coordination_session_id != working_set.coordination_session_id
        ):
            raise CollaborationContractError("awareness_scope_mismatch")

        sources = (
            (active_agents, working_set.agent_sessions, "awareness_agent_source_mismatch"),
            (visible_work, working_set.leased_work, "awareness_work_source_mismatch"),
            (visible_results, working_set.accepted_results, "awareness_result_source_mismatch"),
            (visible_blockers, working_set.blockers, "awareness_blocker_source_mismatch"),
            (visible_conflicts, working_set.conflicts, "awareness_conflict_source_mismatch"),
        )
        for values, source_values, code in sources:
            if any(value not in source_values for value in values):
                raise CollaborationContractError(code)

        full_view = audience.identity.role in _FULL_AWARENESS_ROLES
        projection = _freeze_json(
            {
                "visibility": {
                    "mode": "full-role-view" if full_view else "bounded-role-view",
                    "work_objectives": "redacted",
                    "event_payloads": "redacted",
                    "agent_capabilities": "redacted",
                },
                "agent_presence": [_agent_awareness_view(item) for item in active_agents],
                "leased_work": [_work_awareness_view(item) for item in visible_work],
                "accepted_artifacts": [
                    artifact
                    for result in visible_results
                    for artifact in _artifact_awareness_views(result)
                ],
                "blockers": [_event_awareness_view(item) for item in visible_blockers],
                "conflicts": [_event_awareness_view(item) for item in visible_conflicts],
                "peer_deltas": [_event_awareness_view(item) for item in deltas.events],
            }
        )
        if not isinstance(projection, Mapping):
            raise CollaborationContractError("awareness_projection_invalid")

        instance = object.__new__(cls)
        object.__setattr__(instance, "project", working_set.project)
        object.__setattr__(
            instance,
            "coordination_session_id",
            working_set.coordination_session_id,
        )
        object.__setattr__(instance, "audience_agent_id", audience.identity.agent_id)
        object.__setattr__(instance, "audience_role", audience.identity.role)
        object.__setattr__(instance, "audience_session_id", audience.session_id)
        object.__setattr__(instance, "observed_at", working_set.observed_at)
        object.__setattr__(instance, "goal_summary", working_set.goal_summary)
        object.__setattr__(instance, "plan_revision", working_set.plan_revision)
        object.__setattr__(instance, "after_cursor", deltas.after_cursor)
        object.__setattr__(instance, "next_cursor", deltas.next_cursor)
        object.__setattr__(instance, "has_more", deltas.has_more)
        object.__setattr__(instance, "working_set_sha256", working_set.content_sha256)
        object.__setattr__(instance, "event_page_sha256", deltas.content_sha256)
        object.__setattr__(instance, "_projection", projection)
        if len(instance.canonical_json().encode("utf-8")) > _MAX_AWARENESS_BYTES:
            raise CollaborationContractError("awareness_projection_too_large")
        return instance

    @property
    def audience(self) -> AgentIdentity:
        """Return the redacted audience identity; authority stays session-bound."""

        return AgentIdentity(agent_id=self.audience_agent_id, role=self.audience_role)

    @property
    def projection(self) -> Mapping[str, object]:
        """Expose the immutable, factory-built payload for read-only composers."""

        return self._projection

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": AGENT_AWARENESS_SCHEMA,
            "authority": "non-authoritative-projection",
            "canonical_memory_effect": "none",
            "server_feed_binding": "pr4-process-local-server-bound",
            "persistent_source": "deferred-to-pr5",
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "audience": {
                "agent_id": self.audience_agent_id,
                "role": self.audience_role,
                "session_id": self.audience_session_id,
            },
            "observed_at": self.observed_at,
            "goal_summary": self.goal_summary,
            "plan_revision": self.plan_revision,
            "source_bindings": {
                "working_set_sha256": self.working_set_sha256,
                "event_page_sha256": self.event_page_sha256,
            },
            "projection": _thaw_json(self._projection),
            "cursor": {
                "after": self.after_cursor.to_dict(),
                "next": self.next_cursor.to_dict(),
                "has_more": self.has_more,
            },
        }


@dataclass(frozen=True, slots=True)
class ProjectWorkingSet(_JsonContract):
    """Rebuildable project snapshot used only to derive awareness read views.

    The working set owns no adapter, database, listener, Hook, or MCP wiring.
    PR4's authenticated process-local server runtime may rebuild and restamp it
    from bound sources; durable canonical adapters and restart recovery remain
    PR5 work.  This value only validates bounded inputs and produces a
    non-authoritative view for one declared Agent audience.
    """

    coordination: CoordinationSession
    goal_summary: str
    plan_revision: str
    observed_at: str
    agent_sessions: tuple[AgentSession, ...] = ()
    leased_work: tuple[WorkReceipt, ...] = ()
    accepted_results: tuple[ResultReceipt, ...] = ()
    blockers: tuple[CollaborationEvent, ...] = ()
    conflicts: tuple[CollaborationEvent, ...] = ()

    def __post_init__(self) -> None:
        _require_type(self.coordination, CoordinationSession, "working_set_session_invalid")
        goal_summary = _require_public_text(
            self.goal_summary,
            "working_set_goal_invalid",
            max_bytes=_MAX_SUMMARY_BYTES,
        )
        plan_revision = _require_identifier(self.plan_revision, "plan_revision")
        observed_at = _timestamp(self.observed_at, "working_set_observed_at_invalid")
        if _parse_timestamp(observed_at) < _parse_timestamp(self.coordination.created_at):
            raise CollaborationContractError("working_set_observed_before_session")

        agent_sessions = self._scoped_values(
            self.agent_sessions,
            expected=AgentSession,
            field_name="working_set_agent_sessions",
        )
        leased_work = self._scoped_values(
            self.leased_work,
            expected=WorkReceipt,
            field_name="working_set_leased_work",
        )
        accepted_results = self._scoped_values(
            self.accepted_results,
            expected=ResultReceipt,
            field_name="working_set_accepted_results",
        )
        blockers = self._scoped_values(
            self.blockers,
            expected=CollaborationEvent,
            field_name="working_set_blockers",
        )
        conflicts = self._scoped_values(
            self.conflicts,
            expected=CollaborationEvent,
            field_name="working_set_conflicts",
        )

        self._require_unique(
            agent_sessions,
            key=lambda item: item.session_id,
            code="working_set_agent_session_duplicate",
        )
        self._require_unique(
            leased_work,
            key=lambda item: item.work_item_id,
            code="working_set_work_item_duplicate",
        )
        self._require_unique(
            accepted_results,
            key=lambda item: item.receipt_id,
            code="working_set_result_duplicate",
        )
        self._require_unique(
            accepted_results,
            key=lambda item: item.work_item_id,
            code="working_set_result_work_duplicate",
        )
        self._require_unique(
            (*blockers, *conflicts),
            key=lambda item: item.event_id,
            code="working_set_event_duplicate",
        )

        observed = _parse_timestamp(observed_at)
        for session in agent_sessions:
            if _parse_timestamp(session.last_heartbeat_at) > observed:
                raise CollaborationContractError("working_set_agent_from_future")
        for work in leased_work:
            if _parse_timestamp(work.issued_at) > observed:
                raise CollaborationContractError("working_set_work_from_future")
        work_by_id = {work.work_item_id: work for work in leased_work}
        for result in accepted_results:
            if result.outcome != "completed" or not result.artifact_refs:
                raise CollaborationContractError("working_set_accepted_result_invalid")
            work = work_by_id.get(result.work_item_id)
            if work is None:
                raise CollaborationContractError("working_set_accepted_result_work_missing")
            if result.work_receipt_sha256 != work.content_sha256:
                raise CollaborationContractError("working_set_accepted_result_digest_mismatch")
            if result.submitted_by != work.assigned_agent:
                raise CollaborationContractError("working_set_accepted_result_submitter_mismatch")
            submitted = _parse_timestamp(result.submitted_at)
            if not (
                _parse_timestamp(work.issued_at) <= submitted <= _parse_timestamp(work.expires_at)
            ):
                raise CollaborationContractError("working_set_accepted_result_time_invalid")
            if submitted > observed:
                raise CollaborationContractError("working_set_result_from_future")
        for event in blockers:
            if event.event_type != "blocker.raised":
                raise CollaborationContractError("working_set_blocker_invalid")
            if _parse_timestamp(event.created_at) > observed:
                raise CollaborationContractError("working_set_event_from_future")
        for event in conflicts:
            if event.event_type != "conflict.detected":
                raise CollaborationContractError("working_set_conflict_invalid")
            if _parse_timestamp(event.created_at) > observed:
                raise CollaborationContractError("working_set_event_from_future")

        object.__setattr__(self, "goal_summary", goal_summary)
        object.__setattr__(self, "plan_revision", plan_revision)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(
            self,
            "agent_sessions",
            tuple(
                sorted(agent_sessions, key=lambda item: (item.identity.agent_id, item.session_id))
            ),
        )
        object.__setattr__(
            self,
            "leased_work",
            tuple(sorted(leased_work, key=lambda item: item.work_item_id)),
        )
        object.__setattr__(
            self,
            "accepted_results",
            tuple(sorted(accepted_results, key=lambda item: item.receipt_id)),
        )
        object.__setattr__(
            self,
            "blockers",
            tuple(sorted(blockers, key=lambda item: (item.created_at, item.event_id))),
        )
        object.__setattr__(
            self,
            "conflicts",
            tuple(sorted(conflicts, key=lambda item: (item.created_at, item.event_id))),
        )

    @property
    def project(self) -> ProjectScope:
        return self.coordination.project

    @property
    def coordination_session_id(self) -> str:
        return self.coordination.session_id

    def project_for(
        self,
        *,
        audience: AgentSession,
        deltas: EventPage,
    ) -> AgentAwarenessProjection:
        """Return one bounded role-aware view from an already-read event page."""

        _require_type(audience, AgentSession, "awareness_audience_session_invalid")
        _require_type(deltas, EventPage, "awareness_delta_page_invalid")
        if (
            audience.project != self.project
            or audience.coordination_session_id != self.coordination_session_id
        ):
            raise CollaborationContractError("awareness_audience_scope_mismatch")
        if audience not in self.agent_sessions:
            raise CollaborationContractError("awareness_audience_not_registered")
        if audience.state != "active":
            raise CollaborationContractError("awareness_audience_inactive")
        observed = _parse_timestamp(self.observed_at)
        if audience.expires_at is not None and _parse_timestamp(audience.expires_at) <= observed:
            raise CollaborationContractError("awareness_audience_expired")
        if deltas.after_cursor is None:
            raise CollaborationContractError("awareness_delta_page_unbound")
        if (
            deltas.project != self.project
            or deltas.coordination_session_id != self.coordination_session_id
        ):
            raise CollaborationContractError("awareness_scope_mismatch")
        if len(deltas.events) > _MAX_AWARENESS_DELTAS:
            raise CollaborationContractError("awareness_delta_page_too_large")
        if any(not self._visible_to(event, audience.identity) for event in deltas.events):
            raise CollaborationContractError("awareness_delta_audience_mismatch")

        active_agents = tuple(
            session
            for session in self.agent_sessions
            if session.state != "closed"
            and (session.expires_at is None or _parse_timestamp(session.expires_at) > observed)
        )
        active_work = tuple(
            work for work in self.leased_work if _parse_timestamp(work.expires_at) > observed
        )
        visible_blockers = tuple(
            event
            for event in self.blockers
            if self._event_current(event, observed) and self._visible_to(event, audience.identity)
        )
        visible_conflicts = tuple(
            event
            for event in self.conflicts
            if self._event_current(event, observed) and self._visible_to(event, audience.identity)
        )

        full_view = audience.identity.role in _FULL_AWARENESS_ROLES
        visible_event_work_ids = {
            event.work_item_id
            for event in (*visible_blockers, *visible_conflicts, *deltas.events)
            if event.work_item_id is not None
        }
        if full_view:
            visible_work_ids = {work.work_item_id for work in active_work}
        else:
            owned_work = tuple(
                work
                for work in active_work
                if work.assigned_agent.agent_id == audience.identity.agent_id
            )
            visible_work_ids = {work.work_item_id for work in owned_work}
            visible_work_ids.update(visible_event_work_ids)
            for work in owned_work:
                visible_work_ids.update(work.dependency_work_ids)
        visible_work = tuple(work for work in active_work if work.work_item_id in visible_work_ids)
        visible_results = tuple(
            result
            for result in self.accepted_results
            if full_view
            or result.submitted_by.agent_id == audience.identity.agent_id
            or result.work_item_id in visible_work_ids
        )

        return AgentAwarenessProjection._from_sources(
            working_set=self,
            audience=audience,
            deltas=deltas,
            active_agents=active_agents,
            visible_work=visible_work,
            visible_results=visible_results,
            visible_blockers=visible_blockers,
            visible_conflicts=visible_conflicts,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROJECT_WORKING_SET_SCHEMA,
            "authority": "non-authoritative-rebuildable-source",
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "goal_summary": self.goal_summary,
            "plan_revision": self.plan_revision,
            "observed_at": self.observed_at,
            "counts": {
                "agent_sessions": len(self.agent_sessions),
                "leased_work": len(self.leased_work),
                "accepted_results": len(self.accepted_results),
                "blockers": len(self.blockers),
                "conflicts": len(self.conflicts),
            },
            "source_digests": {
                "coordination": self.coordination.content_sha256,
                "agent_sessions": [item.content_sha256 for item in self.agent_sessions],
                "leased_work": [item.content_sha256 for item in self.leased_work],
                "accepted_results": [item.content_sha256 for item in self.accepted_results],
                "blockers": [item.content_sha256 for item in self.blockers],
                "conflicts": [item.content_sha256 for item in self.conflicts],
            },
            "server_feed_binding": "pr4-process-local-server-bound",
            "persistent_source": "deferred-to-pr5",
            "canonical_memory_effect": "none",
        }

    def _scoped_values(
        self,
        value: object,
        *,
        expected: type[object],
        field_name: str,
    ) -> tuple[object, ...]:
        values = tuple(_sequence(value, field_name))
        if len(values) > _MAX_WORKING_SET_ITEMS:
            raise CollaborationContractError(f"{field_name}_too_many")
        for item in values:
            if not isinstance(item, expected):
                raise CollaborationContractError(f"{field_name}_invalid")
            if (
                item.project != self.coordination.project
                or item.coordination_session_id != self.coordination.session_id
            ):
                raise CollaborationContractError("working_set_scope_mismatch")
        return values

    @staticmethod
    def _require_unique(
        values: Sequence[object],
        *,
        key: Callable[[object], object],
        code: str,
    ) -> None:
        resolved = [key(item) for item in values]
        if len(resolved) != len(set(resolved)):
            raise CollaborationContractError(code)

    @staticmethod
    def _visible_to(event: CollaborationEvent, audience: AgentIdentity) -> bool:
        if not event.audience_roles and not event.audience_agent_ids:
            return True
        return (
            event.actor.agent_id == audience.agent_id
            or audience.role in event.audience_roles
            or audience.agent_id in event.audience_agent_ids
        )

    @staticmethod
    def _event_current(event: CollaborationEvent, observed: datetime) -> bool:
        return event.expires_at is None or _parse_timestamp(event.expires_at) > observed


__all__ = [
    "AGENT_AWARENESS_SCHEMA",
    "PROJECT_WORKING_SET_SCHEMA",
    "AgentAwarenessProjection",
    "ProjectWorkingSet",
]
