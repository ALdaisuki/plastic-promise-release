import asyncio
import json
import sqlite3
from types import SimpleNamespace


def test_benchmark_history_summarizes_latency_percentiles(tmp_path):
    from plastic_promise.core.benchmark import (
        ensure_benchmark_schema,
        record_benchmark_sample,
        summarize_benchmark_history,
    )

    conn = sqlite3.connect(tmp_path / "bench.db")
    ensure_benchmark_schema(conn)

    for latency in (10.0, 20.0, 30.0):
        record_benchmark_sample(
            conn,
            run_id="bench_run",
            benchmark_name="retrieval",
            query="diff review",
            latency_ms=latency,
            memory_count=7,
            lancedb_rows=5,
            pipeline_stats={"core_count": 2},
        )

    summary = summarize_benchmark_history(conn, benchmark_name="retrieval")

    assert summary["sample_count"] == 3
    assert summary["p50_ms"] == 20.0
    assert summary["p95_ms"] == 30.0
    assert summary["p99_ms"] == 30.0
    assert summary["latest"]["memory_count"] == 7
    assert summary["latest"]["lancedb_rows"] == 5
    conn.close()


def test_run_retrieval_benchmark_records_samples_with_pipeline_stats(tmp_path):
    from plastic_promise.core.benchmark import (
        ensure_benchmark_schema,
        run_retrieval_benchmark,
    )

    class FakeEngine:
        memory_count = 3
        _ldb = SimpleNamespace(count_rows=lambda: 2)

        def supply(self, task_description, task_vector=None, task_type="general", scope="global", **kwargs):
            return SimpleNamespace(pipeline_stats={"core_count": len(task_description)})

    ticks = iter([0.00, 0.01, 0.01, 0.03])
    conn = sqlite3.connect(tmp_path / "bench.db")
    ensure_benchmark_schema(conn)

    result = run_retrieval_benchmark(
        FakeEngine(),
        queries=["alpha", "beta"],
        repeat=1,
        conn=conn,
        timer=lambda: next(ticks),
    )

    assert result["mode"] == "run"
    assert result["summary"]["sample_count"] == 2
    assert result["summary"]["p50_ms"] == 10.0
    assert result["summary"]["p95_ms"] == 20.0
    rows = conn.execute("SELECT query, pipeline_json FROM benchmark_runs ORDER BY query").fetchall()
    assert [row[0] for row in rows] == ["alpha", "beta"]
    assert json.loads(rows[0][1]) == {"core_count": 5}
    conn.close()


def test_benchmark_baseline_comparison_flags_regression(tmp_path):
    from plastic_promise.core.benchmark import (
        ensure_benchmark_schema,
        evaluate_benchmark_gate,
        load_benchmark_baseline,
        save_benchmark_baseline,
    )

    conn = sqlite3.connect(tmp_path / "bench.db")
    ensure_benchmark_schema(conn)

    baseline = save_benchmark_baseline(
        conn,
        benchmark_name="retrieval",
        baseline_name="release",
        summary={
            "benchmark_name": "retrieval",
            "sample_count": 3,
            "p50_ms": 10.0,
            "p95_ms": 20.0,
            "p99_ms": 30.0,
        },
        tolerance_ratio=0.10,
    )

    loaded = load_benchmark_baseline(conn, benchmark_name="retrieval", baseline_name="release")
    assert loaded["baseline_name"] == "release"
    assert loaded["p95_ms"] == 20.0

    passing = evaluate_benchmark_gate(
        {"sample_count": 3, "p50_ms": 11.0, "p95_ms": 22.0, "p99_ms": 33.0},
        baseline=baseline,
    )
    assert passing["status"] == "pass"

    failing = evaluate_benchmark_gate(
        {"sample_count": 3, "p50_ms": 11.0, "p95_ms": 23.0, "p99_ms": 33.0},
        baseline=baseline,
    )
    assert failing["status"] == "fail"
    assert failing["regressions"][0]["metric"] == "p95_ms"
    assert failing["regressions"][0]["limit_ms"] == 22.0
    conn.close()


def test_benchmark_gate_uses_absolute_threshold_without_baseline():
    from plastic_promise.core.benchmark import evaluate_benchmark_gate

    gate = evaluate_benchmark_gate(
        {"sample_count": 1, "p50_ms": 8.0, "p95_ms": 55.0, "p99_ms": 60.0},
        max_p95_ms=50.0,
    )

    assert gate["status"] == "fail"
    assert gate["regressions"][0]["metric"] == "p95_ms"
    assert gate["regressions"][0]["limit_ms"] == 50.0


def test_system_benchmark_history_does_not_force_heavy_init(tmp_path, monkeypatch):
    from plastic_promise.mcp.tools.management import handle_system

    class NoHeavyEngine:
        def ensure_heavy_init(self):
            raise AssertionError("history mode should not force heavy init")

        def _ensure_heavy_init(self):
            raise AssertionError("history mode should not force heavy init")

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "bench.db"))

    result = asyncio.run(handle_system(NoHeavyEngine(), {"action": "benchmark", "run": False}))
    payload = json.loads(result[0].text)

    assert payload["tool"] == "system_benchmark"
    assert payload["mode"] == "history"
    assert payload["summary"]["sample_count"] == 0


def test_system_benchmark_run_records_history(tmp_path, monkeypatch):
    from plastic_promise.mcp.tools.management import handle_system

    class FakeEngine:
        memory_count = 1
        _ldb = SimpleNamespace(count_rows=lambda: 0)

        def supply(self, task_description, task_vector=None, task_type="general", scope="global", **kwargs):
            return SimpleNamespace(pipeline_stats={"query": task_description})

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "bench.db"))

    result = asyncio.run(
        handle_system(
            FakeEngine(),
            {"action": "benchmark", "run": True, "queries": ["release"], "repeat": 1},
        )
    )
    payload = json.loads(result[0].text)

    assert payload["tool"] == "system_benchmark"
    assert payload["mode"] == "run"
    assert payload["summary"]["sample_count"] == 1


def test_system_benchmark_can_set_baseline_and_gate(tmp_path, monkeypatch):
    from plastic_promise.core import benchmark as benchmark_core
    from plastic_promise.mcp.tools.management import handle_system

    class FakeEngine:
        pass

    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "bench.db"))

    def baseline_run(*args, **kwargs):
        return {
            "mode": "run",
            "benchmark_name": "retrieval",
            "run_id": "bench:baseline",
            "summary": {
                "benchmark_name": "retrieval",
                "sample_count": 3,
                "p50_ms": 10.0,
                "p95_ms": 20.0,
                "p99_ms": 30.0,
            },
        }

    monkeypatch.setattr(benchmark_core, "run_retrieval_benchmark", baseline_run)
    baseline_result = asyncio.run(
        handle_system(
            FakeEngine(),
            {
                "action": "benchmark",
                "run": True,
                "set_baseline": True,
                "baseline_name": "release",
                "tolerance_ratio": 0.10,
            },
        )
    )
    baseline_payload = json.loads(baseline_result[0].text)
    assert baseline_payload["baseline"]["baseline_name"] == "release"

    def regression_run(*args, **kwargs):
        return {
            "mode": "run",
            "benchmark_name": "retrieval",
            "run_id": "bench:regression",
            "summary": {
                "benchmark_name": "retrieval",
                "sample_count": 3,
                "p50_ms": 10.0,
                "p95_ms": 23.0,
                "p99_ms": 30.0,
            },
        }

    monkeypatch.setattr(benchmark_core, "run_retrieval_benchmark", regression_run)
    gate_result = asyncio.run(
        handle_system(
            FakeEngine(),
            {
                "action": "benchmark",
                "run": True,
                "gate": True,
                "baseline_name": "release",
            },
        )
    )
    gate_payload = json.loads(gate_result[0].text)

    assert gate_payload["gate"]["status"] == "fail"
    assert gate_payload["gate"]["regressions"][0]["metric"] == "p95_ms"
