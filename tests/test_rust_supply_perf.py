"""Rust vs Python supply() performance benchmarks."""

import os
import time
import statistics

os.environ["AGENT_USE_SQLITE"] = "0"


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
    return result


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
    from context_engine_core import ContextEngine as RustEngine

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
