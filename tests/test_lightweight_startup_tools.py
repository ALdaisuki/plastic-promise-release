import asyncio
import json
from unittest.mock import MagicMock, patch


class NoHeavyEngine:
    def __init__(self):
        self._dm = None
        self._memories = {
            "healthy": {"id": "healthy", "decay_multiplier": 1.0},
            "decaying": {"id": "decaying", "decay_multiplier": 0.01},
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

    def iter_memories(self):
        return iter(self._memories.values())


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


def test_memory_gc_dry_run_uses_public_iterator_instead_of_raw_pool():
    from plastic_promise.mcp.tools.memory import handle_memory_gc

    engine = NoHeavyEngine()
    engine._memories["draft-synthesis"] = {
        "id": "draft-synthesis",
        "memory_type": "synthesis",
        "decay_multiplier": 0.0,
    }
    engine.iter_memories = lambda: iter(
        memory for memory_id, memory in engine._memories.items() if memory_id != "draft-synthesis"
    )

    result = asyncio.run(handle_memory_gc(engine, {"dry_run": True}))
    payload = json.loads(result[0].text)

    assert payload["candidates"] == ["decaying"]
    assert "draft-synthesis" not in json.dumps(payload)


def test_skill_session_start_uses_lightweight_creation_only_memory():
    from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

    engine = MagicMock()
    engine.register_entity.return_value = {
        "node_id": "skill_session:skill:brainstorming:2026-01-01T00:00:00",
        "type": "skill_session",
        "name": "brainstorming",
        "is_new": True,
        "edges_created": 0,
    }
    engine.create_ordinary_if_absent.side_effect = lambda record: record["id"]

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
    assert payload["memory_id"].startswith("skill_start_")
    engine.create_ordinary_if_absent.assert_called_once()
    engine.register_memory.assert_not_called()


def test_daily_audit_guards_the_creation_only_writer(monkeypatch):
    from plastic_promise.cron import audit_daily
    from plastic_promise.defense import soul_audit

    stored = []

    class CreationOnlyEngine:
        def memory_stats_json(self):
            return "{}"

        def create_ordinary_if_absent(self, record):
            stored.append(record)
            return record["id"]

    class FailingAuditor:
        async def run_audit(self, *, scope):
            raise RuntimeError(f"audit unavailable: {scope}")

    monkeypatch.setattr(soul_audit, "SoulAuditor", FailingAuditor)

    report = audit_daily.run_sync(CreationOnlyEngine())

    assert report["stored"] is True
    assert len(stored) == 1
    assert stored[0]["source"] == "audit_daily"
