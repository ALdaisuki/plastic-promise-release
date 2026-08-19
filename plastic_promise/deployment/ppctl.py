"""Fixed-operation host facade for Deployment Center reads.

The host adapter deliberately delegates only the two read-only Deployment
Center operations.  It contains no shell command, Docker/Compose, SSH,
service, tunnel, migration, persistence, or runtime activation mechanism.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .deployment_center import (
    DeploymentCenter,
    DeploymentCenterError,
    DeploymentInspection,
    DeploymentPreview,
    DeploymentPreviewRequest,
)
from .endpoint_contract import EndpointContractError, resolve_deployment_manifest_v2

PPCTL_SCHEMA_VERSION = "plastic-promise-ppctl/v1"
PPCTL_HTTP_SCHEMA_VERSION = "plastic-promise-ppctl-http/v1"

_POST_PATHS = frozenset({"/ppctl/v1/inspect", "/ppctl/v1/preview"})
_INSPECT_REQUEST_FIELDS = frozenset({"installation_ref"})
_PREVIEW_REQUEST_FIELDS = frozenset({"installation_ref", "candidate_manifest"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1"})
_ALLOWED_CORS_HEADERS = frozenset({"content-type"})
_MAX_REQUEST_BODY_BYTES = 128 * 1024
_BASE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}

if TYPE_CHECKING:
    from starlette.requests import Request


class Ppctl:
    """Expose a closed allowlist of inspect and preview host operations."""

    _ALLOWED_OPERATIONS = ("inspect", "preview")

    def __init__(self, deployment_center: DeploymentCenter) -> None:
        self._deployment_center = deployment_center

    @property
    def allowed_operations(self) -> tuple[str, str]:
        """Return the complete fixed operation set for a browser control surface."""

        return self._ALLOWED_OPERATIONS

    def inspect(self, installation_ref: str) -> DeploymentInspection:
        """Run the first allowlisted read-only operation."""

        return self._deployment_center.inspect(installation_ref)

    def preview(self, request: DeploymentPreviewRequest) -> DeploymentPreview:
        """Run the second allowlisted read-only operation."""

        return self._deployment_center.preview(request)

    def dispatch(
        self,
        operation: str,
        request: str | DeploymentPreviewRequest,
    ) -> DeploymentInspection | DeploymentPreview:
        """Dispatch only a fixed operation without dynamic lookup or command execution."""

        if not isinstance(operation, str):
            raise DeploymentCenterError("ppctl_operation_invalid")
        if operation == "inspect":
            if not isinstance(request, str):
                raise DeploymentCenterError("ppctl_inspection_reference_required")
            return self.inspect(request)
        if operation == "preview":
            if not isinstance(request, DeploymentPreviewRequest):
                raise DeploymentCenterError("ppctl_preview_request_required")
            return self.preview(request)
        raise DeploymentCenterError("ppctl_operation_not_allowed")


class PpctlHttpAdapter:
    """Build a host-only ASGI bridge with two fixed, read-only POST routes.

    The factory deliberately returns an ASGI application only.  Listener
    binding, process startup, transport security, and endpoint activation stay
    outside this module.  The host deployment adapter must bind it privately.
    """

    def __init__(self, ppctl: Ppctl, *, edge_origins: Iterable[str]) -> None:
        self._ppctl = ppctl
        self._edge_origins = _validated_loopback_origins(edge_origins)

    def create_app(self) -> Starlette:
        """Return the unstarted, fixed-route ASGI app for a host-local bridge."""

        app = Starlette(
            routes=(
                Route("/ppctl/v1/inspect", endpoint=self._inspect, methods=["POST"]),
                Route("/ppctl/v1/preview", endpoint=self._preview, methods=["POST"]),
            )
        )
        app.add_exception_handler(HTTPException, _http_exception_response)
        app.add_middleware(_LoopbackCorsMiddleware, edge_origins=self._edge_origins)

        return app

    async def _inspect(self, request: Request) -> JSONResponse:
        try:
            installation_ref = await _parse_inspect_request(request)
        except DeploymentCenterError as exc:
            return _error_response(400, exc.code)

        try:
            return _success_response(_safe_projection(self._ppctl.inspect(installation_ref)))
        except DeploymentCenterError as exc:
            return _error_response(400, exc.code)
        except Exception:
            return _error_response(503, "ppctl_read_unavailable")

    async def _preview(self, request: Request) -> JSONResponse:
        try:
            preview_request = await _parse_preview_request(request)
        except DeploymentCenterError as exc:
            return _error_response(400, exc.code)

        try:
            return _success_response(_safe_projection(self._ppctl.preview(preview_request)))
        except DeploymentCenterError as exc:
            return _error_response(400, exc.code)
        except Exception:
            return _error_response(503, "ppctl_read_unavailable")


def create_ppctl_app(ppctl: Ppctl, *, edge_origins: Iterable[str]) -> Starlette:
    """Create, but never start, the strict host-only ppctl ASGI bridge."""

    return PpctlHttpAdapter(ppctl, edge_origins=edge_origins).create_app()


class _LoopbackCorsMiddleware(BaseHTTPMiddleware):
    """Enforce explicit 127.0.0.1 origins without wildcard CORS behaviour."""

    def __init__(self, app, *, edge_origins: frozenset[str]) -> None:
        super().__init__(app)
        self._edge_origins = edge_origins

    async def dispatch(self, request: Request, call_next) -> Response:
        origin = request.headers.get("origin")
        normalized_origin: str | None = None
        if origin is not None:
            try:
                normalized_origin = _normalize_loopback_origin(origin)
            except DeploymentCenterError:
                return _error_response(403, "ppctl_origin_forbidden")
            if normalized_origin not in self._edge_origins:
                return _error_response(403, "ppctl_origin_forbidden")

        if request.method == "OPTIONS":
            return _preflight_response(request, normalized_origin)

        response = await call_next(request)
        if normalized_origin is not None:
            _apply_cors_headers(response, normalized_origin)
        return response


async def _parse_inspect_request(request: Request) -> str:
    payload = await _parse_json_body(request)
    if set(payload) != _INSPECT_REQUEST_FIELDS:
        raise DeploymentCenterError("ppctl_request_fields_invalid")
    installation_ref = payload["installation_ref"]
    if not isinstance(installation_ref, str):
        raise DeploymentCenterError("ppctl_request_invalid") from None
    return installation_ref


async def _parse_preview_request(request: Request) -> DeploymentPreviewRequest:
    payload = await _parse_json_body(request)
    if set(payload) != _PREVIEW_REQUEST_FIELDS:
        raise DeploymentCenterError("ppctl_request_fields_invalid")
    candidate_manifest = payload["candidate_manifest"]
    if not isinstance(candidate_manifest, Mapping):
        raise DeploymentCenterError("ppctl_candidate_manifest_required")
    try:
        # Validate exactly the strict V2 public contract before Ppctl sees it.
        # No legacy manifest is translated at this HTTP seam.
        resolve_deployment_manifest_v2(candidate_manifest)
    except EndpointContractError:
        raise DeploymentCenterError("ppctl_candidate_manifest_invalid") from None
    try:
        return DeploymentPreviewRequest(
            candidate_manifest=candidate_manifest,
            installation_ref=payload["installation_ref"],
        )
    except DeploymentCenterError:
        raise DeploymentCenterError("ppctl_request_invalid") from None


async def _parse_json_body(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise DeploymentCenterError("ppctl_content_type_required")
    return _strict_json_object(await _read_bounded_body(request))


async def _read_bounded_body(request: Request) -> bytes:
    """Read a request body without allowing Starlette to buffer it unbounded.

    ``Request.body()`` joins every ASGI chunk before this adapter can apply its
    size policy.  Inspect and preview have deliberately small JSON envelopes,
    so reject an oversized declaration before reading and stop consuming a
    chunked request as soon as it crosses the fixed cap.
    """

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_bytes = int(declared_length)
        except ValueError:
            raise DeploymentCenterError("ppctl_content_length_invalid") from None
        if declared_bytes < 0:
            raise DeploymentCenterError("ppctl_content_length_invalid")
        if declared_bytes > _MAX_REQUEST_BODY_BYTES:
            raise DeploymentCenterError("ppctl_request_too_large")

    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > _MAX_REQUEST_BODY_BYTES:
            raise DeploymentCenterError("ppctl_request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _strict_json_object(body: bytes) -> dict[str, object]:
    try:
        decoded = body.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        raise DeploymentCenterError("ppctl_json_invalid") from None
    if not isinstance(value, dict):
        raise DeploymentCenterError("ppctl_json_object_required")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate_json_key")
        payload[key] = value
    return payload


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"nonfinite_json:{value}")


def _safe_projection(result: object) -> dict[str, object]:
    serializer = getattr(result, "to_dict", None)
    if not callable(serializer):
        raise DeploymentCenterError("ppctl_result_projection_invalid")
    try:
        payload = serializer()
    except Exception:
        raise DeploymentCenterError("ppctl_result_projection_invalid") from None
    if not isinstance(payload, dict):
        raise DeploymentCenterError("ppctl_result_projection_invalid")
    return payload


def _validated_loopback_origins(edge_origins: Iterable[str]) -> frozenset[str]:
    if isinstance(edge_origins, str):
        raise DeploymentCenterError("ppctl_edge_origins_invalid")
    try:
        origins = tuple(_normalize_loopback_origin(value) for value in edge_origins)
    except TypeError:
        raise DeploymentCenterError("ppctl_edge_origins_invalid") from None
    if not origins or len(origins) != len(set(origins)):
        raise DeploymentCenterError("ppctl_edge_origins_invalid")
    return frozenset(origins)


def _normalize_loopback_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value == "*":
        raise DeploymentCenterError("ppctl_edge_origin_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise DeploymentCenterError("ppctl_edge_origin_invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.hostname.casefold() not in _LOOPBACK_HOSTS
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentCenterError("ppctl_edge_origin_invalid")
    host = parsed.hostname.casefold()
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme.casefold()}://{host}{rendered_port}"


def _preflight_response(request: Request, origin: str | None) -> Response:
    if request.url.path not in _POST_PATHS:
        return _error_response(404, "ppctl_route_not_found")
    if origin is None:
        return _error_response(403, "ppctl_origin_forbidden")
    if request.headers.get("access-control-request-method", "").upper() != "POST":
        return _error_response(405, "ppctl_method_not_allowed")
    requested_headers = {
        item.strip().casefold()
        for item in request.headers.get("access-control-request-headers", "").split(",")
        if item.strip()
    }
    if not requested_headers.issubset(_ALLOWED_CORS_HEADERS):
        return _error_response(400, "ppctl_cors_headers_forbidden")
    return Response(status_code=204, headers=_cors_headers(origin))


def _http_exception_response(_: Request, exc: HTTPException) -> JSONResponse:
    code = "ppctl_route_not_found" if exc.status_code == 404 else "ppctl_method_not_allowed"
    return _error_response(exc.status_code, code)


def _success_response(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(payload, status_code=200, headers=dict(_BASE_HEADERS))


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code}},
        status_code=status_code,
        headers=dict(_BASE_HEADERS),
    )


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        **_BASE_HEADERS,
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST",
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Max-Age": "0",
        "Vary": "Origin",
    }


def _apply_cors_headers(response: Response, origin: str) -> None:
    for header, value in _cors_headers(origin).items():
        response.headers[header] = value


__all__ = [
    "PPCTL_HTTP_SCHEMA_VERSION",
    "PPCTL_SCHEMA_VERSION",
    "Ppctl",
    "PpctlHttpAdapter",
    "create_ppctl_app",
]
