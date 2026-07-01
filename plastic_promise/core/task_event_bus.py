"""TaskEventBus -- SSE broadcaster for hunter guild events.

Every task state transition fires an event through the bus. Agents
registered via register() receive targeted broadcasts. The bus is
a module-level singleton accessed via get_event_bus().

Event types:
  task:new         -- new task enqueued
  task:claimed     -- task claimed by a hunter
  task:done        -- task submitted for verification
  task:verified    -- task accepted by elder
  task:reassigned  -- task rejected/reassigned
  task:overdue     -- heartbeat timeout expired
  task:escalated   -- escalated to claude (max escalations hit)
  hunter:rank_change -- hunter rank changed
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskEventBus:
    """Manages SSE client connections and broadcasts task events."""

    def __init__(self):
        self._clients: dict[str, list] = {}

    def register(self, agent_name: str, send_func):
        """Register an SSE client connection for an agent.

        Multiple connections per agent are supported (e.g. multiple
        browser tabs or MCP sessions).
        """
        if agent_name not in self._clients:
            self._clients[agent_name] = []
        self._clients[agent_name].append(send_func)
        logger.debug("SSE client registered: %s", agent_name)

    def unregister(self, agent_name: str, send_func):
        """Remove a disconnected SSE client."""
        if agent_name in self._clients:
            try:
                self._clients[agent_name].remove(send_func)
                if not self._clients[agent_name]:
                    del self._clients[agent_name]
            except ValueError:
                pass

    async def broadcast(
        self, event_type: str, data: dict, to_agents: list[str]
    ) -> int:
        """Broadcast a task event to specified agents.

        Returns the number of clients that received the event.
        Failed sends trigger automatic unregister (dead connection cleanup).
        """
        payload = json.dumps(
            {"event": event_type, "data": data}, ensure_ascii=False
        )
        notified = 0
        for agent in to_agents:
            if agent in self._clients:
                for send_func in self._clients[agent]:
                    try:
                        await send_func(payload)
                        notified += 1
                    except Exception:
                        self.unregister(agent, send_func)
        return notified

    async def broadcast_task_event(self, event_type: str, task: dict) -> int:
        """Determine target agents from task data and broadcast.

        Target selection rules:
          task:new       -- task["to_agent"] + subscription matches
          task:claimed   -- task["from_agent"] (the hunter who claimed)
          task:done      -- task["from_agent"] (the submitter/daemon)
          task:verified  -- task["claimed_by"] (the hunter verified)
          task:reassigned-- task["claimed_by"] (the hunter reassigned)
          task:overdue   -- claimed_by + claude
          task:escalated -- claude
          hunter:rank_change -- agent + claude
        """
        to_agents: list[str] = []

        if event_type == "task:new":
            to_agents = [task.get("to_agent", "")]
            # Also notify subscribers
            try:
                from plastic_promise.core.task_subscriptions import (
                    match_subscribers,
                )

                subs = match_subscribers(task)
                to_agents.extend(s for s in subs if s not in to_agents)
            except Exception:
                pass

        elif event_type in ("task:claimed", "task:done"):
            to_agents = [task.get("from_agent", "daemon")]
        elif event_type in ("task:reassigned", "task:verified"):
            to_agents = [task.get("claimed_by", "")]
        elif event_type in ("task:overdue",):
            to_agents = [task.get("claimed_by", ""), "claude"]
        elif event_type in ("task:escalated",):
            to_agents = ["claude"]
        elif event_type == "hunter:rank_change":
            to_agents = [task.get("agent", ""), "claude"]

        to_agents = [a for a in to_agents if a]  # Filter empty strings

        return await self.broadcast(
            event_type,
            {
                "task_id": task.get("task_id", task.get("id", "")),
                "task_type": task.get("task_type", ""),
                "priority": task.get("priority", 3),
                "to_agent": task.get("to_agent", ""),
                "title": task.get("title", ""),
                "from_agent": task.get("from_agent", ""),
                "claimed_by": task.get("claimed_by", ""),
            },
            to_agents,
        )

    @property
    def client_count(self) -> int:
        """Total number of connected SSE clients across all agents."""
        return sum(len(v) for v in self._clients.values())


# Module-level singleton
_event_bus: TaskEventBus | None = None


def get_event_bus() -> TaskEventBus:
    """Return the module-level TaskEventBus singleton."""
    global _event_bus
    if _event_bus is None:
        _event_bus = TaskEventBus()
    return _event_bus
