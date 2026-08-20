"""Knowledge ingestion HTTP service tests (Markdown-first slice)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from plastic_promise.knowledge.server import (
    KnowledgeIngestSettings,
    create_knowledge_ingest_app,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette


def _settings(
    tmp_path, *, enabled=True, token="test-token-0123456789abcdef"
) -> KnowledgeIngestSettings:
    return KnowledgeIngestSettings(
        enabled=enabled,
        token=token,
        max_upload_bytes=4096,
        max_body_bytes=5120,
        bind_host="127.0.0.1",
    )


def _app(settings: KnowledgeIngestSettings) -> Starlette:
    return create_knowledge_ingest_app(settings)


async def _request(app: Starlette, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9050") as client:
        return await client.request(method, path, **kwargs)


def _auth_headers(extra: dict | None = None) -> dict:
    headers = {"Authorization": "Bearer test-token-0123456789abcdef"}
    if extra:
        headers.update(extra)
    return headers


@pytest.mark.asyncio
async def test_health_without_token(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    response = await _request(app, "GET", "/v1/health")
    assert response.status_code == 200
    assert response.json()["enabled"] is True


@pytest.mark.asyncio
async def test_unauthorized_write_rejected(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    response = await _request(
        app, "POST", "/v1/uploads", content=b"# x", headers={"content-type": "text/markdown"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "knowledge_ingest_unauthorized"


@pytest.mark.asyncio
async def test_fail_closed_without_configured_token(tmp_path) -> None:
    app = _app(_settings(tmp_path, token=""))
    response = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"# x",
        headers=_auth_headers({"content-type": "text/markdown"}),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "knowledge_ingest_token_not_configured"


@pytest.mark.asyncio
async def test_non_loopback_client_rejected(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9050") as client:
        response = await client.post(
            "/v1/uploads",
            content=b"# x",
            headers=_auth_headers({"content-type": "text/markdown"}),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_upload_quarantine_and_submit_flow(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "plastic_knowledge.db"))
    monkeypatch.setenv("PP_KNOWLEDGE_BLOB_ROOT", str(tmp_path / "blobs"))
    app = _app(_settings(tmp_path))
    upload = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"# handbook\nSSH tunnel and backup instructions",
        headers=_auth_headers({"content-type": "text/markdown"}),
    )
    assert upload.status_code == 200
    upload_id = upload.json()["upload_id"]
    assert len(upload_id) == 64

    submit = await _request(
        app,
        "POST",
        "/v1/sources",
        json={
            "project_id": "project:kb",
            "source_name": "handbook",
            "content_sha256": upload_id,
            "space_name": "default",
        },
        headers=_auth_headers(),
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["submission"]["status"] == "done"
    job_id = body["submission"]["job_id"]
    assert body["job"]["status"] == "done"

    detail = await _request(
        app,
        "GET",
        f"/v1/jobs/{job_id}",
        headers=_auth_headers(),
        params={"project_id": "project:kb"},
    )
    assert detail.status_code == 200
    assert detail.json()["job"]["id"] == job_id

    jobs = await _request(
        app, "GET", "/v1/jobs", headers=_auth_headers(), params={"project_id": "project:kb"}
    )
    assert jobs.status_code == 200
    assert len(jobs.json()["jobs"]) == 1

    sources = await _request(
        app, "GET", "/v1/sources", headers=_auth_headers(), params={"project_id": "project:kb"}
    )
    assert sources.status_code == 200
    assert sources.json()["sources"][0]["name"] == "handbook"


@pytest.mark.asyncio
async def test_unknown_upload_reference_rejected(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    submit = await _request(
        app,
        "POST",
        "/v1/sources",
        json={
            "project_id": "project:kb",
            "source_name": "handbook",
            "content_sha256": "0" * 64,
        },
        headers=_auth_headers(),
    )
    assert submit.status_code == 404
    assert submit.json()["error"]["code"] == "knowledge_upload_not_found"


@pytest.mark.asyncio
async def test_cross_project_job_read_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "plastic_knowledge.db"))
    monkeypatch.setenv("PP_KNOWLEDGE_BLOB_ROOT", str(tmp_path / "blobs"))
    app = _app(_settings(tmp_path))
    upload = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"# a\nsecret",
        headers=_auth_headers({"content-type": "text/markdown"}),
    )
    submit = await _request(
        app,
        "POST",
        "/v1/sources",
        json={
            "project_id": "project:alpha",
            "source_name": "alpha-notes",
            "content_sha256": upload.json()["upload_id"],
        },
        headers=_auth_headers(),
    )
    job_id = submit.json()["submission"]["job_id"]
    detail = await _request(
        app,
        "GET",
        f"/v1/jobs/{job_id}",
        headers=_auth_headers(),
        params={"project_id": "project:beta"},
    )
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_oversize_upload_rejected(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    response = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"#" * 8192,
        headers=_auth_headers({"content-type": "text/markdown"}),
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_disallowed_media_type_rejected(tmp_path) -> None:
    app = _app(_settings(tmp_path))
    response = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"%PDF-1.7 fake",
        headers=_auth_headers({"content-type": "application/pdf"}),
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_binary_submit_fails_job_not_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "plastic_knowledge.db"))
    monkeypatch.setenv("PP_KNOWLEDGE_BLOB_ROOT", str(tmp_path / "blobs"))
    app = _app(_settings(tmp_path))
    upload = await _request(
        app,
        "POST",
        "/v1/uploads",
        content=b"# title\n\x00\x01binary payload",
        headers=_auth_headers({"content-type": "application/octet-stream"}),
    )
    assert upload.status_code == 200
    submit = await _request(
        app,
        "POST",
        "/v1/sources",
        json={
            "project_id": "project:kb",
            "source_name": "binary-notes",
            "content_sha256": upload.json()["upload_id"],
        },
        headers=_auth_headers(),
    )
    assert submit.status_code == 200
    assert submit.json()["submission"]["status"] == "failed"
