"""Run a read-only batch comparison of legacy and structure-aware chunking."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plastic_promise.core.chunking import shadow_chunking_diagnostics  # noqa: E402
from plastic_promise.core.paths import get_db_path  # noqa: E402


def run_benchmark(
    records: Iterable[tuple[str, str, str]],
    *,
    target_chars: int = 512,
    hard_chars: int = 1024,
    max_chunks: int = 8,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return aggregate and per-record diagnostics without retaining source text."""

    reports: list[dict[str, Any]] = []
    latencies: list[float] = []
    kind_counts: Counter[str] = Counter()
    source_chars = 0
    empty_count = 0
    legacy_chunks = 0
    candidate_chunks = 0
    legacy_covered = 0
    candidate_covered = 0
    legacy_dropped_tail = 0
    candidate_dropped_tail = 0
    legacy_truncated = 0
    candidate_truncated = 0

    for record_id, text, text_field in records:
        value = text or ""
        started = time.perf_counter()
        diagnostics = shadow_chunking_diagnostics(
            value,
            target_chars=target_chars,
            hard_chars=hard_chars,
            max_chunks=max_chunks,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        legacy = diagnostics["legacy"]
        candidate = diagnostics["candidate"]
        assert isinstance(legacy, dict)
        assert isinstance(candidate, dict)
        kinds = candidate.get("kinds", [])
        kind_counts.update(str(kind) for kind in kinds)
        source_chars += len(value)
        empty_count += int(not value.strip())
        legacy_chunks += int(legacy["chunk_count"])
        candidate_chunks += int(candidate["chunk_count"])
        legacy_covered += int(legacy["covered_source_chars"])
        candidate_covered += int(candidate["covered_source_chars"])
        legacy_dropped_tail += max(len(value) - int(legacy["covered_source_chars"]), 0)
        candidate_dropped_tail += max(
            len(value.rstrip()) - int(candidate["last_source_end"]),
            0,
        )
        legacy_truncated += int(bool(legacy["truncated"]))
        candidate_truncated += int(bool(candidate["truncated"]))
        latencies.append(latency_ms)
        reports.append(
            {
                "id": str(record_id),
                "text_field": text_field,
                "source_chars": len(value),
                "planning_ms": round(latency_ms, 6),
                "legacy": legacy,
                "candidate": candidate,
            }
        )

    count = len(reports)
    return {
        "benchmark": "rag_chunking_shadow",
        "mode": "read-only-diagnostics",
        "configuration": {
            "target_chars": int(target_chars),
            "hard_chars": int(hard_chars),
            "max_chunks": int(max_chunks),
            "budget_unit": "characters-fallback",
        },
        "source": source or {},
        "summary": {
            "record_count": count,
            "empty_count": empty_count,
            "source_chars": source_chars,
            "legacy_chunks": legacy_chunks,
            "candidate_chunks": candidate_chunks,
            "candidate_to_legacy_chunk_ratio": round(
                candidate_chunks / max(legacy_chunks, 1), 6
            ),
            "legacy_covered_chars": legacy_covered,
            "candidate_covered_chars": candidate_covered,
            "legacy_dropped_tail_chars": legacy_dropped_tail,
            "candidate_dropped_tail_chars": candidate_dropped_tail,
            "legacy_truncated_count": legacy_truncated,
            "legacy_truncated_rate": _rate(legacy_truncated, count),
            "candidate_truncated_count": candidate_truncated,
            "candidate_truncated_rate": _rate(candidate_truncated, count),
            "planning_p50_ms": _percentile(latencies, 50),
            "planning_p95_ms": _percentile(latencies, 95),
            "planning_p99_ms": _percentile(latencies, 99),
            "candidate_kinds": dict(sorted(kind_counts.items())),
        },
        "records": reports,
    }


def load_records(path: Path, source_type: str = "auto") -> tuple[Iterator[tuple[str, str, str]], dict[str, Any]]:
    """Load records from a corpus file or a SQLite database in read-only mode."""

    resolved = path.resolve()
    selected = _detect_source_type(resolved, source_type)
    if selected == "sqlite":
        return _load_sqlite_records(resolved)
    return _load_json_records(resolved, selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(get_db_path()),
        help="read-only SQLite database, JSON corpus, or JSONL file",
    )
    parser.add_argument("--source-type", choices=("auto", "sqlite", "json", "jsonl"), default="auto")
    parser.add_argument("--limit", type=int, default=0, help="maximum records; 0 means all")
    parser.add_argument("--target-chars", type=int, default=512)
    parser.add_argument("--hard-chars", type=int, default=1024)
    parser.add_argument("--max-chunks", type=int, default=8)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)

    records, source = load_records(args.source, args.source_type)
    if args.limit > 0:
        records = _take(records, args.limit)
        source["limit"] = args.limit
    report = run_benchmark(
        records,
        target_chars=args.target_chars,
        hard_chars=args.hard_chars,
        max_chunks=args.max_chunks,
        source=source,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


def _load_sqlite_records(path: Path) -> tuple[Iterator[tuple[str, str, str]], dict[str, Any]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
    if "id" not in columns or "content" not in columns:
        connection.close()
        raise ValueError("SQLite source must contain memories.id and memories.content")
    embedding_column = "embedding_text" if "embedding_text" in columns else "''"
    query = f"SELECT id, {embedding_column}, content FROM memories ORDER BY rowid"

    def iterator() -> Iterator[tuple[str, str, str]]:
        try:
            for record_id, embedding_text, content in connection.execute(query):
                if str(embedding_text or "").strip():
                    yield str(record_id), str(embedding_text), "embedding_text"
                else:
                    yield str(record_id), str(content or ""), "content"
        finally:
            connection.close()

    return iterator(), {
        "type": "sqlite",
        "path": str(path),
        "read_only": True,
        "embedding_field_preference": "embedding_text -> content",
    }


def _load_json_records(path: Path, source_type: str) -> tuple[Iterator[tuple[str, str, str]], dict[str, Any]]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if source_type == "jsonl":
        objects = (json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip())
    else:
        payload = json.loads(raw.decode("utf-8"))
        objects = _json_objects(payload)

    def iterator() -> Iterator[tuple[str, str, str]]:
        for index, item in enumerate(objects):
            if not isinstance(item, dict):
                raise ValueError(f"corpus record {index} is not an object")
            record_id, text, field = _record_text(item, index)
            yield record_id, text, field

    return iterator(), {
        "type": source_type,
        "path": str(path),
        "sha256": digest,
        "read_only": True,
    }


def _json_objects(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("corpus", "records", "memories", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    raise ValueError("JSON source must be an object or array")


def _record_text(item: dict[str, Any], index: int) -> tuple[str, str, str]:
    record_id = str(item.get("memory_id") or item.get("id") or item.get("key") or f"record-{index}")
    for field in ("embedding_text", "content", "text", "raw_content"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return record_id, value, field
    return record_id, "", "empty"


def _detect_source_type(path: Path, source_type: str) -> str:
    if source_type != "auto":
        return source_type
    suffix = path.suffix.casefold()
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    if suffix in {".json", ".json5"}:
        return "json"
    return "sqlite"


def _take(records: Iterable[tuple[str, str, str]], limit: int) -> Iterator[tuple[str, str, str]]:
    for index, record in enumerate(records):
        if index >= limit:
            break
        yield record


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((percent / 100.0) * len(ordered) + 0.999999) - 1))
    return round(ordered[index], 6)


if __name__ == "__main__":
    raise SystemExit(main())
