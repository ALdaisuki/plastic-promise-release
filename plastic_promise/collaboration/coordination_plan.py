"""Finite, user-accountable plans for bounded multi-Agent delegation.

The Top-Level Agent chooses the concrete responsibilities, Agent count, and
resource allocation for one coordination session.  The platform validates
that choice; it does not impose a product-wide hierarchy or silently grow one.

These values are immutable public contracts, not bearer authority.  A server
runtime must still bind an active plan revision to the authenticated
Top-Level Agent session before dispatching work.  In particular, a child
Agent cannot activate a new plan merely by constructing equivalent JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .canonical_time import canonical_text, parse_utc, server_now
from .contracts import AgentSession, ProjectScope

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


USER_MANDATE_SCHEMA = "collaboration-user-mandate/v1"
RESOURCE_ALLOCATION_SCHEMA = "collaboration-resource-allocation/v1"
RESPONSIBILITY_NODE_SCHEMA = "collaboration-responsibility-node/v1"
DELEGATION_EDGE_SCHEMA = "collaboration-delegation-edge/v1"
COORDINATION_PLAN_SCHEMA = "collaboration-coordination-plan/v1"
DELEGATION_ENVELOPE_SCHEMA = "collaboration-delegation-envelope/v1"
RESOURCE_USAGE_RECEIPT_SCHEMA = "collaboration-resource-usage-receipt/v1"
COORDINATION_PLAN_ACTIVATION_SCHEMA = "collaboration-plan-activation/v1"
TOP_LEVEL_AGENT_BINDING_SCHEMA = "collaboration-top-level-agent-binding/v1"
COORDINATION_PLAN_ISSUER = "pp-server-backend"

TOKEN_AUTHORITY_PROVIDER = "provider-authoritative"
TOKEN_AUTHORITY_UNAVAILABLE = "unavailable"
TOKEN_MEASUREMENT_AGENT_ESTIMATE = "agent-estimate"
TOKEN_MEASUREMENT_UNAVAILABLE = "unavailable"
TOKEN_AUTHORITIES = frozenset(
    {
        TOKEN_AUTHORITY_PROVIDER,
        TOKEN_AUTHORITY_UNAVAILABLE,
    }
)
TOKEN_MEASUREMENTS = frozenset(
    {
        TOKEN_AUTHORITY_PROVIDER,
        TOKEN_MEASUREMENT_AGENT_ESTIMATE,
        TOKEN_MEASUREMENT_UNAVAILABLE,
    }
)

_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_ROLE = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_SAFE_TOOL = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:")
_MAX_PUBLIC_TEXT_BYTES = 8 * 1024
_MAX_PLAN_BYTES = 1024 * 1024
_MAX_INTEGER = (1 << 63) - 1
_SERVER_AUTHORITY_TOKEN = object()
_VERIFICATION_TOKEN = object()
_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk|rk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"-----BEGIN [^-\r\n]{0,48}PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[-_ ]?key|password|private[-_ ]?key|access[-_ ]?token|"
        r"refresh[-_ ]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)
_PRIVATE_REASONING_PATTERNS = (
    re.compile(r"\bchain[ _-]?of[ _-]?thought\b", re.IGNORECASE),
    re.compile(r"\b(?:hidden|private)[ _-]?(?:reasoning|thoughts?)\b", re.IGNORECASE),
    re.compile(r"\binternal[ _-]?monologue\b", re.IGNORECASE),
    re.compile(r"\breasoning[ _-]?(?:trace|transcript)\b", re.IGNORECASE),
    re.compile(r"\b(?:raw|full)[ _-]?prompt\b", re.IGNORECASE),
    re.compile(r"\bscratchpad\b", re.IGNORECASE),
)


class CoordinationPlanError(ValueError):
    """Stable, non-sensitive refusal from a coordination-plan contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _PlanJsonContract:
    def to_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def content_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class CoordinationPlanActivation(_PlanJsonContract):
    """Immutable server issuance fact for one current plan revision."""

    activation_id: str
    plan_id: str
    plan_revision: int
    plan_sha256: str
    mandate_sha256: str
    project: ProjectScope
    coordination_session_id: str
    top_level_agent_session_id: str
    issued_at_utc: str
    expires_at_utc: str
    supersedes_activation_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activation_id",
            _identifier(self.activation_id, "coordination_plan_activation_id_invalid"),
        )
        object.__setattr__(
            self,
            "plan_id",
            _identifier(self.plan_id, "coordination_plan_activation_plan_id_invalid"),
        )
        object.__setattr__(
            self,
            "plan_revision",
            _positive_integer(
                self.plan_revision,
                "coordination_plan_activation_revision_invalid",
            ),
        )
        object.__setattr__(
            self,
            "plan_sha256",
            _digest(self.plan_sha256, "coordination_plan_activation_digest_invalid"),
        )
        object.__setattr__(
            self,
            "mandate_sha256",
            _digest(
                self.mandate_sha256,
                "coordination_plan_activation_mandate_digest_invalid",
            ),
        )
        if not isinstance(self.project, ProjectScope):
            raise CoordinationPlanError("coordination_plan_activation_project_invalid")
        object.__setattr__(
            self,
            "coordination_session_id",
            _identifier(
                self.coordination_session_id,
                "coordination_plan_activation_session_invalid",
            ),
        )
        object.__setattr__(
            self,
            "top_level_agent_session_id",
            _identifier(
                self.top_level_agent_session_id,
                "coordination_plan_activation_top_level_invalid",
            ),
        )
        issued_at = _timestamp(
            self.issued_at_utc,
            "coordination_plan_activation_issued_at_invalid",
        )
        expires_at = _timestamp(
            self.expires_at_utc,
            "coordination_plan_activation_expires_at_invalid",
        )
        if parse_utc(expires_at) <= parse_utc(issued_at):
            raise CoordinationPlanError("coordination_plan_activation_expiry_invalid")
        supersedes = ""
        if self.plan_revision == 1:
            if self.supersedes_activation_sha256:
                raise CoordinationPlanError(
                    "coordination_plan_initial_activation_supersedes_forbidden"
                )
        else:
            supersedes = _digest(
                self.supersedes_activation_sha256,
                "coordination_plan_activation_supersedes_required",
            )
        object.__setattr__(self, "issued_at_utc", issued_at)
        object.__setattr__(self, "expires_at_utc", expires_at)
        object.__setattr__(self, "supersedes_activation_sha256", supersedes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COORDINATION_PLAN_ACTIVATION_SCHEMA,
            "issuer": COORDINATION_PLAN_ISSUER,
            "activation_id": self.activation_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_sha256": self.plan_sha256,
            "mandate_sha256": self.mandate_sha256,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "top_level_agent_session_id": self.top_level_agent_session_id,
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "supersedes_activation_sha256": self.supersedes_activation_sha256,
            "authority_effect": "server-repository-required",
        }

    @property
    def activation_sha256(self) -> str:
        return self.content_sha256


@dataclass(frozen=True, slots=True)
class TopLevelAgentBinding(_PlanJsonContract):
    """Public audit fact for a server-recognised Top-Level Agent.

    This value is deliberately not bearer authority.  A caller must resolve
    the current binding through the server repository; replaying equivalent
    JSON cannot grant Top-Level privileges.
    """

    project: ProjectScope
    coordination_session_id: str
    top_level_agent_session_id: str
    top_level_agent_id: str
    agent_session_sha256: str
    mandate_sha256: str
    binding_generation: int
    bound_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise CoordinationPlanError("coordination_plan_top_level_binding_project_invalid")
        object.__setattr__(
            self,
            "coordination_session_id",
            _identifier(
                self.coordination_session_id,
                "coordination_plan_top_level_binding_scope_invalid",
            ),
        )
        object.__setattr__(
            self,
            "top_level_agent_session_id",
            _identifier(
                self.top_level_agent_session_id,
                "coordination_plan_top_level_binding_session_invalid",
            ),
        )
        object.__setattr__(
            self,
            "top_level_agent_id",
            _identifier(
                self.top_level_agent_id,
                "coordination_plan_top_level_binding_agent_invalid",
            ),
        )
        object.__setattr__(
            self,
            "agent_session_sha256",
            _digest(
                self.agent_session_sha256,
                "coordination_plan_top_level_binding_registration_invalid",
            ),
        )
        object.__setattr__(
            self,
            "mandate_sha256",
            _digest(
                self.mandate_sha256,
                "coordination_plan_top_level_binding_mandate_invalid",
            ),
        )
        object.__setattr__(
            self,
            "binding_generation",
            _positive_integer(
                self.binding_generation,
                "coordination_plan_top_level_binding_generation_invalid",
            ),
        )
        object.__setattr__(
            self,
            "bound_at_utc",
            _timestamp(
                self.bound_at_utc,
                "coordination_plan_top_level_binding_time_invalid",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": TOP_LEVEL_AGENT_BINDING_SCHEMA,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "top_level_agent_session_id": self.top_level_agent_session_id,
            "top_level_agent_id": self.top_level_agent_id,
            "agent_session_sha256": self.agent_session_sha256,
            "mandate_sha256": self.mandate_sha256,
            "binding_generation": self.binding_generation,
            "bound_at_utc": self.bound_at_utc,
            "authority_effect": "server-repository-required",
        }

    @property
    def binding_sha256(self) -> str:
        return self.content_sha256


@dataclass(frozen=True, slots=True, init=False)
class VerifiedCoordinationPlan:
    """Process-local proof that one exact plan is current and server issued."""

    _plan: CoordinationPlan
    _activation: CoordinationPlanActivation
    _token: object

    def __init__(
        self,
        plan: CoordinationPlan,
        activation: CoordinationPlanActivation,
        *,
        _verification_token: object | None = None,
    ) -> None:
        if _verification_token is not _VERIFICATION_TOKEN:
            raise CoordinationPlanError("coordination_plan_verified_authority_required")
        if not isinstance(plan, CoordinationPlan) or not isinstance(
            activation,
            CoordinationPlanActivation,
        ):
            raise CoordinationPlanError("coordination_plan_verification_invalid")
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "_activation", activation)
        object.__setattr__(self, "_token", _verification_token)

    @property
    def plan(self) -> CoordinationPlan:
        return self._plan

    @property
    def activation(self) -> CoordinationPlanActivation:
        return self._activation


@runtime_checkable
class CoordinationPlanRepository(Protocol):
    """Server-owned persistence seam for plans, activations, and usage."""

    def load_plan_by_digest(self, plan_sha256: str) -> CoordinationPlan | None: ...

    def load_activation_by_digest(
        self,
        activation_sha256: str,
    ) -> CoordinationPlanActivation | None: ...

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation] | None: ...

    def resolve_registered_session(self, agent_session_id: str) -> AgentSession: ...

    def append_top_level_binding(
        self,
        mandate: UserMandate,
        *,
        agent_session_id: str,
        expected_binding_generation: int,
        _authority_token: object | None = None,
    ) -> TopLevelAgentBinding: ...

    def load_top_level_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> TopLevelAgentBinding | None: ...

    def require_top_level_session(self, plan: CoordinationPlan) -> None: ...

    def append_and_activate(
        self,
        plan: CoordinationPlan,
        activation: CoordinationPlanActivation,
        *,
        expected_current_activation_sha256: str,
        _authority_token: object | None = None,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation]: ...

    def append_usage(
        self,
        receipt: ResourceUsageReceipt,
        *,
        _authority_token: object | None = None,
    ) -> ResourceUsageReceipt: ...

    def load_usage_by_id(self, receipt_id: str) -> ResourceUsageReceipt | None: ...

    def total_provider_token_usage(
        self,
        *,
        plan_sha256: str,
        responsibility_node_id: str,
    ) -> int: ...


@runtime_checkable
class TopLevelAgentAuthorizationVerifier(Protocol):
    """Server composition seam for trusted user-authorization verification."""

    def authorize(
        self,
        *,
        mandate: UserMandate,
        session: AgentSession,
    ) -> None: ...


class TopLevelAgentBindingAuthority:
    """Combine trusted user authorization with a registered server session."""

    def __init__(
        self,
        *,
        repository: CoordinationPlanRepository,
        authorization_verifier: TopLevelAgentAuthorizationVerifier,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, CoordinationPlanRepository):
            raise CoordinationPlanError("coordination_plan_repository_invalid")
        if not isinstance(authorization_verifier, TopLevelAgentAuthorizationVerifier):
            raise CoordinationPlanError(
                "coordination_plan_top_level_authorization_verifier_invalid"
            )
        if clock is not None and not callable(clock):
            raise CoordinationPlanError("coordination_plan_clock_invalid")
        self._repository = repository
        self._authorization_verifier = authorization_verifier
        self._clock = clock

    def bind_top_level_session(
        self,
        mandate: UserMandate,
        *,
        agent_session_id: str,
        expected_binding_generation: int = 0,
    ) -> TopLevelAgentBinding:
        """Issue the one immutable Top-Level binding for this exact scope."""

        if not isinstance(mandate, UserMandate):
            raise CoordinationPlanError("user_mandate_invalid")
        session_id = _identifier(
            agent_session_id,
            "coordination_plan_top_level_binding_session_invalid",
        )
        generation = _non_negative_integer(
            expected_binding_generation,
            "coordination_plan_top_level_binding_generation_invalid",
        )
        session = self._repository.resolve_registered_session(session_id)
        _require_session_for_mandate(
            session,
            mandate,
            now=server_now(self._clock),
        )
        try:
            result = self._authorization_verifier.authorize(
                mandate=mandate,
                session=session,
            )
        except CoordinationPlanError:
            raise
        except Exception as exc:
            raise CoordinationPlanError(
                "coordination_plan_top_level_user_authorization_rejected"
            ) from exc
        if result is not None:
            raise CoordinationPlanError("coordination_plan_top_level_authorization_result_invalid")
        return self._repository.append_top_level_binding(
            mandate,
            agent_session_id=session.session_id,
            expected_binding_generation=generation,
            _authority_token=_SERVER_AUTHORITY_TOKEN,
        )

    def bind(
        self,
        mandate: UserMandate,
        *,
        agent_session_id: str,
        expected_binding_generation: int = 0,
    ) -> TopLevelAgentBinding:
        """Compatibility spelling for the explicit bind_top_level_session seam."""

        return self.bind_top_level_session(
            mandate,
            agent_session_id=agent_session_id,
            expected_binding_generation=expected_binding_generation,
        )


class InMemoryCoordinationPlanRepository:
    """Reference server adapter for focused authority tests.

    It deliberately mirrors the durable repository interface: plans and
    activations are immutable, one scope has one CAS-updated current head,
    active Agent sessions are resolved by the repository, and usage receipts
    are idempotent.  It is not restart authority.
    """

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        if not callable(clock):
            raise CoordinationPlanError("coordination_plan_clock_invalid")
        self._clock = clock
        self._sessions: dict[str, AgentSession] = {}
        self._top_level_sessions: dict[tuple[str, str], TopLevelAgentBinding] = {}
        self._plans: dict[str, CoordinationPlan] = {}
        self._activations: dict[str, CoordinationPlanActivation] = {}
        self._current: dict[tuple[str, str], str] = {}
        self._usage_by_id: dict[str, ResourceUsageReceipt] = {}
        self._usage_by_digest: dict[str, ResourceUsageReceipt] = {}

    def register_session(self, session: AgentSession) -> None:
        if not isinstance(session, AgentSession):
            raise CoordinationPlanError("coordination_plan_agent_session_invalid")
        existing = self._sessions.get(session.session_id)
        if existing is not None and existing != session:
            raise CoordinationPlanError("coordination_plan_agent_session_conflict")
        self._sessions[session.session_id] = session

    def resolve_registered_session(self, agent_session_id: str) -> AgentSession:
        session_id = _identifier(
            agent_session_id,
            "coordination_plan_top_level_binding_session_invalid",
        )
        session = self._sessions.get(session_id)
        if session is None:
            raise CoordinationPlanError("coordination_plan_top_level_session_missing")
        return session

    def append_top_level_binding(
        self,
        mandate: UserMandate,
        *,
        agent_session_id: str,
        expected_binding_generation: int,
        _authority_token: object | None = None,
    ) -> TopLevelAgentBinding:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError(
                "coordination_plan_top_level_binding_write_authority_required"
            )
        if not isinstance(mandate, UserMandate):
            raise CoordinationPlanError("user_mandate_invalid")
        expected = _non_negative_integer(
            expected_binding_generation,
            "coordination_plan_top_level_binding_generation_invalid",
        )
        session = self.resolve_registered_session(agent_session_id)
        _require_session_for_mandate(
            session,
            mandate,
            now=server_now(self._clock),
        )
        scope = (mandate.project.project_id, mandate.coordination_session_id)
        existing = self._top_level_sessions.get(scope)
        if existing is not None:
            if (
                existing.top_level_agent_session_id == session.session_id
                and _same_digest(existing.mandate_sha256, mandate.content_sha256)
                and _same_digest(
                    existing.agent_session_sha256,
                    session.content_sha256,
                )
            ):
                if expected not in {0, existing.binding_generation}:
                    raise CoordinationPlanError(
                        "coordination_plan_top_level_binding_generation_conflict"
                    )
                return existing
            raise CoordinationPlanError("coordination_plan_top_level_binding_conflict")
        if expected != 0:
            raise CoordinationPlanError("coordination_plan_top_level_binding_generation_conflict")
        binding = TopLevelAgentBinding(
            project=mandate.project,
            coordination_session_id=mandate.coordination_session_id,
            top_level_agent_session_id=session.session_id,
            top_level_agent_id=session.identity.agent_id,
            agent_session_sha256=session.content_sha256,
            mandate_sha256=mandate.content_sha256,
            binding_generation=1,
            bound_at_utc=canonical_text(server_now(self._clock)),
        )
        self._top_level_sessions[scope] = binding
        return binding

    def load_top_level_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> TopLevelAgentBinding | None:
        project = ProjectScope(project_id)
        coordination = _identifier(
            coordination_session_id,
            "coordination_plan_session_invalid",
        )
        return self._top_level_sessions.get((project.project_id, coordination))

    def load_plan_by_digest(self, plan_sha256: str) -> CoordinationPlan | None:
        return self._plans.get(_digest(plan_sha256, "coordination_plan_digest_invalid"))

    def load_activation_by_digest(
        self,
        activation_sha256: str,
    ) -> CoordinationPlanActivation | None:
        return self._activations.get(
            _digest(
                activation_sha256,
                "coordination_plan_activation_digest_invalid",
            )
        )

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation] | None:
        project = ProjectScope(project_id)
        coordination = _identifier(
            coordination_session_id,
            "coordination_plan_session_invalid",
        )
        activation_digest = self._current.get((project.project_id, coordination))
        if activation_digest is None:
            return None
        activation = self._activations.get(activation_digest)
        if activation is None:
            raise CoordinationPlanError("coordination_plan_repository_corrupt")
        plan = self._plans.get(activation.plan_sha256)
        if plan is None:
            raise CoordinationPlanError("coordination_plan_repository_corrupt")
        _require_activation_matches_plan(plan, activation)
        return plan, activation

    def require_top_level_session(self, plan: CoordinationPlan) -> None:
        if not isinstance(plan, CoordinationPlan):
            raise CoordinationPlanError("coordination_plan_invalid")
        scope = (plan.project.project_id, plan.coordination_session_id)
        binding = self._top_level_sessions.get(scope)
        if binding is None:
            raise CoordinationPlanError("coordination_plan_top_level_binding_missing")
        if binding.top_level_agent_session_id != plan.top_level_agent_session_id:
            raise CoordinationPlanError("coordination_plan_top_level_session_mismatch")
        session = self._sessions.get(plan.top_level_agent_session_id)
        if session is None:
            raise CoordinationPlanError("coordination_plan_top_level_session_missing")
        _require_binding_matches_session_and_mandate(
            binding,
            session=session,
            mandate=plan.mandate,
        )
        _require_session_for_plan(session, plan, now=server_now(self._clock))

    def append_and_activate(
        self,
        plan: CoordinationPlan,
        activation: CoordinationPlanActivation,
        *,
        expected_current_activation_sha256: str,
        _authority_token: object | None = None,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation]:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError("coordination_plan_repository_write_authority_required")
        if not isinstance(plan, CoordinationPlan) or not isinstance(
            activation,
            CoordinationPlanActivation,
        ):
            raise CoordinationPlanError("coordination_plan_invalid")
        _require_activation_matches_plan(plan, activation)
        self.require_top_level_session(plan)
        scope = (plan.project.project_id, plan.coordination_session_id)
        current_digest = self._current.get(scope, "")
        expected = _optional_digest(
            expected_current_activation_sha256,
            "coordination_plan_expected_activation_invalid",
        )
        if current_digest != expected:
            raise CoordinationPlanError("coordination_plan_generation_conflict")
        if current_digest:
            current_activation = self._activations.get(current_digest)
            if current_activation is None:
                raise CoordinationPlanError("coordination_plan_repository_corrupt")
            previous = self._plans.get(current_activation.plan_sha256)
            if previous is None:
                raise CoordinationPlanError("coordination_plan_repository_corrupt")
            plan.validate_successor(previous)
            if activation.supersedes_activation_sha256 != current_digest:
                raise CoordinationPlanError("coordination_plan_activation_successor_mismatch")
        elif plan.plan_revision != 1 or activation.supersedes_activation_sha256:
            raise CoordinationPlanError("coordination_plan_initial_revision_invalid")

        existing_plan = self._plans.get(plan.content_sha256)
        if existing_plan is not None and existing_plan != plan:
            raise CoordinationPlanError("coordination_plan_repository_corrupt")
        for stored in self._plans.values():
            if (
                stored.plan_id == plan.plan_id
                and stored.plan_revision == plan.plan_revision
                and stored.content_sha256 != plan.content_sha256
            ):
                raise CoordinationPlanError("coordination_plan_revision_conflict")
        existing_activation = self._activations.get(activation.activation_sha256)
        if existing_activation is not None and existing_activation != activation:
            raise CoordinationPlanError("coordination_plan_repository_corrupt")
        if any(
            stored.activation_id == activation.activation_id
            and stored.activation_sha256 != activation.activation_sha256
            for stored in self._activations.values()
        ):
            raise CoordinationPlanError("coordination_plan_activation_id_conflict")
        self._plans[plan.content_sha256] = plan
        self._activations[activation.activation_sha256] = activation
        self._current[scope] = activation.activation_sha256
        return plan, activation

    def load_usage_by_id(self, receipt_id: str) -> ResourceUsageReceipt | None:
        return self._usage_by_id.get(_identifier(receipt_id, "resource_usage_receipt_id_invalid"))

    def append_usage(
        self,
        receipt: ResourceUsageReceipt,
        *,
        _authority_token: object | None = None,
    ) -> ResourceUsageReceipt:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError("resource_usage_repository_write_authority_required")
        if not isinstance(receipt, ResourceUsageReceipt):
            raise CoordinationPlanError("resource_usage_receipt_invalid")
        existing = self._usage_by_id.get(receipt.receipt_id)
        if existing is not None:
            if existing != receipt:
                raise CoordinationPlanError("resource_usage_receipt_conflict")
            return existing
        by_digest = self._usage_by_digest.get(receipt.content_sha256)
        if by_digest is not None:
            return by_digest
        plan = self._plans.get(receipt.plan_sha256)
        if plan is None:
            raise CoordinationPlanError("resource_usage_plan_not_found")
        current = self.load_current(
            project_id=plan.project.project_id,
            coordination_session_id=plan.coordination_session_id,
        )
        if current is None or current[0].content_sha256 != plan.content_sha256:
            raise CoordinationPlanError("resource_usage_plan_not_current")
        try:
            plan.node(receipt.responsibility_node_id)
        except CoordinationPlanError as exc:
            raise CoordinationPlanError("resource_usage_node_not_found") from exc
        session = self._sessions.get(receipt.agent_session_id)
        if session is None:
            raise CoordinationPlanError("resource_usage_agent_session_missing")
        _require_session_scope(session, plan, now=server_now(self._clock))
        _require_usage_within_budget(self, plan=plan, receipt=receipt)
        self._usage_by_id[receipt.receipt_id] = receipt
        self._usage_by_digest[receipt.content_sha256] = receipt
        return receipt

    def total_provider_token_usage(
        self,
        *,
        plan_sha256: str,
        responsibility_node_id: str,
    ) -> int:
        digest = _digest(plan_sha256, "resource_usage_plan_digest_invalid")
        node_id = _identifier(
            responsibility_node_id,
            "resource_usage_node_id_invalid",
        )
        return sum(
            int(receipt.token_usage or 0)
            for receipt in self._usage_by_id.values()
            if receipt.plan_sha256 == digest
            and receipt.responsibility_node_id == node_id
            and receipt.token_measurement == TOKEN_AUTHORITY_PROVIDER
        )


class CoordinationPlanAuthority:
    """Server authority for activating and consuming one finite plan head.

    The authority never infers a fixed hierarchy.  It accepts the concrete,
    finite responsibility tree selected by the authenticated Top-Level Agent,
    then enforces that exact frozen revision until the same Top-Level Agent
    activates a compare-and-swap successor.
    """

    def __init__(
        self,
        *,
        repository: CoordinationPlanRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, CoordinationPlanRepository):
            raise CoordinationPlanError("coordination_plan_repository_invalid")
        if clock is not None and not callable(clock):
            raise CoordinationPlanError("coordination_plan_clock_invalid")
        self._repository = repository
        self._clock = clock

    def activate(
        self,
        plan: CoordinationPlan,
        *,
        actor_session_id: str,
        expected_current_activation_sha256: str = "",
    ) -> VerifiedCoordinationPlan:
        """Activate an initial or successor plan as the bound Top-Level Agent."""

        if not isinstance(plan, CoordinationPlan):
            raise CoordinationPlanError("coordination_plan_invalid")
        actor = _identifier(actor_session_id, "coordination_plan_actor_session_invalid")
        if actor != plan.top_level_agent_session_id:
            raise CoordinationPlanError("coordination_plan_top_level_actor_required")
        expected = _optional_digest(
            expected_current_activation_sha256,
            "coordination_plan_expected_activation_invalid",
        )
        now = server_now(self._clock)
        _require_plan_current_window(plan, now=now)
        self._repository.require_top_level_session(plan)
        current = self._repository.load_current(
            project_id=plan.project.project_id,
            coordination_session_id=plan.coordination_session_id,
        )

        if current is not None and _same_digest(
            current[0].content_sha256,
            plan.content_sha256,
        ):
            # Exact replay of the original activation request is idempotent.
            if expected != current[1].supersedes_activation_sha256:
                raise CoordinationPlanError("coordination_plan_generation_conflict")
            _require_activation_current(current[1], now=now)
            return _verified(current[0], current[1])

        supersedes_activation_sha256 = ""
        if current is None:
            if expected:
                raise CoordinationPlanError("coordination_plan_generation_conflict")
            if plan.plan_revision != 1:
                raise CoordinationPlanError("coordination_plan_initial_revision_invalid")
        else:
            previous, previous_activation = current
            if plan.plan_revision <= previous.plan_revision:
                raise CoordinationPlanError("coordination_plan_plan_stale")
            if not _same_digest(expected, previous_activation.activation_sha256):
                raise CoordinationPlanError("coordination_plan_generation_conflict")
            plan.validate_successor(previous)
            supersedes_activation_sha256 = previous_activation.activation_sha256

        issued_at = canonical_text(now)
        activation = CoordinationPlanActivation(
            activation_id=f"plan-activation:{uuid.uuid4().hex}",
            plan_id=plan.plan_id,
            plan_revision=plan.plan_revision,
            plan_sha256=plan.content_sha256,
            mandate_sha256=plan.mandate.content_sha256,
            project=plan.project,
            coordination_session_id=plan.coordination_session_id,
            top_level_agent_session_id=plan.top_level_agent_session_id,
            issued_at_utc=issued_at,
            expires_at_utc=plan.expires_at_utc,
            supersedes_activation_sha256=supersedes_activation_sha256,
        )
        stored_plan, stored_activation = self._repository.append_and_activate(
            plan,
            activation,
            expected_current_activation_sha256=expected,
            _authority_token=_SERVER_AUTHORITY_TOKEN,
        )
        return _verified(stored_plan, stored_activation)

    def verify_current(
        self,
        *,
        plan_sha256: str,
        activation_sha256: str,
    ) -> VerifiedCoordinationPlan:
        """Resolve a repository-backed current plan for dispatch or usage."""

        plan_digest = _digest(plan_sha256, "coordination_plan_digest_invalid")
        activation_digest = _digest(
            activation_sha256,
            "coordination_plan_activation_digest_invalid",
        )
        plan = self._repository.load_plan_by_digest(plan_digest)
        activation = self._repository.load_activation_by_digest(activation_digest)
        if plan is None or activation is None:
            raise CoordinationPlanError("coordination_plan_verification_not_found")
        _require_activation_matches_plan(plan, activation)
        current = self._repository.load_current(
            project_id=plan.project.project_id,
            coordination_session_id=plan.coordination_session_id,
        )
        if current is None or not (
            _same_digest(current[0].content_sha256, plan_digest)
            and _same_digest(current[1].activation_sha256, activation_digest)
        ):
            raise CoordinationPlanError("coordination_plan_not_current")
        now = server_now(self._clock)
        _require_plan_current_window(plan, now=now)
        _require_activation_current(activation, now=now)
        self._repository.require_top_level_session(plan)
        return _verified(plan, activation)

    def verify_issued(
        self,
        *,
        plan_sha256: str,
        activation_sha256: str,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation]:
        """Verify an immutable historical issuance without granting dispatch."""

        plan = self._repository.load_plan_by_digest(plan_sha256)
        activation = self._repository.load_activation_by_digest(activation_sha256)
        if plan is None or activation is None:
            raise CoordinationPlanError("coordination_plan_verification_not_found")
        _require_activation_matches_plan(plan, activation)
        return plan, activation

    def record_usage(
        self,
        verified: VerifiedCoordinationPlan,
        *,
        receipt_id: str,
        responsibility_node_id: str,
        agent_session_id: str,
        token_usage: int | None,
        token_measurement: str,
        measurement_evidence_sha256: str = "",
    ) -> ResourceUsageReceipt:
        """Append usage only while the exact verified plan remains current."""

        if not isinstance(verified, VerifiedCoordinationPlan):
            raise CoordinationPlanError("coordination_plan_verification_required")
        current = self.verify_current(
            plan_sha256=verified.plan.content_sha256,
            activation_sha256=verified.activation.activation_sha256,
        )
        normalized_receipt_id = _identifier(
            receipt_id,
            "resource_usage_receipt_id_invalid",
        )
        existing = self._repository.load_usage_by_id(normalized_receipt_id)
        if existing is not None:
            _require_usage_replay_matches(
                existing,
                plan=current.plan,
                responsibility_node_id=responsibility_node_id,
                agent_session_id=agent_session_id,
                token_usage=token_usage,
                token_measurement=token_measurement,
                measurement_evidence_sha256=measurement_evidence_sha256,
            )
            return existing
        receipt = ResourceUsageReceipt(
            receipt_id=normalized_receipt_id,
            plan_sha256=current.plan.content_sha256,
            responsibility_node_id=responsibility_node_id,
            agent_session_id=agent_session_id,
            token_usage=token_usage,
            token_measurement=token_measurement,
            measurement_evidence_sha256=measurement_evidence_sha256,
            recorded_at_utc=canonical_text(server_now(self._clock)),
        )
        return self._repository.append_usage(
            receipt,
            _authority_token=_SERVER_AUTHORITY_TOKEN,
        )


def open_server_coordination_plan_authority(
    *,
    repository: CoordinationPlanRepository,
    clock: Callable[[], datetime] | None = None,
) -> CoordinationPlanAuthority:
    """Construct the server-owned plan authority around one repository."""

    return CoordinationPlanAuthority(repository=repository, clock=clock)


@dataclass(frozen=True, slots=True)
class UserMandate(_PlanJsonContract):
    """Secret-free summary of the user's authority accepted by a Top-Level Agent."""

    mandate_id: str
    project: ProjectScope
    coordination_session_id: str
    user_instruction_sha256: str
    objective: str
    constraints: tuple[str, ...]
    issued_at_utc: str
    expires_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise CoordinationPlanError("user_mandate_project_invalid")
        object.__setattr__(
            self,
            "mandate_id",
            _identifier(self.mandate_id, "user_mandate_id_invalid"),
        )
        object.__setattr__(
            self,
            "coordination_session_id",
            _identifier(
                self.coordination_session_id,
                "user_mandate_coordination_session_invalid",
            ),
        )
        object.__setattr__(
            self,
            "user_instruction_sha256",
            _digest(
                self.user_instruction_sha256,
                "user_mandate_instruction_digest_invalid",
            ),
        )
        object.__setattr__(
            self,
            "objective",
            _public_text(self.objective, "user_mandate_objective_invalid"),
        )
        object.__setattr__(
            self,
            "constraints",
            _public_lines(self.constraints, "user_mandate_constraints_invalid"),
        )
        issued_at = _timestamp(self.issued_at_utc, "user_mandate_issued_at_invalid")
        expires_at = _timestamp(self.expires_at_utc, "user_mandate_expires_at_invalid")
        if parse_utc(expires_at) <= parse_utc(issued_at):
            raise CoordinationPlanError("user_mandate_expiry_invalid")
        object.__setattr__(self, "issued_at_utc", issued_at)
        object.__setattr__(self, "expires_at_utc", expires_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": USER_MANDATE_SCHEMA,
            "mandate_id": self.mandate_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "user_instruction_sha256": self.user_instruction_sha256,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "raw_user_content": "excluded",
            "authority_effect": "server-binding-required",
        }


@dataclass(frozen=True, slots=True)
class ResourceAllocation(_PlanJsonContract):
    """A subtree allocation chosen by the Top-Level Agent.

    ``agent_slots`` includes the node itself and every planned descendant.
    ``token_budget`` is present only when the provider exposes a ceiling that
    the runtime can authoritatively enforce.  An unavailable provider limit is
    represented by ``None`` rather than an invented number.
    """

    agent_slots: int
    token_budget: int | None = None
    token_budget_authority: str = TOKEN_AUTHORITY_UNAVAILABLE

    def __post_init__(self) -> None:
        slots = _positive_integer(self.agent_slots, "resource_agent_slots_invalid")
        authority = str(self.token_budget_authority or "").strip().casefold()
        if authority not in TOKEN_AUTHORITIES:
            raise CoordinationPlanError("resource_token_authority_invalid")
        token_budget = self.token_budget
        if authority == TOKEN_AUTHORITY_PROVIDER:
            token_budget = _positive_integer(
                token_budget,
                "resource_token_budget_invalid",
            )
        elif token_budget is not None:
            raise CoordinationPlanError("resource_token_budget_unverifiable")
        object.__setattr__(self, "agent_slots", slots)
        object.__setattr__(self, "token_budget", token_budget)
        object.__setattr__(self, "token_budget_authority", authority)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESOURCE_ALLOCATION_SCHEMA,
            "agent_slots": self.agent_slots,
            "token_budget": self.token_budget,
            "token_budget_authority": self.token_budget_authority,
        }


@dataclass(frozen=True, slots=True)
class ResponsibilityNode(_PlanJsonContract):
    """One bounded responsibility, deliberately distinct from Agent identity."""

    node_id: str
    work_item_id: str
    role_intent: str
    scope: str
    allowed_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    acceptance_conditions: tuple[str, ...]
    allocation: ResourceAllocation
    can_delegate: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "node_id",
            _identifier(self.node_id, "responsibility_node_id_invalid"),
        )
        object.__setattr__(
            self,
            "work_item_id",
            _identifier(self.work_item_id, "responsibility_work_item_id_invalid"),
        )
        role = str(self.role_intent or "").strip().casefold()
        if _SAFE_ROLE.fullmatch(role) is None:
            raise CoordinationPlanError("responsibility_role_intent_invalid")
        object.__setattr__(self, "role_intent", role)
        object.__setattr__(
            self,
            "scope",
            _public_text(self.scope, "responsibility_scope_invalid"),
        )
        object.__setattr__(self, "allowed_paths", _paths(self.allowed_paths))
        object.__setattr__(self, "allowed_tools", _tools(self.allowed_tools))
        object.__setattr__(
            self,
            "acceptance_conditions",
            _public_lines(
                self.acceptance_conditions,
                "responsibility_acceptance_conditions_invalid",
            ),
        )
        if not isinstance(self.allocation, ResourceAllocation):
            raise CoordinationPlanError("responsibility_allocation_invalid")
        if not isinstance(self.can_delegate, bool):
            raise CoordinationPlanError("responsibility_delegation_flag_invalid")

    @property
    def responsibility_fingerprint(self) -> str:
        """Semantic identity used to reject duplicate responsibility nodes."""

        return _sha256(
            {
                "role_intent": self.role_intent,
                "scope": self.scope,
                "allowed_paths": list(self.allowed_paths),
                "allowed_tools": list(self.allowed_tools),
                "acceptance_conditions": list(self.acceptance_conditions),
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESPONSIBILITY_NODE_SCHEMA,
            "node_id": self.node_id,
            "work_item_id": self.work_item_id,
            "responsibility_fingerprint": self.responsibility_fingerprint,
            "role_intent": self.role_intent,
            "scope": self.scope,
            "allowed_paths": list(self.allowed_paths),
            "allowed_tools": list(self.allowed_tools),
            "acceptance_conditions": list(self.acceptance_conditions),
            "allocation": self.allocation.to_dict(),
            "can_delegate": self.can_delegate,
            "authority_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class DelegationEdge(_PlanJsonContract):
    """One planned parent-to-child delegation relationship."""

    parent_node_id: str
    child_node_id: str

    def __post_init__(self) -> None:
        parent = _identifier(self.parent_node_id, "delegation_parent_node_invalid")
        child = _identifier(self.child_node_id, "delegation_child_node_invalid")
        if parent == child:
            raise CoordinationPlanError("delegation_self_cycle")
        object.__setattr__(self, "parent_node_id", parent)
        object.__setattr__(self, "child_node_id", child)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DELEGATION_EDGE_SCHEMA,
            "parent_node_id": self.parent_node_id,
            "child_node_id": self.child_node_id,
        }


@dataclass(frozen=True, slots=True)
class CoordinationPlan(_PlanJsonContract):
    """A frozen finite delegation tree selected by one Top-Level Agent."""

    plan_id: str
    plan_revision: int
    mandate: UserMandate
    top_level_agent_session_id: str
    root_node_id: str
    nodes: tuple[ResponsibilityNode, ...]
    edges: tuple[DelegationEdge, ...]
    total_allocation: ResourceAllocation
    created_at_utc: str
    expires_at_utc: str
    supersedes_plan_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plan_id", _identifier(self.plan_id, "coordination_plan_id_invalid")
        )
        revision = _positive_integer(
            self.plan_revision,
            "coordination_plan_revision_invalid",
        )
        if not isinstance(self.mandate, UserMandate):
            raise CoordinationPlanError("coordination_plan_mandate_invalid")
        top_level = _identifier(
            self.top_level_agent_session_id,
            "coordination_plan_top_level_agent_invalid",
        )
        root_node_id = _identifier(self.root_node_id, "coordination_plan_root_node_invalid")
        nodes = _typed_tuple(
            self.nodes,
            ResponsibilityNode,
            "coordination_plan_nodes_invalid",
        )
        if not nodes:
            raise CoordinationPlanError("coordination_plan_nodes_required")
        edges = _typed_tuple(
            self.edges,
            DelegationEdge,
            "coordination_plan_edges_invalid",
        )
        if not isinstance(self.total_allocation, ResourceAllocation):
            raise CoordinationPlanError("coordination_plan_allocation_invalid")
        created_at = _timestamp(self.created_at_utc, "coordination_plan_created_at_invalid")
        expires_at = _timestamp(self.expires_at_utc, "coordination_plan_expires_at_invalid")
        if parse_utc(expires_at) <= parse_utc(created_at):
            raise CoordinationPlanError("coordination_plan_expiry_invalid")
        if parse_utc(created_at) < parse_utc(self.mandate.issued_at_utc):
            raise CoordinationPlanError("coordination_plan_before_mandate")
        if parse_utc(expires_at) > parse_utc(self.mandate.expires_at_utc):
            raise CoordinationPlanError("coordination_plan_exceeds_mandate")
        supersedes = ""
        if revision == 1:
            if self.supersedes_plan_sha256:
                raise CoordinationPlanError("coordination_plan_initial_supersedes_forbidden")
        else:
            supersedes = _digest(
                self.supersedes_plan_sha256,
                "coordination_plan_supersedes_required",
            )
        object.__setattr__(self, "plan_revision", revision)
        object.__setattr__(self, "top_level_agent_session_id", top_level)
        object.__setattr__(self, "root_node_id", root_node_id)
        object.__setattr__(self, "nodes", tuple(sorted(nodes, key=lambda item: item.node_id)))
        object.__setattr__(
            self,
            "edges",
            tuple(sorted(edges, key=lambda item: (item.parent_node_id, item.child_node_id))),
        )
        object.__setattr__(self, "created_at_utc", created_at)
        object.__setattr__(self, "expires_at_utc", expires_at)
        object.__setattr__(self, "supersedes_plan_sha256", supersedes)
        self._validate_graph_and_budget()
        if len(self.canonical_json().encode("utf-8")) > _MAX_PLAN_BYTES:
            raise CoordinationPlanError("coordination_plan_too_large")

    @property
    def project(self) -> ProjectScope:
        return self.mandate.project

    @property
    def coordination_session_id(self) -> str:
        return self.mandate.coordination_session_id

    def node(self, node_id: str) -> ResponsibilityNode:
        normalized = _identifier(node_id, "responsibility_node_id_invalid")
        for node in self.nodes:
            if node.node_id == normalized:
                return node
        raise CoordinationPlanError("coordination_plan_node_not_found")

    def delegation_envelope(self, parent_node_id: str) -> DelegationEnvelope:
        parent = self.node(parent_node_id)
        children = tuple(
            self.node(edge.child_node_id)
            for edge in self.edges
            if edge.parent_node_id == parent.node_id
        )
        allocated_child_slots = sum(child.allocation.agent_slots for child in children)
        remaining_token_budget: int | None = None
        if parent.allocation.token_budget is not None:
            remaining_token_budget = parent.allocation.token_budget - sum(
                int(child.allocation.token_budget or 0) for child in children
            )
        return DelegationEnvelope(
            plan_sha256=self.content_sha256,
            plan_revision=self.plan_revision,
            parent_node_id=parent.node_id,
            child_node_ids=tuple(child.node_id for child in children),
            allocated_agent_slots=allocated_child_slots,
            remaining_agent_slots=parent.allocation.agent_slots - 1 - allocated_child_slots,
            allocated_token_budget=(
                None
                if parent.allocation.token_budget is None
                else parent.allocation.token_budget - int(remaining_token_budget or 0)
            ),
            remaining_token_budget=remaining_token_budget,
            token_budget_authority=parent.allocation.token_budget_authority,
            expires_at_utc=self.expires_at_utc,
        )

    def validate_successor(self, previous: CoordinationPlan) -> None:
        """Fail closed unless this is a Top-Level-Agent-owned successor revision."""

        if not isinstance(previous, CoordinationPlan):
            raise CoordinationPlanError("coordination_plan_previous_invalid")
        if (
            self.plan_id != previous.plan_id
            or self.project != previous.project
            or self.coordination_session_id != previous.coordination_session_id
        ):
            raise CoordinationPlanError("coordination_plan_successor_scope_mismatch")
        if self.plan_revision != previous.plan_revision + 1:
            raise CoordinationPlanError("coordination_plan_successor_revision_invalid")
        if self.supersedes_plan_sha256 != previous.content_sha256:
            raise CoordinationPlanError("coordination_plan_successor_digest_mismatch")
        if self.mandate.content_sha256 != previous.mandate.content_sha256:
            raise CoordinationPlanError("coordination_plan_successor_mandate_mismatch")
        if self.top_level_agent_session_id != previous.top_level_agent_session_id:
            raise CoordinationPlanError("coordination_plan_successor_top_level_mismatch")
        if parse_utc(self.created_at_utc) <= parse_utc(previous.created_at_utc):
            raise CoordinationPlanError("coordination_plan_successor_time_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COORDINATION_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "mandate": self.mandate.to_dict(),
            "mandate_sha256": self.mandate.content_sha256,
            "top_level_agent_session_id": self.top_level_agent_session_id,
            "root_node_id": self.root_node_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "total_allocation": self.total_allocation.to_dict(),
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "supersedes_plan_sha256": self.supersedes_plan_sha256,
            "frozen": True,
            "authority_effect": "server-binding-required",
        }

    def _validate_graph_and_budget(self) -> None:
        nodes = {node.node_id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise CoordinationPlanError("coordination_plan_node_duplicate")
        if self.root_node_id not in nodes:
            raise CoordinationPlanError("coordination_plan_root_missing")
        work_ids = {node.work_item_id for node in self.nodes}
        if len(work_ids) != len(self.nodes):
            raise CoordinationPlanError("coordination_plan_work_item_duplicate")
        fingerprints = {node.responsibility_fingerprint for node in self.nodes}
        if len(fingerprints) != len(self.nodes):
            raise CoordinationPlanError("coordination_plan_responsibility_duplicate")
        root = nodes[self.root_node_id]
        if root.allocation != self.total_allocation:
            raise CoordinationPlanError("coordination_plan_root_allocation_mismatch")
        if len(self.nodes) > self.total_allocation.agent_slots:
            raise CoordinationPlanError("coordination_plan_agent_slots_exceeded")
        if len(self.edges) != len(self.nodes) - 1:
            raise CoordinationPlanError("coordination_plan_edge_count_invalid")

        children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        incoming: dict[str, int] = dict.fromkeys(nodes, 0)
        edge_keys: set[tuple[str, str]] = set()
        for edge in self.edges:
            key = (edge.parent_node_id, edge.child_node_id)
            if key in edge_keys:
                raise CoordinationPlanError("coordination_plan_edge_duplicate")
            edge_keys.add(key)
            if edge.parent_node_id not in nodes or edge.child_node_id not in nodes:
                raise CoordinationPlanError("coordination_plan_edge_node_missing")
            children[edge.parent_node_id].append(edge.child_node_id)
            incoming[edge.child_node_id] += 1
        if incoming[self.root_node_id] != 0:
            raise CoordinationPlanError("coordination_plan_root_has_parent")
        if any(count != 1 for node_id, count in incoming.items() if node_id != self.root_node_id):
            raise CoordinationPlanError("coordination_plan_parent_count_invalid")

        visited: set[str] = set()
        pending = deque((self.root_node_id,))
        while pending:
            node_id = pending.popleft()
            if node_id in visited:
                raise CoordinationPlanError("coordination_plan_cycle")
            visited.add(node_id)
            pending.extend(children[node_id])
        if visited != set(nodes):
            # With one canonical parent for every non-root node and exactly
            # ``n - 1`` edges, an unreachable remainder necessarily contains
            # a delegation cycle.  Report the authority hazard precisely
            # instead of disguising recursive delegation as ordinary drift.
            raise CoordinationPlanError("coordination_plan_cycle")

        for parent_id, child_ids in children.items():
            parent = nodes[parent_id]
            if child_ids and not parent.can_delegate:
                raise CoordinationPlanError("coordination_plan_delegation_forbidden")
            child_nodes = [nodes[child_id] for child_id in child_ids]
            if sum(child.allocation.agent_slots for child in child_nodes) > (
                parent.allocation.agent_slots - 1
            ):
                raise CoordinationPlanError("coordination_plan_agent_budget_exceeded")
            if parent.allocation.token_budget is None:
                if any(child.allocation.token_budget is not None for child in child_nodes):
                    raise CoordinationPlanError("coordination_plan_token_authority_escalation")
            else:
                if any(child.allocation.token_budget is None for child in child_nodes):
                    raise CoordinationPlanError("coordination_plan_child_token_budget_required")
                if sum(int(child.allocation.token_budget or 0) for child in child_nodes) > int(
                    parent.allocation.token_budget
                ):
                    raise CoordinationPlanError("coordination_plan_token_budget_exceeded")
            for child in child_nodes:
                if not _paths_subset(child.allowed_paths, parent.allowed_paths):
                    raise CoordinationPlanError("coordination_plan_path_scope_escalation")
                if not set(child.allowed_tools).issubset(parent.allowed_tools):
                    raise CoordinationPlanError("coordination_plan_tool_scope_escalation")


@dataclass(frozen=True, slots=True)
class DelegationEnvelope(_PlanJsonContract):
    """Read-only projection of the resources a planned coordinator may distribute."""

    plan_sha256: str
    plan_revision: int
    parent_node_id: str
    child_node_ids: tuple[str, ...]
    allocated_agent_slots: int
    remaining_agent_slots: int
    allocated_token_budget: int | None
    remaining_token_budget: int | None
    token_budget_authority: str
    expires_at_utc: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_sha256",
            _digest(self.plan_sha256, "delegation_envelope_plan_digest_invalid"),
        )
        object.__setattr__(
            self,
            "plan_revision",
            _positive_integer(
                self.plan_revision,
                "delegation_envelope_plan_revision_invalid",
            ),
        )
        object.__setattr__(
            self,
            "parent_node_id",
            _identifier(self.parent_node_id, "delegation_envelope_parent_invalid"),
        )
        object.__setattr__(
            self,
            "child_node_ids",
            _identifiers(self.child_node_ids, "delegation_envelope_children_invalid"),
        )
        allocated_slots = _non_negative_integer(
            self.allocated_agent_slots,
            "delegation_envelope_allocated_slots_invalid",
        )
        remaining_slots = _non_negative_integer(
            self.remaining_agent_slots,
            "delegation_envelope_remaining_slots_invalid",
        )
        authority = str(self.token_budget_authority or "").strip().casefold()
        if authority not in TOKEN_AUTHORITIES:
            raise CoordinationPlanError("delegation_envelope_token_authority_invalid")
        if authority == TOKEN_AUTHORITY_PROVIDER:
            allocated_tokens = _non_negative_integer(
                self.allocated_token_budget,
                "delegation_envelope_allocated_tokens_invalid",
            )
            remaining_tokens = _non_negative_integer(
                self.remaining_token_budget,
                "delegation_envelope_remaining_tokens_invalid",
            )
        else:
            if self.allocated_token_budget is not None or self.remaining_token_budget is not None:
                raise CoordinationPlanError("delegation_envelope_token_budget_unverifiable")
            allocated_tokens = None
            remaining_tokens = None
        object.__setattr__(self, "allocated_agent_slots", allocated_slots)
        object.__setattr__(self, "remaining_agent_slots", remaining_slots)
        object.__setattr__(self, "allocated_token_budget", allocated_tokens)
        object.__setattr__(self, "remaining_token_budget", remaining_tokens)
        object.__setattr__(self, "token_budget_authority", authority)
        object.__setattr__(
            self,
            "expires_at_utc",
            _timestamp(self.expires_at_utc, "delegation_envelope_expiry_invalid"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DELEGATION_ENVELOPE_SCHEMA,
            "plan_sha256": self.plan_sha256,
            "plan_revision": self.plan_revision,
            "parent_node_id": self.parent_node_id,
            "child_node_ids": list(self.child_node_ids),
            "allocated_agent_slots": self.allocated_agent_slots,
            "remaining_agent_slots": self.remaining_agent_slots,
            "allocated_token_budget": self.allocated_token_budget,
            "remaining_token_budget": self.remaining_token_budget,
            "token_budget_authority": self.token_budget_authority,
            "expires_at_utc": self.expires_at_utc,
            "authority_effect": "server-current-plan-required",
        }


@dataclass(frozen=True, slots=True)
class ResourceUsageReceipt(_PlanJsonContract):
    """Public usage evidence that never invents provider-authoritative numbers."""

    receipt_id: str
    plan_sha256: str
    responsibility_node_id: str
    agent_session_id: str
    token_usage: int | None
    token_measurement: str
    measurement_evidence_sha256: str
    recorded_at_utc: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _identifier(self.receipt_id, "resource_usage_receipt_id_invalid"),
        )
        object.__setattr__(
            self,
            "plan_sha256",
            _digest(self.plan_sha256, "resource_usage_plan_digest_invalid"),
        )
        object.__setattr__(
            self,
            "responsibility_node_id",
            _identifier(
                self.responsibility_node_id,
                "resource_usage_node_id_invalid",
            ),
        )
        object.__setattr__(
            self,
            "agent_session_id",
            _identifier(self.agent_session_id, "resource_usage_agent_session_invalid"),
        )
        measurement = str(self.token_measurement or "").strip().casefold()
        if measurement not in TOKEN_MEASUREMENTS:
            raise CoordinationPlanError("resource_usage_measurement_invalid")
        token_usage = self.token_usage
        evidence = str(self.measurement_evidence_sha256 or "").strip()
        if measurement == TOKEN_MEASUREMENT_UNAVAILABLE:
            if token_usage is not None or evidence:
                raise CoordinationPlanError("resource_usage_unavailable_has_measurement")
        else:
            token_usage = _non_negative_integer(token_usage, "resource_usage_tokens_invalid")
            if measurement == TOKEN_AUTHORITY_PROVIDER:
                evidence = _digest(evidence, "resource_usage_provider_evidence_invalid")
            elif evidence:
                evidence = _digest(evidence, "resource_usage_estimate_evidence_invalid")
        object.__setattr__(self, "token_usage", token_usage)
        object.__setattr__(self, "token_measurement", measurement)
        object.__setattr__(self, "measurement_evidence_sha256", evidence)
        object.__setattr__(
            self,
            "recorded_at_utc",
            _timestamp(self.recorded_at_utc, "resource_usage_recorded_at_invalid"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESOURCE_USAGE_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "plan_sha256": self.plan_sha256,
            "responsibility_node_id": self.responsibility_node_id,
            "agent_session_id": self.agent_session_id,
            "token_usage": self.token_usage,
            "token_measurement": self.token_measurement,
            "measurement_evidence_sha256": self.measurement_evidence_sha256,
            "recorded_at_utc": self.recorded_at_utc,
            "budget_authority_effect": "none",
        }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(value: object, code: str) -> str:
    normalized = str(value or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(normalized) is None:
        raise CoordinationPlanError(code)
    return normalized


def _digest(value: object, code: str) -> str:
    normalized = str(value or "").strip()
    if _SHA256.fullmatch(normalized) is None:
        raise CoordinationPlanError(code)
    return normalized


def _optional_digest(value: object, code: str) -> str:
    normalized = str(value or "").strip()
    return "" if not normalized else _digest(normalized, code)


def _same_digest(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def _require_activation_matches_plan(
    plan: CoordinationPlan,
    activation: CoordinationPlanActivation,
) -> None:
    if not isinstance(plan, CoordinationPlan) or not isinstance(
        activation,
        CoordinationPlanActivation,
    ):
        raise CoordinationPlanError("coordination_plan_activation_binding_invalid")
    if (
        activation.plan_id != plan.plan_id
        or activation.plan_revision != plan.plan_revision
        or not _same_digest(activation.plan_sha256, plan.content_sha256)
        or not _same_digest(activation.mandate_sha256, plan.mandate.content_sha256)
        or activation.project != plan.project
        or activation.coordination_session_id != plan.coordination_session_id
        or activation.top_level_agent_session_id != plan.top_level_agent_session_id
        or activation.expires_at_utc != plan.expires_at_utc
    ):
        raise CoordinationPlanError("coordination_plan_activation_binding_invalid")
    if plan.plan_revision == 1:
        if activation.supersedes_activation_sha256:
            raise CoordinationPlanError("coordination_plan_activation_binding_invalid")
    elif not activation.supersedes_activation_sha256:
        raise CoordinationPlanError("coordination_plan_activation_binding_invalid")


def _require_session_scope(
    session: AgentSession,
    plan: CoordinationPlan,
    *,
    now: datetime,
) -> None:
    if not isinstance(session, AgentSession):
        raise CoordinationPlanError("coordination_plan_agent_session_invalid")
    if session.project != plan.project:
        raise CoordinationPlanError("coordination_plan_agent_session_project_mismatch")
    if session.coordination_session_id != plan.coordination_session_id:
        raise CoordinationPlanError("coordination_plan_agent_session_scope_mismatch")
    if session.state not in {"registered", "active", "idle"}:
        raise CoordinationPlanError("coordination_plan_agent_session_inactive")
    if session.expires_at is not None and parse_utc(session.expires_at) <= now:
        raise CoordinationPlanError("coordination_plan_agent_session_expired")


def _require_session_for_mandate(
    session: AgentSession,
    mandate: UserMandate,
    *,
    now: datetime,
) -> None:
    if not isinstance(session, AgentSession):
        raise CoordinationPlanError("coordination_plan_agent_session_invalid")
    if session.project != mandate.project:
        raise CoordinationPlanError("coordination_plan_agent_session_project_mismatch")
    if session.coordination_session_id != mandate.coordination_session_id:
        raise CoordinationPlanError("coordination_plan_agent_session_scope_mismatch")
    if session.state not in {"registered", "active", "idle"}:
        raise CoordinationPlanError("coordination_plan_agent_session_inactive")
    if session.expires_at is not None and parse_utc(session.expires_at) <= now:
        raise CoordinationPlanError("coordination_plan_agent_session_expired")
    if parse_utc(mandate.expires_at_utc) <= now:
        raise CoordinationPlanError("user_mandate_expired")


def _require_binding_matches_session_and_mandate(
    binding: TopLevelAgentBinding,
    *,
    session: AgentSession,
    mandate: UserMandate,
) -> None:
    if not isinstance(binding, TopLevelAgentBinding):
        raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
    if (
        binding.project != mandate.project
        or binding.coordination_session_id != mandate.coordination_session_id
        or binding.top_level_agent_session_id != session.session_id
        or binding.top_level_agent_id != session.identity.agent_id
        or not _same_digest(binding.agent_session_sha256, session.content_sha256)
        or not _same_digest(binding.mandate_sha256, mandate.content_sha256)
    ):
        raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")


def _require_session_for_plan(
    session: AgentSession,
    plan: CoordinationPlan,
    *,
    now: datetime,
) -> None:
    _require_session_scope(session, plan, now=now)
    if session.session_id != plan.top_level_agent_session_id:
        raise CoordinationPlanError("coordination_plan_top_level_session_mismatch")


def _require_plan_current_window(plan: CoordinationPlan, *, now: datetime) -> None:
    if parse_utc(plan.created_at_utc) > now:
        raise CoordinationPlanError("coordination_plan_not_yet_active")
    if parse_utc(plan.expires_at_utc) <= now:
        raise CoordinationPlanError("coordination_plan_expired")


def _require_activation_current(
    activation: CoordinationPlanActivation,
    *,
    now: datetime,
) -> None:
    if parse_utc(activation.issued_at_utc) > now:
        raise CoordinationPlanError("coordination_plan_activation_not_yet_active")
    if parse_utc(activation.expires_at_utc) <= now:
        raise CoordinationPlanError("coordination_plan_activation_expired")


def _require_usage_within_budget(
    repository: CoordinationPlanRepository,
    *,
    plan: CoordinationPlan,
    receipt: ResourceUsageReceipt,
) -> None:
    if receipt.token_measurement != TOKEN_AUTHORITY_PROVIDER:
        return
    for ancestor in _node_and_ancestors(plan, receipt.responsibility_node_id):
        if (
            ancestor.allocation.token_budget_authority != TOKEN_AUTHORITY_PROVIDER
            or ancestor.allocation.token_budget is None
        ):
            raise CoordinationPlanError("resource_usage_provider_budget_unavailable")
        subtree_usage = sum(
            repository.total_provider_token_usage(
                plan_sha256=plan.content_sha256,
                responsibility_node_id=node_id,
            )
            for node_id in _subtree_node_ids(plan, ancestor.node_id)
        )
        if subtree_usage + int(receipt.token_usage or 0) > ancestor.allocation.token_budget:
            raise CoordinationPlanError("resource_usage_token_budget_exceeded")


def _require_usage_replay_matches(
    receipt: ResourceUsageReceipt,
    *,
    plan: CoordinationPlan,
    responsibility_node_id: str,
    agent_session_id: str,
    token_usage: int | None,
    token_measurement: str,
    measurement_evidence_sha256: str,
) -> None:
    measurement = str(token_measurement or "").strip().casefold()
    evidence = str(measurement_evidence_sha256 or "").strip()
    if (
        not _same_digest(receipt.plan_sha256, plan.content_sha256)
        or receipt.responsibility_node_id
        != _identifier(responsibility_node_id, "resource_usage_node_id_invalid")
        or receipt.agent_session_id
        != _identifier(agent_session_id, "resource_usage_agent_session_invalid")
        or receipt.token_usage != token_usage
        or receipt.token_measurement != measurement
        or receipt.measurement_evidence_sha256 != evidence
    ):
        raise CoordinationPlanError("resource_usage_receipt_conflict")


def _verified(
    plan: CoordinationPlan,
    activation: CoordinationPlanActivation,
) -> VerifiedCoordinationPlan:
    return VerifiedCoordinationPlan(
        plan,
        activation,
        _verification_token=_VERIFICATION_TOKEN,
    )


def _node_and_ancestors(
    plan: CoordinationPlan,
    node_id: str,
) -> tuple[ResponsibilityNode, ...]:
    current = plan.node(node_id)
    parents = {edge.child_node_id: edge.parent_node_id for edge in plan.edges}
    result = [current]
    while current.node_id != plan.root_node_id:
        parent_id = parents.get(current.node_id)
        if parent_id is None:
            raise CoordinationPlanError("coordination_plan_repository_corrupt")
        current = plan.node(parent_id)
        result.append(current)
    return tuple(result)


def _subtree_node_ids(plan: CoordinationPlan, node_id: str) -> tuple[str, ...]:
    root = plan.node(node_id)
    children: dict[str, list[str]] = {node.node_id: [] for node in plan.nodes}
    for edge in plan.edges:
        children[edge.parent_node_id].append(edge.child_node_id)
    pending = deque((root.node_id,))
    result: list[str] = []
    while pending:
        current = pending.popleft()
        result.append(current)
        pending.extend(children[current])
    return tuple(result)


def _timestamp(value: object, code: str) -> str:
    try:
        return canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CoordinationPlanError(code) from exc


def _positive_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > _MAX_INTEGER:
        raise CoordinationPlanError(code)
    return value


def _non_negative_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_INTEGER:
        raise CoordinationPlanError(code)
    return value


def _public_text(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise CoordinationPlanError(code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > _MAX_PUBLIC_TEXT_BYTES
        or "\x00" in normalized
        or any(pattern.search(normalized) for pattern in _SECRET_PATTERNS)
        or any(pattern.search(normalized) for pattern in _PRIVATE_REASONING_PATTERNS)
    ):
        raise CoordinationPlanError(code)
    return normalized


def _sequence(value: object, code: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CoordinationPlanError(code)
    return tuple(value)


def _public_lines(value: object, code: str) -> tuple[str, ...]:
    lines = tuple(_public_text(item, code) for item in _sequence(value, code))
    if len(set(lines)) != len(lines):
        raise CoordinationPlanError(code)
    return tuple(sorted(lines))


def _identifiers(value: object, code: str) -> tuple[str, ...]:
    items = tuple(_identifier(item, code) for item in _sequence(value, code))
    if len(set(items)) != len(items):
        raise CoordinationPlanError(code)
    return tuple(sorted(items))


def _typed_tuple(value: object, kind: type, code: str):  # type: ignore[no-untyped-def]
    items = _sequence(value, code)
    if any(not isinstance(item, kind) for item in items):
        raise CoordinationPlanError(code)
    return items


def _paths(value: object) -> tuple[str, ...]:
    paths: list[str] = []
    for item in _sequence(value, "responsibility_paths_invalid"):
        if not isinstance(item, str) or item != item.strip() or not item:
            raise CoordinationPlanError("responsibility_path_invalid")
        path = item
        if (
            path.startswith(("/", "~/"))
            or _WINDOWS_DRIVE.match(path) is not None
            or "\\" in path
            or "*" in path
            or "?" in path
            or "[" in path
            or "]" in path
        ):
            raise CoordinationPlanError("responsibility_path_invalid")
        if path != ".":
            segments = path.rstrip("/").split("/")
            if any(segment in {"", ".", ".."} for segment in segments):
                raise CoordinationPlanError("responsibility_path_invalid")
        if len(path.encode("utf-8")) > 512:
            raise CoordinationPlanError("responsibility_path_invalid")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise CoordinationPlanError("responsibility_paths_duplicate")
    return tuple(sorted(paths))


def _tools(value: object) -> tuple[str, ...]:
    tools: list[str] = []
    for item in _sequence(value, "responsibility_tools_invalid"):
        normalized = str(item or "").strip()
        if _SAFE_TOOL.fullmatch(normalized) is None:
            raise CoordinationPlanError("responsibility_tool_invalid")
        tools.append(normalized)
    if len(set(tools)) != len(tools):
        raise CoordinationPlanError("responsibility_tools_duplicate")
    return tuple(sorted(tools))


def _path_covers(parent: str, child: str) -> bool:
    if parent == ".":
        return True
    if parent.endswith("/"):
        return child == parent or child.startswith(parent)
    return child == parent


def _paths_subset(children: tuple[str, ...], parents: tuple[str, ...]) -> bool:
    return all(any(_path_covers(parent, child) for parent in parents) for child in children)
