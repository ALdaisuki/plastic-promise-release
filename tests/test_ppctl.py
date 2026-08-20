from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

from plastic_promise.deployment.deployment_center import (
    DeploymentCenterError,
    DeploymentPreviewRequest,
)
from plastic_promise.deployment.endpoint_contract import DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION
from plastic_promise.deployment.ppctl import Ppctl, _read_bounded_body, create_ppctl_app


class _Center:
    def inspect(self, installation_ref: str) -> str:
        return "inspection"

    def preview(self, request: DeploymentPreviewRequest) -> str:
        return "preview"


class _Projection:
    def __init__(self, operation: str) -> None:
        self.operation = operation

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": "test-ppctl-projection/v1", "operation": self.operation}


class _HttpCenter:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def inspect(self, installation_ref: str) -> _Projection:
        self.operations.append("inspect")
        assert installation_ref == "local-installation"
        return _Projection("inspect")

    def preview(self, request: DeploymentPreviewRequest) -> _Projection:
        self.operations.append("preview")
        assert request.installation_ref == "local-installation"
        return _Projection("preview")


class _OversizedStream(httpx.AsyncByteStream):
    """Send a body without a declared length to exercise chunked input handling."""

    async def __aiter__(self):
        yield b'{"installation_ref":"'
        yield b"x" * (128 * 1024)
        yield b'"}'

    async def aclose(self) -> None:
        return None


def _manifest() -> dict[str, object]:
    return {
        "schema_version": DEPLOYMENT_MANIFEST_V2_SCHEMA_VERSION,
        "deployment_id": "developer-laptop",
        "profile": "local-all-in-one",
        "modules": {},
        "endpoints": [
            {
                "id": "local-edge",
                "role": "pp-local-edge",
                "protocol": {"family": "edge", "major": 1, "minor": 0},
                "capabilities": [],
                "transport_ref": "loopback",
                "resource_policy_ref": "edge-default",
            },
            {
                "id": "server-backend",
                "role": "pp-server-backend",
                "protocol": {"family": "backend", "major": 1, "minor": 0},
                "capabilities": [],
                "transport_ref": "backend-private",
                "resource_policy_ref": "backend-default",
            },
        ],
    }


def _http_payload() -> dict[str, object]:
    return {
        "installation_ref": "local-installation",
        "candidate_manifest": _manifest(),
    }


def _http_app() -> tuple[object, _HttpCenter]:
    center = _HttpCenter()
    app = create_ppctl_app(
        Ppctl(center),  # type: ignore[arg-type]
        edge_origins=("http://127.0.0.1:19021",),
    )
    return app, center


def test_ppctl_exposes_only_fixed_read_operations_and_rejects_mutations():
    ppctl = Ppctl(_Center())  # type: ignore[arg-type]
    request = DeploymentPreviewRequest(candidate_manifest={}, installation_ref="local-installation")

    assert ppctl.allowed_operations == ("inspect", "preview")
    assert ppctl.dispatch("inspect", "local-installation") == "inspection"
    assert ppctl.dispatch("preview", request) == "preview"
    with pytest.raises(DeploymentCenterError, match="ppctl_operation_not_allowed"):
        ppctl.dispatch("apply", request)


@pytest.mark.asyncio
async def test_ppctl_http_adapter_only_allows_fixed_post_routes_and_validated_loopback_cors():
    app, center = _http_app()
    origin = "http://127.0.0.1:19021"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:19021") as client:
        inspect = await client.post(
            "/ppctl/v1/inspect",
            json={"installation_ref": "local-installation"},
            headers={"Origin": origin},
        )
        preview = await client.post(
            "/ppctl/v1/preview", json=_http_payload(), headers={"Origin": origin}
        )
        preflight = await client.options(
            "/ppctl/v1/inspect",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert inspect.status_code == preview.status_code == 200
    assert inspect.json() == {"schema_version": "test-ppctl-projection/v1", "operation": "inspect"}
    assert preview.json() == {"schema_version": "test-ppctl-projection/v1", "operation": "preview"}
    assert center.operations == ["inspect", "preview"]
    assert inspect.headers["cache-control"] == "no-store"
    assert inspect.headers["access-control-allow-origin"] == origin
    assert inspect.headers["access-control-allow-origin"] != "*"
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-methods"] == "POST"
    assert preflight.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_ppctl_http_adapter_rejects_other_routes_methods_origins_and_unsafe_json_without_leaks():
    app, center = _http_app()
    origin = "http://127.0.0.1:19021"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:19021") as client:
        wrong_method = await client.get("/ppctl/v1/inspect", headers={"Origin": origin})
        wrong_route = await client.post(
            "/ppctl/v1/apply", json=_http_payload(), headers={"Origin": origin}
        )
        wrong_origin = await client.post(
            "/ppctl/v1/inspect",
            json={"installation_ref": "local-installation"},
            headers={"Origin": "https://example.invalid"},
        )
        unknown_fields = await client.post(
            "/ppctl/v1/inspect",
            json={"installation_ref": "local-installation", "candidate_manifest": _manifest()},
            headers={"Origin": origin},
        )
        unsafe = await client.post(
            "/ppctl/v1/preview",
            json={
                "installation_ref": "/Users/unsafe",
                "candidate_manifest": {
                    "schema_version": "plastic-promise-deployment/v1",
                    "api_key": "sk-should-never-echo",
                    "state_root": "/Users/unsafe",
                },
            },
            headers={"Origin": origin},
        )

    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "ppctl_method_not_allowed"
    assert wrong_route.status_code == 404
    assert wrong_route.json()["error"]["code"] == "ppctl_route_not_found"
    assert wrong_origin.status_code == 403
    assert "access-control-allow-origin" not in wrong_origin.headers
    assert unknown_fields.status_code == 400
    assert unknown_fields.json()["error"]["code"] == "ppctl_request_fields_invalid"
    assert unsafe.status_code == 400
    assert unsafe.json()["error"]["code"] == "ppctl_candidate_manifest_invalid"
    assert "/Users/unsafe" not in unsafe.text
    assert "sk-should-never-echo" not in unsafe.text
    assert center.operations == []


@pytest.mark.asyncio
async def test_ppctl_http_adapter_caps_chunked_json_before_dispatch():
    app, center = _http_app()
    origin = "http://127.0.0.1:19021"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:19021") as client:
        response = await client.post(
            "/ppctl/v1/inspect",
            content=_OversizedStream(),
            headers={"Content-Type": "application/json", "Origin": origin},
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "ppctl_request_too_large"}}
    assert center.operations == []


@pytest.mark.asyncio
async def test_ppctl_reader_rejects_declared_oversized_body_before_reading():
    async def unexpected_receive() -> dict[str, object]:
        raise AssertionError("the bounded reader must reject before reading")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/ppctl/v1/inspect",
            "query_string": b"",
            "headers": [(b"content-length", str(128 * 1024 + 1).encode("ascii"))],
        },
        receive=unexpected_receive,
    )

    with pytest.raises(DeploymentCenterError, match="ppctl_request_too_large"):
        await _read_bounded_body(request)


@pytest.mark.asyncio
async def test_ppctl_http_adapter_rejects_malformed_declared_content_length_before_dispatch():
    app, center = _http_app()
    origin = "http://127.0.0.1:19021"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:19021") as client:
        response = await client.post(
            "/ppctl/v1/inspect",
            content=b'{"installation_ref":"local-installation"}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "not-a-length",
                "Origin": origin,
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "ppctl_content_length_invalid"}}
    assert center.operations == []


def test_ppctl_http_adapter_rejects_non_loopback_or_wildcard_origin_configuration():
    ppctl = Ppctl(_Center())  # type: ignore[arg-type]

    with pytest.raises(DeploymentCenterError, match="ppctl_edge_origin_invalid"):
        create_ppctl_app(ppctl, edge_origins=("https://example.invalid",))
    with pytest.raises(DeploymentCenterError, match="ppctl_edge_origin_invalid"):
        create_ppctl_app(ppctl, edge_origins=("*",))
    with pytest.raises(DeploymentCenterError, match="ppctl_edge_origin_invalid"):
        create_ppctl_app(ppctl, edge_origins=("http://localhost:19020",))
