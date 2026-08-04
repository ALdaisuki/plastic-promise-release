"""Trusted native-client executor for server-issued local rerank jobs.

The executor is intentionally stateless. It receives exact candidate text in
an immutable gateway package, runs an injected local model, and sends only a
request-scoped ranking back to the server. It never receives provider secrets,
raw vectors, or authority to mutate canonical memory or a derived index.
"""

from __future__ import annotations

import asyncio
import inspect
import json as json_module
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from plastic_promise.core.backend_inference import (
    CLIENT_LOCAL_RESULT_CONTRACT,
    ClientLocalCandidate,
    ClientLocalRerankPackage,
    accept_authoritative_client_local_rerank,
    validate_client_local_rerank_package,
)

_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,512}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32,512}\Z")
_PATH_RE = re.compile(r"\A/[A-Za-z0-9._:/-]{1,2048}\Z")
_FORBIDDEN_REQUEST_HEADERS = frozenset({"authorization", "host", "proxy-authorization"})
_MAX_CLIENT_LOCAL_CANDIDATES = 100
_MAX_CLIENT_LOCAL_PACKAGE_BYTES = 3 * 1024 * 1024
_MAX_GATEWAY_RESPONSE_BYTES = 4 * 1024 * 1024
_PACKAGE_FIELDS = frozenset(
    {
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
)
_CANDIDATE_FIELDS = frozenset({"id", "text", "base_score", "material_sha256", "embedding_sha256"})


class ClientLocalRerankExecutorError(RuntimeError):
    """The local executor rejected a job, package, model result, or response."""


class ClientLocalGatewayError(ClientLocalRerankExecutorError):
    """A gateway request returned a structured non-success response."""

    def __init__(self, *, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class ClientLocalGatewayResponse:
    status_code: int
    payload: Mapping[str, object]


@runtime_checkable
class ClientLocalGatewayTransport(Protocol):
    """Injectable JSON transport used by the executor and native-client hosts."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ClientLocalGatewayResponse: ...


class HTTPXClientLocalGatewayTransport:
    """In-memory-credential HTTP transport for a loopback gateway tunnel."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("client_local_gateway_url_must_be_loopback")
        if not _TOKEN_RE.fullmatch(bearer_token):
            raise ValueError("client_local_gateway_token_invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("client_local_gateway_timeout_invalid")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer_token}"},
            transport=transport,
            timeout=float(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ClientLocalGatewayResponse:
        checked_method = method.upper() if isinstance(method, str) else ""
        if checked_method not in {"GET", "POST"}:
            raise ValueError("client_local_gateway_method_invalid")
        if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or path.startswith("//"):
            raise ValueError("client_local_gateway_path_invalid")
        if headers is not None and (
            not isinstance(headers, Mapping)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or key.lower() in _FORBIDDEN_REQUEST_HEADERS
                for key, value in headers.items()
            )
        ):
            raise ValueError("client_local_gateway_headers_invalid")
        async with self._client.stream(
            checked_method,
            path,
            json=json,
            headers=headers,
        ) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_GATEWAY_RESPONSE_BYTES:
                    raise ClientLocalRerankExecutorError("client_local_gateway_response_too_large")
            status_code = response.status_code
        try:
            payload = json_module.loads(body)
        except (UnicodeDecodeError, ValueError):
            raise ClientLocalRerankExecutorError("client_local_gateway_json_invalid") from None
        if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
            raise ClientLocalRerankExecutorError("client_local_gateway_payload_invalid")
        if not 200 <= status_code < 300:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            if not isinstance(code, str) or not code:
                code = "client_local_gateway_request_failed"
            raise ClientLocalGatewayError(status_code=status_code, code=code)
        return ClientLocalGatewayResponse(status_code=status_code, payload=dict(payload))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HTTPXClientLocalGatewayTransport:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


@dataclass(frozen=True)
class LocalRerankCandidate:
    item_id: str
    text: str
    base_score: float


@dataclass(frozen=True)
class LocalRerankScore:
    item_id: str
    score: float


LocalRerankOutput = (
    Mapping[str, float] | Sequence[LocalRerankScore | tuple[str, float] | Mapping[str, object]]
)
LocalRerankCallable = Callable[
    [str, tuple[LocalRerankCandidate, ...], int],
    LocalRerankOutput | Awaitable[LocalRerankOutput],
]


class ClientLocalRerankExecutor:
    """Lease, locally score, renew, and CAS-complete one client-local job."""

    def __init__(
        self,
        *,
        transport: ClientLocalGatewayTransport,
        rerank: LocalRerankCallable,
        model_identity: str,
        renew_interval_seconds: float = 2.0,
    ) -> None:
        if not isinstance(transport, ClientLocalGatewayTransport):
            raise TypeError("client_local_transport_invalid")
        if not callable(rerank):
            raise TypeError("client_local_rerank_callable_required")
        self._model_identity = _bounded_identifier(
            model_identity,
            reason="client_local_model_identity_invalid",
        )
        if (
            isinstance(renew_interval_seconds, bool)
            or not isinstance(renew_interval_seconds, (int, float))
            or not math.isfinite(float(renew_interval_seconds))
            or renew_interval_seconds <= 0
        ):
            raise ValueError("client_local_renew_interval_invalid")
        self._transport = transport
        self._rerank = rerank
        self._renew_interval_seconds = float(renew_interval_seconds)

    async def execute_job(self, job_id: str) -> Mapping[str, object]:
        """Fetch and execute a known job ID without retaining local state."""

        checked_job_id = _identifier(job_id, reason="client_local_job_id_invalid")
        response = await self._transport.request(
            "GET",
            f"/v1/rerank/jobs/{checked_job_id}",
        )
        return await self.execute(response.payload)

    async def execute(self, job: Mapping[str, object]) -> Mapping[str, object]:
        """Consume a job record, obtaining a lease when it is still pending."""

        if not isinstance(job, Mapping):
            raise TypeError("client_local_job_mapping_required")
        job_id = _identifier(job.get("job_id"), reason="client_local_job_id_invalid")
        if job.get("target") != "client-local":
            raise ClientLocalRerankExecutorError("client_local_job_target_mismatch")
        status = job.get("status")
        if status == "completed":
            return dict(job)
        if status not in {"pending", "leased"}:
            raise ClientLocalRerankExecutorError("client_local_job_not_ready")

        leased = dict(job)
        lease_token = leased.get("lease_token")
        if not isinstance(lease_token, str) or not lease_token:
            lease_response = await self._transport.request(
                "POST",
                f"/v1/rerank/jobs/{job_id}/lease",
                json={},
            )
            leased = dict(lease_response.payload)
            lease_token = leased.get("lease_token")
        if leased.get("job_id") != job_id or leased.get("target") != "client-local":
            raise ClientLocalRerankExecutorError("client_local_lease_response_mismatch")
        if leased.get("status") != "leased" or not isinstance(lease_token, str) or not lease_token:
            raise ClientLocalRerankExecutorError("client_local_job_lease_unavailable")

        package = _package_from_mapping(leased.get("package"))
        if package.model_identity != self._model_identity:
            raise ClientLocalRerankExecutorError("client_local_model_identity_mismatch")
        stop_renewal = asyncio.Event()
        model_task = asyncio.create_task(self._invoke_model(package))
        renewal_task = asyncio.create_task(
            self._renew_until_stopped(job_id, lease_token, stop_renewal)
        )
        completion_task: asyncio.Task[ClientLocalGatewayResponse] | None = None
        try:
            done, _pending = await asyncio.wait(
                {model_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if model_task in done:
                result = model_task.result()
            elif renewal_task in done:
                renewal_error = renewal_task.exception()
                if renewal_error is not None:
                    model_task.cancel()
                    await _consume_cancelled(model_task)
                    raise renewal_error
                raise ClientLocalRerankExecutorError("client_local_renewal_stopped_early")
            completion_task = asyncio.create_task(
                self._transport.request(
                    "POST",
                    f"/v1/rerank/jobs/{job_id}/complete",
                    json={"lease_token": lease_token, "result": result},
                )
            )
            done, _pending = await asyncio.wait(
                {completion_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if completion_task in done:
                completion = completion_task.result()
            else:
                renewal_error = renewal_task.exception()
                if renewal_error is None:
                    renewal_error = ClientLocalRerankExecutorError(
                        "client_local_renewal_stopped_early"
                    )
                # Completion may already have won its server-side CAS while
                # the response is still in flight.  Accept that authoritative
                # response; otherwise report the lease failure.
                try:
                    completion = await completion_task
                except BaseException:
                    raise renewal_error from None
        finally:
            stop_renewal.set()
            for task in (model_task, completion_task):
                if task is not None and not task.done():
                    task.cancel()
                    await _consume_cancelled(task)
            with suppress(BaseException):
                await renewal_task

        completed = completion.payload
        if completed.get("job_id") != job_id or completed.get("status") != "completed":
            raise ClientLocalRerankExecutorError("client_local_completion_response_invalid")
        return completed

    async def _invoke_model(self, package: ClientLocalRerankPackage) -> dict[str, object]:
        candidates = tuple(
            LocalRerankCandidate(
                item_id=item.item_id,
                text=item.text,
                base_score=item.base_score,
            )
            for item in package.candidates
        )
        callable_target = self._rerank
        is_async = inspect.iscoroutinefunction(callable_target) or inspect.iscoroutinefunction(
            type(callable_target).__call__
        )
        if is_async:
            raw = callable_target(package.query, candidates, package.top_k)
        else:
            raw = await asyncio.to_thread(
                callable_target,
                package.query,
                candidates,
                package.top_k,
            )
        if inspect.isawaitable(raw):
            raw = await raw
        result = _result_payload(
            package,
            raw,
        )
        # Mirror the server's authoritative validation before attempting CAS.
        accept_authoritative_client_local_rerank(
            package,
            result,
            authenticated_project_id=package.project_id,
            current_request_id=package.request_id,
        )
        return result

    async def _renew_until_stopped(
        self,
        job_id: str,
        lease_token: str,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._renew_interval_seconds)
                return
            except TimeoutError:
                pass
            renewed = await self._transport.request(
                "POST",
                f"/v1/rerank/jobs/{job_id}/lease/renew",
                json={"lease_token": lease_token},
            )
            payload = renewed.payload
            if (
                payload.get("job_id") != job_id
                or payload.get("target") != "client-local"
                or payload.get("status") != "leased"
            ):
                raise ClientLocalRerankExecutorError("client_local_renewal_response_invalid")


async def _consume_cancelled(task: asyncio.Task[Any]) -> None:
    with suppress(asyncio.CancelledError):
        await task


def _package_from_mapping(value: object) -> ClientLocalRerankPackage:
    if not isinstance(value, Mapping) or set(value) != _PACKAGE_FIELDS:
        raise ClientLocalRerankExecutorError("client_local_package_invalid")
    raw_candidates = value.get("candidates")
    if (
        not isinstance(raw_candidates, list)
        or not raw_candidates
        or len(raw_candidates) > _MAX_CLIENT_LOCAL_CANDIDATES
    ):
        raise ClientLocalRerankExecutorError("client_local_package_invalid")
    try:
        package_bytes = len(
            json_module.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ClientLocalRerankExecutorError("client_local_package_invalid") from None
    if package_bytes > _MAX_CLIENT_LOCAL_PACKAGE_BYTES:
        raise ClientLocalRerankExecutorError("client_local_package_too_large")
    candidates: list[ClientLocalCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping) or set(raw) != _CANDIDATE_FIELDS:
            raise ClientLocalRerankExecutorError("client_local_package_invalid")
        candidates.append(
            ClientLocalCandidate(
                item_id=raw.get("id"),  # type: ignore[arg-type]
                text=raw.get("text"),  # type: ignore[arg-type]
                base_score=raw.get("base_score"),  # type: ignore[arg-type]
                material_sha256=raw.get("material_sha256"),  # type: ignore[arg-type]
                embedding_sha256=raw.get("embedding_sha256"),  # type: ignore[arg-type]
            )
        )
    package = ClientLocalRerankPackage(
        contract_version=value.get("contract_version"),  # type: ignore[arg-type]
        scoring_version=value.get("scoring_version"),  # type: ignore[arg-type]
        project_id=value.get("project_id"),  # type: ignore[arg-type]
        request_id=value.get("request_id"),  # type: ignore[arg-type]
        candidate_set_version=value.get("candidate_set_version"),  # type: ignore[arg-type]
        candidate_set_hash=value.get("candidate_set_hash"),  # type: ignore[arg-type]
        query=value.get("query"),  # type: ignore[arg-type]
        query_hash=value.get("query_hash"),  # type: ignore[arg-type]
        embedding_identity=value.get("embedding_identity"),  # type: ignore[arg-type]
        embedding_dimension=value.get("embedding_dimension"),  # type: ignore[arg-type]
        model_identity=value.get("model_identity"),  # type: ignore[arg-type]
        top_k=value.get("top_k"),  # type: ignore[arg-type]
        candidates=tuple(candidates),
        package_hash=value.get("package_hash"),  # type: ignore[arg-type]
    )
    try:
        validate_client_local_rerank_package(package)
    except (TypeError, ValueError) as exc:
        raise ClientLocalRerankExecutorError(str(exc)) from None
    return package


def _result_payload(
    package: ClientLocalRerankPackage,
    value: object,
) -> dict[str, object]:
    rows = _score_rows(value)
    allowed_ids = [candidate.item_id for candidate in package.candidates]
    allowed = set(allowed_ids)
    scores: dict[str, float] = {}
    for item_id, raw_score in rows:
        checked_id = _bounded_identifier(item_id, reason="client_local_result_id_invalid")
        if checked_id not in allowed or checked_id in scores:
            raise ClientLocalRerankExecutorError("client_local_result_id_invalid")
        if (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, (int, float))
            or not math.isfinite(float(raw_score))
            or not 0.0 <= float(raw_score) <= 1.0
        ):
            raise ClientLocalRerankExecutorError("client_local_result_score_invalid")
        scores[checked_id] = float(raw_score)
    if set(scores) != allowed:
        raise ClientLocalRerankExecutorError("client_local_result_incomplete")
    order = {item_id: index for index, item_id in enumerate(allowed_ids)}
    ranked = sorted(scores.items(), key=lambda row: (-row[1], order[row[0]]))[: package.top_k]
    return {
        "contract_version": CLIENT_LOCAL_RESULT_CONTRACT,
        "package_hash": package.package_hash,
        "model_identity": package.model_identity,
        "items": [{"id": item_id, "score": score} for item_id, score in ranked],
    }


def _score_rows(value: object) -> list[tuple[object, object]]:
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ClientLocalRerankExecutorError("client_local_result_sequence_required")
    rows: list[tuple[object, object]] = []
    for row in value:
        if isinstance(row, LocalRerankScore):
            rows.append((row.item_id, row.score))
        elif isinstance(row, Mapping) and set(row) == {"id", "score"}:
            rows.append((row.get("id"), row.get("score")))
        elif isinstance(row, tuple) and len(row) == 2:
            rows.append((row[0], row[1]))
        else:
            raise ClientLocalRerankExecutorError("client_local_result_row_invalid")
    return rows


def _identifier(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ClientLocalRerankExecutorError(reason)
    return value


def _bounded_identifier(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ClientLocalRerankExecutorError(reason)
    try:
        if len(value.encode("utf-8")) > 512:
            raise ClientLocalRerankExecutorError(reason)
    except UnicodeEncodeError:
        raise ClientLocalRerankExecutorError(reason) from None
    return value


__all__ = [
    "ClientLocalGatewayError",
    "ClientLocalGatewayResponse",
    "ClientLocalGatewayTransport",
    "ClientLocalRerankExecutor",
    "ClientLocalRerankExecutorError",
    "HTTPXClientLocalGatewayTransport",
    "LocalRerankCallable",
    "LocalRerankCandidate",
    "LocalRerankOutput",
    "LocalRerankScore",
]
