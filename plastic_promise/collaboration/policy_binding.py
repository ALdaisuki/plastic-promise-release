"""Server-issued least-privilege bindings for active collaboration Agents.

``AgentIdentity`` and its role are public declarations, never authority.  This
module adds the narrow server seam that binds the current role policy to one
active ``AgentSession`` and one project/coordination-session scope.  Bindings
are public, digest-bound receipts: they contain no bearer token, credential,
private key, or caller-supplied capability.

The authority keeps the issuance fact inside the server process.  A binding
therefore remains useful for audit and transport, but copying or constructing
one cannot manufacture authority.  A server restart deliberately requires a
fresh short-lived binding from a newly registered Agent session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from plastic_promise.collaboration.contracts import AgentSession, ProjectScope
from plastic_promise.core.agent_tool_policy import (
    authorize_agent_mcp_call,
    authorize_external_capability,
    policy_for_role,
)

AGENT_POLICY_BINDING_SCHEMA = "agent-policy-binding/v1"
AGENT_POLICY_DECISION_SCHEMA = "agent-policy-decision/v1"
AGENT_POLICY_BINDING_ISSUER = "pp-server-backend"
# The policy contract is server-owned.  Keep its revision here rather than
# accepting a caller-selected string at an issuance boundary; changing it is
# an explicit policy rollout that must update the role-policy implementation
# and its evidence together.
AGENT_POLICY_REVISION = "agent-role-policy/v1"
# Acceptance is a separate policy plane, but it uses the same server-issued
# binding primitive.  It therefore has its own explicit revision rather than
# widening the authority boundary to arbitrary caller strings.
ACCEPTANCE_REVIEW_POLICY_REVISION = "acceptance-review-policy/v1"
SERVER_POLICY_REVISIONS = frozenset({AGENT_POLICY_REVISION, ACCEPTANCE_REVIEW_POLICY_REVISION})
DEFAULT_AGENT_POLICY_BINDING_TTL_SECONDS = 300
MAX_AGENT_POLICY_BINDING_TTL_SECONDS = 3600

_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_ROLE = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SERVER_AUTHORITY_TOKEN = object()


class AgentPolicyBindingError(ValueError):
    """Stable, non-sensitive error raised while issuing a policy binding."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AgentPolicyBinding:
    """A public receipt binding one role policy to one active Agent session.

    The receipt is intentionally not a bearer capability.  Authorization also
    requires the issuing server authority, the current registered
    ``AgentSession``, and the trusted request scope.
    """

    binding_id: str
    agent_session_id: str
    agent_id: str
    role: str
    project_id: str
    coordination_session_id: str
    policy_revision: str
    policy_digest: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "binding_id",
            _identifier(self.binding_id, "agent_policy_binding_id_invalid"),
        )
        object.__setattr__(
            self,
            "agent_session_id",
            _identifier(self.agent_session_id, "agent_policy_agent_session_id_invalid"),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "agent_policy_agent_id_invalid"),
        )
        role = str(self.role or "").strip().casefold()
        if _SAFE_ROLE.fullmatch(role) is None:
            raise AgentPolicyBindingError("agent_policy_role_invalid")
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self,
            "project_id",
            _project_id(self.project_id),
        )
        object.__setattr__(
            self,
            "coordination_session_id",
            _identifier(
                self.coordination_session_id,
                "agent_policy_coordination_session_id_invalid",
            ),
        )
        object.__setattr__(
            self,
            "policy_revision",
            _identifier(self.policy_revision, "agent_policy_revision_invalid"),
        )
        object.__setattr__(
            self,
            "policy_digest",
            _digest(self.policy_digest, "agent_policy_digest_invalid"),
        )
        issued_at = _timestamp(self.issued_at, "agent_policy_issued_at_invalid")
        expires_at = _timestamp(self.expires_at, "agent_policy_expires_at_invalid")
        if _parse_timestamp(expires_at) <= _parse_timestamp(issued_at):
            raise AgentPolicyBindingError("agent_policy_expiry_invalid")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    @property
    def binding_digest(self) -> str:
        """Digest of every public field that affects authorization."""

        return _sha256(self._payload())

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": AGENT_POLICY_BINDING_SCHEMA,
            "issuer": AGENT_POLICY_BINDING_ISSUER,
            "binding_id": self.binding_id,
            "agent_session_id": self.agent_session_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "project_id": self.project_id,
            "coordination_session_id": self.coordination_session_id,
            "policy_revision": self.policy_revision,
            "policy_digest": self.policy_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, object]:
        projection = self._payload()
        projection["binding_digest"] = self.binding_digest
        return projection


@dataclass(frozen=True, slots=True)
class AgentPolicyDecision:
    """Bounded authorization result safe for audit and collaboration events."""

    allowed: bool
    reason: str
    target_kind: str
    target: str
    agent_session_id: str
    agent_id: str
    role: str
    project_id: str
    coordination_session_id: str
    policy_revision: str
    policy_digest: str
    binding_id: str = ""
    binding_digest: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": AGENT_POLICY_DECISION_SCHEMA,
            "allowed": self.allowed,
            "reason": self.reason,
            "target_kind": self.target_kind,
            "target": self.target,
            "agent_session_id": self.agent_session_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "project_id": self.project_id,
            "coordination_session_id": self.coordination_session_id,
            "policy_revision": self.policy_revision,
            "policy_digest": self.policy_digest,
            "binding_id": self.binding_id,
            "binding_digest": self.binding_digest,
        }


class AgentPolicyBindingAuthority:
    """Server-only issuer and authorization seam for collaboration Agents."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise AgentPolicyBindingError("agent_policy_server_authority_required")
        self._clock = clock
        self._issued_binding_digests: set[str] = set()

    def issue(
        self,
        session: AgentSession,
        *,
        policy_revision: str = AGENT_POLICY_REVISION,
        binding_id: str | None = None,
        ttl_seconds: int = DEFAULT_AGENT_POLICY_BINDING_TTL_SECONDS,
    ) -> AgentPolicyBinding:
        """Issue one short-lived binding from a server-owned active session."""

        now = self._now()
        self._require_active_session(session, now)
        policy = policy_for_role(session.identity.role)
        if policy is None:
            raise AgentPolicyBindingError("agent_policy_unknown_role")
        revision = _identifier(policy_revision, "agent_policy_revision_invalid")
        if revision not in SERVER_POLICY_REVISIONS:
            # Even though this method is only reachable through the server
            # authority token, rejecting arbitrary revisions here prevents a
            # future adapter from accidentally turning a user-controlled
            # policy string into an authority claim.
            raise AgentPolicyBindingError("agent_policy_revision_not_server_current")
        ttl = _ttl_seconds(ttl_seconds)
        expires_at = now + timedelta(seconds=ttl)
        session_expiry = _optional_timestamp_value(session.expires_at)
        if session_expiry is not None:
            expires_at = min(expires_at, session_expiry)
        if expires_at <= now:
            raise AgentPolicyBindingError("agent_session_expired")
        binding = AgentPolicyBinding(
            binding_id=binding_id or f"apb:{uuid.uuid4().hex}",
            agent_session_id=session.session_id,
            agent_id=session.identity.agent_id,
            role=policy.role,
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
            policy_revision=revision,
            policy_digest=policy.digest,
            issued_at=_utc_text(now),
            expires_at=_utc_text(expires_at),
        )
        self._issued_binding_digests.add(binding.binding_digest)
        return binding

    def authorize_mcp(
        self,
        session: AgentSession,
        binding: AgentPolicyBinding | None,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        policy_revision: str,
        tool_name: object,
        arguments: Mapping[str, Any] | None = None,
    ) -> AgentPolicyDecision:
        """Authorize one MCP call after validating the complete binding context."""

        target = str(tool_name or "").strip()
        denied = self._validate_binding(
            session,
            binding,
            project=project,
            coordination_session_id=coordination_session_id,
            policy_revision=policy_revision,
            target_kind="mcp-tool",
            target=target,
        )
        if denied is not None:
            return denied
        assert binding is not None
        core = authorize_agent_mcp_call(
            binding.role,
            target,
            dict(arguments or {}),
        )
        return self._decision(
            bool(core["allowed"]),
            str(core["reason"]),
            session,
            binding,
            policy_revision=policy_revision,
            target_kind="mcp-tool",
            target=target,
        )

    def authorize_external(
        self,
        session: AgentSession,
        binding: AgentPolicyBinding | None,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        policy_revision: str,
        capability: object,
    ) -> AgentPolicyDecision:
        """Authorize one non-MCP capability through the same scoped receipt."""

        target = str(capability or "").strip().casefold()
        denied = self._validate_binding(
            session,
            binding,
            project=project,
            coordination_session_id=coordination_session_id,
            policy_revision=policy_revision,
            target_kind="external-capability",
            target=target,
        )
        if denied is not None:
            return denied
        assert binding is not None
        core = authorize_external_capability(binding.role, target)
        return self._decision(
            bool(core["allowed"]),
            str(core["reason"]),
            session,
            binding,
            policy_revision=policy_revision,
            target_kind="external-capability",
            target=target,
        )

    def _validate_binding(
        self,
        session: AgentSession,
        binding: AgentPolicyBinding | None,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        policy_revision: str,
        target_kind: str,
        target: str,
    ) -> AgentPolicyDecision | None:
        revision = _decision_identifier(policy_revision)
        coordination_id = _decision_identifier(coordination_session_id)
        if not isinstance(session, AgentSession):
            return _empty_decision(
                "agent_policy_agent_session_required",
                target_kind=target_kind,
                target=target,
                project=project,
                coordination_session_id=coordination_id,
                policy_revision=revision,
            )
        if not isinstance(project, ProjectScope):
            return _empty_decision(
                "agent_policy_project_scope_required",
                target_kind=target_kind,
                target=target,
                project=None,
                coordination_session_id=coordination_id,
                policy_revision=revision,
                session=session,
            )
        if binding is None or not isinstance(binding, AgentPolicyBinding):
            return self._decision(
                False,
                "agent_policy_binding_required",
                session,
                None,
                policy_revision=revision,
                target_kind=target_kind,
                target=target,
            )
        if binding.binding_digest not in self._issued_binding_digests:
            return self._decision(
                False,
                "agent_policy_binding_not_server_issued",
                session,
                binding,
                policy_revision=revision,
                target_kind=target_kind,
                target=target,
            )
        now = self._now()
        inactive_reason = self._inactive_session_reason(session, now)
        if inactive_reason is not None:
            return self._decision(
                False,
                inactive_reason,
                session,
                binding,
                policy_revision=revision,
                target_kind=target_kind,
                target=target,
            )
        if binding.agent_session_id != session.session_id:
            reason = "agent_policy_binding_session_mismatch"
        elif binding.agent_id != session.identity.agent_id:
            reason = "agent_policy_binding_agent_mismatch"
        elif binding.role != session.identity.role:
            reason = "agent_policy_binding_role_mismatch"
        elif (
            binding.project_id != session.project.project_id
            or binding.project_id != project.project_id
        ):
            reason = "agent_policy_binding_project_mismatch"
        elif (
            binding.coordination_session_id != session.coordination_session_id
            or binding.coordination_session_id != coordination_id
        ):
            reason = "agent_policy_binding_coordination_session_mismatch"
        elif _parse_timestamp(binding.expires_at) <= now:
            reason = "agent_policy_binding_expired"
        elif binding.policy_revision != revision:
            reason = "agent_policy_revision_mismatch"
        else:
            policy = policy_for_role(session.identity.role)
            if policy is None:
                reason = "agent_policy_unknown_role"
            elif not hmac.compare_digest(binding.policy_digest, policy.digest):
                reason = "agent_policy_digest_mismatch"
            else:
                return None
        return self._decision(
            False,
            reason,
            session,
            binding,
            policy_revision=revision,
            target_kind=target_kind,
            target=target,
        )

    def _decision(
        self,
        allowed: bool,
        reason: str,
        session: AgentSession,
        binding: AgentPolicyBinding | None,
        *,
        policy_revision: str,
        target_kind: str,
        target: str,
    ) -> AgentPolicyDecision:
        policy = policy_for_role(session.identity.role)
        return AgentPolicyDecision(
            allowed=allowed,
            reason=reason,
            target_kind=target_kind,
            target=target,
            agent_session_id=session.session_id,
            agent_id=session.identity.agent_id,
            role=session.identity.role,
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
            policy_revision=policy_revision,
            policy_digest=policy.digest if policy is not None else "",
            binding_id=binding.binding_id if binding is not None else "",
            binding_digest=binding.binding_digest if binding is not None else "",
        )

    def _require_active_session(self, session: AgentSession, now: datetime) -> None:
        if not isinstance(session, AgentSession):
            raise AgentPolicyBindingError("agent_policy_agent_session_required")
        reason = self._inactive_session_reason(session, now)
        if reason is not None:
            raise AgentPolicyBindingError(reason)

    @staticmethod
    def _inactive_session_reason(session: AgentSession, now: datetime) -> str | None:
        if session.state != "active":
            return "agent_session_not_active"
        if _parse_timestamp(session.started_at) > now:
            return "agent_session_not_started"
        if _parse_timestamp(session.last_heartbeat_at) > now:
            return "agent_session_heartbeat_in_future"
        expires_at = _optional_timestamp_value(session.expires_at)
        if expires_at is not None and expires_at <= now:
            return "agent_session_expired"
        return None

    def _now(self) -> datetime:
        return _utc_datetime(self._clock(), "agent_policy_clock_invalid")


def open_server_agent_policy_binding_authority(
    *, clock: Callable[[], datetime] | None = None
) -> AgentPolicyBindingAuthority:
    """Open the binding issuer inside the canonical server runtime only."""

    return AgentPolicyBindingAuthority(
        clock=clock or (lambda: datetime.now(timezone.utc)),
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


def _empty_decision(
    reason: str,
    *,
    target_kind: str,
    target: str,
    project: ProjectScope | None,
    coordination_session_id: str,
    policy_revision: str,
    session: AgentSession | None = None,
) -> AgentPolicyDecision:
    return AgentPolicyDecision(
        allowed=False,
        reason=reason,
        target_kind=target_kind,
        target=target,
        agent_session_id=session.session_id if session is not None else "",
        agent_id=session.identity.agent_id if session is not None else "",
        role=session.identity.role if session is not None else "",
        project_id=(
            session.project.project_id
            if session is not None
            else project.project_id
            if project is not None
            else ""
        ),
        coordination_session_id=coordination_session_id,
        policy_revision=policy_revision,
        policy_digest="",
    )


def _identifier(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(text) is None:
        raise AgentPolicyBindingError(code)
    return text


def _decision_identifier(value: object) -> str:
    text = str(value or "").strip()
    return text if _SAFE_IDENTIFIER.fullmatch(text) is not None else ""


def _project_id(value: object) -> str:
    try:
        return ProjectScope(str(value or "")).project_id
    except (TypeError, ValueError) as exc:
        raise AgentPolicyBindingError("agent_policy_project_id_invalid") from exc


def _digest(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise AgentPolicyBindingError(code)
    return text


def _sha256(value: object) -> str:
    material = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise AgentPolicyBindingError(code)
    try:
        return _utc_text(_parse_timestamp(value))
    except (TypeError, ValueError) as exc:
        raise AgentPolicyBindingError(code) from exc


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc_datetime(parsed, "agent_policy_timestamp_invalid")


def _optional_timestamp_value(value: str | None) -> datetime | None:
    return None if value is None else _parse_timestamp(value)


def _utc_datetime(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AgentPolicyBindingError(code)
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        _utc_datetime(value, "agent_policy_timestamp_invalid")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _ttl_seconds(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_AGENT_POLICY_BINDING_TTL_SECONDS
    ):
        raise AgentPolicyBindingError("agent_policy_binding_ttl_invalid")
    return value


__all__ = [
    "AGENT_POLICY_BINDING_ISSUER",
    "AGENT_POLICY_BINDING_SCHEMA",
    "AGENT_POLICY_DECISION_SCHEMA",
    "AGENT_POLICY_REVISION",
    "ACCEPTANCE_REVIEW_POLICY_REVISION",
    "AgentPolicyBinding",
    "AgentPolicyBindingAuthority",
    "AgentPolicyBindingError",
    "AgentPolicyDecision",
    "DEFAULT_AGENT_POLICY_BINDING_TTL_SECONDS",
    "MAX_AGENT_POLICY_BINDING_TTL_SECONDS",
    "SERVER_POLICY_REVISIONS",
    "open_server_agent_policy_binding_authority",
]
