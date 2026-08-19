"""Fail-closed least-privilege policies for delegated analysis agents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from plastic_promise.core.tool_manifest import manifest_for_tool

READ_SIDE_EFFECTS = frozenset({"read", "access_count"})
FORBIDDEN_EXTERNAL_CAPABILITIES = frozenset(
    {
        "database.write",
        "file.delete",
        "file.write",
        "production.deploy",
        "production.restart",
        "shell.execute",
    }
)


@dataclass(frozen=True)
class AgentRolePolicy:
    role: str
    allowed_mcp_tools: frozenset[str]
    allowed_external_capabilities: frozenset[str]
    allowed_actions: dict[str, frozenset[str]] = field(default_factory=dict)
    purpose: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "purpose": self.purpose,
            "allowed_mcp_tools": sorted(self.allowed_mcp_tools),
            "allowed_external_capabilities": sorted(self.allowed_external_capabilities),
            "allowed_actions": {
                tool: sorted(actions) for tool, actions in sorted(self.allowed_actions.items())
            },
            "forbidden_external_capabilities": sorted(FORBIDDEN_EXTERNAL_CAPABILITIES),
        }

    @property
    def digest(self) -> str:
        material = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


ROLE_POLICIES: dict[str, AgentRolePolicy] = {
    "participant": AgentRolePolicy(
        role="participant",
        purpose=(
            "Baseline authenticated collaboration identity. Work-level authority is "
            "issued separately through an event-derived role assignment."
        ),
        allowed_mcp_tools=frozenset(
            {
                "context_supply",
                "defense",
                "memory_recall",
                "principle_activate",
                "runtime_mode",
            }
        ),
        allowed_external_capabilities=frozenset({"repository.read", "web.open", "web.search"}),
        allowed_actions={
            "defense": frozenset({"evaluate_tool", "get", "history", "status"}),
            "runtime_mode": frozenset({"get"}),
        },
    ),
    "deepsec_reviewer": AgentRolePolicy(
        role="deepsec_reviewer",
        purpose="Read-only security evidence and code-smell review.",
        allowed_mcp_tools=frozenset(
            {
                "audit_pre_check",
                "audit_run",
                "context_supply",
                "defense",
                "memory_recall",
                "principle_activate",
                "review_run",
                "runtime_mode",
            }
        ),
        allowed_external_capabilities=frozenset(
            {"diff.read", "repository.read", "web.open", "web.search"}
        ),
        allowed_actions={
            "audit_run": frozenset({"full", "report"}),
            "defense": frozenset({"evaluate_tool", "get", "history", "status"}),
            "review_run": frozenset({"evaluate", "prepare"}),
            "runtime_mode": frozenset({"get"}),
        },
    ),
    "research_reader": AgentRolePolicy(
        role="research_reader",
        purpose="Read-only primary-source research and project-context lookup.",
        allowed_mcp_tools=frozenset(
            {
                "context_graph",
                "context_supply",
                "defense",
                "memory_list",
                "memory_recall",
                "principle_activate",
                "runtime_mode",
            }
        ),
        allowed_external_capabilities=frozenset(
            {"docs.read", "repository.read", "web.open", "web.search"}
        ),
        allowed_actions={
            "context_graph": frozenset({"edges", "node", "principles", "traverse"}),
            "defense": frozenset({"evaluate_tool", "get", "history", "status"}),
            "runtime_mode": frozenset({"get"}),
        },
    ),
}


def policy_for_role(role: object) -> AgentRolePolicy | None:
    return ROLE_POLICIES.get(str(role or "").strip().casefold())


def authorize_agent_mcp_call(
    role: object,
    tool_name: object,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authorize one delegated MCP call without granting ambient capabilities."""

    policy = policy_for_role(role)
    tool = str(tool_name or "").strip()
    if policy is None:
        return _decision(False, "unknown_agent_role", str(role or ""), tool, None)
    if tool not in policy.allowed_mcp_tools:
        return _decision(False, "tool_not_allowlisted", policy.role, tool, policy)

    values = dict(arguments or {})
    action_policy = policy.allowed_actions.get(tool)
    if action_policy is not None:
        action = str(values.get("action") or "").strip().casefold()
        if action not in action_policy:
            return _decision(False, "action_not_allowlisted", policy.role, tool, policy)
    else:
        side_effects = set(manifest_for_tool(tool).side_effects)
        if not side_effects.issubset(READ_SIDE_EFFECTS):
            return _decision(False, "tool_not_read_only", policy.role, tool, policy)

    return _decision(True, "allowlisted_read_only_call", policy.role, tool, policy)


def authorize_external_capability(role: object, capability: object) -> dict[str, Any]:
    """Authorize read-only non-MCP capabilities such as web or repository reads."""

    policy = policy_for_role(role)
    name = str(capability or "").strip().casefold()
    if policy is None:
        return _decision(False, "unknown_agent_role", str(role or ""), name, None)
    if name in FORBIDDEN_EXTERNAL_CAPABILITIES:
        return _decision(False, "capability_forbidden", policy.role, name, policy)
    if name not in policy.allowed_external_capabilities:
        return _decision(False, "capability_not_allowlisted", policy.role, name, policy)
    return _decision(True, "allowlisted_read_only_capability", policy.role, name, policy)


def policy_receipt(role: object) -> dict[str, Any]:
    policy = policy_for_role(role)
    if policy is None:
        return {
            "role": str(role or ""),
            "status": "denied",
            "reason": "unknown_agent_role",
        }
    return {
        "role": policy.role,
        "status": "enforced",
        "policy_digest": policy.digest,
        "policy": policy.to_dict(),
    }


def _decision(
    allowed: bool,
    reason: str,
    role: str,
    target: str,
    policy: AgentRolePolicy | None,
) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "reason": reason,
        "role": role,
        "target": target,
        "policy_digest": policy.digest if policy is not None else "",
    }


__all__ = [
    "AgentRolePolicy",
    "FORBIDDEN_EXTERNAL_CAPABILITIES",
    "ROLE_POLICIES",
    "authorize_agent_mcp_call",
    "authorize_external_capability",
    "policy_for_role",
    "policy_receipt",
]
