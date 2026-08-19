"""PR5 server scope authority regression checks.

These tests exercise the MCP adapter seam only; they do not install schema or
touch a production database.  Workflow scope must come from the authenticated
server task binding, never from a caller-supplied stage/coordination field.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plastic_promise.collaboration.durable_runtime import DurableCollaborationError
from plastic_promise.mcp import server as mcp_server


class _Session:
    pass


def test_durable_binding_uses_server_task_scope(monkeypatch) -> None:
    session = _Session()
    captured: dict[str, object] = {}

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: session)
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "actor": "codex",
            "stage_session_id": "stage:server",
            "flow_scope_id": "stage:server::flow:codex::project:authority",
        },
    )

    monkeypatch.setattr(
        mcp_server,
        "_authenticated_transport_instance",
        lambda: "transport:mcp:" + ("a" * 32),
    )

    def fake_open(_engine, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            durable=True,
            runtime=object(),
            session=SimpleNamespace(
                project=SimpleNamespace(project_id="project:authority"),
                coordination_session_id="stage:server::flow:codex::project:authority",
                identity=SimpleNamespace(agent_id="agent:codex"),
            ),
            reason="",
        )

    monkeypatch.setattr(
        "plastic_promise.collaboration.runtime_binding.open_mcp_durable_collaboration_runtime",
        fake_open,
    )

    outcome = mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        object(),
        "project:authority",
    )

    assert outcome == (True, "")
    assert captured["project_id"] == "project:authority"
    assert captured["coordination_session_id"] == ("stage:server::flow:codex::project:authority")
    assert captured["transport_session_id"] == "transport:mcp:" + ("a" * 32)


def test_durable_lifecycle_rejects_scope_without_server_task_binding(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_task_session_authority", lambda: None)

    result = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        object(),
        {
            "event": "before_invoke",
            "project_id": "project:caller",
            "stage_session_id": "attacker-controlled",
            "coordination_session_id": "attacker-controlled-too",
        },
    )

    assert result == {
        "state": "deferred",
        "reason": "durable_collaboration_task_session_authority_required",
    }


def test_durable_lifecycle_defers_when_hook_has_no_authenticated_transport_binding(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "actor": "codex",
            "stage_session_id": "stage:server",
            "flow_scope_id": "stage:server::flow:codex::project:authority",
        },
    )

    result = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        object(),
        {
            "event": "before_invoke",
            "project_id": "project:authority",
            "stage_session_id": "attacker-controlled",
            "coordination_session_id": "attacker-controlled-too",
        },
    )

    assert result == {
        "state": "deferred",
        "reason": "durable_collaboration_authenticated_binding_required",
    }


def test_durable_lifecycle_uses_exact_current_transport_binding(monkeypatch) -> None:
    runtime = object()
    host = SimpleNamespace(
        heartbeat=lambda **_kwargs: {
            "schema_version": "durable-collaboration-heartbeat/v1",
            "state": "active",
        }
    )
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "flow_scope_id": "stage:server::flow:codex::project:authority",
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(
            runtime=runtime,
            host=host,
            session=SimpleNamespace(
                session_id="agent-session:exact",
                project=SimpleNamespace(project_id="project:authority"),
                coordination_session_id="stage:server::flow:codex::project:authority",
            ),
        ),
    )

    result = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        object(),
        {"event": "before_invoke", "project_id": "project:authority"},
    )

    assert result == {
        "state": "durable",
        "action": "heartbeat",
        "persistent": True,
        "receipt": {
            "schema_version": "durable-collaboration-heartbeat/v1",
            "state": "active",
        },
    }


def test_task_binding_records_resolved_workflow_scope(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: session)

    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:authority",
        workflow={
            "stage_session_id": "stage:resolved",
            "flow_line_id": "codex",
            "flow_scope_id": "stage:resolved::flow:codex::project:authority",
            "route_id": "idea-to-ship",
        },
    ) == (True, "")

    binding = mcp_server._task_session_authority()  # noqa: SLF001
    assert binding is not None
    assert binding["stage_session_id"] == "stage:resolved"
    assert binding["flow_line_id"] == "codex"
    assert binding["flow_scope_id"] == "stage:resolved::flow:codex::project:authority"
    assert binding["route_id"] == "idea-to-ship"


def _workflow_handoff_binding(
    monkeypatch,
    *,
    runtime,
    binding_actor: str = "codex",
    session_actor: str = "agent:codex",
) -> None:
    scope = "stage:server::flow:codebase-design::project:authority"
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "actor": binding_actor,
            "flow_scope_id": scope,
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_current_durable_collaboration_binding",
        lambda: SimpleNamespace(
            runtime=runtime,
            session=SimpleNamespace(
                session_id="agent-session:exact",
                project=SimpleNamespace(project_id="project:authority"),
                coordination_session_id=scope,
                identity=SimpleNamespace(agent_id=session_actor),
            ),
        ),
    )


def test_sp_stage_handoff_uses_exact_server_transport_binding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def publish(**kwargs):
        captured.update(kwargs)
        return {
            "state": "durable",
            "receipt_sha256": "sha256:" + ("a" * 64),
            "event_ids": ["event:receipt", "event:completed"],
            "event_types": [
                "workflow.receipt_submitted",
                "workflow.stage_completed",
            ],
            "replayed": False,
        }

    runtime = SimpleNamespace(publish_workflow_receipt_events=publish)
    _workflow_handoff_binding(monkeypatch, runtime=runtime)

    result = mcp_server._publish_sp_stage_collaboration_events(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:exact",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )

    assert captured == {
        "agent_session_id": "agent-session:exact",
        "execution_receipt_id": "workflow-receipt:exact",
        "route_id": "codebase-design",
        "stage": "codebase-design",
        "step_index": 0,
    }
    assert result == {
        "schema_version": "sp-stage-collaboration-handoff/v1",
        "state": "durable",
        "persistent": True,
        "execution_receipt_id": "workflow-receipt:exact",
        "receipt_sha256": "sha256:" + ("a" * 64),
        "event_ids": ["event:receipt", "event:completed"],
        "event_types": [
            "workflow.receipt_submitted",
            "workflow.stage_completed",
        ],
        "replayed": False,
        "canonical_memory_effect": "none",
    }


def test_sp_stage_handoff_rejects_caller_scope_before_runtime(monkeypatch) -> None:
    called = False

    def publish(**_kwargs):
        nonlocal called
        called = True
        return {"state": "durable"}

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_receipt_events=publish),
    )

    result = mcp_server._publish_sp_stage_collaboration_events(  # noqa: SLF001
        flow_scope_id="stage:caller::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:exact",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )

    assert called is False
    assert result["state"] == "deferred"
    assert result["reason"] == "durable_collaboration_workflow_scope_conflict"
    assert result["canonical_memory_effect"] == "none"


def test_sp_stage_handoff_exposes_only_stable_collaboration_errors(monkeypatch) -> None:
    def stable_failure(**_kwargs):
        raise DurableCollaborationError("collaboration_event_append_conflict")

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_receipt_events=stable_failure),
    )
    stable = mcp_server._publish_sp_stage_collaboration_events(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:exact",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )
    assert stable["reason"] == "collaboration_event_append_conflict"

    def private_failure(**_kwargs):
        raise RuntimeError("secret database path /srv/private/canonical.sqlite")

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_receipt_events=private_failure),
    )
    generic = mcp_server._publish_sp_stage_collaboration_events(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:exact",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )
    assert generic["reason"] == "durable_collaboration_event_handoff_unavailable"
    assert "private" not in str(generic)


@pytest.mark.parametrize(
    ("binding_actor", "session_actor"),
    [
        ("", "agent:codex"),
        ("codex", "agent:other"),
    ],
)
def test_sp_stage_handoff_fails_closed_on_actor_binding_drift(
    monkeypatch,
    binding_actor: str,
    session_actor: str,
) -> None:
    called = False

    def publish(**_kwargs):
        nonlocal called
        called = True
        return {"state": "durable"}

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_receipt_events=publish),
        binding_actor=binding_actor,
        session_actor=session_actor,
    )

    result = mcp_server._publish_sp_stage_collaboration_events(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:exact",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
    )

    assert called is False
    assert result["state"] == "deferred"
    assert result["reason"] == "durable_collaboration_transport_binding_conflict"


def test_sp_stage_lifecycle_uses_exact_server_transport_binding(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def publish(**kwargs):
        captured.update(kwargs)
        return {
            "state": "durable",
            "workflow_attempt_id": "workflow-attempt:exact",
            "event_id": "event:workflow-stage-started:exact",
            "event_type": "workflow.stage_started",
            "cursor": {"sequence": 1},
            "replayed": False,
        }

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_stage_lifecycle_event=publish),
    )
    result = mcp_server._publish_sp_stage_lifecycle_event(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )

    assert captured == {
        "agent_session_id": "agent-session:exact",
        "execution_receipt_id": "workflow-receipt:candidate",
        "route_id": "codebase-design",
        "stage": "codebase-design",
        "step_index": 0,
        "lifecycle": "started",
        "reason_code": "",
    }
    assert result["state"] == "durable"
    assert result["event_type"] == "workflow.stage_started"
    assert result["canonical_memory_effect"] == "none"


def test_sp_stage_blocked_lifecycle_hides_runtime_exception(monkeypatch) -> None:
    def publish(**_kwargs):
        raise RuntimeError("private sqlite path /srv/canonical.sqlite")

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_stage_lifecycle_event=publish),
    )
    result = mcp_server._publish_sp_stage_lifecycle_event(  # noqa: SLF001
        flow_scope_id="stage:server::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="blocked",
        reason_code="skill_execution_failed",
    )

    assert result["state"] == "deferred"
    assert result["reason"] == "durable_collaboration_lifecycle_unavailable"
    assert "private" not in str(result)


def test_sp_stage_lifecycle_rejects_caller_scope_before_runtime(monkeypatch) -> None:
    called = False

    def publish(**_kwargs):
        nonlocal called
        called = True
        return {"state": "durable"}

    _workflow_handoff_binding(
        monkeypatch,
        runtime=SimpleNamespace(publish_workflow_stage_lifecycle_event=publish),
    )
    result = mcp_server._publish_sp_stage_lifecycle_event(  # noqa: SLF001
        flow_scope_id="stage:caller::flow:codebase-design::project:authority",
        execution_receipt_id="workflow-receipt:candidate",
        route_id="codebase-design",
        stage="codebase-design",
        step_index=0,
        lifecycle="started",
    )

    assert called is False
    assert result["state"] == "deferred"
    assert result["reason"] == "durable_collaboration_workflow_scope_conflict"


def test_work_board_operation_requires_exact_authenticated_binding(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_current_durable_collaboration_binding", lambda: None)

    result = mcp_server._durable_collaboration_work_operation(  # noqa: SLF001
        "list",
        {"project_id": "project:authority", "agent_session_id": "forged"},
    )

    assert result == {
        "schema_version": "collaboration-work-operation/v1",
        "state": "deferred",
        "persistent": False,
        "operation": "list",
        "reason": "durable_collaboration_authenticated_binding_required",
        "canonical_memory_effect": "none",
    }


def test_work_board_operation_uses_host_and_rejects_project_conflict(monkeypatch) -> None:
    calls = []
    host = SimpleNamespace(
        work_list=lambda *, limit: calls.append(limit)
        or {
            "schema_version": "collaboration-work-list/v1",
            "state": "durable",
            "persistent": True,
            "project_id": "project:authority",
            "items": [],
            "count": 0,
            "authority_effect": "none",
            "canonical_memory_effect": "none",
        }
    )
    exact = SimpleNamespace(
        host=host,
        session=SimpleNamespace(
            project=SimpleNamespace(project_id="project:authority"),
            coordination_session_id="stage:server::flow:codex::project:authority",
            identity=SimpleNamespace(agent_id="agent:codex-desktop"),
        ),
    )
    monkeypatch.setattr(mcp_server, "_current_durable_collaboration_binding", lambda: exact)
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "flow_scope_id": "stage:server::flow:codex::project:authority",
            "actor": "codex-desktop",
        },
    )

    conflict = mcp_server._durable_collaboration_work_operation(  # noqa: SLF001
        "list",
        {"project_id": "project:other", "limit": 7},
    )
    assert conflict["reason"] == "durable_collaboration_project_conflict"
    assert calls == []

    durable = mcp_server._durable_collaboration_work_operation(  # noqa: SLF001
        "list",
        {"project_id": "project:authority", "limit": 7},
    )
    assert durable["state"] == "durable"
    assert calls == [7]


@pytest.mark.asyncio
async def test_work_board_public_tool_routes_to_authenticated_facade(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)

    def operate(operation, arguments):
        captured.append((operation, dict(arguments)))
        return {
            "schema_version": "collaboration-work-operation/v1",
            "state": "durable",
            "persistent": True,
            "operation": operation,
            "work": {"work_item_id": arguments["work_item_id"], "state": "in_progress"},
            "canonical_memory_effect": "none",
        }

    monkeypatch.setattr(mcp_server, "_durable_collaboration_work_operation", operate)

    response = await mcp_server.call_tool(
        "collaboration_work_claim",
        {"project_id": "project:authority", "work_item_id": "work:assigned"},
    )

    assert captured == [
        (
            "claim",
            {"project_id": "project:authority", "work_item_id": "work:assigned"},
        )
    ]
    assert __import__("json").loads(response[0].text)["state"] == "durable"


def test_continuation_resume_uses_server_scope_and_never_registers_again(monkeypatch) -> None:
    transport = _Session()
    scope = "stage:server::flow:codex::project:authority"
    captured: dict[str, object] = {}
    exact = SimpleNamespace(
        runtime=object(),
        host=SimpleNamespace(continuation_is_active=lambda: True),
        session=SimpleNamespace(
            session_id="agent-session:continued",
            project=SimpleNamespace(project_id="project:authority"),
            coordination_session_id=scope,
            identity=SimpleNamespace(agent_id="agent:codex-desktop", role="participant"),
        ),
    )
    claims = SimpleNamespace(
        project_id="project:authority",
        flow_scope_id=scope,
        server_actor="codex",
        hook_session_id="hook:server",
        durable_session_id="agent-session:continued",
        agent_id="agent:codex",
        role="participant",
        stage_session_id="stage:server",
        flow_line_id="codex",
    )

    class _Authority:
        def resume(self, token, **kwargs):
            captured["token_present"] = bool(token)
            captured.update(kwargs)
            return SimpleNamespace(valid=True, binding=exact, claims=claims, reason="")

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: transport)
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "actor": "codex-desktop",
            "stage_session_id": "stage:server",
            "flow_line_id": "codex",
            "flow_scope_id": scope,
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority",
        lambda: _Authority(),
    )
    monkeypatch.setattr(
        "plastic_promise.collaboration.runtime_binding.open_mcp_durable_collaboration_runtime",
        lambda *_args, **_kwargs: pytest.fail("continuation resume must not register a session"),
    )

    result = mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        object(),
        "project:authority",
        continuation_token="opaque-bearer",
        hook_session_id="hook:server",
    )

    assert result == (True, "")
    assert captured == {
        "token_present": True,
        "project_id": "project:authority",
        "flow_scope_id": scope,
        "server_actor": "codex",
        "hook_session_id": "hook:server",
        "stage_session_id": "",
        "flow_line_id": "",
    }
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        assert mcp_server._durable_collaboration_bindings[transport] is exact  # noqa: SLF001


def test_continuation_inputs_expose_bearer_and_hook_but_no_durable_authority_claims() -> None:
    tools = __import__("asyncio").run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    allowed = {"hook_session_id", "collaboration_continuation_token"}
    forbidden = {
        "agent_session_id",
        "durable_session_id",
        "coordination_session_id",
        "transport_session_id",
        "actor",
        "role",
        "policy",
        "capability",
    }
    for name in ("session-init", "auto_context_inject"):
        properties = set((by_name[name].inputSchema or {}).get("properties", {}))
        assert allowed <= properties
        assert properties.isdisjoint(forbidden)


def test_continuation_bearer_is_removed_before_trace_or_hook_handler() -> None:
    token = "opaque-server-bearer"
    arguments = {
        "event": "session_end",
        "project_id": "project:authority",
        "hook_session_id": "hook:server",
        "collaboration_continuation_token": token,
        "agent_session_id": "caller-forged",
        "actor": "caller-forged",
    }

    sanitized = mcp_server._without_collaboration_continuation_token(arguments)  # noqa: SLF001
    runtime_context = mcp_server._tool_runtime_event_context(  # noqa: SLF001
        "auto_context_inject",
        arguments,
    )

    assert "collaboration_continuation_token" not in sanitized
    assert token not in str(sanitized)
    assert token not in str(runtime_context)
    assert sanitized["agent_session_id"] == "caller-forged"


@pytest.mark.asyncio
async def test_ordinary_authenticated_tool_call_reconciles_exactly_once_with_exclusions(
    monkeypatch,
) -> None:
    calls = {"reconcile": 0, "lease_heartbeat": 0}
    runtime_events: list[dict[str, object]] = []

    class _Host:
        def reconcile_tool_call(self):
            calls["reconcile"] += 1
            return {
                "state": "active",
                "active_leases": [{"work_item_id": "work:one"}],
                "peer_delta": {"items": [{"event_id": "event:one"}]},
                "cursor": {"stored_sequence": 2, "next_sequence": 3},
            }

        def work_list(self, *, limit):
            return {
                "schema_version": "collaboration-work-operation/v1",
                "state": "durable",
                "operation": "list",
                "limit": limit,
            }

        def lease_heartbeat(self, *, work_item_id):
            calls["lease_heartbeat"] += 1
            return {
                "schema_version": "collaboration-work-operation/v1",
                "state": "durable",
                "operation": "lease_heartbeat",
                "work_item_id": work_item_id,
            }

    host = _Host()
    exact = SimpleNamespace(
        host=host,
        session=SimpleNamespace(
            project=SimpleNamespace(project_id="project:authority"),
            coordination_session_id="stage:server::flow:codex::project:authority",
            identity=SimpleNamespace(agent_id="agent:codex"),
        ),
        acceptance_repository=None,
    )

    async def known_tool(name):
        return name != "unknown-pr5-tool"

    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_is_known_mcp_tool", known_tool)
    monkeypatch.setattr(mcp_server, "_current_durable_collaboration_binding", lambda: exact)
    monkeypatch.setattr(
        mcp_server,
        "_task_session_authority",
        lambda: {
            "project_id": "project:authority",
            "actor": "codex",
            "flow_scope_id": "stage:server::flow:codex::project:authority",
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_record_tool_runtime_event",
        lambda _engine, context, _status: runtime_events.append(dict(context)),
    )

    response = await mcp_server.call_tool(
        "collaboration_work_list",
        {
            "project_id": "project:authority",
            "collaboration_continuation_token": "must-not-enter-runtime-metadata",
        },
    )
    assert __import__("json").loads(response[0].text)["state"] == "durable"
    assert calls["reconcile"] == 1
    assert runtime_events
    assert runtime_events[0]["metadata"]["durable_collaboration"] == {
        "schema_version": "durable-collaboration-tool-call-runtime/v1",
        "state": "durable",
        "presence_state": "active",
        "active_lease_count": 1,
        "peer_event_count": 1,
        "cursor_stored_sequence": 2,
        "cursor_next_sequence": 3,
        "persistent": True,
        "canonical_memory_effect": "none",
    }
    assert "must-not-enter-runtime-metadata" not in repr(runtime_events)

    await mcp_server.call_tool(
        "collaboration_lease_heartbeat",
        {"project_id": "project:authority", "work_item_id": "work:one"},
    )
    assert calls == {"reconcile": 1, "lease_heartbeat": 1}

    await mcp_server.call_tool("unknown-pr5-tool", {})
    assert calls["reconcile"] == 1

    from plastic_promise.core import agent_tool_policy

    monkeypatch.setattr(
        agent_tool_policy,
        "authorize_agent_mcp_call",
        lambda *_args, **_kwargs: {"allowed": False, "reason": "test-denied"},
    )
    denied = await mcp_server.call_tool(
        "collaboration_work_list",
        {"agent_role": "test-role", "project_id": "project:authority"},
    )
    assert __import__("json").loads(denied[0].text)["error"] == (
        "delegated_agent_policy_denied"
    )
    assert calls["reconcile"] == 1
