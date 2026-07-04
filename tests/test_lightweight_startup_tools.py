import asyncio
import json
from unittest.mock import MagicMock, patch


class NoHeavyEngine:
    def __init__(self):
        self._dm = None
        self._memories = {
            "healthy": {"decay_multiplier": 1.0},
            "decaying": {"decay_multiplier": 0.01},
        }

    def ensure_heavy_init(self):
        raise AssertionError("heavy init should not run")

    def _ensure_heavy_init(self):
        raise AssertionError("heavy init should not run")

    def memory_stats_json(self):
        return json.dumps({"total": len(self._memories)})

    def get_graph(self):
        return {"nodes": {}, "edges": []}

    def get_fuzzy_buffer(self):
        return None


def test_system_stats_does_not_force_heavy_init():
    from plastic_promise.mcp.tools.management import handle_system

    result = asyncio.run(handle_system(NoHeavyEngine(), {"action": "stats"}))
    payload = json.loads(result[0].text)

    assert payload["memory"]["total"] == 2


def test_domain_stats_returns_deferred_without_heavy_init():
    from plastic_promise.mcp.tools.domain import handle_domain

    result = asyncio.run(handle_domain(NoHeavyEngine(), {"action": "stats"}))
    payload = json.loads(result[0].text)

    assert payload["status"] == "deferred"
    assert payload["domains"] == {}


def test_memory_gc_dry_run_does_not_force_heavy_init():
    from plastic_promise.mcp.tools.memory import handle_memory_gc

    result = asyncio.run(handle_memory_gc(NoHeavyEngine(), {"dry_run": True}))
    payload = json.loads(result[0].text)

    assert payload["dry_run"] is True
    assert payload["candidates"] == ["decaying"]
    assert payload["merge"]["skipped"] == "lightweight dry_run does not initialize LanceDB"


def test_skill_session_start_uses_lightweight_register_memory():
    from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

    engine = MagicMock()
    engine.register_entity.return_value = {
        "node_id": "skill_session:skill:brainstorming:2026-01-01T00:00:00",
        "type": "skill_session",
        "name": "brainstorming",
        "is_new": True,
        "edges_created": 0,
    }
    engine.register_memory.return_value = "skill_start_test"

    with patch("plastic_promise.mcp.tools.skill_tracking._get_current_branch", return_value=""):
        result = asyncio.run(
            handle_skill_session_start(
                engine,
                {
                    "skill_name": "brainstorming",
                    "task_description": "lightweight startup",
                },
            )
        )

    payload = json.loads(result[0].text)
    assert payload["memory_id"] == "skill_start_test"
    engine.register_memory.assert_called_once()
