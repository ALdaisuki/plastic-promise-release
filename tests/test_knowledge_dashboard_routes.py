"""Knowledge Dashboard V2 route tests (Markdown-first slice)."""

from __future__ import annotations

from contextlib import nullcontext

import httpx
import pytest
from starlette.applications import Starlette

from plastic_promise.knowledge.blobs import MemoryBlobStore
from plastic_promise.knowledge.ingestion import IngestCoordinator
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.mcp.dashboard_v2.config import DashboardSettings
from plastic_promise.mcp.dashboard_v2.routes import create_dashboard_v2_routes


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


def _app():
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


async def _request(app, path: str, project: str):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9128") as client:
        return await client.get(
            path,
            headers={"host": "127.0.0.1:9128"},
            params={"project_id": project},
        )


@pytest.mark.asyncio
async def test_knowledge_routes_disabled_when_gate_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "off")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "plastic_knowledge.db"))
    app = _app()
    response = await _request(app, "/api/dashboard/v2/knowledge-sources", "project:kb")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["enabled"] is False
    assert body["data"]["sources"] == []


@pytest.mark.asyncio
async def test_knowledge_routes_serve_seeded_project(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "on")
    db_path = tmp_path / "plastic_knowledge.db"
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(db_path))
    coordinator = IngestCoordinator(KnowledgeRepository(db_path), MemoryBlobStore(), actor="test")
    coordinator.submit_source(
        "project:kb",
        b"# handbook\nSSH tunnel and backup instructions",
        source_name="handbook",
        actor="test",
    )
    app = _app()
    sources = await _request(app, "/api/dashboard/v2/knowledge-sources", "project:kb")
    assert sources.status_code == 200
    body = sources.json()["data"]
    assert body["enabled"] is True
    assert len(body["sources"]) == 1
    assert body["sources"][0]["name"] == "handbook"
    assert body["sources"][0]["versions"][0]["chunk_count"] >= 1

    jobs = await _request(app, "/api/dashboard/v2/knowledge-jobs", "project:kb")
    assert jobs.status_code == 200
    job_body = jobs.json()["data"]
    assert job_body["enabled"] is True
    assert any(job["status"] == "done" for job in job_body["jobs"])


@pytest.mark.asyncio
async def test_knowledge_semantic_domains_and_artifacts_are_project_scoped(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    monkeypatch.setenv("PP_KNOWLEDGE_AUTO_DOMAINS", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_WIKI", "shadow")
    db_path = tmp_path / "plastic_knowledge.db"
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(db_path))
    repository = KnowledgeRepository(db_path)
    repository.init_schema()
    for project_id, suffix in (("project:kb", "local"), ("project:other", "foreign")):
        space_id = repository.get_or_create_space(project_id, f"space-{suffix}")
        source = repository.create_source(
            project_id=project_id,
            space_id=space_id,
            kind="upload",
            name=f"source-{suffix}",
            origin_ref=None,
        )
        version = repository.create_version(
            source_id=source.id,
            content_hash=("a" if suffix == "local" else "b") * 64,
            blob_sha256=("c" if suffix == "local" else "d") * 64,
            byte_size=1,
            parser_id="test",
            parse_schema="structure-v1",
            document_title=f"{suffix} document",
            structure_manifest={},
        )
        repository.create_semantic_job(
            {
                "project_id": project_id,
                "space_id": space_id,
                "version_id": version.id,
                "batch_sha256": f"batch-{suffix}",
                "chunks": [],
            }
        )
        repository.upsert_domain_candidate(
            project_id=project_id,
            name=f"{suffix} domain",
            description=f"{suffix} description",
            source_id=source.id,
            space_id=space_id,
            evidence={
                "source_ids": source.id,
                "space_ids": space_id,
            },
        )
        repository.upsert_artifact(
            {
                "project_id": project_id,
                "kind": "source_summary",
                "title": f"{suffix} artifact",
                "content": f"{suffix} content",
                "content_hash": f"hash-{suffix}",
                "risk_tier": "low",
                "source_ids": [f"source-{suffix}"],
            }
        )

    app = _app()
    semantic = await _request(app, "/api/dashboard/v2/knowledge-semantic", "project:kb")
    domains = await _request(app, "/api/dashboard/v2/knowledge-domains", "project:kb")
    artifacts = await _request(app, "/api/dashboard/v2/knowledge-artifacts", "project:kb")

    assert semantic.status_code == 200
    semantic_data = semantic.json()["data"]
    assert semantic_data["enabled"] is True
    assert semantic_data["mode"] == "shadow"
    assert semantic_data["status"] == {
        "pending": 1,
        "building": 0,
        "done": 0,
        "failed": 0,
    }
    assert semantic_data["authority"] == "sqlite"
    assert semantic_data["derived_index"] == "rebuildable_only"

    assert domains.status_code == 200
    domain_rows = domains.json()["data"]["domains"]
    assert [row["name"] for row in domain_rows] == ["local domain"]
    assert domain_rows[0]["aliases"] == []
    assert "evidence_json" not in domain_rows[0]

    assert artifacts.status_code == 200
    artifact_rows = artifacts.json()["data"]["artifacts"]
    assert [row["title"] for row in artifact_rows] == ["local artifact"]
    assert artifact_rows[0]["source_ids"] == ["source-local"]
    assert "source_ids_json" not in artifact_rows[0]


@pytest.mark.asyncio
async def test_knowledge_semantic_views_fail_closed_with_empty_projections(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "off")
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(tmp_path / "missing.db"))
    app = _app()

    semantic = await _request(app, "/api/dashboard/v2/knowledge-semantic", "project:kb")
    domains = await _request(app, "/api/dashboard/v2/knowledge-domains", "project:kb")
    artifacts = await _request(app, "/api/dashboard/v2/knowledge-artifacts", "project:kb")

    assert semantic.json()["data"] == {
        "enabled": False,
        "mode": "off",
        "status": {"pending": 0, "building": 0, "done": 0, "failed": 0},
        "authority": "sqlite",
        "derived_index": "rebuildable_only",
        "note": "PP_KNOWLEDGE_SYSTEM is off",
    }
    assert domains.json()["data"]["domains"] == []
    assert artifacts.json()["data"]["artifacts"] == []


@pytest.mark.asyncio
async def test_domain_dashboard_bound_does_not_truncate_core_domain_reads(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "on")
    db_path = tmp_path / "plastic_knowledge.db"
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(db_path))
    repository = KnowledgeRepository(db_path)
    repository.init_schema()
    for index in range(101):
        repository.upsert_domain_candidate(
            project_id="project:kb",
            name=f"domain-{index:03d}",
            description="bounded dashboard projection",
            source_id=f"source-{index:03d}",
            space_id="space:kb",
            evidence={"source_ids": f"source-{index:03d}", "space_ids": "space:kb"},
        )

    response = await _request(_app(), "/api/dashboard/v2/knowledge-domains", "project:kb")

    assert len(repository.list_domains("project:kb")) == 101
    assert len(response.json()["data"]["domains"]) == 100


@pytest.mark.asyncio
async def test_knowledge_semantic_dashboard_assets_register_read_only_views() -> None:
    app = _app()
    shell = await _request(app, "/dashboard", "project:kb")
    script = await _request(app, "/dashboard/assets/v2/app.js", "project:kb")

    assert shell.status_code == 200
    for route in ("#/knowledge-semantic", "#/knowledge-domains", "#/knowledge-artifacts"):
        assert route in shell.text
    for renderer in (
        "function renderKnowledgeSemantic",
        "function renderKnowledgeDomains",
        "function renderKnowledgeArtifacts",
    ):
        assert renderer in script.text
    assert 'endpoint: "/knowledge-semantic"' in script.text
    assert 'endpoint: "/knowledge-domains"' in script.text
    assert 'endpoint: "/knowledge-artifacts"' in script.text
    assert '"knowledge-domains": {' in script.text
    assert '"knowledge-artifacts": {' in script.text
    assert script.text.count("paginated: false") >= 3
