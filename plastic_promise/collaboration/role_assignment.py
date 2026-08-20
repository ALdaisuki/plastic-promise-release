"""Server-owned, work-scoped semantic role assignments.

``AgentIdentity.role`` and ``agent.intent_declared`` are public declarations;
neither is authority for a concrete work action.  This module supplies the
small server seam that derives one short-lived assignment from canonical
session, workflow, WorkReceipt, WorkLease, and result state.

The public receipt is lineage evidence rather than a bearer capability.
Callers that need to submit or review work must resolve its digest through the
same server authority immediately before the operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, NoReturn, Protocol, SupportsIndex, runtime_checkable

from .canonical_time import canonical_text, parse_utc
from .contracts import (
    AgentSession,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from .lease_contract import AGENT_OWNER_KIND, AGENT_WORK_POLICY, WorkLease

if TYPE_CHECKING:
    from collections.abc import Callable


ROLE_ASSIGNMENT_SCHEMA = "collaboration-role-assignment/v1"
ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA = "collaboration-role-assignment-binding-state/v1"
ROLE_ASSIGNMENT_ISSUER = "pp-server-backend"
ROLE_ASSIGNMENT_POLICY_REVISION = "work-role-assignment-policy/v1"

RESULT_SUBMISSION_USE = "result-submission"
ACCEPTANCE_REVIEW_USE = "acceptance-review"
WORK_SUBMITTER_ROLE = "work.submitter"
WORK_REVIEWER_ROLE = "work.reviewer"

DEFAULT_ROLE_ASSIGNMENT_TTL_SECONDS = 300
MAX_ROLE_ASSIGNMENT_TTL_SECONDS = 3600
INITIAL_ROLE_ASSIGNMENT_BINDING_GENERATION = 1

_ASSIGNMENT_ROLES = {
    RESULT_SUBMISSION_USE: WORK_SUBMITTER_ROLE,
    ACCEPTANCE_REVIEW_USE: WORK_REVIEWER_ROLE,
}
_SUBMISSION_STAGES = frozenset({"diagnosing-bugs", "implement", "prototype", "tdd"})
_REVIEW_STAGES = frozenset({"code-review", "review"})
_SUBMISSION_WORK_STATES = frozenset({"leased", "in_progress"})
_REVIEW_WORK_STATES = frozenset({"reviewing", "submitted"})
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_STAGE = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_SERVER_AUTHORITY_TOKEN = object()
_VERIFICATION_TOKEN = object()


class RoleAssignmentError(ValueError):
    """Stable, non-sensitive role-assignment refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RoleAssignmentBasis:
    """Canonical source snapshot returned by a server repository adapter."""

    session: AgentSession
    work: WorkReceipt
    lease: WorkLease
    intent_event: CollaborationEvent
    workflow_stage: str
    work_state: str
    lease_state: str
    result: ResultReceipt | None = None
    submitter_agent_session_id: str = ""


@dataclass(frozen=True, slots=True)
class RoleAssignmentReceipt:
    """Immutable evidence of one server-derived work role."""

    assignment_id: str
    project: ProjectScope
    coordination_session_id: str
    work_item_id: str
    work_receipt_sha256: str
    lease_id: str
    lease_sha256: str
    fencing_generation: int
    agent_session_id: str
    agent_session_sha256: str
    agent_id: str
    assignment_role: str
    use: str
    workflow_stage: str
    intent_event_id: str
    intent_event_sha256: str
    result_receipt_sha256: str
    assignment_policy_revision: str
    issued_at_utc: str
    expires_at_utc: str
    idempotency_sha256: str
    binding_generation: int = INITIAL_ROLE_ASSIGNMENT_BINDING_GENERATION

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise RoleAssignmentError("role_assignment_project_invalid")
        for value, code in (
            (self.assignment_id, "role_assignment_id_invalid"),
            (self.coordination_session_id, "role_assignment_session_scope_invalid"),
            (self.work_item_id, "role_assignment_work_item_invalid"),
            (self.lease_id, "role_assignment_lease_id_invalid"),
            (self.agent_session_id, "role_assignment_agent_session_id_invalid"),
            (self.agent_id, "role_assignment_agent_id_invalid"),
            (self.intent_event_id, "role_assignment_intent_event_id_invalid"),
            (
                self.assignment_policy_revision,
                "role_assignment_policy_revision_invalid",
            ),
        ):
            _identifier(value, code)
        for value, code in (
            (self.work_receipt_sha256, "role_assignment_work_digest_invalid"),
            (self.lease_sha256, "role_assignment_lease_digest_invalid"),
            (
                self.agent_session_sha256,
                "role_assignment_agent_session_digest_invalid",
            ),
            (
                self.intent_event_sha256,
                "role_assignment_intent_event_digest_invalid",
            ),
            (self.idempotency_sha256, "role_assignment_idempotency_digest_invalid"),
        ):
            _digest(value, code)
        if self.result_receipt_sha256:
            _digest(
                self.result_receipt_sha256,
                "role_assignment_result_digest_invalid",
            )
        use = _use(self.use)
        role = _ASSIGNMENT_ROLES[use]
        if self.assignment_role != role:
            raise RoleAssignmentError("role_assignment_role_mismatch")
        stage = _stage(self.workflow_stage)
        generation = _binding_generation(self.binding_generation)
        if (
            isinstance(self.fencing_generation, bool)
            or not isinstance(self.fencing_generation, int)
            or self.fencing_generation < 1
        ):
            raise RoleAssignmentError("role_assignment_fencing_generation_invalid")
        issued = _timestamp(self.issued_at_utc, "role_assignment_issued_at_invalid")
        expires = _timestamp(self.expires_at_utc, "role_assignment_expires_at_invalid")
        if parse_utc(expires) <= parse_utc(issued):
            raise RoleAssignmentError("role_assignment_expiry_invalid")
        if use == RESULT_SUBMISSION_USE and self.result_receipt_sha256:
            raise RoleAssignmentError("role_assignment_submitter_result_forbidden")
        if use == ACCEPTANCE_REVIEW_USE and not self.result_receipt_sha256:
            raise RoleAssignmentError("role_assignment_reviewer_result_required")
        object.__setattr__(self, "use", use)
        object.__setattr__(self, "workflow_stage", stage)
        object.__setattr__(self, "binding_generation", generation)
        object.__setattr__(self, "issued_at_utc", issued)
        object.__setattr__(self, "expires_at_utc", expires)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ROLE_ASSIGNMENT_SCHEMA,
            "issuer": ROLE_ASSIGNMENT_ISSUER,
            "assignment_id": self.assignment_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "work_item_id": self.work_item_id,
            "work_receipt_sha256": self.work_receipt_sha256,
            "lease_id": self.lease_id,
            "lease_sha256": self.lease_sha256,
            "fencing_generation": self.fencing_generation,
            "binding_generation": self.binding_generation,
            "agent_session_id": self.agent_session_id,
            "agent_session_sha256": self.agent_session_sha256,
            "agent_id": self.agent_id,
            "assignment_role": self.assignment_role,
            "use": self.use,
            "workflow_stage": self.workflow_stage,
            "intent_event_id": self.intent_event_id,
            "intent_event_sha256": self.intent_event_sha256,
            "result_receipt_sha256": self.result_receipt_sha256,
            "assignment_policy_revision": self.assignment_policy_revision,
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "idempotency_sha256": self.idempotency_sha256,
            "authority_effect": "none",
            "tool_policy_effect": "none",
            "canonical_memory_effect": "none",
            "verification": "server-repository-required",
        }

    @property
    def assignment_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class RoleAssignmentBindingState:
    """Server-owned current-state seam for one issued assignment digest."""

    assignment_sha256: str
    server_basis_sha256: str
    binding_generation: int
    revoked_at_utc: str = ""

    def __post_init__(self) -> None:
        assignment_sha256 = _digest(
            self.assignment_sha256,
            "role_assignment_binding_digest_invalid",
        )
        server_basis_sha256 = _digest(
            self.server_basis_sha256,
            "role_assignment_server_basis_digest_invalid",
        )
        generation = _binding_generation(self.binding_generation)
        revoked_at = ""
        if self.revoked_at_utc:
            revoked_at = _timestamp(
                self.revoked_at_utc,
                "role_assignment_revoked_at_invalid",
            )
        object.__setattr__(self, "assignment_sha256", assignment_sha256)
        object.__setattr__(self, "server_basis_sha256", server_basis_sha256)
        object.__setattr__(self, "binding_generation", generation)
        object.__setattr__(self, "revoked_at_utc", revoked_at)

    @property
    def revoked(self) -> bool:
        return bool(self.revoked_at_utc)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA,
            "assignment_sha256": self.assignment_sha256,
            "server_basis_sha256": self.server_basis_sha256,
            "binding_generation": self.binding_generation,
            "revoked": self.revoked,
            "revoked_at_utc": self.revoked_at_utc,
        }


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRoleAssignment:
    """Process-local proof resolved by one exact server authority."""

    _receipt: RoleAssignmentReceipt
    _token: object

    def __init__(
        self,
        receipt: RoleAssignmentReceipt,
        *,
        _verification_token: object | None = None,
    ) -> None:
        if _verification_token is not _VERIFICATION_TOKEN:
            raise RoleAssignmentError("role_assignment_verified_authority_required")
        if not isinstance(receipt, RoleAssignmentReceipt):
            raise RoleAssignmentError("role_assignment_receipt_invalid")
        object.__setattr__(self, "_receipt", receipt)
        object.__setattr__(self, "_token", _verification_token)

    def __reduce__(self) -> NoReturn:
        raise TypeError("verified_role_assignment_not_serializable")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("verified_role_assignment_not_serializable")

    @property
    def assignment_sha256(self) -> str:
        return self._receipt.assignment_sha256

    @property
    def assignment_role(self) -> str:
        return self._receipt.assignment_role

    @property
    def use(self) -> str:
        return self._receipt.use

    @property
    def agent_id(self) -> str:
        return self._receipt.agent_id

    @property
    def agent_session_id(self) -> str:
        return self._receipt.agent_session_id

    @property
    def project(self) -> ProjectScope:
        return self._receipt.project

    @property
    def coordination_session_id(self) -> str:
        return self._receipt.coordination_session_id

    @property
    def work_item_id(self) -> str:
        return self._receipt.work_item_id

    @property
    def work_receipt_sha256(self) -> str:
        return self._receipt.work_receipt_sha256

    @property
    def lease_sha256(self) -> str:
        return self._receipt.lease_sha256

    @property
    def result_receipt_sha256(self) -> str:
        return self._receipt.result_receipt_sha256

    @property
    def assignment_policy_revision(self) -> str:
        return self._receipt.assignment_policy_revision

    @property
    def binding_generation(self) -> int:
        return self._receipt.binding_generation


@runtime_checkable
class RoleAssignmentRepository(Protocol):
    """Canonical source and append-only receipt seam used by the authority."""

    def register_basis(self, *, use: str, basis: RoleAssignmentBasis) -> None: ...

    def resolve_issue_basis(
        self,
        *,
        use: str,
        agent_session_id: str,
        work_item_id: str,
        lease_id: str,
        intent_event_id: str,
    ) -> RoleAssignmentBasis: ...

    def append_exact(
        self,
        receipt: RoleAssignmentReceipt,
        *,
        server_basis_sha256: str | None = None,
        _authority_token: object | None = None,
    ) -> RoleAssignmentReceipt: ...

    def load_by_digest(self, assignment_sha256: str) -> RoleAssignmentReceipt | None: ...

    def load_by_idempotency(
        self,
        idempotency_sha256: str,
    ) -> RoleAssignmentReceipt | None: ...

    def load_binding_state(
        self,
        assignment_sha256: str,
    ) -> RoleAssignmentBindingState | None: ...

    def revoke_exact(
        self,
        assignment_sha256: str,
        *,
        expected_generation: int,
        revoked_at_utc: str,
        _authority_token: object | None = None,
    ) -> RoleAssignmentBindingState: ...


class InMemoryRoleAssignmentRepository:
    """Deterministic local adapter for tests and isolated composition."""

    def __init__(self) -> None:
        self._basis_by_key: dict[tuple[str, str, str, str, str], RoleAssignmentBasis] = {}
        self._receipt_by_digest: dict[str, RoleAssignmentReceipt] = {}
        self._receipt_by_idempotency: dict[str, RoleAssignmentReceipt] = {}
        self._receipt_by_id: dict[str, RoleAssignmentReceipt] = {}
        self._binding_state_by_digest: dict[str, RoleAssignmentBindingState] = {}

    def register_basis(self, *, use: str, basis: RoleAssignmentBasis) -> None:
        if not isinstance(basis, RoleAssignmentBasis):
            raise RoleAssignmentError("role_assignment_basis_invalid")
        normalized_use = _use(use)
        key = (
            normalized_use,
            basis.session.session_id,
            basis.work.work_item_id,
            basis.lease.lease_id,
            basis.intent_event.event_id,
        )
        self._basis_by_key[key] = basis

    def resolve_issue_basis(
        self,
        *,
        use: str,
        agent_session_id: str,
        work_item_id: str,
        lease_id: str,
        intent_event_id: str,
    ) -> RoleAssignmentBasis:
        key = (
            _use(use),
            _identifier(agent_session_id, "role_assignment_agent_session_id_invalid"),
            _identifier(work_item_id, "role_assignment_work_item_invalid"),
            _identifier(lease_id, "role_assignment_lease_id_invalid"),
            _identifier(intent_event_id, "role_assignment_intent_event_id_invalid"),
        )
        try:
            return self._basis_by_key[key]
        except KeyError as exc:
            raise RoleAssignmentError("role_assignment_basis_not_found") from exc

    def append_exact(
        self,
        receipt: RoleAssignmentReceipt,
        *,
        server_basis_sha256: str | None = None,
        _authority_token: object | None = None,
    ) -> RoleAssignmentReceipt:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise RoleAssignmentError("role_assignment_repository_write_authority_required")
        if not isinstance(receipt, RoleAssignmentReceipt):
            raise RoleAssignmentError("role_assignment_receipt_invalid")
        basis_digest = _digest(
            server_basis_sha256,
            "role_assignment_server_basis_digest_invalid",
        )
        existing = self._receipt_by_idempotency.get(receipt.idempotency_sha256)
        if existing is not None:
            self._require_existing_binding(existing, basis_digest)
            return existing
        existing = self._receipt_by_id.get(receipt.assignment_id)
        if existing is not None:
            if hmac.compare_digest(existing.assignment_sha256, receipt.assignment_sha256):
                self._require_existing_binding(existing, basis_digest)
                return existing
            raise RoleAssignmentError("role_assignment_replay_conflict")
        existing = self._receipt_by_digest.get(receipt.assignment_sha256)
        if existing is not None:
            self._require_existing_binding(existing, basis_digest)
            return existing
        state = RoleAssignmentBindingState(
            assignment_sha256=receipt.assignment_sha256,
            server_basis_sha256=basis_digest,
            binding_generation=receipt.binding_generation,
        )
        self._receipt_by_id[receipt.assignment_id] = receipt
        self._receipt_by_idempotency[receipt.idempotency_sha256] = receipt
        self._receipt_by_digest[receipt.assignment_sha256] = receipt
        self._binding_state_by_digest[receipt.assignment_sha256] = state
        return receipt

    def load_by_digest(self, assignment_sha256: str) -> RoleAssignmentReceipt | None:
        return self._receipt_by_digest.get(
            _digest(assignment_sha256, "role_assignment_digest_invalid")
        )

    def load_by_idempotency(
        self,
        idempotency_sha256: str,
    ) -> RoleAssignmentReceipt | None:
        return self._receipt_by_idempotency.get(
            _digest(
                idempotency_sha256,
                "role_assignment_idempotency_digest_invalid",
            )
        )

    def load_binding_state(
        self,
        assignment_sha256: str,
    ) -> RoleAssignmentBindingState | None:
        return self._binding_state_by_digest.get(
            _digest(assignment_sha256, "role_assignment_digest_invalid")
        )

    def revoke_exact(
        self,
        assignment_sha256: str,
        *,
        expected_generation: int,
        revoked_at_utc: str,
        _authority_token: object | None = None,
    ) -> RoleAssignmentBindingState:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise RoleAssignmentError("role_assignment_repository_write_authority_required")
        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        generation = _binding_generation(expected_generation)
        revoked_at = _timestamp(revoked_at_utc, "role_assignment_revoked_at_invalid")
        receipt = self._receipt_by_digest.get(digest)
        if receipt is None:
            raise RoleAssignmentError("role_assignment_not_server_issued")
        state = self._binding_state_by_digest.get(digest)
        if state is None:
            raise RoleAssignmentError("role_assignment_binding_state_missing")
        if state.binding_generation != generation:
            raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
        if state.revoked:
            return state
        if parse_utc(revoked_at) < parse_utc(receipt.issued_at_utc):
            raise RoleAssignmentError("role_assignment_revocation_before_issue")
        revoked = replace(state, revoked_at_utc=revoked_at)
        self._binding_state_by_digest[digest] = revoked
        return revoked

    def _require_existing_binding(
        self,
        receipt: RoleAssignmentReceipt,
        server_basis_sha256: str,
    ) -> None:
        state = self._binding_state_by_digest.get(receipt.assignment_sha256)
        if state is None:
            raise RoleAssignmentError("role_assignment_binding_state_missing")
        if not hmac.compare_digest(state.server_basis_sha256, server_basis_sha256):
            raise RoleAssignmentError("role_assignment_replay_conflict")
        if state.binding_generation != receipt.binding_generation:
            raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
        if state.revoked:
            raise RoleAssignmentError("role_assignment_binding_revoked")


class RoleAssignmentAuthority:
    """Server-only issuer and verifier for scoped semantic work roles."""

    def __init__(
        self,
        *,
        repository: RoleAssignmentRepository,
        clock: Callable[[], datetime],
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise RoleAssignmentError("role_assignment_server_authority_required")
        if not isinstance(repository, RoleAssignmentRepository):
            raise RoleAssignmentError("role_assignment_repository_invalid")
        if not callable(clock):
            raise RoleAssignmentError("role_assignment_clock_invalid")
        self._repository = repository
        self._clock = clock

    def issue(
        self,
        *,
        use: str,
        agent_session_id: str,
        work_item_id: str,
        lease_id: str,
        intent_event_id: str,
        ttl_seconds: int = DEFAULT_ROLE_ASSIGNMENT_TTL_SECONDS,
    ) -> RoleAssignmentReceipt:
        """Derive and persist an assignment without accepting a role claim."""

        normalized_use = _use(use)
        ttl = _ttl_seconds(ttl_seconds)
        basis = self._repository.resolve_issue_basis(
            use=normalized_use,
            agent_session_id=agent_session_id,
            work_item_id=work_item_id,
            lease_id=lease_id,
            intent_event_id=intent_event_id,
        )
        now = _utc_datetime(self._clock(), "role_assignment_clock_invalid")
        role = _ASSIGNMENT_ROLES[normalized_use]
        stage = self._validate_basis(basis, use=normalized_use, role=role, at=now)
        if basis.session.expires_at is None:
            raise RoleAssignmentError("role_assignment_session_expired")
        expiry = min(now + timedelta(seconds=ttl), parse_utc(basis.session.expires_at))
        if normalized_use == RESULT_SUBMISSION_USE:
            expiry = min(expiry, parse_utc(basis.lease.expires_at))
        if expiry <= now:
            raise RoleAssignmentError("role_assignment_expired")
        result_digest = basis.result.content_sha256 if basis.result is not None else ""
        binding_generation = INITIAL_ROLE_ASSIGNMENT_BINDING_GENERATION
        server_basis_sha256 = self._server_basis_sha256(
            basis,
            use=normalized_use,
            role=role,
            stage=stage,
        )
        idempotency = _sha256(
            {
                "server_basis_sha256": server_basis_sha256,
                "binding_generation": binding_generation,
                "assignment_policy_revision": ROLE_ASSIGNMENT_POLICY_REVISION,
            }
        )
        receipt = RoleAssignmentReceipt(
            assignment_id=f"role-assignment:{idempotency.removeprefix('sha256:')[:40]}",
            project=basis.work.project,
            coordination_session_id=basis.work.coordination_session_id,
            work_item_id=basis.work.work_item_id,
            work_receipt_sha256=basis.work.content_sha256,
            lease_id=basis.lease.lease_id,
            lease_sha256=basis.lease.content_sha256,
            fencing_generation=basis.lease.fencing_generation,
            agent_session_id=basis.session.session_id,
            agent_session_sha256=basis.session.content_sha256,
            agent_id=basis.session.identity.agent_id,
            assignment_role=role,
            use=normalized_use,
            workflow_stage=stage,
            intent_event_id=basis.intent_event.event_id,
            intent_event_sha256=basis.intent_event.content_sha256,
            result_receipt_sha256=result_digest,
            assignment_policy_revision=ROLE_ASSIGNMENT_POLICY_REVISION,
            issued_at_utc=canonical_text(now),
            expires_at_utc=canonical_text(expiry),
            idempotency_sha256=idempotency,
            binding_generation=binding_generation,
        )
        try:
            existing = self._repository.load_by_idempotency(idempotency)
        except TypeError as exc:
            raise RoleAssignmentError("role_assignment_repository_contract_invalid") from exc
        if existing is None:
            try:
                stored = self._repository.append_exact(
                    receipt,
                    server_basis_sha256=server_basis_sha256,
                    _authority_token=_SERVER_AUTHORITY_TOKEN,
                )
            except TypeError as exc:
                raise RoleAssignmentError("role_assignment_repository_contract_invalid") from exc
        else:
            if not isinstance(existing, RoleAssignmentReceipt):
                raise RoleAssignmentError("role_assignment_receipt_invalid")
            if not hmac.compare_digest(existing.idempotency_sha256, idempotency):
                raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
            stored = existing
        self._require_stored_assignment(
            stored,
            basis=basis,
            use=normalized_use,
            role=role,
            stage=stage,
            server_basis_sha256=server_basis_sha256,
            at=now,
        )
        return stored

    def verify_for_use(
        self,
        assignment_sha256: str,
        *,
        use: str,
        used_at: str,
    ) -> VerifiedRoleAssignment:
        """Resolve one current assignment from canonical server sources."""

        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        receipt = self._repository.load_by_digest(digest)
        if receipt is None:
            raise RoleAssignmentError("role_assignment_not_server_issued")
        if not isinstance(receipt, RoleAssignmentReceipt):
            raise RoleAssignmentError("role_assignment_receipt_invalid")
        if not hmac.compare_digest(receipt.assignment_sha256, digest):
            raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
        normalized_use = _use(use)
        if receipt.use != normalized_use:
            raise RoleAssignmentError("role_assignment_use_mismatch")
        if receipt.assignment_policy_revision != ROLE_ASSIGNMENT_POLICY_REVISION:
            raise RoleAssignmentError("role_assignment_policy_stale")
        expected_role = _ASSIGNMENT_ROLES[normalized_use]
        if receipt.assignment_role != expected_role:
            raise RoleAssignmentError("role_assignment_role_mismatch")
        state = self._repository.load_binding_state(digest)
        self._require_binding_state(receipt, state)
        assert state is not None
        if state.revoked:
            raise RoleAssignmentError("role_assignment_binding_revoked")
        basis = self._repository.resolve_issue_basis(
            use=receipt.use,
            agent_session_id=receipt.agent_session_id,
            work_item_id=receipt.work_item_id,
            lease_id=receipt.lease_id,
            intent_event_id=receipt.intent_event_id,
        )
        observed_at = parse_utc(_timestamp(used_at, "role_assignment_use_time_invalid"))
        server_at = _utc_datetime(self._clock(), "role_assignment_clock_invalid")
        if observed_at > server_at:
            raise RoleAssignmentError("role_assignment_use_time_in_future")
        stage = self._validate_basis(
            basis,
            use=normalized_use,
            role=expected_role,
            at=server_at,
        )
        self._require_receipt_scope(receipt, basis)
        server_basis_sha256 = self._server_basis_sha256(
            basis,
            use=normalized_use,
            role=expected_role,
            stage=stage,
        )
        if not hmac.compare_digest(state.server_basis_sha256, server_basis_sha256):
            raise RoleAssignmentError("role_assignment_binding_basis_mismatch")
        issued_at = parse_utc(receipt.issued_at_utc)
        expires_at = parse_utc(receipt.expires_at_utc)
        if observed_at < issued_at or server_at < issued_at:
            raise RoleAssignmentError("role_assignment_use_before_issue")
        if observed_at >= expires_at or server_at >= expires_at:
            raise RoleAssignmentError("role_assignment_expired")
        return VerifiedRoleAssignment(
            receipt,
            _verification_token=_VERIFICATION_TOKEN,
        )

    def revoke(
        self,
        assignment_sha256: str,
        *,
        expected_generation: int,
    ) -> RoleAssignmentBindingState:
        """Revoke one exact binding generation through the server repository."""

        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        generation = _binding_generation(expected_generation)
        try:
            revoked = self._repository.revoke_exact(
                digest,
                expected_generation=generation,
                revoked_at_utc=canonical_text(
                    _utc_datetime(self._clock(), "role_assignment_clock_invalid")
                ),
                _authority_token=_SERVER_AUTHORITY_TOKEN,
            )
        except TypeError as exc:
            raise RoleAssignmentError("role_assignment_repository_contract_invalid") from exc
        if not isinstance(revoked, RoleAssignmentBindingState):
            raise RoleAssignmentError("role_assignment_binding_state_invalid")
        if not hmac.compare_digest(revoked.assignment_sha256, digest):
            raise RoleAssignmentError("role_assignment_binding_digest_mismatch")
        if revoked.binding_generation != generation:
            raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
        if not revoked.revoked:
            raise RoleAssignmentError("role_assignment_revocation_not_persisted")
        return revoked

    def _require_stored_assignment(
        self,
        receipt: RoleAssignmentReceipt,
        *,
        basis: RoleAssignmentBasis,
        use: str,
        role: str,
        stage: str,
        server_basis_sha256: str,
        at: datetime,
    ) -> None:
        if not isinstance(receipt, RoleAssignmentReceipt):
            raise RoleAssignmentError("role_assignment_receipt_invalid")
        if receipt.use != use:
            raise RoleAssignmentError("role_assignment_use_mismatch")
        if receipt.assignment_role != role:
            raise RoleAssignmentError("role_assignment_role_mismatch")
        if receipt.workflow_stage != stage:
            raise RoleAssignmentError("role_assignment_intent_stage_mismatch")
        if receipt.assignment_policy_revision != ROLE_ASSIGNMENT_POLICY_REVISION:
            raise RoleAssignmentError("role_assignment_policy_stale")
        self._require_receipt_scope(receipt, basis)
        state = self._repository.load_binding_state(receipt.assignment_sha256)
        self._require_binding_state(receipt, state)
        assert state is not None
        if not hmac.compare_digest(state.server_basis_sha256, server_basis_sha256):
            raise RoleAssignmentError("role_assignment_binding_basis_mismatch")
        if state.revoked:
            raise RoleAssignmentError("role_assignment_binding_revoked")
        if parse_utc(receipt.issued_at_utc) > at:
            raise RoleAssignmentError("role_assignment_use_before_issue")
        if parse_utc(receipt.expires_at_utc) <= at:
            raise RoleAssignmentError("role_assignment_expired")

    @staticmethod
    def _require_binding_state(
        receipt: RoleAssignmentReceipt,
        state: RoleAssignmentBindingState | None,
    ) -> None:
        if state is None:
            raise RoleAssignmentError("role_assignment_binding_state_missing")
        if not isinstance(state, RoleAssignmentBindingState):
            raise RoleAssignmentError("role_assignment_binding_state_invalid")
        if not hmac.compare_digest(
            state.assignment_sha256,
            receipt.assignment_sha256,
        ):
            raise RoleAssignmentError("role_assignment_binding_digest_mismatch")
        if state.binding_generation != receipt.binding_generation:
            raise RoleAssignmentError("role_assignment_binding_generation_mismatch")

    @staticmethod
    def _server_basis_sha256(
        basis: RoleAssignmentBasis,
        *,
        use: str,
        role: str,
        stage: str,
    ) -> str:
        if not isinstance(basis, RoleAssignmentBasis):
            raise RoleAssignmentError("role_assignment_basis_invalid")
        submitter_session_id = str(basis.submitter_agent_session_id or "").strip()
        if submitter_session_id:
            submitter_session_id = _identifier(
                submitter_session_id,
                "role_assignment_submitter_session_id_invalid",
            )
        result = basis.result
        return _sha256(
            {
                "schema_version": ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA,
                "assignment_policy_revision": ROLE_ASSIGNMENT_POLICY_REVISION,
                "use": use,
                "assignment_role": role,
                "workflow_stage": stage,
                "work_state": str(basis.work_state or "").strip().casefold(),
                "lease_state": str(basis.lease_state or "").strip().casefold(),
                "project_id": basis.work.project.project_id,
                "coordination_session_id": basis.work.coordination_session_id,
                "agent_session_id": basis.session.session_id,
                "agent_session_sha256": basis.session.content_sha256,
                "agent_id": basis.session.identity.agent_id,
                "work_item_id": basis.work.work_item_id,
                "work_receipt_sha256": basis.work.content_sha256,
                "work_assigned_agent_id": basis.work.assigned_agent.agent_id,
                "lease_id": basis.lease.lease_id,
                "lease_sha256": basis.lease.content_sha256,
                "lease_owner_id": basis.lease.owner_id,
                "fencing_generation": basis.lease.fencing_generation,
                "intent_event_id": basis.intent_event.event_id,
                "intent_event_sha256": basis.intent_event.content_sha256,
                "intent_actor_agent_id": basis.intent_event.actor.agent_id,
                "result_receipt_sha256": result.content_sha256 if result is not None else "",
                "result_submitter_agent_id": (
                    result.submitted_by.agent_id if result is not None else ""
                ),
                "submitter_agent_session_id": submitter_session_id,
            }
        )

    def _validate_basis(
        self,
        basis: RoleAssignmentBasis,
        *,
        use: str,
        role: str,
        at: datetime,
    ) -> str:
        if not isinstance(basis, RoleAssignmentBasis):
            raise RoleAssignmentError("role_assignment_basis_invalid")
        session = basis.session
        work = basis.work
        lease = basis.lease
        event = basis.intent_event
        if session.state != "active":
            raise RoleAssignmentError("role_assignment_session_not_active")
        if parse_utc(session.started_at) > at:
            raise RoleAssignmentError("role_assignment_session_not_started")
        if session.expires_at is None or parse_utc(session.expires_at) <= at:
            raise RoleAssignmentError("role_assignment_session_expired")
        if session.project != work.project:
            raise RoleAssignmentError("role_assignment_project_mismatch")
        if session.coordination_session_id != work.coordination_session_id:
            raise RoleAssignmentError("role_assignment_coordination_session_mismatch")
        if lease.work_item.work_item_id != work.work_item_id:
            raise RoleAssignmentError("role_assignment_work_item_mismatch")
        if lease.project != work.project:
            raise RoleAssignmentError("role_assignment_project_mismatch")
        if lease.work_item.coordination_session_id != work.coordination_session_id:
            raise RoleAssignmentError("role_assignment_coordination_session_mismatch")
        if lease.owner_kind != AGENT_OWNER_KIND or lease.policy_kind != AGENT_WORK_POLICY:
            raise RoleAssignmentError("role_assignment_agent_work_required")
        if lease.result_binding_sha256 != work.content_sha256:
            raise RoleAssignmentError("role_assignment_work_receipt_digest_mismatch")
        if lease.fencing_generation != work.fencing_generation:
            raise RoleAssignmentError("role_assignment_fencing_stale")
        if event.event_type != "agent.intent_declared":
            raise RoleAssignmentError("role_assignment_intent_type_invalid")
        if event.project != work.project:
            raise RoleAssignmentError("role_assignment_intent_scope_mismatch")
        if event.coordination_session_id != work.coordination_session_id:
            raise RoleAssignmentError("role_assignment_intent_scope_mismatch")
        if event.work_item_id != work.work_item_id:
            raise RoleAssignmentError("role_assignment_intent_work_mismatch")
        if parse_utc(event.created_at) > at:
            raise RoleAssignmentError("role_assignment_intent_not_observed")
        if event.expires_at is not None and parse_utc(event.expires_at) <= at:
            raise RoleAssignmentError("role_assignment_intent_expired")
        if event.actor.agent_id != session.identity.agent_id:
            raise RoleAssignmentError("role_assignment_intent_actor_mismatch")
        payload = event.payload
        if str(payload.get("authority_effect") or "").strip().casefold() != "none":
            raise RoleAssignmentError("role_assignment_intent_authority_invalid")
        if str(payload.get("requested_use") or "").strip().casefold() != use:
            raise RoleAssignmentError("role_assignment_intent_use_mismatch")
        if str(payload.get("requested_role") or "").strip().casefold() != role:
            raise RoleAssignmentError("role_assignment_intent_role_mismatch")
        stage = _stage(basis.workflow_stage)
        if str(payload.get("workflow_stage") or "").strip().casefold() != stage:
            raise RoleAssignmentError("role_assignment_intent_stage_mismatch")
        if use == RESULT_SUBMISSION_USE:
            if stage not in _SUBMISSION_STAGES:
                raise RoleAssignmentError("role_assignment_stage_not_assignable")
            if basis.work_state not in _SUBMISSION_WORK_STATES:
                raise RoleAssignmentError("role_assignment_submitter_work_state_invalid")
            if basis.lease_state != "active":
                raise RoleAssignmentError("role_assignment_submitter_lease_not_active")
            if lease.owner_id != session.identity.agent_id:
                raise RoleAssignmentError("role_assignment_submitter_not_lease_owner")
            if (
                lease.owner_identity is None
                or lease.owner_identity.agent_id != session.identity.agent_id
            ):
                raise RoleAssignmentError("role_assignment_lease_identity_mismatch")
            if work.assigned_agent.agent_id != session.identity.agent_id:
                raise RoleAssignmentError("role_assignment_submitter_not_assignee")
            if parse_utc(lease.issued_at) > at:
                raise RoleAssignmentError("role_assignment_lease_not_started")
            if parse_utc(lease.expires_at) <= at:
                raise RoleAssignmentError("role_assignment_lease_expired")
            if basis.result is not None:
                raise RoleAssignmentError("role_assignment_submitter_result_forbidden")
        else:
            if stage not in _REVIEW_STAGES:
                raise RoleAssignmentError("role_assignment_stage_not_assignable")
            if basis.work_state not in _REVIEW_WORK_STATES:
                raise RoleAssignmentError("role_assignment_review_state_invalid")
            if basis.lease_state != "completed":
                raise RoleAssignmentError("role_assignment_review_source_lease_incomplete")
            result = basis.result
            if not isinstance(result, ResultReceipt):
                raise RoleAssignmentError("role_assignment_review_result_missing")
            if result.project != work.project:
                raise RoleAssignmentError("role_assignment_project_mismatch")
            if result.coordination_session_id != work.coordination_session_id:
                raise RoleAssignmentError("role_assignment_coordination_session_mismatch")
            if result.work_item_id != work.work_item_id:
                raise RoleAssignmentError("role_assignment_work_item_mismatch")
            if result.work_receipt_sha256 != work.content_sha256:
                raise RoleAssignmentError("role_assignment_work_receipt_digest_mismatch")
            if result.submitted_by.agent_id != work.assigned_agent.agent_id:
                raise RoleAssignmentError("role_assignment_result_submitter_mismatch")
            if result.submitted_by.agent_id == session.identity.agent_id:
                raise RoleAssignmentError("role_assignment_self_review_forbidden")
            if (
                basis.submitter_agent_session_id
                and basis.submitter_agent_session_id == session.session_id
            ):
                raise RoleAssignmentError("role_assignment_self_review_forbidden")
        return stage

    @staticmethod
    def _require_receipt_scope(
        receipt: RoleAssignmentReceipt,
        basis: RoleAssignmentBasis,
    ) -> None:
        result_digest = basis.result.content_sha256 if basis.result is not None else ""
        checks = (
            (receipt.project == basis.work.project, "role_assignment_project_mismatch"),
            (
                receipt.coordination_session_id == basis.work.coordination_session_id,
                "role_assignment_coordination_session_mismatch",
            ),
            (receipt.work_item_id == basis.work.work_item_id, "role_assignment_work_item_mismatch"),
            (
                hmac.compare_digest(receipt.work_receipt_sha256, basis.work.content_sha256),
                "role_assignment_work_receipt_digest_mismatch",
            ),
            (receipt.lease_id == basis.lease.lease_id, "role_assignment_lease_mismatch"),
            (
                hmac.compare_digest(receipt.lease_sha256, basis.lease.content_sha256),
                "role_assignment_lease_digest_mismatch",
            ),
            (
                receipt.fencing_generation == basis.lease.fencing_generation,
                "role_assignment_fencing_stale",
            ),
            (
                receipt.agent_session_id == basis.session.session_id,
                "role_assignment_session_mismatch",
            ),
            (
                hmac.compare_digest(
                    receipt.agent_session_sha256,
                    basis.session.content_sha256,
                ),
                "role_assignment_session_digest_mismatch",
            ),
            (receipt.agent_id == basis.session.identity.agent_id, "role_assignment_agent_mismatch"),
            (
                hmac.compare_digest(
                    receipt.intent_event_sha256,
                    basis.intent_event.content_sha256,
                ),
                "role_assignment_intent_digest_mismatch",
            ),
            (
                receipt.result_receipt_sha256 == result_digest,
                "role_assignment_result_digest_mismatch",
            ),
        )
        for accepted, code in checks:
            if not accepted:
                raise RoleAssignmentError(code)


def open_server_role_assignment_authority(
    *,
    repository: RoleAssignmentRepository,
    clock: Callable[[], datetime] | None = None,
) -> RoleAssignmentAuthority:
    """Open the assignment authority inside canonical server wiring only."""

    return RoleAssignmentAuthority(
        repository=repository,
        clock=clock or (lambda: datetime.now(timezone.utc)),
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


def _use(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text not in _ASSIGNMENT_ROLES:
        raise RoleAssignmentError("role_assignment_use_invalid")
    return text


def _stage(value: object) -> str:
    text = str(value or "").strip().casefold()
    if _SAFE_STAGE.fullmatch(text) is None:
        raise RoleAssignmentError("role_assignment_workflow_stage_invalid")
    return text


def _identifier(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(text) is None:
        raise RoleAssignmentError(code)
    return text


def _digest(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise RoleAssignmentError(code)
    return text


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise RoleAssignmentError(code)
    try:
        return str(canonical_text(parse_utc(value)))
    except (TypeError, ValueError) as exc:
        raise RoleAssignmentError(code) from exc


def _utc_datetime(value: datetime, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RoleAssignmentError(code)
    return value.astimezone(timezone.utc)


def _ttl_seconds(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ROLE_ASSIGNMENT_TTL_SECONDS
    ):
        raise RoleAssignmentError("role_assignment_ttl_invalid")
    return value


def _binding_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RoleAssignmentError("role_assignment_binding_generation_invalid")
    return value


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ACCEPTANCE_REVIEW_USE",
    "DEFAULT_ROLE_ASSIGNMENT_TTL_SECONDS",
    "INITIAL_ROLE_ASSIGNMENT_BINDING_GENERATION",
    "InMemoryRoleAssignmentRepository",
    "MAX_ROLE_ASSIGNMENT_TTL_SECONDS",
    "RESULT_SUBMISSION_USE",
    "ROLE_ASSIGNMENT_ISSUER",
    "ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA",
    "ROLE_ASSIGNMENT_POLICY_REVISION",
    "ROLE_ASSIGNMENT_SCHEMA",
    "RoleAssignmentAuthority",
    "RoleAssignmentBasis",
    "RoleAssignmentBindingState",
    "RoleAssignmentError",
    "RoleAssignmentReceipt",
    "RoleAssignmentRepository",
    "VerifiedRoleAssignment",
    "WORK_REVIEWER_ROLE",
    "WORK_SUBMITTER_ROLE",
    "open_server_role_assignment_authority",
]
