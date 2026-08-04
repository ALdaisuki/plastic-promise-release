"""Loopback-only remote configuration and server-status control plane."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from plastic_promise.control_plane.auth import (
    ControlPlaneAuthenticationError,
    ControlPlaneAuthenticator,
    ControlPlanePrincipal,
)
from plastic_promise.control_plane.store import ControlPlaneConfigStore, ControlPlaneError
from plastic_promise.core.paths import inference_jobs_path_for
from plastic_promise.core.server_status import ServerStatusSettings, collect_server_status

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from starlette.requests import Request

_MAX_BODY_BYTES = 64 * 1024
_BODY_TIMEOUT_SECONDS = 5.0
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_REVISION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ERROR_RE = re.compile(r"control_[a-z0-9_]{1,96}|embedding_generation_required")
_DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:19020",
    "http://127.0.0.1:9020",
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    # Dashboard V2 is served from the MCP port and calls this loopback-only
    # service on a different port. CORS still enforces the exact origin list.
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ControlPlaneHTTPError(ValueError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class ControlPlaneSettings:
    enabled: bool
    root: Path
    authenticator: ControlPlaneAuthenticator | None
    status: ServerStatusSettings
    bind_host: str = "127.0.0.1"
    allowed_origins: tuple[str, ...] = _DEFAULT_ALLOWED_ORIGINS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("control_enabled_invalid")
        if not _is_loopback_host(self.bind_host):
            raise ValueError("control_bind_not_loopback")
        if not isinstance(self.root, Path) or "\x00" in str(self.root):
            raise ValueError("control_root_invalid")
        if self.enabled and self.authenticator is None:
            raise ValueError("control_credentials_missing")
        normalized_origins = tuple(
            _normalize_loopback_origin(item) for item in self.allowed_origins
        )
        if normalized_origins != self.allowed_origins or len(set(normalized_origins)) != len(
            normalized_origins
        ):
            raise ValueError("control_allowed_origins_invalid")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, object] | None = None,
        *,
        bind_host: str = "127.0.0.1",
    ) -> ControlPlaneSettings:
        env = os.environ if environ is None else environ
        enabled = str(env.get("PP_CONTROL_PLANE", "0")).strip() == "1"
        if not _is_loopback_host(bind_host):
            raise ValueError("control_bind_not_loopback")

        sqlite_path = Path(str(env.get("PLASTIC_DB_PATH") or "data/db/plastic_memory.db"))
        default_state_root = (
            sqlite_path.parent.parent if sqlite_path.parent.name == "db" else sqlite_path.parent
        )
        root = Path(str(env.get("PP_CONTROL_ROOT") or default_state_root / "control"))
        job_path = Path(
            str(env.get("PP_INFERENCE_GATEWAY_DB_PATH") or inference_jobs_path_for(sqlite_path))
        )
        lancedb_root = Path(
            str(
                env.get("PLASTIC_LANCEDB_GENERATION_ROOT")
                or env.get("PLASTIC_LANCEDB_PATH")
                or "data/lancedb"
            )
        )
        run_dir = Path(str(env.get("PP_MAINTENANCE_RUN_DIR") or "var/run"))
        status = ServerStatusSettings(
            sqlite_path=sqlite_path.expanduser(),
            inference_job_db_path=job_path.expanduser(),
            lancedb_root=lancedb_root.expanduser(),
            lancedb_live_root=(
                Path(str(env["PLASTIC_LANCEDB_LIVE_ROOT"])).expanduser()
                if str(env.get("PLASTIC_LANCEDB_LIVE_ROOT") or "").strip()
                else None
            ),
            maintenance_heartbeat_path=run_dir.expanduser() / "maintenance_daemon.heartbeat",
            maintenance_enabled=str(env.get("PP_MAINTENANCE_ENABLED", "0")) == "1",
            listener_ports=(
                ("mcp", 9020),
                ("inference_gateway", 9030),
                ("config_control", 9040),
            ),
        )
        authenticator = ControlPlaneAuthenticator.from_env(env) if enabled else None
        allowed_origins = _allowed_origins_from_env(env.get("PP_CONTROL_ALLOWED_ORIGINS"))
        return cls(
            enabled=enabled,
            root=root.expanduser(),
            authenticator=authenticator,
            status=status,
            bind_host=bind_host,
            allowed_origins=allowed_origins,
        )


class _SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def secured_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                # ASGI response headers are byte pairs; normalize without
                # assuming Starlette's internal representation.
                existing = {
                    name.lower()
                    if isinstance(name, bytes)
                    else str(name).casefold().encode("ascii")
                    for name, _value in headers
                }
                for name, value in _SECURITY_HEADERS.items():
                    encoded = name.lower().encode("ascii")
                    if encoded not in existing:
                        headers.append((encoded, value.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secured_send)


def create_control_plane_app(
    settings: ControlPlaneSettings,
    *,
    store_factory: Callable[[Path, Mapping[str, object]], ControlPlaneConfigStore] | None = None,
    status_collector: Callable[[ServerStatusSettings], dict[str, object]] | None = None,
) -> Any:
    """Create the standalone 9040 ASGI app.

    Configuration writes never share MCP or inference-gateway listeners.
    """

    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if not settings.enabled:
        return Starlette(routes=[])
    if settings.authenticator is None:
        raise ValueError("control_credentials_missing")
    authenticator = settings.authenticator
    factory = store_factory or (lambda root, env: ControlPlaneConfigStore(root, base_env=env))
    store = factory(settings.root, os.environ)
    collect_status = status_collector or collect_server_status

    def response(
        request: Request,
        payload: object,
        *,
        status_code: int = 200,
        etag: str = "",
    ) -> JSONResponse:
        headers = {}
        if etag:
            headers["ETag"] = etag
        request_id = request.headers.get("x-request-id", "")
        if _REVISION_RE.fullmatch(request_id):
            headers["X-Request-ID"] = request_id
        return JSONResponse(payload, status_code=status_code, headers=headers)

    async def home(request: Request) -> JSONResponse:
        _admit_loopback(request)
        return response(
            request,
            {
                "service": "plastic-promise-control-plane",
                "mode": "headless-api",
                "dashboard": "http://127.0.0.1:19020/dashboard",
            },
        )

    async def live(request: Request) -> JSONResponse:
        _admit_loopback(request)
        return response(
            request,
            {
                "status": "ok",
                "service": "plastic-promise-control-plane",
                "bind": "loopback",
            },
        )

    async def session(request: Request) -> JSONResponse:
        principal = _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        return response(request, {"actor": principal.actor, "role": principal.role})

    async def status(request: Request) -> JSONResponse:
        _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        payload = await asyncio.to_thread(collect_status, settings.status)
        safe = await asyncio.to_thread(store.safe_config)
        safe_payload = _public(safe)
        payload["control_config"] = {
            "active_revision_id": safe_payload.get("active_revision_id"),
            "etag": safe_payload.get("etag"),
            "desired_generation_id": safe_payload.get("desired_generation_id"),
            "desired_generation_manifest_sha256": safe_payload.get(
                "desired_generation_manifest_sha256"
            ),
        }
        return response(request, payload)

    async def safe_config(request: Request) -> JSONResponse:
        _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        result = await asyncio.to_thread(store.safe_config)
        payload = _public(result)
        return response(request, payload, etag=_etag(payload, result))

    async def revisions(request: Request) -> JSONResponse:
        _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        limit = _query_limit(request.query_params.get("limit"))
        rows = await asyncio.to_thread(store.list_revisions, limit)
        return response(request, {"revisions": [_public(row) for row in rows]})

    async def retarget_current_generation(request: Request) -> JSONResponse:
        principal = _principal(
            request,
            authenticator,
            role="operator",
            mutation=True,
            allowed_origins=settings.allowed_origins,
        )
        expected_etag = _if_match(request)
        key = _idempotency_key(request)
        payload = await _json_body(request)
        _require_fields(payload, frozenset({"generation_id", "manifest_sha256"}))
        result = await asyncio.to_thread(
            store.retarget_current_generation,
            payload.get("generation_id"),
            manifest_sha256=payload.get("manifest_sha256"),
            expected_etag=expected_etag,
            idempotency_key=key,
            actor=principal.actor,
            role=principal.role,
        )
        return response(request, result)

    async def revision(request: Request) -> JSONResponse:
        _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        revision_id = _revision_id(request.path_params.get("revision_id"))
        result = await asyncio.to_thread(store.get_revision, revision_id)
        return response(request, _public(result))

    async def audit(request: Request) -> JSONResponse:
        _principal(request, authenticator, allowed_origins=settings.allowed_origins)
        limit = _query_limit(request.query_params.get("limit"))
        rows = await asyncio.to_thread(store.audit, limit)
        return response(request, {"audit": [_public(row) for row in rows]})

    async def validate(request: Request) -> JSONResponse:
        principal = _principal(
            request,
            authenticator,
            role="operator",
            mutation=True,
            allowed_origins=settings.allowed_origins,
        )
        expected_etag = _if_match(request)
        payload = await _json_body(request)
        _require_fields(payload, frozenset({"config", "secret_ops"}))
        candidate, secret_ops = _candidate_parts(payload)
        _require_secret_role(principal, secret_ops)
        result = await asyncio.to_thread(
            store.validate,
            candidate,
            secret_ops,
            expected_etag=expected_etag,
        )
        return response(request, _public(result))

    async def stage(request: Request) -> JSONResponse:
        principal = _principal(
            request,
            authenticator,
            role="operator",
            mutation=True,
            allowed_origins=settings.allowed_origins,
        )
        expected_etag = _if_match(request)
        key = _idempotency_key(request)
        payload = await _json_body(request)
        _require_fields(payload, frozenset({"config", "secret_ops"}))
        candidate, secret_ops = _candidate_parts(payload)
        _require_secret_role(principal, secret_ops)
        result = await asyncio.to_thread(
            store.stage,
            candidate,
            secret_ops,
            expected_etag=expected_etag,
            idempotency_key=key,
            actor=principal.actor,
            role=principal.role,
        )
        return response(request, _public(result), status_code=201)

    async def activate(request: Request) -> JSONResponse:
        principal = _principal(
            request,
            authenticator,
            role="operator",
            mutation=True,
            allowed_origins=settings.allowed_origins,
        )
        revision_id = _revision_id(request.path_params.get("revision_id"))
        staged_revision = await asyncio.to_thread(store.get_revision, revision_id)
        if _contains_secret_changes(staged_revision):
            principal.require("secret-admin")
        expected_etag = _if_match(request)
        key = _idempotency_key(request)
        payload = await _json_body(request)
        _require_fields(payload, frozenset({"evidence"}))
        evidence = payload.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            raise ControlPlaneHTTPError(400, "control_evidence_invalid")
        result = await asyncio.to_thread(
            store.activate,
            revision_id,
            expected_etag=expected_etag,
            idempotency_key=key,
            actor=principal.actor,
            role=principal.role,
            evidence=evidence,
        )
        return response(request, _public(result))

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/health/live", live, methods=["GET"]),
        Route("/api/control/v1/session", session, methods=["GET"]),
        Route("/api/control/v1/status", status, methods=["GET"]),
        Route("/api/control/v1/config/safe", safe_config, methods=["GET"]),
        Route("/api/control/v1/config/revisions", revisions, methods=["GET"]),
        Route(
            "/api/control/v1/generation/retarget-current",
            retarget_current_generation,
            methods=["POST"],
        ),
        Route(
            "/api/control/v1/config/revisions/{revision_id}",
            revision,
            methods=["GET"],
        ),
        Route("/api/control/v1/audit", audit, methods=["GET"]),
        Route("/api/control/v1/config/validate", validate, methods=["POST"]),
        Route("/api/control/v1/config/stage", stage, methods=["POST"]),
        Route(
            "/api/control/v1/config/revisions/{revision_id}/activate",
            activate,
            methods=["POST"],
        ),
    ]

    async def exception_handler(request: Request, exc: Exception) -> JSONResponse:
        status_code, code = _error_details(exc)
        if status_code >= 500:
            logger.warning("control plane request failed: %s", type(exc).__name__)
        return response(
            request,
            {"error": {"code": code, "message": "Request could not be completed"}},
            status_code=status_code,
        )

    app = Starlette(
        routes=routes,
        # Starlette treats the bare ``Exception`` key as the 500 handler and
        # re-raises it in test transports. Register expected boundary errors
        # explicitly so callers receive stable JSON status responses.
        exception_handlers={
            ControlPlaneHTTPError: exception_handler,
            ControlPlaneAuthenticationError: exception_handler,
            ControlPlaneError: exception_handler,
            ValueError: exception_handler,
            TypeError: exception_handler,
            Exception: exception_handler,
        },
    )
    cors_app = CORSMiddleware(
        app,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    )
    return _SecurityHeadersMiddleware(cors_app)


def _principal(
    request: Any,
    authenticator: ControlPlaneAuthenticator,
    *,
    role: str = "viewer",
    mutation: bool = False,
    allowed_origins: tuple[str, ...] = (),
) -> ControlPlanePrincipal:
    _admit_loopback(
        request,
        mutation=mutation,
        allowed_origins=allowed_origins,
    )
    principal = authenticator.authenticate(request.headers.getlist("authorization"))
    principal.require(role)
    return principal


def _admit_loopback(
    request: Any,
    *,
    mutation: bool = False,
    allowed_origins: tuple[str, ...] = (),
) -> None:
    client = request.client.host if request.client is not None else ""
    if not _is_loopback_host(client):
        raise ControlPlaneHTTPError(403, "control_loopback_required")
    authority = request.headers.get("host", "")
    if not _is_loopback_authority(authority):
        raise ControlPlaneHTTPError(403, "control_host_required")
    forwarded = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
    if any(request.headers.getlist(name) for name in forwarded):
        raise ControlPlaneHTTPError(403, "control_forwarded_headers_forbidden")
    origin = request.headers.get("origin", "")
    allowed_cross_origin = False
    if origin and not _same_loopback_origin(origin, authority):
        try:
            normalized_origin = _normalize_loopback_origin(origin)
        except ValueError:
            raise ControlPlaneHTTPError(403, "control_origin_invalid") from None
        allowed_cross_origin = normalized_origin in allowed_origins
        if not allowed_cross_origin:
            raise ControlPlaneHTTPError(403, "control_origin_invalid")
    if not mutation:
        return
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"} and not allowed_cross_origin:
        raise ControlPlaneHTTPError(403, "control_cross_origin_forbidden")


def _same_loopback_origin(origin: str, authority: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    return _is_loopback_host(parsed.hostname) and parsed.netloc.casefold() == authority.casefold()


def _normalize_loopback_origin(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError:
        raise ValueError("control_allowed_origins_invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or not _is_loopback_host(parsed.hostname)
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("control_allowed_origins_invalid")
    host = str(parsed.hostname).casefold()
    if ":" in host:
        host = f"[{host}]"
    authority = host if parsed_port is None else f"{host}:{parsed_port}"
    return f"{parsed.scheme.casefold()}://{authority}"


def _allowed_origins_from_env(value: object) -> tuple[str, ...]:
    if value is None or not str(value).strip():
        return _DEFAULT_ALLOWED_ORIGINS
    raw_items = str(value).split(",")
    if any(not item.strip() for item in raw_items):
        raise ValueError("control_allowed_origins_invalid")
    origins = tuple(_normalize_loopback_origin(item) for item in raw_items)
    if len(set(origins)) != len(origins):
        raise ValueError("control_allowed_origins_invalid")
    return origins


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
    if not raw or any(character in raw for character in "\r\n\t/@,\\"):
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


async def _json_body(request: Any) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != "application/json":
        raise ControlPlaneHTTPError(415, "control_content_type_invalid")
    raw_length = request.headers.get("content-length", "")
    if raw_length:
        try:
            if int(raw_length) > _MAX_BODY_BYTES:
                raise ControlPlaneHTTPError(413, "control_request_too_large")
        except ValueError:
            raise ControlPlaneHTTPError(400, "control_content_length_invalid") from None

    async def read_body() -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                raise ControlPlaneHTTPError(413, "control_request_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        body = await asyncio.wait_for(read_body(), timeout=_BODY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise ControlPlaneHTTPError(408, "control_request_body_timeout") from None
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ControlPlaneHTTPError(400, "control_json_invalid") from None
    if not isinstance(payload, dict):
        raise ControlPlaneHTTPError(400, "control_json_object_required")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("control_duplicate_field")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise ValueError("control_json_nonfinite")


def _require_fields(payload: Mapping[str, object], allowed: frozenset[str]) -> None:
    if any(not isinstance(key, str) for key in payload) or set(payload) - allowed:
        raise ControlPlaneHTTPError(400, "control_field_not_allowed")


def _candidate_parts(payload: Mapping[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    candidate = payload.get("config", {})
    secret_ops = payload.get("secret_ops", {})
    if not isinstance(candidate, dict):
        raise ControlPlaneHTTPError(400, "control_config_invalid")
    if not isinstance(secret_ops, dict):
        raise ControlPlaneHTTPError(400, "control_secret_ops_invalid")
    return candidate, secret_ops


def _require_secret_role(
    principal: ControlPlanePrincipal,
    secret_ops: Mapping[str, object],
) -> None:
    if secret_ops:
        principal.require("secret-admin")


def _contains_secret_changes(revision: object) -> bool:
    if isinstance(revision, Mapping):
        return revision.get("contains_secret_changes", True) is not False
    return getattr(revision, "contains_secret_changes", True) is not False


def _if_match(request: Any) -> str:
    values = request.headers.getlist("if-match")
    if len(values) != 1 or not values[0].strip():
        raise ControlPlaneHTTPError(428, "control_if_match_required")
    return values[0].strip()


def _idempotency_key(request: Any) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or not _IDEMPOTENCY_RE.fullmatch(values[0]):
        raise ControlPlaneHTTPError(428, "control_idempotency_key_required")
    return values[0]


def _revision_id(value: object) -> str:
    revision_id = str(value or "")
    if not _REVISION_RE.fullmatch(revision_id):
        raise ControlPlaneHTTPError(400, "control_revision_id_invalid")
    return revision_id


def _query_limit(value: object) -> int:
    if value in {None, ""}:
        return 100
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ControlPlaneHTTPError(400, "control_limit_invalid") from None
    if isinstance(value, bool) or not 1 <= limit <= 500:
        raise ControlPlaneHTTPError(400, "control_limit_invalid")
    return limit


def _public(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        result = value.to_dict()
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise TypeError("control_response_invalid")
    if not isinstance(result, dict):
        raise TypeError("control_response_invalid")
    return result


def _etag(payload: Mapping[str, object], value: object) -> str:
    etag = str(getattr(value, "etag", "") or payload.get("etag") or "")
    if not etag:
        raise TypeError("control_etag_missing")
    return etag


def _error_details(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, (ControlPlaneHTTPError, ControlPlaneAuthenticationError)):
        return exc.status_code, exc.code
    if isinstance(exc, ControlPlaneError):
        code = str(getattr(exc, "code", "control_store_unavailable"))
        status = int(getattr(exc, "status_code", 400))
        return status, code if _ERROR_RE.fullmatch(code) else "control_store_unavailable"
    if isinstance(exc, FileNotFoundError):
        return 404, "control_revision_not_found"
    if isinstance(exc, (ValueError, TypeError)):
        code = str(exc) if len(exc.args) == 1 else ""
        if _ERROR_RE.fullmatch(code):
            return 400, code
        return 400, "control_request_invalid"
    return 503, "control_service_unavailable"
