from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    CollaborationContractError,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    COMPUTE_JOB_POLICY,
    COMPUTE_OWNER_KIND,
    LeaseCompletion,
    LeaseFence,
    LeaseHeartbeat,
    WorkItem,
    WorkLease,
    validate_lease_completion,
    validate_lease_heartbeat,
)

NOW = "2026-08-11T01:00:00Z"
LATER = "2026-08-11T01:05:00Z"
TOO_LATE = "2026-08-11T01:05:01Z"


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _agent() -> AgentIdentity:
    return AgentIdentity("agent:builder", "implementer")


def _agent_work() -> WorkItem:
    return WorkItem(
        work_item_id="work:contracts",
        project=ProjectScope("project:plastic-promise"),
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256=_digest("a"),
        result_schema="collaboration-result/v1",
        created_at=NOW,
        max_attempts=2,
        coordination_session_id="coord:pr1",
    )


def _agent_assignment(work: WorkItem, agent: AgentIdentity) -> WorkReceipt:
    return WorkReceipt(
        receipt_id="work-receipt:contracts",
        work_item_id=work.work_item_id,
        project=work.project,
        coordination_session_id="coord:pr1",
        assigned_agent=agent,
        objective="Implement the shared immutable lease contract",
        fencing_generation=7,
        issued_at=NOW,
        expires_at=LATER,
    )


def _agent_lease() -> tuple[WorkLease, WorkReceipt]:
    work = _agent_work()
    agent = _agent()
    assignment = _agent_assignment(work, agent)
    return (
        WorkLease(
            lease_id="lease:contracts:7",
            work_item=work,
            owner_kind=AGENT_OWNER_KIND,
            policy_kind=AGENT_WORK_POLICY,
            owner_id=agent.agent_id,
            owner_identity=agent,
            fencing_generation=7,
            attempt=1,
            issued_at=NOW,
            expires_at=LATER,
            result_binding_sha256=assignment.content_sha256,
            idempotency_key_sha256=_digest("b"),
        ),
        assignment,
    )


def _compute_lease() -> WorkLease:
    work = WorkItem(
        work_item_id="job:embedding:1",
        project=ProjectScope("project:plastic-promise"),
        owner_kind=COMPUTE_OWNER_KIND,
        policy_kind=COMPUTE_JOB_POLICY,
        operation_kind="embedding",
        input_sha256=_digest("c"),
        result_schema="embedding-result/v1",
        created_at=NOW,
        max_attempts=3,
    )
    return WorkLease(
        lease_id="lease:embedding:9",
        work_item=work,
        owner_kind=COMPUTE_OWNER_KIND,
        policy_kind=COMPUTE_JOB_POLICY,
        owner_id="endpoint:gpu-node",
        fencing_generation=9,
        attempt=2,
        issued_at=NOW,
        expires_at=LATER,
        result_binding_sha256=_digest("d"),
        idempotency_key_sha256=_digest("e"),
    )


def test_agent_and_compute_adapters_share_one_deep_lease_interface_without_sharing_policy() -> None:
    agent_lease, _ = _agent_lease()
    compute_lease = _compute_lease()

    assert agent_lease.to_dict()["operation_policy"] == "adapter-owned"
    assert compute_lease.to_dict()["operation_policy"] == "adapter-owned"
    assert agent_lease.to_dict()["authority_effect"] == "none"
    assert compute_lease.to_dict()["authority_effect"] == "none"
    assert agent_lease.owner_identity == _agent()
    assert compute_lease.owner_identity is None
    assert agent_lease.project == compute_lease.project
    assert agent_lease.content_sha256.startswith("sha256:")
    assert agent_lease.canonical_json() == replace(agent_lease).canonical_json()
    with pytest.raises(FrozenInstanceError):
        agent_lease.owner_id = "agent:other"  # type: ignore[misc]

    with pytest.raises(CollaborationContractError, match="lease_owner_policy_pair_invalid"):
        replace(compute_lease.work_item, policy_kind=AGENT_WORK_POLICY)
    with pytest.raises(
        CollaborationContractError,
        match="agent_work_coordination_session_required",
    ):
        replace(agent_lease.work_item, coordination_session_id=None)
    with pytest.raises(
        CollaborationContractError,
        match="compute_job_coordination_session_forbidden",
    ):
        replace(compute_lease.work_item, coordination_session_id="coord:forged")
    with pytest.raises(
        CollaborationContractError,
        match="compute_lease_agent_identity_forbidden",
    ):
        replace(compute_lease, owner_identity=_agent())
    with pytest.raises(CollaborationContractError, match="lease_attempt_exhausted"):
        replace(compute_lease, attempt=4)


def test_contracts_fail_closed_on_secrets_invalid_time_and_unbound_identity() -> None:
    lease, _ = _agent_lease()
    with pytest.raises(CollaborationContractError, match="lease_owner_id_invalid"):
        replace(lease, owner_id="github_pat_abcdefghijklmnopqrstuvwxyz012345")
    with pytest.raises(CollaborationContractError, match="lease_expiry_invalid"):
        replace(lease, expires_at=NOW)
    with pytest.raises(CollaborationContractError, match="lease_agent_identity_mismatch"):
        replace(lease, owner_id="agent:other")
    with pytest.raises(CollaborationContractError, match="lease_result_binding_sha256_invalid"):
        replace(lease, result_binding_sha256="raw-result-contract")
    with pytest.raises(CollaborationContractError, match="work_created_at_invalid"):
        replace(lease.work_item, created_at="2026-08-11 01:00:00")


def test_agent_result_receipt_is_bound_to_project_work_owner_and_assignment() -> None:
    lease, assignment = _agent_lease()
    result = ResultReceipt.for_work(
        assignment,
        receipt_id="result-receipt:contracts",
        submitted_by=_agent(),
        outcome="completed",
        summary="Lease contracts implemented",
        submitted_at="2026-08-11T01:04:00Z",
        evidence_refs=("test:lease-contract",),
        result={"tests": 6},
    )
    completion = LeaseCompletion.for_agent_result(
        lease,
        result,
        completion_id="completion:contracts",
    )
    fence = LeaseFence.for_lease(lease, observed_at="2026-08-11T01:04:00Z")

    decision = validate_lease_completion(
        lease,
        fence,
        completion,
        observed_at="2026-08-11T01:04:00Z",
    )

    assert (decision.accepted, decision.retryable, decision.reconcile_required) == (
        True,
        False,
        False,
    )
    assert decision.reason_code == "lease_completion_valid"
    assert completion.result_sha256 == result.content_sha256
    assert completion.result_binding_sha256 == assignment.content_sha256
    assert completion.to_dict()["canonical_memory_effect"] == "none"
    assert decision.to_dict()["business_acceptance_effect"] == "none"

    forged_result = replace(result, submitted_by=AgentIdentity("agent:other", "implementer"))
    forged_completion = replace(
        completion,
        agent_result_receipt=forged_result,
        result_sha256=forged_result.content_sha256,
    )
    forged = validate_lease_completion(
        lease,
        fence,
        forged_completion,
        observed_at="2026-08-11T01:04:00Z",
    )
    assert (forged.accepted, forged.retryable, forged.reconcile_required) == (
        False,
        False,
        True,
    )
    assert forged.reason_code == "lease_result_receipt_owner_mismatch"


def test_compute_completion_uses_same_fence_expiry_and_result_binding_contract() -> None:
    lease = _compute_lease()
    fence = LeaseFence.for_lease(lease, observed_at="2026-08-11T01:04:00Z")
    completion = LeaseCompletion.for_compute_result(
        lease,
        completion_id="completion:embedding:1",
        result_sha256=_digest("f"),
        terminal_reason="completed",
        completed_at="2026-08-11T01:04:00Z",
    )

    accepted = validate_lease_completion(
        lease,
        fence,
        completion,
        observed_at="2026-08-11T01:04:00Z",
    )
    stale = validate_lease_completion(
        lease,
        replace(fence, fencing_generation=10),
        completion,
        observed_at="2026-08-11T01:04:00Z",
    )
    expired = validate_lease_completion(
        lease,
        fence,
        completion,
        observed_at=TOO_LATE,
    )
    drifted = validate_lease_completion(
        lease,
        fence,
        replace(completion, result_binding_sha256=_digest("0")),
        observed_at="2026-08-11T01:04:00Z",
    )

    assert (accepted.accepted, accepted.reason_code) == (True, "lease_completion_valid")
    assert (stale.retryable, stale.reconcile_required, stale.reason_code) == (
        True,
        True,
        "lease_fencing_stale",
    )
    assert (expired.retryable, expired.reconcile_required, expired.reason_code) == (
        True,
        True,
        "lease_expired",
    )
    assert (drifted.retryable, drifted.reconcile_required, drifted.reason_code) == (
        False,
        True,
        "lease_result_binding_mismatch",
    )


def test_heartbeat_is_incremental_body_free_and_requires_the_current_fence() -> None:
    lease = _compute_lease()
    fence = LeaseFence.for_lease(lease, observed_at="2026-08-11T01:02:00Z")
    heartbeat = LeaseHeartbeat.for_lease(
        lease,
        heartbeat_id="heartbeat:embedding:4",
        sequence=4,
        sent_at="2026-08-11T01:02:00Z",
    )

    accepted = validate_lease_heartbeat(
        lease,
        fence,
        heartbeat,
        observed_at="2026-08-11T01:02:01Z",
    )
    stale = validate_lease_heartbeat(
        lease,
        replace(fence, lease_sha256=_digest("0")),
        heartbeat,
        observed_at="2026-08-11T01:02:01Z",
    )
    expired = validate_lease_heartbeat(
        lease,
        fence,
        heartbeat,
        observed_at=TOO_LATE,
    )

    assert (accepted.accepted, accepted.reason_code) == (True, "lease_heartbeat_valid")
    assert (stale.retryable, stale.reconcile_required, stale.reason_code) == (
        False,
        True,
        "lease_digest_mismatch",
    )
    assert (expired.retryable, expired.reconcile_required, expired.reason_code) == (
        True,
        True,
        "lease_expired",
    )
    assert set(heartbeat.to_dict()).isdisjoint({"payload", "result", "credential", "token"})


def test_source_clock_cannot_override_server_observation() -> None:
    lease = _compute_lease()
    fence = LeaseFence.for_lease(lease, observed_at=NOW)
    heartbeat = LeaseHeartbeat.for_lease(
        lease,
        heartbeat_id="heartbeat:future",
        sequence=1,
        sent_at="2026-08-11T01:01:00Z",
    )
    completion = LeaseCompletion.for_compute_result(
        lease,
        completion_id="completion:future",
        result_sha256=_digest("f"),
        terminal_reason="completed",
        completed_at="2026-08-11T01:01:00Z",
    )

    heartbeat_decision = validate_lease_heartbeat(lease, fence, heartbeat, observed_at=NOW)
    completion_decision = validate_lease_completion(lease, fence, completion, observed_at=NOW)
    assert heartbeat_decision.accepted is True
    assert completion_decision.accepted is True

    future_fence = replace(fence, observed_at="2026-08-11T01:02:00Z")
    with pytest.raises(CollaborationContractError, match="heartbeat_observed_before_fence"):
        validate_lease_heartbeat(
            lease,
            future_fence,
            heartbeat,
            observed_at="2026-08-11T01:01:00Z",
        )


def test_source_clock_skew_cannot_extend_or_expire_a_server_lease() -> None:
    lease = _compute_lease()
    fence = LeaseFence.for_lease(lease, observed_at="2026-08-11T01:02:00Z")
    future_heartbeat = LeaseHeartbeat.for_lease(
        lease,
        heartbeat_id="heartbeat:future-diagnostic",
        sequence=1,
        sent_at="2036-08-11T01:02:00Z",
    )
    past_completion = LeaseCompletion.for_compute_result(
        lease,
        completion_id="completion:past-diagnostic",
        result_sha256=_digest("f"),
        terminal_reason="completed",
        completed_at="2020-08-11T01:02:00Z",
    )

    heartbeat = validate_lease_heartbeat(
        lease,
        fence,
        future_heartbeat,
        observed_at="2026-08-11T01:02:01Z",
    )
    completion = validate_lease_completion(
        lease,
        fence,
        past_completion,
        observed_at="2026-08-11T01:02:01Z",
    )
    expired = validate_lease_heartbeat(
        lease,
        fence,
        replace(future_heartbeat, sent_at="2099-08-11T01:02:00Z"),
        observed_at=TOO_LATE,
    )

    assert heartbeat.reason_code == "lease_heartbeat_valid"
    assert completion.reason_code == "lease_completion_valid"
    assert expired.reason_code == "lease_expired"


def test_lease_source_timestamps_still_reject_naive_values() -> None:
    lease = _compute_lease()
    with pytest.raises(CollaborationContractError, match="heartbeat_sent_at_invalid"):
        LeaseHeartbeat.for_lease(
            lease,
            heartbeat_id="heartbeat:naive",
            sequence=1,
            sent_at="2026-08-11T01:02:00",
        )
    with pytest.raises(CollaborationContractError, match="completion_completed_at_invalid"):
        LeaseCompletion.for_compute_result(
            lease,
            completion_id="completion:naive",
            result_sha256=_digest("f"),
            terminal_reason="completed",
            completed_at="2026-08-11T01:02:00",
        )
