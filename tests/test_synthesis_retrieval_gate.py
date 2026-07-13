from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import plastic_promise.core.synthesis_retrieval as retrieval
from plastic_promise.core import context_engine as context_engine_module
from plastic_promise.core.context_engine import ContextEngine, ContextItem, ContextPack
from plastic_promise.core.retrieval_planner import RetrievalPlan
from plastic_promise.core.synthesis import (
    SynthesisStore,
    canonical_memory_hash,
    canonical_synthesis_binding,
    ensure_synthesis_schema,
    source_fingerprint,
    synthesis_binding_hash,
    synthesis_content_hash,
)
from plastic_promise.mcp.tools import memory as memory_tools

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(":memory:")
    database.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            memory_type TEXT,
            project_id TEXT NOT NULL DEFAULT 'project:test',
            visibility TEXT NOT NULL DEFAULT 'project',
            source_class TEXT NOT NULL DEFAULT 'experience',
            origin_kind TEXT NOT NULL DEFAULT '',
            origin_uri TEXT NOT NULL DEFAULT '',
            origin_ref TEXT NOT NULL DEFAULT '',
            origin_hash TEXT NOT NULL DEFAULT '',
            embedding_hash TEXT NOT NULL DEFAULT '',
            embedding_text TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            tags TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{}',
            access_count INTEGER NOT NULL DEFAULT 0,
            worth_success INTEGER NOT NULL DEFAULT 0,
            worth_failure INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    ensure_synthesis_schema(database)
    database.execute(
        """
        CREATE TABLE behavior_graph_edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            relation TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}'
        )
        """
    )
    database.execute("CREATE TABLE memory_version (version INTEGER)")
    database.execute("INSERT INTO memory_version (version) VALUES (7)")
    try:
        yield database
    finally:
        database.close()


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    content: str = "source content",
    memory_type: str = "experience",
    visibility: str = "project",
    tags: object = (),
    metadata: object | None = None,
    synthesis_key: str | None = None,
) -> dict[str, object]:
    from plastic_promise.core.memory_index import (
        LEGACY_POLICY,
        build_index_material,
        metadata_with_index_material,
    )

    material = build_index_material(
        {"content": content},
        policy=LEGACY_POLICY,
        model_name="synthesis-index-test",
    )
    is_synthesis = memory_type.strip().casefold() == "synthesis"
    metadata_payload = dict({} if metadata is None else metadata)
    metadata_payload = metadata_with_index_material(metadata_payload, material)
    if is_synthesis:
        metadata_payload.update(
            {
                "synthesis_key": synthesis_key or f"key:{memory_id}",
                "synthesis_revision": 1,
            }
        )
    payload: dict[str, object] = {
        "id": memory_id,
        "content": content,
        "memory_type": memory_type,
        "project_id": "project:test",
        "visibility": visibility,
        "source_class": "synthesis" if is_synthesis else "experience",
        "origin_kind": "synthesis" if is_synthesis else "memory",
        "origin_uri": "memory://test",
        "origin_ref": memory_id,
        "origin_hash": synthesis_content_hash(content) if is_synthesis else f"origin:{memory_id}",
        "embedding_hash": material.embedding_hash,
        "embedding_text": material.vector_text,
        "search_text": material.search_text,
        "tags": json.dumps(tags),
        "metadata_json": "",
    }
    if is_synthesis:
        binding = canonical_synthesis_binding(
            {**payload, "metadata_json": metadata_payload},
            material,
        )
        metadata_payload["synthesis_binding"] = binding
        metadata_payload["synthesis_binding_hash"] = synthesis_binding_hash(binding)
    payload["metadata_json"] = json.dumps(metadata_payload)
    conn.execute(
        """
        INSERT INTO memories (
            id, content, memory_type, project_id, visibility, source_class,
            origin_kind, origin_uri, origin_ref, origin_hash, embedding_hash,
            embedding_text, search_text, tags, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(payload.values()),
    )
    return payload


def _add_edge(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    relation: str,
    metadata: object,
) -> None:
    edge_id = f"edge:{conn.execute('SELECT COUNT(*) FROM behavior_graph_edges').fetchone()[0]}"
    metadata_json = metadata if isinstance(metadata, str) else json.dumps(metadata)
    conn.execute(
        """
        INSERT INTO behavior_graph_edges (id, source, target, relation, metadata_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (edge_id, source, target, relation, metadata_json),
    )


def _add_synthesis(
    conn: sqlite3.Connection,
    *,
    synthesis_id: str = "s1",
    status: str | None = "verified",
    synthesis_visibility: str = "project",
    source_id: str = "source-1",
    source_content: str = "source content",
) -> None:
    source = _insert_memory(conn, source_id, content=source_content)
    second_source_id = f"{source_id}:secondary"
    second_source = _insert_memory(
        conn,
        second_source_id,
        content=f"independent {source_content}",
    )
    synthesis = _insert_memory(
        conn,
        synthesis_id,
        content="candidate synthesis content",
        memory_type="synthesis",
        visibility=synthesis_visibility,
        synthesis_key=f"key:{synthesis_id}",
    )
    snapshots = {
        source_id: canonical_memory_hash(source),
        second_source_id: canonical_memory_hash(second_source),
    }
    for parent_id, content_hash in snapshots.items():
        _add_edge(
            conn,
            synthesis_id,
            parent_id,
            "derived_from",
            {
                "observed_content_hash": content_hash,
                "synthesis_revision": 1,
                "support_scope": "project:test",
            },
        )
    if status is not None:
        synthesis_metadata = json.loads(str(synthesis["metadata_json"]))
        verification_time = "2026-07-10T00:00:00Z" if status == "verified" else ""
        verification_actor = "reviewer" if status == "verified" else ""
        verification_call_id = f"call:verify:{synthesis_id}" if status == "verified" else ""
        conn.execute(
            """
            INSERT INTO synthesis_artifacts (
                memory_id, synthesis_key, status, support_count, validity_scope,
                source_fingerprint, last_verified_at, verified_by_actor,
                verified_by_call_id, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                synthesis_id,
                f"key:{synthesis_id}",
                status,
                2,
                "project:test",
                source_fingerprint(snapshots),
                verification_time,
                verification_actor,
                verification_call_id,
                json.dumps(
                    {
                        "project_id": "project:test",
                        "visibility": synthesis_visibility,
                        "synthesis_binding": synthesis_metadata["synthesis_binding"],
                        "synthesis_binding_hash": synthesis_metadata["synthesis_binding_hash"],
                    }
                ),
                "2026-07-10T00:00:00Z",
                "2026-07-10T00:00:00Z",
            ),
        )


def _replace_synthesis_source(
    conn: sqlite3.Connection,
    *,
    synthesis_id: str,
    old_source_id: str,
    new_source_id: str,
) -> None:
    source = retrieval._load_memory(conn, new_source_id)
    assert source is not None
    row = conn.execute(
        "SELECT metadata_json FROM behavior_graph_edges "
        "WHERE source = ? AND target = ? AND relation = 'derived_from'",
        (synthesis_id, old_source_id),
    ).fetchone()
    assert row is not None
    metadata = json.loads(row[0])
    metadata["observed_content_hash"] = canonical_memory_hash(source)
    source_control = conn.execute(
        "SELECT revision FROM synthesis_artifacts WHERE memory_id = ?",
        (new_source_id,),
    ).fetchone()
    if source_control is not None:
        metadata["source_revision"] = source_control[0]
    conn.execute(
        "UPDATE behavior_graph_edges SET target = ?, metadata_json = ? "
        "WHERE source = ? AND target = ? AND relation = 'derived_from'",
        (new_source_id, json.dumps(metadata), synthesis_id, old_source_id),
    )
    snapshots = {
        source_id: json.loads(metadata_json)["observed_content_hash"]
        for source_id, metadata_json in conn.execute(
            "SELECT target, metadata_json FROM behavior_graph_edges "
            "WHERE source = ? AND relation = 'derived_from'",
            (synthesis_id,),
        )
    }
    conn.execute(
        "UPDATE synthesis_artifacts SET support_count = ?, source_fingerprint = ? "
        "WHERE memory_id = ?",
        (len(snapshots), source_fingerprint(snapshots), synthesis_id),
    )


def _evaluate(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    allow_review: bool = False,
) -> retrieval.SynthesisGateResult:
    conn.commit()
    return retrieval.evaluate_synthesis_ids(
        conn,
        ids,
        allow_review=allow_review,
        memory_version=7,
    )


def test_ordinary_memory_is_admitted_in_input_order_and_deduplicated(
    conn: sqlite3.Connection,
) -> None:
    _insert_memory(conn, "ordinary-1")
    _insert_memory(conn, "ordinary-2")

    result = _evaluate(conn, ["ordinary-2", "ordinary-1", "ordinary-2"])

    assert result.items == ("ordinary-2", "ordinary-1")
    assert result.dropped_ids == ()
    assert result.degradations == ()


def test_direct_evaluator_drops_governed_candidate_during_open_transaction(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    _insert_memory(conn, "ordinary-during-transaction")
    conn.commit()
    conn.execute("BEGIN")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = retrieval.evaluate_synthesis_ids(
        conn,
        ["s1", "ordinary-during-transaction"],
        memory_version=7,
    )

    assert result.items == ("ordinary-during-transaction",)
    assert result.degradations == ({"id": "s1", "reason": "transaction_open"},)


def test_evaluator_rechecks_transaction_after_version_read(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    original_read_version = retrieval.read_memory_version
    calls = 0

    def read_then_open_transaction(database):
        nonlocal calls
        version = original_read_version(database)
        calls += 1
        if calls == 1:
            database.execute("BEGIN")
        return version

    monkeypatch.setattr(retrieval, "read_memory_version", read_then_open_transaction)

    result = retrieval.evaluate_synthesis_ids(conn, ["s1"], memory_version=7)

    assert result.items == ()
    assert result.degradations == ({"id": "s1", "reason": "transaction_open"},)
    conn.rollback()


def test_missing_memory_is_dropped_with_stable_reason(conn: sqlite3.Connection) -> None:
    result = _evaluate(conn, ["missing"])

    assert result.items == ()
    assert result.dropped_ids == ("missing",)
    assert result.degradations == ({"id": "missing", "reason": "candidate_missing"},)


def test_missing_canonical_memory_type_fails_closed(conn: sqlite3.Connection) -> None:
    _insert_memory(conn, "unknown-type")
    conn.execute("UPDATE memories SET memory_type = NULL WHERE id = 'unknown-type'")

    result = _evaluate(conn, ["unknown-type"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "candidate_type_invalid"


def test_control_row_prevents_memory_type_drift_from_bypassing_gate(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.execute("UPDATE memories SET memory_type = 'experience' WHERE id = 's1'")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.dropped_ids == ("s1",)
    assert result.degradations == ({"id": "s1", "reason": "candidate_type_mismatch"},)


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("content", "UNGOVERNED DIRECT CONTENT", "candidate_content_mismatch"),
        ("project_id", "project:other", "candidate_project_mismatch"),
        ("visibility", "global", "candidate_visibility_mismatch"),
        ("source_class", "experience", "candidate_binding_mismatch"),
        ("origin_kind", "memory", "candidate_binding_mismatch"),
        ("origin_hash", "DRIFTED ORIGIN", "candidate_binding_mismatch"),
        ("embedding_text", "DRIFTED VECTOR TEXT", "candidate_index_material_mismatch"),
        ("search_text", "DRIFTED SEARCH TEXT", "candidate_index_material_mismatch"),
        ("embedding_hash", "DRIFTED HASH", "candidate_index_material_mismatch"),
    ],
)
def test_verified_candidate_binding_drift_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: str,
    reason: str,
) -> None:
    _add_synthesis(conn)
    conn.execute(f"UPDATE memories SET {column} = ? WHERE id = 's1'", (value,))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations == ({"id": "s1", "reason": reason},)


@pytest.mark.parametrize("field", ["synthesis_key", "synthesis_revision"])
def test_verified_candidate_metadata_binding_drift_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    _add_synthesis(conn)
    metadata = json.loads(
        conn.execute("SELECT metadata_json FROM memories WHERE id = 's1'").fetchone()[0]
    )
    metadata[field] = "drifted" if field == "synthesis_key" else 99
    conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 's1'",
        (json.dumps(metadata),),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "candidate_binding_mismatch"


def test_verified_candidate_lifecycle_state_drift_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.execute("UPDATE memories SET tags = '[\"status:wrong\"]' WHERE id = 's1'")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "candidate_unavailable"


def test_verified_synthesis_source_status_is_recursively_validated(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn, synthesis_id="upstream", source_id="upstream-source")
    _add_synthesis(conn, synthesis_id="downstream", source_id="downstream-source")
    _replace_synthesis_source(
        conn,
        synthesis_id="downstream",
        old_source_id="downstream-source",
        new_source_id="upstream",
    )
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    assert _evaluate(conn, ["downstream"]).items == ("downstream",)

    conn.execute("UPDATE synthesis_artifacts SET status = 'stale' WHERE memory_id = 'upstream'")

    result = _evaluate(conn, ["downstream"])

    assert result.items == ()
    assert result.degradations == ({"id": "downstream", "reason": "source_synthesis_invalid"},)


def test_synthesis_source_revision_requires_an_exact_integer(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn, synthesis_id="upstream", source_id="upstream-source")
    _add_synthesis(conn, synthesis_id="downstream", source_id="downstream-source")
    _replace_synthesis_source(
        conn,
        synthesis_id="downstream",
        old_source_id="downstream-source",
        new_source_id="upstream",
    )
    row = conn.execute(
        "SELECT id, metadata_json FROM behavior_graph_edges "
        "WHERE source = 'downstream' AND target = 'upstream'"
    ).fetchone()
    metadata = json.loads(row[1])
    metadata["source_revision"] = True
    conn.execute(
        "UPDATE behavior_graph_edges SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata), row[0]),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["downstream"])

    assert result.items == ()
    assert result.degradations == ({"id": "downstream", "reason": "source_revision_mismatch"},)


def test_recursive_synthesis_validation_fails_closed_on_cycle(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn, synthesis_id="cycle-a", source_id="source-a")
    _add_synthesis(conn, synthesis_id="cycle-b", source_id="source-b")
    _replace_synthesis_source(
        conn,
        synthesis_id="cycle-a",
        old_source_id="source-a",
        new_source_id="cycle-b",
    )
    _replace_synthesis_source(
        conn,
        synthesis_id="cycle-b",
        old_source_id="source-b",
        new_source_id="cycle-a",
    )
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["cycle-a"])

    assert result.items == ()
    assert result.degradations == ({"id": "cycle-a", "reason": "synthesis_cycle"},)


def test_synthesis_requires_two_sources_even_when_count_and_fingerprint_agree(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "DELETE FROM behavior_graph_edges WHERE source = 's1' AND target LIKE '%:secondary'"
    )
    source = retrieval._load_memory(conn, "source-1")
    source_hash = canonical_memory_hash(source)
    conn.execute(
        "UPDATE synthesis_artifacts SET support_count = 1, source_fingerprint = ? "
        "WHERE memory_id = 's1'",
        (source_fingerprint({"source-1": source_hash}),),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "support_count_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("synthesis_revision", 2),
        ("synthesis_revision", True),
        ("support_scope", "project:other"),
    ],
)
def test_edge_revision_and_scope_must_match_control(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _add_synthesis(conn)
    row = conn.execute(
        "SELECT id, metadata_json FROM behavior_graph_edges WHERE source = 's1' ORDER BY id LIMIT 1"
    ).fetchone()
    metadata = json.loads(row[1])
    metadata[field] = value
    conn.execute(
        "UPDATE behavior_graph_edges SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata), row[0]),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "edge_metadata_invalid"


def test_control_binding_revision_requires_an_exact_integer(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    memory_metadata = json.loads(
        conn.execute("SELECT metadata_json FROM memories WHERE id = 's1'").fetchone()[0]
    )
    binding = dict(memory_metadata["synthesis_binding"])
    binding["synthesis_revision"] = True
    binding_hash = synthesis_binding_hash(binding)
    memory_metadata["synthesis_revision"] = True
    memory_metadata["synthesis_binding"] = binding
    memory_metadata["synthesis_binding_hash"] = binding_hash
    control_metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM synthesis_artifacts WHERE memory_id = 's1'"
        ).fetchone()[0]
    )
    control_metadata["synthesis_binding"] = binding
    control_metadata["synthesis_binding_hash"] = binding_hash
    conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 's1'",
        (json.dumps(memory_metadata),),
    )
    conn.execute(
        "UPDATE synthesis_artifacts SET metadata_json = ? WHERE memory_id = 's1'",
        (json.dumps(control_metadata),),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations == ({"id": "s1", "reason": "control_binding_invalid"},)


def test_synthesis_type_casing_cannot_bypass_disabled_gate(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_memory(conn, "mixed-case", memory_type="SYNTHESIS")
    monkeypatch.delenv("PP_SYNTHESIS_RETRIEVAL", raising=False)

    result = _evaluate(conn, ["mixed-case"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "retrieval_disabled"


@pytest.mark.parametrize("flag", [None, "", "0", "true", "01", " 1", "1 "])
def test_only_exact_retrieval_flag_enables_verified_synthesis(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
) -> None:
    _add_synthesis(conn)
    if flag is None:
        monkeypatch.delenv("PP_SYNTHESIS_RETRIEVAL", raising=False)
    else:
        monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", flag)

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.dropped_ids == ("s1",)
    assert result.degradations[0]["reason"] == "retrieval_disabled"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "control_missing"),
        ("draft", "status_not_allowed"),
        ("contested", "status_not_allowed"),
        ("stale", "status_not_allowed"),
        ("unknown", "status_not_allowed"),
    ],
)
def test_normal_mode_drops_untrusted_synthesis_statuses(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    reason: str,
) -> None:
    _add_synthesis(conn, status=status)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == reason


@pytest.mark.parametrize(
    ("status", "admitted"),
    [("draft", True), ("contested", True), ("stale", False), ("unknown", False)],
)
def test_review_mode_allows_only_draft_and_contested_beyond_verified(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    admitted: bool,
) -> None:
    _add_synthesis(conn, status=status)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"], allow_review=True)

    assert (result.items == ("s1",)) is admitted
    assert (result.dropped_ids == ()) is admitted


def test_valid_verified_synthesis_is_admitted(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result == retrieval.SynthesisGateResult(("s1",), (), (), ("s1",))


@pytest.mark.parametrize(
    "missing_column",
    ["last_verified_at", "verified_by_actor", "verified_by_call_id"],
)
def test_verified_synthesis_requires_complete_verification_evidence(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    missing_column: str,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE synthesis_artifacts "
        "SET last_verified_at = ?, verified_by_actor = ?, verified_by_call_id = ? "
        "WHERE memory_id = ?",
        ("2026-07-10T01:02:03Z", "reviewer", "call:verify:s1", "s1"),
    )
    conn.execute(
        f"UPDATE synthesis_artifacts SET {missing_column} = '' WHERE memory_id = ?",
        ("s1",),
    )
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.dropped_ids == ("s1",)
    assert result.degradations == ({"id": "s1", "reason": "verification_evidence_missing"},)
    assert retrieval.synthesis_provenance(conn, "s1") == {}


def test_batch_version_change_retracts_synthesis_admitted_earlier(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn, synthesis_id="s1", source_id="source-one")
    _add_synthesis(conn, synthesis_id="s2", source_id="source-two")
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    original_validate = retrieval._validate_synthesis

    def mutate_during_second_validation(database, memory_id, **kwargs):
        if memory_id == "s2":
            database.execute("UPDATE memory_version SET version = version + 1")
            database.commit()
        return original_validate(database, memory_id, **kwargs)

    monkeypatch.setattr(retrieval, "_validate_synthesis", mutate_during_second_validation)

    result = retrieval.evaluate_synthesis_ids(conn, ["s1", "s2"], memory_version=7)

    assert result.items == ()
    assert result.dropped_ids == ("s2", "s1")
    assert result.degradations == (
        {"id": "s2", "reason": "memory_version_mismatch"},
        {"id": "s1", "reason": "memory_version_mismatch"},
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("changed", "source_hash_mismatch"),
        ("missing", "source_missing"),
        ("forgotten", "source_unavailable"),
        ("deprecated", "source_unavailable"),
        ("wrong", "source_unavailable"),
    ],
)
def test_unavailable_or_changed_source_is_dropped(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    _add_synthesis(conn)
    if mutation == "changed":
        conn.execute("UPDATE memories SET content = 'changed' WHERE id = 'source-1'")
    elif mutation == "missing":
        conn.execute("DELETE FROM memories WHERE id = 'source-1'")
    else:
        conn.execute(
            "UPDATE memories SET tags = ? WHERE id = 'source-1'",
            (json.dumps([f"status:{mutation}"]),),
        )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == reason


@pytest.mark.parametrize(
    "metadata",
    [None, "", "{", "[]", "{}", '{"observed_content_hash": 3}'],
)
def test_invalid_or_missing_snapshot_metadata_is_dropped(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str | None,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE behavior_graph_edges SET metadata_json = ? WHERE relation = 'derived_from'",
        (metadata,),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "edge_metadata_invalid"


@pytest.mark.parametrize("metadata", [None, ""])
def test_missing_or_blank_control_metadata_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str | None,
) -> None:
    _add_synthesis(conn)
    if metadata is None:
        conn.execute(
            """
            CREATE TABLE synthesis_artifacts_nullable AS
            SELECT * FROM synthesis_artifacts
            """
        )
        conn.execute("DROP TABLE synthesis_artifacts")
        conn.execute("ALTER TABLE synthesis_artifacts_nullable RENAME TO synthesis_artifacts")
    conn.execute(
        "UPDATE synthesis_artifacts SET metadata_json = ? WHERE memory_id = 's1'",
        (metadata,),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "control_metadata_invalid"


@pytest.mark.parametrize(
    ("mutation_sql", "reason"),
    [
        ("UPDATE synthesis_artifacts SET support_count = 3", "support_count_mismatch"),
        (
            "UPDATE synthesis_artifacts SET source_fingerprint = 'sha256:not-current'",
            "source_fingerprint_mismatch",
        ),
        (
            "UPDATE memories SET visibility = 'global' WHERE id = 's1'",
            "candidate_visibility_mismatch",
        ),
    ],
)
def test_control_and_visibility_mismatches_are_dropped(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    mutation_sql: str,
    reason: str,
) -> None:
    _add_synthesis(conn)
    conn.execute(mutation_sql)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == reason


@pytest.mark.parametrize(
    ("source", "target"),
    [("s1", "other"), ("other", "s1"), ("source-1", "other"), ("other", "source-1")],
)
def test_open_contradiction_in_either_direction_is_dropped(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    target: str,
) -> None:
    _add_synthesis(conn)
    _add_edge(conn, source, target, "contradicts", {})
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "contradiction_open"


def test_open_supersession_targeting_current_source_is_dropped(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    _add_edge(conn, "replacement", "source-1", "supersedes", {})
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "source_superseded"


@pytest.mark.parametrize(
    ("relation", "source", "target", "metadata", "reason"),
    [
        ("contradicts", "other", "s1", {"status": "resolved"}, "contradiction_open"),
        ("contradicts", "other", "s1", {"open": False}, "contradiction_open"),
        ("supersedes", "replacement", "source-1", {"status": "closed"}, "source_superseded"),
        ("supersedes", "replacement", "source-1", {"open": False}, "source_superseded"),
    ],
)
def test_undefined_edge_metadata_cannot_close_a_persisted_conflict(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
    source: str,
    target: str,
    metadata: dict[str, object],
    reason: str,
) -> None:
    _add_synthesis(conn)
    _add_edge(conn, source, target, relation, metadata)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == reason


@pytest.mark.parametrize(
    "metadata",
    [
        {"status": "open", "resolved_at": "2026-07-10T00:00:00Z"},
        {"open": False, "status": "open"},
        {"open": True, "status": "resolved"},
    ],
)
def test_conflicting_edge_state_signals_fail_closed_as_open(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, object],
) -> None:
    _add_synthesis(conn)
    _add_edge(conn, "other", "s1", "contradicts", metadata)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "contradiction_open"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("tags", None),
        ("tags", ""),
        ("tags", "not-json"),
        ("metadata_json", None),
        ("metadata_json", ""),
        ("metadata_json", "not-json"),
    ],
)
def test_unreadable_source_state_json_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: str | None,
) -> None:
    _add_synthesis(conn)
    conn.execute(f"UPDATE memories SET {column} = ? WHERE id = 'source-1'", (value,))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "source_state_invalid"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("metadata_json", json.dumps({"status": "mystery"})),
        ("metadata_json", json.dumps({"quality": "mystery"})),
        ("tags", json.dumps(["status:mystery"])),
    ],
)
def test_unknown_source_lifecycle_string_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: str,
) -> None:
    _add_synthesis(conn)
    conn.execute(f"UPDATE memories SET {column} = ? WHERE id = 'source-1'", (value,))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "source_state_invalid"


@pytest.mark.parametrize(
    "metadata",
    [
        {"wrong": 1},
        {"stale": "yes"},
        {"wrong": None},
    ],
)
def test_malformed_blocked_boolean_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, object],
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 'source-1'",
        (json.dumps(metadata),),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "source_state_invalid"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("metadata_json", json.dumps({"status": "active"})),
        ("tags", json.dumps(["status:active"])),
    ],
)
def test_explicitly_healthy_source_lifecycle_is_allowed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: str,
) -> None:
    _add_synthesis(conn)
    conn.execute(f"UPDATE memories SET {column} = ? WHERE id = 'source-1'", (value,))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    assert _evaluate(conn, ["s1"]).items == ("s1",)


def test_unknown_source_state_shape_fails_closed(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = 'source-1'",
        (json.dumps({"status": []}),),
    )
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])

    assert result.items == ()
    assert result.degradations[0]["reason"] == "source_state_invalid"


def test_degradation_metadata_never_echoes_candidate_source_or_exception_text(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(
        conn,
        source_content="SOURCE-TEXT-MUST-NOT-LEAK",
    )
    conn.execute("UPDATE memories SET content = 'CHANGED-SOURCE-SECRET' WHERE id = 'source-1'")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = _evaluate(conn, ["s1"])
    encoded = json.dumps(result.degradations)

    assert result.degradations[0].keys() == {"id", "reason"}
    assert "SOURCE-TEXT-MUST-NOT-LEAK" not in encoded
    assert "CHANGED-SOURCE-SECRET" not in encoded
    assert "candidate synthesis content" not in encoded


def _database_snapshot(conn: sqlite3.Connection) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = ("memories", "synthesis_artifacts", "behavior_graph_edges", "memory_version")
    return {table: tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


def test_evaluation_does_not_mutate_any_table(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    before = _database_snapshot(conn)
    total_changes = conn.total_changes

    assert _evaluate(conn, ["s1"]).items == ("s1",)

    assert _database_snapshot(conn) == before
    assert conn.total_changes == total_changes


def test_invalid_memory_version_argument_drops_synthesis(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    result = retrieval.evaluate_synthesis_ids(conn, ["s1"], memory_version=-1)

    assert result.items == ()
    assert result.degradations[0]["reason"] == "memory_version_invalid"


def test_evaluator_drops_synthesis_when_memory_version_changes_during_validation(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    original_validate = retrieval._validate_synthesis

    def validate_and_advance_version(*args, **kwargs):
        original_validate(*args, **kwargs)
        conn.execute("UPDATE memory_version SET version = version + 1")
        conn.commit()

    monkeypatch.setattr(retrieval, "_validate_synthesis", validate_and_advance_version)

    result = retrieval.evaluate_synthesis_ids(conn, ["s1"], memory_version=7)

    assert result.items == ()
    assert result.degradations == ({"id": "s1", "reason": "memory_version_mismatch"},)


def test_index_eligibility_is_verified_only_and_reads_memory_version(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    assert retrieval.read_memory_version(conn) == 7
    assert retrieval.synthesis_index_eligible(conn, "s1") is True

    conn.execute("UPDATE synthesis_artifacts SET status = 'draft' WHERE memory_id = 's1'")
    conn.commit()
    assert retrieval.synthesis_index_eligible(conn, "s1") is False


def test_index_eligibility_never_observes_uncommitted_verification(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn, status="draft")
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    conn.execute("BEGIN")
    conn.execute("UPDATE synthesis_artifacts SET status = 'verified' WHERE memory_id = 's1'")
    conn.execute("UPDATE memory_version SET version = version + 1")

    assert retrieval.synthesis_index_eligible(conn, "s1") is False

    conn.rollback()
    assert retrieval.synthesis_index_eligible(conn, "s1") is False


def test_index_eligibility_rejects_ordinary_memory(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_memory(conn, "ordinary")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    assert retrieval.synthesis_index_eligible(conn, "ordinary") is False


@pytest.mark.parametrize("version", [None, "invalid", -1])
def test_index_eligibility_fails_closed_on_missing_or_invalid_memory_version(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    version: object,
) -> None:
    _add_synthesis(conn)
    conn.execute("DELETE FROM memory_version")
    if version is not None:
        conn.execute("INSERT INTO memory_version (version) VALUES (?)", (version,))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    assert retrieval.synthesis_index_eligible(conn, "s1") is False


def test_index_eligibility_fails_closed_on_lookup_schema_and_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    missing_schema = sqlite3.connect(":memory:")
    missing_schema.execute("CREATE TABLE memory_version (version INTEGER)")
    missing_schema.execute("INSERT INTO memory_version VALUES (1)")
    assert retrieval.synthesis_index_eligible(missing_schema, "s1") is False
    missing_schema.close()
    assert retrieval.synthesis_index_eligible(missing_schema, "s1") is False


def _governed_engine(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> ContextEngine:
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "governed.db"))
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    monkeypatch.setenv("PP_CODE_MEMORY_ENABLED", "0")
    monkeypatch.setenv("PP_QUERY_EXPANSION", "0")
    monkeypatch.setenv("PP_RERANK_DISABLED", "1")
    monkeypatch.setenv("PP_FTS_DISABLED", "1")
    monkeypatch.setenv("PP_HARD_MIN_SCORE", "0")
    monkeypatch.setenv("PP_DECAY_IN_RANKING", "0")
    engine = ContextEngine(use_sqlite=True)
    engine._ensure_heavy_init = lambda: None
    engine._activate_principles = lambda task_type, task_description: []
    engine._inject_activated_to_graph = lambda activated, task_type: 0
    engine._graph_traversal = lambda task_type: []
    engine._fts_retrieval = lambda query, scope="global", limit=None: []
    engine._apply_edge_feedback = lambda: None
    engine._maybe_adjust_tier = lambda memory_id: None
    engine._calc_freshness = lambda memory_id: "valid"
    engine._calc_decay_status = lambda memory_id, memory: "healthy"
    engine._compute_divergent_quality = lambda items, all_items: items
    return engine


def _register_governed_synthesis(
    engine: ContextEngine,
    synthesis_id: str,
    *,
    status: str = "verified",
    content: str | None = None,
) -> str:
    from plastic_promise.core.memory_index import (
        LEGACY_POLICY,
        build_index_material,
        index_metadata,
    )

    source_id = f"source:{synthesis_id}"
    source_ids = [source_id, f"{source_id}:secondary"]
    for index, parent_id in enumerate(source_ids):
        source_content = f"source evidence {index} for {synthesis_id}"
        source_material = build_index_material(
            {"content": source_content},
            policy=LEGACY_POLICY,
            model_name="synthesis-index-test",
        )
        engine.register_memory(
            {
                "id": parent_id,
                "content": source_content,
                "memory_type": "experience",
                "source": "user",
                "project_id": "project:test",
                "visibility": "project",
                "origin_kind": "memory",
                "origin_uri": "memory://test",
                "origin_ref": parent_id,
                "origin_hash": f"origin:{parent_id}",
                "embedding_text": source_material.vector_text,
                "search_text": source_material.search_text,
                "embedding_hash": source_material.embedding_hash,
                "metadata_json": {"memory_index": index_metadata(source_material)},
            }
        )
    synthesis_content = content or f"governed synthesis {synthesis_id}"
    synthesis_material = build_index_material(
        {"content": synthesis_content},
        policy=LEGACY_POLICY,
        model_name="synthesis-index-test",
    )
    synthesis_key = f"key:{synthesis_id}"
    synthesis_metadata = {
        "memory_index": index_metadata(synthesis_material),
        "synthesis_key": synthesis_key,
        "synthesis_revision": 1,
    }
    synthesis_record = {
        "id": synthesis_id,
        "content": synthesis_content,
        "memory_type": "synthesis",
        "source": "synthesis",
        "source_class": "synthesis",
        "project_id": "project:test",
        "visibility": "project",
        "origin_kind": "synthesis",
        "origin_uri": f"synthesis://{synthesis_id}",
        "origin_ref": synthesis_key,
        "origin_hash": synthesis_content_hash(synthesis_content),
        "embedding_text": synthesis_material.vector_text,
        "search_text": synthesis_material.search_text,
        "embedding_hash": synthesis_material.embedding_hash,
        "metadata_json": synthesis_metadata,
    }
    binding = canonical_synthesis_binding(synthesis_record, synthesis_material)
    binding_hash = synthesis_binding_hash(binding)
    synthesis_metadata["synthesis_binding"] = binding
    synthesis_metadata["synthesis_binding_hash"] = binding_hash
    engine._sqlite.upsert(synthesis_id, synthesis_record)
    engine._memories[synthesis_id] = engine._sqlite.get(synthesis_id)
    snapshots = {
        parent_id: canonical_memory_hash(engine._memories[parent_id]) for parent_id in source_ids
    }
    now = "2026-07-10T00:00:00Z"
    verification_time = now if status == "verified" else ""
    verification_actor = "reviewer" if status == "verified" else ""
    verification_call_id = f"call:verify:{synthesis_id}" if status == "verified" else ""
    conn = engine._sqlite._conn
    conn.execute(
        """
        INSERT INTO synthesis_artifacts (
            memory_id, synthesis_key, status, support_count, validity_scope,
            source_fingerprint, last_verified_at, verified_by_actor,
            verified_by_call_id, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            synthesis_id,
            synthesis_key,
            status,
            2,
            "project:test",
            source_fingerprint(snapshots),
            verification_time,
            verification_actor,
            verification_call_id,
            json.dumps(
                {
                    "project_id": "project:test",
                    "visibility": "project",
                    "synthesis_binding": binding,
                    "synthesis_binding_hash": binding_hash,
                }
            ),
            now,
            now,
        ),
    )
    for parent_id, source_hash in snapshots.items():
        conn.execute(
            """
            INSERT INTO behavior_graph_edges (
                id, source, target, relation, weight, source_kind, evidence_id,
                metadata_json, schema_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"edge:{synthesis_id}:{parent_id}",
                synthesis_id,
                parent_id,
                "derived_from",
                1.0,
                "synthesis",
                parent_id,
                json.dumps(
                    {
                        "observed_content_hash": source_hash,
                        "synthesis_revision": 1,
                        "support_scope": "project:test",
                    }
                ),
                "behavior-graph/v1",
                now,
            ),
        )
    conn.commit()
    assert engine._refresh_canonical_cache_if_changed(force=True) is True
    return source_id


@pytest.mark.parametrize(
    "surface",
    ["dict", "record", "batch", "list", "iter", "ids", "exists", "count", "stats"],
)
def test_public_memory_reads_recheck_canonical_version_after_admission(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = f"toctou:{surface}"
    source_id = _register_governed_synthesis(engine, synthesis_id)
    baseline_count = engine.memory_count
    database_path = tmp_path / "governed.db"
    concurrent = sqlite3.connect(database_path)
    original_public_ids = engine._public_memory_ids
    changed = False

    def invalidate_after_admission(ids, **kwargs):
        nonlocal changed
        admitted = original_public_ids(ids, **kwargs)
        if synthesis_id in admitted and not changed:
            concurrent.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                ("concurrent corrected source evidence", source_id),
            )
            concurrent.execute("UPDATE memory_version SET version = version + 1")
            concurrent.commit()
            changed = True
        return admitted

    monkeypatch.setattr(engine, "_public_memory_ids", invalidate_after_admission)
    try:
        if surface == "dict":
            result = engine.get_memory_dict(synthesis_id)
            exposed_ids = {str(result.get("id"))} if isinstance(result, dict) else set()
        elif surface == "record":
            result = engine.get_memory(synthesis_id)
            exposed_ids = {str(result.id)} if result is not None else set()
        elif surface == "batch":
            exposed_ids = {str(row["id"]) for row in engine.get_memories_batch([synthesis_id])}
        elif surface == "list":
            exposed_ids = {str(row.id) for row in engine.list_memories(memory_type="synthesis")}
        else:
            if surface == "iter":
                exposed_ids = {
                    str(row["id"])
                    for row in engine.iter_memories()
                    if row.get("memory_type") == "synthesis"
                }
            elif surface == "ids":
                exposed_ids = set(engine.memory_ids())
            elif surface == "exists":
                exposed_ids = {synthesis_id} if engine.memory_exists(synthesis_id) else set()
            elif surface == "count":
                exposed_ids = {
                    synthesis_id if engine.memory_count == baseline_count else "count-changed"
                }
            else:
                stats = json.loads(engine.memory_stats_json())
                exposed_ids = {synthesis_id} if stats["by_type"].get("synthesis", 0) else set()
    finally:
        concurrent.close()
        engine._sqlite._conn.close()

    assert changed is True
    assert synthesis_id not in exposed_ids


@pytest.mark.parametrize(
    "surface",
    ["dict", "record", "batch", "list", "iter", "ids", "exists", "count", "stats"],
)
def test_public_memory_reads_keep_ordinary_rows_inside_transaction_and_hide_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = f"transaction:{surface}"
    source_id = _register_governed_synthesis(engine, synthesis_id)
    source_before = engine.get_memory_dict(source_id)
    engine.begin_batch()
    try:
        assert engine.update_memory_fields(source_id, domain="transaction-local") is True
        if surface == "dict":
            rows = [engine.get_memory_dict(source_id), engine.get_memory_dict(synthesis_id)]
            exposed_ids = {str(row["id"]) for row in rows if isinstance(row, dict)}
            assert rows[0]["content"] == source_before["content"]
            assert rows[0]["domain"] == source_before["domain"]
        elif surface == "record":
            rows = [engine.get_memory(source_id), engine.get_memory(synthesis_id)]
            exposed_ids = {str(row.id) for row in rows if row is not None}
            assert rows[0].content == source_before["content"]
        elif surface == "batch":
            exposed_ids = {
                str(row["id"]) for row in engine.get_memories_batch([source_id, synthesis_id])
            }
        elif surface == "list":
            exposed_ids = {str(row.id) for row in engine.list_memories()}
        elif surface == "iter":
            exposed_ids = {str(row["id"]) for row in engine.iter_memories()}
        elif surface == "ids":
            exposed_ids = set(engine.memory_ids())
        elif surface == "exists":
            exposed_ids = {
                memory_id
                for memory_id in (source_id, synthesis_id)
                if engine.memory_exists(memory_id)
            }
        elif surface == "count":
            assert engine.memory_count == 2
            exposed_ids = {source_id}
        else:
            stats = json.loads(engine.memory_stats_json())
            assert stats["total"] == 2
            assert stats["by_type"].get("synthesis", 0) == 0
            exposed_ids = {source_id}

        assert source_id in exposed_ids
        assert synthesis_id not in exposed_ids
    finally:
        engine.rollback_batch()
        engine._sqlite._conn.close()


@pytest.mark.parametrize("surface", ["dict", "batch"])
def test_public_memory_reads_keep_synthesis_closed_when_initial_version_fence_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = f"flaky-version:{surface}"
    source_id = _register_governed_synthesis(engine, synthesis_id)
    concurrent = sqlite3.connect(tmp_path / "governed.db")
    original_read_version = retrieval.read_memory_version
    original_public_ids = engine._public_memory_ids
    version_calls = 0
    changed = False

    def fail_first_version_read(conn):
        nonlocal version_calls
        version_calls += 1
        if version_calls == 1:
            raise RuntimeError("initial version fence unavailable")
        return original_read_version(conn)

    def invalidate_after_admission(ids, **kwargs):
        nonlocal changed
        admitted = original_public_ids(ids, **kwargs)
        if synthesis_id in admitted and not changed:
            concurrent.execute(
                "UPDATE memories SET content = ? WHERE id = ?",
                ("source corrected after recovered admission", source_id),
            )
            concurrent.execute("UPDATE memory_version SET version = version + 1")
            concurrent.commit()
            changed = True
        return admitted

    monkeypatch.setattr(retrieval, "read_memory_version", fail_first_version_read)
    monkeypatch.setattr(engine, "_public_memory_ids", invalidate_after_admission)
    try:
        if surface == "dict":
            synthesis = engine.get_memory_dict(synthesis_id)
            ordinary = engine.get_memory_dict(source_id)
            exposed_ids = {str(synthesis["id"])} if synthesis is not None else set()
            assert ordinary is not None
        else:
            rows = engine.get_memories_batch([source_id, synthesis_id])
            exposed_ids = {str(row["id"]) for row in rows}
            assert source_id in exposed_ids
    finally:
        concurrent.close()
        engine._sqlite._conn.close()

    assert version_calls > 1
    assert changed is True
    assert synthesis_id not in exposed_ids


def _python_pack_for_results(
    engine: ContextEngine,
    results: list[tuple[str, float, str, str]],
    *,
    debug: bool = True,
    retrieval_plan: RetrievalPlan | None = None,
) -> ContextPack:
    engine._text_retrieval = lambda query, trust_boost=1.0, domain_hint=None: list(results)
    engine._vector_retrieval = lambda vector, scope=None, limit=None: []
    return engine._supply_python(
        "governed synthesis query",
        [0.0],
        task_type="general",
        scope="global",
        debug=debug,
        retrieval_plan=retrieval_plan,
        project_id="project:test",
    )


def _pack_ids(pack: ContextPack) -> list[str]:
    return [item.id for item in [*pack.core, *pack.related, *pack.divergent]]


def _finalize_task8_pack(
    engine: ContextEngine,
    items: list[ContextItem],
    *,
    task_type: str,
    mode: str,
    raw_evidence_budget: int,
) -> ContextPack:
    return engine._finalize_supply_pack(
        ContextPack(core=items),
        RetrievalPlan(
            mode=mode,
            budget={
                "core": len(items),
                "related": 0,
                "divergent": 0,
                "raw_evidence": raw_evidence_budget,
            },
            task_type=task_type,
        ),
        task_type=task_type,
        project_id="project:test",
        project_policy="balanced",
    )


def test_synthesis_provenance_is_compact_verified_canonical_state(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE synthesis_artifacts "
        "SET last_verified_at = ?, verified_by_actor = ?, verified_by_call_id = ? "
        "WHERE memory_id = ?",
        ("2026-07-10T01:02:03Z", "reviewer", "call:verify:s1", "s1"),
    )
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    changes_before = conn.total_changes

    provenance = retrieval.synthesis_provenance(conn, "s1")

    assert provenance == {
        "status": "verified",
        "revision": 1,
        "source_ids": ["source-1", "source-1:secondary"],
        "source_fingerprint": conn.execute(
            "SELECT source_fingerprint FROM synthesis_artifacts WHERE memory_id = 's1'"
        ).fetchone()[0],
        "last_verified_at": "2026-07-10T01:02:03Z",
        "verified_by_actor": "reviewer",
        "verified_by_call_id": "call:verify:s1",
    }
    assert conn.total_changes == changes_before


@pytest.mark.parametrize("status", ["draft", "stale", "contested"])
def test_synthesis_provenance_fails_closed_for_nonverified_state(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    _add_synthesis(conn, status=status)
    conn.commit()
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")

    assert retrieval.synthesis_provenance(conn, "s1") == {}


def test_expand_synthesis_sources_is_current_deduplicated_and_budgeted(
    conn: sqlite3.Connection,
) -> None:
    _add_synthesis(conn)
    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("current source evidence", "source-1"),
    )
    conn.commit()
    changes_before = conn.total_changes

    expanded = retrieval.expand_synthesis_sources(conn, ["s1", "s1"], limit=2)

    assert [item["id"] for item in expanded] == [
        "source-1",
        "source-1:secondary",
    ]
    assert expanded[0]["content"] == "current source evidence"
    assert all(item["source"] == "synthesis_source" for item in expanded)
    assert retrieval.expand_synthesis_sources(conn, ["s1"], limit=1) == expanded[:1]
    assert retrieval.expand_synthesis_sources(conn, ["s1"], limit=0) == []
    assert conn.total_changes == changes_before


def test_high_impact_finalizer_prioritizes_sources_and_deduplicates_budget(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "task8-high-impact")
    synthesis_id = "task8-high-impact"
    second_source_id = f"{source_id}:secondary"
    engine._sqlite._conn.execute(
        "UPDATE synthesis_artifacts SET last_verified_at = ?, verified_by_call_id = ? "
        "WHERE memory_id = ?",
        ("2026-07-10T01:02:03Z", "call:task8", synthesis_id),
    )
    engine._sqlite._conn.commit()
    engine.register_memory(
        {
            "id": "task8-ordinary",
            "content": "ordinary ranked evidence",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )
    items = [
        ContextItem(
            synthesis_id,
            str(engine._memories[synthesis_id]["content"]),
            0.99,
            source="synthesis",
            layer="core",
        ),
        ContextItem(
            "task8-ordinary",
            "ordinary ranked evidence",
            0.90,
            source="text",
            layer="core",
        ),
        ContextItem(
            source_id,
            str(engine._memories[source_id]["content"]),
            0.80,
            source="text",
            layer="core",
        ),
    ]

    pack = _finalize_task8_pack(
        engine,
        items,
        task_type="code_review",
        mode="code",
        raw_evidence_budget=3,
    )

    raw = pack.audit_metadata["raw_evidence"]
    assert [item["id"] for item in raw] == [
        source_id,
        second_source_id,
        "task8-ordinary",
    ]
    assert len({item["id"] for item in raw}) == len(raw) == 3
    assert pack.audit_metadata["synthesis_provenance"][synthesis_id] == {
        "status": "verified",
        "revision": 1,
        "source_ids": [source_id, second_source_id],
        "source_fingerprint": engine._sqlite._conn.execute(
            "SELECT source_fingerprint FROM synthesis_artifacts WHERE memory_id = ?",
            (synthesis_id,),
        ).fetchone()[0],
        "last_verified_at": "2026-07-10T01:02:03Z",
        "verified_by_actor": "reviewer",
        "verified_by_call_id": "call:task8",
    }


def test_high_impact_finalizer_does_not_expand_metadata_only_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "task8-metadata-only"
    source_id = _register_governed_synthesis(engine, synthesis_id)
    second_source_id = f"{source_id}:secondary"
    engine._sqlite._conn.execute(
        "UPDATE synthesis_artifacts "
        "SET last_verified_at = ?, verified_by_actor = ?, verified_by_call_id = ? "
        "WHERE memory_id = ?",
        ("2026-07-10T01:02:03Z", "reviewer", "call:task8", synthesis_id),
    )
    engine._sqlite._conn.commit()
    engine.register_memory(
        {
            "id": "task8-selected-ordinary",
            "content": "selected ordinary evidence",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )
    pack = ContextPack(
        core=[
            ContextItem(
                "task8-selected-ordinary",
                "selected ordinary evidence",
                0.99,
                source="text",
                layer="core",
            )
        ],
        audit_metadata={
            "provider_debug": {
                "candidate": {
                    "id": synthesis_id,
                    "content": str(engine._memories[synthesis_id]["content"]),
                }
            }
        },
    )
    plan = RetrievalPlan(
        mode="code",
        budget={"core": 1, "related": 0, "divergent": 0, "raw_evidence": 2},
        task_type="code_review",
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="code_review",
        project_id="project:test",
        project_policy="balanced",
    )

    raw_ids = [item["id"] for item in finalized.audit_metadata["raw_evidence"]]
    assert raw_ids == ["task8-selected-ordinary"]
    assert source_id not in raw_ids
    assert second_source_id not in raw_ids
    assert "synthesis_provenance" not in finalized.audit_metadata


def test_high_impact_finalizer_expands_synthesis_selected_after_project_refill(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "task8-project-refill"
    source_id = _register_governed_synthesis(engine, synthesis_id)
    second_source_id = f"{source_id}:secondary"
    engine.register_memory(
        {
            "id": "task8-cross-project-top",
            "content": "higher-ranked cross-project candidate",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:other",
            "visibility": "project",
        }
    )
    pack = ContextPack(
        core=[
            ContextItem(
                "task8-cross-project-top",
                "higher-ranked cross-project candidate",
                0.99,
                source="text",
                layer="core",
            ),
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.98,
                source="synthesis",
                layer="core",
            ),
        ]
    )
    plan = RetrievalPlan(
        mode="code",
        budget={"core": 1, "related": 0, "divergent": 0, "raw_evidence": 2},
        task_type="code_review",
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="code_review",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [synthesis_id]
    assert [item["id"] for item in finalized.audit_metadata["raw_evidence"]] == [
        source_id,
        second_source_id,
    ]
    assert set(finalized.audit_metadata["synthesis_provenance"]) == {synthesis_id}


def test_high_impact_finalizer_never_expands_draft_sources_in_normal_review(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "task8-draft-fallback"
    source_id = _register_governed_synthesis(engine, synthesis_id, status="draft")
    engine.register_memory(
        {
            "id": "task8-draft-ordinary",
            "content": "ordinary evidence must retain the raw budget",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
            "visibility": "project",
        }
    )

    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            ),
            ContextItem(
                "task8-draft-ordinary",
                "ordinary evidence must retain the raw budget",
                0.90,
                source="text",
                layer="core",
            ),
        ],
        task_type="code_review",
        mode="code",
        raw_evidence_budget=2,
    )

    raw_ids = [item["id"] for item in pack.audit_metadata["raw_evidence"]]
    assert raw_ids == ["task8-draft-ordinary"]
    assert source_id not in raw_ids
    assert f"{source_id}:secondary" not in raw_ids


@pytest.mark.parametrize(
    ("status", "task_type", "mode", "should_expand"),
    [
        ("contested", "code_generation", "code", False),
        ("contested", "code_review", "code", True),
        ("contested", "general", "audit", True),
        ("stale", "code_review", "code", False),
        ("stale", "debugging", "code", True),
        ("stale", "general", "audit", True),
    ],
)
def test_high_impact_fallback_respects_status_task_matrix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    task_type: str,
    mode: str,
    should_expand: bool,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = f"task8-{status}-{task_type}-{mode}"
    source_id = _register_governed_synthesis(engine, synthesis_id, status=status)

    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        task_type=task_type,
        mode=mode,
        raw_evidence_budget=2,
    )

    raw_ids = [item["id"] for item in pack.audit_metadata["raw_evidence"]]
    expected = [source_id, f"{source_id}:secondary"] if should_expand else []
    assert raw_ids == expected


def test_retrieval_rollback_does_not_expand_verified_synthesis_sources(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "task8-retrieval-disabled"
    _register_governed_synthesis(engine, synthesis_id)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "0")

    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        task_type="debugging",
        mode="code",
        raw_evidence_budget=2,
    )

    assert pack.audit_metadata["raw_evidence"] == []
    assert pack.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": synthesis_id, "reason": "retrieval_disabled"}
    ]


def test_synthesis_fallback_status_lookup_failure_fails_closed() -> None:
    class BrokenStatusConnection:
        in_transaction = False

        @staticmethod
        def execute(*_args, **_kwargs):
            raise sqlite3.OperationalError("status lookup unavailable")

    assert (
        ContextEngine._synthesis_source_fallback_allowed(
            BrokenStatusConnection(),
            "task8-status-lookup-failure",
            reasons={"status_not_allowed"},
            task_type="debugging",
            retrieval_mode="code",
        )
        is False
    )


def test_low_impact_finalizer_attaches_provenance_without_forced_source_expansion(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "task8-learning")
    synthesis_id = "task8-learning"
    items = [
        ContextItem(
            synthesis_id,
            str(engine._memories[synthesis_id]["content"]),
            0.99,
            source="synthesis",
            layer="core",
        )
    ]

    pack = _finalize_task8_pack(
        engine,
        items,
        task_type="learning",
        mode="global",
        raw_evidence_budget=3,
    )

    assert _pack_ids(pack) == [synthesis_id]
    assert pack.audit_metadata["synthesis_provenance"][synthesis_id]["revision"] == 1
    assert source_id not in {item["id"] for item in pack.audit_metadata["raw_evidence"]}
    assert f"{source_id}:secondary" not in {
        item["id"] for item in pack.audit_metadata["raw_evidence"]
    }


def test_high_impact_stale_synthesis_drops_content_and_prefers_current_sources(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "task8-stale")
    synthesis_id = "task8-stale"
    engine._sqlite._conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("current evidence after source drift", source_id),
    )
    engine._sqlite._conn.commit()
    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        task_type="debugging",
        mode="code",
        raw_evidence_budget=2,
    )

    assert synthesis_id not in _pack_ids(pack)
    assert "synthesis_provenance" not in pack.audit_metadata
    assert [item["id"] for item in pack.audit_metadata["raw_evidence"]] == [
        source_id,
        f"{source_id}:secondary",
    ]
    assert pack.audit_metadata["raw_evidence"][0]["content"] == (
        "current evidence after source drift"
    )
    assert {
        item["reason"] for item in pack.audit_metadata["synthesis_retrieval"]["degradations"]
    } == {"source_hash_mismatch"}


def test_high_impact_canonical_hydration_failure_drops_synthesis_and_keeps_sources(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "task8-hydration")
    synthesis_id = "task8-hydration"
    sqlite_get = engine._sqlite.get

    def fail_synthesis_hydration(memory_id: str):
        if memory_id == synthesis_id:
            raise RuntimeError("simulated canonical hydration failure")
        return sqlite_get(memory_id)

    monkeypatch.setattr(engine._sqlite, "get", fail_synthesis_hydration)
    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        task_type="correction",
        mode="global",
        raw_evidence_budget=2,
    )

    assert synthesis_id not in _pack_ids(pack)
    assert "synthesis_provenance" not in pack.audit_metadata
    assert [item["id"] for item in pack.audit_metadata["raw_evidence"]] == [
        source_id,
        f"{source_id}:secondary",
    ]
    assert pack.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": synthesis_id, "reason": "candidate_payload_mismatch"}
    ]


def test_high_impact_source_hydration_failure_drops_verified_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, "task8-source-hydration")
    synthesis_id = "task8-source-hydration"
    monkeypatch.setattr(retrieval, "expand_synthesis_sources", lambda *args, **kwargs: [])

    pack = _finalize_task8_pack(
        engine,
        [
            ContextItem(
                synthesis_id,
                str(engine._memories[synthesis_id]["content"]),
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        task_type="audit",
        mode="audit",
        raw_evidence_budget=2,
    )

    assert synthesis_id not in _pack_ids(pack)
    assert "synthesis_provenance" not in pack.audit_metadata
    assert pack.audit_metadata["raw_evidence"] == []
    assert pack.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": synthesis_id, "reason": "synthesis_source_hydration_unavailable"}
    ]


def test_finalizer_exposes_provenance_for_admitted_verified_synthesis_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, "task8-verified")
    _register_governed_synthesis(engine, "task8-draft", status="draft")
    items = [
        ContextItem(
            memory_id,
            str(engine._memories[memory_id]["content"]),
            score,
            source="synthesis",
            layer="core",
        )
        for memory_id, score in (("task8-verified", 0.99), ("task8-draft", 0.98))
    ]

    pack = _finalize_task8_pack(
        engine,
        items,
        task_type="learning",
        mode="global",
        raw_evidence_budget=2,
    )

    assert _pack_ids(pack) == ["task8-verified"]
    assert set(pack.audit_metadata["synthesis_provenance"]) == {"task8-verified"}
    assert str(engine._memories["task8-draft"]["content"]) not in json.dumps(pack.audit_metadata)


@pytest.mark.parametrize("status", ["draft", "stale", "unknown"])
def test_python_gate_drops_untrusted_synthesis_before_public_enrichment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    secret = f"{status.upper()}-SYNTHESIS-MUST-NOT-SURVIVE"
    _register_governed_synthesis(engine, "untrusted-s1", status=status, content=secret)
    engine.register_memory(
        {
            "id": "ordinary-1",
            "content": "ordinary governed synthesis query evidence",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )

    pack = _python_pack_for_results(
        engine,
        [
            ("untrusted-s1", 0.99, secret, "text"),
            ("ordinary-1", 0.90, "ordinary governed synthesis query evidence", "text"),
        ],
    )
    public_surfaces = json.dumps(
        {
            "ids": _pack_ids(pack),
            "raw": pack.audit_metadata["raw_evidence"],
            "stats": pack.per_item_stats,
            "gap": getattr(pack.gap_signal, "__dict__", pack.gap_signal),
        },
        default=str,
    )

    assert _pack_ids(pack) == ["ordinary-1"]
    assert "untrusted-s1" not in public_surfaces
    assert secret not in public_surfaces
    assert pack.audit_metadata["raw_evidence"][0]["id"] == "ordinary-1"
    degradations = pack.audit_metadata["synthesis_retrieval"]["degradations"]
    assert degradations[0].keys() == {"id", "reason"}
    assert degradations[0]["id"] == "untrusted-s1"


def test_verified_synthesis_is_recalled_but_source_drift_drops_immediately(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "verified-s1")
    results = [("verified-s1", 0.99, "governed synthesis verified-s1", "text")]

    assert _pack_ids(_python_pack_for_results(engine, results)) == ["verified-s1"]

    engine._sqlite._conn.execute(
        "UPDATE memories SET content = 'changed without scanner' WHERE id = ?",
        (source_id,),
    )
    engine._sqlite._conn.commit()

    changed = _python_pack_for_results(engine, results)
    assert "verified-s1" not in _pack_ids(changed)
    assert "governed synthesis verified-s1" not in json.dumps(changed.audit_metadata)


def test_finalizer_sanitizes_real_debug_recommendation_and_gap_surfaces(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    secret = "DROPPED-CONTENT-IN-METADATA"
    _register_governed_synthesis(engine, "draft-meta", status="draft", content=secret)
    engine.register_memory(
        {
            "id": "ordinary-meta",
            "content": "ordinary content",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )
    dropped = ContextItem("draft-meta", secret, 0.99, source="synthesis", layer="core")
    admitted = ContextItem("ordinary-meta", "ordinary content", 0.80, source="user", layer="core")
    pack = ContextPack(core=[dropped, admitted])
    pack.audit_metadata = {
        "raw_evidence": [
            {"id": "draft-meta", "content": secret},
            {"id": "ordinary-meta", "content": "ordinary content"},
        ],
        "context_recommender": {
            "recommendations": [
                {"id": "draft-meta", "content": secret},
                {"id": "ordinary-meta"},
            ]
        },
    }
    pack.per_item_stats = [
        {"id": "draft-meta", "content": secret},
        {"id": "ordinary-meta", "final_score": 0.8},
    ]
    pack.gap_signal = {
        "evidence": [{"id": "draft-meta", "content": secret}],
        "recommendations": ["draft-meta", secret, "safe recommendation"],
    }
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 1, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )
    public_surfaces = json.dumps(
        {
            "ids": _pack_ids(finalized),
            "raw": finalized.audit_metadata["raw_evidence"],
            "recommendations": finalized.audit_metadata.get("context_recommender"),
            "stats": finalized.per_item_stats,
            "gap": finalized.gap_signal,
        },
        default=str,
    )

    assert _pack_ids(finalized) == ["ordinary-meta"]
    assert "draft-meta" not in public_surfaces
    assert secret not in public_surfaces
    assert finalized.per_item_stats == [{"id": "ordinary-meta", "final_score": 0.8}]
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"][0].keys() == {
        "id",
        "reason",
    }


def test_bounded_overfetch_refills_after_synthesis_filtering_and_clamps_factor(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    for index in range(3):
        _register_governed_synthesis(engine, f"draft-overfetch-{index}", status="draft")
    engine.register_memory(
        {
            "id": "ordinary-refill",
            "content": "ordinary refill candidate",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )

    class NeverReadDerivedText:
        def __getitem__(self, key):
            raise AssertionError(f"derived draft text was read before admission: {key!r}")

    rows = [
        (
            f"draft-overfetch-{index}",
            0.99 - index * 0.01,
            NeverReadDerivedText(),
            "L1",
            "global",
        )
        for index in range(3)
    ]
    rows.append(("ordinary-refill", 0.90, "ordinary refill candidate", "L1", "global"))
    seen: dict[str, int] = {}

    class FakeLance:
        def search(self, *, vector, k, scope):
            seen["k"] = k
            return rows[:k]

        def count_rows(self):
            return len(rows)

    engine._ldb = FakeLance()
    engine._text_retrieval = lambda query, trust_boost=1.0, domain_hint=None: []
    monkeypatch.setenv("PP_SYNTHESIS_OVERFETCH_FACTOR", "99")
    monkeypatch.setenv("PP_VECTOR_WEIGHT", "1")
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 1, "related": 1, "divergent": 0, "raw_evidence": 2},
    )

    pack = engine._supply_python(
        "ordinary refill candidate",
        [1.0],
        retrieval_plan=plan,
        project_id="project:test",
    )

    assert seen["k"] == 8
    assert _pack_ids(pack) == ["ordinary-refill"]
    assert all(
        len(getattr(pack, layer)) <= plan.budget[layer]
        for layer in ("core", "related", "divergent")
    )


def test_rust_snapshot_filters_invalid_synthesis_and_preserves_memory_sentinel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, "draft-rust", status="draft")
    _register_governed_synthesis(engine, "type-drift-rust", status="verified")
    engine._sqlite._conn.execute(
        "UPDATE memories SET memory_type = 'experience' WHERE id = 'type-drift-rust'"
    )
    engine._sqlite._conn.commit()
    engine.register_memory(
        {
            "id": "ordinary-rust",
            "content": "ordinary rust snapshot",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )

    class NeverReadDraft:
        def __len__(self):
            return 1

        def __getitem__(self, key):
            raise AssertionError(f"draft field {key!r} was read before canonical admission")

        def keys(self):
            raise AssertionError("draft body was copied before canonical admission")

        def __iter__(self):
            raise AssertionError("draft body was copied before canonical admission")

    engine._memories["draft-rust"] = NeverReadDraft()
    monkeypatch.setattr(engine, "_refresh_canonical_cache_if_changed", lambda force=False: False)
    received: dict[str, object] = {}
    vector_reads: list[str] = []
    full_table_scans: list[bool] = []

    class FullTableSearch:
        def limit(self, _limit):
            return self

        def to_list(self):
            return []

    class FakeLance:
        class _Table:
            def search(self):
                full_table_scans.append(True)
                return FullTableSearch()

        _table = _Table()

        def get_vector(self, memory_id):
            vector_reads.append(memory_id)
            if memory_id in {"draft-rust", "type-drift-rust"}:
                raise AssertionError("blocked synthesis vector was read")
            return [0.0] * 1024

    engine._ldb = FakeLance()

    class FakeRustEngine:
        @classmethod
        def new_with_backends(cls, db_path, lancedb_path):
            received["db_path"] = db_path
            return cls()

        def set_current_time(self, current_time):
            return None

        def supply_with_project_context(self, *args):
            memories = args[4]
            received["ids"] = [memory["id"] for memory in memories]
            return SimpleNamespace(
                core=[],
                related=[],
                divergent=[],
                activated_principles=[],
                audit_metadata={},
                pipeline_stats={},
                per_item_stats=[],
            )

    monkeypatch.setitem(
        sys.modules, "context_engine_core", SimpleNamespace(ContextEngine=FakeRustEngine)
    )

    engine._supply_rust("query", [0.0], "general", "global")

    assert received["db_path"] == str(tmp_path / "governed.db")
    assert "draft-rust" not in received["ids"]
    assert "type-drift-rust" not in received["ids"]
    assert "ordinary-rust" in received["ids"]
    assert full_table_scans == []
    assert set(vector_reads) == set(received["ids"])


def test_rust_path_falls_back_to_python_for_admitted_governed_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust's ordinary-only snapshot path must not silently drop verified synthesis."""
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "verified-rust-fallback"
    _register_governed_synthesis(engine, synthesis_id, status="verified")

    class MetadataOnlySynthesis:
        def get(self, key, default=None):
            if key == "memory_type":
                return "synthesis"
            raise AssertionError(f"verified synthesis body was read before fallback: {key!r}")

        def keys(self):
            raise AssertionError("verified synthesis was copied for Rust ranking")

        def __iter__(self):
            raise AssertionError("verified synthesis was copied for Rust ranking")

    # Keep the canonical database intact while making any body copy observable.
    engine._memories[synthesis_id] = MetadataOnlySynthesis()
    monkeypatch.setattr(engine, "_refresh_canonical_cache_if_changed", lambda force=False: False)
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "0")
    engine._check_rust_health = lambda: True
    engine._rust_healthy = True
    calls = {"rust": 0, "python": 0}

    class UnexpectedRustEngine:
        @staticmethod
        def new_with_backends(*_args):
            calls["rust"] += 1
            return UnexpectedRustEngine()

        def set_current_time(self, _timestamp):
            return None

        def supply_with_project_context(self, *_args):
            raise AssertionError("Rust ranking must not receive admitted synthesis")

    monkeypatch.setitem(
        sys.modules,
        "context_engine_core",
        SimpleNamespace(ContextEngine=UnexpectedRustEngine),
    )
    expected = ContextPack(audit_metadata={"engine_mode": "python_synthesis_fallback"})

    def python_fallback(*_args, **_kwargs):
        calls["python"] += 1
        return expected

    engine._supply_python = python_fallback

    assert engine.supply("verified synthesis query", [0.0], "general", "global") is expected
    assert calls == {"rust": 0, "python": 1}
    assert engine._rust_healthy is True


def test_common_finalizer_drops_type_drift_with_control_without_content_leak(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    secret = "TYPE-DRIFT-SYNTHESIS-SECRET"
    _register_governed_synthesis(
        engine,
        "type-drift-finalizer",
        status="verified",
        content=secret,
    )
    engine._sqlite._conn.execute(
        "UPDATE memories SET memory_type = 'experience' WHERE id = 'type-drift-finalizer'"
    )
    engine._sqlite._conn.commit()
    pack = ContextPack(
        core=[
            ContextItem(
                "type-drift-finalizer",
                secret,
                0.99,
                source="synthesis",
                layer="core",
            )
        ],
        audit_metadata={
            "raw_evidence": [{"id": "type-drift-finalizer", "content": secret, "score": 0.99}]
        },
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    assert secret not in json.dumps(finalized.audit_metadata)
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": "type-drift-finalizer", "reason": "candidate_type_mismatch"}
    ]


def test_gate_requires_loaded_canonical_version_for_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, "loaded-version", status="verified")
    engine._sqlite._conn.execute("UPDATE memory_version SET version = version + 1")
    engine._sqlite._conn.commit()

    decision = engine._gate_memory_ids(["loaded-version"])

    assert decision.items == ()
    assert decision.degradations == ({"id": "loaded-version", "reason": "memory_version_mismatch"},)


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        ("discard", "source_unavailable"),
        ("mystery", "source_state_invalid"),
        (17, "source_state_invalid"),
        ("", "source_state_invalid"),
    ],
)
def test_public_getter_and_finalizer_reject_invalid_quality_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    decision,
    reason,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    memory_id = "ordinary-invalid-quality-decision"
    content = "Ordinary evidence with an invalid persisted quality decision."
    engine.register_memory(
        {
            "id": memory_id,
            "content": content,
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
            "metadata_json": {
                "quality": {"status": "current", "decision": decision}
            },
        }
    )

    assert engine.get_memory_dict(memory_id) is None
    assert engine.get_memory(memory_id) is None
    pack = ContextPack(
        core=[ContextItem(memory_id, content, 0.99, source="text", layer="core")]
    )
    finalized = engine._finalize_supply_pack(
        pack,
        RetrievalPlan(
            mode="global",
            budget={"core": 1, "related": 0, "divergent": 0, "raw_evidence": 1},
        ),
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": memory_id, "reason": reason}
    ]


def test_finalizer_drops_forged_payload_for_verified_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "forged-payload"
    canonical = "Canonical verified synthesis content."
    forged = "FORGED OR STALE SYNTHESIS PAYLOAD"
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=canonical,
    )
    pack = ContextPack(
        core=[ContextItem(synthesis_id, forged, 0.99, source="synthesis", layer="core")],
        audit_metadata={
            "raw_evidence": [{"id": synthesis_id, "content": forged, "score": 0.99}],
            "context_recommender": {"recommendations": [{"id": synthesis_id, "content": forged}]},
        },
        per_item_stats=[{"id": synthesis_id, "content": forged, "final_score": 0.99}],
        gap_signal={"evidence": [{"id": synthesis_id, "content": forged}]},
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    encoded = json.dumps(
        {
            "audit": finalized.audit_metadata,
            "stats": finalized.per_item_stats,
            "gap": finalized.gap_signal,
        }
    )
    assert forged not in encoded
    assert {"id": synthesis_id, "reason": "candidate_payload_mismatch"} in finalized.audit_metadata[
        "synthesis_retrieval"
    ]["degradations"]


def test_finalizer_accepts_canonical_fixed_300_character_representation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "long-canonical-payload"
    canonical = "LONG CANONICAL SYNTHESIS CONTENT " * 20
    assert len(canonical) > 300
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=canonical,
    )
    truncated = canonical[:300]
    pack = ContextPack(
        core=[ContextItem(synthesis_id, truncated, 0.99, source="synthesis", layer="core")],
        audit_metadata={
            "raw_evidence": [{"id": synthesis_id, "content": truncated, "score": 0.99}]
        },
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [synthesis_id]
    assert finalized.core[0].content == truncated
    assert finalized.audit_metadata["raw_evidence"][0]["content"] == truncated


def test_finalizer_accepts_canonical_fixed_500_character_representation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "long-cache-payload"
    canonical = "LONG CACHE SYNTHESIS CONTENT " * 30
    assert len(canonical) > 500
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=canonical,
    )
    cached = canonical[:500]
    pack = ContextPack(
        core=[ContextItem(synthesis_id, cached, 0.99, source="synthesis")],
        audit_metadata={
            "raw_evidence": [{"id": synthesis_id, "content": canonical[:300], "score": 0.99}]
        },
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [synthesis_id]
    assert finalized.core[0].content == cached


def test_finalizer_accepts_canonical_120_character_debug_representation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "long-debug-payload"
    canonical = "LONG DEBUG SYNTHESIS CONTENT " * 24
    assert len(canonical) > 300
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=canonical,
    )
    pack = ContextPack(
        core=[ContextItem(synthesis_id, canonical[:300], 0.99, source="synthesis")],
        audit_metadata={
            "raw_evidence": [{"id": synthesis_id, "content": canonical[:300], "score": 0.99}]
        },
        per_item_stats=[{"id": synthesis_id, "content": canonical[:120], "final_score": 0.99}],
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [synthesis_id]
    assert finalized.per_item_stats[0]["content"] == canonical[:120]


def test_finalizer_sanitizes_pipeline_and_arbitrary_audit_debug_surfaces(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "draft-debug-surface"
    secret = "DRAFT-SYNTHESIS-DEBUG-SECRET"
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="draft",
        content=secret,
    )
    pack = ContextPack(
        audit_metadata={"provider_debug": {"candidate": {"id": synthesis_id, "content": secret}}},
        pipeline_stats={"candidate_debug": [{"id": synthesis_id, "content": secret}]},
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    encoded = json.dumps(
        {
            "audit": finalized.audit_metadata,
            "pipeline": finalized.pipeline_stats,
        }
    )
    assert secret not in encoded
    assert synthesis_id not in json.dumps(finalized.audit_metadata.get("provider_debug", {}))
    assert synthesis_id not in json.dumps(finalized.pipeline_stats)
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": synthesis_id, "reason": "status_not_allowed"}
    ]


def test_finalizer_removes_dropped_candidate_with_noncontent_preview_alias(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "draft-preview-alias"
    secret = "DRAFT-PREVIEW-ALIAS-SECRET"
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="draft",
        content="canonical draft content",
    )
    pack = ContextPack(
        audit_metadata={"provider_debug": {"candidate": {"id": synthesis_id, "preview": secret}}}
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    encoded = json.dumps(finalized.audit_metadata)
    assert secret not in encoded
    assert synthesis_id not in json.dumps(finalized.audit_metadata.get("provider_debug", {}))


def test_finalizer_rebuilds_untrusted_existing_degradation_reasons(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "draft-malicious-degradation"
    secret = "DRAFT-SECRET-IN-UNTRUSTED-REASON"
    _register_governed_synthesis(engine, synthesis_id, status="draft")
    pack = ContextPack(
        audit_metadata={
            "synthesis_retrieval": {"degradations": [{"id": synthesis_id, "reason": secret}]}
        }
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert secret not in json.dumps(finalized.audit_metadata)
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"] == [
        {"id": synthesis_id, "reason": "status_not_allowed"}
    ]


def test_finalizer_uses_sqlite_canonical_payload_when_runtime_entry_is_missing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "runtime-missing-verified"
    canonical = "CANONICAL SQLITE VERIFIED PAYLOAD"
    stale = "STALE INDEX PAYLOAD FROM OLD REVISION"
    _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=canonical,
    )
    del engine._memories[synthesis_id]
    pack = ContextPack(core=[ContextItem(synthesis_id, stale, 0.99, layer="core")])
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    assert stale not in json.dumps(finalized.audit_metadata)
    assert {"id": synthesis_id, "reason": "candidate_payload_mismatch"} in finalized.audit_metadata[
        "synthesis_retrieval"
    ]["degradations"]


def test_finalizer_drops_payload_from_previous_synthesis_revision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    synthesis_id = "stale-revision-payload"
    old_content = "REVISION ONE SYNTHESIS PAYLOAD"
    new_content = "REVISION TWO CANONICAL PAYLOAD"
    source_id = _register_governed_synthesis(
        engine,
        synthesis_id,
        status="verified",
        content=old_content,
    )
    store = SynthesisStore(engine._sqlite._conn, engine=engine)
    store.mark_stale(synthesis_id, "refresh required", 1)
    store.refresh(
        synthesis_id,
        new_content,
        [source_id, f"{source_id}:secondary"],
        1,
        automatic=False,
    )
    store.verify(synthesis_id, "reviewer", "call-reverify", 2)
    pack = ContextPack(
        core=[ContextItem(synthesis_id, old_content, 0.99, source="synthesis", layer="core")]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    assert {"id": synthesis_id, "reason": "candidate_payload_mismatch"} in finalized.audit_metadata[
        "synthesis_retrieval"
    ]["degradations"]


def test_python_and_rust_public_paths_apply_same_final_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    secret = "PUBLIC-RUST-DRAFT-CONTENT"
    _register_governed_synthesis(engine, "draft-public", status="draft", content=secret)
    engine.register_memory(
        {
            "id": "ordinary-public",
            "content": "ordinary public content",
            "memory_type": "experience",
            "source": "user",
            "project_id": "project:test",
        }
    )
    python_pack = _python_pack_for_results(
        engine,
        [
            ("draft-public", 0.99, secret, "text"),
            ("ordinary-public", 0.90, "ordinary public content", "text"),
        ],
    )
    monkeypatch.setenv("PP_PREFER_RUST_SUPPLY", "1")
    monkeypatch.delenv("PP_FORCE_PYTHON_SUPPLY", raising=False)
    engine._check_rust_health = lambda: True
    engine._supply_rust = lambda *args, **kwargs: ContextPack(
        core=[
            ContextItem("draft-public", secret, 0.99, source="synthesis", layer="core"),
            ContextItem(
                "ordinary-public", "ordinary public content", 0.90, source="user", layer="core"
            ),
        ]
    )

    rust_pack = engine.supply(
        "query",
        [0.0],
        project_id="project:test",
    )

    assert _pack_ids(python_pack) == ["ordinary-public"]
    assert _pack_ids(rust_pack) == ["ordinary-public"]
    assert secret not in json.dumps(rust_pack.audit_metadata)


def test_final_gate_preserves_ordinary_code_and_principle_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "ordinary-compatible": {
            "id": "ordinary-compatible",
            "memory_type": "experience",
            "project_id": "project:test",
            "visibility": "project",
        }
    }
    pack = ContextPack(
        core=[ContextItem("ordinary-compatible", "ordinary", 0.9, layer="core")],
        related=[ContextItem("code:file:compatible.py", "code", 0.8, layer="related")],
        divergent=[
            ContextItem(
                "principle:compatible",
                "principle",
                0.5,
                layer="divergent",
                is_principle=True,
            )
        ],
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 2, "raw_evidence": 6},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [
        "ordinary-compatible",
        "code:file:compatible.py",
        "principle:compatible",
    ]


@pytest.mark.parametrize("memory_id", ["code:prefix-collision", "principle:prefix-collision"])
@pytest.mark.parametrize(
    ("status", "retrieval_enabled"),
    [("verified", False), ("draft", True)],
)
def test_canonical_synthesis_cannot_bypass_gate_with_reserved_prefix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
    status: str,
    retrieval_enabled: bool,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, memory_id, status=status)
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1" if retrieval_enabled else "0")
    pack = ContextPack(core=[ContextItem(memory_id, "PREFIX-COLLISION-SECRET", 0.99, layer="core")])
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == []
    assert "PREFIX-COLLISION-SECRET" not in json.dumps(finalized.audit_metadata)
    assert finalized.audit_metadata["synthesis_retrieval"]["degradations"][0]["id"] == memory_id


def test_missing_noncanonical_items_survive_invalid_memory_version(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine._sqlite._conn.execute("DROP TABLE memory_version")
    engine._sqlite._conn.execute("CREATE TABLE memory_version (version)")
    engine._sqlite._conn.execute("INSERT INTO memory_version VALUES (-1)")
    engine._sqlite._conn.commit()
    pack = ContextPack(
        related=[
            ContextItem("code:file:missing.py", "code context", 0.8, layer="related"),
            ContextItem(
                "principle:missing",
                "principle context",
                0.7,
                layer="related",
                is_principle=True,
            ),
        ]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 4, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:test",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == ["code:file:missing.py", "principle:missing"]


def test_finalizer_applies_project_policy_before_rebuilding_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "project-a": {
            "id": "project-a",
            "memory_type": "experience",
            "project_id": "project:a",
            "visibility": "project",
        },
        "project-b-private": {
            "id": "project-b-private",
            "memory_type": "experience",
            "project_id": "project:b",
            "visibility": "project",
        },
        "project-b-shared": {
            "id": "project-b-shared",
            "memory_type": "experience",
            "project_id": "project:b",
            "visibility": "shared",
        },
    }
    secret = "FINALIZER-CROSS-PROJECT-SECRET"
    pack = ContextPack(
        core=[
            ContextItem("project-a", "project a", 0.9, layer="core"),
            ContextItem("project-b-private", secret, 0.89, layer="core"),
        ],
        divergent=[
            ContextItem("project-b-shared", "shared inspiration", 0.5, layer="divergent"),
            ContextItem("code:file:project.py", "code context", 0.4, layer="divergent"),
        ],
    )
    pack.audit_metadata = {
        "raw_evidence": [
            {"id": "project-b-private", "content": secret},
        ]
    }
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 4, "related": 4, "divergent": 4, "raw_evidence": 8},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:a",
        project_policy="balanced",
    )

    assert _pack_ids(finalized) == [
        "project-a",
        "project-b-shared",
        "code:file:project.py",
    ]
    assert secret not in json.dumps(finalized.audit_metadata)


@pytest.mark.parametrize("memory_id", ["principle:orphan-control", "code:orphan-control"])
def test_noncanonical_prefix_cannot_revive_orphan_synthesis_control(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine._sqlite._conn.execute(
        "INSERT INTO synthesis_artifacts "
        "(memory_id, synthesis_key, status, created_at, updated_at) "
        "VALUES (?, ?, 'verified', 'now', 'now')",
        (memory_id, f"key:{memory_id}"),
    )
    engine._sqlite._conn.commit()

    decision = engine._gate_memory_ids([memory_id])

    assert decision.items == ()
    assert decision.dropped_ids == (memory_id,)
    assert decision.degradations == ({"id": memory_id, "reason": "candidate_missing"},)


def test_finalizer_prefers_cross_process_sqlite_project_visibility(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine.register_memory(
        {
            "id": "stale-project-runtime",
            "content": "STALE-PROJECT-RUNTIME-SECRET",
            "memory_type": "experience",
            "project_id": "project:a",
            "visibility": "global",
        }
    )
    engine._sqlite._conn.execute(
        """
        UPDATE memories
        SET project_id = 'project:b', visibility = 'project'
        WHERE id = 'stale-project-runtime'
        """
    )
    engine._sqlite._conn.commit()
    pack = ContextPack(
        core=[
            ContextItem(
                "stale-project-runtime",
                "STALE-PROJECT-RUNTIME-SECRET",
                0.95,
                layer="core",
            )
        ]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:a",
        project_policy="strict",
    )

    assert _pack_ids(finalized) == []
    assert "STALE-PROJECT-RUNTIME-SECRET" not in json.dumps(finalized.audit_metadata)


def test_canonical_project_columns_override_conflicting_metadata_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine.register_memory(
        {
            "id": "canonical-project-columns",
            "content": "CANONICAL-COLUMNS-SECRET",
            "memory_type": "experience",
            "project_id": "project:b",
            "visibility": "project",
            "metadata_json": {
                "project_id": "project:a",
                "visibility": "global",
                "source_class": "experience",
            },
        }
    )
    pack = ContextPack(
        core=[
            ContextItem(
                "canonical-project-columns",
                "CANONICAL-COLUMNS-SECRET",
                0.95,
                layer="core",
            )
        ]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:a",
        project_policy="strict",
    )

    assert _pack_ids(finalized) == []
    assert "CANONICAL-COLUMNS-SECRET" not in json.dumps(finalized.audit_metadata)


def test_same_project_private_visibility_remains_recallable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine.register_memory(
        {
            "id": "same-project-private",
            "content": "same project private context",
            "memory_type": "experience",
            "project_id": "project:a",
            "visibility": "private",
        }
    )
    pack = ContextPack(
        core=[
            ContextItem(
                "same-project-private",
                "same project private context",
                0.95,
                layer="core",
            )
        ]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:a",
        project_policy="strict",
    )

    assert _pack_ids(finalized) == ["same-project-private"]


def test_project_lookup_error_fails_closed_even_with_stale_runtime_record(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _governed_engine(tmp_path, monkeypatch)
    engine.register_memory(
        {
            "id": "closed-project-lookup",
            "content": "CLOSED-PROJECT-LOOKUP-SECRET",
            "memory_type": "experience",
            "project_id": "project:a",
            "visibility": "global",
        }
    )
    engine._gate_memory_ids = lambda ids: retrieval.SynthesisGateResult(tuple(ids), (), ())
    engine._sqlite._conn.close()
    pack = ContextPack(
        core=[
            ContextItem(
                "closed-project-lookup",
                "CLOSED-PROJECT-LOOKUP-SECRET",
                0.95,
                layer="core",
            )
        ]
    )
    plan = RetrievalPlan(
        mode="global",
        budget={"core": 2, "related": 2, "divergent": 1, "raw_evidence": 4},
    )

    finalized = engine._finalize_supply_pack(
        pack,
        plan,
        task_type="general",
        project_id="project:a",
        project_policy="strict",
    )

    assert _pack_ids(finalized) == []


def test_memory_version_migration_collapses_valid_rows_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "legacy-version.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_version (version)")
    conn.executemany("INSERT INTO memory_version VALUES (?)", [(2,), (9,), (4,)])
    conn.commit()
    conn.close()

    first = context_engine_module._SQLiteStorage(str(db_path))
    assert first._conn.execute("SELECT singleton, version FROM memory_version").fetchall() == [
        (1, 9)
    ]
    first._conn.close()

    second = context_engine_module._SQLiteStorage(str(db_path))
    assert second._conn.execute("SELECT singleton, version FROM memory_version").fetchall() == [
        (1, 9)
    ]
    second._conn.execute("UPDATE memory_version SET version = version + 1")
    assert retrieval.read_memory_version(second._conn) == 10
    second._conn.close()


@pytest.mark.parametrize("invalid", ["invalid", -1, 1.5, None])
def test_memory_version_migration_leaves_invalid_legacy_state_fail_closed(
    tmp_path,
    invalid: object,
) -> None:
    db_path = tmp_path / "invalid-version.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_version (version)")
    conn.execute("INSERT INTO memory_version VALUES (?)", (invalid,))
    conn.commit()

    with pytest.raises(ValueError, match="memory_version_invalid"):
        context_engine_module._ensure_memory_version_schema(conn)

    assert conn.execute("SELECT version FROM memory_version").fetchall() == [(invalid,)]
    with pytest.raises(ValueError):
        retrieval.read_memory_version(conn)
    conn.close()


def test_memory_version_migration_does_not_commit_caller_transaction(tmp_path) -> None:
    db_path = tmp_path / "transaction-version.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_version (version)")
    conn.executemany("INSERT INTO memory_version VALUES (?)", [(1,), (3,)])
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO marker VALUES ('uncommitted')")

    context_engine_module._ensure_memory_version_schema(conn)

    assert conn.in_transaction is True
    assert conn.execute("SELECT singleton, version FROM memory_version").fetchall() == [(1, 3)]
    conn.rollback()
    assert conn.execute("SELECT value FROM marker").fetchall() == []
    assert conn.execute("SELECT version FROM memory_version ORDER BY version").fetchall() == [
        (1,),
        (3,),
    ]
    conn.close()


def test_invalid_legacy_memory_version_does_not_block_ordinary_engine_use(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "invalid-engine-version.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_version (version)")
    conn.execute("INSERT INTO memory_version VALUES ('invalid')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))

    engine = ContextEngine(use_sqlite=True)
    engine.register_memory(
        {
            "id": "ordinary-with-invalid-version",
            "content": "ordinary remains available",
            "memory_type": "experience",
        }
    )

    assert engine.memory_exists("ordinary-with-invalid-version")
    assert engine._sqlite._conn.execute("SELECT version FROM memory_version").fetchall() == [
        ("invalid",)
    ]
    with pytest.raises(ValueError):
        retrieval.read_memory_version(engine._sqlite._conn)


def test_invalid_memory_version_cold_start_still_loads_persisted_ordinary_memory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "invalid-version-cold-start.db"
    storage = context_engine_module._SQLiteStorage(str(db_path))
    storage.upsert(
        "persisted-ordinary",
        {
            "id": "persisted-ordinary",
            "content": "persisted ordinary remains available in degraded mode",
            "memory_type": "experience",
        },
    )
    storage._conn.execute("DROP TABLE memory_version")
    storage._conn.execute("CREATE TABLE memory_version (version)")
    storage._conn.execute("INSERT INTO memory_version VALUES ('invalid')")
    storage._conn.commit()
    storage._conn.close()
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))

    engine = ContextEngine(use_sqlite=True)

    assert engine.get_memory_dict("persisted-ordinary")["memory_type"] == "experience"
    assert engine.canonical_sync_ok is False
    with pytest.raises(ValueError, match="memory_version_invalid"):
        retrieval.read_memory_version(engine._sqlite._conn)
    engine._sqlite._conn.close()


def test_recall_cache_key_includes_memory_version() -> None:
    first = memory_tools._cache_key("q", "general", 20, "global", memory_version=1)
    second = memory_tools._cache_key("q", "general", 20, "global", memory_version=2)

    assert first != second


@pytest.mark.parametrize("mutation", ["status", "source", "type"])
def test_cache_hit_revalidates_top_level_and_nested_envelope_surfaces(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    import plastic_promise.adaptive_retrieval as adaptive_retrieval
    import plastic_promise.core.embedder as embedder_mod

    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(
        engine,
        "cached-s1",
        content="CACHED-SYNTHESIS-SECRET",
    )
    calls = {"count": 0}

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0]

    def supply(*args, **kwargs):
        calls["count"] += 1
        item = ContextItem(
            "cached-s1",
            "CACHED-SYNTHESIS-SECRET",
            0.99,
            source="synthesis",
            layer="core",
        )
        pack = ContextPack(core=[item])
        pack.audit_metadata = {
            "mode": "global",
            "budget": {"core": 6, "related": 10, "divergent": 6, "raw_evidence": 8},
            "raw_evidence": [
                {
                    "id": "cached-s1",
                    "source": "text",
                    "score": 0.99,
                    "content": "CACHED-SYNTHESIS-SECRET",
                }
            ],
        }
        pack.per_item_stats = [{"id": "cached-s1", "content": "CACHED-SYNTHESIS-SECRET"}]
        return pack

    engine.supply = supply
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()
    args = {
        "query": "cached synthesis query",
        "project_id": "project:test",
        "request_id": "stable-cache-request",
        "debug": True,
    }

    first = asyncio.run(memory_tools.handle_memory_recall(engine, args))[0].text
    assert "cached-s1" in first
    if mutation == "status":
        engine._sqlite._conn.execute(
            "UPDATE synthesis_artifacts SET status = 'draft' WHERE memory_id = 'cached-s1'"
        )
    elif mutation == "source":
        engine._sqlite._conn.execute(
            "UPDATE memories SET content = 'source changed' WHERE id = ?",
            (source_id,),
        )
    else:
        engine._sqlite._conn.execute(
            "UPDATE memories SET memory_type = 'experience' WHERE id = 'cached-s1'"
        )
    engine._sqlite._conn.commit()

    second_text = asyncio.run(memory_tools.handle_memory_recall(engine, args))[0].text
    second = json.loads(second_text)

    assert calls["count"] == 1
    assert second["core"] == []
    assert second["data"]["core"] == []
    assert second["raw_evidence"] == []
    assert second["data"]["raw_evidence"] == []
    assert "cached-s1" not in json.dumps(second["audit"]["raw_evidence"])
    assert "cached-s1" not in json.dumps(second["data"]["audit"]["raw_evidence"])
    degradations = second["audit"]["synthesis_retrieval"]["degradations"]
    assert degradations[0].keys() == {"id", "reason"}
    assert "CACHED-SYNTHESIS-SECRET" not in second_text


def test_memory_version_change_bypasses_existing_recall_cache(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plastic_promise.adaptive_retrieval as adaptive_retrieval
    import plastic_promise.core.embedder as embedder_mod

    engine = _governed_engine(tmp_path, monkeypatch)
    calls = {"count": 0}

    class FakeEmbedder:
        async def aembed(self, text):
            return [0.0]

    def supply(*args, **kwargs):
        calls["count"] += 1
        return ContextPack()

    engine.supply = supply
    monkeypatch.setattr(adaptive_retrieval, "should_retrieve", lambda query: True)
    monkeypatch.setattr(
        embedder_mod, "get_embedder", lambda fallback_on_error=False: FakeEmbedder()
    )
    with memory_tools._query_cache_lock:
        memory_tools._query_cache.clear()
    args = {
        "query": "versioned cache query",
        "project_id": "project:test",
        "request_id": "same-request-id",
    }

    asyncio.run(memory_tools.handle_memory_recall(engine, args))
    engine._sqlite._conn.execute("UPDATE memory_version SET version = version + 1")
    engine._sqlite._conn.commit()
    asyncio.run(memory_tools.handle_memory_recall(engine, args))

    assert calls["count"] == 2


class _IndexRecordingEmbedder:
    model_name = "synthesis-index-test"

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        return [0.1] * self.dim


@pytest.mark.parametrize("repair", ["sync_with_engine", "rebuild_all", "backfill"])
@pytest.mark.parametrize("status, expected", [("verified", True), ("draft", False)])
def test_lancedb_repair_paths_index_only_verified_synthesis(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    repair: str,
    status: str,
    expected: bool,
) -> None:
    from plastic_promise.core.lancedb_store import EMB_DIM, LanceDBStore

    engine = _governed_engine(tmp_path, monkeypatch)
    _register_governed_synthesis(engine, "repair-index", status=status)
    embedder = _IndexRecordingEmbedder(EMB_DIM)
    store = LanceDBStore(str(tmp_path / f"{repair}-{status}.lancedb"), embedder)

    getattr(store, repair)(engine)

    assert ("repair-index" in store.list_memory_ids()) is expected
    assert ("governed synthesis repair-index" in embedder.texts) is expected
    persisted_status = engine._sqlite._conn.execute(
        "SELECT status FROM synthesis_artifacts WHERE memory_id = 'repair-index'"
    ).fetchone()
    assert persisted_status == (status,)


@pytest.mark.parametrize(
    "mode",
    ["draft", "unknown", "disabled", "missing_control", "source_drift"],
)
def test_lancedb_sync_removes_existing_ineligible_synthesis_without_status_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from plastic_promise.core.lancedb_store import EMB_DIM, LanceDBStore

    engine = _governed_engine(tmp_path, monkeypatch)
    status = mode if mode in {"draft", "unknown"} else "verified"
    source_id = _register_governed_synthesis(engine, "ineligible-index", status=status)
    conn = engine._sqlite._conn
    if mode == "disabled":
        monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "0")
    elif mode == "missing_control":
        conn.execute("DELETE FROM synthesis_artifacts WHERE memory_id = 'ineligible-index'")
    elif mode == "source_drift":
        conn.execute(
            "UPDATE memories SET content = 'source changed after verification' WHERE id = ?",
            (source_id,),
        )
    conn.commit()

    embedder = _IndexRecordingEmbedder(EMB_DIM)
    store = LanceDBStore(str(tmp_path / f"{mode}.lancedb"), embedder)
    store.insert("ineligible-index", [0.1] * EMB_DIM, "must be removed")

    result = store.sync_with_engine(engine)

    assert "ineligible-index" in result["orphan_ids"]
    assert "ineligible-index" not in store.list_memory_ids()
    control = conn.execute(
        "SELECT status FROM synthesis_artifacts WHERE memory_id = 'ineligible-index'"
    ).fetchone()
    if mode == "missing_control":
        assert control is None
    else:
        assert control == (status,)


def test_lancedb_sync_rechecks_synthesis_after_source_materialization(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plastic_promise.core.lancedb_store import EMB_DIM, LanceDBStore

    engine = _governed_engine(tmp_path, monkeypatch)
    source_id = _register_governed_synthesis(engine, "late-stale", status="verified")
    engine.update_memory_fields(
        source_id,
        embedding_text="",
        embedding_hash="",
        search_text="",
        metadata_json={},
    )
    source_hash = canonical_memory_hash(engine._memories[source_id])
    conn = engine._sqlite._conn
    conn.execute(
        "UPDATE behavior_graph_edges SET metadata_json = ? WHERE source = ?",
        (json.dumps({"observed_content_hash": source_hash}), "late-stale"),
    )
    conn.execute(
        "UPDATE synthesis_artifacts SET source_fingerprint = ? WHERE memory_id = ?",
        (source_fingerprint({source_id: source_hash}), "late-stale"),
    )
    conn.commit()

    embedder = _IndexRecordingEmbedder(EMB_DIM)
    store = LanceDBStore(str(tmp_path / "late-stale.lancedb"), embedder)
    store.insert("late-stale", [0.1] * EMB_DIM, "initially eligible synthesis")

    result = store.sync_with_engine(engine)

    assert source_id in store.list_memory_ids()
    assert "late-stale" not in store.list_memory_ids()
    assert "late-stale" in result["orphan_ids"]
    assert engine._memories[source_id]["embedding_hash"]
    assert conn.execute(
        "SELECT status FROM synthesis_artifacts WHERE memory_id = 'late-stale'"
    ).fetchone() == ("verified",)
