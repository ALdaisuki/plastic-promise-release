from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from plastic_promise.core.embedder import (
    CachedEmbedder,
    FallbackEmbedder,
    LocalSentenceEmbedder,
    OllamaEmbedder,
    OpenAICompatibleEmbedder,
    OpenAIEmbedder,
    StructureAwareEmbedder,
    get_embedder,
    reset_embedder,
)
from plastic_promise.core.memory_index import effective_embedding_model_name


class _RecordingHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post_json(self, path, payload, *, deadline=None):
        self.calls.append((path, payload, deadline))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            payload=response,
            attempts=1,
            latency_ms=2.5,
            request_id="request-test",
        )

    def close(self):
        self.closed = True


def _response(*vectors, usage=None):
    return {
        "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
        "usage": usage or {"prompt_tokens": len(vectors) * 3, "total_tokens": len(vectors) * 3},
    }


def test_openai_compatible_provider_honors_contract_batches_and_reuses_client(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "3")
    client = _RecordingHTTPClient(
        [
            _response([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
            _response([0.0, 0.0, 1.0]),
        ]
    )
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model="text-embedding-v4",
        model_revision="2026-07-23",
        dim=3,
        batch_size=2,
        client=client,
    )

    vectors = embedder.embed_batch(["first", "second", "third"])

    assert vectors == [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    assert [call[0] for call in client.calls] == ["/embeddings", "/embeddings"]
    assert client.calls[0][1] == {
        "model": "text-embedding-v4",
        "input": ["first", "second"],
        "dimensions": 3,
    }
    assert client.calls[1][1]["input"] == ["third"]
    assert client.calls[0][2] == client.calls[1][2]
    assert isinstance(client.calls[0][2], float)
    assert embedder.dim == 3
    assert embedder.model_name == "text-embedding-v4"
    assert "revision=2026-07-23" in embedder.index_model_name
    assert "dim=3" in embedder.index_model_name
    assert embedder.stats["requests"] == 2
    assert embedder.stats["input_tokens"] == 9

    embedder.close()
    assert client.closed is True


def test_openai_compatible_provider_can_omit_dimensions_but_still_validates_schema(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "3")
    client = _RecordingHTTPClient([_response([1.0, 0.0, 0.0])])
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model="BAAI/bge-m3",
        model_revision="BAAI/bge-m3",
        dim=3,
        send_dimensions=False,
        client=client,
    )

    assert embedder.embed("native dimension") == [1.0, 0.0, 0.0]
    assert client.calls[0][1] == {
        "model": "BAAI/bge-m3",
        "input": ["native dimension"],
    }
    assert embedder.stats["dimensions_parameter"] == "native"
    assert "|dimensions=native" in embedder.index_model_name


def test_native_dimension_mode_is_bound_to_environment_identity(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "1024")
    monkeypatch.setenv("EMBEDDER_SEND_DIMENSIONS", "0")

    environment_identity = effective_embedding_model_name()
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model="BAAI/bge-m3",
        model_revision="BAAI/bge-m3",
        dim=1024,
        client=_RecordingHTTPClient([]),
    )

    assert environment_identity == embedder.index_model_name
    assert environment_identity.endswith("|dimensions=native")


@pytest.mark.parametrize("value", ["", "sometimes", "2"])
def test_cloud_dimensions_mode_rejects_ambiguous_environment(monkeypatch, value):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("EMBEDDER_SEND_DIMENSIONS", value)

    with pytest.raises(ValueError, match="^embedding_send_dimensions_invalid$"):
        OpenAICompatibleEmbedder(
            api_key="not-a-real-key",
            base_url="https://embedding.example.test/v1",
            client=_RecordingHTTPClient([]),
        )
    with pytest.raises(ValueError, match="^embedding_send_dimensions_invalid$"):
        effective_embedding_model_name()


def test_openai_compatible_provider_restores_response_index_order(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    client = _RecordingHTTPClient(
        [
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }
        ]
    )
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        client=client,
    )

    assert embedder.embed_batch(["left", "right"]) == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    ("response_model", "reason"),
    [
        ("", "embedding_response_model_invalid"),
        (123, "embedding_response_model_invalid"),
        ("different-model", "embedding_response_model_mismatch"),
    ],
)
def test_openai_compatible_provider_binds_optional_response_model(
    monkeypatch, response_model, reason
):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    payload = _response([1.0, 0.0])
    payload["model"] = response_model
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model="configured-model",
        dim=2,
        client=_RecordingHTTPClient([payload]),
    )

    with pytest.raises(RuntimeError, match=f"^{reason}$"):
        embedder.embed("model-bound input")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"data": []}, "embedding_response_count_mismatch"),
        (_response([1.0]), "embedding_response_dimension_mismatch"),
        (_response([math.nan, 1.0]), "embedding_response_value_invalid"),
        (_response([math.inf, 1.0]), "embedding_response_value_invalid"),
        (_response([10**1000, 1.0]), "embedding_response_value_invalid"),
        (_response([0.0, 0.0]), "embedding_response_zero_vector"),
        (
            {"data": [{"index": 1, "embedding": [1.0, 0.0]}]},
            "embedding_response_index_invalid",
        ),
    ],
)
def test_openai_compatible_provider_rejects_invalid_vectors(monkeypatch, payload, reason):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        client=_RecordingHTTPClient([payload]),
    )

    with pytest.raises(RuntimeError, match=f"^{reason}$"):
        embedder.embed_batch(["private input"])


def test_openai_compatible_provider_fails_closed_on_missing_key_and_schema_mismatch(monkeypatch):
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("PP_EMBEDDING_DIM", "1024")

    with pytest.raises(ValueError, match="embedding_api_key_missing"):
        OpenAICompatibleEmbedder(base_url="https://embedding.example.test/v1")

    with pytest.raises(ValueError, match="embedding_dimension_schema_mismatch"):
        OpenAICompatibleEmbedder(
            api_key="not-a-real-key",
            base_url="https://embedding.example.test/v1",
            dim=1536,
        )


def test_openai_compatible_provider_rejects_documentation_endpoint(monkeypatch):
    from plastic_promise.core.provider_http import ProviderHTTPError

    monkeypatch.setenv("PP_EMBEDDING_DIM", "1024")

    with pytest.raises(ProviderHTTPError, match="^provider_http_documentation_base_url$"):
        OpenAICompatibleEmbedder(
            api_key="not-a-real-key",
            base_url="https://wiki.syuan.org",
        )


def test_generic_provider_never_reuses_supplier_specific_api_keys(monkeypatch):
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-for-another-provider")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key-for-another-provider")

    with pytest.raises(ValueError, match="embedding_api_key_missing"):
        OpenAICompatibleEmbedder(base_url="https://embedding.example.test/v1")


def test_legacy_openai_provider_is_pinned_to_official_endpoint(monkeypatch):
    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-test-key")
    monkeypatch.setenv("EMBEDDER_API_KEY", "generic-key-must-not-win")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://unrelated-provider.example/v1")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "1536")
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")

    try:
        selected = get_embedder(fallback_on_error=False)
        assert isinstance(selected, OpenAIEmbedder)
        assert selected._base_url == "https://api.openai.com/v1"
        assert selected._key == "legacy-openai-test-key"
        assert "provider=openai" in selected.index_model_name
    finally:
        reset_embedder()


def test_legacy_openai_provider_rejects_constructor_endpoint_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-test-key")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "1536")

    with pytest.raises(ValueError, match="^openai_base_url_must_be_official$"):
        OpenAIEmbedder(
            base_url="https://attacker.example/v1",
            client=_RecordingHTTPClient([]),
        )

    accepted = OpenAIEmbedder(
        base_url="https://api.openai.com:443/v1/",
        client=_RecordingHTTPClient([]),
    )
    try:
        assert accepted._base_url == "https://api.openai.com/v1"
    finally:
        accepted.close()


def test_endpoint_is_bound_to_runtime_and_environment_index_identity(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    client = _RecordingHTTPClient([])
    first = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://first.example.test/v1",
        dim=2,
        client=client,
    )
    second = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://second.example.test/v1",
        dim=2,
        client=client,
    )

    assert first.index_model_name != second.index_model_name
    assert "endpoint_sha256=" in first.index_model_name

    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", "revision-a")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://first.example.test/v1")
    environment_first = effective_embedding_model_name()
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://second.example.test/v1")
    environment_second = effective_embedding_model_name()

    assert environment_first != environment_second
    assert "endpoint_sha256=" in environment_first


def test_cloud_environment_identity_prefers_provider_model_over_legacy_model(monkeypatch):
    """An ambient legacy model must not detach cloud identity from the factory."""

    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBED_MODEL", "legacy-model-from-shell")
    monkeypatch.setenv("EMBEDDER_MODEL", "text-embedding-v4")
    monkeypatch.setenv("EMBEDDER_MODEL_REVISION", "revision-a")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")

    identity = effective_embedding_model_name()

    assert identity.startswith("text-embedding-v4|provider=openai-compatible|")
    assert "legacy-model-from-shell" not in identity


def test_cached_embedder_namespaces_hash_and_deduplicates_batch(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_COST_PER_MILLION_TOKENS", "2")
    client = _RecordingHTTPClient([_response([1.0, 0.0], [0.0, 1.0])])
    delegate = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model="text-embedding-v4",
        model_revision="revision-a",
        dim=2,
        client=client,
    )
    embedder = CachedEmbedder(delegate, max_size=8, ttl_seconds=60)

    vectors = embedder.embed_batch(["same", "other", "same"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
    assert client.calls[0][1]["input"] == ["same", "other"]
    assert embedder.embed("same") == [1.0, 0.0]
    assert len(client.calls) == 1
    assert embedder.stats["provider"] == "openai-compatible"
    assert embedder.stats["revision"] == "revision-a"
    assert embedder.stats["requests"] == 1
    assert embedder.stats["input_tokens"] == 6
    assert embedder.stats["estimated_cost"] == 0.000012
    assert embedder.stats["cost_currency"] == "USD"
    assert embedder.stats["estimated_cost_usd"] == 0.000012
    assert embedder.stats["hits"] == 1


def test_cloud_embedder_reports_cny_without_claiming_usd(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_COST_PER_MILLION_TOKENS", "0.03")
    monkeypatch.setenv("EMBEDDER_COST_CURRENCY", "CNY")
    monkeypatch.setenv("EMBEDDER_PRICING_REVISION", "syuan-pricing-2026-07-24")
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        client=_RecordingHTTPClient([_response([1.0, 0.0])]),
    )

    embedder.embed("text")

    assert embedder.stats["estimated_cost"] == 0.00000009
    assert embedder.stats["cost_currency"] == "CNY"
    assert embedder.stats["estimated_cost_usd"] is None
    assert embedder.stats["pricing_revision"] == "syuan-pricing-2026-07-24"
    assert embedder.stats["cost_basis"] == "input_tokens"


def test_cloud_embedder_cost_is_unknown_without_token_evidence(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_COST_PER_MILLION_TOKENS", "0.03")
    response = _response([1.0, 0.0])
    response["usage"] = {}
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        client=_RecordingHTTPClient([response]),
    )

    embedder.embed("text")

    assert embedder.stats["estimated_cost"] is None
    assert embedder.stats["estimated_cost_usd"] is None
    assert embedder.stats["cost_basis"] == "unknown"
    assert embedder.stats["cost_limitation"] == "token-count-unavailable"


def test_cloud_embedder_rejects_unknown_cost_currency(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("EMBEDDER_COST_CURRENCY", "EUR")

    with pytest.raises(ValueError, match="embedding_cost_currency_invalid"):
        OpenAICompatibleEmbedder(
            api_key="not-a-real-key",
            base_url="https://embedding.example.test/v1",
            dim=2,
            client=_RecordingHTTPClient([]),
        )


def test_structure_and_cache_wrappers_preserve_cloud_usage_stats(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "structure-v1")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENRICHMENT", "off")
    client = _RecordingHTTPClient([_response([1.0, 0.0])])
    delegate = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        model_revision="revision-b",
        dim=2,
        client=client,
    )
    embedder = CachedEmbedder(StructureAwareEmbedder(delegate), max_size=8, ttl_seconds=60)

    assert embedder.embed("short") == [1.0, 0.0]
    assert embedder.stats["provider"] == "openai-compatible"
    assert embedder.stats["revision"] == "revision-b"
    assert embedder.stats["requests"] == 1
    assert embedder.stats["input_tokens"] == 3


def test_openai_compatible_provider_enforces_utf8_and_total_input_limits(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    client = _RecordingHTTPClient([])
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        max_input_bytes=5,
        max_total_input_bytes=8,
        client=client,
    )

    with pytest.raises(ValueError, match="^embedding_input_too_large$"):
        embedder.embed("你好")
    with pytest.raises(ValueError, match="^embedding_total_input_too_large$"):
        embedder.embed_batch(["12345", "6789"])

    assert client.calls == []


def test_openai_compatible_provider_clamps_batch_size_to_hard_limit(monkeypatch):
    import plastic_promise.core.embedder as embedder_module

    monkeypatch.setenv("PP_EMBEDDING_DIM", "1")
    hard_limit = embedder_module._HARD_MAX_EMBEDDING_BATCH_SIZE
    client = _RecordingHTTPClient(
        [
            _response(*([[1.0]] * hard_limit)),
            _response([1.0]),
        ]
    )
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=1,
        batch_size=hard_limit * 10,
        client=client,
    )

    vectors = embedder.embed_batch(["x"] * (hard_limit + 1))

    assert len(vectors) == hard_limit + 1
    assert [len(call[1]["input"]) for call in client.calls] == [hard_limit, 1]


def test_openai_compatible_provider_rejects_result_after_shared_deadline(monkeypatch):
    from plastic_promise.core.provider_http import ProviderHTTPError

    monkeypatch.setenv("PP_EMBEDDING_DIM", "2")
    clock = [10.0]

    class _SlowClient(_RecordingHTTPClient):
        def post_json(self, path, payload, *, deadline=None):
            result = super().post_json(path, payload, deadline=deadline)
            clock[0] = float(deadline) + 0.1
            return result

    client = _SlowClient([_response([1.0, 0.0])])
    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=2,
        total_timeout_seconds=5.0,
        client=client,
        clock=lambda: clock[0],
    )

    with pytest.raises(ProviderHTTPError, match="^provider_http_deadline_exceeded$"):
        embedder.embed("bounded input")

    assert client.calls[0][2] == pytest.approx(15.0)


def test_factory_cloud_failure_does_not_probe_ollama(monkeypatch):
    import plastic_promise.core.embedder as embedder_module

    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        embedder_module,
        "OllamaEmbedder",
        lambda *args, **kwargs: pytest.fail("cloud fallback must not probe Ollama"),
    )

    selected = embedder_module.get_embedder(fallback_on_error=True)

    assert selected.model_name == "fallback-zero"
    assert selected.dim == 7
    reset_embedder()


def test_factory_normalizes_fallback_provider_and_uses_schema_dimension(monkeypatch):
    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "  FaLlBaCk  ")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")

    try:
        selected = get_embedder(fallback_on_error=False)
        assert isinstance(selected, FallbackEmbedder)
        assert selected.dim == 7
    finally:
        reset_embedder()


def test_fallback_embedder_default_uses_schema_dimension(monkeypatch):
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")

    selected = FallbackEmbedder()

    assert selected.dim == 7
    assert len(selected.embed("schema-bound input")) == 7
    assert FallbackEmbedder(dim=3).dim == 3


def test_fallback_custom_dimension_is_bound_to_embedder_identity(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")
    selected = FallbackEmbedder()

    assert selected.index_model_name == "fallback-zero|dim=7"
    assert effective_embedding_model_name(selected) == "fallback-zero|dim=7"

    monkeypatch.setenv("PP_EMBEDDING_DIM", "1024")
    assert FallbackEmbedder().index_model_name == "fallback-zero"


def test_fallback_environment_identity_binds_custom_dimension(monkeypatch):
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    monkeypatch.setenv("EMBEDDER_PROVIDER", "fallback")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")

    assert effective_embedding_model_name() == "fallback-zero|dim=7"

    monkeypatch.setenv("PP_EMBEDDING_DIM", "1024")
    assert effective_embedding_model_name() == "fallback-zero"


def test_factory_rejects_unknown_provider_without_implicit_probe(monkeypatch):
    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", " local-typo ")
    monkeypatch.setattr(
        "plastic_promise.core.embedder.OllamaEmbedder",
        lambda *args, **kwargs: pytest.fail("unknown provider must not probe Ollama"),
    )

    try:
        with pytest.raises(ValueError, match="^embedding_provider_invalid$"):
            get_embedder(fallback_on_error=True)
    finally:
        reset_embedder()


def test_environment_identity_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "local-typo")

    with pytest.raises(ValueError, match="^embedding_provider_invalid$"):
        effective_embedding_model_name()


def test_environment_identity_requires_cloud_endpoint(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.delenv("EMBEDDER_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="^embedding_base_url_missing$"):
        effective_embedding_model_name()


def test_cloud_dimension_environment_is_shared_with_schema_contract(monkeypatch):
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBEDDER_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("EMBEDDER_DIMENSION", "7")
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)

    embedder = OpenAICompatibleEmbedder(
        api_key="not-a-real-key",
        base_url="https://embedding.example.test/v1",
        dim=7,
        client=_RecordingHTTPClient([]),
    )

    assert embedder.dim == 7
    assert "dim=7" in effective_embedding_model_name()


def test_local_ollama_direct_constructors_honor_legacy_dimension_alias(monkeypatch):
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)
    monkeypatch.setenv("EMBEDDER_DIMENSION", "2")

    ollama = OllamaEmbedder(host="http://127.0.0.1:11434")
    local = LocalSentenceEmbedder()

    assert ollama.dim == 2
    assert local.dim == 2


def test_factory_local_provider_is_explicit_and_disables_ollama_recovery(monkeypatch):
    import plastic_promise.core.embedder as embedder_module

    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "  LOCAL ")
    monkeypatch.setenv("PP_EMBEDDING_DIM", "7")
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")
    real_local = embedder_module.LocalSentenceEmbedder
    captured: dict[str, object] = {}

    def construct_local(*args, **kwargs):
        captured.update(kwargs)
        return real_local(*args, **kwargs)

    monkeypatch.setattr(embedder_module, "LocalSentenceEmbedder", construct_local)
    monkeypatch.setattr(
        embedder_module,
        "OllamaEmbedder",
        lambda *args, **kwargs: pytest.fail("local provider must not construct Ollama"),
    )

    try:
        selected = embedder_module.get_embedder(fallback_on_error=False)
        assert isinstance(selected, real_local)
        assert selected.dim == 7
        assert captured == {"expected_dim": 7, "allow_ollama_recovery": False}
    finally:
        reset_embedder()


def test_factory_openai_failure_keeps_legacy_default_dimension(monkeypatch):
    reset_embedder()
    monkeypatch.setenv("EMBEDDER_PROVIDER", "openai")
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDER_CACHE_SIZE", "0")

    try:
        selected = get_embedder(fallback_on_error=True)
        assert isinstance(selected, FallbackEmbedder)
        assert selected.dim == OpenAIEmbedder._DEFAULT_DIM
    finally:
        reset_embedder()


class _EmbeddingResponse:
    def __init__(self, vector):
        self._vector = vector

    def raise_for_status(self):
        return None

    def json(self):
        return {"embedding": self._vector}


def test_ollama_runtime_vector_dimension_is_validated(monkeypatch):
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)
    monkeypatch.setattr(
        "plastic_promise.core.embedder.requests.post",
        lambda *args, **kwargs: _EmbeddingResponse([1.0]),
    )

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434", expected_dim=2)

    with pytest.raises(RuntimeError, match="^embedding_response_dimension_mismatch$"):
        embedder.embed("dimension-bound input")


def test_ollama_strict_runtime_path_rejects_zero_vector(monkeypatch):
    monkeypatch.setattr(
        "plastic_promise.core.embedder.requests.post",
        lambda *args, **kwargs: _EmbeddingResponse([0.0, 0.0]),
    )

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434", expected_dim=2)

    with pytest.raises(RuntimeError, match="^embedding_response_zero_vector$"):
        embedder.embed("zero-vector input")


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf")])
def test_ollama_runtime_vector_values_must_be_numeric_and_finite(monkeypatch, value):
    monkeypatch.setattr(
        "plastic_promise.core.embedder.requests.post",
        lambda *args, **kwargs: _EmbeddingResponse([value]),
    )

    embedder = OllamaEmbedder(host="http://127.0.0.1:11434", expected_dim=1)

    with pytest.raises(RuntimeError, match="^embedding_response_value_invalid$"):
        embedder.embed("value-bound input")


class _EncodedValues:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


class _LocalModel:
    def __init__(self, values):
        self._values = values

    def encode(self, texts, **kwargs):
        if isinstance(texts, list):
            return _EncodedValues([self._values for _ in texts])
        return _EncodedValues(self._values)


def test_local_runtime_vector_dimension_is_validated(monkeypatch):
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)
    embedder = LocalSentenceEmbedder(expected_dim=2)
    embedder._model = _LocalModel([1.0])

    with pytest.raises(RuntimeError, match="^embedding_response_dimension_mismatch$"):
        embedder.embed("dimension-bound input")


def test_local_runtime_fails_cleanly_when_optional_dependency_is_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def import_without_sentence_transformers(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ModuleNotFoundError(name="sentence_transformers")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sentence_transformers)

    with pytest.raises(RuntimeError, match="^local_embedding_dependency_missing$"):
        LocalSentenceEmbedder().embed("local model input")


def test_local_runtime_recovery_keeps_schema_dimension_bound(monkeypatch):
    monkeypatch.delenv("PP_EMBEDDING_DIM", raising=False)
    monkeypatch.setattr(
        "plastic_promise.core.embedder.requests.post",
        lambda *args, **kwargs: _EmbeddingResponse([1.0]),
    )
    local = LocalSentenceEmbedder()
    local._model = _LocalModel([0.0, 0.0])
    cached = CachedEmbedder(local, max_size=8)

    assert cached.embed("recovery-bound input") == [0.0, 0.0]
    assert cached._delegate is local
