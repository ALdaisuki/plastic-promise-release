import asyncio
import json
import sqlite3

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.traceability import (
    MEMORY_INDEX_JOB_SCHEMA,
    enqueue_memory_index_delete,
    enqueue_memory_index_job,
    enqueue_memory_index_upsert,
    ensure_traceability_schema,
    record_outbox_event,
)
from plastic_promise.mcp.tools import memory as memory_tools
from plastic_promise.mcp.tools.memory import handle_memory_store


def _index_enqueue_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "index.db"))
    engine = ContextEngine(use_sqlite=True)
    assert engine.register_memory(
        {
            "id": "ordinary-index-job",
            "content": "canonical ordinary index evidence",
            "memory_type": "experience",
            "source": "test",
            "project_id": "project:index-a",
            "embedding_hash": "sha256:index-a",
        }
    ) == "ordinary-index-job"
    return engine


def test_record_outbox_event_persists_to_sqlite(tmp_path):
    conn = sqlite3.connect(tmp_path / "trace.db")
    ensure_traceability_schema(conn)

    outbox_id = record_outbox_event(
        conn,
        tool_name="memory_store",
        project_id="project:test-app",
        call_id="call_store",
        status="stored",
        payload={"content": "minimum durable payload"},
        error_class="OperationalError",
        error_message="database is locked",
        metadata={"fallback": "sqlite"},
    )

    row = conn.execute(
        """
        SELECT outbox_id, tool_name, project_id, call_id, status,
               payload_json, error_class, error_message, metadata_json
        FROM store_outbox
        WHERE outbox_id = ?
        """,
        (outbox_id,),
    ).fetchone()

    assert row[:5] == (
        outbox_id,
        "memory_store",
        "project:test-app",
        "call_store",
        "stored",
    )
    assert json.loads(row[5]) == {"content": "minimum durable payload"}
    assert row[6:8] == ("OperationalError", "database is locked")
    assert json.loads(row[8]) == {"fallback": "sqlite"}
    conn.close()


def test_record_outbox_event_falls_back_to_jsonl(tmp_path):
    class BrokenConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    fallback_path = tmp_path / "store_outbox.jsonl"

    outbox_id = record_outbox_event(
        BrokenConn(),
        tool_name="memory_store",
        project_id="project:test-app",
        call_id="call_store",
        status="stored",
        payload={"content": "minimum durable payload"},
        error_class="OperationalError",
        error_message="database is locked",
        fallback_path=fallback_path,
    )

    lines = fallback_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outbox_id"] == outbox_id
    assert record["project_id"] == "project:test-app"
    assert record["payload"]["content"] == "minimum durable payload"
    assert record["error_class"] == "OperationalError"


def test_memory_store_failure_returns_outbox_minimum_result(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    engine = ContextEngine()

    def fail_fuzzy_buffer(_engine):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory_tools, "_get_fuzzy_buffer", fail_fuzzy_buffer)

    result = asyncio.run(
        handle_memory_store(
            engine,
            {
                "content": "release outbox fallback memory",
                "memory_type": "experience",
                "source": "codex",
                "project_id": "project:test-app",
                "call_id": "call_outbox",
                "max_llm_calls": 0,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert payload["stored"] is False
    assert payload["degraded"] is True
    assert payload["fallback_used"] == ["store_outbox"]
    assert payload["minimum_result"] == "outbox_record"
    assert payload["trace"]["call_id"] == "call_outbox"
    assert payload["project_id"] == "project:test-app"
    assert payload["outbox_id"].startswith("outbox_")

    row = engine._sqlite._conn.execute(
        "SELECT project_id, call_id, status, payload_json FROM store_outbox WHERE outbox_id = ?",
        (payload["outbox_id"],),
    ).fetchone()
    span = engine._sqlite._conn.execute(
        "SELECT project_id, tool_name, status, degraded FROM call_spans WHERE call_id = ?",
        ("call_outbox",),
    ).fetchone()
    event = engine._sqlite._conn.execute(
        """
        SELECT project_id, tool_name, link_name, fallback_used, minimum_result
        FROM degradation_events
        WHERE call_id = ?
        """,
        ("call_outbox",),
    ).fetchone()
    assert row[:3] == ("project:test-app", "call_outbox", "pending")
    assert json.loads(row[3])["content"] == "release outbox fallback memory"
    assert span == ("project:test-app", "memory_store", "degraded", 1)
    assert event == (
        "project:test-app",
        "memory_store",
        "store_outbox",
        "store_outbox",
        "outbox_record",
    )
    engine._sqlite._conn.close()


def test_memory_index_v3_enqueue_payload_is_exact_and_legacy_hash_alias_works(
    tmp_path,
    monkeypatch,
):
    engine = _index_enqueue_engine(tmp_path, monkeypatch)
    conn = engine._sqlite._conn
    try:
        first = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            embedding_hash="sha256:index-a",
            call_id="call-first",
        )
        duplicate = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-a",
            call_id="call-duplicate",
        )

        row = conn.execute(
            "SELECT project_id, call_id, payload_json, metadata_json "
            "FROM store_outbox WHERE outbox_id = ?",
            (first,),
        ).fetchone()
        version = conn.execute(
            "SELECT version FROM memory_version WHERE singleton = 1"
        ).fetchone()[0]

        assert duplicate == first
        assert row[:2] == ("project:index-a", "call-first")
        assert json.loads(row[2]) == {
            "action": "upsert",
            "expected_embedding_hash": "sha256:index-a",
            "material_revision": "sha256:index-a",
            "memory_id": "ordinary-index-job",
            "memory_version": version,
            "project_id": "project:index-a",
        }
        assert set(json.loads(row[2])) == {
            "action",
            "expected_embedding_hash",
            "material_revision",
            "memory_id",
            "memory_version",
            "project_id",
        }
        assert json.loads(row[3]) == {"job_schema": MEMORY_INDEX_JOB_SCHEMA}
    finally:
        conn.close()


def test_memory_index_v3_dedupe_includes_action_version_hash_and_project(
    tmp_path,
    monkeypatch,
):
    engine = _index_enqueue_engine(tmp_path, monkeypatch)
    conn = engine._sqlite._conn
    try:
        upsert_a = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-a",
            call_id="call-upsert-a",
        )
        assert (
            enqueue_memory_index_upsert(
                conn,
                memory_id="ordinary-index-job",
                project_id="project:index-a",
                expected_embedding_hash="sha256:index-a",
                call_id="call-upsert-a-duplicate",
            )
            == upsert_a
        )
        delete_a = enqueue_memory_index_delete(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-a",
            call_id="call-delete-a",
        )
        conn.execute("UPDATE memory_version SET version = version + 1 WHERE singleton = 1")
        conn.commit()
        upsert_new_version = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-a",
            call_id="call-upsert-new-version",
        )
        conn.execute(
            "UPDATE memories SET embedding_hash = ? WHERE id = ?",
            ("sha256:index-b", "ordinary-index-job"),
        )
        conn.commit()
        upsert_new_hash = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-b",
            call_id="call-upsert-new-hash",
        )
        conn.execute(
            "UPDATE memories SET project_id = ? WHERE id = ?",
            ("project:index-b", "ordinary-index-job"),
        )
        conn.commit()
        upsert_new_project = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-b",
            expected_embedding_hash="sha256:index-b",
            call_id="call-upsert-new-project",
        )

        assert len(
            {
                upsert_a,
                delete_a,
                upsert_new_version,
                upsert_new_hash,
                upsert_new_project,
            }
        ) == 5
    finally:
        conn.close()


def test_memory_index_v3_enqueue_rejects_invalid_action_or_canonical_mismatch(
    tmp_path,
    monkeypatch,
):
    engine = _index_enqueue_engine(tmp_path, monkeypatch)
    conn = engine._sqlite._conn
    try:
        with pytest.raises(ValueError, match="invalid_memory_index_action"):
            enqueue_memory_index_job(
                conn,
                memory_id="ordinary-index-job",
                project_id="project:index-a",
                action="replace",
                expected_embedding_hash="sha256:index-a",
                call_id="call-invalid-action",
            )
        with pytest.raises(ValueError, match="memory_index_material_mismatch"):
            enqueue_memory_index_upsert(
                conn,
                memory_id="ordinary-index-job",
                project_id="project:index-a",
                expected_embedding_hash="sha256:wrong",
                call_id="call-invalid-hash",
            )
        with pytest.raises(ValueError, match="memory_index_project_mismatch"):
            enqueue_memory_index_delete(
                conn,
                memory_id="ordinary-index-job",
                project_id="project:wrong",
                expected_embedding_hash="sha256:index-a",
                call_id="call-invalid-project",
            )
        with pytest.raises(ValueError, match="invalid_memory_index_job"):
            enqueue_memory_index_upsert(
                conn,
                memory_id="ordinary-index-job",
                project_id="",
                expected_embedding_hash="sha256:index-a",
                call_id="call-empty-project",
            )

        assert conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == 0
    finally:
        conn.close()


def test_memory_index_v3_enqueue_joins_caller_owned_transaction(tmp_path, monkeypatch):
    engine = _index_enqueue_engine(tmp_path, monkeypatch)
    conn = engine._sqlite._conn
    try:
        ensure_traceability_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        job_id = enqueue_memory_index_upsert(
            conn,
            memory_id="ordinary-index-job",
            project_id="project:index-a",
            expected_embedding_hash="sha256:index-a",
            call_id="call-caller-transaction",
        )

        assert conn.in_transaction
        assert conn.execute(
            "SELECT outbox_id FROM store_outbox WHERE outbox_id = ?",
            (job_id,),
        ).fetchone() == (job_id,)
        conn.rollback()
        assert conn.execute(
            "SELECT outbox_id FROM store_outbox WHERE outbox_id = ?",
            (job_id,),
        ).fetchone() is None
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
