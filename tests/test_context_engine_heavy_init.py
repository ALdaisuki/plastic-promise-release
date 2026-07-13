import threading

import pytest

from plastic_promise.core.constants import CORE_PRINCIPLES
from plastic_promise.core.context_engine import ContextEngine, ContextPack, _SQLiteStorage
from plastic_promise.core.synthesis import SynthesisStore


class FakeDomainManager:
    def __init__(self, db_path):
        self.db_path = db_path


class FakeEmbedder:
    def embed(self, text):
        return [0.0] * 1024


class FakeLanceDBStore:
    instances = []

    def __init__(self, path, embedder):
        self.path = path
        self.embedder = embedder
        self.backfill_calls = 0
        self.rebuild_calls = 0
        self.sync_calls = 0
        self.row_count = 999
        FakeLanceDBStore.instances.append(self)

    def backfill(self, engine):
        self.backfill_calls += 1
        return 1

    def rebuild_all(self, engine):
        self.rebuild_calls += 1
        return 1

    def sync_with_engine(self, engine):
        self.sync_calls += 1
        return {"orphan_deleted": 1, "missing_backfilled": 1, "missing_skipped": 0}

    def count_rows(self):
        return self.row_count


def _install_fakes(monkeypatch):
    FakeLanceDBStore.instances = []
    monkeypatch.setattr(
        "plastic_promise.core.domain_manager.DomainManager",
        FakeDomainManager,
    )
    monkeypatch.setattr(
        "plastic_promise.core.embedder.get_embedder",
        lambda fallback_on_error=True: FakeEmbedder(),
    )
    monkeypatch.setattr(
        "plastic_promise.core.lancedb_store.LanceDBStore",
        FakeLanceDBStore,
    )


def _make_engine_with_memory():
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "mem_1": {
            "id": "mem_1",
            "content": "context supply cold start should stay responsive",
            "memory_type": "experience",
            "tier": "L1",
            "category": "fact",
            "scope": "global",
        }
    }
    return engine


def test_heavy_init_skips_lancedb_store_by_default_for_stdio_transport(monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setenv("PLASTIC_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("LDB_INIT_ON_HEAVY_INIT", raising=False)
    monkeypatch.delenv("LDB_BACKFILL_ON_INIT", raising=False)
    monkeypatch.delenv("LDB_REBUILD_ON_INIT", raising=False)

    engine = _make_engine_with_memory()
    engine._ensure_heavy_init()

    assert FakeLanceDBStore.instances == []
    assert engine._ldb is None


def test_heavy_init_runs_lancedb_maintenance_when_explicitly_enabled(monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setenv("PLASTIC_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "1")
    monkeypatch.setenv("LDB_BACKFILL_ON_INIT", "1")
    monkeypatch.setenv("LDB_REBUILD_ON_INIT", "1")

    engine = _make_engine_with_memory()
    engine._ensure_heavy_init()

    store = FakeLanceDBStore.instances[0]
    assert store.backfill_calls == 1
    assert store.sync_calls == 1
    assert store.rebuild_calls == 1


def test_supply_refreshes_canonical_cache_after_cross_process_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-canonical.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_FORCE_PYTHON_SUPPLY", "1")
    writer = ContextEngine(use_sqlite=True)
    writer.register_memory(
        {
            "id": "source-a",
            "content": "Independent source alpha contains durable supporting evidence. " * 3,
            "project_id": "project:test",
            "visibility": "project",
        }
    )
    writer.register_memory(
        {
            "id": "source-b",
            "content": "Independent source beta confirms the same durable conclusion. " * 3,
            "project_id": "project:test",
            "visibility": "project",
        }
    )
    reader = ContextEngine(use_sqlite=True)
    before_version = reader._loaded_memory_version
    store = SynthesisStore(writer._sqlite._conn, engine=writer)

    draft = store.create_draft(
        "Both independent sources support one durable conclusion.",
        ["source-a", "source-b"],
        synthesis_key="cross-process:alpha",
        validity_scope="project:test",
        project_id="project:test",
        visibility="project",
        automatic=False,
        call_id="call-cross-process",
    )
    assert reader.get_memory_dict(draft.memory_id) is None

    monkeypatch.setattr(reader, "_ensure_heavy_init", lambda: None)
    monkeypatch.setattr(reader, "_supply_python", lambda *args, **kwargs: ContextPack())
    reader.supply("refresh canonical cache", task_vector=[0.0] * 4)

    assert reader.get_memory_dict(draft.memory_id) is None
    assert store.get(draft.memory_id).content == draft.content
    assert reader._loaded_memory_version > before_version
    assert reader.canonical_sync_ok is True
    assert reader.list_graph_edges("derived_from") == []
    writer._sqlite._conn.close()
    reader._sqlite._conn.close()


def test_failed_canonical_refresh_retains_cache_and_fails_synthesis_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "refresh-failure.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    monkeypatch.setenv("PP_SYNTHESIS_ARTIFACTS", "on")
    monkeypatch.setenv("PP_SYNTHESIS_RETRIEVAL", "1")
    engine = ContextEngine(use_sqlite=True)
    engine.register_memory(
        {
            "id": "source-a",
            "content": "Alpha source material is long enough to support synthesis. " * 3,
            "project_id": "project:test",
            "visibility": "project",
        }
    )
    engine.register_memory(
        {
            "id": "source-b",
            "content": "Beta source material is independent and supports synthesis. " * 3,
            "project_id": "project:test",
            "visibility": "project",
        }
    )
    store = SynthesisStore(engine._sqlite._conn, engine=engine)
    draft = store.create_draft(
        "The sources jointly support a durable conclusion.",
        ["source-a", "source-b"],
        synthesis_key="refresh:failure",
        validity_scope="project:test",
        project_id="project:test",
        automatic=False,
    )
    verified = store.verify(draft.memory_id, "reviewer", "call-verify", 1)
    assert engine._gate_memory_ids([verified.memory_id]).items == (verified.memory_id,)
    cached = engine.get_memory_dict(verified.memory_id)
    original_iter = engine._sqlite.iter_all
    monkeypatch.setattr(
        engine._sqlite,
        "iter_all",
        lambda: (_ for _ in ()).throw(RuntimeError("snapshot unavailable")),
    )

    assert engine._refresh_canonical_cache_if_changed(force=True) is False
    assert engine.canonical_sync_ok is False
    assert engine.get_memory_dict(verified.memory_id) is None
    assert engine._get_memory_dict_unchecked(verified.memory_id) == cached
    decision = engine._gate_memory_ids([verified.memory_id])
    assert decision.items == ()
    assert decision.degradations[0]["reason"] == "memory_version_invalid"

    monkeypatch.setattr(engine._sqlite, "iter_all", original_iter)
    assert engine._refresh_canonical_cache_if_changed(force=False) is True
    assert engine.canonical_sync_ok is True
    assert engine._gate_memory_ids([verified.memory_id]).items == (verified.memory_id,)
    engine._sqlite._conn.close()


def test_constructor_rebuilds_persisted_overlay_once(tmp_path, monkeypatch):
    db_path = tmp_path / "single-constructor-overlay.db"
    storage = _SQLiteStorage(str(db_path))
    keyword = CORE_PRINCIPLES[0]["keywords"][0]
    storage.upsert(
        "persisted-overlay",
        {
            "id": "persisted-overlay",
            "content": f"{keyword} canonical overlay must be installed exactly once",
            "memory_type": "experience",
            "worth_success": 8,
            "worth_failure": 0,
            "project_id": "project:test",
        },
    )
    storage._conn.close()
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))

    engine = ContextEngine(use_sqlite=True)
    semantic_edges = [
        edge
        for edge in engine._graph_edges
        if edge.get("to") == "persisted-overlay" and edge.get("relation") == "governs"
    ]

    assert len(semantic_edges) == 1
    engine._sqlite._conn.close()


def test_constructor_deduplicates_persisted_graph_edge_by_semantic_identity(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "semantic-constructor-overlay.db"
    storage = _SQLiteStorage(str(db_path))
    principle = CORE_PRINCIPLES[0]
    keyword = principle["keywords"][0]
    storage.upsert(
        "semantic-overlay",
        {
            "id": "semantic-overlay",
            "content": f"{keyword} semantic overlay must not duplicate persisted graph state",
            "memory_type": "experience",
            "project_id": "project:test",
        },
    )
    storage.upsert_graph_edge(
        {
            "from": f"principle:{principle['id']}",
            "to": "semantic-overlay",
            "relation": "governs",
            "weight": 0.123,
            "metadata": {"persisted": True},
        }
    )
    storage._conn.close()
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))

    engine = ContextEngine(use_sqlite=True)
    semantic_edges = [
        edge
        for edge in engine._graph_edges
        if edge.get("from") == f"principle:{principle['id']}"
        and edge.get("to") == "semantic-overlay"
        and edge.get("relation") == "governs"
    ]

    assert len(semantic_edges) == 1
    assert semantic_edges[0]["metadata"] == {"persisted": True}
    engine._sqlite._conn.close()


def test_canonical_refresh_rebuilds_runtime_derived_graph_overlay(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime-graph-overlay.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    engine = ContextEngine(use_sqlite=True)
    keyword = CORE_PRINCIPLES[0]["keywords"][0]
    skill_entity = "skill:cache-refresh:run"
    memory_id = engine.register_memory(
        {
            "id": "ordinary-overlay",
            "content": f"{keyword} ordinary memory keeps deterministic graph context",
            "memory_type": "experience",
            "entity_ids": [skill_entity],
            "project_id": "project:test",
        }
    )
    engine._rebuild_graph_from_memories()

    def overlay_state():
        return {
            "reference": any(
                edge.get("from") == memory_id
                and edge.get("to") == f"skill_session:{skill_entity}"
                and edge.get("relation") == "references"
                for edge in engine._graph_edges
            ),
            "skill_node": f"skill_session:{skill_entity}" in engine._graph_nodes,
            "governs": any(
                edge.get("to") == memory_id and edge.get("relation") == "governs"
                for edge in engine._graph_edges
            ),
            "embodies": any(
                edge.get("from") == memory_id and edge.get("relation") == "embodies"
                for edge in engine._graph_edges
            ),
        }

    assert overlay_state() == {
        "reference": True,
        "skill_node": True,
        "governs": True,
        "embodies": True,
    }
    engine._sqlite._conn.execute("UPDATE memory_version SET version = version + 1")
    engine._sqlite._conn.commit()

    assert engine._refresh_canonical_cache_if_changed() is True
    assert overlay_state() == {
        "reference": True,
        "skill_node": True,
        "governs": True,
        "embodies": True,
    }
    engine._sqlite._conn.close()


def test_canonical_refresh_reapplies_feedback_weight_from_persisted_worth(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime-graph-feedback.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    engine = ContextEngine(use_sqlite=True)
    keyword = CORE_PRINCIPLES[0]["keywords"][0]
    memory_id = engine.register_memory(
        {
            "id": "ordinary-feedback",
            "content": f"{keyword} persisted feedback keeps its graph influence",
            "memory_type": "experience",
            "worth_success": 8,
            "worth_failure": 0,
            "project_id": "project:test",
        }
    )

    def governed_weight():
        return next(
            edge["weight"]
            for edge in engine._graph_edges
            if edge.get("to") == memory_id and edge.get("relation") == "governs"
        )

    base_weight = governed_weight()
    engine.apply_edge_feedback_for_memory(memory_id)
    feedback_weight = governed_weight()
    assert feedback_weight != base_weight
    engine._sqlite._conn.execute("UPDATE memory_version SET version = version + 1")
    engine._sqlite._conn.commit()

    assert engine._refresh_canonical_cache_if_changed() is True
    assert governed_weight() == pytest.approx(feedback_weight)
    engine._sqlite._conn.close()


def test_canonical_feedback_replay_scans_edges_once_and_skips_unobserved_memories():
    class CountingEdges(list):
        def __init__(self, values):
            super().__init__(values)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    memories = {
        "observed-a": {"worth_success": 4, "worth_failure": 0},
        "observed-b": {"worth_success": 0, "worth_failure": 3},
        "partially-observed": {"worth_success": 0, "worth_failure": 0.5},
        "unobserved": {"worth_success": 0, "worth_failure": 0},
    }

    def replay(memory_items):
        engine = ContextEngine(use_sqlite=False)
        engine._memories = dict(memory_items)
        edges = CountingEdges(
            [
                {
                    "from": "principle:1",
                    "to": "observed-a",
                    "relation": "governs",
                    "weight": 0.5,
                },
                {
                    "from": "observed-a",
                    "to": "observed-b",
                    "relation": "references",
                    "weight": 0.5,
                },
                {
                    "from": "principle:1",
                    "to": "partially-observed",
                    "relation": "governs",
                    "weight": 0.5,
                },
                {
                    "from": "principle:1",
                    "to": "unobserved",
                    "relation": "governs",
                    "weight": 0.5,
                },
            ]
        )
        engine._graph_edges = edges
        engine._reapply_canonical_edge_feedback()
        weights = tuple(edges[index]["weight"] for index in range(len(edges)))
        return edges, weights

    forward_edges, forward_weights = replay(memories.items())
    reverse_edges, reverse_weights = replay(reversed(tuple(memories.items())))
    runtime_engine = ContextEngine(use_sqlite=False)
    runtime_engine._memories = dict(memories)
    runtime_engine._graph_edges = [
        {
            "from": forward_edges[index]["from"],
            "to": forward_edges[index]["to"],
            "relation": forward_edges[index]["relation"],
            "weight": 0.5,
        }
        for index in range(len(forward_edges))
    ]
    runtime_engine._apply_edge_feedback_locked()
    runtime_weights = tuple(
        runtime_engine._graph_edges[index]["weight"]
        for index in range(len(runtime_engine._graph_edges))
    )
    runtime_engine._apply_edge_feedback_locked()
    repeated_runtime_weights = tuple(
        runtime_engine._graph_edges[index]["weight"]
        for index in range(len(runtime_engine._graph_edges))
    )

    assert forward_edges.iterations == reverse_edges.iterations == 1
    assert forward_weights == reverse_weights == runtime_weights
    assert repeated_runtime_weights == runtime_weights
    assert forward_weights[0] != 0.5
    assert forward_weights[1] != 0.5
    assert forward_weights[2] != 0.5
    assert forward_weights[3] == 0.5


def _run_writer_replacement_interleave(writer, replacement, reached, release):
    errors = []

    def capture(callable_obj):
        try:
            callable_obj()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    writer_thread = threading.Thread(target=capture, args=(writer,))
    writer_thread.start()
    assert reached.wait(1)
    replacement_thread = threading.Thread(target=capture, args=(replacement,))
    replacement_thread.start()
    replacement_thread.join(0.1)
    release.set()
    writer_thread.join(2)
    replacement_thread.join(2)
    assert not writer_thread.is_alive()
    assert not replacement_thread.is_alive()
    assert errors == []


def test_register_memory_serializes_creation_with_runtime_snapshot_refresh():
    reached = threading.Event()
    release = threading.Event()

    class FakeStorage:
        def __init__(self):
            self.rows = {}

        def create_ordinary_if_absent(self, memory_id, data):
            reached.set()
            assert release.wait(2)
            self.rows[memory_id] = dict(data)
            return dict(data), True

    engine = ContextEngine(use_sqlite=False)
    storage = FakeStorage()
    engine._sqlite = storage

    def replacement():
        with engine._write_lock:
            engine._memories = {memory_id: dict(data) for memory_id, data in storage.rows.items()}

    _run_writer_replacement_interleave(
        lambda: engine.register_memory(
            {
                "id": "ordinary-race",
                "content": "ordinary write must remain visible after snapshot replacement",
                "memory_type": "experience",
            }
        ),
        replacement,
        reached,
        release,
    )

    assert "ordinary-race" in storage.rows
    assert engine.get_memory_dict("ordinary-race") is not None


def test_register_memory_persistence_failure_does_not_dirty_runtime_cache():
    class FailingStorage:
        def create_ordinary_if_absent(self, memory_id, data):
            raise RuntimeError("injected persistence failure")

    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = FailingStorage()

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        engine.register_memory(
            {
                "id": "ordinary-failed-write",
                "content": "this row must never appear only in runtime",
                "memory_type": "experience",
            }
        )

    assert engine.get_memory_dict("ordinary-failed-write") is None


def test_increment_field_persistence_failure_does_not_dirty_runtime_cache():
    class FailingStorage:
        def patch_ordinary(self, memory_id, **_kwargs):
            raise RuntimeError("injected increment persistence failure")

    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "ordinary-counter": {
            "id": "ordinary-counter",
            "content": "counter remains canonical",
            "memory_type": "experience",
            "access_count": 4,
        }
    }
    engine._sqlite = FailingStorage()

    with pytest.raises(RuntimeError, match="injected increment persistence failure"):
        engine.increment_field("ordinary-counter", "access_count")

    assert engine.get_memory_dict("ordinary-counter")["access_count"] == 4


def test_text_retrieval_promotion_failure_does_not_dirty_runtime_cache():
    class FailingStorage:
        def patch_ordinary(self, memory_id, **_kwargs):
            raise RuntimeError("injected promotion persistence failure")

    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "ordinary-promotion": {
            "id": "ordinary-promotion",
            "content": "promotion sentinel searchable content",
            "memory_type": "experience",
            "source": "test",
            "tier": "L1",
            "access_count": 4,
            "worth_success": 0,
            "worth_failure": 0,
        }
    }
    engine._sqlite = FailingStorage()
    before = engine.get_memory_dict("ordinary-promotion")

    with pytest.raises(RuntimeError, match="injected promotion persistence failure"):
        engine._text_retrieval("promotion sentinel searchable")

    assert engine.get_memory_dict("ordinary-promotion") == before


def test_text_retrieval_does_not_mutate_governed_synthesis():
    engine = ContextEngine(use_sqlite=False)
    engine._memories = {
        "governed-synthesis": {
            "id": "governed-synthesis",
            "content": "governed synthesis searchable content",
            "memory_type": "synthesis",
            "source": "test",
            "tier": "L1",
            "access_count": 4,
            "worth_success": 0,
            "worth_failure": 0,
        }
    }
    before = dict(engine._memories["governed-synthesis"])

    results = engine._text_retrieval("governed synthesis searchable")

    assert results == []
    assert engine.get_memory_dict("governed-synthesis") is None
    assert engine._memories["governed-synthesis"] == before


def test_register_entity_serializes_node_and_edge_persistence_with_replacement():
    reached = threading.Event()
    release = threading.Event()

    class FakeStorage:
        def __init__(self):
            self.nodes = {}
            self.edges = []

        def upsert_graph_node(self, node_id, node):
            reached.set()
            assert release.wait(2)
            self.nodes[node_id] = dict(node)

        def upsert_graph_edge(self, edge):
            self.edges.append(dict(edge))

    engine = ContextEngine(use_sqlite=False)
    storage = FakeStorage()
    engine._sqlite = storage

    def replacement():
        with engine._write_lock:
            engine._graph_nodes = {node_id: dict(node) for node_id, node in storage.nodes.items()}
            engine._graph_edges = [dict(edge) for edge in storage.edges]

    _run_writer_replacement_interleave(
        lambda: engine.register_entity(
            "task",
            "race-node",
            "Race node",
            related_entities=["memory:target"],
        ),
        replacement,
        reached,
        release,
    )

    assert "task:race-node" in storage.nodes
    assert engine.get_graph_node("task:race-node") is not None
    assert any(
        edge.get("from") == "task:race-node" and edge.get("to") == "memory:target"
        for edge in storage.edges
    )
    assert any(
        edge.get("from") == "task:race-node" and edge.get("to") == "memory:target"
        for edge in engine._graph_edges
    )


def test_remove_graph_edge_persists_and_canonical_refresh_does_not_restore_it(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "persistent-edge-delete.db"
    monkeypatch.setenv("PLASTIC_DB_PATH", str(db_path))
    engine = ContextEngine(use_sqlite=True)
    assert engine.add_graph_edge("task:source", "memory:target", "references") is True

    assert engine.remove_graph_edge("task:source", "memory:target", "references") == 1
    assert (
        engine._sqlite._conn.execute(
            "SELECT 1 FROM behavior_graph_edges "
            "WHERE source = 'task:source' AND target = 'memory:target' "
            "AND relation = 'references'"
        ).fetchone()
        is None
    )
    assert engine._refresh_canonical_cache_if_changed(force=True) is True
    assert not any(
        edge.get("from") == "task:source"
        and edge.get("to") == "memory:target"
        and edge.get("relation") == "references"
        for edge in engine._graph_edges
    )
    engine._sqlite._conn.close()


def test_remove_graph_edge_persistence_failure_keeps_runtime_edge():
    class FailingStorage:
        def delete_graph_edges(self, source, target, relation=None):
            raise RuntimeError("injected edge delete failure")

    edge = {
        "from": "task:source",
        "to": "memory:target",
        "relation": "references",
        "weight": 0.5,
    }
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = FailingStorage()
    engine._graph_edges = [dict(edge)]

    with pytest.raises(RuntimeError, match="injected edge delete failure"):
        engine.remove_graph_edge("task:source", "memory:target", "references")

    assert engine._graph_edges == [edge]
