import asyncio
import json
from types import SimpleNamespace

import pytest

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.core.context_engine import ContextEngine, ContextItem, ContextPack
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.context import handle_context_supply
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
    assert payload["core"][0]["project_id"] == "project:app"
    assert payload["core"][0]["origin_scope"] == "project"
    assert payload["core"][1]["visibility"] == "global"
    assert payload["core"][1]["origin_scope"] == "global"
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
    assert payload["divergent"][1]["origin_scope"] == "shared"


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


def _leaky_project_engine():
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "visible-project-item": {
            "id": "visible-project-item",
            "memory_type": "experience",
            "project_id": "project:app",
            "visibility": "project",
            "source_class": "experience",
        },
        "private-project-item": {
            "id": "private-project-item",
            "memory_type": "experience",
            "project_id": "project:other",
            "visibility": "project",
            "source_class": "experience",
        },
    }
    calls = {"count": 0}

    def supply(*args, **kwargs):
        calls["count"] += 1
        visible = ContextItem(
            "visible-project-item",
            "VISIBLE-PROJECT-CONTENT",
            0.95,
            source="user",
            layer="core",
        )
        private = ContextItem(
            "private-project-item",
            "PRIVATE-PROJECT-SECRET",
            0.94,
            source="user",
            layer="core",
        )
        pack = ContextPack(core=[visible, private])
        pack.audit_metadata = {
            "mode": "global",
            "budget": {"core": 6, "related": 10, "divergent": 6, "raw_evidence": 8},
            "raw_evidence": [
                {
                    "id": "visible-project-item",
                    "content": "VISIBLE-PROJECT-CONTENT",
                    "score": 0.95,
                    "source": "text",
                },
                {
                    "id": "private-project-item",
                    "content": "PRIVATE-PROJECT-SECRET",
                    "score": 0.94,
                    "source": "text",
                },
            ],
            "context_recommender": {
                "recommendations": [
                    {"id": "visible-project-item", "reason": "visible"},
                    {
                        "id": "private-project-item",
                        "reason": "PRIVATE-PROJECT-SECRET",
                    },
                ]
            },
            "private_debug": "PRIVATE-PROJECT-SECRET",
        }
        pack.per_item_stats = [
            {"id": "visible-project-item", "content": "VISIBLE-PROJECT-CONTENT"},
            {"id": "private-project-item", "content": "PRIVATE-PROJECT-SECRET"},
        ]
        pack.pipeline_stats = {
            "private_debug": "PRIVATE-PROJECT-SECRET",
        }
        pack.gap_signal = {
            "evidence": [
                {"id": "private-project-item", "content": "PRIVATE-PROJECT-SECRET"}
            ],
            "recommendations": ["PRIVATE-PROJECT-SECRET", "safe"],
        }
        return pack

    engine.supply = supply
    return engine, calls


def test_memory_recall_scrubs_private_project_from_first_and_cached_response():
    engine, calls = _leaky_project_engine()
    args = {
        "query": "project private exposure",
        "project_id": "project:app",
        "project_policy": "strict",
        "request_id": "project-exposure-cache",
        "debug": True,
    }

    first_text = asyncio.run(handle_memory_recall(engine, args))[0].text
    second_text = asyncio.run(handle_memory_recall(engine, args))[0].text
    first = json.loads(first_text)
    second = json.loads(second_text)

    assert calls["count"] == 1
    assert "PRIVATE-PROJECT-SECRET" not in first_text
    assert "PRIVATE-PROJECT-SECRET" not in second_text
    assert [item["id"] for item in first["core"]] == ["visible-project-item"]
    assert first["core"] == first["data"]["core"]
    assert second["core"] == second["data"]["core"]
    assert [row["id"] for row in first["per_item_stats"]] == ["visible-project-item"]
    assert [row["id"] for row in second["data"]["per_item_stats"]] == [
        "visible-project-item"
    ]


def test_context_supply_debug_scrubs_private_project_from_all_surfaces(monkeypatch):
    engine, _calls = _leaky_project_engine()
    monkeypatch.setattr(
        embedder_mod,
        "get_embedder",
        lambda fallback_on_error=False: FakeEmbedder(),
    )

    result = asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "project private exposure",
                "project_id": "project:app",
                "project_policy": "strict",
                "request_id": "project-exposure-context",
                "debug": True,
            },
        )
    )
    text = result[0].text
    payload = json.loads(text)

    assert "PRIVATE-PROJECT-SECRET" not in text
    assert [item["id"] for item in payload["core"]] == ["visible-project-item"]
    assert [row["id"] for row in payload["per_item_stats"]] == ["visible-project-item"]
    assert payload["audit_metadata"]["raw_evidence"][0]["id"] == "visible-project-item"


def _stale_sqlite_project_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "stale-project.db"))
    engine = ContextEngine(use_sqlite=True)
    engine.register_memory(
        {
            "id": "cross-process-private",
            "content": "CROSS-PROCESS-PROJECT-SECRET",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:app",
            "visibility": "global",
            "source_class": "experience",
        }
    )
    engine._sqlite._conn.execute(
        """
        UPDATE memories
        SET project_id = 'project:other', visibility = 'project', source_class = 'experience'
        WHERE id = 'cross-process-private'
        """
    )
    engine._sqlite._conn.commit()
    calls = {"count": 0}

    def supply(*args, **kwargs):
        calls["count"] += 1
        item = ContextItem(
            "cross-process-private",
            "CROSS-PROCESS-PROJECT-SECRET",
            0.96,
            source="user",
            layer="core",
        )
        pack = ContextPack(core=[item])
        pack.audit_metadata = {
            "mode": "global",
            "budget": {"core": 6, "related": 10, "divergent": 6, "raw_evidence": 8},
            "raw_evidence": [
                {
                    "id": "cross-process-private",
                    "content": "CROSS-PROCESS-PROJECT-SECRET",
                    "score": 0.96,
                    "source": "text",
                }
            ],
        }
        pack.per_item_stats = [
            {
                "id": "cross-process-private",
                "content": "CROSS-PROCESS-PROJECT-SECRET",
            }
        ]
        return pack

    engine.supply = supply
    return engine, calls


def test_cross_process_project_change_scrubs_first_cache_and_context_supply(
    tmp_path,
    monkeypatch,
):
    engine, calls = _stale_sqlite_project_engine(tmp_path, monkeypatch)
    recall_args = {
        "query": "cross process project visibility",
        "project_id": "project:app",
        "project_policy": "strict",
        "request_id": "cross-process-project-cache",
        "debug": True,
    }

    first_text = asyncio.run(handle_memory_recall(engine, recall_args))[0].text
    second_text = asyncio.run(handle_memory_recall(engine, recall_args))[0].text
    second = json.loads(second_text)

    assert calls["count"] == 1
    assert "CROSS-PROCESS-PROJECT-SECRET" not in first_text
    assert "CROSS-PROCESS-PROJECT-SECRET" not in second_text
    assert second["core"] == []
    assert second["data"]["core"] == []
    assert second["raw_evidence"] == []
    assert second["data"]["raw_evidence"] == []

    context_text = asyncio.run(
        handle_context_supply(
            engine,
            {
                "task_description": "cross process project visibility",
                "project_id": "project:app",
                "project_policy": "strict",
                "request_id": "cross-process-project-context",
                "debug": True,
            },
        )
    )[0].text

    assert "CROSS-PROCESS-PROJECT-SECRET" not in context_text
