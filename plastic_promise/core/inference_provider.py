"""OpenAI-compatible JSON inference used by derived-memory analysis."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import threading
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

from plastic_promise.core.cost_telemetry import TokenCostPolicy
from plastic_promise.core.provider_http import ProviderHTTPClient, ProviderHTTPPolicy
from plastic_promise.core.structured_token_budget import (
    UNBOUNDED_STRUCTURED_TOKEN_LIMIT,
    structured_tokens_allowed,
    validate_structured_token_limit,
)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_MAX_SYSTEM_PROMPT_BYTES = 32 * 1024
_HARD_MAX_SYSTEM_PROMPT_BYTES = 256 * 1024
_DEFAULT_MAX_USER_PAYLOAD_BYTES = 256 * 1024
_HARD_MAX_USER_PAYLOAD_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_TOKENS = UNBOUNDED_STRUCTURED_TOKEN_LIMIT
_DEFAULT_MAX_OUTPUT_CHARS = 16_384
_HARD_MAX_OUTPUT_CHARS = 1024 * 1024


class StructuredJSONProvider(Protocol):
    """Backend-only cloud/local contract for normalized JSON input."""

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]: ...

    @property
    def identity(self) -> str: ...

    @property
    def stats(self) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class OpenAICompatibleJSONProvider:
    """Reusable, content-safe JSON chat client.

    Provider output is parsed here but remains untrusted. Callers must apply
    their domain schema and grounding checks before using it.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        path: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        json_mode: bool | None = None,
        max_output_chars: int | None = None,
        client: object | None = None,
    ) -> None:
        endpoint_role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
        if endpoint_role and endpoint_role != "pp-compute-node":
            raise ValueError("inference_requires_compute_node")
        self._base_url = (
            base_url
            if base_url is not None
            else os.getenv("PP_INFERENCE_BASE_URL", _DEEPSEEK_BASE_URL)
        ).strip()
        if api_key is not None:
            resolved_key = api_key
        else:
            resolved_key = os.getenv("PP_INFERENCE_API_KEY", "")
            if not resolved_key.strip() and _is_pinned_deepseek_endpoint(self._base_url):
                resolved_key = os.getenv("DEEPSEEK_API_KEY", "")
        self._api_key = resolved_key.strip()
        if not self._api_key and client is None:
            raise ValueError("inference_api_key_missing")
        self._model = (model or os.getenv("PP_INFERENCE_MODEL", _DEEPSEEK_DEFAULT_MODEL)).strip()
        self._model_revision = (
            model_revision or os.getenv("PP_INFERENCE_MODEL_REVISION", self._model)
        ).strip()
        self._path = (path or os.getenv("PP_INFERENCE_PATH", "/chat/completions")).strip()
        self._temperature = _bounded_float_setting(
            temperature,
            env_name="PP_INFERENCE_TEMPERATURE",
            default=0.0,
            minimum=0.0,
            maximum=2.0,
            reason="inference_temperature_invalid",
        )
        self._top_p = _bounded_float_setting(
            top_p,
            env_name="PP_INFERENCE_TOP_P",
            default=1.0,
            minimum=0.0,
            maximum=1.0,
            reason="inference_top_p_invalid",
        )
        self._json_mode = _boolean_setting(
            json_mode,
            env_name="PP_INFERENCE_JSON_MODE",
            default=True,
            reason="inference_json_mode_invalid",
        )
        if self._temperature != 1.0 and self._top_p != 1.0:
            raise ValueError("inference_sampling_parameters_conflict")
        self._max_output_chars = _bounded_int_setting(
            max_output_chars,
            env_name="PP_INFERENCE_MAX_OUTPUT_CHARS",
            default=_DEFAULT_MAX_OUTPUT_CHARS,
            minimum=128,
            maximum=_HARD_MAX_OUTPUT_CHARS,
            reason="inference_max_output_chars_invalid",
        )
        self._max_system_prompt_bytes = _bounded_int_setting(
            None,
            env_name="PP_INFERENCE_MAX_SYSTEM_PROMPT_BYTES",
            default=_DEFAULT_MAX_SYSTEM_PROMPT_BYTES,
            minimum=1,
            maximum=_HARD_MAX_SYSTEM_PROMPT_BYTES,
            reason="inference_max_system_prompt_bytes_invalid",
        )
        self._max_user_payload_bytes = _bounded_int_setting(
            None,
            env_name="PP_INFERENCE_MAX_USER_PAYLOAD_BYTES",
            default=_DEFAULT_MAX_USER_PAYLOAD_BYTES,
            minimum=2,
            maximum=_HARD_MAX_USER_PAYLOAD_BYTES,
            reason="inference_max_user_payload_bytes_invalid",
        )
        self._max_tokens = _token_limit_setting(
            None,
            env_name="PP_INFERENCE_MAX_TOKENS",
            default=_DEFAULT_MAX_TOKENS,
            reason="inference_max_tokens_invalid",
        )
        if not self._model or not self._model_revision:
            raise ValueError("inference_model_identity_missing")
        self._cost_policy = TokenCostPolicy.from_environment(
            "PP_INFERENCE",
            reason_prefix="inference",
        )
        self._client = client or self._build_http_client()
        self._stats_lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._token_usage_complete = True
        self._latency_ms = 0.0

    def _build_http_client(self) -> ProviderHTTPClient:
        policy = ProviderHTTPPolicy(
            timeout_seconds=_float_env("PP_INFERENCE_TIMEOUT_SEC", 45.0, minimum=0.1),
            total_timeout_seconds=_float_env("PP_INFERENCE_TOTAL_TIMEOUT_SEC", 120.0, minimum=0.1),
            max_retries=_int_env("PP_INFERENCE_MAX_RETRIES", 2, minimum=0),
            backoff_base_seconds=_float_env("PP_INFERENCE_RETRY_BACKOFF_SEC", 0.5, minimum=0.0),
            backoff_max_seconds=_float_env("PP_INFERENCE_RETRY_BACKOFF_MAX_SEC", 8.0, minimum=0.0),
            circuit_failure_threshold=_int_env(
                "PP_INFERENCE_CIRCUIT_FAILURE_THRESHOLD", 5, minimum=1
            ),
            circuit_recovery_seconds=_float_env(
                "PP_INFERENCE_CIRCUIT_RECOVERY_SEC", 30.0, minimum=0.1
            ),
            max_response_bytes=_int_env(
                "PP_INFERENCE_MAX_RESPONSE_BYTES", 2 * 1024 * 1024, minimum=1024
            ),
        )
        return ProviderHTTPClient(
            provider="json-inference",
            base_url=self._base_url,
            api_key=self._api_key,
            policy=policy,
        )

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]:
        serialized_payload = _validated_inference_input(
            system_prompt=system_prompt,
            user_payload=user_payload,
            max_tokens=max_tokens,
            max_system_prompt_bytes=self._max_system_prompt_bytes,
            max_user_payload_bytes=self._max_user_payload_bytes,
            max_tokens_limit=self._max_tokens,
        )
        if (
            self._json_mode
            and _is_pinned_deepseek_endpoint(self._base_url)
            and "json" not in system_prompt.casefold()
        ):
            raise ValueError("inference_json_prompt_required")
        request_payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": serialized_payload,
                },
            ],
            "temperature": self._temperature,
            "top_p": self._top_p,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._json_mode:
            request_payload["response_format"] = {"type": "json_object"}
        if _is_pinned_deepseek_endpoint(self._base_url):
            request_payload["thinking"] = {"type": "disabled"}
        result = self._client.post_json(self._path, request_payload)
        payload = getattr(result, "payload", result)
        content = _response_content(payload)
        if len(content) > self._max_output_chars:
            raise RuntimeError("inference_output_too_large")
        try:
            decoded = json.loads(content, parse_constant=_reject_constant)
        except (TypeError, ValueError, RecursionError):
            raise RuntimeError("inference_output_invalid_json") from None
        if not isinstance(decoded, dict):
            raise RuntimeError("inference_output_object_required")
        self._record_usage(result, payload)
        return decoded

    def _record_usage(self, result: object, payload: object) -> None:
        usage = payload.get("usage", {}) if isinstance(payload, Mapping) else {}
        usage = usage if isinstance(usage, Mapping) else {}

        def count(name: str) -> tuple[int, bool]:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return int(value), True
            return 0, False

        prompt_tokens, has_prompt_tokens = count("prompt_tokens")
        supplied_input_tokens, has_input_tokens = count("input_tokens")
        completion_tokens, has_completion_tokens = count("completion_tokens")
        supplied_output_tokens, has_output_tokens = count("output_tokens")
        supplied_total_tokens, has_total_tokens = count("total_tokens")
        input_tokens = prompt_tokens if has_prompt_tokens else supplied_input_tokens
        output_tokens = completion_tokens if has_completion_tokens else supplied_output_tokens
        component_usage_complete = (has_prompt_tokens or has_input_tokens) and (
            has_completion_tokens or has_output_tokens
        )
        usage_complete = has_total_tokens or component_usage_complete
        total_tokens = supplied_total_tokens if has_total_tokens else input_tokens + output_tokens
        latency = getattr(result, "latency_ms", 0.0)
        latency_ms = float(latency) if isinstance(latency, (int, float)) else 0.0
        with self._stats_lock:
            self._requests += 1
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._total_tokens += total_tokens
            self._token_usage_complete = self._token_usage_complete and usage_complete
            self._latency_ms += max(latency_ms, 0.0)

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def identity(self) -> str:
        endpoint = hashlib.sha256(f"{self._base_url}\0{self._path}".encode()).hexdigest()
        return (
            f"openai-compatible:{self._model}@{self._model_revision}"
            f"|endpoint_sha256={endpoint}|temperature={self._temperature:g}"
            f"|top_p={self._top_p:g}|json_mode={int(self._json_mode)}"
        )

    @property
    def stats(self) -> dict[str, object]:
        with self._stats_lock:
            return {
                "provider": "openai-compatible",
                "model": self._model,
                "revision": self._model_revision,
                "requests": self._requests,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._total_tokens,
                "latency_ms": round(self._latency_ms, 3),
                **self._cost_policy.telemetry(
                    self._total_tokens if self._token_usage_complete else None,
                    cost_basis="total_tokens_single_blended_rate",
                ),
            }

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class OllamaJSONProvider:
    """Structured JSON inference over a loopback-only Ollama endpoint."""

    def __init__(
        self,
        *,
        host: str | None = None,
        model: str | None = None,
        model_revision: str | None = None,
        path: str = "/api/chat",
        output_schema: Mapping[str, object] | None = None,
        max_output_chars: int | None = None,
        client: object | None = None,
    ) -> None:
        endpoint_role = os.environ.get("PP_ENDPOINT_ROLE", "").strip()
        if endpoint_role and endpoint_role != "pp-compute-node":
            raise ValueError("inference_requires_compute_node")
        self._base_url = _normalize_ollama_base_url(
            host or os.getenv("PP_LOCAL_INFERENCE_BASE_URL") or os.getenv("OLLAMA_HOST")
        )
        self._model = (model or os.getenv("PP_LOCAL_INFERENCE_MODEL", "qwen3:8b")).strip()
        self._model_revision = (
            model_revision or os.getenv("PP_LOCAL_INFERENCE_MODEL_REVISION", self._model)
        ).strip()
        self._path = path.strip()
        if not self._model or not self._model_revision:
            raise ValueError("inference_model_identity_missing")
        if not self._path:
            raise ValueError("inference_path_invalid")
        self._max_output_chars = _bounded_int_setting(
            max_output_chars,
            env_name="PP_LOCAL_INFERENCE_MAX_OUTPUT_CHARS",
            default=_DEFAULT_MAX_OUTPUT_CHARS,
            minimum=128,
            maximum=_HARD_MAX_OUTPUT_CHARS,
            reason="inference_max_output_chars_invalid",
        )
        self._max_system_prompt_bytes = _bounded_int_setting(
            None,
            env_name="PP_LOCAL_INFERENCE_MAX_SYSTEM_PROMPT_BYTES",
            default=_DEFAULT_MAX_SYSTEM_PROMPT_BYTES,
            minimum=1,
            maximum=_HARD_MAX_SYSTEM_PROMPT_BYTES,
            reason="inference_max_system_prompt_bytes_invalid",
        )
        self._max_user_payload_bytes = _bounded_int_setting(
            None,
            env_name="PP_LOCAL_INFERENCE_MAX_USER_PAYLOAD_BYTES",
            default=_DEFAULT_MAX_USER_PAYLOAD_BYTES,
            minimum=2,
            maximum=_HARD_MAX_USER_PAYLOAD_BYTES,
            reason="inference_max_user_payload_bytes_invalid",
        )
        self._max_tokens = _token_limit_setting(
            None,
            env_name="PP_LOCAL_INFERENCE_MAX_TOKENS",
            default=_DEFAULT_MAX_TOKENS,
            reason="inference_max_tokens_invalid",
        )
        self._output_schema = _validated_output_schema(output_schema)
        self._client = client or self._build_http_client()
        self._stats_lock = threading.Lock()
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._latency_ms = 0.0

    def _build_http_client(self) -> ProviderHTTPClient:
        policy = ProviderHTTPPolicy(
            timeout_seconds=_float_env("PP_LOCAL_INFERENCE_TIMEOUT_SEC", 45.0, minimum=0.1),
            total_timeout_seconds=_float_env(
                "PP_LOCAL_INFERENCE_TOTAL_TIMEOUT_SEC", 120.0, minimum=0.1
            ),
            max_retries=_int_env("PP_LOCAL_INFERENCE_MAX_RETRIES", 1, minimum=0),
            backoff_base_seconds=_float_env(
                "PP_LOCAL_INFERENCE_RETRY_BACKOFF_SEC", 0.25, minimum=0.0
            ),
            backoff_max_seconds=_float_env(
                "PP_LOCAL_INFERENCE_RETRY_BACKOFF_MAX_SEC", 2.0, minimum=0.0
            ),
            circuit_failure_threshold=_int_env(
                "PP_LOCAL_INFERENCE_CIRCUIT_FAILURE_THRESHOLD", 3, minimum=1
            ),
            circuit_recovery_seconds=_float_env(
                "PP_LOCAL_INFERENCE_CIRCUIT_RECOVERY_SEC", 15.0, minimum=0.1
            ),
            max_response_bytes=_int_env(
                "PP_LOCAL_INFERENCE_MAX_RESPONSE_BYTES", 2 * 1024 * 1024, minimum=1024
            ),
        )
        return ProviderHTTPClient(
            provider="ollama-json-inference",
            base_url=self._base_url,
            api_key=None,
            policy=policy,
            allow_unauthenticated_loopback=True,
        )

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        max_tokens: int = 768,
    ) -> dict[str, object]:
        serialized_payload = _validated_inference_input(
            system_prompt=system_prompt,
            user_payload=user_payload,
            max_tokens=max_tokens,
            max_system_prompt_bytes=self._max_system_prompt_bytes,
            max_user_payload_bytes=self._max_user_payload_bytes,
            max_tokens_limit=self._max_tokens,
        )
        request_payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": serialized_payload},
            ],
            "stream": False,
            "think": False,
            "format": self._output_schema or "json",
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        result = self._client.post_json(self._path, request_payload)
        payload = getattr(result, "payload", result)
        content = _ollama_response_content(payload)
        if len(content) > self._max_output_chars:
            raise RuntimeError("inference_output_too_large")
        try:
            decoded = json.loads(content, parse_constant=_reject_constant)
        except (TypeError, ValueError, RecursionError):
            raise RuntimeError("inference_output_invalid_json") from None
        if not isinstance(decoded, dict):
            raise RuntimeError("inference_output_object_required")
        self._record_usage(result, payload)
        return decoded

    def _record_usage(self, result: object, payload: object) -> None:
        values = payload if isinstance(payload, Mapping) else {}

        def count(name: str) -> int:
            value = values.get(name, 0)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else 0
            )

        input_tokens = count("prompt_eval_count")
        output_tokens = count("eval_count")
        latency = getattr(result, "latency_ms", 0.0)
        latency_ms = float(latency) if isinstance(latency, (int, float)) else 0.0
        with self._stats_lock:
            self._requests += 1
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._total_tokens += input_tokens + output_tokens
            self._latency_ms += max(latency_ms, 0.0)

    @property
    def identity(self) -> str:
        endpoint = hashlib.sha256(f"{self._base_url}\0{self._path}".encode()).hexdigest()
        return f"ollama:{self._model}@{self._model_revision}|endpoint_sha256={endpoint}"

    @property
    def stats(self) -> dict[str, object]:
        with self._stats_lock:
            return {
                "provider": "ollama",
                "model": self._model,
                "revision": self._model_revision,
                "requests": self._requests,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._total_tokens,
                "latency_ms": round(self._latency_ms, 3),
            }

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_structured_json_provider(
    provider: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    model_revision: str | None = None,
    path: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    json_mode: bool | None = None,
    output_schema: Mapping[str, object] | None = None,
    max_output_chars: int | None = None,
    client: object | None = None,
) -> StructuredJSONProvider:
    """Build one backend-selected cloud or loopback-local JSON provider."""

    selected = (provider or os.getenv("PP_INFERENCE_PROVIDER", "openai-compatible")).strip()
    selected = selected.casefold()
    if selected in {"cloud", "deepseek", "openai", "openai-compatible"}:
        return OpenAICompatibleJSONProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            model_revision=model_revision,
            path=path,
            temperature=temperature,
            top_p=top_p,
            json_mode=json_mode,
            max_output_chars=max_output_chars,
            client=client,
        )
    if selected in {"local", "ollama"}:
        return OllamaJSONProvider(
            host=base_url,
            model=model,
            model_revision=model_revision,
            path=path or "/api/chat",
            output_schema=output_schema,
            max_output_chars=max_output_chars,
            client=client,
        )
    raise ValueError("inference_provider_invalid")


def _validated_inference_input(
    *,
    system_prompt: str,
    user_payload: Mapping[str, object],
    max_tokens: int,
    max_system_prompt_bytes: int,
    max_user_payload_bytes: int,
    max_tokens_limit: int,
) -> str:
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("inference_system_prompt_invalid")
    try:
        system_prompt_bytes = len(system_prompt.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("inference_system_prompt_invalid") from None
    if system_prompt_bytes > max_system_prompt_bytes:
        raise ValueError("inference_system_prompt_too_large")
    if not isinstance(user_payload, Mapping):
        raise TypeError("inference_user_payload_invalid")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not structured_tokens_allowed(max_tokens, max_tokens_limit)
    ):
        raise ValueError("inference_max_tokens_invalid")
    try:
        serialized_payload = json.dumps(
            dict(user_payload),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_bytes = len(serialized_payload.encode("utf-8"))
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeEncodeError,
    ):
        raise ValueError("inference_user_payload_invalid") from None
    if payload_bytes > max_user_payload_bytes:
        raise ValueError("inference_user_payload_too_large")
    return serialized_payload


def _validated_output_schema(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("inference_output_schema_invalid")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("inference_output_schema_invalid") from None
    if not isinstance(decoded, dict):
        raise ValueError("inference_output_schema_invalid")
    return decoded


def _ollama_response_content(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise RuntimeError("inference_response_schema_invalid")
    if payload.get("done") is False:
        raise RuntimeError("inference_finish_reason_not_stop")
    done_reason = payload.get("done_reason")
    if done_reason not in {None, "stop"}:
        raise RuntimeError("inference_finish_reason_not_stop")
    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise RuntimeError("inference_response_schema_invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("inference_response_schema_invalid")
    return content


def _normalize_ollama_base_url(raw: str | None) -> str:
    value = (raw or "http://127.0.0.1:11434").strip()
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("ollama_base_url_invalid") from None
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/")
        or not _is_loopback_hostname(hostname)
    ):
        raise ValueError("ollama_base_url_must_be_loopback")
    if hostname == "0.0.0.0":
        hostname = "127.0.0.1"
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port or 11434}"
    return f"{parsed.scheme}://{authority}"


def _is_loopback_hostname(hostname: str) -> bool:
    lowered = hostname.rstrip(".").casefold()
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered == "0.0.0.0":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _response_content(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise RuntimeError("inference_response_schema_invalid")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise RuntimeError("inference_response_schema_invalid")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise RuntimeError("inference_response_schema_invalid")
    if choice.get("finish_reason") != "stop":
        raise RuntimeError("inference_finish_reason_not_stop")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise RuntimeError("inference_response_schema_invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("inference_response_schema_invalid")
    return content


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _is_pinned_deepseek_endpoint(base_url: str) -> bool:
    """Recognize only official DeepSeek API roots for supplier-specific behavior."""

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").rstrip(".").casefold() == "api.deepseek.com"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") in {"", "/v1"}
        and not parsed.query
        and not parsed.fragment
    )


def _int_env(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name.lower()}_invalid") from None
    if value < minimum:
        raise ValueError(f"{name.lower()}_invalid")
    return value


def _bounded_int_setting(
    explicit: int | None,
    *,
    env_name: str,
    default: int,
    minimum: int,
    maximum: int,
    reason: str,
) -> int:
    raw: object = explicit if explicit is not None else os.getenv(env_name, str(default))
    if isinstance(raw, bool):
        raise ValueError(reason)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    if not minimum <= value <= maximum:
        raise ValueError(reason)
    return value


def _token_limit_setting(
    explicit: int | None,
    *,
    env_name: str,
    default: int,
    reason: str,
) -> int:
    raw: object = explicit if explicit is not None else os.getenv(env_name, str(default))
    if isinstance(raw, bool):
        raise ValueError(reason)
    try:
        value = int(raw)
        validate_structured_token_limit(value)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    return value


def _bounded_float_setting(
    explicit: float | None,
    *,
    env_name: str,
    default: float,
    minimum: float,
    maximum: float,
    reason: str,
) -> float:
    raw: object = explicit if explicit is not None else os.getenv(env_name, str(default))
    if isinstance(raw, bool):
        raise ValueError(reason)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(reason) from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(reason)
    return value


def _boolean_setting(
    explicit: bool | None,
    *,
    env_name: str,
    default: bool,
    reason: str,
) -> bool:
    raw: object = explicit if explicit is not None else os.getenv(env_name, "1" if default else "0")
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(reason)


def _float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name.lower()}_invalid") from None
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name.lower()}_invalid")
    return value
