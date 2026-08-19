"""Focused PR5 MCP-to-durable-session binding regression checks.

These tests exercise the server-owned transport seam only.  They deliberately
avoid a production MCP process, a deployment migration, and Hook emulation:
the property under test is that two live SDK transport sessions never collapse
into one durable ``AgentSession`` merely because their actor/project/workflow
scope happens to match.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plastic_promise.collaboration import runtime_binding
from plastic_promise.collaboration.context_supply_runtime import CollaborationContextReadRequest
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.runtime_binding import open_mcp_durable_collaboration_runtime
from plastic_promise.core.context_engine import _SQLiteStorage
from plastic_promise.mcp import server as mcp_server
from tests.pr5_schema_fixture import install_pr5_collaboration_schema


class _Session:
    """Weak-referenceable stand-in for one SDK-owned transport session."""


class _Storage:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def batch(self):
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @contextmanager
    def migration_batch(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()


class _Engine:
    def __init__(self, path: str = ":memory:", *, install_schema: bool = True) -> None:
        self._sqlite = _Storage(path)
        self._write_lock = threading.RLock()
        if install_schema:
            install_pr5_collaboration_schema(
                self._sqlite._conn,
                transaction_factory=self._sqlite.migration_batch,
                clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
                suffix="mcp-binding",
            )


@pytest.fixture(autouse=True)
def _clear_server_local_bindings():
    with mcp_server._task_session_authorities_guard:  # noqa: SLF001
        mcp_server._task_session_authorities.clear()  # noqa: SLF001
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        mcp_server._durable_collaboration_bindings.clear()  # noqa: SLF001
    with mcp_server._mcp_transport_instances_guard:  # noqa: SLF001
        mcp_server._mcp_transport_instances.clear()  # noqa: SLF001
    authority = mcp_server._durable_collaboration_continuation_authority_instance  # noqa: SLF001
    if authority is not None:
        authority.clear()
    yield
    with mcp_server._task_session_authorities_guard:  # noqa: SLF001
        mcp_server._task_session_authorities.clear()  # noqa: SLF001
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        mcp_server._durable_collaboration_bindings.clear()  # noqa: SLF001
    with mcp_server._mcp_transport_instances_guard:  # noqa: SLF001
        mcp_server._mcp_transport_instances.clear()  # noqa: SLF001
    authority = mcp_server._durable_collaboration_continuation_authority_instance  # noqa: SLF001
    if authority is not None:
        authority.clear()


def _workflow() -> dict[str, str]:
    return {
        "stage_session_id": "stage:binding",
        "flow_line_id": "codebase-design",
        "flow_scope_id": "stage:binding::flow:codebase-design::project:binding",
        "route_id": "codebase-design",
    }


def _assigned_work(session, *, suffix: str = "mcp") -> tuple[WorkReceipt, WorkLease]:
    issued_at = "2026-08-13T00:00:00.000000Z"
    expires_at = "2026-08-20T01:00:00.000000Z"
    receipt = WorkReceipt(
        receipt_id=f"receipt:{suffix}",
        work_item_id=f"work:{suffix}",
        project=ProjectScope(session.project.project_id),
        coordination_session_id=session.coordination_session_id,
        assigned_agent=AgentIdentity(
            session.identity.agent_id,
            session.identity.role,
        ),
        objective="Exercise the authenticated ProjectWorkBoard façade",
        fencing_generation=1,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    item = WorkItem(
        work_item_id=receipt.work_item_id,
        project=receipt.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256=receipt.content_sha256,
        result_schema="collaboration-result/v1",
        created_at=issued_at,
        max_attempts=2,
        coordination_session_id=receipt.coordination_session_id,
    )
    lease = WorkLease(
        lease_id=f"lease:{suffix}",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=receipt.assigned_agent.agent_id,
        owner_identity=receipt.assigned_agent,
        fencing_generation=1,
        attempt=1,
        issued_at="2026-08-13T00:00:01.000000Z",
        expires_at=expires_at,
        result_binding_sha256=receipt.content_sha256,
        idempotency_key_sha256="sha256:" + "a" * 64,
    )
    return receipt, lease


def test_canonical_sqlite_storage_enables_foreign_keys_after_restart(tmp_path) -> None:
    """The real storage opener must restore connection-local FK enforcement."""

    db_path = tmp_path / "canonical-restart.db"
    first = _SQLiteStorage(str(db_path))
    assert first._conn.execute("PRAGMA foreign_keys").fetchone() == (1,)  # noqa: SLF001
    first._conn.close()  # noqa: SLF001

    restarted = _SQLiteStorage(str(db_path))
    assert restarted._conn.execute("PRAGMA foreign_keys").fetchone() == (1,)  # noqa: SLF001
    restarted._conn.close()  # noqa: SLF001


def test_distinct_server_transport_sessions_get_distinct_durable_sessions(monkeypatch) -> None:
    """Concurrent peer transports must not share cursor or lifecycle identity."""

    engine = _Engine()
    first_transport = _Session()
    second_transport = _Session()
    current = {"session": first_transport}

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])

    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    first = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert first is not None and first.session is not None

    current["session"] = second_transport
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    second = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert second is not None and second.session is not None

    assert first.session.session_id != second.session.session_id
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_agent_sessions "
        "WHERE project_id='project:binding' AND coordination_session_id=?",
        (first.session.coordination_session_id,),
    ).fetchone() == (2,)

    # A request on the original SDK transport is a safe in-session replay,
    # not a new durable participant and not a cross-transport resume.
    current["session"] = first_transport
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    rebound = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert rebound is not None and rebound.session is not None
    assert rebound.session.session_id == first.session.session_id

    # Closing one exact session must not close a concurrent peer session.
    assert first.runtime is not None
    closed = first.runtime.end_session(first.session.session_id)
    assert closed["state"] == "closed"
    rows = dict(
        engine._sqlite._conn.execute(
            "SELECT session_id,state FROM collaboration_agent_sessions "
            "WHERE project_id='project:binding'"
        ).fetchall()
    )
    assert rows[first.session.session_id] == "closed"
    assert rows[second.session.session_id] == "active"
    engine._sqlite._conn.close()


def test_concurrent_first_bind_reuses_one_server_authority_graph(monkeypatch) -> None:
    """Concurrent first-use transports must share exact authority instances."""

    engine = _Engine()
    start = threading.Barrier(2)
    creation_count = 0
    creation_guard = threading.Lock()
    original = runtime_binding.open_server_agent_policy_binding_authority

    def slow_open_policy_authority(*, clock):
        nonlocal creation_count
        with creation_guard:
            creation_count += 1
        # Release the GIL long enough to expose an unsynchronized
        # check/create/store sequence in the pre-fix implementation.
        time.sleep(0.03)
        return original(clock=clock)

    monkeypatch.setattr(
        runtime_binding,
        "open_server_agent_policy_binding_authority",
        slow_open_policy_authority,
    )

    def bind(index: int):
        start.wait()
        return open_mcp_durable_collaboration_runtime(
            engine,
            project_id="project:binding",
            server_actor="codex-desktop",
            coordination_session_id="stage:binding::flow:codebase-design::project:binding",
            transport_session_id=f"transport:mcp:{index:032x}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(bind, (1, 2)))

    assert first.durable is True
    assert second.durable is True
    assert creation_count == 1
    assert first.role_assignment_authority is second.role_assignment_authority
    assert first.acceptance_authority is second.acceptance_authority
    assert first.coordinator_authority is second.coordinator_authority
    assert first.session is not None and second.session is not None
    assert first.session.session_id != second.session.session_id
    engine._sqlite._conn.close()


def test_binding_fails_closed_when_engine_cannot_cache_authority_graph() -> None:
    """A transient authority graph must never escape without engine ownership."""

    base = _Engine()

    class _ReadOnlyEngine:
        __slots__ = ("_sqlite", "_write_lock")

        def __init__(self):
            self._sqlite = base._sqlite
            self._write_lock = base._write_lock

        def __setattr__(self, name, value):
            if name not in self.__slots__ and hasattr(self, name):
                raise AttributeError(name)
            object.__setattr__(self, name, value)

    outcome = open_mcp_durable_collaboration_runtime(
        _ReadOnlyEngine(),
        project_id="project:binding",
        server_actor="codex-desktop",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:00000000000000000000000000000003",
    )

    assert outcome.durable is False
    assert outcome.reason == "durable_collaboration_authority_cache_unavailable"
    assert base._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_agent_sessions"
    ).fetchone() == (0,)
    base._sqlite._conn.close()


def test_durable_binding_refuses_missing_authenticated_transport_instance() -> None:
    """A workflow scope alone is insufficient to select a durable session."""

    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="",
    )
    assert outcome.durable is False
    assert outcome.reason == "durable_collaboration_authenticated_transport_required"
    engine._sqlite._conn.close()


def test_session_init_projection_is_bounded_and_does_not_expose_authority_handles() -> None:
    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:00000000000000000000000000000004",
    )

    assert outcome.durable is True
    projection = outcome.host.session_init_projection()
    assert projection["schema_version"] == "durable-collaboration-session-init/v1"
    assert set(projection) >= {
        "bound_policy",
        "working_set_summary",
        "assigned_work",
        "peer_delta",
        "cursor",
    }
    assert projection["working_set_summary"]["agents"]["active"] == 1
    assert projection["assigned_work"] == []
    assert [item["event_type"] for item in projection["peer_delta"]["items"]] == ["agent.joined"]
    assert projection["cursor"] == {
        "schema_version": "collaboration-cursor-delivery/v1",
        "stored_sequence": 0,
        "next_sequence": 1,
        "source_head_sequence": 1,
        "has_more": False,
        "ack_required": True,
        "advance": "explicit-heartbeat-ack",
        "scope": "current-authenticated-session",
    }
    encoded = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "agent_session_id",
        "binding_id",
        "binding_digest",
        "transport_session_id",
        "work_receipt_json",
        "lease_json",
        "owner_session_id",
    ):
        assert forbidden not in encoded
    engine._sqlite._conn.close()


def test_session_init_peer_delta_requires_explicit_ack_before_cursor_advances() -> None:
    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:00000000000000000000000000000005",
    )
    assert outcome.durable is True
    session = outcome.session
    outcome.runtime.append_event(
        CollaborationEvent(
            event_id="event:binding:peer-delta",
            project=session.project,
            coordination_session_id=session.coordination_session_id,
            actor=session.identity,
            event_type="work.progressed",
            summary="bounded progress",
            created_at="2026-08-13T00:00:00.000000Z",
            payload={"private": "must-not-project"},
        )
    )

    first = outcome.host.heartbeat()
    assert first["cursor"]["stored_sequence"] == 0
    assert first["cursor"]["next_sequence"] == 2
    assert first["cursor"]["ack_required"] is True
    assert first["peer_delta"]["items"][0]["payload"] == "redacted"
    assert (
        outcome.runtime.load_cursor(
            project=session.project,
            coordination_session_id=session.coordination_session_id,
            consumer_id=session.session_id,
        ).sequence
        == 0
    )

    acknowledged = outcome.host.heartbeat(cursor_ack=2)
    assert acknowledged["cursor"]["stored_sequence"] == 2
    assert acknowledged["peer_delta"]["items"] == []
    with pytest.raises(
        __import__(
            "plastic_promise.collaboration.durable_runtime",
            fromlist=["DurableCollaborationError"],
        ).DurableCollaborationError,
        match="collaboration_cursor_ack_invalid",
    ):
        outcome.host.heartbeat(cursor_ack=True)
    engine._sqlite._conn.close()


def test_authenticated_host_composes_bounded_durable_context_without_acknowledging_cursor() -> None:
    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:0000000000000000000000000000000c",
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    assert outcome.durable is True
    outcome.runtime.append_event(
        CollaborationEvent(
            event_id="event:binding:context-conflict",
            project=outcome.session.project,
            coordination_session_id=outcome.session.coordination_session_id,
            actor=outcome.session.identity,
            event_type="conflict.detected",
            summary="Migration contract conflicts with the current deployment plan",
            created_at="2026-08-14T00:00:00.000000Z",
            payload={"internal_note": "must-not-project"},
        )
    )

    result = asyncio.run(
        outcome.host.compose(
            memory_context={
                "schema_version": "context-supply-memory-context/v1",
                "project_id": "project:binding",
                "request_scope_id": "request:durable-context",
                "core": [{"content": "deployment migration contract"}],
                "related": [],
                "divergent": [],
                "activated_principles": [],
            },
            request=CollaborationContextReadRequest(
                project=ProjectScope("project:binding"),
                request_scope_id="request:durable-context",
                response_mode="compact",
                after_sequence=1,
                limit=20,
            ),
        )
    )

    assert result.state == "available"
    assert result.projection["authority"] == "authenticated-durable-collaboration"
    assert result.projection["items"][0]["kind"] == "conflict"
    assert result.projection["items"][0]["payload"] == "redacted"
    assert result.projection["cursor"]["ack_required"] is True
    assert "internal_note" not in json.dumps(result.projection, sort_keys=True)
    assert (
        outcome.runtime.load_cursor(
            project=outcome.session.project,
            coordination_session_id=outcome.session.coordination_session_id,
            consumer_id=outcome.session.session_id,
        ).sequence
        == 0
    )
    engine._sqlite._conn.close()


def test_session_init_schema_cannot_receive_transport_or_durable_authority_claims() -> None:
    """The public MCP JSON surface cannot choose a durable binding target."""

    tools = asyncio.run(mcp_server.list_tools())
    session_init = next(tool for tool in tools if tool.name == "session-init")
    properties = set((session_init.inputSchema or {}).get("properties", {}))
    assert properties.isdisjoint(
        {
            "agent_session_id",
            "coordination_session_id",
            "durable_session_id",
            "identity",
            "policy",
            "role",
            "transport_session_id",
        }
    )


def test_project_work_board_schema_excludes_caller_authority_claims() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    names = {
        "collaboration_work_list",
        "collaboration_work_register",
        "collaboration_work_claim",
        "collaboration_lease_heartbeat",
        "collaboration_work_review",
        "collaboration_work_accept",
    }
    assert names <= set(by_name)
    forbidden = {
        "agent_session_id",
        "reviewer_session_id",
        "lease_id",
        "lease",
        "policy",
        "role",
        "capabilities",
        "owner_session_id",
        "sqlite_id",
    }
    for name in names:
        properties = set((by_name[name].inputSchema or {}).get("properties", {}))
        assert properties.isdisjoint(forbidden)


def test_exact_host_lists_claims_and_heartbeats_assigned_work() -> None:
    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:00000000000000000000000000000009",
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    assert outcome.durable is True
    receipt, _lease = _assigned_work(outcome.session, suffix="host-board")
    outcome.runtime.register_work(
        receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=outcome.session.session_id,
    )

    listed = outcome.host.work_list()
    assert [item["work_item_id"] for item in listed["items"]] == [receipt.work_item_id]
    claimed = outcome.host.work_claim(work_item_id=receipt.work_item_id)
    assert claimed["state"] == "durable"
    assert claimed["operation"] == "claim"
    assert claimed["work"]["state"] == "in_progress"
    assert claimed["work"]["lease"]["state"] == "active"

    heartbeat = outcome.host.lease_heartbeat(work_item_id=receipt.work_item_id)
    assert heartbeat["state"] == "durable"
    assert heartbeat["heartbeat"]["heartbeat_sequence"] == 1
    assert engine._sqlite._conn.execute(
        "SELECT owner_session_id,heartbeat_sequence FROM collaboration_work_leases "
        "WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == (outcome.session.session_id, 1)
    engine._sqlite._conn.close()


def test_project_work_board_register_uses_server_owned_scope_identity_and_time() -> None:
    engine = _Engine()
    outcome = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex",
        coordination_session_id="stage:binding::flow:codebase-design::project:binding",
        transport_session_id="transport:mcp:0000000000000000000000000000000a",
    )
    assert outcome.durable is True
    registered = outcome.host.work_register(
        arguments={
            "objective": "Implement the bounded PR5 lifecycle adapter",
            "dependency_work_ids": ["work:dependency-b", "work:dependency-a"],
            "max_attempts": 2,
            "request_id": "request:register-one",
        }
    )
    assert registered["state"] == "durable"
    assert registered["operation"] == "register"
    assert registered["work"]["state"] == "ready"
    assert registered["work"]["assigned_agent_id"] == "agent:codex"
    assert registered["work"]["dependency_work_ids"] == [
        "work:dependency-a",
        "work:dependency-b",
    ]
    replayed = outcome.host.work_register(
        arguments={
            "objective": "Implement the bounded PR5 lifecycle adapter",
            "dependency_work_ids": ["work:dependency-a", "work:dependency-b"],
            "max_attempts": 2,
            "request_id": "request:register-one",
        }
    )
    assert replayed["work"]["work_item_id"] == registered["work"]["work_item_id"]
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_work_items"
    ).fetchone() == (1,)
    with pytest.raises(
        __import__(
            "plastic_promise.collaboration.durable_runtime",
            fromlist=["DurableCollaborationError"],
        ).DurableCollaborationError,
        match="collaboration_work_authority_claim_forbidden",
    ):
        outcome.host.work_register(arguments={"agent_session_id": "forged"})
    engine._sqlite._conn.close()


def test_peer_transport_cannot_heartbeat_another_transports_lease(monkeypatch) -> None:
    engine = _Engine()
    first_transport = _Session()
    second_transport = _Session()
    current = {"session": first_transport}
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])

    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    first = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    receipt, _lease = _assigned_work(first.session, suffix="peer-lease")
    first.runtime.register_work(
        receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=first.session.session_id,
    )
    first.host.work_claim(work_item_id=receipt.work_item_id)

    current["session"] = second_transport
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    second = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001

    with pytest.raises(
        __import__(
            "plastic_promise.collaboration.durable_runtime",
            fromlist=["DurableCollaborationError"],
        ).DurableCollaborationError,
        match="work_active_lease_required",
    ):
        second.host.lease_heartbeat(work_item_id=receipt.work_item_id)
    assert engine._sqlite._conn.execute(
        "SELECT owner_session_id,heartbeat_sequence FROM collaboration_work_leases "
        "WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == (first.session.session_id, 0)
    engine._sqlite._conn.close()


def test_lifecycle_session_end_closes_only_current_transport(monkeypatch) -> None:
    engine = _Engine()
    first_transport = _Session()
    second_transport = _Session()
    current = {"session": first_transport}

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])

    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    first = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001

    current["session"] = second_transport
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    second = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001

    current["session"] = first_transport
    lifecycle = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {"event": "session_end", "project_id": "project:binding"},
    )

    assert lifecycle["state"] == "durable"
    assert lifecycle["action"] == "session_end"
    assert mcp_server._current_durable_collaboration_binding() is None  # noqa: SLF001
    rows = dict(
        engine._sqlite._conn.execute(
            "SELECT session_id,state FROM collaboration_agent_sessions"
        ).fetchall()
    )
    assert rows[first.session.session_id] == "closed"
    assert rows[second.session.session_id] == "active"
    engine._sqlite._conn.close()


def test_lifecycle_without_exact_transport_binding_is_deferred(monkeypatch) -> None:
    engine = _Engine()
    transport = _Session()
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: transport)
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")

    lifecycle = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {"event": "session_end", "project_id": "project:binding"},
    )

    assert lifecycle == {
        "state": "deferred",
        "reason": "durable_collaboration_authenticated_binding_required",
    }
    engine._sqlite._conn.close()


def test_lifecycle_exposes_only_stable_collaboration_error_codes(monkeypatch) -> None:
    engine = _Engine()
    transport = _Session()
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: transport)
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    exact = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001

    from plastic_promise.collaboration.durable_runtime import DurableCollaborationError

    monkeypatch.setattr(
        exact.runtime,
        "heartbeat",
        lambda _session_id: (_ for _ in ()).throw(
            DurableCollaborationError("lease_owner_session_ambiguous")
        ),
    )
    stable = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {"event": "before_invoke", "project_id": "project:binding"},
    )
    assert stable["reason"] == "lease_owner_session_ambiguous"

    monkeypatch.setattr(
        exact.runtime,
        "heartbeat",
        lambda _session_id: (_ for _ in ()).throw(
            RuntimeError("secret filesystem path /srv/private")
        ),
    )
    generic = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {"event": "before_invoke", "project_id": "project:binding"},
    )
    assert generic["reason"] == "durable_collaboration_lifecycle_unavailable"
    assert "private" not in str(generic)
    engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_session_init_issues_continuation_only_in_secret_response_field(
    monkeypatch,
) -> None:
    engine = _Engine()
    transport = _Session()
    captured_arguments: dict[str, object] = {}
    authority = runtime_binding.DurableCollaborationContinuationAuthority(key=b"s" * 32)

    class _SkillEngine:
        async def exec(self, name, arguments, *, caller):
            assert name == "session-init"
            assert caller == "claude"
            captured_arguments.update(arguments)
            return SimpleNamespace(
                skill_name="session-init",
                success=True,
                data={
                    "project_id": "project:binding",
                    "stage_session_id": "stage:binding",
                    "workflow_contract": _workflow(),
                    "chain_state": {},
                    "context_status": {},
                },
                degrade_log=[],
                errors=[],
                audit_trail=[],
            )

    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: _SkillEngine())
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: transport)
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority_instance",
        authority,
    )

    response = await mcp_server.call_tool(
        "session-init",
        {
            "task_description": "Issue a bounded continuation",
            "project_id": "project:binding",
            "stage_session_id": "stage:binding",
            "flow_line_id": "codebase-design",
        },
    )
    payload = json.loads(response[0].text)
    continuation = payload["collaboration_continuation"]
    token = payload["collaboration_continuation_token"]

    assert token
    assert continuation["token"] == token
    assert continuation["hook_session_id"].startswith("hook:mcp:")
    assert "collaboration_continuation_token" not in captured_arguments
    assert token not in json.dumps(payload["diagnostics"], sort_keys=True)
    assert token not in json.dumps(payload["durable_collaboration"], sort_keys=True)
    assert token not in json.dumps(captured_arguments, sort_keys=True)
    engine._sqlite._conn.close()


def test_continuation_keyring_rejects_public_permissions(tmp_path) -> None:
    keyring_path = tmp_path / "continuation-keyring.json"
    authority = runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(
        keyring_path
    )
    assert authority is not None
    assert keyring_path.stat().st_mode & 0o777 == 0o600

    keyring_path.chmod(0o644)
    with pytest.raises(
        ValueError,
        match="durable_collaboration_continuation_keyring_permissions_invalid",
    ):
        runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(keyring_path)


def test_hmac_continuation_fails_closed_for_tampering_scope_expiry_and_key_loss() -> None:
    engine = _Engine()
    now = {"value": datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)}
    authority = runtime_binding.DurableCollaborationContinuationAuthority(
        key=b"a" * 32,
        ttl_seconds=60,
        clock=lambda: now["value"],
    )
    binding = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex-desktop",
        coordination_session_id=_workflow()["flow_scope_id"],
        transport_session_id="transport:mcp:0000000000000000000000000000000d",
        clock=lambda: now["value"],
    )
    issued = authority.issue(
        binding,
        project_id="project:binding",
        flow_scope_id=_workflow()["flow_scope_id"],
        server_actor="codex-desktop",
        hook_session_id="hook:one",
        stage_session_id="stage:binding",
        flow_line_id="codebase-design",
    )
    assert issued.valid is True
    assert issued.token
    payload_text, signature = issued.token.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4)).decode()
    )
    payload["project"] = "project:forged"
    forged_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )

    common = {
        "project_id": "project:binding",
        "flow_scope_id": _workflow()["flow_scope_id"],
        "server_actor": "codex-desktop",
        "hook_session_id": "hook:one",
    }
    assert authority.resume(f"{forged_payload}.{signature}", **common).reason == (
        "durable_collaboration_continuation_invalid"
    )
    forged_signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert authority.resume(f"{payload_text}.{forged_signature}", **common).reason == (
        "durable_collaboration_continuation_invalid"
    )
    assert (
        authority.resume(
            issued.token,
            **{**common, "project_id": "project:other"},
        ).reason
        == "durable_collaboration_continuation_project_conflict"
    )
    assert (
        authority.resume(
            issued.token,
            **{**common, "flow_scope_id": "stage:other::flow:other::project:binding"},
        ).reason
        == "durable_collaboration_continuation_flow_conflict"
    )
    assert (
        authority.resume(
            issued.token,
            **{**common, "hook_session_id": "hook:other"},
        ).reason
        == "durable_collaboration_continuation_hook_conflict"
    )

    restarted = runtime_binding.DurableCollaborationContinuationAuthority(
        key=b"b" * 32,
        clock=lambda: now["value"],
    )
    assert restarted.resume(issued.token, **common).reason == (
        "durable_collaboration_continuation_key_unavailable"
    )
    now["value"] += timedelta(seconds=61)
    assert authority.resume(issued.token, **common).reason == (
        "durable_collaboration_continuation_expired"
    )
    engine._sqlite._conn.close()


def test_continuation_reuses_session_cursor_and_session_end_revokes_all_transports(
    monkeypatch,
) -> None:
    engine = _Engine()
    transports = [_Session(), _Session(), _Session(), _Session()]
    current = {"session": transports[0]}
    authority = runtime_binding.DurableCollaborationContinuationAuthority(key=b"c" * 32)
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority_instance",
        authority,
    )

    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
    ) == (True, "")
    first = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert first is not None and first.session is not None and first.host is not None

    next_sequence = first.host.session_init_projection()["cursor"]["next_sequence"]
    first.host.heartbeat(cursor_ack=next_sequence)
    stored_sequence = first.runtime.load_cursor(
        project=first.session.project,
        coordination_session_id=first.session.coordination_session_id,
        consumer_id=first.session.session_id,
    ).sequence
    token, _expires_at, reason = mcp_server._issue_durable_collaboration_continuation(  # noqa: SLF001
        hook_session_id="hook:continuation"
    )
    assert reason == ""
    token_digest = authority.token_digest(token)

    current["session"] = transports[1]
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        engine,
        "project:binding",
        continuation_token=token,
        hook_session_id="hook:continuation",
    ) == (True, "")
    second = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert second is first
    assert second.session.session_id == first.session.session_id
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_agent_sessions WHERE project_id=?",
        ("project:binding",),
    ).fetchone() == (1,)
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.joined'"
    ).fetchone() == (1,)
    assert (
        second.runtime.load_cursor(
            project=second.session.project,
            coordination_session_id=second.session.coordination_session_id,
            consumer_id=second.session.session_id,
        ).sequence
        == stored_sequence
    )

    receipt, _lease = _assigned_work(first.session, suffix="continuation")
    first.runtime.register_work(
        receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=first.session.session_id,
    )
    first.host.work_claim(work_item_id=receipt.work_item_id)

    current["session"] = transports[2]
    lifecycle = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {
            "event": "session_end",
            "project_id": "project:binding",
            "stage_session_id": "stage:binding",
            "flow_line_id": "codebase-design",
            "hook_session_id": "hook:continuation",
            "collaboration_continuation_token": token,
        },
    )
    assert lifecycle["state"] == "durable"
    assert lifecycle["action"] == "session_end"
    assert lifecycle["receipt"]["released_lease_count"] == 1
    assert engine._sqlite._conn.execute(
        "SELECT state FROM collaboration_agent_sessions WHERE session_id=?",
        (first.session.session_id,),
    ).fetchone() == ("closed",)
    assert engine._sqlite._conn.execute(
        "SELECT state FROM collaboration_work_leases WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == ("released",)
    assert authority.is_revoked(token_digest) is True
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        assert not list(mcp_server._durable_collaboration_bindings.values())  # noqa: SLF001

    current["session"] = transports[3]
    revoked = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {
            "event": "before_invoke",
            "project_id": "project:binding",
            "stage_session_id": "stage:binding",
            "flow_line_id": "codebase-design",
            "hook_session_id": "hook:continuation",
            "collaboration_continuation_token": token,
        },
    )
    assert revoked == {
        "state": "deferred",
        "reason": "durable_collaboration_continuation_revoked",
    }

    durable_projection = first.host.session_init_projection()
    annotated: dict[str, object] = {}
    mcp_server._annotate_durable_collaboration_binding(  # noqa: SLF001
        annotated,
        project_id="project:binding",
        success=True,
        binding=first,
    )
    runtime_context = mcp_server._tool_runtime_event_context(  # noqa: SLF001
        "session-init",
        {
            "project_id": "project:binding",
            "collaboration_continuation_token": token,
        },
    )
    assert token not in json.dumps(durable_projection, sort_keys=True)
    assert token not in json.dumps(annotated, sort_keys=True)
    assert token not in json.dumps(lifecycle, sort_keys=True)
    assert token not in json.dumps(runtime_context, sort_keys=True)
    tables = [
        row[0]
        for row in engine._sqlite._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'collaboration_%'"
        ).fetchall()
    ]
    durable_rows = {
        table: engine._sqlite._conn.execute(f'SELECT * FROM "{table}"').fetchall()
        for table in tables
    }
    assert token not in repr(durable_rows)
    engine._sqlite._conn.close()


def test_continuation_rehydrates_exact_session_across_process_restart(
    monkeypatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "canonical.db"
    keyring_path = tmp_path / "continuation-keyring.json"
    current = {"session": _Session()}
    first_authority = runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(
        keyring_path
    )
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority_instance",
        first_authority,
    )

    first_engine = _Engine(str(database_path))
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        first_engine,
        "project:binding",
    ) == (True, "")
    first = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert first is not None and first.session is not None and first.host is not None
    next_sequence = first.host.session_init_projection()["cursor"]["next_sequence"]
    first.host.heartbeat(cursor_ack=next_sequence)
    stored_sequence = first.runtime.load_cursor(
        project=first.session.project,
        coordination_session_id=first.session.coordination_session_id,
        consumer_id=first.session.session_id,
    ).sequence
    receipt, _lease = _assigned_work(first.session, suffix="restart")
    first.runtime.register_work(
        receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=first.session.session_id,
    )
    first.host.work_claim(work_item_id=receipt.work_item_id)
    old_token, _old_expiry, reason = mcp_server._issue_durable_collaboration_continuation(  # noqa: SLF001
        hook_session_id="hook:restart"
    )
    assert reason == ""
    durable_session_id = first.session.session_id
    first_engine._sqlite._conn.close()

    with mcp_server._task_session_authorities_guard:  # noqa: SLF001
        mcp_server._task_session_authorities.clear()  # noqa: SLF001
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        mcp_server._durable_collaboration_bindings.clear()  # noqa: SLF001
    with mcp_server._mcp_transport_instances_guard:  # noqa: SLF001
        mcp_server._mcp_transport_instances.clear()  # noqa: SLF001

    restarted_authority = runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(
        keyring_path
    )
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority_instance",
        restarted_authority,
    )
    current["session"] = _Session()
    restarted_engine = _Engine(str(database_path), install_schema=False)
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        restarted_engine,
        "project:binding",
        continuation_token=old_token,
        hook_session_id="hook:restart",
    ) == (True, "")
    resumed = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert resumed is not None and resumed.session is not None
    assert resumed.session.session_id == durable_session_id
    assert (
        resumed.runtime.load_cursor(
            project=resumed.session.project,
            coordination_session_id=resumed.session.coordination_session_id,
            consumer_id=resumed.session.session_id,
        ).sequence
        == stored_sequence
    )
    assert restarted_engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.joined'"
    ).fetchone() == (1,)
    assert restarted_engine._sqlite._conn.execute(
        "SELECT state,owner_session_id FROM collaboration_work_leases WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == ("active", durable_session_id)
    reconciled = resumed.host.reconcile_tool_call()
    assert len(reconciled["active_leases"]) == 1
    assert reconciled["active_leases"][0]["work_item_id"] == receipt.work_item_id
    assert reconciled["active_leases"][0]["state"] == "active"
    assert reconciled["active_leases"][0]["fencing_generation"] == 1
    assert reconciled["active_leases"][0]["expires_at"]
    assert restarted_engine._sqlite._conn.execute(
        "SELECT heartbeat_sequence FROM collaboration_work_leases WHERE work_item_id=?",
        (receipt.work_item_id,),
    ).fetchone() == (1,)

    refreshed_token, _refreshed_expiry, reason = (
        mcp_server._issue_durable_collaboration_continuation(  # noqa: SLF001
            hook_session_id="hook:restart"
        )
    )
    assert reason == ""
    assert refreshed_token and refreshed_token != old_token
    ended = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        restarted_engine,
        {
            "event": "session_end",
            "project_id": "project:binding",
            "hook_session_id": "hook:restart",
            "collaboration_continuation_token": refreshed_token,
        },
    )
    assert ended["state"] == "durable"
    restarted_engine._sqlite._conn.close()

    with mcp_server._task_session_authorities_guard:  # noqa: SLF001
        mcp_server._task_session_authorities.clear()  # noqa: SLF001
    with mcp_server._durable_collaboration_bindings_guard:  # noqa: SLF001
        mcp_server._durable_collaboration_bindings.clear()  # noqa: SLF001
    with mcp_server._mcp_transport_instances_guard:  # noqa: SLF001
        mcp_server._mcp_transport_instances.clear()  # noqa: SLF001
    monkeypatch.setattr(
        mcp_server,
        "_durable_collaboration_continuation_authority_instance",
        runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(keyring_path),
    )
    current["session"] = _Session()
    closed_engine = _Engine(str(database_path), install_schema=False)
    assert mcp_server._bind_task_session_authority(  # noqa: SLF001
        "project:binding",
        workflow=_workflow(),
    ) == (True, "")
    assert mcp_server._bind_durable_collaboration_runtime_for_project(  # noqa: SLF001
        closed_engine,
        "project:binding",
        continuation_token=old_token,
        hook_session_id="hook:restart",
    ) == (False, "durable_collaboration_continuation_session_inactive")
    closed_engine._sqlite._conn.close()


def test_stop_activity_publishes_idempotent_typed_progress_for_exact_session(monkeypatch) -> None:
    engine = _Engine()
    current = {"session": _Session()}
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: current["session"])
    assert mcp_server._bind_task_session_authority("project:binding", workflow=_workflow()) == (
        True,
        "",
    )
    assert mcp_server._bind_durable_collaboration_runtime_for_project(
        engine, "project:binding"
    ) == (
        True,
        "",
    )
    binding = mcp_server._current_durable_collaboration_binding()  # noqa: SLF001
    assert binding is not None and binding.host is not None and binding.session is not None
    receipt, _lease = _assigned_work(binding.session, suffix="stop")
    binding.runtime.register_work(
        receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=binding.session.session_id,
    )
    binding.host.work_claim(work_item_id=receipt.work_item_id)

    first = binding.host.publish_stop_activity(idempotency_key="stop-call-1")
    second = binding.host.publish_stop_activity(idempotency_key="stop-call-1")
    assert first["events"][0]["event_type"] == "work.progressed"
    assert second["events"][0]["replayed"] is True
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.progressed'"
    ).fetchone() == (1,)
    lifecycle = mcp_server._durable_collaboration_lifecycle(  # noqa: SLF001
        engine,
        {
            "event": "after_invoke",
            "project_id": "project:binding",
            "request_id": "stop-call-2",
        },
    )
    assert lifecycle["state"] == "durable"
    assert lifecycle["receipt"]["stop_activity"]["events"][0]["event_type"] == ("work.progressed")
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='work.progressed'"
    ).fetchone() == (2,)
    current["session"] = None


def test_stop_activity_submitted_result_requires_exact_session_and_result_identity() -> None:
    engine = _Engine()
    first = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex-desktop",
        coordination_session_id=_workflow()["flow_scope_id"],
        transport_session_id="transport:mcp:" + "a" * 32,
    )
    peer = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex-desktop",
        coordination_session_id=_workflow()["flow_scope_id"],
        transport_session_id="transport:mcp:" + "b" * 32,
    )
    assert first.durable and first.host is not None and first.session is not None
    assert peer.durable and peer.host is not None and peer.session is not None
    assert first.session.session_id != peer.session.session_id

    work_receipt, _unused = _assigned_work(first.session, suffix="submitted-stop")
    first.runtime.register_work(
        work_receipt,
        state="ready",
        max_attempts=2,
        agent_session_id=first.session.session_id,
    )
    first.host.work_claim(work_item_id=work_receipt.work_item_id)
    lease_row = first.runtime._fetchone(  # noqa: SLF001
        "SELECT lease_id,lease_sha256,fencing_generation FROM collaboration_work_leases "
        "WHERE work_item_id=? AND state='active'",
        (work_receipt.work_item_id,),
    )
    assert lease_row is not None
    result = ResultReceipt.for_work(
        work_receipt,
        receipt_id="result:submitted-stop",
        submitted_by=work_receipt.assigned_agent,
        outcome="completed",
        summary="Canonical result for Stop projection",
        submitted_at="2026-08-13T00:01:00.000000Z",
    )
    first.runtime.record_result(
        result,
        lease_id=str(lease_row["lease_id"]),
        fencing_generation=int(lease_row["fencing_generation"]),
        lease_sha256=str(lease_row["lease_sha256"]),
        agent_session_id=first.session.session_id,
    )

    first_event = first.host.publish_stop_activity(idempotency_key="stop-a")
    repeated_result = first.host.publish_stop_activity(idempotency_key="stop-b")
    peer_event = peer.host.publish_stop_activity(idempotency_key="stop-peer")

    assert first_event["events"][0]["event_type"] == "work.submitted"
    assert repeated_result["events"][0]["event_id"] == first_event["events"][0]["event_id"]
    assert repeated_result["events"][0]["replayed"] is True
    assert peer_event["events"] == []
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events "
        "WHERE event_type='work.submitted' AND event_id LIKE 'event:stop-activity:%'"
    ).fetchone() == (1,)
    engine._sqlite._conn.close()


@pytest.mark.parametrize(
    ("column", "value", "expected_reason"),
    (
        (
            "session_sha256",
            "sha256:" + "0" * 64,
            "durable_collaboration_continuation_session_corrupt",
        ),
        (
            "identity_json",
            '{"agent_id":"agent:tampered","capabilities":[],"parent_agent_id":null,"role":"participant"}',
            "durable_collaboration_continuation_session_corrupt",
        ),
        (
            "coordination_session_id",
            "stage:tampered::flow:review::project:binding",
            "durable_collaboration_continuation_binding_invalid",
        ),
    ),
)
def test_restart_continuation_rejects_corrupt_canonical_session_rows(
    tmp_path,
    column: str,
    value: str,
    expected_reason: str,
) -> None:
    database_path = tmp_path / f"corrupt-{column}.db"
    keyring_path = tmp_path / f"corrupt-{column}.keyring.json"
    engine = _Engine(str(database_path))
    binding = open_mcp_durable_collaboration_runtime(
        engine,
        project_id="project:binding",
        server_actor="codex-desktop",
        coordination_session_id=_workflow()["flow_scope_id"],
        transport_session_id="transport:mcp:" + "1" * 32,
    )
    assert binding.durable and binding.session is not None
    authority = runtime_binding.DurableCollaborationContinuationAuthority.from_key_file(
        keyring_path
    )
    issued = authority.issue(
        binding,
        project_id="project:binding",
        flow_scope_id=_workflow()["flow_scope_id"],
        server_actor="codex-desktop",
        hook_session_id="hook:corrupt-row",
    )
    assert issued.valid and issued.claims is not None
    engine._sqlite._conn.execute(
        f"UPDATE collaboration_agent_sessions SET {column}=? WHERE session_id=?",
        (value, binding.session.session_id),
    )
    engine._sqlite._conn.commit()
    resumed = runtime_binding.resume_mcp_durable_collaboration_runtime(
        engine,
        claims=issued.claims,
    )
    assert resumed.durable is False
    assert resumed.reason == expected_reason
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_events WHERE event_type='agent.joined'"
    ).fetchone() == (1,)
