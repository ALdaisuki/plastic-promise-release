import asyncio
import json
from types import SimpleNamespace

import pytest

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.memory import handle_memory_recall
from plastic_promise.mcp.tools.review import handle_review_run


def _item(
    memory_id,
    content,
    project_id,
    visibility="project",
    source_class="experience",
    relevance=0.9,
):
    return SimpleNamespace(
        id=memory_id,
        content=content,
        relevance=relevance,
        source="codex",
        freshness="valid",
        worth_score=0.8,
        metadata={
            "project_id": project_id,
            "visibility": visibility,
            "source_class": source_class,
        },
    )


class FakePack:
    def __init__(self):
        self.core = [
            _item("same-core", "same project core memory", "project:app", "project"),
            _item("other-core", "other project core memory", "project:other", "project"),
            _item("global-core", "global core memory", "project:legacy-global", "global"),
            _item("telemetry-core", "telemetry core memory", "project:app", "project", "telemetry"),
            _item("prompt-core", "prompt core memory", "project:app", "project", "prompt"),
        ]
        self.related = [
            _item("same-related", "same project related memory", "project:app", "project"),
            _item("other-related", "other project related memory", "project:other", "project"),
            _item("global-related", "global related memory", "project:legacy-global", "global"),
            _item(
                "telemetry-related",
                "telemetry related memory",
                "project:app",
                "project",
                "telemetry",
            ),
            _item("prompt-related", "prompt related memory", "project:app", "project", "prompt"),
        ]
        self.divergent = [
            _item("same-divergent", "same project divergent memory", "project:app", "project"),
            _item("shared-divergent", "shared inspiration", "project:other", "shared"),
            _item("global-divergent", "global inspiration", "project:legacy-global", "global"),
            _item("private-divergent", "private other inspiration", "project:other", "project"),
        ]
        self.activated_principles = ["Context Driven"]
        self.total_items = 14
        self.audit_metadata = {}
        self.pipeline_stats = {}
        self.per_item_stats = []


class FakeEngine:
    def supply(self, query, vec, task_type, scope, debug=False):
        return FakePack()


class FakeEmbedder:
    async def aembed(self, text):
        return [0.1, 0.2, 0.3, 0.4]


@pytest.fixture(autouse=True)
def recall_handler_patches(monkeypatch):
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
    monkeypatch.setattr(
        embedder_mod,
        "get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )


def _recall_payload(args):
    result = asyncio.run(handle_memory_recall(FakeEngine(), args))
    return json.loads(result[0].text)


def test_memory_recall_filters_core_and_related_by_project_visibility():
    payload = _recall_payload(
        {
            "query": "diff review",
            "project_id": "project:app",
            "project_policy": "balanced",
            "max_results": 20,
            "request_id": "req:project-filter",
        }
    )

    assert [item["id"] for item in payload["core"]] == ["same-core", "global-core"]
    assert [item["id"] for item in payload["related"]] == [
        "same-related",
        "global-related",
    ]
    assert "data" in payload
    assert [item["id"] for item in payload["data"]["core"]] == ["same-core", "global-core"]
    assert payload["activated_principles"] == ["Context Driven"]
    assert payload["total_items"] == 14


def test_memory_recall_preserves_shared_and_global_divergent_for_open_policy():
    payload = _recall_payload(
        {
            "query": "diff review",
            "project_id": "project:app",
            "project_policy": "open",
            "max_results": 20,
            "request_id": "req:open-divergent",
        }
    )

    assert [item["id"] for item in payload["divergent"]] == [
        "same-divergent",
        "shared-divergent",
        "global-divergent",
    ]


def test_memory_recall_excludes_cross_project_shared_divergent_when_strict():
    payload = _recall_payload(
        {
            "query": "diff review",
            "project_id": "project:app",
            "project_policy": "strict",
            "max_results": 20,
            "request_id": "req:strict-divergent",
        }
    )

    assert [item["id"] for item in payload["divergent"]] == [
        "same-divergent",
        "global-divergent",
    ]


def test_unknown_project_degrades_visibly_and_restricts_core_related_to_global(monkeypatch):
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.delenv("PP_PROJECT_ID", raising=False)

    payload = _recall_payload(
        {
            "query": "diff review",
            "scope": "building",
            "max_results": 20,
            "request_id": "req:unknown-project",
        }
    )

    assert payload["degraded"] is True
    assert payload["minimum_result"] == "project_restricted_context"
    assert "project_id unresolved; using project:unknown" in payload["warnings"]
    assert [item["id"] for item in payload["core"]] == ["global-core"]
    assert [item["id"] for item in payload["related"]] == ["global-related"]
    assert payload["trace"]["project_id"] == "project:unknown"


def test_recall_envelope_trace_preserves_backward_compatible_top_level_fields():
    payload = _recall_payload(
        {
            "query": "diff review",
            "project_id": "app",
            "max_results": 20,
            "stage_session_id": "stage:codex:test",
            "flow_line_id": "normal-development",
            "request_id": "req:trace",
            "call_id": "call_fixed",
        }
    )

    assert payload["trace"] == {
        "call_id": "call_fixed",
        "request_scope_id": "stage:codex:test::flow:normal-development::req:req:trace",
        "project_id": "project:app",
    }
    assert payload["data"]["trace"] == payload["trace"]
    assert payload["core"] == payload["data"]["core"]
    assert payload["related"] == payload["data"]["related"]
    assert payload["divergent"] == payload["data"]["divergent"]


def test_review_prepare_requires_project_id():
    result = asyncio.run(handle_review_run(object(), {"action": "prepare"}))
    payload = json.loads(result[0].text)

    assert payload["error"] == "project_id is required for review prepare"
    assert payload["degraded"] is True
    assert payload["minimum_result"] == "review_project_guard"


def test_context_supply_filters_prompt_layers_by_project_metadata(monkeypatch):
    from plastic_promise.core.context_engine import ContextItem, ContextPack
    from plastic_promise.mcp.tools.context import handle_context_supply

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0] * 4

    class FakeEngine:
        def __init__(self):
            self._memories = {
                "same-core": {
                    "project_id": "project:app",
                    "visibility": "project",
                    "source_class": "experience",
                },
                "other-core": {
                    "project_id": "project:other",
                    "visibility": "project",
                    "source_class": "experience",
                },
                "global-core": {
                    "project_id": "project:legacy-global",
                    "visibility": "global",
                    "source_class": "experience",
                },
                "prompt-core": {
                    "project_id": "project:app",
                    "visibility": "project",
                    "source_class": "prompt",
                },
            }

        def supply(self, task_description, task_vector, task_type, scope, **kwargs):
            pack = ContextPack()
            pack.core = [
                ContextItem("same-core", "same project prompt context", 0.95),
                ContextItem("other-core", "other project prompt context", 0.94),
                ContextItem("global-core", "global prompt context", 0.93),
                ContextItem("prompt-core", "prompt source context", 0.92),
            ]
            return pack

    monkeypatch.setattr(
        "plastic_promise.core.embedder.get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )

    result = asyncio.run(
        handle_context_supply(
            FakeEngine(),
            {
                "task_description": "release prompt filtering",
                "project_id": "project:app",
                "project_policy": "balanced",
            },
        )
    )
    text = result[0].text

    assert "same project prompt context" in text
    assert "global prompt context" in text
    assert "other project prompt context" not in text
    assert "prompt source context" not in text
