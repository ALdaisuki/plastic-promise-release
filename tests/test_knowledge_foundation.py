"""Knowledge truth store foundation tests: interface, security, retrieval.

The first vertical slice is Markdown-first: upload Markdown -> immutable
Source Version -> structure-v1 Evidence Chunks -> lexical search with
citations.  Other file formats are intentionally out of scope.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from plastic_promise.core.chunking import CHUNK_SCHEMA_VERSION
from plastic_promise.knowledge.adapters.parser_markdown import (
    MarkdownTextParser,
    MarkdownTextParserError,
)
from plastic_promise.knowledge.blobs import (
    BlobStoreError,
    FilesystemBlobStore,
    MemoryBlobStore,
)
from plastic_promise.knowledge.ingestion import (
    IngestCoordinator,
    KnowledgeIngestionError,
)
from plastic_promise.knowledge.migrations import (
    backup_evidence,
    migrate_dry_run,
    schema_check,
)
from plastic_promise.knowledge.query import LexicalKnowledgeQuery, tokenize
from plastic_promise.knowledge.repository import SCHEMA_TABLES, KnowledgeRepository

if TYPE_CHECKING:
    from pathlib import Path


MARKDOWN_FIXTURE = """\
# 部署手册

## SSH 隧道

MacBook 通过 SSH LocalForward 访问服务器 MCP，9020 端口不暴露公网。

## 备份

每日使用 SQLite Online Backup API 创建备份，quick_check 必须为 ok。

## 回滚

保留 Windows 原数据库 7-14 天作为只读回滚副本。
"""


def _coordinator(tmp_path: Path, *, memory_blobs: bool = True) -> IngestCoordinator:
    repository = KnowledgeRepository(tmp_path / "plastic_knowledge.db")
    blobs = MemoryBlobStore() if memory_blobs else FilesystemBlobStore(tmp_path / "blobs")
    return IngestCoordinator(repository, blobs, actor="test")


def test_knowledge_scope_rejects_unknown_and_cross_project_space_binding(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "plastic_knowledge.db")
    repository.init_schema()
    with pytest.raises(ValueError, match="knowledge_project_scope_required"):
        repository.get_or_create_space("project:unknown", "default")

    alpha_space = repository.get_or_create_space("project:alpha", "default")
    with pytest.raises(ValueError, match="knowledge_space_project_mismatch"):
        repository.create_source(
            project_id="project:beta",
            space_id=alpha_space,
            kind="upload",
            name="cross-project",
            origin_ref=None,
        )

    with pytest.raises(KnowledgeIngestionError, match="project_id is required"):
        _coordinator(tmp_path).submit_source(
            "project:unknown",
            MARKDOWN_FIXTURE.encode("utf-8"),
            source_name="unknown-project",
        )


def test_knowledge_repository_rejects_cross_project_relationships(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "plastic_knowledge.db")
    coordinator = IngestCoordinator(repository, MemoryBlobStore(), actor="test")
    alpha = coordinator.submit_source(
        "project:alpha",
        MARKDOWN_FIXTURE.encode("utf-8"),
        source_name="alpha-source",
    )
    source = repository.get_source(alpha.source_id)
    version = coordinator.get_versions(alpha.source_id)[0]

    with pytest.raises(ValueError, match="knowledge_job_source_project_mismatch"):
        repository.create_job(
            project_id="project:beta",
            source_id=source.id,
            stage="parse",
        )

    with pytest.raises(ValueError, match="knowledge_semantic_space_project_mismatch"):
        repository.create_semantic_job(
            {
                "project_id": "project:beta",
                "space_id": source.space_id,
                "version_id": version.id,
                "batch_sha256": "a" * 64,
            }
        )

    semantic_job_id = repository.create_semantic_job(
        {
            "project_id": "project:alpha",
            "space_id": source.space_id,
            "version_id": version.id,
            "batch_sha256": "b" * 64,
        }
    )
    with pytest.raises(ValueError, match="knowledge_semantic_unit_job_project_mismatch"):
        repository.insert_semantic_units(
            [{"project_id": "project:beta", "job_id": semantic_job_id}]
        )

    artifact_id = repository.upsert_artifact(
        {
            "project_id": "project:alpha",
            "kind": "source-summary",
            "title": "Alpha",
            "content": "Alpha summary",
            "content_hash": "c" * 64,
        }
    )
    chunk_id = repository.list_chunks(source.id)[0]["chunk_id"]
    with pytest.raises(ValueError, match="knowledge_citation_artifact_project_mismatch"):
        repository.insert_citation(artifact_id, chunk_id, "project:beta")


# -- interface: version reuse and blob deduplication ----------------------


def test_identical_bytes_same_source_reuse_version(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.submit_source(
        "project:kb",
        MARKDOWN_FIXTURE.encode("utf-8"),
        source_name="deploy-handbook",
        actor="test",
    )
    assert first.status == "done"
    second = coordinator.submit_source(
        "project:kb",
        MARKDOWN_FIXTURE.encode("utf-8"),
        source_name="deploy-handbook",
        actor="test",
    )
    assert second.status == "done_reused"
    assert first.reused_version_id is None
    assert second.reused_version_id is not None
    versions = coordinator.get_versions(first.source_id)
    assert len(versions) == 1
    jobs = coordinator.list_jobs("project:kb")
    assert len(jobs) == 1


def test_identical_bytes_two_sources_share_blob_without_merging(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "plastic_knowledge.db")
    blobs = MemoryBlobStore()
    coordinator = IngestCoordinator(repository, blobs, actor="test")
    first = coordinator.submit_source(
        "project:kb", b"# shared\nsame bytes", source_name="source-a", actor="test"
    )
    second = coordinator.submit_source(
        "project:kb", b"# shared\nsame bytes", source_name="source-b", actor="test"
    )
    assert first.source_id != second.source_id
    assert blobs.counts()["blobs"] == 1
    first_version = coordinator.get_versions(first.source_id)[0]
    second_version = coordinator.get_versions(second.source_id)[0]
    assert first_version.blob_sha256 == second_version.blob_sha256


def test_source_update_creates_new_version_and_stales_old(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.submit_source(
        "project:kb", b"# v1\nold facts", source_name="notes", actor="test"
    )
    old_version = coordinator.get_versions(first.source_id)[0]
    coordinator.submit_source(
        "project:kb", b"# v2\nbright new guidance replaces prior", source_name="notes", actor="test"
    )
    versions = coordinator.get_versions(first.source_id)
    assert len(versions) == 2
    new_version = next(v for v in versions if v.id != old_version.id)
    assert new_version.status == "active"
    refreshed_old = next(v for v in versions if v.id == old_version.id)
    assert refreshed_old.status == "stale"
    assert refreshed_old.superseded_at is not None
    # Old chunk citation remains resolvable through include_stale.
    query = LexicalKnowledgeQuery(coordinator._repository)
    active = query.search("project:kb", "old facts")
    assert active.total_hits == 0
    with_stale = query.search("project:kb", "old facts", include_stale=True)
    assert with_stale.total_hits >= 1
    assert any(hit.version_id == old_version.id for hit in with_stale.hits)


def test_expired_lease_can_be_reclaimed(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "plastic_knowledge.db")
    repository.init_schema()
    job = repository.create_job(project_id="project:kb", source_id=None, stage="parse")
    assert repository.claim_job(job.id, owner="worker-a", lease_seconds=1)
    # An active lease cannot be stolen.
    assert not repository.claim_job(job.id, owner="worker-b", lease_seconds=60)
    # Simulate lease expiry: backdate the timestamp.
    with repository.connect() as connection:
        connection.execute(
            "UPDATE knowledge_ingest_jobs SET lease_expires_at='2020-01-01T00:00:00.000Z'"
            " WHERE id=?",
            (job.id,),
        )
    assert repository.claim_job(job.id, owner="worker-b", lease_seconds=60)
    view = repository.get_job(job.id)
    assert view.status == "running"
    assert view.attempts == 2


def test_cross_project_isolation(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source(
        "project:alpha",
        b"# alpha\nalpha secret deployment details",
        source_name="alpha-notes",
        actor="test",
    )
    coordinator.submit_source(
        "project:beta",
        b"# beta\nbeta unrelated content",
        source_name="beta-notes",
        actor="test",
    )
    query = LexicalKnowledgeQuery(coordinator._repository)
    result = query.search("project:alpha", "alpha secret")
    assert result.total_hits >= 1
    assert all(hit.source_id.startswith("ksrc_") for hit in result.hits)
    # No cross-project leakage even with a broad query.
    result = query.search("project:alpha", "beta")
    assert result.total_hits == 0


# -- security: blob store and parser boundaries ---------------------------


def test_filesystem_blob_rejects_invalid_digest(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path / "blobs")
    with pytest.raises(BlobStoreError):
        store.read("not-a-hex-digest")
    assert store.has("../escape") is False


def test_blob_content_addressing_roundtrip(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path / "blobs")
    first = store.put(b"identical payload")
    second = store.put(b"identical payload")
    assert first.sha256 == second.sha256
    assert store.read(first.sha256) == b"identical payload"
    assert store.has(first.sha256) is True


def test_parser_rejects_binary_and_oversized() -> None:
    parser = MarkdownTextParser(max_bytes=64)
    with pytest.raises(MarkdownTextParserError):
        parser.parse(b"# ok\n\x00\x01\x02binary")
    with pytest.raises(MarkdownTextParserError):
        parser.parse(b"#" + b"x" * 65)
    with pytest.raises(MarkdownTextParserError):
        parser.parse("# invalid utf8\n\xff\xfe".encode("latin-1"))


def test_parser_extracts_title_and_preserves_verbatim_text() -> None:
    parser = MarkdownTextParser()
    document = parser.parse("# 部署手册\n\n## SSH 隧道\n\n正文内容".encode())
    assert document.title == "部署手册"
    assert document.parse_schema == CHUNK_SCHEMA_VERSION
    assert "## SSH 隧道" in document.text
    assert document.parser_id == "markdown-text-v1"


# -- migrations and backup evidence ----------------------------------------


def test_schema_check_and_migrate_dry_run(tmp_path: Path) -> None:
    db = tmp_path / "plastic_knowledge.db"
    check = schema_check(db)
    assert check["ok"] is True
    assert all(check["tables"].values())
    plan = migrate_dry_run(db)
    assert plan["would_execute"] is True
    assert plan["statement_count"] >= len(SCHEMA_TABLES)


def test_backup_evidence_is_valid_snapshot(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source(
        "project:kb", b"# backup\ncontent", source_name="source", actor="test"
    )
    target = tmp_path / "backups" / "kb.bak.db"
    evidence = backup_evidence(tmp_path / "plastic_knowledge.db", target)
    assert evidence["ok"] is True
    assert evidence["quick_check"] == "ok"
    assert len(evidence["sha256"]) == 64
    with sqlite3.connect(target) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "knowledge_chunks" in tables


# -- retrieval --------------------------------------------------------------


def test_tokenize_bilingual() -> None:
    tokens = tokenize("SSH 隧道 Backup 备份 backup")
    assert "ssh" in tokens
    assert "backup" in tokens
    assert tokens.count("backup") == 1
    assert "隧" in tokens and "道" in tokens


def test_lexical_recall_with_citations(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source(
        "project:kb",
        MARKDOWN_FIXTURE.encode("utf-8"),
        source_name="deploy-handbook",
        actor="test",
    )
    query = LexicalKnowledgeQuery(coordinator._repository)
    result = query.search("project:kb", "SSH 隧道")
    assert result.total_hits >= 1
    top = result.hits[0]
    assert top.source_name == "deploy-handbook"
    assert "SSH" in top.snippet
    assert top.header_path
    assert top.chunk_id
    assert top.version_id


def test_exact_identifier_retrieval_dominates(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source(
        "project:kb",
        b"# ids\nunique needle phrase alpha",
        source_name="ids",
        actor="test",
    )
    query = LexicalKnowledgeQuery(coordinator._repository)
    result = query.search("project:kb", "alpha")
    assert result.total_hits >= 1
    chunk_id = result.hits[0].chunk_id
    exact = query.search("project:kb", chunk_id)
    assert exact.total_hits >= 1
    assert exact.hits[0].chunk_id == chunk_id


def test_unknown_query_returns_empty_not_error(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source("project:kb", b"# x\nfiller content", source_name="x", actor="test")
    query = LexicalKnowledgeQuery(coordinator._repository)
    result = query.search("project:kb", "完全不存在的内容词语")
    assert result.total_hits == 0
    assert result.hits == ()
    assert result.degraded is False


def test_query_on_missing_store_is_empty_not_file_creating(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "absent" / "plastic_knowledge.db")
    query = LexicalKnowledgeQuery(repository)
    result = query.search("project:kb", "anything")
    assert result.total_hits == 0
    assert repository.db_path.exists() is False


def test_lexical_query_space_filter(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.submit_source(
        "project:kb",
        b"# alpha\nunique needle alpha content",
        source_name="alpha-doc",
        space_name="alpha-space",
        actor="test",
    )
    coordinator.submit_source(
        "project:kb",
        b"# beta\nunique needle beta content",
        source_name="beta-doc",
        space_name="beta-space",
        actor="test",
    )
    repository = coordinator._repository
    alpha_space = repository.get_or_create_space("project:kb", "alpha-space")
    beta_space = repository.get_or_create_space("project:kb", "beta-space")

    query = LexicalKnowledgeQuery(repository)
    alpha_hits = query.search("project:kb", "unique needle", space_id=alpha_space, limit=10).hits
    beta_hits = query.search("project:kb", "unique needle", space_id=beta_space, limit=10).hits

    assert alpha_hits
    assert beta_hits
    assert {hit.source_name for hit in alpha_hits} == {"alpha-doc"}
    assert {hit.source_name for hit in beta_hits} == {"beta-doc"}


def test_knowledge_restore_smoke_cli(tmp_path: Path) -> None:
    import json as _json
    import subprocess
    import sys

    from plastic_promise.knowledge.migrations import backup_evidence

    coordinator = _coordinator(tmp_path, memory_blobs=False)
    coordinator.submit_source(
        "project:kb",
        b"# smoke\nrestore drill probe content",
        source_name="smoke-doc",
        space_name="smoke-space",
        actor="test",
    )
    db_path = coordinator._repository.db_path
    backup_path = tmp_path / "backup" / "plastic_knowledge.db"
    backup_evidence(db_path, backup_path)

    script = (
        "import sys; from plastic_promise.cli import main; "
        "sys.argv=['plastic-promise','knowledge','restore-smoke',"
        "'--backup',%r,'--blob-root',%r,'--project','project:kb']; main()"
        % (str(backup_path), str(tmp_path / "blobs"))
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    evidence = _json.loads(proc.stdout)
    assert evidence["ok"] is True
    assert evidence["integrity_check"] == "ok"
    assert evidence["probe_total_hits"] >= 1


def test_unsupported_kind_and_empty_content_rejected(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    with pytest.raises(KnowledgeIngestionError):
        coordinator.submit_source(
            "project:kb", b"x", source_name="x", kind="presentation", actor="test"
        )
    with pytest.raises(KnowledgeIngestionError):
        coordinator.submit_source("project:kb", b"", source_name="x", actor="test")


# -- MCP handler ------------------------------------------------------------


def test_knowledge_search_handler_gate_off_degrades(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "off")
    import asyncio

    from plastic_promise.mcp.tools.knowledge import handle_knowledge_search

    result = asyncio.run(handle_knowledge_search(None, {"query": "x", "project_id": "project:kb"}))
    import json

    body = json.loads(result[0].text)
    assert body["degraded"] is True
    assert body["error"] == "knowledge_search_disabled"


def test_knowledge_search_handler_returns_citations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SYSTEM", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_RETRIEVAL", "on")
    db_path = tmp_path / "plastic_knowledge.db"
    monkeypatch.setenv("PP_KNOWLEDGE_DB_PATH", str(db_path))
    coordinator = IngestCoordinator(KnowledgeRepository(db_path), MemoryBlobStore(), actor="test")
    coordinator.submit_source(
        "project:kb",
        b"# handbook\nSSH tunnel and backup instructions",
        source_name="handbook",
        actor="test",
    )
    import asyncio
    import json

    from plastic_promise.mcp.tools.knowledge import handle_knowledge_search

    result = asyncio.run(
        handle_knowledge_search(None, {"query": "SSH", "project_id": "project:kb"})
    )
    body = json.loads(result[0].text)
    assert body["total_hits"] >= 1
    assert body["hits"][0]["source_name"] == "handbook"
    assert body["hits"][0]["chunk_id"]
