import asyncio
import json
import time

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.core.context_engine import ContextItem, ContextPack
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.context import handle_context_supply
from plastic_promise.mcp.tools.memory import handle_memory_recall


class FakeEmbedder:
    async def aembed(self, text):
        return [0.0]


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


class SlowEngine(FakeEngine):
    def supply(self, *args, **kwargs):
        time.sleep(0.2)
        return PlannerPack()


class HangingEmbedder:
    async def aembed(self, _text):
        await asyncio.Event().wait()


def test_memory_recall_surfaces_planner_metadata(monkeypatch):
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
    monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

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
    assert payload["raw_evidence"][0]["id"] == "m1"
    assert payload["request_scope_id"].endswith("req:req:p0")


def test_context_supply_prompt_renders_planner_metadata(monkeypatch):
    monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())

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
    monkeypatch.setattr(embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder())
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
    assert payload["audit_metadata"]["minimum_result"] == "degraded_context"
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

    assert payload["audit_metadata"]["mode"] == "mix"
    assert payload["core"][0]["id"] == "m1"
    assert "error" not in payload


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
