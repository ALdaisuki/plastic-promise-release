import json
import sqlite3
from pathlib import Path

from scripts.benchmark_chunking_shadow import load_records, run_benchmark


def test_chunking_benchmark_reports_tail_loss_without_source_text():
    long_text = "# Topic\n\n" + ("First topic. " * 80) + "\n\nTAIL-EVIDENCE"
    report = run_benchmark(
        [
            ("short", "short text", "content"),
            ("long", long_text, "content"),
        ],
        target_chars=24,
        hard_chars=48,
        max_chunks=2,
        source={"type": "test"},
    )

    summary = report["summary"]
    assert summary["record_count"] == 2
    assert summary["legacy_truncated_count"] == 1
    assert summary["legacy_truncated_rate"] == 0.5
    assert summary["candidate_truncated_count"] == 0
    assert summary["candidate_dropped_tail_chars"] == 0
    assert summary["candidate_kinds"]["paragraph"] >= 2
    assert "TAIL-EVIDENCE" not in json.dumps(report, ensure_ascii=False)


def test_chunking_benchmark_loads_recall_quality_json_fixture():
    path = Path("tests/fixtures/recall_quality/v1.json")
    records, source = load_records(path, "json")

    report = run_benchmark(records, source=source)

    assert report["source"]["type"] == "json"
    assert len(report["source"]["sha256"]) == 64
    assert report["summary"]["record_count"] == 96
    assert report["summary"]["legacy_truncated_count"] == 0


def test_chunking_benchmark_reads_sqlite_embedding_text_read_only(tmp_path):
    path = tmp_path / "memories.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, embedding_text TEXT)")
    conn.execute(
        "INSERT INTO memories VALUES (?, ?, ?)",
        ("memory-1", "raw body", "# Heading\n\nIndexed summary"),
    )
    conn.commit()
    conn.close()

    records, source = load_records(path, "sqlite")
    report = run_benchmark(records, source=source)

    assert report["source"]["read_only"] is True
    assert report["summary"]["record_count"] == 1
    assert report["records"][0]["text_field"] == "embedding_text"
