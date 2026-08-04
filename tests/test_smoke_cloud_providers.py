from __future__ import annotations

import io
import json
import math
from types import SimpleNamespace

import pytest

import scripts.smoke_cloud_providers as smoke


@pytest.mark.parametrize(
    "option",
    ["--api-key", "--syuan-api-key", "--deepseek-api-key"],
)
def test_cli_rejects_provider_key_arguments(option):
    with pytest.raises(SystemExit):
        smoke._parser().parse_args([option])


def test_parser_defaults_to_publicly_advertised_provider_contracts():
    args = smoke._parser().parse_args([])

    assert args.syuan_base_url == "https://api.syuan.org"
    assert args.embedding_model == "BAAI/bge-m3"
    assert args.embedding_dimension == 1024
    assert args.embedding_send_dimensions is False
    assert args.rerank_model == "Qwen3-Reranker-8B"
    assert args.deepseek_base_url == "https://api.deepseek.com"
    assert args.deepseek_model == "deepseek-v4-flash"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.syuan.org",
        "https://aiapi.syuan.org",
        "https://other.example/v1",
        "https://api.syuan.org:443",
        "https://user@api.syuan.org",
        "https://api.syuan.org?target=other",
        "https://api.syuan.org#fragment",
        "https://api.syuan.org/../other",
        "https://api.syuan.org/%2e%2e/other",
    ],
)
def test_main_rejects_unapproved_syuan_origin_before_key_read_or_network(
    monkeypatch,
    capsys,
    base_url,
):
    def unexpected_read_key(**_kwargs):
        raise AssertionError("Syuan key must not be read for an unapproved origin")

    def unexpected_smoke(*_args, **_kwargs):
        raise AssertionError("Syuan network path must not run for an unapproved origin")

    monkeypatch.setattr(smoke, "_read_key", unexpected_read_key)
    monkeypatch.setattr(smoke, "_smoke_syuan", unexpected_smoke)

    assert (
        smoke.main(
            ["--skip-deepseek", "--syuan-base-url", base_url],
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["syuan"] == {
        "ok": False,
        "error_type": "ValueError",
        "reason": "syuan_base_url_unapproved",
    }


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "https://other.example",
        "https://api.deepseek.com:443",
        "https://user@api.deepseek.com",
        "https://api.deepseek.com?target=other",
        "https://api.deepseek.com#fragment",
        "https://api.deepseek.com/../other",
        "https://api.deepseek.com/%2e%2e/other",
    ],
)
def test_main_rejects_unapproved_deepseek_origin_before_key_read_or_network(
    monkeypatch,
    capsys,
    base_url,
):
    def unexpected_read_key(**_kwargs):
        raise AssertionError("DeepSeek key must not be read for an unapproved origin")

    def unexpected_smoke(*_args, **_kwargs):
        raise AssertionError("DeepSeek network path must not run for an unapproved origin")

    monkeypatch.setattr(smoke, "_read_key", unexpected_read_key)
    monkeypatch.setattr(smoke, "_smoke_deepseek", unexpected_smoke)

    assert (
        smoke.main(
            ["--skip-syuan", "--deepseek-base-url", base_url],
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["deepseek"] == {
        "ok": False,
        "error_type": "ValueError",
        "reason": "deepseek_base_url_unapproved",
    }


@pytest.mark.parametrize(
    ("provider", "option", "supplied_url", "canonical_url", "skip_option"),
    [
        (
            "syuan",
            "--syuan-base-url",
            "https://api.syuan.org/",
            "https://api.syuan.org",
            "--skip-deepseek",
        ),
        (
            "deepseek",
            "--deepseek-base-url",
            "https://api.deepseek.com/",
            "https://api.deepseek.com",
            "--skip-syuan",
        ),
    ],
)
def test_main_normalizes_approved_trailing_slash_before_provider_use(
    monkeypatch,
    capsys,
    provider,
    option,
    supplied_url,
    canonical_url,
    skip_option,
):
    observed: list[tuple[str, str]] = []

    monkeypatch.setattr(smoke, "_read_key", lambda **_kwargs: "synthetic-secret")

    def fake_smoke(args, api_key):
        assert api_key == "synthetic-secret"
        observed.append((provider, getattr(args, f"{provider}_base_url")))
        return {"ok": True}

    monkeypatch.setattr(smoke, f"_smoke_{provider}", fake_smoke)

    assert smoke.main([skip_option, option, supplied_url]) == 0
    assert observed == [(provider, canonical_url)]
    assert json.loads(capsys.readouterr().out)[provider] == {"ok": True}


def test_main_ignores_provider_key_environment(monkeypatch, capsys):
    for name in (
        "SYUAN_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "PP_RERANK_API_KEY",
    ):
        monkeypatch.setenv(name, f"{name.lower()}-must-not-be-used")
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO("stdin-only-secret\n"))
    received_keys: list[str] = []

    def fake_smoke(_args, api_key):
        received_keys.append(api_key)
        return {"ok": True}

    monkeypatch.setattr(smoke, "_smoke_syuan", fake_smoke)

    assert smoke.main(["--keys-from-stdin", "--skip-deepseek"]) == 0
    assert received_keys == ["stdin-only-secret"]
    assert json.loads(capsys.readouterr().out) == {
        "synthetic_only": True,
        "syuan": {"ok": True},
    }


def test_embedding_dimension_request_is_explicit_and_reported(monkeypatch, capsys):
    observed: dict[str, object] = {}

    class FakeEmbedder:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def embed(self, _text):
            return [0.25, 0.5, 0.75]

        def close(self):
            pass

    class FakeRerankClient:
        def __init__(self, **_kwargs):
            pass

        def post_json(self, _path, payload):
            return SimpleNamespace(
                payload={"model": payload["model"], "results": _valid_rerank_rows()}
            )

        def close(self):
            pass

    monkeypatch.setattr(smoke, "OpenAICompatibleEmbedder", FakeEmbedder)
    monkeypatch.setattr(smoke, "ProviderHTTPClient", FakeRerankClient)
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO("stdin-only-secret\n"))

    assert (
        smoke.main(
            [
                "--keys-from-stdin",
                "--skip-deepseek",
                "--embedding-dimension",
                "3",
                "--embedding-send-dimensions",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert observed["send_dimensions"] is True
    assert report["syuan"]["embedding"] == {
        "model": "BAAI/bge-m3",
        "dimension": 3,
        "dimension_parameter_sent": True,
        "native_dimension": False,
        "nonzero": True,
    }


def test_read_key_uses_hidden_prompt_without_touching_stdin(monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO("stdin-must-not-be-used\n"))

    def fake_getpass(prompt):
        prompts.append(prompt)
        return "hidden-only-secret"

    monkeypatch.setattr(smoke.getpass, "getpass", fake_getpass)

    assert smoke._read_key(prompt="Provider key: ", from_stdin=False) == "hidden-only-secret"
    assert prompts == ["Provider key: "]
    assert smoke.sys.stdin.readline() == "stdin-must-not-be-used\n"


def test_read_key_uses_stdin_without_opening_hidden_prompt(monkeypatch):
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO("stdin-only-secret\r\n"))

    def unexpected_getpass(_prompt):
        raise AssertionError("hidden prompt must not be used in stdin mode")

    monkeypatch.setattr(smoke.getpass, "getpass", unexpected_getpass)

    assert smoke._read_key(prompt="Provider key: ", from_stdin=True) == "stdin-only-secret"


@pytest.mark.parametrize("raw", ["", "\n", " leading\n", "trailing \n", " \r\n"])
def test_read_key_rejects_missing_or_whitespace_padded_values(monkeypatch, raw):
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO(raw))

    with pytest.raises(ValueError, match="^provider_key_missing$"):
        smoke._read_key(prompt="Provider key: ", from_stdin=True)


def test_safe_failure_preserves_only_stable_reason_codes():
    assert smoke._safe_failure(RuntimeError("rerank_response_schema_invalid")) == {
        "ok": False,
        "error_type": "RuntimeError",
        "reason": "rerank_response_schema_invalid",
    }

    sensitive = "sk" + "-synthetic-never-a-real-credential"
    rendered = json.dumps(smoke._safe_failure(RuntimeError(sensitive)), sort_keys=True)

    assert sensitive not in rendered
    assert json.loads(rendered)["reason"] == "provider_smoke_failed"


@pytest.mark.parametrize(
    "credential_text",
    [
        "sk-syntheticCredential123",
        "key-syntheticCredential123",
        "Authorization: Bearer syntheticCredential123",
        "api_key=syntheticCredential123",
        "access-token: syntheticCredential123",
        "secret='syntheticCredential123'",
        "credential: syntheticCredential123",
        "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY",
        "0123456789abcdef0123456789abcdef",
    ],
)
def test_safe_failure_redacts_credential_like_reason_text(credential_text):
    rendered = json.dumps(smoke._safe_failure(RuntimeError(credential_text)), sort_keys=True)

    assert credential_text not in rendered
    assert json.loads(rendered) == {
        "ok": False,
        "error_type": "RuntimeError",
        "reason": "provider_smoke_failed",
    }


def test_safe_failure_does_not_expose_untrusted_exception_type_name():
    credential_text = "sk-syntheticExceptionCredential123"
    credential_error = type(credential_text, (RuntimeError,), {})

    rendered = json.dumps(
        smoke._safe_failure(credential_error("rerank_response_schema_invalid")),
        sort_keys=True,
    )

    assert credential_text not in rendered
    assert json.loads(rendered) == {
        "ok": False,
        "error_type": "Exception",
        "reason": "provider_smoke_failed",
    }


def test_main_redacts_provider_failure_details(monkeypatch, capsys):
    supplied_key = "sk" + "-synthetic-stdin-only"
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO(f"{supplied_key}\n"))

    def fail_with_sensitive_context(_args, api_key):
        raise RuntimeError(f"upstream rejected {api_key}: synthetic source body")

    monkeypatch.setattr(smoke, "_smoke_syuan", fail_with_sensitive_context)

    assert smoke.main(["--keys-from-stdin", "--skip-deepseek"]) == 1
    rendered = capsys.readouterr().out
    report = json.loads(rendered)
    assert supplied_key not in rendered
    assert "synthetic source body" not in rendered
    assert report["syuan"] == {
        "ok": False,
        "error_type": "RuntimeError",
        "reason": "provider_smoke_failed",
    }


def _valid_rerank_rows() -> list[dict[str, object]]:
    return [
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.5},
        {"index": 2, "relevance_score": 0.1},
    ]


def test_validate_rerank_response_accepts_complete_sorted_result():
    assert smoke._validate_rerank_response(
        {"model": "reranker-v1", "results": _valid_rerank_rows()},
        expected_model="reranker-v1",
        candidate_count=3,
    ) == (0, 3)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([], "rerank_response_schema_invalid"),
        (
            {"model": "other-model", "results": _valid_rerank_rows()},
            "rerank_response_model_mismatch",
        ),
        ({"model": "reranker-v1", "results": "invalid"}, "rerank_response_count_mismatch"),
        (
            {"model": "reranker-v1", "results": _valid_rerank_rows()[:2]},
            "rerank_response_count_mismatch",
        ),
        (
            {"model": "reranker-v1", "results": ["invalid", *_valid_rerank_rows()[1:]]},
            "rerank_response_schema_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": True, "relevance_score": 0.9},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_index_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    _valid_rerank_rows()[0],
                    {"index": 0, "relevance_score": 0.5},
                    _valid_rerank_rows()[2],
                ],
            },
            "rerank_response_index_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 3, "relevance_score": 0.9},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_index_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": True},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": "0.9"},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": math.nan},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": math.inf},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": -0.1},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": 1.1},
                    *_valid_rerank_rows()[1:],
                ],
            },
            "rerank_response_score_invalid",
        ),
        (
            {
                "model": "reranker-v1",
                "results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.1},
                ],
            },
            "rerank_response_order_invalid",
        ),
    ],
)
def test_validate_rerank_response_rejects_untrusted_payloads(payload, reason):
    with pytest.raises(RuntimeError, match=f"^{reason}$"):
        smoke._validate_rerank_response(
            payload,
            expected_model="reranker-v1",
            candidate_count=3,
        )


def test_success_report_excludes_vectors_credentials_and_source_material(monkeypatch, capsys):
    embedding_vector = [0.123456789, 0.234567891, 0.345678912]
    embed_inputs: list[str] = []
    rerank_requests: list[tuple[str, dict[str, object]]] = []
    json_requests: list[dict[str, object]] = []

    class FakeEmbedder:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "syuan-stdin-secret"
            assert kwargs["send_dimensions"] is False
            assert smoke.os.environ["EMBEDDER_PATH"] == "/v1/embeddings"
            self.closed = False

        def embed(self, text):
            embed_inputs.append(text)
            return embedding_vector

        def close(self):
            self.closed = True

    class FakeRerankClient:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "syuan-stdin-secret"

        def post_json(self, path, payload):
            rerank_requests.append((path, payload))
            return SimpleNamespace(
                payload={"model": payload["model"], "results": _valid_rerank_rows()}
            )

        def close(self):
            pass

    class FakeJSONProvider:
        identity = "safe-provider-identity"
        stats = {"requests": 1}

        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "deepseek-stdin-secret"
            assert kwargs["temperature"] == 0.0
            assert kwargs["top_p"] == 1.0
            assert kwargs["json_mode"] is True

        def complete_json(self, **kwargs):
            json_requests.append(kwargs)
            return {"selected": "alpha"}

        def close(self):
            pass

    monkeypatch.setattr(smoke, "OpenAICompatibleEmbedder", FakeEmbedder)
    monkeypatch.setattr(smoke, "ProviderHTTPClient", FakeRerankClient)
    monkeypatch.setattr(smoke, "OpenAICompatibleJSONProvider", FakeJSONProvider)
    monkeypatch.setattr(
        smoke.sys,
        "stdin",
        io.StringIO("syuan-stdin-secret\ndeepseek-stdin-secret\n"),
    )
    monkeypatch.setenv("PP_EMBEDDING_DIM", "4096")
    monkeypatch.setenv("EMBEDDER_PATH", "/previous-embeddings")

    assert smoke.main(["--keys-from-stdin", "--embedding-dimension", "3"]) == 0
    rendered = capsys.readouterr().out
    report = json.loads(rendered)

    assert embed_inputs == ["Synthetic provider smoke: canonical records live in SQLite."]
    assert rerank_requests[0][0] == "/v1/rerank"
    assert rerank_requests[0][1]["documents"] == [
        "SQLite stores the canonical records.",
        "LanceDB is a rebuildable derived vector index.",
        "A synthetic weather sentence is unrelated.",
    ]
    assert json_requests[0]["user_payload"] == {
        "candidates": ["alpha", "beta"],
        "required": "alpha",
    }
    assert report == {
        "synthetic_only": True,
        "syuan": {
            "ok": True,
            "embedding": {
                "model": "BAAI/bge-m3",
                "dimension": 3,
                "dimension_parameter_sent": False,
                "native_dimension": True,
                "nonzero": True,
            },
            "rerank": {
                "model": "Qwen3-Reranker-8B",
                "result_count": 3,
                "top_index": 0,
            },
        },
        "deepseek": {
            "ok": True,
            "model": "deepseek-v4-flash",
            "identity": "safe-provider-identity",
            "output_fields": ["selected"],
            "requests": 1,
        },
    }
    for forbidden in (
        "syuan-stdin-secret",
        "deepseek-stdin-secret",
        *[str(component) for component in embedding_vector],
        *embed_inputs,
        *rerank_requests[0][1]["documents"],
        "Which component stores the canonical records?",
        "alpha",
        "beta",
    ):
        assert forbidden not in rendered
    assert smoke.os.environ["PP_EMBEDDING_DIM"] == "4096"
    assert smoke.os.environ["EMBEDDER_PATH"] == "/previous-embeddings"
