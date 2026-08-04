"""Fail-closed reconciliation for immutable LanceDB generation snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

INDEX_OUTBOX_TOOLS = ("memory_index", "synthesis_index")
SOURCE_FINGERPRINT_SCHEMA = "canonical-source/v1"
IMMUTABLE_COLUMNS = (
    "outbox_id",
    "tool_name",
    "project_id",
    "call_id",
    "payload_json",
    "metadata_json",
    "created_at",
)
_SHA256 = __import__("re").compile(r"[0-9a-f]{64}\Z")
_RECONCILIATION_RECEIPT_FIELDS = frozenset(
    {
        "generation_id",
        "manifest_hash",
        "watermark",
        "immutable_digest",
        "job_count",
        "marked_done_count",
        "reconciled_at",
    }
)

# These are the canonical fields that determine the material and eligibility
# of a derived index. Operational counters are deliberately excluded because
# retrieval updates them. The persisted embedding/search material is included
# because SQLite is its source of truth; only the derived ``memory_index``
# metadata is ignored when an offline rebuild refreshes its private clone.
_SOURCE_MEMORY_FIELDS: tuple[tuple[str, object], ...] = (
    ("id", ""),
    ("content", ""),
    ("memory_type", "experience"),
    ("source", "user"),
    ("owner", ""),
    ("tier", "L1"),
    ("scope", "global"),
    ("category", "other"),
    ("tags", []),
    ("domain", "uncategorized"),
    ("importance", 0.7),
    ("entity_ids", []),
    ("created_at", ""),
    ("project_id", "project:legacy-global"),
    ("visibility", "project"),
    ("source_class", "experience"),
    ("created_by_call_id", ""),
    ("origin_kind", ""),
    ("origin_uri", ""),
    ("origin_ref", ""),
    ("origin_hash", ""),
    ("parent_memory_ids", []),
    ("metadata_json", {}),
    ("raw_content", ""),
    ("l0_abstract", ""),
    ("l1_summary", ""),
    ("l2_content", ""),
    ("embedding_text", ""),
    ("embedding_hash", ""),
    ("search_text", ""),
)
_SOURCE_SYNTHESIS_FIELDS: tuple[tuple[str, object], ...] = (
    ("memory_id", ""),
    ("synthesis_key", ""),
    ("status", ""),
    ("revision", 1),
    ("support_count", 0),
    ("validity_scope", ""),
    ("source_fingerprint", ""),
    ("last_verified_at", ""),
    ("last_linted_at", ""),
    ("stale_reason", ""),
    ("created_by_call_id", ""),
    ("verified_by_actor", ""),
    ("verified_by_call_id", ""),
    ("metadata_json", {}),
)
_SOURCE_EDGE_FIELDS: tuple[tuple[str, object], ...] = (
    ("id", ""),
    ("source", ""),
    ("target", ""),
    ("relation", ""),
    ("weight", 0.5),
    ("source_kind", ""),
    ("evidence_id", ""),
    ("metadata_json", {}),
    ("schema_version", "behavior-graph/v1"),
)
_SOURCE_JSON_FIELDS = frozenset(
    {
        "tags",
        "entity_ids",
        "parent_memory_ids",
        "metadata_json",
    }
)
_SOURCE_TABLES = (
    ("memories", _SOURCE_MEMORY_FIELDS, ("id",)),
    ("synthesis_artifacts", _SOURCE_SYNTHESIS_FIELDS, ("memory_id",)),
    ("behavior_graph_edges", _SOURCE_EDGE_FIELDS, ("source", "target", "relation", "id")),
)


class IndexOutboxReconciliationError(RuntimeError):
    """The SQLite state cannot be proven covered by a generation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _source_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("canonical_source_schema_unreadable") from exc


def _source_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    try:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("canonical_source_schema_unreadable") from exc


def _source_json_value(
    value: object,
    default: object,
    *,
    strip_memory_index: bool = False,
) -> object:
    if value is None:
        value = default
    if not isinstance(value, str):
        parsed = value
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            # Preserve malformed source bytes deterministically. The normal
            # index eligibility gate will reject them; the fingerprint must
            # still be stable enough to bind the observed source state.
            return {"__invalid_json__": value}
    if isinstance(parsed, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(parsed).hex()}
    if not isinstance(parsed, (dict, list, str, int, float, bool)) and parsed is not None:
        return {"__repr__": repr(parsed)}
    if strip_memory_index and isinstance(parsed, dict) and "memory_index" in parsed:
        # This key is derived by LanceDBStore.rebuild_all on the private clone.
        parsed = dict(parsed)
        parsed.pop("memory_index", None)
    return parsed


def _source_value(
    field: str,
    value: object,
    default: object,
    *,
    strip_memory_index: bool = False,
) -> object:
    if field in _SOURCE_JSON_FIELDS:
        return _source_json_value(
            value,
            default,
            strip_memory_index=strip_memory_index and field == "metadata_json",
        )
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": bytes(value).hex()}
    return value


def _source_table_rows(
    connection: sqlite3.Connection,
    table: str,
    fields: tuple[tuple[str, object], ...],
    order_fields: tuple[str, ...],
) -> tuple[bool, list[dict[str, object]]]:
    if not _source_table_exists(connection, table):
        return False, []
    available = _source_table_columns(connection, table)
    selected_fields = [field for field, _default in fields if field in available]
    if not selected_fields:
        return True, []
    select_parts = [f'"{field}"' for field in selected_fields]
    # ``order_fields`` used to be interpolated into SQL ORDER BY.  Those
    # columns are not guaranteed to be unique in legacy schemas, so tied rows
    # inherited SQLite rowid order and two logically identical databases could
    # produce different fingerprints.  Fetch and sort canonicalized records
    # below instead; retain the argument for the table descriptor's stable API.
    del order_fields
    try:
        rows = connection.execute(
            f'SELECT {", ".join(select_parts)} FROM "{table}"',
        ).fetchall()
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("canonical_source_rows_unreadable") from exc
    records: list[dict[str, object]] = []
    for row in rows:
        raw = dict(zip(selected_fields, row, strict=True))
        records.append(
            {
                field: _source_value(
                    field,
                    raw.get(field),
                    default,
                    strip_memory_index=table == "memories",
                )
                for field, default in fields
            }
        )
    try:
        records.sort(
            key=lambda record: json.dumps(
                record,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise IndexOutboxReconciliationError("canonical_source_value_invalid") from exc
    return True, records


def _source_digest_update(digest: Any, payload: object) -> None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise IndexOutboxReconciliationError("canonical_source_value_invalid") from exc
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def canonical_source_fingerprint(connection: sqlite3.Connection) -> str:
    """Return a stable logical fingerprint for the index's canonical source.

    The SQLite file bytes are deliberately not used.  Reconciliation changes
    outbox delivery state and SQLite may move bytes between the main file and
    WAL, while neither operation changes the source rows represented here.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise IndexOutboxReconciliationError("canonical_source_database_required")
    digest = hashlib.sha256()
    _source_digest_update(digest, {"schema": SOURCE_FINGERPRINT_SCHEMA})
    for table, fields, order_fields in _SOURCE_TABLES:
        present, rows = _source_table_rows(connection, table, fields, order_fields)
        if table == "memories" and not present:
            raise IndexOutboxReconciliationError("canonical_source_table_absent")
        if table != "memories" and not present:
            # Clone-only startup migrations create these optional tables before
            # the runtime freshness check. Their absence is equivalent to an
            # empty table for the logical source binding.
            present = True
        _source_digest_update(
            digest,
            {
                "table": table,
                "present": present,
                "fields": [field for field, _default in fields],
                "rows": rows,
            },
        )
    return digest.hexdigest()


def _table_columns(connection: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute("PRAGMA table_info(store_outbox)")}
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("store_outbox_schema_unreadable") from exc


def _require_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_outbox'"
    ).fetchone()
    if table is None:
        raise IndexOutboxReconciliationError("store_outbox_table_absent")
    columns = _table_columns(connection)
    if not set(IMMUTABLE_COLUMNS).issubset(columns):
        raise IndexOutboxReconciliationError("store_outbox_immutable_columns_unavailable")


def _rows(
    connection: sqlite3.Connection,
    *,
    watermark: int | None = None,
) -> list[tuple[Any, ...]]:
    placeholders = ",".join("?" for _ in INDEX_OUTBOX_TOOLS)
    suffix = ""
    params: list[Any] = [*INDEX_OUTBOX_TOOLS]
    if watermark is not None:
        suffix = " AND rowid <= ?"
        params.append(watermark)
    query = (
        "SELECT rowid, status, "
        + ", ".join(IMMUTABLE_COLUMNS)
        + " FROM store_outbox WHERE tool_name IN ("
        + placeholders
        + ")"
        + suffix
        + " ORDER BY rowid"
    )
    try:
        return list(connection.execute(query, tuple(params)).fetchall())
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("store_outbox_rows_unreadable") from exc


def immutable_digest(rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            [row[0], *row[2:]],
            ensure_ascii=True,
            sort_keys=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def snapshot_index_outbox(connection: sqlite3.Connection) -> dict[str, Any]:
    """Capture a rowid watermark and a digest of immutable index-job fields."""

    _require_schema(connection)
    source_fingerprint = None
    # Minimal/legacy fixture databases may not have the canonical memories
    # table yet. They retain the pre-fingerprint evidence shape; real
    # generation builds always have memories and therefore carry the binding.
    if _source_table_exists(connection, "memories"):
        source_fingerprint = canonical_source_fingerprint(connection)
    rows = _rows(connection)
    watermark = max((int(row[0]) for row in rows), default=0)
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row[1])
        status_counts[status] = status_counts.get(status, 0) + 1
    evidence = {
        "status": "snapshot",
        "reason": "awaiting_explicit_index_outbox_reconciliation",
        "watermark": watermark,
        "immutable_digest": immutable_digest(rows),
        "job_count": len(rows),
        "snapshot_jobs": len(rows),
        "active_snapshot_jobs": sum(
            count for status, count in status_counts.items() if status not in {"done", "failed"}
        ),
        "status_counts": status_counts,
        "reconciled": False,
        "required_action": "run explicit reconcile after reviewing the source watermark",
    }
    if source_fingerprint is not None:
        evidence["source_fingerprint"] = source_fingerprint
    return evidence


def _validate_evidence(evidence: Mapping[str, Any]) -> tuple[int, str, int]:
    if not isinstance(evidence, Mapping):
        raise IndexOutboxReconciliationError("generation_outbox_evidence_invalid")
    try:
        watermark = evidence["watermark"]
        digest = evidence["immutable_digest"]
        job_count = evidence["job_count"]
    except KeyError as exc:
        raise IndexOutboxReconciliationError("generation_outbox_evidence_missing") from exc
    if (
        isinstance(watermark, bool)
        or not isinstance(watermark, int)
        or watermark < 0
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or isinstance(job_count, bool)
        or not isinstance(job_count, int)
        or job_count < 0
    ):
        raise IndexOutboxReconciliationError("generation_outbox_evidence_invalid")
    source_fingerprint = evidence.get("source_fingerprint")
    if source_fingerprint is not None and (
        not isinstance(source_fingerprint, str) or _SHA256.fullmatch(source_fingerprint) is None
    ):
        raise IndexOutboxReconciliationError("generation_outbox_evidence_invalid")
    return watermark, digest, job_count


def validate_reconciliation_receipt(
    receipt: Mapping[str, Any],
    *,
    generation_id: str,
    manifest_hash: str,
    evidence: Mapping[str, Any],
) -> None:
    """Validate the complete receipt binding before a manifest is resealed."""

    if not isinstance(receipt, Mapping) or set(receipt) != _RECONCILIATION_RECEIPT_FIELDS:
        raise IndexOutboxReconciliationError("reconciliation_receipt_invalid")
    if receipt.get("generation_id") != generation_id:
        raise IndexOutboxReconciliationError("reconciliation_receipt_generation_mismatch")
    if receipt.get("manifest_hash") != manifest_hash:
        raise IndexOutboxReconciliationError("reconciliation_receipt_manifest_mismatch")
    expected_watermark, expected_digest, expected_count = _validate_evidence(evidence)
    if (
        receipt.get("watermark") != expected_watermark
        or receipt.get("immutable_digest") != expected_digest
        or receipt.get("job_count") != expected_count
    ):
        raise IndexOutboxReconciliationError("reconciliation_receipt_evidence_mismatch")
    marked = receipt.get("marked_done_count")
    if (
        isinstance(marked, bool)
        or not isinstance(marked, int)
        or marked < 0
        or marked > expected_count
    ):
        raise IndexOutboxReconciliationError("reconciliation_receipt_count_invalid")
    timestamp = receipt.get("reconciled_at")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise IndexOutboxReconciliationError("reconciliation_receipt_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise IndexOutboxReconciliationError("reconciliation_receipt_timestamp_invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IndexOutboxReconciliationError("reconciliation_receipt_timestamp_invalid")


def assert_reconciliation_receipt_persisted(
    connection: sqlite3.Connection,
    receipt: Mapping[str, Any],
) -> None:
    """Require the exact receipt row committed by ``reconcile_index_outbox``."""

    if not isinstance(connection, sqlite3.Connection):
        raise IndexOutboxReconciliationError("reconciliation_database_required")
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(index_generation_reconciliation)")
        }
        required = {
            "generation_id",
            "manifest_hash",
            "watermark",
            "immutable_digest",
            "job_count",
            "marked_done_count",
            "reconciled_at",
        }
        if not required.issubset(columns):
            raise IndexOutboxReconciliationError("reconciliation_database_receipt_missing")
        row = connection.execute(
            "SELECT generation_id, manifest_hash, watermark, immutable_digest, job_count, "
            "marked_done_count, reconciled_at FROM index_generation_reconciliation "
            "WHERE generation_id = ?",
            (receipt.get("generation_id"),),
        ).fetchone()
    except IndexOutboxReconciliationError:
        raise
    except sqlite3.Error as exc:
        raise IndexOutboxReconciliationError("reconciliation_database_unreadable") from exc
    if row is None or tuple(row) != tuple(
        receipt.get(name)
        for name in (
            "generation_id",
            "manifest_hash",
            "watermark",
            "immutable_digest",
            "job_count",
            "marked_done_count",
            "reconciled_at",
        )
    ):
        raise IndexOutboxReconciliationError("reconciliation_database_receipt_mismatch")


def reconcile_index_outbox(
    connection: sqlite3.Connection,
    *,
    generation_id: str,
    manifest_hash: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically prove and consume the snapshot's index jobs.

    The digest excludes mutable delivery state, but every immutable row must
    still exist unchanged and no newer index job may exist. This makes a
    generation stale instead of silently acknowledging a concurrent write.
    """

    if (
        not generation_id
        or not isinstance(manifest_hash, str)
        or _SHA256.fullmatch(manifest_hash) is None
    ):
        raise IndexOutboxReconciliationError("reconciliation_identity_invalid")
    watermark, expected_digest, expected_count = _validate_evidence(evidence)
    _require_schema(connection)
    try:
        connection.execute("BEGIN IMMEDIATE")
        expected_source_fingerprint = evidence.get("source_fingerprint")
        if expected_source_fingerprint is not None:
            observed_source_fingerprint = canonical_source_fingerprint(connection)
            if observed_source_fingerprint != expected_source_fingerprint:
                raise IndexOutboxReconciliationError("generation_source_snapshot_mismatch")
        rows = _rows(connection, watermark=watermark)
        if len(rows) != expected_count or immutable_digest(rows) != expected_digest:
            raise IndexOutboxReconciliationError("generation_outbox_snapshot_mismatch")
        newer = _rows(connection)
        if any(int(row[0]) > watermark for row in newer):
            raise IndexOutboxReconciliationError("generation_outbox_newer_jobs_make_stale")
        statuses = {str(row[1]) for row in rows}
        if statuses - {"pending", "processing", "blocked", "failed", "done"}:
            raise IndexOutboxReconciliationError("generation_outbox_status_unknown")
        if "processing" in statuses:
            raise IndexOutboxReconciliationError("generation_outbox_processing_job_active")
        columns = _table_columns(connection)
        set_parts = ["status = 'done'"]
        if "error_class" in columns:
            set_parts.append("error_class = ''")
        if "error_message" in columns:
            set_parts.append("error_message = ''")
        if "next_attempt_at" in columns:
            set_parts.append("next_attempt_at = ''")
        if "updated_at" in columns:
            set_parts.append("updated_at = ?")
            update_params: list[Any] = [_utc_now(), watermark]
        else:
            update_params = [watermark]
        updated = connection.execute(
            "UPDATE store_outbox SET "
            + ", ".join(set_parts)
            + " WHERE tool_name IN (?, ?) AND rowid <= ? AND status <> 'done'",
            (*update_params[:-1], *INDEX_OUTBOX_TOOLS, update_params[-1]),
        ).rowcount
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_generation_reconciliation (
                generation_id TEXT PRIMARY KEY,
                manifest_hash TEXT NOT NULL,
                watermark INTEGER NOT NULL,
                immutable_digest TEXT NOT NULL,
                job_count INTEGER NOT NULL,
                marked_done_count INTEGER NOT NULL,
                reconciled_at TEXT NOT NULL
            )
            """
        )
        now = _utc_now()
        connection.execute(
            "INSERT INTO index_generation_reconciliation "
            "(generation_id, manifest_hash, watermark, immutable_digest, job_count, "
            "marked_done_count, reconciled_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(generation_id) DO UPDATE SET "
            "manifest_hash=excluded.manifest_hash, watermark=excluded.watermark, "
            "immutable_digest=excluded.immutable_digest, job_count=excluded.job_count, "
            "marked_done_count=excluded.marked_done_count, reconciled_at=excluded.reconciled_at",
            (
                generation_id,
                manifest_hash,
                watermark,
                expected_digest,
                expected_count,
                int(updated),
                now,
            ),
        )
        connection.commit()
    except IndexOutboxReconciliationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise IndexOutboxReconciliationError("index_outbox_reconciliation_failed") from exc
    return {
        "generation_id": generation_id,
        "manifest_hash": manifest_hash,
        "watermark": watermark,
        "immutable_digest": expected_digest,
        "job_count": expected_count,
        "marked_done_count": int(updated),
        "reconciled_at": now,
    }


def assert_index_outbox_fresh(
    connection: sqlite3.Connection,
    *,
    evidence: Mapping[str, Any],
) -> None:
    """Raise unless the reconciled generation still covers current SQLite."""

    if not isinstance(evidence, Mapping) or evidence.get("reconciled") is not True:
        raise IndexOutboxReconciliationError("generation_outbox_reconciliation_required")
    watermark, expected_digest, expected_count = _validate_evidence(evidence)
    _require_schema(connection)
    expected_source_fingerprint = evidence.get("source_fingerprint")
    if expected_source_fingerprint is not None:
        observed_source_fingerprint = canonical_source_fingerprint(connection)
        if observed_source_fingerprint != expected_source_fingerprint:
            raise IndexOutboxReconciliationError("generation_source_snapshot_stale")
    rows = _rows(connection, watermark=watermark)
    if any(str(row[1]) != "done" for row in rows):
        raise IndexOutboxReconciliationError("generation_outbox_status_not_done")
    if len(rows) != expected_count or immutable_digest(rows) != expected_digest:
        raise IndexOutboxReconciliationError("generation_outbox_snapshot_stale")
    if any(int(row[0]) > watermark for row in _rows(connection)):
        raise IndexOutboxReconciliationError("generation_outbox_newer_jobs_make_stale")


def assert_index_outbox_base_covered(
    connection: sqlite3.Connection,
    *,
    evidence: Mapping[str, Any],
) -> None:
    """Require the immutable base window while permitting newer live-view work."""

    if not isinstance(evidence, Mapping) or evidence.get("reconciled") is not True:
        raise IndexOutboxReconciliationError("generation_outbox_reconciliation_required")
    watermark, expected_digest, expected_count = _validate_evidence(evidence)
    _require_schema(connection)
    rows = _rows(connection, watermark=watermark)
    if any(str(row[1]) != "done" for row in rows):
        raise IndexOutboxReconciliationError("generation_outbox_base_status_not_done")
    if len(rows) != expected_count or immutable_digest(rows) != expected_digest:
        raise IndexOutboxReconciliationError("generation_outbox_base_snapshot_stale")
    receipt = evidence.get("receipt")
    if not isinstance(receipt, Mapping):
        raise IndexOutboxReconciliationError("reconciliation_database_receipt_missing")
    assert_reconciliation_receipt_persisted(connection, receipt)


__all__ = [
    "IMMUTABLE_COLUMNS",
    "INDEX_OUTBOX_TOOLS",
    "SOURCE_FINGERPRINT_SCHEMA",
    "IndexOutboxReconciliationError",
    "immutable_digest",
    "assert_reconciliation_receipt_persisted",
    "assert_index_outbox_base_covered",
    "assert_index_outbox_fresh",
    "canonical_source_fingerprint",
    "reconcile_index_outbox",
    "snapshot_index_outbox",
    "validate_reconciliation_receipt",
]
