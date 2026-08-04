from __future__ import annotations

import pytest

from plastic_promise.core.context_engine import ContextEngine, ContextItem, ContextPack
from plastic_promise.core.memory_relations import organize_memory_relations
from plastic_promise.core.retrieval_planner import plan_retrieval
from plastic_promise.core.synthesis_retrieval import read_memory_version


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "memory-relations.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    instance = ContextEngine(use_sqlite=True)
    try:
        yield instance
    finally:
        instance._sqlite._conn.close()


def _seed(
    engine,
    memory_id: str,
    content: str,
    *,
    project_id: str = "project:test",
    created_at: str = "2026-07-22T00:00:00Z",
):
    engine.create_ordinary_if_absent(
        {
            "id": memory_id,
            "content": content,
            "memory_type": "experience",
            "source": "test",
            "category": "decision",
            "domain": "building",
            "project_id": project_id,
            "visibility": "project",
            "created_at": created_at,
        }
    )


def _plan():
    return plan_retrieval(
        task_type="building",
        scope="global",
        project_policy="balanced",
        has_vector=False,
        has_graph=True,
        has_fts=False,
    )


def test_related_memory_is_tagged_linked_and_expanded_into_context(engine):
    _seed(engine, "old", "TypeScript strict mode supports the API service.")
    _seed(
        engine,
        "new",
        "TypeScript strict mode improves API service reliability.",
        created_at="2026-07-22T00:01:00Z",
    )
    original_content = engine._sqlite.get("new")["content"]

    result = organize_memory_relations(engine, "new", call_id="call:relation")

    assert result["status"] == "completed"
    assert result["relations"][0]["memory_id"] == "old"
    assert result["relations"][0]["relation"] == "related_to"
    assert "topic:typescript" in result["topic_tags"]
    assert engine._sqlite.get("new")["content"] == original_content
    assert len(engine.list_graph_edges("related_to")) == 1
    assert engine._sqlite._conn.execute(
        "SELECT memory_id, parent_memory_id, relation FROM memory_lineage"
    ).fetchall() == [("new", "old", "related_to")]

    pack = ContextPack(
        core=[
            ContextItem(
                id="new",
                content=original_content,
                relevance=0.82,
                source="bm25",
                layer="core",
            )
        ]
    )
    finalized = engine._finalize_supply_pack(
        pack,
        _plan(),
        task_type="building",
        project_id="project:test",
        project_policy="balanced",
    )

    assert [(item.id, item.source) for item in finalized.related] == [
        ("old", "memory-graph:related_to")
    ]
    assert finalized.audit_metadata["memory_relation_expansion"] == {
        "count": 1,
        "max_hops": 1,
    }


@pytest.mark.parametrize(
    ("content", "expected_relation"),
    [
        ("Do not use TypeScript for the API service.", "contradicts"),
        ("Replace TypeScript with Python for the API service.", "supersedes"),
    ],
)
def test_conflict_and_supersession_are_marked_without_rewriting_content(
    engine, content, expected_relation
):
    _seed(engine, "old", "Use TypeScript for the API service.")
    _seed(engine, "new", content, created_at="2026-07-22T00:01:00Z")
    before = {memory_id: engine._sqlite.get(memory_id)["content"] for memory_id in ("old", "new")}

    result = organize_memory_relations(engine, "new", call_id="call:relation")

    assert any(item["relation"] == expected_relation for item in result["relations"])
    assert {
        memory_id: engine._sqlite.get(memory_id)["content"] for memory_id in ("old", "new")
    } == before
    edge = engine.list_graph_edges(expected_relation)[0]
    assert edge["from"] == "new"
    assert edge["to"] == "old"
    if expected_relation == "contradicts":
        assert edge["metadata"]["conflict_resolution"] == "review_required"


def test_relation_organization_is_idempotent_and_does_not_rebump_version(engine):
    _seed(engine, "old", "Use TypeScript for the API service.")
    _seed(
        engine,
        "new",
        "Do not use TypeScript for the API service.",
        created_at="2026-07-22T00:01:00Z",
    )

    first = organize_memory_relations(engine, "new", call_id="call:first")
    first_version = read_memory_version(engine._sqlite._conn)
    second = organize_memory_relations(engine, "new", call_id="call:second")
    second_version = read_memory_version(engine._sqlite._conn)

    assert first["relations"] == second["relations"]
    assert second_version == first_version
    assert len(engine.list_graph_edges("contradicts")) == 1
    assert engine._sqlite._conn.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0] == 1


def test_file_sync_boilerplate_does_not_create_spurious_relations(engine):
    _seed(engine, "old", "[FILE SYNC] alpha:")
    _seed(engine, "new", "[FILE SYNC] beta:", created_at="2026-07-22T00:01:00Z")

    result = organize_memory_relations(engine, "new", call_id="call:file-sync")

    assert result["relations"] == []
    assert "topic:file" not in result["topic_tags"]
    assert "topic:sync" not in result["topic_tags"]


def test_relations_and_context_expansion_do_not_cross_project_boundaries(engine):
    _seed(engine, "local", "Use TypeScript for the API service.", project_id="project:local")
    _seed(
        engine,
        "foreign",
        "Do not use TypeScript for the API service.",
        project_id="project:foreign",
    )

    result = organize_memory_relations(engine, "local", call_id="call:isolated")
    assert result["relations"] == []

    assert engine.add_graph_edge(
        "local",
        "foreign",
        relation="related_to",
        weight=0.9,
        source_kind="test",
    )
    pack = ContextPack(
        core=[
            ContextItem(
                id="local",
                content="Use TypeScript for the API service.",
                relevance=0.9,
                source="bm25",
                layer="core",
            )
        ]
    )
    finalized = engine._finalize_supply_pack(
        pack,
        _plan(),
        task_type="building",
        project_id="project:local",
        project_policy="balanced",
    )

    assert all(item.id != "foreign" for item in finalized.related)
    assert "memory_relation_expansion" not in finalized.audit_metadata
