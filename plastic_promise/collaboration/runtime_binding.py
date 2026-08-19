"""Server-owned composition for exact MCP durable collaboration sessions.

This module is the PR5 *seam* between a server-authenticated MCP transport
instance and the durable collaboration runtime.  Callers only supply a
canonical project, a server-configured actor, a server-resolved workflow scope,
and an opaque transport-instance id created by :mod:`plastic_promise.mcp.server`.
The public MCP JSON schema never receives any of those authority-bearing
values.

The module deliberately keeps a small interface while hiding three pieces of
otherwise repeated complexity:

* exact durable ``AgentSession`` identity derives from the authenticated
  transport instance as well as actor/project/workflow scope;
* the canonical SQLite connection and all policy/review authorities remain
  server-owned and are shared only at their appropriate scopes;
* schema absence and unsupported authority composition fail closed without
  installing a migration or weakening the memory plane;
* a process-local HMAC continuation authority can attach fresh MCP transports
  to the same exact binding without reconstructing authority from caller JSON
  or durable rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plastic_promise.core.project_identity import canonical_project_id

from .acceptance_receipt import (
    AcceptanceReceiptAuthority,
    open_server_acceptance_receipt_authority,
)
from .activity_update import ActivityAuditAuthority, open_server_activity_audit_authority
from .canonical_time import parse_utc, server_now_text
from .contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    EventCursor,
    ProjectScope,
    WorkReceipt,
)
from .coordination_plan import (
    CoordinationPlanAuthority,
    TopLevelAgentAuthorizationVerifier,
    TopLevelAgentBindingAuthority,
    open_server_coordination_plan_authority,
)
from .coordinator_supervisor import (
    CoordinatorAuditAuthority,
    open_server_coordinator_audit_authority,
)
from .durable_acceptance_store import DurableAcceptanceAuthorityRepository
from .durable_activity_store import DurableActivityRepository
from .durable_coordination_plan_store import DurableCoordinationPlanRepository
from .durable_coordinator_store import DurableCoordinatorRepository
from .durable_role_store import DurableRoleAssignmentRepository
from .lease_contract import AGENT_OWNER_KIND, AGENT_WORK_POLICY, LeaseHeartbeat, WorkItem, WorkLease
from .policy_binding import (
    ACCEPTANCE_REVIEW_POLICY_REVISION,
    AgentPolicyBindingAuthority,
    open_server_agent_policy_binding_authority,
)
from .role_assignment import RoleAssignmentAuthority, open_server_role_assignment_authority

_SERVER_ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_TRANSPORT_SESSION_RE = re.compile(r"^transport:mcp:[0-9a-f]{32}$")
_SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SESSION_POLICY_PROFILE = "participant"
_SESSION_TTL_SECONDS = 300
_SESSION_INIT_EVENT_LIMIT = 20
_CONTINUATION_SCHEMA = "durable-collaboration-continuation/v1"
_CONTINUATION_KEYRING_SCHEMA = "durable-collaboration-continuation-keyring/v1"
_CONTINUATION_TTL_SECONDS = 15 * 60

_AUTHORITY_ATTR = "_server_durable_collaboration_authority_bundle"
_SCOPE_AUTHORITY_ATTR = "_server_durable_collaboration_scope_authorities"


@dataclass(frozen=True, slots=True)
class DurableCollaborationRuntimeBindingResult:
    """One exact transport-to-runtime binding result.

    The result intentionally contains no transport-instance identifier.  The
    MCP server owns that private mapping and only retains this result against
    the SDK session object that authenticated it.
    """

    runtime: Any | None
    session: AgentSession | None = None
    host: DurableCollaborationHost | None = None
    session_init_result: Any | None = None
    reason: str = ""
    registered: bool = False
    role_assignment_repository: DurableRoleAssignmentRepository | None = None
    role_assignment_authority: RoleAssignmentAuthority | None = None
    activity_repository: DurableActivityRepository | None = None
    activity_authority: ActivityAuditAuthority | None = None
    acceptance_repository: DurableAcceptanceAuthorityRepository | None = None
    acceptance_authority: AcceptanceReceiptAuthority | None = None
    coordinator_repository: DurableCoordinatorRepository | None = None
    coordinator_authority: CoordinatorAuditAuthority | None = None
    coordination_plan_repository: DurableCoordinationPlanRepository | None = None
    coordination_plan_authority: CoordinationPlanAuthority | None = None
    top_level_binding_authority: TopLevelAgentBindingAuthority | None = None

    @property
    def durable(self) -> bool:
        return (
            self.runtime is not None
            and self.session is not None
            and self.host is not None
            and self.registered
        )


@dataclass(frozen=True, slots=True)
class DurableCollaborationContinuationClaims:
    """Authenticated, bounded identity carried by one continuation assertion."""

    project_id: str
    flow_scope_id: str
    server_actor: str
    hook_session_id: str
    durable_session_id: str
    agent_id: str
    role: str
    stage_session_id: str
    flow_line_id: str
    issued_at_epoch: int
    expires_at_epoch: int
    token_id: str


@dataclass(frozen=True, slots=True)
class DurableCollaborationContinuationResult:
    """Secret-aware result whose public diagnostics never contain the bearer token."""

    binding: DurableCollaborationRuntimeBindingResult | None = None
    claims: DurableCollaborationContinuationClaims | None = None
    token: str = ""
    token_digest: str = ""
    expires_at_epoch: int = 0
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.claims is not None and not self.reason


@dataclass(slots=True)
class _ContinuationEntry:
    binding: DurableCollaborationRuntimeBindingResult
    claims: DurableCollaborationContinuationClaims
    revoked: bool = False


class DurableCollaborationContinuationAuthority:
    """Issue and verify opaque durable-session continuations.

    Signing keys may be injected directly by tests or loaded from a private
    server-owned key-ring file.  Verification is stateless: the process-local
    entry map is only a binding cache and revocation accelerator, never the
    source of authority.  A restarted server therefore verifies the signed
    claims first and lets the MCP composition seam rehydrate the exact active
    session from canonical SQLite.
    """

    def __init__(
        self,
        *,
        key: bytes | None = None,
        key_ring: Mapping[str, bytes] | None = None,
        active_key_id: str | None = None,
        ttl_seconds: int = _CONTINUATION_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if key is not None and key_ring is not None:
            raise ValueError("durable_collaboration_continuation_keyring_conflict")
        if key_ring is None:
            secret = key if key is not None else secrets.token_bytes(32)
            if not isinstance(secret, bytes) or len(secret) < 32:
                raise ValueError("durable_collaboration_continuation_key_invalid")
            key_id = hashlib.sha256(secret).hexdigest()[:16]
            keys = {key_id: secret}
        else:
            keys = self._validated_key_ring(key_ring)
            key_id = str(active_key_id or "").strip()
            if key_id not in keys:
                raise ValueError("durable_collaboration_continuation_active_key_invalid")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("durable_collaboration_continuation_ttl_invalid")
        self._keys = keys
        self._key_id = key_id
        self._ttl_seconds = ttl_seconds
        self._clock = clock or _server_clock
        self._entries: dict[str, _ContinuationEntry] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_key_file(
        cls,
        path: str | os.PathLike[str],
        *,
        create: bool = True,
        ttl_seconds: int = _CONTINUATION_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> DurableCollaborationContinuationAuthority:
        """Load a private key ring, atomically creating its first key if absent."""

        key_path = Path(path).expanduser()
        try:
            payload = _read_private_continuation_keyring(key_path)
        except FileNotFoundError:
            if not create:
                raise ValueError("durable_collaboration_continuation_keyring_unavailable") from None
            payload = _create_private_continuation_keyring(key_path)
        active_key_id, keys = _decode_continuation_keyring(payload)
        return cls(
            key_ring=keys,
            active_key_id=active_key_id,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )

    @staticmethod
    def token_digest(token: object) -> str:
        value = str(token or "").strip()
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def issue(
        self,
        binding: DurableCollaborationRuntimeBindingResult,
        *,
        project_id: object,
        flow_scope_id: object,
        server_actor: object,
        hook_session_id: object,
        stage_session_id: object = "",
        flow_line_id: object = "",
    ) -> DurableCollaborationContinuationResult:
        if not binding.durable or binding.session is None:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_binding_invalid"
            )
        session = binding.session
        project = canonical_project_id(project_id)
        scope = _continuation_claim(flow_scope_id)
        actor = _server_actor(server_actor)
        hook = _continuation_claim(hook_session_id)
        stage = _continuation_claim(stage_session_id, required=False)
        flow_line = _continuation_claim(flow_line_id, required=False)
        if not project:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_project_required"
            )
        if not scope:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_flow_required"
            )
        if not actor:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_actor_required"
            )
        if not hook:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_hook_required"
            )
        if (
            session.project.project_id != project
            or session.coordination_session_id != scope
            or session.identity.agent_id != f"agent:{actor}"
        ):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_binding_conflict"
            )
        issued_at = int(self._now().timestamp())
        try:
            session_expires_at = int(parse_utc(session.expires_at).timestamp())
        except (TypeError, ValueError):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_session_expired"
            )
        expires_at = min(issued_at + self._ttl_seconds, session_expires_at)
        if expires_at <= issued_at:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_session_expired"
            )
        claims = DurableCollaborationContinuationClaims(
            project_id=project,
            flow_scope_id=scope,
            server_actor=actor,
            hook_session_id=hook,
            durable_session_id=session.session_id,
            agent_id=session.identity.agent_id,
            role=session.identity.role,
            stage_session_id=stage,
            flow_line_id=flow_line,
            issued_at_epoch=issued_at,
            expires_at_epoch=expires_at,
            token_id=secrets.token_hex(16),
        )
        payload = self._claims_payload(claims)
        payload_text = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        encoded_payload = _base64url_encode(payload_text.encode("utf-8"))
        signature = hmac.new(
            self._keys[self._key_id], encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        token = f"{encoded_payload}.{_base64url_encode(signature)}"
        digest = self.token_digest(token)
        with self._lock:
            self._entries[digest] = _ContinuationEntry(binding=binding, claims=claims)
        return DurableCollaborationContinuationResult(
            binding=binding,
            claims=claims,
            token=token,
            token_digest=digest,
            expires_at_epoch=expires_at,
        )

    def resume(
        self,
        token: object,
        *,
        project_id: object,
        flow_scope_id: object,
        server_actor: object,
        hook_session_id: object,
        stage_session_id: object = "",
        flow_line_id: object = "",
    ) -> DurableCollaborationContinuationResult:
        value = str(token or "").strip()
        if not value:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_required"
            )
        parsed = self._parse_and_verify(value)
        if parsed.reason:
            return parsed
        claims = parsed.claims
        if claims is None:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_invalid"
            )
        expected_project = canonical_project_id(project_id)
        expected_scope = _continuation_claim(flow_scope_id)
        expected_actor = _server_actor(server_actor)
        expected_hook = _continuation_claim(hook_session_id)
        expected_stage = _continuation_claim(stage_session_id, required=False)
        expected_flow_line = _continuation_claim(flow_line_id, required=False)
        if not expected_project or claims.project_id != expected_project:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_project_conflict"
            )
        if expected_scope and claims.flow_scope_id != expected_scope:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_flow_conflict"
            )
        if not expected_scope and (
            not expected_stage
            or not expected_flow_line
            or claims.stage_session_id != expected_stage
            or claims.flow_line_id != expected_flow_line
        ):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_flow_conflict"
            )
        if not expected_actor or claims.server_actor != expected_actor:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_actor_conflict"
            )
        if not expected_hook or claims.hook_session_id != expected_hook:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_hook_conflict"
            )
        digest = self.token_digest(value)
        with self._lock:
            entry = self._entries.get(digest)
            if entry is not None and entry.revoked:
                return DurableCollaborationContinuationResult(
                    token_digest=digest,
                    reason="durable_collaboration_continuation_revoked",
                )
            binding = entry.binding if entry is not None else None
            registered_claims = entry.claims if entry is not None else None
        if binding is None:
            return DurableCollaborationContinuationResult(
                claims=claims,
                token_digest=digest,
                expires_at_epoch=claims.expires_at_epoch,
            )
        session = binding.session
        if (
            not binding.durable
            or session is None
            or registered_claims != claims
            or session.session_id != claims.durable_session_id
            or session.project.project_id != claims.project_id
            or session.coordination_session_id != claims.flow_scope_id
            or session.identity.agent_id != claims.agent_id
            or session.identity.role != claims.role
        ):
            return DurableCollaborationContinuationResult(
                token_digest=digest,
                reason="durable_collaboration_continuation_binding_invalid",
            )
        return DurableCollaborationContinuationResult(
            binding=binding,
            claims=claims,
            token_digest=digest,
            expires_at_epoch=claims.expires_at_epoch,
        )

    def revoke_binding(self, binding: DurableCollaborationRuntimeBindingResult) -> int:
        session = binding.session
        session_id = session.session_id if session is not None else ""
        revoked = 0
        with self._lock:
            for entry in self._entries.values():
                entry_session = entry.binding.session
                if (
                    entry.binding is binding
                    or (
                        session_id
                        and entry_session is not None
                        and entry_session.session_id == session_id
                    )
                ) and not entry.revoked:
                    entry.revoked = True
                    revoked += 1
        return revoked

    def is_revoked(self, token_digest: object) -> bool:
        digest = str(token_digest or "").strip()
        with self._lock:
            entry = self._entries.get(digest)
            return bool(entry is not None and entry.revoked)

    def clear(self) -> None:
        """Clear process-local assertions without rotating the signing key."""

        with self._lock:
            self._entries.clear()

    def _parse_and_verify(self, token: str) -> DurableCollaborationContinuationResult:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            if not encoded_payload or not encoded_signature or "." in encoded_signature:
                raise ValueError
            payload_bytes = _base64url_decode(encoded_payload)
            signature = _base64url_decode(encoded_signature)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_invalid"
            )
        key_id = str(payload.get("kid") or "")
        verification_key = self._keys.get(key_id)
        if verification_key is None:
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_key_unavailable"
            )
        expected = hmac.new(
            verification_key, encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_invalid"
            )
        try:
            claims = self._claims_from_payload(payload)
        except (KeyError, TypeError, ValueError):
            return DurableCollaborationContinuationResult(
                reason="durable_collaboration_continuation_invalid"
            )
        if claims.expires_at_epoch <= int(self._now().timestamp()):
            return DurableCollaborationContinuationResult(
                claims=claims,
                reason="durable_collaboration_continuation_expired",
            )
        return DurableCollaborationContinuationResult(claims=claims)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _claims_payload(self, claims: DurableCollaborationContinuationClaims) -> dict[str, object]:
        return {
            "v": _CONTINUATION_SCHEMA,
            "kid": self._key_id,
            "project": claims.project_id,
            "flow": claims.flow_scope_id,
            "actor": claims.server_actor,
            "hook": claims.hook_session_id,
            "session": claims.durable_session_id,
            "agent": claims.agent_id,
            "role": claims.role,
            "stage": claims.stage_session_id,
            "line": claims.flow_line_id,
            "iat": claims.issued_at_epoch,
            "exp": claims.expires_at_epoch,
            "jti": claims.token_id,
        }

    @staticmethod
    def _validated_key_ring(key_ring: Mapping[str, bytes]) -> dict[str, bytes]:
        keys: dict[str, bytes] = {}
        for raw_key_id, secret in key_ring.items():
            key_id = str(raw_key_id or "").strip()
            if not re.fullmatch(r"[0-9a-f]{16}", key_id):
                raise ValueError("durable_collaboration_continuation_key_id_invalid")
            if not isinstance(secret, bytes) or len(secret) < 32:
                raise ValueError("durable_collaboration_continuation_key_invalid")
            if hashlib.sha256(secret).hexdigest()[:16] != key_id:
                raise ValueError("durable_collaboration_continuation_key_id_mismatch")
            keys[key_id] = secret
        if not keys:
            raise ValueError("durable_collaboration_continuation_keyring_empty")
        return keys

    @staticmethod
    def _claims_from_payload(payload: Mapping[str, object]) -> DurableCollaborationContinuationClaims:
        if payload["v"] != _CONTINUATION_SCHEMA:
            raise ValueError
        issued_at = payload["iat"]
        expires_at = payload["exp"]
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= issued_at
        ):
            raise ValueError
        return DurableCollaborationContinuationClaims(
            project_id=_required_continuation_claim(payload["project"]),
            flow_scope_id=_required_continuation_claim(payload["flow"]),
            server_actor=_required_continuation_claim(payload["actor"]),
            hook_session_id=_required_continuation_claim(payload["hook"]),
            durable_session_id=_required_continuation_claim(payload["session"]),
            agent_id=_required_continuation_claim(payload["agent"]),
            role=_required_continuation_claim(payload["role"]),
            stage_session_id=_continuation_claim(payload.get("stage"), required=False),
            flow_line_id=_continuation_claim(payload.get("line"), required=False),
            issued_at_epoch=issued_at,
            expires_at_epoch=expires_at,
            token_id=_required_continuation_claim(payload["jti"]),
        )


@dataclass(frozen=True, slots=True)
class DurableCollaborationHost:
    """Deep server-owned interface for one authenticated collaboration session.

    The host keeps the exact durable session, policy receipt, repositories and
    cursor authority behind one small interface.  Public projections are
    allowlisted and never expose a transport id, AgentSession id, policy
    binding id/digest, raw SQLite row, lease JSON or caller-selectable scope.
    """

    runtime: Any
    session: AgentSession
    session_init_result: Any

    async def compose(self, *, memory_context: Mapping[str, object], request: Any) -> Any:
        """Compose one bounded collaboration feed for ``context_supply``.

        The host itself is the authenticated read authority: it is retained
        only against the exact SDK transport that created the durable session.
        The request may choose a cursor and page size, but never an audience,
        project, role, session, policy, or storage source.
        """

        from .context_supply_runtime import (
            CollaborationContextReadRequest,
            CollaborationContextReadResult,
            render_collaboration_prompt,
        )
        from .durable_runtime import DurableCollaborationError

        if not isinstance(request, CollaborationContextReadRequest):
            return CollaborationContextReadResult(
                state="rejected",
                reason="collaboration_context_request_invalid",
            )
        if request.project != self.session.project:
            return CollaborationContextReadResult(
                state="rejected",
                reason="collaboration_context_project_mismatch",
            )
        try:
            after = EventCursor(
                project=self.session.project,
                coordination_session_id=self.session.coordination_session_id,
                sequence=(
                    self.runtime.load_cursor(
                        project=self.session.project,
                        coordination_session_id=self.session.coordination_session_id,
                        consumer_id=self.session.session_id,
                    ).sequence
                    if request.after_sequence is None
                    else request.after_sequence
                ),
            )
            page = self.runtime.peer_delta_page(
                session=self.session,
                after=after,
                limit=request.limit,
            )
        except DurableCollaborationError as exc:
            reason = str(exc).strip()
            return CollaborationContextReadResult(
                state="rejected",
                reason=(
                    reason
                    if reason.startswith("collaboration_")
                    or reason.startswith("agent_session_")
                    else "collaboration_context_authority_rejected"
                ),
            )
        except Exception:
            return CollaborationContextReadResult(
                state="degraded",
                reason="collaboration_context_source_unavailable",
                retryable=True,
            )

        items = [
            self._context_event_projection(item, memory_context=memory_context)
            for item in page["items"]
        ]
        items.sort(
            key=lambda item: (
                -int(item["priority"]),
                -float(item["relevance"]),
                str(item["created_at"]),
                str(item["id"]),
            )
        )
        projection = {
            "schema_version": "durable-collaboration-awareness/v1",
            "authority": "authenticated-durable-collaboration",
            "canonical_memory_effect": "none",
            "project_id": self.session.project.project_id,
            "coordination_session_id": self.session.coordination_session_id,
            "audience": {
                "agent_id": self.session.identity.agent_id,
                "role": self.session.identity.role,
            },
            "cursor": {
                "after": after.to_dict(),
                "next": page["next_cursor"].to_dict(),
                "source_head_sequence": int(page["source_head_sequence"]),
                "has_more": bool(page["has_more"]),
                "ack_required": bool(page["ack_required"]),
                "advance": "explicit-heartbeat-ack",
            },
            "items": items,
            "count": len(items),
        }
        prompt = render_collaboration_prompt(projection)
        return CollaborationContextReadResult(
            state="available" if items else "empty",
            reason=(
                "collaboration_context_available"
                if items
                else "collaboration_context_empty"
            ),
            projection=projection,
            prompt_section=prompt,
        )

    @staticmethod
    def _context_event_projection(
        item: Mapping[str, object],
        *,
        memory_context: Mapping[str, object],
    ) -> dict[str, object]:
        event_type = str(item.get("event_type") or "")
        if event_type == "conflict.detected":
            kind, priority = "conflict", 4
        elif event_type == "blocker.raised":
            kind, priority = "blocker", 3
        elif event_type == "work.accepted":
            kind, priority = "accepted_result", 2
        else:
            kind, priority = "progress", 1
        summary = str(item.get("summary") or "")[:500]
        memory_terms: set[str] = set()
        for section in ("core", "related", "divergent"):
            values = memory_context.get(section)
            if not isinstance(values, list):
                continue
            for value in values[:4]:
                if not isinstance(value, Mapping):
                    continue
                for token in re.findall(r"[A-Za-z0-9_\-]{3,}|[\u4e00-\u9fff]{2,}", str(value)):
                    memory_terms.add(token.casefold())
        summary_terms = {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9_\-]{3,}|[\u4e00-\u9fff]{2,}", summary)
        }
        overlap = len(memory_terms.intersection(summary_terms))
        relevance = min(1.0, 0.35 + priority * 0.1 + overlap * 0.05)
        reasons = [f"event-risk-priority:{kind}"]
        if overlap:
            reasons.append("memory-context-overlap")
        return {
            "id": str(item.get("event_id") or ""),
            "kind": kind,
            "priority": priority,
            "event_type": event_type,
            "actor": dict(item.get("actor") or {}),
            "summary": summary,
            "created_at": str(item.get("created_at") or ""),
            "expires_at": item.get("expires_at"),
            "work_item_id": item.get("work_item_id"),
            "subject_refs": list(item.get("subject_refs") or ())[:32],
            "evidence_refs": list(item.get("evidence_refs") or ())[:32],
            "relevance": relevance,
            "relevance_reasons": reasons,
            "payload": "redacted",
            "authority_effect": "none",
        }

    def work_list(self, *, limit: object = 32) -> dict[str, object]:
        """Return this exact session's bounded ProjectWorkBoard inbox."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            from .durable_runtime import DurableCollaborationError

            raise DurableCollaborationError("work_projection_limit_invalid")
        items = self.runtime.assigned_work_projection(
            self.session.project,
            self.session.session_id,
            limit=limit,
        )
        return {
            "schema_version": "collaboration-work-list/v1",
            "state": "durable",
            "persistent": True,
            "project_id": self.session.project.project_id,
            "items": [dict(item) for item in items],
            "count": len(items),
            "authority_effect": "none",
            "canonical_memory_effect": "none",
        }

    def work_register(self, *, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        """Issue and persist one WorkReceipt for the exact authenticated session.

        Public input may describe the objective and an idempotency hint, but it
        cannot choose project, scope, assignee, receipt identity, server time,
        fencing generation, or initial lifecycle state.
        """

        from .durable_runtime import DurableCollaborationError

        values = dict(arguments or {})
        forbidden = {
            "agent_session_id",
            "assigned_agent",
            "capabilities",
            "coordination_session_id",
            "lease",
            "lease_id",
            "owner_session_id",
            "policy",
            "role",
            "session_id",
            "work_receipt",
        }
        if forbidden.intersection(values):
            raise DurableCollaborationError("collaboration_work_authority_claim_forbidden")
        objective = str(values.get("objective") or "").strip()
        if not objective:
            raise DurableCollaborationError("work_objective_required")
        if len(objective.encode("utf-8")) > 4096:
            raise DurableCollaborationError("work_objective_too_large")
        raw_dependencies = values.get("dependency_work_ids") or ()
        if not isinstance(raw_dependencies, (list, tuple)):
            raise DurableCollaborationError("work_dependencies_invalid")
        dependencies = tuple(
            sorted({str(item or "").strip() for item in raw_dependencies if str(item or "").strip()})
        )
        if len(dependencies) > 32 or any(len(item.encode("utf-8")) > 256 for item in dependencies):
            raise DurableCollaborationError("work_dependencies_invalid")
        max_attempts = values.get("max_attempts", 1)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 10
        ):
            raise DurableCollaborationError("work_max_attempts_invalid")
        idempotency_hint = str(values.get("request_id") or "").strip()
        if len(idempotency_hint.encode("utf-8")) > 256:
            raise DurableCollaborationError("work_request_id_invalid")
        now, _now_text = self.runtime._now()  # noqa: SLF001 - server clock seam
        issued_at = server_now_text(lambda: now)
        expires_at = server_now_text(lambda: now + timedelta(hours=1))
        identity_basis = {
            "project_id": self.session.project.project_id,
            "coordination_session_id": self.session.coordination_session_id,
            "assigned_agent_id": self.session.identity.agent_id,
            "objective": objective,
            "dependency_work_ids": list(dependencies),
            "max_attempts": max_attempts,
            "request_id": idempotency_hint,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity_basis,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        work_item_id = f"work:mcp:{digest[:40]}"
        existing = self.runtime.get_work(work_item_id)
        if isinstance(existing, Mapping):
            return self._work_operation_projection("register", work_item_id, work=existing)
        receipt = WorkReceipt(
            receipt_id=f"work-receipt:mcp:{digest[:40]}",
            work_item_id=work_item_id,
            project=self.session.project,
            coordination_session_id=self.session.coordination_session_id,
            assigned_agent=self.session.identity,
            objective=objective,
            fencing_generation=1,
            issued_at=issued_at,
            expires_at=expires_at,
            dependency_work_ids=dependencies,
        )
        work = self.runtime.register_work(
            receipt,
            state="ready",
            max_attempts=max_attempts,
            agent_session_id=self.session.session_id,
        )
        return self._work_operation_projection("register", receipt.work_item_id, work=work)

    def work_claim(self, *, work_item_id: object) -> dict[str, object]:
        """Claim assigned work using a lease minted from server-owned rows.

        The host derives the next fence while the canonical writer is held.
        This avoids turning the read-only inbox projection into a lease
        authority and prevents two simultaneous claim calls from minting the
        same generation.
        """

        from .durable_runtime import DurableCollaborationError

        requested = str(work_item_id or "").strip()
        if not requested:
            raise DurableCollaborationError("work_item_id_required")
        candidates = self.runtime.assigned_work_projection(
            self.session.project,
            self.session.session_id,
            limit=64,
        )
        projected = next(
            (item for item in candidates if str(item.get("work_item_id") or "") == requested),
            None,
        )
        if projected is None:
            raise DurableCollaborationError("work_not_assigned_to_session")
        if str(projected.get("state") or "") not in {"proposed", "ready", "rework"}:
            raise DurableCollaborationError("work_state_transition_invalid")
        work = self.runtime.get_work(requested)
        if not isinstance(work, Mapping):
            raise DurableCollaborationError("work_not_registered")
        try:
            receipt = self._work_receipt_from_stored(work.get("work_receipt_json"))
            attempt = int(work.get("attempt") or 0) + 1
            max_attempts = int(work.get("max_attempts") or 0)
        except (TypeError, ValueError) as exc:
            raise DurableCollaborationError("work_receipt_projection_corrupt") from exc
        if receipt.project != self.session.project:
            raise DurableCollaborationError("work_project_scope_mismatch")
        if receipt.coordination_session_id != self.session.coordination_session_id:
            raise DurableCollaborationError("work_session_scope_mismatch")
        if receipt.assigned_agent != self.session.identity:
            raise DurableCollaborationError("work_not_assigned_to_session")
        with self.runtime._write():  # noqa: SLF001 - same canonical writer as runtime
            locked_work = self.runtime.get_work(requested)
            if not isinstance(locked_work, Mapping):
                raise DurableCollaborationError("work_not_registered")
            locked_state = str(locked_work.get("state") or "").strip().casefold()
            if locked_state not in {"proposed", "ready", "rework"}:
                raise DurableCollaborationError("work_state_transition_invalid")
            attempt = int(locked_work.get("attempt") or 0) + 1
            max_attempts = int(locked_work.get("max_attempts") or 0)
            previous_leases = self.runtime._fetchall(  # noqa: SLF001
                "SELECT fencing_generation FROM collaboration_work_leases WHERE work_item_id=?",
                (receipt.work_item_id,),
            )
            previous_generation = max(
                (int(row.get("fencing_generation") or 0) for row in previous_leases),
                default=0,
            )
            generation = max(
                int(receipt.fencing_generation),
                int(previous_generation or 0) + 1,
            )
            # Read the same server clock authority used by the durable runtime;
            # the host must not introduce a second wall-clock decision.
            now, _now_text = self.runtime._now()  # noqa: SLF001
            issued_at = server_now_text(lambda: now)
            expires = min(
                datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00")),
                now + timedelta(seconds=_SESSION_TTL_SECONDS),
            )
            if expires <= now:
                raise DurableCollaborationError("work_receipt_expired")
            lease_item = WorkItem(
                work_item_id=receipt.work_item_id,
                project=receipt.project,
                owner_kind=AGENT_OWNER_KIND,
                policy_kind=AGENT_WORK_POLICY,
                operation_kind="implement",
                input_sha256=receipt.content_sha256,
                result_schema="collaboration-result/v1",
                created_at=receipt.issued_at,
                max_attempts=max_attempts,
                coordination_session_id=receipt.coordination_session_id,
            )
            lease_seed = {
                "project_id": receipt.project.project_id,
                "coordination_session_id": receipt.coordination_session_id,
                "work_item_id": receipt.work_item_id,
                "agent_session_id": self.session.session_id,
                "attempt": attempt,
                "fencing_generation": generation,
                "issued_at": issued_at,
            }
            lease_digest = hashlib.sha256(
                repr(sorted(lease_seed.items())).encode("utf-8")
            ).hexdigest()
            lease = WorkLease(
                lease_id=f"lease:mcp:{lease_digest[:40]}",
                work_item=lease_item,
                owner_kind=AGENT_OWNER_KIND,
                policy_kind=AGENT_WORK_POLICY,
                owner_id=self.session.identity.agent_id,
                owner_identity=self.session.identity,
                fencing_generation=generation,
                attempt=attempt,
                issued_at=issued_at,
                expires_at=server_now_text(lambda: expires),
                result_binding_sha256=receipt.content_sha256,
                idempotency_key_sha256=f"sha256:{lease_digest}",
            )
            self.runtime.claim_work(
                lease,
                state="in_progress",
                agent_session_id=self.session.session_id,
            )
        return self._work_operation_projection("claim", requested)

    def lease_heartbeat(self, *, work_item_id: object) -> dict[str, object]:
        """Heartbeat only the active lease owned by this exact session."""

        from .durable_runtime import DurableCollaborationError

        requested = str(work_item_id or "").strip()
        if not requested:
            raise DurableCollaborationError("work_item_id_required")
        projected = self.runtime.assigned_work_projection(
            self.session.project,
            self.session.session_id,
            limit=64,
        )
        item = next(
            (entry for entry in projected if str(entry.get("work_item_id") or "") == requested),
            None,
        )
        if not isinstance(item, Mapping) or not isinstance(item.get("lease"), Mapping):
            raise DurableCollaborationError("work_active_lease_required")
        work = self.runtime.get_work(requested)
        if not isinstance(work, Mapping):
            raise DurableCollaborationError("work_not_registered")
        receipt = self._work_receipt_from_stored(work.get("work_receipt_json"))
        lease = self._active_lease_for_work(requested)
        sequence = int(lease.get("heartbeat_sequence") or 0) + 1
        lease_value = self._lease_from_stored(lease.get("lease_json"))
        heartbeat = LeaseHeartbeat.for_lease(
            lease_value,
            heartbeat_id=f"heartbeat:mcp:{hashlib.sha256(f'{lease_value.lease_id}:{sequence}'.encode()).hexdigest()[:40]}",
            sequence=sequence,
            sent_at=server_now_text(),
        )
        receipt_projection = self.runtime.heartbeat_lease(
            heartbeat,
            agent_session_id=self.session.session_id,
        )
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "durable",
            "operation": "lease_heartbeat",
            "persistent": True,
            "work_item_id": receipt.work_item_id,
            "heartbeat": {
                "fencing_generation": receipt_projection.get("fencing_generation"),
                "heartbeat_sequence": receipt_projection.get("heartbeat_sequence"),
                "observed_at": receipt_projection.get("observed_at"),
                "state": receipt_projection.get("state"),
            },
            "canonical_memory_effect": "none",
        }

    def work_review(self, *, work_item_id: object) -> dict[str, object]:
        """Review submitted peer work as the exact authenticated session."""

        requested = str(work_item_id or "").strip()
        work = self.runtime.review_work(
            requested,
            reviewer_session_id=self.session.session_id,
        )
        return self._work_operation_projection("review", requested, work=work)

    def work_accept(
        self,
        *,
        work_item_id: object,
        acceptance_receipt_id: object,
        acceptance_repository: DurableAcceptanceAuthorityRepository | None,
    ) -> dict[str, object]:
        """Accept only a durable receipt already issued by server authority."""

        from .durable_runtime import DurableCollaborationError

        requested = str(work_item_id or "").strip()
        receipt_id = str(acceptance_receipt_id or "").strip()
        if not requested:
            raise DurableCollaborationError("work_item_id_required")
        if not receipt_id:
            raise DurableCollaborationError("acceptance_receipt_id_required")
        if acceptance_repository is None:
            raise DurableCollaborationError("work_acceptance_repository_required")
        try:
            acceptance = acceptance_repository.load_acceptance_by_id(receipt_id)
        except Exception as exc:
            raise DurableCollaborationError("work_acceptance_receipt_unavailable") from exc
        if acceptance is None:
            raise DurableCollaborationError("work_acceptance_receipt_not_found")
        if acceptance.work_item_id != requested:
            raise DurableCollaborationError("work_acceptance_work_scope_mismatch")
        work = self.runtime.accept_work(
            requested,
            reviewer_session_id=self.session.session_id,
            acceptance_receipt=acceptance,
        )
        return self._work_operation_projection("accept", requested, work=work)

    def _work_operation_projection(
        self,
        operation: str,
        work_item_id: str,
        *,
        work: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        items = self.runtime.assigned_work_projection(
            self.session.project,
            self.session.session_id,
            limit=64,
        )
        item = next(
            (dict(entry) for entry in items if str(entry.get("work_item_id") or "") == work_item_id),
            None,
        )
        if item is None and isinstance(work, Mapping):
            item = {
                "work_item_id": work_item_id,
                "state": str(work.get("state") or ""),
                "authority_effect": "none",
            }
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "durable",
            "operation": operation,
            "persistent": True,
            "work": item,
            "canonical_memory_effect": "none",
        }

    def _active_lease_for_work(self, work_item_id: str) -> Mapping[str, object]:
        from .durable_runtime import DurableCollaborationError

        rows = self.runtime._fetchall(  # noqa: SLF001 - host is the runtime's composition seam
            "SELECT * FROM collaboration_work_leases WHERE work_item_id=? "
            "AND owner_session_id=? AND state='active' ORDER BY fencing_generation DESC",
            (work_item_id, self.session.session_id),
        )
        if len(rows) != 1:
            raise DurableCollaborationError(
                "work_active_lease_required" if not rows else "work_active_lease_ambiguous"
            )
        return rows[0]

    @staticmethod
    def _work_receipt_from_stored(value: object) -> Any:
        from .durable_runtime import _work_receipt_from_projection

        if not isinstance(value, str):
            from .durable_runtime import DurableCollaborationError

            raise DurableCollaborationError("work_receipt_projection_corrupt")
        try:
            import json

            return _work_receipt_from_projection(json.loads(value))
        except Exception as exc:
            from .durable_runtime import DurableCollaborationError

            if isinstance(exc, DurableCollaborationError):
                raise
            raise DurableCollaborationError("work_receipt_projection_corrupt") from exc

    @staticmethod
    def _lease_from_stored(value: object) -> WorkLease:
        from .durable_runtime import DurableCollaborationError, _work_lease_from_projection

        if not isinstance(value, str):
            raise DurableCollaborationError("work_lease_projection_corrupt")
        try:
            import json

            return _work_lease_from_projection(json.loads(value))
        except Exception as exc:
            if isinstance(exc, DurableCollaborationError):
                raise
            raise DurableCollaborationError("work_lease_projection_corrupt") from exc

    def session_init_projection(self) -> dict[str, object]:
        result = self.session_init_result
        stored_cursor = result.cursor
        next_cursor = result.next_cursor
        return {
            "schema_version": "durable-collaboration-session-init/v1",
            "state": result.state,
            "persistent": True,
            "authority": "pp-server-backend/pp-core",
            "project_id": self.session.project.project_id,
            "agent": {
                "agent_id": self.session.identity.agent_id,
                "role": self.session.identity.role,
                "state": self.session.state,
                "expires_at": self.session.expires_at,
            },
            "bound_policy": self._bound_policy_projection(result.policy),
            "working_set_summary": dict(result.working_set_summary),
            "assigned_work": [dict(item) for item in result.assigned_work],
            "peer_delta": {
                "items": [dict(item) for item in result.peer_delta],
                "limit": _SESSION_INIT_EVENT_LIMIT,
                "has_more": result.cursor_has_more,
            },
            "cursor": self._cursor_projection(
                stored=stored_cursor,
                next_cursor=next_cursor,
                source_head_sequence=result.source_head_sequence,
                has_more=result.cursor_has_more,
            ),
            "visibility": {
                "work_receipts": "bounded-display-fields",
                "event_payloads": "redacted",
                "agent_capabilities": "redacted",
                "transport_identity": "server-private",
                "session_authority": "server-private",
            },
            "canonical_memory_effect": "none",
        }

    def reconcile_tool_call(self, *, cursor_ack: object | None = None) -> dict[str, object]:
        """Reconcile presence, exact-session leases, and the bounded peer feed."""

        from .durable_runtime import DurableCollaborationError

        with self.runtime._write():  # noqa: SLF001 - one canonical lifecycle transaction
            if cursor_ack is not None:
                if isinstance(cursor_ack, bool) or not isinstance(cursor_ack, int) or cursor_ack < 0:
                    raise DurableCollaborationError("collaboration_cursor_ack_invalid")
                current = self.runtime.load_cursor(
                    project=self.session.project,
                    coordination_session_id=self.session.coordination_session_id,
                    consumer_id=self.session.session_id,
                )
                page = self.runtime.peer_delta_page(session=self.session, after=current)
                next_cursor = page["next_cursor"]
                if not isinstance(next_cursor, EventCursor) or cursor_ack != next_cursor.sequence:
                    raise DurableCollaborationError("collaboration_cursor_ack_mismatch")
                self.runtime.record_cursor(
                    next_cursor,
                    consumer_id=self.session.session_id,
                    source_head_sequence=int(page["source_head_sequence"]),
                )
            receipt = self.runtime.heartbeat(self.session.session_id)
            lease_rows = self.runtime._fetchall(  # noqa: SLF001 - exact-session authority set
                "SELECT * FROM collaboration_work_leases "
                "WHERE project_id=? AND coordination_session_id=? "
                "AND owner_id=? AND owner_session_id=? "
                "AND owner_kind=? AND state='active' ORDER BY lease_id",
                (
                    self.session.project.project_id,
                    self.session.coordination_session_id,
                    self.session.identity.agent_id,
                    self.session.session_id,
                    AGENT_OWNER_KIND,
                ),
            )
            _now, now_text = self.runtime._now()  # noqa: SLF001 - shared server clock
            for row in lease_rows:
                lease = self._lease_from_stored(row.get("lease_json"))
                sequence = int(row.get("heartbeat_sequence") or 0) + 1
                heartbeat = LeaseHeartbeat.for_lease(
                    lease,
                    heartbeat_id=(
                        "heartbeat:mcp:"
                        + hashlib.sha256(f"{lease.lease_id}:{sequence}".encode()).hexdigest()[:40]
                    ),
                    sequence=sequence,
                    sent_at=now_text,
                )
                self.runtime.heartbeat_lease(
                    heartbeat,
                    agent_session_id=self.session.session_id,
                )
            stored = self.runtime.load_cursor(
                project=self.session.project,
                coordination_session_id=self.session.coordination_session_id,
                consumer_id=self.session.session_id,
            )
            page = self.runtime.peer_delta_page(session=self.session, after=stored)
            assigned_work = self.runtime.assigned_work_projection(
                self.session.project,
                self.session.session_id,
            )
            active_leases = [
                {
                    "work_item_id": str(row.get("work_item_id") or ""),
                    "state": "active",
                    "fencing_generation": int(row.get("fencing_generation") or 0),
                    "expires_at": str(row.get("expires_at") or ""),
                }
                for row in lease_rows
            ]
        return {
            "schema_version": "durable-collaboration-tool-call-reconcile/v1",
            "state": receipt.get("state", "active"),
            "observed_at": receipt.get("observed_at"),
            "reconcile": receipt.get("reconcile", {}),
            "working_set_summary": self.runtime.working_set_summary(session=self.session),
            "assigned_work": assigned_work,
            "active_leases": active_leases,
            "peer_delta": {
                "items": [dict(item) for item in page["items"]],
                "limit": int(page["limit"]),
                "has_more": bool(page["has_more"]),
            },
            "cursor": self._cursor_projection(
                stored=stored,
                next_cursor=page["next_cursor"],
                source_head_sequence=int(page["source_head_sequence"]),
                has_more=bool(page["has_more"]),
            ),
            "persistent": True,
            "canonical_memory_effect": "none",
        }

    def heartbeat(self, *, cursor_ack: object | None = None) -> dict[str, object]:
        """Compatibility lifecycle projection delegated to tool-call reconcile."""

        result = self.reconcile_tool_call(cursor_ack=cursor_ack)
        return {
            **result,
            "schema_version": "durable-collaboration-heartbeat/v1",
        }

    def publish_stop_activity(self, *, idempotency_key: object = "") -> dict[str, object]:
        """Publish bounded typed work activity for one authenticated Stop.

        The Hook never supplies work authority or result content.  The host
        selects exact-session rows from canonical SQLite, emits only a
        progress event for live work, and emits a bounded submitted event only
        when a server-persisted result already exists.  Event ids are derived
        from a server-safe idempotency digest, so retries replay rather than
        append duplicates.  Accepted work is intentionally not fabricated by
        this path; independent server acceptance remains the sole gate.
        """

        from .durable_runtime import DurableCollaborationError, _sha256

        raw_key = str(idempotency_key or "").strip()
        if len(raw_key.encode("utf-8")) > 512:
            raise DurableCollaborationError("stop_activity_idempotency_key_invalid")
        key_digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        with self.runtime._write():  # noqa: SLF001 - one canonical event transaction
            _now, now_text = self.runtime._now()  # noqa: SLF001 - server clock
            rows = self.runtime._fetchall(  # noqa: SLF001 - exact-session source set
                "SELECT work.work_item_id,work.project_id,work.coordination_session_id,"
                "work.state,work.work_receipt_sha256,leases.lease_id "
                "FROM collaboration_work_items AS work "
                "LEFT JOIN collaboration_work_leases AS leases "
                "ON leases.work_item_id=work.work_item_id "
                "AND leases.owner_session_id=? AND leases.owner_kind=? "
                "AND leases.state='active' "
                "WHERE work.project_id=? AND work.coordination_session_id=? "
                "AND work.assigned_agent_id=? "
                "AND (leases.lease_id IS NOT NULL OR work.state IN ('submitted','accepted')) "
                "ORDER BY work.updated_at,work.work_item_id LIMIT 16",
                (
                    self.session.session_id,
                    AGENT_OWNER_KIND,
                    self.session.project.project_id,
                    self.session.coordination_session_id,
                    self.session.identity.agent_id,
                ),
            )
            typed_events: list[dict[str, object]] = []
            for row in rows:
                work_item_id = str(row["work_item_id"])
                state = str(row["state"] or "")
                event_type = "work.progressed"
                summary = "Work progress observed at Stop"
                subject_refs = (work_item_id,)
                evidence_refs: tuple[str, ...] = ()
                event_identity = key_digest
                if state in {"submitted", "accepted"}:
                    result = self.runtime._fetchone(  # noqa: SLF001
                        "SELECT receipt_id,result_sha256,result_json FROM collaboration_results "
                        "WHERE work_item_id=? ORDER BY submitted_at DESC,receipt_id LIMIT 1",
                        (work_item_id,),
                    )
                    if result is None:
                        continue
                    try:
                        result_projection = json.loads(str(result["result_json"]))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise DurableCollaborationError("stop_activity_result_corrupt") from exc
                    if not isinstance(result_projection, Mapping):
                        raise DurableCollaborationError("stop_activity_result_corrupt")
                    stored_binding = result_projection.get("lease_binding")
                    if not isinstance(stored_binding, Mapping):
                        raise DurableCollaborationError("stop_activity_result_lease_binding_missing")
                    stored_receipt = dict(result_projection)
                    stored_receipt.pop("lease_binding", None)
                    if _sha256(stored_receipt) != str(result["result_sha256"]):
                        raise DurableCollaborationError("stop_activity_result_digest_mismatch")
                    if (
                        str(stored_receipt.get("receipt_id") or "") != str(result["receipt_id"])
                        or str(stored_receipt.get("work_item_id") or "") != work_item_id
                        or str(stored_receipt.get("project_id") or "")
                        != self.session.project.project_id
                        or str(stored_receipt.get("coordination_session_id") or "")
                        != self.session.coordination_session_id
                        or str(stored_receipt.get("work_receipt_sha256") or "")
                        != str(row["work_receipt_sha256"])
                    ):
                        raise DurableCollaborationError("stop_activity_result_scope_mismatch")
                    lease_id = str(stored_binding.get("lease_id") or "").strip()
                    if not lease_id:
                        raise DurableCollaborationError("stop_activity_result_lease_binding_missing")
                    lease = self.runtime._fetchone(  # noqa: SLF001
                        "SELECT owner_session_id,owner_id,owner_kind,project_id,"
                        "coordination_session_id,work_item_id,lease_sha256,fencing_generation,state "
                        "FROM collaboration_work_leases WHERE lease_id=?",
                        (lease_id,),
                    )
                    if (
                        lease is None
                        or str(lease["owner_session_id"]) != self.session.session_id
                        or str(lease["owner_id"]) != self.session.identity.agent_id
                        or str(lease["owner_kind"]) != AGENT_OWNER_KIND
                        or str(lease["project_id"]) != self.session.project.project_id
                        or str(lease["coordination_session_id"])
                        != self.session.coordination_session_id
                        or str(lease["work_item_id"]) != work_item_id
                        or str(lease["state"]) != "completed"
                        or str(stored_binding.get("lease_sha256") or "")
                        != str(lease["lease_sha256"])
                        or int(stored_binding.get("fencing_generation") or 0)
                        != int(lease["fencing_generation"])
                        or str(stored_binding.get("result_binding_sha256") or "")
                        != str(row["work_receipt_sha256"])
                    ):
                        continue
                    event_type = "work.submitted"
                    summary = str(result_projection.get("summary") or "Work result submitted").strip()
                    subject_refs = (work_item_id, str(result["receipt_id"]))
                    evidence_refs = (str(result["result_sha256"]),)
                    event_identity = f"{result['receipt_id']}\x1f{result['result_sha256']}"
                event_digest = hashlib.sha256(
                    "\x1f".join(
                        (
                            self.session.project.project_id,
                            self.session.coordination_session_id,
                            work_item_id,
                            event_type,
                            event_identity,
                        )
                    ).encode("utf-8")
                ).hexdigest()[:40]
                event = CollaborationEvent(
                    event_id=f"event:stop-activity:{event_digest}",
                    project=self.session.project,
                    coordination_session_id=self.session.coordination_session_id,
                    actor=self.session.identity,
                    event_type=event_type,
                    summary=summary[:512],
                    created_at=now_text,
                    work_item_id=work_item_id,
                    subject_refs=subject_refs,
                    evidence_refs=evidence_refs,
                    payload={
                        "source": "codex-stop-hook",
                        "state": state,
                        "lease_present": bool(row.get("lease_id")),
                    },
                )
                existing_event = self.runtime._fetchone(  # noqa: SLF001
                    "SELECT 1 FROM collaboration_events WHERE event_id=?",
                    (event.event_id,),
                )
                cursor = self.runtime._append_event_in_transaction(  # noqa: SLF001
                    event,
                    actor_session_id=self.session.session_id,
                )
                typed_events.append(
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "work_item_id": work_item_id,
                        "cursor": cursor.to_dict(),
                        "replayed": existing_event is not None,
                    }
                )
            return {
                "schema_version": "durable-collaboration-stop-activity/v1",
                "state": "durable",
                "persistent": True,
                "events": typed_events,
                "canonical_memory_effect": "none",
            }

    def continuation_is_active(self) -> bool:
        """Re-read exact durable state before attaching a fresh transport."""

        with self.runtime._write():  # noqa: SLF001 - host is the runtime composition seam
            row = self.runtime._fetchone(  # noqa: SLF001
                "SELECT project_id,coordination_session_id,agent_id,state "
                "FROM collaboration_agent_sessions WHERE session_id=?",
                (self.session.session_id,),
            )
        return bool(
            row is not None
            and str(row["project_id"]) == self.session.project.project_id
            and str(row["coordination_session_id"]) == self.session.coordination_session_id
            and str(row["agent_id"]) == self.session.identity.agent_id
            and str(row["state"]) in {"active", "idle"}
        )

    def end_session(self, *, reason: str = "mcp_session_end") -> dict[str, object]:
        receipt = self.runtime.end_session(self.session.session_id, reason=reason)
        return {
            "schema_version": "durable-collaboration-session-end/v1",
            "state": receipt.get("state", "closed"),
            "released_lease_count": len(receipt.get("released_lease_ids") or ()),
            "cursor": receipt.get("cursor", {}),
            "persistent": True,
            "canonical_memory_effect": "none",
        }

    @staticmethod
    def _bound_policy_projection(policy: Any) -> dict[str, object]:
        value = dict(policy) if isinstance(policy, dict) else dict(policy or {})
        return {
            "schema_version": "collaboration-bound-policy/v1",
            "issuer": value.get("issuer", "pp-server-backend"),
            "agent_id": value.get("agent_id", ""),
            "role": value.get("role", ""),
            "project_id": value.get("project_id", ""),
            "coordination_session_id": value.get("coordination_session_id", ""),
            "policy_revision": value.get("policy_revision", ""),
            "policy_digest": value.get("policy_digest", ""),
            "issued_at": value.get("issued_at", ""),
            "expires_at": value.get("expires_at", ""),
            "authority_effect": "none-without-server-session",
        }

    @staticmethod
    def _cursor_projection(
        *,
        stored: EventCursor,
        next_cursor: EventCursor,
        source_head_sequence: int,
        has_more: bool,
    ) -> dict[str, object]:
        return {
            "schema_version": "collaboration-cursor-delivery/v1",
            "stored_sequence": stored.sequence,
            "next_sequence": next_cursor.sequence,
            "source_head_sequence": source_head_sequence,
            "has_more": has_more,
            "ack_required": next_cursor.sequence > stored.sequence,
            "advance": "explicit-heartbeat-ack",
            "scope": "current-authenticated-session",
        }


@dataclass(frozen=True, slots=True)
class _AuthorityBundle:
    """Per-engine, server-owned authorities shared across all live sessions."""

    source_revision: str
    policy_authority: AgentPolicyBindingAuthority
    role_assignment_repository: DurableRoleAssignmentRepository
    role_assignment_authority: RoleAssignmentAuthority
    activity_repository: DurableActivityRepository
    activity_authority: ActivityAuditAuthority
    acceptance_repository: DurableAcceptanceAuthorityRepository
    acceptance_authority: AcceptanceReceiptAuthority
    coordinator_repository: DurableCoordinatorRepository
    coordination_plan_repository: DurableCoordinationPlanRepository
    coordination_plan_authority: CoordinationPlanAuthority


@dataclass(frozen=True, slots=True)
class _ScopeAuthorityBundle:
    """Authorities shared only by one project/workflow coordination scope."""

    coordinator_authority: CoordinatorAuditAuthority
    top_level_authorization_verifier: TopLevelAgentAuthorizationVerifier | None = None
    top_level_binding_authority: TopLevelAgentBindingAuthority | None = None


def _server_clock() -> datetime:
    return datetime.now(timezone.utc)


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if _base64url_encode(decoded) != value:
        raise ValueError
    return decoded


def _read_private_continuation_keyring(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("durable_collaboration_continuation_keyring_not_regular")
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise ValueError("durable_collaboration_continuation_keyring_permissions_invalid")
        if os.name == "posix" and metadata.st_uid != os.geteuid():
            raise ValueError("durable_collaboration_continuation_keyring_owner_invalid")
        if metadata.st_size <= 0 or metadata.st_size > 16 * 1024:
            raise ValueError("durable_collaboration_continuation_keyring_size_invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise ValueError("durable_collaboration_continuation_keyring_read_incomplete")
        return payload
    finally:
        os.close(descriptor)


def _create_private_continuation_keyring(path: Path) -> bytes:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    key_id = hashlib.sha256(secret).hexdigest()[:16]
    payload = json.dumps(
        {
            "schema_version": _CONTINUATION_KEYRING_SCHEMA,
            "active_key_id": key_id,
            "keys": {key_id: _base64url_encode(secret)},
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_private_continuation_keyring(path)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return payload


def _decode_continuation_keyring(payload: bytes) -> tuple[str, dict[str, bytes]]:
    try:
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("durable_collaboration_continuation_keyring_invalid") from exc
    if not isinstance(decoded, Mapping) or decoded.get("schema_version") != (
        _CONTINUATION_KEYRING_SCHEMA
    ):
        raise ValueError("durable_collaboration_continuation_keyring_invalid")
    active_key_id = str(decoded.get("active_key_id") or "").strip()
    raw_keys = decoded.get("keys")
    if not isinstance(raw_keys, Mapping):
        raise ValueError("durable_collaboration_continuation_keyring_invalid")
    keys: dict[str, bytes] = {}
    try:
        for raw_key_id, raw_secret in raw_keys.items():
            key_id = str(raw_key_id or "").strip()
            keys[key_id] = _base64url_decode(str(raw_secret or ""))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("durable_collaboration_continuation_keyring_invalid") from exc
    return active_key_id, keys


def _continuation_claim(value: object, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        return "" if not required else ""
    if len(text) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in text):
        return ""
    return text


def _required_continuation_claim(value: object) -> str:
    text = _continuation_claim(value)
    if not text:
        raise ValueError
    return text


def _server_actor(value: object) -> str:
    """Normalize a configured actor without deriving a work role from its name."""

    actor = str(value or "").strip().casefold().replace("-", "_")
    return actor if _SERVER_ACTOR_RE.fullmatch(actor) is not None else ""


def _transport_session(value: object) -> str:
    """Validate the opaque id minted solely by the MCP server composition seam."""

    token = str(value or "").strip()
    return token if _TRANSPORT_SESSION_RE.fullmatch(token) is not None else ""


def _workflow_scope(value: object) -> str:
    """Return the server-resolved workflow/coordination scope or fail closed."""

    scope = str(value or "").strip()
    if not scope or len(scope) > 255:
        return ""
    return scope


def _durable_session_id(
    *,
    project_id: str,
    server_actor: str,
    coordination_session_id: str,
    transport_session_id: str,
) -> str:
    """Mint a repeatable id only for the same trusted transport instance.

    The transport id is randomly created by the MCP server and is never part
    of caller JSON.  Hashing it avoids exposing even that local identifier in
    durable storage while guaranteeing that same-scope peer transports obtain
    different durable sessions.
    """

    digest = hashlib.sha256(
        "\x1f".join(
            (project_id, server_actor, coordination_session_id, transport_session_id)
        ).encode("utf-8")
    ).hexdigest()[:40]
    return f"agent-session:mcp:{digest}"


def _transaction_factory(*, write_lock: Any, batch: Callable[[], Any]) -> Callable[[], Any]:
    """Compose the canonical engine writer lock and SQLite transaction adapter."""

    @contextmanager
    def transaction():
        with write_lock, batch():
            yield

    return transaction


def _source_revision() -> str:
    """Resolve immutable image lineage without weakening production checks."""

    revision = (
        str(
            os.environ.get("PP_BUILD_SOURCE_REVISION") or os.environ.get("PP_SOURCE_REVISION") or ""
        )
        .strip()
        .casefold()
    )
    if _SOURCE_REVISION_RE.fullmatch(revision):
        return revision
    runtime_environment = str(os.environ.get("PP_RUNTIME_ENV") or "").strip().casefold()
    if runtime_environment in {"production", "prod", "staging"}:
        from .durable_runtime import DurableCollaborationError

        raise DurableCollaborationError("durable_acceptance_source_revision_unavailable")
    # Local/unit composition has no OCI image label.  The sentinel keeps the
    # authority graph real while remaining visibly non-publishable.
    return "0" * 40


def _server_storage(engine: Any) -> tuple[Any, Any, Callable[[], Any], Any] | None:
    storage = getattr(engine, "_sqlite", None)
    connection = getattr(storage, "_conn", None)
    batch = getattr(storage, "batch", None)
    write_lock = getattr(engine, "_write_lock", None)
    if connection is None or not callable(batch) or write_lock is None:
        return None
    return storage, connection, batch, write_lock


def _authority_bundle(
    engine: Any,
    *,
    connection: Any,
    transaction_factory: Callable[[], Any],
    clock: Callable[[], datetime],
    source_revision: str,
) -> _AuthorityBundle:
    """Open or reuse only server-owned authority instances for one engine."""

    from .durable_runtime import DurableCollaborationError, DurableCollaborationRuntime

    existing = getattr(engine, _AUTHORITY_ATTR, None)
    if isinstance(existing, _AuthorityBundle):
        if existing.source_revision != source_revision:
            raise DurableCollaborationError("durable_acceptance_source_revision_changed")
        return existing

    policy_authority = open_server_agent_policy_binding_authority(clock=clock)
    # The durable runtime is verify-only.  Constructing it before the additive
    # authorities preserves the more useful primary missing/stale-schema code.
    DurableCollaborationRuntime(
        connection,
        transaction_factory=transaction_factory,
        clock=clock,
        policy_authority=policy_authority,
    )
    role_assignment_repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=transaction_factory,
        clock=clock,
    )
    role_assignment_authority = open_server_role_assignment_authority(
        repository=role_assignment_repository,
        clock=clock,
    )
    activity_repository = DurableActivityRepository(
        connection,
        transaction_factory=transaction_factory,
    )
    activity_authority = open_server_activity_audit_authority(
        repository=activity_repository,
        clock=clock,
    )
    acceptance_repository = DurableAcceptanceAuthorityRepository(
        connection,
        transaction_factory=transaction_factory,
    )
    acceptance_authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_assignment_authority,
        repository=acceptance_repository,
        current_review_policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
        current_source_revision=source_revision,
        clock=clock,
    )
    coordinator_repository = DurableCoordinatorRepository(
        connection,
        activity_repository=activity_repository,
        transaction_factory=transaction_factory,
    )
    coordination_plan_repository = DurableCoordinationPlanRepository(
        connection,
        transaction_factory=transaction_factory,
        clock=clock,
    )
    bundle = _AuthorityBundle(
        source_revision=source_revision,
        policy_authority=policy_authority,
        role_assignment_repository=role_assignment_repository,
        role_assignment_authority=role_assignment_authority,
        activity_repository=activity_repository,
        activity_authority=activity_authority,
        acceptance_repository=acceptance_repository,
        acceptance_authority=acceptance_authority,
        coordinator_repository=coordinator_repository,
        coordination_plan_repository=coordination_plan_repository,
        coordination_plan_authority=open_server_coordination_plan_authority(
            repository=coordination_plan_repository,
            clock=clock,
        ),
    )
    try:
        setattr(engine, _AUTHORITY_ATTR, bundle)
    except Exception as exc:
        raise DurableCollaborationError(
            "durable_collaboration_authority_cache_unavailable"
        ) from exc
    return bundle


def _scope_authorities(
    engine: Any,
    *,
    bundle: _AuthorityBundle,
    project: ProjectScope,
    coordination_session_id: str,
    clock: Callable[[], datetime],
    verifier: TopLevelAgentAuthorizationVerifier | None,
) -> _ScopeAuthorityBundle:
    """Return scope-local coordinator authority without crossing transports."""

    from .durable_runtime import DurableCollaborationError

    mapping = getattr(engine, _SCOPE_AUTHORITY_ATTR, None)
    if not isinstance(mapping, dict):
        mapping = {}
        try:
            setattr(engine, _SCOPE_AUTHORITY_ATTR, mapping)
        except Exception as exc:
            raise DurableCollaborationError(
                "durable_collaboration_authority_cache_unavailable"
            ) from exc
    key = f"{project.project_id}\x1f{coordination_session_id}"
    existing = mapping.get(key)
    if not isinstance(existing, _ScopeAuthorityBundle):
        top_level = (
            TopLevelAgentBindingAuthority(
                repository=bundle.coordination_plan_repository,
                authorization_verifier=verifier,
                clock=clock,
            )
            if verifier is not None
            else None
        )
        existing = _ScopeAuthorityBundle(
            coordinator_authority=open_server_coordinator_audit_authority(
                project=project,
                coordination_session_id=coordination_session_id,
                repository=bundle.coordinator_repository,
                clock=clock,
            ),
            top_level_authorization_verifier=verifier,
            top_level_binding_authority=top_level,
        )
        mapping[key] = existing
        return existing
    if verifier is None:
        return existing
    if existing.top_level_authorization_verifier is None:
        updated = replace(
            existing,
            top_level_authorization_verifier=verifier,
            top_level_binding_authority=TopLevelAgentBindingAuthority(
                repository=bundle.coordination_plan_repository,
                authorization_verifier=verifier,
                clock=clock,
            ),
        )
        mapping[key] = updated
        return updated
    if existing.top_level_authorization_verifier is not verifier:
        raise DurableCollaborationError("durable_collaboration_top_level_verifier_conflict")
    return existing


def _safe_failure(exc: Exception) -> str:
    """Return only stable reason codes from the server-owned composition seam."""

    code = str(getattr(exc, "code", "") or "").strip()
    allowed = {
        "durable_acceptance_source_revision_changed",
        "durable_acceptance_source_revision_unavailable",
        "durable_collaboration_schema_missing",
        "durable_collaboration_schema_stale",
        "durable_collaboration_authority_cache_unavailable",
        "durable_connection_invalid",
        "durable_policy_authority_required",
        "durable_role_assignment_authority_invalid",
        "durable_writer_required",
        "role_assignment_durable_schema_missing",
        "role_assignment_durable_schema_stale",
        "agent_session_closed",
        "agent_session_identity_conflict",
        "agent_session_stale",
        "coordination_plan_top_level_authorization_verifier_invalid",
        "durable_collaboration_top_level_verifier_conflict",
    }
    return code if code in allowed else "durable_collaboration_runtime_unavailable"


def open_mcp_durable_collaboration_runtime(
    engine: Any,
    *,
    project_id: object,
    server_actor: object,
    coordination_session_id: object,
    transport_session_id: object,
    clock: Callable[[], datetime] | None = None,
    top_level_authorization_verifier: TopLevelAgentAuthorizationVerifier | None = None,
) -> DurableCollaborationRuntimeBindingResult:
    """Bind one exact authenticated MCP transport to one durable AgentSession.

    ``transport_session_id`` is deliberately required: project + actor +
    workflow scope alone identify a *collaboration scope*, not a concrete
    participant session.  The MCP server creates this opaque value from the
    SDK-owned request session and never accepts it from tool arguments.
    """

    scoped_project_id = canonical_project_id(project_id)
    actor = _server_actor(server_actor)
    scope = _workflow_scope(coordination_session_id)
    transport = _transport_session(transport_session_id)
    if not scoped_project_id:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_project_required"
        )
    if not actor:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_server_actor_unconfigured"
        )
    if not scope:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_workflow_scope_required"
        )
    if not transport:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_authenticated_transport_required"
        )
    if top_level_authorization_verifier is not None and not isinstance(
        top_level_authorization_verifier,
        TopLevelAgentAuthorizationVerifier,
    ):
        return DurableCollaborationRuntimeBindingResult(
            None,
            reason="coordination_plan_top_level_authorization_verifier_invalid",
        )
    storage = _server_storage(engine)
    if storage is None:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_server_writer_unavailable"
        )
    _storage, connection, batch, write_lock = storage
    server_clock = clock or _server_clock

    try:
        from .durable_runtime import DurableCollaborationRuntime

        transaction_factory = _transaction_factory(write_lock=write_lock, batch=batch)
        source_revision = _source_revision()
        project = ProjectScope(scoped_project_id)
        # Authority identity is process-local and exact-instance verification is
        # intentional. Serialize first construction, scope-cache updates, and
        # session registration under the canonical server writer lock so
        # concurrent first-use transports cannot observe different authority
        # graphs or race SQLite schema/lease state. Production composition uses
        # the engine's re-entrant writer lock because registration re-enters it
        # through ``transaction_factory``.
        with write_lock:
            bundle = _authority_bundle(
                engine,
                connection=connection,
                transaction_factory=transaction_factory,
                clock=server_clock,
                source_revision=source_revision,
            )
            scope_bundle = _scope_authorities(
                engine,
                bundle=bundle,
                project=project,
                coordination_session_id=scope,
                clock=server_clock,
                verifier=top_level_authorization_verifier,
            )
            runtime = DurableCollaborationRuntime(
                connection,
                transaction_factory=transaction_factory,
                clock=server_clock,
                policy_authority=bundle.policy_authority,
                role_assignment_repository=bundle.role_assignment_repository,
                role_assignment_authority=bundle.role_assignment_authority,
                acceptance_authority=bundle.acceptance_authority,
            )
            created_at = server_now_text(server_clock)
            session = AgentSession(
                session_id=_durable_session_id(
                    project_id=scoped_project_id,
                    server_actor=actor,
                    coordination_session_id=scope,
                    transport_session_id=transport,
                ),
                identity=AgentIdentity(agent_id=f"agent:{actor}", role=_SESSION_POLICY_PROFILE),
                project=project,
                coordination_session_id=scope,
                state="active",
                started_at=created_at,
                last_heartbeat_at=created_at,
                expires_at=server_now_text(
                    lambda: server_clock() + timedelta(seconds=_SESSION_TTL_SECONDS)
                ),
            )
            registered = runtime.register_session(session)
            session = registered.session
            host = DurableCollaborationHost(
                runtime=runtime,
                session=session,
                session_init_result=registered,
            )
    except Exception as exc:
        return DurableCollaborationRuntimeBindingResult(None, reason=_safe_failure(exc))

    return DurableCollaborationRuntimeBindingResult(
        runtime=runtime,
        session=session,
        host=host,
        session_init_result=registered,
        registered=True,
        role_assignment_repository=bundle.role_assignment_repository,
        role_assignment_authority=bundle.role_assignment_authority,
        activity_repository=bundle.activity_repository,
        activity_authority=bundle.activity_authority,
        acceptance_repository=bundle.acceptance_repository,
        acceptance_authority=bundle.acceptance_authority,
        coordinator_repository=bundle.coordinator_repository,
        coordinator_authority=scope_bundle.coordinator_authority,
        coordination_plan_repository=bundle.coordination_plan_repository,
        coordination_plan_authority=bundle.coordination_plan_authority,
        top_level_binding_authority=(
            scope_bundle.top_level_binding_authority
            if top_level_authorization_verifier is not None
            else None
        ),
    )


def resume_mcp_durable_collaboration_runtime(
    engine: Any,
    *,
    claims: DurableCollaborationContinuationClaims,
    clock: Callable[[], datetime] | None = None,
    top_level_authorization_verifier: TopLevelAgentAuthorizationVerifier | None = None,
) -> DurableCollaborationRuntimeBindingResult:
    """Rehydrate one signed continuation from the exact canonical session row.

    Signed claims identify the row but never recreate authority by themselves.
    The canonical SQLite row must still be active, match every identity/scope
    claim, and pass the ordinary server authority composition.  Registering
    the exact existing session is idempotent, so its cursor and leases remain
    attached and no second ``agent.joined`` event is emitted.
    """

    if not isinstance(claims, DurableCollaborationContinuationClaims):
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_continuation_claims_invalid"
        )
    project_id = canonical_project_id(claims.project_id)
    actor = _server_actor(claims.server_actor)
    scope = _workflow_scope(claims.flow_scope_id)
    if (
        not project_id
        or not actor
        or not scope
        or claims.agent_id != f"agent:{actor}"
        or claims.role != _SESSION_POLICY_PROFILE
    ):
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_continuation_binding_invalid"
        )
    if top_level_authorization_verifier is not None and not isinstance(
        top_level_authorization_verifier,
        TopLevelAgentAuthorizationVerifier,
    ):
        return DurableCollaborationRuntimeBindingResult(
            None,
            reason="coordination_plan_top_level_authorization_verifier_invalid",
        )
    storage = _server_storage(engine)
    if storage is None:
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_server_writer_unavailable"
        )
    _storage, connection, batch, write_lock = storage
    server_clock = clock or _server_clock

    try:
        from .durable_runtime import DurableCollaborationError, DurableCollaborationRuntime

        transaction_factory = _transaction_factory(write_lock=write_lock, batch=batch)
        source_revision = _source_revision()
        project = ProjectScope(project_id)
        with write_lock:
            bundle = _authority_bundle(
                engine,
                connection=connection,
                transaction_factory=transaction_factory,
                clock=server_clock,
                source_revision=source_revision,
            )
            scope_bundle = _scope_authorities(
                engine,
                bundle=bundle,
                project=project,
                coordination_session_id=scope,
                clock=server_clock,
                verifier=top_level_authorization_verifier,
            )
            runtime = DurableCollaborationRuntime(
                connection,
                transaction_factory=transaction_factory,
                clock=server_clock,
                policy_authority=bundle.policy_authority,
                role_assignment_repository=bundle.role_assignment_repository,
                role_assignment_authority=bundle.role_assignment_authority,
                acceptance_authority=bundle.acceptance_authority,
            )
            row = runtime._fetchone(  # noqa: SLF001 - canonical resume composition seam
                "SELECT session_json,session_sha256,identity_json,project_id,agent_id,"
                "coordination_session_id,state,expires_at "
                "FROM collaboration_agent_sessions "
                "WHERE session_id=?",
                (claims.durable_session_id,),
            )
            if row is None or str(row.get("state") or "") not in {"active", "idle"}:
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_inactive"
                )
            stored = json.loads(str(row.get("session_json") or ""))
            stored_identity_projection = json.loads(str(row.get("identity_json") or ""))
            if not isinstance(stored, Mapping) or not isinstance(
                stored_identity_projection,
                Mapping,
            ):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_corrupt"
                )
            identity_value = stored.get("identity") if isinstance(stored, Mapping) else None
            if not isinstance(identity_value, Mapping) or dict(identity_value) != dict(
                stored_identity_projection
            ):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_corrupt"
                )
            identity = AgentIdentity(
                agent_id=str(identity_value.get("agent_id") or ""),
                role=str(identity_value.get("role") or ""),
                parent_agent_id=(
                    None
                    if identity_value.get("parent_agent_id") is None
                    else str(identity_value.get("parent_agent_id") or "")
                ),
                capabilities=tuple(identity_value.get("capabilities") or ()),
            )
            session = AgentSession(
                session_id=str(stored.get("session_id") or ""),
                identity=identity,
                project=ProjectScope(str(stored.get("project_id") or "")),
                coordination_session_id=str(stored.get("coordination_session_id") or ""),
                state=str(stored.get("state") or ""),
                started_at=str(stored.get("started_at") or ""),
                last_heartbeat_at=str(stored.get("last_heartbeat_at") or ""),
                expires_at=(
                    None if stored.get("expires_at") is None else str(stored.get("expires_at") or "")
                ),
            )
            if not hmac.compare_digest(
                session.content_sha256,
                str(row.get("session_sha256") or ""),
            ):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_corrupt"
                )
            if (
                session.session_id != claims.durable_session_id
                or session.project.project_id != project_id
                or session.coordination_session_id != scope
                or str(row.get("project_id") or "") != project_id
                or str(row.get("coordination_session_id") or "") != scope
                or str(row.get("agent_id") or "") != claims.agent_id
                or identity.agent_id != claims.agent_id
                or identity.role != claims.role
            ):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_binding_invalid"
                )
            expires_at = str(row.get("expires_at") or "")
            now_text = server_now_text(server_clock)
            if not expires_at or parse_utc(expires_at) <= parse_utc(now_text):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_expired"
                )
            if session.expires_at != expires_at:
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_session_corrupt"
                )
            if claims.expires_at_epoch > int(parse_utc(expires_at).timestamp()):
                return DurableCollaborationRuntimeBindingResult(
                    None, reason="durable_collaboration_continuation_expiry_conflict"
                )
            registered = runtime.register_session(session)
            session = registered.session
            host = DurableCollaborationHost(
                runtime=runtime,
                session=session,
                session_init_result=registered,
            )
    except DurableCollaborationError as exc:
        return DurableCollaborationRuntimeBindingResult(None, reason=_safe_failure(exc))
    except (json.JSONDecodeError, TypeError, ValueError):
        return DurableCollaborationRuntimeBindingResult(
            None, reason="durable_collaboration_continuation_binding_invalid"
        )
    except Exception as exc:
        return DurableCollaborationRuntimeBindingResult(None, reason=_safe_failure(exc))

    return DurableCollaborationRuntimeBindingResult(
        runtime=runtime,
        session=session,
        host=host,
        session_init_result=registered,
        registered=True,
        role_assignment_repository=bundle.role_assignment_repository,
        role_assignment_authority=bundle.role_assignment_authority,
        activity_repository=bundle.activity_repository,
        activity_authority=bundle.activity_authority,
        acceptance_repository=bundle.acceptance_repository,
        acceptance_authority=bundle.acceptance_authority,
        coordinator_repository=bundle.coordinator_repository,
        coordinator_authority=scope_bundle.coordinator_authority,
        coordination_plan_repository=bundle.coordination_plan_repository,
        coordination_plan_authority=bundle.coordination_plan_authority,
        top_level_binding_authority=(
            scope_bundle.top_level_binding_authority
            if top_level_authorization_verifier is not None
            else None
        ),
    )


__all__ = [
    "DurableCollaborationContinuationAuthority",
    "DurableCollaborationContinuationClaims",
    "DurableCollaborationContinuationResult",
    "DurableCollaborationHost",
    "DurableCollaborationRuntimeBindingResult",
    "open_mcp_durable_collaboration_runtime",
    "resume_mcp_durable_collaboration_runtime",
]
