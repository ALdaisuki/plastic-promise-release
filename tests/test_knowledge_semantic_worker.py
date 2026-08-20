"""Async knowledge semantic worker tests (no live provider calls)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from plastic_promise.knowledge.blobs import MemoryBlobStore
from plastic_promise.knowledge.ingestion import IngestCoordinator
from plastic_promise.knowledge.repository import KnowledgeRepository
from plastic_promise.knowledge.semantic import SEMANTIC_SCHEMA_VERSION, SemanticBatch
from plastic_promise.knowledge.worker import KnowledgeSemanticWorker


class FakeProvider:
    def __init__(self) -> None:
        self.projects: list[str] = []

    def complete_batch(self, batch: SemanticBatch) -> dict:
        self.projects.append(batch.project_id)
        first = batch.chunks[0]
        return {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "units": [
                {
                    "kind": "fact",
                    "text": str(first["text"]),
                    "evidence_chunk_ids": [str(first["chunk_id"])],
                    "metadata": {},
                }
            ],
            "domains": [],
            "claims": [],
            "artifacts": [],
        }


def _seed(repository: KnowledgeRepository, project_id: str, *, suffix: str = "default") -> None:
    ingest = IngestCoordinator(repository, MemoryBlobStore(), actor="test")
    result = ingest.submit_source(
        project_id,
        f"# {project_id} {suffix}\n\nproject scoped evidence {suffix}".encode(),
        source_name=f"{project_id}-source-{suffix}",
        actor="test",
    )
    assert result.status == "done"


def test_worker_cycle_plans_and_processes_projects_independently(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:alpha")
    _seed(repository, "project:beta")
    provider = FakeProvider()
    worker = KnowledgeSemanticWorker(
        repository,
        provider=provider,
        batch_limit=10,
        partial_flush_seconds=0,
    )

    result = worker.run_cycle()

    assert result["project_count"] == 2
    assert result["planned"] == 2
    assert result["processed"] == 2
    assert sorted(provider.projects) == ["project:alpha", "project:beta"]
    assert repository.semantic_status("project:alpha")["done"] == 1
    assert repository.semantic_status("project:beta")["done"] == 1


def test_worker_gate_off_is_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "off")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    _seed(repository, "project:alpha")
    provider = FakeProvider()
    worker = KnowledgeSemanticWorker(repository, provider=provider)

    result = worker.run_cycle()

    assert result == {
        "gate": "off",
        "project_count": 0,
        "planned": 0,
        "processed": 0,
        "failed": 0,
        "reason": "semantic_disabled",
    }
    assert provider.projects == []


def test_worker_defers_partial_batch_until_flush_deadline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    database = tmp_path / "knowledge.db"
    repository = KnowledgeRepository(database)
    _seed(repository, "project:alpha")
    provider = FakeProvider()
    worker = KnowledgeSemanticWorker(
        repository,
        provider=provider,
        partial_flush_seconds=30,
    )

    fresh = worker.run_cycle()
    assert fresh["planned"] == 0
    assert provider.projects == []

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE knowledge_chunks SET created_at='2020-01-01T00:00:00.000Z'")
    flushed = worker.run_cycle()
    assert flushed["planned"] == 1
    assert flushed["processed"] == 1


def test_worker_eventually_plans_chunks_beyond_one_scan_page(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    for index in range(5):
        _seed(repository, "project:alpha", suffix=str(index))
    provider = FakeProvider()
    worker = KnowledgeSemanticWorker(
        repository,
        provider=provider,
        plan_chunk_limit=2,
        batch_limit=10,
        partial_flush_seconds=0,
    )

    results = [worker.run_cycle() for _ in range(3)]

    assert sum(int(result["planned"]) for result in results) == 5
    assert repository.semantic_status("project:alpha")["done"] == 5
    assert provider.projects == ["project:alpha"] * 5


def test_worker_eventually_plans_projects_beyond_one_project_page(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    project_ids = [f"project:{index}" for index in range(5)]
    for project_id in project_ids:
        _seed(repository, project_id)
    provider = FakeProvider()
    worker = KnowledgeSemanticWorker(
        repository,
        provider=provider,
        project_limit=2,
        batch_limit=10,
        partial_flush_seconds=0,
    )

    results = [worker.run_cycle() for _ in range(3)]

    assert sum(int(result["planned"]) for result in results) == 5
    assert all(repository.semantic_status(project_id)["done"] == 1 for project_id in project_ids)
    assert sorted(provider.projects) == project_ids


@pytest.mark.asyncio
async def test_worker_loop_stops_without_waiting_full_interval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PP_KNOWLEDGE_SEMANTIC", "shadow")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    worker = KnowledgeSemanticWorker(
        repository,
        provider=FakeProvider(),
        interval_seconds=3600,
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.serve(stop))
    await asyncio.sleep(0.05)
    stop.set()
    worker.notify()
    await asyncio.wait_for(task, timeout=1)

    snapshot = worker.snapshot()
    assert snapshot["gate"] == "shadow"
    assert snapshot["running"] is False
