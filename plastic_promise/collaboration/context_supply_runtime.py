"""Server-bound PR4 collaboration retrieval for ``context_supply``.

This module owns the process-local, authenticated read path between one MCP
Agent session and the non-authoritative collaboration projection.  Callers can
request a cursor and page size, but cannot supply identities, roles, event
pages, policy claims, source lineage, working sets, acceptance receipts, or
authorities.  Durable registries, restart recovery, and Maintenance lifecycle
remain PR5 work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TypeVar, cast

from .acceptance_receipt import AcceptanceReceipt
from .awareness import AgentAwarenessProjection, ProjectWorkingSet
from .context_projection import (
    COLLABORATION_PROJECTION_FACTORY_REVISION,
    COLLABORATION_SOURCE_AUTHORITY,
    COLLABORATION_SOURCE_KIND,
    CollaborationContextBudget,
    CollaborationContextProjectionError,
    CollaborationPolicyClaim,
    CollaborationSourceTuple,
    SourcePageLineageClaim,
    compose_context_projection,
    open_server_collaboration_context_authority,
)
from .contracts import (
    COLLABORATION_EVENT_SCHEMA,
    AgentSession,
    CollaborationContractError,
    EventCursor,
    ProjectScope,
)
from .event_log import CollaborationEventLog, CollaborationEventReadReceipt

COLLABORATION_CONTEXT_RUNTIME_SCHEMA = "collaboration-context-runtime/v1"
COLLABORATION_CONTEXT_READ_SCHEMA = "collaboration-context-read/v1"
COLLABORATION_CONTEXT_POLICY_REVISION = "policy:collaboration-context:v1"

_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_REASON_RE = re.compile(r"\A[a-z][a-z0-9_]{0,95}\Z")
_RUNTIME_TOKEN = object()
_MAX_PAGE_SIZE = 20
_MAX_ACCEPTANCE_RECEIPTS = 32
_MAX_ISSUED_PROOFS = 1024
_POLICY_TTL_SECONDS = 300
_SESSION_FRESHNESS_SECONDS = 300

_K = TypeVar("_K")
_V = TypeVar("_V")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class CollaborationContextReadRequest:
    """Non-authoritative cursor hint accepted from the MCP consumer."""

    project: ProjectScope
    request_scope_id: str
    response_mode: str
    after_sequence: int | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise CollaborationContextProjectionError("collaboration_context_project_invalid")
        if not isinstance(self.request_scope_id, str) or not _SAFE_IDENTIFIER_RE.fullmatch(
            self.request_scope_id
        ):
            raise CollaborationContextProjectionError(
                "collaboration_context_request_scope_invalid"
            )
        mode = str(self.response_mode or "").strip().casefold()
        if mode not in {"standard", "compact", "debug"}:
            raise CollaborationContextProjectionError("collaboration_context_mode_invalid")
        if self.after_sequence is not None and (
            isinstance(self.after_sequence, bool)
            or not isinstance(self.after_sequence, int)
            or self.after_sequence < 0
        ):
            raise CollaborationContextProjectionError("collaboration_cursor_invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 20:
            raise CollaborationContextProjectionError("collaboration_context_limit_invalid")
        object.__setattr__(self, "response_mode", mode)


@dataclass(frozen=True, slots=True)
class CollaborationContextReadResult:
    """Bounded collaboration-only result; canonical memory is never included."""

    state: str
    reason: str
    projection: Mapping[str, object] | None = None
    prompt_section: str = ""
    retryable: bool = False
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.state not in {"available", "empty", "skipped", "degraded", "rejected"}:
            raise CollaborationContextProjectionError("collaboration_context_state_invalid")
        if not isinstance(self.reason, str) or not _SAFE_REASON_RE.fullmatch(self.reason):
            raise CollaborationContextProjectionError("collaboration_context_reason_invalid")
        if self.projection is not None and not isinstance(self.projection, Mapping):
            raise CollaborationContextProjectionError("collaboration_context_projection_invalid")
        if not isinstance(self.prompt_section, str):
            raise CollaborationContextProjectionError("collaboration_context_prompt_invalid")
        if not isinstance(self.retryable, bool) or not isinstance(self.replayed, bool):
            raise CollaborationContextProjectionError("collaboration_context_state_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_CONTEXT_READ_SCHEMA,
            "state": self.state,
            "reason": self.reason,
            "retryable": self.retryable,
            "replayed": self.replayed,
            "canonical_memory_effect": "none",
            "projection": _json_clone(self.projection) if self.projection is not None else None,
        }


@dataclass(frozen=True, slots=True)
class _ReplayRecord:
    request_after_sequence: int
    next_sequence: int
    request_scope_id: str
    response_mode: str
    limit: int
    memory_context_sha256: str
    result: CollaborationContextReadResult


class ServerCollaborationContextRuntime:
    """One process-local server authority bound to one exact Agent session."""

    __slots__ = (
        "_acceptance_receipts_provider",
        "_acceptance_verifier",
        "_authority",
        "_bound_session",
        "_clock",
        "_cursor",
        "_event_log",
        "_issued_lineages",
        "_issued_policy_claims",
        "_issued_projections",
        "_last_replay",
        "_lock",
        "_policy_digest",
        "_working_set_provider",
    )

    def __init__(
        self,
        *,
        bound_session: AgentSession,
        event_log: CollaborationEventLog,
        working_set_provider: Callable[[], ProjectWorkingSet],
        acceptance_receipts_provider: Callable[[], Sequence[AcceptanceReceipt]],
        acceptance_verifier: Callable[[AcceptanceReceipt], bool],
        clock: Callable[[], datetime],
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _RUNTIME_TOKEN:
            raise CollaborationContextProjectionError("collaboration_context_server_required")
        if not isinstance(bound_session, AgentSession) or bound_session.state != "active":
            raise CollaborationContextProjectionError("collaboration_context_session_invalid")
        if not isinstance(event_log, CollaborationEventLog):
            raise CollaborationContextProjectionError("collaboration_context_source_invalid")
        for dependency in (
            working_set_provider,
            acceptance_receipts_provider,
            acceptance_verifier,
            clock,
        ):
            if not callable(dependency):
                raise CollaborationContextProjectionError(
                    "collaboration_context_dependency_invalid"
                )
        self._bound_session = bound_session
        self._event_log = event_log
        self._working_set_provider = working_set_provider
        self._acceptance_receipts_provider = acceptance_receipts_provider
        self._acceptance_verifier = acceptance_verifier
        self._clock = clock
        self._cursor = EventCursor.start(
            bound_session.project,
            bound_session.coordination_session_id,
        )
        self._lock = asyncio.Lock()
        self._last_replay: _ReplayRecord | None = None
        self._issued_policy_claims: dict[str, CollaborationPolicyClaim] = {}
        self._issued_lineages: dict[
            str,
            tuple[SourcePageLineageClaim, CollaborationEventReadReceipt],
        ] = {}
        self._issued_projections: dict[tuple[str, str], None] = {}
        self._policy_digest = _digest(
            {
                "schema_version": "collaboration-context-policy/v1",
                "policy_revision": COLLABORATION_CONTEXT_POLICY_REVISION,
                "role": bound_session.identity.role,
                "operations": [
                    "event-log.read",
                    "working-set.project",
                    "context-supply.compose",
                ],
                "canonical_memory_effect": "none",
            }
        )
        self._authority = open_server_collaboration_context_authority(
            clock=clock,
            policy_verifier=self._verify_policy_claim,
            source_lineage_verifier=self._verify_source_lineage,
            projection_verifier=self._verify_projection,
            acceptance_verifier=self._verify_acceptance_receipt,
        )

    @property
    def bound_agent_session(self) -> AgentSession:
        return self._bound_session

    async def compose(
        self,
        *,
        memory_context: Mapping[str, object],
        request: CollaborationContextReadRequest,
    ) -> CollaborationContextReadResult:
        """Read, authenticate, rank, and compose one collaboration projection."""

        if not isinstance(request, CollaborationContextReadRequest):
            return _rejected("collaboration_context_request_invalid")
        if request.project != self._bound_session.project:
            return _rejected("collaboration_context_project_mismatch")
        try:
            self._require_current_bound_session()
        except CollaborationContextProjectionError as exc:
            return _rejected(_stable_reason(exc.code, "collaboration_context_authority_rejected"))
        try:
            memory_digest = _digest(memory_context)
        except Exception:
            return _rejected("collaboration_context_memory_invalid")

        async with self._lock:
            return self._compose_locked(
                memory_context=memory_context,
                memory_digest=memory_digest,
                request=request,
            )

    def _compose_locked(
        self,
        *,
        memory_context: Mapping[str, object],
        memory_digest: str,
        request: CollaborationContextReadRequest,
    ) -> CollaborationContextReadResult:
        current_sequence = self._cursor.sequence
        requested_sequence = (
            current_sequence if request.after_sequence is None else request.after_sequence
        )
        if requested_sequence > current_sequence:
            return _rejected("collaboration_cursor_gap")
        if requested_sequence < current_sequence:
            replay = self._last_replay
            if (
                replay is not None
                and replay.request_after_sequence == requested_sequence
                and replay.next_sequence == current_sequence
                and replay.request_scope_id == request.request_scope_id
                and replay.response_mode == request.response_mode
                and replay.limit == request.limit
                and replay.memory_context_sha256 == memory_digest
            ):
                return replace(replay.result, replayed=True)
            return _rejected("collaboration_cursor_replay_ambiguous")

        read_after = EventCursor(
            self._bound_session.project,
            self._bound_session.coordination_session_id,
            requested_sequence,
        )
        try:
            event_page, read_receipt = self._event_log.read_with_receipt(
                project=self._bound_session.project,
                coordination_session_id=self._bound_session.coordination_session_id,
                audience=self._bound_session.identity,
                after=read_after,
                limit=request.limit,
            )
            working_set = self._server_stamp_working_set(
                self._working_set_provider(),
                server_observed_at_utc=read_receipt.generated_at_utc,
            )
            awareness = working_set.project_for(
                audience=self._bound_session,
                deltas=event_page,
            )
            policy_claim = self._issue_policy_claim()
            source_lineage = self._issue_source_lineage(
                awareness=awareness,
                read_receipt=read_receipt,
                policy_claim=policy_claim,
            )
            acceptance_receipts = self._acceptance_receipts()
            feed = self._authority.bind_sources(
                memory_context=memory_context,
                awareness=awareness,
                event_page=event_page,
                authenticated_project=self._bound_session.project,
                authenticated_session_id=self._bound_session.coordination_session_id,
                authenticated_audience_session=self._bound_session,
                read_after_cursor=read_after,
                policy_claim=policy_claim,
                source_lineage=source_lineage,
                acceptance_receipts=acceptance_receipts,
            )
            composed = compose_context_projection(
                memory_context=memory_context,
                collaboration_feed=feed,
                authority=self._authority,
                budget=CollaborationContextBudget(max_items=request.limit),
            )
            projection = composed.get("collaboration")
            if not isinstance(projection, Mapping):
                raise CollaborationContextProjectionError(
                    "collaboration_context_projection_invalid"
                )
            projected = _json_clone(projection)
            if not isinstance(projected, dict):
                raise CollaborationContextProjectionError(
                    "collaboration_context_projection_invalid"
                )
            items = projected.get("items")
            state = "available" if isinstance(items, list) and items else "empty"
            result = CollaborationContextReadResult(
                state=state,
                reason=(
                    "collaboration_context_available"
                    if state == "available"
                    else "collaboration_context_empty"
                ),
                projection=projected,
                prompt_section=render_collaboration_prompt(projected),
            )
        except (CollaborationContextProjectionError, CollaborationContractError) as exc:
            reason = getattr(exc, "code", "collaboration_context_authority_rejected")
            return _rejected(_stable_reason(reason, "collaboration_context_authority_rejected"))
        except Exception:
            return CollaborationContextReadResult(
                state="degraded",
                reason="collaboration_context_source_unavailable",
                retryable=True,
            )

        self._cursor = event_page.next_cursor
        self._last_replay = _ReplayRecord(
            request_after_sequence=requested_sequence,
            next_sequence=event_page.next_cursor.sequence,
            request_scope_id=request.request_scope_id,
            response_mode=request.response_mode,
            limit=request.limit,
            memory_context_sha256=memory_digest,
            result=result,
        )
        return result

    def _server_stamp_working_set(
        self,
        value: ProjectWorkingSet,
        *,
        server_observed_at_utc: str,
    ) -> ProjectWorkingSet:
        """Replace source-reported observation time with the server read time.

        Edge, client, and compute clocks may describe their own diagnostics, but
        they never choose the canonical observation instant used for ordering,
        freshness, expiry, or projection identity.  Reconstructing the frozen
        value also re-runs every future-time check against the server timestamp.
        """

        if not isinstance(value, ProjectWorkingSet):
            raise CollaborationContextProjectionError(
                "collaboration_context_working_set_invalid"
            )
        if (
            value.project != self._bound_session.project
            or value.coordination_session_id
            != self._bound_session.coordination_session_id
            or self._bound_session not in value.agent_sessions
        ):
            raise CollaborationContextProjectionError(
                "collaboration_context_working_set_scope_mismatch"
            )
        server_observed = _parse_time(server_observed_at_utc)
        if server_observed < _parse_time(value.coordination.created_at):
            raise CollaborationContextProjectionError(
                "collaboration_context_server_time_before_session"
            )
        if server_observed > self._now():
            raise CollaborationContextProjectionError(
                "collaboration_context_server_time_from_future"
            )
        try:
            return replace(value, observed_at=_utc_text(server_observed))
        except CollaborationContractError as exc:
            raise CollaborationContextProjectionError(
                "collaboration_context_source_time_invalid"
            ) from exc

    def _acceptance_receipts(self) -> tuple[AcceptanceReceipt, ...]:
        values = tuple(self._acceptance_receipts_provider())
        if len(values) > _MAX_ACCEPTANCE_RECEIPTS or any(
            not isinstance(value, AcceptanceReceipt) for value in values
        ):
            raise CollaborationContextProjectionError(
                "collaboration_context_acceptance_source_invalid"
            )
        if any(
            value.project != self._bound_session.project
            or value.coordination_session_id
            != self._bound_session.coordination_session_id
            for value in values
        ):
            raise CollaborationContextProjectionError(
                "collaboration_context_acceptance_scope_mismatch"
            )
        return tuple(sorted(values, key=lambda value: value.receipt_id))

    def _issue_policy_claim(self) -> CollaborationPolicyClaim:
        now = self._now()
        expires = now + timedelta(seconds=_POLICY_TTL_SECONDS)
        if self._bound_session.expires_at is not None:
            expires = min(expires, _parse_time(self._bound_session.expires_at))
        if expires <= now:
            raise CollaborationContextProjectionError(
                "collaboration_context_policy_expired"
            )
        binding_id = f"context-policy:{uuid.uuid4().hex}"
        binding_sha256 = _digest(
            {
                "binding_id": binding_id,
                "audience_session_sha256": self._bound_session.content_sha256,
                "policy_revision": COLLABORATION_CONTEXT_POLICY_REVISION,
                "policy_digest": self._policy_digest,
                "issued_at": _utc_text(now),
                "expires_at": _utc_text(expires),
            }
        )
        claim = CollaborationPolicyClaim(
            binding_id=binding_id,
            binding_sha256=binding_sha256,
            policy_revision=COLLABORATION_CONTEXT_POLICY_REVISION,
            policy_digest=self._policy_digest,
            audience_session_sha256=self._bound_session.content_sha256,
            expires_at=_utc_text(expires),
        )
        self._issued_policy_claims[claim.content_sha256] = claim
        _trim_mapping(self._issued_policy_claims)
        return claim

    def _issue_source_lineage(
        self,
        *,
        awareness: AgentAwarenessProjection,
        read_receipt: CollaborationEventReadReceipt,
        policy_claim: CollaborationPolicyClaim,
    ) -> SourcePageLineageClaim:
        source_tuple = CollaborationSourceTuple(
            source_kind=COLLABORATION_SOURCE_KIND,
            source_authority=COLLABORATION_SOURCE_AUTHORITY,
            project=self._bound_session.project,
            coordination_session_id=self._bound_session.coordination_session_id,
            audience_agent_id=self._bound_session.identity.agent_id,
            audience_role=self._bound_session.identity.role,
            audience_session_id=self._bound_session.session_id,
            audience_session_sha256=self._bound_session.content_sha256,
            agent_session_policy_revision=policy_claim.policy_revision,
            event_schema_revision=COLLABORATION_EVENT_SCHEMA,
            event_log_revision=read_receipt.event_log_revision,
            cursor_from=read_receipt.after_cursor,
            cursor_to=read_receipt.next_cursor,
            source_page_digest=read_receipt.event_page_sha256,
            projection_factory_revision=COLLABORATION_PROJECTION_FACTORY_REVISION,
            generated_at_utc=read_receipt.generated_at_utc,
        )
        lineage = SourcePageLineageClaim(
            source_receipt_id=read_receipt.receipt_id,
            source_receipt_sha256=read_receipt.content_sha256,
            source_anchor_sha256=read_receipt.source_anchor_sha256,
            source_tuple=source_tuple,
            source_head_cursor=read_receipt.source_head_cursor,
            event_page_sha256=read_receipt.event_page_sha256,
            awareness_sha256=awareness.content_sha256,
            working_set_sha256=awareness.working_set_sha256,
            visible_event_count=read_receipt.visible_event_count,
        )
        self._issued_lineages[lineage.content_sha256] = (lineage, read_receipt)
        self._issued_projections[(awareness.content_sha256, lineage.content_sha256)] = None
        _trim_mapping(self._issued_lineages)
        _trim_mapping(self._issued_projections)
        return lineage

    def _verify_policy_claim(
        self,
        session: AgentSession,
        claim: CollaborationPolicyClaim,
    ) -> bool:
        issued = self._issued_policy_claims.get(claim.content_sha256)
        return bool(
            session == self._bound_session
            and issued == claim
            and claim.policy_revision == COLLABORATION_CONTEXT_POLICY_REVISION
            and claim.policy_digest == self._policy_digest
            and claim.audience_session_sha256 == self._bound_session.content_sha256
        )

    def _verify_source_lineage(self, claim: SourcePageLineageClaim) -> bool:
        issued = self._issued_lineages.get(claim.content_sha256)
        if issued is None:
            return False
        canonical_claim, read_receipt = issued
        return bool(
            canonical_claim == claim
            and self._event_log.verify_read_receipt(read_receipt)
            and claim.source_receipt_sha256 == read_receipt.content_sha256
            and claim.source_anchor_sha256 == read_receipt.source_anchor_sha256
            and claim.source_head_cursor == read_receipt.source_head_cursor
            and claim.event_page_sha256 == read_receipt.event_page_sha256
        )

    def _verify_projection(
        self,
        awareness: AgentAwarenessProjection,
        claim: SourcePageLineageClaim,
    ) -> bool:
        return (
            awareness.content_sha256,
            claim.content_sha256,
        ) in self._issued_projections

    def _verify_acceptance_receipt(self, receipt: AcceptanceReceipt) -> bool:
        try:
            return self._acceptance_verifier(receipt) is True
        except Exception:
            return False

    def _require_current_bound_session(self) -> None:
        """Reject stale MCP bindings before reading the collaboration feed.

        The runtime is process-local in PR4, but its audience session is still
        server-issued and must not be used after its heartbeat freshness window
        or explicit expiry.  Checking before ``read_with_receipt`` keeps a
        stale session from advancing its cursor or producing a fresh snapshot.
        PR5 will replace this in-memory check with durable registry state.
        """

        now = self._now()
        session = self._bound_session
        if session.state != "active":
            raise CollaborationContextProjectionError(
                "collaboration_audience_session_inactive"
            )
        heartbeat = _parse_time(
            session.last_heartbeat_at,
        )
        if heartbeat > now:
            raise CollaborationContextProjectionError(
                "collaboration_audience_session_from_future"
            )
        if (now - heartbeat).total_seconds() > _SESSION_FRESHNESS_SECONDS:
            raise CollaborationContextProjectionError(
                "collaboration_audience_session_stale"
            )
        if session.expires_at is not None and _parse_time(session.expires_at) <= now:
            raise CollaborationContextProjectionError(
                "collaboration_audience_session_expired"
            )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CollaborationContextProjectionError("collaboration_context_clock_invalid")
        return value.astimezone(timezone.utc)


def open_server_collaboration_context_runtime(
    *,
    bound_session: AgentSession,
    event_log: CollaborationEventLog,
    working_set_provider: Callable[[], ProjectWorkingSet],
    acceptance_receipts_provider: Callable[[], Sequence[AcceptanceReceipt]] | None = None,
    acceptance_verifier: Callable[[AcceptanceReceipt], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ServerCollaborationContextRuntime:
    """Open the PR4 process-local runtime inside canonical server wiring."""

    return ServerCollaborationContextRuntime(
        bound_session=bound_session,
        event_log=event_log,
        working_set_provider=working_set_provider,
        acceptance_receipts_provider=acceptance_receipts_provider or (lambda: ()),
        acceptance_verifier=acceptance_verifier or (lambda _receipt: False),
        clock=clock or (lambda: datetime.now(timezone.utc)),
        _server_token=_RUNTIME_TOKEN,
    )


def render_collaboration_prompt(projection: Mapping[str, object]) -> str:
    """Render a bounded safe prompt section from the redacted projection only."""

    cursor = projection.get("cursor")
    cursor = cursor if isinstance(cursor, Mapping) else {}
    after = cursor.get("after")
    after = after if isinstance(after, Mapping) else {}
    next_cursor = cursor.get("next")
    next_cursor = next_cursor if isinstance(next_cursor, Mapping) else {}
    lines = [
        "## [COLLABORATION]",
        "- authority: non-authoritative live collaboration",
        "- canonical_memory_effect: none",
        f"- cursor: {_safe_sequence(after.get('sequence'))} -> "
        f"{_safe_sequence(next_cursor.get('sequence'))}",
        f"- has_more: {str(bool(cursor.get('has_more'))).lower()}",
    ]
    items = projection.get("items")
    if not isinstance(items, list) or not items:
        lines.extend(("", "- No relevant collaboration deltas."))
        return "\n".join(lines)

    headings = {
        "conflict": "Conflict",
        "blocker": "Blocker",
        "accepted_result": "Accepted result",
        "progress": "Progress",
    }
    current_kind = ""
    for raw in items[:_MAX_PAGE_SIZE]:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "progress")
        if kind != current_kind:
            lines.extend(("", f"### {headings.get(kind, 'Update')}"))
            current_kind = kind
        actor = raw.get("actor") or raw.get("accepted_by")
        actor = actor if isinstance(actor, Mapping) else {}
        actor_text = "/".join(
            value
            for value in (
                str(actor.get("role") or "").strip(),
                str(actor.get("agent_id") or "").strip(),
            )
            if value
        )
        summary = str(raw.get("summary") or "").replace("\n", " ").strip()
        if not summary and kind == "accepted_result":
            artifacts = raw.get("artifact_refs")
            if isinstance(artifacts, list):
                summary = "accepted artifacts: " + ", ".join(str(item) for item in artifacts[:4])
        summary = summary[:500] or "bounded collaboration update"
        relevance = raw.get("relevance")
        prefix = f"[{actor_text}] " if actor_text else ""
        suffix = f" (relevance={relevance})" if isinstance(relevance, (int, float)) else ""
        lines.append(f"- {prefix}{summary}{suffix}")
        reasons = raw.get("relevance_reasons")
        if isinstance(reasons, list) and reasons:
            lines.append("  - signals: " + ", ".join(str(item) for item in reasons[:6]))
    return "\n".join(lines)


def _rejected(reason: str) -> CollaborationContextReadResult:
    return CollaborationContextReadResult(
        state="rejected",
        reason=_stable_reason(reason, "collaboration_context_authority_rejected"),
    )


def _stable_reason(value: object, fallback: str) -> str:
    candidate = str(value or "").strip().casefold().replace("-", "_")
    return candidate if _SAFE_REASON_RE.fullmatch(candidate) else fallback


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str) or value != value.strip():
        raise CollaborationContextProjectionError("collaboration_context_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollaborationContextProjectionError("collaboration_context_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollaborationContextProjectionError("collaboration_context_time_invalid")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CollaborationContextProjectionError("collaboration_context_time_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_clone(value: _T) -> _T:
    return cast(
        "_T",
        json.loads(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )


def _trim_mapping(value: MutableMapping[_K, _V]) -> None:
    while len(value) > _MAX_ISSUED_PROOFS:
        value.pop(next(iter(value)))


def _safe_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


__all__ = [
    "COLLABORATION_CONTEXT_POLICY_REVISION",
    "COLLABORATION_CONTEXT_READ_SCHEMA",
    "COLLABORATION_CONTEXT_RUNTIME_SCHEMA",
    "CollaborationContextReadRequest",
    "CollaborationContextReadResult",
    "ServerCollaborationContextRuntime",
    "open_server_collaboration_context_runtime",
    "render_collaboration_prompt",
]
