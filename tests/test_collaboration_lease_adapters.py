from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    CollaborationContractError,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.lease_adapters import (
    AgentTaskLeaseEvidence,
    AgentTaskQueueLeaseAdapter,
    ComputeJobLeaseAdapter,
    ComputeWorkEvidence,
    HeartbeatProjectionEvidence,
    ServerFenceEvidence,
)
from plastic_promise.collaboration.lease_contract import (
    validate_lease_completion,
    validate_lease_heartbeat,
)
from plastic_promise.deployment.endpoint_contract import (
    ComputeFence,
    ComputeLease,
    ComputeResult,
    EmbeddingIdentity,
    EndpointIdentityEvidence,
)

NOW = "2026-08-11T01:00:00Z"
HEARTBEAT_AT = "2026-08-11T01:02:00Z"
DONE_AT = "2026-08-11T01:04:00Z"
EXPIRES_AT = "2026-08-11T01:05:00Z"
UTC_NOW = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _agent() -> AgentIdentity:
    return AgentIdentity("agent:builder", "implementer")


def _task_row(*, status: str = "executing") -> dict[str, object]:
    return {
        "id": "task:lease-adapter",
        "project_id": "project:plastic-promise",
        "task_type": "implement",
        "title": "Project the task through the shared lease seam",
        "description": "Adapter-only work",
        "payload": '{"scope":"lease"}',
        "from_agent": "agent:coordinator",
        "to_agent": "agent:builder",
        "domain": "development",
        "memory_id": None,
        "principle_id": None,
        "source_scan": None,
        "parent_task_id": None,
        "created_at": "2026-08-11T00:59:00Z",
        "status": status,
        "claimed_by": "agent:builder",
        "claimed_at": NOW,
        "heartbeat_at": HEARTBEAT_AT,
        "done_at": DONE_AT if status in {"done", "verified"} else None,
        "timeout_seconds": 300,
    }


def _assignment() -> WorkReceipt:
    return WorkReceipt(
        receipt_id="work-receipt:lease-adapter",
        work_item_id="task:lease-adapter",
        project=ProjectScope("project:plastic-promise"),
        coordination_session_id="coord:pr2",
        assigned_agent=_agent(),
        objective="Project the task without merging Task Queue policy",
        fencing_generation=7,
        issued_at=NOW,
        expires_at=EXPIRES_AT,
    )


def _agent_evidence() -> AgentTaskLeaseEvidence:
    assignment = _assignment()
    return AgentTaskLeaseEvidence(
        assignment=assignment,
        persisted_work_receipt_sha256=assignment.content_sha256,
        lease_id="lease:task:7",
        attempt=1,
        max_attempts=2,
        idempotency_key_sha256=_digest("a"),
    )


def _embedding_identity(*, golden: str = "b") -> EndpointIdentityEvidence:
    return EndpointIdentityEvidence(
        embedding=EmbeddingIdentity(
            model="Qwen/Qwen3-Embedding-4B",
            revision="a" * 40,
            dimension=2560,
            normalization="l2",
            metric="cosine",
            tokenization="qwen3",
            pooling="last-token",
            artifact_sha256=_digest("c"),
            golden_vector_sha256=_digest(golden),
        )
    )


def _compute_lease() -> ComputeLease:
    identity = _embedding_identity()
    fingerprint = identity.fingerprint_for("embedding")
    assert fingerprint is not None
    return ComputeLease(
        lease_id="lease-compute-9",
        job_id="job-embedding-9",
        project_id="project:plastic-promise",
        endpoint_id="compute-node",
        manifest_digest=_digest("d"),
        fencing_generation=9,
        capability="embedding",
        contract_version="embedding/v1",
        required_identity_fingerprint=fingerprint,
        result_schema="embedding-result/v1",
        idempotency_key=_digest("e"),
        issued_at=UTC_NOW,
        expires_at=datetime(2026, 8, 11, 1, 5, tzinfo=timezone.utc),
        input_schema="embedding-input/v1",
        capability_binding_fingerprint=_digest("f"),
    )


def _compute_result(*, identity: EndpointIdentityEvidence | None = None) -> ComputeResult:
    return ComputeResult(
        lease_id="lease-compute-9",
        endpoint_id="compute-node",
        fencing_generation=9,
        capability="embedding",
        contract_version="embedding/v1",
        identity=identity or _embedding_identity(),
        result_schema="embedding-result/v1",
        result_digest=_digest("1"),
        result_item_count=20,
        vector_dimension=2560,
        capability_binding_fingerprint=_digest("f"),
    )


def test_agent_task_queue_adapter_projects_only_persisted_lease_evidence() -> None:
    row = _task_row()
    lease = AgentTaskQueueLeaseAdapter.project_lease(row, _agent_evidence())
    fence = AgentTaskQueueLeaseAdapter.project_fence(
        row,
        lease,
        ServerFenceEvidence(
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            fencing_generation=lease.fencing_generation,
            observed_at=HEARTBEAT_AT,
        ),
    )
    heartbeat = AgentTaskQueueLeaseAdapter.project_heartbeat(
        row,
        lease,
        HeartbeatProjectionEvidence(
            heartbeat_id="heartbeat:task:2",
            sequence=2,
            sent_at=HEARTBEAT_AT,
        ),
    )
    decision = validate_lease_heartbeat(
        lease,
        fence,
        heartbeat,
        observed_at=HEARTBEAT_AT,
    )

    assert (decision.accepted, decision.reason_code) == (True, "lease_heartbeat_valid")
    assert lease.work_item.operation_kind == "implement"
    assert lease.owner_identity == _agent()
    assert lease.to_dict()["operation_policy"] == "adapter-owned"
    assert "title" not in lease.canonical_json()
    assert "payload" not in lease.canonical_json()


def test_agent_completion_requires_structured_receipt_persistence_and_exact_scope() -> None:
    row = _task_row(status="done")
    evidence = _agent_evidence()
    lease = AgentTaskQueueLeaseAdapter.project_lease(row, evidence)
    result = ResultReceipt.for_work(
        evidence.assignment,
        receipt_id="result-receipt:lease-adapter",
        submitted_by=_agent(),
        outcome="completed",
        summary="Adapter projected",
        submitted_at=DONE_AT,
        result={"tests": 4},
    )
    completion = AgentTaskQueueLeaseAdapter.project_completion(
        row,
        lease,
        result,
        completion_id="completion:task:7",
        persisted_result_receipt_sha256=result.content_sha256,
    )
    fence = AgentTaskQueueLeaseAdapter.project_fence(
        row,
        lease,
        ServerFenceEvidence(
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            fencing_generation=7,
            observed_at=DONE_AT,
        ),
    )
    decision = validate_lease_completion(
        lease,
        fence,
        completion,
        observed_at=DONE_AT,
    )

    assert (decision.accepted, decision.reason_code) == (True, "lease_completion_valid")
    assert decision.to_dict()["business_acceptance_effect"] == "none"
    with pytest.raises(
        CollaborationContractError,
        match="agent_task_result_receipt_binding_required",
    ):
        AgentTaskQueueLeaseAdapter.project_completion(
            row,
            lease,
            result,
            completion_id="completion:unbound",
            persisted_result_receipt_sha256=None,
        )
    with pytest.raises(
        CollaborationContractError,
        match="agent_task_result_owner_mismatch",
    ):
        forged = replace(
            result,
            submitted_by=AgentIdentity("agent:other", "implementer"),
        )
        AgentTaskQueueLeaseAdapter.project_completion(
            row,
            lease,
            forged,
            completion_id="completion:forged",
            persisted_result_receipt_sha256=forged.content_sha256,
        )


def test_current_task_queue_shape_fails_closed_on_missing_lease_state_and_naive_time() -> None:
    row = _task_row()
    with pytest.raises(
        CollaborationContractError,
        match="agent_task_lease_evidence_required",
    ):
        AgentTaskQueueLeaseAdapter.project_lease(row, None)

    naive = {**row, "created_at": "2026-08-11T00:59:00"}
    with pytest.raises(
        CollaborationContractError,
        match="agent_task_created_at_invalid",
    ):
        AgentTaskQueueLeaseAdapter.project_lease(naive, _agent_evidence())

    missing = dict(row)
    missing.pop("heartbeat_at")
    with pytest.raises(CollaborationContractError, match="agent_task_row_incomplete"):
        AgentTaskQueueLeaseAdapter.project_lease(missing, _agent_evidence())


def test_compute_adapter_projects_source_types_to_the_same_validation_seam() -> None:
    source = _compute_lease()
    lease = ComputeJobLeaseAdapter.project_lease(
        source,
        ComputeWorkEvidence(
            input_sha256=_digest("2"),
            work_created_at="2026-08-11T00:59:00Z",
            attempt=1,
            max_attempts=3,
        ),
    )
    fence = ComputeJobLeaseAdapter.project_fence(
        ComputeFence(job_id=source.job_id, fencing_generation=source.fencing_generation),
        lease,
        ServerFenceEvidence(
            lease_id=lease.lease_id,
            lease_sha256=lease.content_sha256,
            fencing_generation=lease.fencing_generation,
            observed_at=HEARTBEAT_AT,
        ),
    )
    heartbeat = ComputeJobLeaseAdapter.project_heartbeat(
        lease,
        HeartbeatProjectionEvidence(
            heartbeat_id="heartbeat:compute:2",
            sequence=2,
            sent_at=HEARTBEAT_AT,
        ),
    )
    completion = ComputeJobLeaseAdapter.project_completion(
        source,
        _compute_result(),
        lease,
        completion_id="completion:compute:9",
        completed_at=DONE_AT,
    )

    heartbeat_decision = validate_lease_heartbeat(
        lease,
        fence,
        heartbeat,
        observed_at=HEARTBEAT_AT,
    )
    completion_decision = validate_lease_completion(
        lease,
        fence,
        completion,
        observed_at=DONE_AT,
    )

    assert (heartbeat_decision.accepted, heartbeat_decision.reason_code) == (
        True,
        "lease_heartbeat_valid",
    )
    assert (completion_decision.accepted, completion_decision.reason_code) == (
        True,
        "lease_completion_valid",
    )
    assert lease.owner_identity is None
    assert completion.result_sha256 != _compute_result().result_digest
    assert completion.to_dict()["canonical_memory_effect"] == "none"


def test_compute_adapter_rejects_missing_durable_evidence_and_identity_drift() -> None:
    source = _compute_lease()
    with pytest.raises(CollaborationContractError, match="compute_work_evidence_required"):
        ComputeJobLeaseAdapter.project_lease(source, None)

    lease = ComputeJobLeaseAdapter.project_lease(
        source,
        ComputeWorkEvidence(
            input_sha256=_digest("2"),
            work_created_at="2026-08-11T00:59:00Z",
            attempt=1,
            max_attempts=3,
        ),
    )
    with pytest.raises(
        CollaborationContractError,
        match="compute_current_lease_binding_required",
    ):
        ComputeJobLeaseAdapter.project_fence(
            ComputeFence(job_id=source.job_id, fencing_generation=9),
            lease,
            None,
        )
    with pytest.raises(
        CollaborationContractError,
        match="compute_job_heartbeat_evidence_required",
    ):
        ComputeJobLeaseAdapter.project_heartbeat(lease, None)
    with pytest.raises(
        CollaborationContractError,
        match="compute_job_heartbeat_evidence_invalid",
    ):
        ComputeJobLeaseAdapter.project_heartbeat(lease, object())  # type: ignore[arg-type]
    with pytest.raises(CollaborationContractError, match="compute_result_identity_mismatch"):
        ComputeJobLeaseAdapter.project_completion(
            source,
            _compute_result(identity=_embedding_identity(golden="9")),
            lease,
            completion_id="completion:drifted",
            completed_at=DONE_AT,
        )
    with pytest.raises(
        CollaborationContractError,
        match="compute_shared_lease_projection_mismatch",
    ):
        ComputeJobLeaseAdapter.project_completion(
            source,
            _compute_result(),
            replace(lease, expires_at="2026-08-11T01:06:00Z"),
            completion_id="completion:lease-drift",
            completed_at=DONE_AT,
        )


def test_compute_timed_out_result_is_representable_without_becoming_agent_policy() -> None:
    source = _compute_lease()
    lease = ComputeJobLeaseAdapter.project_lease(
        source,
        ComputeWorkEvidence(
            input_sha256=_digest("2"),
            work_created_at="2026-08-11T00:59:00Z",
            attempt=1,
            max_attempts=3,
        ),
    )
    completion = ComputeJobLeaseAdapter.project_completion(
        source,
        replace(_compute_result(), terminal_reason="timed-out"),
        lease,
        completion_id="completion:timed-out",
        completed_at=DONE_AT,
    )

    assert completion.terminal_reason == "timed-out"
    assert completion.owner_kind == "compute"
    assert completion.policy_kind == "compute-job"
