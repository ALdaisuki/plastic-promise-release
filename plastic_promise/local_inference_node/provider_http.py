"""Fail-closed HTTP transport shared by cloud inference providers."""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlsplit

import httpcore
import httpx

from .provider_errors import ProviderHTTPDiagnostics, ProviderHTTPError, ProviderHTTPResult
from .support import require_compute_node_role

_RETRYABLE_STATUSES = frozenset({408, 429})
_MAX_RETRIES = 32
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_USAGE_VALUE = (1 << 63) - 1
_DOCUMENTATION_HOST_LABELS = frozenset(
    {
        "doc",
        "docs",
        "documentation",
        "docs-site",
        "help",
        "help-center",
        "helpcenter",
        "wiki",
        "wiki-site",
    }
)
_DOCUMENTATION_PATH_LABELS = frozenset(
    {
        "doc",
        "docs",
        "documentation",
        "help",
        "help-center",
        "helpcenter",
        "wiki",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)


class _ResolvedPublicNetworkBackend(httpcore.NetworkBackend):
    """Resolve a provider hostname and connect only to an approved address.

    ``httpcore`` normally resolves the hostname inside ``socket.create_connection``.
    That leaves a DNS-rebinding window between any application-level hostname
    check and the actual connect.  Resolve here, validate every answer, and pass
    a numeric address to the underlying backend so it cannot resolve the name a
    second time.  TLS still receives the original hostname from httpcore for
    certificate and SNI validation.
    """

    def __init__(
        self,
        *,
        allow_loopback: bool,
        resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._allow_loopback = allow_loopback
        self._resolver = resolver
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        try:
            answers = self._resolver(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except (OSError, UnicodeError, TypeError):
            raise httpcore.ConnectError("provider_http_dns_resolution_failed") from None

        addresses: list[str] = []
        seen: set[str] = set()
        for answer in answers:
            if len(answer) < 5:
                raise httpcore.ConnectError("provider_http_dns_resolution_failed")
            sockaddr = answer[4]
            if not isinstance(sockaddr, tuple) or not sockaddr:
                raise httpcore.ConnectError("provider_http_dns_resolution_failed")
            raw_address = sockaddr[0]
            if not isinstance(raw_address, str):
                raise httpcore.ConnectError("provider_http_dns_resolution_failed")
            # IPv6 scoped addresses are local/link-local by definition.  Strip
            # the zone only after rejecting the special address below.
            address_text = raw_address.split("%", 1)[0]
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError:
                raise httpcore.ConnectError("provider_http_dns_resolution_failed") from None
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            allowed = address.is_loopback if self._allow_loopback else _is_public_address(address)
            if not allowed:
                raise httpcore.ConnectError("provider_http_dns_address_rejected")
            normalized = str(address)
            if normalized not in seen:
                seen.add(normalized)
                addresses.append(normalized)

        if not addresses:
            raise httpcore.ConnectError("provider_http_dns_resolution_failed")

        last_error: BaseException | None = None
        for address in addresses:
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("provider_http_connection_failed")

    def connect_unix_socket(self, *args: object, **kwargs: object) -> httpcore.NetworkStream:
        return self._backend.connect_unix_socket(*args, **kwargs)  # type: ignore[arg-type]

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return true only for globally routable unicast addresses."""

    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _resolved_transport(base_url: str) -> httpx.HTTPTransport:
    """Build the default transport with a connection-time DNS guard."""

    transport = httpx.HTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        # A future httpx/httpcore API change must fail closed rather than silently
        # dropping the address validation.
        transport.close()
        raise ProviderHTTPError("provider_http_dns_guard_unavailable")
    pool._network_backend = _ResolvedPublicNetworkBackend(  # type: ignore[attr-defined]
        allow_loopback=_is_loopback_base_url(base_url)
    )
    return transport


@dataclass(frozen=True)
class ProviderHTTPPolicy:
    """Validated retry, timeout, response, and circuit-breaker limits."""

    timeout_seconds: float = 30.0
    total_timeout_seconds: float = 90.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 30.0
    max_request_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        numeric_values = (
            self.timeout_seconds,
            self.total_timeout_seconds,
            self.backoff_base_seconds,
            self.backoff_max_seconds,
            self.circuit_recovery_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        ):
            raise ProviderHTTPError("provider_http_invalid_config")
        if (
            self.timeout_seconds <= 0
            or self.total_timeout_seconds <= 0
            or self.backoff_base_seconds < 0
            or self.backoff_max_seconds < 0
            or self.circuit_recovery_seconds <= 0
            or self.backoff_base_seconds > self.backoff_max_seconds
        ):
            raise ProviderHTTPError("provider_http_invalid_config")
        if not _is_nonnegative_int(self.max_retries) or self.max_retries > _MAX_RETRIES:
            raise ProviderHTTPError("provider_http_invalid_config")
        if (
            not _is_positive_int(self.max_request_bytes)
            or self.max_request_bytes > _MAX_REQUEST_BYTES
            or not _is_positive_int(self.max_response_bytes)
            or self.max_response_bytes > _MAX_RESPONSE_BYTES
        ):
            raise ProviderHTTPError("provider_http_invalid_config")
        if not _is_positive_int(self.circuit_failure_threshold):
            raise ProviderHTTPError("provider_http_invalid_config")


@dataclass(frozen=True)
class _CircuitPermission:
    epoch: int
    is_probe: bool = False


class ProviderHTTPClient:
    """Reusable synchronous HTTP client with retries, deadlines, and a circuit breaker."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str | None,
        policy: ProviderHTTPPolicy | Mapping[str, object] | None = None,
        *,
        allow_unauthenticated_loopback: bool = False,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider = _validate_provider(provider)
        self._base_url = _validate_base_url(base_url)
        if not isinstance(allow_unauthenticated_loopback, bool):
            raise ProviderHTTPError("provider_http_invalid_config")
        if not isinstance(api_key, str) or not api_key.strip():
            if not allow_unauthenticated_loopback or not _is_loopback_base_url(self._base_url):
                raise ProviderHTTPError("provider_http_api_key_missing")
            self._api_key = ""
        else:
            self._api_key = api_key.strip()
        try:
            require_compute_node_role(injected_transport=transport is not None)
        except RuntimeError:
            raise ProviderHTTPError("cloud_provider_requires_compute_node") from None
        self._policy = _coerce_policy(policy)
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._client = httpx.Client(
            transport=transport if transport is not None else _resolved_transport(self._base_url),
            timeout=self._policy.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._lock = threading.RLock()
        self._closed = False
        self._circuit_state = "closed"
        self._circuit_failures = 0
        self._circuit_opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._circuit_epoch = 0

    @property
    def circuit_state(self) -> str:
        with self._lock:
            if self._circuit_state == "open" and self._recovery_elapsed_locked():
                return "half-open"
            return self._circuit_state

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._client.close()

    def __enter__(self) -> ProviderHTTPClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> ProviderHTTPResult:
        """POST one JSON object and return a validated JSON object response."""
        self._ensure_open()
        url = self._resolve_endpoint(path)
        body = _encode_payload(payload, maximum=self._policy.max_request_bytes)
        started_at = self._clock()
        effective_deadline = _effective_deadline(
            started_at=started_at,
            policy_timeout=float(self._policy.total_timeout_seconds),
            caller_deadline=deadline,
        )
        if effective_deadline <= started_at:
            raise ProviderHTTPError(
                "provider_http_deadline_exceeded",
                self._diagnostics(
                    started_at=started_at,
                    attempts=0,
                    status_code=None,
                    request_id=None,
                ),
            )
        permission = self._acquire_circuit_permission()

        status_code: int | None = None
        request_id: str | None = None

        max_attempts = self._policy.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            remaining = effective_deadline - self._clock()
            if remaining <= 0:
                raise self._terminal_error(
                    "provider_http_deadline_exceeded",
                    started_at=started_at,
                    attempts=attempt - 1,
                    status_code=status_code,
                    request_id=request_id,
                    permission=permission,
                    counts_for_circuit=True,
                )

            try:
                headers = {
                    "accept": "application/json",
                    "content-type": "application/json",
                }
                if self._api_key:
                    headers["authorization"] = f"Bearer {self._api_key}"
                with self._client.stream(
                    "POST",
                    url,
                    content=body,
                    headers=headers,
                    timeout=min(float(self._policy.timeout_seconds), remaining),
                ) as response:
                    status_code = response.status_code
                    request_id = _safe_request_id(response.headers.get("x-request-id"))
                    if request_id is None:
                        request_id = _safe_request_id(response.headers.get("request-id"))

                    if self._clock() >= effective_deadline:
                        raise self._terminal_error(
                            "provider_http_deadline_exceeded",
                            started_at=started_at,
                            attempts=attempt,
                            status_code=status_code,
                            request_id=request_id,
                            permission=permission,
                            counts_for_circuit=True,
                        )

                    if status_code == 401:
                        raise self._terminal_error(
                            "provider_http_unauthorized",
                            started_at=started_at,
                            attempts=attempt,
                            status_code=status_code,
                            request_id=request_id,
                            permission=permission,
                            counts_for_circuit=False,
                        )
                    if status_code == 403:
                        raise self._terminal_error(
                            "provider_http_forbidden",
                            started_at=started_at,
                            attempts=attempt,
                            status_code=status_code,
                            request_id=request_id,
                            permission=permission,
                            counts_for_circuit=False,
                        )
                    if _is_retryable_status(status_code):
                        if attempt >= max_attempts:
                            raise self._terminal_error(
                                "provider_http_retry_exhausted",
                                started_at=started_at,
                                attempts=attempt,
                                status_code=status_code,
                                request_id=request_id,
                                permission=permission,
                                counts_for_circuit=True,
                            )
                        retry_after = _parse_retry_after(
                            response.headers.get("retry-after"),
                            wall_time=self._wall_clock(),
                            maximum=float(self._policy.backoff_max_seconds),
                        )
                        delay = self._retry_delay(attempt, retry_after)
                    elif not 200 <= status_code < 300:
                        raise self._terminal_error(
                            "provider_http_request_failed",
                            started_at=started_at,
                            attempts=attempt,
                            status_code=status_code,
                            request_id=request_id,
                            permission=permission,
                            counts_for_circuit=False,
                        )
                    else:
                        try:
                            decoded = self._decode_response(
                                response,
                                deadline=effective_deadline,
                            )
                        except _ResponseValidationError as exc:
                            raise self._terminal_error(
                                exc.reason,
                                started_at=started_at,
                                attempts=attempt,
                                status_code=status_code,
                                request_id=request_id,
                                permission=permission,
                                counts_for_circuit=True,
                            ) from None
                        if self._clock() >= effective_deadline:
                            raise self._terminal_error(
                                "provider_http_deadline_exceeded",
                                started_at=started_at,
                                attempts=attempt,
                                status_code=status_code,
                                request_id=request_id,
                                permission=permission,
                                counts_for_circuit=True,
                            )
                        self._record_success(permission=permission)
                        diagnostics = self._diagnostics(
                            started_at=started_at,
                            attempts=attempt,
                            status_code=status_code,
                            request_id=request_id,
                            usage=_safe_usage(decoded.get("usage")),
                        )
                        return ProviderHTTPResult(
                            payload=decoded,
                            attempts=diagnostics.attempts,
                            latency_ms=diagnostics.latency_ms,
                            request_id=diagnostics.request_id,
                            status_code=diagnostics.status_code,
                            usage=diagnostics.usage,
                            circuit_state=diagnostics.circuit_state,
                        )
            except ProviderHTTPError:
                raise
            except httpx.TransportError:
                if attempt >= max_attempts:
                    raise self._terminal_error(
                        "provider_http_retry_exhausted",
                        started_at=started_at,
                        attempts=attempt,
                        status_code=status_code,
                        request_id=request_id,
                        permission=permission,
                        counts_for_circuit=True,
                    ) from None
                delay = self._retry_delay(attempt, None)
            except httpx.HTTPError:
                raise self._terminal_error(
                    "provider_http_request_failed",
                    started_at=started_at,
                    attempts=attempt,
                    status_code=status_code,
                    request_id=request_id,
                    permission=permission,
                    counts_for_circuit=True,
                ) from None
            except Exception:
                raise self._terminal_error(
                    "provider_http_request_failed",
                    started_at=started_at,
                    attempts=attempt,
                    status_code=status_code,
                    request_id=request_id,
                    permission=permission,
                    counts_for_circuit=True,
                ) from None

            remaining = effective_deadline - self._clock()
            if delay >= remaining:
                raise self._terminal_error(
                    "provider_http_deadline_exceeded",
                    started_at=started_at,
                    attempts=attempt,
                    status_code=status_code,
                    request_id=request_id,
                    permission=permission,
                    counts_for_circuit=True,
                )
            try:
                self._sleeper(delay)
            except Exception:
                raise self._terminal_error(
                    "provider_http_request_failed",
                    started_at=started_at,
                    attempts=attempt,
                    status_code=status_code,
                    request_id=request_id,
                    permission=permission,
                    counts_for_circuit=True,
                ) from None

        raise AssertionError("provider retry loop exhausted unexpectedly")

    def _ensure_open(self) -> None:
        with self._lock:
            if self._closed:
                raise ProviderHTTPError(
                    "provider_http_client_closed",
                    ProviderHTTPDiagnostics(circuit_state=self._circuit_state),
                )

    def _resolve_endpoint(self, endpoint: str) -> str:
        if not isinstance(endpoint, str):
            raise ProviderHTTPError("provider_http_invalid_endpoint")
        raw = endpoint.strip()
        if not raw or raw != endpoint or _contains_control(raw) or _contains_whitespace(raw):
            raise ProviderHTTPError("provider_http_invalid_endpoint")

        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ProviderHTTPError("provider_http_invalid_endpoint")
        if raw.startswith("//"):
            raise ProviderHTTPError("provider_http_invalid_endpoint")
        relative = raw[1:] if raw.startswith("/") else raw
        if not relative or relative.startswith("/"):
            raise ProviderHTTPError("provider_http_invalid_endpoint")

        decoded = relative
        for _ in range(3):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise ProviderHTTPError("provider_http_invalid_endpoint")
        if any(character in decoded for character in ("?", "#")) or _contains_control(decoded):
            raise ProviderHTTPError("provider_http_invalid_endpoint")

        return f"{self._base_url}/{relative}"

    def _acquire_circuit_permission(self) -> _CircuitPermission:
        with self._lock:
            if self._closed:
                raise ProviderHTTPError(
                    "provider_http_client_closed",
                    ProviderHTTPDiagnostics(circuit_state=self._circuit_state),
                )
            if self._circuit_state == "closed":
                return _CircuitPermission(epoch=self._circuit_epoch)
            if self._circuit_state == "open":
                if not self._recovery_elapsed_locked():
                    raise ProviderHTTPError(
                        "provider_http_circuit_open",
                        ProviderHTTPDiagnostics(circuit_state="open"),
                    )
                self._circuit_state = "half-open"
                self._half_open_probe_in_flight = True
                return _CircuitPermission(epoch=self._circuit_epoch, is_probe=True)
            if self._half_open_probe_in_flight:
                raise ProviderHTTPError(
                    "provider_http_circuit_open",
                    ProviderHTTPDiagnostics(circuit_state="half-open"),
                )
            self._half_open_probe_in_flight = True
            return _CircuitPermission(epoch=self._circuit_epoch, is_probe=True)

    def _record_success(self, *, permission: _CircuitPermission) -> None:
        with self._lock:
            if permission.epoch != self._circuit_epoch:
                return
            if permission.is_probe:
                self._circuit_state = "closed"
                self._circuit_failures = 0
                self._circuit_opened_at = None
                self._half_open_probe_in_flight = False
                self._circuit_epoch += 1
            elif self._circuit_state == "closed":
                self._circuit_failures = 0

    def _record_failure(
        self,
        *,
        permission: _CircuitPermission,
        counts_for_circuit: bool,
    ) -> None:
        with self._lock:
            if permission.epoch != self._circuit_epoch:
                return
            if permission.is_probe:
                self._open_circuit_locked()
                return
            if not counts_for_circuit or self._circuit_state != "closed":
                return
            self._circuit_failures += 1
            if self._circuit_failures >= self._policy.circuit_failure_threshold:
                self._open_circuit_locked()

    def _open_circuit_locked(self) -> None:
        self._circuit_state = "open"
        self._circuit_failures = self._policy.circuit_failure_threshold
        self._circuit_opened_at = self._clock()
        self._half_open_probe_in_flight = False
        self._circuit_epoch += 1

    def _recovery_elapsed_locked(self) -> bool:
        return self._circuit_opened_at is not None and (
            self._clock() - self._circuit_opened_at >= self._policy.circuit_recovery_seconds
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        exponential = min(
            float(self._policy.backoff_base_seconds) * (2 ** (attempt - 1)),
            float(self._policy.backoff_max_seconds),
        )
        return max(exponential, retry_after or 0.0)

    def _decode_response(
        self,
        response: httpx.Response,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        if self._clock() >= deadline:
            raise _ResponseValidationError("provider_http_deadline_exceeded")
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except (TypeError, ValueError):
                raise _ResponseValidationError("provider_http_invalid_response") from None
            if parsed_length < 0:
                raise _ResponseValidationError("provider_http_invalid_response")
            if parsed_length > self._policy.max_response_bytes:
                raise _ResponseValidationError("provider_http_response_too_large")

        body = bytearray()
        for chunk in response.iter_bytes():
            if self._clock() >= deadline:
                raise _ResponseValidationError("provider_http_deadline_exceeded")
            if len(body) + len(chunk) > self._policy.max_response_bytes:
                raise _ResponseValidationError("provider_http_response_too_large")
            body.extend(chunk)
        if self._clock() >= deadline:
            raise _ResponseValidationError("provider_http_deadline_exceeded")
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            raise _ResponseValidationError("provider_http_invalid_utf8") from None
        try:
            decoded = json.loads(text, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise _ResponseValidationError("provider_http_invalid_json") from None
        if not isinstance(decoded, dict):
            raise _ResponseValidationError("provider_http_json_object_required")
        return decoded

    def _terminal_error(
        self,
        reason: str,
        *,
        started_at: float,
        attempts: int,
        status_code: int | None,
        request_id: str | None,
        permission: _CircuitPermission,
        counts_for_circuit: bool,
    ) -> ProviderHTTPError:
        self._record_failure(
            permission=permission,
            counts_for_circuit=counts_for_circuit,
        )
        return ProviderHTTPError(
            reason,
            self._diagnostics(
                started_at=started_at,
                attempts=attempts,
                status_code=status_code,
                request_id=request_id,
            ),
        )

    def _diagnostics(
        self,
        *,
        started_at: float,
        attempts: int,
        status_code: int | None,
        request_id: str | None,
        usage: dict[str, int | float] | None = None,
    ) -> ProviderHTTPDiagnostics:
        return ProviderHTTPDiagnostics(
            attempts=attempts,
            latency_ms=max(0.0, (self._clock() - started_at) * 1_000.0),
            status_code=status_code,
            request_id=request_id,
            usage=usage or {},
            circuit_state=self.circuit_state,
        )


class _ResponseValidationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise ProviderHTTPError("provider_http_invalid_base_url")
    if (
        _contains_control(base_url)
        or _contains_whitespace(base_url)
        or "\\" in base_url
        or "?" in base_url
        or "#" in base_url
    ):
        raise ProviderHTTPError("provider_http_invalid_base_url")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ProviderHTTPError("provider_http_invalid_base_url") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderHTTPError("provider_http_invalid_base_url")

    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ProviderHTTPError("provider_http_invalid_base_url")

    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
        raise ProviderHTTPError("provider_http_invalid_base_url")
    if _contains_control(decoded_path):
        raise ProviderHTTPError("provider_http_invalid_base_url")
    if _looks_like_documentation_url(parsed.hostname, decoded_path):
        raise ProviderHTTPError("provider_http_documentation_base_url")

    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = f"{host}:{port}" if port is not None else host
    normalized = f"{parsed.scheme}://{authority}{parsed.path}".rstrip("/")
    if not normalized or normalized.endswith(":"):
        raise ProviderHTTPError("provider_http_invalid_base_url")
    return normalized


def _looks_like_documentation_url(hostname: str, path: str) -> bool:
    """Identify obvious documentation sites before a provider can receive input."""

    host = hostname.rstrip(".").casefold()
    with suppress(UnicodeError):
        host = host.encode("idna").decode("ascii")
    if any(label in _DOCUMENTATION_HOST_LABELS for label in host.split(".")):
        return True

    # A documentation site commonly uses /docs or /wiki as its root. Only
    # inspect the first path component so legitimate API routes such as
    # /v1/docs remain available.
    first_path_component = next((part for part in path.split("/") if part), "")
    return first_path_component.casefold() in _DOCUMENTATION_PATH_LABELS


def _validate_provider(provider: object) -> str:
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", provider):
        raise ProviderHTTPError("provider_http_invalid_provider")
    return provider


def _coerce_policy(
    policy: ProviderHTTPPolicy | Mapping[str, object] | None,
) -> ProviderHTTPPolicy:
    if policy is None:
        return ProviderHTTPPolicy()
    if isinstance(policy, ProviderHTTPPolicy):
        return policy
    if not isinstance(policy, Mapping):
        raise ProviderHTTPError("provider_http_invalid_config")

    aliases = {
        "timeout_sec": "timeout_seconds",
        "total_timeout_sec": "total_timeout_seconds",
    }
    values: dict[str, object] = {}
    for raw_key, value in policy.items():
        if not isinstance(raw_key, str):
            raise ProviderHTTPError("provider_http_invalid_config")
        key = aliases.get(raw_key, raw_key)
        if key in values:
            raise ProviderHTTPError("provider_http_invalid_config")
        values[key] = value
    try:
        return ProviderHTTPPolicy(**values)
    except TypeError:
        raise ProviderHTTPError("provider_http_invalid_config") from None


def _effective_deadline(
    *,
    started_at: float,
    policy_timeout: float,
    caller_deadline: float | None,
) -> float:
    policy_deadline = started_at + policy_timeout
    if caller_deadline is None:
        return policy_deadline
    if (
        isinstance(caller_deadline, bool)
        or not isinstance(caller_deadline, (int, float))
        or not math.isfinite(float(caller_deadline))
    ):
        raise ProviderHTTPError("provider_http_invalid_deadline")
    return min(policy_deadline, float(caller_deadline))


def _is_loopback_host(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _is_loopback_base_url(base_url: str) -> bool:
    try:
        hostname = urlsplit(base_url).hostname
    except (TypeError, ValueError):
        return False
    return bool(hostname and _is_loopback_host(hostname))


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _contains_whitespace(value: str) -> bool:
    return any(character.isspace() for character in value)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _encode_payload(payload: Mapping[str, Any], *, maximum: int) -> bytes:
    if not isinstance(payload, Mapping):
        raise ProviderHTTPError("provider_http_invalid_payload")
    try:
        encoded = json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError):
        raise ProviderHTTPError("provider_http_invalid_payload") from None
    if len(encoded) > maximum:
        raise ProviderHTTPError("provider_http_request_too_large")
    return encoded


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or 500 <= status_code <= 599


def _parse_retry_after(value: str | None, *, wall_time: float, maximum: float) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    try:
        delay = float(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            delay = parsed.timestamp() - wall_time
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return min(delay, maximum)


def _safe_request_id(value: str | None) -> str | None:
    if value is None or not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def _safe_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, int | float] = {}
    for key in sorted(_USAGE_FIELDS):
        numeric = value.get(key)
        if isinstance(numeric, int) and not isinstance(numeric, bool):
            if 0 <= numeric <= _MAX_USAGE_VALUE:
                safe[key] = numeric
        elif (
            isinstance(numeric, float)
            and math.isfinite(numeric)
            and 0 <= numeric <= _MAX_USAGE_VALUE
        ):
            safe[key] = numeric
    return safe


def _reject_json_constant(_value: str) -> None:
    raise ValueError
