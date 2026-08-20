"""Immutable, non-secret value contracts for project-scoped Agent collaboration.

Live collaboration is deliberately separate from canonical memory.  These
contracts describe bounded public coordination facts, work assignments, and
result receipts.  They reject credential-like fields and explicit opaque
lease-capability or private-reasoning channels; callers must not disguise such
content under unrelated field names.  This module does not implement an Agent
registry, transport wiring, or a mutable work-lease lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from plastic_promise.core.project_identity import canonical_project_id

from .canonical_time import canonical_text, parse_utc, server_now_text

if TYPE_CHECKING:
    from datetime import datetime

COLLABORATION_EVENT_SCHEMA = "collaboration-event/v1"
COLLABORATION_WORK_SCHEMA = "collaboration-work/v1"
COLLABORATION_RESULT_SCHEMA = "collaboration-result/v2"

_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_ROLE_RE = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
_EVENT_TYPES = frozenset(
    {
        "agent.joined",
        "agent.heartbeat",
        "agent.intent_declared",
        "agent.closed",
        "work.claimed",
        "work.progressed",
        "work.submitted",
        "work.accepted",
        "work.released",
        "finding.published",
        "assumption.published",
        "artifact.published",
        "blocker.raised",
        "conflict.detected",
        "workflow.stage_started",
        "workflow.stage_completed",
        "workflow.stage_blocked",
        "workflow.receipt_submitted",
    }
)
_AGENT_STATES = frozenset({"registered", "active", "idle", "stale", "closed"})
_RESULT_OUTCOMES = frozenset({"completed", "blocked", "failed", "cancelled"})
_SECRET_EXACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "authorization_header",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)
_SECRET_COMPACT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorizationheader",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "idtoken",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessiontoken",
        "token",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk|rk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_FORBIDDEN_SEMANTIC_COMPACT_KEYS = frozenset(
    {
        "authoritycapability",
        "authorityhandle",
        "chainofthought",
        "fullprompt",
        "hiddenreasoning",
        "hiddenthoughts",
        "internalmonologue",
        "leasecapability",
        "leasehandle",
        "opaquecapability",
        "privatereasoning",
        "privatethoughts",
        "prompttranscript",
        "rawprompt",
        "reasoningtrace",
        "reasoningtranscript",
        "scratchpad",
    }
)
_FORBIDDEN_SEMANTIC_TOKEN_SEQUENCES = (
    ("authority", "capability"),
    ("authority", "handle"),
    ("chain", "of", "thought"),
    ("hidden", "reasoning"),
    ("hidden", "thoughts"),
    ("internal", "monologue"),
    ("lease", "capability"),
    ("lease", "handle"),
    ("opaque", "capability"),
    ("private", "reasoning"),
    ("private", "thoughts"),
    ("reasoning", "trace"),
    ("reasoning", "transcript"),
    ("scratchpad",),
)
_COLLABORATION_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "project_id",
        "coordination_session_id",
        "actor",
        "event_type",
        "summary",
        "created_at",
        "expires_at",
        "causal_parent_event_id",
        "work_item_id",
        "subject_refs",
        "evidence_refs",
        "audience",
        "payload",
    }
)
_AGENT_IDENTITY_FIELDS = frozenset({"agent_id", "role", "parent_agent_id", "capabilities"})
_AUDIENCE_FIELDS = frozenset({"roles", "agent_ids"})
_MAX_PUBLIC_TEXT_BYTES = 8 * 1024
_MAX_SUMMARY_BYTES = 4 * 1024
_MAX_OBJECT_BYTES = 64 * 1024
_MAX_COLLECTION_ITEMS = 64
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 512
_MAX_INTEGER = (1 << 63) - 1


class CollaborationContractError(ValueError):
    """A stable, non-sensitive collaboration contract error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _JsonContract:
    """Stable JSON projection shared by immutable collaboration contracts."""

    def to_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def content_sha256(self) -> str:
        encoded = self.canonical_json().encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectScope(_JsonContract):
    """Canonical project identity used by every collaboration operation."""

    project_id: str

    def __post_init__(self) -> None:
        project_id = _require_project_id(self.project_id)
        object.__setattr__(self, "project_id", project_id)

    def to_dict(self) -> dict[str, object]:
        return {"project_id": self.project_id}


@dataclass(frozen=True, slots=True)
class AgentIdentity(_JsonContract):
    """Public Agent identity value; it is not proof of registration or authority."""

    agent_id: str
    role: str
    parent_agent_id: str | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _require_identifier(self.agent_id, "agent_id"))
        object.__setattr__(self, "role", _require_role(self.role))
        if self.parent_agent_id is not None:
            object.__setattr__(
                self,
                "parent_agent_id",
                _require_identifier(self.parent_agent_id, "parent_agent_id"),
            )
        capabilities = _bounded_strings(
            self.capabilities,
            field_name="agent_capabilities",
            max_items=32,
            max_bytes=128,
            identifiers=True,
            sort_values=True,
        )
        object.__setattr__(self, "capabilities", capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "parent_agent_id": self.parent_agent_id,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class CoordinationSession(_JsonContract):
    """Immutable description of one bounded project collaboration session."""

    session_id: str
    project: ProjectScope
    objective: str
    created_at: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "coordination_project_invalid")
        object.__setattr__(
            self, "session_id", _require_identifier(self.session_id, "coordination_session_id")
        )
        object.__setattr__(
            self,
            "objective",
            _require_public_text(
                self.objective,
                "coordination_objective_invalid",
                max_bytes=_MAX_PUBLIC_TEXT_BYTES,
            ),
        )
        created_at = _timestamp(self.created_at, "coordination_created_at_invalid")
        expires_at = _optional_timestamp(self.expires_at, "coordination_expires_at_invalid")
        _require_expiry(created_at, expires_at, "coordination_expiry_invalid")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "project_id": self.project.project_id,
            "objective": self.objective,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class AgentSession(_JsonContract):
    """Immutable presence snapshot, not a persisted AgentRegistry entry."""

    session_id: str
    identity: AgentIdentity
    project: ProjectScope
    coordination_session_id: str
    state: str
    started_at: str
    last_heartbeat_at: str
    expires_at: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.identity, AgentIdentity, "agent_session_identity_invalid")
        _require_type(self.project, ProjectScope, "agent_session_project_invalid")
        object.__setattr__(
            self, "session_id", _require_identifier(self.session_id, "agent_session_id")
        )
        object.__setattr__(
            self,
            "coordination_session_id",
            _require_identifier(self.coordination_session_id, "coordination_session_id"),
        )
        state = str(self.state or "").strip().casefold()
        if state not in _AGENT_STATES:
            raise CollaborationContractError("agent_session_state_invalid")
        started_at = _timestamp(self.started_at, "agent_session_started_at_invalid")
        heartbeat = _timestamp(self.last_heartbeat_at, "agent_session_last_heartbeat_at_invalid")
        expires_at = _optional_timestamp(self.expires_at, "agent_session_expires_at_invalid")
        if _parse_timestamp(heartbeat) < _parse_timestamp(started_at):
            raise CollaborationContractError("agent_session_heartbeat_before_start")
        _require_expiry(started_at, expires_at, "agent_session_expiry_invalid")
        if expires_at is not None and _parse_timestamp(heartbeat) > _parse_timestamp(expires_at):
            raise CollaborationContractError("agent_session_heartbeat_after_expiry")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "last_heartbeat_at", heartbeat)
        object.__setattr__(self, "expires_at", expires_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "identity": self.identity.to_dict(),
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "state": self.state,
            "started_at": self.started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class CollaborationEvent(_JsonContract):
    """One append-only, audience-scoped collaboration fact."""

    event_id: str
    project: ProjectScope
    coordination_session_id: str
    actor: AgentIdentity
    event_type: str
    summary: str
    created_at: str
    expires_at: str | None = None
    causal_parent_event_id: str | None = None
    work_item_id: str | None = None
    subject_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    audience_roles: tuple[str, ...] = ()
    audience_agent_ids: tuple[str, ...] = ()
    payload: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "collaboration_event_project_invalid")
        _require_type(self.actor, AgentIdentity, "collaboration_event_actor_invalid")
        object.__setattr__(self, "event_id", _require_identifier(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "coordination_session_id",
            _require_identifier(self.coordination_session_id, "coordination_session_id"),
        )
        event_type = str(self.event_type or "").strip().casefold()
        if event_type not in _EVENT_TYPES:
            raise CollaborationContractError("collaboration_event_type_invalid")
        summary = _require_public_text(
            self.summary,
            "collaboration_event_summary_invalid",
            max_bytes=_MAX_SUMMARY_BYTES,
        )
        created_at = _timestamp(self.created_at, "collaboration_event_created_at_invalid")
        expires_at = _optional_timestamp(self.expires_at, "collaboration_event_expires_at_invalid")
        _require_expiry(created_at, expires_at, "collaboration_event_expiry_invalid")
        parent = _optional_identifier(self.causal_parent_event_id, "causal_parent_event_id")
        if parent == self.event_id:
            raise CollaborationContractError("collaboration_event_self_parent")
        work_item_id = _optional_identifier(self.work_item_id, "work_item_id")
        subject_refs = _bounded_strings(
            self.subject_refs,
            field_name="subject_refs",
            max_items=32,
            max_bytes=1024,
        )
        evidence_refs = _bounded_strings(
            self.evidence_refs,
            field_name="evidence_refs",
            max_items=32,
            max_bytes=1024,
        )
        audience_role_values = _sequence(self.audience_roles, "audience_roles")
        if len(audience_role_values) > 32:
            raise CollaborationContractError("audience_roles_too_many")
        normalized_audience_roles = [_require_role(role) for role in audience_role_values]
        if len(set(normalized_audience_roles)) != len(normalized_audience_roles):
            raise CollaborationContractError("audience_roles_duplicate")
        audience_roles = tuple(sorted(normalized_audience_roles))
        audience_agent_ids = _bounded_strings(
            self.audience_agent_ids,
            field_name="audience_agent_ids",
            max_items=32,
            max_bytes=256,
            identifiers=True,
            sort_values=True,
        )
        payload = _freeze_json(self.payload)
        if not isinstance(payload, Mapping):
            raise CollaborationContractError("collaboration_event_payload_invalid")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "causal_parent_event_id", parent)
        object.__setattr__(self, "work_item_id", work_item_id)
        object.__setattr__(self, "subject_refs", subject_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "audience_roles", audience_roles)
        object.__setattr__(self, "audience_agent_ids", audience_agent_ids)
        object.__setattr__(self, "payload", payload)
        _require_projection_size(self.to_dict(), "collaboration_event_too_large")

    @classmethod
    def create(
        cls,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        actor: AgentIdentity,
        event_type: str,
        summary: str,
        created_at: str | None = None,
        **kwargs: object,
    ) -> CollaborationEvent:
        """Create an event with a non-semantic random identity and UTC timestamp."""

        return cls(
            event_id=f"event:{uuid.uuid4().hex}",
            project=project,
            coordination_session_id=coordination_session_id,
            actor=actor,
            event_type=event_type,
            summary=summary,
            created_at=created_at or utc_now_text(),
            **kwargs,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CollaborationEvent:
        """Rebuild a validated event from its public JSON projection."""

        if not isinstance(value, Mapping):
            raise CollaborationContractError("collaboration_event_projection_invalid")
        _require_projection_keys(
            value,
            _COLLABORATION_EVENT_FIELDS,
            "collaboration_event_projection_invalid",
        )
        if value.get("schema_version") != COLLABORATION_EVENT_SCHEMA:
            raise CollaborationContractError("collaboration_event_schema_invalid")
        actor = value.get("actor")
        audience = value.get("audience")
        if not isinstance(actor, Mapping) or not isinstance(audience, Mapping):
            raise CollaborationContractError("collaboration_event_projection_invalid")
        _require_projection_keys(
            actor,
            _AGENT_IDENTITY_FIELDS,
            "collaboration_event_projection_invalid",
        )
        _require_projection_keys(
            audience,
            _AUDIENCE_FIELDS,
            "collaboration_event_projection_invalid",
        )
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise CollaborationContractError("collaboration_event_projection_invalid")
        return cls(
            event_id=value.get("event_id"),  # type: ignore[arg-type]
            project=ProjectScope(value.get("project_id")),  # type: ignore[arg-type]
            coordination_session_id=value.get("coordination_session_id"),  # type: ignore[arg-type]
            actor=AgentIdentity(
                agent_id=actor.get("agent_id"),  # type: ignore[arg-type]
                role=actor.get("role"),  # type: ignore[arg-type]
                parent_agent_id=actor.get("parent_agent_id"),  # type: ignore[arg-type]
                capabilities=_projection_tuple(actor.get("capabilities"), "actor_capabilities"),
            ),
            event_type=value.get("event_type"),  # type: ignore[arg-type]
            summary=value.get("summary"),  # type: ignore[arg-type]
            created_at=value.get("created_at"),  # type: ignore[arg-type]
            expires_at=value.get("expires_at"),  # type: ignore[arg-type]
            causal_parent_event_id=value.get("causal_parent_event_id"),  # type: ignore[arg-type]
            work_item_id=value.get("work_item_id"),  # type: ignore[arg-type]
            subject_refs=_projection_tuple(value.get("subject_refs"), "subject_refs"),
            evidence_refs=_projection_tuple(value.get("evidence_refs"), "evidence_refs"),
            audience_roles=_projection_tuple(audience.get("roles"), "audience_roles"),
            audience_agent_ids=_projection_tuple(
                audience.get("agent_ids"),
                "audience_agent_ids",
            ),
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_EVENT_SCHEMA,
            "event_id": self.event_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "actor": self.actor.to_dict(),
            "event_type": self.event_type,
            "summary": self.summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "causal_parent_event_id": self.causal_parent_event_id,
            "work_item_id": self.work_item_id,
            "subject_refs": list(self.subject_refs),
            "evidence_refs": list(self.evidence_refs),
            "audience": {
                "roles": list(self.audience_roles),
                "agent_ids": list(self.audience_agent_ids),
            },
            "payload": _thaw_json(self.payload),
        }


@dataclass(frozen=True, slots=True)
class EventCursor(_JsonContract):
    """Project/session-bound keyset cursor; it cannot be replayed across scopes."""

    project: ProjectScope
    coordination_session_id: str
    sequence: int = 0

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "event_cursor_project_invalid")
        object.__setattr__(
            self,
            "coordination_session_id",
            _require_identifier(self.coordination_session_id, "coordination_session_id"),
        )
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
            or self.sequence > _MAX_INTEGER
        ):
            raise CollaborationContractError("event_cursor_sequence_invalid")

    @classmethod
    def start(cls, project: ProjectScope, coordination_session_id: str) -> EventCursor:
        return cls(project=project, coordination_session_id=coordination_session_id, sequence=0)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class EventPage(_JsonContract):
    """One bounded audience projection from the collaboration event log.

    ``after_cursor`` binds the page to the exact read position that produced
    it.  Legacy event-log callers may still construct an unbound page, but an
    unbound page is not eligible for Agent awareness projection.
    """

    project: ProjectScope
    coordination_session_id: str
    events: tuple[CollaborationEvent, ...]
    next_cursor: EventCursor
    has_more: bool
    after_cursor: EventCursor | None = None

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "event_page_project_invalid")
        session_id = _require_identifier(self.coordination_session_id, "coordination_session_id")
        _require_type(self.next_cursor, EventCursor, "event_page_cursor_invalid")
        if (
            self.next_cursor.project != self.project
            or self.next_cursor.coordination_session_id != session_id
        ):
            raise CollaborationContractError("event_page_cursor_scope_mismatch")
        if self.after_cursor is not None:
            _require_type(self.after_cursor, EventCursor, "event_page_after_cursor_invalid")
            if (
                self.after_cursor.project != self.project
                or self.after_cursor.coordination_session_id != session_id
            ):
                raise CollaborationContractError("event_page_cursor_scope_mismatch")
            if self.next_cursor.sequence < self.after_cursor.sequence:
                raise CollaborationContractError("event_page_cursor_regression")
        event_values = _sequence(self.events, "event_page_events")
        if len(event_values) > 200:
            raise CollaborationContractError("event_page_events_invalid")
        events = tuple(event_values)
        if any(not isinstance(event, CollaborationEvent) for event in events):
            raise CollaborationContractError("event_page_events_invalid")
        if any(
            event.project != self.project or event.coordination_session_id != session_id
            for event in events
        ):
            raise CollaborationContractError("event_page_event_scope_mismatch")
        if not isinstance(self.has_more, bool):
            raise CollaborationContractError("event_page_has_more_invalid")
        if self.after_cursor is not None:
            if events and self.next_cursor.sequence == self.after_cursor.sequence:
                raise CollaborationContractError("event_page_cursor_did_not_advance")
            if not events and self.next_cursor != self.after_cursor:
                raise CollaborationContractError("event_page_empty_cursor_gap")
        object.__setattr__(self, "coordination_session_id", session_id)
        object.__setattr__(self, "events", events)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "events": [event.to_dict() for event in self.events],
            "after_cursor": (
                self.after_cursor.to_dict() if self.after_cursor is not None else None
            ),
            "next_cursor": self.next_cursor.to_dict(),
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class WorkReceipt(_JsonContract):
    """Immutable assignment description; it grants no authority or lease capability."""

    receipt_id: str
    work_item_id: str
    project: ProjectScope
    coordination_session_id: str
    assigned_agent: AgentIdentity
    objective: str
    fencing_generation: int
    issued_at: str
    expires_at: str
    dependency_work_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "work_receipt_project_invalid")
        _require_type(self.assigned_agent, AgentIdentity, "work_receipt_agent_invalid")
        object.__setattr__(
            self, "receipt_id", _require_identifier(self.receipt_id, "work_receipt_id")
        )
        object.__setattr__(
            self, "work_item_id", _require_identifier(self.work_item_id, "work_item_id")
        )
        object.__setattr__(
            self,
            "coordination_session_id",
            _require_identifier(self.coordination_session_id, "coordination_session_id"),
        )
        object.__setattr__(
            self,
            "objective",
            _require_public_text(
                self.objective, "work_receipt_objective_invalid", max_bytes=_MAX_PUBLIC_TEXT_BYTES
            ),
        )
        if (
            isinstance(self.fencing_generation, bool)
            or not isinstance(self.fencing_generation, int)
            or self.fencing_generation < 1
            or self.fencing_generation > _MAX_INTEGER
        ):
            raise CollaborationContractError("work_receipt_fencing_generation_invalid")
        issued_at = _timestamp(self.issued_at, "work_receipt_issued_at_invalid")
        expires_at = _timestamp(self.expires_at, "work_receipt_expires_at_invalid")
        _require_expiry(issued_at, expires_at, "work_receipt_expiry_invalid")
        dependencies = _bounded_strings(
            self.dependency_work_ids,
            field_name="dependency_work_ids",
            max_items=_MAX_COLLECTION_ITEMS,
            max_bytes=256,
            identifiers=True,
            sort_values=True,
        )
        if self.work_item_id in dependencies:
            raise CollaborationContractError("work_receipt_self_dependency")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "dependency_work_ids", dependencies)
        _require_projection_size(self.to_dict(), "work_receipt_too_large")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_WORK_SCHEMA,
            "receipt_id": self.receipt_id,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "assigned_agent": self.assigned_agent.to_dict(),
            "objective": self.objective,
            "fencing_generation": self.fencing_generation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "dependency_work_ids": list(self.dependency_work_ids),
        }


@dataclass(frozen=True, slots=True)
class ResultReceipt(_JsonContract):
    """Immutable work result shape; ``for_work`` binds it to an assignment receipt."""

    receipt_id: str
    work_item_id: str
    work_receipt_sha256: str
    project: ProjectScope
    coordination_session_id: str
    submitted_by: AgentIdentity
    outcome: str
    summary: str
    submitted_at: str
    role_assignment_sha256: str = ""
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    result: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "result_receipt_project_invalid")
        _require_type(self.submitted_by, AgentIdentity, "result_receipt_agent_invalid")
        object.__setattr__(
            self, "receipt_id", _require_identifier(self.receipt_id, "result_receipt_id")
        )
        object.__setattr__(
            self, "work_item_id", _require_identifier(self.work_item_id, "work_item_id")
        )
        if not isinstance(self.work_receipt_sha256, str) or not _SHA256_RE.fullmatch(
            self.work_receipt_sha256
        ):
            raise CollaborationContractError("result_work_receipt_sha256_invalid")
        object.__setattr__(
            self,
            "coordination_session_id",
            _require_identifier(self.coordination_session_id, "coordination_session_id"),
        )
        outcome = str(self.outcome or "").strip().casefold()
        if outcome not in _RESULT_OUTCOMES:
            raise CollaborationContractError("result_outcome_invalid")
        summary = _require_public_text(
            self.summary, "result_summary_invalid", max_bytes=_MAX_SUMMARY_BYTES
        )
        submitted_at = _timestamp(self.submitted_at, "result_submitted_at_invalid")
        role_assignment_sha256 = str(self.role_assignment_sha256 or "").strip()
        if role_assignment_sha256 and _SHA256_RE.fullmatch(role_assignment_sha256) is None:
            raise CollaborationContractError("result_role_assignment_sha256_invalid")
        artifact_refs = _bounded_strings(
            self.artifact_refs,
            field_name="artifact_refs",
            max_items=32,
            max_bytes=1024,
        )
        evidence_refs = _bounded_strings(
            self.evidence_refs,
            field_name="result_evidence_refs",
            max_items=32,
            max_bytes=1024,
        )
        result = _freeze_json(self.result)
        if not isinstance(result, Mapping):
            raise CollaborationContractError("result_projection_invalid")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "submitted_at", submitted_at)
        object.__setattr__(self, "role_assignment_sha256", role_assignment_sha256)
        object.__setattr__(self, "artifact_refs", artifact_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "result", result)
        _require_projection_size(self.to_dict(), "result_receipt_too_large")

    @classmethod
    def for_work(
        cls,
        work: WorkReceipt,
        *,
        receipt_id: str,
        submitted_by: AgentIdentity,
        outcome: str,
        summary: str,
        submitted_at: str | None = None,
        **kwargs: object,
    ) -> ResultReceipt:
        """Create a result that cannot silently drift from its work scope."""

        _require_type(work, WorkReceipt, "result_work_receipt_invalid")
        _require_type(submitted_by, AgentIdentity, "result_receipt_agent_invalid")
        if submitted_by != work.assigned_agent:
            raise CollaborationContractError("result_submitter_not_assignee")
        return cls(
            receipt_id=receipt_id,
            work_item_id=work.work_item_id,
            work_receipt_sha256=work.content_sha256,
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            submitted_by=submitted_by,
            outcome=outcome,
            summary=summary,
            submitted_at=submitted_at or utc_now_text(),
            **kwargs,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_RESULT_SCHEMA,
            "receipt_id": self.receipt_id,
            "work_item_id": self.work_item_id,
            "work_receipt_sha256": self.work_receipt_sha256,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "submitted_by": self.submitted_by.to_dict(),
            "outcome": self.outcome,
            "summary": self.summary,
            "submitted_at": self.submitted_at,
            "role_assignment_sha256": self.role_assignment_sha256,
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "result": _thaw_json(self.result),
        }


def utc_now_text() -> str:
    """Return canonical UTC text used by collaboration contracts."""

    return server_now_text()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_type(value: object, expected: type[object], code: str) -> None:
    if not isinstance(value, expected):
        raise CollaborationContractError(code)


def _require_project_id(value: object) -> str:
    if isinstance(value, str) and value.casefold() == "project:unknown":
        raise CollaborationContractError("project_unknown_forbidden")
    project_id = canonical_project_id(value)
    if not project_id:
        raise CollaborationContractError("project_id_invalid")
    if _secret_value(project_id):
        raise CollaborationContractError("secret_value_forbidden")
    return project_id


def _require_identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _SAFE_IDENTIFIER_RE.fullmatch(value)
    ):
        raise CollaborationContractError(f"{field_name}_invalid")
    if _secret_value(value):
        raise CollaborationContractError("secret_value_forbidden")
    return value


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, field_name)


def _require_role(value: object) -> str:
    if not isinstance(value, str):
        raise CollaborationContractError("agent_role_invalid")
    normalized = value.strip().casefold()
    if normalized != value or not _ROLE_RE.fullmatch(normalized):
        raise CollaborationContractError("agent_role_invalid")
    return normalized


def _require_public_text(value: object, code: str, *, max_bytes: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CollaborationContractError(code)
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes or any(
        ord(character) < 32 and character not in "\n\t" for character in value
    ):
        raise CollaborationContractError(code)
    if _secret_value(value):
        raise CollaborationContractError("secret_value_forbidden")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CollaborationContractError(f"{field_name}_invalid")
    return value


def _bounded_strings(
    value: object,
    *,
    field_name: str,
    max_items: int,
    max_bytes: int,
    identifiers: bool = False,
    sort_values: bool = False,
) -> tuple[str, ...]:
    values = _sequence(value, field_name)
    if len(values) > max_items:
        raise CollaborationContractError(f"{field_name}_too_many")
    normalized: list[str] = []
    for item in values:
        if identifiers:
            text = _require_identifier(item, field_name)
        else:
            text = _require_public_text(
                item,
                f"{field_name}_invalid",
                max_bytes=max_bytes,
            )
        if len(text.encode("utf-8")) > max_bytes:
            raise CollaborationContractError(f"{field_name}_invalid")
        normalized.append(text)
    if len(set(normalized)) != len(normalized):
        raise CollaborationContractError(f"{field_name}_duplicate")
    if sort_values:
        normalized.sort()
    return tuple(normalized)


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise CollaborationContractError(code)
    try:
        return canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CollaborationContractError(code) from exc


def _optional_timestamp(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, code)


def _parse_timestamp(value: str) -> datetime:
    return parse_utc(value)


def _require_expiry(created_at: str, expires_at: str | None, code: str) -> None:
    if expires_at is not None and _parse_timestamp(expires_at) <= _parse_timestamp(created_at):
        raise CollaborationContractError(code)


def _normalized_secret_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")


def _secret_field(key: str) -> bool:
    normalized = _normalized_secret_key(key)
    compact = normalized.replace("_", "")
    if normalized in _SECRET_EXACT_KEYS or compact in _SECRET_COMPACT_KEYS:
        return True
    if normalized.endswith(("_sha256", "_digest", "_hash")):
        return False
    return normalized.endswith(_SECRET_KEY_SUFFIXES)


def _secret_value(value: str) -> bool:
    lowered = value.casefold()
    if re.search(r"-----begin [^-\r\n]{0,48}private key-----", lowered):
        return True
    if re.search(r"\bbearer\s+\S+", value, re.I):
        return True
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _forbidden_semantic_field(key: str) -> bool:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = _normalized_secret_key(camel_split)
    compact = normalized.replace("_", "")
    if compact in _FORBIDDEN_SEMANTIC_COMPACT_KEYS:
        return True
    tokens = tuple(token for token in normalized.split("_") if token)
    token_set = set(tokens)
    if "prompt" in token_set and token_set.intersection({"full", "raw", "transcript"}):
        return True
    return any(
        tokens[index : index + len(sequence)] == sequence
        for sequence in _FORBIDDEN_SEMANTIC_TOKEN_SEQUENCES
        for index in range(len(tokens) - len(sequence) + 1)
    )


def _require_projection_keys(
    value: Mapping[object, object],
    expected: frozenset[str],
    code: str,
) -> None:
    if set(value) != expected:
        raise CollaborationContractError(code)


def _projection_tuple(value: object, field_name: str) -> tuple[object, ...]:
    return tuple(_sequence(value, field_name))


def _freeze_json(value: object) -> object:
    counter = [0]

    def freeze(item: object, *, depth: int) -> object:
        counter[0] += 1
        if counter[0] > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise CollaborationContractError("collaboration_json_too_complex")
        if item is None or isinstance(item, (str, bool)):
            if isinstance(item, str):
                _require_public_text(
                    item,
                    "collaboration_json_text_invalid",
                    max_bytes=_MAX_PUBLIC_TEXT_BYTES,
                )
            return item
        if isinstance(item, int):
            if not -_MAX_INTEGER <= item <= _MAX_INTEGER:
                raise CollaborationContractError("collaboration_json_number_invalid")
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise CollaborationContractError("collaboration_json_number_invalid")
            return item
        if isinstance(item, Mapping):
            if len(item) > _MAX_COLLECTION_ITEMS:
                raise CollaborationContractError("collaboration_json_mapping_too_large")
            frozen: dict[str, object] = {}
            for key, child in item.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or key != key.strip()
                    or len(key.encode("utf-8")) > 128
                    or any(ord(character) < 32 for character in key)
                ):
                    raise CollaborationContractError("collaboration_json_key_invalid")
                if _forbidden_semantic_field(key):
                    raise CollaborationContractError("collaboration_semantic_channel_forbidden")
                if _secret_field(key):
                    raise CollaborationContractError("secret_field_forbidden")
                frozen[key] = freeze(child, depth=depth + 1)
            return MappingProxyType(frozen)
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if len(item) > _MAX_COLLECTION_ITEMS:
                raise CollaborationContractError("collaboration_json_sequence_too_large")
            return tuple(freeze(child, depth=depth + 1) for child in item)
        raise CollaborationContractError("collaboration_json_value_invalid")

    return freeze(value, depth=0)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _require_projection_size(value: object, code: str) -> None:
    if len(_canonical_json(value).encode("utf-8")) > _MAX_OBJECT_BYTES:
        raise CollaborationContractError(code)
