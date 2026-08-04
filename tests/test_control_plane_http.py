from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from plastic_promise.control_plane.auth import (
    ControlPlaneAuthenticator,
    ControlPlaneCredential,
)
from plastic_promise.control_plane.store import ControlPlaneConfigStore
from plastic_promise.core.server_status import ServerStatusSettings
from plastic_promise.mcp.control_plane import (
    ControlPlaneSettings,
    create_control_plane_app,
)

if TYPE_CHECKING:
    from pathlib import Path


def _token(character: str) -> str:
    return character * 48


class FakeStore:
    def __init__(self) -> None:
        self.validations = []
        self.stages = []
        self.activations = []
        self.generation_retargets = []
        self.revision_contains_secret_changes = False

    def safe_config(self):
        return {
            "contract": "control-plane-config/v1",
            "etag": '"sha256:active"',
            "active_revision_id": "rev-active",
            "desired_generation_id": "generation-desired",
            "desired_generation_manifest_sha256": "a" * 64,
            "config": {"embedding": {"enabled": False}},
            "secrets": {"embedding_api_key": True},
        }

    def validate(self, candidate, secret_ops, *, expected_etag=None):
        self.validations.append((candidate, secret_ops, expected_etag))
        return {"valid": True, "embedding_identity_changed": False}

    def stage(
        self,
        candidate,
        secret_ops,
        *,
        expected_etag,
        idempotency_key,
        actor,
        role,
    ):
        self.stages.append((candidate, secret_ops, expected_etag, idempotency_key, actor, role))
        return {
            "revision_id": "rev-staged",
            "status": "staged",
            "changed_fields": ["rerank.enabled"],
        }

    def activate(
        self,
        revision_id,
        *,
        expected_etag,
        idempotency_key,
        actor,
        role,
        evidence,
    ):
        self.activations.append(
            (revision_id, expected_etag, idempotency_key, actor, role, evidence)
        )
        return {
            "revision_id": revision_id,
            "status": "active",
            "restart_required": True,
        }

    def retarget_current_generation(
        self,
        generation_id,
        *,
        manifest_sha256,
        expected_etag,
        idempotency_key,
        actor,
        role,
    ):
        self.generation_retargets.append(
            (
                generation_id,
                manifest_sha256,
                expected_etag,
                idempotency_key,
                actor,
                role,
            )
        )
        return {
            "desired_generation_id": generation_id,
            "desired_generation_manifest_sha256": manifest_sha256,
            "etag": '"sha256:retargeted"',
            "restart_required": False,
        }

    def list_revisions(self, limit):
        assert limit <= 500
        return [
            {
                "revision_id": "rev-staged",
                "status": "staged",
                "changed_fields": ["rerank.enabled"],
            }
        ]

    def get_revision(self, revision_id):
        return {
            "revision_id": revision_id,
            "status": "staged",
            "contains_secret_changes": self.revision_contains_secret_changes,
        }

    def audit(self, limit):
        assert limit <= 500
        return [{"actor": "operator", "action": "stage", "result": "ok"}]


def _settings(tmp_path: Path) -> ControlPlaneSettings:
    authenticator = ControlPlaneAuthenticator(
        [
            ControlPlaneCredential.from_token("viewer", "viewer", _token("v")),
            ControlPlaneCredential.from_token("operator", "operator", _token("o")),
            ControlPlaneCredential.from_token("secret", "secret-admin", _token("s")),
        ]
    )
    status = ServerStatusSettings(
        sqlite_path=tmp_path / "memory.db",
        inference_job_db_path=tmp_path / "jobs.db",
        lancedb_root=tmp_path / "lancedb",
        maintenance_heartbeat_path=tmp_path / "maintenance.heartbeat",
        listener_ports=(),
    )
    return ControlPlaneSettings(
        enabled=True,
        root=tmp_path / "control",
        authenticator=authenticator,
        status=status,
    )


def _app(tmp_path: Path, store: FakeStore | None = None):
    fake_store = store or FakeStore()
    app = create_control_plane_app(
        _settings(tmp_path),
        store_factory=lambda _root, _env: fake_store,
        status_collector=lambda _settings: {
            "schema": "plastic-promise/server-status/v1",
            "listeners": {},
            "sqlite": {"state": "missing"},
            "inference_jobs": {"state": "missing"},
            "lancedb": {"state": "missing"},
            "maintenance": {"state": "disabled"},
        },
    )
    return app, fake_store


def test_settings_normalize_only_explicit_loopback_dashboard_origins():
    settings = ControlPlaneSettings.from_env(
        {
            "PP_CONTROL_PLANE": "0",
            "PP_CONTROL_ALLOWED_ORIGINS": ("http://127.0.0.1:19020/,http://127.0.0.1:9020"),
        }
    )
    assert settings.allowed_origins == (
        "http://127.0.0.1:19020",
        "http://127.0.0.1:9020",
    )

    with pytest.raises(ValueError, match="control_allowed_origins_invalid"):
        ControlPlaneSettings.from_env(
            {
                "PP_CONTROL_PLANE": "0",
                "PP_CONTROL_ALLOWED_ORIGINS": "https://attacker.example",
            }
        )
    with pytest.raises(ValueError, match="control_allowed_origins_invalid"):
        ControlPlaneSettings.from_env(
            {
                "PP_CONTROL_PLANE": "0",
                "PP_CONTROL_ALLOWED_ORIGINS": ("http://127.0.0.1:19020,http://127.0.0.1:19020"),
            }
        )


def test_status_settings_default_to_isolated_inference_job_database(tmp_path):
    canonical = tmp_path / "state" / "db" / "plastic_memory.db"

    settings = ControlPlaneSettings.from_env(
        {
            "PP_CONTROL_PLANE": "0",
            "PLASTIC_DB_PATH": str(canonical),
        }
    )

    assert settings.status.inference_job_db_path == (
        tmp_path / "state" / "inference" / "inference_jobs.db"
    )


async def _request(
    app,
    method,
    path,
    *,
    token: str | None = None,
    headers=None,
    json=None,
    client_host="127.0.0.1",
    host="127.0.0.1:9040",
):
    transport = httpx.ASGITransport(app=app, client=(client_host, 43123))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:9040",
    ) as client:
        request_headers = [("host", host)]
        if token:
            request_headers.append(("authorization", f"Bearer {token}"))
        if headers:
            request_headers.extend(headers.items() if isinstance(headers, dict) else headers)
        return await client.request(method, path, headers=request_headers, json=json)


@pytest.mark.asyncio
async def test_live_and_headless_root_are_loopback_only(tmp_path):
    app, _store = _app(tmp_path)

    live = await _request(app, "GET", "/health/live")
    external = await _request(app, "GET", "/health/live", client_host="203.0.113.7")
    bad_host = await _request(app, "GET", "/", host="attacker.example")
    script = await _request(app, "GET", "/assets/control.js")
    stylesheet = await _request(app, "GET", "/assets/control.css")
    page = await _request(app, "GET", "/")

    assert live.status_code == 200
    assert live.json()["bind"] == "loopback"
    assert external.status_code == 403
    assert bad_host.status_code == 403
    assert script.status_code == 404
    assert stylesheet.status_code == 404
    assert page.status_code == 200
    assert page.json() == {
        "service": "plastic-promise-control-plane",
        "mode": "headless-api",
        "dashboard": "http://127.0.0.1:19020/dashboard",
    }
    assert page.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_dashboard_origin_gets_strict_cors_and_can_mutate_with_bearer(tmp_path):
    app, store = _app(tmp_path)
    origin = "http://127.0.0.1:19020"
    preflight = await _request(
        app,
        "OPTIONS",
        "/api/control/v1/config/validate",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,if-match",
        },
    )
    rejected = await _request(
        app,
        "OPTIONS",
        "/api/control/v1/config/validate",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,if-match",
        },
    )
    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={
            "Origin": origin,
            "Sec-Fetch-Site": "same-site",
            "If-Match": '"sha256:active"',
        },
        json={"config": {}, "secret_ops": {}},
    )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert "authorization" in preflight.headers["access-control-allow-headers"].casefold()
    assert preflight.headers["cross-origin-resource-policy"] == "same-site"
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["cross-origin-resource-policy"] == "same-site"
    assert store.validations == [({}, {}, '"sha256:active"')]


@pytest.mark.asyncio
async def test_session_requires_one_valid_bearer_and_returns_role(tmp_path):
    app, _store = _app(tmp_path)

    missing = await _request(app, "GET", "/api/control/v1/session")
    duplicate = await _request(
        app,
        "GET",
        "/api/control/v1/session",
        headers=[
            ("authorization", f"Bearer {_token('v')}"),
            ("authorization", f"Bearer {_token('v')}"),
        ],
    )
    valid = await _request(
        app,
        "GET",
        "/api/control/v1/session",
        token=_token("o"),
    )

    assert missing.status_code == 401
    assert duplicate.status_code == 401
    assert valid.status_code == 200
    assert valid.json() == {"actor": "operator", "role": "operator"}
    assert valid.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_safe_config_and_status_are_authenticated_read_only_views(tmp_path):
    app, _store = _app(tmp_path)

    safe = await _request(
        app,
        "GET",
        "/api/control/v1/config/safe",
        token=_token("v"),
    )
    status = await _request(
        app,
        "GET",
        "/api/control/v1/status",
        token=_token("v"),
    )

    assert safe.status_code == 200
    assert safe.headers["etag"] == '"sha256:active"'
    assert safe.json()["secrets"]["embedding_api_key"] is True
    assert status.status_code == 200
    assert status.json()["control_config"] == {
        "active_revision_id": "rev-active",
        "etag": '"sha256:active"',
        "desired_generation_id": "generation-desired",
        "desired_generation_manifest_sha256": "a" * 64,
    }


@pytest.mark.asyncio
async def test_validate_requires_json_and_if_match_but_not_idempotency_key(tmp_path):
    app, store = _app(tmp_path)
    body = {"config": {"rerank": {"enabled": True}}, "secret_ops": {}}

    missing_etag = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        json=body,
    )
    wrong_content_type = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={
            "If-Match": '"sha256:active"',
            "Content-Type": "text/plain",
        },
        json=body,
    )
    valid = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={"If-Match": '"sha256:active"'},
        json=body,
    )

    assert missing_etag.status_code == 428
    assert missing_etag.json()["error"]["code"] == "control_if_match_required"
    assert wrong_content_type.status_code == 415
    assert wrong_content_type.json()["error"]["code"] == "control_content_type_invalid"
    assert valid.status_code == 200
    assert valid.json() == {"valid": True, "embedding_identity_changed": False}
    assert store.validations == [(body["config"], body["secret_ops"], '"sha256:active"')]


@pytest.mark.asyncio
async def test_stage_requires_if_match_and_idempotency_key(tmp_path):
    app, _store = _app(tmp_path)
    body = {"config": {"rerank": {"enabled": True}}, "secret_ops": {}}

    missing = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("o"),
        json=body,
    )
    only_etag = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("o"),
        headers={"If-Match": '"sha256:active"'},
        json=body,
    )

    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "control_if_match_required"
    assert only_etag.status_code == 428
    assert only_etag.json()["error"]["code"] == "control_idempotency_key_required"


@pytest.mark.asyncio
async def test_activate_requires_idempotency_key(tmp_path):
    app, store = _app(tmp_path)

    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/revisions/rev-staged/activate",
        token=_token("o"),
        headers={"If-Match": '"sha256:active"'},
        json={},
    )

    assert response.status_code == 428
    assert response.json()["error"]["code"] == "control_idempotency_key_required"
    assert store.activations == []


@pytest.mark.asyncio
async def test_generation_retarget_requires_operator_and_forwards_cas_inputs(tmp_path):
    app, store = _app(tmp_path)
    body = {
        "generation_id": "generation-current",
        "manifest_sha256": "b" * 64,
    }
    headers = {
        "If-Match": '"sha256:active"',
        "Idempotency-Key": "retarget-current-001",
    }

    forbidden = await _request(
        app,
        "POST",
        "/api/control/v1/generation/retarget-current",
        token=_token("v"),
        headers=headers,
        json=body,
    )
    accepted = await _request(
        app,
        "POST",
        "/api/control/v1/generation/retarget-current",
        token=_token("o"),
        headers=headers,
        json=body,
    )

    assert forbidden.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["etag"] == '"sha256:retargeted"'
    assert store.generation_retargets == [
        (
            "generation-current",
            "b" * 64,
            '"sha256:active"',
            "retarget-current-001",
            "operator",
            "operator",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("config", []), ("config", False), ("secret_ops", 0)])
async def test_validate_rejects_falsey_non_object_channels(tmp_path, field, value):
    app, store = _app(tmp_path)
    body = {"config": {}, "secret_ops": {}}
    body[field] = value

    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={"If-Match": '"sha256:active"'},
        json=body,
    )

    expected_code = "control_config_invalid" if field == "config" else "control_secret_ops_invalid"
    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code
    assert store.validations == []


@pytest.mark.asyncio
async def test_operator_can_stage_safe_config_but_not_secrets(tmp_path):
    app, store = _app(tmp_path)
    headers = {
        "If-Match": '"sha256:active"',
        "Idempotency-Key": "stage-safe-0001",
    }
    safe_body = {"config": {"rerank": {"enabled": True}}, "secret_ops": {}}
    secret_value = "synthetic-secret-value"
    secret_body = {
        "config": {},
        "secret_ops": {"rerank_api_key": {"op": "set", "value": secret_value}},
    }

    staged = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("o"),
        headers=headers,
        json=safe_body,
    )
    forbidden = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("o"),
        headers={**headers, "Idempotency-Key": "stage-secret-0001"},
        json=secret_body,
    )

    assert staged.status_code == 201
    assert store.stages[0][0] == safe_body["config"]
    assert forbidden.status_code == 403
    assert secret_value not in forbidden.text


@pytest.mark.asyncio
async def test_secret_admin_write_response_never_echoes_secret(tmp_path):
    app, store = _app(tmp_path)
    secret_value = "synthetic-secret-value"
    body = {
        "config": {},
        "secret_ops": {"rerank_api_key": {"op": "set", "value": secret_value}},
    }
    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("s"),
        headers={
            "If-Match": '"sha256:active"',
            "Idempotency-Key": "stage-secret-0002",
        },
        json=body,
    )

    assert response.status_code == 201
    assert store.stages[0][1] == body["secret_ops"]
    assert secret_value not in response.text


@pytest.mark.asyncio
async def test_mutations_reject_cross_origin_and_forwarded_headers(tmp_path):
    app, _store = _app(tmp_path)
    common = {"If-Match": '"sha256:active"'}
    body = {"config": {}, "secret_ops": {}}

    cross_origin = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={**common, "Origin": "https://attacker.example"},
        json=body,
    )
    forwarded = await _request(
        app,
        "POST",
        "/api/control/v1/config/validate",
        token=_token("o"),
        headers={**common, "X-Forwarded-For": "127.0.0.1"},
        json=body,
    )

    assert cross_origin.status_code == 403
    assert forwarded.status_code == 403


@pytest.mark.asyncio
async def test_activate_is_cas_scoped_and_returns_restart_required(tmp_path):
    app, store = _app(tmp_path)

    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/revisions/rev-staged/activate",
        token=_token("o"),
        headers={
            "If-Match": '"sha256:active"',
            "Idempotency-Key": "activate-0001",
        },
        json={"evidence": {}},
    )

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert store.activations[0][0] == "rev-staged"
    assert store.activations[0][-1] == {}


@pytest.mark.asyncio
async def test_activate_accepts_absent_evidence_for_non_embedding_revision(tmp_path):
    app, store = _app(tmp_path)

    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/revisions/rev-staged/activate",
        token=_token("o"),
        headers={
            "If-Match": '"sha256:active"',
            "Idempotency-Key": "activate-no-evidence-0001",
        },
        json={},
    )

    assert response.status_code == 200
    assert store.activations[0][-1] is None


@pytest.mark.asyncio
async def test_operator_cannot_activate_secret_revision(tmp_path):
    app, store = _app(tmp_path)
    store.revision_contains_secret_changes = True

    response = await _request(
        app,
        "POST",
        "/api/control/v1/config/revisions/rev-staged/activate",
        token=_token("o"),
        headers={
            "If-Match": '"sha256:active"',
            "Idempotency-Key": "activate-secret-0001",
        },
        json={"evidence": {}},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "control_role_insufficient"
    assert store.activations == []


@pytest.mark.asyncio
async def test_real_store_stages_and_activates_non_embedding_revision_over_http(tmp_path):
    base_env = {
        "PP_INFERENCE_GATEWAY_PROJECT_ID": "project:http-test",
        "PP_INFERENCE_PROVIDER_HOST_ALLOWLIST": "api.example.test",
        "PP_INFERENCE_GATEWAY_TOKEN": "g" * 48,
    }
    real_store = ControlPlaneConfigStore(tmp_path / "control", base_env=base_env)
    app = create_control_plane_app(
        _settings(tmp_path),
        store_factory=lambda _root, _env: real_store,
        status_collector=lambda _settings: {
            "schema": "plastic-promise/server-status/v1",
            "listeners": {},
            "sqlite": {"state": "missing"},
            "inference_jobs": {"state": "missing"},
            "lancedb": {"state": "missing"},
            "maintenance": {"state": "disabled"},
        },
    )
    initial = await _request(
        app,
        "GET",
        "/api/control/v1/config/safe",
        token=_token("o"),
    )
    etag = initial.headers["etag"]
    staged = await _request(
        app,
        "POST",
        "/api/control/v1/config/stage",
        token=_token("o"),
        headers={
            "If-Match": etag,
            "Idempotency-Key": "real-stage-rerank-0001",
        },
        json={"config": {"rerank": {"max_candidates": 40}}, "secret_ops": {}},
    )

    assert staged.status_code == 201
    revision_id = staged.json()["revision_id"]
    assert staged.json()["requires_embedding_evidence"] is False
    assert staged.json()["runtime_embedding_index_identity"] == "fallback-zero"
    activated = await _request(
        app,
        "POST",
        f"/api/control/v1/config/revisions/{revision_id}/activate",
        token=_token("o"),
        headers={
            "If-Match": etag,
            "Idempotency-Key": "real-activate-rerank-0001",
        },
        json={},
    )

    assert activated.status_code == 200
    assert activated.json()["restart_required"] is True
    assert real_store.safe_config().revision_id == revision_id
    managed_environment = real_store.managed_env_path.read_text()
    assert "PP_RERANK_MAX_CANDIDATES=40" in managed_environment
    assert "PP_INFERENCE_TIMEOUT_SEC=45" in managed_environment
    assert "PP_EMBEDDING_IDENTITY=" not in managed_environment
    assert "PP_INFERENCE_CLIENT_VECTOR_IDENTITY=" not in managed_environment
    assert "PP_INFERENCE_CLIENT_VECTOR_DIMENSION=" not in managed_environment
