"""Offline contract tests for the authenticated inference gateway."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette

import plastic_promise.mcp.inference_gateway as gateway
from plastic_promise.client.hot_memory_cache import hot_memory_cache_contract
from plastic_promise.core.backend_inference import BackendInferenceService, material_sha256
from plastic_promise.core.embedder import Embedder
from plastic_promise.core.inference_jobs import InferenceJobConflictError, InferenceJobStore
from plastic_promise.mcp.inference_gateway import (
    InferenceGatewayConfigurationError,
    InferenceGatewaySettings,
    create_inference_gateway_routes,
    recover_pending_cloud_jobs,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _cloud_only_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EMBEDDER_API_KEY", "synthetic-embedding-key")
    monkeypatch.setenv("EMBEDDER_MODEL", "test-embedding")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "3")
    monkeypatch.setenv("PP_INFERENCE_CLIENT_VECTOR_IDENTITY", "test-embedding")
    monkeypatch.setenv("PP_INFERENCE_CLIENT_VECTOR_DIMENSION", "3")
    monkeypatch.setenv("PP_INFERENCE_PROVIDER_HOST_ALLOWLIST", "example.invalid")
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,original")
    monkeypatch.setenv("PP_RERANK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PP_RERANK_API_KEY", "synthetic-rerank-key")
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL", "test-reranker")


class FakeEmbedder(Embedder):
    calls = 0

    @property
    def dim(self):
        return 3

    @property
    def model_name(self):
        return "test-embedding"

    @property
    def index_model_name(self):
        return "test-embedding"

    def embed(self, text):
        type(self).calls += 1
        return [1.0, 2.0, 3.0]

    def embed_batch(self, texts):
        type(self).calls += 1
        return [[1.0, 2.0, 3.0] for _ in texts]


class FakeReranker:
    last_diagnostics = {"provider": "test"}
    last_model_identity = "test-reranker"

    def rerank_tuples(self, query, candidates, top_k=10):
        del query
        return [(item[0], 1.0 - index / 10.0) for index, item in enumerate(candidates[:top_k])]


def _settings(tmp_path: Path, *, token: str = "t" * 32):
    return InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token=token,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=120,
    )


def _app(
    tmp_path: Path,
    *,
    service_factory=None,
    rerank_service_factory=None,
    settings=None,
    store_factory=None,
):
    FakeEmbedder.calls = 0

    def default_service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=FakeReranker,
            provider_policy_revision="test-policy",
        )

    return Starlette(
        routes=create_inference_gateway_routes(
            settings or _settings(tmp_path),
            service_factory=service_factory or default_service_factory,
            rerank_service_factory=rerank_service_factory,
            store_factory=store_factory,
        )
    )


async def _request(
    app, method, path, *, headers=None, json=None, client_host="127.0.0.1", host="127.0.0.1:9030"
):
    transport = httpx.ASGITransport(app=app, client=(client_host, 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9030") as client:
        request_headers = [("host", host)]
        if headers:
            request_headers.extend(headers.items() if isinstance(headers, dict) else headers)
        return await client.request(method, path, headers=request_headers, json=json)


def _auth(token="t" * 32):
    return {"authorization": f"Bearer {token}"}


def _job_payload(*, key="key-a", target="client-local", query="canonical"):
    payload = {
        "target": target,
        "idempotency_key": key,
        "candidate_set_version": "candidate-v1",
        "query": query,
        "top_k": 2,
        "items": [
            {"id": "memory-a", "text": "SQLite is canonical.", "base_score": 0.8},
            {"id": "memory-b", "text": "LanceDB is derived.", "base_score": 0.7},
        ],
    }
    if target == "client-local":
        payload["model_identity"] = "client:test-model@revision-1"
    return payload


def _all_supplied_vector_payload(*, key="key-a", target="client-local", query="canonical"):
    payload = _job_payload(key=key, target=target, query=query)
    for item in payload["items"]:
        item["embedding"] = {
            "vector": [1.0, 2.0, 3.0],
            "dimension": 3,
            "identity": "test-embedding",
            "material_sha256": material_sha256(item["text"]),
        }
    return payload


def test_settings_require_ascii_token_and_lease_before_ttl(tmp_path):
    with pytest.raises(
        InferenceGatewayConfigurationError,
        match="inference_gateway_token_invalid",
    ):
        _settings(tmp_path, token="密" * 32)

    with pytest.raises(
        InferenceGatewayConfigurationError,
        match="inference_gateway_lease_must_precede_ttl",
    ):
        InferenceGatewaySettings(
            enabled=True,
            project_id="project:test",
            token="t" * 32,
            db_path=tmp_path / "inference_jobs.db",
            ttl_seconds=30,
            lease_seconds=30,
        )


def test_settings_default_job_db_uses_isolated_inference_directory(tmp_path):
    canonical = tmp_path / "state" / "db" / "plastic_memory.db"

    settings = InferenceGatewaySettings.from_env(
        {
            "PP_INFERENCE_GATEWAY": "0",
            "PLASTIC_DB_PATH": str(canonical),
        }
    )

    assert settings.db_path == tmp_path / "state" / "inference" / "inference_jobs.db"


def test_settings_reject_explicit_canonical_db_from_supplied_environment(tmp_path):
    canonical = tmp_path / "state" / "db" / "plastic_memory.db"

    with pytest.raises(
        InferenceGatewayConfigurationError,
        match="inference_gateway_db_must_be_separate",
    ):
        InferenceGatewaySettings.from_env(
            {
                "PP_INFERENCE_GATEWAY": "0",
                "PLASTIC_DB_PATH": str(canonical),
                "PP_INFERENCE_GATEWAY_DB_PATH": str(canonical),
            }
        )


@pytest.mark.asyncio
async def test_disabled_gateway_listener_exits_cleanly(monkeypatch):
    from plastic_promise.mcp import inference_gateway_server

    monkeypatch.setenv("PP_INFERENCE_GATEWAY", "0")
    monkeypatch.setattr(
        inference_gateway_server,
        "configure_default_environment",
        lambda _root: None,
    )

    assert await inference_gateway_server.serve(9030) is None


@pytest.mark.asyncio
async def test_standalone_app_lifespan_bounds_drain_and_leaves_cloud_job_retryable(
    tmp_path,
    monkeypatch,
    caplog,
):
    current = [datetime(2026, 7, 23, tzinfo=timezone.utc)]
    provider_started = threading.Event()
    provider_release = threading.Event()
    provider_finished = threading.Event()
    captured_runtimes = []

    def store_factory(path):
        return InferenceJobStore(path, clock=lambda: current[0])

    class ControlledReranker(FakeReranker):
        calls = 0

        def rerank_tuples(self, query, candidates, top_k=10):
            type(self).calls += 1
            if type(self).calls == 1:
                provider_started.set()
                try:
                    if not provider_release.wait(timeout=5):
                        raise RuntimeError("test_provider_release_timeout")
                finally:
                    provider_finished.set()
            return super().rerank_tuples(query, candidates, top_k)

    def service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=ControlledReranker,
            provider_policy_revision="test-policy",
        )

    original_drain = gateway._drain_runtime_tasks

    async def observed_drain(runtime, *, timeout_seconds):
        captured_runtimes.append(runtime)
        return await original_drain(runtime, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(gateway, "_drain_runtime_tasks", observed_drain)
    monkeypatch.setattr(gateway, "_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 0.01)
    caplog.set_level(logging.WARNING, logger="plastic-promise.inference-gateway")
    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
        max_concurrency=1,
    )
    app = gateway.create_inference_gateway_app(
        settings,
        service_factory=service_factory,
        store_factory=store_factory,
    )
    headers = {
        **_auth(),
        "content-type": "application/json",
        "prefer": "respond-async",
    }

    async with app.router.lifespan_context(app):
        submitted = await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers=headers,
            json=_job_payload(key="shutdown-over-deadline", target="cloud"),
        )
        assert submitted.status_code == 202
        assert await asyncio.to_thread(provider_started.wait, 2)
        shutdown_started = time.monotonic()

    assert time.monotonic() - shutdown_started < 1.0
    assert any("durable retry and may already be billed" in message for message in caplog.messages)
    assert captured_runtimes

    # Model the event-loop/process stop that follows a bounded listener drain.
    # The blocking provider thread may finish, but its cancelled coroutine can
    # no longer promise that SQLite completion will run.
    remaining_tasks = tuple(captured_runtimes[0]._background_tasks)
    for task in remaining_tasks:
        task.cancel()
    provider_release.set()
    await asyncio.gather(*remaining_tasks, return_exceptions=True)
    assert await asyncio.to_thread(provider_finished.wait, 2)

    store = store_factory(settings.db_path)
    reservation = store.get_reservation(
        settings.project_id,
        submitted.json()["idempotency_key_hash"],
    )
    assert reservation is not None
    job_id = reservation["job_id"]
    assert isinstance(job_id, str)

    current[0] += timedelta(seconds=6)
    assert store.require(job_id, settings.project_id).status == "pending"

    recovered = await recover_pending_cloud_jobs(
        settings,
        rerank_service_factory=lambda: BackendInferenceService.from_rerank_runtime(
            reranker_factory=ControlledReranker,
            provider_policy_revision="test-policy",
        ),
        store_factory=store_factory,
    )

    assert recovered == 1
    assert store.require(job_id, settings.project_id).status == "completed"
    assert ControlledReranker.calls == 2


@pytest.mark.asyncio
async def test_lifespan_drains_nested_request_and_provider_before_result_is_reused(
    tmp_path,
    monkeypatch,
):
    provider_started = threading.Event()
    provider_release = threading.Event()
    shutdown_started = asyncio.Event()

    class ControlledReranker(FakeReranker):
        calls = 0

        def rerank_tuples(self, query, candidates, top_k=10):
            type(self).calls += 1
            provider_started.set()
            if not provider_release.wait(timeout=5):
                raise RuntimeError("test_provider_release_timeout")
            return super().rerank_tuples(query, candidates, top_k)

    def service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=ControlledReranker,
            provider_policy_revision="test-policy",
        )

    original_drain = gateway._drain_runtime_tasks

    async def observed_drain(runtime, *, timeout_seconds):
        shutdown_started.set()
        return await original_drain(runtime, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(gateway, "_drain_runtime_tasks", observed_drain)
    app = gateway.create_inference_gateway_app(
        _settings(tmp_path),
        service_factory=service_factory,
    )
    headers = {
        **_auth(),
        "content-type": "application/json",
        "prefer": "respond-async",
    }
    payload = _job_payload(key="shutdown-drain", target="cloud")

    async def release_after_shutdown_starts():
        await asyncio.wait_for(shutdown_started.wait(), timeout=2)
        provider_release.set()

    releaser = asyncio.create_task(release_after_shutdown_starts())
    async with app.router.lifespan_context(app):
        submitted = await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers=headers,
            json=payload,
        )
        assert submitted.status_code == 202
        assert await asyncio.to_thread(provider_started.wait, 2)

    await releaser
    replayed = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=payload,
    )

    assert replayed.status_code == 200
    assert replayed.json()["status"] == "completed"
    assert ControlledReranker.calls == 1


def test_settings_reserve_default_executor_capacity_for_store_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.os, "cpu_count", lambda: 4)

    accepted = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "accepted.db",
        max_concurrency=6,
    )
    assert accepted.max_concurrency == 6

    with pytest.raises(
        InferenceGatewayConfigurationError,
        match="inference_gateway_concurrency_invalid",
    ):
        InferenceGatewaySettings(
            enabled=True,
            project_id="project:test",
            token="t" * 32,
            db_path=tmp_path / "rejected.db",
            max_concurrency=7,
        )


@pytest.mark.asyncio
async def test_json_body_has_total_read_deadline(monkeypatch):
    class SlowRequest:
        headers = {"content-type": "application/json"}

        async def stream(self):
            await asyncio.sleep(0.05)
            yield b"{}"

    monkeypatch.setattr(gateway, "_BODY_READ_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(ValueError, match="^inference_request_body_timeout$"):
        await gateway._json_body(SlowRequest())


def test_default_store_receives_all_gateway_capacity_settings(tmp_path, monkeypatch):
    captured = {}

    class CapturingStore:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

    monkeypatch.setattr(gateway, "InferenceJobStore", CapturingStore)
    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "jobs.db",
        ttl_seconds=3600,
        lease_seconds=1800,
        max_active_jobs=7,
        retention_seconds=7200,
        max_retained_rows=19,
        max_retained_json_bytes=2 * 1024 * 1024,
    )

    create_inference_gateway_routes(settings)

    assert captured == {
        "path": settings.db_path,
        "default_ttl_seconds": 3600,
        "max_ttl_seconds": 3600,
        "default_lease_seconds": 1800,
        "max_lease_seconds": 1800,
        "max_package_bytes": gateway._MAX_PACKAGE_BYTES,
        "max_active_jobs": 7,
        "retention_seconds": 7200,
        "max_retained_rows_per_project": 19,
        "max_retained_json_bytes_per_project": 2 * 1024 * 1024,
    }


def test_server_backend_cannot_start_legacy_inference_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")
    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "jobs.db",
    )

    with pytest.raises(
        InferenceGatewayConfigurationError,
        match="^inference_requires_compute_node$",
    ):
        create_inference_gateway_routes(settings)


@pytest.mark.asyncio
async def test_gateway_requires_loopback_host_and_single_bearer(tmp_path):
    app = _app(tmp_path)
    missing = await _request(app, "GET", "/v1/capabilities")
    remote = await _request(app, "GET", "/v1/capabilities", headers=_auth(), client_host="10.0.0.9")
    duplicate = await _request(
        app,
        "GET",
        "/v1/capabilities",
        headers=[("authorization", "Bearer " + "t" * 32), ("authorization", "Bearer " + "t" * 32)],
    )

    assert missing.status_code == 401
    assert remote.status_code == 403
    assert duplicate.status_code == 401


@pytest.mark.asyncio
async def test_capabilities_fail_closed_without_cloud_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDER_PROVIDER", "ollama")
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cosine")
    response = await _request(_app(tmp_path), "GET", "/v1/capabilities", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["targets"]["cloud"]["ready"] is False
    assert payload["targets"]["client-local"]["ready"] is True
    assert payload["targets"]["client-local"]["readiness_scope"] == "input-dependent"
    assert payload["targets"]["client-local"]["all_supplied_embeddings"]["ready"] is True
    assert payload["targets"]["client-local"]["missing_embeddings"]["ready"] is False
    assert payload["server_local"]["enabled"] is False


@pytest.mark.asyncio
async def test_capabilities_exposes_runtime_embedding_identity_and_cache_boundary(tmp_path):
    response = await _request(_app(tmp_path), "GET", "/v1/capabilities", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    cloud = payload["targets"]["cloud"]
    client_local = payload["targets"]["client-local"]
    assert cloud["embedding_identity"] == "test-embedding"
    assert cloud["embedding_dimension"] == 3
    assert client_local["embedding_identity"] == "test-embedding"
    assert client_local["embedding_dimension"] == 3
    assert client_local["model_identity"] == {
        "required": True,
        "binding": "idempotency-and-package",
        "immutable_revision_recommended": True,
    }
    assert cloud["readiness_scope"] == "input-dependent"
    assert cloud["all_supplied_embeddings"]["ready"] is True
    assert cloud["all_supplied_embeddings"]["requires_embedding"] is False
    assert cloud["all_supplied_embeddings"]["requires_rerank"] is True
    assert cloud["missing_embeddings"]["ready"] is True
    assert cloud["missing_embeddings"]["requires_embedding"] is True
    assert client_local["readiness_scope"] == "input-dependent"
    assert client_local["all_supplied_embeddings"]["requires_rerank"] is False
    assert cloud["mixed_embeddings"] == {
        "ready": True,
        "reason": "",
        "requires_embedding": True,
        "requires_rerank": True,
        "embedding_dimension": 3,
        "embedding_identity": "test-embedding",
    }
    assert client_local["mixed_embeddings"] == {
        "ready": True,
        "reason": "",
        "requires_embedding": True,
        "requires_rerank": False,
        "embedding_dimension": 3,
        "embedding_identity": "test-embedding",
    }
    assert payload["jobs"]["startup_recovery"] == {
        "enabled": True,
        "target": "cloud",
        "project_scoped": True,
        "client_local_claimed": False,
        "max_jobs": 4,
        "overflow": "later-recovery-cycle-or-client-retry",
    }
    assert payload["client_cache"] == hot_memory_cache_contract()
    selection = payload["client_cache"]["selection"]
    assert selection["cadence"] == "lazy-on-cache-operation"
    assert selection["system_score_signals"] == ["access_frequency", "recency", "entry_size"]
    assert selection["retain_false"] == "invalidate-project-memory-identity"


@pytest.mark.asyncio
async def test_capabilities_separates_supplied_and_runtime_embedding_identities(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PP_INFERENCE_CLIENT_VECTOR_IDENTITY", "client-vector-contract")

    response = await _request(_app(tmp_path), "GET", "/v1/capabilities", headers=_auth())

    assert response.status_code == 200
    for target in response.json()["targets"].values():
        assert target["embedding_identity"] is None
        assert target["all_supplied_embeddings"]["embedding_identity"] == "client-vector-contract"
        assert target["missing_embeddings"]["embedding_identity"] == "test-embedding"
        assert target["mixed_embeddings"]["embedding_identity"] == "test-embedding"


@pytest.mark.asyncio
async def test_client_local_job_uses_server_package_and_completes(tmp_path):
    app = _app(tmp_path)
    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(),
    )
    assert response.status_code == 201
    created = response.json()
    package = created["package"]
    result = {
        "contract_version": "client-local-rerank-result/v1",
        "package_hash": package["package_hash"],
        "model_identity": package["model_identity"],
        "items": [
            {"id": "memory-a", "score": 0.9},
            {"id": "memory-b", "score": 0.1},
        ],
    }
    completed = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/complete",
        headers={**_auth(), "content-type": "application/json"},
        json={"lease_token": created["lease_token"], "result": result},
    )

    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["result"]["package_hash"] == package["package_hash"]


@pytest.mark.asyncio
async def test_prefer_respond_async_returns_before_cloud_provider_and_polls_by_key(tmp_path):
    provider_started = threading.Event()
    provider_release = threading.Event()

    class ControlledReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            provider_started.set()
            if not provider_release.wait(timeout=5):
                raise RuntimeError("test_provider_release_timeout")
            return super().rerank_tuples(query, candidates, top_k)

    def service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=ControlledReranker,
            provider_policy_revision="test-policy",
        )

    app = _app(tmp_path, service_factory=service_factory)
    headers = {
        **_auth(),
        "content-type": "application/json",
        "prefer": "respond-async",
    }
    submitted = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers=headers,
        json=_job_payload(target="cloud"),
    )

    assert submitted.status_code == 202
    initial = submitted.json()
    assert initial["status"] == "preparing"
    assert initial["preference_applied"] == "respond-async"
    assert initial["poll_path"].endswith(initial["idempotency_key_hash"])
    assert await asyncio.to_thread(provider_started.wait, 2)

    pending = await _request(app, "GET", initial["poll_path"], headers=_auth())
    assert pending.status_code == 202
    assert pending.json()["status"] in {"pending", "leased"}

    provider_release.set()
    completed = None
    for _ in range(40):
        completed = await _request(app, "GET", initial["poll_path"], headers=_auth())
        if completed.status_code == 200:
            break
        await asyncio.sleep(0.05)

    assert completed is not None
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_async_client_local_job_is_polled_then_explicitly_leased(tmp_path):
    app = _app(tmp_path)
    headers = {
        **_auth(),
        "content-type": "application/json",
        "prefer": "respond-async",
    }
    submitted = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers=headers,
        json=_job_payload(target="client-local"),
    )

    assert submitted.status_code == 202
    initial = submitted.json()
    assert initial["status"] == "preparing"

    polled = None
    for _ in range(40):
        polled = await _request(app, "GET", initial["poll_path"], headers=_auth())
        if polled.status_code == 200:
            break
        await asyncio.sleep(0.01)

    assert polled is not None
    assert polled.status_code == 202
    job = polled.json()
    assert job["status"] == "pending"
    leased = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{job['job_id']}/lease",
        headers={**_auth(), "content-type": "application/json"},
        json={},
    )
    assert leased.status_code == 200
    assert leased.json()["lease_token"]


@pytest.mark.asyncio
async def test_client_local_lease_can_be_renewed_with_current_capability(tmp_path):
    app = _app(tmp_path)
    created = (
        await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(),
        )
    ).json()

    renewed = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/lease/renew",
        headers={**_auth(), "content-type": "application/json"},
        json={"lease_token": created["lease_token"]},
    )
    rejected = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/lease/renew",
        headers={**_auth(), "content-type": "application/json"},
        json={"lease_token": "wrong-lease-token"},
    )

    assert renewed.status_code == 200
    assert renewed.json()["status"] == "leased"
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "inference_job_lease_token_invalid"


@pytest.mark.asyncio
async def test_gateway_accepts_mixed_provided_and_missing_embeddings(tmp_path):
    app = _app(tmp_path)
    payload = _job_payload()
    payload["items"][0]["embedding"] = {
        "vector": [1.0, 2.0, 3.0],
        "dimension": 3,
        "identity": "test-embedding",
        "material_sha256": material_sha256(payload["items"][0]["text"]),
    }

    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=payload,
    )

    assert response.status_code == 201
    assert response.json()["status"] == "leased"
    assert FakeEmbedder.calls == 1


@pytest.mark.asyncio
async def test_mixed_embeddings_bind_supplied_vectors_to_runtime_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_INFERENCE_CLIENT_VECTOR_IDENTITY", "client-vector-contract")
    app = _app(tmp_path)
    payload = _job_payload()
    payload["items"][0]["embedding"] = {
        "vector": [1.0, 2.0, 3.0],
        "dimension": 3,
        "identity": "test-embedding",
        "material_sha256": material_sha256(payload["items"][0]["text"]),
    }

    accepted = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=payload,
    )

    assert accepted.status_code == 201
    assert accepted.json()["package"]["embedding_identity"] == "test-embedding"
    assert FakeEmbedder.calls == 1

    mismatched = _job_payload(key="mixed-wrong-contract")
    mismatched["items"][0]["embedding"] = {
        "vector": [1.0, 2.0, 3.0],
        "dimension": 3,
        "identity": "client-vector-contract",
        "material_sha256": material_sha256(mismatched["items"][0]["text"]),
    }
    rejected = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=mismatched,
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "provided_embedding_identity_mismatch"
    assert FakeEmbedder.calls == 1


@pytest.mark.asyncio
async def test_all_supplied_client_vectors_do_not_construct_runtime_embedder_without_cloud(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "ollama")
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)

    def forbidden_service_factory():
        raise AssertionError("runtime embedder must not be constructed")

    response = await _request(
        _app(tmp_path, service_factory=forbidden_service_factory),
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_all_supplied_vector_payload(),
    )

    assert response.status_code == 201
    assert response.json()["package"]["embedding_identity"] == "test-embedding"
    assert response.json()["package"]["embedding_dimension"] == 3
    assert FakeEmbedder.calls == 0


@pytest.mark.asyncio
async def test_all_supplied_cloud_vectors_survive_embedding_outage(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "ollama")
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    rerank_service_calls = 0

    def forbidden_preparation_service():
        raise AssertionError("all-supplied cloud input must not construct an embedder")

    def rerank_only_service():
        nonlocal rerank_service_calls
        rerank_service_calls += 1
        return BackendInferenceService.from_rerank_runtime(reranker_factory=FakeReranker)

    app = _app(
        tmp_path,
        service_factory=forbidden_preparation_service,
        rerank_service_factory=rerank_only_service,
    )
    capabilities = await _request(app, "GET", "/v1/capabilities", headers=_auth())
    cloud_capability = capabilities.json()["targets"]["cloud"]
    assert cloud_capability["ready"] is True
    assert cloud_capability["all_supplied_embeddings"]["ready"] is True
    assert cloud_capability["missing_embeddings"]["ready"] is False

    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_all_supplied_vector_payload(target="cloud"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["result"]["model_identity"] == "test-reranker"
    assert rerank_service_calls == 1
    assert FakeEmbedder.calls == 0


@pytest.mark.asyncio
async def test_missing_client_vector_requires_hosted_embedding_before_runtime_factory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "ollama")
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)

    payload = _all_supplied_vector_payload()
    payload["items"][1].pop("embedding")

    def forbidden_service_factory():
        raise AssertionError("runtime embedder must not be constructed")

    response = await _request(
        _app(tmp_path, service_factory=forbidden_service_factory),
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=payload,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "cloud_embedding_provider_not_selected"


@pytest.mark.asyncio
async def test_all_supplied_client_vectors_require_server_owned_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "ollama")
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("PP_INFERENCE_CLIENT_VECTOR_IDENTITY", raising=False)
    monkeypatch.delenv("PP_INFERENCE_CLIENT_VECTOR_DIMENSION", raising=False)

    response = await _request(
        _app(tmp_path),
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_all_supplied_vector_payload(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "client_vector_embedding_contract_missing"


@pytest.mark.asyncio
async def test_same_idempotency_key_does_not_reembed_and_changed_input_conflicts(tmp_path):
    app = _app(tmp_path)
    first = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(),
    )
    calls_after_first = FakeEmbedder.calls
    retry = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json={**_job_payload(), "request_id": "different-request-id"},
    )
    conflict = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(query="different query"),
    )
    model_conflict_payload = _job_payload()
    model_conflict_payload["model_identity"] = "client:test-model@revision-2"
    model_conflict = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=model_conflict_payload,
    )

    assert first.status_code == 201
    assert retry.status_code == 202
    assert retry.json()["status"] == "leased"
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert conflict.status_code == 409
    assert model_conflict.status_code == 409
    assert model_conflict.json()["error"]["code"] == "inference_job_idempotency_conflict"
    assert FakeEmbedder.calls == calls_after_first


@pytest.mark.asyncio
async def test_client_completion_rejects_model_identity_not_bound_by_package(tmp_path):
    app = _app(tmp_path)
    created = (
        await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(),
        )
    ).json()
    package = created["package"]

    completed = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/complete",
        headers={**_auth(), "content-type": "application/json"},
        json={
            "lease_token": created["lease_token"],
            "result": {
                "contract_version": "client-local-rerank-result/v1",
                "package_hash": package["package_hash"],
                "model_identity": "client:test-model@different-revision",
                "items": [
                    {"id": "memory-a", "score": 0.9},
                    {"id": "memory-b", "score": 0.1},
                ],
            },
        },
    )

    assert completed.status_code == 400
    assert completed.json()["error"]["code"] == "client_local_model_identity_mismatch"


@pytest.mark.asyncio
async def test_cloud_job_cannot_be_client_leased(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("EMBEDDER_API_KEY", "synthetic-key")
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud")
    monkeypatch.setenv("PP_RERANK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("PP_RERANK_API_KEY", "synthetic-key")
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL", "rerank-test")
    app = _app(tmp_path)
    # A fake service still makes this a deterministic cloud-job route test.
    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(target="cloud"),
    )
    job_id = response.json()["job_id"]
    lease = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{job_id}/lease",
        headers=_auth(),
    )
    renew = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{job_id}/lease/renew",
        headers={**_auth(), "content-type": "application/json"},
        json={"lease_token": "not-applicable"},
    )

    assert lease.status_code == 403
    assert lease.json()["error"]["code"] == "inference_cloud_lease_internal"
    assert renew.status_code == 403
    assert renew.json()["error"]["code"] == "inference_cloud_lease_internal"


@pytest.mark.asyncio
async def test_body_contract_rejects_unknown_fields_and_non_json(tmp_path):
    app = _app(tmp_path)
    unknown = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json={**_job_payload(), "project_id": "project:attacker"},
    )
    non_json = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "text/plain"},
        json=None,
    )

    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "inference_field_not_allowed"
    assert non_json.status_code == 400
    assert non_json.json()["error"]["code"] == "inference_content_type_invalid"


@pytest.mark.asyncio
async def test_model_identity_is_required_only_for_client_local_jobs(tmp_path):
    app = _app(tmp_path)
    missing = _job_payload()
    del missing["model_identity"]
    client_response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=missing,
    )
    cloud = _job_payload(target="cloud")
    cloud["model_identity"] = "client:model-not-applicable"
    cloud_response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=cloud,
    )

    assert client_response.status_code == 400
    assert client_response.json()["error"]["code"] == "client_local_model_identity_invalid"
    assert cloud_response.status_code == 400
    assert cloud_response.json()["error"]["code"] == "client_local_model_identity_not_applicable"


@pytest.mark.parametrize(
    "untrusted_message",
    [
        "sk-syntheticcredential0123456789abcdef",
        "syntheticcredential0123456789abcdef",
        "store_error",
        "free-form-provider-detail",
    ],
)
def test_safe_code_rejects_credentials_and_untrusted_error_namespaces(untrusted_message):
    assert gateway._safe_code(ValueError(untrusted_message)) == "inference_gateway_unavailable"


def test_safe_code_preserves_stable_contract_namespace():
    assert (
        gateway._safe_code(ValueError("inference_field_not_allowed"))
        == "inference_field_not_allowed"
    )


@pytest.mark.asyncio
async def test_concurrent_same_key_has_one_preparing_response(tmp_path):
    app = _app(tmp_path)
    payload = _job_payload()
    headers = {**_auth(), "content-type": "application/json"}
    first, second = await asyncio.gather(
        _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload),
        _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload),
    )

    assert sorted((first.status_code, second.status_code)) == [201, 202]
    assert FakeEmbedder.calls == 1


@pytest.mark.asyncio
async def test_gateway_returns_429_when_project_active_capacity_is_full(tmp_path):
    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        max_active_jobs=1,
    )
    app = _app(tmp_path, settings=settings)
    headers = {**_auth(), "content-type": "application/json"}
    first = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers=headers,
        json=_job_payload(key="first"),
    )
    second = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers=headers,
        json=_job_payload(key="second"),
    )

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "inference_job_project_capacity_exceeded"


@pytest.mark.asyncio
async def test_completed_cloud_retry_does_not_depend_on_current_provider_readiness(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    payload = _job_payload(target="cloud")
    headers = {**_auth(), "content-type": "application/json"}
    first = await _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)
    calls_after_first = FakeEmbedder.calls

    monkeypatch.delenv("PP_RERANK_API_KEY")
    retry = await _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert retry.json()["status"] == "completed"
    assert FakeEmbedder.calls == calls_after_first


@pytest.mark.asyncio
async def test_existing_client_package_survives_embedding_configuration_outage(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    payload = _job_payload()
    headers = {**_auth(), "content-type": "application/json"}
    first = await _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)
    calls_after_first = FakeEmbedder.calls

    monkeypatch.delenv("EMBEDDER_API_KEY")
    retry = await _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)

    assert first.status_code == 201
    assert retry.status_code == 202
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert retry.json()["package"] == first.json()["package"]
    assert FakeEmbedder.calls == calls_after_first


@pytest.mark.asyncio
async def test_client_result_can_complete_after_restart_without_provider_factory(tmp_path):
    first_app = _app(tmp_path)
    created_response = await _request(
        first_app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(),
    )
    created = created_response.json()
    package = created["package"]

    def unavailable_service_factory():
        raise AssertionError("provider factory must not run during durable completion")

    restarted_app = _app(tmp_path, service_factory=unavailable_service_factory)
    completed = await _request(
        restarted_app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/complete",
        headers={**_auth(), "content-type": "application/json"},
        json={
            "lease_token": created["lease_token"],
            "result": {
                "contract_version": "client-local-rerank-result/v1",
                "package_hash": package["package_hash"],
                "model_identity": package["model_identity"],
                "items": [
                    {"id": "memory-a", "score": 0.9},
                    {"id": "memory-b", "score": 0.1},
                ],
            },
        },
    )

    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_pending_cloud_job_recovers_after_restart_without_embedder_or_client_claim(
    tmp_path,
):
    current = [datetime(2026, 7, 23, tzinfo=timezone.utc)]

    def store_factory(path):
        return InferenceJobStore(path, clock=lambda: current[0])

    class InitiallyUnavailableReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            del query, candidates, top_k
            raise RuntimeError("test_rerank_temporarily_unavailable")

    def initial_service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=InitiallyUnavailableReranker,
            provider_policy_revision="test-policy",
        )

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
        max_concurrency=1,
    )
    first_app = _app(
        tmp_path,
        settings=settings,
        service_factory=initial_service_factory,
        store_factory=store_factory,
    )
    client_job = (
        await _request(
            first_app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(key="client-before-restart"),
        )
    ).json()
    cloud_response = await _request(
        first_app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(key="cloud-explicit-recovery", target="cloud"),
    )
    cloud_job = cloud_response.json()
    current[0] += timedelta(seconds=1)
    startup_cloud_response = await _request(
        first_app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(key="cloud-startup-recovery", target="cloud"),
    )
    startup_cloud_job = startup_cloud_response.json()
    other_settings = replace(settings, project_id="project:other")
    other_app = _app(
        tmp_path,
        settings=other_settings,
        service_factory=initial_service_factory,
        store_factory=store_factory,
    )
    other_cloud_response = await _request(
        other_app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(key="other-project-cloud", target="cloud"),
    )
    other_cloud_job = other_cloud_response.json()
    embedding_calls_before_recovery = FakeEmbedder.calls
    assert cloud_response.status_code == 201
    assert cloud_job["status"] == "leased"
    assert startup_cloud_response.status_code == 201
    assert startup_cloud_job["status"] == "leased"
    assert other_cloud_response.status_code == 201
    assert other_cloud_job["status"] == "leased"

    current[0] += timedelta(seconds=6)

    def recovered_rerank_service():
        return BackendInferenceService.from_rerank_runtime(
            reranker_factory=FakeReranker,
            provider_policy_revision="test-policy",
        )

    recovered = await recover_pending_cloud_jobs(
        settings,
        rerank_service_factory=recovered_rerank_service,
        store_factory=store_factory,
    )

    store = store_factory(settings.db_path)
    recovered_cloud = store.require(cloud_job["job_id"], settings.project_id)
    pending_startup_cloud = store.require(startup_cloud_job["job_id"], settings.project_id)
    untouched_client = store.require(client_job["job_id"], settings.project_id)
    untouched_other_project = store.require(
        other_cloud_job["job_id"],
        other_settings.project_id,
    )
    assert recovered == 1
    assert recovered_cloud.status == "completed"
    assert pending_startup_cloud.status == "pending"
    assert untouched_client.target == "client-local"
    assert untouched_client.status == "pending"
    assert untouched_other_project.target == "cloud"
    assert untouched_other_project.status == "pending"

    def forbidden_preparation_service():
        raise AssertionError("startup recovery must not construct an embedder")

    restarted_app = gateway.create_inference_gateway_app(
        settings,
        service_factory=forbidden_preparation_service,
        rerank_service_factory=recovered_rerank_service,
        store_factory=store_factory,
    )
    async with restarted_app.router.lifespan_context(restarted_app):
        for _ in range(100):
            recovered_on_startup = store.require(
                startup_cloud_job["job_id"],
                settings.project_id,
            )
            if recovered_on_startup.status == "completed":
                break
            await asyncio.sleep(0.01)

    assert recovered_on_startup.status == "completed"
    assert store.require(client_job["job_id"], settings.project_id).status == "pending"
    assert store.require(other_cloud_job["job_id"], other_settings.project_id).status == "pending"
    assert FakeEmbedder.calls == embedding_calls_before_recovery


@pytest.mark.asyncio
async def test_fast_restart_recovers_cloud_job_after_live_lease_expires(
    tmp_path,
    monkeypatch,
):
    current = [datetime(2026, 7, 23, tzinfo=timezone.utc)]
    provider_attempts = []

    def store_factory(path):
        return InferenceJobStore(path, clock=lambda: current[0])

    class InitiallyUnavailableReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            del query, candidates, top_k
            provider_attempts.append("initial")
            raise RuntimeError("test_rerank_temporarily_unavailable")

    class RecoveredReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            provider_attempts.append("recovered")
            return super().rerank_tuples(query, candidates, top_k)

    def initial_service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=InitiallyUnavailableReranker,
            provider_policy_revision="test-policy",
        )

    def recovered_rerank_service():
        return BackendInferenceService.from_rerank_runtime(
            reranker_factory=RecoveredReranker,
            provider_policy_revision="test-policy",
        )

    def forbidden_preparation_service():
        raise AssertionError("restart recovery must not construct an embedder")

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
        max_concurrency=1,
    )
    monkeypatch.setattr(
        gateway,
        "_cloud_recovery_poll_interval",
        lambda _lease_seconds: 0.01,
    )

    first_app = gateway.create_inference_gateway_app(
        settings,
        service_factory=initial_service_factory,
        store_factory=store_factory,
    )
    async with first_app.router.lifespan_context(first_app):
        client_job = (
            await _request(
                first_app,
                "POST",
                "/v1/rerank/jobs",
                headers={**_auth(), "content-type": "application/json"},
                json=_job_payload(key="fast-restart-client"),
            )
        ).json()
        cloud_response = await _request(
            first_app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(key="fast-restart-cloud", target="cloud"),
        )

    cloud_job = cloud_response.json()
    store = store_factory(settings.db_path)
    assert cloud_response.status_code == 201
    assert store.require(cloud_job["job_id"], settings.project_id).status == "leased"
    assert provider_attempts == ["initial"]

    restarted_app = gateway.create_inference_gateway_app(
        settings,
        service_factory=forbidden_preparation_service,
        rerank_service_factory=recovered_rerank_service,
        store_factory=store_factory,
    )
    async with restarted_app.router.lifespan_context(restarted_app):
        await asyncio.sleep(0.05)
        assert store.require(cloud_job["job_id"], settings.project_id).status == "leased"
        assert provider_attempts == ["initial"]

        current[0] += timedelta(seconds=6)
        for _ in range(100):
            recovered = store.require(cloud_job["job_id"], settings.project_id)
            if recovered.status == "completed":
                break
            await asyncio.sleep(0.01)

    assert recovered.status == "completed"
    assert provider_attempts == ["initial", "recovered"]
    assert store.require(client_job["job_id"], settings.project_id).status == "pending"


@pytest.mark.asyncio
async def test_restart_recovery_executes_a_full_claim_batch_concurrently(tmp_path):
    current = [datetime(2026, 7, 23, tzinfo=timezone.utc)]

    def store_factory(path):
        return InferenceJobStore(path, clock=lambda: current[0])

    class InitiallyUnavailableReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            del query, candidates, top_k
            raise RuntimeError("test_rerank_temporarily_unavailable")

    def initial_service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=InitiallyUnavailableReranker,
            provider_policy_revision="test-policy",
        )

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
        max_concurrency=2,
    )
    first_app = _app(
        tmp_path,
        settings=settings,
        service_factory=initial_service_factory,
        store_factory=store_factory,
    )
    jobs = []
    for index in range(2):
        response = await _request(
            first_app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(key=f"concurrent-recovery-{index}", target="cloud"),
        )
        assert response.status_code == 201
        jobs.append(response.json())

    current[0] += timedelta(seconds=6)
    barrier = threading.Barrier(2, timeout=2)
    active = 0
    max_active = 0
    lock = threading.Lock()

    class ConcurrentReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait()
                return super().rerank_tuples(query, candidates, top_k)
            finally:
                with lock:
                    active -= 1

    def recovered_rerank_service():
        return BackendInferenceService.from_rerank_runtime(
            reranker_factory=ConcurrentReranker,
            provider_policy_revision="test-policy",
        )

    recovered = await recover_pending_cloud_jobs(
        settings,
        rerank_service_factory=recovered_rerank_service,
        store_factory=store_factory,
    )

    store = store_factory(settings.db_path)
    assert recovered == 2
    assert max_active == 2
    assert all(
        store.require(job["job_id"], settings.project_id).status == "completed" for job in jobs
    )


@pytest.mark.asyncio
async def test_concurrent_client_completions_accept_exactly_one_result(tmp_path):
    app = _app(tmp_path)
    created = (
        await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(),
        )
    ).json()
    package = created["package"]
    completion_payload = {
        "lease_token": created["lease_token"],
        "result": {
            "contract_version": "client-local-rerank-result/v1",
            "package_hash": package["package_hash"],
            "model_identity": package["model_identity"],
            "items": [
                {"id": "memory-a", "score": 0.9},
                {"id": "memory-b", "score": 0.1},
            ],
        },
    }
    path = f"/v1/rerank/jobs/{created['job_id']}/complete"
    headers = {**_auth(), "content-type": "application/json"}

    first, second = await asyncio.gather(
        _request(app, "POST", path, headers=headers, json=completion_payload),
        _request(app, "POST", path, headers=headers, json=completion_payload),
    )

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    accepted = first if first.status_code == 200 else second
    rejected = second if first.status_code == 200 else first
    assert accepted.json()["status"] == "completed"
    assert rejected.json()["error"]["code"] == "inference_job_already_completed"


@pytest.mark.asyncio
async def test_late_client_result_is_rejected_and_another_device_can_reclaim(tmp_path):
    current = [datetime(2026, 7, 23, tzinfo=timezone.utc)]

    def store_factory(path):
        return InferenceJobStore(path, clock=lambda: current[0])

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
    )
    app = _app(tmp_path, settings=settings, store_factory=store_factory)
    created = (
        await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers={**_auth(), "content-type": "application/json"},
            json=_job_payload(),
        )
    ).json()
    package = created["package"]
    current[0] += timedelta(seconds=6)

    late = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/complete",
        headers={**_auth(), "content-type": "application/json"},
        json={
            "lease_token": created["lease_token"],
            "result": {
                "contract_version": "client-local-rerank-result/v1",
                "package_hash": package["package_hash"],
                "model_identity": package["model_identity"],
                "items": [
                    {"id": "memory-a", "score": 0.9},
                    {"id": "memory-b", "score": 0.1},
                ],
            },
        },
    )
    reclaimed = await _request(
        app,
        "POST",
        f"/v1/rerank/jobs/{created['job_id']}/lease",
        headers=_auth(),
    )

    assert late.status_code == 409
    assert late.json()["error"]["code"] == "inference_job_not_leased"
    assert reclaimed.status_code == 200
    assert reclaimed.json()["lease_token"] != created["lease_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "expected_code"),
    [
        ({"EMBEDDER_PROVIDER": "ollama"}, "cloud_embedding_provider_not_selected"),
        ({"EMBEDDER_BASE_URL": "http://example.invalid/v1"}, "cloud_embedding_base_url_invalid"),
        (
            {"EMBEDDER_BASE_URL": "https://wiki.example.invalid/"},
            "cloud_embedding_base_url_invalid",
        ),
        ({"EMBEDDER_API_KEY": None}, "cloud_embedding_api_key_missing"),
        ({"EMBEDDER_MODEL": ""}, "cloud_embedding_model_invalid"),
        ({"PP_RERANK_PROVIDERS": "cloud,ollama"}, "cloud_rerank_local_fallback_forbidden"),
        ({"PP_RERANK_API_KEY": None}, "cloud_rerank_api_key_missing"),
        ({"PP_RERANK_CLOUD_MODEL": ""}, "cloud_rerank_model_missing"),
    ],
)
async def test_new_job_rejects_invalid_cloud_configuration_before_provider_call(
    tmp_path, monkeypatch, environment, expected_code
):
    for name, value in environment.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    app = _app(tmp_path)
    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(target="cloud"),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == expected_code
    assert FakeEmbedder.calls == 0


@pytest.mark.parametrize(
    "base_url",
    [
        "https://localhost/v1",
        "https://loopback.localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.1/v1",
        "https://192.168.1.1/v1",
        "https://169.254.169.254/v1",
        "https://224.0.0.1/v1",
        "https://0.0.0.0/v1",
        "https://[::1]/v1",
        "https://[fc00::1]/v1",
        "https://[fe80::1]/v1",
        "https://[ff00::1]/v1",
        "https://[::]/v1",
        "https://user:password@example.invalid/v1",
        "https://example.invalid:bad/v1",
    ],
)
def test_cloud_provider_endpoint_rejects_non_public_or_malformed_hosts(base_url):
    assert not gateway._provider_endpoint_is_cloud(
        provider="test-provider",
        base_url=base_url,
        api_key="synthetic-key",
    )


def test_cloud_provider_endpoint_requires_explicit_operator_host_allowlist(monkeypatch):
    monkeypatch.delenv("PP_INFERENCE_PROVIDER_HOST_ALLOWLIST", raising=False)

    assert not gateway._provider_endpoint_is_cloud(
        provider="test-provider",
        base_url="https://example.invalid/v1",
        api_key="synthetic-key",
    )

    monkeypatch.setenv("PP_INFERENCE_PROVIDER_HOST_ALLOWLIST", "example.invalid")
    assert gateway._provider_endpoint_is_cloud(
        provider="test-provider",
        base_url="https://example.invalid/v1",
        api_key="synthetic-key",
    )


@pytest.mark.asyncio
async def test_invalid_top_k_is_rejected_before_provider_call(tmp_path):
    app = _app(tmp_path)
    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json={**_job_payload(target="cloud"), "top_k": 3},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "rerank_top_k_invalid"
    assert FakeEmbedder.calls == 0


@pytest.mark.asyncio
async def test_slow_cloud_provider_renews_lease_before_completion(tmp_path):
    class SlowReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            time.sleep(2.7)
            return super().rerank_tuples(query, candidates, top_k)

    class TrackingStore(InferenceJobStore):
        renewals = 0

        def renew_lease(self, *args, **kwargs):
            type(self).renewals += 1
            return super().renew_lease(*args, **kwargs)

    TrackingStore.renewals = 0

    def service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=SlowReranker,
            provider_policy_revision="test-policy",
        )

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
    )
    app = _app(
        tmp_path,
        service_factory=service_factory,
        settings=settings,
        store_factory=TrackingStore,
    )
    response = await _request(
        app,
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(target="cloud"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert TrackingStore.renewals >= 1


@pytest.mark.asyncio
async def test_cloud_job_survives_initiating_http_cancellation(tmp_path):
    provider_started = threading.Event()
    provider_release = threading.Event()

    class ControlledReranker(FakeReranker):
        def rerank_tuples(self, query, candidates, top_k=10):
            provider_started.set()
            if not provider_release.wait(timeout=5):
                raise RuntimeError("test_provider_release_timeout")
            return super().rerank_tuples(query, candidates, top_k)

    def service_factory():
        return BackendInferenceService(
            embedder=FakeEmbedder(),
            reranker_factory=ControlledReranker,
            provider_policy_revision="test-policy",
        )

    app = _app(tmp_path, service_factory=service_factory)
    payload = _job_payload(target="cloud")
    headers = {**_auth(), "content-type": "application/json"}
    request_task = asyncio.create_task(
        _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)
    )
    assert await asyncio.to_thread(provider_started.wait, 2)

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    provider_release.set()

    response = None
    for _ in range(40):
        response = await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers=headers,
            json=payload,
        )
        if response.status_code == 200:
            break
        await asyncio.sleep(0.05)

    assert response is not None
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_embedding_preparation_survives_initiating_http_cancellation(tmp_path):
    embedding_started = threading.Event()
    embedding_release = threading.Event()
    reservation_renewed = threading.Event()

    class ControlledEmbedder(FakeEmbedder):
        calls = 0

        def embed_batch(self, texts):
            type(self).calls += 1
            embedding_started.set()
            if not embedding_release.wait(timeout=5):
                raise RuntimeError("test_embedding_release_timeout")
            return [[1.0, 2.0, 3.0] for _ in texts]

    class TrackingStore(InferenceJobStore):
        renewals = 0

        def renew_submission(self, *args, **kwargs):
            type(self).renewals += 1
            renewed = super().renew_submission(*args, **kwargs)
            reservation_renewed.set()
            return renewed

    TrackingStore.renewals = 0

    def service_factory():
        return BackendInferenceService(
            embedder=ControlledEmbedder(),
            reranker_factory=FakeReranker,
            provider_policy_revision="test-policy",
        )

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
    )
    app = _app(
        tmp_path,
        service_factory=service_factory,
        settings=settings,
        store_factory=TrackingStore,
    )
    payload = _job_payload()
    headers = {**_auth(), "content-type": "application/json"}
    request_task = asyncio.create_task(
        _request(app, "POST", "/v1/rerank/jobs", headers=headers, json=payload)
    )
    assert await asyncio.to_thread(embedding_started.wait, 2)

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert await asyncio.to_thread(reservation_renewed.wait, 4)
    embedding_release.set()

    response = None
    for _ in range(40):
        response = await _request(
            app,
            "POST",
            "/v1/rerank/jobs",
            headers=headers,
            json=payload,
        )
        if "package" in response.json():
            break
        await asyncio.sleep(0.05)

    assert response is not None
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert ControlledEmbedder.calls == 1
    assert TrackingStore.renewals >= 1


@pytest.mark.asyncio
async def test_preparation_renewal_failure_prevents_stale_finalize(tmp_path):
    class SlowEmbedder(FakeEmbedder):
        def embed_batch(self, texts):
            time.sleep(2.7)
            return super().embed_batch(texts)

    class FailingRenewStore(InferenceJobStore):
        finalizations = 0

        def renew_submission(self, *args, **kwargs):
            del args, kwargs
            raise InferenceJobConflictError("inference_job_reservation_expired")

        def finalize_submission(self, *args, **kwargs):
            type(self).finalizations += 1
            return super().finalize_submission(*args, **kwargs)

    FailingRenewStore.finalizations = 0

    def service_factory():
        return BackendInferenceService(
            embedder=SlowEmbedder(),
            reranker_factory=FakeReranker,
            provider_policy_revision="test-policy",
        )

    settings = InferenceGatewaySettings(
        enabled=True,
        project_id="project:test",
        token="t" * 32,
        db_path=tmp_path / "inference_jobs.db",
        ttl_seconds=900,
        lease_seconds=5,
    )
    response = await _request(
        _app(
            tmp_path,
            service_factory=service_factory,
            settings=settings,
            store_factory=FailingRenewStore,
        ),
        "POST",
        "/v1/rerank/jobs",
        headers={**_auth(), "content-type": "application/json"},
        json=_job_payload(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "inference_job_reservation_expired"
    assert FailingRenewStore.finalizations == 0
