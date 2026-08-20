"""Loopback-only HTTP service for Markdown knowledge ingestion.

This service owns the deterministic write path: quarantine upload, source
registration, and job/status reads.  It binds to 127.0.0.1 only and
requires a Bearer token on every route except health.  No database or blob
write happens without explicit enablement.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from plastic_promise.knowledge.blobs import FilesystemBlobStore
from plastic_promise.knowledge.contracts import (
    knowledge_blob_root,
    knowledge_db_path,
    knowledge_feature_gate,
)
from plastic_promise.knowledge.ingestion import IngestCoordinator
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.knowledge.worker import KnowledgeSemanticWorker

if TYPE_CHECKING:
    from starlette.requests import Request

_ALLOWED_UPLOAD_MEDIA_TYPES = frozenset({"text/markdown", "text/plain", "application/octet-stream"})


class KnowledgeIngestServerError(RuntimeError):
    """Raised for unsafe or unsupported server configuration."""


@dataclass(frozen=True)
class KnowledgeIngestSettings:
    """Validated ingestion service settings; token never appears in payloads."""

    enabled: bool = False
    token: str = field(default="", repr=False)
    max_upload_bytes: int = 8 * 1024 * 1024
    max_body_bytes: int = 9 * 1024 * 1024
    bind_host: str = "127.0.0.1"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> KnowledgeIngestSettings:
        source = env if env is not None else os.environ
        enabled = knowledge_feature_gate("PP_KNOWLEDGE_SYSTEM") in {"shadow", "on"}
        token = str(source.get("PP_KNOWLEDGE_API_TOKEN") or "").strip()
        bind_host = str(source.get("PP_KNOWLEDGE_INGEST_BIND_HOST") or "127.0.0.1").strip()
        if not _is_loopback_host(bind_host):
            raise KnowledgeIngestServerError("knowledge_ingest_bind_not_loopback")
        max_upload = int(source.get("PP_KNOWLEDGE_MAX_UPLOAD_BYTES") or 8 * 1024 * 1024)
        if max_upload < 1 or max_upload > 64 * 1024 * 1024:
            raise KnowledgeIngestServerError("knowledge_ingest_max_upload_invalid")
        if token and len(token) > 128:
            raise KnowledgeIngestServerError("knowledge_ingest_token_invalid")
        return cls(
            enabled=enabled,
            token=token,
            max_upload_bytes=max_upload,
            max_body_bytes=max_upload + 1024 * 1024,
            bind_host=bind_host,
        )


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().casefold()
    if value == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


class _IngestDependencies:
    """Bounded runtime dependencies resolved once per request cycle."""

    def __init__(self, settings: KnowledgeIngestSettings) -> None:
        self._settings = settings
        self._blobs = FilesystemBlobStore(knowledge_blob_root())
        repository = KnowledgeRepository(knowledge_db_path())
        self._coordinator = IngestCoordinator(repository, self._blobs, actor="ingest-service")
        self._semantic_worker = KnowledgeSemanticWorker(repository)

    def coordinator(self) -> IngestCoordinator:
        return self._coordinator

    def semantic_worker(self) -> KnowledgeSemanticWorker:
        return self._semantic_worker

    def quarantine(self, data: bytes) -> str:
        """Persist raw upload bytes and return the content digest."""
        return self._blobs.put(data).sha256

    def read_quarantined(self, digest: str) -> bytes:
        return self._blobs.read(digest)


def create_knowledge_ingest_app(settings: KnowledgeIngestSettings) -> Starlette:
    dependencies = _IngestDependencies(settings)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        worker = dependencies.semantic_worker()
        if settings.enabled and worker.enabled:
            task = asyncio.create_task(worker.serve(stop))
        try:
            yield
        finally:
            if task is not None:
                stop.set()
                worker.notify()
                await task

    def _error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=status_code,
            headers=_security_headers(),
        )

    def _admit(request: Request) -> tuple[bool, Response | None]:
        """Enforce loopback client and Bearer token admission."""
        client_host = request.client.host if request.client is not None else ""
        if not _is_loopback_host(client_host):
            return False, _error(
                403, "knowledge_ingest_forbidden", "Only loopback clients are admitted"
            )
        if not settings.enabled:
            return False, _error(503, "knowledge_ingest_disabled", "PP_KNOWLEDGE_SYSTEM is off")
        if not settings.token:
            return False, _error(
                503,
                "knowledge_ingest_token_not_configured",
                "PP_KNOWLEDGE_API_TOKEN must be configured for the ingestion service",
            )
        header = str(request.headers.get("authorization") or "")
        scheme, _, provided = header.partition(" ")
        if scheme.casefold() != "bearer" or not _constant_time_equal(
            str(provided).strip(), settings.token
        ):
            return False, _error(401, "knowledge_ingest_unauthorized", "Invalid bearer token")
        return True, None

    async def health(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok" if settings.enabled else "disabled",
                "enabled": settings.enabled,
                "service": "knowledge-ingest",
                "semantic": dependencies.semantic_worker().snapshot(),
            },
            headers=_security_headers(),
        )

    async def uploads(request: Request) -> Response:
        admitted, rejection = _admit(request)
        if rejection is not None:
            return rejection
        content_type = (
            str(request.headers.get("content-type") or "").split(";")[0].strip().casefold()
        )
        if content_type and content_type not in _ALLOWED_UPLOAD_MEDIA_TYPES:
            return _error(
                415, "knowledge_upload_media_type", "Only Markdown/plain text uploads are supported"
            )
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > settings.max_upload_bytes:
                    return _error(413, "knowledge_upload_too_large", "Upload exceeds size limit")
            except ValueError:
                return _error(400, "knowledge_upload_invalid_length", "Invalid content-length")
        raw = await request.body()
        if len(raw) > settings.max_upload_bytes:
            return _error(413, "knowledge_upload_too_large", "Upload exceeds size limit")
        if not raw:
            return _error(400, "knowledge_upload_empty", "Upload body is empty")
        digest = hashlib.sha256(raw).hexdigest()
        stored = await asyncio.to_thread(dependencies.quarantine, raw)
        if stored != digest:
            return _error(500, "knowledge_upload_store_mismatch", "Quarantine digest mismatch")
        return JSONResponse(
            {
                "upload_id": digest,
                "byte_size": len(raw),
                "quarantined": True,
                "blob_root": str(knowledge_blob_root()),
            },
            headers=_security_headers(),
        )

    async def sources(request: Request) -> Response:
        admitted, rejection = _admit(request)
        if rejection is not None:
            return rejection
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error(400, "knowledge_source_invalid_json", "Request body must be JSON")
        project_id = str(payload.get("project_id") or "").strip()
        source_name = str(payload.get("source_name") or "").strip()
        content_sha256 = str(payload.get("content_sha256") or "").strip()
        space_name = str(payload.get("space_name") or "default").strip() or "default"
        origin_ref = payload.get("origin_ref")
        if not project_id or not source_name or not content_sha256:
            return _error(
                400,
                "knowledge_source_missing_fields",
                "project_id, source_name, and content_sha256 are required",
            )
        try:
            content = await asyncio.to_thread(dependencies.read_quarantined, content_sha256)
        except Exception:
            return _error(404, "knowledge_upload_not_found", "Unknown upload_id")
        try:
            submission = await asyncio.to_thread(
                dependencies.coordinator().submit_source,
                project_id,
                content,
                source_name=source_name,
                space_name=space_name,
                kind="upload",
                origin_ref=origin_ref,
                actor="ingest-service",
            )
        except Exception as exc:
            return _error(
                422, "knowledge_source_submit_failed", f"Submit failed: {type(exc).__name__}"
            )
        job = dependencies.coordinator().get_job(submission.job_id) if submission.job_id else None
        if submission.status in {"done", "done_reused"}:
            dependencies.semantic_worker().notify()
        return JSONResponse(
            {
                "submission": {
                    "job_id": submission.job_id,
                    "source_id": submission.source_id,
                    "reused_version_id": submission.reused_version_id,
                    "status": submission.status,
                },
                "job": _job_projection(job) if job is not None else None,
            },
            headers=_security_headers(),
        )

    async def job_detail(request: Request) -> Response:
        admitted, rejection = _admit(request)
        if rejection is not None:
            return rejection
        project_id = request.query_params.get("project_id") or ""
        try:
            job = dependencies.coordinator().get_job(request.path_params["job_id"])
        except KeyError:
            return _error(404, "knowledge_job_not_found", "Unknown job_id")
        if job.project_id != project_id:
            return _error(404, "knowledge_job_not_found", "Unknown job_id")
        return JSONResponse({"job": _job_projection(job)}, headers=_security_headers())

    async def job_list(request: Request) -> Response:
        admitted, rejection = _admit(request)
        if rejection is not None:
            return rejection
        project_id = request.query_params.get("project_id") or ""
        status = request.query_params.get("status")
        if not project_id:
            return _error(400, "knowledge_jobs_project_required", "project_id is required")
        jobs = dependencies.coordinator().list_jobs(project_id, status=status, limit=100)
        return JSONResponse(
            {"jobs": [_job_projection(job) for job in jobs]},
            headers=_security_headers(),
        )

    async def source_list(request: Request) -> Response:
        admitted, rejection = _admit(request)
        if rejection is not None:
            return rejection
        project_id = request.query_params.get("project_id") or ""
        if not project_id:
            return _error(400, "knowledge_sources_project_required", "project_id is required")
        sources = dependencies.coordinator().list_sources(project_id, limit=100)
        return JSONResponse(
            {
                "sources": [
                    {
                        "id": source.id,
                        "name": source.name,
                        "kind": source.kind,
                        "status": source.status,
                        "active_version_id": source.active_version_id,
                        "created_at": source.created_at,
                        "updated_at": source.updated_at,
                    }
                    for source in sources
                ]
            },
            headers=_security_headers(),
        )

    routes = [
        Route("/v1/health", endpoint=health, methods=["GET"]),
        Route("/v1/uploads", endpoint=uploads, methods=["POST"]),
        Route("/v1/sources", endpoint=sources, methods=["POST"]),
        Route("/v1/jobs/{job_id}", endpoint=job_detail, methods=["GET"]),
        Route("/v1/jobs", endpoint=job_list, methods=["GET"]),
        Route("/v1/sources", endpoint=source_list, methods=["GET"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _job_projection(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "source_id": job.source_id,
        "project_id": job.project_id,
        "stage": job.stage,
        "status": job.status,
        "attempts": job.attempts,
        "error": job.error,
        "result": job.result_json,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    return hmac.compare_digest(left_bytes, right_bytes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9050)
    return parser


async def serve(port: int = 9050) -> None:
    if isinstance(port, bool) or not 1 <= port <= 65_535:
        raise KnowledgeIngestServerError("knowledge_ingest_port_invalid")
    settings = KnowledgeIngestSettings.from_env()
    app = create_knowledge_ingest_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.bind_host,
        port=port,
        log_level="info",
        limit_concurrency=16,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
    )
    await uvicorn.Server(config).serve()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    asyncio.run(serve(arguments.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
