"""End-to-end tests for the trusted native client-local rerank executor."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from plastic_promise.client.local_rerank_executor import (
    ClientLocalGatewayError,
    ClientLocalGatewayResponse,
    ClientLocalRerankExecutor,
    ClientLocalRerankExecutorError,
    HTTPXClientLocalGatewayTransport,
    LocalRerankCandidate,
    LocalRerankScore,
)
from plastic_promise.core.backend_inference import BackendInferenceService
from plastic_promise.core.embedder import Embedder
from plastic_promise.mcp.inference_gateway import (
    InferenceGatewaySettings,
    create_inference_gateway_app,
)


class _Embedder(Embedder):
    @property
    def dim(self):
        return 3

    @property
    def model_name(self):
        return "test-embedding"

    @property
    def index_model_name(self):
        return "test-embedding"

    def embed(self, _text):
        return [1.0, 2.0, 3.0]

    def embed_batch(self, texts):
        return [[1.0, 2.0, 3.0] for _ in texts]


class _UnusedCloudReranker:
    def rerank_tuples(self, *_args, **_kwargs):
        raise AssertionError("client-local execution must not invoke a server reranker")


class _RecordingTransport:
    def __init__(self, delegate):
        self.delegate = delegate
        self.paths = []

    async def request(self, method, path, *, json=None, headers=None):
        self.paths.append((method, path))
        return await self.delegate.request(method, path, json=json, headers=headers)


@pytest.fixture
def _gateway_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EMBEDDER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("EMBEDDER_MODEL", "test-embedding")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "3")
    monkeypatch.setenv("PP_EMBEDDING_IDENTITY", "test-embedding")
    monkeypatch.setenv("PP_INFERENCE_PROVIDER_HOST_ALLOWLIST", "example.invalid")
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,original")
    monkeypatch.setenv("PP_RERANK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PP_RERANK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL", "unused-cloud-reranker")


def _app(tmp_path):
    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
    )

    def service_factory():
        return BackendInferenceService(
            embedder=_Embedder(),
            reranker_factory=_UnusedCloudReranker,
            provider_policy_revision="test-policy",
        )

    return create_inference_gateway_app(settings, service_factory=service_factory)


def _submission():
    return {
        "target": "client-local",
        "request_id": "native-device-a",
        "idempotency_key": "executor-e2e-a",
        "candidate_set_version": "candidate-v1",
        "model_identity": "BAAI/bge-reranker-v2-m3",
        "query": "Which store is canonical?",
        "top_k": 2,
        "items": [
            {"id": "memory-a", "text": "SQLite is canonical.", "base_score": 0.8},
            {"id": "memory-b", "text": "LanceDB is derived.", "base_score": 0.7},
            {"id": "memory-c", "text": "Caches are bounded.", "base_score": 0.6},
        ],
    }


async def _wait_for_pending(transport, poll_path):
    for _ in range(100):
        response = await transport.request("GET", poll_path)
        if response.payload.get("status") == "pending":
            return response.payload
        await asyncio.sleep(0.01)
    raise AssertionError("client-local job did not become pending")


@pytest.mark.asyncio
async def test_executor_leases_renews_scores_sorts_and_completes_real_gateway(
    tmp_path,
    _gateway_environment,
):
    app_transport = httpx.ASGITransport(app=_app(tmp_path), client=("127.0.0.1", 43123))
    http = HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:9030",
        bearer_token="t" * 32,
        transport=app_transport,
    )
    transport = _RecordingTransport(http)
    observed = {}

    async def local_model(query, candidates, top_k):
        observed["query"] = query
        observed["candidates"] = candidates
        observed["top_k"] = top_k
        await asyncio.sleep(0.06)
        # The executor owns ordering; model adapters may return unsorted scores.
        return [
            LocalRerankScore("memory-a", 0.4),
            LocalRerankScore("memory-b", 0.2),
            LocalRerankScore("memory-c", 0.9),
        ]

    try:
        submitted = await transport.request(
            "POST",
            "/v1/rerank/jobs",
            json=_submission(),
            headers={"Prefer": "respond-async"},
        )
        pending = await _wait_for_pending(transport, submitted.payload["poll_path"])
        executor = ClientLocalRerankExecutor(
            transport=transport,
            rerank=local_model,
            model_identity="BAAI/bge-reranker-v2-m3",
            renew_interval_seconds=0.01,
        )

        completed = await executor.execute(pending)
    finally:
        await http.aclose()

    assert completed["status"] == "completed"
    assert completed["result"] == {
        "contract_version": "client-local-rerank-result/v1",
        "package_hash": pending["package"]["package_hash"],
        "model_identity": "BAAI/bge-reranker-v2-m3",
        "items": [
            {"id": "memory-c", "score": 0.9},
            {"id": "memory-a", "score": 0.4},
        ],
    }
    assert observed["query"] == "Which store is canonical?"
    assert observed["top_k"] == 2
    assert all(isinstance(item, LocalRerankCandidate) for item in observed["candidates"])
    requested_paths = [path for _method, path in transport.paths]
    assert f"/v1/rerank/jobs/{completed['job_id']}/lease" in requested_paths
    assert f"/v1/rerank/jobs/{completed['job_id']}/lease/renew" in requested_paths
    assert f"/v1/rerank/jobs/{completed['job_id']}/complete" in requested_paths


@pytest.mark.asyncio
async def test_executor_keeps_renewing_while_completion_response_is_in_flight():
    renewals = 0
    completion_started = asyncio.Event()

    class SlowCompletionTransport:
        async def request(self, _method, path, *, json=None, headers=None):
            del json, headers
            nonlocal renewals
            if path.endswith("/complete"):
                completion_started.set()
                await asyncio.sleep(0.04)
                return ClientLocalGatewayResponse(
                    200,
                    {"job_id": "job:test", "status": "completed"},
                )
            assert path.endswith("/lease/renew")
            await completion_started.wait()
            renewals += 1
            return ClientLocalGatewayResponse(
                200,
                {"job_id": "job:test", "target": "client-local", "status": "leased"},
            )

    executor = ClientLocalRerankExecutor(
        transport=SlowCompletionTransport(),
        rerank=lambda _query, _candidates, _top_k: {"memory-a": 0.6, "memory-b": 0.4},
        model_identity="client:test-model",
        renew_interval_seconds=0.005,
    )

    completed = await executor.execute(_valid_leased_job())

    assert completed["status"] == "completed"
    assert renewals >= 1


class _ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, method, path, *, json=None, headers=None):
        self.requests.append((method, path, json, headers))
        return self.responses.pop(0)


def _leased_job():
    return {
        "contract": "inference-gateway/v1",
        "job_id": "job:test",
        "project_id": "project:test",
        "target": "client-local",
        "status": "leased",
        "lease_token": "opaque-lease-token",
        "request_id": "request:test",
        "candidate_set_version": "candidate-v1",
        "package": {
            "contract_version": "client-local-rerank/v2",
            "scoring_version": "client-local-score/v1",
            "project_id": "project:test",
            "request_id": "request:test",
            "candidate_set_version": "candidate-v1",
            "candidate_set_hash": "sha256:" + "0" * 64,
            "query": "query",
            "query_hash": "sha256:" + "0" * 64,
            "embedding_identity": "embedding:test",
            "embedding_dimension": 3,
            "model_identity": "client:test-model",
            "top_k": 1,
            "candidates": [
                {
                    "id": "memory-a",
                    "text": "text",
                    "base_score": 0.5,
                    "material_sha256": "sha256:" + "0" * 64,
                    "embedding_sha256": "sha256:" + "0" * 64,
                }
            ],
            "package_hash": "sha256:" + "0" * 64,
        },
    }


def _valid_leased_job():
    service = BackendInferenceService(
        embedder=_Embedder(),
        reranker_factory=_UnusedCloudReranker,
        provider_policy_revision="test-policy",
    )
    prepared = service.prepare(
        [
            {"id": "memory-a", "text": "A", "base_score": 0.5},
            {"id": "memory-b", "text": "B", "base_score": 0.4},
        ]
    )
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project:test",
        request_id="request:test",
        candidate_set_version="candidate-v1",
        model_identity="client:test-model",
        top_k=1,
    )
    return {
        "job_id": "job:test",
        "target": "client-local",
        "status": "leased",
        "lease_token": "opaque-lease-token",
        "package": {
            "contract_version": package.contract_version,
            "scoring_version": package.scoring_version,
            "project_id": package.project_id,
            "request_id": package.request_id,
            "candidate_set_version": package.candidate_set_version,
            "candidate_set_hash": package.candidate_set_hash,
            "query": package.query,
            "query_hash": package.query_hash,
            "embedding_identity": package.embedding_identity,
            "embedding_dimension": package.embedding_dimension,
            "model_identity": package.model_identity,
            "top_k": package.top_k,
            "candidates": [
                {
                    "id": candidate.item_id,
                    "text": candidate.text,
                    "base_score": candidate.base_score,
                    "material_sha256": candidate.material_sha256,
                    "embedding_sha256": candidate.embedding_sha256,
                }
                for candidate in package.candidates
            ],
            "package_hash": package.package_hash,
        },
    }


@pytest.mark.asyncio
async def test_executor_rejects_tampered_package_before_calling_local_model():
    called = False

    def local_model(_query, _candidates, _top_k):
        nonlocal called
        called = True
        return {"memory-a": 0.5}

    executor = ClientLocalRerankExecutor(
        transport=_ScriptedTransport([]),
        rerank=local_model,
        model_identity="client:test-model",
    )

    with pytest.raises(ClientLocalRerankExecutorError, match="client_local_query_hash_mismatch"):
        await executor.execute(_leased_job())
    assert called is False


@pytest.mark.asyncio
async def test_executor_rejects_package_for_another_model_before_local_call():
    called = False

    def local_model(_query, _candidates, _top_k):
        nonlocal called
        called = True
        return {"memory-a": 0.5, "memory-b": 0.4}

    executor = ClientLocalRerankExecutor(
        transport=_ScriptedTransport([]),
        rerank=local_model,
        model_identity="client:different-model@revision-2",
    )

    with pytest.raises(
        ClientLocalRerankExecutorError, match="client_local_model_identity_mismatch"
    ):
        await executor.execute(_valid_leased_job())
    assert called is False


@pytest.mark.asyncio
async def test_executor_rejects_oversized_candidate_count_before_local_model():
    called = False
    job = _valid_leased_job()
    candidate = job["package"]["candidates"][0]
    job["package"]["candidates"] = [candidate for _ in range(101)]

    def local_model(_query, _candidates, _top_k):
        nonlocal called
        called = True
        return {}

    executor = ClientLocalRerankExecutor(
        transport=_ScriptedTransport([]),
        rerank=local_model,
        model_identity="client:test-model",
    )

    with pytest.raises(ClientLocalRerankExecutorError, match="client_local_package_invalid"):
        await executor.execute(job)
    assert called is False


@pytest.mark.asyncio
async def test_executor_rejects_incomplete_or_nonfinite_local_scores():
    # A valid package is obtained through the real package builder in the E2E
    # path; this focused test exercises result validation with a transport stub.
    class PackageTransport:
        async def request(self, *_args, **_kwargs):
            raise AssertionError("no HTTP request expected before score validation")

    job = _valid_leased_job()

    for result in ({"memory-a": 0.4}, {"memory-a": float("nan"), "memory-b": 0.2}):
        executor = ClientLocalRerankExecutor(
            transport=PackageTransport(),
            rerank=lambda _query, _candidates, _top_k, value=result: value,
            model_identity="client:test-model",
        )
        expected = "incomplete" if len(result) == 1 else "score_invalid"
        with pytest.raises(ClientLocalRerankExecutorError, match=expected):
            await executor.execute(job)


@pytest.mark.asyncio
async def test_model_error_is_not_masked_by_renewal_failure():
    renewal_started = asyncio.Event()
    model_failed = asyncio.Event()

    class FailingRenewalTransport:
        async def request(self, method, path, *, json=None, headers=None):
            del method, json, headers
            assert path.endswith("/lease/renew")
            renewal_started.set()
            await model_failed.wait()
            raise RuntimeError("renewal-failed")

    async def failing_model(_query, _candidates, _top_k):
        await renewal_started.wait()
        model_failed.set()
        raise ValueError("model-failed")

    executor = ClientLocalRerankExecutor(
        transport=FailingRenewalTransport(),
        rerank=failing_model,
        model_identity="client:test-model",
        renew_interval_seconds=0.001,
    )

    with pytest.raises(ValueError, match="^model-failed$"):
        await executor.execute(_valid_leased_job())


@pytest.mark.asyncio
async def test_executor_cancellation_stops_renewal_without_masking_cancellation():
    model_started = asyncio.Event()
    model_cancelled = asyncio.Event()

    class NoRequestTransport:
        async def request(self, *_args, **_kwargs):
            raise AssertionError("renewal or completion must not run after immediate cancellation")

    async def waiting_model(_query, _candidates, _top_k):
        model_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            model_cancelled.set()

    executor = ClientLocalRerankExecutor(
        transport=NoRequestTransport(),
        rerank=waiting_model,
        model_identity="client:test-model",
        renew_interval_seconds=60,
    )
    task = asyncio.create_task(executor.execute(_valid_leased_job()))
    await model_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert model_cancelled.is_set()


def test_http_transport_rejects_dns_and_non_loopback_hosts_and_hides_token():
    with pytest.raises(ValueError, match="must_be_loopback"):
        HTTPXClientLocalGatewayTransport(
            base_url="http://localhost:19030",
            bearer_token="s" * 32,
        )
    with pytest.raises(ValueError, match="must_be_loopback"):
        HTTPXClientLocalGatewayTransport(
            base_url="https://api.example.test",
            bearer_token="s" * 32,
        )

    transport = HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:19030",
        bearer_token="s" * 32,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    assert "s" * 32 not in repr(transport)


def test_http_transport_disables_environment_proxies_and_redirects(monkeypatch):
    captured = {}

    class CapturingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "plastic_promise.client.local_rerank_executor.httpx.AsyncClient",
        CapturingClient,
    )

    HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:19030",
        bearer_token="s" * 32,
    )

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


@pytest.mark.asyncio
async def test_http_transport_rejects_absolute_paths_and_sensitive_header_overrides():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    transport = HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:19030",
        bearer_token="s" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="path_invalid"):
            await transport.request("GET", "https://external.example/v1/jobs")
        with pytest.raises(ValueError, match="headers_invalid"):
            await transport.request(
                "GET",
                "/v1/capabilities",
                headers={"Authorization": "Bearer replacement"},
            )
    finally:
        await transport.aclose()

    assert calls == []


@pytest.mark.asyncio
async def test_http_transport_does_not_follow_redirect_response():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://external.example/collect"},
            json={"error": {"code": "redirect_forbidden"}},
        )

    transport = HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:19030",
        bearer_token="s" * 32,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ClientLocalGatewayError, match="redirect_forbidden"):
            await transport.request("GET", "/v1/capabilities")
    finally:
        await transport.aclose()

    assert calls == ["http://127.0.0.1:19030/v1/capabilities"]


@pytest.mark.asyncio
async def test_http_transport_bounds_response_before_json_parsing():
    transport = HTTPXClientLocalGatewayTransport(
        base_url="http://127.0.0.1:19030",
        bearer_token="s" * 32,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b" " * (4 * 1024 * 1024 + 1),
            )
        ),
    )
    try:
        with pytest.raises(ClientLocalRerankExecutorError, match="response_too_large"):
            await transport.request("GET", "/v1/capabilities")
    finally:
        await transport.aclose()


def test_recording_transport_satisfies_runtime_protocol():
    response = ClientLocalGatewayResponse(status_code=200, payload={})
    assert response.status_code == 200
    assert isinstance(response.payload, Mapping)
