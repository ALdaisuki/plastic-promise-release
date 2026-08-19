from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    ProjectScope,
)
from plastic_promise.collaboration.coordination_plan import (
    TOKEN_AUTHORITY_PROVIDER,
    TOKEN_AUTHORITY_UNAVAILABLE,
    CoordinationPlan,
    CoordinationPlanActivation,
    CoordinationPlanAuthority,
    CoordinationPlanError,
    DelegationEdge,
    InMemoryCoordinationPlanRepository,
    ResourceAllocation,
    ResourceUsageReceipt,
    ResponsibilityNode,
    TopLevelAgentBindingAuthority,
    UserMandate,
    VerifiedCoordinationPlan,
)

NOW = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:coordination-plan")


def _time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _mandate() -> UserMandate:
    return UserMandate(
        mandate_id="mandate:one",
        project=PROJECT,
        coordination_session_id="coord:one",
        user_instruction_sha256="sha256:" + "1" * 64,
        objective="Complete one bounded collaboration program",
        constraints=("Only the Top-Level Agent may activate a successor plan",),
        issued_at_utc=_time(NOW),
        expires_at_utc=_time(NOW + timedelta(hours=4)),
    )


def _allocation(slots: int, tokens: int | None = None) -> ResourceAllocation:
    return ResourceAllocation(
        agent_slots=slots,
        token_budget=tokens,
        token_budget_authority=(
            TOKEN_AUTHORITY_PROVIDER if tokens is not None else TOKEN_AUTHORITY_UNAVAILABLE
        ),
    )


def _node(
    suffix: str,
    *,
    scope: str,
    paths: tuple[str, ...],
    tools: tuple[str, ...],
    allocation: ResourceAllocation,
    can_delegate: bool,
) -> ResponsibilityNode:
    return ResponsibilityNode(
        node_id=f"node:{suffix}",
        work_item_id=f"work:{suffix}",
        role_intent=f"role.{suffix}",
        scope=scope,
        allowed_paths=paths,
        allowed_tools=tools,
        acceptance_conditions=(f"{suffix} receipt accepted",),
        allocation=allocation,
        can_delegate=can_delegate,
    )


def _plan(*, tokens: bool = True) -> CoordinationPlan:
    root_allocation = _allocation(5, 100_000) if tokens else _allocation(5)
    reviewer_allocation = _allocation(2, 30_000) if tokens else _allocation(2)
    worker_allocation = _allocation(1, 20_000) if tokens else _allocation(1)
    nested_allocation = _allocation(1, 10_000) if tokens else _allocation(1)
    nodes = (
        _node(
            "root",
            scope="Own the user mandate and final acceptance",
            paths=(".",),
            tools=("collaboration.plan", "git.read", "review.read"),
            allocation=root_allocation,
            can_delegate=True,
        ),
        _node(
            "review",
            scope="Coordinate independent review",
            paths=("plastic_promise/", "tests/"),
            tools=("git.read", "review.read"),
            allocation=reviewer_allocation,
            can_delegate=True,
        ),
        _node(
            "implementation",
            scope="Implement the bounded runtime slice",
            paths=("plastic_promise/collaboration/",),
            tools=("git.read",),
            allocation=worker_allocation,
            can_delegate=False,
        ),
        _node(
            "spec",
            scope="Perform the independent specification review",
            paths=("plastic_promise/", "tests/"),
            tools=("review.read",),
            allocation=nested_allocation,
            can_delegate=False,
        ),
    )
    return CoordinationPlan(
        plan_id="plan:one",
        plan_revision=1,
        mandate=_mandate(),
        top_level_agent_session_id="agent-session:root",
        root_node_id="node:root",
        nodes=nodes,
        edges=(
            DelegationEdge("node:root", "node:review"),
            DelegationEdge("node:root", "node:implementation"),
            DelegationEdge("node:review", "node:spec"),
        ),
        total_allocation=root_allocation,
        created_at_utc=_time(NOW + timedelta(seconds=1)),
        expires_at_utc=_time(NOW + timedelta(hours=3)),
    )


def _session(
    session_id: str,
    *,
    project: ProjectScope = PROJECT,
    coordination_session_id: str = "coord:one",
    state: str = "active",
    expires_at: datetime | None = None,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=AgentIdentity(
            agent_id=session_id.replace("agent-session", "agent"),
            role="participant",
        ),
        project=project,
        coordination_session_id=coordination_session_id,
        state=state,
        started_at=_time(NOW - timedelta(minutes=1)),
        last_heartbeat_at=_time(NOW),
        expires_at=_time(expires_at or NOW + timedelta(hours=2)),
    )


class _TrustedTopLevelAuthorizationVerifier:
    """Test-only stand-in for the server's authenticated user-authority seam."""

    def authorize(self, *, mandate: UserMandate, session: AgentSession) -> None:
        if (
            session.session_id != "agent-session:root"
            or session.project != mandate.project
            or session.coordination_session_id != mandate.coordination_session_id
        ):
            raise CoordinationPlanError("coordination_plan_top_level_user_authorization_rejected")


def _binding_authority(
    repository: InMemoryCoordinationPlanRepository,
) -> TopLevelAgentBindingAuthority:
    return TopLevelAgentBindingAuthority(
        repository=repository,
        authorization_verifier=_TrustedTopLevelAuthorizationVerifier(),
        clock=lambda: NOW + timedelta(seconds=5),
    )


class _RejectingTopLevelAuthorizationVerifier:
    def authorize(self, *, mandate: UserMandate, session: AgentSession) -> None:
        del mandate, session
        raise RuntimeError("trusted-verifier-rejected")


def _authority() -> tuple[
    CoordinationPlanAuthority,
    InMemoryCoordinationPlanRepository,
]:
    repository = InMemoryCoordinationPlanRepository(clock=lambda: NOW + timedelta(seconds=5))
    repository.register_session(_session("agent-session:root"))
    repository.register_session(_session("agent-session:worker"))
    _binding_authority(repository).bind_top_level_session(
        _mandate(),
        agent_session_id="agent-session:root",
    )
    return CoordinationPlanAuthority(
        repository=repository, clock=lambda: NOW + timedelta(seconds=5)
    ), repository


def test_top_level_plan_controls_agent_count_scope_and_provider_token_budget() -> None:
    plan = _plan()

    assert plan.content_sha256.startswith("sha256:")
    assert plan.to_dict()["frozen"] is True
    root = plan.delegation_envelope("node:root")
    assert root.child_node_ids == ("node:implementation", "node:review")
    assert root.allocated_agent_slots == 3
    assert root.remaining_agent_slots == 1
    assert root.allocated_token_budget == 50_000
    assert root.remaining_token_budget == 50_000
    review = plan.delegation_envelope("node:review")
    assert review.allocated_agent_slots == 1
    assert review.remaining_agent_slots == 0
    assert review.remaining_token_budget == 20_000


def test_unavailable_provider_budget_never_fabricates_token_numbers() -> None:
    plan = _plan(tokens=False)

    envelope = plan.delegation_envelope("node:root")
    assert envelope.allocated_token_budget is None
    assert envelope.remaining_token_budget is None
    assert envelope.token_budget_authority == TOKEN_AUTHORITY_UNAVAILABLE
    with pytest.raises(CoordinationPlanError, match="resource_token_budget_unverifiable"):
        ResourceAllocation(agent_slots=1, token_budget=100, token_budget_authority="unavailable")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda plan: replace(
                plan,
                edges=plan.edges + (DelegationEdge("node:spec", "node:root"),),
            ),
            "coordination_plan_edge_count_invalid",
        ),
        (
            lambda plan: replace(
                plan,
                edges=(
                    DelegationEdge("node:root", "node:review"),
                    DelegationEdge("node:implementation", "node:spec"),
                    DelegationEdge("node:spec", "node:implementation"),
                ),
            ),
            "coordination_plan_cycle",
        ),
        (
            lambda plan: replace(
                plan,
                nodes=tuple(
                    replace(node, can_delegate=False) if node.node_id == "node:review" else node
                    for node in plan.nodes
                ),
            ),
            "coordination_plan_delegation_forbidden",
        ),
    ],
)
def test_plan_rejects_unplanned_recursive_shapes(mutation, code: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CoordinationPlanError, match=code):
        mutation(_plan())


def test_child_cannot_expand_agent_token_path_or_tool_budget() -> None:
    plan = _plan()
    review = plan.node("node:review")

    mutations = (
        replace(review, allocation=_allocation(5, 30_000)),
        replace(review, allocation=_allocation(2, 101_000)),
        replace(review, allowed_paths=("deploy/",)),
        replace(review, allowed_tools=("review.read", "shell.write")),
    )
    codes = (
        "coordination_plan_agent_budget_exceeded",
        "coordination_plan_token_budget_exceeded",
        "coordination_plan_path_scope_escalation",
        "coordination_plan_tool_scope_escalation",
    )
    for replacement, code in zip(mutations, codes, strict=True):
        nodes = tuple(replacement if node.node_id == "node:review" else node for node in plan.nodes)
        with pytest.raises(CoordinationPlanError, match=code):
            replace(plan, nodes=nodes)


def test_duplicate_responsibility_fingerprint_is_rejected_even_with_new_ids() -> None:
    plan = _plan()
    implementation = plan.node("node:implementation")
    duplicate = replace(
        implementation,
        node_id="node:duplicate",
        work_item_id="work:duplicate",
    )

    with pytest.raises(CoordinationPlanError, match="coordination_plan_responsibility_duplicate"):
        replace(
            plan,
            nodes=plan.nodes + (duplicate,),
            edges=plan.edges + (DelegationEdge("node:root", "node:duplicate"),),
        )


def test_only_exact_successor_of_same_mandate_and_top_level_agent_is_valid() -> None:
    previous = _plan()
    successor = replace(
        previous,
        plan_revision=2,
        created_at_utc=_time(NOW + timedelta(seconds=2)),
        supersedes_plan_sha256=previous.content_sha256,
    )
    successor.validate_successor(previous)

    for candidate, code in (
        (
            replace(successor, top_level_agent_session_id="agent-session:child"),
            "coordination_plan_successor_top_level_mismatch",
        ),
        (
            replace(successor, supersedes_plan_sha256="sha256:" + "f" * 64),
            "coordination_plan_successor_digest_mismatch",
        ),
        (
            replace(successor, plan_revision=3),
            "coordination_plan_successor_revision_invalid",
        ),
    ):
        with pytest.raises(CoordinationPlanError, match=code):
            candidate.validate_successor(previous)


def test_resource_usage_distinguishes_provider_measurement_from_agent_estimate() -> None:
    plan = _plan()
    provider = ResourceUsageReceipt(
        receipt_id="usage:provider",
        plan_sha256=plan.content_sha256,
        responsibility_node_id="node:implementation",
        agent_session_id="agent-session:worker",
        token_usage=4096,
        token_measurement="provider-authoritative",
        measurement_evidence_sha256="sha256:" + "a" * 64,
        recorded_at_utc=_time(NOW + timedelta(minutes=1)),
    )
    estimate = ResourceUsageReceipt(
        receipt_id="usage:estimate",
        plan_sha256=plan.content_sha256,
        responsibility_node_id="node:implementation",
        agent_session_id="agent-session:worker",
        token_usage=3900,
        token_measurement="agent-estimate",
        measurement_evidence_sha256="",
        recorded_at_utc=_time(NOW + timedelta(minutes=1)),
    )

    assert provider.token_measurement == "provider-authoritative"
    assert estimate.token_measurement == "agent-estimate"
    with pytest.raises(CoordinationPlanError, match="resource_usage_provider_evidence_invalid"):
        replace(provider, measurement_evidence_sha256="")


@pytest.mark.parametrize("path", ("/tmp/a", "../a", "C:/a", "src/*.py", "src\\a.py"))
def test_responsibility_paths_are_portable_and_non_glob(path: str) -> None:
    with pytest.raises(CoordinationPlanError, match="responsibility_path_invalid"):
        _node(
            "unsafe",
            scope="Unsafe path probe",
            paths=(path,),
            tools=("git.read",),
            allocation=_allocation(1),
            can_delegate=False,
        )


def test_mandate_excludes_raw_secrets_and_private_reasoning() -> None:
    with pytest.raises(CoordinationPlanError, match="user_mandate_objective_invalid"):
        replace(_mandate(), objective="api_key=sk-proj-abcdefghijklmnop")
    with pytest.raises(CoordinationPlanError, match="user_mandate_constraints_invalid"):
        replace(_mandate(), constraints=("include chain-of-thought",))


def test_only_server_bound_top_level_agent_can_activate_and_verify_current_plan() -> None:
    authority, _ = _authority()
    plan = _plan()

    verified = authority.activate(plan, actor_session_id="agent-session:root")

    assert verified.plan == plan
    assert verified.activation.plan_sha256 == plan.content_sha256
    assert (
        authority.verify_current(
            plan_sha256=plan.content_sha256,
            activation_sha256=verified.activation.activation_sha256,
        ).activation
        == verified.activation
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_top_level_actor_required"):
        authority.activate(plan, actor_session_id="agent-session:worker")
    with pytest.raises(
        CoordinationPlanError, match="coordination_plan_verified_authority_required"
    ):
        VerifiedCoordinationPlan(plan, verified.activation)


def test_top_level_binding_authority_is_idempotent_and_fails_closed_on_rejection() -> None:
    repository = InMemoryCoordinationPlanRepository(clock=lambda: NOW + timedelta(seconds=5))
    repository.register_session(_session("agent-session:root"))
    authority = _binding_authority(repository)

    first = authority.bind_top_level_session(
        _mandate(),
        agent_session_id="agent-session:root",
    )
    replay = authority.bind_top_level_session(
        _mandate(),
        agent_session_id="agent-session:root",
        expected_binding_generation=first.binding_generation,
    )
    assert replay == first

    rejecting_repository = InMemoryCoordinationPlanRepository(
        clock=lambda: NOW + timedelta(seconds=5)
    )
    rejecting_repository.register_session(_session("agent-session:root"))
    rejecting = TopLevelAgentBindingAuthority(
        repository=rejecting_repository,
        authorization_verifier=_RejectingTopLevelAuthorizationVerifier(),
        clock=lambda: NOW + timedelta(seconds=5),
    )
    with pytest.raises(
        CoordinationPlanError,
        match="coordination_plan_top_level_user_authorization_rejected",
    ):
        rejecting.bind_top_level_session(
            _mandate(),
            agent_session_id="agent-session:root",
        )


def test_portable_top_level_binding_json_is_not_bearer_authority() -> None:
    authority, repository = _authority()
    binding = repository.load_top_level_binding(
        project_id=PROJECT.project_id,
        coordination_session_id="coord:one",
    )
    assert binding is not None

    fresh_repository = InMemoryCoordinationPlanRepository(clock=lambda: NOW + timedelta(seconds=5))
    fresh_repository.register_session(_session("agent-session:root"))
    # Reconstructing the same public JSON has no repository effect.
    portable_copy = type(binding)(
        project=binding.project,
        coordination_session_id=binding.coordination_session_id,
        top_level_agent_session_id=binding.top_level_agent_session_id,
        top_level_agent_id=binding.top_level_agent_id,
        agent_session_sha256=binding.agent_session_sha256,
        mandate_sha256=binding.mandate_sha256,
        binding_generation=binding.binding_generation,
        bound_at_utc=binding.bound_at_utc,
    )
    assert portable_copy.canonical_json() == binding.canonical_json()
    fresh_authority = CoordinationPlanAuthority(
        repository=fresh_repository,
        clock=lambda: NOW + timedelta(seconds=5),
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_top_level_binding_missing"):
        fresh_authority.activate(_plan(), actor_session_id="agent-session:root")


def test_successor_requires_exact_current_activation_and_supersedes_dispatch_head() -> None:
    authority, _ = _authority()
    previous = _plan()
    current = authority.activate(previous, actor_session_id="agent-session:root")
    successor = replace(
        previous,
        plan_revision=2,
        created_at_utc=_time(NOW + timedelta(seconds=2)),
        supersedes_plan_sha256=previous.content_sha256,
    )

    with pytest.raises(CoordinationPlanError, match="coordination_plan_generation_conflict"):
        authority.activate(successor, actor_session_id="agent-session:root")
    updated = authority.activate(
        successor,
        actor_session_id="agent-session:root",
        expected_current_activation_sha256=current.activation.activation_sha256,
    )

    historical_plan, historical_activation = authority.verify_issued(
        plan_sha256=previous.content_sha256,
        activation_sha256=current.activation.activation_sha256,
    )
    assert historical_plan == previous
    assert historical_activation == current.activation
    assert updated.plan == successor
    with pytest.raises(CoordinationPlanError, match="coordination_plan_not_current"):
        authority.verify_current(
            plan_sha256=previous.content_sha256,
            activation_sha256=current.activation.activation_sha256,
        )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_plan_stale"):
        authority.activate(
            previous,
            actor_session_id="agent-session:root",
            expected_current_activation_sha256=updated.activation.activation_sha256,
        )


def test_activation_rejects_foreign_inactive_expired_or_undesignated_session() -> None:
    plan = _plan()
    cases = (
        (
            _session("agent-session:root", project=ProjectScope("project:foreign")),
            "coordination_plan_agent_session_project_mismatch",
        ),
        (
            _session("agent-session:root", coordination_session_id="coord:foreign"),
            "coordination_plan_agent_session_scope_mismatch",
        ),
        (
            _session("agent-session:root", state="closed"),
            "coordination_plan_agent_session_inactive",
        ),
        (
            _session("agent-session:root", expires_at=NOW + timedelta(seconds=1)),
            "coordination_plan_agent_session_expired",
        ),
    )
    for session, code in cases:
        repository = InMemoryCoordinationPlanRepository(clock=lambda: NOW + timedelta(seconds=5))
        repository.register_session(session)
        with pytest.raises(CoordinationPlanError, match=code):
            _binding_authority(repository).bind_top_level_session(
                _mandate(),
                agent_session_id=session.session_id,
            )
        authority = CoordinationPlanAuthority(
            repository=repository,
            clock=lambda: NOW + timedelta(seconds=5),
        )
        with pytest.raises(
            CoordinationPlanError, match="coordination_plan_top_level_binding_missing"
        ):
            authority.activate(plan, actor_session_id="agent-session:root")

    repository = InMemoryCoordinationPlanRepository(clock=lambda: NOW + timedelta(seconds=5))
    repository.register_session(_session("agent-session:root"))
    authority = CoordinationPlanAuthority(
        repository=repository,
        clock=lambda: NOW + timedelta(seconds=5),
    )
    with pytest.raises(CoordinationPlanError, match="coordination_plan_top_level_binding_missing"):
        authority.activate(plan, actor_session_id="agent-session:root")


def test_portable_activation_json_is_not_bearer_authority() -> None:
    authority, repository = _authority()
    verified = authority.activate(_plan(), actor_session_id="agent-session:root")
    forged = CoordinationPlanActivation(
        activation_id="plan-activation:portable-copy",
        plan_id=verified.plan.plan_id,
        plan_revision=verified.plan.plan_revision,
        plan_sha256=verified.plan.content_sha256,
        mandate_sha256=verified.plan.mandate.content_sha256,
        project=verified.plan.project,
        coordination_session_id=verified.plan.coordination_session_id,
        top_level_agent_session_id=verified.plan.top_level_agent_session_id,
        issued_at_utc=verified.activation.issued_at_utc,
        expires_at_utc=verified.activation.expires_at_utc,
    )

    assert repository.load_activation_by_digest(forged.activation_sha256) is None
    with pytest.raises(CoordinationPlanError, match="coordination_plan_verification_not_found"):
        authority.verify_current(
            plan_sha256=verified.plan.content_sha256,
            activation_sha256=forged.activation_sha256,
        )


def test_provider_usage_enforces_node_and_ancestor_budget_but_estimates_do_not() -> None:
    authority, _ = _authority()
    verified = authority.activate(_plan(), actor_session_id="agent-session:root")

    first = authority.record_usage(
        verified,
        receipt_id="usage:one",
        responsibility_node_id="node:implementation",
        agent_session_id="agent-session:worker",
        token_usage=18_000,
        token_measurement=TOKEN_AUTHORITY_PROVIDER,
        measurement_evidence_sha256="sha256:" + "a" * 64,
    )
    assert (
        authority.record_usage(
            verified,
            receipt_id="usage:one",
            responsibility_node_id="node:implementation",
            agent_session_id="agent-session:worker",
            token_usage=18_000,
            token_measurement=TOKEN_AUTHORITY_PROVIDER,
            measurement_evidence_sha256="sha256:" + "a" * 64,
        )
        == first
    )
    with pytest.raises(CoordinationPlanError, match="resource_usage_token_budget_exceeded"):
        authority.record_usage(
            verified,
            receipt_id="usage:two",
            responsibility_node_id="node:implementation",
            agent_session_id="agent-session:worker",
            token_usage=3_000,
            token_measurement=TOKEN_AUTHORITY_PROVIDER,
            measurement_evidence_sha256="sha256:" + "b" * 64,
        )

    estimate = authority.record_usage(
        verified,
        receipt_id="usage:estimate",
        responsibility_node_id="node:implementation",
        agent_session_id="agent-session:worker",
        token_usage=999_999,
        token_measurement="agent-estimate",
    )
    assert estimate.token_measurement == "agent-estimate"
    with pytest.raises(CoordinationPlanError, match="resource_usage_receipt_conflict"):
        authority.record_usage(
            verified,
            receipt_id="usage:estimate",
            responsibility_node_id="node:implementation",
            agent_session_id="agent-session:worker",
            token_usage=1,
            token_measurement="agent-estimate",
        )


def test_top_level_token_budget_caps_combined_descendant_usage() -> None:
    authority, _ = _authority()
    verified = authority.activate(_plan(), actor_session_id="agent-session:root")

    for receipt_id, node_id, agent_session_id, usage, evidence_char in (
        ("usage:root", "node:root", "agent-session:root", 60_000, "1"),
        ("usage:implementation", "node:implementation", "agent-session:worker", 20_000, "2"),
        ("usage:review", "node:review", "agent-session:worker", 19_000, "3"),
    ):
        authority.record_usage(
            verified,
            receipt_id=receipt_id,
            responsibility_node_id=node_id,
            agent_session_id=agent_session_id,
            token_usage=usage,
            token_measurement=TOKEN_AUTHORITY_PROVIDER,
            measurement_evidence_sha256="sha256:" + evidence_char * 64,
        )

    with pytest.raises(CoordinationPlanError, match="resource_usage_token_budget_exceeded"):
        authority.record_usage(
            verified,
            receipt_id="usage:spec",
            responsibility_node_id="node:spec",
            agent_session_id="agent-session:worker",
            token_usage=2_000,
            token_measurement=TOKEN_AUTHORITY_PROVIDER,
            measurement_evidence_sha256="sha256:" + "4" * 64,
        )
