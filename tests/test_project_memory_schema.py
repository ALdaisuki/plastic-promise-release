import asyncio
import json

from plastic_promise.core.context_engine import ContextEngine, _SQLiteMemoryStore
from plastic_promise.mcp.tools.memory import handle_memory_store
from plastic_promise.memory.soul_memory import MemoryRecord


class _CountingLanceDB:
    def __init__(self):
        self.check_duplicate_calls = 0
        self.insert_calls = 0

    def check_duplicate(self, vector, threshold):
        self.check_duplicate_calls += 1
        return None

    def insert(self, **kwargs):
        self.insert_calls += 1


def test_memory_store_persists_project_metadata(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))
    store.upsert(
        "m1",
        {
            "content": "project scoped memory",
            "memory_type": "experience",
            "source": "codex",
            "scope": "agent:codex",
            "tags": ["project:plastic-promise"],
            "project_id": "project:plastic-promise",
            "visibility": "shared",
            "source_class": "experience",
            "created_by_call_id": "call_one",
            "origin_kind": "tool_call",
            "origin_uri": "mcp://memory_store",
            "origin_ref": "req:one",
            "origin_hash": "hash_one",
            "parent_memory_ids": ["parent_a"],
            "metadata_json": {"quality": "release-gate"},
        },
    )

    row = store.get("m1")

    assert row["project_id"] == "project:plastic-promise"
    assert row["visibility"] == "shared"
    assert row["source_class"] == "experience"
    assert row["created_by_call_id"] == "call_one"
    assert row["origin_kind"] == "tool_call"
    assert row["origin_uri"] == "mcp://memory_store"
    assert row["origin_ref"] == "req:one"
    assert row["origin_hash"] == "hash_one"
    assert row["parent_memory_ids"] == ["parent_a"]
    assert row["metadata_json"] == {"quality": "release-gate"}
    store._conn.close()


def test_traceability_tables_exist(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))

    table_names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert {"projects", "call_spans", "memory_lineage", "degradation_events"}.issubset(
        table_names
    )
    store._conn.close()


def test_synthesis_artifacts_table_exists(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))

    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(synthesis_artifacts)").fetchall()
    }

    assert {"memory_id", "synthesis_key", "status", "revision", "source_fingerprint"} <= columns
    store._conn.close()


def test_memory_proposals_table_exists(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))

    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(memory_proposals)").fetchall()
    }

    assert {
        "proposal_id",
        "project_id",
        "visibility",
        "content_hash",
        "status",
        "approval_actor",
        "approval_call_id",
        "expires_at",
        "redacted_at",
    } <= columns
    store._conn.close()


def test_memory_store_handles_malformed_provenance_json(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))
    store.upsert(
        "m1",
        {
            "content": "project scoped memory",
            "memory_type": "experience",
            "source": "codex",
        },
    )
    store._conn.execute(
        "UPDATE memories SET parent_memory_ids = ?, metadata_json = ? WHERE id = ?",
        ("not-json", "{broken", "m1"),
    )
    store._conn.commit()

    row = store.get("m1")

    assert row["parent_memory_ids"] == []
    assert row["metadata_json"] == {}
    assert list(store.iter_all())
    store._conn.close()


def test_memory_store_handles_wrong_type_provenance_json(tmp_path):
    store = _SQLiteMemoryStore(str(tmp_path / "mem.db"))
    store.upsert(
        "m1",
        {
            "content": "project scoped memory",
            "memory_type": "experience",
            "source": "codex",
        },
    )
    store._conn.execute(
        "UPDATE memories SET parent_memory_ids = ?, metadata_json = ? WHERE id = ?",
        ('{"not": "a-list"}', '["not", "a-dict"]', "m1"),
    )
    store._conn.commit()

    row = store.get("m1")

    assert row["parent_memory_ids"] == []
    assert row["metadata_json"] == {}
    assert list(store.iter_all())
    store._conn.close()


def test_memory_store_handler_carries_project_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    engine = ContextEngine()

    result = asyncio.run(
        handle_memory_store(
            engine,
            {
                "content": "project memory store metadata test",
                "memory_type": "experience",
                "source": "codex",
                "project_id": "project:test-app",
                "visibility": "shared",
                "source_class": "experience",
                "origin_kind": "tool_call",
                "origin_uri": "mcp://memory_store",
                "origin_ref": "req:test",
                "origin_hash": "hash:test",
                "call_id": "call_test",
                "parent_memory_ids": ["mem_parent"],
                "metadata_json": {"suite": "project"},
                "max_llm_calls": 0,
            },
        )
    )
    payload = json.loads(result[0].text)
    stored = engine._sqlite.get(payload["memory_id"])

    assert payload["project_id"] == "project:test-app"
    assert payload["visibility"] == "shared"
    assert payload["source_class"] == "experience"
    assert stored["project_id"] == "project:test-app"
    assert stored["created_by_call_id"] == "call_test"
    assert stored["parent_memory_ids"] == ["mem_parent"]
    assert stored["metadata_json"]["suite"] == "project"
    engine._sqlite._conn.close()


def test_memory_store_skip_embed_does_not_write_zero_vectors(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "mem.db"))
    engine = ContextEngine()
    fake_lancedb = _CountingLanceDB()
    engine._ldb = fake_lancedb

    result = asyncio.run(
        handle_memory_store(
            engine,
            {
                "content": "skip embed project provenance store",
                "memory_type": "experience",
                "source": "codex",
                "project_id": "project:test-app",
                "visibility": "shared",
                "source_class": "experience",
                "call_id": "call_skip_embed",
                "max_llm_calls": 0,
            },
        )
    )
    payload = json.loads(result[0].text)

    assert engine._sqlite.get(payload["memory_id"]) is not None
    assert fake_lancedb.check_duplicate_calls == 0
    assert fake_lancedb.insert_calls == 0
    engine._sqlite._conn.close()


def test_memory_record_from_dict_hydrates_project_provenance_metadata():
    record = MemoryRecord.from_dict(
        {
            "id": "mem_engine_row",
            "content": "engine style memory",
            "memory_type": "experience",
            "source": "codex",
            "metadata": {"existing": "kept"},
            "project_id": "project:test-app",
            "visibility": "shared",
            "source_class": "experience",
            "created_by_call_id": "call_from_dict",
            "origin_kind": "tool_call",
            "origin_uri": "mcp://memory_store",
            "origin_ref": "req:from-dict",
            "origin_hash": "hash:from-dict",
            "parent_memory_ids": ["mem_parent"],
            "metadata_json": {"suite": "project"},
        }
    )

    assert record.memory_id == "mem_engine_row"
    assert record.metadata["existing"] == "kept"
    assert record.metadata["project_id"] == "project:test-app"
    assert record.metadata["visibility"] == "shared"
    assert record.metadata["source_class"] == "experience"
    assert record.metadata["created_by_call_id"] == "call_from_dict"
    assert record.metadata["origin_kind"] == "tool_call"
    assert record.metadata["origin_uri"] == "mcp://memory_store"
    assert record.metadata["origin_ref"] == "req:from-dict"
    assert record.metadata["origin_hash"] == "hash:from-dict"
    assert record.metadata["parent_memory_ids"] == ["mem_parent"]
    assert record.metadata["metadata_json"] == {"suite": "project"}
