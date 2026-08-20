"""Tests for TaskEventBus — SSE broadcasting and subscription matching."""

import asyncio
import json

import pytest

from plastic_promise.core.task_event_bus import TaskEventBus, get_event_bus
from plastic_promise.mcp.server import (
    _ProjectNotificationHub,
    _task_event_subscription_scope,
)


def test_event_bus_singleton():
    """get_event_bus() always returns the same instance."""
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2


def test_task_event_subscription_scope_is_explicit_and_canonical():
    with pytest.raises(ValueError, match="canonical project_id"):
        _task_event_subscription_scope({})
    assert _task_event_subscription_scope(
        {"project_id": "project:alpha", "agent_name": "pi_fixer"}
    ) == ("project:alpha", "pi_fixer")
    with pytest.raises(ValueError, match="canonical project_id"):
        _task_event_subscription_scope({"project_id": "project:unknown", "agent_name": "pi_fixer"})
    with pytest.raises(ValueError, match="agent_name"):
        _task_event_subscription_scope({"project_id": "project:alpha"})


@pytest.mark.asyncio
async def test_general_notification_hub_fans_out_within_project_only():
    hub = _ProjectNotificationHub()
    alpha_one = asyncio.Queue()
    alpha_two = asyncio.Queue()
    beta = asyncio.Queue()
    hub.register("project:alpha", alpha_one)
    hub.register("project:alpha", alpha_two)
    hub.register("project:beta", beta)

    await hub.put(
        {
            "type": "memory_stored",
            "project_id": "project:alpha",
            "memory_id": "memory-alpha",
        }
    )

    expected = {
        "type": "memory_stored",
        "project_id": "project:alpha",
        "memory_id": "memory-alpha",
    }
    assert alpha_one.get_nowait() == expected
    assert alpha_two.get_nowait() == expected
    assert beta.empty()


def test_general_notification_hub_routes_unscoped_system_event_and_rejects_invalid_scope():
    hub = _ProjectNotificationHub()
    system = asyncio.Queue()
    hub.register("project:system-governance", system)

    hub.put_nowait({"type": "issue_changed", "issue_id": "issue-1"})

    assert system.get_nowait() == {
        "type": "issue_changed",
        "issue_id": "issue-1",
        "project_id": "project:system-governance",
    }
    with pytest.raises(ValueError, match="canonical project_id"):
        hub.put_nowait({"type": "issue_changed", "project_id": "project:unknown"})


@pytest.mark.asyncio
async def test_event_bus_broadcast():
    """Registered agents receive broadcast events."""
    bus = TaskEventBus()
    received = []

    async def fake_send(payload):
        received.append(payload)

    bus.register("project:alpha", "pi_fixer", fake_send)
    notified = await bus.broadcast("project:alpha", "task:new", {"task_id": "t_test"}, ["pi_fixer"])
    assert notified == 1
    assert len(received) == 1
    assert "task:new" in received[0]


@pytest.mark.asyncio
async def test_event_bus_offline_agent():
    """Broadcasting to an unregistered agent returns 0, not an error."""
    bus = TaskEventBus()
    notified = await bus.broadcast(
        "project:alpha", "task:new", {"task_id": "t_test"}, ["offline_agent"]
    )
    assert notified == 0


@pytest.mark.asyncio
async def test_event_bus_broadcast_task_event():
    """broadcast_task_event determines targets from event_type and task data."""
    bus = TaskEventBus()
    received = []

    async def fake_send(payload):
        received.append(payload)

    bus.register("project:alpha", "pi_fixer", fake_send)
    notified = await bus.broadcast_task_event(
        "task:new",
        {
            "project_id": "project:alpha",
            "task_id": "t_abc",
            "task_type": "fix_memory",
            "priority": 3,
            "to_agent": "pi_fixer",
            "title": "Fix stale memory records",
            "from_agent": "daemon",
            "claimed_by": "",
        },
    )
    assert notified == 1
    assert len(received) == 1
    assert "task:new" in received[0]
    assert "t_abc" in received[0]


@pytest.mark.asyncio
async def test_event_bus_isolates_same_agent_by_project_and_keeps_project_in_payload():
    bus = TaskEventBus()
    alpha_received = []
    beta_received = []

    async def alpha_send(payload):
        alpha_received.append(json.loads(payload))

    async def beta_send(payload):
        beta_received.append(json.loads(payload))

    bus.register("project:alpha", "pi_fixer", alpha_send)
    bus.register("project:beta", "pi_fixer", beta_send)

    notified = await bus.broadcast_task_event(
        "task:new",
        {
            "project_id": "project:alpha",
            "task_id": "t_alpha",
            "task_type": "fix_memory",
            "priority": 3,
            "to_agent": "pi_fixer",
            "title": "alpha only",
            "from_agent": "daemon",
        },
    )

    assert notified == 1
    assert beta_received == []
    assert alpha_received == [
        {
            "event": "task:new",
            "data": {
                "project_id": "project:alpha",
                "task_id": "t_alpha",
                "task_type": "fix_memory",
                "priority": 3,
                "to_agent": "pi_fixer",
                "title": "alpha only",
                "from_agent": "daemon",
                "claimed_by": "",
            },
        }
    ]
