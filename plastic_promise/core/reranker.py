"""Multi-provider reranker with explicit, observable degradation.

The default chain remains local-first for backward compatibility. Hosted cloud
reranking is opt-in::

    PP_RERANK_PROVIDERS = cloud, original

``original`` is the canonical no-network fallback and preserves the incoming
order and scores. The historical ``cosine`` name remains an alias, but it no
longer claims to compute cosine similarity.

Successful provider scores are blended with the upstream retrieval score::

    final = max(original * 0.5, 0.6 * provider + 0.4 * original)

Only stable reason codes and bounded provider metadata are exposed through
``last_diagnostics``. Queries, documents, API keys, response bodies, and raw
exception messages are never recorded there.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from plastic_promise.core.cost_telemetry import TokenCostPolicy

logger = logging.getLogger("plastic-promise.reranker")

# Keep the legacy default spelling because a few callers inspect the private
# list. Dispatch canonicalizes ``cosine`` to the truthful ``original`` name.
_DEFAULT_PROVIDERS = ["ollama", "cosine"]
_DEFAULT_PROVIDER_TIMEOUT = 5.0
_DEFAULT_TOTAL_TIMEOUT = 10.0
_DEFAULT_MAX_CANDIDATES = 30
_HARD_MAX_CANDIDATES = 100
_DEFAULT_MAX_DOCUMENT_CHARS = 4_000
_HARD_MAX_DOCUMENT_CHARS = 16_000
_DEFAULT_MAX_QUERY_CHARS = 4_000
_HARD_MAX_QUERY_CHARS = 16_000
_MAX_LEGACY_RESPONSE_BYTES = 2 * 1024 * 1024
_LEGACY_RESPONSE_READ_CHUNK_BYTES = 64 * 1024
_RERANK_CACHE_SIZE = 64
_RERANK_CACHE_TTL = 60.0
_SAFE_REASON_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{0,79}")
_HASHED_REQUEST_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
)
_TOTAL_TOKEN_COST_BASIS = "total_tokens_single_blended_rate"
_COMPONENT_TOKEN_COST_BASIS = "input_output_tokens_single_blended_rate"
_UNKNOWN_TOKEN_COST_BASIS = "unknown"
_TOKEN_COUNT_UNAVAILABLE = "token-count-unavailable"


@dataclass(frozen=True)
class _CloudConfig:
    base_url: str
    path: str
    api_key: str = field(repr=False)
    model: str = ""
    model_revision: str = ""
    timeout_sec: float = _DEFAULT_PROVIDER_TIMEOUT
    total_timeout_sec: float = _DEFAULT_TOTAL_TIMEOUT
    max_retries: int = 2
    max_candidates: int = _DEFAULT_MAX_CANDIDATES
    max_document_chars: int = _DEFAULT_MAX_DOCUMENT_CHARS
    max_query_chars: int = _DEFAULT_MAX_QUERY_CHARS
    cost_policy: TokenCostPolicy = field(
        default_factory=lambda: TokenCostPolicy(None, "USD", "", False)
    )

    @classmethod
    def from_env(cls, *, cloud_enabled: bool = True) -> _CloudConfig:
        # Keep the legacy variable as a cloud-only fallback. A mixed
        # cloud/Ollama chain must use separate model names so cloud fallback
        # never sends a hosted model identifier to the local runtime.
        model = (
            os.environ.get("PP_RERANK_CLOUD_MODEL", "").strip()
            or os.environ.get("PP_RERANK_MODEL", "").strip()
        )
        model_revision = os.environ.get("PP_RERANK_CLOUD_MODEL_REVISION", "").strip()
        if not model_revision:
            model_revision = os.environ.get("PP_RERANK_MODEL_REVISION", model).strip() or model
        return cls(
            base_url=os.environ.get("PP_RERANK_BASE_URL", "").strip(),
            path=os.environ.get("PP_RERANK_PATH", "/rerank").strip(),
            api_key=os.environ.get("PP_RERANK_API_KEY", "").strip(),
            model=model,
            model_revision=model_revision,
            timeout_sec=_float_env(
                "PP_RERANK_TIMEOUT_SEC",
                _float_env("PP_RERANK_TIMEOUT", _DEFAULT_PROVIDER_TIMEOUT, minimum=0.05),
                minimum=0.05,
            ),
            total_timeout_sec=_float_env(
                "PP_RERANK_TOTAL_TIMEOUT_SEC",
                _float_env("PP_RERANK_TOTAL_TIMEOUT", _DEFAULT_TOTAL_TIMEOUT, minimum=0.05),
                minimum=0.05,
            ),
            max_retries=_int_env("PP_RERANK_MAX_RETRIES", 2, minimum=0),
            max_candidates=_int_env(
                "PP_RERANK_MAX_CANDIDATES",
                _DEFAULT_MAX_CANDIDATES,
                minimum=1,
                maximum=_HARD_MAX_CANDIDATES,
            ),
            max_document_chars=_int_env(
                "PP_RERANK_MAX_DOCUMENT_CHARS",
                _DEFAULT_MAX_DOCUMENT_CHARS,
                minimum=1,
                maximum=_HARD_MAX_DOCUMENT_CHARS,
            ),
            max_query_chars=_int_env(
                "PP_RERANK_MAX_QUERY_CHARS",
                _DEFAULT_MAX_QUERY_CHARS,
                minimum=1,
                maximum=_HARD_MAX_QUERY_CHARS,
            ),
            cost_policy=(
                TokenCostPolicy.from_environment(
                    "PP_RERANK",
                    reason_prefix="rerank",
                )
                if cloud_enabled
                else TokenCostPolicy(None, "USD", "", False)
            ),
        )

    def validation_reason(self) -> str:
        if not self.api_key:
            return "cloud_missing_api_key"
        if not self.base_url:
            return "cloud_missing_base_url"
        if not self.model:
            return "cloud_missing_model"
        if not self.path:
            return "cloud_missing_path"
        return ""


@dataclass(frozen=True)
class _ProviderScores:
    scores: dict[int, float]
    attempts: int = 1
    latency_ms: float = 0.0
    request_id: str = ""
    usage: dict[str, int | float | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class _CacheEntry:
    scores: dict[int, float]
    provider: str


class _RerankFailure(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = _stable_reason(reason, default="provider_error")
        super().__init__(self.reason)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject provider redirects before urllib can create a second request."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        raise _RerankFailure(self._reason)

    def http_error_308(self, req, fp, code, msg, headers):
        del req, fp, code, msg, headers
        raise _RerankFailure(self._reason)


def _safe_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
    failure_reason: str,
):
    """Open a legacy provider request without proxies or redirects.

    The legacy providers retain their urllib request contract, but their
    default opener inherits proxy settings and forwards Authorization headers
    across redirects.  A per-request opener keeps the hardening local to these
    opt-in paths and leaves the rest of urllib compatibility intact.
    """

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(failure_reason),
    )
    return opener.open(request, timeout=timeout)


def _read_legacy_response(
    response: object,
    *,
    provider: str,
    deadline: float,
) -> bytes:
    """Read an opt-in urllib response under both size and total-time limits.

    ``HTTPResponse.read()`` otherwise accepts an unbounded body, and a peer
    can keep a connection alive indefinitely by sending small chunks.  The
    compatibility fallback for zero-argument test doubles still enforces the
    final byte limit.
    """

    if time.monotonic() >= deadline:
        raise _RerankFailure(f"{provider}_total_timeout")

    headers = getattr(response, "headers", None)
    get_header = getattr(headers, "get", None)
    if callable(get_header):
        raw_length = get_header("Content-Length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                raise _RerankFailure(f"{provider}_response_invalid") from None
            if content_length < 0:
                raise _RerankFailure(f"{provider}_response_invalid")
            if content_length > _MAX_LEGACY_RESPONSE_BYTES:
                raise _RerankFailure(f"{provider}_response_too_large")

    read = getattr(response, "read", None)
    if not callable(read):
        raise _RerankFailure(f"{provider}_response_invalid")

    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise _RerankFailure(f"{provider}_total_timeout")
        remaining = _MAX_LEGACY_RESPONSE_BYTES - total
        if remaining <= 0:
            raise _RerankFailure(f"{provider}_response_too_large")
        try:
            chunk = read(min(_LEGACY_RESPONSE_READ_CHUNK_BYTES, remaining + 1))
        except TypeError:
            # A few legacy test doubles expose only ``read()``.  Keep that
            # compatibility while checking the complete returned body.
            chunk = read()
            if not isinstance(chunk, (bytes, bytearray)):
                raise _RerankFailure(f"{provider}_response_invalid") from None
            if len(chunk) > remaining:
                raise _RerankFailure(f"{provider}_response_too_large") from None
            chunks.append(bytes(chunk))
            break
        if chunk in (b"", ""):
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise _RerankFailure(f"{provider}_response_invalid")
        total += len(chunk)
        if total > _MAX_LEGACY_RESPONSE_BYTES:
            raise _RerankFailure(f"{provider}_response_too_large")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


_rerank_cache: dict[str, tuple[_CacheEntry, float]] = {}
_rerank_cache_lock = threading.Lock()
_shared_cloud_clients: dict[str, Any] = {}
_shared_cloud_clients_lock = threading.Lock()


class MultiProviderReranker:
    """Unified reranker with a configurable provider fallback chain.

    ``http_client`` is an injection point for isolated tests. Production cloud
    calls lazily construct the shared :class:`ProviderHTTPClient` only after
    configuration has passed fail-closed validation.
    """

    def __init__(self, *, http_client: Any | None = None) -> None:
        disabled = os.environ.get("PP_RERANK_DISABLED", "0") == "1"
        provider_str = os.environ.get("PP_RERANK_PROVIDERS", ",".join(_DEFAULT_PROVIDERS))
        self._providers = [p.strip().casefold() for p in provider_str.split(",") if p.strip()] or [
            "cosine"
        ]
        supported = {"cloud", "jina", "siliconflow", "ollama", "original"}
        if any(_canonical_provider(provider) not in supported for provider in self._providers):
            raise ValueError("rerank_provider_invalid")
        configured = {_canonical_provider(provider) for provider in self._providers}
        explicit_ollama_model = os.environ.get("PP_RERANK_OLLAMA_MODEL", "").strip()
        legacy_model = os.environ.get("PP_RERANK_MODEL", "").strip()
        self._ollama_model = explicit_ollama_model or (
            "qwen2.5:3b"
            if {"cloud", "ollama"}.issubset(configured)
            else legacy_model or "qwen2.5:3b"
        )
        self._disabled = disabled
        self._cloud = _CloudConfig.from_env(cloud_enabled="cloud" in configured)
        self._provider_timeout = self._cloud.timeout_sec
        self._total_timeout = self._cloud.total_timeout_sec
        self._http_client = http_client
        self._last_provider = "none"
        self._last_error = ""
        self._last_diagnostics: dict[str, Any] = _diagnostics(
            provider="none",
            status="not_run",
            degraded=False,
            reason="",
            candidate_count=0,
            reranked_count=0,
        )

    def rerank(
        self,
        query: str,
        candidates: list,
        top_k: int | None = None,
    ) -> list:
        """Rerank ContextItem-like candidates while preserving the old API."""

        if self._disabled:
            self._record_skipped("disabled", candidates)
            return _original_candidates(candidates, top_k)
        if len(candidates) <= 1:
            self._record_skipped("insufficient_candidates", candidates)
            return _original_candidates(candidates, top_k)

        scores = self._scores_for(query, candidates)
        if scores is None:
            return _original_candidates(candidates, top_k)
        return self._apply_rerank_scores(candidates, scores, top_k)

    def rerank_tuples(
        self,
        query: str,
        candidates: list[tuple[str, str, float]],
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Rerank tuple candidates, preserving the backward-compatible result."""

        if self._disabled:
            self._record_skipped("disabled", candidates)
            return _original_tuples(candidates, top_k)
        if len(candidates) <= 1:
            self._record_skipped("insufficient_candidates", candidates)
            return _original_tuples(candidates, top_k)

        scores = self._scores_for(query, candidates)
        if scores is None:
            return _original_tuples(candidates, top_k)
        return _tuple_scores(candidates, scores, top_k)

    @property
    def last_provider(self) -> str:
        return self._last_provider

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_diagnostics(self) -> dict[str, Any]:
        return _copy_diagnostics(self._last_diagnostics)

    @property
    def last_model_identity(self) -> str:
        """Bounded identity for the provider that produced the last result."""

        if self._last_provider == "cloud":
            revision = self._cloud.model_revision or self._cloud.model
            return f"cloud:{self._cloud.model}@{revision}"
        if self._last_provider == "ollama":
            return f"ollama:{self._ollama_model}"
        if self._last_provider == "jina":
            return "jina:jina-reranker-v2-base-multilingual"
        if self._last_provider == "siliconflow":
            return "siliconflow:BAAI/bge-reranker-v2-m3"
        return self._last_provider

    def _scores_for(self, query: str, candidates: list) -> dict[int, float] | None:
        providers = tuple(_canonical_provider(name) for name in self._providers)
        cache_key = _cache_key(
            query,
            candidates,
            model=self._cache_model_identity(providers),
            providers=providers,
            config=self._cache_config_identity(providers),
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            self._last_provider = cached.provider
            self._last_error = ""
            self._last_diagnostics = _diagnostics(
                provider=cached.provider,
                status="cache_hit",
                degraded=False,
                reason="",
                candidate_count=len(candidates),
                reranked_count=len(cached.scores),
                cache_hit=True,
            )
            return dict(cached.scores)

        deadline = time.monotonic() + self._total_timeout
        failures: list[str] = []
        for configured_name in self._providers:
            provider_name = _canonical_provider(configured_name)
            if provider_name == "original":
                reason = failures[-1] if failures else "original_configured"
                self._record_original(reason, candidates, failed=bool(failures))
                return None
            if time.monotonic() >= deadline:
                failures.append("rerank_total_timeout")
                break

            handler = getattr(self, f"_rerank_{provider_name}", None)
            if handler is None:
                failures.append("unknown_provider")
                continue
            try:
                raw_result = handler(query, candidates, deadline)
                result = (
                    raw_result
                    if isinstance(raw_result, _ProviderScores)
                    else _ProviderScores(scores=dict(raw_result or {}))
                )
                if not result.scores:
                    raise _RerankFailure(f"{provider_name}_empty_results")
            except Exception as exc:
                failures.append(_provider_failure_reason(provider_name, exc))
                continue

            self._last_provider = provider_name
            self._last_error = ""
            self._last_diagnostics = _diagnostics(
                provider=provider_name,
                status="success",
                degraded=False,
                reason="",
                attempts=result.attempts,
                latency_ms=result.latency_ms,
                request_id=result.request_id,
                usage=result.usage,
                candidate_count=len(candidates),
                reranked_count=len(result.scores),
            )
            _cache_set(cache_key, _CacheEntry(dict(result.scores), provider_name))
            return dict(result.scores)

        reason = failures[-1] if failures else "all_providers_failed"
        self._record_original(reason, candidates, failed=True)
        return None

    def _record_skipped(self, reason: str, candidates: Sequence[Any]) -> None:
        provider = "disabled" if reason == "disabled" else "original"
        self._last_provider = provider
        self._last_error = ""
        self._last_diagnostics = _diagnostics(
            provider=provider,
            status="skipped",
            degraded=False,
            reason=reason,
            candidate_count=len(candidates),
            reranked_count=0,
        )

    def _record_original(
        self,
        reason: str,
        candidates: Sequence[Any],
        *,
        failed: bool,
    ) -> None:
        stable_reason = _stable_reason(reason, default="all_providers_failed")
        self._last_provider = "original"
        self._last_error = stable_reason if failed else ""
        self._last_diagnostics = _diagnostics(
            provider="original",
            status="degraded" if failed else "skipped",
            degraded=failed,
            reason=stable_reason,
            candidate_count=len(candidates),
            reranked_count=0,
        )

    def _cache_model_identity(self, providers: Sequence[str]) -> str:
        identities: list[str] = []
        for provider in providers:
            if provider == "cloud":
                identities.append(
                    f"cloud:{self._cloud.model}@{self._cloud.model_revision or self._cloud.model}"
                )
            elif provider == "ollama":
                identities.append(f"ollama:{self._ollama_model}")
            elif provider == "jina":
                identities.append("jina:jina-reranker-v2-base-multilingual")
            elif provider == "siliconflow":
                identities.append("siliconflow:BAAI/bge-reranker-v2-m3")
            else:
                identities.append(provider)
        return "|".join(identities)

    def _cache_config_identity(self, providers: Sequence[str]) -> dict[str, object]:
        if "cloud" not in providers:
            return {}
        identity = _cloud_config_identity(self._cloud)
        identity["credential_fingerprint"] = hashlib.sha256(
            self._cloud.api_key.encode("utf-8")
        ).hexdigest()
        return identity

    # Provider implementations

    def _rerank_cloud(self, query, candidates, deadline) -> _ProviderScores:
        config_reason = self._cloud.validation_reason()
        if config_reason:
            raise _RerankFailure(config_reason)
        if time.monotonic() >= deadline:
            raise _RerankFailure("cloud_total_timeout")

        limited = candidates[: self._cloud.max_candidates]
        payload = {
            "model": self._cloud.model,
            "query": str(query)[: self._cloud.max_query_chars],
            "documents": [
                _candidate_content(candidate)[: self._cloud.max_document_chars]
                for candidate in limited
            ],
            "top_n": len(limited),
        }
        response = self._cloud_client().post_json(self._cloud.path, payload, deadline=deadline)
        if time.monotonic() >= deadline:
            raise _RerankFailure("cloud_total_timeout")
        response_payload = _response_value(response, "payload", response)
        scores = _validate_cloud_scores(response_payload, len(limited))
        return _ProviderScores(
            scores=scores,
            attempts=_safe_attempts(_response_value(response, "attempts", 1)),
            latency_ms=_safe_latency(_response_value(response, "latency_ms", 0.0)),
            request_id=_safe_request_id(_response_value(response, "request_id", "")),
            usage=_cloud_usage_with_cost(
                _response_value(response, "usage", {}),
                self._cloud.cost_policy,
            ),
        )

    def _cloud_client(self):
        if self._http_client is not None:
            return self._http_client
        return _get_shared_cloud_client(self._cloud)

    def _rerank_jina(self, query, candidates, deadline):
        """Jina AI Reranker API (legacy opt-in provider)."""

        url = "https://api.jina.ai/v1/rerank"
        documents = [_candidate_content(c)[:500] for c in candidates[:30]]
        payload = json.dumps(
            {
                "model": "jina-reranker-v2-base-multilingual",
                "query": query[:500],
                "documents": documents,
                "top_n": min(len(candidates), 20),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers)
        resp = _safe_urlopen(
            req,
            timeout=_bounded_timeout(deadline, self._provider_timeout),
            failure_reason="jina_redirect_blocked",
        )
        data = json.loads(
            _read_legacy_response(resp, provider="jina", deadline=deadline).decode("utf-8")
        )
        scores = {}
        for result in data.get("results", []):
            index = result.get("index", -1)
            if 0 <= index < len(candidates):
                scores[index] = result.get("relevance_score", 0.5)
        return scores

    def _rerank_siliconflow(self, query, candidates, deadline):
        """SiliconFlow Reranker API (legacy opt-in provider)."""

        url = "https://api.siliconflow.cn/v1/rerank"
        documents = [_candidate_content(c)[:500] for c in candidates[:30]]
        payload = json.dumps(
            {
                "model": "BAAI/bge-reranker-v2-m3",
                "query": query[:500],
                "documents": documents,
                "top_n": min(len(candidates), 20),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("SILICONFLOW_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=payload, headers=headers)
        resp = _safe_urlopen(
            req,
            timeout=_bounded_timeout(deadline, self._provider_timeout),
            failure_reason="siliconflow_redirect_blocked",
        )
        data = json.loads(
            _read_legacy_response(
                resp,
                provider="siliconflow",
                deadline=deadline,
            ).decode("utf-8")
        )
        scores = {}
        for result in data.get("results", []):
            index = result.get("index", -1)
            if 0 <= index < len(candidates):
                scores[index] = result.get("relevance_score", 0.5)
        return scores

    def _rerank_ollama(self, query, candidates, deadline):
        """Local Ollama generation model via ``/api/generate``."""

        host = _normalize_ollama_host(os.environ.get("OLLAMA_HOST"))
        model = self._ollama_model
        limited_candidates = candidates[:30]
        passages = "\n\n".join(
            f"[{i}] {_candidate_content(c)[:300]}" for i, c in enumerate(limited_candidates)
        )
        prompt = (
            f"Query: {query[:200]}\n\n"
            f"Rate each passage relevance from 0 to 100:\n\n{passages}\n\n"
            f"Return only valid JSON with exactly {len(limited_candidates)} numeric scores, "
            f'one per passage, no markdown and no ellipsis: {{"scores":[0]}}'
        )
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
            }
        ).encode("utf-8")
        url = f"{host}/api/generate"
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        resp = _safe_urlopen(
            req,
            timeout=_bounded_timeout(deadline, self._provider_timeout),
            failure_reason="ollama_redirect_blocked",
        )
        raw = json.loads(
            _read_legacy_response(resp, provider="ollama", deadline=deadline).decode("utf-8")
        ).get("response", "")
        return _parse_ollama_score_response(raw, len(limited_candidates))

    def _rerank_original(self, query, candidates, deadline):
        """Return upstream scores unchanged; no similarity is fabricated."""

        del query, deadline
        return {
            index: _candidate_relevance(candidate) for index, candidate in enumerate(candidates)
        }

    def _rerank_cosine(self, query, candidates, deadline):
        """Compatibility alias for the truthful ``original`` fallback."""

        return self._rerank_original(query, candidates, deadline)

    @staticmethod
    def _apply_rerank_scores(candidates, provider_scores, top_k=None):
        """Blend returned scores; preserve unreturned partial-result scores."""

        for index, item in enumerate(candidates):
            if index not in provider_scores:
                continue
            original = _candidate_relevance(item)
            provider_score = provider_scores[index]
            _set_candidate_relevance(
                candidates,
                index,
                min(1.0, max(original * 0.5, 0.6 * provider_score + 0.4 * original)),
            )

        candidates.sort(key=_candidate_relevance, reverse=True)
        if top_k:
            return candidates[:top_k]
        return candidates


def _candidate_id(candidate) -> str:
    if isinstance(candidate, tuple):
        return str(candidate[0])
    return str(getattr(candidate, "id", candidate))


def _candidate_content(candidate) -> str:
    if isinstance(candidate, tuple):
        return str(candidate[1]) if len(candidate) > 1 else ""
    return str(getattr(candidate, "content", ""))


def _candidate_relevance(candidate) -> float:
    if isinstance(candidate, tuple):
        return float(candidate[2]) if len(candidate) > 2 else 0.0
    return float(getattr(candidate, "relevance", 0.0))


def _set_candidate_relevance(candidates: list, index: int, relevance: float) -> None:
    candidate = candidates[index]
    if isinstance(candidate, tuple):
        candidates[index] = (candidate[0], candidate[1], relevance)
    else:
        candidate.relevance = relevance


def _original_candidates(candidates: list, top_k: int | None) -> list:
    return candidates[:top_k] if top_k else candidates


def _original_tuples(
    candidates: Sequence[tuple[str, str, float]], top_k: int | None
) -> list[tuple[str, float]]:
    values = [(candidate_id, score) for candidate_id, _content, score in candidates]
    return values[:top_k] if top_k else values


def _tuple_scores(candidates, provider_scores, top_k):
    result = []
    for index, (candidate_id, _content, original) in enumerate(candidates):
        if index not in provider_scores:
            final = original
        else:
            provider_score = provider_scores[index]
            final = min(1.0, max(original * 0.5, 0.6 * provider_score + 0.4 * original))
        result.append((candidate_id, final))
    result.sort(key=lambda item: item[1], reverse=True)
    return result[:top_k] if top_k else result


def _canonical_provider(name: str) -> str:
    return "original" if str(name).strip().casefold() == "cosine" else str(name).strip().casefold()


def _normalize_ollama_host(host: str | None) -> str:
    raw = (host or "http://127.0.0.1:11434").strip().rstrip("/")
    raw = raw.replace("0.0.0.0", "127.0.0.1")
    if "://" in raw:
        return raw
    if ":" in raw:
        return f"http://{raw}"
    return f"http://{raw}:11434"


def _parse_ollama_score_response(raw: str, expected_count: int) -> dict[int, float]:
    values = _extract_json_score_array(raw)
    if values is None:
        target = raw.split("scores", 1)[1] if "scores" in raw else raw
        values = re.findall(r"-?\d+(?:\.\d+)?", target)

    scores: dict[int, float] = {}
    for index, value in enumerate(values[:expected_count]):
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            scores[index] = max(0.0, min(1.0, score / 100.0))
    return scores


def _extract_json_score_array(raw: str) -> list | None:
    for candidate in _json_candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            scores = parsed.get("scores")
            if isinstance(scores, list):
                return scores
        if isinstance(parsed, list):
            return parsed
    return None


def _json_candidates(raw: str) -> list[str]:
    candidates = [raw]
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.index("{") : raw.rindex("}") + 1])
    if "[" in raw and "]" in raw:
        candidates.append(raw[raw.index("[") : raw.rindex("]") + 1])
    return candidates


def _validate_cloud_scores(payload: object, candidate_count: int) -> dict[int, float]:
    if not isinstance(payload, Mapping):
        raise _RerankFailure("cloud_invalid_payload")
    results = payload.get("results")
    if not isinstance(results, list):
        raise _RerankFailure("cloud_invalid_results")
    if not results:
        raise _RerankFailure("cloud_empty_results")

    scores: dict[int, float] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise _RerankFailure("cloud_invalid_result")
        index = result.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise _RerankFailure("cloud_invalid_index")
        if index < 0 or index >= candidate_count:
            raise _RerankFailure("cloud_index_out_of_range")
        if index in scores:
            raise _RerankFailure("cloud_duplicate_index")
        score = result.get("score", result.get("relevance_score"))
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise _RerankFailure("cloud_invalid_score")
        numeric_score = float(score)
        if not math.isfinite(numeric_score):
            raise _RerankFailure("cloud_invalid_score")
        if numeric_score < 0.0 or numeric_score > 1.0:
            raise _RerankFailure("cloud_score_out_of_range")
        scores[index] = numeric_score
    return scores


def _response_value(response: object, name: str, default: Any) -> Any:
    if isinstance(response, Mapping):
        return response.get(name, default)
    return getattr(response, name, default)


def _provider_failure_reason(provider: str, error: Exception) -> str:
    if isinstance(error, _RerankFailure):
        return error.reason
    reason = (
        getattr(error, "reason", None)
        or getattr(error, "reason_code", None)
        or getattr(error, "code", None)
    )
    if isinstance(reason, str):
        stable = _stable_reason(reason, default="")
        if stable:
            return stable if stable.startswith(f"{provider}_") else f"{provider}_{stable}"
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        return (
            f"{provider}_timeout"
            if isinstance(error, TimeoutError)
            else f"{provider}_transport_error"
        )
    return f"{provider}_provider_error"


def _stable_reason(value: object, *, default: str) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized if _SAFE_REASON_RE.fullmatch(normalized) else default


def _safe_attempts(value: object) -> int:
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return 0


def _safe_latency(value: object) -> float:
    try:
        latency = float(value)
    except (TypeError, ValueError):
        return 0.0
    return latency if math.isfinite(latency) and latency >= 0.0 else 0.0


def _safe_request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if not request_id:
        return ""
    if _HASHED_REQUEST_ID_RE.fullmatch(request_id):
        return request_id
    digest = hashlib.sha256(request_id.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def _safe_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    usage: dict[str, int | float] = {}
    for key in sorted(_USAGE_FIELDS):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        numeric = float(raw)
        if not math.isfinite(numeric) or numeric < 0.0:
            continue
        usage[key] = int(raw) if isinstance(raw, int) else numeric
    return usage


def _cloud_usage_with_cost(
    value: object,
    policy: TokenCostPolicy,
) -> dict[str, int | float | str | None]:
    usage: dict[str, int | float | str | None] = _safe_usage(value)
    if not policy.configured:
        return usage
    token_count, cost_basis = _usage_token_count(usage)
    if token_count is None:
        telemetry: dict[str, object] = {
            "estimated_cost": None,
            "cost_currency": policy.currency,
            "estimated_cost_usd": None,
            "pricing_revision": policy.pricing_revision,
            "cost_basis": _UNKNOWN_TOKEN_COST_BASIS,
            "cost_limitation": _TOKEN_COUNT_UNAVAILABLE,
        }
    else:
        telemetry = policy.telemetry(token_count, cost_basis=cost_basis)
        if cost_basis == _COMPONENT_TOKEN_COST_BASIS:
            telemetry["cost_limitation"] = "distinct-input-output-rates-not-modeled"
    usage.update(
        {
            "cost": telemetry["estimated_cost"],
            "cost_currency": telemetry["cost_currency"],
            "cost_usd": telemetry["estimated_cost_usd"],
            "pricing_revision": telemetry["pricing_revision"],
            "cost_basis": telemetry["cost_basis"],
            "cost_limitation": telemetry["cost_limitation"],
        }
    )
    return usage


def _usage_token_count(
    usage: Mapping[str, int | float | str | None],
) -> tuple[int | None, str]:
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return int(total), _TOTAL_TOKEN_COST_BASIS

    token_count = 0
    for aliases in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        component_found = False
        for key in aliases:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                token_count += int(value)
                component_found = True
                break
        if not component_found:
            return None, _UNKNOWN_TOKEN_COST_BASIS
    return token_count, _COMPONENT_TOKEN_COST_BASIS


def _safe_diagnostic_usage(value: object) -> dict[str, int | float | str | None]:
    usage: dict[str, int | float | str | None] = _safe_usage(value)
    if not isinstance(value, Mapping):
        return usage
    currency = value.get("cost_currency")
    cost = value.get("cost")
    cost_usd = value.get("cost_usd")
    pricing_revision = value.get("pricing_revision")
    cost_basis = value.get("cost_basis")
    cost_limitation = value.get("cost_limitation")
    if currency not in {"USD", "CNY"}:
        return usage
    if not _valid_optional_cost(cost) or not _valid_optional_cost(cost_usd):
        return usage
    if currency == "USD" and cost != cost_usd:
        return usage
    if currency == "CNY" and cost_usd is not None:
        return usage
    if (
        not isinstance(pricing_revision, str)
        or len(pricing_revision) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in pricing_revision)
    ):
        return usage
    valid_cost_contract = (
        cost_basis in {_TOTAL_TOKEN_COST_BASIS, _COMPONENT_TOKEN_COST_BASIS}
        and cost_limitation == "distinct-input-output-rates-not-modeled"
    ) or (
        cost_basis == _UNKNOWN_TOKEN_COST_BASIS
        and cost is None
        and cost_usd is None
        and cost_limitation == _TOKEN_COUNT_UNAVAILABLE
    )
    if not valid_cost_contract:
        return usage
    usage.update(
        {
            "cost": float(cost) if cost is not None else None,
            "cost_currency": currency,
            "cost_usd": float(cost_usd) if cost_usd is not None else None,
            "pricing_revision": pricing_revision,
            "cost_basis": cost_basis,
            "cost_limitation": cost_limitation,
        }
    )
    return usage


def _valid_optional_cost(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0.0
    )


def _diagnostics(
    *,
    provider: str,
    status: str,
    degraded: bool,
    reason: str,
    attempts: int = 0,
    latency_ms: float = 0.0,
    request_id: str = "",
    usage: Mapping[str, int | float | str | None] | None = None,
    candidate_count: int,
    reranked_count: int,
    cache_hit: bool = False,
) -> dict[str, Any]:
    return {
        "provider": _canonical_provider(provider),
        "status": _stable_reason(status, default="unknown"),
        "degraded": bool(degraded),
        "reason": _stable_reason(reason, default="") if reason else "",
        "attempts": _safe_attempts(attempts),
        "latency_ms": _safe_latency(latency_ms),
        "request_id": _safe_request_id(request_id),
        "usage": _safe_diagnostic_usage(usage or {}),
        "candidate_count": max(int(candidate_count), 0),
        "reranked_count": max(int(reranked_count), 0),
        "cache_hit": bool(cache_hit),
    }


def _copy_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(value)
    copied["usage"] = dict(copied.get("usage") or {})
    return copied


def _cache_key(
    query: str,
    candidates: Sequence[Any],
    *,
    model: str = "",
    providers: Sequence[str] = (),
    config: Mapping[str, object] | None = None,
) -> str:
    candidate_bindings = [
        {
            "id": _candidate_id(candidate),
            "content_hash": hashlib.sha256(
                _candidate_content(candidate).encode("utf-8")
            ).hexdigest(),
        }
        for candidate in candidates
    ]
    identity = {
        "query_hash": hashlib.sha256(str(query).encode("utf-8")).hexdigest(),
        "candidates": candidate_bindings,
        "model": str(model),
        "providers": [_canonical_provider(provider) for provider in providers],
        "config": dict(config or {}),
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> _CacheEntry | None:
    now = time.monotonic()
    with _rerank_cache_lock:
        cached = _rerank_cache.get(key)
        if cached is None:
            return None
        entry, timestamp = cached
        if now - timestamp < _RERANK_CACHE_TTL:
            return entry
        del _rerank_cache[key]
    return None


def _cache_set(key: str, value: _CacheEntry) -> None:
    now = time.monotonic()
    with _rerank_cache_lock:
        if len(_rerank_cache) >= _RERANK_CACHE_SIZE:
            oldest = min(_rerank_cache, key=lambda item: _rerank_cache[item][1])
            del _rerank_cache[oldest]
        _rerank_cache[key] = (value, now)


def _float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return max(default, minimum)
    return max(value, minimum) if math.isfinite(value) else max(default, minimum)


def _int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        value = max(int(os.environ.get(name, str(default))), minimum)
    except (TypeError, ValueError):
        value = max(default, minimum)
    return min(value, maximum) if maximum is not None else value


def _cloud_config_identity(config: _CloudConfig) -> dict[str, object]:
    """Return all non-secret cloud settings that can change cached results."""

    return {
        "base_url": config.base_url,
        "path": config.path,
        "model": config.model,
        "model_revision": config.model_revision,
        "timeout_sec": config.timeout_sec,
        "total_timeout_sec": config.total_timeout_sec,
        "max_retries": config.max_retries,
        "max_candidates": config.max_candidates,
        "max_document_chars": config.max_document_chars,
        "max_query_chars": config.max_query_chars,
    }


def _cloud_client_key(config: _CloudConfig) -> str:
    identity = _cloud_config_identity(config)
    identity["api_key_hash"] = hashlib.sha256(config.api_key.encode("utf-8")).hexdigest()
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _get_shared_cloud_client(config: _CloudConfig):
    key = _cloud_client_key(config)
    with _shared_cloud_clients_lock:
        client = _shared_cloud_clients.get(key)
        if client is not None:
            return client

        from plastic_promise.core.provider_http import ProviderHTTPClient, ProviderHTTPPolicy

        policy = ProviderHTTPPolicy(
            timeout_seconds=config.timeout_sec,
            total_timeout_seconds=config.total_timeout_sec,
            max_retries=config.max_retries,
        )
        client = ProviderHTTPClient(
            provider="rerank",
            base_url=config.base_url,
            api_key=config.api_key,
            policy=policy,
        )
        _shared_cloud_clients[key] = client
        return client


def _reset_shared_cloud_clients() -> None:
    """Close pooled transports and reset circuit state, primarily for shutdown/tests."""

    with _shared_cloud_clients_lock:
        clients = list(_shared_cloud_clients.values())
        _shared_cloud_clients.clear()
    for client in clients:
        close = getattr(client, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:
            logger.warning("cloud reranker client close failed")


def _bounded_timeout(deadline: float, provider_timeout: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("provider deadline exceeded")
    # Never extend a caller-owned deadline with a convenience floor.  urllib
    # accepts a small positive timeout, and the caller's deadline is the
    # authoritative budget for the whole provider chain.
    return min(provider_timeout, remaining)


def cross_encode_rerank(
    query: str,
    candidates: list[tuple[str, str, float]],
    top_k: int = 10,
    **kwargs,
) -> list[tuple[str, float]]:
    """Backward-compatible shim delegating to ``MultiProviderReranker``."""

    del kwargs
    reranker = MultiProviderReranker()
    return reranker.rerank_tuples(query, candidates, top_k=top_k)


atexit.register(_reset_shared_cloud_clients)
