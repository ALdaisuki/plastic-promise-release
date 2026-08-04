from __future__ import annotations

import sqlite3

import pytest

import plastic_promise.core.index_outbox_reconciliation as reconciliation_module
from plastic_promise.core.index_outbox_reconciliation import (
    IndexOutboxReconciliationError,
    assert_index_outbox_base_covered,
    assert_index_outbox_fresh,
    canonical_source_fingerprint,
    reconcile_index_outbox,
    snapshot_index_outbox,
)


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE store_outbox (
            outbox_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            next_attempt_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO store_outbox
        (outbox_id, tool_name, project_id, call_id, status, payload_json,
         metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("job-a", "memory_index", "p", "c-a", "pending", '{"memory_id":"a"}', "{}", "t1"),
            ("job-b", "synthesis_index", "p", "c-b", "failed", '{"memory_id":"b"}', "{}", "t2"),
            ("unrelated", "audit", "p", "c-c", "pending", "{}", "{}", "t3"),
        ],
    )
    connection.commit()
    return connection


def _canonical_db() -> sqlite3.Connection:
    connection = _db()
    connection.execute(
        "CREATE TABLE memories ("
        "id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT, "
        "embedding_text TEXT, embedding_hash TEXT, search_text TEXT"
        ")"
    )
    connection.execute(
        "INSERT INTO memories "
        "(id, content, metadata_json, embedding_text, embedding_hash, search_text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "memory-a",
            "source-a",
            '{"memory_index":{"derived":true},"state":"active"}',
            "canonical vector text",
            "canonical embedding hash",
            "canonical search text",
        ),
    )
    connection.commit()
    return connection


def _canonical_rows_db(*, reverse_insert_order: bool = False) -> sqlite3.Connection:
    """Build source tables with deliberately tied legacy edge sort keys."""

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)"
    )
    connection.execute("INSERT INTO memories VALUES ('memory-a', 'source-a', '{}')")
    connection.execute("CREATE TABLE synthesis_artifacts (memory_id TEXT, metadata_json TEXT)")
    connection.execute(
        "CREATE TABLE behavior_graph_edges ("
        "id TEXT, source TEXT, target TEXT, relation TEXT, metadata_json TEXT"
        ")"
    )
    synthesis_rows = [
        ("synthesis-a", '{"memory_index":{"rank":1},"state":"a"}'),
        ("synthesis-b", '{"memory_index":{"rank":2},"state":"b"}'),
    ]
    # These rows intentionally share every historical ORDER BY field. Their
    # metadata is canonical for these tables and must be part of the digest.
    edge_rows = [
        (
            "same-id",
            "source-a",
            "target-a",
            "supports",
            '{"memory_index":{"rank":1},"state":"a"}',
        ),
        (
            "same-id",
            "source-a",
            "target-a",
            "supports",
            '{"memory_index":{"rank":2},"state":"b"}',
        ),
    ]
    if reverse_insert_order:
        synthesis_rows.reverse()
        edge_rows.reverse()
    connection.executemany(
        "INSERT INTO synthesis_artifacts (memory_id, metadata_json) VALUES (?, ?)",
        synthesis_rows,
    )
    connection.executemany(
        "INSERT INTO behavior_graph_edges "
        "(id, source, target, relation, metadata_json) VALUES (?, ?, ?, ?, ?)",
        edge_rows,
    )
    connection.commit()
    return connection


def test_snapshot_contains_watermark_and_immutable_digest():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)

    assert snapshot["status"] == "snapshot"
    assert snapshot["watermark"] == 2
    assert snapshot["job_count"] == 2
    assert len(snapshot["immutable_digest"]) == 64
    assert snapshot["reconciled"] is False


def test_snapshot_binds_logical_source_and_ignores_derived_memory_metadata():
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)

    assert len(snapshot["source_fingerprint"]) == 64
    connection.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = ?",
        ('{"state":"active","memory_index":{"derived":false}}', "memory-a"),
    )
    connection.execute("UPDATE store_outbox SET status = 'done' WHERE outbox_id = 'job-a'")
    connection.commit()
    assert snapshot["source_fingerprint"] == canonical_source_fingerprint(connection)


def test_source_fingerprint_is_stable_when_legacy_sort_keys_tie():
    first = _canonical_rows_db()
    second = _canonical_rows_db(reverse_insert_order=True)

    assert canonical_source_fingerprint(first) == canonical_source_fingerprint(second)


def test_source_fingerprint_normalizes_json_recursion_error(monkeypatch):
    connection = _canonical_db()

    def raise_recursion(_value):
        raise RecursionError("nested JSON")

    monkeypatch.setattr(reconciliation_module.json, "loads", raise_recursion)

    fingerprint = canonical_source_fingerprint(connection)
    assert len(fingerprint) == 64


@pytest.mark.parametrize("table", ["synthesis_artifacts", "behavior_graph_edges"])
def test_source_fingerprint_tracks_non_memory_index_metadata(table):
    connection = _canonical_rows_db()
    before = canonical_source_fingerprint(connection)
    if table == "synthesis_artifacts":
        connection.execute(
            "UPDATE synthesis_artifacts SET metadata_json = ? WHERE memory_id = ?",
            ('{"memory_index":{"rank":99},"state":"a"}', "synthesis-a"),
        )
    else:
        connection.execute(
            "UPDATE behavior_graph_edges SET metadata_json = ? WHERE rowid = 1",
            ('{"memory_index":{"rank":99},"state":"a"}',),
        )
    connection.commit()

    assert canonical_source_fingerprint(connection) != before


def test_reconcile_rejects_persisted_index_material_drift():
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)
    connection.execute(
        "UPDATE memories SET embedding_text = 'changed-material', embedding_hash = 'changed-hash', "
        "search_text = 'changed-search' WHERE id = 'memory-a'"
    )
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="source_snapshot_mismatch"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=snapshot,
        )
    assert connection.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = 'job-a'"
    ).fetchone() == ("pending",)


def test_reconcile_rejects_different_canonical_source_before_consuming_jobs():
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)
    connection.execute("UPDATE memories SET content = 'source-b' WHERE id = 'memory-a'")
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="source_snapshot_mismatch"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=snapshot,
        )
    assert connection.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = 'job-a'"
    ).fetchone() == ("pending",)


def test_runtime_freshness_rejects_canonical_source_drift():
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)
    reconcile_index_outbox(
        connection,
        generation_id="generation-a",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )
    connection.execute("UPDATE memories SET content = 'source-b' WHERE id = 'memory-a'")
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="source_snapshot_stale"):
        assert_index_outbox_fresh(
            connection,
            evidence={**snapshot, "reconciled": True},
        )


def test_live_base_coverage_permits_source_drift_and_newer_jobs():
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)
    receipt = reconcile_index_outbox(
        connection,
        generation_id="generation-a",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )
    connection.execute("UPDATE memories SET content = 'source-b' WHERE id = 'memory-a'")
    connection.execute(
        "INSERT INTO store_outbox "
        "(outbox_id, tool_name, status, payload_json, metadata_json, created_at) "
        "VALUES ('job-new', 'memory_index', 'pending', '{}', '{}', 't4')"
    )
    connection.commit()

    assert_index_outbox_base_covered(
        connection,
        evidence={**snapshot, "reconciled": True, "receipt": receipt},
    )


def test_live_base_coverage_rejects_base_status_regression():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    receipt = reconcile_index_outbox(
        connection,
        generation_id="generation-a",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )
    connection.execute("UPDATE store_outbox SET status = 'pending' WHERE outbox_id = 'job-a'")
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="base_status_not_done"):
        assert_index_outbox_base_covered(
            connection,
            evidence={**snapshot, "reconciled": True, "receipt": receipt},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embedding_text", "changed vector text"),
        ("embedding_hash", "changed embedding hash"),
        ("search_text", "changed search text"),
    ],
)
def test_runtime_freshness_rejects_persisted_index_material_drift(field, value):
    connection = _canonical_db()
    snapshot = snapshot_index_outbox(connection)
    reconcile_index_outbox(
        connection,
        generation_id="generation-material",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )
    connection.execute(f'UPDATE memories SET "{field}" = ? WHERE id = ?', (value, "memory-a"))
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="source_snapshot_stale"):
        assert_index_outbox_fresh(
            connection,
            evidence={**snapshot, "reconciled": True},
        )


def test_reconcile_rejects_non_mapping_evidence_with_stable_error():
    connection = _db()

    with pytest.raises(IndexOutboxReconciliationError, match="evidence_invalid"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=[],
        )


def test_reconcile_marks_snapshot_jobs_done_and_records_receipt():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    receipt = reconcile_index_outbox(
        connection,
        generation_id="generation-a",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )

    assert receipt["marked_done_count"] == 2
    assert connection.execute(
        "SELECT status FROM store_outbox WHERE tool_name IN ('memory_index', 'synthesis_index') "
        "ORDER BY rowid"
    ).fetchall() == [("done",), ("done",)]
    assert connection.execute(
        "SELECT generation_id, watermark FROM index_generation_reconciliation"
    ).fetchone() == ("generation-a", 2)


def test_freshness_rejects_index_job_status_regression_after_reconciliation():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    reconcile_index_outbox(
        connection,
        generation_id="generation-a",
        manifest_hash="a" * 64,
        evidence=snapshot,
    )
    connection.execute("UPDATE store_outbox SET status = 'pending' WHERE outbox_id = 'job-a'")
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="status_not_done"):
        assert_index_outbox_fresh(
            connection,
            evidence={**snapshot, "reconciled": True},
        )


def test_new_job_after_snapshot_fails_closed_without_consuming_jobs():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    connection.execute(
        "INSERT INTO store_outbox "
        "(outbox_id, tool_name, status, payload_json, metadata_json, created_at) "
        "VALUES ('job-new', 'memory_index', 'pending', '{}', '{}', 't4')"
    )
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="newer_jobs_make_stale"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=snapshot,
        )
    assert connection.execute(
        "SELECT status FROM store_outbox WHERE outbox_id = 'job-a'"
    ).fetchone() == ("pending",)


def test_immutable_payload_change_fails_closed():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    connection.execute(
        'UPDATE store_outbox SET payload_json = \'{"memory_id":"changed"}\' '
        "WHERE outbox_id = 'job-a'"
    )
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="snapshot_mismatch"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=snapshot,
        )


def test_processing_job_requires_daemon_quiescence():
    connection = _db()
    snapshot = snapshot_index_outbox(connection)
    connection.execute("UPDATE store_outbox SET status = 'processing' WHERE outbox_id = 'job-a'")
    connection.commit()

    with pytest.raises(IndexOutboxReconciliationError, match="processing_job_active"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=snapshot,
        )


@pytest.mark.parametrize("evidence", [None, [], "invalid"])
def test_non_mapping_evidence_fails_closed(evidence):
    connection = _db()

    with pytest.raises(IndexOutboxReconciliationError, match="generation_outbox_evidence_invalid"):
        reconcile_index_outbox(
            connection,
            generation_id="generation-a",
            manifest_hash="a" * 64,
            evidence=evidence,
        )
