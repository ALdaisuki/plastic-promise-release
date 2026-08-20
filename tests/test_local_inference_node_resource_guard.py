from __future__ import annotations

import subprocess

import httpx
import pytest

from plastic_promise.local_inference_node.app import create_node_app
from plastic_promise.local_inference_node.contract import NodeIdentity
from plastic_promise.local_inference_node.resource_guard import (
    NodeResourceGuard,
    resource_guard_from_environment,
)

_AUTHORIZATION = "Bearer private-node-resource-test-token"


def _identity() -> NodeIdentity:
    return NodeIdentity(
        protocol_version="local-inference-node/v1",
        node_id="resource-node",
        embedding_model="embedding",
        embedding_revision="a" * 40,
        embedding_dimension=2,
        embedding_normalization="l2",
        rerank_model="rerank",
        rerank_revision="b" * 40,
    )


class _Embedder:
    def embed_batch(self, _texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]]


@pytest.mark.asyncio
async def test_guard_defers_when_another_gpu_workload_is_busy(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "55\n", ""),
    )
    guard = NodeResourceGuard(gpu_utilization_limit=20, sample_ttl_seconds=60)

    decision = await guard.admit(active=0)
    assert decision.allowed is False
    assert decision.state == "degraded"
    assert decision.reason == "external_gpu_overloaded"
    assert decision.gpu_utilization_percent == 55.0

    # A request already owned by this node is allowed to finish instead of
    # being interrupted by its own aggregate GPU utilization.
    owned = await guard.admit(active=1)
    assert owned.allowed is True
    assert owned.state == "owned"

    # A new request must still observe the external workload while the node
    # finishes its own in-flight operation.
    new_request = await guard.admit_new_request()
    assert new_request.allowed is False
    assert new_request.reason == "external_gpu_overloaded"


@pytest.mark.asyncio
async def test_node_returns_retryable_resource_degradation_without_running_inference(
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "80\n", ""),
    )
    guard = NodeResourceGuard(gpu_utilization_limit=20, sample_ttl_seconds=60)
    app = create_node_app(
        _identity(),
        authorization=_AUTHORIZATION,
        embedder=_Embedder(),
        resource_guard=guard,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": _AUTHORIZATION},
    ) as client:
        response = await client.post("/v1/embeddings", json={"input": ["busy"]})
        health = await client.get("/health")

    assert response.status_code == 429
    assert response.json() == {"error": "node_overloaded"}
    assert response.headers["retry-after"] == "5"
    assert health.json()["resource_guard"] == {
        "state": "degraded",
        "reason": "external_gpu_overloaded",
        "gpu_utilization_percent": 80.0,
        "retry_after_seconds": 5,
    }


def test_resource_guard_environment_defaults_and_override():
    guard = resource_guard_from_environment({})
    assert guard.enabled is True
    assert guard.gpu_utilization_limit == 70.0

    disabled = resource_guard_from_environment(
        {
            "PP_LOCAL_NODE_RESOURCE_GUARD": "off",
            "PP_LOCAL_NODE_RESOURCE_GPU_UTILIZATION_LIMIT": "75",
            "PP_LOCAL_NODE_RESOURCE_SAMPLE_TTL_SECONDS": "3",
            "PP_LOCAL_NODE_RESOURCE_RETRY_AFTER_SECONDS": "9",
        }
    )
    assert disabled.enabled is False
    assert disabled.gpu_utilization_limit == 75.0
    assert disabled.sample_ttl_seconds == 3.0
    assert disabled.retry_after_seconds == 9
