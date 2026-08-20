"""Narrow adapters from existing Agent/compute values to shared lease contracts.

These adapters translate evidence; they do not create authority or persistence.
The Task Queue keeps its Agent operation policy and tables.  EndpointAuthority
keeps compute admission, model identity, and result-policy validation.  Missing
server-owned evidence is rejected rather than reconstructed from convenient but
non-equivalent fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from plastic_promise.deployment.endpoint_contract import (
    ComputeFence,
    ComputeLease,
    ComputeResult,
)

from .canonical_time import canonical_text, parse_utc
from .contracts import (
    COLLABORATION_RESULT_SCHEMA,
    CollaborationContractError,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from .lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    COMPUTE_JOB_POLICY,
    COMPUTE_OWNER_KIND,
    LeaseCompletion,
    LeaseFence,
    LeaseHeartbeat,
    WorkItem,
    WorkLease,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

_TASK_STATUSES_WITH_LEASE_EVIDENCE = frozenset({"claimed", "done", "executing", "verified"})
_TASK_STATUSES_WITH_HEARTBEAT = frozenset({"claimed", "executing"})
_TASK_STATUSES_WITH_COMPLETION = frozenset({"done", "verified"})
_TASK_INPUT_FIELDS = (
    "id",
    "project_id",
    "task_type",
    "title",
    "description",
    "payload",
    "from_agent",
    "to_agent",
    "domain",
    "memory_id",
    "principle_id",
    "source_scan",
    "parent_task_id",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class AgentTaskLeaseEvidence:
    """Server-owned lease facts absent from the current Task Queue row."""

    assignment: WorkReceipt
    persisted_work_receipt_sha256: str
    lease_id: str
    attempt: int
    max_attempts: int
    idempotency_key_sha256: str


@dataclass(frozen=True, slots=True)
class ComputeWorkEvidence:
    """Durable-work facts not carried by ``ComputeLease`` itself."""

    input_sha256: str
    work_created_at: str
    attempt: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class ServerFenceEvidence:
    """Exact current-lease binding supplied by the canonical server store."""

    lease_id: str
    lease_sha256: str
    fencing_generation: int
    observed_at: str


@dataclass(frozen=True, slots=True)
class HeartbeatProjectionEvidence:
    """Lease-scoped heartbeat identity unavailable in existing source types."""

    heartbeat_id: str
    sequence: int
    sent_at: str | None = None


class AgentTaskQueueLeaseAdapter:
    """Project Task Queue rows through the shared lease seam without SQL writes."""

    @staticmethod
    def project_lease(
        task_row: object,
        evidence: AgentTaskLeaseEvidence | None,
    ) -> WorkLease:
        if evidence is None:
            raise CollaborationContractError("agent_task_lease_evidence_required")
        if not isinstance(evidence, AgentTaskLeaseEvidence):
            raise CollaborationContractError("agent_task_lease_evidence_invalid")
        assignment = evidence.assignment
        if not isinstance(assignment, WorkReceipt):
            raise CollaborationContractError("agent_task_work_receipt_required")
        if evidence.persisted_work_receipt_sha256 != assignment.content_sha256:
            raise CollaborationContractError("agent_task_work_receipt_binding_mismatch")

        task = _task_projection(task_row)
        _require_task_status(task, _TASK_STATUSES_WITH_LEASE_EVIDENCE, "agent_task_not_leased")
        project = ProjectScope(_required_text(task, "project_id", "agent_task_project_invalid"))
        task_id = _required_text(task, "id", "agent_task_id_invalid")
        claimed_by = _required_text(task, "claimed_by", "agent_task_claimed_by_required")
        claimed_at = _utc_text(
            _required_text(task, "claimed_at", "agent_task_claimed_at_required"),
            "agent_task_claimed_at_invalid",
        )
        timeout_seconds = _positive_int(
            task.get("timeout_seconds"),
            "agent_task_timeout_seconds_invalid",
        )
        if assignment.project != project or assignment.work_item_id != task_id:
            raise CollaborationContractError("agent_task_work_receipt_scope_mismatch")
        if assignment.assigned_agent.agent_id != claimed_by:
            raise CollaborationContractError("agent_task_work_receipt_owner_mismatch")
        if assignment.issued_at != claimed_at:
            raise CollaborationContractError("agent_task_work_receipt_issue_mismatch")
        duration = _parse_timestamp(assignment.expires_at) - _parse_timestamp(assignment.issued_at)
        if duration.total_seconds() != timeout_seconds:
            raise CollaborationContractError("agent_task_work_receipt_expiry_mismatch")

        work = WorkItem(
            work_item_id=task_id,
            project=project,
            owner_kind=AGENT_OWNER_KIND,
            policy_kind=AGENT_WORK_POLICY,
            operation_kind=_required_text(
                task,
                "task_type",
                "agent_task_type_invalid",
            ),
            input_sha256=_task_input_sha256(task),
            result_schema=COLLABORATION_RESULT_SCHEMA,
            created_at=_utc_text(
                _required_text(task, "created_at", "agent_task_created_at_required"),
                "agent_task_created_at_invalid",
            ),
            max_attempts=evidence.max_attempts,
            coordination_session_id=assignment.coordination_session_id,
        )
        return WorkLease(
            lease_id=evidence.lease_id,
            work_item=work,
            owner_kind=AGENT_OWNER_KIND,
            policy_kind=AGENT_WORK_POLICY,
            owner_id=assignment.assigned_agent.agent_id,
            owner_identity=assignment.assigned_agent,
            fencing_generation=assignment.fencing_generation,
            attempt=evidence.attempt,
            issued_at=assignment.issued_at,
            expires_at=assignment.expires_at,
            result_binding_sha256=assignment.content_sha256,
            idempotency_key_sha256=evidence.idempotency_key_sha256,
        )

    @staticmethod
    def project_fence(
        task_row: object,
        lease: WorkLease,
        evidence: ServerFenceEvidence | None,
    ) -> LeaseFence:
        _require_agent_lease(lease)
        if evidence is None:
            raise CollaborationContractError("agent_task_current_fence_evidence_required")
        if not isinstance(evidence, ServerFenceEvidence):
            raise CollaborationContractError("agent_task_current_fence_evidence_invalid")
        task = _task_projection(task_row)
        _require_task_scope(task, lease)
        return LeaseFence(
            lease_id=evidence.lease_id,
            lease_sha256=evidence.lease_sha256,
            work_item_id=lease.work_item.work_item_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=evidence.fencing_generation,
            observed_at=evidence.observed_at,
        )

    @staticmethod
    def project_heartbeat(
        task_row: object,
        lease: WorkLease,
        evidence: HeartbeatProjectionEvidence | None,
    ) -> LeaseHeartbeat:
        _require_agent_lease(lease)
        if evidence is None:
            raise CollaborationContractError("agent_task_heartbeat_sequence_evidence_required")
        if not isinstance(evidence, HeartbeatProjectionEvidence):
            raise CollaborationContractError("agent_task_heartbeat_evidence_invalid")
        task = _task_projection(task_row)
        _require_task_scope(task, lease)
        _require_task_status(
            task,
            _TASK_STATUSES_WITH_HEARTBEAT,
            "agent_task_heartbeat_status_invalid",
        )
        sent_at = _utc_text(
            _required_text(task, "heartbeat_at", "agent_task_heartbeat_at_required"),
            "agent_task_heartbeat_at_invalid",
        )
        if (
            evidence.sent_at is not None
            and _utc_text(
                evidence.sent_at,
                "agent_task_heartbeat_evidence_timestamp_invalid",
            )
            != sent_at
        ):
            raise CollaborationContractError("agent_task_heartbeat_timestamp_mismatch")
        return LeaseHeartbeat.for_lease(
            lease,
            heartbeat_id=evidence.heartbeat_id,
            sequence=evidence.sequence,
            sent_at=sent_at,
        )

    @staticmethod
    def project_completion(
        task_row: object,
        lease: WorkLease,
        result: ResultReceipt,
        *,
        completion_id: str,
        persisted_result_receipt_sha256: str | None,
    ) -> LeaseCompletion:
        _require_agent_lease(lease)
        if not isinstance(result, ResultReceipt):
            raise CollaborationContractError("agent_task_result_receipt_required")
        if persisted_result_receipt_sha256 is None:
            raise CollaborationContractError("agent_task_result_receipt_binding_required")
        if persisted_result_receipt_sha256 != result.content_sha256:
            raise CollaborationContractError("agent_task_result_receipt_binding_mismatch")
        task = _task_projection(task_row)
        _require_task_scope(task, lease)
        _require_task_status(
            task,
            _TASK_STATUSES_WITH_COMPLETION,
            "agent_task_completion_status_invalid",
        )
        done_at = _utc_text(
            _required_text(task, "done_at", "agent_task_done_at_required"),
            "agent_task_done_at_invalid",
        )
        if result.submitted_at != done_at:
            raise CollaborationContractError("agent_task_result_timestamp_mismatch")
        if result.project != lease.project or result.work_item_id != lease.work_item.work_item_id:
            raise CollaborationContractError("agent_task_result_scope_mismatch")
        if result.coordination_session_id != lease.work_item.coordination_session_id:
            raise CollaborationContractError("agent_task_result_session_mismatch")
        if result.submitted_by != lease.owner_identity:
            raise CollaborationContractError("agent_task_result_owner_mismatch")
        if result.work_receipt_sha256 != lease.result_binding_sha256:
            raise CollaborationContractError("agent_task_result_work_receipt_mismatch")
        return LeaseCompletion.for_agent_result(
            lease,
            result,
            completion_id=completion_id,
            completed_at=done_at,
        )


class ComputeJobLeaseAdapter:
    """Project typed compute values without replacing ``EndpointAuthority``."""

    @staticmethod
    def project_lease(
        compute_lease: ComputeLease,
        evidence: ComputeWorkEvidence | None,
    ) -> WorkLease:
        if not isinstance(compute_lease, ComputeLease):
            raise CollaborationContractError("compute_lease_invalid")
        if evidence is None:
            raise CollaborationContractError("compute_work_evidence_required")
        if not isinstance(evidence, ComputeWorkEvidence):
            raise CollaborationContractError("compute_work_evidence_invalid")
        work = WorkItem(
            work_item_id=compute_lease.job_id,
            project=ProjectScope(compute_lease.project_id),
            owner_kind=COMPUTE_OWNER_KIND,
            policy_kind=COMPUTE_JOB_POLICY,
            operation_kind=compute_lease.capability,
            input_sha256=evidence.input_sha256,
            result_schema=compute_lease.result_schema,
            created_at=evidence.work_created_at,
            max_attempts=evidence.max_attempts,
        )
        return WorkLease(
            lease_id=compute_lease.lease_id,
            work_item=work,
            owner_kind=COMPUTE_OWNER_KIND,
            policy_kind=COMPUTE_JOB_POLICY,
            owner_id=compute_lease.endpoint_id,
            fencing_generation=compute_lease.fencing_generation,
            attempt=evidence.attempt,
            issued_at=_utc_text(compute_lease.issued_at, "compute_lease_issued_at_invalid"),
            expires_at=_utc_text(compute_lease.expires_at, "compute_lease_expires_at_invalid"),
            result_binding_sha256=_compute_result_binding_sha256(compute_lease),
            idempotency_key_sha256=compute_lease.idempotency_key,
        )

    @staticmethod
    def project_fence(
        compute_fence: ComputeFence,
        lease: WorkLease,
        evidence: ServerFenceEvidence | None,
    ) -> LeaseFence:
        _require_compute_lease(lease)
        if not isinstance(compute_fence, ComputeFence):
            raise CollaborationContractError("compute_fence_invalid")
        if evidence is None:
            raise CollaborationContractError("compute_current_lease_binding_required")
        if not isinstance(evidence, ServerFenceEvidence):
            raise CollaborationContractError("compute_current_lease_binding_invalid")
        if compute_fence.job_id != lease.work_item.work_item_id:
            raise CollaborationContractError("compute_fence_work_item_mismatch")
        if compute_fence.fencing_generation != evidence.fencing_generation:
            raise CollaborationContractError("compute_fence_generation_evidence_mismatch")
        return LeaseFence(
            lease_id=evidence.lease_id,
            lease_sha256=evidence.lease_sha256,
            work_item_id=compute_fence.job_id,
            project=lease.project,
            owner_kind=lease.owner_kind,
            policy_kind=lease.policy_kind,
            owner_id=lease.owner_id,
            fencing_generation=compute_fence.fencing_generation,
            observed_at=evidence.observed_at,
        )

    @staticmethod
    def project_heartbeat(
        lease: WorkLease,
        evidence: HeartbeatProjectionEvidence | None,
    ) -> LeaseHeartbeat:
        _require_compute_lease(lease)
        if evidence is None:
            raise CollaborationContractError("compute_job_heartbeat_evidence_required")
        if not isinstance(evidence, HeartbeatProjectionEvidence):
            raise CollaborationContractError("compute_job_heartbeat_evidence_invalid")
        if evidence.sent_at is None:
            raise CollaborationContractError("compute_job_heartbeat_evidence_required")
        return LeaseHeartbeat.for_lease(
            lease,
            heartbeat_id=evidence.heartbeat_id,
            sequence=evidence.sequence,
            sent_at=evidence.sent_at,
        )

    @staticmethod
    def project_completion(
        compute_lease: ComputeLease,
        result: ComputeResult,
        lease: WorkLease,
        *,
        completion_id: str,
        completed_at: str,
    ) -> LeaseCompletion:
        _require_compute_lease(lease)
        if not isinstance(compute_lease, ComputeLease):
            raise CollaborationContractError("compute_lease_invalid")
        if not isinstance(result, ComputeResult):
            raise CollaborationContractError("compute_result_invalid")
        _require_compute_lease_projection(compute_lease, lease)
        if result.lease_id != compute_lease.lease_id:
            raise CollaborationContractError("compute_result_lease_mismatch")
        if result.endpoint_id != compute_lease.endpoint_id:
            raise CollaborationContractError("compute_result_endpoint_mismatch")
        if result.fencing_generation != compute_lease.fencing_generation:
            raise CollaborationContractError("compute_result_fencing_mismatch")
        if (
            result.capability != compute_lease.capability
            or result.contract_version != compute_lease.contract_version
            or result.result_schema != compute_lease.result_schema
        ):
            raise CollaborationContractError("compute_result_contract_mismatch")
        if result.capability_binding_fingerprint != compute_lease.capability_binding_fingerprint:
            raise CollaborationContractError("compute_result_capability_binding_mismatch")
        if result.identity.fingerprint_for(result.capability) != (
            compute_lease.required_identity_fingerprint
        ):
            raise CollaborationContractError("compute_result_identity_mismatch")
        return LeaseCompletion.for_compute_result(
            lease,
            completion_id=completion_id,
            result_sha256=_compute_result_envelope_sha256(result),
            terminal_reason=result.terminal_reason,
            completed_at=completed_at,
        )


def _require_compute_lease_projection(source: ComputeLease, projected: WorkLease) -> None:
    if (
        projected.lease_id != source.lease_id
        or projected.work_item.work_item_id != source.job_id
        or projected.project != ProjectScope(source.project_id)
        or projected.owner_id != source.endpoint_id
        or projected.fencing_generation != source.fencing_generation
        or projected.issued_at != _utc_text(source.issued_at, "compute_lease_issued_at_invalid")
        or projected.expires_at != _utc_text(source.expires_at, "compute_lease_expires_at_invalid")
        or projected.work_item.operation_kind != source.capability
        or projected.work_item.result_schema != source.result_schema
        or projected.result_binding_sha256 != _compute_result_binding_sha256(source)
        or projected.idempotency_key_sha256 != source.idempotency_key
    ):
        raise CollaborationContractError("compute_shared_lease_projection_mismatch")


def _require_agent_lease(lease: object) -> None:
    if not isinstance(lease, WorkLease) or (
        lease.owner_kind,
        lease.policy_kind,
    ) != (AGENT_OWNER_KIND, AGENT_WORK_POLICY):
        raise CollaborationContractError("agent_shared_lease_required")


def _require_compute_lease(lease: object) -> None:
    if not isinstance(lease, WorkLease) or (
        lease.owner_kind,
        lease.policy_kind,
    ) != (COMPUTE_OWNER_KIND, COMPUTE_JOB_POLICY):
        raise CollaborationContractError("compute_shared_lease_required")


def _require_task_scope(task: Mapping[str, object], lease: WorkLease) -> None:
    if _required_text(task, "id", "agent_task_id_invalid") != lease.work_item.work_item_id:
        raise CollaborationContractError("agent_task_lease_work_item_mismatch")
    if (
        ProjectScope(_required_text(task, "project_id", "agent_task_project_invalid"))
        != lease.project
    ):
        raise CollaborationContractError("agent_task_lease_project_mismatch")
    if (
        _required_text(
            task,
            "claimed_by",
            "agent_task_claimed_by_required",
        )
        != lease.owner_id
    ):
        raise CollaborationContractError("agent_task_lease_owner_mismatch")


def _require_task_status(
    task: Mapping[str, object],
    allowed: frozenset[str],
    code: str,
) -> None:
    status = _required_text(task, "status", "agent_task_status_invalid").casefold()
    if status not in allowed:
        raise CollaborationContractError(code)


def _task_projection(row: object) -> dict[str, object]:
    keys_method = getattr(row, "keys", None)
    if not callable(keys_method):
        raise CollaborationContractError("agent_task_row_invalid")
    try:
        keys = set(keys_method())
    except Exception as exc:
        raise CollaborationContractError("agent_task_row_invalid") from exc
    required = set(_TASK_INPUT_FIELDS).union(
        {"claimed_at", "claimed_by", "done_at", "heartbeat_at", "status", "timeout_seconds"}
    )
    if not required.issubset(keys):
        raise CollaborationContractError("agent_task_row_incomplete")
    try:
        return {key: row[key] for key in required}  # type: ignore[index]
    except Exception as exc:
        raise CollaborationContractError("agent_task_row_invalid") from exc


def _task_input_sha256(task: Mapping[str, object]) -> str:
    projection: dict[str, object] = {}
    for field in _TASK_INPUT_FIELDS:
        value = task[field]
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise CollaborationContractError("agent_task_input_value_invalid")
        projection[field] = value
    projection["created_at"] = _utc_text(
        _required_text(task, "created_at", "agent_task_created_at_required"),
        "agent_task_created_at_invalid",
    )
    return _canonical_sha256(projection)


def _compute_result_binding_sha256(lease: ComputeLease) -> str:
    return _canonical_sha256(
        {
            "schema_version": "compute-result-binding/v1",
            "manifest_digest": lease.manifest_digest,
            "capability": lease.capability,
            "contract_version": lease.contract_version,
            "required_identity_fingerprint": lease.required_identity_fingerprint,
            "input_schema": lease.input_schema,
            "result_schema": lease.result_schema,
            "capability_binding_fingerprint": lease.capability_binding_fingerprint,
        }
    )


def _compute_result_envelope_sha256(result: ComputeResult) -> str:
    return _canonical_sha256(
        {
            "schema_version": "compute-result-envelope/v1",
            "lease_id": result.lease_id,
            "endpoint_id": result.endpoint_id,
            "fencing_generation": result.fencing_generation,
            "capability": result.capability,
            "contract_version": result.contract_version,
            "identity_fingerprint": result.identity.fingerprint_for(result.capability),
            "result_schema": result.result_schema,
            "result_digest": result.result_digest,
            "result_item_count": result.result_item_count,
            "vector_dimension": result.vector_dimension,
            "capability_binding_fingerprint": result.capability_binding_fingerprint,
            "terminal_reason": result.terminal_reason,
        }
    )


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _required_text(row: Mapping[str, object], field: str, code: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CollaborationContractError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CollaborationContractError(code)
    return value


def _utc_text(value: object, code: str) -> str:
    try:
        return canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CollaborationContractError(code) from exc


def _parse_timestamp(value: str) -> datetime:
    return parse_utc(value)
