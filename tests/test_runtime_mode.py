import json
import threading
from types import SimpleNamespace

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.launcher.runtime_mode import (
    apply_runtime_mode,
    get_runtime_mode,
    runtime_mode_status,
    select_runtime_mode,
)


def test_runtime_mode_env_for_light():
    env = {}

    mode = apply_runtime_mode("light", env)

    assert mode.key == "light"
    assert env["PP_FORCE_PYTHON_SUPPLY"] == "1"
    assert env["PP_PREFER_RUST_SUPPLY"] == "0"
    assert env["LDB_INIT_ON_HEAVY_INIT"] == "0"
    assert env["PLASTIC_SKIP_LANCEDB_WARMUP"] == "1"
    assert env["PP_MEMORY_CHUNKING"] == "off"
    assert env["PP_MEMORY_CHUNK_ENGINE"] == "python"


def test_runtime_mode_env_for_rust_full():
    env = {}

    mode = apply_runtime_mode("rust-full", env)

    assert mode.key == "rust-full"
    assert env["PP_FORCE_PYTHON_SUPPLY"] == "0"
    assert env["PP_PREFER_RUST_SUPPLY"] == "1"
    assert env["LDB_INIT_ON_HEAVY_INIT"] == "1"
    assert env["LDB_BACKFILL_ON_INIT"] == "0"
    assert env["LDB_REBUILD_ON_INIT"] == "0"
    assert env["PLASTIC_SKIP_LANCEDB_WARMUP"] == "0"
    assert env["PP_MEMORY_CHUNKING"] == "structure-v1"
    assert env["PP_MEMORY_CHUNK_ENGINE"] == "rust"


def test_runtime_mode_env_for_python_full_enables_structure_v1():
    env = {"PP_MEMORY_CHUNKING": "shadow", "PP_MEMORY_CHUNK_ENGINE": "rust"}

    mode = apply_runtime_mode("full", env)

    assert mode.key == "full"
    assert env["PP_MEMORY_CHUNKING"] == "structure-v1"
    assert env["PP_MEMORY_CHUNK_ENGINE"] == "python"


def test_switching_from_full_to_normal_disables_structured_chunking():
    env = {}
    apply_runtime_mode("rust-full", env)

    apply_runtime_mode("rust-normal", env)

    assert env["PP_MEMORY_CHUNKING"] == "off"
    assert env["PP_MEMORY_CHUNK_ENGINE"] == "rust"


def test_non_full_mode_preserves_explicit_shadow_chunking():
    env = {"PP_MEMORY_CHUNKING": "shadow"}

    apply_runtime_mode("rust-normal", env)

    assert env["PP_MEMORY_CHUNKING"] == "shadow"
    assert env["PP_MEMORY_CHUNK_ENGINE"] == "rust"


def test_runtime_mode_accepts_chinese_aliases():
    assert get_runtime_mode("普通").key == "normal"
    assert get_runtime_mode("Rust加速版完全").key == "rust-full"


def test_select_runtime_mode_prompts_interactively():
    prompts = []
    mode = select_runtime_mode(
        interactive=True,
        input_func=lambda _: "3",
        print_func=prompts.append,
        environ={},
    )

    assert mode.key == "rust-normal"
    assert any("Plastic Promise" in line for line in prompts)


def test_select_runtime_mode_non_interactive_preserves_rust_full_default():
    mode = select_runtime_mode(interactive=False, environ={})

    assert mode.key == "rust-full"


@pytest.mark.asyncio
async def test_runtime_mode_mcp_set_refreshes_engine(monkeypatch):
    from plastic_promise.mcp.tools.runtime import handle_runtime_mode

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def refresh_runtime_mode(self, initialize_heavy=False, *, synchronize_index=False):
            self.calls.append((initialize_heavy, synchronize_index))
            return {
                "index_sync": {
                    "requested": synchronize_index,
                    "ready": True,
                    "status": "ready",
                }
            }

    for key in [
        "PLASTIC_RUNTIME_MODE",
        "PLASTIC_RUNTIME_DEPTH",
        "PP_FORCE_PYTHON_SUPPLY",
        "PP_PREFER_RUST_SUPPLY",
        "LDB_INIT_ON_HEAVY_INIT",
        "LDB_BACKFILL_ON_INIT",
        "LDB_REBUILD_ON_INIT",
        "PLASTIC_SKIP_LANCEDB_WARMUP",
        "PP_MEMORY_CHUNKING",
        "PP_MEMORY_CHUNK_ENGINE",
    ]:
        # Record absent originals so values written directly by the runtime
        # handler are removed during monkeypatch teardown.
        monkeypatch.setenv(key, "")

    engine = FakeEngine()

    result = await handle_runtime_mode(engine, {"action": "set", "mode": "rust-full"})
    data = json.loads(result[0].text)

    assert data["mode"] == "rust-full"
    assert data["rust_accelerated"] is True
    assert data["refresh"]["called"] is True
    assert engine.calls == [(True, True)]
    assert data["refresh"]["details"]["index_sync"]["ready"] is True


@pytest.mark.asyncio
async def test_runtime_mode_mcp_normal_does_not_synchronize_index(monkeypatch):
    from plastic_promise.mcp.tools.runtime import handle_runtime_mode

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def refresh_runtime_mode(self, initialize_heavy=False, *, synchronize_index=False):
            self.calls.append((initialize_heavy, synchronize_index))
            return {
                "index_sync": {
                    "requested": synchronize_index,
                    "ready": not synchronize_index,
                    "status": "not_requested" if not synchronize_index else "ready",
                }
            }

    engine = FakeEngine()
    result = await handle_runtime_mode(engine, {"action": "set", "mode": "rust-normal"})
    data = json.loads(result[0].text)

    assert data["mode"] == "rust-normal"
    assert engine.calls == [(True, False)]
    assert data["refresh"]["details"]["index_sync"] == {
        "requested": False,
        "ready": True,
        "status": "not_requested",
    }


def test_runtime_mode_status_reports_applied_mode():
    env = {}
    apply_runtime_mode("normal", env)

    status = runtime_mode_status(env)

    assert status["mode"] == "normal"
    assert status["runs_lancedb_warmup"] is False
    assert status["chunking"]["configured_mode"] == "off"
    assert status["chunking"]["requested_engine"] == "python"


def test_runtime_mode_status_matches_rust_first_supply_default():
    status = runtime_mode_status({})

    assert status["mode"] == "rust-normal"


def test_runtime_refresh_waits_for_active_retrieval_operations():
    engine = ContextEngine.__new__(ContextEngine)
    engine._enter_runtime_operation()
    refresh_started = threading.Event()
    refresh_acquired = threading.Event()

    def refresh():
        refresh_started.set()
        engine._begin_runtime_refresh()
        refresh_acquired.set()
        engine._end_runtime_refresh()

    worker = threading.Thread(target=refresh)
    worker.start()
    assert refresh_started.wait(1.0)
    assert not refresh_acquired.wait(0.05)

    engine._exit_runtime_operation()

    assert refresh_acquired.wait(1.0)
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_refresh_runtime_mode_light_clears_mode_coupled_dependencies(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    engine._ldb = object()
    old_embedder = SimpleNamespace(close=lambda: None)
    engine._embedder = old_embedder
    engine._principle_anchors = {1: [1.0]}
    engine.reset_rust_health = lambda: None
    resets = []
    monkeypatch.setattr(
        "plastic_promise.core.embedder.reset_embedder",
        lambda: resets.append(True) or old_embedder,
    )
    monkeypatch.setenv("LDB_INIT_ON_HEAVY_INIT", "0")

    result = ContextEngine.refresh_runtime_mode(engine, initialize_heavy=False)

    assert engine._ldb is None
    assert engine._embedder is None
    assert engine._principle_anchors == {}
    assert engine._heavy_init_done is False
    assert resets == [True]
    assert result["index_sync"] == {
        "requested": False,
        "ready": True,
        "status": "not_requested",
    }


def test_refresh_runtime_mode_full_reinitializes_and_synchronizes_index(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    old_embedder = SimpleNamespace(close=lambda: None)
    new_embedder = SimpleNamespace(index_model_name="vector-test|chunking=structure-v1")

    class FakeLanceDB:
        def sync_with_engine(self, candidate):
            assert candidate is engine
            return {
                "orphan_deleted": 1,
                "orphan_ids": ["orphan"],
                "missing_backfilled": 2,
                "missing_skipped": 0,
                "stale_ids": ["old-a", "old-b"],
                "stale_reindexed": 2,
            }

    new_lancedb = FakeLanceDB()
    engine._embedder = old_embedder
    engine._ldb = object()
    engine._principle_anchors = {1: [1.0]}
    engine.reset_rust_health = lambda: None

    def initialize():
        assert engine._embedder is None
        assert engine._ldb is None
        engine._embedder = new_embedder
        engine._ldb = new_lancedb
        engine._heavy_init_done = True

    engine._ensure_heavy_init = initialize
    monkeypatch.setattr(
        "plastic_promise.core.embedder.reset_embedder",
        lambda: old_embedder,
    )

    result = ContextEngine.refresh_runtime_mode(
        engine,
        initialize_heavy=True,
        synchronize_index=True,
    )

    assert engine._embedder is new_embedder
    assert engine._ldb is new_lancedb
    assert result["embedding_model"] == "vector-test|chunking=structure-v1"
    assert result["index_sync"] == {
        "requested": True,
        "ready": True,
        "status": "ready",
        "orphan_deleted": 1,
        "missing_backfilled": 2,
        "missing_skipped": 0,
        "stale_detected": 2,
        "stale_reindexed": 2,
        "invalid_material_count": 0,
        "diagnostic_count": 0,
    }


def test_refresh_runtime_mode_disables_mixed_identity_index(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    old_embedder = SimpleNamespace(close=lambda: None)
    new_embedder = SimpleNamespace(index_model_name="vector-test|chunking=structure-v1")

    class IncompleteLanceDB:
        def sync_with_engine(self, _engine):
            return {
                "orphan_deleted": 0,
                "orphan_ids": [],
                "missing_backfilled": 0,
                "missing_skipped": 1,
                "stale_ids": ["old"],
                "stale_reindexed": 0,
                "diagnostics": [{"reason": "embedding_failed"}],
            }

    engine._embedder = old_embedder
    engine._ldb = object()
    engine._principle_anchors = {}
    engine.reset_rust_health = lambda: None

    def initialize():
        engine._embedder = new_embedder
        engine._ldb = IncompleteLanceDB()
        engine._heavy_init_done = True

    engine._ensure_heavy_init = initialize
    monkeypatch.setattr(
        "plastic_promise.core.embedder.reset_embedder",
        lambda: old_embedder,
    )

    result = ContextEngine.refresh_runtime_mode(
        engine,
        initialize_heavy=True,
        synchronize_index=True,
    )

    assert result["index_sync"]["status"] == "maintenance_required"
    assert result["index_sync"]["ready"] is False
    assert engine._ldb is None


def test_refresh_runtime_mode_validates_index_when_sync_is_deferred(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    old_embedder = SimpleNamespace(close=lambda: None)
    engine._embedder = old_embedder
    engine._ldb = object()
    engine._principle_anchors = {}
    engine.reset_rust_health = lambda: None

    class MixedIdentityLanceDB:
        def validate_with_engine(self, _engine):
            return {
                "ready": False,
                "status": "maintenance_required",
                "missing_ids": [],
                "orphan_ids": [],
                "stale_ids": ["full-mode-memory"],
                "invalid_material_ids": [],
                "text_mismatch_ids": [],
            }

    def initialize():
        engine._embedder = SimpleNamespace(index_model_name="vector-test|chunking=off")
        engine._ldb = MixedIdentityLanceDB()
        engine._heavy_init_done = True

    engine._ensure_heavy_init = initialize
    monkeypatch.setattr(
        "plastic_promise.core.embedder.reset_embedder",
        lambda: old_embedder,
    )

    result = ContextEngine.refresh_runtime_mode(
        engine,
        initialize_heavy=True,
        synchronize_index=False,
    )

    assert result["index_sync"] == {
        "requested": False,
        "ready": False,
        "status": "maintenance_required",
        "missing_count": 0,
        "orphan_count": 0,
        "stale_count": 1,
        "invalid_material_count": 0,
        "text_mismatch_count": 0,
    }
    assert engine._ldb is None


def test_refresh_runtime_mode_fails_closed_when_index_sync_raises(monkeypatch):
    engine = ContextEngine.__new__(ContextEngine)
    engine._heavy_init_lock = threading.RLock()
    engine._heavy_init_done = True
    old_embedder = SimpleNamespace(close=lambda: None)
    engine._embedder = old_embedder
    engine._ldb = object()
    engine._principle_anchors = {}
    engine.reset_rust_health = lambda: None

    class BrokenLanceDB:
        def sync_with_engine(self, _engine):
            raise RuntimeError("backend unavailable")

    def initialize():
        engine._embedder = SimpleNamespace(index_model_name="vector-test")
        engine._ldb = BrokenLanceDB()
        engine._heavy_init_done = True

    engine._ensure_heavy_init = initialize
    monkeypatch.setattr(
        "plastic_promise.core.embedder.reset_embedder",
        lambda: old_embedder,
    )

    result = ContextEngine.refresh_runtime_mode(
        engine,
        initialize_heavy=True,
        synchronize_index=True,
    )

    assert result["index_sync"] == {
        "requested": True,
        "ready": False,
        "status": "sync_failed",
        "error_class": "RuntimeError",
    }
    assert engine._ldb is None


def test_refresh_memory_pipeline_cache_preserves_deferred_records(monkeypatch):
    from plastic_promise.mcp.tools import memory as memory_tools

    engine = SimpleNamespace(_embedder=object(), _ldb=object(), _dm=object())
    engine.get_fuzzy_buffer = lambda: None
    old_embedder = object()
    pipeline = SimpleNamespace(
        embedder=old_embedder,
        _lancedb=object(),
        _dm=None,
        _buffer={"pending": {"stage": "classified"}},
    )
    eid = id(engine)
    monkeypatch.setitem(memory_tools._fuzzy_buffers, eid, pipeline)

    result = memory_tools.refresh_memory_pipeline_cache(engine)

    assert pipeline.embedder is engine._embedder
    assert pipeline._lancedb is engine._ldb
    assert pipeline._dm is engine._dm
    assert pipeline._buffer == {"pending": {"stage": "classified"}}
    assert result == {"cached": True, "rebound": 1, "buffered": 1}
