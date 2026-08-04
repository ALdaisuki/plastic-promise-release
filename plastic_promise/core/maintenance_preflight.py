"""Read-only readiness projection for the maintenance daemon.

This module deliberately avoids every schema initializer and mutating scanner.
The canonical database is opened with SQLite ``mode=ro`` and ``query_only`` so
preflight cannot silently turn into a repair or migration path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from plastic_promise.core.decay_engine import WeibullDecayCalculator
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.core.synthesis_retrieval import (
    available_ordinary_memory_sql_predicate,
    ordinary_memory_sql_predicate,
)
from plastic_promise.mcp.tools.task_queue import _compute_payload_hash

if TYPE_CHECKING:
    from collections.abc import Mapping

_REQUIRED_TABLES = frozenset(
    {
        "behavior_graph_edges",
        "memories",
        "memory_proposals",
        "store_outbox",
        "synthesis_artifacts",
        "task_queue",
    }
)
_IDENTITY_ENV_NAMES = (
    "EMBEDDER_PROVIDER",
    "EMBEDDER_MODEL",
    "EMBEDDER_MODEL_REVISION",
    "PP_EMBEDDING_DIM",
    "PP_INFERENCE_CLIENT_VECTOR_IDENTITY",
    "PP_INFERENCE_CLIENT_VECTOR_DIMENSION",
)


def _readonly_connection(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise OSError("maintenance_database_not_regular")
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    if wal_path.exists() and not shm_path.exists():
        raise OSError("maintenance_database_wal_snapshot_unavailable")
    options = "mode=ro" if wal_path.exists() else "mode=ro&immutable=1"
    uri = f"file:{quote(str(path), safe='/')}?{options}"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    }


def _queue_report(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute(
        "SELECT task_type, title, to_agent, source_scan, payload, created_at "
        "FROM task_queue WHERE status = 'pending'"
    ).fetchall()
    by_type = Counter(str(row["task_type"] or "unknown") for row in rows)
    by_source = Counter(str(row["source_scan"] or "manual") for row in rows)
    age_buckets = Counter()
    for row in rows:
        age = conn.execute(
            "SELECT julianday('now') - julianday(?)", (row["created_at"],)
        ).fetchone()[0]
        if age is None or age < 0:
            age_buckets["invalid"] += 1
        elif age < 1:
            age_buckets["under_1d"] += 1
        elif age < 7:
            age_buckets["one_to_7d"] += 1
        elif age < 30:
            age_buckets["seven_to_30d"] += 1
        else:
            age_buckets["over_30d"] += 1

    payload_counts = Counter()
    exact_counts = Counter()
    for row in rows:
        payload = str(row["payload"] or "")
        payload_hash = ""
        if payload:
            try:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    payload_hash = str(decoded.get("payload_hash") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if payload_hash:
            payload_counts[(str(row["task_type"]), payload_hash)] += 1
        exact_counts[
            (
                str(row["task_type"]),
                str(row["title"]),
                str(row["to_agent"]),
                str(row["source_scan"]),
                payload,
            )
        ] += 1

    payload_duplicates = [count for count in payload_counts.values() if count > 1]
    exact_duplicates = [count for count in exact_counts.values() if count > 1]
    oldest, newest = conn.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM task_queue WHERE status = 'pending'"
    ).fetchone()
    return {
        "pending_total": len(rows),
        "by_type": dict(sorted(by_type.items())),
        "by_source": dict(sorted(by_source.items())),
        "age_buckets": dict(sorted(age_buckets.items())),
        "oldest_created_at": str(oldest or ""),
        "newest_created_at": str(newest or ""),
        "payload_duplicate_groups": len(payload_duplicates),
        "payload_duplicate_extra_rows": sum(count - 1 for count in payload_duplicates),
        "exact_duplicate_groups": len(exact_duplicates),
        "exact_duplicate_extra_rows": sum(count - 1 for count in exact_duplicates),
    }


def _lifecycle_report(
    conn: sqlite3.Connection, environ: Mapping[str, str]
) -> dict[str, int | bool]:
    ordinary = ordinary_memory_sql_predicate("memories")
    available = available_ordinary_memory_sql_predicate("memories")
    eligibility = " AND ".join(
        (
            "typeof(memories.id) = 'text' AND TRIM(memories.id) != ''",
            "typeof(memories.content) = 'text' AND TRIM(memories.content) != ''",
            "typeof(memories.project_id) = 'text' AND TRIM(memories.project_id) != ''",
            "typeof(memories.embedding_hash) = 'text' AND TRIM(memories.embedding_hash) != ''",
            "typeof(memories.created_at) = 'text' AND TRIM(memories.created_at) != ''",
            "typeof(memories.worth_success) IN ('integer', 'real') "
            "AND memories.worth_success >= 0 "
            "AND memories.worth_success < 1.0e308",
            "typeof(memories.worth_failure) IN ('integer', 'real') "
            "AND memories.worth_failure >= 0 "
            "AND memories.worth_failure < 1.0e308",
            "(memories.worth_success + memories.worth_failure) < 1.0e308",
            "typeof(memories.access_count) = 'integer' AND memories.access_count >= 0",
            "typeof(memories.tags) = 'text' AND json_valid(memories.tags) "
            "AND json_type(CASE WHEN json_valid(memories.tags) "
            "THEN memories.tags ELSE 'null' END) = 'array'",
            "typeof(memories.metadata_json) = 'text' "
            "AND json_valid(memories.metadata_json) "
            "AND json_type(CASE WHEN json_valid(memories.metadata_json) "
            "THEN memories.metadata_json ELSE 'null' END) = 'object'",
        )
    )
    periodic = str(environ.get("PP_PERIODIC_MAINTENANCE", "1")) == "1"
    decay_rows = conn.execute(
        "SELECT id, tier, created_at, effective_half_life, decay_multiplier "
        f"FROM memories WHERE {ordinary} AND {eligibility}"
    ).fetchall()
    projected_decay: dict[str, float] = {}
    decay_updates = 0
    if periodic:
        calculator = WeibullDecayCalculator()
        now = datetime.now().isoformat()
        for row in decay_rows:
            try:
                value = calculator.compute_decay(
                    tier=str(row["tier"] or "L1"),
                    created_at=str(row["created_at"]),
                    effective_half_life=float(row["effective_half_life"]),
                    current_time_str=now,
                )
                projected_decay[str(row["id"])] = value
                decay_updates += int(abs(float(row["decay_multiplier"]) - value) > 0.001)
            except (TypeError, ValueError, OverflowError):
                continue

    stale_rows = conn.execute(
        "SELECT id, decay_multiplier, worth_success, worth_failure FROM memories "
        f"WHERE {eligibility} AND {available} ORDER BY created_at, id"
    ).fetchall()
    stale_candidates = sum(
        1
        for row in stale_rows
        if projected_decay.get(str(row["id"]), float(row["decay_multiplier"])) < 0.2
        and float(row["worth_failure"] or 0) >= float(row["worth_success"] or 0)
    )
    stale_candidates = min(stale_candidates, 50)
    duplicate_rows = conn.execute(
        "SELECT COUNT(*) - 1 AS extras FROM memories "
        f"WHERE {eligibility} AND {available} "
        "GROUP BY project_id, content HAVING COUNT(*) > 1 "
        "ORDER BY project_id, content LIMIT 20"
    ).fetchall()
    return {
        "periodic_decay_enabled": periodic,
        "decay_evaluated": len(decay_rows) if periodic else 0,
        "decay_recalculation_candidates": decay_updates,
        "stale_transition_candidates": int(stale_candidates),
        "duplicate_transition_candidates": sum(int(row[0]) for row in duplicate_rows),
    }


def _synthesis_report(conn: sqlite3.Connection) -> dict[str, object]:
    # SynthesisStore.lint() is read-only, but its constructor runs idempotent
    # schema initializers. Bypass only the constructor so preflight uses the
    # production lint contract without issuing DDL against the read-only DB.
    store = object.__new__(SynthesisStore)
    store.conn = conn
    store.engine = None
    affected = []
    reason_counts = Counter()
    artifacts = conn.execute(
        "SELECT memory_id FROM synthesis_artifacts WHERE status = 'verified' ORDER BY memory_id"
    ).fetchall()
    for artifact in artifacts:
        memory_id = str(artifact["memory_id"])
        findings = store.lint(memory_id=memory_id)
        reasons = {str(finding["code"]) for finding in findings}
        if reasons:
            affected.append(memory_id)
            reason_counts.update(reasons)
    return {
        "verified_total": len(artifacts),
        "affected_candidates": len(affected),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


def _parse_managed_environment(path: str) -> tuple[dict[str, str], str]:
    managed = Path(path)
    if not managed.exists():
        return {}, ""
    values = {}
    revision = ""
    for raw_line in managed.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# revision="):
            revision = line.removeprefix("# revision=").strip()
        elif line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value
    return values, revision


def _configuration_report(managed_env_path: str, environ: Mapping[str, str]) -> dict[str, object]:
    values, revision = _parse_managed_environment(managed_env_path)
    present = Path(managed_env_path).is_file()
    mismatched = sorted(name for name, value in values.items() if environ.get(name) != value)
    identity_mismatches = sorted(name for name in mismatched if name in _IDENTITY_ENV_NAMES)
    return {
        "managed_environment_present": present,
        "managed_revision": revision,
        "loaded_into_process": present and not mismatched,
        "mismatched_variable_names": mismatched,
        "identity_consistent": present and not identity_mismatches,
        "identity_mismatch_names": identity_mismatches,
    }


async def build_maintenance_preflight(
    *,
    db_path: str,
    managed_env_path: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Project daemon writes and readiness without mutating canonical state."""
    runtime_environment = dict(os.environ if environ is None else environ)
    blockers = []
    warnings = []
    report: dict[str, object] = {
        "schema": "maintenance-preflight/v1",
        "mode": "read-only",
        "database": {"quick_check": "unavailable"},
        "task_queue": {"pending_total": 0, "by_type": {}},
        "proposals": {"expired_pending": 0},
        "outbox": {"pending_total": 0, "by_tool": {}},
        "lifecycle": {"decay_recalculation_candidates": 0},
        "synthesis": {"affected_candidates": 0},
        "scanners": {"coupling": {"projected_findings": 0, "projected_new_tasks": 0}},
    }
    try:
        conn = _readonly_connection(db_path)
    except (OSError, sqlite3.Error):
        blockers.append("canonical_database_read_only_open_failed")
        conn = None

    if conn is not None:
        try:
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            report["database"] = {"quick_check": quick_check}
            if quick_check != "ok":
                blockers.append("canonical_database_quick_check_failed")
            missing = sorted(_REQUIRED_TABLES - _table_names(conn))
            if missing:
                report["database"]["missing_tables"] = missing
                blockers.append("maintenance_schema_incomplete")
            else:
                queue = _queue_report(conn)
                report["task_queue"] = queue
                expired = conn.execute(
                    "SELECT COUNT(*) FROM memory_proposals "
                    "WHERE status = 'pending' AND expires_at <= datetime('now')"
                ).fetchone()[0]
                report["proposals"] = {"expired_pending": int(expired)}
                outbox_rows = conn.execute(
                    "SELECT tool_name, COUNT(*) FROM store_outbox "
                    "WHERE status = 'pending' GROUP BY tool_name ORDER BY tool_name"
                ).fetchall()
                outbox_by_tool = {str(row[0]): int(row[1]) for row in outbox_rows}
                report["outbox"] = {
                    "pending_total": sum(outbox_by_tool.values()),
                    "by_tool": outbox_by_tool,
                }
                report["lifecycle"] = _lifecycle_report(conn, runtime_environment)
                report["synthesis"] = _synthesis_report(conn)

                from plastic_promise.cron.scan_coupling import scan_coupling

                coupling = await scan_coupling(
                    None, connection=conn, dispatch=False, include_findings=True
                )
                findings = coupling.pop("projected_findings")
                pending_hashes = {
                    (str(row[0]), str(row[1]))
                    for row in conn.execute(
                        "SELECT task_type, json_extract(payload, '$.payload_hash') "
                        "FROM task_queue WHERE status = 'pending' "
                        "AND json_extract(payload, '$.payload_hash') IS NOT NULL"
                    ).fetchall()
                }
                new_findings = sum(
                    1
                    for finding in findings
                    if (
                        str(finding["task_type_field"]),
                        _compute_payload_hash(finding),
                    )
                    not in pending_hashes
                )
                report["scanners"] = {
                    "coupling": {
                        "projected_findings": int(coupling["findings"]),
                        "already_pending": int(coupling["findings"] - new_findings),
                        "projected_new_tasks": int(new_findings),
                    }
                }
        except sqlite3.Error:
            blockers.append("maintenance_preflight_query_failed")
        finally:
            conn.close()

    configuration = _configuration_report(managed_env_path, runtime_environment)
    report["configuration"] = configuration
    if configuration["managed_environment_present"]:
        if not configuration["loaded_into_process"]:
            blockers.append("managed_environment_not_loaded")
        if not configuration["identity_consistent"]:
            blockers.append("managed_embedding_identity_mismatch")
    else:
        warnings.append("managed_environment_missing")

    queue = report["task_queue"]
    proposals = report["proposals"]
    outbox = report["outbox"]
    lifecycle = report["lifecycle"]
    synthesis = report["synthesis"]
    scanners = report["scanners"]
    if queue.get("pending_total", 0):
        blockers.append("pending_task_queue_requires_review")
    if proposals.get("expired_pending", 0):
        blockers.append("expired_memory_proposals_would_mutate")
    if outbox.get("pending_total", 0):
        blockers.append("pending_index_outbox_would_replay")
    if lifecycle.get("stale_transition_candidates", 0) or lifecycle.get(
        "duplicate_transition_candidates", 0
    ):
        blockers.append("memory_lifecycle_would_transition_canonical_rows")
    if synthesis.get("affected_candidates", 0):
        blockers.append("synthesis_integrity_would_invalidate_artifacts")
    if scanners.get("coupling", {}).get("projected_new_tasks", 0):
        blockers.append("coupling_scanner_would_enqueue_tasks")
    if lifecycle.get("decay_recalculation_candidates", 0):
        warnings.append("periodic_decay_would_recalculate_canonical_rows")

    expected_writes = {
        "decay_recalculations": int(lifecycle.get("decay_recalculation_candidates", 0)),
        "memory_transitions": int(lifecycle.get("stale_transition_candidates", 0))
        + int(lifecycle.get("duplicate_transition_candidates", 0)),
        "proposal_expirations": int(proposals.get("expired_pending", 0)),
        "outbox_replays": int(outbox.get("pending_total", 0)),
        "synthesis_invalidations": int(synthesis.get("affected_candidates", 0)),
        "task_enqueues": int(scanners.get("coupling", {}).get("projected_new_tasks", 0)),
    }
    report["expected_writes"] = expected_writes
    report["blockers"] = sorted(set(blockers))
    report["warnings"] = sorted(set(warnings))
    report["ready"] = not report["blockers"]
    report["ok"] = report["ready"]
    return report
