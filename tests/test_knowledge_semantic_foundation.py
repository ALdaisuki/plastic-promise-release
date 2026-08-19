"""Knowledge semantic compilation foundation tests (Slice 2a).

Covers batch planning, untrusted-response validation, durable job recovery,
domain candidate activation, and Wiki artifact promotion gates.  The cloud
provider is always a deterministic fake; live credentials are never needed.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

import pytest

from plastic_promise.knowledge.artifacts import promote_eligible_artifacts, review_artifact
from plastic_promise.knowledge.domains import (
    evaluate_domain_activations,
    merge_domains,
    retire_domain,
    split_domain,
)
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.knowledge.semantic import (
    SEMANTIC_SCHEMA_VERSION,
    SemanticBatch,
    SemanticResponseValidator,
    SemanticValidationError,
    build_semantic_batches,
)

if TYPE_CHECKING:
    from pathlib import Path


MARKDOWN_ONE = """\
# 部署手册

## SSH 隧道

MacBook 通过 SSH LocalForward 访问服务器 MCP，9020 端口不暴露公网。

## 备份

每日使用 SQLite Online Backup API 创建备份，quick_check 必须为 ok。
"""

MARKDOWN_TWO = """\
# 理财笔记

## 预算

每月按收入比例分配预算，保留紧急备用金。

## 投资

指数基金定投适合长期持有，波动时可分批买入。
"""


def _coordinator(
    tmp_path: Path, db_name: str = "plastic_knowledge.db"
) -> tuple[KnowledgeRepository, object]:
    from plastic_promise.knowledge.blobs import MemoryBlobStore
    from plastic_promise.knowledge.ingestion import IngestCoordinator

    repository = KnowledgeRepository(tmp_path / db_name)
    ingest = IngestCoordinator(repository, MemoryBlobStore(), actor="test")
    return repository, ingest


def _ingest(ingest: object, *, project: str, name: str, space: str, content: str) -> None:
    ingest.submit_source(  # type: ignore[attr-defined]
        project,
        content.encode("utf-8"),
        source_name=name,
        space_name=space,
        actor="test",
    )


def _valid_payload(batch: SemanticBatch) -> dict:
    chunks = list(batch.chunks)
    first = chunks[0]
    second = chunks[1] if len(chunks) > 1 else chunks[0]
    return {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "units": [
            {
                "kind": "fact",
                "text": str(first["text"])[:80],
                "evidence_chunk_ids": [str(first["chunk_id"])],
                "metadata": {"language": "zh"},
            },
            {
                "kind": "summary",
                "text": "部署手册 SSH 隧道 备份 说明",
                "evidence_chunk_ids": [str(first["chunk_id"]), str(second["chunk_id"])],
                "metadata": {},
            },
        ],
        "domains": [{"name": "开发运维", "description": "服务器部署、备份与恢复"}],
        "claims": [
            {
                "text": "9020 端口不暴露公网",
                "stance": "supports",
                "evidence_chunk_ids": [str(first["chunk_id"])],
            }
        ],
        "artifacts": [
            {
                "kind": "source_summary",
                "title": "部署手册摘要",
                "content": "本文档覆盖 SSH 隧道与每日备份。",
                "evidence_chunk_ids": [str(first["chunk_id"])],
            }
        ],
    }


class FakeSemanticProvider:
    def __init__(self, payload_factory=None) -> None:
        self._factory = payload_factory or _valid_payload
        self.calls = 0

    def complete_batch(self, batch: SemanticBatch) -> dict:
        self.calls += 1
        return self._factory(batch)


# -- batch planning ----------------------------------------------------------


def test_batch_builder_groups_by_space_and_version(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path)
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)
    _ingest(ingest, project="project:kb", name="finance", space="money", content=MARKDOWN_TWO)
    rows = repository.list_active_chunks_for_semantic("project:kb")
    batches = build_semantic_batches(rows, batch_size=20)
    assert len(batches) == 2
    spaces = {b.space_id for b in batches}
    assert len(spaces) == 2
    assert all(len(b.chunks) >= 1 for b in batches)
    # deterministic identity
    again = build_semantic_batches(rows, batch_size=20)
    assert [b.batch_sha256 for b in batches] == [b.batch_sha256 for b in again]


def test_batch_builder_splits_at_batch_size(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path)
    big = MARKDOWN_ONE + "\n\n" + "\n\n".join(f"## 小节 {i}\n第 {i} 段内容。" for i in range(30))
    _ingest(ingest, project="project:kb", name="big", space="ops", content=big)
    rows = repository.list_active_chunks_for_semantic("project:kb")
    batches = build_semantic_batches(rows, batch_size=20)
    assert len(batches) >= 2
    assert all(len(b.chunks) <= 20 for b in batches)


# -- validation --------------------------------------------------------------


def _single_batch(tmp_path: Path) -> SemanticBatch:
    repository, ingest = _coordinator(tmp_path, db_name="validation.db")
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)
    rows = repository.list_active_chunks_for_semantic("project:kb")
    return build_semantic_batches(rows)[0]


def test_validator_accepts_grounded_response(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    result = SemanticResponseValidator().validate(_valid_payload(batch), batch)
    assert len(result["units"]) == 2
    assert result["domains"][0]["name"] == "开发运维"
    assert result["claims"][0]["stance"] == "supports"
    assert result["artifacts"][0]["kind"] == "source_summary"


def test_validator_rejects_unknown_chunk_id(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    payload = _valid_payload(batch)
    payload["units"][0]["evidence_chunk_ids"] = ["chunk_not_in_batch"]
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_unknown_chunk_id"


def test_validator_rejects_unsupported_stance(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    payload = _valid_payload(batch)
    payload["claims"][0]["stance"] = "maybe"
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_claim_stance"


def test_validator_rejects_ungrounded_verbatim_unit(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    payload = _valid_payload(batch)
    payload["units"][0] = {
        "kind": "fact",
        "text": "完全不存在的句子内容。",
        "evidence_chunk_ids": [str(batch.chunks[0]["chunk_id"])],
        "metadata": {},
    }
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_ungrounded_text"


def test_validator_rejects_secret_shape(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    payload = _valid_payload(batch)
    payload["domains"] = [{"name": "泄露密钥", "description": "api_key=sk-abcdef0123456789abcdef"}]
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_secret_shape"


def test_validator_rejects_bad_schema_and_unknown_keys(tmp_path: Path) -> None:
    batch = _single_batch(tmp_path)
    payload = _valid_payload(batch)
    payload["schema_version"] = "wrong"
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_schema_version"
    payload["schema_version"] = SEMANTIC_SCHEMA_VERSION
    payload["extra"] = True
    with pytest.raises(SemanticValidationError) as exc:
        SemanticResponseValidator().validate(payload, batch)
    assert exc.value.code == "semantic_unknown_keys"


# -- durable coordinator -----------------------------------------------------


def test_coordinator_plan_is_idempotent_and_persists(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="coordinator.db")
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    coordinator = KnowledgeSemanticCoordinator(repository, provider=FakeSemanticProvider())
    first = coordinator.plan("project:kb")
    second = coordinator.plan("project:kb")
    assert first["created"] >= 1
    assert second["created"] == 0
    summary = coordinator.process_next(limit=5)
    assert summary["processed"] == 1
    assert summary["units"] >= 1
    assert summary["domains"] >= 1
    assert summary["claims"] >= 1
    assert summary["artifacts"] >= 1
    status = coordinator.status("project:kb")
    assert status["done"] == 1
    assert repository.semantic_units_for_project("project:kb")


def test_coordinator_project_filter_does_not_claim_another_project(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="project-filter.db")
    _ingest(ingest, project="project:alpha", name="alpha", space="ops", content=MARKDOWN_ONE)
    _ingest(ingest, project="project:beta", name="beta", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    provider = FakeSemanticProvider()
    coordinator = KnowledgeSemanticCoordinator(repository, provider=provider)
    coordinator.plan("project:alpha")
    coordinator.plan("project:beta")

    result = coordinator.process_next(project_id="project:beta", limit=5)

    assert result["processed"] == 1
    assert result["projects"] == ["project:beta"]
    assert repository.semantic_status("project:alpha")["pending"] == 1
    assert repository.semantic_status("project:beta")["done"] == 1


def test_coordinator_runs_provider_calls_concurrently(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="concurrency.db")
    _ingest(ingest, project="project:alpha", name="alpha", space="ops", content=MARKDOWN_ONE)
    _ingest(ingest, project="project:beta", name="beta", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    class ConcurrentProvider:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def complete_batch(self, batch: SemanticBatch) -> dict:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return _valid_payload(batch)

    provider = ConcurrentProvider()
    coordinator = KnowledgeSemanticCoordinator(
        repository,
        provider=provider,
        max_concurrency=2,
    )
    coordinator.plan("project:alpha")
    coordinator.plan("project:beta")

    result = coordinator.process_next(limit=2)

    assert result["processed"] == 2
    assert provider.maximum == 2


def test_stale_semantic_job_owner_cannot_complete_reclaimed_job(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="lease-fencing.db")
    _ingest(ingest, project="project:kb", name="one", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    coordinator = KnowledgeSemanticCoordinator(repository, provider=None, owner="planner")
    coordinator.plan("project:kb")
    first = repository.claim_ready_semantic_jobs("worker-old", lease_seconds=-1)
    assert len(first) == 1
    assert repository.reconcile_semantic_jobs() == 1
    second = repository.claim_ready_semantic_jobs("worker-new", lease_seconds=300)
    assert len(second) == 1

    completed = repository.complete_semantic_job(str(first[0]["id"]), owner="worker-old")

    assert completed is False
    current = repository.semantic_job_by_batch("project:kb", str(first[0]["batch_sha256"]))
    assert current is not None
    assert current["status"] == "building"
    assert current["lease_owner"] == "worker-new"


def test_coordinator_retries_when_derived_promotion_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC_BACKOFF_BASE_SECONDS", "0")
    repository, ingest = _coordinator(tmp_path, db_name="promotion-retry.db")
    _ingest(ingest, project="project:kb", name="one", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    coordinator = KnowledgeSemanticCoordinator(repository, provider=FakeSemanticProvider())
    coordinator.plan("project:kb")
    monkeypatch.setattr(
        "plastic_promise.knowledge.artifacts.promote_eligible_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("promotion unavailable")),
    )

    result = coordinator.process_next(limit=1)

    assert result["processed"] == 0
    assert result["failed"] == 1
    job = repository.claim_ready_semantic_jobs("retry-worker", limit=1)[0]
    assert job["error_code"] == "semantic_promotion_error"


def test_coordinator_renews_lease_during_slow_provider_call(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="lease-heartbeat.db")
    _ingest(ingest, project="project:kb", name="one", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    class SlowProvider:
        def complete_batch(self, batch: SemanticBatch) -> dict:
            time.sleep(1.2)
            return _valid_payload(batch)

    coordinator = KnowledgeSemanticCoordinator(
        repository,
        provider=SlowProvider(),
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
    )
    coordinator.plan("project:kb")

    result = coordinator.process_next(limit=1)

    assert result["processed"] == 1
    assert result["stale"] == 0
    assert repository.semantic_status("project:kb")["done"] == 1


def test_coordinator_retries_then_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC_BACKOFF_BASE_SECONDS", "0")
    repository, ingest = _coordinator(tmp_path, db_name="retry.db")
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    class FailingProvider:
        def complete_batch(self, batch: SemanticBatch) -> dict:
            raise RuntimeError("provider boom")

    coordinator = KnowledgeSemanticCoordinator(
        repository, provider=FailingProvider(), lease_seconds=30
    )
    coordinator.plan("project:kb")
    for _ in range(5):
        result = coordinator.process_next(limit=1)
        assert result["processed"] == 0
    status = coordinator.status("project:kb")
    assert status["failed"] == 1


def test_coordinator_reclaims_expired_lease(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="lease.db")
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)

    from plastic_promise.knowledge.semantic import KnowledgeSemanticCoordinator

    coordinator = KnowledgeSemanticCoordinator(repository, provider=None, lease_seconds=-1)
    coordinator.plan("project:kb")
    jobs = repository.claim_ready_semantic_jobs("worker-a", lease_seconds=-1)
    assert jobs
    reclaimed = repository.reconcile_semantic_jobs()
    assert reclaimed == 1
    status = coordinator.status("project:kb")
    assert status["pending"] == 1


def test_provider_factory_degrades_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "on")
    monkeypatch.delenv("PP_MEMORY_CHUNK_ENRICHMENT_API_KEY", raising=False)
    monkeypatch.delenv("PP_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from plastic_promise.knowledge.semantic import create_knowledge_semantic_provider

    assert create_knowledge_semantic_provider() is None


# -- domain activation and lineage -------------------------------------------


def test_domain_activation_thresholds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_AUTO_DOMAINS", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_DOMAIN_ACTIVATION_MIN_SOURCES", "2")
    repository, ingest = _coordinator(tmp_path, db_name="domains.db")
    _ingest(ingest, project="project:kb", name="a", space="s1", content=MARKDOWN_ONE)
    _ingest(ingest, project="project:kb", name="b", space="s2", content=MARKDOWN_TWO)

    # candidate from one source only -> not activated
    repository.upsert_domain_candidate(
        project_id="project:kb",
        name="部署运维",
        description="",
        source_id="a",
        space_id="s1",
        evidence={"source_ids": "a", "space_ids": "s1"},
    )
    result = evaluate_domain_activations(repository, "project:kb")
    assert result["activated"] == 0

    # same candidate seen from a second source/space -> activated
    repository.upsert_domain_candidate(
        project_id="project:kb",
        name="部署运维",
        description="",
        source_id="b",
        space_id="s2",
        evidence={"source_ids": "b", "space_ids": "s2"},
    )
    result = evaluate_domain_activations(repository, "project:kb")
    assert result["activated"] == 1
    domains = repository.list_domains("project:kb")
    assert any(d["kind"] == "active" and d["name"] == "部署运维" for d in domains)


def test_domain_merge_split_retire_lineage(tmp_path: Path) -> None:
    repository, ingest = _coordinator(tmp_path, db_name="lineage.db")
    _ingest(ingest, project="project:kb", name="a", space="s1", content=MARKDOWN_ONE)
    _ingest(ingest, project="project:kb", name="b", space="s2", content=MARKDOWN_TWO)
    left = repository.upsert_domain_candidate(
        project_id="project:kb",
        name="运维左",
        description="",
        source_id="a",
        space_id="s1",
        evidence={"source_ids": "a", "space_ids": "s1"},
    )
    right = repository.upsert_domain_candidate(
        project_id="project:kb",
        name="运维右",
        description="",
        source_id="b",
        space_id="s2",
        evidence={"source_ids": "b", "space_ids": "s2"},
    )
    merged = merge_domains(
        repository, "project:kb", target_id=left["id"], source_id=right["id"], reason="近义"
    )
    assert merged["merged"] is True
    left_after = repository._domain_by_id(left["id"])
    assert "运维右" in json.loads(left_after["aliases_json"])

    split = split_domain(
        repository,
        "project:kb",
        domain_id=left["id"],
        children=[{"name": "部署", "description": ""}, {"name": "备份", "description": ""}],
        reason="拆分为两个社区",
    )
    assert split["split"] is True
    assert repository._domain_by_id(left["id"])["kind"] == "retired"

    child_id = split["children"][0]
    retired = retire_domain(repository, "project:kb", domain_id=child_id, reason="无使用")
    assert retired["retired"] is True
    assert repository._domain_by_id(child_id)["kind"] == "retired"


# -- artifact promotion ------------------------------------------------------


def test_artifact_promotion_low_risk_active_high_risk_review(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_WIKI", "on")
    monkeypatch.setenv("PP_KNOWLEDGE_ARTIFACT_MIN_CITATION_COVERAGE", "0.8")
    repository, ingest = _coordinator(tmp_path, db_name="artifacts.db")
    _ingest(ingest, project="project:kb", name="deploy", space="ops", content=MARKDOWN_ONE)
    chunk_id = repository.list_active_chunks_for_semantic("project:kb")[0]["chunk_id"]

    low = repository.upsert_artifact(
        {
            "project_id": "project:kb",
            "kind": "source_summary",
            "title": "部署摘要",
            "content": "覆盖 SSH 与备份的摘要。",
            "content_hash": "h-low",
            "risk_tier": "low",
            "source_ids": ["s1"],
        }
    )
    repository.insert_citation(low, chunk_id, "project:kb")
    high = repository.upsert_artifact(
        {
            "project_id": "project:kb",
            "kind": "source_summary",
            "title": "安全操作摘要",
            "content": "涉及生产操作的安全说明。",
            "content_hash": "h-high",
            "risk_tier": "high",
            "source_ids": ["s1"],
        }
    )
    repository.insert_citation(high, chunk_id, "project:kb")

    result = promote_eligible_artifacts(repository, "project:kb")
    assert result["promoted"] == 1
    assert result["pending_review"] == 1
    assert repository.artifact_by_id(low)["status"] == "active"
    assert repository.artifact_by_id(high)["status"] == "pending_review"

    reviewed = review_artifact(repository, high, decision="approve")
    assert reviewed["status"] == "active"
    assert repository.artifact_by_id(high)["status"] == "active"
