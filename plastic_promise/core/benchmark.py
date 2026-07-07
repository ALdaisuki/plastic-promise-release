"""Performance benchmark persistence for release readiness checks."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from plastic_promise.core.paths import get_db_path

DEFAULT_RETRIEVAL_QUERIES = (
    "memory recall",
    "project context",
    "release readiness",
)


def ensure_benchmark_schema(conn: sqlite3.Connection) -> None:
    """Create benchmark tables used by system(action=benchmark)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            benchmark_name TEXT NOT NULL,
            query TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            memory_count INTEGER NOT NULL DEFAULT 0,
            lancedb_rows INTEGER NOT NULL DEFAULT 0,
            pipeline_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_benchmark_runs_name_created
        ON benchmark_runs (benchmark_name, created_at, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_baselines (
            benchmark_name TEXT NOT NULL,
            baseline_name TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            p50_ms REAL,
            p95_ms REAL,
            p99_ms REAL,
            tolerance_ratio REAL NOT NULL DEFAULT 0.20,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (benchmark_name, baseline_name)
        )
        """
    )
    conn.commit()


def record_benchmark_sample(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    benchmark_name: str,
    query: str,
    latency_ms: float,
    memory_count: int,
    lancedb_rows: int,
    pipeline_stats: dict[str, Any] | None = None,
) -> None:
    """Persist one benchmark sample."""
    ensure_benchmark_schema(conn)
    conn.execute(
        """
        INSERT INTO benchmark_runs (
            run_id,
            benchmark_name,
            query,
            latency_ms,
            memory_count,
            lancedb_rows,
            pipeline_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            benchmark_name,
            query,
            round(float(latency_ms), 6),
            int(memory_count or 0),
            int(lancedb_rows or 0),
            json.dumps(pipeline_stats or {}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()


def summarize_benchmark_history(
    conn: sqlite3.Connection,
    *,
    benchmark_name: str = "retrieval",
    limit: int = 50,
) -> dict[str, Any]:
    """Return nearest-rank latency percentiles over recent benchmark samples."""
    ensure_benchmark_schema(conn)
    row_limit = max(1, int(limit or 50))
    rows = conn.execute(
        """
        SELECT
            run_id,
            benchmark_name,
            query,
            latency_ms,
            memory_count,
            lancedb_rows,
            pipeline_json,
            created_at
        FROM benchmark_runs
        WHERE benchmark_name = ?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (benchmark_name, row_limit),
    ).fetchall()

    latencies = sorted(float(row[3]) for row in rows)

    def percentile(percent: float) -> float | None:
        if not latencies:
            return None
        index = max(0, min(len(latencies) - 1, math.ceil((percent / 100.0) * len(latencies)) - 1))
        return latencies[index]

    latest = None
    if rows:
        latest_row = rows[0]
        try:
            pipeline_stats = json.loads(latest_row[6] or "{}")
        except json.JSONDecodeError:
            pipeline_stats = {}
        latest = {
            "run_id": latest_row[0],
            "benchmark_name": latest_row[1],
            "query": latest_row[2],
            "latency_ms": float(latest_row[3]),
            "memory_count": int(latest_row[4] or 0),
            "lancedb_rows": int(latest_row[5] or 0),
            "pipeline_stats": pipeline_stats,
            "created_at": latest_row[7],
        }

    return {
        "benchmark_name": benchmark_name,
        "sample_count": len(rows),
        "p50_ms": percentile(50),
        "p95_ms": percentile(95),
        "p99_ms": percentile(99),
        "latest": latest,
    }


def save_benchmark_baseline(
    conn: sqlite3.Connection,
    *,
    benchmark_name: str,
    baseline_name: str = "default",
    summary: dict[str, Any],
    tolerance_ratio: float = 0.20,
) -> dict[str, Any]:
    """Persist a named baseline from an existing benchmark summary."""
    ensure_benchmark_schema(conn)
    sample_count = int(summary.get("sample_count") or 0)
    if sample_count < 1:
        raise ValueError("cannot create benchmark baseline without samples")

    record = {
        "benchmark_name": benchmark_name,
        "baseline_name": baseline_name,
        "sample_count": sample_count,
        "p50_ms": _optional_float(summary.get("p50_ms")),
        "p95_ms": _optional_float(summary.get("p95_ms")),
        "p99_ms": _optional_float(summary.get("p99_ms")),
        "tolerance_ratio": float(tolerance_ratio),
    }

    conn.execute(
        """
        INSERT INTO benchmark_baselines (
            benchmark_name,
            baseline_name,
            sample_count,
            p50_ms,
            p95_ms,
            p99_ms,
            tolerance_ratio
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(benchmark_name, baseline_name) DO UPDATE SET
            sample_count = excluded.sample_count,
            p50_ms = excluded.p50_ms,
            p95_ms = excluded.p95_ms,
            p99_ms = excluded.p99_ms,
            tolerance_ratio = excluded.tolerance_ratio,
            updated_at = datetime('now')
        """,
        (
            record["benchmark_name"],
            record["baseline_name"],
            record["sample_count"],
            record["p50_ms"],
            record["p95_ms"],
            record["p99_ms"],
            record["tolerance_ratio"],
        ),
    )
    conn.commit()
    return load_benchmark_baseline(
        conn, benchmark_name=benchmark_name, baseline_name=baseline_name
    ) or record


def load_benchmark_baseline(
    conn: sqlite3.Connection,
    *,
    benchmark_name: str,
    baseline_name: str = "default",
) -> dict[str, Any] | None:
    """Load a named benchmark baseline, if present."""
    ensure_benchmark_schema(conn)
    row = conn.execute(
        """
        SELECT
            benchmark_name,
            baseline_name,
            sample_count,
            p50_ms,
            p95_ms,
            p99_ms,
            tolerance_ratio,
            created_at,
            updated_at
        FROM benchmark_baselines
        WHERE benchmark_name = ? AND baseline_name = ?
        """,
        (benchmark_name, baseline_name),
    ).fetchone()
    if row is None:
        return None
    return {
        "benchmark_name": row[0],
        "baseline_name": row[1],
        "sample_count": int(row[2] or 0),
        "p50_ms": _optional_float(row[3]),
        "p95_ms": _optional_float(row[4]),
        "p99_ms": _optional_float(row[5]),
        "tolerance_ratio": float(row[6] if row[6] is not None else 0.20),
        "created_at": row[7],
        "updated_at": row[8],
    }


def evaluate_benchmark_gate(
    summary: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    tolerance_ratio: float | None = None,
    max_p50_ms: float | None = None,
    max_p95_ms: float | None = None,
    max_p99_ms: float | None = None,
) -> dict[str, Any]:
    """Evaluate current benchmark summary against baseline and absolute limits."""
    if int(summary.get("sample_count") or 0) < 1:
        return {"status": "insufficient_samples", "checks": [], "regressions": []}

    checks: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    absolute_limits = {
        "p50_ms": max_p50_ms,
        "p95_ms": max_p95_ms,
        "p99_ms": max_p99_ms,
    }

    for metric in ("p50_ms", "p95_ms", "p99_ms"):
        current = _optional_float(summary.get(metric))
        if current is None:
            continue

        if baseline is not None:
            baseline_value = _optional_float(baseline.get(metric))
            if baseline_value is not None:
                ratio = float(
                    tolerance_ratio
                    if tolerance_ratio is not None
                    else baseline.get("tolerance_ratio", 0.20)
                )
                checks.append(
                    _benchmark_check(
                        metric=metric,
                        current=current,
                        limit=baseline_value * (1.0 + ratio),
                        source="baseline",
                        baseline_ms=baseline_value,
                    )
                )

        absolute_limit = _optional_float(absolute_limits[metric])
        if absolute_limit is not None:
            checks.append(
                _benchmark_check(
                    metric=metric,
                    current=current,
                    limit=absolute_limit,
                    source="absolute",
                    baseline_ms=None,
                )
            )

    for check in checks:
        if check["status"] == "fail":
            regressions.append(check)

    if regressions:
        status = "fail"
    elif checks:
        status = "pass"
    else:
        status = "missing_baseline"

    return {
        "status": status,
        "baseline_name": baseline.get("baseline_name") if baseline else None,
        "checks": checks,
        "regressions": regressions,
    }


def run_retrieval_benchmark(
    engine: Any,
    *,
    queries: Iterable[str] | None = None,
    repeat: int = 1,
    conn: sqlite3.Connection | None = None,
    timer: Callable[[], float] = time.perf_counter,
    benchmark_name: str = "retrieval",
) -> dict[str, Any]:
    """Run context retrieval probes and persist latency samples."""
    owned_conn = conn is None
    if owned_conn:
        db_path = Path(get_db_path())
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)

    assert conn is not None
    ensure_benchmark_schema(conn)

    run_id = f"bench:{uuid.uuid4().hex}"
    if isinstance(queries, str):
        raw_queries: Iterable[str] = (queries,)
    else:
        raw_queries = queries or DEFAULT_RETRIEVAL_QUERIES
    query_list = [query for query in raw_queries if str(query).strip()]
    if not query_list:
        query_list = list(DEFAULT_RETRIEVAL_QUERIES)

    try:
        for _ in range(max(1, int(repeat or 1))):
            for query in query_list:
                started = timer()
                pack = engine.supply(
                    str(query),
                    task_vector=None,
                    task_type="general",
                    scope="global",
                    debug=True,
                )
                latency_ms = (timer() - started) * 1000.0
                record_benchmark_sample(
                    conn,
                    run_id=run_id,
                    benchmark_name=benchmark_name,
                    query=str(query),
                    latency_ms=latency_ms,
                    memory_count=_memory_count(engine, pack),
                    lancedb_rows=_lancedb_rows(engine),
                    pipeline_stats=_pipeline_stats(pack),
                )

        return {
            "mode": "run",
            "benchmark_name": benchmark_name,
            "run_id": run_id,
            "summary": summarize_benchmark_history(conn, benchmark_name=benchmark_name),
        }
    finally:
        if owned_conn:
            conn.close()


def _pipeline_stats(pack: Any) -> dict[str, Any]:
    if isinstance(pack, dict):
        stats = pack.get("pipeline_stats", {})
    else:
        stats = getattr(pack, "pipeline_stats", {})
    return stats if isinstance(stats, dict) else {"value": stats}


def _memory_count(engine: Any, pack: Any) -> int:
    count = getattr(engine, "memory_count", None)
    if callable(count):
        count = count()
    if count is not None:
        try:
            return int(count)
        except (TypeError, ValueError):
            pass
    return sum(len(getattr(pack, layer, []) or []) for layer in ("core", "related", "divergent"))


def _lancedb_rows(engine: Any) -> int:
    store = getattr(engine, "_ldb", None)
    count_rows = getattr(store, "count_rows", None)
    if not callable(count_rows):
        return 0
    try:
        return int(count_rows())
    except Exception:
        return 0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _benchmark_check(
    *,
    metric: str,
    current: float,
    limit: float,
    source: str,
    baseline_ms: float | None,
) -> dict[str, Any]:
    limit_ms = round(float(limit), 6)
    current_ms = round(float(current), 6)
    return {
        "metric": metric,
        "source": source,
        "current_ms": current_ms,
        "baseline_ms": round(float(baseline_ms), 6) if baseline_ms is not None else None,
        "limit_ms": limit_ms,
        "status": "pass" if current_ms <= limit_ms else "fail",
    }
