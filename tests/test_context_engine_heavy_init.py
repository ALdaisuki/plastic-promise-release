from plastic_promise.core.context_engine import ContextEngine


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
        self.row_count = 999
        FakeLanceDBStore.instances.append(self)

    def backfill(self, engine):
        self.backfill_calls += 1
        return 1

    def rebuild_all(self, engine):
        self.rebuild_calls += 1
        return 1

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
    assert store.rebuild_calls == 1
