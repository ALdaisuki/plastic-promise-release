from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import plastic_promise.core.reranker as reranker_module
from plastic_promise.core.reranker import MultiProviderReranker, _cache_key


@dataclass
class _Item:
    id: str
    content: str
    relevance: float


class _FakeClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict, float | None]] = []

    def post_json(self, path: str, payload: dict, *, deadline: float | None = None):
        self.calls.append((path, payload, deadline))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def _isolated_cloud_config(monkeypatch):
    reranker_module._rerank_cache.clear()
    reranker_module._reset_shared_cloud_clients()
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,original")
    monkeypatch.setenv("PP_RERANK_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("PP_RERANK_PATH", "/rerank")
    monkeypatch.setenv("PP_RERANK_API_KEY", "test-secret-key")
    monkeypatch.setenv("PP_RERANK_MODEL", "cloud-reranker-v1")
    monkeypatch.setenv("PP_RERANK_MODEL_REVISION", "revision-1")
    monkeypatch.setenv("PP_RERANK_TIMEOUT_SEC", "2.5")
    monkeypatch.setenv("PP_RERANK_TOTAL_TIMEOUT_SEC", "8")
    monkeypatch.setenv("PP_RERANK_MAX_RETRIES", "2")
    monkeypatch.setenv("PP_RERANK_MAX_CANDIDATES", "30")
    monkeypatch.setenv("PP_RERANK_MAX_DOCUMENT_CHARS", "4000")
    monkeypatch.delenv("PP_RERANK_COST_PER_MILLION_TOKENS", raising=False)
    monkeypatch.delenv("PP_RERANK_COST_CURRENCY", raising=False)
    monkeypatch.delenv("PP_RERANK_PRICING_REVISION", raising=False)
    monkeypatch.delenv("PP_RERANK_DISABLED", raising=False)
    yield
    reranker_module._rerank_cache.clear()
    reranker_module._reset_shared_cloud_clients()


def _items() -> list[_Item]:
    return [
        _Item("first", "first document", 0.8),
        _Item("second", "second document", 0.6),
        _Item("third", "third document", 0.4),
    ]


def _context_engine_with_one_result():
    from plastic_promise.core.context_engine import ContextEngine

    result = ("memory-1", 0.7, "useful context", "vector")
    engine = ContextEngine(use_sqlite=False)
    engine._ensure_heavy_init = lambda: None
    engine._activate_principles = lambda _task_type, _task_description, **_kwargs: []
    engine._inject_activated_to_graph = lambda _activated, _task_type: 0
    engine._graph_traversal = lambda _task_type: []
    engine._text_retrieval = lambda _query, trust_boost=1.0, domain_hint=None: []
    engine._vector_retrieval = lambda _vector, scope=None: []
    engine._fts_retrieval = lambda _query, scope="global": []
    engine._layered_fuse = lambda _graph, _text, _vector: [result]
    engine._apply_edge_feedback = lambda: None
    engine._apply_decay_awareness = lambda score, _memory, _current_time, _trust_boost: score
    engine._apply_mmr = lambda items, threshold=0.85, penalty=0.70: items
    engine._compute_divergent_quality = lambda items, _all_items: items
    engine._calc_freshness = lambda _item_id: "valid"
    engine._calc_decay_status = lambda _item_id, _memory: "healthy"
    engine._memories = {
        "memory-1": {
            "source": "user",
            "memory_type": "experience",
            "worth_success": 0,
            "worth_failure": 0,
        }
    }
    return engine


def _response(results, **metadata):
    return SimpleNamespace(
        payload={"results": results},
        attempts=metadata.get("attempts", 2),
        latency_ms=metadata.get("latency_ms", 12.5),
        request_id=metadata.get("request_id", "req-safe"),
        usage=metadata.get("usage", {"total_tokens": 17}),
    )


def test_cloud_rerank_sends_bounded_contract_and_reports_safe_diagnostics():
    client = _FakeClient(
        _response(
            [
                {"index": 0, "score": 0.1},
                {"index": 1, "score": 1.0},
                {"index": 2, "score": 0.2},
            ]
        )
    )
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("find the second", _items())

    assert len(client.calls) == 1
    path, payload, deadline = client.calls[0]
    assert path == "/rerank"
    assert payload == {
        "model": "cloud-reranker-v1",
        "query": "find the second",
        "documents": ["first document", "second document", "third document"],
        "top_n": 3,
    }
    assert isinstance(deadline, float)
    assert [item.id for item in result] == ["second", "first", "third"]
    assert reranker.last_provider == "cloud"
    assert reranker.last_diagnostics == {
        "provider": "cloud",
        "status": "success",
        "degraded": False,
        "reason": "",
        "attempts": 2,
        "latency_ms": 12.5,
        "request_id": f"sha256:{hashlib.sha256(b'req-safe').hexdigest()}",
        "usage": {"total_tokens": 17},
        "candidate_count": 3,
        "reranked_count": 3,
        "cache_hit": False,
    }
    assert "test-secret-key" not in repr(reranker.last_diagnostics)
    assert "req-safe" not in repr(reranker.last_diagnostics)


def test_cloud_rerank_reports_explicit_cny_cost(monkeypatch):
    monkeypatch.setenv("PP_RERANK_COST_PER_MILLION_TOKENS", "0.02")
    monkeypatch.setenv("PP_RERANK_COST_CURRENCY", "CNY")
    monkeypatch.setenv("PP_RERANK_PRICING_REVISION", "syuan-pricing-2026-07-24")
    reranker = MultiProviderReranker(
        http_client=_FakeClient(
            _response(
                [{"index": 0, "score": 1.0}],
                usage={"total_tokens": 17},
            )
        )
    )

    reranker.rerank("query", _items())

    assert reranker.last_diagnostics["usage"] == {
        "total_tokens": 17,
        "cost": 0.00000034,
        "cost_currency": "CNY",
        "cost_usd": None,
        "pricing_revision": "syuan-pricing-2026-07-24",
        "cost_basis": "total_tokens_single_blended_rate",
        "cost_limitation": "distinct-input-output-rates-not-modeled",
    }


def test_cloud_rerank_cost_sums_token_alias_groups_without_double_counting(monkeypatch):
    monkeypatch.setenv("PP_RERANK_COST_PER_MILLION_TOKENS", "0.02")
    monkeypatch.setenv("PP_RERANK_COST_CURRENCY", "CNY")
    monkeypatch.setenv("PP_RERANK_PRICING_REVISION", "pricing-v1")
    reranker = MultiProviderReranker(
        http_client=_FakeClient(
            _response(
                [{"index": 0, "score": 1.0}],
                usage={
                    "input_tokens": 11,
                    "prompt_tokens": 11,
                    "output_tokens": 6,
                    "completion_tokens": 6,
                },
            )
        )
    )

    reranker.rerank("query", _items())

    usage = reranker.last_diagnostics["usage"]
    assert usage["cost"] == 0.00000034
    assert usage["cost_basis"] == "input_output_tokens_single_blended_rate"
    assert usage["cost_limitation"] == "distinct-input-output-rates-not-modeled"


def test_cloud_rerank_cost_is_unknown_without_token_evidence(monkeypatch):
    monkeypatch.setenv("PP_RERANK_COST_PER_MILLION_TOKENS", "0.02")
    monkeypatch.setenv("PP_RERANK_COST_CURRENCY", "CNY")
    monkeypatch.setenv("PP_RERANK_PRICING_REVISION", "pricing-v1")
    reranker = MultiProviderReranker(
        http_client=_FakeClient(
            _response(
                [{"index": 0, "score": 1.0}],
                usage={},
            )
        )
    )

    reranker.rerank("query", _items())

    assert reranker.last_diagnostics["usage"] == {
        "cost": None,
        "cost_currency": "CNY",
        "cost_usd": None,
        "pricing_revision": "pricing-v1",
        "cost_basis": "unknown",
        "cost_limitation": "token-count-unavailable",
    }


def test_cloud_rerank_cost_is_unknown_with_only_one_token_component(monkeypatch):
    monkeypatch.setenv("PP_RERANK_COST_PER_MILLION_TOKENS", "0.02")
    monkeypatch.setenv("PP_RERANK_PRICING_REVISION", "pricing-v1")
    reranker = MultiProviderReranker(
        http_client=_FakeClient(
            _response(
                [{"index": 0, "score": 1.0}],
                usage={"input_tokens": 11},
            )
        )
    )

    reranker.rerank("query", _items())

    usage = reranker.last_diagnostics["usage"]
    assert usage["cost"] is None
    assert usage["cost_basis"] == "unknown"
    assert usage["cost_limitation"] == "token-count-unavailable"


def test_cloud_rerank_limits_candidates_before_sending(monkeypatch):
    monkeypatch.setenv("PP_RERANK_MAX_CANDIDATES", "2")
    client = _FakeClient(_response([{"index": 1, "score": 1.0}]))
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("query", _items())

    assert client.calls[0][1]["documents"] == ["first document", "second document"]
    assert client.calls[0][1]["top_n"] == 2
    assert [item.id for item in result] == ["second", "first", "third"]
    assert reranker.last_diagnostics["candidate_count"] == 3
    assert reranker.last_diagnostics["reranked_count"] == 1


def test_cloud_rerank_enforces_hard_candidate_and_document_limits(monkeypatch):
    monkeypatch.setenv("PP_RERANK_MAX_CANDIDATES", "999999")
    monkeypatch.setenv("PP_RERANK_MAX_DOCUMENT_CHARS", "999999")
    monkeypatch.setenv("PP_RERANK_MAX_QUERY_CHARS", "999999")
    candidates = [
        _Item(str(index), f"document-{index}-" + ("x" * 20_000), 0.5) for index in range(101)
    ]
    client = _FakeClient(_response([{"index": 0, "score": 1.0}]))
    reranker = MultiProviderReranker(http_client=client)

    reranker.rerank("q" * 20_000, candidates)

    documents = client.calls[0][1]["documents"]
    query = client.calls[0][1]["query"]
    assert len(documents) == reranker_module._HARD_MAX_CANDIDATES
    assert all(len(document) == reranker_module._HARD_MAX_DOCUMENT_CHARS for document in documents)
    assert len(query) == reranker_module._HARD_MAX_QUERY_CHARS
    assert client.calls[0][1]["top_n"] == reranker_module._HARD_MAX_CANDIDATES


def test_cloud_rerank_missing_key_does_not_call_client(monkeypatch):
    monkeypatch.delenv("PP_RERANK_API_KEY", raising=False)
    client = _FakeClient(_response([{"index": 0, "score": 1.0}]))
    items = _items()
    original = [(item.id, item.relevance) for item in items]
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("query", items)

    assert client.calls == []
    assert [(item.id, item.relevance) for item in result] == original
    assert reranker.last_provider == "original"
    assert reranker.last_diagnostics["degraded"] is True
    assert reranker.last_diagnostics["reason"] == "cloud_missing_api_key"


def test_cloud_client_uses_shared_http_policy(monkeypatch):
    from plastic_promise.core.provider_http import ProviderHTTPClient, ProviderHTTPPolicy

    # Hosted reranking is owned by the compute node; the backend path must
    # fail closed instead of constructing a cloud client.
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-compute-node")
    reranker = MultiProviderReranker()
    client = reranker._cloud_client()
    try:
        assert isinstance(client, ProviderHTTPClient)
        assert isinstance(client._policy, ProviderHTTPPolicy)
        assert client._policy.timeout_seconds == 2.5
        assert client._policy.total_timeout_seconds == 8.0
        assert client._policy.max_retries == 2
    finally:
        client.close()


def test_cloud_clients_are_reused_and_reset_across_reranker_instances(monkeypatch):
    import plastic_promise.core.provider_http as provider_http

    created = []

    class _SharedClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(provider_http, "ProviderHTTPClient", _SharedClient)
    first = MultiProviderReranker()._cloud_client()
    second = MultiProviderReranker()._cloud_client()

    assert first is second
    assert len(created) == 1

    reranker_module._reset_shared_cloud_clients()

    assert first.closed is True
    third = MultiProviderReranker()._cloud_client()
    assert third is not first
    assert len(created) == 2


def test_cloud_result_after_outer_deadline_is_rejected(monkeypatch):
    clock = [0.0]
    response = _response([{"index": 1, "score": 1.0}])

    class _AdvancingClient(_FakeClient):
        def post_json(self, path: str, payload: dict, *, deadline: float | None = None):
            self.calls.append((path, payload, deadline))
            clock[0] += 5.0
            return response

    monkeypatch.setattr(reranker_module.time, "monotonic", lambda: clock[0])
    client = _AdvancingClient()
    reranker = MultiProviderReranker(http_client=client)
    reranker._providers = ["slow", "cloud", "original"]

    def _slow_provider(_query, _candidates, _deadline):
        clock[0] += 7.0
        raise TimeoutError("private upstream details")

    reranker._rerank_slow = _slow_provider
    items = _items()
    original = [(item.id, item.relevance) for item in items]

    result = reranker.rerank("query", items)

    assert client.calls[0][2] == pytest.approx(8.0)
    assert clock[0] == 12.0
    assert [(item.id, item.relevance) for item in result] == original
    assert reranker.last_provider == "original"
    assert reranker.last_error == "cloud_total_timeout"


def test_legacy_provider_timeout_never_exceeds_remaining_deadline(monkeypatch):
    monkeypatch.setattr(reranker_module.time, "monotonic", lambda: 10.0)

    timeout = reranker_module._bounded_timeout(10.01, 5.0)

    assert timeout == pytest.approx(0.01)
    assert timeout <= 0.01


@pytest.mark.parametrize(
    ("diagnostics", "expected_status"),
    [
        (
            reranker_module._diagnostics(
                provider="cloud",
                status="success",
                degraded=False,
                reason="",
                candidate_count=1,
                reranked_count=1,
            ),
            "cloud_success",
        ),
        (
            reranker_module._diagnostics(
                provider="original",
                status="degraded",
                degraded=True,
                reason="cloud_provider_http_unauthorized",
                candidate_count=1,
                reranked_count=0,
            ),
            "fallback_original_order",
        ),
    ],
)
def test_context_engine_reports_actual_rerank_outcome(monkeypatch, diagnostics, expected_status):
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")

    def _rerank_with_diagnostics(self, _query, items):
        self._last_diagnostics = diagnostics
        return items

    monkeypatch.setattr(MultiProviderReranker, "rerank", _rerank_with_diagnostics)
    pack = _context_engine_with_one_result()._supply_python("query", [0.0], debug=True)

    assert pack.audit_metadata["rerank_status"] == expected_status
    assert pack.audit_metadata["rerank"] == diagnostics
    assert pack.audit_metadata["rerank_status"] != "multi-provider"


@pytest.mark.parametrize(
    "results,reason",
    [
        ("not-a-list", "cloud_invalid_results"),
        ([{"index": 0, "score": 0.9}, {"index": 0, "score": 0.8}], "cloud_duplicate_index"),
        ([{"index": 9, "score": 0.9}], "cloud_index_out_of_range"),
        ([{"index": True, "score": 0.9}], "cloud_invalid_index"),
        ([{"index": 0, "score": float("nan")}], "cloud_invalid_score"),
        ([{"index": 0, "score": float("inf")}], "cloud_invalid_score"),
        ([{"index": 0, "score": -0.1}], "cloud_score_out_of_range"),
        ([{"index": 0, "score": 1.1}], "cloud_score_out_of_range"),
    ],
)
def test_cloud_rerank_rejects_invalid_results_without_mutating_candidates(results, reason):
    client = _FakeClient(_response(results))
    items = _items()
    original = [(item.id, item.relevance) for item in items]
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("query", items)

    assert [(item.id, item.relevance) for item in result] == original
    assert reranker.last_provider == "original"
    assert reranker.last_diagnostics["degraded"] is True
    assert reranker.last_diagnostics["reason"] == reason


def test_cloud_rerank_accepts_partial_results_and_preserves_unreturned_scores():
    client = _FakeClient(_response([{"index": 2, "relevance_score": 1.0}]))
    items = _items()
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("query", items)

    assert [item.id for item in result] == ["first", "third", "second"]
    by_id = {item.id: item.relevance for item in result}
    assert by_id["first"] == 0.8
    assert by_id["second"] == 0.6
    assert by_id["third"] == pytest.approx(0.76)
    assert reranker.last_provider == "cloud"
    assert reranker.last_diagnostics["reranked_count"] == 1


def test_cloud_exception_is_reduced_to_stable_reason_without_secret():
    leaked = "Bearer super-secret-response-body"
    client = _FakeClient(error=RuntimeError(leaked))
    reranker = MultiProviderReranker(http_client=client)

    result = reranker.rerank("private query", _items())

    assert [item.id for item in result] == ["first", "second", "third"]
    assert reranker.last_provider == "original"
    assert reranker.last_error == "cloud_provider_error"
    assert reranker.last_diagnostics["reason"] == "cloud_provider_error"
    assert leaked not in repr(reranker.last_diagnostics)


def test_cloud_transport_reason_is_preserved_without_raw_error_text():
    error = RuntimeError("Bearer secret response text")
    error.reason = "provider_http_unauthorized"
    client = _FakeClient(error=error)
    reranker = MultiProviderReranker(http_client=client)

    reranker.rerank("private query", _items())

    assert reranker.last_error == "cloud_provider_http_unauthorized"
    assert reranker.last_diagnostics["reason"] == "cloud_provider_http_unauthorized"
    assert "secret response text" not in repr(reranker.last_diagnostics)


@pytest.mark.parametrize("provider", ["original", "cosine"])
def test_original_and_cosine_alias_preserve_order_and_scores(monkeypatch, provider):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", provider)
    items = _items()
    original = [(item.id, item.relevance) for item in items]
    reranker = MultiProviderReranker()

    result = reranker.rerank("query", items)

    assert [(item.id, item.relevance) for item in result] == original
    assert reranker.last_provider == "original"
    assert reranker.last_error == ""
    assert reranker.last_diagnostics["status"] == "skipped"
    assert reranker.last_diagnostics["degraded"] is False
    assert reranker.last_diagnostics["reason"] == "original_configured"


def test_tuple_api_preserves_original_order_and_scores_on_cloud_failure():
    candidates = [("one", "first", 0.2), ("two", "second", 0.9)]
    reranker = MultiProviderReranker(http_client=_FakeClient(error=TimeoutError("secret text")))

    result = reranker.rerank_tuples("query", candidates, top_k=10)

    assert result == [("one", 0.2), ("two", 0.9)]
    assert reranker.last_provider == "original"
    assert reranker.last_error == "cloud_timeout"


def test_cache_key_binds_content_model_and_provider_chain():
    first = [("same-id", "first content", 0.5)]
    changed = [("same-id", "changed content", 0.5)]

    assert _cache_key("q", first, model="model-a", providers=("cloud", "original")) != _cache_key(
        "q", changed, model="model-a", providers=("cloud", "original")
    )
    assert _cache_key("q", first, model="model-a", providers=("cloud", "original")) != _cache_key(
        "q", first, model="model-b", providers=("cloud", "original")
    )
    assert _cache_key("q", first, model="model-a", providers=("cloud", "original")) != _cache_key(
        "q", first, model="model-a", providers=("original",)
    )


def test_cache_key_binds_all_candidate_content_and_cloud_config():
    original = [(f"id-{index}", f"content-{index}", 0.5) for index in range(45)]
    changed = list(original)
    changed[44] = ("id-44", "changed-after-old-cache-prefix", 0.5)
    base_config = {
        "base_url": "https://first.example/v1",
        "path": "/rerank",
        "model_revision": "revision-a",
        "max_candidates": 30,
    }
    base = _cache_key(
        "q",
        original,
        model="model-a",
        providers=("cloud", "original"),
        config=base_config,
    )

    assert base != _cache_key(
        "q",
        changed,
        model="model-a",
        providers=("cloud", "original"),
        config=base_config,
    )
    for name, value in (
        ("base_url", "https://second.example/v1"),
        ("path", "/other-rerank"),
        ("model_revision", "revision-b"),
        ("max_candidates", 20),
        ("max_query_chars", 2000),
    ):
        changed_config = dict(base_config)
        changed_config[name] = value
        assert base != _cache_key(
            "q",
            original,
            model="model-a",
            providers=("cloud", "original"),
            config=changed_config,
        )


def test_global_cache_does_not_cross_cloud_endpoints(monkeypatch):
    first_client = _FakeClient(_response([{"index": 0, "score": 1.0}]))
    MultiProviderReranker(http_client=first_client).rerank("same query", _items())

    monkeypatch.setenv("PP_RERANK_BASE_URL", "https://second-provider.example/v1")
    second_client = _FakeClient(_response([{"index": 1, "score": 1.0}]))
    second = MultiProviderReranker(http_client=second_client)
    result = second.rerank("same query", _items())

    assert len(first_client.calls) == 1
    assert len(second_client.calls) == 1
    assert second.last_diagnostics["cache_hit"] is False
    assert [item.id for item in result][:2] == ["second", "first"]


def test_global_cache_does_not_cross_cloud_credentials(monkeypatch):
    first_client = _FakeClient(_response([{"index": 0, "score": 1.0}]))
    MultiProviderReranker(http_client=first_client).rerank("same query", _items())

    monkeypatch.setenv("PP_RERANK_API_KEY", "different-test-key")
    second_client = _FakeClient(_response([{"index": 1, "score": 1.0}]))
    second = MultiProviderReranker(http_client=second_client)
    second.rerank("same query", _items())

    assert len(first_client.calls) == 1
    assert len(second_client.calls) == 1
    assert second.last_diagnostics["cache_hit"] is False


def test_successful_cloud_result_is_cached_without_second_client_call():
    client = _FakeClient(_response([{"index": 1, "score": 0.9}]))
    reranker = MultiProviderReranker(http_client=client)

    reranker.rerank("same query", _items())
    reranker.rerank("same query", _items())

    assert len(client.calls) == 1
    assert reranker.last_provider == "cloud"
    assert reranker.last_diagnostics["cache_hit"] is True
    assert reranker.last_diagnostics["status"] == "cache_hit"


def test_mixed_cloud_and_ollama_chain_uses_separate_model_configuration(monkeypatch):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,ollama,original")
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("PP_RERANK_CLOUD_MODEL_REVISION", "hosted-revision-7")
    monkeypatch.setenv("PP_RERANK_OLLAMA_MODEL", "qwen2.5:3b-local")
    monkeypatch.setenv("PP_RERANK_MODEL", "ambiguous-legacy-model")

    reranker = MultiProviderReranker(http_client=_FakeClient())

    assert reranker._cloud.model == "BAAI/bge-reranker-v2-m3"
    assert reranker._cloud.model_revision == "hosted-revision-7"
    assert reranker._ollama_model == "qwen2.5:3b-local"
    assert (
        reranker._cache_model_identity(("cloud", "ollama", "original"))
        == "cloud:BAAI/bge-reranker-v2-m3@hosted-revision-7|"
        "ollama:qwen2.5:3b-local|original"
    )


def test_mixed_chain_never_reuses_legacy_cloud_model_for_ollama(monkeypatch):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,ollama,original")
    monkeypatch.setenv("PP_RERANK_MODEL", "hosted-only-model")
    monkeypatch.delenv("PP_RERANK_OLLAMA_MODEL", raising=False)

    reranker = MultiProviderReranker(http_client=_FakeClient())

    assert reranker._cloud.model == "hosted-only-model"
    assert reranker._ollama_model == "qwen2.5:3b"


def test_original_provider_ignores_malformed_cloud_cost_rate(monkeypatch):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "original")
    monkeypatch.setenv("PP_RERANK_COST_PER_MILLION_TOKENS", "not-a-number")

    reranker = MultiProviderReranker(http_client=_FakeClient())

    assert reranker._providers == ["original"]
    assert reranker._cloud.cost_policy.configured is False


def test_ollama_original_chain_ignores_malformed_cloud_cost_currency(monkeypatch):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "ollama,original")
    monkeypatch.setenv("PP_RERANK_COST_CURRENCY", "EUR")

    reranker = MultiProviderReranker(http_client=_FakeClient())

    assert reranker._providers == ["ollama", "original"]
    assert reranker._cloud.cost_policy.configured is False


def test_unknown_rerank_provider_fails_during_configuration(monkeypatch):
    monkeypatch.setenv("PP_RERANK_PROVIDERS", "cloud,typo-provider,original")

    with pytest.raises(ValueError, match="^rerank_provider_invalid$"):
        MultiProviderReranker(http_client=_FakeClient())


def test_context_supply_prefers_governed_node_rerank_and_keeps_explanation(monkeypatch):
    engine = _context_engine_with_one_result()
    engine._layered_fuse = lambda _graph, _text, _vector: [
        ("memory-1", 0.7, "first document", "vector"),
        ("memory-2", 0.6, "second document", "vector"),
    ]
    engine._memories["memory-2"] = dict(engine._memories["memory-1"])

    class GovernedRuntime:
        def rerank_for_context(self, *, project_id, query, documents):
            assert project_id == "project:alpha"
            assert query == "find second"
            assert documents == ["first document", "second document"]
            return SimpleNamespace(
                scores={0: 0.1, 1: 0.9},
                node_id="remote-a",
                selection_reason="pinned-node",
                degradation_reason="",
            )

    engine.install_memory_index_node_runtime(GovernedRuntime())
    monkeypatch.setattr(
        MultiProviderReranker,
        "rerank",
        lambda *_args, **_kwargs: pytest.fail("ungoverned fallback should not run"),
    )

    pack = engine._supply_python("find second", [0.0], project_id="project:alpha", debug=True)

    assert pack.audit_metadata["rerank_status"] == "governed-node_success"
    assert pack.audit_metadata["rerank"]["node_id"] == "remote-a"
    assert pack.audit_metadata["rerank"]["reason"] == "pinned-node"


def test_blocked_governed_runtime_uses_only_original_order_fallback(monkeypatch):
    engine = _context_engine_with_one_result()
    engine._layered_fuse = lambda _graph, _text, _vector: [
        ("memory-1", 0.7, "first document", "vector"),
        ("memory-2", 0.6, "second document", "vector"),
    ]
    engine._memories["memory-2"] = dict(engine._memories["memory-1"])
    engine.install_memory_index_node_runtime(object())
    observed: list[list[str]] = []

    def rerank(self, _query, items):
        observed.append(list(self._providers))
        return items

    monkeypatch.setattr(MultiProviderReranker, "rerank", rerank)

    engine._supply_python("find second", [0.0], project_id="project:alpha")

    assert observed == [["original"]]
