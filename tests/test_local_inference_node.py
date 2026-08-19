from __future__ import annotations

import asyncio
import threading
from hashlib import sha256

import httpx
import pytest

from plastic_promise.local_inference_node import NodeConfigurationError, NodeIdentity, NodeLimits
from plastic_promise.local_inference_node.adapters import NodeModelIdentityDriftError
from plastic_promise.local_inference_node.app import create_node_app
from plastic_promise.local_inference_node.runtime import (
    NodeRuntimeConfig,
    bind_model_artifact_identity,
)
from plastic_promise.local_inference_node.server import (
    create_runtime_app,
    main,
    validate_loopback_bind_host,
)

_EMBEDDING_REVISION = "a" * 40
_RERANK_REVISION = "b" * 40
_AUTHORIZATION = "Bearer private-node-test-token"
_AUTH_HEADERS = {"Authorization": _AUTHORIZATION}


def _identity() -> NodeIdentity:
    return NodeIdentity(
        protocol_version="local-inference-node/v1",
        node_id="development-node",
        embedding_model="BAAI/bge-m3",
        embedding_revision=_EMBEDDING_REVISION,
        embedding_dimension=1024,
        embedding_normalization="l2",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_revision=_RERANK_REVISION,
    )


class _FakeEmbedder:
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * 1024 for index, _text in enumerate(texts)]


def _test_node_app(identity: NodeIdentity, **kwargs):  # type: ignore[no-untyped-def]
    return create_node_app(identity, authorization=_AUTHORIZATION, **kwargs)


def _test_client(transport: httpx.ASGITransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://node",
        headers=_AUTH_HEADERS,
    )


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic private-node-test-token", "Bearer ", "Bearer token\r\nInjected: yes"],
)
def test_node_app_refuses_missing_or_malformed_authorization(authorization):
    with pytest.raises(NodeConfigurationError, match="node_authorization_(required|invalid)"):
        create_node_app(_identity(), authorization=authorization)


@pytest.mark.asyncio
async def test_every_node_route_requires_the_exact_bearer_authorization():
    app = create_node_app(
        _identity(),
        authorization=_AUTHORIZATION,
        embedder=_FakeEmbedder(),
    )
    routes = [
        ("GET", "/health", None),
        ("GET", "/v1/identity", None),
        ("POST", "/v1/embeddings", {"input": ["one"]}),
        ("POST", "/v1/rerank", {"query": "q", "documents": ["d"], "top_k": 1}),
        ("POST", "/v1/structured-json", {}),
    ]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        for method, path, payload in routes:
            missing = await client.request(method, path, json=payload)
            wrong = await client.request(
                method,
                path,
                json=payload,
                headers={"Authorization": "Bearer wrong-token"},
            )

            assert missing.status_code == 401
            assert wrong.status_code == 401
            assert missing.json() == {"error": "node_authorization_invalid"}
            assert wrong.json() == {"error": "node_authorization_invalid"}
            assert "private-node-test-token" not in missing.text
            assert "private-node-test-token" not in wrong.text

        authorized_health = await client.get("/health", headers=_AUTH_HEADERS)
        authorized_identity = await client.get("/v1/identity", headers=_AUTH_HEADERS)
        authorized_embedding = await client.post(
            "/v1/embeddings",
            json={"input": ["one"]},
            headers=_AUTH_HEADERS,
        )
        duplicate_authorization = await client.get(
            "/health",
            headers=[
                ("Authorization", _AUTHORIZATION),
                ("Authorization", _AUTHORIZATION),
            ],
        )

    assert authorized_health.status_code == 200
    assert authorized_identity.status_code == 200
    assert authorized_embedding.status_code == 200
    assert duplicate_authorization.status_code == 401
    assert duplicate_authorization.json() == {"error": "node_authorization_invalid"}


@pytest.mark.asyncio
async def test_health_and_identity_are_safe_and_model_bound():
    app = _test_node_app(_identity())
    transport = httpx.ASGITransport(app=app)

    async with _test_client(transport) as client:
        health = await client.get("/health")
        identity = await client.get("/v1/identity")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "protocol_version": "local-inference-node/v1",
        "queue_depth": 0,
        "available_slots": 1,
        "max_concurrency": 1,
    }
    assert identity.status_code == 200
    assert identity.json()["embedding"] == {
        "model": "BAAI/bge-m3",
        "revision": _EMBEDDING_REVISION,
        "dimension": 1024,
        "normalization": "l2",
    }
    assert identity.json()["capabilities"] == ["embeddings", "rerank"]
    assert "endpoint" not in identity.text
    assert "token" not in identity.text


@pytest.mark.asyncio
async def test_embedding_identity_drift_is_exposed_as_a_governance_failure():
    class _DriftEmbedder:
        def embed_batch(self, _texts):
            raise NodeModelIdentityDriftError("node_ollama_model_identity_drift")

    transport = httpx.ASGITransport(app=_test_node_app(_identity(), embedder=_DriftEmbedder()))
    async with _test_client(transport) as client:
        response = await client.post("/v1/embeddings", json={"input": ["one"]})

    assert response.status_code == 409
    assert response.json() == {"error": "node_embedding_identity_drift"}


@pytest.mark.asyncio
async def test_cancelled_active_request_keeps_capacity_reserved_until_worker_exits():
    """A cancelled client cannot let another request overlap active inference."""

    class _BlockingEmbedder:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()
            self.started = threading.Event()
            self.release = threading.Event()

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            with self.lock:
                self.calls += 1
                self.started.set()
            self.release.wait(timeout=5)
            return [[1.0] * 1024 for _ in texts]

    embedder = _BlockingEmbedder()
    transport = httpx.ASGITransport(app=_test_node_app(_identity(), embedder=embedder))

    async with _test_client(transport) as client:
        first = asyncio.create_task(client.post("/v1/embeddings", json={"input": ["first"]}))
        await asyncio.to_thread(embedder.started.wait, 1)

        second = asyncio.create_task(client.post("/v1/embeddings", json={"input": ["second"]}))
        for _ in range(100):
            if (await client.get("/health")).json()["queue_depth"] == 1:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("second request did not enter the node queue")

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        for _ in range(100):
            health = (await client.get("/health")).json()
            if (
                health["queue_depth"] == 1
                and health["available_slots"] == 0
                and embedder.calls == 1
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled client released capacity before its inference worker stopped")

        embedder.release.set()
        assert (await second).status_code == 200
        assert embedder.calls == 2


@pytest.mark.asyncio
async def test_cancelled_queued_request_does_not_leak_its_future_capacity_slot():
    class _BlockingEmbedder:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            self.started.set()
            self.release.wait(timeout=5)
            return [[1.0] * 1024 for _ in texts]

    embedder = _BlockingEmbedder()
    transport = httpx.ASGITransport(app=_test_node_app(_identity(), embedder=embedder))

    async with _test_client(transport) as client:
        first = asyncio.create_task(client.post("/v1/embeddings", json={"input": ["first"]}))
        await asyncio.to_thread(embedder.started.wait, 1)
        queued = asyncio.create_task(client.post("/v1/embeddings", json={"input": ["queued"]}))
        for _ in range(100):
            if (await client.get("/health")).json()["queue_depth"] == 1:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("request did not enter the node queue")

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert (await client.get("/health")).json() == {
            "status": "ok",
            "protocol_version": "local-inference-node/v1",
            "queue_depth": 0,
            "available_slots": 0,
            "max_concurrency": 1,
        }

        embedder.release.set()
        assert (await first).status_code == 200
        for _ in range(100):
            if (await client.get("/health")).json()["available_slots"] == 1:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled queue request leaked the released capacity slot")


@pytest.mark.asyncio
async def test_embeddings_are_bounded_and_return_identity_matched_1024d_vectors():
    app = _test_node_app(_identity(), embedder=_FakeEmbedder())
    transport = httpx.ASGITransport(app=app)

    async with _test_client(transport) as client:
        response = await client.post("/v1/embeddings", json={"input": ["first", "second"]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["embedding_identity"] == f"BAAI/bge-m3@{_EMBEDDING_REVISION}"
    assert payload["dimension"] == 1024
    assert [item["index"] for item in payload["data"]] == [0, 1]
    assert all(len(item["embedding"]) == 1024 for item in payload["data"])


@pytest.mark.asyncio
async def test_embedding_result_identity_changes_when_model_revision_changes():
    drifted_identity = NodeIdentity(
        protocol_version="local-inference-node/v1",
        node_id="development-node",
        embedding_model="BAAI/bge-m3",
        embedding_revision="c" * 40,
        embedding_dimension=1024,
        embedding_normalization="l2",
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_revision=_RERANK_REVISION,
    )
    baseline = _test_node_app(_identity(), embedder=_FakeEmbedder())
    drifted = _test_node_app(drifted_identity, embedder=_FakeEmbedder())

    async with _test_client(httpx.ASGITransport(app=baseline)) as client:
        baseline_result = await client.post("/v1/embeddings", json={"input": ["test"]})
    async with _test_client(httpx.ASGITransport(app=drifted)) as client:
        drifted_result = await client.post("/v1/embeddings", json={"input": ["test"]})

    assert baseline_result.json()["embedding_identity"] == f"BAAI/bge-m3@{_EMBEDDING_REVISION}"
    assert drifted_result.json()["embedding_identity"] == f"BAAI/bge-m3@{'c' * 40}"


@pytest.mark.asyncio
async def test_embeddings_reject_empty_input_and_dimension_drift():
    class _WrongDimensionEmbedder:
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 3 for _text in texts]

    transport = httpx.ASGITransport(
        app=_test_node_app(_identity(), embedder=_WrongDimensionEmbedder())
    )
    async with _test_client(transport) as client:
        empty = await client.post("/v1/embeddings", json={"input": []})
        drift = await client.post("/v1/embeddings", json={"input": ["test"]})

    assert empty.status_code == 400
    assert empty.json()["error"] == "node_embedding_input_invalid"
    assert drift.status_code == 409
    assert drift.json()["error"] == "node_embedding_dimension_mismatch"


@pytest.mark.asyncio
async def test_embeddings_reject_oversized_bodies_nonfinite_outputs_and_provider_errors():
    class _NonFiniteEmbedder:
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[float("nan")] * 1024 for _text in texts]

    class _FailingEmbedder:
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            del texts
            raise RuntimeError("private upstream failure")

    small_body = _test_node_app(
        _identity(),
        embedder=_FakeEmbedder(),
        limits=NodeLimits(max_request_bytes=16),
    )
    nonfinite = _test_node_app(_identity(), embedder=_NonFiniteEmbedder())
    failing = _test_node_app(_identity(), embedder=_FailingEmbedder())

    async with _test_client(httpx.ASGITransport(app=small_body)) as client:
        too_large = await client.post("/v1/embeddings", json={"input": ["too-large"]})
    async with _test_client(httpx.ASGITransport(app=nonfinite)) as client:
        invalid_output = await client.post("/v1/embeddings", json={"input": ["test"]})
    async with _test_client(httpx.ASGITransport(app=failing)) as client:
        failed = await client.post("/v1/embeddings", json={"input": ["test"]})

    assert too_large.status_code == 413
    assert too_large.json()["error"] == "node_request_too_large"
    assert invalid_output.status_code == 409
    assert invalid_output.json()["error"] == "node_embedding_dimension_mismatch"
    assert failed.status_code == 502
    assert failed.json() == {"error": "node_embedding_failed"}
    assert "private" not in failed.text


@pytest.mark.asyncio
async def test_rerank_returns_every_candidate_once_in_score_order():
    class _FakeReranker:
        def rerank_tuples(self, query, candidates, top_k):
            assert query == "needle"
            assert top_k == 2
            return [(2, 0.2), (0, 0.9), (1, 0.5)]

    app = _test_node_app(_identity(), reranker=_FakeReranker())
    transport = httpx.ASGITransport(app=app)
    async with _test_client(transport) as client:
        response = await client.post(
            "/v1/rerank",
            json={"query": "needle", "documents": ["a", "b", "c"], "top_k": 2},
        )

    assert response.status_code == 200
    assert response.json()["results"] == [
        {"index": 0, "score": 0.9},
        {"index": 1, "score": 0.5},
        {"index": 2, "score": 0.2},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [[(0, 1.0), (0, 0.5)], [(0, 1.0), (2, 0.5)]])
async def test_rerank_rejects_duplicate_or_missing_candidate_indices(raw):
    class _InvalidReranker:
        def rerank_tuples(self, query, candidates, top_k):
            del query, candidates, top_k
            return raw

    transport = httpx.ASGITransport(app=_test_node_app(_identity(), reranker=_InvalidReranker()))
    async with _test_client(transport) as client:
        response = await client.post(
            "/v1/rerank",
            json={"query": "needle", "documents": ["a", "b"], "top_k": 1},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "node_rerank_result_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        [(0, float("nan")), (1, 0.5)],
        [(0, 0.5), (3, 0.2)],
        [(0, 0.5), (True, 0.2)],
    ],
)
async def test_rerank_rejects_nonfinite_and_out_of_range_results(raw):
    class _InvalidReranker:
        def rerank_tuples(self, query, candidates, top_k):
            del query, candidates, top_k
            return raw

    transport = httpx.ASGITransport(app=_test_node_app(_identity(), reranker=_InvalidReranker()))
    async with _test_client(transport) as client:
        response = await client.post(
            "/v1/rerank",
            json={"query": "needle", "documents": ["a", "b"], "top_k": 1},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "node_rerank_result_incomplete"


@pytest.mark.asyncio
async def test_rerank_rejects_unavailable_and_invalid_requests():
    transport = httpx.ASGITransport(app=_test_node_app(_identity()))
    async with _test_client(transport) as client:
        unavailable = await client.post(
            "/v1/rerank",
            json={"query": "needle", "documents": ["a"], "top_k": 1},
        )
        invalid = await client.post(
            "/v1/rerank",
            json={"query": " ", "documents": ["a"], "top_k": True},
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["error"] == "node_rerank_unavailable"
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "node_rerank_input_invalid"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.5.14", "example.invalid"])
def test_node_server_rejects_non_loopback_bind_hosts(host: str):
    with pytest.raises(NodeConfigurationError, match="node_bind_host_must_be_loopback"):
        validate_loopback_bind_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_node_server_accepts_loopback_bind_hosts(host: str):
    assert validate_loopback_bind_host(host) == host


def test_identity_requires_pinned_revisions_and_a_positive_dimension():
    with pytest.raises(NodeConfigurationError, match="embedding_revision_must_be_pinned"):
        NodeIdentity(
            protocol_version="local-inference-node/v1",
            node_id="development-node",
            embedding_model="BAAI/bge-m3",
            embedding_revision="latest",
            embedding_dimension=1024,
            embedding_normalization="l2",
            rerank_model="BAAI/bge-reranker-v2-m3",
            rerank_revision=_RERANK_REVISION,
        )
    with pytest.raises(NodeConfigurationError, match="node_embedding_dimension_invalid"):
        NodeIdentity(
            protocol_version="local-inference-node/v1",
            node_id="development-node",
            embedding_model="BAAI/bge-m3",
            embedding_revision=_EMBEDDING_REVISION,
            embedding_dimension=0,
            embedding_normalization="l2",
            rerank_model="BAAI/bge-reranker-v2-m3",
            rerank_revision=_RERANK_REVISION,
        )
    with pytest.raises(NodeConfigurationError, match="embedding_revision_must_be_pinned"):
        NodeIdentity(
            protocol_version="local-inference-node/v1",
            node_id="development-node",
            embedding_model="BAAI/bge-m3",
            embedding_revision="v1",
            embedding_dimension=1024,
            embedding_normalization="l2",
            rerank_model="BAAI/bge-reranker-v2-m3",
            rerank_revision=_RERANK_REVISION,
        )
    assert (
        NodeIdentity(
            protocol_version="local-inference-node/v1",
            node_id="development-node",
            embedding_model="custom-embedding",
            embedding_revision=_EMBEDDING_REVISION,
            embedding_dimension=768,
            embedding_normalization="none",
            rerank_model="custom-reranker",
            rerank_revision=_RERANK_REVISION,
        ).embedding_dimension
        == 768
    )


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "PP_LOCAL_NODE_AUTHORIZATION": _AUTHORIZATION,
        # Most contract tests exercise the explicit in-process compatibility
        # path.  The production default is covered separately below.
        "PP_LOCAL_NODE_EMBEDDING_BACKEND": "bge-local",
        "PP_LOCAL_NODE_EMBEDDING_MODEL": "BAAI/bge-m3",
        "PP_LOCAL_NODE_EMBEDDING_REVISION": _EMBEDDING_REVISION,
        "PP_LOCAL_NODE_EMBEDDING_DIMENSION": "1024",
        "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION": "l2",
        "PP_LOCAL_NODE_RERANK_BACKEND": "bge-local",
        "PP_LOCAL_NODE_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        "PP_LOCAL_NODE_RERANK_REVISION": _RERANK_REVISION,
    }
    values.update(overrides)
    return values


def test_runtime_config_defaults_to_llama_cpp_and_display_name():
    values = _environment(
        PP_LOCAL_NODE_EMBEDDING_BACKEND=None,
        PP_LOCAL_NODE_RERANK_BACKEND=None,
        PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=None,
        PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=None,
    )
    values = {key: value for key, value in values.items() if value is not None}
    config = NodeRuntimeConfig.from_environment(values)

    assert config.embedding_backend == "llama.cpp"
    assert config.rerank_backend == "llama.cpp"
    assert config.identity.node_id == "inference-node"
    assert config.embedding_model_reference is not None
    assert config.embedding_model_reference.as_posix() == "/models/embedding"
    assert config.rerank_model_reference is not None
    assert config.rerank_model_reference.as_posix() == "/models/rerank"


def test_node_identity_uses_control_compatible_technical_identifier():
    with pytest.raises(NodeConfigurationError, match="node_id_invalid"):
        NodeRuntimeConfig.from_environment(_environment(PP_LOCAL_NODE_ID="推理节点"))


def test_runtime_config_requires_pinned_models_and_loopback_listener():
    config = NodeRuntimeConfig.from_environment(_environment())

    assert config.identity.embedding_dimension == 1024
    assert config.bind_host == "127.0.0.1"
    assert config.port == 19130
    custom_config = NodeRuntimeConfig.from_environment(
        _environment(
            PP_LOCAL_NODE_EMBEDDING_MODEL="acme/embeddings-v2",
            PP_LOCAL_NODE_EMBEDDING_DIMENSION="768",
            PP_LOCAL_NODE_EMBEDDING_NORMALIZATION="none",
            PP_LOCAL_NODE_RERANK_MODEL="acme/reranker-v2",
        )
    )
    assert custom_config.identity.embedding_model == "acme/embeddings-v2"
    assert custom_config.identity.embedding_dimension == 768
    assert custom_config.identity.embedding_normalization == "none"
    assert custom_config.identity.rerank_model == "acme/reranker-v2"
    with pytest.raises(NodeConfigurationError, match="pp_local_node_embedding_revision_required"):
        NodeRuntimeConfig.from_environment({"PP_LOCAL_NODE_RERANK_REVISION": _RERANK_REVISION})
    with pytest.raises(NodeConfigurationError, match="node_bind_host_must_be_loopback"):
        NodeRuntimeConfig.from_environment(_environment(PP_LOCAL_NODE_BIND_HOST="0.0.0.0"))
    with pytest.raises(NodeConfigurationError, match="pp_local_node_embedding_model_required"):
        NodeRuntimeConfig.from_environment(
            {
                key: value
                for key, value in _environment().items()
                if key != "PP_LOCAL_NODE_EMBEDDING_MODEL"
            }
        )
    with pytest.raises(
        NodeConfigurationError,
        match="pp_local_node_embedding_normalization_required",
    ):
        NodeRuntimeConfig.from_environment(
            {
                key: value
                for key, value in _environment().items()
                if key != "PP_LOCAL_NODE_EMBEDDING_NORMALIZATION"
            }
        )
    with pytest.raises(
        NodeConfigurationError,
        match="pp_local_node_embedding_model_reference_must_be_local_absolute_path",
    ):
        NodeRuntimeConfig.from_environment(
            _environment(PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE="../another-model")
        )
    with pytest.raises(NodeConfigurationError, match="node_ollama_host_not_allowed"):
        NodeRuntimeConfig.from_environment(
            _environment(PP_LOCAL_NODE_OLLAMA_HOST="https://remote.example/v1")
        )
    governed_ollama = NodeRuntimeConfig.from_environment(
        _environment(PP_LOCAL_NODE_EMBEDDING_BACKEND="ollama")
    )
    assert governed_ollama.embedding_backend == "ollama"

    governed_llama_cpp = NodeRuntimeConfig.from_environment(
        _environment(
            PP_LOCAL_NODE_EMBEDDING_BACKEND="llama.cpp",
            PP_LOCAL_NODE_RERANK_BACKEND="llama.cpp",
            PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL="http://127.0.0.1:19131",
            PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL="http://127.0.0.1:19132",
        )
    )
    assert governed_llama_cpp.embedding_backend == "llama.cpp"
    assert governed_llama_cpp.rerank_backend == "llama.cpp"
    assert governed_llama_cpp.embedding_model_reference is not None
    assert governed_llama_cpp.embedding_model_reference.as_posix() == "/models/embedding"
    assert governed_llama_cpp.rerank_model_reference is not None
    assert governed_llama_cpp.rerank_model_reference.as_posix() == "/models/rerank"

    with pytest.raises(NodeConfigurationError, match="node_llama_cpp_host_not_allowed"):
        NodeRuntimeConfig.from_environment(
            _environment(
                PP_LOCAL_NODE_EMBEDDING_BACKEND="llama.cpp",
                PP_LOCAL_NODE_RERANK_BACKEND="llama.cpp",
                PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL="http://192.168.5.6:19131",
            )
        )


def test_model_artifact_identity_binds_declared_models_to_local_content(tmp_path):
    embedding = tmp_path / "embedding"
    rerank = tmp_path / "rerank"
    embedding.mkdir()
    rerank.mkdir()
    (embedding / "config.json").write_text('{"model":"embedding"}', encoding="utf-8")
    (rerank / "config.json").write_text('{"model":"rerank"}', encoding="utf-8")

    identity = bind_model_artifact_identity(
        NodeRuntimeConfig.from_environment(
            _environment(
                PP_LOCAL_NODE_EMBEDDING_MODEL="custom-embedding",
                PP_LOCAL_NODE_EMBEDDING_DIMENSION="768",
                PP_LOCAL_NODE_RERANK_MODEL="custom-reranker",
                PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=str(embedding),
                PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=str(rerank),
            )
        )
    )

    assert identity.embedding_artifact_sha256 is not None
    assert identity.rerank_artifact_sha256 is not None
    assert (
        identity.public_json()["embedding"]["artifact_sha256"] == identity.embedding_artifact_sha256
    )


def test_llama_cpp_artifact_identity_hashes_exact_gguf_files(tmp_path):
    embedding = tmp_path / "embedding.gguf"
    rerank = tmp_path / "rerank.gguf"
    embedding_bytes = b"gguf-embedding-content"
    rerank_bytes = b"gguf-rerank-content"
    embedding.write_bytes(embedding_bytes)
    rerank.write_bytes(rerank_bytes)

    identity = bind_model_artifact_identity(
        NodeRuntimeConfig.from_environment(
            _environment(
                PP_LOCAL_NODE_EMBEDDING_BACKEND="llama.cpp",
                PP_LOCAL_NODE_RERANK_BACKEND="llama.cpp",
                PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=str(embedding),
                PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=str(rerank),
            )
        )
    )

    assert identity.embedding_artifact_sha256 == f"sha256:{sha256(embedding_bytes).hexdigest()}"
    assert identity.rerank_artifact_sha256 == f"sha256:{sha256(rerank_bytes).hexdigest()}"


def test_server_config_check_does_not_load_models_or_open_a_listener(monkeypatch):
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-compute-node")

    assert main(["--config-check"]) == 0


def test_server_config_check_refuses_missing_authorization(monkeypatch, capsys):
    for name, value in _environment().items():
        if name != "PP_LOCAL_NODE_AUTHORIZATION":
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("PP_LOCAL_NODE_AUTHORIZATION", raising=False)
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-compute-node")

    assert main(["--config-check"]) == 2
    error = capsys.readouterr().err
    assert "node_authorization_required" in error
    assert "private-node-test-token" not in error


def test_runtime_factory_requires_exact_compute_node_role(monkeypatch):
    monkeypatch.delenv("PP_ENDPOINT_ROLE", raising=False)

    with pytest.raises(NodeConfigurationError, match="node_endpoint_role_mismatch"):
        create_runtime_app(object())  # type: ignore[arg-type]

    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")
    with pytest.raises(NodeConfigurationError, match="node_endpoint_role_mismatch"):
        create_runtime_app(object())  # type: ignore[arg-type]
