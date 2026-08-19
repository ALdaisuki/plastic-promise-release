from __future__ import annotations

import sqlite3

import pytest

from plastic_promise.core.knowledge_base import KnowledgeDocumentStore


def _store(conn: sqlite3.Connection) -> KnowledgeDocumentStore:
    return KnowledgeDocumentStore(conn, allow_legacy_test_adapter=True)


def test_legacy_markdown_store_is_disabled_without_explicit_test_opt_in():
    with pytest.raises(RuntimeError, match="legacy_knowledge_document_store_disabled"):
        KnowledgeDocumentStore(sqlite3.connect(":memory:"))


def test_markdown_source_is_retained_and_chunks_are_structure_aware():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    source = "# Deployment\n\n## Rollback\n\nKeep the previous generation available.\n"

    document = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="file:///docs/deployment.md",
        markdown=source,
        source_revision="git:abc123",
    )

    assert document.raw_text == source
    assert document.source_revision == "git:abc123"
    assert document.title == "Deployment"
    assert document.chunk_count >= 1
    chunks = store.list_chunks(project_id="project:alpha", document_id=document.document_id)
    assert chunks
    assert any(chunk["heading_path"] == ["Deployment", "Rollback"] for chunk in chunks)
    assert all(chunk["project_id"] == "project:alpha" for chunk in chunks)
    assert all(chunk["chunk_manifest_hash"] == document.chunk_manifest_hash for chunk in chunks)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
        ).fetchone()[0]
        == 0
    )


def test_markdown_upsert_is_idempotent_and_updates_original_without_losing_identity():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    first = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="https://github.com/example/repo/blob/main/README.md",
        markdown="# First\n\nOriginal body.\n",
    )
    replay = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="https://github.com/example/repo/blob/main/README.md",
        markdown="# First\n\nOriginal body.\n",
    )
    updated = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="https://github.com/example/repo/blob/main/README.md",
        markdown="# First\n\nUpdated body.\n",
        source_revision="git:def456",
    )

    assert replay.document_id == first.document_id
    assert replay.raw_text_sha256 == first.raw_text_sha256
    assert updated.document_id == first.document_id
    assert updated.raw_text == "# First\n\nUpdated body.\n"
    assert updated.source_revision == "git:def456"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 1


def test_knowledge_documents_are_hard_project_scoped():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    alpha = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="file:///alpha.md",
        markdown="# Alpha\n\nAlpha only.\n",
    )
    beta = store.upsert_markdown(
        project_id="project:beta",
        source_uri="file:///beta.md",
        markdown="# Beta\n\nBeta only.\n",
    )

    assert [doc.document_id for doc in store.list_documents(project_id="project:alpha")] == [
        alpha.document_id
    ]
    assert store.list_chunks(project_id="project:alpha", document_id=beta.document_id) == ()
    with pytest.raises(ValueError, match="knowledge_project_scope_required"):
        store.list_documents(project_id="project:unknown")


def test_domain_name_is_persisted_and_source_registry_is_allowlisted():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    document = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="file:///ops/rollback.md",
        markdown="# Operations\n\nRollback playbook.\n",
        domain_name="运维",
    )
    domain = conn.execute(
        "SELECT project_id, name, name_hash FROM knowledge_domains WHERE domain_id = ?",
        (document.domain_id,),
    ).fetchone()
    assert domain[0:2] == ("project:alpha", "运维")
    assert len(domain[2]) == 64

    source = store.register_source(
        project_id="project:alpha",
        platform="github",
        source_uri="https://github.com/example/repo",
    )
    assert source["platform"] == "github"
    with pytest.raises(ValueError, match="knowledge_source_platform_not_allowed"):
        store.register_source(
            project_id="project:alpha",
            platform="random-blog",
            source_uri="https://example.com/blog",
        )


def test_unknown_project_and_invalid_markdown_fail_closed():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    with pytest.raises(ValueError, match="knowledge_project_scope_required"):
        store.upsert_markdown(
            project_id="project:unknown",
            source_uri="file:///unknown.md",
            markdown="# Unknown",
        )
    with pytest.raises(ValueError, match="knowledge_markdown_required"):
        store.upsert_markdown(
            project_id="project:alpha",
            source_uri="file:///empty.md",
            markdown="   ",
        )


@pytest.mark.parametrize("visibility", ["shared", "global"])
def test_cross_project_knowledge_visibility_requires_governance(visibility: str):
    conn = sqlite3.connect(":memory:")
    store = _store(conn)

    with pytest.raises(ValueError, match="knowledge_visibility_governance_required"):
        store.upsert_markdown(
            project_id="project:alpha",
            source_uri=f"file:///{visibility}.md",
            markdown="# Governed later",
            visibility=visibility,
        )


def test_shared_and_global_visibility_require_governance_evidence():
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    for visibility in ("shared", "global"):
        with pytest.raises(ValueError, match="knowledge_visibility_governance_required"):
            store.upsert_markdown(
                project_id="project:alpha",
                source_uri=f"file:///{visibility}.md",
                markdown="# Governed",
                visibility=visibility,
            )
    conn.execute(
        "INSERT INTO knowledge_governance_evidence "
        "(relation_id, project_id, decision, status, created_at) "
        "VALUES (?, ?, 'approved', 'active', '2026-08-05T00:00:00Z')",
        ("memory:decision-123", "project:alpha"),
    )
    conn.commit()
    document = store.upsert_markdown(
        project_id="project:alpha",
        source_uri="file:///shared.md",
        markdown="# Governed",
        visibility="shared",
        governance_reason="reusable deployment contract",
        evidence_relation="memory:decision-123",
        governance_decision="approved",
    )
    assert document.governance_reason == "reusable deployment contract"
    assert document.evidence_relation == "memory:decision-123"
    assert document.governance_decision == "approved"
