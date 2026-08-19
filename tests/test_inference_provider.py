from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plastic_promise.core.inference_provider import (
    OllamaJSONProvider,
    OpenAICompatibleJSONProvider,
    build_structured_json_provider,
)


@pytest.fixture(autouse=True)
def _compute_node_role(monkeypatch):
    """Provider construction is intentionally restricted to the compute plane."""

    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-compute-node")


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.closed = False

    def post_json(self, path, payload):
        self.calls.append((path, payload))
        return SimpleNamespace(payload=self.payload, latency_ms=4.5)

    def close(self):
        self.closed = True


def _response(content, usage=None):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def test_json_provider_uses_openai_contract_reuses_client_and_tracks_usage():
    client = _Client(_response('{"summary":"source text"}'))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        model="deepseek-chat",
        model_revision="DeepSeek-V3.2",
        client=client,
    )

    first = provider.complete_json(system_prompt="Return JSON.", user_payload={"source": "one"})
    second = provider.complete_json(system_prompt="Return JSON.", user_payload={"source": "two"})

    assert first == second == {"summary": "source text"}
    assert [path for path, _ in client.calls] == ["/chat/completions", "/chat/completions"]
    request = client.calls[0][1]
    assert request["model"] == "deepseek-chat"
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    assert request["top_p"] == 1
    assert "thinking" not in request
    assert json.loads(request["messages"][1]["content"]) == {"source": "one"}
    assert provider.identity.startswith("openai-compatible:deepseek-chat@DeepSeek-V3.2|")
    assert "endpoint_sha256=" in provider.identity
    assert provider.stats["requests"] == 2
    assert provider.stats["total_tokens"] == 20
    provider.close()
    assert client.closed is True


def test_json_provider_uses_deepseek_defaults_and_disables_thinking(monkeypatch):
    monkeypatch.delenv("PP_INFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("PP_INFERENCE_MODEL", raising=False)
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        client=client,
    )

    provider.complete_json(system_prompt="Return json.", user_payload={})

    assert provider._base_url == "https://api.deepseek.com"
    assert provider.model == "deepseek-v4-flash"
    assert client.calls[0][1]["thinking"] == {"type": "disabled"}


def test_json_provider_supports_one_sampling_override_and_binds_identity():
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        temperature=0.2,
        top_p=1.0,
        json_mode=True,
        client=client,
    )

    provider.complete_json(system_prompt="Return JSON.", user_payload={})

    request = client.calls[0][1]
    assert request["temperature"] == 0.2
    assert request["top_p"] == 1.0
    assert request["response_format"] == {"type": "json_object"}
    assert "|temperature=0.2|top_p=1|json_mode=1" in provider.identity


def test_json_provider_rejects_simultaneous_sampling_overrides():
    with pytest.raises(ValueError, match="inference_sampling_parameters_conflict"):
        OpenAICompatibleJSONProvider(
            api_key="not-a-real-key",
            temperature=0.2,
            top_p=0.8,
            client=_Client(_response("{}")),
        )


def test_deepseek_json_mode_requires_json_instruction():
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://api.deepseek.com",
        client=_Client(_response("{}")),
    )

    with pytest.raises(ValueError, match="inference_json_prompt_required"):
        provider.complete_json(system_prompt="Return an object.", user_payload={})


def test_json_provider_reports_explicit_cny_cost(monkeypatch):
    monkeypatch.setenv("PP_INFERENCE_COST_PER_MILLION_TOKENS", "0.02")
    monkeypatch.setenv("PP_INFERENCE_COST_CURRENCY", "CNY")
    monkeypatch.setenv("PP_INFERENCE_PRICING_REVISION", "syuan-pricing-2026-07-24")
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=_Client(_response("{}")),
    )

    provider.complete_json(system_prompt="Return json.", user_payload={})

    assert provider.stats["estimated_cost"] == 0.0000002
    assert provider.stats["cost_currency"] == "CNY"
    assert provider.stats["estimated_cost_usd"] is None
    assert provider.stats["pricing_revision"] == "syuan-pricing-2026-07-24"
    assert provider.stats["cost_basis"] == "total_tokens_single_blended_rate"
    assert provider.stats["cost_limitation"] == "distinct-input-output-rates-not-modeled"


def test_json_provider_cost_is_unknown_with_incomplete_token_evidence(monkeypatch):
    monkeypatch.setenv("PP_INFERENCE_COST_PER_MILLION_TOKENS", "0.02")
    response = _response("{}")
    response["usage"] = {"prompt_tokens": 7}
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=_Client(response),
    )

    provider.complete_json(system_prompt="Return json.", user_payload={})

    assert provider.stats["estimated_cost"] is None
    assert provider.stats["estimated_cost_usd"] is None
    assert provider.stats["cost_basis"] == "unknown"
    assert provider.stats["cost_limitation"] == "token-count-unavailable"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "https://api.deepseek.com.evil.test",
        "https://api.deepseek.com/private/v1",
        "https://api.deepseek.com?target=third-party",
    ],
)
def test_json_provider_does_not_send_thinking_to_unpinned_endpoints(base_url):
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url=base_url,
        client=client,
    )

    provider.complete_json(system_prompt="Return json.", user_payload={})

    assert "thinking" not in client.calls[0][1]


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "tool_calls", 7])
def test_json_provider_rejects_non_stop_finish_reason_with_stable_error(finish_reason):
    response = _response("{}")
    response["choices"][0]["finish_reason"] = finish_reason
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=_Client(response),
    )

    with pytest.raises(RuntimeError, match="^inference_finish_reason_not_stop$"):
        provider.complete_json(system_prompt="Return json.", user_payload={})


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "inference_response_schema_invalid"),
        (_response(""), "inference_response_schema_invalid"),
        (_response("[]"), "inference_output_object_required"),
        (_response('{"score":NaN}'), "inference_output_invalid_json"),
        (_response("not-json"), "inference_output_invalid_json"),
    ],
)
def test_json_provider_rejects_untrusted_response(payload, reason):
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=_Client(payload),
    )

    with pytest.raises(RuntimeError, match=f"^{reason}$"):
        provider.complete_json(system_prompt="Return JSON.", user_payload={"source": "private"})


def test_json_provider_requires_key_without_injected_client(monkeypatch):
    monkeypatch.delenv("PP_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="inference_api_key_missing"):
        OpenAICompatibleJSONProvider(base_url="https://inference.example.test/v1")


def test_supplier_key_is_only_selected_for_pinned_deepseek_endpoint(monkeypatch):
    monkeypatch.delenv("PP_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("PP_INFERENCE_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-supplier-key")

    provider = OpenAICompatibleJSONProvider()
    try:
        assert provider._base_url == "https://api.deepseek.com"
        assert provider._api_key == "deepseek-supplier-key"
    finally:
        provider.close()

    with pytest.raises(ValueError, match="inference_api_key_missing"):
        OpenAICompatibleJSONProvider(base_url="https://inference.example.test/v1")


def test_custom_endpoint_uses_independent_generic_key(monkeypatch):
    monkeypatch.setenv("PP_INFERENCE_API_KEY", "independent-provider-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key-must-not-win")

    provider = OpenAICompatibleJSONProvider(base_url="https://inference.example.test/v1")
    try:
        assert provider._api_key == "independent-provider-key"
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"system_prompt": "x" * (32 * 1024 + 1), "user_payload": {}},
            "inference_system_prompt_too_large",
        ),
        (
            {"system_prompt": "Return JSON.", "user_payload": {"source": "x" * (256 * 1024)}},
            "inference_user_payload_too_large",
        ),
        (
            {"system_prompt": "Return JSON.", "user_payload": {}, "max_tokens": 0},
            "inference_max_tokens_invalid",
        ),
    ],
)
def test_json_provider_enforces_cost_and_payload_budgets(kwargs, reason):
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=client,
    )

    with pytest.raises(ValueError, match=f"^{reason}$"):
        provider.complete_json(**kwargs)

    assert client.calls == []


def test_json_provider_rejects_non_json_payload_before_transport():
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=client,
    )

    with pytest.raises(ValueError, match="^inference_user_payload_invalid$"):
        provider.complete_json(
            system_prompt="Return JSON.",
            user_payload={"value": float("nan")},
        )

    assert client.calls == []


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        (
            "PP_INFERENCE_MAX_SYSTEM_PROMPT_BYTES",
            str(256 * 1024 + 1),
            "inference_max_system_prompt_bytes_invalid",
        ),
        (
            "PP_INFERENCE_MAX_USER_PAYLOAD_BYTES",
            str(2 * 1024 * 1024 + 1),
            "inference_max_user_payload_bytes_invalid",
        ),
        ("PP_INFERENCE_MAX_TOKENS", "-1", "inference_max_tokens_invalid"),
        (
            "PP_INFERENCE_MAX_OUTPUT_CHARS",
            str(1024 * 1024 + 1),
            "inference_max_output_chars_invalid",
        ),
    ],
)
def test_json_provider_rejects_configuration_above_hard_limits(monkeypatch, name, value, reason):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"^{reason}$"):
        OpenAICompatibleJSONProvider(
            api_key="not-a-real-key",
            base_url="https://inference.example.test/v1",
            client=_Client(_response("{}")),
        )


def test_json_provider_accepts_configured_token_budget_above_legacy_ceiling(monkeypatch):
    monkeypatch.setenv("PP_INFERENCE_MAX_TOKENS", "16384")
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=_Client(_response("{}")),
    )

    assert provider._max_tokens == 16_384


def test_json_provider_accepts_request_above_legacy_ceiling_by_default():
    client = _Client(_response("{}"))
    provider = OpenAICompatibleJSONProvider(
        api_key="not-a-real-key",
        base_url="https://inference.example.test/v1",
        client=client,
    )

    assert (
        provider.complete_json(system_prompt="Return JSON.", user_payload={}, max_tokens=16_384)
        == {}
    )
    assert client.calls[0][1]["max_tokens"] == 16_384


def test_ollama_json_provider_uses_same_structured_input_contract():
    client = _Client(
        {
            "message": {"role": "assistant", "content": '{"summary":"source text"}'},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 2,
        }
    )
    schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    provider = OllamaJSONProvider(
        host="http://127.0.0.1:11434",
        model="qwen3:8b",
        model_revision="sha256:" + ("a" * 64),
        output_schema=schema,
        client=client,
    )

    result = provider.complete_json(
        system_prompt="Return json.",
        user_payload={"source": "one"},
        max_tokens=256,
    )

    assert result == {"summary": "source text"}
    path, request = client.calls[0]
    assert path == "/api/chat"
    assert request["model"] == "qwen3:8b"
    assert request["think"] is False
    assert request["stream"] is False
    assert request["format"] == schema
    assert request["options"] == {"temperature": 0, "num_predict": 256}
    assert json.loads(request["messages"][1]["content"]) == {"source": "one"}
    assert provider.identity.startswith("ollama:qwen3:8b@sha256:")
    assert provider.stats["total_tokens"] == 7
    provider.close()
    assert client.closed is True


@pytest.mark.parametrize(
    "host",
    [
        "http://192.0.2.10:11434",
        "https://ollama.example.test",
        "http://user:password@127.0.0.1:11434",
        "http://127.0.0.1:11434/proxy",
    ],
)
def test_ollama_json_provider_is_loopback_only_even_with_injected_client(host):
    with pytest.raises(ValueError, match="^ollama_base_url_must_be_loopback$"):
        OllamaJSONProvider(host=host, client=_Client({}))


@pytest.mark.parametrize("done_reason", ["length", "load", 7])
def test_ollama_json_provider_rejects_incomplete_output(done_reason):
    client = _Client(
        {
            "message": {"content": "{}"},
            "done": True,
            "done_reason": done_reason,
        }
    )
    provider = OllamaJSONProvider(client=client)

    with pytest.raises(RuntimeError, match="^inference_finish_reason_not_stop$"):
        provider.complete_json(system_prompt="Return json.", user_payload={})


def test_structured_provider_factory_keeps_selection_on_backend(monkeypatch):
    cloud_client = _Client(_response("{}"))
    cloud = build_structured_json_provider(
        "cloud",
        api_key="not-a-real-key",
        base_url="https://provider.example.test/v1",
        client=cloud_client,
    )
    local = build_structured_json_provider(
        "local",
        base_url="http://localhost:11434",
        client=_Client({"message": {"content": "{}"}, "done": True}),
    )

    assert isinstance(cloud, OpenAICompatibleJSONProvider)
    assert isinstance(local, OllamaJSONProvider)
    monkeypatch.setenv("PP_INFERENCE_PROVIDER", "frontend-injected-provider")
    with pytest.raises(ValueError, match="^inference_provider_invalid$"):
        build_structured_json_provider(client=_Client({}))
