"""Focused contracts for retrieval routing and optional semantic health."""

from __future__ import annotations

import json


class _LegacyEmbedder:
    model_name = "legacy-cloud-model"
    dim = 2
    stats = {
        "provider": "openai-compatible",
        "revision": "legacy-cloud-model-r1",
        "requests": 0,
        "input_tokens": 0,
        "estimated_cost_usd": 0.0,
    }

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self._fail = fail

    def embed(self, _text: str) -> list[float]:
        self.calls += 1
        if self._fail:
            raise RuntimeError("https://secret-provider.invalid?token=do-not-leak")
        return [0.5, 0.5]


class _RuntimeEngine:
    def __init__(self, *, legacy_fail: bool = False) -> None:
        self._embedder = _LegacyEmbedder(fail=legacy_fail)
        self._ldb = object()
        self._graph_edges = {"edge": []}

    def _ensure_heavy_init(self) -> None:
        return None

    def _text_retrieval(self, _query: str) -> list[object]:
        return []

    def _check_rust_health(self) -> bool:
        return False


class _GovernedRuntimeEngine(_RuntimeEngine):
    def __init__(self, *, probe_error: bool = False) -> None:
        super().__init__(legacy_fail=True)
        self.probe_calls = 0
        self._probe_error = probe_error

    def retrieval_embedding_probe(
        self,
        _text: str,
    ) -> list[float]:
        self.probe_calls += 1
        if self._probe_error:
            raise RuntimeError("node request failed: bearer secret-token")
        return [0.25, 0.75]

    def retrieval_embedding_identity(self) -> dict[str, object]:
        return {
            "provider": "governed-node",
            "model": "matched-embedding-model",
            "revision": "matched-embedding-model-r1",
            "dimension": 2,
            "normalization": "l2",
            "index_identity": "sha256:" + "a" * 64,
        }

    def retrieval_embedding_usage(self) -> dict[str, object]:
        return {
            "embedding_requests": self.probe_calls,
            "embedding_input_tokens": 9 * self.probe_calls,
            "cost": 0.0,
            "cost_currency": "USD",
            "cost_usd": 0.0,
            "pricing_revision": "governed-node-local-v1",
        }


class _UnifiedLegacyRuntimeEngine(_RuntimeEngine):
    def retrieval_embedding_probe(self, _text: str) -> list[float]:
        return self._embedder.embed(_text)

    def memory_index_node_runtime(self) -> None:
        return None


def _strict_env(**overrides: str) -> dict[str, str]:
    env = {
        "PP_RETRIEVAL_FUSION_POLICY": "legacy-auto",
        "PP_FORCE_PYTHON_SUPPLY": "1",
        "LDB_INIT_ON_HEAVY_INIT": "1",
    }
    env.update(overrides)
    return env


def test_health_probes_governed_route_instead_of_legacy_embedder() -> None:
    from plastic_promise.mcp import server

    engine = _GovernedRuntimeEngine()
    identity = server._server_process_identity(engine=engine, environ=_strict_env())

    assert engine.probe_calls == 1
    assert engine._embedder.calls == 0
    assert identity["vector_ready"] is True
    assert identity["embedding_probe"] == {
        "source": "governed_route",
        "status": "ready",
    }
    assert identity["embedding"] == {
        "provider": "governed-node",
        "model": "matched-embedding-model",
        "model_revision": "matched-embedding-model-r1",
        "dimension": 2,
        "normalization": "l2",
        "index_identity": "sha256:" + "a" * 64,
        "usage": {
            "embedding_requests": 1,
            "embedding_input_tokens": 9,
            "cost": 0.0,
            "cost_currency": "USD",
            "cost_usd": 0.0,
            "pricing_revision": "governed-node-local-v1",
        },
    }


def test_health_fails_closed_when_governed_usage_reader_is_missing() -> None:
    from plastic_promise.mcp import server

    engine = _GovernedRuntimeEngine()
    engine.retrieval_embedding_usage = None

    try:
        server._server_process_identity(engine=engine, environ=_strict_env())
    except RuntimeError as exc:
        assert str(exc) == "retrieval_embedding_usage_unavailable"
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("governed health must require route-aligned usage")


def test_health_normalizes_governed_route_probe_failure() -> None:
    from plastic_promise.mcp import server

    engine = _GovernedRuntimeEngine(probe_error=True)
    identity = server._server_process_identity(
        engine=engine,
        environ=_strict_env(PP_HEALTH_ALLOW_TEXT_ONLY="1"),
    )

    encoded = json.dumps(identity)
    assert identity["identity_valid"] is True
    assert identity["vector_reason"] == "retrieval_embedding_probe_failed"
    assert identity["embedding_probe"] == {
        "source": "governed_route",
        "status": "failed",
    }
    assert "secret-token" not in encoded
    assert "bearer" not in encoded.casefold()
    assert engine._embedder.calls == 0


def test_health_keeps_legacy_probe_compatibility() -> None:
    from plastic_promise.mcp import server

    engine = _RuntimeEngine()
    identity = server._server_process_identity(engine=engine, environ=_strict_env())

    assert engine._embedder.calls == 1
    assert identity["embedding_probe"] == {
        "source": "legacy_embedder",
        "status": "ready",
    }
    assert identity["embedding"]["provider"] == "openai-compatible"


def test_health_uses_retrieval_route_when_unified_route_has_no_node_runtime() -> None:
    from plastic_promise.mcp import server

    engine = _UnifiedLegacyRuntimeEngine()
    identity = server._server_process_identity(engine=engine, environ=_strict_env())

    assert engine._embedder.calls == 1
    assert identity["embedding_probe"] == {
        "source": "retrieval_route",
        "status": "ready",
    }
    assert identity["embedding"]["provider"] == "openai-compatible"
    assert identity["embedding"]["usage"]["embedding_requests"] == 0


def test_optional_semantic_provider_failure_is_observable_but_not_core_fatal() -> None:
    from plastic_promise.core import structured_memory_fusion
    from plastic_promise.mcp import server
    from plastic_promise.passive_memory import semantic_pipeline

    engine = _RuntimeEngine()
    structured_memory_fusion._DURABLE_RUNTIME_FAILURES.add(id(engine))
    semantic_pipeline._RUNTIME_FAILURES[id(engine)] = (
        "provider failed at https://secret-provider.invalid?token=do-not-leak"
    )
    try:
        identity = server._server_process_identity(
            engine=engine,
            environ=_strict_env(
                PP_STRUCTURED_MEMORY_FUSION="on",
                PP_SYNTHESIS_ARTIFACTS="on",
                PP_PASSIVE_SEMANTIC_CAPTURE="on",
            ),
        )
    finally:
        structured_memory_fusion._DURABLE_RUNTIME_FAILURES.discard(id(engine))
        semantic_pipeline._RUNTIME_FAILURES.pop(id(engine), None)

    assert identity["identity_valid"] is True
    assert identity["vector_ready"] is True
    assert identity["degraded"] is False
    assert identity["optional_capabilities_degraded"] is True
    assert identity["optional_capabilities"] == {
        "passive_semantic_capture": {
            "enabled": True,
            "state": "degraded",
            "reason": "semantic_memory_runtime_unavailable",
        },
        "structured_memory_fusion": {
            "enabled": True,
            "state": "degraded",
            "reason": "structured_memory_fusion_runtime_unavailable",
        },
    }
    encoded = json.dumps(identity)
    assert "secret-provider" not in encoded
    assert "do-not-leak" not in encoded
