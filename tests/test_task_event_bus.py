"""Tests for TaskEventBus — SSE broadcasting and subscription matching."""

import pytest
from plastic_promise.core.task_event_bus import TaskEventBus, get_event_bus


def test_event_bus_singleton():
    """get_event_bus() always returns the same instance."""
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2


@pytest.mark.asyncio
async def test_event_bus_broadcast():
    """Registered agents receive broadcast events."""
    bus = TaskEventBus()
    received = []

    async def fake_send(payload):
        received.append(payload)

    bus.register("pi_fixer", fake_send)
    notified = await bus.broadcast("task:new", {"task_id": "t_test"}, ["pi_fixer"])
    assert notified == 1
    assert len(received) == 1
    assert "task:new" in received[0]


@pytest.mark.asyncio
async def test_event_bus_offline_agent():
    """Broadcasting to an unregistered agent returns 0, not an error."""
    bus = TaskEventBus()
    notified = await bus.broadcast("task:new", {"task_id": "t_test"}, ["offline_agent"])
    assert notified == 0


@pytest.mark.asyncio
async def test_event_bus_broadcast_task_event():
    """broadcast_task_event determines targets from event_type and task data."""
    bus = TaskEventBus()
    received = []

    async def fake_send(payload):
        received.append(payload)

    bus.register("pi_fixer", fake_send)
    notified = await bus.broadcast_task_event(
        "task:new",
        {
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
