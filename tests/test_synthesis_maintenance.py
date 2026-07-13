"""Invalidation and derived-index repair tests for governed synthesis."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
from plastic_promise.core.lancedb_store import LanceDBStore
from plastic_promise.core.memory_index import build_index_material, metadata_with_index_material
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.core.synthesis_maintenance import (
    enqueue_synthesis_index_job,
    replay_synthesis_index_jobs,
    scan_synthesis_integrity,
)


class _FakeEmbedder:
    model_name = "fake-maintenance"

    def __init__(self):
        self.inputs = []
        self.after_embed = None

    def embed(self, text):
        assert text
        self.inputs.append(text)
        if self.after_embed is not None:
            self.after_embed()
        return [0.25] * 1024


class _FakeLanceDB:
    def __init__(self):
        self.inserted = []
        self.deleted = []
        self.rows = {}
        self.fail_insert = False
        self.fail_delete = False
        self.after_insert = None

    def insert_checked(self, **kwargs):
        if self.fail_insert:
            raise RuntimeError("backend down")
        if kwargs["memory_id"] in self.rows:
            return
        self.inserted.append(kwargs)
        self.rows[kwargs["memory_id"]] = kwargs
        if self.after_insert is not None:
            self.after_insert()

    def replace_checked(self, **kwargs):
        if self.fail_insert:
            raise RuntimeError("backend down")
        if self.rows.get(kwargs["memory_id"]) == kwargs:
            return
        self.inserted.append(kwargs)
        self.rows[kwargs["memory_id"]] = kwargs
        if self.after_insert is not None:
            self.after_insert()

    def delete_checked(self, memory_id):
        if self.fail_delete:
            raise RuntimeError("backend down")
        self.deleted.append(memory_id)
        self.rows.pop(memory_id, None)


def _version(conn) -> int:
    return int(conn.execute("SELECT version FROM memory_version").fetchone()[0])


@pytest.fixture
def synthesis_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    storage = _SQLiteStorage(str(tmp_path / "synthesis-maintenance.db"))
    for memory in (
        {
            "id": "source-a",
            "content": "Alpha evidence independently supports the conclusion. " * 4,
            "memory_type": "experience",
            "source": "user",
            "source_class": "user_fact",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///alpha.md",
            "origin_ref": "alpha",
            "origin_hash": "origin-alpha",
            "metadata_json": {"status": "current"},
        },
        {
            "id": "source-b",
            "content": "Beta evidence confirms the conclusion from another source. " * 4,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///beta.md",
            "origin_ref": "beta",
            "origin_hash": "origin-beta",
            "metadata_json": {"quality_status": "verified"},
        },
    ):
        storage.upsert(memory["id"], memory)

    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    engine._loaded_memory_version = _version(storage._conn)
    engine.canonical_sync_ok = True
    engine._embedder = _FakeEmbedder()
    engine._ldb = _FakeLanceDB()
    engine.ensure_heavy_init = lambda: None
    yield engine
    storage._conn.close()


def _create_and_verify(engine, *, key="topic:maintenance"):
    store = SynthesisStore(engine._sqlite._conn, engine=engine)
    draft = store.create_draft(
        "The independent evidence supports one stable reusable conclusion.",
        ["source-a", "source-b"],
        synthesis_key=key,
        validity_scope="project:test",
        project_id="project:test",
        visibility="project",
        actor="codex",
        call_id="call-create",
    )
    return store.verify(draft.memory_id, "reviewer", "call-verify", 1)


def _jobs(conn, memory_id):
    rows = conn.execute(
        "SELECT outbox_id, status, payload_json, attempt_count, next_attempt_at "
        "FROM store_outbox WHERE tool_name = 'synthesis_index' AND payload_json LIKE ? "
        "ORDER BY created_at, outbox_id",
        (f'%"memory_id":"{memory_id}"%',),
    ).fetchall()
    return [
        {
            "outbox_id": row[0],
            "status": row[1],
            "payload": json.loads(row[2]),
            "attempt_count": row[3],
            "next_attempt_at": row[4],
        }
        for row in rows
    ]


def _store_ordinary_index_candidate(engine, memory_id="ordinary-index"):
    material = build_index_material(
        {"content": "Canonical ordinary memory for derived indexing."},
        policy="legacy",
        model_name=engine._embedder.model_name,
    )
    engine._sqlite.upsert(
        memory_id,
        {
            "id": memory_id,
            "content": "Canonical ordinary memory for derived indexing.",
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "project",
            "tier": "L1",
            "category": "fact",
            "scope": "global",
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
            "metadata_json": metadata_with_index_material({}, material),
        },
    )
    return material


def test_ordinary_memory_index_replay_is_checked_bound_and_idempotent(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        embedding_hash=material.embedding_hash,
        call_id="call-ordinary-index",
    )

    first = replay_memory_index_jobs(synthesis_engine)
    second = replay_memory_index_jobs(synthesis_engine)
    row = conn.execute(
        "SELECT status, payload_json FROM store_outbox WHERE outbox_id = ?",
        (job_id,),
    ).fetchone()

    assert first.succeeded == 1
    assert second.selected == 0
    assert row[0] == "done"
    assert json.loads(row[1])["material_revision"] == material.embedding_hash
    assert synthesis_engine._ldb.rows["ordinary-index"]["text"] == material.search_text
    assert (
        len(
            [
                item
                for item in synthesis_engine._ldb.inserted
                if item["memory_id"] == "ordinary-index"
            ]
        )
        == 1
    )


def test_v3_upsert_replays_after_unrelated_memory_version_change(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-version-drift",
    )
    job_version = json.loads(
        conn.execute(
            "SELECT payload_json FROM store_outbox WHERE outbox_id = ?", (job_id,)
        ).fetchone()[0]
    )["memory_version"]
    synthesis_engine._sqlite.upsert(
        "unrelated-version-change",
        {
            "id": "unrelated-version-change",
            "content": "An unrelated ordinary memory changed the global version.",
            "memory_type": "experience",
            "source": "agent",
            "project_id": "project:test",
        },
    )

    assert _version(conn) > job_version
    report = replay_memory_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert synthesis_engine._ldb.rows["ordinary-index"]["text"] == material.search_text


def test_ordinary_memory_index_replay_drops_invalid_material(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        embedding_hash=material.embedding_hash,
        call_id="call-ordinary-stale",
    )
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": "STALE MATERIAL",
    }
    conn.execute(
        "UPDATE memories SET embedding_hash = 'changed-material' WHERE id = 'ordinary-index'"
    )
    conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
    conn.commit()

    report = replay_memory_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_ordinary_memory_index_replay_keeps_vector_for_stale_material_revision(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        embedding_hash=material.embedding_hash,
        call_id="call-ordinary-old-revision",
    )
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": "STALE MATERIAL",
    }
    current = synthesis_engine._sqlite.get("ordinary-index")
    replacement = build_index_material(
        {"content": "A newer canonical material revision."},
        policy="legacy",
        model_name=synthesis_engine._embedder.model_name,
    )
    current.update(
        {
            "content": "A newer canonical material revision.",
            "embedding_text": replacement.vector_text,
            "embedding_hash": replacement.embedding_hash,
            "search_text": replacement.search_text,
            "metadata_json": metadata_with_index_material(
                current.get("metadata_json"),
                replacement,
            ),
        }
    )
    synthesis_engine._sqlite.upsert("ordinary-index", current)

    report = replay_memory_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert synthesis_engine._ldb.rows["ordinary-index"] == {
        "memory_id": "ordinary-index",
        "text": "STALE MATERIAL",
    }
    assert synthesis_engine._ldb.deleted == []


def test_ordinary_memory_index_replay_deletes_vector_for_deleted_memory(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        embedding_hash=material.embedding_hash,
        call_id="call-ordinary-deleted",
    )
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": "ORPHAN MATERIAL",
    }
    conn.execute("DELETE FROM memories WHERE id = 'ordinary-index'")
    conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
    conn.commit()

    report = replay_memory_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_checked_delete_is_bound_to_blocked_state_and_exact_material(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_delete

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    tombstone = conn.execute(
        "SELECT metadata_json FROM memories WHERE id = 'ordinary-index'"
    ).fetchone()
    metadata = json.loads(tombstone[0])
    metadata["quality_status"] = "wrong"
    conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 'ordinary-index'",
        (json.dumps(metadata),),
    )
    conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
    conn.commit()
    job_id = enqueue_memory_index_delete(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-blocked-delete",
    )
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": material.search_text,
    }

    report = replay_memory_index_jobs(synthesis_engine)
    status = conn.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()[0]

    assert report.succeeded == 1
    assert status == "done"
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_checked_delete_survives_unrelated_global_version_drift(synthesis_engine) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_delete

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    blocked = synthesis_engine._sqlite.get("ordinary-index")
    blocked_metadata = dict(blocked["metadata_json"])
    blocked_metadata["quality_status"] = "wrong"
    blocked["metadata_json"] = blocked_metadata
    synthesis_engine._sqlite.upsert("ordinary-index", blocked)
    job_id = enqueue_memory_index_delete(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-delete-before-unrelated-write",
    )
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": material.search_text,
    }

    conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
    conn.commit()

    report = replay_memory_index_jobs(synthesis_engine)
    status = conn.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()[0]

    assert report.succeeded == 1
    assert status == "done"
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_checked_delete_removes_blocked_vector_after_project_and_material_drift(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_delete

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    blocked = synthesis_engine._sqlite.get("ordinary-index")
    blocked_metadata = dict(blocked["metadata_json"])
    blocked_metadata["quality_status"] = "wrong"
    blocked["metadata_json"] = blocked_metadata
    synthesis_engine._sqlite.upsert("ordinary-index", blocked)
    job_id = enqueue_memory_index_delete(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-delete-before-blocked-drift",
    )

    replacement = build_index_material(
        {"content": "A blocked source whose index identity drifted."},
        policy="legacy",
        model_name=synthesis_engine._embedder.model_name,
    )
    drifted = synthesis_engine._sqlite.get("ordinary-index")
    drifted.update(
        {
            "content": "A blocked source whose index identity drifted.",
            "project_id": "project:drifted",
            "embedding_text": replacement.vector_text,
            "embedding_hash": replacement.embedding_hash,
            "search_text": replacement.search_text,
            "metadata_json": metadata_with_index_material(
                drifted["metadata_json"],
                replacement,
            ),
        }
    )
    synthesis_engine._sqlite.upsert("ordinary-index", drifted)
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": material.search_text,
    }

    report = replay_memory_index_jobs(synthesis_engine)
    status = conn.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()[0]

    assert report.succeeded == 1
    assert status == "done"
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_old_delete_cannot_remove_newer_corrected_vector(synthesis_engine) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_delete

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    blocked = synthesis_engine._sqlite.get("ordinary-index")
    blocked_metadata = dict(blocked["metadata_json"])
    blocked_metadata["quality_status"] = "wrong"
    blocked["metadata_json"] = blocked_metadata
    synthesis_engine._sqlite.upsert("ordinary-index", blocked)
    job_id = enqueue_memory_index_delete(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-old-delete",
    )
    replacement = build_index_material(
        {"content": "A corrected ordinary memory has newer index material."},
        policy="legacy",
        model_name=synthesis_engine._embedder.model_name,
    )
    corrected = synthesis_engine._sqlite.get("ordinary-index")
    corrected_metadata = metadata_with_index_material(corrected.get("metadata_json"), replacement)
    corrected_metadata["quality_status"] = "current"
    corrected.update(
        {
            "content": "A corrected ordinary memory has newer index material.",
            "embedding_text": replacement.vector_text,
            "embedding_hash": replacement.embedding_hash,
            "search_text": replacement.search_text,
            "metadata_json": corrected_metadata,
        }
    )
    synthesis_engine._sqlite.upsert("ordinary-index", corrected)
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": replacement.search_text,
    }

    report = replay_memory_index_jobs(synthesis_engine)
    status = conn.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()[0]

    assert report.succeeded == 1
    assert status == "done"
    assert synthesis_engine._ldb.rows["ordinary-index"] == {
        "memory_id": "ordinary-index",
        "text": replacement.search_text,
    }
    assert synthesis_engine._ldb.deleted == []


def test_old_upsert_cannot_resurrect_current_tombstone(synthesis_engine) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-old-upsert",
    )
    tombstone = synthesis_engine._sqlite.get("ordinary-index")
    tombstone_metadata = dict(tombstone["metadata_json"])
    tombstone_metadata["quality_status"] = "deprecated"
    tombstone["metadata_json"] = tombstone_metadata
    synthesis_engine._sqlite.upsert("ordinary-index", tombstone)
    synthesis_engine._ldb.rows["ordinary-index"] = {
        "memory_id": "ordinary-index",
        "text": material.search_text,
    }

    report = replay_memory_index_jobs(synthesis_engine)
    status = conn.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()[0]

    assert report.succeeded == 1
    assert status == "done"
    assert "ordinary-index" not in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.inserted == []
    assert synthesis_engine._ldb.deleted == ["ordinary-index"]


def test_historical_memory_index_v2_upsert_still_replays(synthesis_engine) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-legacy-v2",
    )
    legacy_payload = {
        "action": "upsert",
        "embedding_hash": material.embedding_hash,
        "material_revision": material.embedding_hash,
        "memory_id": "ordinary-index",
        "memory_version": _version(conn),
    }
    conn.execute(
        "UPDATE store_outbox SET metadata_json = ?, payload_json = ? WHERE outbox_id = ?",
        (
            json.dumps({"job_schema": "memory-index/v2"}),
            json.dumps(legacy_payload),
            job_id,
        ),
    )
    conn.commit()

    report = replay_memory_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert synthesis_engine._ldb.rows["ordinary-index"]["text"] == material.search_text


@pytest.mark.parametrize(
    "mutation",
    ["missing_project_id", "unexpected_field", "numeric_hash", "boolean_project_id"],
)
def test_memory_index_v3_rejects_incomplete_payload_without_side_effect(
    synthesis_engine,
    mutation,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id=f"call-v3-invalid-{mutation}",
    )
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM store_outbox WHERE outbox_id = ?", (job_id,)
        ).fetchone()[0]
    )
    if mutation == "missing_project_id":
        payload.pop("project_id")
    elif mutation == "unexpected_field":
        payload["unexpected"] = True
    elif mutation == "numeric_hash":
        payload["expected_embedding_hash"] = 17
    else:
        payload["project_id"] = True
    conn.execute(
        "UPDATE store_outbox SET payload_json = ? WHERE outbox_id = ?",
        (json.dumps(payload), job_id),
    )
    conn.commit()
    before_rows = dict(synthesis_engine._ldb.rows)

    report = replay_memory_index_jobs(synthesis_engine)
    status, error_class = conn.execute(
        "SELECT status, error_class FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()

    assert report.failed == 1
    assert status == "pending"
    assert error_class == "ValueError"
    assert synthesis_engine._ldb.rows == before_rows


def test_failure_marker_requires_test_mode_at_replay_start(
    synthesis_engine, monkeypatch, tmp_path
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs

    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(tmp_path / "marker.json"))
    monkeypatch.delenv("PP_TEST_MODE", raising=False)

    with pytest.raises(RuntimeError, match="index_failure_marker_requires_test_mode"):
        replay_memory_index_jobs(synthesis_engine)


def test_failure_marker_requires_test_mode_at_engine_startup(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "test-index-failure/v1",
                "action": "upsert",
                "memory_id": "ordinary-index",
                "remaining": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(marker))
    monkeypatch.delenv("PP_TEST_MODE", raising=False)

    with pytest.raises(RuntimeError, match="index_failure_marker_requires_test_mode"):
        ContextEngine(use_sqlite=False)


def test_failure_marker_is_atomically_consumed_once(monkeypatch, tmp_path) -> None:
    from plastic_promise.core.synthesis_maintenance import (
        InjectedIndexFailure,
        consume_test_index_failure,
    )

    marker = tmp_path / "index-failure.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "test-index-failure/v1",
                "action": "upsert",
                "memory_id": "ordinary-index",
                "remaining": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_TEST_MODE", "1")
    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(marker))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            barrier.wait()
            consume_test_index_failure(action="upsert", memory_id="ordinary-index")
        except InjectedIndexFailure:
            outcomes.append("injected")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        else:
            outcomes.append("not_injected")

    workers = [threading.Thread(target=consume) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert sorted(outcomes) == ["injected", "not_injected"]
    assert not marker.exists()


def test_failure_marker_blocks_checked_upsert_before_backend_side_effect(
    synthesis_engine,
    monkeypatch,
    tmp_path,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    material = _store_ordinary_index_candidate(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_memory_index_upsert(
        conn,
        memory_id="ordinary-index",
        project_id="project:test",
        expected_embedding_hash=material.embedding_hash,
        call_id="call-marker-upsert",
    )
    marker = tmp_path / "index-failure.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "test-index-failure/v1",
                "action": "upsert",
                "memory_id": "ordinary-index",
                "remaining": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PP_TEST_MODE", "1")
    monkeypatch.setenv("PP_TEST_INDEX_FAIL_MARKER", str(marker))

    report = replay_memory_index_jobs(synthesis_engine)
    status, error_class = conn.execute(
        "SELECT status, error_class FROM store_outbox WHERE outbox_id = ?", (job_id,)
    ).fetchone()

    assert report.failed == 1
    assert status == "pending"
    assert error_class == "InjectedIndexFailure"
    assert synthesis_engine._ldb.rows == {}
    assert not marker.exists()


def test_expired_processing_synthesis_job_is_reclaimed_but_fresh_lease_is_not(
    synthesis_engine,
) -> None:
    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    expired_id = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="upsert",
        call_id="call-expired-lease",
    )
    conn.execute(
        "UPDATE store_outbox SET status = 'processing', updated_at = ? WHERE outbox_id = ?",
        ("2000-01-01T00:00:00Z", expired_id),
    )
    fresh_id = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="delete",
        call_id="call-fresh-lease",
    )
    conn.execute(
        "UPDATE store_outbox SET status = 'processing', updated_at = ? WHERE outbox_id = ?",
        (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), fresh_id),
    )
    conn.commit()

    report = replay_synthesis_index_jobs(synthesis_engine, lease_seconds=60)

    states = dict(
        conn.execute(
            "SELECT outbox_id, status FROM store_outbox WHERE outbox_id IN (?, ?)",
            (expired_id, fresh_id),
        ).fetchall()
    )
    assert report.claimed == 1
    assert states[expired_id] == "done"
    assert states[fresh_id] == "processing"


def test_expired_processing_lease_is_claimed_by_only_one_worker(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import _claim_job

    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="upsert",
        call_id="call-concurrent-lease",
    )
    conn.execute(
        "UPDATE store_outbox SET status = 'processing', updated_at = ? WHERE outbox_id = ?",
        ("2000-01-01T00:00:00Z", job_id),
    )
    conn.commit()
    db_path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    workers = [sqlite3.connect(db_path, timeout=5, check_same_thread=False) for _ in range(2)]
    barrier = threading.Barrier(2)
    results = []
    errors = []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def claim(worker_conn):
        try:
            barrier.wait()
            results.append(
                _claim_job(
                    worker_conn,
                    job_id,
                    now,
                    lease_cutoff="2001-01-01T00:00:00Z",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for worker in workers:
        worker.close()

    assert errors == []
    assert sorted(results) == [False, True]


@pytest.mark.parametrize(
    ("tool_name", "metadata_json", "payload_mutation"),
    [
        ("memory_index", '{"job_schema":"memory-index/v1"}', None),
        ("memory_index", '{"job_schema":"memory-index/v2"}', "memory_version"),
        ("synthesis_index", '{"job_schema":"synthesis-index/v0"}', None),
        ("synthesis_index", '{"job_schema":"synthesis-index/v1"}', "revision"),
    ],
)
def test_index_replay_rejects_unknown_schema_and_incomplete_payload(
    synthesis_engine,
    tool_name,
    metadata_json,
    payload_mutation,
) -> None:
    from plastic_promise.core.synthesis_maintenance import replay_memory_index_jobs
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    conn = synthesis_engine._sqlite._conn
    if tool_name == "memory_index":
        material = _store_ordinary_index_candidate(synthesis_engine)
        job_id = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index",
            project_id="project:test",
            embedding_hash=material.embedding_hash,
            call_id="call-invalid-schema",
        )
        replay = replay_memory_index_jobs
    else:
        verified = _create_and_verify(synthesis_engine)
        job_id = enqueue_synthesis_index_job(
            conn,
            memory_id=verified.memory_id,
            revision=verified.revision,
            action="upsert",
            call_id="call-invalid-schema",
        )
        replay = replay_synthesis_index_jobs
    row = conn.execute(
        "SELECT payload_json FROM store_outbox WHERE outbox_id = ?",
        (job_id,),
    ).fetchone()
    payload = json.loads(row[0])
    if payload_mutation is not None:
        payload.pop(payload_mutation)
    conn.execute(
        "UPDATE store_outbox SET metadata_json = ?, payload_json = ? WHERE outbox_id = ?",
        (metadata_json, json.dumps(payload), job_id),
    )
    conn.commit()
    before_rows = dict(synthesis_engine._ldb.rows)

    report = replay(synthesis_engine)

    row = conn.execute(
        "SELECT status, error_class FROM store_outbox WHERE outbox_id = ?",
        (job_id,),
    ).fetchone()
    assert report.failed == 1
    assert row[0] == "pending"
    assert row[1] == "ValueError"
    assert synthesis_engine._ldb.rows == before_rows


def test_reclaimed_worker_loses_lease_before_lancedb_side_effect(
    synthesis_engine,
) -> None:
    from plastic_promise.core.synthesis_maintenance import _replay_index_job

    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    job_id = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="upsert",
        call_id="call-old-worker",
    )
    old_claim = "2000-01-01T00:00:00Z"
    new_claim = "2026-07-11T15:00:00Z"
    conn.execute(
        "UPDATE store_outbox SET status = 'processing', updated_at = ? WHERE outbox_id = ?",
        (new_claim, job_id),
    )
    conn.commit()
    before_rows = dict(synthesis_engine._ldb.rows)
    before_inserts = list(synthesis_engine._ldb.inserted)

    with pytest.raises(RuntimeError, match="index_job_lease_lost"):
        _replay_index_job(
            synthesis_engine,
            {
                "action": "upsert",
                "memory_id": verified.memory_id,
                "revision": verified.revision,
            },
            lease_owner=(job_id, old_claim),
        )

    assert synthesis_engine._ldb.rows == before_rows
    assert synthesis_engine._ldb.inserted == before_inserts


def test_verify_queues_and_opportunistically_replays_upsert(synthesis_engine) -> None:
    verified = _create_and_verify(synthesis_engine)

    jobs = _jobs(synthesis_engine._sqlite._conn, verified.memory_id)
    assert [(job["status"], job["payload"]["action"]) for job in jobs] == [("done", "upsert")]
    assert len(synthesis_engine._ldb.inserted) == 1
    assert synthesis_engine._ldb.inserted[0]["memory_id"] == verified.memory_id


def test_changed_source_marks_verified_synthesis_stale_and_queues_delete(
    synthesis_engine,
) -> None:
    verified = _create_and_verify(synthesis_engine)
    synthesis_engine._sqlite.upsert(
        "source-a",
        {
            **synthesis_engine._sqlite.get("source-a"),
            "content": "The canonical source changed after verification.",
        },
    )

    report = scan_synthesis_integrity(synthesis_engine)

    assert report.stale_ids == (verified.memory_id,)
    assert report.contested_ids == ()
    assert SynthesisStore(synthesis_engine._sqlite._conn).get(verified.memory_id).status == "stale"
    assert (
        _jobs(synthesis_engine._sqlite._conn, verified.memory_id)[-1]["payload"]["action"]
        == "delete"
    )


def test_contradiction_marks_verified_synthesis_contested(synthesis_engine) -> None:
    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    conn.execute(
        "INSERT INTO behavior_graph_edges "
        "(id, source, target, relation, weight, source_kind, evidence_id, "
        "metadata_json, schema_version, updated_at) "
        "VALUES (?, ?, ?, 'contradicts', 1.0, 'evidence', ?, '{}', "
        "'behavior-graph/v1', ?)",
        (
            "edge:contradiction",
            "source-a",
            verified.memory_id,
            "source-a",
            "2026-07-11T00:00:00Z",
        ),
    )
    conn.commit()

    report = scan_synthesis_integrity(synthesis_engine)

    assert report.contested_ids == (verified.memory_id,)
    assert report.stale_ids == ()
    assert SynthesisStore(conn).get(verified.memory_id).status == "contested"


def test_index_job_enqueue_and_replay_are_idempotent_for_current_verified_row(
    synthesis_engine,
) -> None:
    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    first = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="delete",
        call_id="call-delete",
    )
    second = enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="delete",
        call_id="call-delete-again",
    )

    assert first == second
    first_report = replay_synthesis_index_jobs(synthesis_engine)
    second_report = replay_synthesis_index_jobs(synthesis_engine)

    assert first_report.succeeded == 1
    assert second_report.succeeded == 0
    assert synthesis_engine._ldb.deleted == []
    assert verified.memory_id in synthesis_engine._ldb.rows


def test_old_revision_jobs_cannot_clobber_current_verified_index(
    synthesis_engine,
) -> None:
    verified_v1 = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    store = SynthesisStore(conn, engine=synthesis_engine)
    contested = store.mark_contested(
        verified_v1.memory_id,
        "source review requires refresh",
        1,
        actor="reviewer",
        call_id="call-contest-v1",
    )
    delete_v1 = _jobs(conn, verified_v1.memory_id)[-1]["outbox_id"]
    synthesis_engine._ldb.fail_delete = True
    assert replay_synthesis_index_jobs(synthesis_engine).failed == 1

    refreshed = SynthesisStore(conn).refresh(
        contested.memory_id,
        "The refreshed exact conclusion remains supported by both sources.",
        ["source-a", "source-b"],
        1,
        call_id="call-refresh-v2",
    )
    synthesis_engine._ldb.fail_delete = False
    verified_v2 = SynthesisStore(conn, engine=synthesis_engine).verify(
        refreshed.memory_id,
        "reviewer-v2",
        "call-verify-v2",
        2,
    )
    assert verified_v2.status == "verified"
    assert verified_v2.memory_id in synthesis_engine._ldb.rows
    assert synthesis_engine._ldb.rows[verified_v2.memory_id]["text"] == (
        "The refreshed exact conclusion remains supported by both sources."
    )
    current_row = dict(synthesis_engine._ldb.rows[verified_v2.memory_id])
    insert_count = len(synthesis_engine._ldb.inserted)
    embed_count = len(synthesis_engine._embedder.inputs)

    conn.execute(
        "UPDATE store_outbox SET next_attempt_at = '' WHERE outbox_id = ?",
        (delete_v1,),
    )
    conn.commit()
    assert replay_synthesis_index_jobs(synthesis_engine).succeeded == 1
    assert synthesis_engine._ldb.deleted == []
    assert synthesis_engine._ldb.rows[verified_v2.memory_id] == current_row

    enqueue_synthesis_index_job(
        conn,
        memory_id=verified_v2.memory_id,
        revision=1,
        action="upsert",
        call_id="call-late-upsert-v1",
    )
    assert replay_synthesis_index_jobs(synthesis_engine).succeeded == 1
    assert len(synthesis_engine._ldb.inserted) == insert_count
    assert len(synthesis_engine._embedder.inputs) == embed_count
    assert synthesis_engine._ldb.rows[verified_v2.memory_id] == current_row


def test_upsert_rechecks_eligibility_after_embedding(synthesis_engine) -> None:
    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="upsert",
        call_id="call-upsert-recheck-after-embed",
    )
    insert_count = len(synthesis_engine._ldb.inserted)

    def invalidate_after_embed():
        conn.execute(
            "UPDATE synthesis_artifacts SET status = 'contested' WHERE memory_id = ?",
            (verified.memory_id,),
        )
        conn.commit()
        synthesis_engine._embedder.after_embed = None

    synthesis_engine._embedder.after_embed = invalidate_after_embed
    report = replay_synthesis_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert len(synthesis_engine._ldb.inserted) == insert_count
    assert synthesis_engine._ldb.deleted == [verified.memory_id]
    assert verified.memory_id not in synthesis_engine._ldb.rows


def test_upsert_rolls_back_index_when_control_changes_during_insert(
    synthesis_engine,
) -> None:
    verified = _create_and_verify(synthesis_engine)
    conn = synthesis_engine._sqlite._conn
    synthesis_engine._ldb.rows[verified.memory_id]["text"] = "STALE INDEX TEXT"
    enqueue_synthesis_index_job(
        conn,
        memory_id=verified.memory_id,
        revision=verified.revision,
        action="upsert",
        call_id="call-upsert-recheck-after-insert",
    )

    def invalidate_after_insert():
        conn.execute(
            "UPDATE synthesis_artifacts SET status = 'contested' WHERE memory_id = ?",
            (verified.memory_id,),
        )
        synthesis_engine._ldb.after_insert = None

    synthesis_engine._ldb.after_insert = invalidate_after_insert
    report = replay_synthesis_index_jobs(synthesis_engine)

    assert report.succeeded == 1
    assert synthesis_engine._ldb.deleted == [verified.memory_id]
    assert verified.memory_id not in synthesis_engine._ldb.rows


def test_failed_checked_index_operation_remains_pending(synthesis_engine) -> None:
    verified = _create_and_verify(synthesis_engine)
    store = SynthesisStore(synthesis_engine._sqlite._conn, engine=synthesis_engine)
    store.mark_contested(
        verified.memory_id,
        "index must be removed",
        verified.revision,
        actor="reviewer",
        call_id="call-contest-before-failed-delete",
    )
    synthesis_engine._ldb.fail_delete = True
    job_id = _jobs(synthesis_engine._sqlite._conn, verified.memory_id)[-1]["outbox_id"]

    report = replay_synthesis_index_jobs(synthesis_engine)
    row = synthesis_engine._sqlite._conn.execute(
        "SELECT status, attempt_count, next_attempt_at, error_class "
        "FROM store_outbox WHERE outbox_id = ?",
        (job_id,),
    ).fetchone()

    assert report.failed == 1
    assert row[0] == "pending"
    assert row[1] == 1
    assert row[2]
    assert row[3] == "RuntimeError"


def test_contested_transition_queues_delete_for_previous_revision(synthesis_engine) -> None:
    verified = _create_and_verify(synthesis_engine)
    store = SynthesisStore(synthesis_engine._sqlite._conn, engine=synthesis_engine)

    contested = store.mark_contested(
        verified.memory_id,
        "new contradiction",
        verified.revision,
        actor="reviewer",
        call_id="call-contest",
    )

    assert contested.status == "contested"
    assert _jobs(synthesis_engine._sqlite._conn, verified.memory_id)[-1]["payload"] == {
        "action": "delete",
        "memory_id": verified.memory_id,
        "revision": verified.revision,
    }


def test_checked_lancedb_methods_propagate_backend_failures() -> None:
    class BrokenTable:
        def search(self):
            return self

        def where(self, *_args, **_kwargs):
            return self

        def limit(self, _limit):
            return self

        def to_list(self):
            return []

        def add(self, _rows):
            raise RuntimeError("insert failed")

        def delete(self, _predicate):
            raise RuntimeError("delete failed")

    store = object.__new__(LanceDBStore)
    store._vectors_disabled = False
    store._table = BrokenTable()

    with pytest.raises(RuntimeError, match="insert failed"):
        store.insert_checked("m1", [0.1] * 1024, "text")
    with pytest.raises(RuntimeError, match="delete failed"):
        store.delete_checked("m1")
