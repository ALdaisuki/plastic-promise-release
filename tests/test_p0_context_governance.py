import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.core.context_engine import ContextItem, ContextPack
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.context import handle_context_supply
from plastic_promise.mcp.tools.memory import handle_memory_recall


def _healthy_embedding():
    """Return a 2560-dimensional, unit-L2 test embedding."""
    return [1.0] + [0.0] * 2559


class FakeEmbedder:
    async def aembed(self, text):
        # Match the governed 2560-dimensional embedding contract used by the
        # current compute-node profile, including its non-zero L2 contract.
        return _healthy_embedding()


class PlannerPack(ContextPack):
    def __init__(self):
        super().__init__()
        self.core = [ContextItem("m1", "project memory", 0.91, source="codex")]
        self.audit_metadata = {
            "mode": "mix",
            "budget": {"core": 8, "related": 12, "divergent": 6, "raw_evidence": 10},
            "raw_evidence": [
                {
                    "id": "m1",
                    "source": "bm25",
                    "score": 0.91,
                    "content": "project memory",
                }
            ],
        }


class FakeEngine:
    def __init__(self):
        self._memories = {
            "m1": {
                "project_id": "project:app",
                "visibility": "project",
                "source_class": "experience",
            }
        }

    def supply(self, *args, **kwargs):
        return PlannerPack()

    def retrieval_embedding_probe(self, text, *, project_id):
        """Expose the governed retrieval seam used by context_supply.

        The production MCP boundary no longer rediscovers a legacy embedder
        provider.  Keep this fixture on that seam so latency tests exercise a
        healthy, contract-compliant 2560-dimensional route instead of the
        intentional text-only degradation path.
        """
        assert project_id == "project:app"
        return _healthy_embedding()


class SlowEngine(FakeEngine):
    def supply(self, *args, **kwargs):
        time.sleep(0.2)
        return PlannerPack()


class HangingEmbedder:
    async def aembed(self, _text):
        await asyncio.Event().wait()


def test_memory_recall_surfaces_planner_metadata(monkeypatch):
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )

    result = asyncio.run(
        handle_memory_recall(
            FakeEngine(),
            {
                "query": "architecture context",
                "task_type": "architecture",
                "project_id": "project:app",
                "request_id": "req:p0",
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["mode"] == "mix"
    assert payload["budget"]["raw_evidence"] == 10
    assert payload["diagnostics"]["summary"]["retrieval_mode"] == "mix"
    assert "raw_evidence" not in payload
    assert payload["request_scope_id"].endswith("req:req:p0")


def test_context_supply_prompt_renders_planner_metadata(monkeypatch):
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )

    result = asyncio.run(
        handle_context_supply(
            FakeEngine(),
            {
                "task_description": "architecture context",
                "task_type": "architecture",
                "project_id": "project:app",
                "request_id": "req:p0ctx",
            },
        )
    )
    text = result[0].text

    assert "## [RETRIEVAL_PLAN]" in text
    assert "- mode: mix" in text
    assert "- raw_evidence_budget: 10" in text
    assert "[bm25] m1" in text


def test_context_supply_times_out_blocking_engine(monkeypatch):
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )
    monkeypatch.setenv("PP_CONTEXT_SUPPLY_TIMEOUT_SEC", "0.01")

    started = time.monotonic()
    result = asyncio.run(
        handle_context_supply(
            SlowEngine(),
            {
                "task_description": "blocking context path",
                "task_type": "architecture",
                "project_id": "project:app",
                "request_id": "req:timeout",
                "debug": True,
            },
        )
    )
    elapsed = time.monotonic() - started
    payload = json.loads(result[0].text)

    assert elapsed < 0.15
    assert payload["minimum_result"] == "degraded_context"
    assert payload["diagnostics"]["summary"]["trace"]["project_id"] == "project:app"
    assert "timed out" in payload["error"]


def test_context_supply_embedding_timeout_uses_sync_fallback(monkeypatch):
    monkeypatch.setattr(
        embedder_mod,
        "get_embedder",
        lambda fallback_on_error=False: HangingEmbedder(),
    )
    monkeypatch.setenv("PP_CONTEXT_EMBED_TIMEOUT_SEC", "0.01")

    result = asyncio.run(
        handle_context_supply(
            FakeEngine(),
            {
                "task_description": "embedding timeout",
                "task_type": "architecture",
                "project_id": "project:app",
                "request_id": "req:embed-timeout",
                "debug": True,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["diagnostics"]["summary"]["retrieval_mode"] == "mix"
    assert payload["core"][0]["id"] == "m1"
    assert "error" not in payload


def test_context_supply_does_not_wait_for_trace_persistence(monkeypatch, tmp_path):
    from plastic_promise.core import traceability

    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )
    database = tmp_path / "trace.db"
    connection = sqlite3.connect(database, check_same_thread=False)
    traceability.ensure_traceability_schema(connection)
    engine = FakeEngine()
    engine._sqlite = SimpleNamespace(_conn=connection, _db_path=str(database))

    def slow_record_call_span(_connection, **_kwargs):
        time.sleep(0.2)

    monkeypatch.setattr(traceability, "record_call_span", slow_record_call_span)

    started = time.monotonic()
    result = asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "trace persistence latency",
                "task_type": "architecture",
                "project_id": "project:app",
                "response_mode": "compact",
            },
        )
    )
    elapsed = time.monotonic() - started
    payload = json.loads(result[0].text)

    drain = getattr(traceability, "drain_deferred_trace_writes", lambda timeout=1.0: True)
    assert drain(timeout=1.0) is True
    connection.close()
    assert payload["degraded"] is False
    assert elapsed < 0.15


def test_governed_recall_cache_key_tracks_canonical_memory_version():
    before = memory_tools._cache_key(
        "architecture context",
        "architecture",
        20,
        "global",
        memory_version=17,
    )
    after = memory_tools._cache_key(
        "architecture context",
        "architecture",
        20,
        "global",
        memory_version=18,
    )

    assert before != after
