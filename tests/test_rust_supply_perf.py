"""Rust vs Python supply() performance benchmarks."""

import os
import statistics
import sys
import time
from pathlib import Path

import pytest

slow_benchmark = pytest.mark.skipif(
    os.environ.get("PP_RUN_SLOW_BENCHMARKS") != "1",
    reason="Set PP_RUN_SLOW_BENCHMARKS=1 to run slow Python supply benchmarks.",
)


def _ensure_rust_extension_importable() -> None:
    """Allow local cargo release builds to satisfy `import context_engine_core`."""
    root = Path(__file__).resolve().parents[1]
    release_dir = root / "rust" / "context-engine-core" / "target" / "release"
    if release_dir.exists():
        sys.path.insert(0, str(release_dir))

    if sys.platform.startswith("win"):
        dll_path = release_dir / "context_engine_core.dll"
        pyd_path = release_dir / "context_engine_core.pyd"
        if dll_path.exists() and (
            not pyd_path.exists() or dll_path.stat().st_mtime > pyd_path.stat().st_mtime
        ):
            pyd_path.write_bytes(dll_path.read_bytes())


def _load_rust_engine():
    _ensure_rust_extension_importable()
    module = pytest.importorskip("context_engine_core")
    return module.ContextEngine


def benchmark_supply(engine, memory_count: int, iterations: int = 10) -> dict:
    """Measure supply() latency with N memories in pool."""
    from plastic_promise.core.context_engine import ContextEngine

    # Pre-load synthetic memories
    for i in range(memory_count):
        topics = [
            "code review",
            "architecture design",
            "testing strategy",
            "deployment pipeline",
            "performance optimization",
        ]
        engine.register_memory(
            {
                "id": f"perf_{i:04d}",
                "content": f"Performance test memory {i} about {topics[i % len(topics)]} "
                f"with additional context for realistic retrieval scenarios",
                "memory_type": "task" if i % 2 == 0 else "experience",
                "source": "benchmark",
            }
        )

    latencies = []
    for i in range(iterations):
        start = time.perf_counter()
        pack = engine.supply(
            f"performance optimization task iteration {i}",
            task_type="code_generation",
            scope="global",
        )
        elapsed = (time.perf_counter() - start) * 1000  # ms
        latencies.append(elapsed)

    latencies.sort()
    return {
        "count": memory_count,
        "iterations": iterations,
        "p50": statistics.median(latencies),
        "p95": latencies[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[-1],
        "p99": latencies[int(len(latencies) * 0.99)] if len(latencies) > 2 else latencies[-1],
        "min": latencies[0],
        "max": latencies[-1],
    }


@pytest.mark.slow
@slow_benchmark
def test_baseline_python_supply():
    """Record baseline Python supply() latency (no Rust)."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    # Force Python path by making Rust unavailable
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999  # don't re-probe

    result = benchmark_supply(engine, memory_count=100, iterations=5)
    print(
        f"Python baseline (100 memories): p50={result['p50']:.1f}ms, "
        f"p95={result['p95']:.1f}ms, p99={result['p99']:.1f}ms"
    )
    assert result["p50"] > 0


@pytest.mark.slow
@slow_benchmark
def test_benchmark_1000_memories():
    """Benchmark with 1000 memories — comparison point for Rust."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    # Force Python path for baseline
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999

    result = benchmark_supply(engine, memory_count=1000, iterations=10)
    print(
        f"Python 1000-memory supply(): p50={result['p50']:.1f}ms, "
        f"p95={result['p95']:.1f}ms, p99={result['p99']:.1f}ms"
    )
    assert result["p50"] > 0


@pytest.mark.slow
@slow_benchmark
def test_benchmark_empty_pool():
    """Benchmark supply() with empty memory pool."""
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999

    start = time.perf_counter()
    for _ in range(10):
        engine.supply("empty pool test", task_type="general", scope="global")
    elapsed = (time.perf_counter() - start) * 1000 / 10  # avg ms
    print(f"Empty pool supply() avg: {elapsed:.1f}ms")
    assert elapsed < 5000  # Python baseline: ~2s avg due to embedder loading


def test_pyo3_memory_pass_overhead():
    """Measure PyO3 Vec<PyObject> pass time for 1000 memories."""
    RustEngine = _load_rust_engine()

    memories = [
        {
            "id": f"m{i:04d}",
            "content": f"test memory {i} with some content",
            "source": "benchmark",
            "memory_type": "task",
            "worth_success": 1,
            "worth_failure": 0,
            "created_at": "2026-07-01T00:00:00",
            "last_accessed": "2026-07-01T00:00:00",
        }
        for i in range(1000)
    ]

    rust = RustEngine()
    rust.set_current_time("2026-07-02T00:00:00")

    # Measure PyO3 pass + Rust processing
    latencies = []
    for _ in range(5):
        start = time.perf_counter()
        pack = rust.supply("benchmark task", [0.5] * 768, "general", "global", memories)
        elapsed = (time.perf_counter() - start) * 1000
        latencies.append(elapsed)

    p50 = statistics.median(latencies)
    print(
        f"PyO3 pass 1000 memories: p50={p50:.1f}ms (over {len(memories)} items, "
        f"result total={pack.total_items})"
    )
    # Rust should process 1000 items well under 100ms
    assert p50 < 100, f"PyO3 pass too slow: {p50:.1f}ms"


def test_pyo3_supply_exposes_pipeline_metadata():
    """Actual PyO3 ContextPack exposes Rust debug counters to Python."""
    RustEngine = _load_rust_engine()

    memories = [
        {
            "id": f"meta_{i}",
            "content": f"Rust metadata smoke memory {i} about BM25 vector fusion MMR",
            "source": "benchmark",
            "memory_type": "experience",
        }
        for i in range(4)
    ]

    rust = RustEngine()
    pack = rust.supply(
        "Rust metadata smoke BM25 vector fusion MMR",
        [0.5] * 768,
        "code_generation",
        "global",
        memories,
    )

    assert pack.audit_metadata["engine_mode"] == "snapshot"
    assert pack.pipeline_stats["engine_mode"] == "snapshot"
    assert "mmr_demoted" in pack.pipeline_stats
    assert isinstance(pack.per_item_stats, list)
