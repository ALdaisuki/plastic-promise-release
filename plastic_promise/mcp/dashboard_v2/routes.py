"""Read-only Starlette routes for the scoped Dashboard V2 application."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from plastic_promise.core.paths import get_db_path
from plastic_promise.core.retrieval_explain import sanitize_retrieval_explain_snapshot
from plastic_promise.mcp.dashboard_v2.config import (
    DashboardAccessError,
    DashboardScope,
    DashboardSettings,
    resolve_local_scope,
)
from plastic_promise.mcp.dashboard_v2.repository import (
    DashboardCursorError,
    DashboardRepository,
    redact_value,
)

if TYPE_CHECKING:
    from starlette.requests import Request


RepositoryProvider = Callable[[DashboardScope], AbstractContextManager[DashboardRepository]]
IdentityProvider = Callable[[], dict[str, Any]]
IssueProvider = Callable[[], list[dict[str, Any]]]

_STATIC_DIR = Path(__file__).with_name("static")
_ASSET_MEDIA_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "style-src-attr 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@contextmanager
def _default_repository_provider(scope: DashboardScope) -> Iterator[DashboardRepository]:
    """Open the canonical SQLite store in read-only/query-only mode."""
    database = Path(get_db_path()).expanduser().resolve()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        yield DashboardRepository(connection, scope)
    finally:
        connection.close()


def _request_id(request: Request) -> str:
    supplied = str(request.headers.get("x-request-id") or "").strip()
    if supplied and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied):
        return supplied
    return f"dash_{uuid.uuid4().hex}"


def _response(
    request: Request,
    payload: Any,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = dict(_SECURITY_HEADERS)
    response_headers["X-Request-ID"] = _request_id(request)
    if headers:
        response_headers.update(headers)
    return JSONResponse(payload, status_code=status_code, headers=response_headers)


def _error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return _response(
        request,
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _client_host(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def _scope(settings: DashboardSettings, request: Request) -> DashboardScope:
    return resolve_local_scope(
        settings,
        client_host=_client_host(request),
        request_host=request.headers.get("host", ""),
        requested_project_id=request.query_params.get("project_id"),
    )


def _limit(request: Request, *, default: int = 25) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_limit") from exc
    if value < 1 or value > 100:
        raise ValueError("invalid_limit")
    return value


def _optional_bool(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    normalized = raw.casefold()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise ValueError(f"invalid_{name}")


def _not_found(request: Request, resource: str) -> JSONResponse:
    title = resource.replace("_", " ").capitalize()
    return _error(request, 404, f"{resource}_not_found", f"{title} not found")


def _repository_error(request: Request) -> JSONResponse:
    return _error(
        request,
        503,
        "dashboard_data_unavailable",
        "Dashboard data is temporarily unavailable",
    )


def _with_scope(payload: Any, scope: DashboardScope) -> dict[str, Any]:
    existing_scope = payload.get("scope") if isinstance(payload, dict) else None
    if (
        isinstance(existing_scope, dict)
        and "project_id" in existing_scope
        and "auth_mode" in existing_scope
    ):
        return payload
    if isinstance(payload, dict) and "data" in payload:
        envelope = dict(payload)
        envelope.update(
            {
                "scope": scope.to_dict(),
                "degraded": bool(payload.get("degraded", False)),
                "warnings": list(payload.get("warnings") or []),
            }
        )
        return envelope
    degraded = bool(payload.get("degraded", False)) if isinstance(payload, dict) else False
    warnings = list(payload.get("warnings") or []) if isinstance(payload, dict) else []
    return {
        "data": payload,
        "scope": scope.to_dict(),
        "degraded": degraded,
        "warnings": warnings,
    }


def _mode(name: str, allowed: set[str], default: str) -> str:
    value = str(os.environ.get(name, default)).strip().casefold()
    return value if value in allowed else "invalid"


def _synthesis_governance() -> dict[str, Any]:
    artifacts_mode = _mode("PP_SYNTHESIS_ARTIFACTS", {"off", "shadow", "on"}, "off")
    retrieval_enabled = os.environ.get("PP_SYNTHESIS_RETRIEVAL", "0") == "1"
    return {
        "source_of_truth": "synthesis_artifacts",
        "artifacts_mode": artifacts_mode,
        "creation_enabled": artifacts_mode == "on",
        "retrieval_enabled": retrieval_enabled,
        "retrieval_effective": artifacts_mode == "on" and retrieval_enabled,
        "proposal_mode": _mode("PP_MEMORY_PROPOSALS", {"off", "shadow", "on"}, "off"),
    }


def _call_summary(call: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "call_id",
        "tool_name",
        "status",
        "degraded",
        "started_at",
        "ended_at",
        "duration_ms",
        "duration_status",
        "request_scope_id",
        "project_id",
    )
    return {field: call.get(field) for field in fields}


def create_dashboard_v2_routes(
    settings: DashboardSettings,
    *,
    repository_provider: RepositoryProvider | None = None,
    version: str = "",
    identity_provider: IdentityProvider | None = None,
    issue_provider: IssueProvider | None = None,
) -> list[Route]:
    """Build the V2 route set only when its exact feature gate is enabled."""
    if not settings.enabled:
        return []

    provide_repository = repository_provider or _default_repository_provider
    provide_identity = identity_provider or (lambda: {})
    provide_issues = issue_provider or (lambda: [])

    def system_issues() -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Project the process-local issue board without claiming project ownership.

        Issues predate the project-scoped SQLite schema and are deliberately
        process-local.  The legacy HTTP API already exposes this same board;
        Dashboard V2 presents it as a clearly labelled system projection.
        """
        try:
            raw_issues = provide_issues()
        except Exception:
            raw_issues = []
        rows = [redact_value(issue) for issue in raw_issues if isinstance(issue, dict)]
        return rows, {
            "authority_scope": "system_global",
            "source": "process_issue_manager",
            "mode": "read_only_projection",
        }

    def system_trust(repository: DashboardRepository) -> dict[str, Any] | None:
        trust = repository.get_trust("")
        if not isinstance(trust, dict):
            return None
        return {**trust, "authority_scope": "system_global"}

    async def dashboard(request: Request) -> Response:
        try:
            _scope(settings, request)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        response = FileResponse(_STATIC_DIR / "index.html", media_type="text/html")
        response.headers.update(_SECURITY_HEADERS)
        response.headers["X-Request-ID"] = _request_id(request)
        return response

    async def asset(request: Request) -> Response:
        try:
            _scope(settings, request)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        asset_name = str(request.path_params.get("asset_name") or "")
        media_type = _ASSET_MEDIA_TYPES.get(asset_name)
        if media_type is None:
            return _not_found(request, "asset")
        response = FileResponse(_STATIC_DIR / asset_name, media_type=media_type)
        response.headers.update(_SECURITY_HEADERS)
        response.headers["X-Request-ID"] = _request_id(request)
        return response

    async def overview(request: Request) -> Response:
        def read(repository: DashboardRepository) -> dict[str, Any]:
            result = repository.overview()
            if "scope" in result:
                return result
            try:
                identity = provide_identity()
            except Exception:
                identity = {}
            runtime = identity.get("runtime") if isinstance(identity, dict) else None
            runtime_mode = identity.get("runtime_mode") if isinstance(identity, dict) else None
            if not runtime_mode and isinstance(runtime, dict):
                runtime_mode = runtime.get("mode")
            trust = system_trust(repository)
            trust_tier = trust.get("tier") if isinstance(trust, dict) else "unavailable"
            trust_score = trust.get("trust") if isinstance(trust, dict) else None
            issues, issue_scope = system_issues()
            return {
                **result,
                "runtime_mode": runtime_mode or "unknown",
                "trust": trust,
                "issues": issues,
                "issue_scope": issue_scope,
                "readiness": {
                    "status": "ready",
                    "components": [
                        {
                            "name": "Canonical SQLite",
                            "status": "ready",
                            "detail": "read-only projection",
                        },
                        {
                            "name": "Project authority",
                            "status": "ready",
                            "detail": repository.scope.project_id,
                        },
                        {
                            "name": "Retrieval explain",
                            "status": "enabled" if settings.explain_enabled else "disabled",
                            "detail": "bounded stored snapshots",
                        },
                        {
                            "name": "Trust",
                            "status": trust_tier,
                            "detail": "unavailable" if trust_score is None else str(trust_score),
                        },
                        {
                            "name": "Issues",
                            "status": "ready",
                            "detail": "system_global_read_only_projection",
                        },
                    ],
                },
            }

        return _run_repository(request, read)

    async def requests(request: Request) -> Response:
        try:
            limit = _limit(request)
            degraded = _optional_bool(request, "degraded")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.list_requests(
                limit=limit,
                cursor=request.query_params.get("cursor"),
                status=request.query_params.get("status"),
                tool_name=request.query_params.get("tool_name"),
                degraded=degraded,
            ),
        )

    async def memories(request: Request) -> Response:
        try:
            limit = _limit(request)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.list_memories(
                limit=limit,
                cursor=request.query_params.get("cursor"),
                memory_type=request.query_params.get("memory_type"),
                query=request.query_params.get("query"),
            ),
        )

    async def memory_detail(request: Request) -> Response:
        memory_id = str(request.path_params["memory_id"])

        def read(repository: DashboardRepository) -> Any:
            result = repository.get_memory(memory_id)
            return result if result is not None else _Missing("memory")

        return _run_repository(request, read)

    async def lineage(request: Request) -> Response:
        memory_id = str(request.path_params["memory_id"])
        try:
            limit = _limit(request, default=100)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")

        def read(repository: DashboardRepository) -> Any:
            result = repository.get_lineage(memory_id, limit=limit)
            if result is None:
                return _Missing("memory")
            if isinstance(result, dict):
                return result
            return {"memory_id": memory_id, "data": result}

        return _run_repository(request, read)

    async def synthesis(request: Request) -> Response:
        try:
            limit = _limit(request)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        def read(repository: DashboardRepository) -> dict[str, Any]:
            result = repository.list_synthesis(
                limit=limit,
                cursor=request.query_params.get("cursor"),
                status=request.query_params.get("status"),
            )
            result["governance"] = _synthesis_governance()
            return result

        return _run_repository(request, read)

    async def operations(request: Request) -> Response:
        try:
            limit = _limit(request)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.list_operations(
                limit=limit,
                cursor=request.query_params.get("cursor"),
                kind=request.query_params.get("kind"),
                status=request.query_params.get("status"),
            ),
        )

    async def trust_issues(request: Request) -> Response:
        if request.query_params.get("target") not in {None, ""}:
            return _error(
                request,
                400,
                "trust_target_not_supported",
                "Trust target selection requires an ownership model",
            )

        def read(repository: DashboardRepository) -> dict[str, Any]:
            issues, issue_scope = system_issues()
            return {
                "trust": system_trust(repository),
                "issues": issues,
                "issue_scope": issue_scope,
            }

        return _run_repository(request, read)

    async def configuration(request: Request) -> Response:
        def read(_repository: DashboardRepository) -> dict[str, Any]:
            degraded = False
            warnings: list[str] = []
            try:
                identity = redact_value(provide_identity())
            except Exception:
                identity = {"status": "unavailable"}
                degraded = True
                warnings.append("runtime_identity_unavailable")
            return {
                "version": version,
                "dashboard": {
                    "enabled": settings.enabled,
                    "retrieval_explain_enabled": settings.explain_enabled,
                    "auth_mode": settings.auth_mode,
                    "project_id": settings.project_id,
                    "bind_host": settings.bind_host,
                    "read_only": True,
                },
                "memory_governance": _synthesis_governance(),
                "runtime": identity,
                "degraded": degraded,
                "warnings": warnings,
            }

        return _run_repository(request, read)

    async def retrieval_explain(request: Request) -> Response:
        call_id = str(request.query_params.get("call_id") or "").strip()
        if not call_id:
            return _error(request, 400, "call_id_required", "call_id is required")

        def read(repository: DashboardRepository) -> Any:
            call = repository.get_request(call_id)
            if call is None:
                return _Missing("request")
            metadata = call.get("metadata") if isinstance(call, dict) else None
            stored_snapshot = (
                metadata.get("retrieval_explain_v1") if isinstance(metadata, dict) else None
            )
            snapshot = sanitize_retrieval_explain_snapshot(stored_snapshot)
            call_summary = _call_summary(call)
            if snapshot is None:
                return {
                    "call_id": call_id,
                    "availability": "unavailable",
                    "reason": "snapshot_not_captured",
                    "snapshot": None,
                    "call": call_summary,
                }
            enrich = getattr(repository, "enrich_retrieval_explain", None)
            if callable(enrich):
                enriched = enrich(snapshot)
                if isinstance(enriched, dict) and enriched:
                    snapshot = enriched
            return {
                **snapshot,
                "call_id": call_id,
                "availability": "available",
                "call": call_summary,
            }

        return _run_repository(request, read)

    def _run_repository(request: Request, read: Callable[[DashboardRepository], Any]) -> Response:
        try:
            scope = _scope(settings, request)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        try:
            with provide_repository(scope) as repository:
                result = read(repository)
        except DashboardCursorError as exc:
            return _error(request, 400, "invalid_cursor", str(exc))
        except ValueError as exc:
            return _error(request, 400, "invalid_filter", str(exc))
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)
        if isinstance(result, _Missing):
            return _not_found(request, result.resource)
        return _response(request, _with_scope(result, scope))

    routes = [
        Route("/dashboard", endpoint=dashboard, methods=["GET"]),
        Route("/dashboard/assets/v2/{asset_name}", endpoint=asset, methods=["GET"]),
        Route("/api/dashboard/v2/overview", endpoint=overview, methods=["GET"]),
        Route("/api/dashboard/v2/requests", endpoint=requests, methods=["GET"]),
        Route("/api/dashboard/v2/memories", endpoint=memories, methods=["GET"]),
        Route(
            "/api/dashboard/v2/memories/{memory_id}/lineage",
            endpoint=lineage,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/memories/{memory_id}",
            endpoint=memory_detail,
            methods=["GET"],
        ),
        Route("/api/dashboard/v2/synthesis", endpoint=synthesis, methods=["GET"]),
        Route("/api/dashboard/v2/operations", endpoint=operations, methods=["GET"]),
        Route("/api/dashboard/v2/trust-issues", endpoint=trust_issues, methods=["GET"]),
        Route("/api/dashboard/v2/configuration", endpoint=configuration, methods=["GET"]),
    ]
    if settings.explain_enabled:
        routes.append(
            Route(
                "/api/dashboard/v2/retrieval-explain",
                endpoint=retrieval_explain,
                methods=["GET"],
            )
        )
    return routes


class _Missing:
    def __init__(self, resource: str) -> None:
        self.resource = resource


__all__ = ["create_dashboard_v2_routes"]
