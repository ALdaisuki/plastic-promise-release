import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest

from plastic_promise.core.context_engine import (
    _ORDINARY_JSON_PATCH_FIELDS,
    _ORDINARY_NUMERIC_INCREMENT_FIELDS,
    _ORDINARY_SCALAR_PATCH_FIELDS,
    _RETRIEVAL_VISIBLE_PATCH_FIELDS,
    ContextEngine,
    ContextPack,
    OrdinaryMemoryConflict,
)
from plastic_promise.core.context_engine import (
    MemoryRecord as EngineMemoryRecord,
)
from plastic_promise.core.memory_index import (
    build_index_material,
    metadata_with_index_material,
)
from plastic_promise.core.synthesis import SynthesisStore, synthesis_content_hash
from plastic_promise.core.synthesis_retrieval import (
    _source_is_available,
    read_memory_version,
)
from plastic_promise.mcp.tools.memory import handle_memory_correct, handle_memory_update
from plastic_promise.mcp.tools.reflection import handle_feedback_apply
from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete
from plastic_promise.memory.pipeline import MemoryPipeline, PreparedMemory
from plastic_promise.memory.soul_memory import MemoryRecord, RecMem

_JSON_FIELDS = {
    "tags": ["patched", "\u503c"],
    "entity_ids": ["entity:z", "entity:a"],
    "parent_memory_ids": ["parent:z", "parent:a"],
    "metadata_json": {"z": 1, "a": {"\u952e": "\u503c"}},
}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "canonical.db"))
    instance = ContextEngine(use_sqlite=True)
    try:
        yield instance
    finally:
        instance._sqlite._conn.close()


@pytest.fixture
def rich_row(engine):
    return _seed_rich_row(engine)


def _seed_rich_row(
    engine,
    memory_id="ordinary-private",
    *,
    project_id="project:private-corrective",
):
    row = {
        "id": memory_id,
        "content": "private compact evidence",
        "memory_type": "experience",
        "source": "codex",
        "owner": "owner:private",
        "tier": "L3",
        "scope": "agent:codex",
        "category": "fact",
        "tags": ["private", "compact-v2"],
        "domain": "building",
        "importance": 0.91,
        "entity_ids": ["entity:alpha", "entity:beta"],
        "created_at": "2026-07-01T01:02:03Z",
        "access_count": 7,
        "worth_success": 2,
        "worth_failure": 1,
        "activation_weight": 0.73,
        "decay_multiplier": 0.625,
        "effective_half_life": 90.0,
        "last_accessed": "2026-07-02T01:02:03Z",
        "project_id": project_id,
        "visibility": "private",
        "source_class": "experience",
        "created_by_call_id": "call:seed",
        "origin_kind": "tool_call",
        "origin_uri": "mcp://memory_store/private",
        "origin_ref": "request:seed",
        "origin_hash": "sha256:origin-before",
        "parent_memory_ids": ["parent:one", "parent:two"],
        "metadata_json": {
            "schema": "memory-index/compact-v2",
            "quality": {"status": "current"},
            "private": True,
        },
        "raw_content": "raw private compact evidence",
        "l0_abstract": "private compact abstract",
        "l1_summary": "- private compact summary",
        "l2_content": "private compact evidence",
        "embedding_text": "L0: private compact abstract\nL1: private compact summary",
        "embedding_hash": "sha256:embedding-before",
        "search_text": "private compact abstract",
    }
    assert engine._sqlite.upsert_ordinary(memory_id, row)

    # Deliberately non-canonical JSON bytes prove unrelated patches do not
    # hydrate and rewrite private/project/index material through defaults.
    engine._sqlite._conn.execute(
        "UPDATE memories SET tags = ?, entity_ids = ?, parent_memory_ids = ?, "
        "metadata_json = ? WHERE id = ?",
        (
            '[ "private", "compact-v2" ]',
            '[ "entity:alpha", "entity:beta" ]',
            '[ "parent:one", "parent:two" ]',
            '{ "schema": "memory-index/compact-v2", "private": true, '
            '"quality": { "status": "current" } }',
            memory_id,
        ),
    )
    engine._sqlite._conn.commit()
    canonical = engine._sqlite.get(memory_id)
    engine._memories[memory_id] = canonical
    return canonical


def _raw_canonical_row(engine, memory_id):
    conn = engine._sqlite._conn
    columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
    expressions = []
    for column in columns:
        quoted = '"' + column.replace('"', '""') + '"'
        expressions.extend((f"typeof({quoted})", f"CAST({quoted} AS BLOB)"))
    row = conn.execute(
        f"SELECT {', '.join(expressions)} FROM memories WHERE id = ?",  # noqa: S608
        (memory_id,),
    ).fetchone()
    assert row is not None
    return {column: (row[index * 2], row[index * 2 + 1]) for index, column in enumerate(columns)}


def _assert_untargeted_columns_identical(before, after, targeted):
    assert before.keys() == after.keys()
    for column in before.keys() - set(targeted):
        assert after[column] == before[column], column


def test_decay_backfill_changes_only_decay_multiplier(tmp_path):
    from plastic_promise.core.context_engine import _SQLiteStorage

    db_path = tmp_path / "decay-backfill.db"
    storage = _SQLiteStorage(str(db_path))
    memory_id = "legacy-decay-backfill"
    storage.create_ordinary_if_absent(
        memory_id,
        {
            "id": memory_id,
            "content": "legacy ordinary memory requiring one startup decay migration",
            "memory_type": "experience",
            "source": "test",
            "tier": "L1",
            "created_at": "2020-01-01T00:00:00",
            "decay_multiplier": 1.0,
        },
    )
    before = storage.get(memory_id)
    before_version = read_memory_version(storage._conn)
    storage._conn.close()

    reopened = _SQLiteStorage(str(db_path))
    try:
        after = reopened.get(memory_id)
        assert after["decay_multiplier"] < 1.0
        assert read_memory_version(reopened._conn) == before_version + 1
        assert {key: value for key, value in after.items() if key != "decay_multiplier"} == {
            key: value for key, value in before.items() if key != "decay_multiplier"
        }
    finally:
        reopened._conn.close()


def _raw_field(engine, memory_id, field):
    assert field in _JSON_FIELDS
    return engine._sqlite._conn.execute(
        f'SELECT CAST("{field}" AS BLOB) FROM memories WHERE id = ?',  # noqa: S608
        (memory_id,),
    ).fetchone()[0]


def test_create_ordinary_if_absent_is_idempotent_only_for_identical_binding(engine, rich_row):
    memory_id = "ordinary-create-only"
    candidate = {**rich_row, "id": memory_id, "content": "creation-only binding"}

    assert engine.create_ordinary_if_absent(candidate) == memory_id
    before = _raw_canonical_row(engine, memory_id)
    before_version = read_memory_version(engine._sqlite._conn)

    assert engine.create_ordinary_if_absent(copy.deepcopy(candidate)) == memory_id
    assert _raw_canonical_row(engine, memory_id) == before
    assert read_memory_version(engine._sqlite._conn) == before_version

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_memory_already_exists",
    ):
        engine.create_ordinary_if_absent({**candidate, "content": "conflicting replay"})

    assert _raw_canonical_row(engine, memory_id) == before
    assert read_memory_version(engine._sqlite._conn) == before_version


@pytest.mark.parametrize("api", ["register_memory", "store_memory"])
def test_store_and_register_reject_existing_id_without_changing_row(engine, rich_row, api):
    memory_id = rich_row["id"]
    before = _raw_canonical_row(engine, memory_id)
    before_version = read_memory_version(engine._sqlite._conn)
    if api == "register_memory":
        conflicting = {**rich_row, "content": "registration must not replace"}
    else:
        conflicting = EngineMemoryRecord(
            id=memory_id,
            content="storage must not replace",
            memory_type="experience",
            source="test",
        )

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_memory_already_exists",
    ):
        getattr(engine, api)(conflicting)

    assert _raw_canonical_row(engine, memory_id) == before
    assert read_memory_version(engine._sqlite._conn) == before_version


def test_register_memories_rejects_conflicting_replay_without_partial_replacement(engine, rich_row):
    first_id = "ordinary-bulk-create"
    first = {**rich_row, "id": first_id, "content": "bulk creation"}
    conflict = {**rich_row, "content": "bulk conflict"}
    before_existing = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_memory_already_exists",
    ):
        engine.register_memories([first, conflict])

    assert engine._sqlite.get(first_id) is not None
    assert _raw_canonical_row(engine, rich_row["id"]) == before_existing


def test_recmem_caller_supplied_id_rejects_conflicting_replay(engine):
    recmem = RecMem(engine)
    memory_id = "recmem-create-only"
    first = recmem.store(
        "caller supplied binding",
        memory_id=memory_id,
        project_id="project:private-corrective",
    )
    before = _raw_canonical_row(engine, memory_id)

    assert first.memory_id == memory_id
    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_memory_already_exists",
    ):
        recmem.store(
            "conflicting caller replay",
            memory_id=memory_id,
            project_id="project:private-corrective",
        )

    assert _raw_canonical_row(engine, memory_id) == before


def test_patch_allowlists_are_fixed_and_disjoint():
    assert isinstance(_ORDINARY_SCALAR_PATCH_FIELDS, frozenset)
    assert isinstance(_ORDINARY_JSON_PATCH_FIELDS, frozenset)
    assert isinstance(_ORDINARY_NUMERIC_INCREMENT_FIELDS, frozenset)
    assert isinstance(_RETRIEVAL_VISIBLE_PATCH_FIELDS, frozenset)
    assert frozenset(_JSON_FIELDS) == _ORDINARY_JSON_PATCH_FIELDS
    assert (
        frozenset({"access_count", "worth_success", "worth_failure"})
        == _ORDINARY_NUMERIC_INCREMENT_FIELDS
    )
    assert "id" not in _ORDINARY_SCALAR_PATCH_FIELDS
    assert "id" not in _ORDINARY_JSON_PATCH_FIELDS
    assert _ORDINARY_SCALAR_PATCH_FIELDS.isdisjoint(_ORDINARY_JSON_PATCH_FIELDS)
    assert _RETRIEVAL_VISIBLE_PATCH_FIELDS <= (
        _ORDINARY_SCALAR_PATCH_FIELDS | _ORDINARY_JSON_PATCH_FIELDS
    )


def test_patch_ordinary_preserves_every_untargeted_canonical_column(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    after = engine.patch_ordinary_memory(
        rich_row["id"],
        increments={"worth_success": 1},
        replacements={"last_accessed": "2026-07-12T00:00:00Z"},
    )

    assert after["worth_success"] == rich_row["worth_success"] + 1
    assert after["project_id"] == "project:private-corrective"
    assert after["visibility"] == "private"
    assert after["metadata_json"]["schema"] == "memory-index/compact-v2"
    canonical_after = _raw_canonical_row(engine, rich_row["id"])
    _assert_untargeted_columns_identical(
        before,
        canonical_after,
        {"worth_success", "last_accessed"},
    )


@pytest.mark.parametrize("field,value", _JSON_FIELDS.items())
def test_patch_ordinary_serializes_only_targeted_json_canonically(
    engine,
    rich_row,
    field,
    value,
):
    before = _raw_canonical_row(engine, rich_row["id"])

    engine.patch_ordinary_memory(rich_row["id"], replacements={field: value})

    expected = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert _raw_field(engine, rich_row["id"], field) == expected
    _assert_untargeted_columns_identical(
        before,
        _raw_canonical_row(engine, rich_row["id"]),
        {field},
    )


@pytest.mark.parametrize(
    "replacements,increments",
    [(None, None), ({}, None), (None, {}), ({}, {})],
)
def test_patch_ordinary_rejects_empty_patch_without_mutation(
    engine,
    rich_row,
    replacements,
    increments,
):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_empty"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements=replacements,
            increments=increments,
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


@pytest.mark.parametrize(
    "replacements,increments",
    [
        ({"project_owner": "x"}, None),
        ({"id": "replacement-id"}, None),
        ({"content = NULL WHERE 1=1 --": "x"}, None),
        (None, {"importance": 1}),
    ],
)
def test_patch_ordinary_rejects_unknown_or_immutable_fields_without_mutation(
    engine,
    rich_row,
    replacements,
    increments,
):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_field_not_allowed"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements=replacements,
            increments=increments,
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_patch_ordinary_rejects_replacement_increment_collision(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_field_conflict"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"worth_success": 10},
            increments={"worth_success": 1},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


@pytest.mark.parametrize(
    "value", [True, False, "1", None, float("nan"), float("inf"), -float("inf")]
)
def test_patch_ordinary_rejects_invalid_numeric_increments(engine, rich_row, value):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_increment_invalid"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            increments={"worth_success": value},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -float("inf")])
def test_patch_ordinary_rejects_invalid_numeric_replacements(engine, rich_row, value):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_value_invalid"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"importance": value},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_patch_ordinary_rejects_synthesis_memory_type_without_mutation(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_memory_reserved"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"memory_type": "  SyNtHeSiS  "},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_patch_ordinary_rejects_canonically_reserved_row(engine, rich_row):
    conn = engine._sqlite._conn
    conn.execute(
        "INSERT INTO synthesis_artifacts "
        "(memory_id, synthesis_key, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (rich_row["id"], "reserved:key", "draft", "2026-07-12", "2026-07-12"),
    )
    conn.commit()
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_memory_reserved"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            increments={"worth_success": 1},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_patch_ordinary_rejects_synthesis_typed_row(engine, rich_row):
    conn = engine._sqlite._conn
    conn.execute(
        "UPDATE memories SET memory_type = 'synthesis' WHERE id = ?",
        (rich_row["id"],),
    )
    conn.commit()
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_memory_reserved"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            increments={"worth_success": 1},
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_patch_ordinary_requires_exactly_one_existing_row(engine):
    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_target_not_found"):
        engine.patch_ordinary_memory("missing", increments={"worth_success": 1})


def test_patch_ordinary_content_cas_accepts_current_and_rejects_stale(engine, rich_row):
    current_hash = synthesis_content_hash(rich_row["content"])
    updated = engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"content": "new content"},
        expected_content_hash=current_hash,
    )
    assert updated["content"] == "new content"
    before_stale = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"content": "stale writer"},
            expected_content_hash=current_hash,
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before_stale


def test_patch_ordinary_index_cas_accepts_current_and_rejects_stale(engine, rich_row):
    updated = engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"last_accessed": "2026-07-12T03:00:00Z"},
        expected_content_hash=synthesis_content_hash(rich_row["content"]),
        expected_embedding_hash=rich_row["embedding_hash"],
    )
    assert updated["last_accessed"] == "2026-07-12T03:00:00Z"
    before_stale = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"content": "new"},
            expected_embedding_hash="sha256:stale-index",
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before_stale


def test_projection_metadata_patch_automatically_queues_checked_index_upsert(engine, rich_row):
    conn = engine._sqlite._conn
    conn.execute("DELETE FROM store_outbox")
    conn.commit()

    updated = engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"category": "decision", "tier": "L2"},
        expected_project_id=rich_row["project_id"],
        expected_embedding_hash=rich_row["embedding_hash"],
    )

    row = conn.execute(
        "SELECT payload_json, metadata_json FROM store_outbox WHERE tool_name = 'memory_index'"
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["action"] == "upsert"
    assert payload["memory_id"] == rich_row["id"]
    assert payload["project_id"] == rich_row["project_id"]
    assert payload["expected_embedding_hash"] == rich_row["embedding_hash"]
    assert json.loads(row[1])["job_schema"] == "memory-index/v3"
    assert updated["category"] == "decision"
    assert updated["tier"] == "L2"


def _increment_n_times(engine, memory_id, count):
    for _ in range(count):
        engine.patch_ordinary_memory(memory_id, increments={"worth_success": 1})
    return count


def test_numeric_increments_are_atomic_across_two_sqlite_connections(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    first = ContextEngine(use_sqlite=True)
    _seed_rich_row(first, "m1")
    first.patch_ordinary_memory("m1", replacements={"worth_success": 2})
    second = ContextEngine(use_sqlite=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_increment_n_times, first, "m1", 25),
                pool.submit(_increment_n_times, second, "m1", 25),
            ]
            assert [future.result() for future in futures] == [25, 25]
        assert first._sqlite.get("m1")["worth_success"] == 52
    finally:
        second._sqlite._conn.close()
        first._sqlite._conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("content", "retrieval-visible content"),
        ("memory_type", "task"),
        ("tier", "L2"),
        ("tags", ["retrieval-visible"]),
        ("domain", "designing"),
        ("project_id", "project:other"),
        ("visibility", "shared"),
        ("source_class", "code"),
        (
            "metadata_json",
            {"classification": "updated", "quality": {"status": "current"}},
        ),
        ("l0_abstract", "new abstract"),
        ("embedding_text", "new vector text"),
        ("embedding_hash", "sha256:new-index"),
        ("search_text", "new search text"),
    ],
)
def test_retrieval_visible_replacements_bump_memory_version_once(
    engine,
    rich_row,
    field,
    value,
):
    before = read_memory_version(engine._sqlite._conn)

    engine.patch_ordinary_memory(rich_row["id"], replacements={field: value})

    assert read_memory_version(engine._sqlite._conn) == before + 1


def test_non_retrieval_patch_does_not_bump_version_without_override(engine, rich_row):
    before = read_memory_version(engine._sqlite._conn)

    engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"last_accessed": "2026-07-12T04:00:00Z"},
        increments={"worth_success": 1},
    )

    assert read_memory_version(engine._sqlite._conn) == before


def test_memory_version_bump_can_be_forced_or_suppressed(engine, rich_row):
    before = read_memory_version(engine._sqlite._conn)
    engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"last_accessed": "2026-07-12T05:00:00Z"},
        bump_memory_version=True,
    )
    assert read_memory_version(engine._sqlite._conn) == before + 1

    engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"content": "suppressed version bump"},
        bump_memory_version=False,
    )
    assert read_memory_version(engine._sqlite._conn) == before + 1


def test_patch_preserves_caller_transaction_and_rollback_keeps_cache_canonical(
    engine,
    rich_row,
):
    before_row = _raw_canonical_row(engine, rich_row["id"])
    before_cache = copy.deepcopy(engine._memories[rich_row["id"]])
    conn = engine._sqlite._conn
    conn.execute("BEGIN IMMEDIATE")

    result = engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"last_accessed": "2026-07-12T06:00:00Z"},
    )

    assert conn.in_transaction is True
    assert result["last_accessed"] == "2026-07-12T06:00:00Z"
    assert engine._memories[rich_row["id"]] == before_cache
    conn.rollback()
    assert _raw_canonical_row(engine, rich_row["id"]) == before_row
    assert engine._memories[rich_row["id"]] == before_cache


def test_patch_replaces_only_target_cache_entry_from_committed_canonical_row(
    engine,
    rich_row,
):
    other = _seed_rich_row(engine, "ordinary-other")
    other_before = copy.deepcopy(engine._memories[other["id"]])
    engine._memories[rich_row["id"]] = {
        **engine._memories[rich_row["id"]],
        "project_id": "project:stale-cache",
        "metadata_json": {},
        "embedding_hash": "stale-cache-index",
    }

    result = engine.patch_ordinary_memory(
        rich_row["id"],
        increments={"worth_success": 1},
    )

    assert result == engine._sqlite.get(rich_row["id"])
    assert engine._memories[rich_row["id"]] == result
    assert engine._memories[rich_row["id"]]["project_id"] == "project:private-corrective"
    assert engine._memories[rich_row["id"]]["metadata_json"]["schema"] == (
        "memory-index/compact-v2"
    )
    assert engine._memories[other["id"]] == other_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback_type", "success_delta", "failure_delta"),
    [
        ("adopted", 1, 0),
        ("rejected", 0, 1),
        ("ignored", 0, 0.5),
    ],
)
async def test_feedback_changes_only_declared_counters(
    engine,
    rich_row,
    feedback_type,
    success_delta,
    failure_delta,
):
    before = _raw_canonical_row(engine, rich_row["id"])

    response = await handle_feedback_apply(
        engine,
        {"item_id": rich_row["id"], "feedback_type": feedback_type},
    )

    payload = json.loads(response[0].text)
    after = _raw_canonical_row(engine, rich_row["id"])
    assert payload["updated"] is True
    assert engine._sqlite.get(rich_row["id"])["worth_success"] == (
        rich_row["worth_success"] + success_delta
    )
    assert engine._sqlite.get(rich_row["id"])["worth_failure"] == (
        rich_row["worth_failure"] + failure_delta
    )
    _assert_untargeted_columns_identical(
        before,
        after,
        {"worth_success", "worth_failure"},
    )


def test_reset_ordinary_worth_preserves_index_and_provenance(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    updated = engine.reset_ordinary_worth(rich_row["id"])

    assert updated["worth_success"] == 0
    assert updated["worth_failure"] == 0
    _assert_untargeted_columns_identical(
        before,
        _raw_canonical_row(engine, rich_row["id"]),
        {"worth_success", "worth_failure"},
    )


@pytest.mark.asyncio
async def test_memory_update_reset_worth_preserves_index_and_provenance(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    response = await handle_memory_update(
        engine,
        {"memory_id": rich_row["id"], "reset_worth": True},
    )

    assert json.loads(response[0].text) == {
        "updated": True,
        "memory_id": rich_row["id"],
    }
    _assert_untargeted_columns_identical(
        before,
        _raw_canonical_row(engine, rich_row["id"]),
        {"worth_success", "worth_failure"},
    )


@pytest.mark.asyncio
async def test_memory_update_combines_metadata_and_worth_reset_in_one_patch(engine, rich_row):
    before_version = read_memory_version(engine._sqlite._conn)

    response = await handle_memory_update(
        engine,
        {
            "memory_id": rich_row["id"],
            "importance": 0.42,
            "reset_worth": True,
        },
    )

    assert json.loads(response[0].text)["updated"] is True
    after = engine._sqlite.get(rich_row["id"])
    assert after["importance"] == 0.42
    assert after["worth_success"] == 0
    assert after["worth_failure"] == 0
    assert read_memory_version(engine._sqlite._conn) == before_version + 1


def test_recmem_feedback_uses_atomic_engine_feedback_not_store_memory(
    engine,
    rich_row,
    monkeypatch,
):
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)

    def reject_whole_record_store(*_args, **_kwargs):
        raise AssertionError("feedback must not call store_memory")

    monkeypatch.setattr(engine, "store_memory", reject_whole_record_store)
    result = rec_mem.apply_feedback(rich_row["id"], "adopted")

    assert engine._sqlite.get(rich_row["id"])["worth_success"] == (rich_row["worth_success"] + 1)
    assert rec_mem._records[rich_row["id"]].worth_success == (rich_row["worth_success"] + 1)
    assert result["new_worth"] == rec_mem._records[rich_row["id"]].worth_score


def test_recmem_update_combines_metadata_and_worth_reset_in_one_patch(engine, rich_row):
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    before_version = read_memory_version(engine._sqlite._conn)

    updated = rec_mem.update(
        rich_row["id"],
        importance=0.37,
        reset_worth=True,
    )

    assert updated is rec_mem._records[rich_row["id"]]
    canonical = engine._sqlite.get(rich_row["id"])
    assert canonical["importance"] == 0.37
    assert canonical["worth_success"] == 0
    assert canonical["worth_failure"] == 0
    assert updated.activation_weight == 0.37
    assert updated.worth_success == 0
    assert updated.worth_failure == 0
    assert read_memory_version(engine._sqlite._conn) == before_version + 1


def test_recmem_mixed_content_metadata_and_worth_commit_atomically(
    engine,
    rich_row,
    monkeypatch,
):
    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    monkeypatch.setattr(
        MemoryPipeline,
        "prepare_correction",
        _prepare_test_correction,
    )
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    before_version = read_memory_version(engine._sqlite._conn)

    updated = rec_mem.update(
        rich_row["id"],
        content="replacement commits with its metadata atomically",
        importance=0.37,
        reset_worth=True,
    )

    assert updated is rec_mem._records[rich_row["id"]]
    canonical = engine._sqlite.get(rich_row["id"])
    assert canonical["content"] == "replacement commits with its metadata atomically"
    assert canonical["importance"] == 0.37
    assert canonical["worth_success"] == 0
    assert canonical["worth_failure"] == 0
    assert updated.content == canonical["content"]
    assert updated.activation_weight == 0.37
    assert updated.worth_success == 0
    assert updated.worth_failure == 0
    assert read_memory_version(engine._sqlite._conn) == before_version + 1
    assert {store.get(item.memory_id).status for item in verified} == {"stale"}


def test_recmem_mixed_update_rolls_back_every_effect_on_index_enqueue_failure(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import ordinary_memory_mutation

    monkeypatch.setattr(
        MemoryPipeline,
        "prepare_correction",
        _prepare_test_correction,
    )
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    before = _task4_snapshot(engine._sqlite._conn)
    before_record = rec_mem._records[rich_row["id"]].to_dict()

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected ordinary index enqueue failure")

    monkeypatch.setattr(
        ordinary_memory_mutation,
        "enqueue_memory_index_upsert",
        fail_enqueue,
    )

    assert (
        rec_mem.update(
            rich_row["id"],
            content="this replacement must roll back",
            importance=0.11,
            reset_worth=True,
        )
        is None
    )
    assert _task4_snapshot(engine._sqlite._conn) == before
    assert rec_mem._records[rich_row["id"]].to_dict() == before_record


def test_recmem_mixed_update_uses_committed_cache_when_postcommit_get_fails(
    engine,
    rich_row,
    monkeypatch,
):
    monkeypatch.setattr(
        MemoryPipeline,
        "prepare_correction",
        _prepare_test_correction,
    )
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    original_mutate = engine.mutate_ordinary_source

    def mutate_then_break_get(memory_id, **kwargs):
        result = original_mutate(memory_id, **kwargs)

        def fail_get(_memory_id):
            raise RuntimeError("postcommit canonical get failed")

        monkeypatch.setattr(engine._sqlite, "get", fail_get)
        return result

    monkeypatch.setattr(engine, "mutate_ordinary_source", mutate_then_break_get)

    updated = rec_mem.update(
        rich_row["id"],
        content="committed result remains observable",
        importance=0.29,
        reset_worth=True,
    )

    assert updated is rec_mem._records[rich_row["id"]]
    assert updated.content == "committed result remains observable"
    assert updated.activation_weight == 0.29
    assert updated.worth_success == 0
    assert updated.worth_failure == 0
    assert engine._memories[rich_row["id"]]["content"] == updated.content


def test_recmem_content_update_cannot_revive_concurrent_tombstone(
    engine,
    rich_row,
    monkeypatch,
):
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    original_mutate = engine.mutate_ordinary_source
    before_version = read_memory_version(engine._sqlite._conn)

    def tombstone_then_mutate(memory_id, **kwargs):
        original_mutate(
            memory_id,
            operation="forgotten",
            reason="concurrent tombstone",
            actor="test",
            call_id="call:concurrent-tombstone",
        )
        return original_mutate(memory_id, **kwargs)

    monkeypatch.setattr(engine, "mutate_ordinary_source", tombstone_then_mutate)

    assert rec_mem.update(rich_row["id"], content="stale replacement") is None
    canonical = engine._sqlite.get(rich_row["id"])
    assert _source_is_available(canonical) is False
    assert canonical["content"] == rich_row["content"]
    assert canonical["metadata_json"]["quality"]["status"] == "forgotten"
    assert read_memory_version(engine._sqlite._conn) == before_version + 1


def test_recmem_forget_does_not_repeat_concurrent_tombstone(
    engine,
    rich_row,
    monkeypatch,
):
    rec_mem = RecMem(engine)
    rec_mem._records[rich_row["id"]] = MemoryRecord.from_dict(rich_row)
    original_mutate = engine.mutate_ordinary_source
    before_version = read_memory_version(engine._sqlite._conn)

    def tombstone_then_mutate(memory_id, **kwargs):
        original_mutate(
            memory_id,
            operation="forgotten",
            reason="concurrent tombstone",
            actor="test",
            call_id="call:concurrent-forget",
        )
        return original_mutate(memory_id, **kwargs)

    monkeypatch.setattr(engine, "mutate_ordinary_source", tombstone_then_mutate)

    assert rec_mem.forget(rich_row["id"], "stale forget") is False
    canonical = engine._sqlite.get(rich_row["id"])
    assert _source_is_available(canonical) is False
    assert read_memory_version(engine._sqlite._conn) == before_version + 1


@pytest.mark.parametrize(
    "facade",
    [
        pytest.param(
            lambda instance, memory_id: instance.update_memory(
                memory_id,
                content="stale facade replacement",
            ),
            id="update-memory",
        ),
        pytest.param(
            lambda instance, memory_id: instance.update_memory_fields(
                memory_id,
                content="stale facade replacement",
            ),
            id="update-memory-fields",
        ),
        pytest.param(
            lambda instance, memory_id: instance.delete_memory(memory_id),
            id="delete-memory",
        ),
    ],
)
def test_internal_mutation_facades_cannot_repeat_or_revive_concurrent_tombstone(
    engine,
    rich_row,
    monkeypatch,
    facade,
):
    original_mutate = engine.mutate_ordinary_source
    before_version = read_memory_version(engine._sqlite._conn)

    def tombstone_then_mutate(memory_id, **kwargs):
        original_mutate(
            memory_id,
            operation="forgotten",
            reason="concurrent facade tombstone",
            actor="test",
            call_id="call:concurrent-facade-tombstone",
        )
        return original_mutate(memory_id, **kwargs)

    monkeypatch.setattr(engine, "mutate_ordinary_source", tombstone_then_mutate)

    assert facade(engine, rich_row["id"]) is False
    canonical = engine._sqlite.get(rich_row["id"])
    assert canonical["content"] == rich_row["content"]
    assert canonical["metadata_json"]["quality"]["status"] == "forgotten"
    assert _source_is_available(canonical) is False
    assert read_memory_version(engine._sqlite._conn) == before_version + 1


@pytest.mark.parametrize(
    "failure",
    [
        OrdinaryMemoryConflict("ordinary_patch_cas_mismatch"),
        RuntimeError("injected coordinator runtime failure"),
    ],
)
def test_internal_mutation_facade_converts_coordinator_exceptions_to_false(
    engine,
    rich_row,
    monkeypatch,
    failure,
):
    before = _raw_canonical_row(engine, rich_row["id"])

    def fail_mutation(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(engine, "mutate_ordinary_source", fail_mutation)

    assert (
        engine.update_memory(
            rich_row["id"],
            content="compatibility facade failure",
        )
        is False
    )
    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_content_update_routes_coordinator_while_metadata_patch_stays_narrow(
    engine,
    rich_row,
    monkeypatch,
):
    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)

    assert (
        engine.update_memory_fields(
            rich_row["id"],
            content="replacement content",
        )
        is True
    )

    corrected = engine._sqlite.get(rich_row["id"])
    assert corrected["content"] == "replacement content"
    assert corrected["metadata_json"]["quality"]["actor"] == "context_engine"
    assert corrected["metadata_json"]["quality"]["call_id"].startswith(
        "internal:context_engine:update:"
    )
    assert all(store.get(item.memory_id).status == "stale" for item in verified)
    before_lineage = engine._sqlite._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[
        0
    ]
    before_outbox = engine._sqlite._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]

    assert engine.update_memory_fields(
        rich_row["id"],
        tags=["private", "compact-v2", "patched"],
        domain="governing",
    )
    after = engine._sqlite.get(rich_row["id"])
    assert after["tags"] == ["private", "compact-v2", "patched"]
    assert after["domain"] == "governing"
    assert after["project_id"] == rich_row["project_id"]
    assert after["embedding_hash"] == corrected["embedding_hash"]
    assert (
        engine._sqlite._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
        == before_lineage
    )
    assert (
        engine._sqlite._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0]
        == before_outbox
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"content": "replacement source content"},
        {"mark_as": "deprecated"},
        {"content": "replacement source content", "mark_as": "wrong"},
    ],
)
async def test_memory_correct_rejects_content_or_lifecycle_without_mutation(
    engine,
    rich_row,
    args,
):
    memory_id = rich_row["id"]
    before = _raw_canonical_row(engine, memory_id)
    cache_before = copy.deepcopy(engine._memories[memory_id])

    response = await handle_memory_correct(engine, {"memory_id": memory_id, **args})

    assert json.loads(response[0].text) == {
        "corrected": False,
        "memory_id": memory_id,
        "reason": "ordinary_content_requires_coordinator",
    }
    assert _raw_canonical_row(engine, memory_id) == before
    assert engine._memories[memory_id] == cache_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mark_as", "target_counter", "action"),
    [
        ("wrong", "worth_failure", "marked_wrong"),
        ("corrected", "worth_success", "marked_corrected"),
    ],
)
async def test_memory_correct_quality_feedback_uses_canonical_patch(
    engine,
    rich_row,
    mark_as,
    target_counter,
    action,
    monkeypatch,
):
    memory_id = rich_row["id"]
    before = _raw_canonical_row(engine, memory_id)

    def reject_whole_record_store(*_args, **_kwargs):
        raise AssertionError("memory_correct must not call store_memory for feedback")

    monkeypatch.setattr(engine, "store_memory", reject_whole_record_store)
    monkeypatch.setattr(
        "plastic_promise.memory.soul_memory.EvolveR.evolve_cycle",
        lambda _self: None,
    )

    response = await handle_memory_correct(
        engine,
        {"memory_id": memory_id, "mark_as": mark_as},
    )

    payload = json.loads(response[0].text)
    after = _raw_canonical_row(engine, memory_id)
    assert payload["corrected"] is True
    assert payload["actions"] == [action]
    assert engine._sqlite.get(memory_id)[target_counter] == rich_row[target_counter] + 1
    _assert_untargeted_columns_identical(before, after, {target_counter})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "marker", "status"),
    [
        ({"outcome": "still_in_progress"}, "[still_in_progress]", "still_active"),
        (
            {"outcome": "abandoned: no longer needed"},
            "[SKILL ABANDONED] no longer needed",
            "abandoned",
        ),
        ({}, "[SKILL COMPLETE]", "done"),
    ],
)
async def test_skill_content_transitions_route_through_source_invalidation(
    engine,
    rich_row,
    monkeypatch,
    args,
    marker,
    status,
):
    entity_id = "skill:brainstorming:2026-07-12T00:00:00.000000"
    memory_id = "skill_start_" + entity_id.replace(":", "_")
    engine._sqlite._conn.execute(
        "UPDATE memories SET id = ?, content = ?, entity_ids = ?, tags = ? WHERE id = ?",
        (
            memory_id,
            "[SKILL START] brainstorming: protected transition",
            json.dumps([entity_id], separators=(",", ":")),
            json.dumps(
                ["task:active", "skill:brainstorming", "domain:designing"], separators=(",", ":")
            ),
            rich_row["id"],
        ),
    )
    engine._sqlite._conn.commit()
    engine._memories.pop(rich_row["id"])
    engine._memories[memory_id] = engine._sqlite.get(memory_id)
    store, verified = _seed_verified_dependents(engine, memory_id, monkeypatch)
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)

    response = await handle_skill_session_complete(engine, {"entity_id": entity_id, **args})

    payload = json.loads(response[0].text)
    assert payload["status"] == status
    assert marker in engine._sqlite.get(memory_id)["content"]
    assert all(store.get(item.memory_id).status == "stale" for item in verified)


def test_feedback_bumps_version_and_refreshes_another_engine_cache(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    writer = ContextEngine(use_sqlite=True)
    reader = ContextEngine(use_sqlite=True)
    try:
        _seed_rich_row(writer, "ordinary-feedback-cross-process")
        assert reader._refresh_canonical_cache_if_changed(force=True)
        before_version = read_memory_version(writer._sqlite._conn)
        assert reader._memories["ordinary-feedback-cross-process"]["worth_success"] == 2

        updated = writer.apply_ordinary_feedback(
            "ordinary-feedback-cross-process",
            "adopted",
        )

        assert updated["worth_success"] == 3
        assert read_memory_version(writer._sqlite._conn) == before_version + 1
        monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
        monkeypatch.setattr(reader, "_ensure_heavy_init", lambda: None)
        monkeypatch.setattr(reader, "_supply_python", lambda *args, **kwargs: ContextPack())
        reader.supply("refresh feedback cache", task_vector=[0.0] * 4)
        assert reader._memories["ordinary-feedback-cross-process"]["worth_success"] == 3
    finally:
        reader._sqlite._conn.close()
        writer._sqlite._conn.close()


def test_duplicate_reinforcement_unions_entity_ids_across_two_engines(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    first = ContextEngine(use_sqlite=True)
    _seed_rich_row(first, "ordinary-concurrent-duplicate")
    second = ContextEngine(use_sqlite=True)
    try:
        before_version = read_memory_version(first._sqlite._conn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(
                    engine.reinforce_ordinary_duplicate,
                    "ordinary-concurrent-duplicate",
                    entity_ids=entity_ids,
                    last_accessed=timestamp,
                    expected_project_id="project:private-corrective",
                    expected_visibility="private",
                    expected_source_class="experience",
                    expected_memory_type="experience",
                )
                for engine, entity_ids, timestamp in (
                    (first, ["entity:first"], "2026-07-12T01:00:00Z"),
                    (second, ["entity:second"], "2026-07-12T02:00:00Z"),
                )
            ]
            assert all(
                result.result()["id"] == "ordinary-concurrent-duplicate" for result in results
            )

        canonical = first._sqlite.get("ordinary-concurrent-duplicate")
        assert canonical["access_count"] == 9
        assert canonical["worth_success"] == 4
        assert set(canonical["entity_ids"]) == {
            "entity:alpha",
            "entity:beta",
            "entity:first",
            "entity:second",
        }
        assert read_memory_version(first._sqlite._conn) == before_version + 2
    finally:
        second._sqlite._conn.close()
        first._sqlite._conn.close()


def test_manual_batch_rejected_delete_preserves_pending_patch(engine, rich_row):
    memory_id = rich_row["id"]
    before = copy.deepcopy(engine._memories[memory_id])

    engine.begin_batch()
    assert engine.update_memory_fields(memory_id, domain="manual-pending")
    assert engine.delete_memory(memory_id) is False
    assert engine._memories[memory_id] == before
    engine.commit_batch()

    assert engine._sqlite.get(memory_id)["domain"] == "manual-pending"
    assert engine._memories[memory_id]["domain"] == "manual-pending"


def test_manual_batch_register_after_pending_create_delete_installs_final_row(engine):
    memory_id = "manual-register-delete-recreate"

    engine.begin_batch()
    assert (
        engine.register_memory(
            {
                "id": memory_id,
                "content": "transient pending create",
                "memory_type": "experience",
            }
        )
        == memory_id
    )
    assert engine.delete_memory(memory_id)
    assert (
        engine.register_memory(
            {
                "id": memory_id,
                "content": "replacement after transactional delete",
                "memory_type": "experience",
                "source": "test",
                "category": "decision",
            }
        )
        == memory_id
    )
    assert memory_id not in engine._memories
    engine.commit_batch()

    assert engine._sqlite.get(memory_id)["content"] == "replacement after transactional delete"
    assert engine._memories[memory_id]["content"] == "replacement after transactional delete"


def test_manual_batch_store_after_pending_create_delete_installs_final_row(engine):
    memory_id = "manual-store-delete-recreate"

    engine.begin_batch()
    assert (
        engine.store_memory(
            EngineMemoryRecord(
                id=memory_id,
                content="transient pending store",
                memory_type="experience",
            )
        )
        == memory_id
    )
    assert engine.delete_memory(memory_id)
    assert (
        engine.store_memory(
            EngineMemoryRecord(
                id=memory_id,
                content="store replacement after transactional delete",
                memory_type="experience",
            )
        )
        == memory_id
    )
    engine.commit_batch()

    assert (
        engine._sqlite.get(memory_id)["content"] == "store replacement after transactional delete"
    )
    assert engine._memories[memory_id]["content"] == "store replacement after transactional delete"


def test_manual_batch_register_then_delete_removes_pending_create(engine):
    memory_id = "manual-create-delete"

    engine.begin_batch()
    assert (
        engine.register_memory(
            {
                "id": memory_id,
                "content": "transient pending create",
                "memory_type": "experience",
            }
        )
        == memory_id
    )
    assert engine.delete_memory(memory_id)
    engine.commit_batch()

    assert engine._sqlite.get(memory_id) is None
    assert memory_id not in engine._memories


def test_manual_batch_store_then_delete_removes_pending_create(engine):
    memory_id = "manual-store-delete"

    engine.begin_batch()
    assert (
        engine.store_memory(
            EngineMemoryRecord(
                id=memory_id,
                content="transient pending store",
                memory_type="experience",
            )
        )
        == memory_id
    )
    assert engine.delete_memory(memory_id)
    engine.commit_batch()

    assert engine._sqlite.get(memory_id) is None
    assert memory_id not in engine._memories


@pytest.mark.parametrize("creation", ["register", "store"])
def test_manual_batch_create_then_metadata_patch_uses_canonical_pending_row(engine, creation):
    memory_id = f"manual-{creation}-metadata"

    engine.begin_batch()
    try:
        if creation == "register":
            assert (
                engine.register_memory(
                    {
                        "id": memory_id,
                        "content": "pending ordinary row",
                        "memory_type": "experience",
                        "source": "test",
                    }
                )
                == memory_id
            )
        else:
            assert (
                engine.store_memory(
                    EngineMemoryRecord(
                        id=memory_id,
                        content="pending ordinary row",
                        memory_type="experience",
                        source="test",
                    )
                )
                == memory_id
            )

        assert memory_id not in engine._memories
        assert engine.update_memory_fields(
            memory_id,
            tags=["pending", creation],
            domain="building",
        )
        assert memory_id not in engine._memories
        engine.commit_batch()
    except BaseException:
        if engine._manual_batch_state is not None:
            engine.rollback_batch()
        raise

    canonical = engine._sqlite.get(memory_id)
    assert canonical["tags"] == ["pending", creation]
    assert canonical["domain"] == "building"
    assert engine._memories[memory_id] == canonical


def test_metadata_patch_uses_canonical_row_when_second_engine_cache_is_stale(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    writer = ContextEngine(use_sqlite=True)
    reader = ContextEngine(use_sqlite=True)
    memory_id = "ordinary-stale-cache-metadata"
    try:
        _seed_rich_row(writer, memory_id)
        assert memory_id not in reader._memories

        assert reader.update_memory_fields(memory_id, domain="cross-process")

        canonical = writer._sqlite.get(memory_id)
        assert canonical["domain"] == "cross-process"
        assert reader._memories[memory_id] == canonical
    finally:
        reader._sqlite._conn.close()
        writer._sqlite._conn.close()


def test_manual_batch_create_then_batch_update_uses_canonical_pending_row(engine):
    memory_id = "manual-batch-update-pending"

    engine.begin_batch()
    try:
        assert (
            engine.register_memory(
                {
                    "id": memory_id,
                    "content": "pending ordinary row",
                    "memory_type": "experience",
                    "source": "test",
                }
            )
            == memory_id
        )
        assert memory_id not in engine._memories
        assert (
            engine.batch_update(
                [
                    {
                        "id": memory_id,
                        "tags": ["pending", "batch"],
                        "domain": "building",
                    }
                ]
            )
            == 1
        )
        assert memory_id not in engine._memories
        engine.commit_batch()
    except BaseException:
        if engine._manual_batch_state is not None:
            engine.rollback_batch()
        raise

    canonical = engine._sqlite.get(memory_id)
    assert canonical["tags"] == ["pending", "batch"]
    assert canonical["domain"] == "building"
    assert engine._memories[memory_id] == canonical


def test_batch_update_uses_canonical_row_when_second_engine_cache_is_stale(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    writer = ContextEngine(use_sqlite=True)
    reader = ContextEngine(use_sqlite=True)
    memory_id = "ordinary-stale-cache-batch"
    try:
        _seed_rich_row(writer, memory_id)
        assert memory_id not in reader._memories

        assert reader.batch_update([{"id": memory_id, "domain": "batch-cross-process"}]) == 1

        canonical = writer._sqlite.get(memory_id)
        assert canonical["domain"] == "batch-cross-process"
        assert reader._memories[memory_id] == canonical
    finally:
        reader._sqlite._conn.close()
        writer._sqlite._conn.close()


def test_duplicate_reinforcement_never_moves_last_accessed_backward(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    first = ContextEngine(use_sqlite=True)
    _seed_rich_row(first, "ordinary-monotonic-access")
    second = ContextEngine(use_sqlite=True)
    try:
        newer = "2026-07-12T02:00:00Z"
        older = "2026-07-12T01:00:00Z"
        first.reinforce_ordinary_duplicate(
            "ordinary-monotonic-access",
            entity_ids=["entity:newer"],
            last_accessed=newer,
            expected_project_id="project:private-corrective",
            expected_visibility="private",
            expected_source_class="experience",
            expected_memory_type="experience",
        )
        result = second.reinforce_ordinary_duplicate(
            "ordinary-monotonic-access",
            entity_ids=["entity:older"],
            last_accessed=older,
            expected_project_id="project:private-corrective",
            expected_visibility="private",
            expected_source_class="experience",
            expected_memory_type="experience",
        )

        canonical = first._sqlite.get("ordinary-monotonic-access")
        assert result["last_accessed"] == newer
        assert canonical["last_accessed"] == newer
        assert canonical["access_count"] == 9
        assert canonical["worth_success"] == 4
        assert {"entity:newer", "entity:older"} <= set(canonical["entity_ids"])
    finally:
        second._sqlite._conn.close()
        first._sqlite._conn.close()


def test_duplicate_reinforcement_rejects_unavailable_source(engine, rich_row):
    engine.mutate_ordinary_source(
        rich_row["id"],
        operation="forgotten",
        reason="reinforcement availability guard",
        actor="test",
        call_id="call:reinforcement-availability",
    )
    before = _raw_canonical_row(engine, rich_row["id"])

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_patch_source_unavailable|ordinary_patch_cas_mismatch",
    ):
        engine.reinforce_ordinary_duplicate(
            rich_row["id"],
            entity_ids=[],
            last_accessed="2026-07-12T02:00:00Z",
            expected_project_id=rich_row["project_id"],
            expected_visibility=rich_row["visibility"],
            expected_source_class=rich_row["source_class"],
            expected_memory_type=rich_row["memory_type"],
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_project_id", "project:other"),
        ("expected_project_id", "project:legacy-global"),
        ("expected_visibility", "project"),
        ("expected_source_class", "telemetry"),
        ("expected_memory_type", "code"),
    ],
)
def test_duplicate_reinforcement_rejects_provenance_drift(
    engine,
    rich_row,
    field,
    value,
):
    before = _raw_canonical_row(engine, rich_row["id"])
    binding = {
        "expected_project_id": rich_row["project_id"],
        "expected_visibility": rich_row["visibility"],
        "expected_source_class": rich_row["source_class"],
        "expected_memory_type": rich_row["memory_type"],
    }
    binding[field] = value

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.reinforce_ordinary_duplicate(
            rich_row["id"],
            entity_ids=["entity:must-not-merge"],
            last_accessed="2026-07-12T02:00:00Z",
            **binding,
        )

    assert _raw_canonical_row(engine, rich_row["id"]) == before


@pytest.mark.parametrize(
    "replacements",
    [
        {"tags": ["private", "status:wrong"]},
        {
            "metadata_json": {
                "schema": "memory-index/compact-v2",
                "private": True,
                "quality": {"status": "wrong"},
            }
        },
    ],
)
def test_field_patch_cannot_change_source_availability_or_bypass_invalidation(
    engine,
    rich_row,
    monkeypatch,
    replacements,
):
    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    before = _task4_snapshot(engine._sqlite._conn)

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_patch_availability_change_requires_coordinator",
    ):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements=replacements,
            expected_project_id=rich_row["project_id"],
            require_source_available=True,
        )

    assert _task4_snapshot(engine._sqlite._conn) == before
    assert all(store.get(item.memory_id).status == "verified" for item in verified)


class _CoordinatorEmbedder:
    model_name = "coordinator-test"

    def embed(self, text):
        assert text
        return [0.25] * 1024


def _prepare_test_correction(_pipeline, current, new_content):
    normalized = " ".join(str(new_content or "").split())
    material = build_index_material(
        {"content": normalized},
        policy="legacy",
        model_name=_CoordinatorEmbedder.model_name,
    )
    metadata = metadata_with_index_material(current["metadata_json"], material)
    metadata.update(
        {
            "quality": {"status": "current"},
            "raw_content": normalized,
            "l0_abstract": material.vector_text,
            "l1_summary": material.search_text,
            "l2_content": normalized,
        }
    )
    return PreparedMemory(
        content=normalized,
        category=str(current["category"]),
        tier=str(current["tier"]),
        tags=tuple(current["tags"]),
        vector=tuple([0.25] * 1024),
        index_material=material,
        metadata=MappingProxyType(metadata),
    )


def _seed_verified_dependents(engine, source_id, monkeypatch):
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    engine._embedder = _CoordinatorEmbedder()
    engine.ensure_heavy_init = lambda: None
    source = engine._sqlite.get(source_id)
    counterpart_id = f"{source_id}-counterpart"
    assert (
        engine.register_memory(
            {
                "id": counterpart_id,
                "content": "Independent supporting evidence for transactional invalidation.",
                "memory_type": "experience",
                "source": "test",
                "source_class": "experience",
                "project_id": source["project_id"],
                "visibility": source["visibility"],
                "metadata_json": {"quality": {"status": "current"}},
            }
        )
        == counterpart_id
    )
    store = SynthesisStore(engine._sqlite._conn, engine=engine)
    verified = []
    for index in (1, 2):
        draft = store.create_draft(
            f"Verified dependent {index} combines the two source records.",
            [source_id, counterpart_id],
            synthesis_key=f"task4:{source_id}:{index}",
            validity_scope=source["project_id"],
            project_id=source["project_id"],
            visibility=source["visibility"],
            actor="test",
            call_id=f"call-task4-draft-{index}",
        )
        assert draft is not None
        verified.append(
            store.verify(
                draft.memory_id,
                "reviewer",
                f"call-task4-verify-{index}",
                draft.revision,
            )
        )
    return store, tuple(verified)


def _task4_snapshot(conn):
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "memories",
            "synthesis_artifacts",
            "behavior_graph_edges",
            "memory_lineage",
            "store_outbox",
            "memory_version",
        )
    }


def _pending_jobs(conn, tool_name, memory_ids, *, outbox_ids=None):
    expected_outbox_ids = set(outbox_ids or ())
    return [
        (row[1], json.loads(row[2]))
        for row in conn.execute(
            "SELECT outbox_id, status, payload_json FROM store_outbox "
            "WHERE tool_name = ? ORDER BY created_at, outbox_id",
            (tool_name,),
        ).fetchall()
        if (
            json.loads(row[2]).get("memory_id") in set(memory_ids)
            and (not expected_outbox_ids or row[0] in expected_outbox_ids)
        )
    ]


def test_corrected_content_commits_material_lineage_jobs_and_stales_dependents(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)
    before = engine._sqlite.get(rich_row["id"])
    before_version = read_memory_version(engine._sqlite._conn)

    result = OrdinaryMemoryMutationCoordinator(engine).replace_content(
        rich_row["id"],
        content="Materially corrected private evidence for the shared conclusion.",
        reason="user correction",
        actor="codex",
        call_id="call-task4-correct",
    )

    after = engine._sqlite.get(rich_row["id"])
    assert after["content"] == "Materially corrected private evidence for the shared conclusion."
    assert after["embedding_hash"] != before["embedding_hash"]
    assert after["metadata_json"]["quality"]["status"] == "current"
    assert after["metadata_json"]["quality"]["actor"] == "codex"
    assert after["metadata_json"]["quality"]["call_id"] == "call-task4-correct"
    assert after["metadata_json"]["last_correction"] == {
        "previous_content_hash": result.previous_content_hash,
        "current_content_hash": result.current_content_hash,
        "call_id": "call-task4-correct",
    }
    assert read_memory_version(engine._sqlite._conn) == before_version + 1
    assert result.stale_synthesis_ids == tuple(sorted(item.memory_id for item in verified))
    assert result.current_content_hash != result.previous_content_hash
    assert result.ordinary_index_job_id
    assert len(result.synthesis_index_job_ids) == len(verified)
    assert all(store.get(item.memory_id).status == "stale" for item in verified)
    assert all(store.get(item.memory_id).stale_reason == "source_changed" for item in verified)
    assert all(store.get(item.memory_id).verified_by_actor == "" for item in verified)
    assert all(store.get(item.memory_id).verified_by_call_id == "" for item in verified)
    ordinary_jobs = _pending_jobs(
        engine._sqlite._conn,
        "memory_index",
        [rich_row["id"]],
        outbox_ids=[result.ordinary_index_job_id],
    )
    synthesis_jobs = _pending_jobs(
        engine._sqlite._conn,
        "synthesis_index",
        [item.memory_id for item in verified],
        outbox_ids=result.synthesis_index_job_ids,
    )
    assert [payload["action"] for _status, payload in ordinary_jobs] == ["upsert"]
    assert {payload["action"] for _status, payload in synthesis_jobs} == {"delete"}
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM memory_lineage "
        "WHERE parent_memory_id = ? AND relation = 'synthesis_invalidated'",
        (rich_row["id"],),
    ).fetchone()[0] == len(verified)
    correction_lineage = json.loads(
        engine._sqlite._conn.execute(
            "SELECT metadata_json FROM memory_lineage "
            "WHERE memory_id = ? AND relation = 'ordinary_source_corrected'",
            (rich_row["id"],),
        ).fetchone()[0]
    )
    assert correction_lineage["previous_embedding_hash"] == before["embedding_hash"]
    assert correction_lineage["current_embedding_hash"] == after["embedding_hash"]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("wrong", "source_wrong"),
        ("deprecated", "source_deprecated"),
        ("forgotten", "source_forgotten"),
    ],
)
def test_unavailable_state_is_a_persistent_tombstone_and_stales_dependents(
    engine,
    rich_row,
    monkeypatch,
    state,
    reason,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    result = OrdinaryMemoryMutationCoordinator(engine).mark_unavailable(
        rich_row["id"],
        state=state,
        reason="user lifecycle decision",
        actor="codex",
        call_id=f"call-task4-{state}",
    )

    source = engine._sqlite.get(rich_row["id"])
    assert source is not None
    assert _source_is_available(source) is False
    assert source["metadata_json"]["quality"] == {
        "status": state,
        "reason": "user lifecycle decision",
        "actor": "codex",
        "call_id": f"call-task4-{state}",
        "changed_at": source["metadata_json"]["quality"]["changed_at"],
    }
    assert result.operation == state
    assert result.stale_synthesis_ids == tuple(sorted(item.memory_id for item in verified))
    assert all(store.get(item.memory_id).status == "stale" for item in verified)
    assert all(store.get(item.memory_id).stale_reason == reason for item in verified)
    ordinary_jobs = _pending_jobs(
        engine._sqlite._conn,
        "memory_index",
        [rich_row["id"]],
        outbox_ids=[result.ordinary_index_job_id],
    )
    synthesis_jobs = _pending_jobs(
        engine._sqlite._conn,
        "synthesis_index",
        [item.memory_id for item in verified],
        outbox_ids=result.synthesis_index_job_ids,
    )
    assert [payload["action"] for _status, payload in ordinary_jobs] == ["delete"]
    assert len(synthesis_jobs) == len(verified)
    assert {payload["action"] for _status, payload in synthesis_jobs} == {"delete"}


def test_delete_memory_tombstones_committed_source_and_stales_dependents(
    engine,
    rich_row,
    monkeypatch,
):
    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)

    assert engine.delete_memory(rich_row["id"]) is True

    source = engine._sqlite.get(rich_row["id"])
    assert source is not None
    assert _source_is_available(source) is False
    assert source["metadata_json"]["lifecycle_status"] == "forgotten"
    assert source["metadata_json"]["quality"]["actor"] == "context_engine"
    assert source["metadata_json"]["quality"]["call_id"].startswith(
        "internal:context_engine:delete:"
    )
    assert engine.get_memory_dict(rich_row["id"]) is None
    assert all(store.get(item.memory_id).status == "stale" for item in verified)
    jobs = _pending_jobs(
        engine._sqlite._conn,
        "memory_index",
        [rich_row["id"]],
    )
    assert [payload["action"] for _status, payload in jobs] == ["delete"]


def test_manual_batch_rejects_hard_delete_of_committed_source(engine, rich_row):
    before = _raw_canonical_row(engine, rich_row["id"])

    engine.begin_batch()
    try:
        assert engine.delete_memory(rich_row["id"]) is False
    finally:
        engine.rollback_batch()

    assert _raw_canonical_row(engine, rich_row["id"]) == before


def test_lineage_failure_rolls_back_source_dependent_and_outbox(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import ordinary_memory_mutation
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)
    before = _task4_snapshot(engine._sqlite._conn)

    def fail_lineage(*_args, **_kwargs):
        raise RuntimeError("injected lineage failure")

    monkeypatch.setattr(ordinary_memory_mutation, "record_memory_lineage", fail_lineage)
    with pytest.raises(RuntimeError, match="injected lineage failure"):
        OrdinaryMemoryMutationCoordinator(engine).replace_content(
            rich_row["id"],
            content="Correction that must not partially commit.",
            reason="test rollback",
            actor="codex",
            call_id="call-task4-lineage-failure",
        )

    assert _task4_snapshot(engine._sqlite._conn) == before


def test_scanner_is_defense_in_depth_not_required_for_immediate_block(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import synthesis_maintenance

    _store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)

    def fail_scanner(*_args, **_kwargs):
        raise AssertionError("scanner must not be needed for public blocking")

    monkeypatch.setattr(synthesis_maintenance, "scan_synthesis_integrity", fail_scanner)
    result = engine.mutate_ordinary_source(
        rich_row["id"],
        operation="wrong",
        reason="immediate public block",
        actor="codex",
        call_id="call-task4-public-block",
    )

    assert result.operation == "wrong"
    assert engine.get_memory_dict(rich_row["id"]) is None
    assert engine.get_memory(rich_row["id"]) is None
    assert engine.memory_exists(rich_row["id"]) is False
    assert rich_row["id"] not in engine.memory_ids()
    assert all(engine.get_memory_dict(item.memory_id) is None for item in verified)
    public_ids = {row["id"] for row in engine.iter_memories()}
    assert rich_row["id"] not in public_ids
    assert not ({item.memory_id for item in verified} & public_ids)


@pytest.mark.parametrize(
    "quality",
    ["store", "low_quality", {"status": "current", "decision": "store"}],
)
def test_public_gate_admits_persisted_pipeline_quality_states(
    engine,
    rich_row,
    quality,
):
    metadata = dict(rich_row["metadata_json"])
    metadata["quality"] = quality
    engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"metadata_json": metadata},
    )

    assert engine.get_memory_dict(rich_row["id"])["id"] == rich_row["id"]
    assert engine.get_memory(rich_row["id"]).id == rich_row["id"]
    assert engine.memory_exists(rich_row["id"]) is True


@pytest.mark.parametrize(
    ("tags", "metadata_status"),
    [(["audit", "status:pass"], None), (["research"], "reviewed")],
)
def test_public_gate_admits_existing_review_status_namespace(
    engine,
    rich_row,
    tags,
    metadata_status,
):
    metadata = dict(rich_row["metadata_json"])
    if metadata_status is not None:
        metadata["status"] = metadata_status
    engine.patch_ordinary_memory(
        rich_row["id"],
        replacements={"tags": tags, "metadata_json": metadata},
    )

    assert engine.get_memory_dict(rich_row["id"])["id"] == rich_row["id"]
    assert engine.get_memory(rich_row["id"]).id == rich_row["id"]


@pytest.mark.parametrize(
    "blocked_tag",
    ["status:replaced", "lifecycle:conflict"],
)
def test_blocked_lifecycle_overrides_healthy_review_status(
    engine,
    rich_row,
    blocked_tag,
):
    metadata = dict(rich_row["metadata_json"])
    metadata["status"] = "reviewed"
    engine._sqlite._conn.execute(
        "UPDATE memories SET tags = ?, metadata_json = ? WHERE id = ?",
        (
            json.dumps(["audit", "status:pass", blocked_tag]),
            json.dumps(metadata),
            rich_row["id"],
        ),
    )
    engine._sqlite._conn.commit()
    engine._memories[rich_row["id"]] = engine._sqlite.get(rich_row["id"])

    assert engine.get_memory_dict(rich_row["id"]) is None
    assert engine.get_memory(rich_row["id"]) is None
    assert engine.memory_exists(rich_row["id"]) is False


def _clear_index_material_for_legacy_row(engine, memory_id):
    metadata = dict(engine._sqlite.get(memory_id)["metadata_json"])
    metadata.pop("memory_index", None)
    engine._sqlite._conn.execute(
        "UPDATE memories SET embedding_text = '', embedding_hash = '', "
        "search_text = '', metadata_json = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), memory_id),
    )
    engine._sqlite._conn.commit()
    engine._memories[memory_id] = engine._sqlite.get(memory_id)


def test_unavailable_legacy_empty_hash_materializes_tombstone_and_job_atomically(
    engine,
    rich_row,
):
    _clear_index_material_for_legacy_row(engine, rich_row["id"])
    before_version = read_memory_version(engine._sqlite._conn)

    result = engine.mutate_ordinary_source(
        rich_row["id"],
        operation="forgotten",
        reason="legacy audit replacement",
        actor="maintenance_daemon",
        call_id="call:legacy-empty-hash",
    )

    canonical = engine._sqlite.get(rich_row["id"])
    material = build_index_material(
        {"content": rich_row["content"]},
        policy="legacy-fallback",
        model_name="unknown",
    )
    assert canonical["embedding_text"] == material.vector_text
    assert canonical["embedding_hash"] == material.embedding_hash
    assert canonical["search_text"] == material.search_text
    assert (
        canonical["metadata_json"]["memory_index"]
        == (metadata_with_index_material({}, material)["memory_index"])
    )
    assert canonical["metadata_json"]["quality"]["status"] == "forgotten"
    assert _source_is_available(canonical) is False
    assert read_memory_version(engine._sqlite._conn) == before_version + 1
    assert result.peer_index_job_ids == ()
    jobs = _pending_jobs(
        engine._sqlite._conn,
        "memory_index",
        [rich_row["id"]],
        outbox_ids=[result.ordinary_index_job_id],
    )
    assert len(jobs) == 1
    status, payload = jobs[0]
    assert status in {"pending", "done"}
    assert payload["action"] == "delete"
    assert payload["expected_embedding_hash"] == material.embedding_hash
    assert payload["material_revision"] == material.embedding_hash


def test_unavailable_legacy_material_and_tombstone_roll_back_on_job_failure(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import ordinary_memory_mutation

    _clear_index_material_for_legacy_row(engine, rich_row["id"])
    before = _task4_snapshot(engine._sqlite._conn)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected legacy delete enqueue failure")

    monkeypatch.setattr(
        ordinary_memory_mutation,
        "enqueue_memory_index_delete",
        fail_enqueue,
    )

    with pytest.raises(RuntimeError, match="legacy delete enqueue failure"):
        engine.mutate_ordinary_source(
            rich_row["id"],
            operation="forgotten",
            reason="legacy rollback proof",
            actor="maintenance_daemon",
            call_id="call:legacy-empty-hash-rollback",
        )

    assert _task4_snapshot(engine._sqlite._conn) == before
    canonical = engine._sqlite.get(rich_row["id"])
    assert canonical["embedding_hash"] == ""
    assert _source_is_available(canonical) is True


def test_prepare_correction_preserves_persisted_index_policy_and_model(rich_row):
    current = copy.deepcopy(rich_row)
    material = build_index_material(
        {
            "content": current["content"],
            "raw_content": current["raw_content"],
            "l0_abstract": current["l0_abstract"],
            "l1_summary": current["l1_summary"],
            "l2_content": current["l2_content"],
        },
        policy="compact-v2",
        model_name=_CoordinatorEmbedder.model_name,
    )
    current.update(
        {
            "embedding_text": material.vector_text,
            "embedding_hash": material.embedding_hash,
            "search_text": material.search_text,
            "metadata_json": metadata_with_index_material(current["metadata_json"], material),
        }
    )
    before = copy.deepcopy(current)

    prepared = MemoryPipeline(embedder=_CoordinatorEmbedder()).prepare_correction(
        current,
        "Corrected compact evidence preserves the existing material contract for recall.",
    )

    assert prepared.index_material.policy == "compact-v2"
    assert prepared.index_material.model_name == _CoordinatorEmbedder.model_name
    assert prepared.metadata["memory_index"]["policy"] == "compact-v2"
    assert prepared.metadata["memory_index"]["model_name"] == _CoordinatorEmbedder.model_name
    assert prepared.metadata["quality"]["status"] == "current"
    assert current == before


def test_stale_verified_dependents_is_caller_owned_and_clears_verification(
    engine,
    rich_row,
    monkeypatch,
):
    store, verified = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    conn = engine._sqlite._conn
    before_version = read_memory_version(conn)
    before_outbox = conn.execute(
        "SELECT outbox_id, payload_json FROM store_outbox ORDER BY outbox_id"
    ).fetchall()

    conn.execute("BEGIN IMMEDIATE")
    try:
        affected = store.stale_verified_dependents(
            rich_row["id"],
            reason="source_changed",
            actor="codex",
            call_id="call-task4-caller-owned",
        )
        assert conn.in_transaction
        assert tuple(item[0] for item in affected) == tuple(
            sorted(item.memory_id for item in verified)
        )
        assert all(item[1] == 1 for item in affected)
        assert all(item[2] == rich_row["project_id"] for item in affected)
        assert read_memory_version(conn) == before_version
        assert (
            conn.execute(
                "SELECT outbox_id, payload_json FROM store_outbox ORDER BY outbox_id"
            ).fetchall()
            == before_outbox
        )
        assert all(store.get(item.memory_id).status == "stale" for item in verified)
        assert all(store.get(item.memory_id).last_verified_at == "" for item in verified)
        assert all(store.get(item.memory_id).verified_by_actor == "" for item in verified)
        assert all(store.get(item.memory_id).verified_by_call_id == "" for item in verified)
    finally:
        conn.rollback()

    assert all(store.get(item.memory_id).status == "verified" for item in verified)
    assert read_memory_version(conn) == before_version


def test_post_commit_replay_failure_preserves_committed_jobs(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import synthesis_maintenance
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    _seed_verified_dependents(engine, rich_row["id"], monkeypatch)

    def fail_replay(*_args, **_kwargs):
        raise RuntimeError("derived projection unavailable")

    monkeypatch.setattr(synthesis_maintenance, "replay_memory_index_jobs", fail_replay)
    monkeypatch.setattr(synthesis_maintenance, "replay_synthesis_index_jobs", fail_replay)

    result = OrdinaryMemoryMutationCoordinator(engine).mark_unavailable(
        rich_row["id"],
        state="forgotten",
        reason="retain durable repair evidence",
        actor="codex",
        call_id="call-task4-replay-failure",
    )

    assert engine._sqlite.get(rich_row["id"])["metadata_json"]["quality"]["status"] == "forgotten"
    assert (
        engine._sqlite._conn.execute(
            "SELECT status FROM store_outbox WHERE outbox_id = ?",
            (result.ordinary_index_job_id,),
        ).fetchone()[0]
        == "pending"
    )
    assert set(result.synthesis_index_job_ids)


def test_coordinator_rejects_ambient_transaction_without_mutation(
    engine,
    rich_row,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
        OrdinaryMemoryMutationError,
    )

    before = _task4_snapshot(engine._sqlite._conn)
    engine._sqlite._conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(
            OrdinaryMemoryMutationError,
            match="ordinary_source_requires_clean_transaction",
        ):
            OrdinaryMemoryMutationCoordinator(engine).mark_unavailable(
                rich_row["id"],
                state="wrong",
                reason="ambient transaction is not publishable",
                actor="codex",
                call_id="call-task4-ambient",
            )
    finally:
        engine._sqlite._conn.rollback()

    assert _task4_snapshot(engine._sqlite._conn) == before


def test_coordinator_rejects_transaction_opened_before_batch_ownership(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
        OrdinaryMemoryMutationError,
    )

    storage = engine._sqlite
    conn = storage._conn
    original_begin = storage._begin_batch_scope

    def begin_after_ambient_transaction():
        conn.execute("BEGIN IMMEDIATE")
        original_begin()

    monkeypatch.setattr(storage, "_begin_batch_scope", begin_after_ambient_transaction)
    before = _task4_snapshot(conn)
    try:
        with pytest.raises(
            OrdinaryMemoryMutationError,
            match="ordinary_source_requires_clean_transaction",
        ):
            OrdinaryMemoryMutationCoordinator(engine).mark_unavailable(
                rich_row["id"],
                state="wrong",
                reason="batch ownership changed before BEGIN",
                actor="codex",
                call_id="call-task4-batch-ownership",
            )
    finally:
        conn.rollback()

    assert _task4_snapshot(conn) == before


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("content", "concurrent scanner content"),
        ("project_id", "project:concurrent-owner"),
        ("worth_failure", 99),
        ("decay_multiplier", 0.01),
    ],
)
def test_coordinator_rechecks_scanner_preconditions_inside_transaction(
    engine,
    rich_row,
    monkeypatch,
    column,
    replacement,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationError,
    )

    storage = engine._sqlite
    original_begin = storage._begin_batch_scope
    second = ContextEngine(use_sqlite=True)
    competing_snapshot = {}

    def begin_after_competing_update():
        second._sqlite._conn.execute(
            f"UPDATE memories SET {column} = ? WHERE id = ?",
            (replacement, rich_row["id"]),
        )
        second._sqlite._conn.commit()
        competing_snapshot["value"] = _task4_snapshot(second._sqlite._conn)
        original_begin()

    monkeypatch.setattr(storage, "_begin_batch_scope", begin_after_competing_update)
    try:
        with pytest.raises(
            OrdinaryMemoryMutationError,
            match="ordinary_source_precondition_mismatch",
        ):
            engine.mutate_ordinary_source(
                rich_row["id"],
                operation="forgotten",
                reason="scanner candidate changed before canonical mutation",
                actor="scan_memory_decay",
                call_id=f"call-scanner-race-{column}",
                expected_project_id=rich_row["project_id"],
                expected_content_hash=synthesis_content_hash(rich_row["content"]),
                expected_source_snapshot={
                    "decay_multiplier": rich_row["decay_multiplier"],
                    "metadata_json": rich_row["metadata_json"],
                    "tags": rich_row["tags"],
                    "worth_failure": rich_row["worth_failure"],
                    "worth_success": rich_row["worth_success"],
                },
                require_source_available=True,
            )

        assert _task4_snapshot(storage._conn) == competing_snapshot["value"]
    finally:
        second._sqlite._conn.close()


@pytest.mark.parametrize("peer_change", ["tombstone", "worth"])
def test_coordinator_rechecks_duplicate_survivor_inside_transaction(
    engine,
    rich_row,
    monkeypatch,
    peer_change,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationError,
    )

    survivor = _seed_rich_row(engine, "scanner-survivor")
    storage = engine._sqlite
    original_begin = storage._begin_batch_scope
    second = ContextEngine(use_sqlite=True)
    competing_snapshot = {}

    def begin_after_survivor_change():
        if peer_change == "tombstone":
            second._sqlite._conn.execute(
                "UPDATE memories SET tags = ?, metadata_json = ? WHERE id = ?",
                (
                    json.dumps(["status:wrong"]),
                    json.dumps({"quality": {"status": "wrong"}}),
                    survivor["id"],
                ),
            )
        else:
            second._sqlite._conn.execute(
                "UPDATE memories SET worth_success = 0, worth_failure = 100 WHERE id = ?",
                (survivor["id"],),
            )
        second._sqlite._conn.commit()
        competing_snapshot["value"] = _task4_snapshot(second._sqlite._conn)
        original_begin()

    monkeypatch.setattr(storage, "_begin_batch_scope", begin_after_survivor_change)
    peer_snapshot = {
        "access_count": survivor["access_count"],
        "content_hash": synthesis_content_hash(survivor["content"]),
        "created_at": survivor["created_at"],
        "decay_multiplier": survivor["decay_multiplier"],
        "metadata_json": survivor["metadata_json"],
        "project_id": survivor["project_id"],
        "tags": survivor["tags"],
        "worth_failure": survivor["worth_failure"],
        "worth_success": survivor["worth_success"],
    }
    try:
        with pytest.raises(
            OrdinaryMemoryMutationError,
            match="ordinary_source_precondition_mismatch",
        ):
            engine.mutate_ordinary_source(
                rich_row["id"],
                operation="forgotten",
                reason="duplicate survivor changed before canonical mutation",
                actor="maintenance_daemon",
                call_id=f"call-survivor-race-{peer_change}",
                expected_project_id=rich_row["project_id"],
                expected_peer_snapshots={survivor["id"]: peer_snapshot},
                require_source_available=True,
            )

        assert _task4_snapshot(storage._conn) == competing_snapshot["value"]
    finally:
        second._sqlite._conn.close()


def test_coordinator_rejects_cross_project_peer_with_full_zero_write(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationError,
    )

    survivor = _seed_rich_row(
        engine,
        "cross-project-survivor",
        project_id="project:other",
    )
    peer_snapshot = {
        "category": survivor["category"],
        "content_hash": synthesis_content_hash(survivor["content"]),
        "embedding_hash": survivor["embedding_hash"],
        "metadata_json": survivor["metadata_json"],
        # A caller can forge a same-project declaration. Canonical SQLite
        # ownership must still reject the actual cross-project peer.
        "project_id": rich_row["project_id"],
        "tags": survivor["tags"],
        "worth_failure": survivor["worth_failure"],
        "worth_success": survivor["worth_success"],
    }
    survivor_metadata = copy.deepcopy(survivor["metadata_json"])
    survivor_metadata["merged_from"] = [{"memory_id": rich_row["id"]}]
    before = _task4_snapshot(engine._sqlite._conn)
    cached_before = copy.deepcopy(engine._memories)
    original_begin = engine._sqlite._begin_batch_scope
    transaction_started = False

    def track_transaction_start():
        nonlocal transaction_started
        transaction_started = True
        original_begin()

    monkeypatch.setattr(
        engine._sqlite,
        "_begin_batch_scope",
        track_transaction_start,
    )

    with pytest.raises(
        OrdinaryMemoryMutationError,
        match="ordinary_source_peer_project_mismatch",
    ):
        engine.mutate_ordinary_source(
            rich_row["id"],
            operation="forgotten",
            reason="cross-project coordinator bypass attempt",
            actor="memory_gc",
            call_id="call-cross-project-peer",
            expected_project_id=rich_row["project_id"],
            expected_content_hash=synthesis_content_hash(rich_row["content"]),
            expected_source_snapshot={
                "category": rich_row["category"],
                "embedding_hash": rich_row["embedding_hash"],
                "metadata_json": rich_row["metadata_json"],
                "tags": rich_row["tags"],
                "worth_failure": rich_row["worth_failure"],
                "worth_success": rich_row["worth_success"],
            },
            expected_peer_snapshots={survivor["id"]: peer_snapshot},
            peer_metadata_replacements={survivor["id"]: survivor_metadata},
            require_source_available=True,
        )

    assert transaction_started is True
    assert _task4_snapshot(engine._sqlite._conn) == before
    assert engine._memories == cached_before


def test_patch_ordinary_expected_project_mismatch_is_zero_write(engine, rich_row):
    before = _task4_snapshot(engine._sqlite._conn)
    cached = copy.deepcopy(engine._memories[rich_row["id"]])

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"category": "decision"},
            expected_project_id="project:other",
        )

    assert _task4_snapshot(engine._sqlite._conn) == before
    assert engine._memories[rich_row["id"]] == cached


def test_patch_ordinary_rechecks_expected_tags_inside_transaction(
    engine,
    rich_row,
    monkeypatch,
):
    storage = engine._sqlite
    original_begin = storage._begin_batch_scope
    second = ContextEngine(use_sqlite=True)
    competing_snapshot = {}

    def begin_after_competing_tag_update():
        second._sqlite._conn.execute(
            "UPDATE memories SET tags = ? WHERE id = ?",
            (
                json.dumps([*rich_row["tags"], "classification:concurrent"]),
                rich_row["id"],
            ),
        )
        second._sqlite._conn.commit()
        competing_snapshot["value"] = _task4_snapshot(second._sqlite._conn)
        original_begin()

    monkeypatch.setattr(storage, "_begin_batch_scope", begin_after_competing_tag_update)
    try:
        with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
            engine.patch_ordinary_memory(
                rich_row["id"],
                replacements={"category": "decision"},
                expected_project_id=rich_row["project_id"],
                expected_tags=rich_row["tags"],
                require_source_available=True,
            )

        assert _task4_snapshot(storage._conn) == competing_snapshot["value"]
    finally:
        second._sqlite._conn.close()


def test_patch_ordinary_rechecks_expected_category_inside_transaction(
    engine,
    rich_row,
    monkeypatch,
):
    storage = engine._sqlite
    original_begin = storage._begin_batch_scope
    second = ContextEngine(use_sqlite=True)
    competing_snapshot = {}

    def begin_after_competing_category_update():
        second._sqlite._conn.execute(
            "UPDATE memories SET category = ? WHERE id = ?",
            ("human-reviewed", rich_row["id"]),
        )
        second._sqlite._conn.commit()
        competing_snapshot["value"] = _task4_snapshot(second._sqlite._conn)
        original_begin()

    monkeypatch.setattr(
        storage,
        "_begin_batch_scope",
        begin_after_competing_category_update,
    )
    try:
        with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
            engine.patch_ordinary_memory(
                rich_row["id"],
                replacements={"category": "decision"},
                expected_project_id=rich_row["project_id"],
                expected_tags=rich_row["tags"],
                expected_category=rich_row["category"],
                require_source_available=True,
            )

        assert _task4_snapshot(storage._conn) == competing_snapshot["value"]
    finally:
        second._sqlite._conn.close()


def test_patch_ordinary_available_guard_rejects_tombstone(engine, rich_row):
    conn = engine._sqlite._conn
    conn.execute(
        "UPDATE memories SET tags = ?, metadata_json = ? WHERE id = ?",
        (
            json.dumps(["status:wrong"]),
            json.dumps({"quality": {"status": "wrong"}}),
            rich_row["id"],
        ),
    )
    conn.commit()
    before = _task4_snapshot(conn)
    cached = copy.deepcopy(engine._memories[rich_row["id"]])

    with pytest.raises(
        OrdinaryMemoryConflict,
        match="ordinary_patch_source_unavailable",
    ):
        engine.patch_ordinary_memory(
            rich_row["id"],
            replacements={"category": "decision"},
            expected_project_id=rich_row["project_id"],
            require_source_available=True,
        )

    assert _task4_snapshot(conn) == before
    assert engine._memories[rich_row["id"]] == cached


def test_apply_feedback_forwards_project_and_availability_guards(engine, rich_row):
    before = _task4_snapshot(engine._sqlite._conn)

    with pytest.raises(OrdinaryMemoryConflict, match="ordinary_patch_cas_mismatch"):
        engine.apply_ordinary_feedback(
            rich_row["id"],
            "adopted",
            expected_project_id="project:other",
            require_source_available=True,
        )

    assert _task4_snapshot(engine._sqlite._conn) == before


def test_correction_preparation_failure_is_stable_and_zero_write(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
        OrdinaryMemoryMutationError,
    )

    def fail_preparation(*_args, **_kwargs):
        raise RuntimeError("embedder offline")

    monkeypatch.setattr(MemoryPipeline, "prepare_correction", fail_preparation)
    before = _task4_snapshot(engine._sqlite._conn)

    with pytest.raises(
        OrdinaryMemoryMutationError,
        match="ordinary_source_preparation_failed",
    ):
        OrdinaryMemoryMutationCoordinator(engine).replace_content(
            rich_row["id"],
            content="A correction whose material cannot be prepared.",
            reason="preparation failure contract",
            actor="codex",
            call_id="call-preparation-failure",
        )

    assert _task4_snapshot(engine._sqlite._conn) == before


@pytest.mark.parametrize(
    ("content", "failure"),
    [
        ("", ValueError("correction_content_required")),
        ("quality failure", RuntimeError("quality pipeline unavailable")),
        ("embedding failure", RuntimeError("embedder unavailable")),
    ],
)
def test_compat_content_update_fails_closed_on_preparation_errors(
    engine,
    rich_row,
    monkeypatch,
    content,
    failure,
):
    def fail_preparation(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(MemoryPipeline, "prepare_correction", fail_preparation)
    before = _task4_snapshot(engine._sqlite._conn)

    assert engine.update_memory(rich_row["id"], content=content) is False

    assert _task4_snapshot(engine._sqlite._conn) == before


def test_concurrent_tombstone_cannot_be_overwritten_by_stale_correction(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
        OrdinaryMemoryMutationError,
    )

    second = ContextEngine(use_sqlite=True)
    try:
        before_version = read_memory_version(engine._sqlite._conn)

        def interleaved_prepare(_pipeline, current, new_content):
            second.mutate_ordinary_source(
                rich_row["id"],
                operation="wrong",
                reason="concurrent lifecycle decision",
                actor="second-engine",
                call_id="call-task4-concurrent-tombstone",
            )
            return _prepare_test_correction(_pipeline, current, new_content)

        monkeypatch.setattr(MemoryPipeline, "prepare_correction", interleaved_prepare)
        with pytest.raises(
            OrdinaryMemoryMutationError,
            match="ordinary_source_cas_mismatch",
        ):
            OrdinaryMemoryMutationCoordinator(engine).replace_content(
                rich_row["id"],
                content="A stale correction must not revive a concurrent tombstone.",
                reason="concurrent correction",
                actor="first-engine",
                call_id="call-task4-concurrent-correction",
            )

        source = engine._sqlite.get(rich_row["id"])
        assert source["metadata_json"]["quality"]["status"] == "wrong"
        assert _source_is_available(source) is False
        assert read_memory_version(engine._sqlite._conn) == before_version + 1
    finally:
        second._sqlite._conn.close()


def test_source_mutation_stales_transitive_verified_dependents(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    store, direct = _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    counterpart_id = f"{rich_row['id']}-counterpart"
    downstream = store.create_draft(
        "A downstream verified synthesis depends on a direct synthesis source.",
        [direct[0].memory_id, counterpart_id],
        synthesis_key=f"task4:downstream:{rich_row['id']}",
        validity_scope=rich_row["project_id"],
        project_id=rich_row["project_id"],
        visibility=rich_row["visibility"],
        actor="test",
        call_id="call-task4-downstream-draft",
    )
    assert downstream is not None
    downstream = store.verify(
        downstream.memory_id,
        "reviewer",
        "call-task4-downstream-verify",
        downstream.revision,
    )
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)

    result = OrdinaryMemoryMutationCoordinator(engine).replace_content(
        rich_row["id"],
        content="A correction invalidates direct and transitive synthesis dependents.",
        reason="transitive evidence changed",
        actor="codex",
        call_id="call-task4-transitive",
    )

    expected_ids = tuple(sorted([*(item.memory_id for item in direct), downstream.memory_id]))
    assert result.stale_synthesis_ids == expected_ids
    assert len(result.synthesis_index_job_ids) == len(expected_ids)
    assert all(store.get(memory_id).status == "stale" for memory_id in expected_ids)
    delete_jobs = _pending_jobs(
        engine._sqlite._conn,
        "synthesis_index",
        expected_ids,
        outbox_ids=result.synthesis_index_job_ids,
    )
    assert {
        (payload["memory_id"], payload["revision"], payload["action"])
        for _status, payload in delete_jobs
    } == {(memory_id, 1, "delete") for memory_id in expected_ids}


def test_outbox_failure_rolls_back_source_dependent_and_lineage(
    engine,
    rich_row,
    monkeypatch,
):
    from plastic_promise.core import ordinary_memory_mutation
    from plastic_promise.core.ordinary_memory_mutation import (
        OrdinaryMemoryMutationCoordinator,
    )

    _seed_verified_dependents(engine, rich_row["id"], monkeypatch)
    monkeypatch.setattr(MemoryPipeline, "prepare_correction", _prepare_test_correction)
    before = _task4_snapshot(engine._sqlite._conn)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected ordinary outbox failure")

    monkeypatch.setattr(
        ordinary_memory_mutation,
        "enqueue_memory_index_upsert",
        fail_enqueue,
    )
    with pytest.raises(RuntimeError, match="injected ordinary outbox failure"):
        OrdinaryMemoryMutationCoordinator(engine).replace_content(
            rich_row["id"],
            content="Correction that must not publish a partial outbox.",
            reason="test rollback",
            actor="codex",
            call_id="call-task4-outbox-failure",
        )

    assert _task4_snapshot(engine._sqlite._conn) == before
