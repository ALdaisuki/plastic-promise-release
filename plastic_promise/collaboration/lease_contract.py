"""Shared immutable lease contracts for Agent work and compute jobs.

The contracts in this module are deliberately smaller than either the Task
Queue or compute implementations.  They provide one wire-safe seam for the
parts that must mean the same thing in both planes: project scope, lease and
fence identity, heartbeat freshness, result binding, retry, and reconcile
decisions.

They do **not** grant authority, persist state, select work, accept a business
result, or merge the Agent and compute operation policies.  Server-owned
adapters remain responsible for those decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .canonical_time import canonical_text, parse_utc
from .contracts import (
    AgentIdentity,
    CollaborationContractError,
    ProjectScope,
    ResultReceipt,
)

if TYPE_CHECKING:
    from datetime import datetime

WORK_ITEM_SCHEMA = "work-item/v1"
WORK_LEASE_SCHEMA = "work-lease/v1"
LEASE_FENCE_SCHEMA = "lease-fence/v1"
LEASE_HEARTBEAT_SCHEMA = "lease-heartbeat/v1"
LEASE_HEARTBEAT_DECISION_SCHEMA = "lease-heartbeat-decision/v1"
LEASE_COMPLETION_SCHEMA = "lease-completion/v1"
LEASE_COMPLETION_DECISION_SCHEMA = "lease-completion-decision/v1"

AGENT_OWNER_KIND = "agent"
COMPUTE_OWNER_KIND = "compute"
AGENT_WORK_POLICY = "agent-work"
COMPUTE_JOB_POLICY = "compute-job"

_OWNER_POLICY_PAIRS = frozenset(
    {
        (AGENT_OWNER_KIND, AGENT_WORK_POLICY),
        (COMPUTE_OWNER_KIND, COMPUTE_JOB_POLICY),
    }
)
_TERMINAL_REASONS = frozenset({"blocked", "cancelled", "completed", "failed", "timed-out"})
_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_CODE_RE = re.compile(r"\A[a-z][a-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk|rk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_MAX_INTEGER = (1 << 63) - 1


class _JsonContract:
    """Stable canonical JSON projection for immutable lease values."""

    def to_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def content_sha256(self) -> str:
        payload = self.canonical_json().encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkItem(_JsonContract):
    """One durable work description, independent of its storage adapter."""

    work_item_id: str
    project: ProjectScope
    owner_kind: str
    policy_kind: str
    operation_kind: str
    input_sha256: str
    result_schema: str
    created_at: str
    max_attempts: int = 1
    coordination_session_id: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "lease_project_invalid")
        object.__setattr__(self, "work_item_id", _identifier(self.work_item_id, "work_item_id"))
        owner_kind, policy_kind = _owner_policy(self.owner_kind, self.policy_kind)
        operation_kind = _code(self.operation_kind, "work_operation_kind_invalid")
        input_sha256 = _sha256(self.input_sha256, "work_input_sha256_invalid")
        result_schema = _identifier(self.result_schema, "work_result_schema")
        created_at = _timestamp(self.created_at, "work_created_at_invalid")
        max_attempts = _positive_int(self.max_attempts, "work_max_attempts_invalid")
        coordination_session_id = _optional_identifier(
            self.coordination_session_id,
            "coordination_session_id",
        )
        if owner_kind == AGENT_OWNER_KIND and coordination_session_id is None:
            raise CollaborationContractError("agent_work_coordination_session_required")
        if owner_kind == COMPUTE_OWNER_KIND and coordination_session_id is not None:
            raise CollaborationContractError("compute_job_coordination_session_forbidden")
        object.__setattr__(self, "owner_kind", owner_kind)
        object.__setattr__(self, "policy_kind", policy_kind)
        object.__setattr__(self, "operation_kind", operation_kind)
        object.__setattr__(self, "input_sha256", input_sha256)
        object.__setattr__(self, "result_schema", result_schema)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "max_attempts", max_attempts)
        object.__setattr__(self, "coordination_session_id", coordination_session_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": WORK_ITEM_SCHEMA,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "owner_kind": self.owner_kind,
            "policy_kind": self.policy_kind,
            "operation_kind": self.operation_kind,
            "input_sha256": self.input_sha256,
            "result_schema": self.result_schema,
            "created_at": self.created_at,
            "max_attempts": self.max_attempts,
            "coordination_session_id": self.coordination_session_id,
            "authority_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class WorkLease(_JsonContract):
    """One immutable lease generation; it is evidence, not a capability token."""

    lease_id: str
    work_item: WorkItem
    owner_kind: str
    policy_kind: str
    owner_id: str
    fencing_generation: int
    attempt: int
    issued_at: str
    expires_at: str
    result_binding_sha256: str
    idempotency_key_sha256: str
    owner_identity: AgentIdentity | None = None

    def __post_init__(self) -> None:
        _require_type(self.work_item, WorkItem, "lease_work_item_invalid")
        lease_id = _identifier(self.lease_id, "lease_id")
        owner_kind, policy_kind = _owner_policy(self.owner_kind, self.policy_kind)
        if (owner_kind, policy_kind) != (
            self.work_item.owner_kind,
            self.work_item.policy_kind,
        ):
            raise CollaborationContractError("lease_work_policy_mismatch")
        owner_id = _identifier(self.owner_id, "lease_owner_id")
        if owner_kind == AGENT_OWNER_KIND:
            _require_type(self.owner_identity, AgentIdentity, "lease_agent_identity_required")
            assert self.owner_identity is not None
            if self.owner_identity.agent_id != owner_id:
                raise CollaborationContractError("lease_agent_identity_mismatch")
        elif self.owner_identity is not None:
            raise CollaborationContractError("compute_lease_agent_identity_forbidden")
        fencing_generation = _positive_int(
            self.fencing_generation,
            "lease_fencing_generation_invalid",
        )
        attempt = _positive_int(self.attempt, "lease_attempt_invalid")
        if attempt > self.work_item.max_attempts:
            raise CollaborationContractError("lease_attempt_exhausted")
        issued_at = _timestamp(self.issued_at, "lease_issued_at_invalid")
        expires_at = _timestamp(self.expires_at, "lease_expires_at_invalid")
        if _parse_timestamp(issued_at) < _parse_timestamp(self.work_item.created_at):
            raise CollaborationContractError("lease_issued_before_work_created")
        if _parse_timestamp(expires_at) <= _parse_timestamp(issued_at):
            raise CollaborationContractError("lease_expiry_invalid")
        result_binding_sha256 = _sha256(
            self.result_binding_sha256,
            "lease_result_binding_sha256_invalid",
        )
        idempotency_key_sha256 = _sha256(
            self.idempotency_key_sha256,
            "lease_idempotency_key_sha256_invalid",
        )
        object.__setattr__(self, "lease_id", lease_id)
        object.__setattr__(self, "owner_kind", owner_kind)
        object.__setattr__(self, "policy_kind", policy_kind)
        object.__setattr__(self, "owner_id", owner_id)
        object.__setattr__(self, "fencing_generation", fencing_generation)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "result_binding_sha256", result_binding_sha256)
        object.__setattr__(self, "idempotency_key_sha256", idempotency_key_sha256)

    @property
    def project(self) -> ProjectScope:
        return self.work_item.project

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": WORK_LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "work_item": self.work_item.to_dict(),
            "work_item_sha256": self.work_item.content_sha256,
            "project_id": self.project.project_id,
            "owner_kind": self.owner_kind,
            "policy_kind": self.policy_kind,
            "owner_id": self.owner_id,
            "owner_identity": (
                self.owner_identity.to_dict() if self.owner_identity is not None else None
            ),
            "fencing_generation": self.fencing_generation,
            "attempt": self.attempt,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "result_binding_sha256": self.result_binding_sha256,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "authority_effect": "none",
            "operation_policy": "adapter-owned",
        }


@dataclass(frozen=True, slots=True)
class LeaseFence(_JsonContract):
    """Server-observed fence projection for one exact immutable lease."""

    lease_id: str
    lease_sha256: str
    work_item_id: str
    project: ProjectScope
    owner_kind: str
    policy_kind: str
    owner_id: str
    fencing_generation: int
    observed_at: str

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "fence_project_invalid")
        object.__setattr__(self, "lease_id", _identifier(self.lease_id, "fence_lease_id"))
        object.__setattr__(
            self,
            "lease_sha256",
            _sha256(self.lease_sha256, "fence_lease_sha256_invalid"),
        )
        object.__setattr__(
            self,
            "work_item_id",
            _identifier(self.work_item_id, "fence_work_item_id"),
        )
        owner_kind, policy_kind = _owner_policy(self.owner_kind, self.policy_kind)
        object.__setattr__(self, "owner_kind", owner_kind)
        object.__setattr__(self, "policy_kind", policy_kind)
        object.__setattr__(self, "owner_id", _identifier(self.owner_id, "fence_owner_id"))
        object.__setattr__(
            self,
            "fencing_generation",
            _positive_int(self.fencing_generation, "fence_generation_invalid"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _timestamp(self.observed_at, "fence_observed_at_invalid"),
        )

    @classmethod
    def for_lease(cls, lease: WorkLease, *, observed_at: str) -> LeaseFence:
        _require_type(lease, WorkLease, "fence_lease_invalid")
        return cls(
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            work_item_id=lease.work_item.work_item_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=lease.fencing_generation,
            observed_at=observed_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_FENCE_SCHEMA,
            "lease_id": self.lease_id,
            "lease_sha256": self.lease_sha256,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "owner_kind": self.owner_kind,
            "policy_kind": self.policy_kind,
            "owner_id": self.owner_id,
            "fencing_generation": self.fencing_generation,
            "observed_at": self.observed_at,
            "authority_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class LeaseHeartbeat(_JsonContract):
    """Owner-produced liveness fact for one exact lease generation."""

    heartbeat_id: str
    lease_id: str
    lease_sha256: str
    work_item_id: str
    project: ProjectScope
    owner_kind: str
    policy_kind: str
    owner_id: str
    fencing_generation: int
    sequence: int
    sent_at: str

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "heartbeat_project_invalid")
        object.__setattr__(
            self,
            "heartbeat_id",
            _identifier(self.heartbeat_id, "heartbeat_id"),
        )
        object.__setattr__(self, "lease_id", _identifier(self.lease_id, "heartbeat_lease_id"))
        object.__setattr__(
            self,
            "lease_sha256",
            _sha256(self.lease_sha256, "heartbeat_lease_sha256_invalid"),
        )
        object.__setattr__(
            self,
            "work_item_id",
            _identifier(self.work_item_id, "heartbeat_work_item_id"),
        )
        owner_kind, policy_kind = _owner_policy(self.owner_kind, self.policy_kind)
        object.__setattr__(self, "owner_kind", owner_kind)
        object.__setattr__(self, "policy_kind", policy_kind)
        object.__setattr__(
            self,
            "owner_id",
            _identifier(self.owner_id, "heartbeat_owner_id"),
        )
        object.__setattr__(
            self,
            "fencing_generation",
            _positive_int(self.fencing_generation, "heartbeat_generation_invalid"),
        )
        object.__setattr__(
            self,
            "sequence",
            _positive_int(self.sequence, "heartbeat_sequence_invalid"),
        )
        object.__setattr__(
            self,
            "sent_at",
            _timestamp(self.sent_at, "heartbeat_sent_at_invalid"),
        )

    @classmethod
    def for_lease(
        cls,
        lease: WorkLease,
        *,
        heartbeat_id: str,
        sequence: int,
        sent_at: str,
    ) -> LeaseHeartbeat:
        _require_type(lease, WorkLease, "heartbeat_lease_invalid")
        return cls(
            heartbeat_id=heartbeat_id,
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            work_item_id=lease.work_item.work_item_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=lease.fencing_generation,
            sequence=sequence,
            sent_at=sent_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_HEARTBEAT_SCHEMA,
            "heartbeat_id": self.heartbeat_id,
            "lease_id": self.lease_id,
            "lease_sha256": self.lease_sha256,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "owner_kind": self.owner_kind,
            "policy_kind": self.policy_kind,
            "owner_id": self.owner_id,
            "fencing_generation": self.fencing_generation,
            "sequence": self.sequence,
            "sent_at": self.sent_at,
            "authority_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class LeaseCompletion(_JsonContract):
    """Body-free terminal result envelope bound to one immutable lease."""

    completion_id: str
    lease_id: str
    lease_sha256: str
    work_item_id: str
    project: ProjectScope
    owner_kind: str
    policy_kind: str
    owner_id: str
    fencing_generation: int
    result_binding_sha256: str
    result_schema: str
    result_sha256: str
    terminal_reason: str
    completed_at: str
    agent_result_receipt: ResultReceipt | None = None

    def __post_init__(self) -> None:
        _require_type(self.project, ProjectScope, "completion_project_invalid")
        object.__setattr__(
            self,
            "completion_id",
            _identifier(self.completion_id, "completion_id"),
        )
        object.__setattr__(self, "lease_id", _identifier(self.lease_id, "completion_lease_id"))
        object.__setattr__(
            self,
            "lease_sha256",
            _sha256(self.lease_sha256, "completion_lease_sha256_invalid"),
        )
        object.__setattr__(
            self,
            "work_item_id",
            _identifier(self.work_item_id, "completion_work_item_id"),
        )
        owner_kind, policy_kind = _owner_policy(self.owner_kind, self.policy_kind)
        object.__setattr__(self, "owner_kind", owner_kind)
        object.__setattr__(self, "policy_kind", policy_kind)
        object.__setattr__(
            self,
            "owner_id",
            _identifier(self.owner_id, "completion_owner_id"),
        )
        object.__setattr__(
            self,
            "fencing_generation",
            _positive_int(self.fencing_generation, "completion_generation_invalid"),
        )
        object.__setattr__(
            self,
            "result_binding_sha256",
            _sha256(
                self.result_binding_sha256,
                "completion_result_binding_sha256_invalid",
            ),
        )
        object.__setattr__(
            self,
            "result_schema",
            _identifier(self.result_schema, "completion_result_schema"),
        )
        object.__setattr__(
            self,
            "result_sha256",
            _sha256(self.result_sha256, "completion_result_sha256_invalid"),
        )
        terminal_reason = _terminal_reason(self.terminal_reason)
        completed_at = _timestamp(self.completed_at, "completion_completed_at_invalid")
        if owner_kind == AGENT_OWNER_KIND:
            _require_type(
                self.agent_result_receipt,
                ResultReceipt,
                "completion_agent_result_receipt_required",
            )
            assert self.agent_result_receipt is not None
            if self.agent_result_receipt.outcome != terminal_reason:
                raise CollaborationContractError("completion_agent_outcome_mismatch")
            if self.agent_result_receipt.content_sha256 != self.result_sha256:
                raise CollaborationContractError("completion_agent_result_sha256_mismatch")
            if self.agent_result_receipt.work_receipt_sha256 != self.result_binding_sha256:
                raise CollaborationContractError("completion_agent_result_binding_mismatch")
        elif self.agent_result_receipt is not None:
            raise CollaborationContractError("compute_completion_agent_receipt_forbidden")
        object.__setattr__(self, "terminal_reason", terminal_reason)
        object.__setattr__(self, "completed_at", completed_at)

    @classmethod
    def for_agent_result(
        cls,
        lease: WorkLease,
        result: ResultReceipt,
        *,
        completion_id: str,
        completed_at: str | None = None,
    ) -> LeaseCompletion:
        _require_type(lease, WorkLease, "completion_lease_invalid")
        _require_type(result, ResultReceipt, "completion_agent_result_receipt_required")
        if lease.owner_kind != AGENT_OWNER_KIND:
            raise CollaborationContractError("completion_agent_lease_required")
        return cls(
            completion_id=completion_id,
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            work_item_id=lease.work_item.work_item_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=lease.fencing_generation,
            result_binding_sha256=lease.result_binding_sha256,
            result_schema=lease.work_item.result_schema,
            result_sha256=result.content_sha256,
            terminal_reason=result.outcome,
            completed_at=completed_at or result.submitted_at,
            agent_result_receipt=result,
        )

    @classmethod
    def for_compute_result(
        cls,
        lease: WorkLease,
        *,
        completion_id: str,
        result_sha256: str,
        terminal_reason: str,
        completed_at: str,
    ) -> LeaseCompletion:
        _require_type(lease, WorkLease, "completion_lease_invalid")
        if lease.owner_kind != COMPUTE_OWNER_KIND:
            raise CollaborationContractError("completion_compute_lease_required")
        return cls(
            completion_id=completion_id,
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            work_item_id=lease.work_item.work_item_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=lease.fencing_generation,
            result_binding_sha256=lease.result_binding_sha256,
            result_schema=lease.work_item.result_schema,
            result_sha256=result_sha256,
            terminal_reason=terminal_reason,
            completed_at=completed_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_COMPLETION_SCHEMA,
            "completion_id": self.completion_id,
            "lease_id": self.lease_id,
            "lease_sha256": self.lease_sha256,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "owner_kind": self.owner_kind,
            "policy_kind": self.policy_kind,
            "owner_id": self.owner_id,
            "fencing_generation": self.fencing_generation,
            "result_binding_sha256": self.result_binding_sha256,
            "result_schema": self.result_schema,
            "result_sha256": self.result_sha256,
            "terminal_reason": self.terminal_reason,
            "completed_at": self.completed_at,
            "agent_result_receipt": (
                self.agent_result_receipt.to_dict()
                if self.agent_result_receipt is not None
                else None
            ),
            "authority_effect": "none",
            "canonical_memory_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class LeaseHeartbeatDecision(_JsonContract):
    """Protocol result for a heartbeat; persistence remains adapter-owned."""

    accepted: bool
    retryable: bool
    reconcile_required: bool
    reason_code: str
    lease_id: str
    work_item_id: str
    project: ProjectScope
    fencing_generation: int
    observed_at: str
    heartbeat_sha256: str

    def __post_init__(self) -> None:
        _decision_fields(self)
        object.__setattr__(
            self,
            "heartbeat_sha256",
            _sha256(self.heartbeat_sha256, "heartbeat_decision_digest_invalid"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_HEARTBEAT_DECISION_SCHEMA,
            "accepted": self.accepted,
            "retryable": self.retryable,
            "reconcile_required": self.reconcile_required,
            "reason_code": self.reason_code,
            "lease_id": self.lease_id,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "fencing_generation": self.fencing_generation,
            "observed_at": self.observed_at,
            "heartbeat_sha256": self.heartbeat_sha256,
            "authority_effect": "none",
        }


@dataclass(frozen=True, slots=True)
class LeaseCompletionDecision(_JsonContract):
    """Protocol validity decision, never business or canonical-memory acceptance."""

    accepted: bool
    retryable: bool
    reconcile_required: bool
    reason_code: str
    lease_id: str
    work_item_id: str
    project: ProjectScope
    fencing_generation: int
    observed_at: str
    completion_sha256: str

    def __post_init__(self) -> None:
        _decision_fields(self)
        object.__setattr__(
            self,
            "completion_sha256",
            _sha256(self.completion_sha256, "completion_decision_digest_invalid"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LEASE_COMPLETION_DECISION_SCHEMA,
            "accepted": self.accepted,
            "retryable": self.retryable,
            "reconcile_required": self.reconcile_required,
            "reason_code": self.reason_code,
            "lease_id": self.lease_id,
            "work_item_id": self.work_item_id,
            "project_id": self.project.project_id,
            "fencing_generation": self.fencing_generation,
            "observed_at": self.observed_at,
            "completion_sha256": self.completion_sha256,
            "authority_effect": "none",
            "business_acceptance_effect": "none",
            "canonical_memory_effect": "none",
        }


def validate_lease_heartbeat(
    lease: WorkLease,
    current_fence: LeaseFence,
    heartbeat: LeaseHeartbeat,
    *,
    observed_at: str,
) -> LeaseHeartbeatDecision:
    """Validate a heartbeat against one server-owned lease and current fence."""

    _require_type(lease, WorkLease, "heartbeat_lease_invalid")
    _require_type(current_fence, LeaseFence, "heartbeat_fence_invalid")
    _require_type(heartbeat, LeaseHeartbeat, "heartbeat_invalid")
    observed = _timestamp(observed_at, "heartbeat_observed_at_invalid")
    # ``sent_at`` is an owner/edge diagnostic only.  A skewed client clock
    # must not make a valid server observation fail or extend a lease.
    if _parse_timestamp(observed) < _parse_timestamp(current_fence.observed_at):
        raise CollaborationContractError("heartbeat_observed_before_fence")
    reason = _exchange_mismatch(lease, current_fence, heartbeat)
    if reason is None and _parse_timestamp(current_fence.observed_at) < _parse_timestamp(
        lease.issued_at
    ):
        reason = "lease_fence_before_issue"
    if reason is None and (_parse_timestamp(observed) > _parse_timestamp(lease.expires_at)):
        reason = "lease_expired"
    return _heartbeat_decision(lease, heartbeat, observed, reason)


def validate_lease_completion(
    lease: WorkLease,
    current_fence: LeaseFence,
    completion: LeaseCompletion,
    *,
    observed_at: str,
) -> LeaseCompletionDecision:
    """Validate a result envelope without accepting its business contents."""

    _require_type(lease, WorkLease, "completion_lease_invalid")
    _require_type(current_fence, LeaseFence, "completion_fence_invalid")
    _require_type(completion, LeaseCompletion, "completion_invalid")
    observed = _timestamp(observed_at, "completion_observed_at_invalid")
    # ``completed_at`` is supplied by the owner/compute node for diagnostics
    # and result lineage.  Only the server observation participates in lease
    # expiry and ordering decisions.
    if _parse_timestamp(observed) < _parse_timestamp(current_fence.observed_at):
        raise CollaborationContractError("completion_observed_before_fence")
    reason = _exchange_mismatch(lease, current_fence, completion)
    if reason is None and _parse_timestamp(current_fence.observed_at) < _parse_timestamp(
        lease.issued_at
    ):
        reason = "lease_fence_before_issue"
    if reason is None and (_parse_timestamp(observed) > _parse_timestamp(lease.expires_at)):
        reason = "lease_expired"
    if reason is None and completion.result_binding_sha256 != lease.result_binding_sha256:
        reason = "lease_result_binding_mismatch"
    if reason is None and completion.result_schema != lease.work_item.result_schema:
        reason = "lease_result_schema_mismatch"
    if reason is None and lease.owner_kind == AGENT_OWNER_KIND:
        assert lease.owner_identity is not None
        receipt = completion.agent_result_receipt
        if receipt is None or receipt.project != lease.project:
            reason = "lease_result_receipt_scope_mismatch"
        elif receipt.work_item_id != lease.work_item.work_item_id:
            reason = "lease_result_receipt_work_mismatch"
        elif receipt.coordination_session_id != lease.work_item.coordination_session_id:
            reason = "lease_result_receipt_session_mismatch"
        elif receipt.submitted_by != lease.owner_identity:
            reason = "lease_result_receipt_owner_mismatch"
    return _completion_decision(lease, completion, observed, reason)


def _exchange_mismatch(
    lease: WorkLease,
    fence: LeaseFence,
    exchange: LeaseHeartbeat | LeaseCompletion,
) -> str | None:
    if fence.project != lease.project or exchange.project != lease.project:
        return "lease_project_mismatch"
    if (
        fence.work_item_id != lease.work_item.work_item_id
        or exchange.work_item_id != lease.work_item.work_item_id
    ):
        return "lease_work_item_mismatch"
    if fence.lease_id != lease.lease_id or exchange.lease_id != lease.lease_id:
        return "lease_id_mismatch"
    if fence.lease_sha256 != lease.content_sha256 or exchange.lease_sha256 != lease.content_sha256:
        return "lease_digest_mismatch"
    if (fence.owner_kind, fence.policy_kind) != (lease.owner_kind, lease.policy_kind) or (
        exchange.owner_kind,
        exchange.policy_kind,
    ) != (lease.owner_kind, lease.policy_kind):
        return "lease_policy_mismatch"
    if fence.owner_id != lease.owner_id or exchange.owner_id != lease.owner_id:
        return "lease_owner_mismatch"
    if (
        fence.fencing_generation != lease.fencing_generation
        or exchange.fencing_generation != lease.fencing_generation
    ):
        return "lease_fencing_stale"
    return None


def _heartbeat_decision(
    lease: WorkLease,
    heartbeat: LeaseHeartbeat,
    observed_at: str,
    reason: str | None,
) -> LeaseHeartbeatDecision:
    accepted = reason is None
    reason_code = reason or "lease_heartbeat_valid"
    retryable, reconcile = _disposition(reason)
    return LeaseHeartbeatDecision(
        accepted=accepted,
        retryable=retryable,
        reconcile_required=reconcile,
        reason_code=reason_code,
        lease_id=lease.lease_id,
        work_item_id=lease.work_item.work_item_id,
        project=lease.project,
        fencing_generation=lease.fencing_generation,
        observed_at=observed_at,
        heartbeat_sha256=heartbeat.content_sha256,
    )


def _completion_decision(
    lease: WorkLease,
    completion: LeaseCompletion,
    observed_at: str,
    reason: str | None,
) -> LeaseCompletionDecision:
    accepted = reason is None
    reason_code = reason or "lease_completion_valid"
    retryable, reconcile = _disposition(reason)
    return LeaseCompletionDecision(
        accepted=accepted,
        retryable=retryable,
        reconcile_required=reconcile,
        reason_code=reason_code,
        lease_id=lease.lease_id,
        work_item_id=lease.work_item.work_item_id,
        project=lease.project,
        fencing_generation=lease.fencing_generation,
        observed_at=observed_at,
        completion_sha256=completion.content_sha256,
    )


def _disposition(reason: str | None) -> tuple[bool, bool]:
    if reason is None:
        return False, False
    if reason in {"lease_expired", "lease_fencing_stale"}:
        return True, True
    if reason in {
        "lease_completion_before_issue",
        "lease_fence_before_issue",
        "lease_heartbeat_before_issue",
    }:
        return False, False
    return False, True


def _decision_fields(decision: LeaseHeartbeatDecision | LeaseCompletionDecision) -> None:
    for name in ("accepted", "retryable", "reconcile_required"):
        if not isinstance(getattr(decision, name), bool):
            raise CollaborationContractError(f"lease_decision_{name}_invalid")
    _require_type(decision.project, ProjectScope, "lease_decision_project_invalid")
    object.__setattr__(
        decision,
        "reason_code",
        _code(decision.reason_code, "lease_decision_reason_invalid"),
    )
    object.__setattr__(
        decision,
        "lease_id",
        _identifier(decision.lease_id, "lease_decision_lease_id"),
    )
    object.__setattr__(
        decision,
        "work_item_id",
        _identifier(decision.work_item_id, "lease_decision_work_item_id"),
    )
    object.__setattr__(
        decision,
        "fencing_generation",
        _positive_int(decision.fencing_generation, "lease_decision_generation_invalid"),
    )
    object.__setattr__(
        decision,
        "observed_at",
        _timestamp(decision.observed_at, "lease_decision_observed_at_invalid"),
    )
    if decision.accepted and (decision.retryable or decision.reconcile_required):
        raise CollaborationContractError("lease_decision_accepted_disposition_invalid")


def _owner_policy(owner_kind: object, policy_kind: object) -> tuple[str, str]:
    owner = _code(owner_kind, "lease_owner_kind_invalid")
    policy = _code(policy_kind, "lease_policy_kind_invalid")
    if (owner, policy) not in _OWNER_POLICY_PAIRS:
        raise CollaborationContractError("lease_owner_policy_pair_invalid")
    return owner, policy


def _terminal_reason(value: object) -> str:
    reason = _code(value, "completion_terminal_reason_invalid")
    if reason not in _TERMINAL_REASONS:
        raise CollaborationContractError("completion_terminal_reason_invalid")
    return reason


def _require_type(value: object, expected: type[object], code: str) -> None:
    if not isinstance(value, expected):
        raise CollaborationContractError(code)


def _identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _SAFE_IDENTIFIER_RE.fullmatch(value)
        or _secret_value(value)
    ):
        raise CollaborationContractError(f"{field_name}_invalid")
    return value


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _code(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise CollaborationContractError(code)
    normalized = value.strip().casefold()
    if normalized != value or not _SAFE_CODE_RE.fullmatch(normalized):
        raise CollaborationContractError(code)
    return normalized


def _sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CollaborationContractError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > _MAX_INTEGER:
        raise CollaborationContractError(code)
    return value


def _timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise CollaborationContractError(code)
    try:
        return canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CollaborationContractError(code) from exc


def _parse_timestamp(value: str) -> datetime:
    return parse_utc(value)


def _secret_value(value: str) -> bool:
    lowered = value.casefold()
    if re.search(r"-----begin [^-\r\n]{0,48}private key-----", lowered):
        return True
    if re.search(r"\bbearer\s+\S+", value, re.I):
        return True
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
