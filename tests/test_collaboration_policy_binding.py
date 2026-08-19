from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import AgentIdentity, AgentSession, ProjectScope
from plastic_promise.collaboration.policy_binding import (
    ACCEPTANCE_REVIEW_POLICY_REVISION,
    AGENT_POLICY_BINDING_ISSUER,
    AGENT_POLICY_REVISION,
    AgentPolicyBinding,
    AgentPolicyBindingError,
    open_server_agent_policy_binding_authority,
)
from plastic_promise.core.agent_tool_policy import ROLE_POLICIES

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
POLICY_REVISION = "agent-role-policy/v1"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _session(
    *,
    role: str = "deepsec_reviewer",
    project_id: str = "project:plastic-promise",
    coordination_session_id: str = "coord:policy-binding",
    state: str = "active",
    expires_at: datetime | None = None,
) -> AgentSession:
    return AgentSession(
        session_id="agent-session:reviewer-1",
        identity=AgentIdentity(agent_id="agent:reviewer-1", role=role),
        project=ProjectScope(project_id),
        coordination_session_id=coordination_session_id,
        state=state,
        started_at=_text(NOW - timedelta(minutes=1)),
        last_heartbeat_at=_text(NOW),
        expires_at=_text(expires_at or NOW + timedelta(minutes=10)),
    )


def _issue(clock: MutableClock, session: AgentSession):
    authority = open_server_agent_policy_binding_authority(clock=clock)
    binding = authority.issue(
        session,
        binding_id="binding:reviewer-1",
        policy_revision=POLICY_REVISION,
        ttl_seconds=300,
    )
    return authority, binding


def test_server_binding_is_public_digest_bound_and_required_for_authority():
    clock = MutableClock()
    session = _session()
    authority, binding = _issue(clock, session)

    projection = binding.to_dict()
    assert projection["issuer"] == AGENT_POLICY_BINDING_ISSUER
    assert str(projection["binding_digest"]).startswith("sha256:")
    assert projection["policy_digest"] == ROLE_POLICIES[session.identity.role].digest
    assert not {
        "api_key",
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }.intersection(projection)
    assert "capabilities" not in projection

    denied = authority.authorize_mcp(
        session,
        None,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert denied.allowed is False
    assert denied.reason == "agent_policy_binding_required"

    allowed = authority.authorize_mcp(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert allowed.allowed is True
    assert allowed.reason == "allowlisted_read_only_call"
    assert allowed.binding_digest == binding.binding_digest


def test_server_issuance_rejects_caller_selected_policy_revision():
    authority = open_server_agent_policy_binding_authority(clock=MutableClock())

    assert POLICY_REVISION == AGENT_POLICY_REVISION
    acceptance_binding = authority.issue(
        _session(),
        policy_revision=ACCEPTANCE_REVIEW_POLICY_REVISION,
    )
    assert acceptance_binding.policy_revision == ACCEPTANCE_REVIEW_POLICY_REVISION
    with pytest.raises(
        AgentPolicyBindingError,
        match="^agent_policy_revision_not_server_current$",
    ):
        authority.issue(_session(), policy_revision="policy:caller-forged")


def test_bare_or_self_claimed_reviewer_role_never_creates_authority():
    authority = open_server_agent_policy_binding_authority(clock=MutableClock())
    self_claimed = _session(role="reviewer")

    with pytest.raises(AgentPolicyBindingError, match="^agent_policy_unknown_role$"):
        authority.issue(self_claimed, policy_revision=POLICY_REVISION)

    result = authority.authorize_mcp(  # type: ignore[arg-type]
        self_claimed.identity,
        None,
        project=self_claimed.project,
        coordination_session_id=self_claimed.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert result.allowed is False
    assert result.reason == "agent_policy_agent_session_required"


def test_constructed_binding_without_server_issuance_fails_closed():
    clock = MutableClock()
    session = _session()
    authority = open_server_agent_policy_binding_authority(clock=clock)
    forged = AgentPolicyBinding(
        binding_id="binding:forged",
        agent_session_id=session.session_id,
        agent_id=session.identity.agent_id,
        role=session.identity.role,
        project_id=session.project.project_id,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        policy_digest=ROLE_POLICIES[session.identity.role].digest,
        issued_at=_text(NOW),
        expires_at=_text(NOW + timedelta(minutes=5)),
    )

    decision = authority.authorize_mcp(
        session,
        forged,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert decision.allowed is False
    assert decision.reason == "agent_policy_binding_not_server_issued"


@pytest.mark.parametrize(
    ("project", "coordination_session_id", "reason"),
    [
        (
            ProjectScope("project:other"),
            "coord:policy-binding",
            "agent_policy_binding_project_mismatch",
        ),
        (
            ProjectScope("project:plastic-promise"),
            "coord:other",
            "agent_policy_binding_coordination_session_mismatch",
        ),
    ],
)
def test_request_scope_must_match_binding_and_active_session(
    project: ProjectScope,
    coordination_session_id: str,
    reason: str,
):
    clock = MutableClock()
    session = _session()
    authority, binding = _issue(clock, session)

    decision = authority.authorize_mcp(
        session,
        binding,
        project=project,
        coordination_session_id=coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert decision.allowed is False
    assert decision.reason == reason


def test_expired_or_non_active_session_fails_closed():
    clock = MutableClock()
    session = _session(expires_at=NOW + timedelta(seconds=30))
    authority, binding = _issue(clock, session)
    clock.value = NOW + timedelta(seconds=31)

    expired = authority.authorize_mcp(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert expired.allowed is False
    assert expired.reason == "agent_session_expired"

    with pytest.raises(AgentPolicyBindingError, match="^agent_session_not_active$"):
        authority.issue(_session(state="stale"), policy_revision=POLICY_REVISION)


def test_role_drift_fails_closed_even_with_same_agent_and_session_ids():
    clock = MutableClock()
    original = _session(role="deepsec_reviewer")
    authority, binding = _issue(clock, original)
    drifted = _session(role="research_reader")

    decision = authority.authorize_mcp(
        drifted,
        binding,
        project=drifted.project,
        coordination_session_id=drifted.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert decision.allowed is False
    assert decision.reason == "agent_policy_binding_role_mismatch"


def test_policy_revision_and_digest_drift_fail_closed(monkeypatch):
    clock = MutableClock()
    session = _session()
    authority, binding = _issue(clock, session)

    revision_drift = authority.authorize_mcp(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision="agent-role-policy/v2",
        tool_name="memory_recall",
    )
    assert revision_drift.allowed is False
    assert revision_drift.reason == "agent_policy_revision_mismatch"

    monkeypatch.setitem(
        ROLE_POLICIES,
        session.identity.role,
        replace(ROLE_POLICIES[session.identity.role], purpose="Policy changed after issue."),
    )
    digest_drift = authority.authorize_mcp(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        tool_name="memory_recall",
    )
    assert digest_drift.allowed is False
    assert digest_drift.reason == "agent_policy_digest_mismatch"


def test_bound_external_capabilities_keep_existing_least_privilege_policy():
    clock = MutableClock()
    session = _session()
    authority, binding = _issue(clock, session)

    allowed = authority.authorize_external(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        capability="repository.read",
    )
    denied = authority.authorize_external(
        session,
        binding,
        project=session.project,
        coordination_session_id=session.coordination_session_id,
        policy_revision=POLICY_REVISION,
        capability="file.write",
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.reason == "capability_forbidden"
