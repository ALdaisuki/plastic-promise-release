"""Compile passive collaboration facts and publish server-owned Hook events.

The public compiler in this module is side-effect free: it converts bounded,
typed work facts into one ``CollaborationEvent`` and, for independently
accepted work only, a command candidate for a *pending* memory proposal.  The
server-only runtime adds a process-local opaque-reference registry and an
idempotent event-log append seam for Codex Stop Hooks.

Neither layer mutates Hook state, enqueues work, persists a proposal, or writes
canonical memory.  Durable Agent/work registries and proposal promotion remain
behind the later PR5 server adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from .acceptance_receipt import (
    ACCEPTANCE_RECEIPT_SCHEMA,
    AcceptanceReceipt,
    AcceptanceReceiptAuthority,
    AcceptanceReceiptError,
    VerifiedAcceptanceReceipt,
)
from .contracts import (
    AgentIdentity,
    CollaborationContractError,
    CollaborationEvent,
    EventCursor,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)

PASSIVE_COLLABORATION_BRIDGE_SCHEMA = "passive-collaboration-bridge/v1"
PROMOTION_CANDIDATE_SCHEMA = "collaboration-promotion-candidate/v1"
PASSIVE_COLLABORATION_PUBLICATION_SCHEMA = "passive-collaboration-publication/v1"

_BRIDGE_KINDS = frozenset({"progress", "submitted", "accepted"})
_BRIDGE_SOURCES = frozenset({"stop_hook"})
_EVENT_TYPE_BY_KIND = {
    "progress": "work.progressed",
    "submitted": "work.submitted",
    "accepted": "work.accepted",
}
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_HOOK_SCOPE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_COLLABORATION_REF_RE = re.compile(r"\Acollaboration-ref:[A-Za-z0-9_-]{32}\Z")
_SERVER_PASSIVE_RUNTIME_TOKEN = object()


class PassiveCollaborationBridgeError(ValueError):
    """Stable, non-sensitive failure raised by the passive bridge."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PassiveCollaborationSource:
    """Server-owned typed work context bound to one Codex Hook turn.

    The Hook never constructs or serializes this value.  It receives only the
    opaque reference issued by :class:`PassiveCollaborationRuntime`.  The
    presence of a result and independently verified acceptance proof selects
    submitted/accepted semantics; caller text cannot select the event kind.
    """

    hook_session_id: str
    hook_turn_id: str
    work: WorkReceipt
    progress_summary: str | None = None
    result: ResultReceipt | None = None
    acceptance: VerifiedAcceptanceReceipt | None = None
    causal_parent_event_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    audience_roles: tuple[str, ...] = ()
    audience_agent_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        hook_session_id = _require_hook_scope_id(
            self.hook_session_id,
            "passive_collaboration_hook_session_invalid",
        )
        hook_turn_id = _require_hook_scope_id(
            self.hook_turn_id,
            "passive_collaboration_hook_turn_invalid",
        )
        if not isinstance(self.work, WorkReceipt):
            raise PassiveCollaborationBridgeError("passive_bridge_work_receipt_invalid")
        if self.result is not None:
            _require_work_result_binding(self.work, self.result)
        if self.acceptance is not None:
            if self.result is None:
                raise PassiveCollaborationBridgeError("passive_bridge_acceptance_result_required")
            if not isinstance(self.acceptance, VerifiedAcceptanceReceipt):
                raise PassiveCollaborationBridgeError("passive_bridge_acceptance_receipt_invalid")
            _require_acceptance_binding(self.work, self.result, self.acceptance)
            if self.acceptance.accepted_by == self.result.submitted_by:
                raise PassiveCollaborationBridgeError(
                    "passive_bridge_independent_acceptance_required"
                )
        if self.result is None and self.progress_summary is None:
            raise PassiveCollaborationBridgeError("passive_collaboration_progress_summary_required")
        if self.result is not None and self.progress_summary is not None:
            raise PassiveCollaborationBridgeError(
                "passive_collaboration_result_progress_summary_forbidden"
            )

        kind = self.kind
        actor = self.actor
        source_occurred_at = (
            self.acceptance.accepted_at
            if self.acceptance is not None
            else self.result.submitted_at
            if self.result is not None
            else self.work.issued_at
        )
        summary = self.result.summary if self.result is not None else self.progress_summary or ""
        probe = CollaborationEvent(
            event_id="event:passive-source-validator",
            project=self.work.project,
            coordination_session_id=self.work.coordination_session_id,
            actor=actor,
            event_type=_EVENT_TYPE_BY_KIND[kind],
            summary=summary,
            created_at=source_occurred_at,
            causal_parent_event_id=self.causal_parent_event_id,
            work_item_id=self.work.work_item_id,
            evidence_refs=self.evidence_refs,
            audience_roles=self.audience_roles,
            audience_agent_ids=self.audience_agent_ids,
        )
        object.__setattr__(self, "hook_session_id", hook_session_id)
        object.__setattr__(self, "hook_turn_id", hook_turn_id)
        object.__setattr__(self, "progress_summary", None if self.result else probe.summary)
        object.__setattr__(self, "causal_parent_event_id", probe.causal_parent_event_id)
        object.__setattr__(self, "evidence_refs", probe.evidence_refs)
        object.__setattr__(self, "audience_roles", probe.audience_roles)
        object.__setattr__(self, "audience_agent_ids", probe.audience_agent_ids)

    @property
    def kind(self) -> str:
        if self.acceptance is not None:
            return "accepted"
        if self.result is not None:
            return "submitted"
        return "progress"

    @property
    def actor(self) -> AgentIdentity:
        if self.acceptance is not None:
            return self.acceptance.accepted_by
        if self.result is not None:
            return self.result.submitted_by
        return self.work.assigned_agent

    @property
    def project(self) -> ProjectScope:
        return self.work.project

    @property
    def binding(self) -> tuple[str, str, str]:
        return (
            self.work.project.project_id,
            self.hook_session_id,
            self.hook_turn_id,
        )

    @property
    def content_sha256(self) -> str:
        return _digest(
            {
                "project_id": self.work.project.project_id,
                "hook_session_id": self.hook_session_id,
                "hook_turn_id": self.hook_turn_id,
                "work_receipt_sha256": self.work.content_sha256,
                "progress_summary": self.progress_summary,
                "result_receipt_sha256": (
                    None if self.result is None else self.result.content_sha256
                ),
                "acceptance_receipt_sha256": (
                    None if self.acceptance is None else self.acceptance.content_sha256
                ),
                "causal_parent_event_id": self.causal_parent_event_id,
                "evidence_refs": list(self.evidence_refs),
                "audience_roles": list(self.audience_roles),
                "audience_agent_ids": list(self.audience_agent_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class PassiveCollaborationPublication:
    """Bounded outcome returned to the Hook after a server-owned publish attempt."""

    status: str
    reason: str
    event_id: str | None = None
    event_type: str | None = None
    event_sequence: int | None = None
    promotion_status: str = "not-applicable"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PASSIVE_COLLABORATION_PUBLICATION_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_sequence": self.event_sequence,
            "promotion_status": self.promotion_status,
            "canonical_memory_effect": "none",
            "durable_collaboration_context": "deferred-to-pr5",
        }


class PassiveCollaborationRuntime:
    """Process-local PR3 source registry plus idempotent Stop publisher.

    Durable AgentRegistry/ProjectWorkBoard state, lifecycle reconciliation, and
    pending-proposal persistence remain PR5 work.  This module owns only the
    bounded source/reference mapping and event append attempt for the current
    server process.
    """

    __slots__ = (
        "_acceptance_authorities",
        "_append_event",
        "_clock",
        "_lock",
        "_plans",
        "_publishing",
        "_publications",
        "_references_by_binding",
        "_sources",
    )

    def __init__(
        self,
        *,
        append_event: Callable[[CollaborationEvent], EventCursor],
        clock: Callable[[], datetime],
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_PASSIVE_RUNTIME_TOKEN:
            raise PassiveCollaborationBridgeError("passive_collaboration_runtime_server_required")
        if not callable(append_event) or not callable(clock):
            raise PassiveCollaborationBridgeError(
                "passive_collaboration_runtime_dependency_invalid"
            )
        self._append_event = append_event
        self._clock = clock
        self._lock = threading.RLock()
        self._sources: dict[str, PassiveCollaborationSource] = {}
        self._references_by_binding: dict[tuple[str, str, str], str] = {}
        self._acceptance_authorities: dict[str, AcceptanceReceiptAuthority] = {}
        self._plans: dict[str, PassiveCollaborationPlan] = {}
        self._publishing: set[str] = set()
        self._publications: dict[
            str,
            tuple[PassiveCollaborationPlan, EventCursor],
        ] = {}

    def register_source(
        self,
        source: PassiveCollaborationSource,
        *,
        acceptance_authority: AcceptanceReceiptAuthority | None = None,
    ) -> str:
        """Register or monotonically refine one server-owned Hook-turn source."""

        if not isinstance(source, PassiveCollaborationSource):
            raise PassiveCollaborationBridgeError("passive_collaboration_source_invalid")
        if source.acceptance is not None and not isinstance(
            acceptance_authority,
            AcceptanceReceiptAuthority,
        ):
            raise PassiveCollaborationBridgeError("passive_bridge_acceptance_authority_required")
        if source.acceptance is None and acceptance_authority is not None:
            raise PassiveCollaborationBridgeError(
                "passive_collaboration_acceptance_authority_unexpected"
            )

        with self._lock:
            reference = self._references_by_binding.get(source.binding)
            if reference is None:
                reference = self._new_reference()
                self._references_by_binding[source.binding] = reference
            else:
                existing = self._sources[reference]
                if reference in self._publishing:
                    existing_authority = self._acceptance_authorities.get(reference)
                    if (
                        existing.content_sha256 == source.content_sha256
                        and existing_authority is acceptance_authority
                    ):
                        return reference
                    raise PassiveCollaborationBridgeError(
                        "passive_collaboration_source_publish_in_progress"
                    )
                if reference in self._publications:
                    if existing.content_sha256 == source.content_sha256:
                        return reference
                    raise PassiveCollaborationBridgeError(
                        "passive_collaboration_source_already_published"
                    )
                _require_source_transition(existing, source)
                if existing.content_sha256 != source.content_sha256:
                    self._plans.pop(reference, None)

            self._sources[reference] = source
            if acceptance_authority is None:
                self._acceptance_authorities.pop(reference, None)
            else:
                self._acceptance_authorities[reference] = acceptance_authority
            return reference

    def reference_for(
        self,
        *,
        project: ProjectScope,
        hook_session_id: str,
        hook_turn_id: str,
    ) -> str | None:
        """Return the opaque reference for an exact Hook scope, if registered."""

        if not isinstance(project, ProjectScope):
            return None
        try:
            binding = (
                project.project_id,
                _require_hook_scope_id(
                    hook_session_id,
                    "passive_collaboration_hook_session_invalid",
                ),
                _require_hook_scope_id(
                    hook_turn_id,
                    "passive_collaboration_hook_turn_invalid",
                ),
            )
        except PassiveCollaborationBridgeError:
            return None
        with self._lock:
            return self._references_by_binding.get(binding)

    def publish_stop(
        self,
        *,
        collaboration_ref: str | None,
        project: ProjectScope,
        hook_session_id: str,
        hook_turn_id: str,
    ) -> PassiveCollaborationPublication:
        """Compile and append one Hook-turn event without trusting Hook content."""

        if collaboration_ref is None:
            return _skipped_publication("passive_collaboration_context_absent")
        if not isinstance(collaboration_ref, str) or not _COLLABORATION_REF_RE.fullmatch(
            collaboration_ref
        ):
            return PassiveCollaborationPublication(
                status="rejected",
                reason="passive_collaboration_reference_invalid",
            )
        if not isinstance(project, ProjectScope):
            return PassiveCollaborationPublication(
                status="rejected",
                reason="passive_collaboration_project_invalid",
            )
        try:
            binding = (
                project.project_id,
                _require_hook_scope_id(
                    hook_session_id,
                    "passive_collaboration_hook_session_invalid",
                ),
                _require_hook_scope_id(
                    hook_turn_id,
                    "passive_collaboration_hook_turn_invalid",
                ),
            )
        except PassiveCollaborationBridgeError as exc:
            return PassiveCollaborationPublication(status="rejected", reason=exc.code)

        with self._lock:
            source = self._sources.get(collaboration_ref)
            if source is None:
                return _skipped_publication("passive_collaboration_context_unavailable")
            if source.binding != binding:
                return PassiveCollaborationPublication(
                    status="rejected",
                    reason="passive_collaboration_scope_mismatch",
                )
            recorded = self._publications.get(collaboration_ref)
            if recorded is not None:
                plan, cursor = recorded
                return _recorded_publication("duplicate", plan, cursor)
            if collaboration_ref in self._publishing:
                return PassiveCollaborationPublication(
                    status="retry",
                    reason="passive_collaboration_event_append_in_progress",
                )

            plan = self._plans.get(collaboration_ref)
            if plan is None:
                try:
                    plan = self._compile_source(
                        source,
                        acceptance_authority=self._acceptance_authorities.get(collaboration_ref),
                    )
                except (PassiveCollaborationBridgeError, CollaborationContractError) as exc:
                    reason = getattr(exc, "code", "passive_collaboration_source_rejected")
                    return PassiveCollaborationPublication(status="rejected", reason=reason)
                self._plans[collaboration_ref] = plan
            self._publishing.add(collaboration_ref)

        try:
            try:
                cursor = self._append_event(plan.event)
            except CollaborationContractError as exc:
                if exc.code in {
                    "collaboration_event_id_conflict",
                    "collaboration_parent_not_found",
                    "collaboration_parent_scope_mismatch",
                }:
                    return PassiveCollaborationPublication(status="rejected", reason=exc.code)
                return PassiveCollaborationPublication(
                    status="retry",
                    reason="passive_collaboration_event_append_unavailable",
                )
            except Exception:
                return PassiveCollaborationPublication(
                    status="retry",
                    reason="passive_collaboration_event_append_unavailable",
                )
            if (
                not isinstance(cursor, EventCursor)
                or cursor.project != plan.event.project
                or cursor.coordination_session_id != plan.event.coordination_session_id
                or cursor.sequence < 1
            ):
                return PassiveCollaborationPublication(
                    status="retry",
                    reason="passive_collaboration_event_cursor_invalid",
                )
            with self._lock:
                self._publications[collaboration_ref] = (plan, cursor)
            return _recorded_publication("recorded", plan, cursor)
        finally:
            with self._lock:
                self._publishing.discard(collaboration_ref)

    def _compile_source(
        self,
        source: PassiveCollaborationSource,
        *,
        acceptance_authority: AcceptanceReceiptAuthority | None,
    ) -> PassiveCollaborationPlan:
        server_observed_at_utc = _utc_text(self._clock())
        return compile_passive_collaboration(
            PassiveCollaborationInput(
                kind=source.kind,
                source="stop_hook",
                project=source.project,
                coordination_session_id=source.work.coordination_session_id,
                actor=source.actor,
                work=source.work,
                server_observed_at_utc=server_observed_at_utc,
                summary=source.progress_summary,
                result=source.result,
                acceptance=source.acceptance,
                causal_parent_event_id=source.causal_parent_event_id,
                evidence_refs=source.evidence_refs,
                audience_roles=source.audience_roles,
                audience_agent_ids=source.audience_agent_ids,
            ),
            acceptance_authority=acceptance_authority,
        )

    def _new_reference(self) -> str:
        while True:
            reference = f"collaboration-ref:{secrets.token_urlsafe(24)}"
            if reference not in self._sources:
                return reference


def open_server_passive_collaboration_runtime(
    *,
    append_event: Callable[[CollaborationEvent], EventCursor],
    clock: Callable[[], datetime] | None = None,
) -> PassiveCollaborationRuntime:
    """Open the process-local PR3 source registry inside server wiring only."""

    return PassiveCollaborationRuntime(
        append_event=append_event,
        clock=clock or (lambda: datetime.now(timezone.utc)),
        _server_token=_SERVER_PASSIVE_RUNTIME_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class PassiveCollaborationInput:
    """Bounded public input from one Stop Hook compilation invocation."""

    kind: str
    source: str
    project: ProjectScope
    coordination_session_id: str
    actor: AgentIdentity
    work: WorkReceipt
    server_observed_at_utc: str
    summary: str | None = None
    result: ResultReceipt | None = None
    acceptance: VerifiedAcceptanceReceipt | None = None
    expires_at: str | None = None
    causal_parent_event_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    audience_roles: tuple[str, ...] = ()
    audience_agent_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().casefold()
        source = str(self.source or "").strip().casefold()
        if kind not in _BRIDGE_KINDS:
            raise PassiveCollaborationBridgeError("passive_bridge_kind_invalid")
        if source not in _BRIDGE_SOURCES:
            raise PassiveCollaborationBridgeError("passive_bridge_source_invalid")
        if not isinstance(self.project, ProjectScope):
            raise PassiveCollaborationBridgeError("passive_bridge_project_invalid")
        if not isinstance(self.actor, AgentIdentity):
            raise PassiveCollaborationBridgeError("passive_bridge_actor_invalid")
        if not isinstance(self.work, WorkReceipt):
            raise PassiveCollaborationBridgeError("passive_bridge_work_receipt_invalid")
        if self.result is not None and not isinstance(self.result, ResultReceipt):
            raise PassiveCollaborationBridgeError("passive_bridge_result_receipt_invalid")
        if self.acceptance is not None and not isinstance(
            self.acceptance,
            VerifiedAcceptanceReceipt,
        ):
            raise PassiveCollaborationBridgeError("passive_bridge_acceptance_receipt_invalid")

        if kind == "progress":
            if self.result is not None or self.acceptance is not None:
                raise PassiveCollaborationBridgeError("passive_bridge_progress_receipt_forbidden")
            if self.summary is None:
                raise PassiveCollaborationBridgeError("passive_bridge_progress_summary_required")
            effective_summary = self.summary
        else:
            if self.result is None:
                raise PassiveCollaborationBridgeError("passive_bridge_result_required")
            if self.summary is not None and self.summary != self.result.summary:
                raise PassiveCollaborationBridgeError("passive_bridge_summary_result_mismatch")
            effective_summary = self.result.summary
            if kind == "submitted" and self.acceptance is not None:
                raise PassiveCollaborationBridgeError(
                    "passive_bridge_submitted_acceptance_forbidden"
                )
            if kind == "accepted" and self.acceptance is None:
                raise PassiveCollaborationBridgeError("passive_bridge_acceptance_required")

        server_observed_at_utc = _normalize_server_utc_time(
            self.server_observed_at_utc,
        )
        event_occurred_at = (
            self.acceptance.accepted_at
            if kind == "accepted" and self.acceptance is not None
            else self.result.submitted_at
            if kind == "submitted" and self.result is not None
            else server_observed_at_utc
        )
        _require_not_after(
            event_occurred_at,
            server_observed_at_utc,
            "passive_collaboration_source_time_from_future",
        )
        probe = CollaborationEvent(
            event_id="event:passive-bridge-input",
            project=self.project,
            coordination_session_id=self.coordination_session_id,
            actor=self.actor,
            event_type=_EVENT_TYPE_BY_KIND[kind],
            summary=effective_summary,
            created_at=event_occurred_at,
            expires_at=self.expires_at,
            causal_parent_event_id=self.causal_parent_event_id,
            work_item_id=self.work.work_item_id,
            evidence_refs=self.evidence_refs,
            audience_roles=self.audience_roles,
            audience_agent_ids=self.audience_agent_ids,
        )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "coordination_session_id", probe.coordination_session_id)
        object.__setattr__(self, "server_observed_at_utc", server_observed_at_utc)
        object.__setattr__(self, "summary", probe.summary)
        object.__setattr__(self, "expires_at", probe.expires_at)
        object.__setattr__(self, "causal_parent_event_id", probe.causal_parent_event_id)
        object.__setattr__(self, "evidence_refs", probe.evidence_refs)
        object.__setattr__(self, "audience_roles", probe.audience_roles)
        object.__setattr__(self, "audience_agent_ids", probe.audience_agent_ids)


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """A pending-proposal command; never a canonical-memory write command."""

    candidate_id: str
    project: ProjectScope
    coordination_session_id: str
    work_item_id: str
    source_event_id: str
    source_event_sha256: str
    work_receipt_sha256: str
    result_receipt_sha256: str
    acceptance_receipt_sha256: str
    summary: str
    evidence_refs: tuple[str, ...]
    idempotency_sha256: str
    reason_code: str = "passive_bridge_accepted_work_pending_proposal"

    def __post_init__(self) -> None:
        for digest, code in (
            (self.source_event_sha256, "passive_bridge_candidate_event_digest_invalid"),
            (self.work_receipt_sha256, "passive_bridge_candidate_work_digest_invalid"),
            (self.result_receipt_sha256, "passive_bridge_candidate_result_digest_invalid"),
            (
                self.acceptance_receipt_sha256,
                "passive_bridge_candidate_acceptance_digest_invalid",
            ),
            (self.idempotency_sha256, "passive_bridge_candidate_idempotency_invalid"),
        ):
            _require_sha256(digest, code)
        if self.reason_code != "passive_bridge_accepted_work_pending_proposal":
            raise PassiveCollaborationBridgeError("passive_bridge_candidate_reason_invalid")
        probe = CollaborationEvent(
            event_id=self.candidate_id,
            project=self.project,
            coordination_session_id=self.coordination_session_id,
            actor=AgentIdentity("agent:promotion-candidate-validator", "coordinator"),
            event_type="work.accepted",
            summary=self.summary,
            created_at="2000-01-01T00:00:00Z",
            work_item_id=self.work_item_id,
            subject_refs=(self.source_event_id,),
            evidence_refs=self.evidence_refs,
        )
        object.__setattr__(self, "candidate_id", probe.event_id)
        object.__setattr__(self, "coordination_session_id", probe.coordination_session_id)
        object.__setattr__(self, "work_item_id", probe.work_item_id)
        object.__setattr__(self, "source_event_id", probe.subject_refs[0])
        object.__setattr__(self, "summary", probe.summary)
        object.__setattr__(self, "evidence_refs", probe.evidence_refs)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROMOTION_CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "command": "create_pending_memory_proposal",
            "target_state": "pending",
            "canonical_memory_effect": "none",
            "direct_memory_write_allowed": False,
            "requires_server_receipt_verification": True,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "work_item_id": self.work_item_id,
            "source_event_id": self.source_event_id,
            "source_event_sha256": self.source_event_sha256,
            "work_receipt_sha256": self.work_receipt_sha256,
            "result_receipt_sha256": self.result_receipt_sha256,
            "acceptance_receipt_sha256": self.acceptance_receipt_sha256,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "idempotency_sha256": self.idempotency_sha256,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class PassiveCollaborationPlan:
    """Pure output ready for later server-owned persistence adapters."""

    event: CollaborationEvent
    promotion_candidate: PromotionCandidate | None
    reason_code: str
    idempotency_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PASSIVE_COLLABORATION_BRIDGE_SCHEMA,
            "event": self.event.to_dict(),
            "promotion_candidate": (
                None if self.promotion_candidate is None else self.promotion_candidate.to_dict()
            ),
            "reason_code": self.reason_code,
            "idempotency_sha256": self.idempotency_sha256,
            "canonical_memory_effect": "none",
        }


def compile_passive_collaboration(
    value: PassiveCollaborationInput,
    *,
    acceptance_authority: AcceptanceReceiptAuthority | None = None,
) -> PassiveCollaborationPlan:
    """Compile one bounded Hook/closure fact without performing side effects."""

    if not isinstance(value, PassiveCollaborationInput):
        raise PassiveCollaborationBridgeError("passive_bridge_input_invalid")
    _require_scope_binding(value)

    result = value.result
    acceptance = value.acceptance
    canonical_acceptance: AcceptanceReceipt | None = None
    if result is not None:
        _require_work_result_binding(value.work, result)
        _require_not_before(
            result.submitted_at,
            value.work.issued_at,
            "passive_bridge_result_before_work",
        )
        if _parse_time(result.submitted_at) > _parse_time(value.work.expires_at):
            raise PassiveCollaborationBridgeError("passive_bridge_result_after_work_expiry")

    if value.kind in {"progress", "submitted"}:
        if value.actor != value.work.assigned_agent:
            raise PassiveCollaborationBridgeError("passive_bridge_actor_work_mismatch")
    else:
        if result is None or acceptance is None:  # guarded by input; keeps narrowing explicit
            raise PassiveCollaborationBridgeError("passive_bridge_acceptance_required")
        if not isinstance(acceptance_authority, AcceptanceReceiptAuthority):
            raise PassiveCollaborationBridgeError("passive_bridge_acceptance_authority_required")
        try:
            canonical_acceptance = acceptance_authority.verify_consumption_proof(acceptance)
        except AcceptanceReceiptError as exc:
            raise PassiveCollaborationBridgeError(
                "passive_bridge_acceptance_authority_rejected"
            ) from exc
        _require_acceptance_binding(value.work, result, canonical_acceptance)
        if canonical_acceptance.decision != "accepted":
            raise PassiveCollaborationBridgeError("passive_bridge_acceptance_decision_invalid")
        if result.outcome != "completed":
            raise PassiveCollaborationBridgeError("passive_bridge_accepted_result_not_completed")
        if canonical_acceptance.accepted_by == result.submitted_by:
            raise PassiveCollaborationBridgeError("passive_bridge_independent_acceptance_required")
        if value.actor != canonical_acceptance.accepted_by:
            raise PassiveCollaborationBridgeError("passive_bridge_actor_acceptance_mismatch")
        _require_not_before(
            canonical_acceptance.accepted_at,
            result.submitted_at,
            "passive_bridge_acceptance_before_result",
        )

    source_envelope = _source_envelope(
        value,
        canonical_acceptance=canonical_acceptance,
    )
    idempotency_sha256 = _digest(source_envelope)
    digest_suffix = idempotency_sha256.removeprefix("sha256:")
    event = CollaborationEvent(
        event_id=f"event:passive:{digest_suffix[:40]}",
        project=value.project,
        coordination_session_id=value.coordination_session_id,
        actor=value.actor,
        event_type=_EVENT_TYPE_BY_KIND[value.kind],
        summary=value.summary or "",  # normalized to non-empty by input
        created_at=_event_occurred_at(
            value,
            canonical_acceptance=canonical_acceptance,
        ),
        expires_at=value.expires_at,
        causal_parent_event_id=value.causal_parent_event_id,
        work_item_id=value.work.work_item_id,
        subject_refs=_subject_refs(result),
        evidence_refs=_event_evidence_refs(
            value,
            canonical_acceptance=canonical_acceptance,
        ),
        audience_roles=value.audience_roles,
        audience_agent_ids=value.audience_agent_ids,
        payload={
            "bridge_schema": PASSIVE_COLLABORATION_BRIDGE_SCHEMA,
            "bridge_kind": value.kind,
            "source": value.source,
            "canonical_memory_effect": "none",
            "work_receipt_sha256": value.work.content_sha256,
            "result_receipt_sha256": None if result is None else result.content_sha256,
            "acceptance_receipt_sha256": (
                None if canonical_acceptance is None else canonical_acceptance.content_sha256
            ),
            "idempotency_sha256": idempotency_sha256,
        },
    )

    candidate: PromotionCandidate | None = None
    reason_code = {
        "progress": "passive_bridge_progress_event_only",
        "submitted": "passive_bridge_submitted_event_only",
        "accepted": "passive_bridge_accepted_work_pending_proposal",
    }[value.kind]
    if value.kind == "accepted":
        assert result is not None and canonical_acceptance is not None
        candidate = PromotionCandidate(
            candidate_id=f"promotion-candidate:{digest_suffix[:40]}",
            project=value.project,
            coordination_session_id=value.coordination_session_id,
            work_item_id=value.work.work_item_id,
            source_event_id=event.event_id,
            source_event_sha256=event.content_sha256,
            work_receipt_sha256=value.work.content_sha256,
            result_receipt_sha256=result.content_sha256,
            acceptance_receipt_sha256=canonical_acceptance.content_sha256,
            summary=result.summary,
            evidence_refs=_candidate_evidence_refs(result, canonical_acceptance),
            idempotency_sha256=idempotency_sha256,
        )

    return PassiveCollaborationPlan(
        event=event,
        promotion_candidate=candidate,
        reason_code=reason_code,
        idempotency_sha256=idempotency_sha256,
    )


def _require_scope_binding(value: PassiveCollaborationInput) -> None:
    if value.project != value.work.project:
        raise PassiveCollaborationBridgeError("passive_bridge_project_mismatch")
    if value.coordination_session_id != value.work.coordination_session_id:
        raise PassiveCollaborationBridgeError("passive_bridge_session_mismatch")


def _require_work_result_binding(work: WorkReceipt, result: ResultReceipt) -> None:
    if not isinstance(work, WorkReceipt):
        raise PassiveCollaborationBridgeError("passive_bridge_work_receipt_invalid")
    if not isinstance(result, ResultReceipt):
        raise PassiveCollaborationBridgeError("passive_bridge_result_receipt_invalid")
    if result.project != work.project:
        raise PassiveCollaborationBridgeError("passive_bridge_result_project_mismatch")
    if result.coordination_session_id != work.coordination_session_id:
        raise PassiveCollaborationBridgeError("passive_bridge_result_session_mismatch")
    if result.work_item_id != work.work_item_id:
        raise PassiveCollaborationBridgeError("passive_bridge_result_work_mismatch")
    if result.work_receipt_sha256 != work.content_sha256:
        raise PassiveCollaborationBridgeError("passive_bridge_result_work_digest_mismatch")
    if result.submitted_by != work.assigned_agent:
        raise PassiveCollaborationBridgeError("passive_bridge_result_submitter_mismatch")


def _require_acceptance_binding(
    work: WorkReceipt,
    result: ResultReceipt,
    acceptance: AcceptanceReceipt,
) -> None:
    if acceptance.project != work.project:
        raise PassiveCollaborationBridgeError("passive_bridge_acceptance_project_mismatch")
    if acceptance.coordination_session_id != work.coordination_session_id:
        raise PassiveCollaborationBridgeError("passive_bridge_acceptance_session_mismatch")
    if acceptance.work_item_id != work.work_item_id:
        raise PassiveCollaborationBridgeError("passive_bridge_acceptance_work_mismatch")
    if acceptance.work_receipt_sha256 != work.content_sha256:
        raise PassiveCollaborationBridgeError("passive_bridge_acceptance_work_digest_mismatch")
    if acceptance.result_receipt_sha256 != result.content_sha256:
        raise PassiveCollaborationBridgeError("passive_bridge_acceptance_result_digest_mismatch")


def _source_envelope(
    value: PassiveCollaborationInput,
    *,
    canonical_acceptance: AcceptanceReceipt | None,
) -> dict[str, object]:
    return {
        "schema_version": PASSIVE_COLLABORATION_BRIDGE_SCHEMA,
        "kind": value.kind,
        "source": value.source,
        "project_id": value.project.project_id,
        "coordination_session_id": value.coordination_session_id,
        "actor": value.actor.to_dict(),
        "work_item_id": value.work.work_item_id,
        "work_receipt_sha256": value.work.content_sha256,
        "result_receipt_sha256": None if value.result is None else value.result.content_sha256,
        "acceptance_receipt_sha256": (
            None if canonical_acceptance is None else canonical_acceptance.content_sha256
        ),
        "occurred_at": _event_occurred_at(
            value,
            canonical_acceptance=canonical_acceptance,
        ),
        "expires_at": value.expires_at,
        "causal_parent_event_id": value.causal_parent_event_id,
        "summary": value.summary,
        "evidence_refs": list(value.evidence_refs),
        "audience_roles": list(value.audience_roles),
        "audience_agent_ids": list(value.audience_agent_ids),
    }


def _subject_refs(result: ResultReceipt | None) -> tuple[str, ...]:
    if result is None or not result.artifact_refs:
        return ()
    return tuple(str(reference) for reference in result.artifact_refs)


def _event_evidence_refs(
    value: PassiveCollaborationInput,
    *,
    canonical_acceptance: AcceptanceReceipt | None,
) -> tuple[str, ...]:
    if value.kind == "accepted" and canonical_acceptance is not None:
        return canonical_acceptance.evidence_refs
    if value.kind == "submitted" and value.result is not None:
        return tuple(str(reference) for reference in value.result.evidence_refs)
    return value.evidence_refs


def _candidate_evidence_refs(
    result: ResultReceipt,
    acceptance: AcceptanceReceipt,
) -> tuple[str, ...]:
    combined = tuple(
        dict.fromkeys((*acceptance.evidence_refs, *result.evidence_refs, *result.artifact_refs))
    )
    if len(combined) > 32:
        raise PassiveCollaborationBridgeError("passive_bridge_candidate_evidence_too_many")
    return combined


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PassiveCollaborationBridgeError(code)
    return value


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_not_before(value: str, lower_bound: str, code: str) -> None:
    if _parse_time(value) < _parse_time(lower_bound):
        raise PassiveCollaborationBridgeError(code)


def _require_not_after(value: str, upper_bound: str, code: str) -> None:
    if _parse_time(value) > _parse_time(upper_bound):
        raise PassiveCollaborationBridgeError(code)


def _normalize_server_utc_time(value: str) -> str:
    parsed = _parse_time(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PassiveCollaborationBridgeError("passive_collaboration_server_time_invalid")
    return _utc_text(parsed)


def _event_occurred_at(
    value: PassiveCollaborationInput,
    *,
    canonical_acceptance: AcceptanceReceipt | None,
) -> str:
    if canonical_acceptance is not None:
        return canonical_acceptance.accepted_at
    if value.result is not None:
        return value.result.submitted_at
    return value.server_observed_at_utc


def _require_hook_scope_id(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _HOOK_SCOPE_RE.fullmatch(value):
        raise PassiveCollaborationBridgeError(code)
    return value


def _require_source_transition(
    previous: PassiveCollaborationSource,
    current: PassiveCollaborationSource,
) -> None:
    if previous.binding != current.binding:
        raise PassiveCollaborationBridgeError("passive_collaboration_source_binding_mismatch")
    if previous.work.content_sha256 != current.work.content_sha256:
        raise PassiveCollaborationBridgeError("passive_collaboration_work_source_conflict")
    rank = {"progress": 0, "submitted": 1, "accepted": 2}
    if rank[current.kind] < rank[previous.kind]:
        raise PassiveCollaborationBridgeError("passive_collaboration_source_regression")
    if (
        previous.audience_roles != current.audience_roles
        or previous.audience_agent_ids != current.audience_agent_ids
    ):
        raise PassiveCollaborationBridgeError("passive_collaboration_source_visibility_widening")
    if previous.causal_parent_event_id != current.causal_parent_event_id:
        raise PassiveCollaborationBridgeError("passive_collaboration_source_parent_conflict")
    if previous.evidence_refs != current.evidence_refs:
        raise PassiveCollaborationBridgeError("passive_collaboration_source_evidence_conflict")
    if (
        previous.result is not None
        and current.result is not None
        and previous.result.content_sha256 != current.result.content_sha256
    ):
        raise PassiveCollaborationBridgeError("passive_collaboration_result_source_conflict")
    if (
        previous.acceptance is not None
        and current.acceptance is not None
        and previous.acceptance.content_sha256 != current.acceptance.content_sha256
    ):
        raise PassiveCollaborationBridgeError("passive_collaboration_acceptance_source_conflict")
    if (
        previous.kind == current.kind
        and current.kind in {"submitted", "accepted"}
        and previous.content_sha256 != current.content_sha256
    ):
        raise PassiveCollaborationBridgeError("passive_collaboration_terminal_source_conflict")


def _skipped_publication(reason: str) -> PassiveCollaborationPublication:
    return PassiveCollaborationPublication(status="skipped", reason=reason)


def _recorded_publication(
    status: str,
    plan: PassiveCollaborationPlan,
    cursor: EventCursor,
) -> PassiveCollaborationPublication:
    return PassiveCollaborationPublication(
        status=status,
        reason=(
            "passive_collaboration_event_recorded"
            if status == "recorded"
            else "passive_collaboration_event_duplicate"
        ),
        event_id=plan.event.event_id,
        event_type=plan.event.event_type,
        event_sequence=cursor.sequence,
        promotion_status=(
            "source-only-deferred" if plan.promotion_candidate is not None else "not-applicable"
        ),
    )


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PassiveCollaborationBridgeError("passive_collaboration_clock_invalid")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ACCEPTANCE_RECEIPT_SCHEMA",
    "PASSIVE_COLLABORATION_BRIDGE_SCHEMA",
    "PASSIVE_COLLABORATION_PUBLICATION_SCHEMA",
    "PROMOTION_CANDIDATE_SCHEMA",
    "AcceptanceReceipt",
    "VerifiedAcceptanceReceipt",
    "PassiveCollaborationBridgeError",
    "PassiveCollaborationInput",
    "PassiveCollaborationPlan",
    "PassiveCollaborationPublication",
    "PassiveCollaborationRuntime",
    "PassiveCollaborationSource",
    "PromotionCandidate",
    "compile_passive_collaboration",
    "open_server_passive_collaboration_runtime",
]
