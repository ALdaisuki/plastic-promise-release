import json
import sqlite3

import pytest

from plastic_promise.core import synthesis_retrieval as retrieval
from plastic_promise.core.context_engine import (
    ContextEngine,
    MemoryRecord,
    _SQLiteStorage,
)
from plastic_promise.core.synthesis import (
    SYNTHESIS_STATUSES,
    SynthesisConflict,
    SynthesisStore,
    canonical_memory_hash,
    ensure_synthesis_schema,
    source_fingerprint,
    visibility_allows,
)
from plastic_promise.core.traceability import record_call_span, record_memory_lineage
from plastic_promise.memory.soul_memory import EvolveR, MemoryGC, RecMem
from plastic_promise.memory.soul_memory import MemoryRecord as SoulMemoryRecord


class _CommitFailOnceConnection:
    def __init__(self, conn):
        self._conn = conn
        self._fail_commit = True

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        if self._fail_commit:
            self._fail_commit = False
            raise RuntimeError("injected commit failure")
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _ReleaseFailOnceConnection:
    def __init__(self, conn):
        self._conn = conn
        self._failed = False

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def execute(self, sql, *args, **kwargs):
        if str(sql).startswith("RELEASE SAVEPOINT synthesis_") and not self._failed:
            self._failed = True
            raise RuntimeError("injected savepoint release failure")
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


def test_synthesis_schema_is_idempotent_and_exact(sqlite_conn):
    ensure_synthesis_schema(sqlite_conn)
    ensure_synthesis_schema(sqlite_conn)

    columns = [row[1] for row in sqlite_conn.execute("PRAGMA table_info(synthesis_artifacts)")]
    assert columns == [
        "memory_id",
        "synthesis_key",
        "status",
        "revision",
        "support_count",
        "validity_scope",
        "source_fingerprint",
        "last_verified_at",
        "last_linted_at",
        "stale_reason",
        "created_by_call_id",
        "verified_by_actor",
        "verified_by_call_id",
        "metadata_json",
        "created_at",
        "updated_at",
    ]

    indexes = {
        tuple(index_row[2] for index_row in sqlite_conn.execute(f"PRAGMA index_info({row[1]})"))
        for row in sqlite_conn.execute("PRAGMA index_list(synthesis_artifacts)")
    }
    assert ("status", "updated_at") in indexes
    assert ("synthesis_key",) in indexes


def test_synthesis_schema_does_not_commit_outer_transaction(sqlite_conn):
    sqlite_conn.execute("BEGIN")

    ensure_synthesis_schema(sqlite_conn)

    assert sqlite_conn.in_transaction
    sqlite_conn.rollback()
    table = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("synthesis_artifacts",),
    ).fetchone()
    assert table is None


def test_sqlite_storage_commit_failure_rolls_back_before_next_write(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "commit-failure.db"))
    real_conn = storage._conn
    storage._conn = _CommitFailOnceConnection(real_conn)

    with pytest.raises(RuntimeError, match="injected commit failure"):
        storage.upsert(
            "failed-write",
            {"content": "must roll back", "memory_type": "experience"},
        )

    assert real_conn.in_transaction is False
    assert storage.get("failed-write") is None
    storage.upsert(
        "successful-write",
        {"content": "commits alone", "memory_type": "experience"},
    )
    assert storage.get("failed-write") is None
    assert storage.get("successful-write") is not None
    real_conn.close()


def test_sqlite_batch_body_failure_rolls_back_all_writes(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "batch-body-failure.db"))

    with pytest.raises(RuntimeError, match="injected batch body failure"), storage.batch():
        storage.upsert(
            "failed-batch-write",
            {"content": "must roll back", "memory_type": "experience"},
        )
        raise RuntimeError("injected batch body failure")

    assert storage._conn.in_transaction is False
    assert storage.get("failed-batch-write") is None
    storage._conn.close()


def test_sqlite_batch_commit_failure_rolls_back_all_writes(tmp_path):
    storage = _SQLiteStorage(str(tmp_path / "batch-commit-failure.db"))
    real_conn = storage._conn
    storage._conn = _CommitFailOnceConnection(real_conn)

    with pytest.raises(RuntimeError, match="injected commit failure"), storage.batch():
        storage.upsert(
            "failed-batch-commit",
            {"content": "must roll back", "memory_type": "experience"},
        )

    assert real_conn.in_transaction is False
    assert storage.get("failed-batch-commit") is None
    real_conn.close()


def test_source_hash_ignores_mutable_worth_fields():
    base = {"id": "m1", "content": "fact", "origin_hash": "o1", "embedding_hash": "e1"}

    assert canonical_memory_hash({**base, "worth_success": 1}) == canonical_memory_hash(
        {**base, "worth_success": 99, "access_count": 500}
    )
    assert canonical_memory_hash(base).startswith("sha256:")
    assert canonical_memory_hash(base) != canonical_memory_hash({**base, "content": "changed"})


def test_source_fingerprint_is_order_independent_and_tracks_hashes():
    snapshots = {"m2": "sha256:two", "m1": "sha256:one"}

    fingerprint = source_fingerprint(snapshots)

    assert fingerprint == source_fingerprint(dict(reversed(list(snapshots.items()))))
    assert fingerprint.startswith("sha256:")
    assert fingerprint != source_fingerprint({**snapshots, "m1": "sha256:changed"})


def test_synthesis_visibility_cannot_be_wider_than_sources():
    assert visibility_allows("private", ["private", "project"])
    assert visibility_allows("project", ["project", "global"])
    assert not visibility_allows("global", ["project", "global"])
    assert not visibility_allows("project", [])
    assert not visibility_allows("unknown", ["project"])


def test_synthesis_vocabulary_and_store_constructor(sqlite_conn):
    engine = object()

    store = SynthesisStore(sqlite_conn, engine=engine)

    assert frozenset({"draft", "verified", "contested", "stale"}) == SYNTHESIS_STATUSES
    assert issubclass(SynthesisConflict, RuntimeError)
    assert store.conn is sqlite_conn
    assert store.engine is engine
    assert sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("synthesis_artifacts",),
    ).fetchone() == ("synthesis_artifacts",)


@pytest.fixture
def lifecycle_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    storage = _SQLiteStorage(str(tmp_path / "synthesis-lifecycle.db"))
    conn = storage._conn
    sources = {
        "source-a": {
            "id": "source-a",
            "content": "Alpha evidence is independently supported and remains current. " * 4,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///alpha.md",
            "origin_ref": "alpha",
            "origin_hash": "origin-alpha",
            "tags": ["status:current"],
            "metadata_json": {"status": "current"},
        },
        "source-b": {
            "id": "source-b",
            "content": "Beta evidence confirms the same conclusion from another source. " * 4,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "global",
            "origin_kind": "document",
            "origin_uri": "file:///beta.md",
            "origin_ref": "beta",
            "origin_hash": "origin-beta",
            "tags": ["quality:verified"],
            "metadata_json": {"quality_status": "verified"},
        },
        "source-c": {
            "id": "source-c",
            "content": "Gamma evidence is a replacement source with a fresh independent observation. "
            * 4,
            "memory_type": "experience",
            "source": "agent",
            "source_class": "experience",
            "project_id": "project:test",
            "visibility": "project",
            "origin_kind": "document",
            "origin_uri": "file:///gamma.md",
            "origin_ref": "gamma",
            "origin_hash": "origin-gamma",
            "tags": [],
            "metadata_json": {},
        },
    }
    for source in sources.values():
        storage.upsert(source["id"], source)
    yield storage, SynthesisStore(conn), sources
    conn.close()


def _version(conn):
    return conn.execute("SELECT version FROM memory_version").fetchone()[0]


def _table_counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "memories",
            "synthesis_artifacts",
            "behavior_graph_nodes",
            "behavior_graph_edges",
            "memory_lineage",
        )
    }


def _create(store, **overrides):
    kwargs = {
        "content": "The independent evidence supports one stable conclusion.",
        "source_ids": ["source-a", "source-b"],
        "synthesis_key": "topic:alpha",
        "validity_scope": "project:test",
        "project_id": "project:test",
        "visibility": "project",
        "actor": "codex",
        "call_id": "call-create",
        "automatic": False,
    }
    kwargs.update(overrides)
    return store.create_draft(**kwargs)


def _engine_for_storage(storage):
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    engine._loaded_memory_version = _version(storage._conn)
    engine.canonical_sync_ok = True
    return engine


def _reserve_control_row(conn, memory_id):
    now = "2026-07-10T12:00:00Z"
    conn.execute(
        "INSERT INTO synthesis_artifacts "
        "(memory_id, synthesis_key, status, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, 'draft', '{}', ?, ?)",
        (memory_id, f"reservation:{memory_id}", now, now),
    )
    conn.commit()


def test_synthesis_commit_failure_rolls_back_before_unrelated_commit(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    real_conn = storage._conn
    store.conn = _CommitFailOnceConnection(real_conn)

    with pytest.raises(RuntimeError, match="injected commit failure"):
        store.verify(draft.memory_id, "reviewer", "call-failed-verify", 1)

    assert real_conn.in_transaction is False
    assert store.get(draft.memory_id).status == "draft"
    storage.upsert(
        "unrelated-after-failure",
        {"content": "independent commit", "memory_type": "experience"},
    )
    assert store.get(draft.memory_id).status == "draft"


def test_synthesis_release_failure_rolls_back_savepoint_and_preserves_outer_transaction(
    lifecycle_db,
):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    real_conn = storage._conn
    real_conn.execute("BEGIN")
    store.conn = _ReleaseFailOnceConnection(real_conn)

    with pytest.raises(RuntimeError, match="injected savepoint release failure"):
        store.verify(draft.memory_id, "reviewer", "call-failed-release", 1)

    assert real_conn.in_transaction is True
    assert store.get(draft.memory_id).status == "draft"
    real_conn.rollback()


def test_create_draft_rejects_empty_project_id(lifecycle_db):
    _storage, store, _sources = lifecycle_db

    with pytest.raises(SynthesisConflict, match="missing_project_id"):
        _create(store, project_id="  ")


def test_create_draft_atomically_stores_memory_control_edges_and_lineage(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    before_version = _version(store.conn)

    artifact = _create(store)

    assert artifact.status == "draft"
    assert artifact.revision == 1
    assert artifact.source_ids == ("source-a", "source-b")
    memory = store.conn.execute(
        "SELECT memory_type, source_class, parent_memory_ids, embedding_text, search_text, "
        "embedding_hash, metadata_json FROM memories WHERE id = ?",
        (artifact.memory_id,),
    ).fetchone()
    assert memory[:2] == ("synthesis", "synthesis")
    assert json.loads(memory[2]) == ["source-a", "source-b"]
    assert memory[3] == memory[4] == artifact.content
    assert memory[5]
    assert json.loads(memory[6])["memory_index"]["policy"] == "legacy"

    edges = store.conn.execute(
        "SELECT target, metadata_json FROM behavior_graph_edges "
        "WHERE source = ? AND relation = 'derived_from' ORDER BY target",
        (artifact.memory_id,),
    ).fetchall()
    assert [row[0] for row in edges] == ["source-a", "source-b"]
    assert all(json.loads(row[1])["observed_content_hash"].startswith("sha256:") for row in edges)
    lineage = store.conn.execute(
        "SELECT parent_memory_id, relation FROM memory_lineage "
        "WHERE memory_id = ? ORDER BY parent_memory_id",
        (artifact.memory_id,),
    ).fetchall()
    assert lineage == [("source-a", "derived_from"), ("source-b", "derived_from")]
    assert _version(store.conn) == before_version + 1


def test_create_draft_persists_explicit_compact_v2_index_material(lifecycle_db, monkeypatch):
    from plastic_promise.core.memory_index import read_persisted_index_material

    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
    raw_content = "RAW SYNTHESIS CONTENT MUST STAY OUT OF COMPACT VECTOR TEXT"

    artifact = _create(
        store,
        content=raw_content,
        metadata={
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "Verified synthesis summary",
            "l1_summary": "Evidence supports a governed conclusion.",
        },
    )
    memory = store._load_memory(artifact.memory_id)
    material = read_persisted_index_material(memory)

    assert material is not None
    assert material.policy == "compact-v2"
    assert "Verified synthesis summary" in material.vector_text
    assert raw_content not in material.vector_text


def test_refresh_preserves_persisted_compact_v2_policy_when_environment_changes(
    lifecycle_db, monkeypatch
):
    from plastic_promise.core.memory_index import read_persisted_index_material

    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
    draft = _create(
        store,
        metadata={
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "Initial compact conclusion",
            "l1_summary": "Initial evidence summary.",
        },
    )
    contested = store.mark_contested(draft.memory_id, "refresh required", draft.revision)
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "legacy")
    replacement_raw = "RAW REPLACEMENT MUST NOT ENTER COMPACT VECTOR TEXT"

    refreshed = store.refresh(
        contested.memory_id,
        replacement_raw,
        ["source-b", "source-c"],
        contested.revision,
        metadata={
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "Refreshed compact conclusion",
            "l1_summary": "Replacement evidence summary.",
        },
        call_id="call-compact-refresh",
    )
    memory = store._load_memory(refreshed.memory_id)
    material = read_persisted_index_material(memory)

    assert material is not None
    assert material.policy == "compact-v2"
    assert "Refreshed compact conclusion" in material.vector_text
    assert replacement_raw not in material.vector_text


@pytest.mark.parametrize(
    "metadata",
    [
        {"domain": "governing", "category": "decision"},
        {
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "Only a new L0 is not enough.",
        },
        {
            "domain": "governing",
            "category": "decision",
            "l1_summary": "Only a new L1 is not enough.",
        },
    ],
)
def test_compact_v2_create_rejects_missing_summary_material(lifecycle_db, monkeypatch, metadata):
    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")

    with pytest.raises(
        SynthesisConflict,
        match="compact_index_material_requires_current_summary",
    ):
        _create(store, metadata=metadata)

    assert store.conn.execute("SELECT COUNT(*) FROM synthesis_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "replacement_metadata",
    [
        {"domain": "governing", "category": "decision"},
        {
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "New L0 must not inherit the previous L1.",
        },
        {
            "domain": "governing",
            "category": "decision",
            "l1_summary": "New L1 must not inherit the previous L0.",
        },
    ],
)
def test_compact_v2_refresh_rejects_stale_inherited_summary_material(
    lifecycle_db, monkeypatch, replacement_metadata
):
    from plastic_promise.core.memory_index import read_persisted_index_material

    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_MEMORY_INDEX_TEXT_POLICY", "compact-v2")
    draft = _create(
        store,
        metadata={
            "domain": "governing",
            "category": "decision",
            "l0_abstract": "Initial compact conclusion",
            "l1_summary": "Initial evidence summary.",
        },
    )
    contested = store.mark_contested(draft.memory_id, "refresh required", draft.revision)
    before_memory = store._load_memory(contested.memory_id)
    before_material = read_persisted_index_material(before_memory)

    with pytest.raises(
        SynthesisConflict,
        match="compact_index_material_requires_current_summary",
    ):
        store.refresh(
            contested.memory_id,
            "Replacement content without a replacement summary.",
            ["source-b", "source-c"],
            contested.revision,
            metadata=replacement_metadata,
            call_id="call-missing-compact-summary",
        )

    after = store.get(contested.memory_id)
    after_material = read_persisted_index_material(store._load_memory(contested.memory_id))
    assert after.status == "contested"
    assert after.revision == contested.revision
    assert after_material == before_material


def test_create_draft_defaults_to_manual_governance(lifecycle_db):
    _storage, store, _sources = lifecycle_db

    artifact = store.create_draft(
        "Manual synthesis does not require an automatic reuse signal.",
        ["source-a", "source-b"],
        synthesis_key="topic:manual-default",
        validity_scope="project:test",
        project_id="project:test",
    )

    assert artifact is not None
    assert artifact.status == "draft"


def test_create_draft_preserves_outer_transaction_ownership_and_rollback(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)
    store.conn.execute("BEGIN")

    artifact = _create(store)

    assert store.conn.in_transaction is True
    assert store.get(artifact.memory_id) is not None
    store.conn.rollback()
    assert store.get(artifact.memory_id) is None
    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version


def test_gate_never_admits_uncommitted_verified_lifecycle_state(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    engine._loaded_memory_version = _version(store.conn)
    engine.canonical_sync_ok = True

    assert engine._gate_memory_ids([draft.memory_id]).items == ()
    store.conn.execute("BEGIN")
    uncommitted = store.verify(draft.memory_id, "reviewer", "call-uncommitted", 1)
    assert uncommitted.status == "verified"
    during = engine._gate_memory_ids([draft.memory_id, "source-a"])
    assert during.items == ("source-a",)
    assert during.degradations[0]["reason"] == "transaction_open"
    store.conn.rollback()
    assert engine._gate_memory_ids([draft.memory_id]).items == ()

    store.conn.execute("BEGIN")
    store.verify(draft.memory_id, "reviewer", "call-committed", 1)
    store.conn.commit()
    assert engine._refresh_canonical_cache_if_changed(force=True) is True
    assert engine._gate_memory_ids([draft.memory_id]).items == (draft.memory_id,)


@pytest.mark.parametrize(
    ("operation", "raises", "reason"),
    [
        ("register", True, "synthesis_memory_reserved"),
        ("store", True, "synthesis_requires_governed_store"),
        ("update", False, ""),
        ("update_fields", False, ""),
        ("delete", False, ""),
    ],
)
def test_generic_crud_reserves_control_associated_synthesis(
    lifecycle_db, operation, raises, reason
):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    verified = store.verify(draft.memory_id, "reviewer", "call-verify", 1)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_rows = {
        table: store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("memories", "synthesis_artifacts", "memory_version")
    }
    before_cache = engine.get_memory_dict(verified.memory_id)

    def mutate():
        if operation == "register":
            return engine.register_memory(
                {
                    "id": verified.memory_id,
                    "content": "UNGOVERNED REGISTER OVERWRITE",
                    "memory_type": "experience",
                }
            )
        if operation == "store":
            record = engine.get_memory(verified.memory_id)
            record.content = "UNGOVERNED STORE OVERWRITE"
            return engine.store_memory(record)
        if operation == "update":
            return engine.update_memory(verified.memory_id, content="UNGOVERNED UPDATE")
        if operation == "update_fields":
            return engine.update_memory_fields(
                verified.memory_id,
                content="UNGOVERNED FIELD UPDATE",
            )
        return engine.delete_memory(verified.memory_id)

    if raises:
        with pytest.raises(SynthesisConflict, match=reason):
            mutate()
    else:
        assert mutate() is False

    assert engine.get_memory_dict(verified.memory_id) == before_cache
    assert {
        table: store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("memories", "synthesis_artifacts", "memory_version")
    } == before_rows


def test_public_memory_boundaries_hide_nonadmitted_synthesis(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    type_only = storage.get("source-c")
    type_only.update(
        id="type-only-synthesis",
        content="TYPE-ONLY SYNTHESIS MUST STAY PRIVATE",
        memory_type="synthesis",
        source_class="synthesis",
    )
    storage.upsert("type-only-synthesis", type_only)
    control_only = storage.get("source-c")
    control_only.update(
        id="control-only-synthesis",
        content="CONTROL-ONLY SYNTHESIS MUST STAY PRIVATE",
        memory_type="experience",
    )
    storage.upsert("control-only-synthesis", control_only)
    _reserve_control_row(storage._conn, "control-only-synthesis")
    engine = _engine_for_storage(storage)
    hidden_ids = {
        draft.memory_id,
        "type-only-synthesis",
        "control-only-synthesis",
    }

    assert hidden_ids.isdisjoint(engine.memory_ids())
    assert engine.memory_count == 3
    for memory_id in hidden_ids:
        assert engine.memory_exists(memory_id) is False
        assert engine.get_memory_dict(memory_id) is None
        assert engine.get_memory(memory_id) is None
    assert engine.get_memories_batch(list(hidden_ids)) == []
    assert engine.get_memory_dict_for_review(draft.memory_id)["content"] == draft.content
    assert engine.get_memory_dict_for_review("type-only-synthesis") is None
    assert engine.get_memory_dict_for_review("control-only-synthesis") is None

    stats = json.loads(engine.memory_stats_json())
    assert stats["total"] == 3
    assert stats["by_type"] == {"experience": 3}
    assert sum(stats["by_category"].values()) == 3
    assert sum(stats["by_tier"].values()) == 3


@pytest.mark.parametrize("reservation_kind", ["type", "control"])
@pytest.mark.parametrize("operation", ["register", "store", "update", "delete", "batch"])
def test_atomic_ordinary_memory_writes_recheck_governance_after_precheck(
    lifecycle_db,
    monkeypatch,
    reservation_kind,
    operation,
):
    storage, store, _sources = lifecycle_db
    target = (
        f"synthesis:race-{reservation_kind}-{operation}"
        if operation in {"register", "store"}
        else "source-c"
    )
    engine = _engine_for_storage(storage)
    runtime_before = dict(engine._memories.get(target, {}))
    reserved = {}

    def stale_precheck(memory_id):
        if memory_id == target and not reserved:
            if operation in {"register", "store"}:
                monkeypatch.setattr(
                    "plastic_promise.core.synthesis.secrets.token_hex",
                    lambda _size: target.removeprefix("synthesis:"),
                )
                artifact = _create(
                    store,
                    content="Canonical synthesis reservation created inside the governed store.",
                    synthesis_key=f"reservation:{target}",
                    call_id=f"call-reserve-{reservation_kind}-{operation}",
                )
                assert artifact.memory_id == target
                if reservation_kind == "control":
                    storage._conn.execute(
                        "UPDATE memories SET memory_type = 'experience', "
                        "source_class = 'experience' WHERE id = ?",
                        (target,),
                    )
                    storage._conn.commit()
            elif reservation_kind == "type":
                if storage.get(target) is None:
                    storage.upsert(
                        target,
                        {
                            "id": target,
                            "content": "canonical type reservation",
                            "memory_type": "synthesis",
                            "source_class": "synthesis",
                        },
                    )
                else:
                    storage._conn.execute(
                        "UPDATE memories SET memory_type = 'synthesis', "
                        "source_class = 'synthesis' WHERE id = ?",
                        (target,),
                    )
                    storage._conn.commit()
            else:
                _reserve_control_row(storage._conn, target)
            reserved["row"] = storage.get(target)
            reserved["version"] = _version(storage._conn)
        return False

    monkeypatch.setattr(engine, "_synthesis_memory_reserved", stale_precheck)

    if operation == "register":
        with pytest.raises(SynthesisConflict, match="synthesis_memory_reserved"):
            engine.register_memory(
                {"id": target, "content": "ordinary overwrite", "memory_type": "experience"}
            )
    elif operation == "store":
        with pytest.raises(SynthesisConflict, match="synthesis_memory_reserved"):
            engine.store_memory(
                MemoryRecord(
                    id=target,
                    content="ordinary overwrite",
                    memory_type="experience",
                )
            )
    elif operation == "update":
        assert engine.update_memory_fields(target, content="ordinary overwrite") is False
    elif operation == "delete":
        assert engine.delete_memory(target) is False
    else:
        assert engine.batch_update([{"id": target, "content": "ordinary overwrite"}]) == 0

    assert storage.get(target) == reserved["row"]
    assert _version(storage._conn) == reserved["version"]
    if runtime_before:
        assert engine._memories[target] == runtime_before
    else:
        assert target not in engine._memories


@pytest.mark.parametrize("reservation_kind", ["type", "control"])
def test_recmem_feedback_decay_evolver_and_gc_leave_governed_memory_unchanged(
    lifecycle_db,
    reservation_kind,
):
    storage, _store, _sources = lifecycle_db
    governed_id = "source-c"
    if reservation_kind == "type":
        storage._conn.execute(
            "UPDATE memories SET memory_type = 'synthesis', source_class = 'synthesis' "
            "WHERE id = ?",
            (governed_id,),
        )
        storage._conn.commit()
    else:
        _reserve_control_row(storage._conn, governed_id)
    engine = _engine_for_storage(storage)
    rec_mem = RecMem(engine)
    stale_runtime = SoulMemoryRecord(
        content="stale runtime copy must not authorize generic mutation",
        memory_type="experience",
        source="test",
        memory_id=governed_id,
        tier="L1",
        worth_success=0,
        worth_failure=10,
    )
    stale_runtime.last_accessed = "2000-01-01T00:00:00"
    rec_mem._records[governed_id] = stale_runtime
    before_rows = {
        table: storage._conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("memories", "synthesis_artifacts", "memory_version")
    }
    before_record = dict(stale_runtime.__dict__)

    assert rec_mem.forget(governed_id, "ordinary forget") is False
    feedback = rec_mem.apply_feedback(governed_id, "rejected")
    assert feedback["governed"] is True
    assert rec_mem.update_all_decay() == 0
    evolution = EvolveR(rec_mem).evolve_cycle()
    assert evolution["promoted"] == evolution["demoted"] == evolution["decayed"] == 0
    gc_result = MemoryGC(rec_mem).collect(dry_run=False, force=True)
    assert governed_id not in gc_result["candidates"]
    assert gc_result["removed"] == 0
    assert {
        table: storage._conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("memories", "synthesis_artifacts", "memory_version")
    } == before_rows
    assert stale_runtime.__dict__ == before_record


@pytest.mark.parametrize("operation", ["register", "store"])
def test_generic_create_rejects_ungoverned_synthesis_type(operation):
    engine = ContextEngine(use_sqlite=False)

    with pytest.raises(SynthesisConflict, match="synthesis_requires_governed_store"):
        if operation == "register":
            engine.register_memory(
                {
                    "id": "orphan-register",
                    "content": "Ungoverned synthesis must not enter runtime.",
                    "memory_type": "synthesis",
                }
            )
        else:
            engine.store_memory(
                MemoryRecord(
                    id="orphan-store",
                    content="Ungoverned synthesis must not enter runtime.",
                    memory_type="synthesis",
                )
            )

    assert engine.memory_count == 0


def test_runtime_synthesis_without_control_cannot_be_washed_to_ordinary(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    orphan = storage.get("source-c")
    orphan.update(
        id="runtime-orphan",
        content="A canonical orphan synthesis row remains reserved.",
        memory_type="synthesis",
        source_class="synthesis",
    )
    storage.upsert("runtime-orphan", orphan)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())

    assert engine.update_memory_fields("runtime-orphan", memory_type="experience") is False
    assert engine._get_memory_dict_unchecked("runtime-orphan")["memory_type"] == "synthesis"
    assert storage.get("runtime-orphan")["memory_type"] == "synthesis"


def test_batch_update_skips_reserved_synthesis_and_updates_ordinary_after_commit(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    verified = store.verify(draft.memory_id, "reviewer", "call-batch-verify", 1)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    synthesis_before = engine.get_memory_dict(verified.memory_id)

    count = engine.batch_update(
        [
            {"id": verified.memory_id, "content": "UNGOVERNED BATCH OVERWRITE"},
            {"id": "source-a", "domain": "batch-updated"},
        ]
    )

    assert count == 1
    assert engine.get_memory_dict(verified.memory_id) == synthesis_before
    assert storage.get(verified.memory_id) == synthesis_before
    assert engine.get_memory_dict("source-a")["domain"] == "batch-updated"
    assert storage.get("source-a")["domain"] == "batch-updated"


def test_generic_graph_crud_cannot_mutate_governed_derived_edges(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    verified = store.verify(draft.memory_id, "reviewer", "call-graph-owner", 1)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_edges = storage._conn.execute(
        "SELECT id, source, target, relation, metadata_json "
        "FROM behavior_graph_edges WHERE source = ? AND relation = 'derived_from' "
        "ORDER BY target",
        (verified.memory_id,),
    ).fetchall()
    before_version = _version(storage._conn)

    assert (
        engine.remove_graph_edge(
            verified.memory_id,
            "source-a",
            "derived_from",
        )
        == 0
    )
    assert (
        engine.add_graph_edge(
            verified.memory_id,
            "source-extra",
            "derived_from",
        )
        is False
    )

    after_edges = storage._conn.execute(
        "SELECT id, source, target, relation, metadata_json "
        "FROM behavior_graph_edges WHERE source = ? AND relation = 'derived_from' "
        "ORDER BY target",
        (verified.memory_id,),
    ).fetchall()
    assert after_edges == before_edges
    assert _version(storage._conn) == before_version


@pytest.mark.parametrize("reservation_kind", ["type", "control"])
def test_atomic_graph_crud_blocks_every_edge_touching_governed_memory(
    lifecycle_db,
    monkeypatch,
    reservation_kind,
):
    storage, _store, _sources = lifecycle_db
    governed_id = "source-c"
    if reservation_kind == "type":
        storage._conn.execute(
            "UPDATE memories SET memory_type = 'synthesis', source_class = 'synthesis' "
            "WHERE id = ?",
            (governed_id,),
        )
        storage._conn.commit()
    else:
        _reserve_control_row(storage._conn, governed_id)
    existing = {
        "id": f"edge:guarded:{reservation_kind}",
        "from": governed_id,
        "to": "source-a",
        "relation": "supports",
        "weight": 0.7,
    }
    storage.upsert_graph_edge(existing)
    engine = _engine_for_storage(storage)
    engine._graph_edges = list(storage.iter_graph_edges())
    before_rows = storage._conn.execute(
        "SELECT id, source, target, relation, weight FROM behavior_graph_edges ORDER BY id"
    ).fetchall()
    before_version = _version(storage._conn)

    # Simulate a stale Python-side precheck; the SQLite statement must still own the guard.
    monkeypatch.setattr(engine, "_synthesis_memory_reserved", lambda _memory_id: False)

    assert engine.add_graph_edge(governed_id, "source-b", relation="supports") is False
    assert engine.add_graph_edge("source-b", governed_id, relation="references") is False
    assert engine.remove_graph_edge(governed_id, "source-a", "supports") == 0
    assert engine.has_graph_edge(existing) is False
    assert (
        storage._conn.execute(
            "SELECT id, source, target, relation, weight FROM behavior_graph_edges ORDER BY id"
        ).fetchall()
        == before_rows
    )
    assert _version(storage._conn) == before_version


@pytest.mark.parametrize("reservation_kind", ["type", "control"])
def test_atomic_register_entity_cannot_replace_governed_graph_node(
    lifecycle_db,
    monkeypatch,
    reservation_kind,
):
    storage, _store, _sources = lifecycle_db
    node_id = "memory:governed-node"
    reserved = storage.get("source-c")
    reserved.update(
        id=node_id,
        memory_type="synthesis" if reservation_kind == "type" else "experience",
        source_class="synthesis" if reservation_kind == "type" else "experience",
    )
    storage.upsert(node_id, reserved)
    storage.upsert_graph_node(
        node_id,
        {
            "type": "memory",
            "name": "governed synthesis",
            "description": "",
            "source_kind": "synthesis",
            "metadata": {"governed": True},
        },
    )
    if reservation_kind == "control":
        _reserve_control_row(storage._conn, node_id)
    engine = _engine_for_storage(storage)
    engine._graph_nodes = dict(storage.iter_graph_nodes())
    before_node = storage._conn.execute(
        "SELECT * FROM behavior_graph_nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    before_version = _version(storage._conn)

    with pytest.raises(SynthesisConflict, match="synthesis_graph_node_reserved"):
        engine.register_entity(
            "memory",
            "governed-node",
            "UNGOVERNED NODE OVERWRITE",
            "UNGOVERNED GRAPH BODY",
        )

    # The SQLite statement must remain authoritative after a stale precheck.
    monkeypatch.setattr(engine, "_synthesis_memory_reserved", lambda _memory_id: False)

    with pytest.raises(SynthesisConflict, match="synthesis_graph_node_reserved"):
        engine.register_entity(
            "memory",
            "governed-node",
            "UNGOVERNED NODE OVERWRITE",
            "UNGOVERNED GRAPH BODY",
        )

    assert (
        storage._conn.execute(
            "SELECT * FROM behavior_graph_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        == before_node
    )
    assert engine._graph_nodes[node_id]["source_kind"] == "synthesis"
    assert engine._graph_nodes[node_id]["metadata"] == {"governed": True}
    assert _version(storage._conn) == before_version


@pytest.mark.parametrize("reservation_kind", ["type", "control"])
def test_code_memory_graph_registration_cannot_touch_governed_endpoint(
    lifecycle_db,
    monkeypatch,
    reservation_kind,
):
    storage, _store, _sources = lifecycle_db
    node_id = "file:governed.py"
    reserved = storage.get("source-c")
    reserved.update(
        id=node_id,
        memory_type="synthesis" if reservation_kind == "type" else "experience",
        source_class="synthesis" if reservation_kind == "type" else "experience",
    )
    storage.upsert(node_id, reserved)
    storage.upsert_graph_node(
        node_id,
        {
            "type": "memory",
            "name": "governed synthesis",
            "description": "",
            "source_kind": "synthesis",
            "metadata": {"governed": True},
        },
    )
    if reservation_kind == "control":
        _reserve_control_row(storage._conn, node_id)
    engine = _engine_for_storage(storage)
    engine._graph_nodes = dict(storage.iter_graph_nodes())
    engine._graph_edges = list(storage.iter_graph_edges())
    before_nodes = storage._conn.execute(
        "SELECT * FROM behavior_graph_nodes ORDER BY id"
    ).fetchall()
    before_edges = storage._conn.execute(
        "SELECT * FROM behavior_graph_edges ORDER BY id"
    ).fetchall()
    before_version = _version(storage._conn)

    class CodeIndex:
        nodes = [
            {
                "id": node_id,
                "type": "file",
                "name": "UNGOVERNED CODE NODE",
                "description": "UNGOVERNED INDEX BODY",
                "source_kind": "code_memory",
                "metadata": {},
            }
        ]
        edges = [
            {
                "from": node_id,
                "to": "source-a",
                "relation": "references",
                "weight": 0.7,
                "source_kind": "code_memory",
            },
            {
                "from": "source-a",
                "to": node_id,
                "relation": "references",
                "weight": 0.7,
                "source_kind": "code_memory",
            },
        ]

    engine._register_code_memory_graph(CodeIndex())

    monkeypatch.setattr(engine, "_synthesis_memory_reserved", lambda _memory_id: False)

    engine._register_code_memory_graph(CodeIndex())

    assert (
        storage._conn.execute("SELECT * FROM behavior_graph_nodes ORDER BY id").fetchall()
        == before_nodes
    )
    assert (
        storage._conn.execute("SELECT * FROM behavior_graph_edges ORDER BY id").fetchall()
        == before_edges
    )
    assert engine._graph_nodes[node_id]["source_kind"] == "synthesis"
    assert engine._graph_edges == list(storage.iter_graph_edges())
    assert _version(storage._conn) == before_version


def test_load_graph_with_sqlite_preserves_canonical_governed_subgraph(lifecycle_db):
    storage, store, _sources = lifecycle_db
    verified = store.verify(
        _create(store).memory_id,
        "reviewer",
        "call-load-graph-owner",
        1,
    )
    engine = _engine_for_storage(storage)
    engine._graph_nodes = dict(storage.iter_graph_nodes())
    engine._graph_edges = list(storage.iter_graph_edges())
    before_node = storage._conn.execute(
        "SELECT * FROM behavior_graph_nodes WHERE id = ?",
        (verified.memory_id,),
    ).fetchone()
    before_edges = storage._conn.execute(
        "SELECT * FROM behavior_graph_edges WHERE source = ? ORDER BY id",
        (verified.memory_id,),
    ).fetchall()
    before_version = _version(storage._conn)

    engine.load_graph(
        {
            "nodes": {
                "attacker": {
                    "type": "memory",
                    "name": "replacement",
                    "description": "replacement graph",
                }
            },
            "edges": [],
        }
    )

    public_node = engine.get_graph_node(verified.memory_id)
    assert public_node is not None
    assert public_node["description"] == ""
    assert {
        edge["to"]
        for edge in engine.list_graph_edges("derived_from")
        if edge["from"] == verified.memory_id
    } == {"source-a", "source-b"}
    assert "attacker" not in engine._graph_nodes
    assert (
        storage._conn.execute(
            "SELECT * FROM behavior_graph_nodes WHERE id = ?",
            (verified.memory_id,),
        ).fetchone()
        == before_node
    )
    assert (
        storage._conn.execute(
            "SELECT * FROM behavior_graph_edges WHERE source = ? ORDER BY id",
            (verified.memory_id,),
        ).fetchall()
        == before_edges
    )
    assert _version(storage._conn) == before_version


def test_public_graph_snapshot_fails_closed_when_admission_version_changes(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    raw_conn = storage._conn

    def raced_engine():
        storage._conn = raw_conn
        raw_conn.execute(
            "UPDATE memories SET memory_type = 'experience', source_class = 'experience' "
            "WHERE id = 'source-a'"
        )
        raw_conn.commit()
        storage.upsert_graph_node(
            "source-a",
            {
                "type": "memory",
                "name": "race candidate",
                "description": "DRAFT BODY LEAK",
                "source_kind": "",
                "metadata": {},
            },
        )
        engine = _engine_for_storage(storage)
        engine._graph_nodes = dict(storage.iter_graph_nodes())

        class VersionRaceConnection:
            def __init__(self, conn):
                self._conn = conn
                self._version_reads = 0
                self._tripped = False

            @property
            def in_transaction(self):
                return self._conn.in_transaction

            def execute(self, sql, *args, **kwargs):
                if str(sql).strip().startswith("SELECT version FROM memory_version"):
                    self._version_reads += 1
                    if self._version_reads == 3 and not self._tripped:
                        self._tripped = True
                        self._conn.execute(
                            "UPDATE memories SET memory_type = 'synthesis', source_class = 'synthesis' "
                            "WHERE id = 'source-a'"
                        )
                        self._conn.execute("UPDATE memory_version SET version = version + 1")
                        self._conn.commit()
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        storage._conn = VersionRaceConnection(raw_conn)
        return engine

    for public_read in (
        lambda engine: engine.get_graph(),
        lambda engine: engine.get_graph_node("source-a"),
        lambda engine: engine.list_graph_nodes(),
        lambda engine: engine.query_graph("full_graph"),
    ):
        engine = raced_engine()
        result = public_read(engine)
        assert "DRAFT BODY LEAK" not in str(result)

    storage._conn = raw_conn


def test_ordinary_field_update_cannot_create_synthesis_orphan(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before = engine.get_memory_dict("source-a")

    assert engine.update_memory_fields("source-a", memory_type="synthesis") is False

    assert engine.get_memory_dict("source-a") == before
    assert storage.get("source-a") == before


def test_ordinary_batch_update_cannot_create_synthesis_orphan(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before = engine.get_memory_dict("source-a")

    assert engine.batch_update([{"id": "source-a", "memory_type": "SYNTHESIS"}]) == 0

    assert engine.get_memory_dict("source-a") == before
    assert storage.get("source-a") == before


@pytest.mark.parametrize("operation", ["register", "update"])
def test_canonical_synthesis_orphan_is_reserved_with_stale_runtime(
    lifecycle_db,
    operation,
):
    storage, _store, _sources = lifecycle_db
    memory_id = f"canonical-orphan-{operation}"
    storage.upsert(
        memory_id,
        {
            "id": memory_id,
            "content": "canonical synthesis orphan",
            "memory_type": "synthesis",
            "source_class": "synthesis",
            "project_id": "project:test",
        },
    )
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = {}

    if operation == "register":
        with pytest.raises(SynthesisConflict, match="synthesis_memory_reserved"):
            engine.register_memory(
                {
                    "id": memory_id,
                    "content": "ordinary overwrite",
                    "memory_type": "experience",
                }
            )
    else:
        engine._memories[memory_id] = {
            "id": memory_id,
            "content": "stale ordinary runtime",
            "memory_type": "experience",
        }
        assert engine.update_memory_fields(memory_id, content="ordinary overwrite") is False

    assert storage.get(memory_id)["memory_type"] == "synthesis"
    assert storage.get(memory_id)["content"] == "canonical synthesis orphan"


def test_batch_update_persistence_failure_rolls_back_runtime_and_sqlite(lifecycle_db, monkeypatch):
    storage, _store, _sources = lifecycle_db
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_runtime = {
        memory_id: engine.get_memory_dict(memory_id) for memory_id in ("source-a", "source-b")
    }
    before_rows = {memory_id: storage.get(memory_id) for memory_id in ("source-a", "source-b")}
    before_version = _version(storage._conn)
    original_patch = storage.patch_ordinary
    calls = 0

    def fail_second(memory_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected batch persistence failure")
        return original_patch(memory_id, **kwargs)

    monkeypatch.setattr(storage, "patch_ordinary", fail_second)

    with pytest.raises(RuntimeError, match="injected batch persistence failure"):
        engine.batch_update(
            [
                {"id": "source-a", "domain": "dirty-a"},
                {"id": "source-b", "domain": "dirty-b"},
            ]
        )

    assert {
        memory_id: engine.get_memory_dict(memory_id) for memory_id in ("source-a", "source-b")
    } == before_runtime
    assert {
        memory_id: storage.get(memory_id) for memory_id in ("source-a", "source-b")
    } == before_rows
    assert _version(storage._conn) == before_version


def test_manual_batch_rollback_restores_runtime_and_closes_transaction(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_runtime = engine.get_memory_dict("source-a")
    before_persisted = storage.get("source-a")

    engine.begin_batch()
    assert engine.update_memory_fields("source-a", domain="manual-dirty") is True
    assert engine.get_memory_dict("source-a") == before_runtime
    engine.rollback_batch()

    assert storage._conn.in_transaction is False
    assert engine.get_memory_dict("source-a") == before_runtime
    assert storage.get("source-a") == before_persisted


def test_manual_batch_commit_failure_restores_runtime_and_sqlite(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    real_conn = storage._conn
    storage._conn = _CommitFailOnceConnection(real_conn)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_runtime = engine.get_memory_dict("source-a")

    engine.begin_batch()
    assert engine.update_memory_fields("source-a", domain="commit-dirty") is True
    with pytest.raises(RuntimeError, match="injected commit failure"):
        engine.commit_batch()

    assert real_conn.in_transaction is False
    assert engine.get_memory_dict("source-a") == before_runtime
    assert storage.get("source-a") == before_runtime


def test_manual_batch_cannot_commit_after_caught_database_write_failure(lifecycle_db):
    storage, _store, _sources = lifecycle_db
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before_runtime = engine.get_memory_dict("source-a")

    engine.begin_batch()
    assert engine.update_memory_fields("source-a", domain="rollback-only-dirty") is True
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        storage._execute_write("INSERT INTO missing_batch_table VALUES (1)")
    with pytest.raises(RuntimeError, match="storage_batch_rollback_only"):
        engine.commit_batch()

    assert storage._conn.in_transaction is False
    assert engine.get_memory_dict("source-a") == before_runtime
    assert storage.get("source-a") == before_runtime


def test_increment_field_rejects_reserved_synthesis(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    engine._memories = dict(storage.iter_all())
    before = engine._get_memory_dict_unchecked(draft.memory_id)

    assert engine.increment_field(draft.memory_id, "access_count") is False
    assert engine.get_memory_dict(draft.memory_id) is None
    assert engine._get_memory_dict_unchecked(draft.memory_id) == before
    assert storage.get(draft.memory_id) == before


def test_generic_crud_fails_closed_when_reservation_lookup_errors():
    class FailingConnection:
        def execute(self, statement, parameters=()):
            raise RuntimeError("injected reservation lookup failure")

    class TrackingStorage:
        def __init__(self):
            self._conn = FailingConnection()
            self.upsert_called = False

        def upsert(self, memory_id, data):
            self.upsert_called = True

    engine = ContextEngine(use_sqlite=False)
    storage = TrackingStorage()
    engine._sqlite = storage
    engine._memories = {
        "unknown-reservation": {
            "id": "unknown-reservation",
            "content": "runtime type cannot prove this row is ordinary",
            "memory_type": "experience",
        }
    }

    assert engine.update_memory_fields("unknown-reservation", content="unsafe") is False
    assert storage.upsert_called is False
    assert engine._get_memory_dict_unchecked("unknown-reservation")["content"] == (
        "runtime type cannot prove this row is ordinary"
    )


def test_record_memory_lineage_round_trips_parent_without_committing(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    store.conn.execute("BEGIN")

    lineage_id = record_memory_lineage(
        store.conn,
        memory_id="source-a",
        parent_memory_id="source-b",
        relation="derived_from",
        call_id="call-lineage",
        metadata={"revision": 7},
    )

    assert lineage_id > 0
    assert store.conn.in_transaction is True
    row = store.conn.execute(
        "SELECT parent_memory_id, relation, metadata_json FROM memory_lineage WHERE lineage_id = ?",
        (lineage_id,),
    ).fetchone()
    assert row[:2] == ("source-b", "derived_from")
    assert json.loads(row[2]) == {"revision": 7}
    store.conn.rollback()
    assert (
        store.conn.execute(
            "SELECT 1 FROM memory_lineage WHERE lineage_id = ?", (lineage_id,)
        ).fetchone()
        is None
    )


def test_create_draft_injected_edge_failure_rolls_back_every_table(lifecycle_db, monkeypatch):
    _storage, store, _sources = lifecycle_db
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)
    original = store._insert_derived_edge
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected edge failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_insert_derived_edge", fail_second)

    with pytest.raises(RuntimeError, match="injected edge failure"):
        _create(store)

    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("off", "synthesis_artifacts_disabled"), ("future", "unknown_synthesis_mode")],
)
def test_create_draft_modes_fail_closed_without_writes(lifecycle_db, monkeypatch, mode, reason):
    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", mode)
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)

    with pytest.raises(SynthesisConflict, match=reason):
        _create(store)

    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version


def test_shadow_validates_but_writes_only_bounded_hash_diagnostics(lifecycle_db, monkeypatch):
    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    record_call_span(
        store.conn,
        call_id="call-shadow",
        project_id="project:test",
        tool_name="memory_store",
        metadata={"kept": True},
    )
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)
    secret_content = "A supported conclusion whose plaintext must not enter shadow diagnostics."

    result = _create(store, content=secret_content, call_id="call-shadow")

    assert result is None
    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version
    raw = store.conn.execute(
        "SELECT metadata_json FROM call_spans WHERE call_id = 'call-shadow'"
    ).fetchone()[0]
    metadata = json.loads(raw)
    diagnostic = metadata["synthesis_shadow"]
    assert metadata["kept"] is True
    assert diagnostic["content_hash"].startswith("sha256:")
    assert diagnostic["source_count"] == 2
    assert diagnostic["reason"] == "eligible"
    assert secret_content not in raw
    assert "source-a" not in raw


def test_shadow_rejection_records_reason_code_without_source_identifier(lifecycle_db, monkeypatch):
    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "shadow")
    record_call_span(
        store.conn,
        call_id="call-shadow-reject",
        project_id="project:test",
        tool_name="memory_store",
    )
    missing_id = "sensitive-source-identifier"
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)

    with pytest.raises(SynthesisConflict, match="source_missing"):
        _create(
            store,
            source_ids=["source-a", missing_id],
            call_id="call-shadow-reject",
        )

    raw = store.conn.execute(
        "SELECT metadata_json FROM call_spans WHERE call_id = 'call-shadow-reject'"
    ).fetchone()[0]
    assert json.loads(raw)["synthesis_shadow"]["reason"] == "source_missing"
    assert missing_id not in raw
    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"source_ids": ["source-a", "source-a"]}, "insufficient_distinct_sources"),
        ({"source_ids": ["source-a", "missing"]}, "source_missing"),
        ({"validity_scope": "  "}, "missing_validity_scope"),
        ({"synthesis_key": "  "}, "missing_synthesis_key"),
        ({"content": "  "}, "missing_synthesis_content"),
        ({"visibility": "global"}, "synthesis_visibility_widened"),
    ],
)
def test_create_draft_rejects_invalid_sources_scope_key_content_and_visibility(
    lifecycle_db, overrides, reason
):
    _storage, store, _sources = lifecycle_db
    before = _table_counts(store.conn), _version(store.conn)

    with pytest.raises(SynthesisConflict, match=reason):
        _create(store, **overrides)

    assert (_table_counts(store.conn), _version(store.conn)) == before


def test_create_draft_rejects_unavailable_or_invalidated_sources(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    store.conn.execute(
        "UPDATE memories SET tags = ? WHERE id = 'source-a'", (json.dumps(["status:wrong"]),)
    )
    store.conn.commit()
    with pytest.raises(SynthesisConflict, match="source_unavailable"):
        _create(store)

    store.conn.execute("UPDATE memories SET tags = '[]' WHERE id = 'source-a'")
    store.conn.execute(
        "INSERT INTO behavior_graph_edges "
        "(id, source, target, relation, metadata_json, updated_at) "
        "VALUES ('supersede-a', 'source-c', 'source-a', 'supersedes', '{}', 'now')"
    )
    store.conn.commit()
    with pytest.raises(SynthesisConflict, match="source_superseded"):
        _create(store)


def test_create_draft_rejects_unverified_synthesis_as_source(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    upstream = _create(store)

    with pytest.raises(SynthesisConflict, match="source_unavailable"):
        _create(
            store,
            source_ids=[upstream.memory_id, "source-c"],
            synthesis_key="topic:derived-from-draft",
        )


def test_create_draft_rejects_control_associated_source_after_type_drift(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    upstream = _create(store, synthesis_key="topic:type-drift-upstream")
    store.conn.execute(
        "UPDATE memories SET memory_type = 'experience' WHERE id = ?",
        (upstream.memory_id,),
    )
    store.conn.commit()
    before = _table_counts(store.conn), _version(store.conn)

    with pytest.raises(SynthesisConflict, match="source_unavailable"):
        _create(
            store,
            source_ids=[upstream.memory_id, "source-c"],
            synthesis_key="topic:type-drift-downstream",
        )

    assert (_table_counts(store.conn), _version(store.conn)) == before
    assert (
        store.conn.execute(
            "SELECT 1 FROM synthesis_artifacts WHERE synthesis_key = ?",
            ("topic:type-drift-downstream",),
        ).fetchone()
        is None
    )


def test_downstream_binds_governed_source_revision_even_when_content_is_unchanged(
    lifecycle_db,
):
    storage, store, _sources = lifecycle_db
    upstream = _create(store, synthesis_key="topic:revision-upstream")
    upstream = store.verify(upstream.memory_id, "reviewer", "call-upstream-v1", 1)
    downstream = _create(
        store,
        source_ids=[upstream.memory_id, "source-c"],
        synthesis_key="topic:revision-downstream",
    )
    downstream = store.verify(downstream.memory_id, "reviewer", "call-downstream-v1", 1)
    edge_metadata = json.loads(
        store.conn.execute(
            "SELECT metadata_json FROM behavior_graph_edges "
            "WHERE source = ? AND target = ? AND relation = 'derived_from'",
            (downstream.memory_id, upstream.memory_id),
        ).fetchone()[0]
    )
    assert edge_metadata["source_revision"] == 1
    before = retrieval.evaluate_synthesis_ids(
        store.conn,
        [downstream.memory_id],
        memory_version=_version(store.conn),
    )
    assert before.items == (downstream.memory_id,)

    store.mark_contested(upstream.memory_id, "evidence set changed", 1)
    store.refresh(
        upstream.memory_id,
        upstream.content,
        ["source-a", "source-c"],
        1,
        automatic=False,
    )
    store.verify(upstream.memory_id, "reviewer", "call-upstream-v2", 2)

    after = retrieval.evaluate_synthesis_ids(
        store.conn,
        [downstream.memory_id],
        memory_version=_version(store.conn),
    )
    assert after.items == ()
    assert after.degradations == (
        {"id": downstream.memory_id, "reason": "source_revision_mismatch"},
    )
    lint_codes = {
        finding["code"]
        for finding in store.lint(project_id="project:test")
        if finding["memory_id"] == downstream.memory_id
    }
    assert "SYNTHESIS_SOURCE_CHANGED" in lint_codes
    storage._conn.commit()


def test_automatic_draft_enforces_compression_reuse_and_non_reflection_source(lifecycle_db):
    storage, store, sources = lifecycle_db
    with pytest.raises(SynthesisConflict, match="missing_reuse_signal"):
        _create(store, automatic=True, reuse_signal=False)

    long_content = (sources["source-a"]["content"] + sources["source-b"]["content"]) * 2
    with pytest.raises(SynthesisConflict, match="insufficient_compression"):
        _create(store, automatic=True, reuse_signal=True, content=long_content)

    for source_id in ("source-a", "source-b"):
        source = storage.get(source_id)
        source["memory_type"] = "reflection"
        storage.upsert(source_id, source)
    with pytest.raises(SynthesisConflict, match="reflection_only_sources"):
        _create(store, automatic=True, reuse_signal=True)

    artifact = _create(
        store,
        automatic=True,
        reuse_signal=True,
        audit_synthesis=True,
        synthesis_key="topic:audit",
    )
    assert artifact.status == "draft"


def test_verify_requires_actor_call_matching_snapshots_and_revision(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    before_version = _version(store.conn)

    for actor, call_id in (("", "call-verify"), ("reviewer", "")):
        with pytest.raises(SynthesisConflict, match="missing_verification_evidence"):
            store.verify(
                draft.memory_id,
                actor=actor,
                call_id=call_id,
                expected_revision=1,
            )
    with pytest.raises(SynthesisConflict, match="revision_conflict"):
        store.verify(
            draft.memory_id,
            actor="reviewer",
            call_id="call-verify",
            expected_revision=99,
        )
    assert _version(store.conn) == before_version

    verified = store.verify(
        draft.memory_id,
        actor="reviewer",
        call_id="call-verify",
        expected_revision=1,
    )
    assert verified.status == "verified"
    assert verified.revision == 1
    assert verified.verified_by_actor == "reviewer"
    assert _version(store.conn) == before_version + 1


def test_verify_fails_when_source_snapshot_changed(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    store.conn.execute("UPDATE memories SET content = content || ' changed' WHERE id = 'source-a'")
    store.conn.commit()
    before = _table_counts(store.conn), _version(store.conn)

    with pytest.raises(SynthesisConflict, match="SYNTHESIS_SOURCE_CHANGED"):
        store.verify(
            draft.memory_id,
            actor="reviewer",
            call_id="call-verify",
            expected_revision=1,
        )

    assert (_table_counts(store.conn), _version(store.conn)) == before
    assert store.get(draft.memory_id).status == "draft"


def test_verify_rejects_source_that_became_unavailable_without_hash_drift(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    store.conn.execute(
        "UPDATE memories SET tags = ? WHERE id = 'source-a'",
        (json.dumps(["status:wrong"]),),
    )
    store.conn.commit()
    before_version = _version(store.conn)

    with pytest.raises(SynthesisConflict, match="SYNTHESIS_SOURCE_CHANGED"):
        store.verify(draft.memory_id, "reviewer", "call-verify", 1)

    assert store.get(draft.memory_id).status == "draft"
    assert {finding["code"] for finding in store.lint(memory_id=draft.memory_id)} == {
        "SYNTHESIS_SOURCE_CHANGED"
    }
    assert _version(store.conn) == before_version


def test_forbidden_state_transitions_fail_without_version_change(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    before_version = _version(store.conn)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.mark_stale(draft.memory_id, "draft is not verified", 1)
    assert _version(store.conn) == before_version

    contested = store.mark_contested(draft.memory_id, "disputed", 1)
    contested_version = _version(store.conn)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.verify(contested.memory_id, "reviewer", "call-v", 1)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.mark_stale(contested.memory_id, "invalid", 1)
    assert _version(store.conn) == contested_version

    refreshed = store.refresh(
        contested.memory_id,
        "A resolved conclusion based on current independent evidence.",
        ["source-b", "source-c"],
        1,
    )
    verified = store.verify(refreshed.memory_id, "reviewer", "call-v2", 2)
    stale = store.mark_stale(verified.memory_id, "source drift", 2)
    stale_version = _version(store.conn)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.verify(stale.memory_id, "reviewer", "call-v3", 2)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.mark_contested(stale.memory_id, "invalid", 2)
    assert _version(store.conn) == stale_version


def test_allowed_state_transitions_keep_revision_until_refresh(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    verified = store.verify(draft.memory_id, "reviewer", "call-v", 1)
    stale = store.mark_stale(verified.memory_id, "source_changed", 1)
    assert (verified.status, stale.status, stale.revision) == ("verified", "stale", 1)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.verify(stale.memory_id, "reviewer", "call-v2", 1)
    refreshed = store.refresh(
        stale.memory_id,
        "A refreshed conclusion supported by current evidence.",
        ["source-b", "source-c"],
        1,
        call_id="call-refresh",
    )
    assert (refreshed.status, refreshed.revision) == ("draft", 2)


def test_draft_and_verified_can_be_contested_but_other_transitions_are_forbidden(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    contested = store.mark_contested(draft.memory_id, "review dispute", 1)
    assert (contested.status, contested.revision) == ("contested", 1)
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.mark_stale(contested.memory_id, "not allowed", 1)

    refreshed = store.refresh(
        contested.memory_id,
        "A resolved conclusion supported by replacement evidence.",
        ["source-b", "source-c"],
        1,
        call_id="call-resolve",
    )
    with pytest.raises(SynthesisConflict, match="transition_not_allowed"):
        store.refresh(
            refreshed.memory_id,
            "Cannot refresh a current draft.",
            ["source-a", "source-b"],
            2,
        )
    verified = store.verify(refreshed.memory_id, "reviewer", "call-v", 2)
    contested_again = store.mark_contested(verified.memory_id, "new contradiction", 2)
    assert (contested_again.status, contested_again.revision) == ("contested", 2)


def test_cas_conflict_leaves_control_edges_lineage_memory_and_version_unchanged(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    before_counts = _table_counts(store.conn)
    before_version = _version(store.conn)
    before_memory = store.conn.execute(
        "SELECT * FROM memories WHERE id = ?", (draft.memory_id,)
    ).fetchone()

    with pytest.raises(SynthesisConflict, match="revision_conflict"):
        store.mark_contested(draft.memory_id, "stale caller", 7)

    assert _table_counts(store.conn) == before_counts
    assert _version(store.conn) == before_version
    assert (
        store.conn.execute("SELECT * FROM memories WHERE id = ?", (draft.memory_id,)).fetchone()
        == before_memory
    )


def test_refresh_replaces_content_and_current_evidence_and_clears_verification(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    verified = store.verify(draft.memory_id, "reviewer", "call-v", 1)
    stale = store.mark_stale(verified.memory_id, "source changed", 1)

    refreshed = store.refresh(
        stale.memory_id,
        "Replacement evidence now supports this revised conclusion.",
        ["source-b", "source-c"],
        1,
        validity_scope="project:test/revised",
        actor="codex",
        call_id="call-refresh",
    )

    assert refreshed.status == "draft"
    assert refreshed.revision == 2
    assert refreshed.validity_scope == "project:test/revised"
    assert refreshed.verified_by_actor == ""
    assert refreshed.last_verified_at == ""
    assert refreshed.stale_reason == ""
    assert (
        store.conn.execute(
            "SELECT content FROM memories WHERE id = ?", (refreshed.memory_id,)
        ).fetchone()[0]
        == refreshed.content
    )
    targets = store.conn.execute(
        "SELECT target FROM behavior_graph_edges "
        "WHERE source = ? AND relation = 'derived_from' ORDER BY target",
        (refreshed.memory_id,),
    ).fetchall()
    assert targets == [("source-b",), ("source-c",)]
    assert (
        store.conn.execute(
            "SELECT description FROM behavior_graph_nodes WHERE id = ?",
            (refreshed.memory_id,),
        ).fetchone()[0]
        == ""
    )


def test_detached_refresh_preserves_persisted_embedding_model_binding(
    lifecycle_db,
    monkeypatch,
):
    from plastic_promise.core.memory_index import (
        LEGACY_POLICY,
        build_index_material,
        read_persisted_index_material,
    )

    _storage, store, _sources = lifecycle_db
    monkeypatch.setenv("EMBED_MODEL", "persisted-model-a")
    draft = _create(store)
    contested = store.mark_contested(draft.memory_id, "refresh required", 1)
    refreshed_content = "Replacement evidence supports the new exact conclusion."

    monkeypatch.setenv("EMBED_MODEL", "unrelated-ambient-model-b")
    detached_store = SynthesisStore(store.conn)
    refreshed = detached_store.refresh(
        contested.memory_id,
        refreshed_content,
        ["source-b", "source-c"],
        1,
        call_id="call-detached-refresh",
    )

    cursor = store.conn.execute(
        "SELECT content, embedding_text, embedding_hash, search_text, metadata_json "
        "FROM memories WHERE id = ?",
        (refreshed.memory_id,),
    )
    row = cursor.fetchone()
    memory = dict(zip((column[0] for column in cursor.description), row, strict=True))
    material = read_persisted_index_material(
        memory,
        model_name="persisted-model-a",
    )
    expected = build_index_material(
        {"content": refreshed_content},
        policy=LEGACY_POLICY,
        model_name="persisted-model-a",
    )

    assert material == expected
    assert json.loads(memory["metadata_json"])["memory_index"]["model_name"] == (
        "persisted-model-a"
    )
    assert (
        detached_store.verify(
            refreshed.memory_id,
            "reviewer",
            "call-detached-verify",
            2,
        ).status
        == "verified"
    )


def test_refresh_type_drift_rolls_back_control_edges_lineage_and_version(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    contested = store.mark_contested(draft.memory_id, "review dispute", 1)
    store.conn.execute(
        "UPDATE memories SET memory_type = 'experience' WHERE id = ?",
        (contested.memory_id,),
    )
    store.conn.commit()

    def snapshot():
        return {
            "memory": store.conn.execute(
                "SELECT * FROM memories WHERE id = ?", (contested.memory_id,)
            ).fetchone(),
            "control": store.conn.execute(
                "SELECT * FROM synthesis_artifacts WHERE memory_id = ?",
                (contested.memory_id,),
            ).fetchone(),
            "edges": store.conn.execute(
                "SELECT * FROM behavior_graph_edges WHERE source = ? ORDER BY id",
                (contested.memory_id,),
            ).fetchall(),
            "lineage": store.conn.execute(
                "SELECT * FROM memory_lineage WHERE memory_id = ? ORDER BY lineage_id",
                (contested.memory_id,),
            ).fetchall(),
            "version": _version(store.conn),
        }

    before = snapshot()

    with pytest.raises(SynthesisConflict, match="synthesis_memory_type_mismatch"):
        store.refresh(
            contested.memory_id,
            "This replacement must not partially commit.",
            ["source-b", "source-c"],
            1,
            call_id="call-type-drift",
        )

    assert snapshot() == before


def test_refresh_atomically_updates_project_visibility_and_binding(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    contested = store.mark_contested(draft.memory_id, "project move", 1)
    store.conn.execute(
        "UPDATE memories SET visibility = 'global' WHERE id IN ('source-b', 'source-c')"
    )
    store.conn.commit()

    refreshed = store.refresh(
        contested.memory_id,
        "Current globally visible evidence supports the moved project conclusion.",
        ["source-b", "source-c"],
        1,
        project_id="project:other",
        visibility="shared",
        call_id="call-project-move",
    )

    control_metadata = json.loads(
        store.conn.execute(
            "SELECT metadata_json FROM synthesis_artifacts WHERE memory_id = ?",
            (refreshed.memory_id,),
        ).fetchone()[0]
    )
    memory_metadata = json.loads(
        store.conn.execute(
            "SELECT metadata_json FROM memories WHERE id = ?",
            (refreshed.memory_id,),
        ).fetchone()[0]
    )
    assert (refreshed.project_id, refreshed.visibility) == ("project:other", "shared")
    assert control_metadata["project_id"] == "project:other"
    assert control_metadata["visibility"] == "shared"
    assert control_metadata["synthesis_binding"] == memory_metadata["synthesis_binding"]
    assert control_metadata["synthesis_binding_hash"] == memory_metadata["synthesis_binding_hash"]


def test_get_list_and_lint_are_deterministic_and_emit_only_initial_codes(lifecycle_db):
    storage, store, _sources = lifecycle_db
    draft = _create(store)
    assert store.get(draft.memory_id).memory_id == draft.memory_id
    assert [item.memory_id for item in store.list(status="draft", project_id="project:test")] == [
        draft.memory_id
    ]

    # Make the governed artifact exhibit every source/control integrity class.
    store.conn.execute("UPDATE memories SET content = content || ' drift' WHERE id = 'source-a'")
    store.conn.execute("DELETE FROM memories WHERE id = 'source-b'")
    store.conn.execute(
        "UPDATE synthesis_artifacts SET support_count = 99, source_fingerprint = 'bad' "
        "WHERE memory_id = ?",
        (draft.memory_id,),
    )
    store.conn.execute("UPDATE memories SET visibility = 'global' WHERE id = ?", (draft.memory_id,))
    store.conn.execute(
        "INSERT INTO behavior_graph_edges "
        "(id, source, target, relation, metadata_json, updated_at) "
        "VALUES ('sup-lint', 'source-c', 'source-a', 'supersedes', '{}', 'now')"
    )
    store.conn.execute(
        "INSERT INTO behavior_graph_edges "
        "(id, source, target, relation, metadata_json, updated_at) "
        "VALUES ('contra-lint', 'source-c', ?, 'contradicts', '{}', 'now')",
        (draft.memory_id,),
    )
    now = "2026-07-10T00:00:00Z"
    store.conn.execute(
        "INSERT INTO synthesis_artifacts "
        "(memory_id, synthesis_key, status, metadata_json, created_at, updated_at) "
        "VALUES ('missing-control-memory', 'orphan:control', 'draft', ?, ?, ?)",
        (json.dumps({"project_id": "project:test"}), now, now),
    )
    orphan = storage.get("source-c")
    orphan.update(
        id="orphan-synthesis",
        content="An orphan synthesis memory without a control record.",
        memory_type="synthesis",
        source_class="synthesis",
    )
    storage.upsert("orphan-synthesis", orphan)
    store.conn.commit()
    content_before = store.conn.execute(
        "SELECT content FROM memories WHERE id = ?", (draft.memory_id,)
    ).fetchone()[0]

    first = store.lint(project_id="project:test")
    second = store.lint(project_id="project:test")

    assert first == second
    codes = {finding["code"] for finding in first}
    assert codes == {
        "SYNTHESIS_SOURCE_MISSING",
        "SYNTHESIS_SOURCE_CHANGED",
        "SYNTHESIS_SOURCE_SUPERSEDED",
        "SYNTHESIS_SUPPORT_MISMATCH",
        "SYNTHESIS_FINGERPRINT_MISMATCH",
        "SYNTHESIS_CONTRADICTION_OPEN",
        "SYNTHESIS_VISIBILITY_WIDENED",
        "SYNTHESIS_ORPHAN_CONTROL",
        "SYNTHESIS_ORPHAN_MEMORY",
    }
    assert (
        store.conn.execute(
            "SELECT content FROM memories WHERE id = ?", (draft.memory_id,)
        ).fetchone()[0]
        == content_before
    )


def test_lint_reports_support_mismatch_below_two_even_when_count_agrees(lifecycle_db):
    _storage, store, _sources = lifecycle_db
    draft = _create(store)
    store.conn.execute(
        "DELETE FROM behavior_graph_edges WHERE source = ? AND target = 'source-b'",
        (draft.memory_id,),
    )
    remaining_hash = canonical_memory_hash(store._load_memory("source-a"))
    store.conn.execute(
        "UPDATE synthesis_artifacts SET support_count = 1, source_fingerprint = ? "
        "WHERE memory_id = ?",
        (source_fingerprint({"source-a": remaining_hash}), draft.memory_id),
    )
    store.conn.commit()

    codes = {finding["code"] for finding in store.lint(memory_id=draft.memory_id)}

    assert "SYNTHESIS_SUPPORT_MISMATCH" in codes
