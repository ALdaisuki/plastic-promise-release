"""Tests for the governed Qwen3/Ollama local inference node backends."""

from __future__ import annotations

import pytest

from plastic_promise.local_inference_node import NodeConfigurationError
from plastic_promise.local_inference_node import runtime as runtime_module
from plastic_promise.local_inference_node.adapters import (
    NodeModelUnavailableError,
    Qwen3CrossEncoderReranker,
)
from plastic_promise.local_inference_node.runtime import (
    NodeRuntimeConfig,
    bind_model_artifact_identity,
    create_embedding_engine,
    create_reranking_engine,
)

_EMBEDDING_REVISION = "c" * 40
_RERANK_REVISION = "d" * 40


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "PP_LOCAL_NODE_EMBEDDING_MODEL": "qwen3-embedding:4b",
        "PP_LOCAL_NODE_EMBEDDING_REVISION": _EMBEDDING_REVISION,
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION": "2560",
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "l2",
        "PP_LOCAL_NODE_EMBEDDING_BACKEND": "ollama",
        "PP_LOCAL_NODE_RERANK_MODEL": "Qwen/Qwen3-Reranker-4B",
        "PP_LOCAL_NODE_RERANK_REVISION": _RERANK_REVISION,
        "PP_LOCAL_NODE_RERANK_BACKEND": "qwen3-cross-encoder",
    }
    values.update(overrides)
    return values


class _FakeCrossEncoder:
    def __init__(self, *, scores: list[float]) -> None:
        self._scores = scores

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        assert len(pairs) == len(self._scores)
        return list(self._scores)


def _fake_loader(scores: list[float]):
    def load(model_reference: str, **kwargs):
        return _FakeCrossEncoder(scores=scores)

    return load


def test_qwen3_cross_encoder_rerank_returns_every_candidate_once():
    reranker = Qwen3CrossEncoderReranker(
        model_reference="/models/rerank",
        revision=_RERANK_REVISION,
        loader=_fake_loader([6.4375, -14.375, 0.5]),
    )
    result = reranker.rerank_tuples(
        "What is the capital of China?",
        [(0, "Beijing is the capital."), (1, "Gravity is a force."), (2, "It rains a lot.")],
        top_k=2,
    )
    assert [index for index, _score in result] == [0, 1, 2]
    assert result[0] == (0, 6.4375)
    assert result[1] == (1, -14.375)


def test_qwen3_cross_encoder_rerank_rejects_incomplete_scores():
    class BadLoader:
        def predict(self, pairs):
            return [0.0]

    reranker = Qwen3CrossEncoderReranker(
        model_reference="/models/rerank",
        revision=_RERANK_REVISION,
        loader=lambda *a, **k: BadLoader(),
    )
    with pytest.raises(NodeModelUnavailableError, match="node_rerank_result_incomplete"):
        reranker.rerank_tuples("q", [(0, "a"), (1, "b")], top_k=1)


def test_qwen3_cross_encoder_rerank_fails_closed_on_loader_error():
    def failing_loader(*args, **kwargs):
        raise OSError("missing model tree")

    with pytest.raises(NodeModelUnavailableError, match="node_rerank_model_unavailable"):
        Qwen3CrossEncoderReranker(
            model_reference="/models/rerank",
            revision=_RERANK_REVISION,
            loader=failing_loader,
        )


def test_runtime_allows_governed_ollama_embedding_backend():
    config = NodeRuntimeConfig.from_environment(_environment())
    assert config.embedding_backend == "ollama"
    assert config.identity.embedding_model == "qwen3-embedding:4b"
    assert config.identity.embedding_dimension == 2560
    assert config.identity.embedding_normalization == "l2"


def test_runtime_allows_qwen3_cross_encoder_rerank_backend():
    config = NodeRuntimeConfig.from_environment(_environment())
    assert config.rerank_backend == "qwen3-cross-encoder"
    assert config.identity.rerank_model == "Qwen/Qwen3-Reranker-4B"


def test_runtime_rejects_unknown_backends():
    with pytest.raises(NodeConfigurationError, match="node_embedding_backend_invalid"):
        NodeRuntimeConfig.from_environment(_environment(PP_LOCAL_NODE_EMBEDDING_BACKEND="unknown"))
    with pytest.raises(NodeConfigurationError, match="node_rerank_backend_invalid"):
        NodeRuntimeConfig.from_environment(_environment(PP_LOCAL_NODE_RERANK_BACKEND="unknown"))


def test_runtime_accepts_explicit_docker_desktop_ollama_host():
    config = NodeRuntimeConfig.from_environment(
        _environment(PP_LOCAL_NODE_OLLAMA_HOST="http://host.docker.internal:11434")
    )
    assert config.ollama_host == "http://host.docker.internal:11434"


def test_runtime_rejects_public_ollama_host():
    with pytest.raises(NodeConfigurationError, match="node_ollama_host_not_allowed"):
        NodeRuntimeConfig.from_environment(
            _environment(PP_LOCAL_NODE_OLLAMA_HOST="http://192.168.1.10:11434")
        )


def test_create_embedding_engine_selects_ollama_adapter(monkeypatch):
    config = NodeRuntimeConfig.from_environment(_environment())
    seen = {}

    class FakeOllamaAdapter:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

    monkeypatch.setattr(runtime_module, "OllamaEmbeddingAdapter", FakeOllamaAdapter)
    create_embedding_engine(
        config,
        embedding_artifact_sha256="sha256:" + "a" * 64,
    )
    assert seen["kwargs"]["expected_dimension"] == 2560
    assert seen["kwargs"]["model"] == "qwen3-embedding:4b"
    assert seen["kwargs"]["normalization"] == "l2"


def test_create_reranking_engine_selects_qwen3(monkeypatch):
    config = NodeRuntimeConfig.from_environment(_environment())
    seen = {}

    class FakeQwen3Reranker:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

    monkeypatch.setattr(runtime_module, "Qwen3CrossEncoderReranker", FakeQwen3Reranker)
    engine = create_reranking_engine(config)
    assert engine is not None
    assert seen["kwargs"]["model_reference"] == "/models/rerank"
    assert seen["kwargs"]["revision"] == _RERANK_REVISION


def test_bind_ollama_model_artifact_identity(monkeypatch):
    config = NodeRuntimeConfig.from_environment(_environment())
    monkeypatch.setattr(
        runtime_module,
        "_ollama_model_digest",
        lambda host, model: "sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        runtime_module,
        "_model_tree_sha256",
        lambda root: "sha256:" + "f" * 64,
    )
    identity = bind_model_artifact_identity(config)
    assert identity.embedding_artifact_sha256 == "sha256:" + "e" * 64
    assert identity.rerank_artifact_sha256 == "sha256:" + "f" * 64


def test_ollama_model_digest_reads_loopback_tags(monkeypatch):
    import json

    payload = json.dumps({"models": [{"name": "qwen3-embedding:4b", "digest": "ab" * 32}]}).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return payload

    monkeypatch.setattr(
        runtime_module,
        "urlopen",
        lambda url, timeout: FakeResponse(),
    )
    assert runtime_module._ollama_model_digest("http://127.0.0.1:11434", "qwen3-embedding:4b") == (
        "sha256:" + "ab" * 32
    )
