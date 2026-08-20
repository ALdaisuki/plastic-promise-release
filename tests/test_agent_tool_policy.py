from plastic_promise.core.agent_tool_policy import (
    FORBIDDEN_EXTERNAL_CAPABILITIES,
    ROLE_POLICIES,
    authorize_agent_mcp_call,
    authorize_external_capability,
    policy_receipt,
)
from plastic_promise.skills.official_workflow_stages import build_stage_guidance


def test_every_allowlisted_agent_tool_has_a_fail_closed_read_policy():
    representative_arguments = {
        "audit_run": {"action": "report"},
        "context_graph": {"action": "traverse"},
        "defense": {"action": "get"},
        "review_run": {"action": "prepare"},
        "runtime_mode": {"action": "get"},
    }
    for role, policy in ROLE_POLICIES.items():
        for tool in policy.allowed_mcp_tools:
            result = authorize_agent_mcp_call(
                role,
                tool,
                representative_arguments.get(tool, {}),
            )
            assert result["allowed"] is True, (role, tool, result)


def test_delegated_agents_cannot_call_mutation_tools_or_mutating_actions():
    for role in ROLE_POLICIES:
        for tool in (
            "memory_store",
            "memory_forget",
            "memory_gc",
            "system",
            "task_enqueue",
            "market_install",
        ):
            result = authorize_agent_mcp_call(role, tool, {})
            assert result["allowed"] is False
            assert result["reason"] == "tool_not_allowlisted"

    assert (
        authorize_agent_mcp_call("deepsec_reviewer", "review_run", {"action": "apply"})["reason"]
        == "action_not_allowlisted"
    )
    assert (
        authorize_agent_mcp_call(
            "deepsec_reviewer", "runtime_mode", {"action": "set", "mode": "rust-full"}
        )["reason"]
        == "action_not_allowlisted"
    )
    assert (
        authorize_agent_mcp_call("deepsec_reviewer", "defense", {"action": "adjust"})["reason"]
        == "action_not_allowlisted"
    )


def test_delegated_agents_never_receive_shell_file_database_or_production_writes():
    for role in ROLE_POLICIES:
        for capability in FORBIDDEN_EXTERNAL_CAPABILITIES:
            result = authorize_external_capability(role, capability)
            assert result["allowed"] is False
            assert result["reason"] == "capability_forbidden"
        assert authorize_external_capability(role, "repository.read")["allowed"] is True
        assert authorize_external_capability(role, "web.search")["allowed"] is True


def test_official_review_and_research_guidance_include_versioned_role_policy_receipts():
    review = build_stage_guidance("code-review")
    research = build_stage_guidance("research")
    tdd = build_stage_guidance("tdd")

    assert review["delegation_policy"]["roles"] == ["deepsec_reviewer"]
    assert research["delegation_policy"]["roles"] == ["research_reader"]
    assert tdd["delegation_policy"]["roles"] == []
    for guidance in (review, research):
        receipt = guidance["delegation_policy"]["receipts"][0]
        assert receipt["status"] == "enforced"
        assert receipt["policy_digest"].startswith("sha256:")
        assert "shell.execute" in receipt["policy"]["forbidden_external_capabilities"]


def test_unknown_role_fails_closed_without_a_policy_receipt():
    assert authorize_agent_mcp_call("unknown", "memory_recall", {}) == {
        "allowed": False,
        "reason": "unknown_agent_role",
        "role": "unknown",
        "target": "memory_recall",
        "policy_digest": "",
    }
    assert policy_receipt("unknown") == {
        "role": "unknown",
        "status": "denied",
        "reason": "unknown_agent_role",
    }


def test_participant_is_a_baseline_identity_not_a_work_role():
    receipt = policy_receipt("participant")

    assert receipt["status"] == "enforced"
    assert receipt["policy"]["role"] == "participant"
    assert "task_enqueue" not in receipt["policy"]["allowed_mcp_tools"]
    assert "shell.execute" in receipt["policy"]["forbidden_external_capabilities"]
    assert authorize_agent_mcp_call("implementer", "context_supply", {})["reason"] == (
        "unknown_agent_role"
    )
    assert authorize_agent_mcp_call("reviewer", "context_supply", {})["reason"] == (
        "unknown_agent_role"
    )
