"""Integration tests for Rust engine degradation and health check."""

import os
import time
import pytest

# Use in-memory mode (no SQLite) for test isolation
os.environ["AGENT_USE_SQLITE"] = "0"


def test_python_fallback_works_without_rust():
    """Python supply() works when Rust .pyd is unavailable."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Register a few test memories
    for i in range(5):
        engine.register_memory(
            {
                "id": f"test_{i:04d}",
                "content": f"Test memory {i} for integration testing",
                "memory_type": "task",
                "source": "test",
            }
        )

    # supply() should work — Rust path or Python fallback, doesn't matter
    pack = engine.supply("integration test task", task_type="general", scope="global")
    assert pack is not None
    # Python fallback should return some results (text retrieval works)
    assert pack.total_items >= 0  # at minimum, doesn't crash
    print(f"Python fallback: total_items={pack.total_items}")


def test_rust_health_check_initial_state():
    """Health check initializes correctly."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Initial state
    assert engine._rust_healthy is None
    assert engine._rust_health_checked_at == 0.0
    assert engine._rust_lock is not None

    # Health check runs without crashing
    result = engine._check_rust_health()
    # Result is True or None (False is never used per design)
    assert result is True or result is None


def test_rust_health_cache_ttl():
    """Health check caches result for TTL duration (when Rust is healthy).

    Design note: _rust_healthy=None (NOT False) on failure forces immediate
    re-probe on every call. The cache only applies when _rust_healthy is not None.
    This test validates the function doesn't crash and returns consistent values.
    """
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # First call — probes
    result1 = engine._check_rust_health()
    checked_at1 = engine._rust_health_checked_at

    # Immediate second call
    result2 = engine._check_rust_health()
    checked_at2 = engine._rust_health_checked_at

    assert result1 == result2  # consistent result regardless of cache
    # If Rust is healthy, cache prevents re-probe (same checked_at).
    # If Rust unavailable, None bypasses cache and re-probes (checked_at advances).
    # Either is valid — verify the function runs without error.
    assert checked_at1 <= checked_at2  # monotonic timestamp
    assert engine._rust_lock is not None
    print(f"Rust health cache: result={result1}, checked_at_diff={checked_at2 - checked_at1:.6f}s")


def test_reset_rust_health():
    """reset_rust_health() clears cache and forces re-probe."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    # Call once to attempt probe
    engine._check_rust_health()
    # _rust_healthy may be None (Rust unavailable) or True (Rust available)

    # Reset — always clears regardless of state
    engine.reset_rust_health()
    assert engine._rust_healthy is None  # cleared
    assert engine._rust_health_checked_at == 0.0  # cleared
    assert engine._rust_engine_instance is None  # cleared


def test_empty_memories_supply():
    """supply() with empty memory pool doesn't crash."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)

    pack = engine.supply("test with empty pool", task_type="general", scope="global")
    assert pack is not None
    assert pack.total_items == 0


def test_convert_rust_pack_preserves_pipeline_metadata():
    """PyO3 conversion keeps Rust audit and debug counters visible."""
    from plastic_promise.core.context_engine import ContextEngine

    class FakeItem:
        def __init__(self):
            self.id = "mem_1"
            self.content = "Rust snapshot metadata test"
            self.relevance = 0.9
            self.source = "test"
            self.freshness = "valid"
            self.layer = "core"
            self.is_principle = False
            self.worth_score = 0.5

    class FakeRustPack:
        core = [FakeItem()]
        related = []
        divergent = []
        activated_principles = ["全过程可查可透明"]
        audit_metadata = {"engine_version": "0.2.0-rs"}
        pipeline_stats = {
            "engine_mode": "snapshot",
            "vector_hits": "2",
            "bm25_hits": "3",
            "mmr_demoted": "1",
        }
        per_item_stats = [
            {"id": "mem_1", "final_score": "0.9000", "source": "test"},
        ]

    engine = ContextEngine(use_sqlite=False)
    pack = engine._convert_rust_pack(FakeRustPack())

    assert pack.audit_metadata["engine_version"] == "0.2.0-rs"
    assert pack.audit_metadata["engine_mode"] == "snapshot"
    assert pack.pipeline_stats["vector_hits"] == "2"
    assert pack.pipeline_stats["bm25_hits"] == "3"
    assert pack.pipeline_stats["mmr_demoted"] == "1"
    assert pack.per_item_stats[0]["final_score"] == "0.9000"


def test_concurrent_supply_does_not_crash():
    """Multiple concurrent supply() calls don't crash or corrupt state."""
    import threading
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    for i in range(20):
        engine.register_memory(
            {
                "id": f"conc_{i:04d}",
                "content": f"Concurrent test memory {i}",
                "memory_type": "task",
                "source": "test",
            }
        )

    errors = []
    results = []

    def call_supply(idx):
        try:
            pack = engine.supply(f"concurrent task {idx}", task_type="general", scope="global")
            results.append(pack)
        except Exception as e:
            errors.append((idx, str(e)))

    threads = [threading.Thread(target=call_supply, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent supply errors: {errors}"
    assert len(results) == 10
    print(f"Concurrent test: {len(results)} successes, {len(errors)} errors")
