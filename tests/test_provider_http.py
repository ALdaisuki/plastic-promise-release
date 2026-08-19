import datetime as dt
import socket
import threading

import httpcore
import httpx
import pytest

from plastic_promise.core.provider_http import (
    ProviderHTTPClient,
    ProviderHTTPError,
    ProviderHTTPPolicy,
    _ResolvedPublicNetworkBackend,
)


class _Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def wall_time(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AdvancingByteStream(httpx.SyncByteStream):
    def __init__(self, clock: _Clock, chunks: list[tuple[float, bytes]]) -> None:
        self._clock = clock
        self._chunks = chunks

    def __iter__(self):
        for delay, chunk in self._chunks:
            self._clock.advance(delay)
            yield chunk


def _policy(**overrides) -> ProviderHTTPPolicy:
    values = {
        "timeout_seconds": 5.0,
        "total_timeout_seconds": 20.0,
        "max_retries": 2,
        "backoff_base_seconds": 0.5,
        "backoff_max_seconds": 4.0,
        "max_request_bytes": 1_024,
        "max_response_bytes": 1_024,
        "circuit_failure_threshold": 3,
        "circuit_recovery_seconds": 10.0,
    }
    values.update(overrides)
    return ProviderHTTPPolicy(**values)


def _client(
    handler,
    *,
    clock: _Clock | None = None,
    base_url: str = "https://provider.example/v1",
    api_key: str = "test-secret-key",
    **policy_overrides,
) -> ProviderHTTPClient:
    clock = clock or _Clock()
    return ProviderHTTPClient(
        provider="test-provider",
        base_url=base_url,
        api_key=api_key,
        policy=_policy(**policy_overrides),
        transport=httpx.MockTransport(handler),
        clock=clock.monotonic,
        wall_clock=clock.wall_time,
        sleeper=clock.sleep,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.example/v1",
        "http://localhost:8080/v1",
        "http://service.localhost/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_config_accepts_https_or_loopback_http(base_url):
    client = _client(lambda _request: httpx.Response(200, json={"ok": True}), base_url=base_url)

    assert client.post_json("health", {}).payload == {"ok": True}
    client.close()


def test_server_backend_cannot_construct_any_inference_provider(monkeypatch):
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-server-backend")

    with pytest.raises(ProviderHTTPError, match="cloud_provider_requires_compute_node"):
        ProviderHTTPClient(
            provider="future-provider-label",
            base_url="https://provider.example/v1",
            api_key="test-secret-key",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        )


def test_unknown_endpoint_role_cannot_construct_inference_provider(monkeypatch):
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "unknown-endpoint")

    with pytest.raises(ProviderHTTPError, match="cloud_provider_requires_compute_node"):
        ProviderHTTPClient(
            provider="future-provider-label",
            base_url="https://provider.example/v1",
            api_key="test-secret-key",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        )


def test_missing_endpoint_role_cannot_construct_production_inference_provider(monkeypatch):
    monkeypatch.delenv("PP_ENDPOINT_ROLE", raising=False)

    with pytest.raises(ProviderHTTPError, match="cloud_provider_requires_compute_node"):
        ProviderHTTPClient(
            provider="future-provider-label",
            base_url="https://provider.example/v1",
            api_key="test-secret-key",
        )


def test_missing_endpoint_role_allows_only_injected_test_transport(monkeypatch):
    monkeypatch.delenv("PP_ENDPOINT_ROLE", raising=False)
    client = ProviderHTTPClient(
        provider="future-provider-label",
        base_url="https://provider.example/v1",
        api_key="test-secret-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True})),
    )

    assert client.post_json("health", {}).payload == {"ok": True}
    client.close()


def test_compute_node_can_construct_inference_provider(monkeypatch):
    monkeypatch.setenv("PP_ENDPOINT_ROLE", "pp-compute-node")
    client = ProviderHTTPClient(
        provider="node-cloud-structured-json",
        base_url="https://provider.example/v1",
        api_key="test-secret-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True})),
    )

    assert client.post_json("chat/completions", {}).payload == {"ok": True}
    client.close()


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.7",
        "192.168.1.7",
        "127.0.0.1",
        "169.254.10.4",
        "::1",
        "fc00::7",
        "2001:db8::7",
    ],
)
def test_connection_time_dns_guard_rejects_private_answers(address):
    def resolver(*_args, **_kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    backend = _ResolvedPublicNetworkBackend(
        allow_loopback=False,
        resolver=resolver,
        backend=object(),
    )

    with pytest.raises(httpcore.ConnectError, match="^provider_http_dns_address_rejected$"):
        backend.connect_tcp("provider.example", 443)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://wiki.syuan.org",
        "https://wiki.syuan.org/",
        "https://docs.example.com/v1",
        "https://api.docs.example/v1",
        "https://help.example.com/v1",
        "https://api.example.com/docs/v1",
        "https://api.example.com/%64ocs/v1",
    ],
)
def test_config_rejects_obvious_documentation_base_url(base_url):
    with pytest.raises(ProviderHTTPError) as caught:
        _client(lambda _request: httpx.Response(200, json={}), base_url=base_url)

    assert str(caught.value) == "provider_http_documentation_base_url"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://helpful-api.example.com/v1",
        "https://api.example.com/v1/docs",
    ],
)
def test_config_does_not_reject_documentation_like_api_labels(base_url):
    client = _client(lambda _request: httpx.Response(200, json={"ok": True}), base_url=base_url)

    assert client.post_json("health", {}).payload == {"ok": True}
    client.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?secret=value",
        "https://provider.example/v1?",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1#",
        "provider.example/v1",
    ],
)
def test_config_rejects_unsafe_base_url(base_url):
    with pytest.raises(ProviderHTTPError) as caught:
        _client(lambda _request: httpx.Response(200, json={}), base_url=base_url)

    assert str(caught.value) == "provider_http_invalid_base_url"


def test_config_fails_closed_without_api_key_and_redacts_repr():
    with pytest.raises(ProviderHTTPError) as caught:
        _client(lambda _request: httpx.Response(200, json={}), api_key="   ")

    assert str(caught.value) == "provider_http_api_key_missing"
    client = _client(lambda _request: httpx.Response(200, json={}))
    assert "test-secret-key" not in repr(client)
    client.close()


def test_explicit_unauthenticated_loopback_omits_authorization_header():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = ProviderHTTPClient(
        provider="local-model",
        base_url="http://127.0.0.1:11434",
        api_key=None,
        policy=_policy(),
        allow_unauthenticated_loopback=True,
        transport=httpx.MockTransport(handler),
    )

    assert client.post_json("api/chat", {}).payload == {"ok": True}
    assert "authorization" not in requests[0].headers
    client.close()


def test_unauthenticated_mode_never_applies_to_remote_https():
    with pytest.raises(ProviderHTTPError, match="^provider_http_api_key_missing$"):
        ProviderHTTPClient(
            provider="local-model",
            base_url="https://provider.example/v1",
            api_key=None,
            policy=_policy(),
            allow_unauthenticated_loopback=True,
        )


def test_policy_rejects_response_budget_above_hard_limit():
    with pytest.raises(ProviderHTTPError, match="provider_http_invalid_config"):
        ProviderHTTPPolicy(max_response_bytes=64 * 1024 * 1024 + 1)


def test_client_reuses_transport_and_returns_safe_diagnostics():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "request-123"},
            json={
                "data": [{"embedding": [0.1, 0.2]}],
                "usage": {
                    "prompt_tokens": 7,
                    "total_tokens": 7,
                    "provider_note": "must-not-leak",
                    "cached": True,
                },
            },
        )

    client = _client(handler)
    first = client.post_json("embeddings", {"input": "first"})
    second = client.post_json(path="/embeddings", payload={"input": "second"})

    assert len(requests) == 2
    assert requests[0].url == httpx.URL("https://provider.example/v1/embeddings")
    assert requests[1].url == requests[0].url
    assert requests[0].headers["authorization"] == "Bearer test-secret-key"
    assert first.payload["data"][0]["embedding"] == [0.1, 0.2]
    assert first.attempts == 1
    assert first.request_id.startswith("sha256:")
    assert "request-123" not in first.request_id
    assert first.usage == {"prompt_tokens": 7, "total_tokens": 7}
    assert second.circuit_state == "closed"


def test_retryable_statuses_use_retry_after_and_exponential_backoff():
    clock = _Clock()
    statuses = [408, 429, 500, 200]

    def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        headers = {"retry-after": "2"} if status == 429 else {}
        return httpx.Response(status, headers=headers, json={"ok": True})

    client = _client(handler, clock=clock, max_retries=3)
    result = client.post_json("embeddings", {"input": "bounded"})

    assert result.payload == {"ok": True}
    assert result.attempts == 4
    assert clock.sleeps == [0.5, 2.0, 2.0]
    assert result.latency_ms == pytest.approx(4_500.0)


def test_retry_after_http_date_is_bounded():
    clock = _Clock(value=1_700_000_000.0)
    retry_at = dt.datetime.fromtimestamp(clock.value + 30, tz=dt.timezone.utc)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"retry-after": retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")},
                json={"error": "rate limit"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, clock=clock, backoff_max_seconds=3.0)

    assert client.post_json("embeddings", {"input": "bounded"}).payload == {"ok": True}
    assert clock.sleeps == [3.0]


def test_transport_errors_are_retried_without_exposing_details():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("secret-host-detail", request=request)
        return httpx.Response(200, json={"ok": True})

    result = _client(handler).post_json("embeddings", {"input": "private-input"})

    assert calls == 3
    assert result.attempts == 3


def test_unexpected_transport_failure_is_reduced_to_stable_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("test-secret-key private-input provider-body")

    with pytest.raises(ProviderHTTPError) as caught:
        _client(handler).post_json("embeddings", {"input": "private-input"})

    assert str(caught.value) == "provider_http_request_failed"
    assert caught.value.args == ("provider_http_request_failed",)
    assert "test-secret-key" not in repr(caught.value)
    assert "private-input" not in repr(caught.value)


def test_oversized_request_is_rejected_before_transport_without_content_leak():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, max_request_bytes=32)
    with pytest.raises(ProviderHTTPError) as caught:
        client.post_json("embeddings", {"input": "private-input" * 8})

    assert str(caught.value) == "provider_http_request_too_large"
    assert "private-input" not in repr(caught.value)
    assert calls == 0


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "provider_http_unauthorized"), (403, "provider_http_forbidden")],
)
def test_auth_failures_are_not_retried(status, reason):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "contains-secret-body"})

    with pytest.raises(ProviderHTTPError) as caught:
        _client(handler).post_json("embeddings", {"input": "private-input"})

    assert calls == 1
    assert str(caught.value) == reason
    assert caught.value.attempts == 1
    assert "contains-secret-body" not in repr(caught.value)
    assert "private-input" not in repr(caught.value)


def test_total_deadline_prevents_unbounded_retry_after_sleep():
    clock = _Clock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "9"}, json={"error": "wait"})

    with pytest.raises(ProviderHTTPError) as caught:
        _client(handler, clock=clock, total_timeout_seconds=2.0).post_json(
            "embeddings", {"input": "private-input"}
        )

    assert calls == 1
    assert clock.sleeps == []
    assert str(caught.value) == "provider_http_deadline_exceeded"


def test_caller_deadline_is_checked_while_streaming_response_chunks():
    clock = _Clock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AdvancingByteStream(
                clock,
                [(0.6, b'{"ok":'), (0.6, b"true}")],
            ),
        )

    client = _client(handler, clock=clock, total_timeout_seconds=20.0)
    with pytest.raises(ProviderHTTPError) as caught:
        client.post_json(
            "embeddings",
            {"input": "private-input"},
            deadline=clock.value + 1.0,
        )

    assert str(caught.value) == "provider_http_deadline_exceeded"
    assert caught.value.attempts == 1
    assert "private-input" not in repr(caught.value)


@pytest.mark.parametrize("deadline", [True, float("inf"), float("nan"), "soon"])
def test_caller_deadline_rejects_non_finite_or_non_numeric_values(deadline):
    client = _client(lambda _request: httpx.Response(200, json={}))

    with pytest.raises(ProviderHTTPError, match="provider_http_invalid_deadline"):
        client.post_json("embeddings", {"input": "x"}, deadline=deadline)


def test_retry_exhaustion_has_stable_redacted_error():
    key = "key-that-must-never-appear"
    body = f"provider body included {key} and private-input"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=body)

    client = ProviderHTTPClient(
        provider="test-provider",
        base_url="https://provider.example/v1",
        api_key=key,
        policy=_policy(max_retries=1),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ProviderHTTPError) as caught:
        client.post_json("embeddings", {"input": "private-input"})

    rendered = repr(caught.value)
    assert str(caught.value) == "provider_http_retry_exhausted"
    assert caught.value.args == ("provider_http_retry_exhausted",)
    assert key not in rendered
    assert body not in rendered
    assert "private-input" not in rendered
    assert caught.value.attempts == 2
    assert caught.value.status_code == 503


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(200, content=b"{"), "provider_http_invalid_json"),
        (httpx.Response(200, content=b"\xff"), "provider_http_invalid_utf8"),
        (httpx.Response(200, json=[1, 2, 3]), "provider_http_json_object_required"),
        (
            httpx.Response(200, content=b"x" * 1_025),
            "provider_http_response_too_large",
        ),
        (
            httpx.Response(200, headers={"content-length": "9999"}, content=b"{}"),
            "provider_http_response_too_large",
        ),
    ],
)
def test_response_validation_fails_closed_without_body(response, reason):
    with pytest.raises(ProviderHTTPError) as caught:
        _client(lambda _request: response).post_json("embeddings", {"input": "private-input"})

    assert str(caught.value) == reason
    assert "private-input" not in repr(caught.value)
    assert "9999" not in repr(caught.value)


def test_request_id_with_control_characters_is_hashed():
    response = httpx.Response(
        200,
        headers={"x-request-id": "unsafe\nrequest-id"},
        json={"ok": True},
    )

    result = _client(lambda _request: response).post_json("embeddings", {"input": "x"})

    assert result.request_id.startswith("sha256:")
    assert "\n" not in result.request_id


def test_request_id_is_always_hashed_even_when_header_looks_safe():
    response = httpx.Response(
        401,
        headers={"x-request-id": "syntactically-safe-secret-value"},
        json={"error": "denied"},
    )

    with pytest.raises(ProviderHTTPError) as caught:
        _client(lambda _request: response).post_json("embeddings", {"input": "x"})

    assert caught.value.request_id.startswith("sha256:")
    assert "syntactically-safe-secret-value" not in caught.value.request_id


def test_circuit_opens_after_threshold_and_recovers_via_half_open_probe():
    clock = _Clock()
    healthy = False
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200 if healthy else 503, json={"ok": healthy})

    client = _client(
        handler,
        clock=clock,
        max_retries=0,
        circuit_failure_threshold=2,
        circuit_recovery_seconds=5.0,
    )
    for _ in range(2):
        with pytest.raises(ProviderHTTPError, match="provider_http_retry_exhausted"):
            client.post_json("embeddings", {"input": "x"})

    assert client.circuit_state == "open"
    with pytest.raises(ProviderHTTPError, match="provider_http_circuit_open"):
        client.post_json("embeddings", {"input": "x"})
    assert calls == 2

    healthy = True
    clock.advance(5.1)
    result = client.post_json("embeddings", {"input": "x"})

    assert result.payload == {"ok": True}
    assert client.circuit_state == "closed"
    assert calls == 3


def test_only_one_thread_owns_half_open_probe():
    clock = _Clock()
    entered = threading.Event()
    release = threading.Event()
    phase = "fail"

    def handler(_request: httpx.Request) -> httpx.Response:
        if phase == "fail":
            return httpx.Response(503, json={"ok": False})
        entered.set()
        assert release.wait(timeout=2)
        return httpx.Response(200, json={"ok": True})

    client = _client(
        handler,
        clock=clock,
        max_retries=0,
        circuit_failure_threshold=1,
        circuit_recovery_seconds=1.0,
    )
    with pytest.raises(ProviderHTTPError):
        client.post_json("embeddings", {"input": "x"})
    clock.advance(1.1)
    phase = "probe"

    probe_result = []

    def probe() -> None:
        probe_result.append(client.post_json("embeddings", {"input": "x"}))

    thread = threading.Thread(target=probe)
    thread.start()
    assert entered.wait(timeout=2)
    with pytest.raises(ProviderHTTPError, match="provider_http_circuit_open"):
        client.post_json("embeddings", {"input": "x"})
    release.set()
    thread.join(timeout=2)

    assert len(probe_result) == 1
    assert client.circuit_state == "closed"


def test_late_failure_from_old_circuit_epoch_cannot_reopen_recovered_circuit():
    clock = _Clock()
    late_entered = threading.Event()
    release_late = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if b'"late"' in request.content:
            late_entered.set()
            assert release_late.wait(timeout=2)
            return httpx.Response(503, json={"ok": False})
        if b'"trip"' in request.content:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    client = _client(
        handler,
        clock=clock,
        max_retries=0,
        circuit_failure_threshold=1,
        circuit_recovery_seconds=1.0,
    )
    late_errors: list[ProviderHTTPError] = []

    def late_request() -> None:
        try:
            client.post_json("embeddings", {"input": "late"})
        except ProviderHTTPError as exc:
            late_errors.append(exc)

    thread = threading.Thread(target=late_request)
    thread.start()
    assert late_entered.wait(timeout=2)

    with pytest.raises(ProviderHTTPError, match="provider_http_retry_exhausted"):
        client.post_json("embeddings", {"input": "trip"})
    assert client.circuit_state == "open"

    clock.advance(1.1)
    assert client.post_json("embeddings", {"input": "probe"}).payload == {"ok": True}
    assert client.circuit_state == "closed"

    release_late.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert [error.reason for error in late_errors] == ["provider_http_retry_exhausted"]
    assert client.circuit_state == "closed"


def test_close_is_idempotent_and_prevents_further_requests():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.close()
    client.close()

    with pytest.raises(ProviderHTTPError, match="provider_http_client_closed"):
        client.post_json("embeddings", {"input": "x"})
    assert calls == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/v1",
        "//evil.example/v1",
        "../escape",
        "..",
        ".",
        "%2e%2e/escape",
        "",
        "#frag",
    ],
)
def test_endpoint_must_remain_below_configured_base_url(endpoint):
    client = _client(lambda _request: httpx.Response(200, json={"ok": True}))

    with pytest.raises(ProviderHTTPError, match="provider_http_invalid_endpoint"):
        client.post_json(endpoint, {"input": "x"})
