"""Dashboard upload/proxy, source detail, restore drill and hook routing tests."""

from __future__ import annotations

from contextlib import nullcontext

import httpx
import pytest
from starlette.applications import Starlette

from plastic_promise.knowledge.blobs import FilesystemBlobStore
from plastic_promise.knowledge.ingestion import IngestCoordinator
from plastic_promise.knowledge.migrations import backup_evidence
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.knowledge.restore import restore_smoke_evidence
from plastic_promise.mcp.dashboard_v2.config import DashboardSettings
from plastic_promise.mcp.dashboard_v2.routes import create_dashboard_v2_routes
from plastic_promise.passive_memory.coordinator import (
    _render_knowledge_routing,
    strip_injected_context,
)


class _FakeRepository:
    def __init__(self, scope):
        self.scope = scope

    def overview(self):
        return {"data": {}, "scope": self.scope.to_dict(), "degraded": False}

    def list_requests(self, **kwargs):
        return {"data": [], "scope": self.scope.to_dict(), "degraded": False}

    def list_memories(self, **kwargs):
        return {"data": [], "scope": self.scope.to_dict(), "degraded": False}

    def passive_memory_overview(self, **kwargs):
        return {"summary": {}, "events": [], "quality_cases": []}

    def list_memory_proposals(self, **kwargs):
        return {"data": [], "scope": self.scope.to_dict(), "degraded": False}

    def list_synthesis(self, **kwargs):
        return {"data": [], "scope": self.scope.to_dict(), "degraded": False}

    def list_operations(self, **kwargs):
        return {"data": [], "scope": self.scope.to_dict(), "degraded": False}

    def get_trust(self, target=""):
        return {"target": target or "default", "trust": 0.53, "tier": "medium"}


def _settings() -> DashboardSettings:
    return DashboardSettings.from_env(
        {
            "PP_DASHBOARD_V2": "1",
            "PP_RETRIEVAL_EXPLAIN": "0",
            "PP_DASHBOARD_REVIEW_ACTIONS": "0",
            "PP_DASHBOARD_AUTH": "local",
            "PP_DASHBOARD_PROJECT_ID": "project:kb",
        },
        bind_host="127.0.0.1",
    )


def _app() -> Starlette:
    config = _settings()

    def provider(scope):
        return nullcontext(_FakeRepository(scope))

    routes = create_dashboard_v2_routes(
        config,
        repository_provider=provider,
        version="9.9.9",
        identity_provider=lambda: {"status": "ok", "runtime_mode": "normal"},
        issue_provider=lambda: [],
        proposal_review_provider=lambda *_: {"status": "unavailable"},
        project_scope_provider=lambda: [{"project_id": "project:kb", "latest_at": "x"}],
    )
    return Starlette(routes=routes)


async def _request(app: Starlette, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    headers = {"host": "127.0.0.1:9128"}
    headers.update(kwargs.pop("headers", {}) or {})
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9128") as client:
        return await client.request(
            method,
            path,
            headers=headers,
            params={"project_id": "project:kb"},
            **kwargs,
        )


def _seed_knowledge(tmp_path) -> tuple[KnowledgeRepository, str, str]:
    db = tmp_path / "plastic_knowledge.db"
    blob_root = tmp_path / "blobs"
    repository = KnowledgeRepository(db)
    repository.init_schema()
    blobs = FilesystemBlobStore(blob_root)
    coordinator = IngestCoordinator(repository, blobs, actor="test")
    submission = coordinator.submit_source(
        "project:kb",
        "# 内存对齐指南\n\n向量维度必须与嵌入模型一致，否则检索会失败。".encode(),
        source_name="memory-alignment.md",
        space_name="default",
        actor="test",
    )
    assert submission.status == "done"
    return repository, str(db), str(blob_root)


@pytest.mark.asyncio
async def test_upload_route_forwards_quarantine(monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", "/tmp/nonexistent-kb.db")
    captured = {}

    def fake_forward_upload(data: bytes, content_type: str) -> dict:
        captured["data"] = data
        captured["content_type"] = content_type
        return {"upload_id": "u" + "0" * 63, "byte_size": len(data), "quarantined": True}

    monkeypatch.setattr(
        "plastic_promise.mcp.dashboard_v2.routes.forward_upload", fake_forward_upload
    )
    app = _app()
    response = await _request(
        app,
        "POST",
        "/api/dashboard/v2/knowledge-uploads",
        content=b"# hello knowledge",
        headers={"content-type": "text/markdown"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["upload_id"].startswith("u")
    assert captured["data"] == b"# hello knowledge"
    assert captured["content_type"] == "text/markdown"


@pytest.mark.asyncio
async def test_upload_route_rejects_oversized_and_wrong_media(monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", "/tmp/nonexistent-kb.db")
    app = _app()
    response = await _request(
        app,
        "POST",
        "/api/dashboard/v2/knowledge-uploads",
        content=b"x" * (8 * 1024 * 1024 + 1),
        headers={"content-type": "application/pdf"},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "knowledge_upload_media_type"
    response = await _request(
        app,
        "POST",
        "/api/dashboard/v2/knowledge-uploads",
        content=b"x" * (8 * 1024 * 1024 + 1),
        headers={"content-type": "text/markdown"},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_source_submit_enforces_project_scope(monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", "/tmp/nonexistent-kb.db")
    called = {}

    def fake_forward_submit(payload: dict) -> dict:
        called["payload"] = payload
        return {"submission": {"job_id": "kj_1", "source_id": "ksrc_1", "status": "done"}}

    monkeypatch.setattr(
        "plastic_promise.mcp.dashboard_v2.routes.forward_submit", fake_forward_submit
    )
    app = _app()
    response = await _request(
        app,
        "POST",
        "/api/dashboard/v2/knowledge-sources/submit",
        json={
            "project_id": "project:other",
            "source_name": "x.md",
            "content_sha256": "u" + "0" * 63,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "knowledge_source_cross_project"

    response = await _request(
        app,
        "POST",
        "/api/dashboard/v2/knowledge-sources/submit",
        json={
            "project_id": "project:kb",
            "source_name": "x.md",
            "content_sha256": "u" + "0" * 63,
        },
    )
    assert response.status_code == 200
    assert called["payload"]["project_id"] == "project:kb"


@pytest.mark.asyncio
async def test_source_versions_and_chunks_routes(tmp_path, monkeypatch) -> None:
    _, db, _ = _seed_knowledge(tmp_path)
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", db)
    app = _app()
    response = await _request(app, "GET", "/api/dashboard/v2/knowledge-sources")
    assert response.status_code == 200
    sources = response.json()["data"]["sources"]
    assert sources and sources[0]["name"] == "memory-alignment.md"
    source_id = sources[0]["id"]

    response = await _request(
        app,
        "GET",
        f"/api/dashboard/v2/knowledge-sources/{source_id}/versions",
    )
    assert response.status_code == 200
    versions = response.json()["data"]["versions"]
    assert versions and versions[0]["version_no"] == 1
    assert versions[0]["chunk_count"] >= 1

    response = await _request(
        app,
        "GET",
        f"/api/dashboard/v2/knowledge-sources/{source_id}/chunks",
    )
    assert response.status_code == 200
    chunks = response.json()["data"]["chunks"]
    assert chunks and chunks[0]["chunk_id"]
    assert "向量" in chunks[0]["snippet"]

    response = await _request(
        app,
        "GET",
        "/api/dashboard/v2/knowledge-sources/ksrc_unknown/versions",
    )
    assert response.status_code == 404


def test_restore_smoke_evidence(tmp_path) -> None:
    _, db, blob_root = _seed_knowledge(tmp_path)
    backup = tmp_path / "backups" / "plastic_knowledge-backup.db"
    evidence = backup_evidence(db, backup)
    assert evidence["ok"] is True

    drill = restore_smoke_evidence(
        backup,
        blob_root=blob_root,
        project_id="project:kb",
        keep=True,
    )
    assert drill["ok"] is True
    assert drill["integrity_check"] == "ok"
    assert drill["backup_sha256"] == drill["restore_sha256"]
    assert drill["blobs_verified"] >= 1
    assert drill["blobs_missing"] == []
    assert drill["probe_total_hits"] >= 1
    assert drill["probe_hits"]


def test_knowledge_routing_render_and_strip(tmp_path, monkeypatch) -> None:
    _, db, _ = _seed_knowledge(tmp_path)
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_HOOK", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", db)
    block = _render_knowledge_routing(
        "向量维度不一致会怎样",
        "project:kb",
        max_chars=800,
    )
    assert "<knowledge-routing" in block
    assert "memory-alignment.md" in block
    assert "knowledge[" in block
    assert "SQLite" in block
    assert "derived" in block
    assert len(block) <= 800
    stripped = strip_injected_context("prefix\n" + block + "\nsuffix")
    assert "<knowledge-routing" not in stripped
    assert "prefix" in stripped
    assert "suffix" in stripped


def test_knowledge_routing_gate_off_and_missing_db(tmp_path, monkeypatch) -> None:
    _, db, _ = _seed_knowledge(tmp_path)
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_HOOK", "off")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", db)
    assert _render_knowledge_routing("向量", "project:kb", max_chars=800) == ""

    monkeypatch.setenv("PP_KNOWLEDGE_HOOK", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "missing.db"))
    assert _render_knowledge_routing("向量", "project:kb", max_chars=800) == ""
