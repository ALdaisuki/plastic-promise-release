"""Focused contracts for the PR6 secret-free model catalog."""

from __future__ import annotations

import hashlib

import pytest

from plastic_promise.deployment import (
    MODEL_RUNTIME_CLOUD,
    MODEL_RUNTIME_REMOTE,
    EmbeddingIdentity,
    EndpointCapability,
    EndpointContractError,
    ModelCatalog,
    ModelResourceEstimate,
    ReleaseBundleError,
    RerankIdentity,
    canonical_model_catalog_bytes,
)


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _embedding(
    *, model: str = "acme/embedding-v1", artifact: str = "embedding"
) -> EmbeddingIdentity:
    return EmbeddingIdentity(
        model=model,
        revision="a" * 40,
        dimension=1024,
        normalization="l2",
        metric="cosine",
        tokenization="wordpiece",
        pooling="mean",
        artifact_sha256=_digest(artifact),
        golden_vector_sha256=_digest(f"golden:{artifact}"),
    )


def _rerank(*, artifact: str = "rerank") -> RerankIdentity:
    return RerankIdentity(
        model="acme/rerank-v1",
        revision="b" * 40,
        artifact_sha256=_digest(artifact),
        scoring_schema="rerank-score/v1",
    )


def _resources(**overrides: object) -> ModelResourceEstimate:
    values: dict[str, object] = {
        "model_cache_bytes": 5 * 1024**3,
        "minimum_system_memory_bytes": 8 * 1024**3,
        "minimum_gpu_memory_bytes": 0,
    }
    values.update(overrides)
    return ModelResourceEstimate(**values)  # type: ignore[arg-type]


def _catalog(**overrides: object) -> ModelCatalog:
    values: dict[str, object] = {
        "catalog_id": "rc-models-v1",
        "profile_id": "split-accelerated",
        "runtime": MODEL_RUNTIME_REMOTE,
        "capabilities": (
            EndpointCapability("embedding", "embedding/v1"),
            EndpointCapability("rerank", "rerank/v1"),
        ),
        "resource_estimate": _resources(),
        "embedding": _embedding(),
        "rerank": _rerank(),
    }
    values.update(overrides)
    return ModelCatalog(**values)  # type: ignore[arg-type]


def test_catalog_binds_complete_embedding_and_rerank_identity_deterministically():
    catalog = _catalog()
    reordered = _catalog(
        capabilities=(
            EndpointCapability("rerank", "rerank/v1"),
            EndpointCapability("embedding", "embedding/v1"),
        )
    )

    assert catalog.capability_contracts == ("embedding/v1", "rerank/v1")
    assert catalog.digest == reordered.digest
    assert canonical_model_catalog_bytes(catalog) == canonical_model_catalog_bytes(reordered)
    payload = catalog.to_dict()
    assert payload["embedding"] == {
        "model": "acme/embedding-v1",
        "revision": "a" * 40,
        "dimension": 1024,
        "normalization": "l2",
        "metric": "cosine",
        "tokenization": "wordpiece",
        "pooling": "mean",
        "artifact_sha256": _digest("embedding"),
        "golden_vector_sha256": _digest("golden:embedding"),
    }
    assert payload["rerank"] == {
        "model": "acme/rerank-v1",
        "revision": "b" * 40,
        "artifact_sha256": _digest("rerank"),
        "scoring_schema": "rerank-score/v1",
    }
    assert payload["resource_estimate"] == {
        "model_cache_bytes": 5 * 1024**3,
        "minimum_system_memory_bytes": 8 * 1024**3,
        "minimum_gpu_memory_bytes": 0,
    }


def test_catalog_rejects_profile_runtime_and_identity_mismatches():
    with pytest.raises(ReleaseBundleError, match="profile_runtime_incompatible"):
        _catalog(profile_id="local-cloud")

    with pytest.raises(ReleaseBundleError, match="embedding_identity_mismatch"):
        _catalog(embedding=None)

    with pytest.raises(ReleaseBundleError, match="rerank_identity_mismatch"):
        _catalog(capabilities=(EndpointCapability("embedding", "embedding/v1"),))

    cloud = _catalog(
        profile_id="local-cloud",
        runtime=MODEL_RUNTIME_CLOUD,
        catalog_id="cloud-models-v1",
    )
    assert cloud.profile_id == "local-cloud"


def test_catalog_rejects_path_like_or_url_like_model_references_without_adding_secret_fields():
    with pytest.raises(ReleaseBundleError, match="model_reference_unsafe"):
        _catalog(embedding=_embedding(model="../private-weights"))

    with pytest.raises(EndpointContractError, match="embedding_model_invalid"):
        _catalog(embedding=_embedding(model="https://models.example.invalid/embedding"))

    with pytest.raises(ReleaseBundleError, match="model_reference_unsafe"):
        _catalog(embedding=_embedding(model="sk-0123456789abcdef0123456789abcdef"))


def test_catalog_rejects_malformed_typed_input_with_a_stable_error_code():
    with pytest.raises(ReleaseBundleError, match="capability_invalid"):
        _catalog(capabilities=(object(),))


def test_catalog_binds_bounded_resource_estimates_and_rejects_unsafe_json_input():
    catalog = _catalog()
    gpu_catalog = _catalog(resource_estimate=_resources(minimum_gpu_memory_bytes=16 * 1024**3))

    assert catalog.digest != gpu_catalog.digest
    assert ModelCatalog.from_dict(catalog.to_dict()) == catalog

    with pytest.raises(ReleaseBundleError, match="model_cache_bytes_invalid"):
        _resources(model_cache_bytes=0)

    with pytest.raises(ReleaseBundleError, match="gpu_memory_bytes_invalid"):
        _resources(minimum_gpu_memory_bytes=-1)

    payload = catalog.to_dict()
    payload["api_key"] = "not-accepted"
    with pytest.raises(ReleaseBundleError, match="secret_forbidden"):
        ModelCatalog.from_dict(payload)
