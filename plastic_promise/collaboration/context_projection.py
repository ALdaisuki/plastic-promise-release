"""Compose memory and live collaboration context without merging their authority planes.

The module is the PR4 seam between two independently produced read models:

* a project-scoped memory response containing ``core``/``related``/``divergent``;
* an authenticated, audience-scoped collaboration awareness projection and the
  event page from which its incremental peer delta was derived.

The memory ranking is copied unchanged.  Collaboration facts are projected
under a separate top-level ``collaboration`` field, are response-budgeted, and
never acquire canonical-memory authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from plastic_promise.collaboration.awareness import AgentAwarenessProjection
from plastic_promise.collaboration.contracts import (
    COLLABORATION_EVENT_SCHEMA,
    AgentSession,
    EventCursor,
    EventPage,
    ProjectScope,
)
from plastic_promise.collaboration.passive_bridge import AcceptanceReceipt

COLLABORATION_CONTEXT_SCHEMA = "collaboration-context-projection/v1"
COLLABORATION_RELEVANCE_POLICY = "role-aware-conflict-first/v2"
COLLABORATION_SOURCE_KIND = "collaboration-event-log"
COLLABORATION_SOURCE_AUTHORITY = "server"
COLLABORATION_PROJECTION_FACTORY_REVISION = "collaboration-context-projection-factory/v1"

_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_REQUIRED_MEMORY_LAYERS = ("core", "related", "divergent")
_PRIORITY = {
    "conflict": 4,
    "blocker": 3,
    "accepted_result": 2,
    "progress": 1,
}
_BASE_RELEVANCE = {
    "conflict": 0.90,
    "blocker": 0.80,
    "accepted_result": 0.67,
    "progress": 0.48,
}
_SEVERITY = {
    "conflict": 1.0,
    "blocker": 0.85,
    "accepted_result": 0.45,
    "progress": 0.25,
}
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_EVIDENCE_REFS = 4
_MAX_ARTIFACT_REFS = 4
_MAX_SUBJECT_REFS = 16
_MAX_TEXT_BYTES = 512
_ACCEPTANCE_ROLES = frozenset({"coordinator", "deepsec_reviewer", "reviewer"})
_CONTEXT_AUTHORITY_TOKEN = object()
_DEFAULT_BINDING_TTL_SECONDS = 60
_MAX_BINDING_TTL_SECONDS = 300
_DEFAULT_SESSION_FRESHNESS_SECONDS = 300
_MAX_SESSION_FRESHNESS_SECONDS = 3600
_DEFAULT_SNAPSHOT_FRESHNESS_SECONDS = 300
_MAX_SNAPSHOT_FRESHNESS_SECONDS = 3600
_FRESHNESS_HORIZON_SECONDS = 7 * 24 * 60 * 60
_REFERENCE_PREFIXES = {
    "module": ("module:", "file:", "path:", "package:"),
    "symbol": ("symbol:", "class:", "function:", "method:"),
    "artifact": ("artifact:",),
    "decision": ("decision:", "adr:"),
}
_REFERENCE_WEIGHTS = {
    "module": 0.03,
    "symbol": 0.04,
    "artifact": 0.03,
    "decision": 0.04,
}


class CollaborationContextProjectionError(ValueError):
    """Stable, non-sensitive failure raised when context composition is unsafe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _RelevanceContext:
    observed_at: datetime
    audience_refs: Mapping[str, frozenset[str]]
    causal_distance_by_event_id: Mapping[str, int | None]


@dataclass(frozen=True, slots=True)
class CollaborationContextBudget:
    """Hard response limits for the collaboration slice and final projection."""

    max_items: int = 12
    max_collaboration_bytes: int = 24 * 1024
    max_response_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_items, bool)
            or not isinstance(self.max_items, int)
            or not 1 <= self.max_items <= 64
        ):
            raise CollaborationContextProjectionError("context_budget_items_invalid")
        if (
            isinstance(self.max_collaboration_bytes, bool)
            or not isinstance(self.max_collaboration_bytes, int)
            or not 1024 <= self.max_collaboration_bytes <= _MAX_JSON_BYTES
        ):
            raise CollaborationContextProjectionError("context_budget_collaboration_invalid")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 2048 <= self.max_response_bytes <= _MAX_JSON_BYTES
            or self.max_response_bytes < self.max_collaboration_bytes
        ):
            raise CollaborationContextProjectionError("context_budget_response_invalid")


@dataclass(frozen=True, slots=True)
class CollaborationPolicyClaim:
    """Public upstream policy-binding claim, never authority by itself."""

    binding_id: str
    binding_sha256: str
    policy_revision: str
    policy_digest: str
    audience_session_sha256: str
    expires_at: str

    def __post_init__(self) -> None:
        for value, code in (
            (self.binding_id, "context_policy_binding_id_invalid"),
            (self.policy_revision, "context_policy_revision_invalid"),
        ):
            if not isinstance(value, str) or not _SAFE_IDENTIFIER_RE.fullmatch(value):
                raise CollaborationContextProjectionError(code)
        for digest, code in (
            (self.binding_sha256, "context_policy_binding_digest_invalid"),
            (self.policy_digest, "context_policy_digest_invalid"),
            (self.audience_session_sha256, "context_policy_session_digest_invalid"),
        ):
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise CollaborationContextProjectionError(code)
        _parse_time(self.expires_at, "context_policy_expiry_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "policy_revision": self.policy_revision,
            "policy_digest": self.policy_digest,
            "audience_session_sha256": self.audience_session_sha256,
            "expires_at": self.expires_at,
        }

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class CollaborationSourceTuple:
    """Exact, versioned identity of one server-built collaboration page."""

    source_kind: str
    source_authority: str
    project: ProjectScope
    coordination_session_id: str
    audience_agent_id: str
    audience_role: str
    audience_session_id: str
    audience_session_sha256: str
    agent_session_policy_revision: str
    event_schema_revision: str
    event_log_revision: str
    cursor_from: EventCursor
    cursor_to: EventCursor
    source_page_digest: str
    projection_factory_revision: str
    generated_at_utc: str

    def __post_init__(self) -> None:
        if self.source_kind != COLLABORATION_SOURCE_KIND:
            raise CollaborationContextProjectionError("context_source_kind_invalid")
        if self.source_authority != COLLABORATION_SOURCE_AUTHORITY:
            raise CollaborationContextProjectionError("context_source_authority_invalid")
        if not isinstance(self.project, ProjectScope):
            raise CollaborationContextProjectionError("context_source_project_invalid")
        for value, code in (
            (self.coordination_session_id, "context_source_session_invalid"),
            (self.audience_agent_id, "context_source_audience_invalid"),
            (self.audience_role, "context_source_audience_invalid"),
            (self.audience_session_id, "context_source_audience_invalid"),
            (
                self.agent_session_policy_revision,
                "context_source_policy_revision_invalid",
            ),
            (self.event_log_revision, "context_source_event_log_revision_invalid"),
        ):
            if not isinstance(value, str) or not _SAFE_IDENTIFIER_RE.fullmatch(value):
                raise CollaborationContextProjectionError(code)
        if self.event_schema_revision != COLLABORATION_EVENT_SCHEMA:
            raise CollaborationContextProjectionError("context_source_event_schema_revision_stale")
        if self.projection_factory_revision != COLLABORATION_PROJECTION_FACTORY_REVISION:
            raise CollaborationContextProjectionError(
                "context_source_projection_factory_revision_stale"
            )
        for digest, code in (
            (
                self.audience_session_sha256,
                "context_source_audience_digest_invalid",
            ),
            (self.source_page_digest, "context_source_page_digest_invalid"),
        ):
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise CollaborationContextProjectionError(code)
        for cursor in (self.cursor_from, self.cursor_to):
            if not isinstance(cursor, EventCursor):
                raise CollaborationContextProjectionError("context_source_cursor_invalid")
            if (
                cursor.project != self.project
                or cursor.coordination_session_id != self.coordination_session_id
            ):
                raise CollaborationContextProjectionError("context_source_cursor_scope_mismatch")
        if self.cursor_to.sequence < self.cursor_from.sequence:
            raise CollaborationContextProjectionError("context_source_cursor_regression")
        _parse_time(self.generated_at_utc, "context_source_generated_at_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "source_authority": self.source_authority,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "audience": {
                "agent_id": self.audience_agent_id,
                "role": self.audience_role,
                "session_id": self.audience_session_id,
                "session_sha256": self.audience_session_sha256,
            },
            "agent_session_policy_revision": self.agent_session_policy_revision,
            "event_schema_revision": self.event_schema_revision,
            "event_log_revision": self.event_log_revision,
            "cursor_from": self.cursor_from.to_dict(),
            "cursor_to": self.cursor_to.to_dict(),
            "source_page_digest": self.source_page_digest,
            "projection_factory_revision": self.projection_factory_revision,
            "generated_at_utc": self.generated_at_utc,
        }

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class VerifiedCausalDistanceClaim:
    """Server-adapter claim for causal ancestry that crosses cursor pages."""

    event_id: str
    root_event_id: str
    distance: int
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        for value in (self.event_id, self.root_event_id):
            if not isinstance(value, str) or not _SAFE_IDENTIFIER_RE.fullmatch(value):
                raise CollaborationContextProjectionError("context_lineage_causal_event_id_invalid")
        if (
            isinstance(self.distance, bool)
            or not isinstance(self.distance, int)
            or not 0 <= self.distance <= 200
        ):
            raise CollaborationContextProjectionError("context_lineage_causal_distance_invalid")
        if (self.distance == 0) != (self.event_id == self.root_event_id):
            raise CollaborationContextProjectionError("context_lineage_causal_root_mismatch")
        if not isinstance(self.source_receipt_sha256, str) or not _SHA256_RE.fullmatch(
            self.source_receipt_sha256
        ):
            raise CollaborationContextProjectionError("context_lineage_causal_receipt_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "root_event_id": self.root_event_id,
            "distance": self.distance,
            "source_receipt_sha256": self.source_receipt_sha256,
        }

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class SourcePageLineageClaim:
    """Server-adapter claim for one bounded event-page read transaction.

    The claim names the durable source receipt and source head that the PR5
    canonical event-log adapter must verify.  Shape validation here catches
    regressions and impossible spans, but only the injected verifier can prove
    the receipt against persistent server state.
    """

    source_receipt_id: str
    source_receipt_sha256: str
    source_anchor_sha256: str
    source_tuple: CollaborationSourceTuple
    source_head_cursor: EventCursor
    event_page_sha256: str
    awareness_sha256: str
    working_set_sha256: str
    visible_event_count: int
    causal_distances: tuple[VerifiedCausalDistanceClaim, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_tuple, CollaborationSourceTuple):
            raise CollaborationContextProjectionError("context_source_tuple_invalid")
        if not isinstance(self.source_receipt_id, str) or not _SAFE_IDENTIFIER_RE.fullmatch(
            self.source_receipt_id
        ):
            raise CollaborationContextProjectionError("context_lineage_receipt_id_invalid")
        for digest in (
            self.source_receipt_sha256,
            self.source_anchor_sha256,
            self.event_page_sha256,
            self.awareness_sha256,
            self.working_set_sha256,
        ):
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise CollaborationContextProjectionError("context_lineage_digest_invalid")
        if self.event_page_sha256 != self.source_tuple.source_page_digest:
            raise CollaborationContextProjectionError("context_lineage_page_digest_mismatch")
        for cursor in (self.after_cursor, self.next_cursor, self.source_head_cursor):
            if not isinstance(cursor, EventCursor):
                raise CollaborationContextProjectionError("context_lineage_cursor_invalid")
            if (
                cursor.project != self.project
                or cursor.coordination_session_id != self.coordination_session_id
            ):
                raise CollaborationContextProjectionError("context_lineage_cursor_scope_mismatch")
        if (
            isinstance(self.visible_event_count, bool)
            or not isinstance(self.visible_event_count, int)
            or self.visible_event_count < 0
            or self.visible_event_count > 200
        ):
            raise CollaborationContextProjectionError("context_lineage_event_count_invalid")
        span = self.next_cursor.sequence - self.after_cursor.sequence
        if span < 0:
            raise CollaborationContextProjectionError("context_lineage_cursor_regression")
        if self.next_cursor.sequence > self.source_head_cursor.sequence:
            raise CollaborationContextProjectionError("context_lineage_cursor_beyond_head")
        if self.visible_event_count > span:
            raise CollaborationContextProjectionError("context_lineage_visible_count_exceeds_span")
        if self.visible_event_count > 0 and span == 0:
            raise CollaborationContextProjectionError("context_lineage_cursor_did_not_advance")
        if self.visible_event_count == 0 and span != 0:
            raise CollaborationContextProjectionError("context_lineage_empty_cursor_gap")
        causal_distances = tuple(self.causal_distances)
        if len(causal_distances) > 200 or any(
            not isinstance(item, VerifiedCausalDistanceClaim) for item in causal_distances
        ):
            raise CollaborationContextProjectionError("context_lineage_causal_distances_invalid")
        if len({item.event_id for item in causal_distances}) != len(causal_distances):
            raise CollaborationContextProjectionError("context_lineage_causal_event_duplicate")
        if any(
            item.source_receipt_sha256 != self.source_receipt_sha256 for item in causal_distances
        ):
            raise CollaborationContextProjectionError("context_lineage_causal_receipt_mismatch")
        object.__setattr__(
            self,
            "causal_distances",
            tuple(sorted(causal_distances, key=lambda item: item.event_id)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256,
            "source_anchor_sha256": self.source_anchor_sha256,
            "source_tuple": self.source_tuple.to_dict(),
            "source_head_cursor": self.source_head_cursor.to_dict(),
            "event_page_sha256": self.event_page_sha256,
            "awareness_sha256": self.awareness_sha256,
            "working_set_sha256": self.working_set_sha256,
            "visible_event_count": self.visible_event_count,
            "causal_distances": [item.to_dict() for item in self.causal_distances],
            "persistent_head_authority": "pr5-canonical-event-log-adapter",
        }

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.to_dict())

    @property
    def project(self) -> ProjectScope:
        return self.source_tuple.project

    @property
    def coordination_session_id(self) -> str:
        return self.source_tuple.coordination_session_id

    @property
    def after_cursor(self) -> EventCursor:
        return self.source_tuple.cursor_from

    @property
    def next_cursor(self) -> EventCursor:
        return self.source_tuple.cursor_to


@dataclass(frozen=True, slots=True, init=False)
class ServerCollaborationContextBinding:
    """Short-lived receipt issued by one server-owned context authority."""

    binding_id: str
    authority_id: str
    audience_session_sha256: str
    policy_claim_sha256: str
    source_lineage_sha256: str
    acceptance_receipt_sha256s: tuple[str, ...]
    feed_payload_sha256: str
    issued_at: str
    expires_at: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationContextProjectionError("context_authority_binding_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        binding_id: str,
        authority_id: str,
        audience_session_sha256: str,
        policy_claim_sha256: str,
        source_lineage_sha256: str,
        acceptance_receipt_sha256s: tuple[str, ...],
        feed_payload_sha256: str,
        issued_at: str,
        expires_at: str,
        _authority_token: object,
    ) -> ServerCollaborationContextBinding:
        if _authority_token is not _CONTEXT_AUTHORITY_TOKEN:
            raise CollaborationContextProjectionError("context_server_authority_required")
        instance = object.__new__(cls)
        for field_name, value in {
            "binding_id": binding_id,
            "authority_id": authority_id,
            "audience_session_sha256": audience_session_sha256,
            "policy_claim_sha256": policy_claim_sha256,
            "source_lineage_sha256": source_lineage_sha256,
            "acceptance_receipt_sha256s": acceptance_receipt_sha256s,
            "feed_payload_sha256": feed_payload_sha256,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }.items():
            object.__setattr__(instance, field_name, value)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        for value, code in (
            (self.binding_id, "context_authority_binding_id_invalid"),
            (self.authority_id, "context_authority_id_invalid"),
        ):
            if not isinstance(value, str) or not _SAFE_IDENTIFIER_RE.fullmatch(value):
                raise CollaborationContextProjectionError(code)
        for digest in (
            self.audience_session_sha256,
            self.policy_claim_sha256,
            self.source_lineage_sha256,
            self.feed_payload_sha256,
            *self.acceptance_receipt_sha256s,
        ):
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise CollaborationContextProjectionError("context_authority_digest_invalid")
        issued = _parse_time(self.issued_at, "context_authority_issued_at_invalid")
        expires = _parse_time(self.expires_at, "context_authority_expires_at_invalid")
        if expires <= issued:
            raise CollaborationContextProjectionError("context_authority_expiry_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "authority_id": self.authority_id,
            "audience_session_sha256": self.audience_session_sha256,
            "policy_claim_sha256": self.policy_claim_sha256,
            "source_lineage_sha256": self.source_lineage_sha256,
            "acceptance_receipt_sha256s": list(self.acceptance_receipt_sha256s),
            "feed_payload_sha256": self.feed_payload_sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "verification_mode": "server-authority-required-on-compose",
        }

    @property
    def content_sha256(self) -> str:
        return _content_sha256(self.to_dict())


class CollaborationContextAuthority:
    """PR4 validation seam for policy, session, projection, and lineage.

    This object is not a security root merely because it exists in Python.  PR5
    must keep the sole instance inside server wiring and supply callbacks backed
    by canonical policy, redacted projection, event, and acceptance stores.
    Within that boundary, bindings are stateful, short-lived, and instance-bound.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        policy_verifier: Callable[[AgentSession, CollaborationPolicyClaim], bool],
        source_lineage_verifier: Callable[[SourcePageLineageClaim], bool],
        projection_verifier: Callable[[AgentAwarenessProjection, SourcePageLineageClaim], bool],
        acceptance_verifier: Callable[[AcceptanceReceipt], bool],
        session_freshness_seconds: int = _DEFAULT_SESSION_FRESHNESS_SECONDS,
        snapshot_freshness_seconds: int = _DEFAULT_SNAPSHOT_FRESHNESS_SECONDS,
        _authority_token: object | None = None,
    ) -> None:
        if _authority_token is not _CONTEXT_AUTHORITY_TOKEN:
            raise CollaborationContextProjectionError("context_server_authority_required")
        if not callable(clock) or not callable(policy_verifier):
            raise CollaborationContextProjectionError("context_authority_verifier_invalid")
        if (
            not callable(source_lineage_verifier)
            or not callable(projection_verifier)
            or not callable(acceptance_verifier)
        ):
            raise CollaborationContextProjectionError("context_authority_verifier_invalid")
        if (
            isinstance(session_freshness_seconds, bool)
            or not isinstance(session_freshness_seconds, int)
            or not 1 <= session_freshness_seconds <= _MAX_SESSION_FRESHNESS_SECONDS
        ):
            raise CollaborationContextProjectionError("context_session_freshness_invalid")
        if (
            isinstance(snapshot_freshness_seconds, bool)
            or not isinstance(snapshot_freshness_seconds, int)
            or not 1 <= snapshot_freshness_seconds <= _MAX_SNAPSHOT_FRESHNESS_SECONDS
        ):
            raise CollaborationContextProjectionError("context_snapshot_freshness_invalid")
        self._clock = clock
        self._policy_verifier = policy_verifier
        self._source_lineage_verifier = source_lineage_verifier
        self._projection_verifier = projection_verifier
        self._acceptance_verifier = acceptance_verifier
        self._session_freshness_seconds = session_freshness_seconds
        self._snapshot_freshness_seconds = snapshot_freshness_seconds
        self._authority_id = f"context-authority:{uuid.uuid4().hex}"
        self._issued: dict[str, tuple[str, datetime]] = {}

    def bind_sources(
        self,
        *,
        memory_context: Mapping[str, object],
        awareness: AgentAwarenessProjection,
        event_page: EventPage,
        authenticated_project: ProjectScope,
        authenticated_session_id: str,
        authenticated_audience_session: AgentSession,
        read_after_cursor: EventCursor,
        policy_claim: CollaborationPolicyClaim,
        source_lineage: SourcePageLineageClaim,
        acceptance_receipts: Sequence[AcceptanceReceipt] = (),
        ttl_seconds: int = _DEFAULT_BINDING_TTL_SECONDS,
    ) -> AuthenticatedCollaborationFeed:
        """Issue one short-lived feed after current server checks pass."""

        now = self._now()
        self._prune_issued(now)
        normalized_memory = _normalize_memory_context(memory_context)
        receipts = _normalize_acceptance_receipts(acceptance_receipts)
        self._require_current_session(authenticated_audience_session, now)
        self._require_current_snapshot(awareness, now)
        self._require_policy(authenticated_audience_session, policy_claim, now)
        self._require_source_lineage(
            awareness=awareness,
            event_page=event_page,
            read_after_cursor=read_after_cursor,
            source_lineage=source_lineage,
            audience_session=authenticated_audience_session,
            policy_claim=policy_claim,
            now=now,
        )
        self._require_projection(awareness, source_lineage)
        for receipt in receipts:
            self._require_acceptance(receipt)
        ttl = _binding_ttl(ttl_seconds)
        expires_at = now + timedelta(seconds=ttl)
        session_expiry = _optional_time(authenticated_audience_session.expires_at)
        policy_expiry = _parse_time(policy_claim.expires_at, "context_policy_expiry_invalid")
        if session_expiry is not None:
            expires_at = min(expires_at, session_expiry)
        expires_at = min(expires_at, policy_expiry)
        if expires_at <= now:
            raise CollaborationContextProjectionError("context_authority_binding_expired")
        issued_at_text = _utc_text(now)
        resolved_binding_id = f"context-binding:{uuid.uuid4().hex}"
        while resolved_binding_id in self._issued:
            resolved_binding_id = f"context-binding:{uuid.uuid4().hex}"
        acceptance_digests = tuple(receipt.content_sha256 for receipt in receipts)
        feed_payload_sha256 = _feed_payload_sha256(
            project=authenticated_project,
            coordination_session_id=authenticated_session_id,
            audience_session_sha256=authenticated_audience_session.content_sha256,
            read_after_cursor=read_after_cursor,
            memory_context_sha256=_content_sha256(normalized_memory),
            awareness_sha256=awareness.content_sha256,
            working_set_sha256=awareness.working_set_sha256,
            event_page_sha256=event_page.content_sha256,
            policy_claim_sha256=policy_claim.content_sha256,
            source_lineage_sha256=source_lineage.content_sha256,
            acceptance_receipt_sha256s=acceptance_digests,
        )
        authority_binding = ServerCollaborationContextBinding._issue(
            binding_id=resolved_binding_id,
            authority_id=self._authority_id,
            audience_session_sha256=authenticated_audience_session.content_sha256,
            policy_claim_sha256=policy_claim.content_sha256,
            source_lineage_sha256=source_lineage.content_sha256,
            acceptance_receipt_sha256s=acceptance_digests,
            feed_payload_sha256=feed_payload_sha256,
            issued_at=issued_at_text,
            expires_at=_utc_text(expires_at),
            _authority_token=_CONTEXT_AUTHORITY_TOKEN,
        )
        feed = AuthenticatedCollaborationFeed._from_authority(
            memory_context=memory_context,
            awareness=awareness,
            event_page=event_page,
            authenticated_project=authenticated_project,
            authenticated_session_id=authenticated_session_id,
            authenticated_audience_session=authenticated_audience_session,
            read_after_cursor=read_after_cursor,
            policy_claim=policy_claim,
            source_lineage=source_lineage,
            authority_binding=authority_binding,
            acceptance_receipts=receipts,
            _authority_token=_CONTEXT_AUTHORITY_TOKEN,
        )
        _validate_sources(normalized_memory, feed)
        self._issued[authority_binding.binding_id] = (
            authority_binding.content_sha256,
            expires_at,
        )
        return feed

    def verify_feed(self, feed: AuthenticatedCollaborationFeed) -> None:
        """Revalidate a feed at response time, rejecting stale replay."""

        if not isinstance(feed, AuthenticatedCollaborationFeed):
            raise CollaborationContextProjectionError("context_feed_invalid")
        binding = feed.authority_binding
        if binding.authority_id != self._authority_id:
            raise CollaborationContextProjectionError("context_authority_instance_mismatch")
        now = self._now()
        binding_expiry = _parse_time(
            binding.expires_at,
            "context_authority_expires_at_invalid",
        )
        if binding_expiry <= now:
            self._prune_issued(now)
            raise CollaborationContextProjectionError("context_authority_binding_expired")
        self._prune_issued(now)
        issued = self._issued.get(binding.binding_id)
        if issued is None or issued[0] != binding.content_sha256:
            raise CollaborationContextProjectionError("context_authority_binding_not_issued")
        self._require_current_session(feed.audience_session, now)
        self._require_current_snapshot(feed.awareness, now)
        self._require_policy(feed.audience_session, feed.policy_claim, now)
        self._require_source_lineage(
            awareness=feed.awareness,
            event_page=feed.event_page,
            read_after_cursor=feed.read_after_cursor,
            source_lineage=feed.source_lineage,
            audience_session=feed.audience_session,
            policy_claim=feed.policy_claim,
            now=now,
        )
        self._require_projection(feed.awareness, feed.source_lineage)
        for receipt in feed.acceptance_receipts:
            self._require_acceptance(receipt)
        if (
            binding.audience_session_sha256 != feed.audience_session_sha256
            or binding.policy_claim_sha256 != feed.policy_claim.content_sha256
            or binding.source_lineage_sha256 != feed.source_lineage.content_sha256
            or binding.acceptance_receipt_sha256s != feed.acceptance_receipt_sha256s
            or binding.feed_payload_sha256 != feed.feed_payload_sha256
        ):
            raise CollaborationContextProjectionError("context_authority_binding_drift")

    def _require_current_session(self, session: AgentSession, now: datetime) -> None:
        if not isinstance(session, AgentSession):
            raise CollaborationContextProjectionError("context_feed_audience_invalid")
        if session.state != "active":
            raise CollaborationContextProjectionError("collaboration_audience_session_inactive")
        heartbeat = _parse_time(
            session.last_heartbeat_at,
            "collaboration_audience_session_time_invalid",
        )
        if heartbeat > now:
            raise CollaborationContextProjectionError("collaboration_audience_session_from_future")
        if (now - heartbeat).total_seconds() > self._session_freshness_seconds:
            raise CollaborationContextProjectionError("collaboration_audience_session_stale")
        expiry = _optional_time(session.expires_at)
        if expiry is not None and expiry <= now:
            raise CollaborationContextProjectionError("collaboration_audience_session_expired")

    def _require_policy(
        self,
        session: AgentSession,
        claim: CollaborationPolicyClaim,
        now: datetime,
    ) -> None:
        if not isinstance(claim, CollaborationPolicyClaim):
            raise CollaborationContextProjectionError("context_policy_claim_invalid")
        if claim.audience_session_sha256 != session.content_sha256:
            raise CollaborationContextProjectionError("context_policy_session_digest_mismatch")
        if _parse_time(claim.expires_at, "context_policy_expiry_invalid") <= now:
            raise CollaborationContextProjectionError("context_policy_binding_expired")
        self._require_verified(
            self._policy_verifier,
            session,
            claim,
            code="context_policy_authority_rejected",
        )

    def _require_current_snapshot(
        self,
        awareness: AgentAwarenessProjection,
        now: datetime,
    ) -> None:
        if not isinstance(awareness, AgentAwarenessProjection):
            raise CollaborationContextProjectionError("context_feed_awareness_invalid")
        observed_at = _parse_time(
            awareness.observed_at,
            "collaboration_observed_at_invalid",
        )
        if observed_at > now:
            raise CollaborationContextProjectionError("collaboration_snapshot_from_future")
        if (now - observed_at).total_seconds() > self._snapshot_freshness_seconds:
            raise CollaborationContextProjectionError("collaboration_snapshot_stale")

    def _require_source_lineage(
        self,
        *,
        awareness: AgentAwarenessProjection,
        event_page: EventPage,
        read_after_cursor: EventCursor,
        source_lineage: SourcePageLineageClaim,
        audience_session: AgentSession,
        policy_claim: CollaborationPolicyClaim,
        now: datetime,
    ) -> None:
        if not isinstance(source_lineage, SourcePageLineageClaim):
            raise CollaborationContextProjectionError("context_source_lineage_invalid")
        source_tuple = source_lineage.source_tuple
        if (
            source_lineage.after_cursor != read_after_cursor
            or source_lineage.next_cursor != event_page.next_cursor
            or source_lineage.event_page_sha256 != event_page.content_sha256
            or source_tuple.source_page_digest != event_page.content_sha256
            or source_lineage.awareness_sha256 != awareness.content_sha256
            or source_lineage.working_set_sha256 != awareness.working_set_sha256
            or source_lineage.visible_event_count != len(event_page.events)
        ):
            raise CollaborationContextProjectionError("context_source_lineage_mismatch")
        if (
            source_tuple.audience_agent_id != audience_session.identity.agent_id
            or source_tuple.audience_role != audience_session.identity.role
            or source_tuple.audience_session_id != audience_session.session_id
            or source_tuple.audience_session_sha256 != audience_session.content_sha256
            or source_tuple.audience_agent_id != awareness.audience_agent_id
            or source_tuple.audience_role != awareness.audience_role
            or source_tuple.audience_session_id != awareness.audience_session_id
        ):
            raise CollaborationContextProjectionError("context_source_audience_mismatch")
        if source_tuple.agent_session_policy_revision != policy_claim.policy_revision:
            raise CollaborationContextProjectionError("context_source_policy_revision_mismatch")
        generated_at = _parse_time(
            source_tuple.generated_at_utc,
            "context_source_generated_at_invalid",
        )
        observed_at = _parse_time(
            awareness.observed_at,
            "collaboration_observed_at_invalid",
        )
        if generated_at < observed_at:
            raise CollaborationContextProjectionError("context_source_generated_before_snapshot")
        if generated_at > now:
            raise CollaborationContextProjectionError("context_source_generated_from_future")
        if (now - generated_at).total_seconds() > self._snapshot_freshness_seconds:
            raise CollaborationContextProjectionError("context_source_generation_stale")
        self._require_verified(
            self._source_lineage_verifier,
            source_lineage,
            code="context_source_lineage_authority_rejected",
        )

    def _require_projection(
        self,
        awareness: AgentAwarenessProjection,
        source_lineage: SourcePageLineageClaim,
    ) -> None:
        self._require_verified(
            self._projection_verifier,
            awareness,
            source_lineage,
            code="context_projection_authority_rejected",
        )

    def _require_acceptance(self, receipt: AcceptanceReceipt) -> None:
        self._require_verified(
            self._acceptance_verifier,
            receipt,
            code="context_acceptance_authority_rejected",
        )

    @staticmethod
    def _require_verified(
        verifier: Callable[..., bool],
        *args: object,
        code: str,
    ) -> None:
        try:
            accepted = verifier(*args)
        except Exception as exc:
            raise CollaborationContextProjectionError(code) from exc
        if accepted is not True:
            raise CollaborationContextProjectionError(code)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CollaborationContextProjectionError("context_authority_clock_invalid")
        return value.astimezone(timezone.utc)

    def _prune_issued(self, now: datetime) -> None:
        expired_ids = [
            binding_id for binding_id, (_, expires_at) in self._issued.items() if expires_at <= now
        ]
        for binding_id in expired_ids:
            del self._issued[binding_id]


def open_server_collaboration_context_authority(
    *,
    clock: Callable[[], datetime],
    policy_verifier: Callable[[AgentSession, CollaborationPolicyClaim], bool],
    source_lineage_verifier: Callable[[SourcePageLineageClaim], bool],
    projection_verifier: Callable[[AgentAwarenessProjection, SourcePageLineageClaim], bool],
    acceptance_verifier: Callable[[AcceptanceReceipt], bool],
    session_freshness_seconds: int = _DEFAULT_SESSION_FRESHNESS_SECONDS,
    snapshot_freshness_seconds: int = _DEFAULT_SNAPSHOT_FRESHNESS_SECONDS,
) -> CollaborationContextAuthority:
    """Create the validation seam that only PR5 server wiring may own."""

    return CollaborationContextAuthority(
        clock=clock,
        policy_verifier=policy_verifier,
        source_lineage_verifier=source_lineage_verifier,
        projection_verifier=projection_verifier,
        acceptance_verifier=acceptance_verifier,
        session_freshness_seconds=session_freshness_seconds,
        snapshot_freshness_seconds=snapshot_freshness_seconds,
        _authority_token=_CONTEXT_AUTHORITY_TOKEN,
    )


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedCollaborationFeed:
    """Server-adapter assertion binding one awareness view to its source page.

    This value is intentionally not a transport authentication mechanism.  A
    trusted server adapter must create it only *after* authenticating the Agent
    session.  The composer then fails closed if any project, session, audience,
    cursor, or source digest drifts before response construction.
    """

    project: ProjectScope
    coordination_session_id: str
    audience_session: AgentSession
    read_after_cursor: EventCursor
    awareness: AgentAwarenessProjection
    event_page: EventPage
    policy_claim: CollaborationPolicyClaim
    source_lineage: SourcePageLineageClaim
    authority_binding: ServerCollaborationContextBinding
    acceptance_receipts: tuple[AcceptanceReceipt, ...]
    memory_context_sha256: str
    audience_session_sha256: str
    awareness_sha256: str
    working_set_sha256: str
    event_page_sha256: str
    acceptance_receipt_sha256s: tuple[str, ...]
    feed_payload_sha256: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationContextProjectionError("context_feed_factory_required")

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise CollaborationContextProjectionError("context_feed_project_invalid")
        if not isinstance(self.audience_session, AgentSession):
            raise CollaborationContextProjectionError("context_feed_audience_invalid")
        if not isinstance(self.read_after_cursor, EventCursor):
            raise CollaborationContextProjectionError("context_feed_cursor_invalid")
        if not isinstance(self.awareness, AgentAwarenessProjection):
            raise CollaborationContextProjectionError("context_feed_awareness_invalid")
        if not isinstance(self.event_page, EventPage):
            raise CollaborationContextProjectionError("context_feed_event_page_invalid")
        if not isinstance(self.policy_claim, CollaborationPolicyClaim):
            raise CollaborationContextProjectionError("context_policy_claim_invalid")
        if not isinstance(self.source_lineage, SourcePageLineageClaim):
            raise CollaborationContextProjectionError("context_source_lineage_invalid")
        if not isinstance(self.authority_binding, ServerCollaborationContextBinding):
            raise CollaborationContextProjectionError("context_authority_binding_invalid")
        receipts = _normalize_acceptance_receipts(self.acceptance_receipts)
        if not isinstance(self.coordination_session_id, str) or not _SAFE_IDENTIFIER_RE.fullmatch(
            self.coordination_session_id
        ):
            raise CollaborationContextProjectionError("context_feed_session_invalid")
        for digest in (
            self.memory_context_sha256,
            self.audience_session_sha256,
            self.awareness_sha256,
            self.working_set_sha256,
            self.event_page_sha256,
            self.feed_payload_sha256,
        ):
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise CollaborationContextProjectionError("context_feed_digest_invalid")
        receipt_digests = tuple(self.acceptance_receipt_sha256s)
        if len(receipt_digests) != len(receipts) or any(
            not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
            for digest in receipt_digests
        ):
            raise CollaborationContextProjectionError("context_feed_digest_invalid")
        object.__setattr__(self, "acceptance_receipts", receipts)
        object.__setattr__(self, "acceptance_receipt_sha256s", receipt_digests)

    @classmethod
    def _from_authority(
        cls,
        *,
        memory_context: Mapping[str, object],
        awareness: AgentAwarenessProjection,
        event_page: EventPage,
        authenticated_project: ProjectScope,
        authenticated_session_id: str,
        authenticated_audience_session: AgentSession,
        read_after_cursor: EventCursor,
        policy_claim: CollaborationPolicyClaim,
        source_lineage: SourcePageLineageClaim,
        authority_binding: ServerCollaborationContextBinding,
        acceptance_receipts: Sequence[AcceptanceReceipt] = (),
        _authority_token: object,
    ) -> AuthenticatedCollaborationFeed:
        """Bind sources only after the server authority has verified them."""

        if _authority_token is not _CONTEXT_AUTHORITY_TOKEN:
            raise CollaborationContextProjectionError("context_server_authority_required")

        normalized_memory = _normalize_memory_context(memory_context)
        receipts = _normalize_acceptance_receipts(acceptance_receipts)
        memory_context_sha256 = _content_sha256(normalized_memory)
        audience_session_sha256 = authenticated_audience_session.content_sha256
        awareness_sha256 = awareness.content_sha256
        event_page_sha256 = event_page.content_sha256
        acceptance_digests = tuple(receipt.content_sha256 for receipt in receipts)
        instance = object.__new__(cls)
        values = {
            "project": authenticated_project,
            "coordination_session_id": authenticated_session_id,
            "audience_session": authenticated_audience_session,
            "read_after_cursor": read_after_cursor,
            "awareness": awareness,
            "event_page": event_page,
            "policy_claim": policy_claim,
            "source_lineage": source_lineage,
            "authority_binding": authority_binding,
            "acceptance_receipts": receipts,
            "memory_context_sha256": memory_context_sha256,
            "audience_session_sha256": audience_session_sha256,
            "awareness_sha256": awareness_sha256,
            "working_set_sha256": awareness.working_set_sha256,
            "event_page_sha256": event_page_sha256,
            "acceptance_receipt_sha256s": acceptance_digests,
            "feed_payload_sha256": _feed_payload_sha256(
                project=authenticated_project,
                coordination_session_id=authenticated_session_id,
                audience_session_sha256=audience_session_sha256,
                read_after_cursor=read_after_cursor,
                memory_context_sha256=memory_context_sha256,
                awareness_sha256=awareness_sha256,
                working_set_sha256=awareness.working_set_sha256,
                event_page_sha256=event_page_sha256,
                policy_claim_sha256=policy_claim.content_sha256,
                source_lineage_sha256=source_lineage.content_sha256,
                acceptance_receipt_sha256s=acceptance_digests,
            ),
        }
        for field_name, value in values.items():
            object.__setattr__(instance, field_name, value)
        instance.__post_init__()
        return instance


def compose_context_projection(
    *,
    memory_context: Mapping[str, object],
    collaboration_feed: AuthenticatedCollaborationFeed,
    authority: CollaborationContextAuthority,
    budget: CollaborationContextBudget | None = None,
) -> dict[str, object]:
    """Return one bounded final context projection with separate authority planes.

    Callers receive the memory projection unchanged plus one independent
    ``collaboration`` field.  Peer events are never inserted into the memory
    ``core``, ``related``, or ``divergent`` lists.
    """

    if not isinstance(collaboration_feed, AuthenticatedCollaborationFeed):
        raise CollaborationContextProjectionError("context_feed_invalid")
    if not isinstance(authority, CollaborationContextAuthority):
        raise CollaborationContextProjectionError("context_server_authority_required")
    resolved_budget = budget or CollaborationContextBudget()
    if not isinstance(resolved_budget, CollaborationContextBudget):
        raise CollaborationContextProjectionError("context_budget_invalid")

    memory = _normalize_memory_context(memory_context)
    _validate_sources(memory, collaboration_feed)
    authority.verify_feed(collaboration_feed)

    candidate_groups = _collaboration_candidates(collaboration_feed)
    candidates = sorted(
        candidate_groups,
        key=lambda item: (
            -_PRIORITY[item["kind"]],
            -float(item["relevance"]),
            -_parse_time(
                str(item.get("created_at") or ""),
                "collaboration_candidate_time_invalid",
            ).timestamp(),
            str(item["id"]),
        ),
    )
    requested_counts = _count_kinds(candidates)
    emitted: list[dict[str, object]] = []

    collaboration = _base_collaboration_projection(
        feed=collaboration_feed,
        budget=resolved_budget,
        requested_counts=requested_counts,
    )
    result: dict[str, object] = {**memory, "collaboration": collaboration}
    _require_base_budget(result, collaboration, resolved_budget)

    for candidate in candidates:
        if len(emitted) >= resolved_budget.max_items:
            break
        tentative_items = [*emitted, candidate]
        tentative = _with_emitted_items(
            collaboration,
            items=tentative_items,
            requested_counts=requested_counts,
        )
        tentative_result = {**memory, "collaboration": tentative}
        if _within_budget(tentative_result, tentative, resolved_budget):
            emitted = tentative_items
        else:
            # A strict risk order is only meaningful when the response is a
            # prefix of that order.  Skipping an oversized blocker in order to
            # admit a later progress item would invert the declared policy.
            break

    collaboration = _with_emitted_items(
        collaboration,
        items=emitted,
        requested_counts=requested_counts,
    )
    result = {**memory, "collaboration": collaboration}
    if not _within_budget(result, collaboration, resolved_budget):
        raise CollaborationContextProjectionError("context_response_budget_exceeded")
    return result


def _normalize_memory_context(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CollaborationContextProjectionError("memory_context_invalid")
    if "collaboration" in value:
        raise CollaborationContextProjectionError("memory_context_collaboration_collision")
    normalized = _json_clone(value, "memory_context_invalid")
    if not isinstance(normalized, dict):
        raise CollaborationContextProjectionError("memory_context_invalid")
    for layer in _REQUIRED_MEMORY_LAYERS:
        if not isinstance(normalized.get(layer), list):
            raise CollaborationContextProjectionError("memory_context_layers_invalid")
    project_id = normalized.get("project_id")
    if not isinstance(project_id, str):
        raise CollaborationContextProjectionError("memory_context_project_missing")
    return normalized


def _validate_sources(
    memory: Mapping[str, object],
    feed: AuthenticatedCollaborationFeed,
) -> None:
    awareness = feed.awareness
    page = feed.event_page
    if memory.get("project_id") != feed.project.project_id:
        raise CollaborationContextProjectionError("context_project_scope_mismatch")
    if _content_sha256(memory) != feed.memory_context_sha256:
        raise CollaborationContextProjectionError("memory_context_digest_mismatch")
    if awareness.content_sha256 != feed.awareness_sha256:
        raise CollaborationContextProjectionError("awareness_digest_mismatch")
    if feed.audience_session.content_sha256 != feed.audience_session_sha256:
        raise CollaborationContextProjectionError("audience_session_digest_mismatch")
    if awareness.working_set_sha256 != feed.working_set_sha256:
        raise CollaborationContextProjectionError("working_set_digest_mismatch")
    if page.content_sha256 != feed.event_page_sha256:
        raise CollaborationContextProjectionError("event_page_digest_mismatch")
    if awareness.project != feed.project or page.project != feed.project:
        raise CollaborationContextProjectionError("collaboration_project_scope_mismatch")
    if (
        awareness.coordination_session_id != feed.coordination_session_id
        or page.coordination_session_id != feed.coordination_session_id
    ):
        raise CollaborationContextProjectionError("collaboration_session_scope_mismatch")
    audience_session = feed.audience_session
    observed_at = _parse_time(
        awareness.observed_at,
        "collaboration_observed_at_invalid",
    )
    if (
        audience_session.project != feed.project
        or audience_session.coordination_session_id != feed.coordination_session_id
        or awareness.audience_agent_id != audience_session.identity.agent_id
        or awareness.audience_role != audience_session.identity.role
        or awareness.audience_session_id != audience_session.session_id
    ):
        raise CollaborationContextProjectionError("collaboration_audience_mismatch")
    if audience_session.state != "active":
        raise CollaborationContextProjectionError("collaboration_audience_session_inactive")
    if (
        _parse_time(
            audience_session.last_heartbeat_at,
            "collaboration_audience_session_time_invalid",
        )
        > observed_at
    ):
        raise CollaborationContextProjectionError("collaboration_audience_session_from_future")
    if (
        audience_session.expires_at is not None
        and _parse_time(
            audience_session.expires_at,
            "collaboration_audience_session_time_invalid",
        )
        <= observed_at
    ):
        raise CollaborationContextProjectionError("collaboration_audience_session_expired")
    actual_acceptance_digests = tuple(
        receipt.content_sha256 for receipt in feed.acceptance_receipts
    )
    if actual_acceptance_digests != feed.acceptance_receipt_sha256s:
        raise CollaborationContextProjectionError("acceptance_receipt_digest_mismatch")
    policy_claim = feed.policy_claim
    if policy_claim.audience_session_sha256 != feed.audience_session_sha256:
        raise CollaborationContextProjectionError("context_policy_session_digest_mismatch")
    source_lineage = feed.source_lineage
    source_tuple = source_lineage.source_tuple
    if (
        source_lineage.project != feed.project
        or source_lineage.coordination_session_id != feed.coordination_session_id
    ):
        raise CollaborationContextProjectionError("context_source_lineage_scope_mismatch")
    if (
        feed.read_after_cursor.project != feed.project
        or feed.read_after_cursor.coordination_session_id != feed.coordination_session_id
        or awareness.after_cursor != feed.read_after_cursor
        or page.after_cursor != feed.read_after_cursor
    ):
        raise CollaborationContextProjectionError("collaboration_after_cursor_mismatch")
    if awareness.next_cursor != page.next_cursor or awareness.has_more != page.has_more:
        raise CollaborationContextProjectionError("collaboration_page_cursor_mismatch")
    if awareness.event_page_sha256 != page.content_sha256:
        raise CollaborationContextProjectionError("collaboration_source_binding_mismatch")
    if (
        source_lineage.after_cursor != feed.read_after_cursor
        or source_lineage.next_cursor != page.next_cursor
        or source_lineage.event_page_sha256 != feed.event_page_sha256
        or source_tuple.source_page_digest != feed.event_page_sha256
        or source_lineage.awareness_sha256 != feed.awareness_sha256
        or source_lineage.working_set_sha256 != feed.working_set_sha256
        or source_lineage.visible_event_count != len(page.events)
    ):
        raise CollaborationContextProjectionError("context_source_lineage_mismatch")
    if (
        source_tuple.audience_agent_id != audience_session.identity.agent_id
        or source_tuple.audience_role != audience_session.identity.role
        or source_tuple.audience_session_id != audience_session.session_id
        or source_tuple.audience_session_sha256 != feed.audience_session_sha256
        or source_tuple.agent_session_policy_revision != policy_claim.policy_revision
    ):
        raise CollaborationContextProjectionError("context_source_identity_mismatch")
    authority_binding = feed.authority_binding
    if (
        authority_binding.audience_session_sha256 != feed.audience_session_sha256
        or authority_binding.policy_claim_sha256 != policy_claim.content_sha256
        or authority_binding.source_lineage_sha256 != source_lineage.content_sha256
        or authority_binding.acceptance_receipt_sha256s != feed.acceptance_receipt_sha256s
        or authority_binding.feed_payload_sha256 != feed.feed_payload_sha256
    ):
        raise CollaborationContextProjectionError("context_authority_binding_drift")
    actual_feed_payload_sha256 = _feed_payload_sha256(
        project=feed.project,
        coordination_session_id=feed.coordination_session_id,
        audience_session_sha256=feed.audience_session_sha256,
        read_after_cursor=feed.read_after_cursor,
        memory_context_sha256=feed.memory_context_sha256,
        awareness_sha256=feed.awareness_sha256,
        working_set_sha256=feed.working_set_sha256,
        event_page_sha256=feed.event_page_sha256,
        policy_claim_sha256=policy_claim.content_sha256,
        source_lineage_sha256=source_lineage.content_sha256,
        acceptance_receipt_sha256s=feed.acceptance_receipt_sha256s,
    )
    if actual_feed_payload_sha256 != feed.feed_payload_sha256:
        raise CollaborationContextProjectionError("context_feed_payload_digest_mismatch")

    projection = _awareness_projection(awareness)
    peer_deltas = _mapping_sequence(
        projection.get("peer_deltas"),
        "collaboration_peer_deltas_invalid",
    )
    projected_event_identity = [
        (
            _required_string(item, "event_id", "collaboration_peer_deltas_invalid"),
            _required_string(item, "event_type", "collaboration_peer_deltas_invalid"),
            item.get("work_item_id"),
        )
        for item in peer_deltas
    ]
    source_event_identity = [
        (event.event_id, event.event_type, event.work_item_id) for event in page.events
    ]
    if projected_event_identity != source_event_identity:
        raise CollaborationContextProjectionError("collaboration_event_page_mismatch")


def _collaboration_candidates(
    feed: AuthenticatedCollaborationFeed,
) -> list[dict[str, object]]:
    awareness = feed.awareness
    projection = _awareness_projection(awareness)
    leased_work = _mapping_sequence(
        projection.get("leased_work"),
        "collaboration_leased_work_invalid",
    )
    own_work_ids: set[str] = set()
    dependency_work_ids: set[str] = set()
    work_receipt_sha256_by_id: dict[str, str] = {}
    for work in leased_work:
        work_id = _required_string(work, "work_item_id", "collaboration_leased_work_invalid")
        work_receipt_sha256_by_id[work_id] = _required_string(
            work,
            "receipt_sha256",
            "collaboration_leased_work_invalid",
        )
        assigned_agent_id = _required_string(
            work,
            "assigned_agent_id",
            "collaboration_leased_work_invalid",
        )
        if assigned_agent_id == awareness.audience_agent_id:
            own_work_ids.add(work_id)
            dependency_work_ids.update(
                _string_sequence(
                    work.get("dependency_work_ids"),
                    "collaboration_leased_work_invalid",
                )
            )

    relevance_context = _build_relevance_context(
        feed,
        projection=projection,
        own_work_ids=own_work_ids,
    )

    by_id: dict[str, dict[str, object]] = {}
    for source_name, forced_kind in (("conflicts", "conflict"), ("blockers", "blocker")):
        for item in _mapping_sequence(
            projection.get(source_name),
            f"collaboration_{source_name}_invalid",
        ):
            candidate = _event_candidate(
                item,
                forced_kind=forced_kind,
                awareness=awareness,
                own_work_ids=own_work_ids,
                dependency_work_ids=dependency_work_ids,
                relevance_context=relevance_context,
            )
            _keep_stronger(by_id, candidate)

    for item in _mapping_sequence(
        projection.get("peer_deltas"),
        "collaboration_peer_deltas_invalid",
    ):
        event_type = _required_string(
            item,
            "event_type",
            "collaboration_peer_deltas_invalid",
        )
        kind = {
            "conflict.detected": "conflict",
            "blocker.raised": "blocker",
        }.get(event_type, "progress")
        candidate = _event_candidate(
            item,
            forced_kind=kind,
            awareness=awareness,
            own_work_ids=own_work_ids,
            dependency_work_ids=dependency_work_ids,
            relevance_context=relevance_context,
        )
        _keep_stronger(by_id, candidate)

    accepted_by_receipt: dict[str, list[Mapping[str, object]]] = {}
    accepted_by_digest: dict[str, list[Mapping[str, object]]] = {}
    for item in _mapping_sequence(
        projection.get("accepted_artifacts"),
        "collaboration_accepted_results_invalid",
    ):
        receipt_id = _required_string(
            item,
            "result_receipt_id",
            "collaboration_accepted_results_invalid",
        )
        accepted_by_receipt.setdefault(receipt_id, []).append(item)
        receipt_sha256 = _required_string(
            item,
            "result_receipt_sha256",
            "collaboration_accepted_results_invalid",
        )
        accepted_by_digest.setdefault(receipt_sha256, []).append(item)

    acceptance_by_result = _validated_acceptance_by_result(
        feed,
        projection=projection,
        accepted_by_digest=accepted_by_digest,
        work_receipt_sha256_by_id=work_receipt_sha256_by_id,
    )
    for receipt_id, artifacts in accepted_by_receipt.items():
        result_receipt_sha256 = _required_string(
            artifacts[0],
            "result_receipt_sha256",
            "collaboration_accepted_results_invalid",
        )
        acceptance = acceptance_by_result.get(result_receipt_sha256)
        if acceptance is None:
            # A completed result with artifacts is still only submitted work.
            # Without an independently accepted, server-bound receipt it must
            # not be mislabeled as an accepted result.
            continue
        candidate = _accepted_result_candidate(
            receipt_id,
            artifacts,
            acceptance=acceptance,
            awareness=awareness,
            own_work_ids=own_work_ids,
            dependency_work_ids=dependency_work_ids,
            relevance_context=relevance_context,
        )
        _keep_stronger(by_id, candidate)
    return list(by_id.values())


def _event_candidate(
    item: Mapping[str, object],
    *,
    forced_kind: str,
    awareness: AgentAwarenessProjection,
    own_work_ids: set[str],
    dependency_work_ids: set[str],
    relevance_context: _RelevanceContext,
) -> dict[str, object]:
    code = f"collaboration_{forced_kind}_invalid"
    event_id = _required_string(item, "event_id", code)
    event_type = _required_string(item, "event_type", code)
    summary = _bounded_text(_required_string(item, "summary", code))
    created_at = _required_string(item, "created_at", code)
    work_item_id = _optional_string(item.get("work_item_id"), code)
    actor = item.get("actor")
    if not isinstance(actor, Mapping):
        raise CollaborationContextProjectionError(code)
    actor_id = _required_string(actor, "agent_id", code)
    actor_role = _required_string(actor, "role", code)
    subject_refs = _bounded_strings(
        item.get("subject_refs"),
        code,
        limit=_MAX_SUBJECT_REFS,
    )
    relevance, reasons, signals = _role_relevance(
        kind=forced_kind,
        role=awareness.audience_role,
        audience_agent_id=awareness.audience_agent_id,
        actor_agent_id=actor_id,
        work_item_id=work_item_id,
        own_work_ids=own_work_ids,
        dependency_work_ids=dependency_work_ids,
        created_at=created_at,
        subject_refs=subject_refs,
        event_id=event_id,
        relevance_context=relevance_context,
    )
    return {
        "id": event_id,
        "kind": forced_kind,
        "relevance": relevance,
        "relevance_reasons": reasons,
        "relevance_signals": signals,
        "event_id": event_id,
        "event_type": event_type,
        "summary": summary,
        "actor": {"agent_id": actor_id, "role": actor_role},
        "created_at": created_at,
        "work_item_id": work_item_id,
        "evidence_refs": _bounded_strings(
            item.get("evidence_refs"),
            code,
            limit=_MAX_EVIDENCE_REFS,
        ),
    }


def _accepted_result_candidate(
    receipt_id: str,
    artifacts: Sequence[Mapping[str, object]],
    *,
    acceptance: AcceptanceReceipt,
    awareness: AgentAwarenessProjection,
    own_work_ids: set[str],
    dependency_work_ids: set[str],
    relevance_context: _RelevanceContext,
) -> dict[str, object]:
    first = artifacts[0]
    code = "collaboration_accepted_results_invalid"
    work_item_id = _required_string(first, "work_item_id", code)
    submitted_by = _required_string(first, "submitted_by", code)
    submitted_at = _required_string(first, "submitted_at", code)
    receipt_sha256 = _required_string(first, "result_receipt_sha256", code)
    artifact_refs = []
    evidence_refs: list[str] = []
    for artifact in artifacts:
        if (
            _required_string(artifact, "work_item_id", code) != work_item_id
            or _required_string(artifact, "submitted_by", code) != submitted_by
            or _required_string(artifact, "submitted_at", code) != submitted_at
            or _required_string(artifact, "result_receipt_sha256", code) != receipt_sha256
        ):
            raise CollaborationContextProjectionError(code)
        artifact_refs.append(_bounded_text(_required_string(artifact, "artifact_ref", code)))
        evidence_refs.extend(_bounded_strings(artifact.get("evidence_refs"), code))
    artifact_refs = _unique(artifact_refs)[:_MAX_ARTIFACT_REFS]
    relevance, reasons, signals = _role_relevance(
        kind="accepted_result",
        role=awareness.audience_role,
        audience_agent_id=awareness.audience_agent_id,
        actor_agent_id=submitted_by,
        work_item_id=work_item_id,
        own_work_ids=own_work_ids,
        dependency_work_ids=dependency_work_ids,
        created_at=acceptance.accepted_at,
        subject_refs=artifact_refs,
        event_id=None,
        relevance_context=relevance_context,
    )
    return {
        "id": f"accepted:{receipt_id}",
        "kind": "accepted_result",
        "relevance": relevance,
        "relevance_reasons": reasons,
        "relevance_signals": signals,
        "result_receipt_id": receipt_id,
        "result_receipt_sha256": receipt_sha256,
        "acceptance_receipt_id": acceptance.receipt_id,
        "acceptance_receipt_sha256": acceptance.content_sha256,
        "work_item_id": work_item_id,
        "submitted_by": submitted_by,
        "submitted_at": submitted_at,
        "accepted_by": {
            "agent_id": acceptance.accepted_by.agent_id,
            "role": acceptance.accepted_by.role,
        },
        "accepted_at": acceptance.accepted_at,
        "created_at": acceptance.accepted_at,
        "artifact_refs": artifact_refs,
        "evidence_refs": _unique([*acceptance.evidence_refs, *evidence_refs])[:_MAX_EVIDENCE_REFS],
    }


def _validated_acceptance_by_result(
    feed: AuthenticatedCollaborationFeed,
    *,
    projection: Mapping[str, object],
    accepted_by_digest: Mapping[str, Sequence[Mapping[str, object]]],
    work_receipt_sha256_by_id: Mapping[str, str],
) -> dict[str, AcceptanceReceipt]:
    active_reviewers: set[tuple[str, str]] = set()
    for presence in _mapping_sequence(
        projection.get("agent_presence"),
        "collaboration_agent_presence_invalid",
    ):
        if _required_string(presence, "state", "collaboration_agent_presence_invalid") != "active":
            continue
        active_reviewers.add(
            (
                _required_string(
                    presence,
                    "agent_id",
                    "collaboration_agent_presence_invalid",
                ),
                _required_string(
                    presence,
                    "role",
                    "collaboration_agent_presence_invalid",
                ),
            )
        )

    observed_at = _parse_time(feed.awareness.observed_at, "collaboration_observed_at_invalid")
    by_result: dict[str, AcceptanceReceipt] = {}
    for receipt in feed.acceptance_receipts:
        if (
            receipt.project != feed.project
            or receipt.coordination_session_id != feed.coordination_session_id
        ):
            raise CollaborationContextProjectionError("collaboration_acceptance_scope_mismatch")
        artifacts = accepted_by_digest.get(receipt.result_receipt_sha256)
        if not artifacts:
            raise CollaborationContextProjectionError("collaboration_acceptance_result_unbound")
        first = artifacts[0]
        work_item_id = _required_string(
            first,
            "work_item_id",
            "collaboration_accepted_results_invalid",
        )
        submitted_by = _required_string(
            first,
            "submitted_by",
            "collaboration_accepted_results_invalid",
        )
        submitted_at = _parse_time(
            _required_string(
                first,
                "submitted_at",
                "collaboration_accepted_results_invalid",
            ),
            "collaboration_accepted_results_invalid",
        )
        if receipt.work_item_id != work_item_id:
            raise CollaborationContextProjectionError("collaboration_acceptance_work_mismatch")
        expected_work_receipt_sha256 = work_receipt_sha256_by_id.get(work_item_id)
        if expected_work_receipt_sha256 is None:
            raise CollaborationContextProjectionError(
                "collaboration_acceptance_work_receipt_unbound"
            )
        if receipt.work_receipt_sha256 != expected_work_receipt_sha256:
            raise CollaborationContextProjectionError(
                "collaboration_acceptance_work_receipt_digest_mismatch"
            )
        if receipt.accepted_by.agent_id == submitted_by:
            raise CollaborationContextProjectionError(
                "collaboration_acceptance_independent_reviewer_required"
            )
        if receipt.accepted_by.role not in _ACCEPTANCE_ROLES:
            raise CollaborationContextProjectionError(
                "collaboration_acceptance_reviewer_role_forbidden"
            )
        if (receipt.accepted_by.agent_id, receipt.accepted_by.role) not in active_reviewers:
            raise CollaborationContextProjectionError(
                "collaboration_acceptance_reviewer_not_active"
            )
        accepted_at = _parse_time(
            receipt.accepted_at,
            "collaboration_acceptance_time_invalid",
        )
        if accepted_at < submitted_at:
            raise CollaborationContextProjectionError("collaboration_acceptance_before_result")
        if accepted_at > observed_at:
            raise CollaborationContextProjectionError("collaboration_acceptance_from_future")
        if receipt.result_receipt_sha256 in by_result:
            raise CollaborationContextProjectionError("collaboration_acceptance_result_duplicate")
        by_result[receipt.result_receipt_sha256] = receipt
    return by_result


def _build_relevance_context(
    feed: AuthenticatedCollaborationFeed,
    *,
    projection: Mapping[str, object],
    own_work_ids: set[str],
) -> _RelevanceContext:
    observed_at = _parse_time(
        feed.awareness.observed_at,
        "collaboration_observed_at_invalid",
    )
    audience_refs: dict[str, set[str]] = {
        reference_kind: set() for reference_kind in _REFERENCE_PREFIXES
    }

    def include_view(item: Mapping[str, object], code: str) -> None:
        actor = item.get("actor")
        if not isinstance(actor, Mapping):
            raise CollaborationContextProjectionError(code)
        actor_id = _required_string(actor, "agent_id", code)
        work_item_id = _optional_string(item.get("work_item_id"), code)
        if actor_id != feed.awareness.audience_agent_id and work_item_id not in own_work_ids:
            return
        _merge_reference_profile(
            audience_refs,
            _bounded_strings(
                item.get("subject_refs"),
                code,
                limit=_MAX_SUBJECT_REFS,
            ),
        )

    for source_name in ("blockers", "conflicts", "peer_deltas"):
        code = f"collaboration_{source_name}_invalid"
        for item in _mapping_sequence(projection.get(source_name), code):
            include_view(item, code)

    for artifact in _mapping_sequence(
        projection.get("accepted_artifacts"),
        "collaboration_accepted_results_invalid",
    ):
        if (
            _required_string(
                artifact,
                "work_item_id",
                "collaboration_accepted_results_invalid",
            )
            in own_work_ids
        ):
            _merge_reference_profile(
                audience_refs,
                (
                    _required_string(
                        artifact,
                        "artifact_ref",
                        "collaboration_accepted_results_invalid",
                    ),
                ),
            )

    events_by_id: dict[str, object] = {}
    parent_by_event_id: dict[str, str | None] = {}
    roots: set[str] = set()
    for event in feed.event_page.events:
        if event.event_id in events_by_id:
            raise CollaborationContextProjectionError("collaboration_event_id_duplicate")
        events_by_id[event.event_id] = event
        parent_by_event_id[event.event_id] = event.causal_parent_event_id
        if (
            event.actor.agent_id == feed.awareness.audience_agent_id
            or event.work_item_id in own_work_ids
        ):
            roots.add(event.event_id)

    causal_distances = _causal_distances(parent_by_event_id, roots)
    for claim in feed.source_lineage.causal_distances:
        if claim.event_id not in parent_by_event_id:
            raise CollaborationContextProjectionError("context_lineage_causal_event_not_visible")
        local_distance = causal_distances[claim.event_id]
        if local_distance is not None and local_distance != claim.distance:
            raise CollaborationContextProjectionError("context_lineage_causal_distance_conflict")
        if local_distance is None:
            causal_distances[claim.event_id] = claim.distance
    return _RelevanceContext(
        observed_at=observed_at,
        audience_refs={
            reference_kind: frozenset(values) for reference_kind, values in audience_refs.items()
        },
        causal_distance_by_event_id=causal_distances,
    )


def _causal_distances(
    parent_by_event_id: Mapping[str, str | None],
    roots: set[str],
) -> dict[str, int | None]:
    for start in parent_by_event_id:
        seen: set[str] = set()
        current = start
        while current in parent_by_event_id:
            if current in seen:
                raise CollaborationContextProjectionError("collaboration_causal_cycle")
            seen.add(current)
            parent = parent_by_event_id[current]
            if parent is None or parent not in parent_by_event_id:
                break
            current = parent

    distances: dict[str, int | None] = {}
    for event_id in parent_by_event_id:
        current = event_id
        distance = 0
        resolved: int | None = None
        while current in parent_by_event_id:
            if current in roots:
                resolved = distance
                break
            parent = parent_by_event_id[current]
            if parent is None or parent not in parent_by_event_id:
                break
            current = parent
            distance += 1
        distances[event_id] = resolved
    return distances


def _merge_reference_profile(
    target: Mapping[str, set[str]],
    refs: Sequence[str],
) -> None:
    for reference in refs:
        reference_kind = _reference_kind(reference)
        if reference_kind is not None:
            target[reference_kind].add(reference)


def _reference_kind(reference: str) -> str | None:
    normalized = reference.strip().casefold()
    for reference_kind, prefixes in _REFERENCE_PREFIXES.items():
        if normalized.startswith(prefixes):
            return reference_kind
    return None


def _role_relevance(
    *,
    kind: str,
    role: str,
    audience_agent_id: str,
    actor_agent_id: str,
    work_item_id: str | None,
    own_work_ids: set[str],
    dependency_work_ids: set[str],
    created_at: str,
    subject_refs: Sequence[str],
    event_id: str | None,
    relevance_context: _RelevanceContext,
) -> tuple[float, list[str], dict[str, object]]:
    score = _BASE_RELEVANCE[kind]
    reasons = [f"kind:{kind}"]
    if work_item_id in own_work_ids:
        score += 0.08
        reasons.append("audience-work")
    elif work_item_id in dependency_work_ids:
        score += 0.05
        reasons.append("work-dependency")
    if actor_agent_id == audience_agent_id:
        score += 0.02
        reasons.append("audience-authored")
    if role == "coordinator" and kind in {"conflict", "blocker"}:
        score += 0.05
        reasons.append("coordinator-risk")
    elif role in {"reviewer", "deepsec_reviewer"} and kind in {
        "conflict",
        "accepted_result",
    }:
        score += 0.05
        reasons.append("reviewer-verification")
    elif role in {"implementer", "builder"} and work_item_id in own_work_ids:
        score += 0.03
        reasons.append("implementation-owner")

    created = _parse_time(created_at, "collaboration_candidate_time_invalid")
    age_seconds = (relevance_context.observed_at - created).total_seconds()
    if age_seconds < 0:
        raise CollaborationContextProjectionError("collaboration_candidate_from_future")
    freshness = max(
        0.0,
        min(1.0, 1.0 - (age_seconds / _FRESHNESS_HORIZON_SECONDS)),
    )
    severity = _SEVERITY[kind]
    score += 0.04 * freshness
    score += 0.02 * severity

    candidate_refs: dict[str, set[str]] = {
        reference_kind: set() for reference_kind in _REFERENCE_PREFIXES
    }
    _merge_reference_profile(candidate_refs, subject_refs)
    reference_overlap: dict[str, int] = {}
    for reference_kind in _REFERENCE_PREFIXES:
        overlap_count = len(
            candidate_refs[reference_kind] & relevance_context.audience_refs[reference_kind]
        )
        reference_overlap[reference_kind] = overlap_count
        if overlap_count:
            score += _REFERENCE_WEIGHTS[reference_kind]
            reasons.append(f"same-{reference_kind}")

    causal_distance = (
        relevance_context.causal_distance_by_event_id.get(event_id)
        if event_id is not None
        else None
    )
    if causal_distance is not None:
        score += 0.03 / (causal_distance + 1)
        reasons.append(f"causal-distance:{causal_distance}")
    if not math.isfinite(score):
        raise CollaborationContextProjectionError("collaboration_relevance_invalid")
    signals: dict[str, object] = {
        "severity": severity,
        "freshness": round(freshness, 4),
        "causal_distance": causal_distance,
        "reference_overlap": reference_overlap,
    }
    return round(min(1.0, max(0.0, score)), 4), reasons, signals


def _base_collaboration_projection(
    *,
    feed: AuthenticatedCollaborationFeed,
    budget: CollaborationContextBudget,
    requested_counts: Mapping[str, int],
) -> dict[str, object]:
    awareness = feed.awareness
    return {
        "schema_version": COLLABORATION_CONTEXT_SCHEMA,
        "authority": "non-authoritative-context-projection",
        "canonical_memory_effect": "none",
        "project_id": feed.project.project_id,
        "coordination_session_id": feed.coordination_session_id,
        "audience": {
            "agent_id": feed.audience_session.identity.agent_id,
            "role": feed.audience_session.identity.role,
            "session_id": feed.audience_session.session_id,
        },
        "observed_at": awareness.observed_at,
        "goal_summary": _bounded_text(awareness.goal_summary),
        "plan_revision": awareness.plan_revision,
        "relevance_policy": {
            "id": COLLABORATION_RELEVANCE_POLICY,
            "strict_kind_order": ["conflict", "blocker", "accepted_result", "progress"],
            "signals": [
                "role",
                "work-ownership",
                "module",
                "symbol",
                "artifact",
                "decision",
                "freshness",
                "severity",
                "causal-distance",
            ],
        },
        "runtime_authority": {
            "binding_id": feed.authority_binding.binding_id,
            "binding_sha256": feed.authority_binding.content_sha256,
            "feed_payload_sha256": feed.feed_payload_sha256,
            "server_feed_binding": "pr4-process-local-server-bound",
            "security_boundary": "pr4-authenticated-process-local",
            "python_factory_is_validation_seam_only": True,
            "policy_binding": "verified-on-every-compose",
            "source_lineage": "verified-on-every-compose",
            "redacted_projection": "verified-on-every-compose",
            "acceptance_receipts": "verified-on-every-compose-if-present",
            "persistent_head_authority": "pr5-durable-canonical-adapter",
            "restart_recovery": "deferred-to-pr5",
            "expires_at": feed.authority_binding.expires_at,
        },
        "source_tuple": {
            **feed.source_lineage.source_tuple.to_dict(),
            "source_tuple_sha256": feed.source_lineage.source_tuple.content_sha256,
            "source_receipt_id": feed.source_lineage.source_receipt_id,
            "source_receipt_sha256": feed.source_lineage.source_receipt_sha256,
            "source_anchor_sha256": feed.source_lineage.source_anchor_sha256,
            "source_head_cursor": feed.source_lineage.source_head_cursor.to_dict(),
            "source_lineage_sha256": feed.source_lineage.content_sha256,
            "acceptance_receipts": [
                {
                    "acceptance_receipt_id": receipt.receipt_id,
                    "receipt_sha256": receipt.content_sha256,
                }
                for receipt in feed.acceptance_receipts
            ],
        },
        "items": [],
        "cursor": {
            "after": feed.read_after_cursor.to_dict(),
            "next": awareness.next_cursor.to_dict(),
            "has_more": awareness.has_more,
            "source_event_count": len(feed.event_page.events),
        },
        "source_digests": {
            "memory_context": feed.memory_context_sha256,
            "audience_session": feed.audience_session_sha256,
            "awareness": feed.awareness_sha256,
            "event_page": feed.event_page_sha256,
            "working_set": feed.working_set_sha256,
            "acceptance_receipts": list(feed.acceptance_receipt_sha256s),
            "policy_claim": feed.policy_claim.content_sha256,
            "source_lineage": feed.source_lineage.content_sha256,
            "authority_binding": feed.authority_binding.content_sha256,
            "feed_payload": feed.feed_payload_sha256,
            "feed_binding": feed.feed_payload_sha256,
        },
        "budget": {
            "max_items": budget.max_items,
            "max_collaboration_bytes": budget.max_collaboration_bytes,
            "max_response_bytes": budget.max_response_bytes,
            "requested_by_kind": dict(requested_counts),
            "emitted_by_kind": _zero_kind_counts(),
            "omitted_by_kind": dict(requested_counts),
        },
    }


def _with_emitted_items(
    base: Mapping[str, object],
    *,
    items: list[dict[str, object]],
    requested_counts: Mapping[str, int],
) -> dict[str, object]:
    emitted_counts = _count_kinds(items)
    result = dict(base)
    result["items"] = items
    budget = dict(result["budget"])  # type: ignore[arg-type]
    budget["emitted_by_kind"] = emitted_counts
    budget["omitted_by_kind"] = {
        kind: requested_counts.get(kind, 0) - emitted_counts.get(kind, 0) for kind in _PRIORITY
    }
    result["budget"] = budget
    return result


def _require_base_budget(
    result: Mapping[str, object],
    collaboration: Mapping[str, object],
    budget: CollaborationContextBudget,
) -> None:
    if not _within_budget(result, collaboration, budget):
        raise CollaborationContextProjectionError("context_response_budget_exceeded")


def _within_budget(
    result: Mapping[str, object],
    collaboration: Mapping[str, object],
    budget: CollaborationContextBudget,
) -> bool:
    return (
        _json_size(collaboration) <= budget.max_collaboration_bytes
        and _json_size(result) <= budget.max_response_bytes
    )


def _keep_stronger(
    candidates: dict[str, dict[str, object]],
    candidate: dict[str, object],
) -> None:
    candidate_id = str(candidate["id"])
    current = candidates.get(candidate_id)
    if current is None or _PRIORITY[str(candidate["kind"])] > _PRIORITY[str(current["kind"])]:
        candidates[candidate_id] = candidate


def _count_kinds(items: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts = _zero_kind_counts()
    for item in items:
        kind = str(item.get("kind") or "")
        if kind not in counts:
            raise CollaborationContextProjectionError("collaboration_kind_invalid")
        counts[kind] += 1
    return counts


def _zero_kind_counts() -> dict[str, int]:
    return dict.fromkeys(_PRIORITY, 0)


def _mapping_sequence(value: object, code: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CollaborationContextProjectionError(code)
    if len(value) > 64:
        raise CollaborationContextProjectionError(code)
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CollaborationContextProjectionError(code)
        result.append(item)
    return result


def _awareness_projection(
    awareness: AgentAwarenessProjection,
) -> Mapping[str, object]:
    payload = awareness.to_dict()
    projection = payload.get("projection")
    if not isinstance(projection, Mapping):
        raise CollaborationContextProjectionError("collaboration_awareness_projection_invalid")
    return projection


def _normalize_acceptance_receipts(
    value: Sequence[AcceptanceReceipt],
) -> tuple[AcceptanceReceipt, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CollaborationContextProjectionError("context_feed_acceptance_receipts_invalid")
    if len(value) > 64 or any(not isinstance(item, AcceptanceReceipt) for item in value):
        raise CollaborationContextProjectionError("context_feed_acceptance_receipts_invalid")
    receipts = tuple(
        sorted(
            value,
            key=lambda item: (item.result_receipt_sha256, item.receipt_id),
        )
    )
    if len({item.receipt_id for item in receipts}) != len(receipts):
        raise CollaborationContextProjectionError("context_feed_acceptance_receipt_duplicate")
    if len({item.result_receipt_sha256 for item in receipts}) != len(receipts):
        raise CollaborationContextProjectionError("context_feed_acceptance_result_duplicate")
    return receipts


def _string_sequence(value: object, code: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CollaborationContextProjectionError(code)
    if len(value) > 64 or any(not isinstance(item, str) for item in value):
        raise CollaborationContextProjectionError(code)
    return list(value)


def _bounded_strings(
    value: object,
    code: str,
    *,
    limit: int = _MAX_EVIDENCE_REFS,
) -> list[str]:
    return [_bounded_text(item) for item in _string_sequence(value, code)[:limit]]


def _required_string(value: Mapping[str, object], key: str, code: str) -> str:
    resolved = value.get(key)
    if not isinstance(resolved, str) or not resolved:
        raise CollaborationContextProjectionError(code)
    return resolved


def _optional_string(value: object, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CollaborationContextProjectionError(code)
    return value


def _bounded_text(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_TEXT_BYTES:
        return value
    return encoded[:_MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def _parse_time(value: str, code: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CollaborationContextProjectionError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollaborationContextProjectionError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollaborationContextProjectionError(code)
    return parsed.astimezone(timezone.utc)


def _optional_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_time(value, "context_timestamp_invalid")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CollaborationContextProjectionError("context_timestamp_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _binding_ttl(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_BINDING_TTL_SECONDS
    ):
        raise CollaborationContextProjectionError("context_authority_ttl_invalid")
    return value


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _feed_payload_sha256(
    *,
    project: ProjectScope,
    coordination_session_id: str,
    audience_session_sha256: str,
    read_after_cursor: EventCursor,
    memory_context_sha256: str,
    awareness_sha256: str,
    working_set_sha256: str,
    event_page_sha256: str,
    policy_claim_sha256: str,
    source_lineage_sha256: str,
    acceptance_receipt_sha256s: Sequence[str],
) -> str:
    return _content_sha256(
        {
            "project_id": project.project_id,
            "coordination_session_id": coordination_session_id,
            "audience_session_sha256": audience_session_sha256,
            "read_after_cursor": read_after_cursor.to_dict(),
            "memory_context_sha256": memory_context_sha256,
            "awareness_sha256": awareness_sha256,
            "working_set_sha256": working_set_sha256,
            "event_page_sha256": event_page_sha256,
            "policy_claim_sha256": policy_claim_sha256,
            "source_lineage_sha256": source_lineage_sha256,
            "acceptance_receipt_sha256s": list(acceptance_receipt_sha256s),
        }
    )


def _content_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CollaborationContextProjectionError("context_json_invalid") from exc


def _json_clone(value: object, code: str) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise CollaborationContextProjectionError(code)
        return json.loads(encoded)
    except CollaborationContextProjectionError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CollaborationContextProjectionError(code) from exc


def _json_size(value: object) -> int:
    return len(_canonical_json(value).encode("utf-8"))
