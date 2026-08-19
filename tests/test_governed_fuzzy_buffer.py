from __future__ import annotations

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.mcp.tools import memory as memory_tools
from scripts.benchmark_recall_quality import _validated_embedding_identity


class _GovernedEmbedder:
    model_name = "Qwen3-Embedding-4B-GGUF"
    index_model_name = model_name
    dim = 2560


def test_fuzzy_buffer_reuses_engine_governed_embedder(monkeypatch):
    engine = ContextEngine(use_sqlite=False)
    governed = _GovernedEmbedder()
    engine._embedder = governed
    engine.memory_index_node_runtime = lambda: object()

    def fail_global_embedder():
        raise AssertionError("governed route must not call the global embedder")

    monkeypatch.setattr(
        "plastic_promise.core.server_embedder.get_embedder",
        fail_global_embedder,
    )

    buffer = memory_tools._get_fuzzy_buffer(engine)

    assert buffer.embedder is governed


def test_fuzzy_buffer_fails_closed_when_governed_embedder_initialization_fails(monkeypatch):
    engine = ContextEngine(use_sqlite=False)
    engine.memory_index_node_runtime = lambda: object()

    def fail_governed_embedder():
        raise RuntimeError("governed route unavailable")

    engine.ensure_runtime_embedder = fail_governed_embedder

    def fail_global_embedder():
        raise AssertionError("governed failure must not rediscover a global embedder")

    monkeypatch.setattr(
        "plastic_promise.core.server_embedder.get_embedder",
        fail_global_embedder,
    )

    with pytest.raises(RuntimeError, match="governed route unavailable"):
        memory_tools._get_fuzzy_buffer(engine)


def test_cached_fuzzy_buffer_rebinds_after_governed_hot_switch(monkeypatch):
    engine = ContextEngine(use_sqlite=False)
    legacy = _GovernedEmbedder()
    governed = _GovernedEmbedder()
    governed.model_name = "governed-after-switch"
    cached = type("CachedPipeline", (), {"embedder": legacy})()
    monkeypatch.setitem(memory_tools._fuzzy_buffers, id(engine), cached)
    engine.memory_index_node_runtime = lambda: object()
    engine.ensure_runtime_embedder = lambda: governed

    buffer = memory_tools._get_fuzzy_buffer(engine)

    assert buffer is cached
    assert buffer.embedder is governed


def test_benchmark_accepts_governed_embedding_health_identity():
    identity = _validated_embedding_identity(
        {
            "provider": "governed-node",
            "model": "Qwen3-Embedding-4B-GGUF",
            "model_revision": "f4602530db1d980e16da9d7d3a70294cf5c190be",
            "dimension": 2560,
            "normalization": "l2",
            "index_identity": "sha256:6c7ae71d639b2132d37c9781f89a58fa326b2b081196437832ed92e30aff1205",
            "usage": {
                "embedding_requests": 0,
                "embedding_input_tokens": 0,
                "cost": 0.0,
                "cost_currency": "USD",
                "cost_usd": 0.0,
                "pricing_revision": "governed-node-local-v1",
            },
        }
    )

    assert identity["provider"] == "governed-node"
    assert identity["dimension"] == 2560
    assert identity["usage"]["embedding_requests"] == 0


def test_benchmark_rejects_governed_identity_without_usage():
    with pytest.raises(RuntimeError, match="health_embedding_identity_invalid"):
        _validated_embedding_identity(
            {
                "provider": "governed-node",
                "model": "Qwen3-Embedding-4B-GGUF",
                "model_revision": "f4602530db1d980e16da9d7d3a70294cf5c190be",
                "dimension": 2560,
                "normalization": "l2",
                "index_identity": "sha256:6c7ae71d639b2132d37c9781f89a58fa326b2b081196437832ed92e30aff1205",
            }
        )
