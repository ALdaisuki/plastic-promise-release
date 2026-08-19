from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING

import lancedb
import pytest

from plastic_promise.core.chunking import CHUNK_SCHEMA_VERSION, build_chunk_manifest
from plastic_promise.knowledge.lancedb_shadow import (
    PROJECT_WIDE_DOMAIN_ID,
    SHADOW_FUSION_POLICY_IDENTITY,
    SHADOW_PROJECTION_VERSION,
    TABLE_NAME,
    KnowledgeLanceShadowBuilder,
)
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.knowledge.semantic import SEMANTIC_PROMPT_SHA256, SEMANTIC_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path


class FakeEmbeddingProvider:
    def __init__(
        self,
        identity: str = "fake-embedding@v1",
        *,
        endpoint: str | None = None,
        fail_on_call: int | None = None,
        on_call=None,
    ):
        self._identity = identity
        self._endpoint = endpoint
        self._fail_on_call = fail_on_call
        self._on_call = on_call
        self.calls: list[list[str]] = []

    @property
    def dim(self) -> int:
        return 3

    @property
    def index_model_name(self) -> str:
        return self._identity

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    @property
    def stats(self) -> dict[str, object]:
        return {
            "requests": len(self.calls),
            "input_count": sum(len(call) for call in self.calls),
            "pricing_revision": "fake-pricing-v1",
        }

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._on_call is not None:
            callback, self._on_call = self._on_call, None
            callback()
        if self._fail_on_call == len(self.calls):
            raise RuntimeError("private provider response must not escape")
        return [_vector(text) for text in texts]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [1.0, (digest[0] + 1) / 256, (digest[1] + 1) / 256]


def _seed(repository: KnowledgeRepository, project_id: str) -> dict[str, str]:
    repository.init_schema()
    result: dict[str, str] = {}
    for suffix in ("a", "b"):
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
            content_hash=hashlib.sha256(f"content-{suffix}".encode()).hexdigest(),
            blob_sha256=hashlib.sha256(f"blob-{suffix}".encode()).hexdigest(),
            byte_size=10,
            parser_id="markdown-text-v1",
            parse_schema="structure-v1",
            document_title=f"document-{suffix}",
            structure_manifest=build_chunk_manifest(
                f"content-{suffix}", target_chars=1200, hard_chars=2400
            ),
        )
        repository.insert_chunks(
            version.id,
            [
                {
                    "chunk_id": f"chunk-{suffix}",
                    "ordinal": 0,
                    "kind": "paragraph",
                    "header_path": [],
                    "source_start": 0,
                    "source_end": len(f"content-{suffix}"),
                    "text_hash": hashlib.sha256(f"content-{suffix}".encode()).hexdigest(),
                    "text": f"content-{suffix}",
                }
            ],
        )
        job_id = repository.create_semantic_job(
            {
                "project_id": project_id,
                "space_id": space_id,
                "version_id": version.id,
                "batch_sha256": hashlib.sha256(f"{project_id}-{suffix}".encode()).hexdigest(),
            }
        )
        text = f"grounded semantic unit {suffix}"
        repository.insert_semantic_units(
            [
                {
                    "job_id": job_id,
                    "project_id": project_id,
                    "space_id": space_id,
                    "source_id": source.id,
                    "version_id": version.id,
                    "kind": "fact",
                    "text": text,
                    "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "evidence_chunk_ids": [f"chunk-{suffix}"],
                    "metadata": {"domain_ids": [f"domain-{suffix}"]},
                    "payload_hash": hashlib.sha256(f"payload-{suffix}".encode()).hexdigest(),
                }
            ]
        )
        artifact_id = repository.upsert_artifact(
            {
                "project_id": project_id,
                "kind": "source_summary",
                "title": f"summary {suffix}",
                "content": f"active artifact {suffix}",
                "content_hash": hashlib.sha256(f"artifact-{suffix}".encode()).hexdigest(),
                "risk_tier": "low",
                "source_ids": [source.id],
            }
        )
        repository.update_artifact_status(artifact_id, "active", citation_coverage=1.0)
        result.update(
            {
                f"space_{suffix}": space_id,
                f"source_{suffix}": source.id,
                f"version_{suffix}": version.id,
                f"artifact_{suffix}": artifact_id,
            }
        )
    return result


def _add_source_only(repository: KnowledgeRepository, project_id: str, suffix: str) -> str:
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
        content_hash=hashlib.sha256(f"content-{suffix}".encode()).hexdigest(),
        blob_sha256=hashlib.sha256(f"blob-{suffix}".encode()).hexdigest(),
        byte_size=10,
        parser_id="markdown-text-v1",
        parse_schema="structure-v1",
        document_title=f"document-{suffix}",
        structure_manifest=build_chunk_manifest(
            f"content-{suffix}", target_chars=1200, hard_chars=2400
        ),
    )
    return version.id


def _rows(root: Path, generation_id: str) -> list[dict[str, object]]:
    database = lancedb.connect(root / generation_id / "index")
    return database.open_table(TABLE_NAME).to_arrow().to_pylist()


def test_gate_defaults_off_without_provider_or_filesystem_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PP_KNOWLEDGE_LANCE_SHADOW", raising=False)
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    repository.init_schema()
    provider = FakeEmbeddingProvider()
    root = tmp_path / "shadow"

    result = KnowledgeLanceShadowBuilder(repository, root=root, provider=provider).build(
        "project:kb"
    )

    assert result == {
        "status": "disabled",
        "reason": "knowledge_lance_shadow_disabled",
        "gate": "off",
        "promotion_eligible": False,
    }
    assert provider.calls == []
    assert not root.exists()
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_generations").fetchone()[0] == 0


@pytest.mark.parametrize("gate", ["shadow", "on"])
def test_builds_project_wide_active_records_with_explicit_identity(
    tmp_path: Path, monkeypatch, gate: str
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", gate)
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    _seed(repository, "project:other")
    provider = FakeEmbeddingProvider()
    root = tmp_path / "shadow"

    result = KnowledgeLanceShadowBuilder(repository, root=root, provider=provider).build(
        "project:kb"
    )

    assert result["status"] == "shadow"
    assert result["promotion_eligible"] is False
    assert result["metrics"]["canonical_records"] == 4
    rows = _rows(root, result["generation_id"])
    assert {row["record_kind"] for row in rows} == {"semantic_unit", "artifact"}
    assert {row["project_id"] for row in rows} == {"project:kb"}
    assert {row["domain_id"] for row in rows} == {PROJECT_WIDE_DOMAIN_ID}
    assert all(row["version_ids_json"] for row in rows)
    assert all("other" not in str(row) for row in rows)

    generation = repository.shadow_generation(result["generation_id"])
    assert generation is not None
    assert generation["status"] == "shadow"
    assert generation["activated_at"] is None
    manifest = json.loads(str(generation["manifest_json"]))
    assert manifest["truth_store"] == "sqlite"
    assert manifest["index_role"] == "rebuildable-shadow"
    assert manifest["promotion_eligible"] is False
    identity = manifest["identity"]
    assert identity["domain_id"] == PROJECT_WIDE_DOMAIN_ID
    assert identity["chunking_schema"] == CHUNK_SCHEMA_VERSION
    assert identity["chunking_identities"] == [
        "structure-v1|target_chars=1200|hard_chars=2400|max_chunks=unbounded"
        "|offsets=unicode-codepoints"
    ]
    assert identity["semantic_schema_version"] == SEMANTIC_SCHEMA_VERSION
    assert identity["semantic_prompt_sha256"] == SEMANTIC_PROMPT_SHA256
    assert identity["projection_version"] == SHADOW_PROJECTION_VERSION
    assert identity["fusion_policy_identity"] == SHADOW_FUSION_POLICY_IDENTITY
    assert len(identity["source_revision_set_sha256"]) == 64
    assert len(identity["corpus_sha256"]) == 64
    with pytest.raises(ValueError, match="knowledge_shadow_generation_status_invalid"):
        repository.record_shadow_generation(
            result["generation_id"], "production", manifest, actor="test"
        )


def test_domain_names_are_project_scoped_embedding_projection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    seeded = _seed(repository, "project:kb")
    _seed(repository, "project:other")
    domain_id = repository.create_domain(
        project_id="project:kb",
        name="开发",
        description="工程开发知识",
        kind="active",
    )
    other_domain_id = repository.create_domain(
        project_id="project:other",
        name="不应泄漏",
        description="其他项目",
        kind="active",
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE knowledge_semantic_units SET metadata_json=? WHERE id IN "
            " (SELECT id FROM knowledge_semantic_units WHERE project_id=? ORDER BY id LIMIT 1)",
            (json.dumps({"domain_ids": [domain_id]}), "project:kb"),
        )
        connection.execute(
            "UPDATE knowledge_semantic_units SET metadata_json=? WHERE id IN "
            " (SELECT id FROM knowledge_semantic_units WHERE project_id=? ORDER BY id LIMIT 1)",
            (json.dumps({"domain_ids": [other_domain_id]}), "project:other"),
        )
    provider = FakeEmbeddingProvider()
    result = KnowledgeLanceShadowBuilder(
        repository, root=tmp_path / "shadow", provider=provider
    ).build("project:kb")

    assert result["status"] == "shadow"
    embedded_texts = [text for batch in provider.calls for text in batch]
    assert any("[domain:开发]" in text for text in embedded_texts)
    assert all("不应泄漏" not in text for text in embedded_texts)
    assert seeded["source_a"]


def test_unsupported_domain_fails_before_provider_filesystem_or_generation_writes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    repository.init_schema()
    provider = FakeEmbeddingProvider()
    root = tmp_path / "shadow"

    result = KnowledgeLanceShadowBuilder(repository, root=root, provider=provider).build(
        "project:kb", domain_id="domain:finance"
    )

    assert result == {
        "status": "failed",
        "generation_id": None,
        "failure_code": "knowledge_lance_domain_bindings_unavailable",
        "promotion_eligible": False,
    }
    assert provider.calls == []
    assert not root.exists()
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_generations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM knowledge_audit_events").fetchone()[0] == 0


def test_rerun_reuses_rows_and_reconciles_orphan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    provider = FakeEmbeddingProvider()
    root = tmp_path / "shadow"
    builder = KnowledgeLanceShadowBuilder(repository, root=root, provider=provider)

    first = builder.build("project:kb")
    calls_after_first = len(provider.calls)
    second = builder.build("project:kb")
    assert second["generation_id"] == first["generation_id"]
    assert second["resumed"] is True
    assert second["metrics"]["embedded_records"] == 0
    assert len(provider.calls) == calls_after_first

    database = lancedb.connect(root / first["generation_id"] / "index")
    table = database.open_table(TABLE_NAME)
    template = dict(table.to_arrow().to_pylist()[0])
    template.update(
        {
            "record_id": "artifact:orphan",
            "canonical_hash": "f" * 64,
            "vector": [1.0, 0.5, 0.25],
        }
    )
    table.add([template])

    reconciled = builder.build("project:kb")
    assert reconciled["metrics"]["reconciled_deleted"] == 1
    assert reconciled["metrics"]["embedded_records"] == 0
    assert len(provider.calls) == calls_after_first
    assert "artifact:orphan" not in {
        row["record_id"] for row in _rows(root, first["generation_id"])
    }


def test_generation_identity_isolates_project_config_and_provider_and_normalizes_alias(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    _seed(repository, "project:other")
    root = tmp_path / "shadow"

    def build(project_id: str, *, domain_id: str = "all", provider="provider-a", config="c1"):
        return KnowledgeLanceShadowBuilder(
            repository,
            root=root,
            provider=FakeEmbeddingProvider(provider),
            config_revision=config,
        ).build(project_id, domain_id=domain_id)

    baseline = build("project:kb")
    alias = build("project:kb", domain_id="knowledge")
    variants = [
        baseline,
        build("project:kb", config="c2"),
        build("project:kb", provider="provider-b"),
        build("project:other"),
    ]

    assert alias["generation_id"] == baseline["generation_id"]
    assert alias["resumed"] is True
    generation_ids = {result["generation_id"] for result in variants}
    assert len(generation_ids) == len(variants)
    assert all(result["status"] == "shadow" for result in variants)
    assert all(
        (root / generation_id / "manifest.json").is_file() for generation_id in generation_ids
    )


def test_provider_endpoint_is_part_of_generation_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    root = tmp_path / "shadow"

    first = KnowledgeLanceShadowBuilder(
        repository,
        root=root,
        provider=FakeEmbeddingProvider(endpoint="http://localhost:11434"),
    ).build("project:kb")
    second = KnowledgeLanceShadowBuilder(
        repository,
        root=root,
        provider=FakeEmbeddingProvider(endpoint="http://localhost:11435"),
    ).build("project:kb")

    assert first["generation_id"] != second["generation_id"]


def test_citation_and_version_links_are_part_of_corpus_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    scopes = _seed(repository, "project:kb")
    repository.insert_citation(scopes["artifact_a"], "chunk-a", "project:kb")
    root = tmp_path / "shadow"

    first = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=FakeEmbeddingProvider()
    ).build("project:kb")
    with repository.connect() as connection:
        connection.execute(
            "UPDATE knowledge_citations SET chunk_id=? WHERE artifact_id=?",
            ("chunk-relinked", scopes["artifact_a"]),
        )
    second = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=FakeEmbeddingProvider()
    ).build("project:kb")

    assert first["generation_id"] != second["generation_id"]
    assert first["metrics"]["canonical_records"] == second["metrics"]["canonical_records"] == 4


def test_complete_source_revision_set_changes_identity_without_new_vector_rows(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    root = tmp_path / "shadow"

    first = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=FakeEmbeddingProvider()
    ).build("project:kb")
    _add_source_only(repository, "project:kb", "c")
    second = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=FakeEmbeddingProvider()
    ).build("project:kb")

    first_manifest = json.loads(
        str(repository.shadow_generation(first["generation_id"])["manifest_json"])
    )
    second_manifest = json.loads(
        str(repository.shadow_generation(second["generation_id"])["manifest_json"])
    )
    assert first["generation_id"] != second["generation_id"]
    assert first["metrics"]["canonical_records"] == second["metrics"]["canonical_records"] == 4
    assert (
        first_manifest["identity"]["corpus_sha256"] == second_manifest["identity"]["corpus_sha256"]
    )
    assert (
        first_manifest["identity"]["source_revision_set_sha256"]
        != second_manifest["identity"]["source_revision_set_sha256"]
    )


def test_failed_batch_resumes_without_reembedding_completed_rows(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:kb")
    root = tmp_path / "shadow"
    failing = FakeEmbeddingProvider(fail_on_call=2)

    first = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=failing, batch_size=1
    ).build("project:kb")
    assert first["status"] == "failed"
    assert first["failure_code"] == "knowledge_lance_provider_error"
    assert len(_rows(root, first["generation_id"])) == 1

    resumed_provider = FakeEmbeddingProvider()
    resumed = KnowledgeLanceShadowBuilder(
        repository, root=root, provider=resumed_provider, batch_size=1
    ).build("project:kb")
    assert resumed["generation_id"] == first["generation_id"]
    assert resumed["status"] == "shadow"
    assert resumed["metrics"]["existing_records"] == 1
    assert resumed["metrics"]["embedded_records"] == 3
    assert len(resumed_provider.calls) == 3
    assert "private provider response" not in json.dumps(resumed, ensure_ascii=False)


@pytest.mark.parametrize("change_kind", ["corpus", "source_revision"])
def test_canonical_change_during_embedding_records_failed_without_shadow_transition(
    tmp_path: Path, monkeypatch, change_kind: str
) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_LANCE_SHADOW", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    scopes = _seed(repository, "project:kb")
    root = tmp_path / "shadow"

    def change_canonical_snapshot() -> None:
        if change_kind == "source_revision":
            _add_source_only(repository, "project:kb", "concurrent")
            return
        changed_content = "changed while the shadow vectors were building"
        with repository.connect() as connection:
            connection.execute(
                "UPDATE knowledge_artifacts SET content=?, content_hash=? WHERE id=?",
                (
                    changed_content,
                    hashlib.sha256(changed_content.encode()).hexdigest(),
                    scopes["artifact_a"],
                ),
            )

    result = KnowledgeLanceShadowBuilder(
        repository,
        root=root,
        provider=FakeEmbeddingProvider(on_call=change_canonical_snapshot),
    ).build("project:kb")

    assert result["status"] == "failed"
    assert result["failure_code"] == "knowledge_lance_canonical_changed"
    generation = repository.shadow_generation(result["generation_id"])
    assert generation is not None
    assert generation["status"] == "failed"
    with repository.connect() as connection:
        rows = connection.execute(
            "SELECT detail_json FROM knowledge_audit_events"
            " WHERE object_type='knowledge_generation' AND object_id=? ORDER BY rowid",
            (result["generation_id"],),
        ).fetchall()
    details = [json.loads(str(row["detail_json"])) for row in rows]
    assert [(detail["from_status"], detail["to_status"]) for detail in details] == [
        (None, "building"),
        ("building", "failed"),
    ]
    assert details[-1]["failure_code"] == "knowledge_lance_canonical_changed"
    assert all(detail["to_status"] != "shadow" for detail in details)


def test_generation_and_audit_insert_roll_back_together(tmp_path: Path) -> None:
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    repository.init_schema()
    with repository.connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_generation_audit BEFORE INSERT ON knowledge_audit_events"
            " WHEN NEW.object_type='knowledge_generation'"
            " BEGIN SELECT RAISE(ABORT, 'generation audit rejected'); END"
        )
    manifest = {
        "schema_version": "knowledge-lance-shadow-v2",
        "canonical_record_count": 0,
        "identity": {
            "project_id": "project:kb",
            "domain_id": PROJECT_WIDE_DOMAIN_ID,
        },
    }

    with pytest.raises(sqlite3.IntegrityError, match="generation audit rejected"):
        repository.record_shadow_generation(
            "klsh_transaction_probe", "building", manifest, actor="test"
        )

    assert repository.shadow_generation("klsh_transaction_probe") is None
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_audit_events").fetchone()[0] == 0
