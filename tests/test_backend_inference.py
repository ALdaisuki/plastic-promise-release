from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from plastic_promise.core.backend_inference import (
    CLIENT_LOCAL_RERANK_CONTRACT,
    CLIENT_LOCAL_RESULT_CONTRACT,
    PREPARED_INPUT_CONTRACT,
    RERANK_CONTRACT,
    RERANK_REQUEST_CONTRACT,
    BackendInferenceService,
    material_sha256,
)
from plastic_promise.core.embedder import Embedder


class _Embedder(Embedder):
    def __init__(self, *, vectors=None, dim=3, identity="embedder:test@revision-1"):
        self._vectors = vectors or [[0.0, 1.0, 0.0]]
        self._dim = dim
        self._identity = identity
        self.calls = []

    def embed(self, text):
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [list(vector) for vector in self._vectors[: len(texts)]]

    @property
    def dim(self):
        return self._dim

    @property
    def model_name(self):
        return "test"

    @property
    def index_model_name(self):
        return self._identity


class _Reranker:
    def __init__(self, *, identity="rerank:test@revision-1", invalid=None):
        self._identity = identity
        self._invalid = invalid
        self._diagnostics = {}

    def rerank_tuples(self, query, candidates, top_k):
        self._diagnostics = {
            "provider": "test",
            "status": "success",
            "query_hash": material_sha256(query),
            "usage": {"input_tokens": len(candidates)},
            "attempts": [{"provider": "test"}],
        }
        if self._invalid == "duplicate":
            return [(candidates[0][0], 0.9), (candidates[0][0], 0.8)]
        if self._invalid == "short":
            return [(candidates[0][0], 0.9)]
        return [(item_id, min(score + 0.2, 1.0)) for item_id, _text, score in candidates][::-1][
            :top_k
        ]

    @property
    def last_diagnostics(self):
        return self._diagnostics

    @property
    def last_model_identity(self):
        return self._identity


def _provided(text, *, vector=None, dimension=3, identity="embedder:test@revision-1"):
    return {
        "vector": vector or [1.0, 0.0, 0.0],
        "dimension": dimension,
        "identity": identity,
        "material_sha256": material_sha256(text),
    }


def _items():
    return [
        {
            "id": "provided",
            "text": "already embedded",
            "base_score": 0.4,
            "embedding": _provided("already embedded"),
        },
        {
            "id": "missing",
            "text": "backend must embed this",
            "base_score": 0.7,
        },
    ]


def _client_result_payload(package, *, items=None):
    return {
        "contract_version": CLIENT_LOCAL_RESULT_CONTRACT,
        "package_hash": package.package_hash,
        "model_identity": package.model_identity,
        "items": (
            [
                {"id": "missing", "score": 0.9},
                {"id": "provided", "score": 0.8},
            ]
            if items is None
            else items
        ),
    }


def test_prepare_reuses_valid_vector_and_batches_only_missing_material():
    payloads = _items()
    original = copy.deepcopy(payloads)
    embedder = _Embedder()
    service = BackendInferenceService(embedder=embedder)

    result = service.prepare(payloads)

    assert result.contract_version == PREPARED_INPUT_CONTRACT
    assert service.embedding_identity == "embedder:test@revision-1"
    assert service.embedding_dimension == 3
    assert result.embedding_dimension == 3
    assert result.provided_count == result.generated_count == 1
    assert embedder.calls == [["backend must embed this"]]
    assert result.items[0].embedding == (1.0, 0.0, 0.0)
    assert result.items[0].embedding_provenance == "frontend-supplied"
    assert result.items[1].embedding == (0.0, 1.0, 0.0)
    assert result.items[1].embedding_provenance == "backend-generated"
    assert all(item.reusable_for_index is False for item in result.items)
    assert payloads == original


@pytest.mark.parametrize("field", ["api_key", "base_url", "path", "provider", "model"])
def test_frontend_cannot_override_backend_provider_configuration(field):
    service = BackendInferenceService(embedder=_Embedder())
    payload = {"id": "one", "text": "content", field: "forbidden"}

    with pytest.raises(ValueError, match="^input_field_not_allowed$"):
        service.prepare([payload])


def test_nested_embedding_rejects_unknown_or_secret_fields():
    service = BackendInferenceService(embedder=_Embedder())
    embedding = _provided("content")
    embedding["api_key"] = "forbidden"

    with pytest.raises(ValueError, match="^input_field_not_allowed$"):
        service.prepare([{"id": "one", "text": "content", "embedding": embedding}])


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"dimension": 2, "vector": [1.0, 0.0]}, "provided_embedding_dimension_mismatch"),
        ({"identity": "embedder:stale"}, "provided_embedding_identity_mismatch"),
        (
            {"material_sha256": "sha256:" + ("0" * 64)},
            "provided_embedding_material_hash_mismatch",
        ),
        ({"vector": [0.0, 0.0, 0.0]}, "provided_embedding_zero_vector"),
        ({"vector": [float("nan"), 0.0, 1.0]}, "provided_embedding_value_invalid"),
    ],
)
def test_prepare_rejects_stale_or_invalid_provided_embedding(update, reason):
    embedding = _provided("content")
    embedding.update(update)
    service = BackendInferenceService(embedder=_Embedder())

    with pytest.raises(ValueError, match=f"^{reason}$"):
        service.prepare([{"id": "one", "text": "content", "embedding": embedding}])


def test_prepare_rejects_duplicate_ids_and_backend_zero_vector():
    duplicate = [{"id": "same", "text": "one"}, {"id": "same", "text": "two"}]
    service = BackendInferenceService(embedder=_Embedder())
    with pytest.raises(ValueError, match="^input_id_duplicate$"):
        service.prepare(duplicate)

    zero_service = BackendInferenceService(embedder=_Embedder(vectors=[[0.0, 0.0, 0.0]]))
    with pytest.raises(ValueError, match="^backend_embedding_zero_vector$"):
        zero_service.prepare([{"id": "one", "text": "content"}])


def test_rerank_is_immutable_and_binds_versions_and_hashes():
    service = BackendInferenceService(
        embedder=_Embedder(),
        reranker_factory=_Reranker,
        provider_policy_revision="policy-7",
    )
    prepared = service.prepare(_items())

    result = service.rerank(
        query="find the backend item",
        prepared=prepared,
        project_id="project-a",
        request_id="request-1",
        idempotency_key="device-command-99",
        candidate_set_version="snapshot-3",
        top_k=2,
    )

    assert result.contract_version == RERANK_CONTRACT
    assert result.project_id == "project-a"
    assert result.candidate_set_version == "snapshot-3"
    assert result.candidate_set_hash.startswith("sha256:")
    assert result.query_hash == material_sha256("find the backend item")
    assert result.idempotency_key_hash.startswith("sha256:")
    assert result.input_hash.startswith("sha256:")
    assert result.model_identity == "rerank:test@revision-1"
    assert result.top_k == 2
    assert [item.item_id for item in result.items] == ["missing", "provided"]
    assert [item.item_id for item in prepared.items] == ["provided", "missing"]
    with pytest.raises(TypeError):
        result.diagnostics["status"] = "tampered"
    with pytest.raises(TypeError):
        result.diagnostics["usage"]["input_tokens"] = 999
    with pytest.raises(TypeError):
        result.diagnostics["attempts"][0]["provider"] = "tampered"

    changed = service.rerank(
        query="find the backend item",
        prepared=prepared,
        project_id="project-a",
        request_id="request-2",
        idempotency_key="device-command-100",
        candidate_set_version="snapshot-4",
        top_k=2,
    )
    assert changed.candidate_set_hash != result.candidate_set_hash
    assert changed.input_hash != result.input_hash


def test_request_binding_supports_project_scoped_durable_idempotency():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())

    def bind(*, project_id="project-a", request_id="request-1", query="query"):
        return service.bind_rerank_request(
            query=query,
            prepared=prepared,
            project_id=project_id,
            request_id=request_id,
            idempotency_key="device-command-1",
            candidate_set_version="snapshot-1",
            top_k=2,
        )

    first = bind()
    retried_from_another_device = bind(request_id="request-2")
    conflicting_input = bind(request_id="request-3", query="changed query")
    another_project = bind(project_id="project-b", request_id="request-4")

    assert first.contract_version == RERANK_REQUEST_CONTRACT
    assert first.input_hash == retried_from_another_device.input_hash
    assert first.idempotency_key_hash == retried_from_another_device.idempotency_key_hash
    assert first.input_hash != conflicting_input.input_hash
    assert first.idempotency_key_hash == conflicting_input.idempotency_key_hash
    assert first.idempotency_key_hash != another_project.idempotency_key_hash
    assert first.input_hash != another_project.input_hash


def test_request_binding_includes_exact_optional_embedding_material():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    first = service.prepare([{"id": "one", "text": "content", "embedding": _provided("content")}])
    second = service.prepare(
        [
            {
                "id": "one",
                "text": "content",
                "embedding": _provided("content", vector=[0.0, 1.0, 0.0]),
            }
        ]
    )

    def bind(prepared):
        return service.bind_rerank_request(
            query="query",
            prepared=prepared,
            project_id="project-a",
            request_id="request",
            idempotency_key="key",
            candidate_set_version="snapshot-1",
        )

    assert bind(first).candidate_set_hash != bind(second).candidate_set_hash


@pytest.mark.asyncio
async def test_async_wrappers_keep_blocking_provider_work_off_the_caller_contract():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)

    prepared = await service.aprepare(_items())
    result = await service.arerank(
        query="query",
        prepared=prepared,
        project_id="project-a",
        request_id="request",
        idempotency_key="key",
        candidate_set_version="snapshot-1",
    )

    assert result.project_id == "project-a"
    assert len(result.items) == 2


def test_client_local_export_contains_exact_text_receipts_but_no_vectors_or_provider_config():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())

    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    assert package.contract_version == CLIENT_LOCAL_RERANK_CONTRACT
    assert package.project_id == "project-a"
    assert package.query == "query"
    assert package.query_hash == material_sha256("query")
    assert package.embedding_identity == prepared.embedding_identity
    assert package.embedding_dimension == prepared.embedding_dimension
    assert package.model_identity == "client-local:test@revision-1"
    assert [candidate.text for candidate in package.candidates] == [
        "already embedded",
        "backend must embed this",
    ]
    assert all(candidate.embedding_sha256.startswith("sha256:") for candidate in package.candidates)
    serialized = asdict(package)
    assert all("embedding" not in candidate for candidate in serialized["candidates"])
    assert "provider_policy_revision" not in serialized
    assert "api_key" not in serialized


def test_client_local_result_is_accepted_only_for_current_authenticated_request():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    result = service.accept_client_local_rerank(
        package=package,
        payload=_client_result_payload(package),
        authenticated_project_id="project-a",
        current_request_id="request-1",
        current_query="query",
        current_candidate_set_version="snapshot-1",
        current_prepared=prepared,
    )

    assert result.contract_version == CLIENT_LOCAL_RESULT_CONTRACT
    assert result.project_id == "project-a"
    assert result.request_id == "request-1"
    assert result.package_hash == package.package_hash
    assert result.reported_model_identity == "client-local:test@revision-1"
    assert [(item.item_id, item.rank) for item in result.items] == [
        ("missing", 1),
        ("provided", 2),
    ]
    assert not hasattr(result, "reusable_for_index")


def test_durable_authoritative_package_supports_restart_rerank_without_vectors():
    service = BackendInferenceService(
        embedder=_Embedder(),
        reranker_factory=_Reranker,
        provider_policy_revision="policy-7",
    )
    prepared = service.prepare(_items())
    binding = service.bind_rerank_request(
        query="query",
        prepared=prepared,
        project_id="project-a",
        request_id="request-1",
        idempotency_key="key-1",
        candidate_set_version="snapshot-1",
    )
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    result = service.rerank_authoritative_package(package=package, binding=binding)

    assert service.provider_policy_revision == "policy-7"
    assert result.request_id == "request-1"
    assert result.input_hash == binding.input_hash
    assert [item.item_id for item in result.items] == ["missing", "provided"]
    with pytest.raises(ValueError, match="^rerank_binding_package_mismatch$"):
        service.rerank_authoritative_package(
            package=package,
            binding=replace(binding, request_id="different-request"),
        )
    with pytest.raises(ValueError, match="^rerank_provider_policy_revision_mismatch$"):
        service.rerank_authoritative_package(
            package=package,
            binding=replace(binding, provider_policy_revision="policy-8"),
        )


def test_durable_client_result_uses_only_server_loaded_package_authority():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    result = service.accept_client_local_rerank_authoritative(
        package=package,
        payload=_client_result_payload(package),
        authenticated_project_id="project-a",
        current_request_id="request-1",
    )

    assert result.package_hash == package.package_hash
    with pytest.raises(ValueError, match="^client_local_package_hash_mismatch$"):
        service.accept_client_local_rerank_authoritative(
            package=replace(package, package_hash="sha256:" + "0" * 64),
            payload=_client_result_payload(package),
            authenticated_project_id="project-a",
            current_request_id="request-1",
        )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"authenticated_project_id": "project-b"}, "client_local_project_mismatch"),
        ({"current_request_id": "request-2"}, "client_local_request_mismatch"),
        ({"current_query": "changed query"}, "client_local_query_mismatch"),
        ({"current_top_k": 1}, "client_local_top_k_mismatch"),
        (
            {"current_candidate_set_version": "snapshot-2"},
            "client_local_candidate_set_version_mismatch",
        ),
    ],
)
def test_client_local_result_rejects_cross_project_stale_or_other_device_state(overrides, reason):
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )
    arguments = {
        "package": package,
        "payload": _client_result_payload(package),
        "authenticated_project_id": "project-a",
        "current_request_id": "request-1",
        "current_query": "query",
        "current_candidate_set_version": "snapshot-1",
        "current_prepared": prepared,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=f"^{reason}$"):
        service.accept_client_local_rerank(**arguments)


def test_client_local_result_rejects_package_tampering_and_candidate_drift():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    with pytest.raises(ValueError, match="^client_local_query_hash_mismatch$"):
        service.accept_client_local_rerank(
            package=replace(package, query="tampered query"),
            payload=_client_result_payload(package),
            authenticated_project_id="project-a",
            current_request_id="request-1",
            current_query="query",
            current_candidate_set_version="snapshot-1",
            current_prepared=prepared,
        )

    changed_items = _items()
    changed_items[1]["base_score"] = 0.1
    changed_prepared = service.prepare(changed_items)
    with pytest.raises(ValueError, match="^client_local_candidate_set_mismatch$"):
        service.accept_client_local_rerank(
            package=package,
            payload=_client_result_payload(package),
            authenticated_project_id="project-a",
            current_request_id="request-1",
            current_query="query",
            current_candidate_set_version="snapshot-1",
            current_prepared=changed_prepared,
        )


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        ([{"id": "missing", "score": 0.9}], "client_local_result_items_invalid"),
        (
            [{"id": "unknown", "score": 0.9}, {"id": "provided", "score": 0.8}],
            "client_local_result_id_invalid",
        ),
        (
            [{"id": "missing", "score": 0.8}, {"id": "provided", "score": 0.9}],
            "client_local_result_order_invalid",
        ),
        (
            [{"id": "missing", "score": float("nan")}, {"id": "provided", "score": 0.8}],
            "client_local_result_score_invalid",
        ),
    ],
)
def test_client_local_result_rejects_incomplete_unknown_unordered_or_nonfinite_scores(
    items, reason
):
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())
    package = service.export_client_local_rerank(
        query="query",
        prepared=prepared,
        authenticated_project_id="project-a",
        request_id="request-1",
        candidate_set_version="snapshot-1",
        model_identity="client-local:test@revision-1",
    )

    with pytest.raises(ValueError, match=f"^{reason}$"):
        service.accept_client_local_rerank(
            package=package,
            payload=_client_result_payload(package, items=items),
            authenticated_project_id="project-a",
            current_request_id="request-1",
            current_query="query",
            current_candidate_set_version="snapshot-1",
            current_prepared=prepared,
        )


def test_identical_client_local_material_isolated_by_request_id_for_multi_device_use():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())

    def export(request_id):
        return service.export_client_local_rerank(
            query="query",
            prepared=prepared,
            authenticated_project_id="project-a",
            request_id=request_id,
            candidate_set_version="snapshot-1",
            model_identity="client-local:test@revision-1",
        )

    first = export("request-device-a")
    second = export("request-device-b")

    assert first.candidate_set_hash == second.candidate_set_hash
    assert first.package_hash != second.package_hash


def test_concurrent_reranks_keep_request_diagnostics_isolated():
    service = BackendInferenceService(embedder=_Embedder(), reranker_factory=_Reranker)
    prepared = service.prepare(_items())

    def run(index):
        query = f"query {index}"
        result = service.rerank(
            query=query,
            prepared=prepared,
            project_id="project-a",
            request_id=f"request-{index}",
            idempotency_key=f"key-{index}",
            candidate_set_version="snapshot-1",
        )
        return result.query_hash, result.diagnostics["query_hash"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        pairs = list(executor.map(run, range(32)))

    assert all(expected == observed for expected, observed in pairs)
    assert len({expected for expected, _observed in pairs}) == 32


@pytest.mark.parametrize("invalid", ["duplicate", "short"])
def test_rerank_rejects_duplicate_or_incomplete_provider_results(invalid):
    service = BackendInferenceService(
        embedder=_Embedder(),
        reranker_factory=lambda: _Reranker(invalid=invalid),
    )
    prepared = service.prepare(_items())

    with pytest.raises(RuntimeError, match="^rerank_response_(invalid|incomplete)$"):
        service.rerank(
            query="query",
            prepared=prepared,
            project_id="project-a",
            request_id="request",
            idempotency_key="key",
            candidate_set_version="snapshot",
        )
