"""Focused coverage for governed foreground retrieval embeddings."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import plastic_promise.adaptive_retrieval as adaptive_retrieval
import plastic_promise.core.embedder as embedder_mod
from plastic_promise.core.context_engine import (
    ContextEngine,
    ContextPack,
    RetrievalEmbeddingError,
)
from plastic_promise.core.memory_index_node_runtime import (
    GovernedRetrievalEmbedder,
    MemoryIndexNodeRuntime,
    MemoryIndexNodeRuntimeError,
    NodeIndexEmbedding,
)
from plastic_promise.core.node_governance import NodeExecutionResult

_IDENTITY = "sha256:" + "a" * 64
_REVISION = "b" * 40
_PROFILE_DIGEST = "sha256:" + "c" * 64


def _embedding(vector: tuple[float, ...] = (0.25, 0.75)) -> NodeIndexEmbedding:
    return NodeIndexEmbedding(
        vector=vector,
        identity=_IDENTITY,
        model="BAAI/bge-m3",
        revision=_REVISION,
        dimension=len(vector),
        normalization="l2",
    )


def _runtime_with_foreground_result() -> tuple[MemoryIndexNodeRuntime, object]:
    captured = SimpleNamespace()

    class Control:
        def safe_config(self):
            return SimpleNamespace(
                revision_id="cfg-20260807T000000Z-000000000000",
                config={
                    "node_routing": {
                        "enabled": True,
                        "embedding_required_identity": _IDENTITY,
                        "embedding_policy": "remote-node-first",
                        "embedding_pinned_node_id": "",
                        "allowed_node_ids": ["remote-a"],
                    }
                },
            )

        def compute_profile_digest(self, revision_id):
            assert revision_id == "cfg-20260807T000000Z-000000000000"
            return _PROFILE_DIGEST

    class Coordinator:
        def execute_foreground(self, *, resolved, request_fingerprint, executor):
            captured.resolved = resolved
            captured.request_fingerprint = request_fingerprint
            captured.executor = executor
            return (
                NodeExecutionResult(
                    latency_ms=1.0,
                    evidence={},
                    result={
                        "embedding": [0.25, 0.75],
                        "embedding_identity": _IDENTITY,
                        "embedding_model": "BAAI/bge-m3",
                        "embedding_revision": _REVISION,
                        "embedding_dimension": 2,
                        "embedding_normalization": "l2",
                    },
                ),
                "remote-a",
                "policy-first",
                "dwj_foreground_receipt",
            )

    runtime = MemoryIndexNodeRuntime.__new__(MemoryIndexNodeRuntime)
    runtime._control_config = Control()
    runtime._coordinator = Coordinator()
    runtime._transport = object()
    return runtime, captured


def test_retrieval_embedding_uses_active_control_route_without_persistence():
    runtime, captured = _runtime_with_foreground_result()

    result = runtime.embedding_for_retrieval(
        text="find the deployment decision",
        project_id="project:alpha",
    )

    assert result.vector == _embedding().vector
    assert result.identity == _embedding().identity
    assert result.receipt_reference == "dwj_foreground_receipt"
    assert captured.resolved.project_id == "project:alpha"
    assert captured.resolved.operation == "embedding"
    assert captured.resolved.required_identity == _IDENTITY
    assert captured.resolved.scheduling_policy == "remote-node-first"
    assert captured.resolved.allowed_node_ids == ("remote-a",)
    assert captured.resolved.profile_digest == _PROFILE_DIGEST
    assert captured.resolved.input_reference.startswith("retrieval:")


def test_context_engine_governed_route_never_falls_back_to_legacy_embedder():
    engine = ContextEngine(use_sqlite=False)
    engine._heavy_init_done = True

    class LegacyEmbedder:
        dim = 2

        def embed(self, _text):
            pytest.fail("legacy embedder must not be called")

    class UnavailableRuntime:
        def embedding_for_retrieval(self, **_kwargs):
            raise MemoryIndexNodeRuntimeError("governed_node_unavailable")

    engine._embedder = LegacyEmbedder()
    engine._memory_index_node_runtime = UnavailableRuntime()

    assert engine._embed("query", project_id="project:alpha") == [0.0, 0.0]
    with pytest.raises(RetrievalEmbeddingError, match="^governed_node_unavailable$"):
        engine.retrieval_embedding_probe("query", project_id="project:alpha")


def test_principle_anchors_and_identity_share_governed_retrieval_adapter():
    calls: list[tuple[str, str]] = []

    class Runtime:
        def embedding_for_retrieval(self, *, text, project_id):
            calls.append((project_id, text))
            return _embedding()

    runtime = Runtime()
    adapter = GovernedRetrievalEmbedder(runtime)  # type: ignore[arg-type]
    engine = ContextEngine(use_sqlite=False)
    engine._heavy_init_done = True
    engine._memory_index_node_runtime = runtime
    engine._embedder = adapter

    engine._build_principle_anchors()
    identity = engine.retrieval_embedding_identity()

    assert engine._principle_anchors
    assert all(project == "project:legacy-global" for project, _text in calls)
    assert identity == {
        "provider": "governed-node",
        "model": "BAAI/bge-m3",
        "revision": _REVISION,
        "dimension": 2,
        "normalization": "l2",
        "index_identity": _IDENTITY,
    }


def test_governed_retrieval_embedder_reports_local_usage():
    class Runtime:
        def embedding_for_retrieval(self, *, text, project_id):
            assert text == "abcd"
            assert project_id == "project:alpha"
            return _embedding()

    adapter = GovernedRetrievalEmbedder(Runtime())  # type: ignore[arg-type]
    adapter.embed_for_project("abcd", project_id="project:alpha")

    stats = adapter.stats

    assert stats["provider"] == "governed-node"
    assert stats["embedding_requests"] == 1
    assert stats["embedding_input_tokens"] == 1
    assert stats["cost"] == 0.0
    assert stats["pricing_revision"] == "governed-node-local-v1"


def test_supply_keeps_principle_semantic_activation_in_the_request_project():
    engine = ContextEngine(use_sqlite=False)
    engine._ensure_heavy_init = lambda: None
    engine._inject_activated_to_graph = lambda _activated, _task_type: 0
    engine._graph_traversal = lambda _task_type: []
    engine._text_retrieval = lambda _query, trust_boost=1.0, domain_hint=None: []
    engine._vector_retrieval = lambda _vector, scope=None: []
    engine._fts_retrieval = lambda _query, scope="global": []
    engine._apply_edge_feedback = lambda: None
    engine._apply_mmr = lambda items, threshold=0.85, penalty=0.70: items
    engine._compute_divergent_quality = lambda items, all_items: items
    engine._memories = {}

    seen: dict[str, str] = {}

    def activate(task_type: str, task_description: str, *, project_id: str) -> list[str]:
        seen["task_type"] = task_type
        seen["task_description"] = task_description
        seen["project_id"] = project_id
        return []

    engine._activate_principles = activate
    engine._supply_python(
        "retrieve the project routing decision",
        [0.25, 0.75],
        task_type="architecture",
        project_id="project:alpha",
    )

    assert seen == {
        "task_type": "architecture",
        "task_description": "retrieve the project routing decision",
        "project_id": "project:alpha",
    }


@pytest.mark.parametrize(
    ("handler_name", "arguments", "text_field"),
    [
        (
            "context_supply",
            {
                "task_description": "find the project deployment decision",
                "project_id": "project:alpha",
                "request_id": "governed-context",
            },
            "task_description",
        ),
        (
            "memory_recall",
            {
                "query": "find the project deployment decision",
                "project_id": "project:alpha",
                "request_id": "governed-recall",
            },
            "query",
        ),
    ],
)
def test_live_mcp_retrieval_handlers_use_governed_embedding_route(
    monkeypatch,
    handler_name: str,
    arguments: dict[str, str],
    text_field: str,
):
    """The live MCP entry points must not rediscover a legacy embedder.

    This exercises the handlers themselves rather than asserting source text:
    their configured governed seam receives the project ID and its vector is
    what reaches ``ContextEngine.supply``.  If either handler regresses to
    ``get_embedder``/``FallbackEmbedder``, the legacy-provider tripwire and
    the routed-call assertion make the test fail.
    """
    from plastic_promise.mcp.tools import memory as memory_tools
    from plastic_promise.mcp.tools.context import handle_context_supply
    from plastic_promise.mcp.tools.memory import handle_memory_recall

    class Engine:
        def __init__(self) -> None:
            self.probe_calls: list[tuple[str, str]] = []
            self.supply_calls: list[dict[str, object]] = []

        def retrieval_embedding_probe(self, text: str, *, project_id: str) -> list[float]:
            self.probe_calls.append((text, project_id))
            return [0.25, 0.75]

        def supply(self, **kwargs):
            self.supply_calls.append(kwargs)
            return ContextPack()

    def legacy_provider_must_not_be_discovered(*_args, **_kwargs):
        pytest.fail("MCP retrieval handler must use the governed embedding seam")

    engine = Engine()
    monkeypatch.setattr(embedder_mod, "get_embedder", legacy_provider_must_not_be_discovered)
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda _query: True)
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()

    if handler_name == "context_supply":
        result = asyncio.run(handle_context_supply(engine, arguments))
    else:
        result = asyncio.run(handle_memory_recall(engine, arguments))

    assert engine.probe_calls == [(arguments[text_field], "project:alpha")]
    assert len(engine.supply_calls) == 1
    assert engine.supply_calls[0]["task_vector"] == [0.25, 0.75]
    assert engine.supply_calls[0]["project_id"] == "project:alpha"
    assert "error" not in result[0].text
    if handler_name == "memory_recall":
        assert json.loads(result[0].text)["project_id"] == "project:alpha"


@pytest.mark.parametrize(
    ("handler_name", "arguments", "timeout_variable"),
    [
        (
            "context_supply",
            {
                "task_description": "find the project deployment decision",
                "project_id": "project:alpha",
            },
            "PP_CONTEXT_EMBED_TIMEOUT_SEC",
        ),
        (
            "memory_recall",
            {
                "query": "find the project deployment decision",
                "project_id": "project:alpha",
            },
            "PP_MEMORY_RECALL_EMBED_TIMEOUT_SEC",
        ),
    ],
)
def test_live_mcp_retrieval_handlers_degrade_without_legacy_provider_on_governed_timeout(
    monkeypatch,
    handler_name: str,
    arguments: dict[str, str],
    timeout_variable: str,
):
    """A governed route timeout remains a text-only retrieval, not a bypass."""
    from plastic_promise.mcp.tools import memory as memory_tools
    from plastic_promise.mcp.tools.context import handle_context_supply
    from plastic_promise.mcp.tools.memory import handle_memory_recall

    class Engine:
        def __init__(self) -> None:
            self.task_vectors: list[list[float] | None] = []
            self.pack = ContextPack()

        def retrieval_embedding_probe(self, _text: str, *, project_id: str) -> list[float]:
            assert project_id == "project:alpha"
            time.sleep(0.05)
            return [0.25, 0.75]

        def supply(self, **kwargs):
            self.task_vectors.append(kwargs["task_vector"])
            return self.pack

    def legacy_provider_must_not_be_discovered(*_args, **_kwargs):
        pytest.fail("governed timeout must not trigger legacy provider discovery")

    engine = Engine()
    monkeypatch.setenv(timeout_variable, "0.001")
    monkeypatch.setattr(embedder_mod, "get_embedder", legacy_provider_must_not_be_discovered)
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda _query: True)
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()

    if handler_name == "context_supply":
        result = asyncio.run(handle_context_supply(engine, arguments))
    else:
        result = asyncio.run(handle_memory_recall(engine, arguments))

    assert engine.task_vectors == [[]]
    assert engine.pack.audit_metadata["retrieval_embedding"] == {
        "route": "text-only-degraded",
        "degraded": True,
        "reason": "retrieval_embedding_timeout",
    }
    assert "error" not in result[0].text


def test_context_supply_timeout_exposes_text_only_degradation_in_response_explain_and_trace(
    monkeypatch,
):
    """A governed timeout stays observable across the MCP response and trace."""
    import plastic_promise.core.traceability as traceability
    from plastic_promise.mcp.tools.context import handle_context_supply

    class Engine:
        def __init__(self) -> None:
            self.pack = ContextPack(pipeline_stats={"candidate_count": 0})

        def retrieval_embedding_probe(self, _text: str, *, project_id: str) -> list[float]:
            assert project_id == "project:alpha"
            time.sleep(0.05)
            return [0.25, 0.75]

        def supply(self, **kwargs):
            assert kwargs["task_vector"] == []
            return self.pack

    spans: list[dict[str, object]] = []
    degradations: list[dict[str, object]] = []
    monkeypatch.setenv("PP_CONTEXT_EMBED_TIMEOUT_SEC", "0.001")
    monkeypatch.setenv("PP_RETRIEVAL_EXPLAIN", "1")
    monkeypatch.setattr(
        traceability,
        "defer_record_call_span",
        lambda _engine, **kwargs: spans.append(kwargs) or True,
    )
    monkeypatch.setattr(
        traceability,
        "defer_record_degradation_event",
        lambda _engine, **kwargs: degradations.append(kwargs) or True,
    )

    result = asyncio.run(
        handle_context_supply(
            Engine(),
            {
                "task_description": "find the project deployment decision",
                "project_id": "project:alpha",
                "call_id": "governed-context-timeout",
                "response_mode": "debug",
                "diagnostics_level": "full",
            },
        )
    )

    payload = json.loads(result[0].text)
    assert payload["degraded"] is True
    assert payload["degradation_reason"] == "retrieval_embedding_timeout"
    assert payload["minimum_result"] == "text_only_context"
    assert payload["retrieval_embedding"] == {
        "route": "text-only-degraded",
        "degraded": True,
        "reason": "retrieval_embedding_timeout",
    }
    assert len(spans) == 1
    assert spans[0]["degraded"] is True
    metadata = spans[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["degradation_reason"] == "retrieval_embedding_timeout"
    assert metadata["retrieval_embedding"] == payload["retrieval_embedding"]
    explain = metadata["retrieval_explain_v1"]
    assert explain["pipeline"] == {
        "candidate_count": 0,
        "degraded": True,
        "degradation_state": "text-only-degraded",
        "fallback_reason": "retrieval_embedding_timeout",
    }
    assert len(degradations) == 1
    assert degradations[0]["metadata"] == {"reason": "retrieval_embedding_timeout"}
