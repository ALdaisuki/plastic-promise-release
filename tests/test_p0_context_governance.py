import asyncio
import json

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.core.context_engine import ContextItem, ContextPack
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
