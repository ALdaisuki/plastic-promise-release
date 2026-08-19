"""Authenticated, loopback-only HTTP gateway for cloud inference jobs.

The MCP transport and the operator dashboard are intentionally not reused as a
browser API.  This module exposes a small, project-scoped surface for a
frontend that reaches the server through the existing SSH tunnel.  Provider
credentials and project identity are process configuration; neither can be
selected by a request.

Cloud provider calls are asynchronous from Starlette's point of view.  The
blocking provider implementations run in worker threads, while the SQLite job
store supplies idempotency, leases, expiry and compare-and-swap completion for
multiple devices.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from plastic_promise.client.hot_memory_cache import hot_memory_cache_contract
from plastic_promise.core.backend_inference import (
    BackendInferenceService,
    ClientLocalCandidate,
    ClientLocalRerankPackage,
    ClientLocalRerankResult,
    RerankRequestBinding,
    RerankResult,
    accept_authoritative_client_local_rerank,
    validate_rerank_submission,
)
from plastic_promise.core.execution_plane import current_endpoint_role
from plastic_promise.core.paths import get_db_path, inference_jobs_path_for
from plastic_promise.core.provider_http import ProviderHTTPClient, ProviderHTTPError

if TYPE_CHECKING:
    from starlette.requests import Request

try:
    from plastic_promise.core.inference_jobs import (
        InferenceJobConflictError,
        InferenceJobError,
        InferenceJobNotFoundError,
        InferenceJobStore,
    )
except ImportError:  # pragma: no cover - gives a stable startup error if packaging is incomplete
    InferenceJobError = RuntimeError  # type: ignore[assignment,misc]
    InferenceJobConflictError = RuntimeError  # type: ignore[assignment,misc]
    InferenceJobNotFoundError = RuntimeError  # type: ignore[assignment,misc]
    InferenceJobStore = None  # type: ignore[assignment,misc]


logger = logging.getLogger("plastic-promise.inference-gateway")

GATEWAY_CONTRACT = "inference-gateway/v1"
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,512}\Z")
_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,512}\Z")
_ERROR_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,96}\Z")
_SAFE_ERROR_PREFIXES = (
    "backend_",
    "client_local_",
    "client_vector_",
    "cloud_",
    "embedding_",
    "inference_",
    "input_",
    "prepared_",
    "provided_",
    "provider_",
    "rerank_",
    "reranker_",
    "server_local_",
    "trusted_client_",
)
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_MAX_BODY_BYTES = 4 * 1024 * 1024
_MAX_PACKAGE_BYTES = 3 * 1024 * 1024
_BODY_READ_TIMEOUT_SECONDS = 15
_DEFAULT_MAX_RETAINED_JSON_BYTES = 512 * 1024 * 1024
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 30.0
_PROVIDER_HOST_ALLOWLIST_ENV = "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST"
_CLIENT_VECTOR_IDENTITY_ENV = "PP_INFERENCE_CLIENT_VECTOR_IDENTITY"
_CLIENT_VECTOR_DIMENSION_ENV = "PP_INFERENCE_CLIENT_VECTOR_DIMENSION"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
_CREATE_FIELDS = frozenset(
    {
        "target",
        "request_id",
        "idempotency_key",
        "candidate_set_version",
        "query",
        "model_identity",
        "top_k",
        "items",
    }
)
_COMPLETE_FIELDS = frozenset({"lease_token", "result"})
_RENEW_FIELDS = frozenset({"lease_token"})
_TARGETS = frozenset({"cloud", "client-local"})
_CAPACITY_ERROR_CODES = frozenset(
    {
        "inference_job_project_capacity_exceeded",
        "inference_job_project_retained_json_bytes_exceeded",
        "inference_job_project_retained_rows_exceeded",
    }
)
_TERMINAL_CLOUD_FAILURE_CODES = frozenset({"rerank_provider_policy_revision_mismatch"})


class InferenceGatewayConfigurationError(ValueError):
    """The gateway cannot start without a safe server-owned configuration."""


class InferenceGatewayAccessError(PermissionError):
    """A request failed loopback or bearer-token admission."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class _ClientVectorContract:
    """Server-owned identity required for request-scoped supplied vectors."""

    identity: str
    dimension: int


@dataclass(frozen=True)
class _ClientSuppliedVectorEmbedder:
    """A no-network adapter used only after every request vector is supplied.

    ``BackendInferenceService`` owns the package/binding algorithms, but its
    normal runtime factory may select a local model or construct a cloud
    client.  This adapter deliberately has no implementation for generation,
    so the all-supplied client-local path cannot silently become a model call.
    """

    contract: _ClientVectorContract

    @property
    def dim(self) -> int:
        return self.contract.dimension

    @property
    def model_name(self) -> str:
        return self.contract.identity

    @property
    def index_model_name(self) -> str:
        return self.contract.identity

    def embed(self, _text: str) -> list[float]:
        raise RuntimeError("client_vector_embedding_generation_forbidden")

    def embed_batch(self, _texts: object) -> list[list[float]]:
        raise RuntimeError("client_vector_embedding_generation_forbidden")


@dataclass(frozen=True)
class InferenceGatewaySettings:
    """Validated gateway settings; ``token`` never appears in public payloads."""

    enabled: bool
    project_id: str
    token: str = field(repr=False)
    db_path: Path
    ttl_seconds: int = 900
    lease_seconds: int = 120
    max_concurrency: int = 4
    max_active_jobs: int = 1_000
    retention_seconds: int = 86_400
    max_retained_rows: int | None = None
    max_retained_json_bytes: int = _DEFAULT_MAX_RETAINED_JSON_BYTES
    bind_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise InferenceGatewayConfigurationError("inference_gateway_enabled_invalid")
        if not _is_loopback_host(self.bind_host):
            raise InferenceGatewayConfigurationError("inference_gateway_bind_not_loopback")
        if not isinstance(self.db_path, Path) or "\x00" in str(self.db_path):
            raise InferenceGatewayConfigurationError("inference_gateway_db_path_invalid")
        if _same_path(self.db_path, Path(get_db_path())):
            raise InferenceGatewayConfigurationError("inference_gateway_db_must_be_separate")
        _bounded_setting(
            self.ttl_seconds,
            minimum=30,
            maximum=86_400,
            reason="inference_gateway_ttl_invalid",
        )
        _bounded_setting(
            self.lease_seconds,
            minimum=5,
            maximum=3_600,
            reason="inference_gateway_lease_invalid",
        )
        if self.lease_seconds >= self.ttl_seconds:
            raise InferenceGatewayConfigurationError("inference_gateway_lease_must_precede_ttl")
        _bounded_setting(
            self.max_concurrency,
            minimum=1,
            maximum=_provider_concurrency_limit(),
            reason="inference_gateway_concurrency_invalid",
        )
        _bounded_setting(
            self.max_active_jobs,
            minimum=1,
            maximum=100_000,
            reason="inference_gateway_active_jobs_invalid",
        )
        _bounded_setting(
            self.retention_seconds,
            minimum=3_600,
            maximum=30 * 86_400,
            reason="inference_gateway_retention_invalid",
        )
        if self.max_retained_rows is not None:
            _bounded_setting(
                self.max_retained_rows,
                minimum=1,
                maximum=1_000_000,
                reason="inference_gateway_retained_rows_invalid",
            )
        _bounded_setting(
            self.max_retained_json_bytes,
            minimum=1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
            reason="inference_gateway_retained_json_bytes_invalid",
        )
        if self.enabled:
            if _normalize_project_id(self.project_id) != self.project_id:
                raise InferenceGatewayConfigurationError("inference_gateway_project_missing")
            if not _TOKEN_RE.fullmatch(self.token):
                raise InferenceGatewayConfigurationError("inference_gateway_token_invalid")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, object] | None = None,
        *,
        bind_host: str | None = None,
    ) -> InferenceGatewaySettings:
        env = os.environ if environ is None else environ
        enabled = str(env.get("PP_INFERENCE_GATEWAY", "0")) == "1"
        host = str(
            bind_host
            if bind_host is not None
            else env.get("PP_INFERENCE_GATEWAY_BIND", "127.0.0.1")
        )
        if not _is_loopback_host(host):
            raise InferenceGatewayConfigurationError("inference_gateway_bind_not_loopback")

        project = ""
        for key in (
            "PP_INFERENCE_GATEWAY_PROJECT_ID",
            "PLASTIC_PROJECT_ID",
            "PP_PROJECT_ID",
        ):
            value = _normalize_project_id(env.get(key))
            if value:
                project = value
                break
        token = str(env.get("PP_INFERENCE_GATEWAY_TOKEN") or "")
        if enabled:
            if not project:
                raise InferenceGatewayConfigurationError("inference_gateway_project_missing")
            if not _TOKEN_RE.fullmatch(token):
                raise InferenceGatewayConfigurationError("inference_gateway_token_invalid")

        ttl = _bounded_setting(
            env.get("PP_INFERENCE_GATEWAY_TTL_SEC", 900),
            minimum=30,
            maximum=86_400,
            reason="inference_gateway_ttl_invalid",
        )
        lease = _bounded_setting(
            env.get("PP_INFERENCE_GATEWAY_LEASE_SEC", 120),
            minimum=5,
            maximum=3_600,
            reason="inference_gateway_lease_invalid",
        )
        max_concurrency = _bounded_setting(
            env.get("PP_INFERENCE_GATEWAY_MAX_CONCURRENCY", 4),
            minimum=1,
            maximum=_provider_concurrency_limit(),
            reason="inference_gateway_concurrency_invalid",
        )
        max_active_jobs = _bounded_setting(
            env.get("PP_INFERENCE_GATEWAY_MAX_ACTIVE_JOBS", 1_000),
            minimum=1,
            maximum=100_000,
            reason="inference_gateway_active_jobs_invalid",
        )
        retention_seconds = _bounded_setting(
            env.get("PP_INFERENCE_GATEWAY_RETENTION_SEC", 86_400),
            minimum=3_600,
            maximum=30 * 86_400,
            reason="inference_gateway_retention_invalid",
        )
        raw_retained_rows = env.get("PP_INFERENCE_GATEWAY_MAX_RETAINED_ROWS")
        max_retained_rows = (
            None
            if raw_retained_rows is None or raw_retained_rows == ""
            else _bounded_setting(
                raw_retained_rows,
                minimum=1,
                maximum=1_000_000,
                reason="inference_gateway_retained_rows_invalid",
            )
        )
        max_retained_json_bytes = _bounded_setting(
            env.get(
                "PP_INFERENCE_GATEWAY_MAX_RETAINED_JSON_BYTES",
                _DEFAULT_MAX_RETAINED_JSON_BYTES,
            ),
            minimum=1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
            reason="inference_gateway_retained_json_bytes_invalid",
        )
        canonical_db_path = Path(str(env.get("PLASTIC_DB_PATH") or get_db_path())).expanduser()
        default_job_path = inference_jobs_path_for(canonical_db_path)
        raw_path = str(env.get("PP_INFERENCE_GATEWAY_DB_PATH") or default_job_path).strip()
        if not raw_path or "\x00" in raw_path:
            raise InferenceGatewayConfigurationError("inference_gateway_db_path_invalid")
        job_path = Path(raw_path).expanduser()
        if _same_path(job_path, canonical_db_path):
            raise InferenceGatewayConfigurationError("inference_gateway_db_must_be_separate")
        return cls(
            enabled=enabled,
            project_id=project,
            token=token,
            db_path=job_path,
            ttl_seconds=ttl,
            lease_seconds=lease,
            max_concurrency=max_concurrency,
            max_active_jobs=max_active_jobs,
            retention_seconds=retention_seconds,
            max_retained_rows=max_retained_rows,
            max_retained_json_bytes=max_retained_json_bytes,
            bind_host=host,
        )


@dataclass
class _GatewayRuntime:
    settings: InferenceGatewaySettings
    store: Any
    preparation_service_factory: Callable[[], BackendInferenceService]
    rerank_service_factory: Callable[[], BackendInferenceService]
    provider_slots: asyncio.Semaphore
    _preparation_service: BackendInferenceService | None = None
    _rerank_service: BackendInferenceService | None = None
    _preparation_service_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rerank_service_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)

    def preparation_service(self) -> BackendInferenceService:
        if self._preparation_service is not None:
            return self._preparation_service
        with self._preparation_service_lock:
            if self._preparation_service is None:
                self._preparation_service = self.preparation_service_factory()
        return self._preparation_service

    def rerank_service(self) -> BackendInferenceService:
        if self.rerank_service_factory is self.preparation_service_factory:
            return self.preparation_service()
        if self._rerank_service is not None:
            return self._rerank_service
        with self._rerank_service_lock:
            if self._rerank_service is None:
                self._rerank_service = self.rerank_service_factory()
        return self._rerank_service

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)

        def discard(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            except BaseException:
                logger.warning(
                    "inference gateway background task failed: %s",
                    "background_task_failed",
                )
                return
            if error is not None:
                code = (
                    _safe_code(error) if isinstance(error, Exception) else "background_task_failed"
                )
                logger.warning("inference gateway background task failed: %s", code)

        task.add_done_callback(discard)


async def _drain_runtime_tasks(
    runtime: _GatewayRuntime,
    *,
    timeout_seconds: float,
) -> int:
    """Wait for runtime-owned work, including tasks spawned while draining.

    A provider call running in ``asyncio.to_thread`` cannot be force-stopped.
    Shutdown therefore gives the owning task a bounded opportunity to persist
    the provider result.  Over-deadline work remains durable but may be retried
    after the event loop or process stops; that retry can repeat external
    billing because provider calls and SQLite commits are not one transaction.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    current = asyncio.current_task()
    while True:
        pending = {
            task for task in runtime._background_tasks if task is not current and not task.done()
        }
        if not pending:
            return 0
        remaining = deadline - loop.time()
        if remaining <= 0.0:
            return len(pending)
        await asyncio.wait(pending, timeout=remaining)


def _cloud_recovery_poll_interval(lease_seconds: int) -> float:
    """Poll often enough to reclaim an inherited lease soon after expiry."""

    return max(1.0, min(float(lease_seconds) / 2.0, 30.0))


async def _run_cloud_recovery_worker(
    runtime: _GatewayRuntime,
    *,
    max_jobs: int,
    stop: asyncio.Event,
) -> None:
    """Repeat the bounded cloud-only recovery cycle until shutdown begins."""

    poll_interval = _cloud_recovery_poll_interval(runtime.settings.lease_seconds)
    while not stop.is_set():
        try:
            claimed = await _recover_pending_cloud_jobs(runtime, max_jobs=max_jobs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cloud inference recovery cycle failed: %s", _safe_code(exc))
            claimed = 0
        if claimed >= max_jobs:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            continue


def _create_gateway_runtime(
    settings: InferenceGatewaySettings,
    *,
    service_factory: Callable[[], BackendInferenceService] | None,
    rerank_service_factory: Callable[[], BackendInferenceService] | None,
    store_factory: Callable[[Path], Any] | None,
) -> _GatewayRuntime:
    # The legacy gateway owns BackendInferenceService factories and therefore
    # is not an inference execution plane.  In the composed deployment the
    # server may schedule a governed compute-node lease, but it must not
    # instantiate this legacy cloud/local executor.  Keep the old API
    # available for isolated library callers and tests with no endpoint role;
    # an explicitly launched server fails closed until its private-node lease
    # adapter is wired in.
    if current_endpoint_role() == "pp-server-backend":
        raise InferenceGatewayConfigurationError("inference_requires_compute_node")
    if InferenceJobStore is None:
        raise InferenceGatewayConfigurationError("inference_job_store_unavailable")
    try:
        store = (
            _default_store_factory(settings)
            if store_factory is None
            else store_factory(settings.db_path)
        )
    except (InferenceJobError, OSError, sqlite3.Error, ValueError) as exc:
        logger.error("inference gateway store unavailable: %s", _safe_code(exc))
        raise InferenceGatewayConfigurationError("inference_gateway_store_unavailable") from None

    preparation_factory = service_factory or BackendInferenceService.from_runtime
    if rerank_service_factory is not None:
        rerank_factory = rerank_service_factory
    elif service_factory is not None:
        # Existing embedders injected by tests/hosts often also carry their
        # deterministic reranker. Production defaults remain split below.
        rerank_factory = preparation_factory
    else:
        rerank_factory = BackendInferenceService.from_rerank_runtime
    return _GatewayRuntime(
        settings=settings,
        store=store,
        preparation_service_factory=preparation_factory,
        rerank_service_factory=rerank_factory,
        provider_slots=asyncio.Semaphore(settings.max_concurrency),
    )


def create_inference_gateway_routes(
    settings: InferenceGatewaySettings,
    *,
    service_factory: Callable[[], BackendInferenceService] | None = None,
    rerank_service_factory: Callable[[], BackendInferenceService] | None = None,
    store_factory: Callable[[Path], Any] | None = None,
    _runtime: _GatewayRuntime | None = None,
) -> list[Route]:
    """Return the gateway routes, or no routes when its feature gate is off."""

    if not settings.enabled:
        return []
    runtime = _runtime or _create_gateway_runtime(
        settings,
        service_factory=service_factory,
        rerank_service_factory=rerank_service_factory,
        store_factory=store_factory,
    )

    async def capabilities(request: Request) -> JSONResponse:
        try:
            _admit(request, settings)
            embedding_provider = os.getenv("EMBEDDER_PROVIDER", "").strip().casefold()
            embedding_model = os.getenv("EMBEDDER_MODEL", "text-embedding-v4").strip()
            try:
                embedding_dimension = int(
                    os.getenv("PP_EMBEDDING_DIM") or os.getenv("EMBEDDER_DIMENSION") or "1024"
                )
            except ValueError:
                embedding_dimension = 0
            rerank_providers = [
                value.strip().casefold()
                for value in os.getenv("PP_RERANK_PROVIDERS", "cosine").split(",")
                if value.strip()
            ]
            client_vector_contract, client_vector_reason = _client_vector_contract_from_env()
            embedding_ready, embedding_reason = _embedding_readiness_from_env()
            embedding_identity: str | None = None
            if embedding_ready:
                try:
                    runtime_service = await asyncio.to_thread(runtime.preparation_service)
                    embedding_identity = runtime_service.embedding_identity
                    runtime_dimension = runtime_service.embedding_dimension
                    if runtime_dimension != embedding_dimension:
                        embedding_ready = False
                        embedding_reason = "cloud_embedding_dimension_runtime_mismatch"
                        embedding_identity = None
                    else:
                        embedding_dimension = runtime_dimension
                except Exception:
                    embedding_ready = False
                    embedding_reason = "cloud_embedding_runtime_invalid"
            rerank_ready, rerank_reason = _rerank_readiness_from_env()
            vector_contract_ready = client_vector_contract is not None
            cloud_all_supplied_ready = vector_contract_ready and rerank_ready
            cloud_missing_ready = embedding_ready and rerank_ready
            cloud_ready = cloud_all_supplied_ready or cloud_missing_ready
            client_local_all_supplied_ready = vector_contract_ready
            client_local_missing_ready = embedding_ready
            client_local_ready = client_local_all_supplied_ready or client_local_missing_ready
            cloud_reason = ""
            if not cloud_ready:
                cloud_reason = (
                    rerank_reason
                    if not rerank_ready
                    else (client_vector_reason or embedding_reason)
                )
            client_local_reason = ""
            if not client_local_ready:
                client_local_reason = client_vector_reason or embedding_reason
            all_supplied_identity = (
                client_vector_contract.identity if client_vector_contract is not None else None
            )
            all_supplied_dimension = (
                client_vector_contract.dimension if client_vector_contract is not None else None
            )
            shared_embedding_identity = (
                all_supplied_identity
                if all_supplied_identity is not None and all_supplied_identity == embedding_identity
                else None
            )
            return _response(
                request,
                {
                    "contract": GATEWAY_CONTRACT,
                    "project_id": settings.project_id,
                    "targets": {
                        "cloud": {
                            "enabled": cloud_ready,
                            "ready": cloud_ready,
                            "configuration_valid": cloud_ready,
                            "live_verified": False,
                            "readiness_scope": "input-dependent",
                            "reason": cloud_reason,
                            "embedding_provider": embedding_provider,
                            "embedding_model": embedding_model,
                            "embedding_dimension": all_supplied_dimension or embedding_dimension,
                            "embedding_identity": shared_embedding_identity,
                            "rerank_providers": rerank_providers,
                            "all_supplied_embeddings": {
                                "ready": cloud_all_supplied_ready,
                                "reason": (
                                    ""
                                    if cloud_all_supplied_ready
                                    else (client_vector_reason or rerank_reason)
                                ),
                                "requires_embedding": False,
                                "requires_rerank": True,
                                "embedding_dimension": all_supplied_dimension,
                                "embedding_identity": all_supplied_identity,
                            },
                            "missing_embeddings": {
                                "ready": cloud_missing_ready,
                                "reason": (
                                    ""
                                    if cloud_missing_ready
                                    else (embedding_reason or rerank_reason)
                                ),
                                "requires_embedding": True,
                                "requires_rerank": True,
                                "embedding_dimension": embedding_dimension,
                                "embedding_identity": embedding_identity,
                            },
                            "mixed_embeddings": {
                                "ready": cloud_missing_ready,
                                "reason": (
                                    ""
                                    if cloud_missing_ready
                                    else (embedding_reason or rerank_reason)
                                ),
                                "requires_embedding": True,
                                "requires_rerank": True,
                                "embedding_dimension": embedding_dimension,
                                "embedding_identity": embedding_identity,
                            },
                        },
                        "client-local": {
                            "enabled": client_local_ready,
                            "ready": client_local_ready,
                            "configuration_valid": client_local_ready,
                            "live_verified": False,
                            "readiness_scope": "input-dependent",
                            "reason": client_local_reason,
                            "index_authority": False,
                            "result_scope": "request-only",
                            "model_identity": {
                                "required": True,
                                "binding": "idempotency-and-package",
                                "immutable_revision_recommended": True,
                            },
                            "embedding_dimension": all_supplied_dimension or embedding_dimension,
                            "embedding_identity": shared_embedding_identity,
                            "all_supplied_embeddings": {
                                "ready": client_local_all_supplied_ready,
                                "reason": (
                                    "" if client_local_all_supplied_ready else client_vector_reason
                                ),
                                "requires_embedding": False,
                                "requires_rerank": False,
                                "embedding_dimension": all_supplied_dimension,
                                "embedding_identity": all_supplied_identity,
                            },
                            "missing_embeddings": {
                                "ready": client_local_missing_ready,
                                "reason": "" if client_local_missing_ready else embedding_reason,
                                "requires_embedding": True,
                                "requires_rerank": False,
                                "embedding_dimension": embedding_dimension,
                                "embedding_identity": embedding_identity,
                            },
                            "mixed_embeddings": {
                                "ready": client_local_missing_ready,
                                "reason": "" if client_local_missing_ready else embedding_reason,
                                "requires_embedding": True,
                                "requires_rerank": False,
                                "embedding_dimension": embedding_dimension,
                                "embedding_identity": embedding_identity,
                            },
                        },
                    },
                    "input_policy": {
                        "embedding_optional": True,
                        "missing_embedding": (
                            "backend-fills" if embedding_ready else "hosted-cloud-required"
                        ),
                        "client_vectors": "request-scoped-only",
                        "all_supplied_client_vectors": "server-contract-required",
                    },
                    "client_cache": hot_memory_cache_contract(),
                    "server_local": {"enabled": False, "reason": "server_local_model_disabled"},
                    "jobs": {
                        "ttl_seconds": settings.ttl_seconds,
                        "lease_seconds": settings.lease_seconds,
                        "max_active_jobs": settings.max_active_jobs,
                        "max_retained_rows": (
                            settings.max_retained_rows
                            if settings.max_retained_rows is not None
                            else min(settings.max_active_jobs * 4, 1_000_000)
                        ),
                        "max_retained_json_bytes": settings.max_retained_json_bytes,
                        "retention_seconds": settings.retention_seconds,
                        "provider_concurrency": settings.max_concurrency,
                        "idempotency": "project-plus-key",
                        "async_submission": {
                            "supported": True,
                            "request_header": "Prefer: respond-async",
                            "poll_by": "idempotency_key_hash",
                        },
                        "startup_recovery": {
                            "enabled": True,
                            "target": "cloud",
                            "project_scoped": True,
                            "client_local_claimed": False,
                            "max_jobs": min(
                                settings.max_concurrency,
                                settings.max_active_jobs,
                            ),
                            "overflow": "later-recovery-cycle-or-client-retry",
                        },
                    },
                    "network": {
                        "bind_host": settings.bind_host,
                        "public": False,
                        "browser_direct": False,
                        "trusted_client_or_same_origin_proxy_required": True,
                    },
                },
            )
        except Exception as exc:
            return _error_response(request, exc)

    async def create_job(request: Request) -> JSONResponse:
        try:
            _admit(request, settings)
            respond_async = _prefers_async(request)
            payload = await _json_body(request)
            _require_fields(payload, _CREATE_FIELDS)
            target = payload.get("target", "cloud")
            if target not in _TARGETS:
                raise ValueError("inference_target_invalid")
            raw_model_identity = payload.get("model_identity")
            if target == "client-local":
                model_identity = _bounded_model_identity(raw_model_identity)
            else:
                if raw_model_identity is not None:
                    raise ValueError("client_local_model_identity_not_applicable")
                model_identity = None
            query = payload.get("query")
            items = payload.get("items")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("rerank_query_invalid")
            if not isinstance(items, list):
                raise ValueError("input_items_sequence_required")
            supplied_request_id = payload.get("request_id")
            request_id = (
                f"req_{uuid.uuid4().hex}" if supplied_request_id is None else supplied_request_id
            )
            idempotency_key = payload.get("idempotency_key")
            candidate_set_version = payload.get("candidate_set_version")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError("rerank_idempotency_key_invalid")
            if idempotency_key != idempotency_key.strip():
                raise ValueError("rerank_idempotency_key_invalid")
            if not isinstance(candidate_set_version, str) or not candidate_set_version.strip():
                raise ValueError("rerank_candidate_set_version_invalid")
            top_k = payload.get("top_k")
            if top_k is not None and (
                isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0
            ):
                raise ValueError("rerank_top_k_invalid")

            validate_rerank_submission(
                payloads=items,
                query=query,
                project_id=settings.project_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                candidate_set_version=candidate_set_version,
                top_k=top_k,
            )

            idempotency_key_hash = _canonical_sha256(
                {"project_id": settings.project_id, "idempotency_key": idempotency_key}
            )
            request_hash = _canonical_sha256(
                {
                    "candidate_set_version": candidate_set_version,
                    "items": items,
                    "policy_revision": _gateway_policy_revision(),
                    "query": query,
                    "model_identity": model_identity,
                    "target": target,
                    "top_k": top_k,
                }
            )
            disposition, reservation_token = await asyncio.to_thread(
                _store_reserve,
                runtime.store,
                project_id=settings.project_id,
                idempotency_key_hash=idempotency_key_hash,
                request_hash=request_hash,
                target=target,
                ttl_seconds=settings.ttl_seconds,
            )
            if disposition == "preparing":
                return _response(
                    request,
                    {
                        "contract": GATEWAY_CONTRACT,
                        "status": "preparing",
                        "target": target,
                        "idempotency_key_hash": idempotency_key_hash,
                        "poll_path": _poll_path(idempotency_key_hash),
                    },
                    status_code=202,
                )
            if disposition == "existing":
                existing = await asyncio.to_thread(
                    _store_get_by_reservation,
                    runtime.store,
                    settings.project_id,
                    idempotency_key_hash,
                )
                if existing is None:
                    raise RuntimeError("inference_job_store_inconsistent")
                if _record_target(existing) == "cloud" and _record_status(existing) == "pending":
                    _require_cloud_readiness(
                        requires_embedding=False,
                        requires_rerank=True,
                    )
                    authoritative_binding = _binding_from_mapping(_record_binding(existing))
                    authoritative_package = _package_from_mapping(_record_package(existing))
                    if respond_async:
                        cloud_task = asyncio.create_task(
                            _run_cloud_job(
                                runtime,
                                existing,
                                authoritative_binding,
                                authoritative_package,
                            )
                        )
                        runtime.track(cloud_task)
                    else:
                        existing = await _run_cloud_job(
                            runtime,
                            existing,
                            authoritative_binding,
                            authoritative_package,
                        )
                return _response(
                    request,
                    _public_record(existing),
                    status_code=(
                        202
                        if respond_async and _record_status(existing) in {"pending", "leased"}
                        else _status_for_record(existing, created=False)
                    ),
                )
            if disposition != "reserved" or not reservation_token:
                raise RuntimeError("inference_job_reservation_invalid")

            all_embeddings_supplied = _all_embeddings_supplied(items)
            try:
                if all_embeddings_supplied:
                    _require_client_vector_contract()
                _require_cloud_readiness(
                    requires_embedding=not all_embeddings_supplied,
                    requires_rerank=target == "cloud",
                )
            except Exception:
                await asyncio.to_thread(
                    _store_release_reservation,
                    runtime.store,
                    settings.project_id,
                    idempotency_key_hash,
                    reservation_token,
                )
                raise

            if respond_async:
                preparation_task = asyncio.create_task(
                    _prepare_and_dispatch(
                        runtime,
                        target=target,
                        items=items,
                        query=query,
                        request_id=request_id,
                        model_identity=model_identity,
                        idempotency_key=idempotency_key,
                        idempotency_key_hash=idempotency_key_hash,
                        candidate_set_version=candidate_set_version,
                        top_k=top_k,
                        request_hash=request_hash,
                        reservation_token=reservation_token,
                    )
                )
                runtime.track(preparation_task)
                return _response(
                    request,
                    {
                        "contract": GATEWAY_CONTRACT,
                        "status": "preparing",
                        "target": target,
                        "request_id": request_id,
                        "idempotency_key_hash": idempotency_key_hash,
                        "poll_path": _poll_path(idempotency_key_hash),
                        "preference_applied": "respond-async",
                    },
                    status_code=202,
                )

            preparation_task = asyncio.create_task(
                _prepare_submission(
                    runtime,
                    target=target,
                    items=items,
                    query=query,
                    request_id=request_id,
                    model_identity=model_identity,
                    idempotency_key=idempotency_key,
                    idempotency_key_hash=idempotency_key_hash,
                    candidate_set_version=candidate_set_version,
                    top_k=top_k,
                    request_hash=request_hash,
                    reservation_token=reservation_token,
                )
            )
            runtime.track(preparation_task)
            record = await asyncio.shield(preparation_task)
            created = _record_bool(record, "created", default=True)
            if target == "cloud" and (created or _record_status(record) in {"pending", "leased"}):
                # Always use the durable record as authority on retries.  In
                # particular, request_id is not part of input_hash, so a
                # duplicate request must not replace the original binding.
                authoritative_binding = _binding_from_mapping(_record_binding(record))
                authoritative_package = _package_from_mapping(_record_package(record))
                record = await _run_cloud_job(
                    runtime,
                    record,
                    authoritative_binding,
                    authoritative_package,
                )
            elif target == "client-local" and created:
                # Give the creating device a lease; another device can reclaim
                # it after expiry through the explicit lease endpoint.
                leased = await asyncio.to_thread(
                    _store_lease,
                    runtime.store,
                    _record_id(record),
                    settings.project_id,
                    settings.lease_seconds,
                )
                if leased is not None:
                    record, lease_token = leased
                else:
                    lease_token = None
            else:
                lease_token = None
            response_payload = _public_record(record)
            if target == "client-local" and lease_token:
                response_payload["lease_token"] = lease_token
            status = _status_for_record(record, created=created)
            return _response(request, response_payload, status_code=status)
        except Exception as exc:
            return _error_response(request, exc)

    async def get_job_by_key(request: Request) -> JSONResponse:
        idempotency_key_hash = request.path_params.get("idempotency_key_hash")
        try:
            _admit(request, settings)
            if not isinstance(idempotency_key_hash, str) or not _SHA256_RE.fullmatch(
                idempotency_key_hash
            ):
                raise ValueError("inference_job_idempotency_hash_invalid")
            reservation = await asyncio.to_thread(
                _store_get_reservation,
                runtime.store,
                settings.project_id,
                idempotency_key_hash,
            )
            if reservation is None:
                raise InferenceGatewayAccessError(404, "inference_job_not_found")
            if reservation.get("status") == "finalized":
                record = await asyncio.to_thread(
                    _store_get_by_reservation,
                    runtime.store,
                    settings.project_id,
                    idempotency_key_hash,
                )
                if record is None:
                    raise RuntimeError("inference_job_store_inconsistent")
                return _response(
                    request,
                    _public_record(record),
                    status_code=_status_for_record(record, created=False),
                )
            payload = _public_reservation(reservation)
            status = str(reservation.get("status", "unknown"))
            if status in {"reserved", "preparing"}:
                return _response(request, payload, status_code=202)
            if status == "expired":
                return _response(request, payload, status_code=410)
            return _response(request, payload, status_code=409)
        except Exception as exc:
            return _error_response(request, exc)

    async def get_job(request: Request) -> JSONResponse:
        job_id = request.path_params.get("job_id")
        try:
            _admit(request, settings)
            _validate_id(job_id, "inference_job_id_invalid")
            await asyncio.to_thread(_store_expire, runtime.store)
            record = await asyncio.to_thread(_store_get, runtime.store, job_id, settings.project_id)
            if record is None:
                raise InferenceGatewayAccessError(404, "inference_job_not_found")
            return _response(request, _public_record(record))
        except Exception as exc:
            return _error_response(request, exc)

    async def lease_job(request: Request) -> JSONResponse:
        job_id = request.path_params.get("job_id")
        try:
            _admit(request, settings)
            _validate_id(job_id, "inference_job_id_invalid")
            record = await asyncio.to_thread(_store_get, runtime.store, job_id, settings.project_id)
            if record is None:
                raise InferenceGatewayAccessError(404, "inference_job_not_found")
            if _record_target(record) != "client-local":
                raise InferenceGatewayAccessError(403, "inference_cloud_lease_internal")
            try:
                leased = await asyncio.to_thread(
                    _store_lease,
                    runtime.store,
                    job_id,
                    settings.project_id,
                    settings.lease_seconds,
                )
            except InferenceJobError as exc:
                if _safe_code(exc) != "inference_job_lease_active":
                    raise
                current = await asyncio.to_thread(
                    _store_get, runtime.store, job_id, settings.project_id
                )
                return _response(request, _public_record(current), status_code=202)
            if leased is None:
                current = await asyncio.to_thread(
                    _store_get, runtime.store, job_id, settings.project_id
                )
                return _response(request, _public_record(current), status_code=202)
            leased_record, lease_token = leased
            return _response(
                request,
                {**_public_record(leased_record), "lease_token": lease_token},
                status_code=200,
            )
        except Exception as exc:
            return _error_response(request, exc)

    async def complete_job(request: Request) -> JSONResponse:
        job_id = request.path_params.get("job_id")
        try:
            _admit(request, settings)
            _validate_id(job_id, "inference_job_id_invalid")
            payload = await _json_body(request)
            _require_fields(payload, _COMPLETE_FIELDS)
            lease_token = payload.get("lease_token")
            result_payload = payload.get("result")
            if not isinstance(lease_token, str) or not lease_token.strip():
                raise ValueError("inference_lease_token_invalid")
            if not isinstance(result_payload, Mapping):
                raise ValueError("inference_result_mapping_required")
            record = await asyncio.to_thread(_store_get, runtime.store, job_id, settings.project_id)
            if record is None:
                raise InferenceGatewayAccessError(404, "inference_job_not_found")
            if _record_target(record) != "client-local":
                raise ValueError("inference_job_target_mismatch")
            package = _package_from_mapping(_record_package(record))
            validated = await asyncio.to_thread(
                accept_authoritative_client_local_rerank,
                package,
                result_payload,
                authenticated_project_id=settings.project_id,
                current_request_id=package.request_id,
            )
            result_mapping = _client_result_to_mapping(validated)
            completed = await asyncio.to_thread(
                _store_complete,
                runtime.store,
                job_id,
                settings.project_id,
                lease_token,
                result_mapping,
            )
            return _response(request, _public_record(completed), status_code=200)
        except Exception as exc:
            return _error_response(request, exc)

    async def renew_job(request: Request) -> JSONResponse:
        job_id = request.path_params.get("job_id")
        try:
            _admit(request, settings)
            _validate_id(job_id, "inference_job_id_invalid")
            payload = await _json_body(request)
            _require_fields(payload, _RENEW_FIELDS)
            lease_token = payload.get("lease_token")
            if not isinstance(lease_token, str) or not lease_token.strip():
                raise ValueError("inference_lease_token_invalid")
            record = await asyncio.to_thread(_store_get, runtime.store, job_id, settings.project_id)
            if record is None:
                raise InferenceGatewayAccessError(404, "inference_job_not_found")
            if _record_target(record) != "client-local":
                raise InferenceGatewayAccessError(403, "inference_cloud_lease_internal")
            renewed = await asyncio.to_thread(
                _store_renew,
                runtime.store,
                job_id,
                settings.project_id,
                lease_token,
                settings.lease_seconds,
            )
            return _response(request, _public_record(renewed), status_code=200)
        except Exception as exc:
            return _error_response(request, exc)

    return [
        Route("/v1/capabilities", endpoint=capabilities, methods=["GET"]),
        Route("/v1/rerank/jobs", endpoint=create_job, methods=["POST"]),
        Route(
            "/v1/rerank/jobs/by-key/{idempotency_key_hash}",
            endpoint=get_job_by_key,
            methods=["GET"],
        ),
        Route("/v1/rerank/jobs/{job_id}", endpoint=get_job, methods=["GET"]),
        Route("/v1/rerank/jobs/{job_id}/lease", endpoint=lease_job, methods=["POST"]),
        Route(
            "/v1/rerank/jobs/{job_id}/lease/renew",
            endpoint=renew_job,
            methods=["POST"],
        ),
        Route("/v1/rerank/jobs/{job_id}/complete", endpoint=complete_job, methods=["POST"]),
    ]


def create_inference_gateway_app(
    settings: InferenceGatewaySettings,
    *,
    service_factory: Callable[[], BackendInferenceService] | None = None,
    rerank_service_factory: Callable[[], BackendInferenceService] | None = None,
    store_factory: Callable[[Path], Any] | None = None,
) -> Any:
    """Build a standalone ASGI app for a dedicated loopback listener.

    The app must run on its own port (9030) so an API credential never shares
    a listener with MCP.
    """

    from starlette.applications import Starlette

    if not settings.enabled:
        return Starlette(routes=[])
    runtime = _create_gateway_runtime(
        settings,
        service_factory=service_factory,
        rerank_service_factory=rerank_service_factory,
        store_factory=store_factory,
    )

    @asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        stop_recovery = asyncio.Event()
        recovery_task = asyncio.create_task(
            _run_cloud_recovery_worker(
                runtime,
                max_jobs=min(settings.max_concurrency, settings.max_active_jobs),
                stop=stop_recovery,
            )
        )
        runtime.track(recovery_task)
        try:
            yield
        finally:
            stop_recovery.set()
            drain_task = asyncio.create_task(
                _drain_runtime_tasks(
                    runtime,
                    timeout_seconds=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
                )
            )
            try:
                remaining = await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                # Preserve cancellation, but do not let it skip the bounded
                # provider-result persistence window.
                remaining = await asyncio.shield(drain_task)
                raise
            if remaining:
                logger.warning(
                    "inference gateway shutdown drain timed out: %d task(s) may require "
                    "durable retry and may already be billed",
                    remaining,
                )

    return Starlette(
        routes=create_inference_gateway_routes(
            settings,
            service_factory=service_factory,
            rerank_service_factory=rerank_service_factory,
            store_factory=store_factory,
            _runtime=runtime,
        ),
        lifespan=lifespan,
    )


async def recover_pending_cloud_jobs(
    settings: InferenceGatewaySettings,
    *,
    rerank_service_factory: Callable[[], BackendInferenceService] | None = None,
    store_factory: Callable[[Path], Any] | None = None,
    max_jobs: int | None = None,
) -> int:
    """Explicitly recover a bounded batch of durable cloud-only jobs.

    The project comes exclusively from server settings. Client-local jobs are
    excluded by the store's atomic target filter, and no preparation service
    or embedder is constructed.
    """

    if not settings.enabled:
        return 0
    limit = (
        min(settings.max_concurrency, settings.max_active_jobs) if max_jobs is None else max_jobs
    )
    limit = _bounded_setting(
        limit,
        minimum=1,
        maximum=settings.max_active_jobs,
        reason="inference_gateway_recovery_batch_invalid",
    )
    runtime = _create_gateway_runtime(
        settings,
        service_factory=None,
        rerank_service_factory=rerank_service_factory,
        store_factory=store_factory,
    )
    return await _recover_pending_cloud_jobs(runtime, max_jobs=limit)


def _default_store_factory(settings: InferenceGatewaySettings) -> Any:
    return InferenceJobStore(
        settings.db_path,
        default_ttl_seconds=settings.ttl_seconds,
        max_ttl_seconds=settings.ttl_seconds,
        default_lease_seconds=settings.lease_seconds,
        max_lease_seconds=settings.lease_seconds,
        max_package_bytes=_MAX_PACKAGE_BYTES,
        max_active_jobs=settings.max_active_jobs,
        retention_seconds=settings.retention_seconds,
        max_retained_rows_per_project=settings.max_retained_rows,
        max_retained_json_bytes_per_project=settings.max_retained_json_bytes,
    )


def _admit(request: Request, settings: InferenceGatewaySettings) -> None:
    client = request.client.host if request.client is not None else ""
    if not _is_loopback_host(client):
        raise InferenceGatewayAccessError(403, "inference_gateway_loopback_required")
    if not _is_loopback_authority(request.headers.get("host", "")):
        raise InferenceGatewayAccessError(403, "inference_gateway_host_required")
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1:
        raise InferenceGatewayAccessError(401, "inference_gateway_token_invalid")
    authorization = authorization_values[0]
    prefix = "Bearer "
    if not authorization.startswith(prefix) or not hmac.compare_digest(
        authorization[len(prefix) :], settings.token
    ):
        raise InferenceGatewayAccessError(401, "inference_gateway_token_invalid")


def _prefers_async(request: Request) -> bool:
    """Honor the standard ``Prefer: respond-async`` request preference."""

    values = request.headers.getlist("prefer")
    return any(
        token.strip().casefold() == "respond-async"
        for value in values
        for token in value.split(",")
    )


def _poll_path(idempotency_key_hash: str) -> str:
    return f"/v1/rerank/jobs/by-key/{idempotency_key_hash}"


async def _json_body(request: Request) -> dict[str, object]:
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > _MAX_BODY_BYTES:
                raise ValueError("inference_request_too_large")
        except ValueError as exc:
            if str(exc) == "inference_request_too_large":
                raise
            raise ValueError("inference_content_length_invalid") from None

    async def read_body() -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                raise ValueError("inference_request_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        body = await asyncio.wait_for(read_body(), timeout=_BODY_READ_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise ValueError("inference_request_body_timeout") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("inference_request_body_unavailable") from None
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != "application/json":
        raise ValueError("inference_content_type_invalid")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("inference_json_invalid") from None
    if not isinstance(value, dict):
        raise ValueError("inference_json_object_required")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("inference_duplicate_field")
        output[key] = value
    return output


def _reject_json_constant(_value: str) -> object:
    raise ValueError("inference_json_nonfinite")


def _require_fields(payload: Mapping[str, object], allowed: frozenset[str]) -> None:
    if any(not isinstance(key, str) for key in payload) or set(payload) - allowed:
        raise ValueError("inference_field_not_allowed")


def _bounded_model_identity(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("client_local_model_identity_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("client_local_model_identity_invalid") from None
    if size > 512:
        raise ValueError("client_local_model_identity_invalid")
    return value


def _response(request: Request, payload: object, *, status_code: int = 200) -> JSONResponse:
    headers = dict(_SECURITY_HEADERS)
    request_id = request.headers.get("x-request-id")
    if request_id and _ID_RE.fullmatch(request_id):
        headers["X-Request-ID"] = request_id
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _error_response(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, InferenceGatewayAccessError):
        status, code = exc.status_code, exc.code
    elif isinstance(exc, InferenceJobNotFoundError):
        status, code = 404, "inference_job_not_found"
    elif isinstance(exc, InferenceJobConflictError):
        code = _safe_code(exc)
        status = 429 if code in _CAPACITY_ERROR_CODES else 409
    elif isinstance(exc, InferenceJobError):
        code = _safe_code(exc)
        status = 400 if code.endswith("_invalid") or "schema" in code else 503
    elif isinstance(exc, (ValueError, TypeError)):
        code = _safe_code(exc)
        status = 400
    else:
        code = "inference_gateway_unavailable"
        status = 503
        logger.warning("inference gateway request failed: %s", type(exc).__name__)
    return _response(
        request,
        {"error": {"code": code, "message": "Request could not be completed"}},
        status_code=status,
    )


def _safe_code(exc: Exception) -> str:
    value = ""
    if len(getattr(exc, "args", ())) == 1 and isinstance(exc.args[0], str):
        value = exc.args[0].strip()
    if not _ERROR_RE.fullmatch(value) or not value.startswith(_SAFE_ERROR_PREFIXES):
        return "inference_gateway_unavailable"
    return value


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _gateway_policy_revision() -> str:
    names = (
        "EMBEDDER_PROVIDER",
        "EMBEDDER_BASE_URL",
        "EMBEDDER_PATH",
        "EMBEDDER_MODEL",
        "EMBEDDER_MODEL_REVISION",
        "PP_EMBEDDING_DIM",
        _CLIENT_VECTOR_IDENTITY_ENV,
        _CLIENT_VECTOR_DIMENSION_ENV,
        "PP_RERANK_PROVIDERS",
        "PP_RERANK_BASE_URL",
        "PP_RERANK_PATH",
        "PP_RERANK_CLOUD_MODEL",
        "PP_RERANK_CLOUD_MODEL_REVISION",
    )
    return _canonical_sha256({name: os.getenv(name, "") for name in names})


def _normalize_project_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw or not _ID_RE.fullmatch(raw.removeprefix("project:")):
        return ""
    return raw if raw.startswith("project:") else f"project:{raw}"


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.expanduser().resolve(strict=False) == second.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        raise InferenceGatewayConfigurationError("inference_gateway_db_path_invalid") from None


def _validate_id(value: object, reason: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(reason)
    return value


def _bounded_setting(value: object, *, minimum: int, maximum: int, reason: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InferenceGatewayConfigurationError(reason) from None
    if isinstance(value, bool) or not minimum <= parsed <= maximum:
        raise InferenceGatewayConfigurationError(reason)
    return parsed


def _provider_concurrency_limit() -> int:
    """Reserve two default-executor threads for lease and store operations."""

    default_workers = min(32, (os.cpu_count() or 1) + 4)
    return max(1, default_workers - 2)


def _is_loopback_host(value: object) -> bool:
    raw = str(value or "").strip().casefold()
    if raw == "localhost":
        return True
    try:
        address = ipaddress.ip_address(raw.strip("[]"))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _is_loopback_authority(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw or any(c in raw for c in "\r\n\t/@,\\"):
        return False
    if raw.startswith("["):
        close = raw.find("]")
        if close < 0:
            return False
        host, suffix = raw[1:close], raw[close + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return False
    elif raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if not port.isdigit():
            return False
    else:
        host = raw
    return _is_loopback_host(host)


def _client_vector_contract_from_env() -> tuple[_ClientVectorContract | None, str]:
    """Read the explicit contract for client-provided vector reuse.

    Do not infer this from a model name or construct the runtime embedder.
    The contract is server process configuration, and is intentionally
    independent from hosted embedding readiness.
    """

    identity = os.getenv(_CLIENT_VECTOR_IDENTITY_ENV, "")
    if not identity or identity != identity.strip() or "\x00" in identity:
        return None, "client_vector_embedding_contract_missing"
    try:
        if len(identity.encode("utf-8")) > 512:
            return None, "client_vector_embedding_contract_invalid"
    except UnicodeEncodeError:
        return None, "client_vector_embedding_contract_invalid"

    raw_dimension = os.getenv(_CLIENT_VECTOR_DIMENSION_ENV, "")
    if not raw_dimension or raw_dimension != raw_dimension.strip():
        return None, "client_vector_embedding_contract_missing"
    try:
        dimension = int(raw_dimension)
    except ValueError:
        return None, "client_vector_embedding_contract_invalid"
    if dimension <= 0 or dimension > 16_384:
        return None, "client_vector_embedding_contract_invalid"
    return _ClientVectorContract(identity=identity, dimension=dimension), ""


def _require_client_vector_contract() -> _ClientVectorContract:
    contract, reason = _client_vector_contract_from_env()
    if contract is None:
        raise InferenceGatewayAccessError(503, reason)
    return contract


def _all_embeddings_supplied(items: list[Mapping[str, object]]) -> bool:
    """Return true only after request validation has established item shape."""

    return bool(items) and all(item.get("embedding") is not None for item in items)


def _embedding_readiness(*, provider: str, model: str, dimension: int) -> tuple[bool, str]:
    if provider not in {"cloud", "openai-compatible"}:
        return False, "cloud_embedding_provider_not_selected"
    if not model or len(model.encode("utf-8")) > 512:
        return False, "cloud_embedding_model_invalid"
    if dimension <= 0 or dimension > 16_384:
        return False, "cloud_embedding_dimension_invalid"
    if not os.getenv("EMBEDDER_BASE_URL", "").strip():
        return False, "cloud_embedding_base_url_missing"
    if not os.getenv("EMBEDDER_API_KEY", "").strip():
        return False, "cloud_embedding_api_key_missing"
    if not _provider_endpoint_is_cloud(
        provider="gateway-embedding",
        base_url=os.getenv("EMBEDDER_BASE_URL", "").strip(),
        api_key=os.getenv("EMBEDDER_API_KEY", "").strip(),
    ):
        return False, "cloud_embedding_base_url_invalid"
    return True, ""


def _rerank_readiness(providers: list[str]) -> tuple[bool, str]:
    if not providers or providers[0] != "cloud":
        return False, "cloud_rerank_provider_not_selected"
    if any(provider not in {"cloud", "original", "cosine"} for provider in providers):
        return False, "cloud_rerank_local_fallback_forbidden"
    if not os.getenv("PP_RERANK_BASE_URL", "").strip():
        return False, "cloud_rerank_base_url_missing"
    if not os.getenv("PP_RERANK_API_KEY", "").strip():
        return False, "cloud_rerank_api_key_missing"
    if (
        not os.getenv("PP_RERANK_CLOUD_MODEL", "").strip()
        and not os.getenv("PP_RERANK_MODEL", "").strip()
    ):
        return False, "cloud_rerank_model_missing"
    if not _provider_endpoint_is_cloud(
        provider="gateway-rerank",
        base_url=os.getenv("PP_RERANK_BASE_URL", "").strip(),
        api_key=os.getenv("PP_RERANK_API_KEY", "").strip(),
    ):
        return False, "cloud_rerank_base_url_invalid"
    return True, ""


def _embedding_readiness_from_env() -> tuple[bool, str]:
    try:
        dimension = int(os.getenv("PP_EMBEDDING_DIM") or os.getenv("EMBEDDER_DIMENSION") or "1024")
    except ValueError:
        dimension = 0
    return _embedding_readiness(
        provider=os.getenv("EMBEDDER_PROVIDER", "").strip().casefold(),
        model=os.getenv("EMBEDDER_MODEL", "text-embedding-v4").strip(),
        dimension=dimension,
    )


def _rerank_readiness_from_env() -> tuple[bool, str]:
    providers = [
        value.strip().casefold()
        for value in os.getenv("PP_RERANK_PROVIDERS", "cosine").split(",")
        if value.strip()
    ]
    return _rerank_readiness(providers)


def _require_cloud_readiness(*, requires_embedding: bool, requires_rerank: bool) -> None:
    """Require only providers that the current stage will actually call."""

    if requires_embedding:
        embedding_ready, embedding_reason = _embedding_readiness_from_env()
        if not embedding_ready:
            raise InferenceGatewayAccessError(503, embedding_reason)
    if requires_rerank:
        rerank_ready, rerank_reason = _rerank_readiness_from_env()
        if not rerank_ready:
            raise InferenceGatewayAccessError(503, rerank_reason)


def _normalized_provider_host(value: object) -> str | None:
    """Normalize a hostname for an exact operator-owned allowlist match."""

    if not isinstance(value, str):
        return None
    raw = value.strip().rstrip(".")
    if not raw or any(character in raw for character in "/@?#[]%\\\r\n\t"):
        return None
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        # Reject ambiguous numeric forms such as ``127.1`` and ``0x7f000001``.
        # Some network stacks treat them as IPv4 literals even though the
        # standard library parser does not.
        if re.fullmatch(r"[0-9.]+|0x[0-9a-fA-F]+", raw):
            return None
        if ":" in raw:
            return None
        try:
            normalized = raw.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return None
        return normalized or None
    return str(address).casefold()


def _provider_host_allowlist() -> frozenset[str]:
    """Return no hosts when the operator configuration is absent or malformed.

    An exact host allowlist is deliberate: it avoids an eager DNS lookup that
    makes readiness nondeterministic and limits DNS-rebinding trust to hosts
    the server operator explicitly selected.
    """

    raw = os.getenv(_PROVIDER_HOST_ALLOWLIST_ENV, "")
    if not raw:
        return frozenset()
    hosts: set[str] = set()
    for entry in raw.split(","):
        normalized = _normalized_provider_host(entry)
        if normalized is None:
            return frozenset()
        hosts.add(normalized)
    return frozenset(hosts)


def _provider_host_is_cloud_allowed(hostname: object) -> bool:
    normalized = _normalized_provider_host(hostname)
    if normalized is None:
        return False
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            # ``is_global`` additionally excludes shared, documentation, and
            # other special-purpose ranges that must not receive cloud traffic.
            or not address.is_global
        ):
            return False
    return normalized in _provider_host_allowlist()


def _provider_endpoint_is_cloud(*, provider: str, base_url: str, api_key: str) -> bool:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        return False
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not _provider_host_is_cloud_allowed(parsed.hostname)
    ):
        return False
    try:
        # This helper validates the endpoint contract only; it must not be
        # treated as an actual provider construction that bypasses the
        # compute-node role gate.  An injected no-op transport keeps the
        # validation side-effect free while retaining the constructor's
        # schema checks.
        client = ProviderHTTPClient(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            transport=httpx.MockTransport(lambda _request: httpx.Response(599)),
        )
    except ProviderHTTPError:
        return False
    client.close()
    return True


def _binding_to_mapping(binding: RerankRequestBinding) -> dict[str, object]:
    return {
        "contract_version": binding.contract_version,
        "scoring_version": binding.scoring_version,
        "project_id": binding.project_id,
        "request_id": binding.request_id,
        "idempotency_key_hash": binding.idempotency_key_hash,
        "candidate_set_version": binding.candidate_set_version,
        "candidate_set_hash": binding.candidate_set_hash,
        "query_hash": binding.query_hash,
        "input_hash": binding.input_hash,
        "provider_policy_revision": binding.provider_policy_revision,
        "top_k": binding.top_k,
    }


def _binding_from_mapping(value: Mapping[str, object]) -> RerankRequestBinding:
    fields = {
        "contract_version",
        "scoring_version",
        "project_id",
        "request_id",
        "idempotency_key_hash",
        "candidate_set_version",
        "candidate_set_hash",
        "query_hash",
        "input_hash",
        "provider_policy_revision",
        "top_k",
    }
    if set(value) != fields:
        raise ValueError("inference_binding_invalid")
    if not isinstance(value.get("top_k"), int) or isinstance(value.get("top_k"), bool):
        raise ValueError("inference_binding_invalid")
    text_fields = fields - {"top_k"}
    if not all(isinstance(value.get(name), str) and value.get(name) for name in text_fields):
        raise ValueError("inference_binding_invalid")
    return RerankRequestBinding(**{name: value[name] for name in fields})  # type: ignore[arg-type]


def _package_to_mapping(package: ClientLocalRerankPackage) -> dict[str, object]:
    return {
        "contract_version": package.contract_version,
        "scoring_version": package.scoring_version,
        "project_id": package.project_id,
        "request_id": package.request_id,
        "candidate_set_version": package.candidate_set_version,
        "candidate_set_hash": package.candidate_set_hash,
        "query": package.query,
        "query_hash": package.query_hash,
        "embedding_identity": package.embedding_identity,
        "embedding_dimension": package.embedding_dimension,
        "model_identity": package.model_identity,
        "top_k": package.top_k,
        "candidates": [
            {
                "id": item.item_id,
                "text": item.text,
                "base_score": item.base_score,
                "material_sha256": item.material_sha256,
                "embedding_sha256": item.embedding_sha256,
            }
            for item in package.candidates
        ],
        "package_hash": package.package_hash,
    }


def _package_from_mapping(value: Mapping[str, object]) -> ClientLocalRerankPackage:
    fields = {
        "contract_version",
        "scoring_version",
        "project_id",
        "request_id",
        "candidate_set_version",
        "candidate_set_hash",
        "query",
        "query_hash",
        "embedding_identity",
        "embedding_dimension",
        "model_identity",
        "top_k",
        "candidates",
        "package_hash",
    }
    if set(value) != fields:
        raise ValueError("inference_package_invalid")
    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("inference_package_invalid")
    candidates: list[ClientLocalCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping) or set(raw) != {
            "id",
            "text",
            "base_score",
            "material_sha256",
            "embedding_sha256",
        }:
            raise ValueError("inference_package_invalid")
        candidates.append(
            ClientLocalCandidate(
                item_id=raw["id"],
                text=raw["text"],
                base_score=raw["base_score"],
                material_sha256=raw["material_sha256"],
                embedding_sha256=raw["embedding_sha256"],
            )  # type: ignore[arg-type]
        )
    try:
        package = ClientLocalRerankPackage(
            contract_version=value["contract_version"],  # type: ignore[arg-type]
            scoring_version=value["scoring_version"],  # type: ignore[arg-type]
            project_id=value["project_id"],  # type: ignore[arg-type]
            request_id=value["request_id"],  # type: ignore[arg-type]
            candidate_set_version=value["candidate_set_version"],  # type: ignore[arg-type]
            candidate_set_hash=value["candidate_set_hash"],  # type: ignore[arg-type]
            query=value["query"],  # type: ignore[arg-type]
            query_hash=value["query_hash"],  # type: ignore[arg-type]
            embedding_identity=value["embedding_identity"],  # type: ignore[arg-type]
            embedding_dimension=value["embedding_dimension"],  # type: ignore[arg-type]
            model_identity=value["model_identity"],  # type: ignore[arg-type]
            top_k=value["top_k"],  # type: ignore[arg-type]
            candidates=tuple(candidates),
            package_hash=value["package_hash"],  # type: ignore[arg-type]
        )
    except TypeError:
        raise ValueError("inference_package_invalid") from None
    return package


def _client_result_to_mapping(result: ClientLocalRerankResult) -> dict[str, object]:
    return {
        "contract_version": result.contract_version,
        "package_hash": result.package_hash,
        "model_identity": result.reported_model_identity,
        "items": [{"id": item.item_id, "score": item.score} for item in result.items],
    }


def _cloud_result_to_mapping(result: RerankResult) -> dict[str, object]:
    diagnostics = _json_safe(result.diagnostics)
    return {
        "contract_version": result.contract_version,
        "scoring_version": result.scoring_version,
        "project_id": result.project_id,
        "request_id": result.request_id,
        "idempotency_key_hash": result.idempotency_key_hash,
        "candidate_set_version": result.candidate_set_version,
        "candidate_set_hash": result.candidate_set_hash,
        "query_hash": result.query_hash,
        "input_hash": result.input_hash,
        "provider_policy_revision": result.provider_policy_revision,
        "model_identity": result.model_identity,
        "top_k": result.top_k,
        "items": [
            {"id": item.item_id, "score": item.score, "rank": item.rank} for item in result.items
        ],
        "diagnostics": diagnostics,
    }


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items() if isinstance(key, str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


async def _prepare_submission(
    runtime: _GatewayRuntime,
    *,
    target: str,
    items: list[Mapping[str, object]],
    query: str,
    request_id: str,
    model_identity: str | None,
    idempotency_key: str,
    idempotency_key_hash: str,
    candidate_set_version: str,
    top_k: int | None,
    request_hash: str,
    reservation_token: str,
) -> Any:
    """Prepare and finalize a reservation independently of its HTTP request."""

    try:
        if _all_embeddings_supplied(items):
            # Both cloud and client-local paths use the same no-network
            # validator when every vector is already present.
            _require_cloud_readiness(
                requires_embedding=False,
                requires_rerank=target == "cloud",
            )
            policy_revision = _gateway_policy_revision()
            if target == "cloud":
                rerank_service = await asyncio.to_thread(runtime.rerank_service)
                policy_revision = rerank_service.provider_policy_revision
            service = BackendInferenceService(
                embedder=_ClientSuppliedVectorEmbedder(_require_client_vector_contract()),
                provider_policy_revision=policy_revision,
            )
        else:
            _require_cloud_readiness(
                requires_embedding=True,
                requires_rerank=target == "cloud",
            )
            service = await asyncio.to_thread(runtime.preparation_service)
        async with runtime.provider_slots:
            prepared = await _prepare_with_reservation_renewal(
                runtime,
                service=service,
                items=items,
                idempotency_key_hash=idempotency_key_hash,
                reservation_token=reservation_token,
            )
        binding = service.bind_rerank_request(
            query=query,
            prepared=prepared,
            project_id=runtime.settings.project_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            candidate_set_version=candidate_set_version,
            top_k=top_k,
        )
        package = service.export_client_local_rerank(
            query=query,
            prepared=prepared,
            authenticated_project_id=runtime.settings.project_id,
            request_id=request_id,
            candidate_set_version=candidate_set_version,
            model_identity=(
                model_identity
                if model_identity is not None
                else f"server-cloud-policy:{service.provider_policy_revision}"
            ),
            top_k=top_k,
        )
        _ensure_package_size(package)
        return await asyncio.to_thread(
            _store_finalize,
            runtime.store,
            project_id=runtime.settings.project_id,
            idempotency_key_hash=idempotency_key_hash,
            reservation_token=reservation_token,
            binding=_binding_to_mapping(binding),
            package=_package_to_mapping(package),
            target=target,
            ttl_seconds=runtime.settings.ttl_seconds,
            request_material={"request_hash": request_hash},
        )
    except BaseException:
        await asyncio.to_thread(
            _store_release_reservation,
            runtime.store,
            runtime.settings.project_id,
            idempotency_key_hash,
            reservation_token,
        )
        raise


async def _prepare_and_dispatch(
    runtime: _GatewayRuntime,
    *,
    target: str,
    items: list[Mapping[str, object]],
    query: str,
    request_id: str,
    model_identity: str | None,
    idempotency_key: str,
    idempotency_key_hash: str,
    candidate_set_version: str,
    top_k: int | None,
    request_hash: str,
    reservation_token: str,
) -> Any:
    """Prepare a durable job and, for cloud work, dispatch it off-request."""

    record = await _prepare_submission(
        runtime,
        target=target,
        items=items,
        query=query,
        request_id=request_id,
        model_identity=model_identity,
        idempotency_key=idempotency_key,
        idempotency_key_hash=idempotency_key_hash,
        candidate_set_version=candidate_set_version,
        top_k=top_k,
        request_hash=request_hash,
        reservation_token=reservation_token,
    )
    if target == "cloud" and (
        _record_bool(record, "created", default=True)
        or _record_status(record) in {"pending", "leased"}
    ):
        binding = _binding_from_mapping(_record_binding(record))
        package = _package_from_mapping(_record_package(record))
        return await _run_cloud_job(runtime, record, binding, package)
    return record


async def _prepare_with_reservation_renewal(
    runtime: _GatewayRuntime,
    *,
    service: BackendInferenceService,
    items: list[Mapping[str, object]],
    idempotency_key_hash: str,
    reservation_token: str,
) -> Any:
    """Keep the preparation capability live until embeddings are ready."""

    renew = getattr(runtime.store, "renew_submission", None)
    if not callable(renew):
        return await service.aprepare(items)

    preparation_task = asyncio.create_task(service.aprepare(items))
    runtime.track(preparation_task)
    renew_interval = max(1.0, min(float(runtime.settings.lease_seconds) / 2.0, 30.0))
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(preparation_task),
                    timeout=renew_interval,
                )
            except TimeoutError:
                await asyncio.to_thread(
                    _store_renew_submission,
                    runtime.store,
                    runtime.settings.project_id,
                    idempotency_key_hash,
                    reservation_token,
                    runtime.settings.lease_seconds,
                )
    except BaseException:
        # ``aprepare`` may be backed by ``asyncio.to_thread``. Cancelling its
        # wrapper cannot stop an already-issued provider call, so leave the
        # tracked task to finish and make that state visible to shutdown drain.
        raise


async def _recover_pending_cloud_jobs(
    runtime: _GatewayRuntime,
    *,
    max_jobs: int,
) -> int:
    """Run one project-scoped cycle over persisted cloud packages."""

    _require_cloud_readiness(
        requires_embedding=False,
        requires_rerank=True,
    )
    claimed = 0
    executions: list[asyncio.Task[None]] = []

    async def execute_claimed(
        record: Any,
        lease_token: str,
        binding: RerankRequestBinding,
        package: ClientLocalRerankPackage,
    ) -> None:
        try:
            await _execute_cloud_lease(
                runtime,
                record,
                lease_token,
                binding,
                package,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cloud inference recovery failed: %s", _safe_code(exc))

    for _ in range(max_jobs):
        lease = await asyncio.to_thread(
            runtime.store.claim_next,
            runtime.settings.project_id,
            target="cloud",
            lease_seconds=runtime.settings.lease_seconds,
        )
        if lease is None:
            break
        claimed += 1
        record = lease.job
        if (
            _record_target(record) != "cloud"
            or _record_value(record, "project_id", "") != runtime.settings.project_id
        ):
            logger.error("cloud recovery store returned a job outside its target scope")
            continue
        try:
            binding = _binding_from_mapping(_record_binding(record))
            package = _package_from_mapping(_record_package(record))
            execution = asyncio.create_task(
                execute_claimed(
                    record,
                    lease.lease_token,
                    binding,
                    package,
                )
            )
            runtime.track(execution)
            executions.append(execution)
        except Exception as exc:
            logger.warning("cloud inference recovery failed: %s", _safe_code(exc))
    if executions:
        await asyncio.gather(*executions)
    return claimed


async def _run_cloud_job(
    runtime: _GatewayRuntime,
    record: Any,
    binding: RerankRequestBinding,
    package: ClientLocalRerankPackage,
) -> Any:
    job_id = _record_id(record)
    try:
        leased = await asyncio.to_thread(
            _store_lease,
            runtime.store,
            job_id,
            runtime.settings.project_id,
            runtime.settings.lease_seconds,
        )
    except InferenceJobError as exc:
        if _safe_code(exc) == "inference_job_lease_active":
            current = await asyncio.to_thread(
                _store_get, runtime.store, job_id, runtime.settings.project_id
            )
            return current or record
        raise
    if leased is None:
        current = await asyncio.to_thread(
            _store_get, runtime.store, job_id, runtime.settings.project_id
        )
        return current or record
    leased_record, lease_token = leased
    completion_task = asyncio.create_task(
        _execute_cloud_lease(
            runtime,
            leased_record,
            lease_token,
            binding,
            package,
        )
    )
    runtime.track(completion_task)
    return await asyncio.shield(completion_task)


async def _execute_cloud_lease(
    runtime: _GatewayRuntime,
    leased_record: Any,
    lease_token: str,
    binding: RerankRequestBinding,
    package: ClientLocalRerankPackage,
) -> Any:
    """Own provider execution independently of the initiating HTTP request."""

    job_id = _record_id(leased_record)

    async def invoke_provider_and_persist() -> Any:
        async with runtime.provider_slots:
            result = await asyncio.to_thread(
                runtime.rerank_service().rerank_authoritative_package,
                package=package,
                binding=binding,
            )
        try:
            return await asyncio.to_thread(
                _store_complete,
                runtime.store,
                job_id,
                runtime.settings.project_id,
                lease_token,
                _cloud_result_to_mapping(result),
            )
        except InferenceJobConflictError:
            current = await asyncio.to_thread(
                _store_get,
                runtime.store,
                job_id,
                runtime.settings.project_id,
            )
            return current or leased_record

    # This task owns both the issued provider call and the durable completion.
    # Shielding isolates ordinary request/recovery cancellation and graceful
    # shutdown within the bounded drain window.  A process failure or
    # over-deadline shutdown can still occur after billing but before CAS, so
    # the durable pending job may be retried and billed again.
    provider_task = asyncio.create_task(invoke_provider_and_persist())
    runtime.track(provider_task)
    renew_interval = max(1.0, min(float(runtime.settings.lease_seconds) / 2.0, 30.0))
    try:
        while True:
            try:
                completed_record = await asyncio.wait_for(
                    asyncio.shield(provider_task),
                    timeout=renew_interval,
                )
                return completed_record
            except TimeoutError:
                leased_record = await asyncio.to_thread(
                    _store_renew,
                    runtime.store,
                    job_id,
                    runtime.settings.project_id,
                    lease_token,
                    runtime.settings.lease_seconds,
                )
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, Exception):
            raise
        failure_code = _safe_code(exc)
        if failure_code in _TERMINAL_CLOUD_FAILURE_CODES:
            try:
                return await asyncio.to_thread(
                    _store_fail,
                    runtime.store,
                    job_id,
                    runtime.settings.project_id,
                    lease_token,
                    failure_code,
                )
            except InferenceJobError:
                current = await asyncio.to_thread(
                    _store_get, runtime.store, job_id, runtime.settings.project_id
                )
                logger.warning("cloud inference job terminal failure could not persist")
                return current or leased_record
        # The lease deliberately remains until its short TTL.  A subsequent
        # request can reclaim it without an unsafe force-complete operation.
        logger.warning("cloud inference job failed: %s", "provider_error")
        return leased_record


def _store_create(store: Any, **kwargs: object) -> Any:
    return store.create_or_get(**kwargs)


def _store_reserve(store: Any, **kwargs: object) -> tuple[str, str | None]:
    reserve = getattr(store, "reserve_submission", None)
    if not callable(reserve):
        # Test doubles and older deployments can still use the original
        # create-after-prepare contract; production store always implements
        # the durable reservation path.
        return "reserved", "legacy-reservation"
    return reserve(**kwargs)


def _store_finalize(store: Any, **kwargs: object) -> Any:
    finalize = getattr(store, "finalize_submission", None)
    if callable(finalize):
        return finalize(**kwargs)
    kwargs.pop("request_material", None)
    kwargs.pop("reservation_token", None)
    kwargs.pop("idempotency_key_hash", None)
    return _store_create(store, **kwargs)


def _store_renew_submission(
    store: Any,
    project_id: str,
    idempotency_key_hash: str,
    reservation_token: str,
    lease_seconds: int,
) -> Any:
    return store.renew_submission(
        project_id,
        idempotency_key_hash,
        reservation_token,
        lease_seconds,
    )


def _store_get_by_reservation(store: Any, project_id: str, idempotency_key_hash: str) -> Any:
    reservation = _store_get_reservation(store, project_id, idempotency_key_hash)
    if not isinstance(reservation, Mapping):
        return None
    job_id = reservation.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    return _store_get(store, job_id, project_id)


def _store_get_reservation(store: Any, project_id: str, idempotency_key_hash: str) -> Any:
    getter = getattr(store, "get_reservation", None)
    if not callable(getter):
        return None
    return getter(project_id, idempotency_key_hash)


def _store_release_reservation(
    store: Any,
    project_id: str,
    idempotency_key_hash: str,
    reservation_token: str,
) -> None:
    release = getattr(store, "release_submission", None)
    if callable(release):
        try:
            release(project_id, idempotency_key_hash, reservation_token)
        except Exception:
            logger.warning("inference reservation release failed: %s", "store_error")


def _store_get(store: Any, job_id: str, project_id: str) -> Any:
    try:
        return store.get(job_id, project_id=project_id)
    except TypeError:
        return store.get(job_id, project_id)


def _store_lease(store: Any, job_id: str, project_id: str, lease_seconds: int) -> Any:
    return store.lease(job_id, project_id, lease_seconds)


def _store_renew(
    store: Any,
    job_id: str,
    project_id: str,
    lease_token: str,
    lease_seconds: int,
) -> Any:
    return store.renew_lease(job_id, project_id, lease_token, lease_seconds)


def _store_complete(
    store: Any, job_id: str, project_id: str, lease_token: str, result: Mapping[str, object]
) -> Any:
    try:
        return store.complete(
            job_id,
            project_id=project_id,
            lease_token=lease_token,
            result=result,
        )
    except TypeError:
        return store.complete(job_id, project_id, lease_token, result)


def _store_fail(
    store: Any,
    job_id: str,
    project_id: str,
    lease_token: str,
    failure_code: str,
) -> Any:
    fail = getattr(store, "fail", None)
    if not callable(fail):
        raise InferenceJobError("inference_job_failure_unsupported")
    try:
        return fail(
            job_id,
            project_id=project_id,
            lease_token=lease_token,
            failure_code=failure_code,
        )
    except TypeError:
        return fail(job_id, project_id, lease_token, failure_code)


def _store_expire(store: Any) -> None:
    expire = getattr(store, "expire_due", None)
    if callable(expire):
        expire()


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    nested = getattr(record, "job", None)
    if nested is not None and name != "created":
        return _record_value(nested, name, default)
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _record_id(record: Any) -> str:
    value = _record_value(record, "job_id", _record_value(record, "id", ""))
    return _validate_id(value, "inference_job_id_invalid")


def _record_status(record: Any) -> str:
    return str(_record_value(record, "status", "unknown"))


def _record_target(record: Any) -> str:
    return str(_record_value(record, "target", "cloud"))


def _record_bool(record: Any, name: str, *, default: bool) -> bool:
    value = _record_value(record, name, default)
    return value if isinstance(value, bool) else default


def _record_package(record: Any) -> Mapping[str, object]:
    value = _record_value(record, "package", {})
    if not isinstance(value, Mapping):
        raise ValueError("inference_package_invalid")
    return value


def _record_binding(record: Any) -> Mapping[str, object]:
    value = _record_value(record, "binding", {})
    if not isinstance(value, Mapping):
        raise ValueError("inference_binding_invalid")
    return value


def _public_record(record: Any) -> dict[str, object]:
    package = _record_package(record)
    output: dict[str, object] = {
        "contract": GATEWAY_CONTRACT,
        "job_id": _record_value(record, "job_id", _record_value(record, "id", "")),
        "project_id": _record_value(record, "project_id", ""),
        "target": _record_target(record),
        "status": _record_status(record),
        "request_id": _record_value(record, "request_id", package.get("request_id", "")),
        "candidate_set_version": _record_value(
            record, "candidate_set_version", package.get("candidate_set_version", "")
        ),
        "input_hash": _record_value(record, "input_hash", ""),
        "created_at": _record_value(record, "created_at", ""),
        "expires_at": _record_value(record, "expires_at", ""),
    }
    if _record_target(record) == "client-local":
        output["package"] = package
    result = _record_value(record, "result", None)
    if isinstance(result, Mapping):
        output["result"] = result
    failure_code = _record_value(record, "failure_code", None)
    if isinstance(failure_code, str) and failure_code:
        output["status"] = "failed"
        output["error"] = {
            "code": failure_code,
            "message": "Job cannot run under the current provider policy",
            "retryable": False,
        }
    return output


def _public_reservation(reservation: Mapping[str, object]) -> dict[str, object]:
    """Expose poll-safe reservation metadata without lease capabilities."""

    status = str(reservation.get("status", "unknown"))
    output: dict[str, object] = {
        "contract": GATEWAY_CONTRACT,
        "project_id": reservation.get("project_id", ""),
        "target": reservation.get("target", ""),
        "status": "preparing" if status in {"reserved", "preparing"} else status,
        "idempotency_key_hash": reservation.get("idempotency_key_hash", ""),
        "job_id": reservation.get("job_id"),
        "created_at": reservation.get("created_at", ""),
        "updated_at": reservation.get("updated_at", ""),
        "expires_at": reservation.get("expires_at", ""),
        "poll_path": _poll_path(str(reservation.get("idempotency_key_hash", ""))),
    }
    if status == "released":
        output["status"] = "failed"
        output["error"] = {
            "code": "inference_job_preparation_failed",
            "message": "Job preparation did not complete",
            "retryable": True,
        }
    return output


def _status_for_record(record: Any, *, created: bool) -> int:
    if _record_value(record, "failure_code", None):
        return 409
    if _record_status(record) == "completed":
        return 200
    if created:
        return 201
    return 202


def _ensure_package_size(package: ClientLocalRerankPackage) -> None:
    if (
        len(json.dumps(_package_to_mapping(package), ensure_ascii=False).encode("utf-8"))
        > _MAX_PACKAGE_BYTES
    ):
        raise ValueError("inference_package_too_large")


__all__ = [
    "GATEWAY_CONTRACT",
    "InferenceGatewayAccessError",
    "InferenceGatewayConfigurationError",
    "InferenceGatewaySettings",
    "create_inference_gateway_app",
    "create_inference_gateway_routes",
]
