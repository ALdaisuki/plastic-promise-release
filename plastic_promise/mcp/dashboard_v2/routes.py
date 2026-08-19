"""Scoped Dashboard V2 routes with opt-in governed proposal review."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterator
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
from plastic_promise.mcp.dashboard_v2.knowledge_proxy import (
    KnowledgeProxyError,
    forward_job_detail,
    forward_submit,
    forward_upload,
    knowledge_ingest_enabled,
)
from plastic_promise.mcp.dashboard_v2.repository import (
    DashboardCollaborationError,
    DashboardCursorError,
    DashboardRepository,
    redact_value,
)

if TYPE_CHECKING:
    from starlette.requests import Request


RepositoryProvider = Callable[[DashboardScope], AbstractContextManager[DashboardRepository]]
IdentityProvider = Callable[[], dict[str, Any]]
IssueProvider = Callable[[], list[dict[str, Any]]]
ProposalReviewProvider = Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
ProjectScopeProvider = Callable[[], list[dict[str, Any]]]

_STATIC_DIR = Path(__file__).with_name("static")
_ASSET_MEDIA_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_ACTION_HEADER_VALUE = "proposal-review-v1"
_MAX_ACTION_BODY_BYTES = 4096
_MAX_KNOWLEDGE_UPLOAD_BYTES = 8 * 1024 * 1024
_ALLOWED_KNOWLEDGE_UPLOAD_TYPES = frozenset(
    {"text/markdown", "text/plain", "application/octet-stream"}
)
_REJECTION_REASON_CODES = frozenset(
    {
        "duplicate",
        "incorrect",
        "not_durable",
        "not_reusable",
        "outdated",
        "policy_rejected",
        "reviewer_rejected",
    }
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "style-src-attr 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' http://127.0.0.1:9040 "
        "http://127.0.0.1:19040; object-src 'none'; "
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


def _default_project_scope_provider() -> list[dict[str, Any]]:
    """Return bounded project activity metadata from canonical read-only SQLite."""
    database = Path(get_db_path()).expanduser().resolve()
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        aggregates: dict[str, dict[str, Any]] = {}
        sources = (
            ("runtime_events", "created_at", "event_count"),
            ("memory_proposals", "created_at", "proposal_count"),
            ("call_spans", "started_at", "call_count"),
            ("memories", "created_at", "memory_count"),
            ("collaboration_agent_sessions", "updated_at", "agent_session_count"),
            ("collaboration_work_items", "updated_at", "work_item_count"),
            ("collaboration_events", "created_at", "collaboration_event_count"),
        )
        for table, timestamp_column, count_key in sources:
            try:
                rows = connection.execute(
                    f"SELECT project_id, COUNT(*), MAX({timestamp_column}) "
                    f"FROM {table} WHERE project_id IS NOT NULL "
                    f"AND TRIM(project_id) <> '' GROUP BY project_id"
                ).fetchall()
            except sqlite3.DatabaseError:
                continue
            for project_id, count, latest_at in rows:
                normalized = str(project_id or "").strip()
                if not normalized:
                    continue
                item = aggregates.setdefault(
                    normalized,
                    {"project_id": normalized, "latest_at": None},
                )
                item[count_key] = int(count or 0)
                if latest_at and (
                    item["latest_at"] is None or str(latest_at) > str(item["latest_at"])
                ):
                    item["latest_at"] = str(latest_at)
        return sorted(
            aggregates.values(),
            key=lambda item: (str(item.get("latest_at") or ""), str(item["project_id"])),
            reverse=True,
        )
    finally:
        connection.close()


def _knowledge_read_only_repository():
    """Open the knowledge truth store read-only, or None when gated off.

    The dashboard never creates the knowledge database; a missing store is
    reported as an empty projection rather than a write.
    """
    from plastic_promise.knowledge.contracts import (
        knowledge_db_path,
        knowledge_feature_gate,
    )
    from plastic_promise.knowledge.repository import KnowledgeRepository

    if knowledge_feature_gate("PP_KNOWLEDGE_SYSTEM") not in {"shadow", "on"}:
        return None
    return KnowledgeRepository(knowledge_db_path(), read_only=True)


def _json_string_list(value: object, *, limit: int = 50) -> list[str]:
    """Project a bounded string list from an internal JSON column."""
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item)[:500] for item in parsed[:limit] if isinstance(item, str)]


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


def _scope(
    settings: DashboardSettings,
    request: Request,
    project_scope_provider: ProjectScopeProvider,
) -> DashboardScope:
    requested_project_id = request.query_params.get("project_id")
    allowed_project_ids: list[str] | None = None
    if requested_project_id:
        try:
            allowed_project_ids = [
                str(item.get("project_id") or "")
                for item in project_scope_provider()
                if isinstance(item, dict)
            ]
        except (OSError, sqlite3.DatabaseError):
            allowed_project_ids = []
    return resolve_local_scope(
        settings,
        client_host=_client_host(request),
        request_host=request.headers.get("host", ""),
        requested_project_id=requested_project_id,
        allowed_project_ids=allowed_project_ids,
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
    proposal_review_provider: ProposalReviewProvider | None = None,
    project_scope_provider: ProjectScopeProvider | None = None,
) -> list[Route]:
    """Build the V2 route set only when its exact feature gate is enabled."""
    if not settings.enabled:
        return []

    provide_repository = repository_provider or _default_repository_provider
    provide_identity = identity_provider or (lambda: {})
    provide_issues = issue_provider or (lambda: [])
    provide_project_scopes = project_scope_provider or _default_project_scope_provider
    review_actions_enabled = (
        settings.review_actions_enabled and proposal_review_provider is not None
    )

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
            _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        response = FileResponse(_STATIC_DIR / "index.html", media_type="text/html")
        response.headers.update(_SECURITY_HEADERS)
        response.headers["X-Request-ID"] = _request_id(request)
        return response

    async def asset(request: Request) -> Response:
        try:
            _scope(settings, request, provide_project_scopes)
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

    async def scopes(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        try:
            available = provide_project_scopes()
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in available:
            if not isinstance(item, dict):
                continue
            project_id = str(item.get("project_id") or "").strip()
            if not project_id or project_id in seen:
                continue
            seen.add(project_id)
            normalized.append(
                {
                    "project_id": project_id,
                    "latest_at": item.get("latest_at"),
                    **{
                        key: int(item[key])
                        for key in (
                            "event_count",
                            "proposal_count",
                            "call_count",
                            "memory_count",
                            "agent_session_count",
                            "work_item_count",
                            "collaboration_event_count",
                        )
                        if isinstance(item.get(key), (int, float))
                        and not isinstance(item.get(key), bool)
                    },
                }
            )
        if scope.project_id not in seen:
            normalized.append({"project_id": scope.project_id, "latest_at": None})
        normalized.sort(
            key=lambda item: (str(item.get("latest_at") or ""), item["project_id"]),
            reverse=True,
        )
        recommended = normalized[0]["project_id"] if normalized else scope.project_id
        return _response(
            request,
            {
                "data": {
                    "scopes": normalized,
                    "default_project_id": scope.project_id,
                    "recommended_project_id": recommended,
                },
                "scope": scope.to_dict(),
                "degraded": False,
                "warnings": [],
            },
        )

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

    async def collaboration(request: Request) -> Response:
        raw_limit = request.query_params.get("limit")
        try:
            event_limit = 20 if raw_limit in (None, "") else int(raw_limit)
        except (TypeError, ValueError):
            return _error(request, 400, "invalid_limit", "Invalid request filter")
        if not 1 <= event_limit <= 20:
            return _error(request, 400, "invalid_limit", "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.collaboration_snapshot(
                coordination_session_id=request.query_params.get(
                    "coordination_session_id"
                ),
                agent_session_id=request.query_params.get("agent_session_id"),
                role=request.query_params.get("role"),
                event_cursor=request.query_params.get("cursor"),
                event_limit=event_limit,
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

    async def passive_memory(request: Request) -> Response:
        try:
            limit = _limit(request)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.passive_memory_overview(limit=limit),
        )

    async def memory_proposals(request: Request) -> Response:
        try:
            limit = _limit(request)
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        return _run_repository(
            request,
            lambda repository: repository.list_memory_proposals(
                limit=limit,
                cursor=request.query_params.get("cursor"),
                status=request.query_params.get("status"),
                category=request.query_params.get("category"),
            ),
        )

    async def review_memory_proposal(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        fetch_site = str(request.headers.get("sec-fetch-site") or "").casefold()
        if fetch_site == "cross-site":
            return _error(
                request,
                403,
                "dashboard_action_cross_site",
                "Cross-site dashboard actions are not allowed",
            )
        if request.headers.get("x-pp-dashboard-action") != _ACTION_HEADER_VALUE:
            return _error(
                request,
                403,
                "dashboard_action_confirmation_required",
                "Dashboard action confirmation header is required",
            )
        content_type = str(request.headers.get("content-type") or "").casefold()
        if not content_type.startswith("application/json"):
            return _error(
                request,
                415,
                "dashboard_action_json_required",
                "Dashboard actions require application/json",
            )
        body = await request.body()
        if len(body) > _MAX_ACTION_BODY_BYTES:
            return _error(
                request,
                413,
                "dashboard_action_too_large",
                "Dashboard action payload is too large",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(request, 400, "dashboard_action_invalid", "Invalid JSON payload")
        if not isinstance(payload, dict):
            return _error(request, 400, "dashboard_action_invalid", "JSON object required")
        feedback_type = str(payload.get("feedback_type") or "").strip().casefold()
        if feedback_type not in {"adopted", "rejected"}:
            return _error(
                request,
                400,
                "proposal_feedback_invalid",
                "feedback_type must be adopted or rejected",
            )
        rejection_reason = (
            str(payload.get("rejection_reason") or "reviewer_rejected").strip().casefold()
        )
        if feedback_type == "rejected" and rejection_reason not in _REJECTION_REASON_CODES:
            return _error(
                request,
                400,
                "proposal_rejection_reason_invalid",
                "Unsupported proposal rejection reason",
            )
        proposal_id = str(request.path_params.get("proposal_id") or "").strip()
        try:
            with provide_repository(scope) as repository:
                proposal = repository.get_memory_proposal(proposal_id)
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)
        if proposal is None:
            return _not_found(request, "memory_proposal")
        if proposal_review_provider is None:
            return _error(
                request,
                503,
                "proposal_review_unavailable",
                "Proposal review is temporarily unavailable",
            )
        try:
            result = await proposal_review_provider(
                proposal_id,
                feedback_type,
                rejection_reason,
                scope.project_id,
            )
        except Exception:
            return _error(
                request,
                503,
                "proposal_review_failed",
                "Proposal review failed",
            )
        if not isinstance(result, dict):
            return _error(
                request,
                503,
                "proposal_review_invalid_response",
                "Proposal review returned an invalid response",
            )
        if not result.get("updated"):
            reason = str(result.get("reason") or "proposal_review_rejected")
            if reason == "feedback_project_mismatch":
                return _not_found(request, "memory_proposal")
            status_code = 403 if reason == "feedback_runtime_authorization_denied" else 409
            return _error(request, status_code, reason, "Proposal review was not applied")
        return _response(request, _with_scope(redact_value(result), scope))

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
            if settings.review_actions_enabled and not review_actions_enabled:
                degraded = True
                warnings.append("proposal_review_provider_unavailable")
            feature_defaults = {
                "dashboard": {
                    "enabled": settings.enabled,
                    "default": True,
                    "key": "PP_DASHBOARD_V2",
                },
                "retrieval_explain": {
                    "enabled": settings.explain_enabled,
                    "default": True,
                    "key": "PP_RETRIEVAL_EXPLAIN",
                },
                "structured_slicing": {
                    "mode": _mode("PP_MEMORY_CHUNKING", {"off", "shadow", "structure-v1"}, "structure-v1"),
                    "default": "structure-v1",
                    "key": "PP_MEMORY_CHUNKING",
                },
                "semantic_enrichment": {
                    "mode": _mode("PP_MEMORY_CHUNK_ENRICHMENT", {"off", "shadow", "on"}, "shadow"),
                    "provider": _mode(
                        "PP_MEMORY_CHUNK_ENRICHMENT_PROVIDER",
                        {"ollama", "openai-compatible"},
                        "openai-compatible",
                    ),
                    "default": "shadow",
                    "key": "PP_MEMORY_CHUNK_ENRICHMENT",
                },
                "knowledge_semantic": {
                    "mode": _mode("PP_KNOWLEDGE_SEMANTIC", {"off", "shadow", "on"}, "shadow"),
                    "default": "shadow",
                    "key": "PP_KNOWLEDGE_SEMANTIC",
                },
                "cloud_inference": {
                    "mode": _mode(
                        "PP_LOCAL_NODE_PROVIDER_MODE",
                        {"local", "cloud", "hybrid"},
                        "local",
                    ),
                    "key": "PP_LOCAL_NODE_PROVIDER_MODE",
                    "credentials_in": "pp-compute-node",
                },
            }
            return {
                "version": version,
                "dashboard": {
                    "enabled": settings.enabled,
                    "retrieval_explain_enabled": settings.explain_enabled,
                    "proposal_review_enabled": review_actions_enabled,
                    "knowledge_enabled": _knowledge_read_only_repository() is not None,
                    "proposal_review_requested": settings.review_actions_enabled,
                    "auth_mode": settings.auth_mode,
                    "project_id": settings.project_id,
                    "bind_host": settings.bind_host,
                    "read_only": not review_actions_enabled,
                    "write_surface": "proposal_review_only" if review_actions_enabled else "none",
                },
                "feature_defaults": feature_defaults,
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
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        try:
            with provide_repository(scope) as repository:
                result = read(repository)
        except DashboardCursorError as exc:
            return _error(request, 400, "invalid_cursor", str(exc))
        except DashboardCollaborationError as exc:
            return _error(
                request,
                503,
                exc.code,
                "Collaboration projection is unavailable",
            )
        except ValueError as exc:
            return _error(request, 400, "invalid_filter", str(exc))
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)
        if isinstance(result, _Missing):
            return _not_found(request, result.resource)
        return _response(request, _with_scope(result, scope))

    async def knowledge_sources(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
            limit = _limit(request)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {
                        "enabled": False,
                        "sources": [],
                        "note": "PP_KNOWLEDGE_SYSTEM is off",
                    },
                    scope,
                ),
            )
        try:
            sources = repository.list_sources(scope.project_id, limit=limit)
            rows = []
            for source in sources:
                versions = repository.list_versions(source.id, limit=3)
                rows.append(
                    {
                        "id": source.id,
                        "name": source.name,
                        "kind": source.kind,
                        "status": source.status,
                        "origin_ref": source.origin_ref,
                        "active_version_id": source.active_version_id,
                        "created_at": source.created_at,
                        "updated_at": source.updated_at,
                        "versions": [
                            {
                                "id": version.id,
                                "version_no": version.version_no,
                                "status": version.status,
                                "chunk_count": version.chunk_count,
                                "created_at": version.created_at,
                            }
                            for version in versions
                        ],
                    }
                )
        except (OSError, sqlite3.DatabaseError, KeyError):
            return _repository_error(request)
        return _response(request, _with_scope({"enabled": True, "sources": rows}, scope))

    async def knowledge_jobs(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
            limit = _limit(request)
            status = request.query_params.get("status")
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {
                        "enabled": False,
                        "jobs": [],
                        "note": "PP_KNOWLEDGE_SYSTEM is off",
                    },
                    scope,
                ),
            )
        try:
            jobs = repository.list_jobs(scope.project_id, status=status, limit=limit)
            rows = [
                {
                    "id": job.id,
                    "source_id": job.source_id,
                    "stage": job.stage,
                    "status": job.status,
                    "attempts": job.attempts,
                    "error": job.error,
                    "result": job.result_json,
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                }
                for job in jobs
            ]
        except (OSError, sqlite3.DatabaseError, KeyError):
            return _repository_error(request)
        return _response(request, _with_scope({"enabled": True, "jobs": rows}, scope))

    async def knowledge_semantic(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        repository = _knowledge_read_only_repository()
        empty_status = {"pending": 0, "building": 0, "done": 0, "failed": 0}
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {
                        "enabled": False,
                        "mode": "off",
                        "status": empty_status,
                        "authority": "sqlite",
                        "derived_index": "rebuildable_only",
                        "note": "PP_KNOWLEDGE_SYSTEM is off",
                    },
                    scope,
                ),
            )
        from plastic_promise.knowledge.contracts import knowledge_feature_gate

        try:
            status = repository.semantic_status(scope.project_id)
        except (OSError, sqlite3.DatabaseError, KeyError):
            return _repository_error(request)
        return _response(
            request,
            _with_scope(
                {
                    "enabled": True,
                    "mode": knowledge_feature_gate("PP_KNOWLEDGE_SEMANTIC"),
                    "status": status,
                    "authority": "sqlite",
                    "derived_index": "rebuildable_only",
                },
                scope,
            ),
        )

    async def knowledge_domains(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
            limit = _limit(request, default=100)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {
                        "enabled": False,
                        "mode": "off",
                        "domains": [],
                        "note": "PP_KNOWLEDGE_SYSTEM is off",
                    },
                    scope,
                ),
            )
        from plastic_promise.knowledge.contracts import knowledge_feature_gate

        try:
            domains = repository.list_domains(scope.project_id)[:limit]
        except (OSError, sqlite3.DatabaseError, KeyError):
            return _repository_error(request)
        rows = [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "description": row.get("description"),
                "kind": row.get("kind"),
                "parent_domain_id": row.get("parent_domain_id"),
                "aliases": _json_string_list(row.get("aliases_json"), limit=20),
                "source_count": int(row.get("source_count") or 0),
                "distinct_spaces": int(row.get("distinct_spaces") or 0),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "retired_at": row.get("retired_at"),
            }
            for row in domains
        ]
        return _response(
            request,
            _with_scope(
                {
                    "enabled": True,
                    "mode": knowledge_feature_gate("PP_KNOWLEDGE_AUTO_DOMAINS"),
                    "domains": rows,
                },
                scope,
            ),
        )

    async def knowledge_artifacts(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
            limit = _limit(request, default=100)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {
                        "enabled": False,
                        "mode": "off",
                        "artifacts": [],
                        "note": "PP_KNOWLEDGE_SYSTEM is off",
                    },
                    scope,
                ),
            )
        from plastic_promise.knowledge.contracts import knowledge_feature_gate

        try:
            artifacts = repository.list_artifacts(scope.project_id, limit=limit)
        except (OSError, sqlite3.DatabaseError, KeyError):
            return _repository_error(request)
        rows = [
            {
                "id": row.get("id"),
                "kind": row.get("kind"),
                "title": row.get("title"),
                "content": row.get("content"),
                "status": row.get("status"),
                "risk_tier": row.get("risk_tier"),
                "citation_coverage": float(row.get("citation_coverage") or 0.0),
                "source_ids": _json_string_list(row.get("source_ids_json"), limit=50),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            for row in artifacts
        ]
        return _response(
            request,
            _with_scope(
                {
                    "enabled": True,
                    "mode": knowledge_feature_gate("PP_KNOWLEDGE_WIKI"),
                    "artifacts": rows,
                },
                scope,
            ),
        )

    async def knowledge_upload(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        if not knowledge_ingest_enabled():
            return _response(
                request,
                _with_scope(
                    {"enabled": False, "note": "PP_KNOWLEDGE_SYSTEM is off"},
                    scope,
                ),
            )
        content_type = (
            str(request.headers.get("content-type") or "text/markdown")
            .split(";")[0]
            .strip()
            .casefold()
        )
        if content_type and content_type not in _ALLOWED_KNOWLEDGE_UPLOAD_TYPES:
            return _error(
                request,
                415,
                "knowledge_upload_media_type",
                "Only Markdown/plain text uploads are supported",
            )
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > _MAX_KNOWLEDGE_UPLOAD_BYTES:
                    return _error(
                        request,
                        413,
                        "knowledge_upload_too_large",
                        "Upload exceeds the 8 MiB limit",
                    )
            except ValueError:
                return _error(
                    request,
                    400,
                    "knowledge_upload_invalid_length",
                    "Invalid content-length",
                )
        raw = await request.body()
        try:
            payload = await asyncio.to_thread(
                forward_upload,
                raw,
                content_type or "text/markdown",
            )
        except KnowledgeProxyError as exc:
            return _error(request, exc.status_code, exc.code, str(exc))
        return _response(request, _with_scope(payload, scope))

    async def knowledge_source_submit(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        if not knowledge_ingest_enabled():
            return _response(
                request,
                _with_scope(
                    {"enabled": False, "note": "PP_KNOWLEDGE_SYSTEM is off"},
                    scope,
                ),
            )
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _error(
                request,
                400,
                "knowledge_source_invalid_json",
                "Request body must be JSON",
            )
        if not isinstance(payload, dict):
            return _error(
                request,
                400,
                "knowledge_source_invalid_json",
                "Request body must be a JSON object",
            )
        project_id = str(payload.get("project_id") or "").strip()
        source_name = str(payload.get("source_name") or "").strip()
        content_sha256 = str(payload.get("content_sha256") or "").strip()
        if project_id != scope.project_id:
            return _error(
                request,
                403,
                "knowledge_source_cross_project",
                "project_id must match the active dashboard scope",
            )
        if not source_name or not content_sha256:
            return _error(
                request,
                400,
                "knowledge_source_missing_fields",
                "source_name and content_sha256 are required",
            )
        forward_payload = {
            "project_id": project_id,
            "source_name": source_name,
            "content_sha256": content_sha256,
            "space_name": str(payload.get("space_name") or "default").strip() or "default",
            "origin_ref": payload.get("origin_ref"),
        }
        try:
            result = await asyncio.to_thread(forward_submit, forward_payload)
        except KnowledgeProxyError as exc:
            return _error(request, exc.status_code, exc.code, str(exc))
        return _response(request, _with_scope(result, scope))

    async def knowledge_job_detail(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        if not knowledge_ingest_enabled():
            return _response(
                request,
                _with_scope(
                    {"enabled": False, "note": "PP_KNOWLEDGE_SYSTEM is off"},
                    scope,
                ),
            )
        job_id = str(request.path_params.get("job_id") or "")
        try:
            result = await asyncio.to_thread(forward_job_detail, job_id, scope.project_id)
        except KnowledgeProxyError as exc:
            return _error(request, exc.status_code, exc.code, str(exc))
        return _response(request, _with_scope(result, scope))

    def _knowledge_source_or_none(
        scope: DashboardScope,
        repository: Any,
        source_id: str,
    ) -> Any:
        try:
            source = repository.get_source(source_id)
        except KeyError:
            return None
        if source.project_id != scope.project_id:
            return None
        return source

    async def knowledge_source_versions(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
            limit = _limit(request)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        except ValueError as exc:
            return _error(request, 400, str(exc), "Invalid request filter")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {"enabled": False, "note": "PP_KNOWLEDGE_SYSTEM is off"},
                    scope,
                ),
            )
        source_id = str(request.path_params.get("source_id") or "")
        try:
            source = _knowledge_source_or_none(scope, repository, source_id)
            if source is None:
                return _not_found(request, "knowledge_source")
            versions = repository.list_versions(source_id, limit=limit)
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)
        rows = [
            {
                "id": version.id,
                "version_no": version.version_no,
                "byte_size": version.byte_size,
                "parser_id": version.parser_id,
                "parse_schema": version.parse_schema,
                "document_title": version.document_title,
                "status": version.status,
                "chunk_count": version.chunk_count,
                "created_at": version.created_at,
            }
            for version in versions
        ]
        return _response(
            request,
            _with_scope(
                {
                    "enabled": True,
                    "source": {
                        "id": source.id,
                        "name": source.name,
                        "kind": source.kind,
                        "status": source.status,
                    },
                    "versions": rows,
                },
                scope,
            ),
        )

    async def knowledge_source_chunks(request: Request) -> Response:
        try:
            scope = _scope(settings, request, provide_project_scopes)
        except DashboardAccessError as exc:
            return _error(request, exc.status_code, exc.code, "Dashboard access denied")
        repository = _knowledge_read_only_repository()
        if repository is None:
            return _response(
                request,
                _with_scope(
                    {"enabled": False, "note": "PP_KNOWLEDGE_SYSTEM is off"},
                    scope,
                ),
            )
        source_id = str(request.path_params.get("source_id") or "")
        try:
            source = _knowledge_source_or_none(scope, repository, source_id)
            if source is None:
                return _not_found(request, "knowledge_source")
            chunks = repository.list_chunks(source_id, limit=200)
        except (OSError, sqlite3.DatabaseError):
            return _repository_error(request)
        return _response(
            request,
            _with_scope(
                {
                    "enabled": True,
                    "source_id": source_id,
                    "chunks": chunks,
                },
                scope,
            ),
        )

    routes = [
        Route("/dashboard", endpoint=dashboard, methods=["GET"]),
        Route("/dashboard/assets/v2/{asset_name}", endpoint=asset, methods=["GET"]),
        Route("/api/dashboard/v2/overview", endpoint=overview, methods=["GET"]),
        Route("/api/dashboard/v2/scopes", endpoint=scopes, methods=["GET"]),
        Route("/api/dashboard/v2/collaboration", endpoint=collaboration, methods=["GET"]),
        Route("/api/dashboard/v2/requests", endpoint=requests, methods=["GET"]),
        Route("/api/dashboard/v2/memories", endpoint=memories, methods=["GET"]),
        Route("/api/dashboard/v2/passive-memory", endpoint=passive_memory, methods=["GET"]),
        Route(
            "/api/dashboard/v2/memory-proposals",
            endpoint=memory_proposals,
            methods=["GET"],
        ),
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
        Route(
            "/api/dashboard/v2/knowledge-sources",
            endpoint=knowledge_sources,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-jobs",
            endpoint=knowledge_jobs,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-semantic",
            endpoint=knowledge_semantic,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-domains",
            endpoint=knowledge_domains,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-artifacts",
            endpoint=knowledge_artifacts,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-uploads",
            endpoint=knowledge_upload,
            methods=["POST"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-sources/submit",
            endpoint=knowledge_source_submit,
            methods=["POST"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-jobs/{job_id}",
            endpoint=knowledge_job_detail,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-sources/{source_id}/versions",
            endpoint=knowledge_source_versions,
            methods=["GET"],
        ),
        Route(
            "/api/dashboard/v2/knowledge-sources/{source_id}/chunks",
            endpoint=knowledge_source_chunks,
            methods=["GET"],
        ),
    ]
    if review_actions_enabled:
        routes.append(
            Route(
                "/api/dashboard/v2/memory-proposals/{proposal_id}/review",
                endpoint=review_memory_proposal,
                methods=["POST"],
            )
        )
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
