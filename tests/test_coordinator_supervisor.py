from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from plastic_promise.collaboration.activity_update import (
    ActivityScope,
    ActivitySlice,
    AgentActivityUpdate,
    open_server_activity_audit_authority,
)
from plastic_promise.collaboration.contracts import ProjectScope
from plastic_promise.collaboration.coordinator_supervisor import (
    EVIDENCE_KINDS,
    CoordinatorActivityAuditReceipt,
    CoordinatorAuditAuthority,
    CoordinatorAuditError,
    CoordinatorDispatchError,
    CoordinatorSupervisor,
    EvidenceObservation,
    InMemoryCoordinatorAuditRepository,
    open_server_coordinator_audit_authority,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    COMPUTE_JOB_POLICY,
    COMPUTE_OWNER_KIND,
    WorkItem,
)

# Historical fixture time: preserve the August 11 contract evidence.
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:coordinator-supervisor")
OTHER_PROJECT = ProjectScope("project:other")
COORDINATION_SESSION_ID = "coord:coordinator-supervisor"
ROLE_ASSIGNMENT_SHA256 = "sha256:" + "a" * 64
CURRENT_PATH = "plastic_promise/collaboration/coordinator_supervisor.py"
PREVIOUS_PATH = "plastic_promise/collaboration/activity_update.py"
NEXT_PATH = "tests/test_coordinator_supervisor.py"

_ADAPTER_ARGUMENTS = {
    "lease": "lease_adapter",
    "event": "event_adapter",
    "git_diff": "git_diff_adapter",
    "result_receipt": "result_receipt_adapter",
}
_EVIDENCE_CHARACTERS = {
    "lease": "1",
    "event": "2",
    "git_diff": "3",
    "result_receipt": "4",
}


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _slice(scope: str, summary: str, path: str) -> ActivitySlice:
    return ActivitySlice(scope=scope, paths=(path,), summary=summary)


def _update(
    *,
    project: ProjectScope = PROJECT,
    coordination_session_id: str = COORDINATION_SESSION_ID,
    agent_session_id: str = "agent-session:builder",
    summary: str = "Coordinator progress update",
    current: str = "Implementing the bounded coordinator seam",
    blockers: tuple[str, ...] = (),
    work_item_id: str = "work:coordinator-supervisor",
    role_assignment_sha256: str = ROLE_ASSIGNMENT_SHA256,
    cursor: int = 3,
) -> AgentActivityUpdate:
    return AgentActivityUpdate(
        scope=ActivityScope(
            project=project,
            coordination_session_id=coordination_session_id,
            agent_session_id=agent_session_id,
            agent_id="agent:builder",
        ),
        role="implementer",
        summary=summary,
        previous=_slice(
            "activity contract inspection",
            "Inspected the activity contract",
            PREVIOUS_PATH,
        ),
        current=_slice("coordinator implementation", current, CURRENT_PATH),
        next=_slice("coordinator verification", "Run focused verification", NEXT_PATH),
        blockers=blockers,
        work_item_id=work_item_id,
        role_assignment_sha256=role_assignment_sha256,
        cursor=cursor,
    )


def _completed_update(**kwargs: object) -> AgentActivityUpdate:
    return _update(current="Completed the assigned WorkItem", **kwargs)  # type: ignore[arg-type]


def _work(
    work_item_id: str,
    *,
    project: ProjectScope = PROJECT,
    coordination_session_id: str | None = COORDINATION_SESSION_ID,
    owner_kind: str = AGENT_OWNER_KIND,
    policy_kind: str = AGENT_WORK_POLICY,
    input_character: str = "5",
) -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        project=project,
        owner_kind=owner_kind,
        policy_kind=policy_kind,
        operation_kind="implement" if owner_kind == AGENT_OWNER_KIND else "embedding",
        input_sha256=_digest(input_character),
        result_schema="collaboration-result/v1",
        created_at="2026-08-11T11:00:00Z",
        max_attempts=2,
        coordination_session_id=coordination_session_id,
    )


class BoundAdapter:
    def __init__(
        self,
        kind: str,
        *,
        status: str = "verified",
        proves_completion: bool = False,
        **overrides: object,
    ) -> None:
        self.kind = kind
        self.status = status
        self.proves_completion = proves_completion
        self.overrides = overrides

    def inspect(self, update, receipt) -> EvidenceObservation:
        values: dict[str, object] = {
            "status": self.status,
            "activity_update_sha256": receipt.activity_update_sha256,
            "activity_scope_sha256": receipt.activity_scope_sha256,
            "evidence_sha256": _digest(_EVIDENCE_CHARACTERS[self.kind]),
            "work_item_id": receipt.work_item_id,
            "role_assignment_sha256": receipt.role_assignment_sha256,
            "cursor": receipt.cursor,
            "observed_paths": update.evidence_paths if self.kind == "git_diff" else (),
            "proves_completion": self.proves_completion,
        }
        values.update(self.overrides)
        return EvidenceObservation(**values)  # type: ignore[arg-type]


class StaticAdapter:
    def __init__(self, observation: EvidenceObservation | None) -> None:
        self.observation = observation

    def inspect(self, _update, _receipt) -> EvidenceObservation | None:
        return self.observation


class FailingAdapter:
    def inspect(self, _update, _receipt) -> EvidenceObservation:
        raise RuntimeError("sensor unavailable")


class PartialAdapter:
    def inspect(self, _update, receipt) -> dict[str, object]:
        return {
            "status": "verified",
            "activity_update_sha256": receipt.activity_update_sha256,
            "activity_scope_sha256": receipt.activity_scope_sha256,
            "evidence_sha256": _digest("9"),
            "work_item_id": receipt.work_item_id,
            "role_assignment_sha256": receipt.role_assignment_sha256,
            # cursor intentionally absent
        }


def _default_adapters() -> dict[str, object]:
    return {argument: BoundAdapter(kind) for kind, argument in _ADAPTER_ARGUMENTS.items()}


def _supervisor(**kwargs: object) -> CoordinatorSupervisor:
    adapters = _default_adapters()
    for argument in _ADAPTER_ARGUMENTS.values():
        if argument in kwargs:
            adapters[argument] = kwargs.pop(argument)
    return CoordinatorSupervisor(
        project=PROJECT,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_authority=kwargs.pop(
            "activity_authority",
            open_server_activity_audit_authority(clock=lambda: NOW),
        ),
        **adapters,
        **kwargs,
    )


def _coordinator_authority(
    repository: InMemoryCoordinatorAuditRepository,
) -> CoordinatorAuditAuthority:
    return open_server_coordinator_audit_authority(
        project=PROJECT,
        coordination_session_id=COORDINATION_SESSION_ID,
        repository=repository,
        clock=lambda: NOW,
    )


def _completion_proof() -> tuple[
    CoordinatorSupervisor,
    AgentActivityUpdate,
    CoordinatorActivityAuditReceipt,
]:
    supervisor = _supervisor(
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        )
    )
    update = _completed_update()
    return supervisor, update, supervisor.audit_activity(update)


def _clone_receipt(
    receipt: CoordinatorActivityAuditReceipt,
    **overrides: object,
) -> CoordinatorActivityAuditReceipt:
    clone = object.__new__(CoordinatorActivityAuditReceipt)
    for field_name in CoordinatorActivityAuditReceipt.__dataclass_fields__:
        object.__setattr__(
            clone,
            field_name,
            overrides.get(field_name, getattr(receipt, field_name)),
        )
    return clone


def _dispatch(
    supervisor: CoordinatorSupervisor,
    update: AgentActivityUpdate,
    receipt: CoordinatorActivityAuditReceipt,
    work_items: list[WorkItem] | tuple[WorkItem, ...],
    *,
    dependency_callback=lambda _item: True,
    dispatch_callback=lambda _item: None,
) -> tuple[str, ...]:
    return supervisor.dispatch_eligible(
        audit_receipt=receipt,
        source_update=update,
        work_items=work_items,
        dependency_callback=dependency_callback,
        dispatch_callback=dispatch_callback,
    )


@pytest.mark.parametrize(("kind", "argument"), tuple(_ADAPTER_ARGUMENTS.items()))
def test_every_evidence_adapter_is_required_at_construction(
    kind: str,
    argument: str,
) -> None:
    with pytest.raises(CoordinatorAuditError, match=f"coordinator_{kind}_adapter_required"):
        _supervisor(**{argument: None})


def test_verified_audit_is_factory_issued_with_all_four_lineages() -> None:
    supervisor = _supervisor()
    update = _update()

    receipt = supervisor.audit_activity(update)

    assert receipt.status == "verified"
    assert receipt.evidence_kinds == EVIDENCE_KINDS
    assert len(receipt.evidence_sha256s) == 4
    assert receipt.completion_verified is False
    assert receipt.activity_update_sha256 == update.content_sha256
    assert receipt.to_dict()["activity_narrative"] == "omitted"
    assert receipt.to_dict()["canonical_memory_effect"] == "none"
    assert receipt.to_dict()["merge_effect"] == "none"
    assert receipt.to_dict()["deploy_effect"] == "none"
    with pytest.raises(CoordinatorAuditError, match="coordinator_audit_receipt_factory_required"):
        CoordinatorActivityAuditReceipt()
    with pytest.raises(FrozenInstanceError):
        receipt.status = "blocked"  # type: ignore[misc]


@pytest.mark.parametrize(("kind", "argument"), tuple(_ADAPTER_ARGUMENTS.items()))
def test_missing_observation_from_each_port_is_blocked(
    kind: str,
    argument: str,
) -> None:
    receipt = _supervisor(**{argument: StaticAdapter(None)}).audit_activity(_update())

    assert receipt.status == "blocked"
    assert receipt.reason_codes == (f"{kind}_evidence_missing",)
    assert kind not in receipt.evidence_kinds
    assert receipt.completion_verified is False


@pytest.mark.parametrize(("kind", "argument"), tuple(_ADAPTER_ARGUMENTS.items()))
def test_adapter_exception_from_each_port_is_blocked(
    kind: str,
    argument: str,
) -> None:
    receipt = _supervisor(**{argument: FailingAdapter()}).audit_activity(_update())

    assert receipt.status == "blocked"
    assert receipt.reason_codes == (f"{kind}_adapter_error",)
    assert kind not in receipt.evidence_kinds


def test_partial_observation_is_an_adapter_error_and_never_verified() -> None:
    receipt = _supervisor(lease_adapter=PartialAdapter()).audit_activity(_update())

    assert receipt.status == "blocked"
    assert receipt.reason_codes == ("lease_adapter_error",)
    assert "lease" not in receipt.evidence_kinds


@pytest.mark.parametrize(
    ("argument", "adapter", "reason"),
    [
        (
            "lease_adapter",
            BoundAdapter("lease", activity_update_sha256=_digest("b")),
            "lease_activity_digest_mismatch",
        ),
        (
            "event_adapter",
            BoundAdapter("event", activity_scope_sha256=_digest("c")),
            "event_scope_digest_mismatch",
        ),
        (
            "git_diff_adapter",
            BoundAdapter("git_diff", work_item_id="work:other"),
            "git_diff_work_item_mismatch",
        ),
        (
            "result_receipt_adapter",
            BoundAdapter("result_receipt", role_assignment_sha256=_digest("d")),
            "result_receipt_role_assignment_digest_mismatch",
        ),
        (
            "lease_adapter",
            BoundAdapter("lease", cursor=2),
            "lease_cursor_unverified",
        ),
    ],
)
def test_every_evidence_binding_must_match_exactly(
    argument: str,
    adapter: object,
    reason: str,
) -> None:
    receipt = _supervisor(**{argument: adapter}).audit_activity(_update())

    assert receipt.status == "mismatch"
    assert reason in receipt.reason_codes
    assert receipt.completion_verified is False


def test_observation_ahead_of_activity_cursor_is_stale() -> None:
    receipt = _supervisor(event_adapter=BoundAdapter("event", cursor=4)).audit_activity(
        _update(cursor=3)
    )

    assert receipt.status == "stale"
    assert receipt.reason_codes == ("activity_cursor_stale",)


def test_status_precedence_is_mismatch_then_overlap_then_stale_then_blocked() -> None:
    receipt = _supervisor(
        lease_adapter=BoundAdapter("lease", status="blocked"),
        event_adapter=BoundAdapter("event", status="stale"),
        git_diff_adapter=BoundAdapter("git_diff", status="overlap"),
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            activity_update_sha256=_digest("e"),
        ),
    ).audit_activity(_update())

    assert receipt.status == "mismatch"
    assert receipt.reason_codes == ("result_receipt_activity_digest_mismatch",)


def test_git_diff_compares_evidence_paths_and_ignores_planned_paths() -> None:
    update = _update()
    verified = _supervisor().audit_activity(update)
    mismatched = _supervisor(
        git_diff_adapter=BoundAdapter(
            "git_diff",
            observed_paths=(CURRENT_PATH, PREVIOUS_PATH, NEXT_PATH),
        )
    ).audit_activity(update)

    assert update.evidence_paths == tuple(sorted((CURRENT_PATH, PREVIOUS_PATH)))
    assert update.planned_paths == (NEXT_PATH,)
    assert verified.status == "verified"
    assert mismatched.status == "mismatch"
    assert mismatched.reason_codes == ("git_diff_evidence_paths_mismatch",)


def test_completion_is_never_inferred_from_narrative() -> None:
    receipt = _supervisor().audit_activity(_completed_update())

    assert receipt.status == "blocked"
    assert receipt.reason_codes == ("completion_evidence_required",)
    assert receipt.completion_verified is False


def test_only_matching_result_receipt_evidence_can_verify_completion() -> None:
    _supervisor_value, _update_value, receipt = _completion_proof()

    assert receipt.status == "verified"
    assert receipt.completion_verified is True


def test_non_result_evidence_cannot_claim_completion() -> None:
    receipt = _supervisor(
        lease_adapter=BoundAdapter("lease", proves_completion=True),
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        ),
    ).audit_activity(_completed_update())

    assert receipt.status == "mismatch"
    assert receipt.reason_codes == ("lease_completion_proof_forbidden",)
    assert receipt.completion_verified is False


def test_result_evidence_with_wrong_binding_cannot_verify_completion() -> None:
    receipt = _supervisor(
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            activity_update_sha256=_digest("f"),
            proves_completion=True,
        )
    ).audit_activity(_completed_update())

    assert receipt.status == "mismatch"
    assert receipt.reason_codes == ("result_receipt_activity_digest_mismatch",)
    assert receipt.completion_verified is False


def test_reported_blocker_tightens_otherwise_verified_evidence() -> None:
    receipt = _supervisor().audit_activity(_update(blockers=("Waiting for the owner",)))

    assert receipt.status == "blocked"
    assert receipt.reason_codes == ("activity_reported_blockers",)


def test_reusing_one_evidence_digest_across_ports_is_a_mismatch() -> None:
    receipt = _supervisor(
        event_adapter=BoundAdapter("event", evidence_sha256=_digest("1"))
    ).audit_activity(_update())

    assert receipt.status == "mismatch"
    assert receipt.reason_codes == ("evidence_digest_reused",)


def test_exact_canonical_copy_is_accepted_but_shape_only_forgery_is_not() -> None:
    supervisor, update, receipt = _completion_proof()
    portable_copy = _clone_receipt(receipt)
    calls: list[str] = []

    assert _dispatch(
        supervisor,
        update,
        portable_copy,
        [_work("work:next")],
        dependency_callback=lambda item: True,
        dispatch_callback=lambda item: calls.append(item.work_item_id),
    ) == ("work:next",)
    assert calls == ["work:next"]

    foreign_repository = InMemoryCoordinatorAuditRepository()
    foreign_supervisor = _supervisor(
        coordinator_authority=_coordinator_authority(foreign_repository),
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        ),
    )
    forged = _clone_receipt(receipt)
    with pytest.raises(CoordinatorDispatchError, match="not_server_issued"):
        _dispatch(foreign_supervisor, update, forged, [_work("work:other")])

    assert calls == ["work:next"]


def test_foreign_authority_receipt_cannot_authorize_callbacks() -> None:
    repository = InMemoryCoordinatorAuditRepository()
    authority = _coordinator_authority(repository)
    issuer = _supervisor(
        coordinator_authority=authority,
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        ),
    )
    update = _completed_update()
    receipt = issuer.audit_activity(update)
    foreign = CoordinatorSupervisor(
        project=OTHER_PROJECT,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_authority=open_server_activity_audit_authority(clock=lambda: NOW),
        coordinator_authority=open_server_coordinator_audit_authority(
            project=OTHER_PROJECT,
            coordination_session_id=COORDINATION_SESSION_ID,
            repository=repository,
            clock=lambda: NOW,
        ),
        lease_adapter=BoundAdapter("lease"),
        event_adapter=BoundAdapter("event"),
        git_diff_adapter=BoundAdapter("git_diff"),
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        ),
    )
    calls: list[str] = []

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_audit_receipt_foreign_authority",
    ):
        _dispatch(
            foreign,
            update,
            receipt,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_tampered_issued_receipt_cannot_authorize_callbacks() -> None:
    supervisor, update, receipt = _completion_proof()
    object.__setattr__(receipt, "audit_generation", receipt.audit_generation + 1)
    calls: list[str] = []

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_audit_receipt_tampered",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_wrong_source_update_cannot_authorize_callbacks() -> None:
    supervisor, _update_value, receipt = _completion_proof()
    wrong_update = _completed_update(
        agent_session_id="agent-session:other",
        work_item_id="work:other",
        cursor=1,
    )
    calls: list[str] = []

    with pytest.raises(CoordinatorDispatchError, match="update"):
        _dispatch(
            supervisor,
            wrong_update,
            receipt,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_superseded_receipt_cannot_authorize_callbacks() -> None:
    supervisor = _supervisor(
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        )
    )
    update = _completed_update()
    first = supervisor.audit_activity(update)
    second = supervisor.audit_activity(update)
    calls: list[str] = []

    assert first is not second
    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_audit_receipt_superseded",
    ):
        _dispatch(
            supervisor,
            update,
            first,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )
    assert calls == []
    assert _dispatch(supervisor, update, second, [_work("work:next")]) == ("work:next",)


def test_verified_non_completion_receipt_cannot_unlock_downstream_work() -> None:
    supervisor = _supervisor()
    update = _update()
    receipt = supervisor.audit_activity(update)
    calls: list[str] = []

    assert receipt.status == "verified"
    assert receipt.completion_verified is False
    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_verified_completion_required",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )
    assert calls == []


def test_nonverified_receipt_cannot_unlock_downstream_work() -> None:
    supervisor = _supervisor(lease_adapter=StaticAdapter(None))
    update = _completed_update()
    receipt = supervisor.audit_activity(update)
    calls: list[str] = []

    assert receipt.status == "blocked"
    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_verified_audit_required",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:next")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )
    assert calls == []


def test_dispatch_checks_exact_true_dependencies_after_proof_consumption() -> None:
    supervisor, update, receipt = _completion_proof()
    ready = _work("work:ready")
    truthy = _work("work:truthy")
    blocked = _work("work:blocked")
    dependency_checks: list[str] = []
    dispatched: list[str] = []

    def dependencies_ready(item: WorkItem) -> object:
        dependency_checks.append(item.work_item_id)
        return {
            ready.work_item_id: True,
            truthy.work_item_id: 1,
            blocked.work_item_id: False,
        }[item.work_item_id]

    result = _dispatch(
        supervisor,
        update,
        receipt,
        [ready, truthy, blocked],
        dependency_callback=dependencies_ready,
        dispatch_callback=lambda item: dispatched.append(item.work_item_id),
    )

    assert result == (ready.work_item_id,)
    assert dependency_checks == [ready.work_item_id, truthy.work_item_id, blocked.work_item_id]
    assert dispatched == [ready.work_item_id]


def test_compute_cross_scope_and_source_work_are_skipped() -> None:
    supervisor, update, receipt = _completion_proof()
    ready = _work("work:ready")
    wrong_project = _work("work:wrong-project", project=OTHER_PROJECT)
    wrong_session = _work("work:wrong-session", coordination_session_id="coord:other")
    compute = _work(
        "job:compute",
        coordination_session_id=None,
        owner_kind=COMPUTE_OWNER_KIND,
        policy_kind=COMPUTE_JOB_POLICY,
    )
    source = _work(update.work_item_id)
    dependency_checks: list[str] = []

    result = _dispatch(
        supervisor,
        update,
        receipt,
        [wrong_project, wrong_session, compute, source, ready],
        dependency_callback=lambda item: dependency_checks.append(item.work_item_id) or True,
    )

    assert result == (ready.work_item_id,)
    assert dependency_checks == [ready.work_item_id]


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_or_conflicting_work_ids_fail_before_callbacks(conflict: bool) -> None:
    supervisor, update, receipt = _completion_proof()
    first = _work("work:duplicate", input_character="6")
    second = _work(
        "work:duplicate",
        input_character="7" if conflict else "6",
    )
    calls: list[str] = []
    expected = "conflict" if conflict else "duplicate"

    with pytest.raises(
        CoordinatorDispatchError,
        match=f"coordinator_dispatch_work_item_{expected}",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [first, second],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_dispatch_batch_bound_fails_before_callbacks() -> None:
    supervisor = _supervisor(
        result_receipt_adapter=BoundAdapter(
            "result_receipt",
            proves_completion=True,
        ),
        max_work_items=2,
    )
    update = _completed_update()
    receipt = supervisor.audit_activity(update)
    calls: list[str] = []

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_dispatch_batch_too_large",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:one"), _work("work:two"), _work("work:three")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_exact_receipt_is_one_shot_and_replay_calls_neither_callback() -> None:
    supervisor, update, receipt = _completion_proof()
    assert _dispatch(supervisor, update, receipt, [_work("work:first")]) == ("work:first",)
    calls: list[str] = []

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_audit_receipt_replayed",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:second")],
            dependency_callback=lambda item: calls.append(item.work_item_id) or True,
            dispatch_callback=lambda item: calls.append(item.work_item_id),
        )

    assert calls == []


def test_dispatch_failure_is_visible_and_receipt_remains_consumed() -> None:
    supervisor, update, receipt = _completion_proof()

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_dispatch_callback_failed",
    ):
        _dispatch(
            supervisor,
            update,
            receipt,
            [_work("work:ready")],
            dispatch_callback=lambda _item: (_ for _ in ()).throw(RuntimeError("boom")),
        )

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_audit_receipt_replayed",
    ):
        _dispatch(supervisor, update, receipt, [_work("work:retry")])
