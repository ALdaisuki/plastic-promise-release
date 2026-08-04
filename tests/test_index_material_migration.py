from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from plastic_promise.core import index_material_migration as migration
from plastic_promise.core.memory_index import (
    build_index_material,
    metadata_with_index_material,
    read_persisted_index_material,
)
from plastic_promise.core.synthesis import (
    canonical_synthesis_binding,
    synthesis_binding_hash,
)
from plastic_promise.core.synthesis_retrieval import _validate_candidate_binding

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_index_material.py"
_SPEC = importlib.util.spec_from_file_location("migrate_index_material", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT_MODULE)
main = _SCRIPT_MODULE.main


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            owner TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'L1',
            scope TEXT NOT NULL DEFAULT 'global',
            category TEXT NOT NULL DEFAULT 'other',
            tags TEXT NOT NULL DEFAULT '[]',
            domain TEXT NOT NULL DEFAULT 'uncategorized',
            importance REAL NOT NULL DEFAULT 0.7,
            entity_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT '2026-07-24T00:00:00Z',
            project_id TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'project',
            source_class TEXT NOT NULL DEFAULT 'experience',
            created_by_call_id TEXT NOT NULL DEFAULT '',
            origin_kind TEXT NOT NULL DEFAULT '',
            origin_uri TEXT NOT NULL DEFAULT '',
            origin_ref TEXT NOT NULL DEFAULT '',
            origin_hash TEXT NOT NULL DEFAULT '',
            parent_memory_ids TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            raw_content TEXT NOT NULL DEFAULT '',
            l0_abstract TEXT NOT NULL DEFAULT '',
            l1_summary TEXT NOT NULL DEFAULT '',
            l2_content TEXT NOT NULL DEFAULT '',
            embedding_text TEXT NOT NULL,
            embedding_hash TEXT NOT NULL,
            search_text TEXT NOT NULL
        );
        CREATE TABLE synthesis_artifacts (
            memory_id TEXT PRIMARY KEY,
            synthesis_key TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'project',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE memory_version (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL
        );
        INSERT INTO memory_version (singleton, version) VALUES (1, 7);
        CREATE TABLE store_outbox (
            outbox_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            dedupe_key TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            next_attempt_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_store_outbox_active_dedupe
        ON store_outbox(dedupe_key)
        WHERE dedupe_key <> '' AND status IN ('pending', 'processing');
        CREATE TABLE task_queue (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        INSERT INTO task_queue VALUES ('task-1', 'pending', '{"preserve":true}');
        CREATE TABLE behavior_graph_edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            relation TEXT NOT NULL,
            weight REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO behavior_graph_edges
        VALUES ('edge-1', 'memory-legacy', 'memory-fallback', 'related', 0.5, '{}');
        """
    )


def _seed_memory(
    connection: sqlite3.Connection,
    *,
    memory_id: str,
    content: str,
    policy: str,
    memory_type: str = "experience",
    metadata: dict[str, object] | None = None,
) -> tuple[dict[str, object], object]:
    record: dict[str, object] = {
        "id": memory_id,
        "content": content,
        "memory_type": memory_type,
        "project_id": "project:test",
        "visibility": "project",
        "source_class": "synthesis" if memory_type == "synthesis" else "experience",
        "origin_kind": "synthesis" if memory_type == "synthesis" else "user",
        "origin_hash": f"sha256:{memory_id}",
        "l0_abstract": f"Abstract {memory_id}",
        "l1_summary": f"Summary {memory_id}",
        "l2_content": content,
    }
    if policy == "summary-v1":
        record["embedding_text"] = f"L0: {record['l0_abstract']}\nL1: {record['l1_summary']}"
    material = build_index_material(record, policy=policy, model_name="mxbai-embed-large")
    memory_metadata = metadata_with_index_material(metadata or {}, material)
    record.update(
        {
            "metadata_json": memory_metadata,
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
        }
    )
    connection.execute(
        "INSERT INTO memories ("
        "id, content, memory_type, project_id, visibility, source_class, origin_kind, "
        "origin_hash, metadata_json, l0_abstract, l1_summary, l2_content, "
        "embedding_text, embedding_hash, search_text"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            content,
            memory_type,
            "project:test",
            "project",
            record["source_class"],
            record["origin_kind"],
            record["origin_hash"],
            _json(memory_metadata),
            record["l0_abstract"],
            record["l1_summary"],
            record["l2_content"],
            material.vector_text,
            material.embedding_hash,
            material.search_text,
        ),
    )
    return record, material


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "plastic_memory.db"
    connection = sqlite3.connect(path)
    _schema(connection)
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    _seed_memory(
        connection,
        memory_id="memory-legacy",
        content="Legacy canonical content.",
        policy="legacy",
    )
    _seed_memory(
        connection,
        memory_id="memory-fallback",
        content="Fallback canonical content.",
        policy="legacy-fallback",
    )
    metadata = {"synthesis_key": "syn-key", "synthesis_revision": 2}
    record, material = _seed_memory(
        connection,
        memory_id="synthesis-1",
        content="Governed synthesis content.",
        policy="summary-v1",
        memory_type="synthesis",
        metadata=metadata,
    )
    binding = canonical_synthesis_binding(record, material)
    binding_hash = synthesis_binding_hash(binding)
    memory_metadata = dict(record["metadata_json"])
    memory_metadata["synthesis_binding"] = binding
    memory_metadata["synthesis_binding_hash"] = binding_hash
    connection.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 'synthesis-1'",
        (_json(memory_metadata),),
    )
    connection.execute(
        "INSERT INTO synthesis_artifacts ("
        "memory_id, synthesis_key, status, revision, project_id, visibility, metadata_json"
        ") VALUES ('synthesis-1', 'syn-key', 'verified', 2, 'project:test', 'project', ?)",
        (
            _json(
                {
                    "project_id": "project:test",
                    "visibility": "project",
                    "synthesis_binding": binding,
                    "synthesis_binding_hash": binding_hash,
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    return path


def _environment_file(tmp_path: Path) -> Path:
    path = tmp_path / "revision.env"
    path.write_text(
        "\n".join(
            (
                "EMBEDDER_PROVIDER=openai-compatible",
                "EMBEDDER_BASE_URL=https://api.example.test/v1",
                "EMBEDDER_PATH=/embeddings",
                "EMBEDDER_MODEL=Qwen3-Embedding-8B",
                "EMBEDDER_MODEL_REVISION=Qwen3-Embedding-8B",
                "EMBEDDER_DIMENSION=1024",
                "PP_EMBEDDING_DIM=1024",
                "EMBEDDER_SEND_DIMENSIONS=0",
                "PP_MEMORY_CHUNKING=structure-v1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _read_rows(path: Path) -> tuple[list[sqlite3.Row], int, list[sqlite3.Row]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        memories = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
        version = int(
            connection.execute("SELECT version FROM memory_version WHERE singleton = 1").fetchone()[
                0
            ]
        )
        outbox = connection.execute("SELECT * FROM store_outbox ORDER BY rowid").fetchall()
        return memories, version, outbox
    finally:
        connection.close()


def test_check_only_reports_mixed_material_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    before = database.read_bytes()
    old_chunking = os.environ.get("PP_MEMORY_CHUNKING")

    assert (
        main(
            [
                "--db",
                str(database),
                "--environment-file",
                str(environment),
                "--target-policy",
                "legacy",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False
    assert report["row_count"] == 3
    assert report["changed_row_count"] == 3
    assert report["ordinary_changed_count"] == 2
    assert report["synthesis_changed_count"] == 1
    assert report["current_policy_counts"] == {
        "legacy": 1,
        "legacy-fallback": 1,
        "summary-v1": 1,
    }
    assert len(report["source_fingerprint"]) == 64
    assert len(report["protected_fingerprint"]) == 64
    assert len(report["target_model_sha256"]) == 64
    assert database.read_bytes() == before
    assert _read_rows(database)[1:] == (7, [])
    assert os.environ.get("PP_MEMORY_CHUNKING") == old_chunking


def test_apply_backs_up_and_migrates_with_durable_outbox_and_synthesis_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    before_rows, before_version, _ = _read_rows(database)
    before_content = {str(row["id"]): str(row["content"]) for row in before_rows}

    with migration.configured_environment([environment]):
        plan = migration.inspect_database(database, target_policy="legacy")
        report = migration.apply_migration(
            database,
            backup_directory=backups,
            target_policy="legacy",
            expected_row_count=plan.row_count,
            expected_source_fingerprint=plan.source_fingerprint,
            expected_target_model_sha256=plan.target_model_sha256,
        )

    assert report["applied"] is True
    assert report["quick_check"] == report["integrity_check"] == "ok"
    assert report["source_fingerprint_after"] != plan.source_fingerprint
    assert report["protected_fingerprint_after"] == plan.protected_fingerprint
    backup = Path(str(report["backup_path"]))
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(str(report["backup_sha256"])) == 64

    backup_rows, backup_version, backup_outbox = _read_rows(backup)
    assert backup_version == before_version == 7
    assert backup_outbox == []
    assert [dict(row) for row in backup_rows] == [dict(row) for row in before_rows]

    rows, version, outbox = _read_rows(database)
    assert version == 8
    assert len(outbox) == 3
    assert {str(row["status"]) for row in outbox} == {"pending"}
    assert {str(row["tool_name"]) for row in outbox} == {
        "memory_index",
        "synthesis_index",
    }
    assert {str(row["id"]): str(row["content"]) for row in rows} == before_content
    for row in rows:
        memory = dict(row)
        material = read_persisted_index_material(
            memory,
            model_name=plan.target_model_identity,
        )
        assert material is not None
        assert material.policy == "legacy"
        assert material.vector_text == memory["content"]
        assert material.search_text == memory["content"]

    memory_jobs = [row for row in outbox if row["tool_name"] == "memory_index"]
    assert len(memory_jobs) == 2
    for job in memory_jobs:
        assert json.loads(job["metadata_json"]) == {"job_schema": "memory-index/v3"}
        payload = json.loads(job["payload_json"])
        assert payload["memory_version"] == 8
        assert payload["material_revision"] == payload["expected_embedding_hash"]
    synthesis_job = next(row for row in outbox if row["tool_name"] == "synthesis_index")
    assert json.loads(synthesis_job["metadata_json"]) == {"job_schema": "synthesis-index/v1"}
    assert json.loads(synthesis_job["payload_json"]) == {
        "action": "upsert",
        "memory_id": "synthesis-1",
        "revision": 2,
    }

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        candidate = dict(
            connection.execute("SELECT * FROM memories WHERE id = 'synthesis-1'").fetchone()
        )
        control = connection.execute(
            "SELECT metadata_json FROM synthesis_artifacts WHERE memory_id = 'synthesis-1'"
        ).fetchone()
        _validate_candidate_binding(
            candidate,
            json.loads(control[0]),
            synthesis_key="syn-key",
            revision=2,
        )
        with migration.configured_environment([environment]):
            final = migration.build_migration_plan(
                connection,
                target_policy="legacy",
                require_quiescent_outbox=False,
            )
        assert final.changed_row_count == 0
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["stale", "contested"])
def test_apply_preserves_ineligible_synthesis_status_and_enqueues_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE synthesis_artifacts SET status = ? WHERE memory_id = 'synthesis-1'",
        (status,),
    )
    connection.commit()
    connection.close()

    with migration.configured_environment([environment]):
        plan = migration.inspect_database(database, target_policy="legacy")
        report = migration.apply_migration(
            database,
            backup_directory=backups,
            target_policy="legacy",
            expected_row_count=plan.row_count,
            expected_source_fingerprint=plan.source_fingerprint,
            expected_target_model_sha256=plan.target_model_sha256,
        )

    assert report["applied"] is True
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        control = connection.execute(
            "SELECT status, revision FROM synthesis_artifacts WHERE memory_id = 'synthesis-1'"
        ).fetchone()
        job = connection.execute(
            "SELECT payload_json, dedupe_key FROM store_outbox WHERE tool_name = 'synthesis_index'"
        ).fetchone()
    finally:
        connection.close()

    assert tuple(control) == (status, 2)
    assert json.loads(job["payload_json"]) == {
        "action": "delete",
        "memory_id": "synthesis-1",
        "revision": 2,
    }
    assert job["dedupe_key"] == "synthesis-index:synthesis-1:2:delete"


def test_check_only_rejects_draft_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE synthesis_artifacts SET status = 'draft' WHERE memory_id = 'synthesis-1'"
    )
    connection.commit()
    connection.close()

    with (
        migration.configured_environment([environment]),
        pytest.raises(
            migration.IndexMaterialMigrationError,
            match="synthesis_control_not_terminal",
        ),
    ):
        migration.inspect_database(database, target_policy="legacy")


def test_apply_rolls_back_source_when_outbox_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    before_rows, before_version, before_outbox = _read_rows(database)
    with migration.configured_environment([environment]):
        plan = migration.inspect_database(database, target_policy="legacy")

        def fail_synthesis(*_args: object, **_kwargs: object) -> None:
            raise migration.IndexMaterialMigrationError("injected_outbox_failure")

        monkeypatch.setattr(migration, "_insert_synthesis_job", fail_synthesis)
        with pytest.raises(
            migration.IndexMaterialMigrationError,
            match="injected_outbox_failure",
        ):
            migration.apply_migration(
                database,
                backup_directory=backups,
                target_policy="legacy",
                expected_row_count=plan.row_count,
                expected_source_fingerprint=plan.source_fingerprint,
                expected_target_model_sha256=plan.target_model_sha256,
            )

    after_rows, after_version, after_outbox = _read_rows(database)
    assert [dict(row) for row in after_rows] == [dict(row) for row in before_rows]
    assert after_version == before_version
    assert after_outbox == before_outbox
    assert len(list(backups.iterdir())) == 1


def test_check_only_rejects_unresolved_index_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path, monkeypatch)
    environment = _environment_file(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO store_outbox ("
        "outbox_id, tool_name, status, created_at, updated_at"
        ") VALUES ('pending-1', 'memory_index', 'pending', '2026-07-24T00:00:00Z', "
        "'2026-07-24T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    with (
        migration.configured_environment([environment]),
        pytest.raises(
            migration.IndexMaterialMigrationError,
            match="index_outbox_not_quiescent",
        ),
    ):
        migration.inspect_database(database, target_policy="legacy")
