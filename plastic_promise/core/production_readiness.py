"""Fail-closed, read-only production readiness projection.

The module deliberately accepts evidence rather than collecting production
state.  It reads the canonical SQLite database in ``mode=ro`` with
``query_only`` enabled, computes bounded governance metrics, and returns a
machine-readable decision without repairing schemas or mutating queues.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "plastic-promise/production-readiness/v1"
OBSERVATION_WINDOW = timedelta(hours=24)
MAX_ACTIVE_OUTBOX_LAG_SECONDS = 300.0
SQLITE_QUERY_BUDGET_SECONDS = 2.0
SQLITE_PROGRESS_INSTRUCTIONS = 10_000

_REQUIRED_TABLES = frozenset(
    {
        "call_spans",
        "runtime_events",
        "degradation_events",
        "store_outbox",
        "derived_work_jobs",
        "memory_proposal_promotion_tasks",
        "security_finding_versions",
        "production_evidence_attestations",
    }
)
_ACTIVE_OUTBOX_STATUSES = frozenset({"pending", "processing", "retry_wait", "leased"})
_ACTIVE_PROMOTION_STATUSES = frozenset({"queued", "leased", "retry_wait"})
_CAPTURE_SUCCESS_STATUSES = frozenset({"queued", "duplicate", "shadow", "skipped"})
_HEX_DIGITS = frozenset("0123456789abcdef")


def _utc(value: datetime | None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("production_readiness_now_timezone_required")
    return observed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _json_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise OSError("production_readiness_database_not_regular")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    try:
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + SQLITE_QUERY_BUDGET_SECONDS
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            SQLITE_PROGRESS_INSTRUCTIONS,
        )
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            raise sqlite3.OperationalError("query_only_unavailable")
        return connection
    except BaseException:
        connection.close()
        raise


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }


def _recent_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    timestamp_column: str,
    since: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        f'SELECT * FROM "{table}" WHERE "{timestamp_column}" >= ?',
        (since,),
    ).fetchall()


def _scope_metrics(
    call_rows: list[sqlite3.Row],
    runtime_rows: list[sqlite3.Row],
    degradation_rows: list[sqlite3.Row],
) -> tuple[int, int, int, float, int]:
    isolation_violations = 0
    forbidden_hits = 0
    labeled_quality_cases = 0
    scopes: dict[str, set[str]] = {}
    observations = [*call_rows, *runtime_rows, *degradation_rows]

    for row in observations:
        project_id = str(row["project_id"] or "").strip()
        request_scope_id = str(row["request_scope_id"] or "").strip()
        if request_scope_id and project_id and project_id != "project:unknown":
            scopes.setdefault(request_scope_id, set()).add(project_id)

        metadata = _json_object(row["metadata_json"])
        if metadata.get("project_isolation_violation") is True:
            isolation_violations += 1
        metadata_project = str(metadata.get("project_id") or "").strip()
        if metadata_project and project_id and metadata_project != project_id:
            isolation_violations += 1

        quality = metadata.get("live_recall_quality_v1")
        if isinstance(quality, Mapping):
            labeled_quality_cases += 1
            if quality.get("forbidden_hit") is True:
                forbidden_hits += 1
        elif metadata.get("cross_project_forbidden_hit") is True:
            labeled_quality_cases += 1
            forbidden_hits += 1

    collisions = sum(1 for projects in scopes.values() if len(projects) > 1)
    unknown = sum(
        1 for row in observations if str(row["project_id"] or "").strip() in {"", "project:unknown"}
    )
    unknown_rate = round(unknown / len(observations), 6) if observations else 0.0
    return (
        isolation_violations,
        forbidden_hits,
        collisions,
        unknown_rate,
        labeled_quality_cases,
    )


def _outbox_metrics(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> tuple[int, float | None, int]:
    active_timestamps: list[datetime] = []
    active_count = 0
    invalid_timestamps = 0
    for table in ("store_outbox", "derived_work_jobs"):
        rows = connection.execute(f'SELECT status, created_at FROM "{table}"').fetchall()
        for row in rows:
            if str(row["status"] or "") not in _ACTIVE_OUTBOX_STATUSES:
                continue
            active_count += 1
            timestamp = _parse_time(row["created_at"])
            if timestamp is None or timestamp > now:
                invalid_timestamps += 1
                continue
            active_timestamps.append(timestamp)
    if active_count == 0:
        return 0, 0.0, 0
    if not active_timestamps:
        return active_count, None, invalid_timestamps
    lag = max(0.0, (now - min(active_timestamps)).total_seconds())
    return active_count, round(lag, 3), invalid_timestamps


def _timeout_rate(
    call_rows: list[sqlite3.Row],
    degradation_rows: list[sqlite3.Row],
) -> tuple[float, int]:
    relevant = [
        row
        for row in call_rows
        if str(row["tool_name"] or "")
        in {"memory_recall", "context_supply", "passive_memory.before_invoke"}
    ]
    timeout_call_ids = {
        str(row["call_id"] or "")
        for row in degradation_rows
        if "timeout" in f"{row['error_class']} {row['error_message']}".casefold()
    }
    failures = 0
    for row in relevant:
        metadata = _json_object(row["metadata_json"])
        status = str(row["status"] or "").casefold()
        if (
            str(row["call_id"] or "") in timeout_call_ids
            or "timeout" in status
            or metadata.get("timeout") is True
        ):
            failures += 1
    return (round(failures / len(relevant), 6) if relevant else 0.0, len(relevant))


def _capture_rate(runtime_rows: list[sqlite3.Row]) -> tuple[float, int]:
    captures = [
        row for row in runtime_rows if str(row["event_name"] or "") == "passive_memory_after_invoke"
    ]
    success = 0
    for row in captures:
        metadata = _json_object(row["metadata_json"])
        event_status = str(row["status"] or "").casefold()
        capture_status = str(metadata.get("status") or "").casefold()
        if event_status == "completed" and capture_status in _CAPTURE_SUCCESS_STATUSES:
            success += 1
    return (round(success / len(captures), 6) if captures else 0.0, len(captures))


def _promotion_metrics(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status, COUNT(*) FROM memory_proposal_promotion_tasks GROUP BY status"
        ).fetchall()
    }
    return {
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "active": sum(counts.get(status, 0) for status in _ACTIVE_PROMOTION_STATUSES),
    }


def _recurring_findings(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT finding_id, project_id, security_state, created_at, version_id "
        "FROM security_finding_versions ORDER BY created_at, version_id"
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        latest[(str(row["project_id"]), str(row["finding_id"]))] = row
    return sum(1 for row in latest.values() if str(row["security_state"]) == "recurring")


def _nested(evidence: Mapping[str, object], section: str, field: str) -> object:
    value = evidence.get(section)
    return value.get(field) if isinstance(value, Mapping) else None


def _sha256(value: object) -> bool:
    text = str(value or "").strip().casefold()
    return len(text) == 64 and all(character in _HEX_DIGITS for character in text)


def _evidence_blockers(evidence: Mapping[str, object]) -> list[dict[str, str]]:
    required_truths = (
        ("generation.verified", _nested(evidence, "generation", "verified") is True),
        ("generation.reconciled", _nested(evidence, "generation", "reconciled") is True),
        ("generation.shadow_passed", _nested(evidence, "generation", "shadow_passed") is True),
        ("generation.canary_passed", _nested(evidence, "generation", "canary_passed") is True),
        (
            "sqlite.migration_dry_run_passed",
            _nested(evidence, "sqlite", "migration_dry_run_passed") is True,
        ),
        ("rollback.drill_passed", _nested(evidence, "rollback", "drill_passed") is True),
        ("mcp.health_ok", _nested(evidence, "mcp", "health_ok") is True),
        (
            "mcp.restart_recovery_ok",
            _nested(evidence, "mcp", "restart_recovery_ok") is True,
        ),
        (
            "validation.local_tests_passed",
            _nested(evidence, "validation", "local_tests_passed") is True,
        ),
        (
            "validation.independent_project_passed",
            _nested(evidence, "validation", "independent_project_passed") is True,
        ),
    )
    blockers = [
        {"code": "evidence_missing_or_failed", "field": field}
        for field, passed in required_truths
        if not passed
    ]
    if _nested(evidence, "generation", "stale") is not False:
        blockers.append({"code": "generation_stale_or_unknown", "field": "generation.stale"})
    if not _sha256(_nested(evidence, "sqlite", "backup_sha256")):
        blockers.append({"code": "backup_hash_missing_or_invalid", "field": "sqlite.backup_sha256"})
    if str(_nested(evidence, "sqlite", "integrity_check") or "").strip().casefold() != "ok":
        blockers.append({"code": "sqlite_integrity_unproven", "field": "sqlite.integrity_check"})
    if _nested(evidence, "mcp", "listener_host") not in {"127.0.0.1", "::1", "localhost"}:
        blockers.append({"code": "mcp_listener_not_loopback", "field": "mcp.listener_host"})
    if _nested(evidence, "mcp", "listener_port") != 9020:
        blockers.append({"code": "mcp_listener_port_invalid", "field": "mcp.listener_port"})
    if _nested(evidence, "mcp", "public_reachable") is not False:
        blockers.append(
            {"code": "mcp_public_reachability_not_denied", "field": "mcp.public_reachable"}
        )
    return blockers


def _evidence_digest(evidence: Mapping[str, object]) -> str:
    """Hash the evidence body without its self-referential attestation."""

    body = {key: value for key, value in evidence.items() if key != "attestation"}
    material = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _attestation_blockers(
    connection: sqlite3.Connection,
    evidence: Mapping[str, object],
    database: Path,
    now: datetime,
) -> list[dict[str, str]]:
    """Require evidence recorded by the local verifier, not caller booleans.

    The readiness API is intentionally read-only.  A trusted collector writes
    one verified attestation row into the canonical SQLite database; this
    function binds the supplied evidence to that row, the exact database
    subject, a bounded validity interval, and the canonical evidence digest.
    """

    raw = evidence.get("attestation")
    if not isinstance(raw, Mapping):
        return [{"code": "evidence_attestation_missing", "field": "attestation"}]
    attestation_id = str(raw.get("attestation_id") or "").strip()
    subject = str(raw.get("subject_path_sha256") or "").strip().casefold()
    digest = str(raw.get("evidence_sha256") or "").strip().casefold()
    issuer = str(raw.get("issuer") or "").strip()
    signature = str(raw.get("signature") or "").strip()
    issued_at = _parse_time(raw.get("issued_at"))
    expires_at = _parse_time(raw.get("expires_at"))
    blockers: list[dict[str, str]] = []
    if not attestation_id:
        blockers.append(
            {"code": "evidence_attestation_id_missing", "field": "attestation.attestation_id"}
        )
    if subject != hashlib.sha256(str(database).encode("utf-8")).hexdigest():
        blockers.append(
            {"code": "evidence_subject_mismatch", "field": "attestation.subject_path_sha256"}
        )
    if digest != _evidence_digest(evidence):
        blockers.append(
            {"code": "evidence_digest_mismatch", "field": "attestation.evidence_sha256"}
        )
    if not issuer or not signature:
        blockers.append(
            {"code": "evidence_attestation_unverified", "field": "attestation.signature"}
        )
    if (
        issued_at is None
        or expires_at is None
        or issued_at > now
        or expires_at < now
        or expires_at <= issued_at
    ):
        blockers.append(
            {"code": "evidence_attestation_expired_or_invalid", "field": "attestation.expires_at"}
        )
    if attestation_id:
        row = connection.execute(
            "SELECT subject_path_sha256, evidence_sha256, issuer, signature, status, issued_at, expires_at "
            "FROM production_evidence_attestations WHERE attestation_id = ?",
            (attestation_id,),
        ).fetchone()
        if row is None:
            blockers.append(
                {"code": "evidence_attestation_not_recorded", "field": "attestation.attestation_id"}
            )
        else:
            expected = (subject, digest, issuer, signature, "verified")
            actual = tuple(str(row[index] or "").strip() for index in range(5))
            if actual != expected:
                blockers.append(
                    {"code": "evidence_attestation_record_mismatch", "field": "attestation"}
                )
    return blockers


def _metric_blockers(
    metrics: Mapping[str, object],
    *,
    quality_case_count: int,
    recall_observation_count: int,
    capture_observation_count: int,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    zero_metrics = (
        "project_isolation_violations",
        "cross_project_forbidden_hits",
        "request_scope_collisions",
    )
    for field in zero_metrics:
        if int(metrics[field]) != 0:
            blockers.append({"code": f"{field}_detected", "field": field})
    if float(metrics["unknown_project_rate"]) != 0.0:
        blockers.append({"code": "unknown_project_rate_nonzero", "field": "unknown_project_rate"})
    if int(metrics["active_outbox_invalid_timestamps"]) != 0:
        blockers.append(
            {
                "code": "active_outbox_timestamp_invalid",
                "field": "active_outbox_invalid_timestamps",
            }
        )
    outbox_lag = metrics["active_outbox_lag_seconds"]
    if outbox_lag is None:
        blockers.append(
            {"code": "active_outbox_lag_unavailable", "field": "active_outbox_lag_seconds"}
        )
    elif float(outbox_lag) > MAX_ACTIVE_OUTBOX_LAG_SECONDS:
        blockers.append(
            {"code": "active_outbox_lag_exceeded", "field": "active_outbox_lag_seconds"}
        )
    if recall_observation_count == 0:
        blockers.append(
            {"code": "recall_context_observations_missing", "field": "recall_context_timeout_rate"}
        )
    elif float(metrics["recall_context_timeout_rate"]) != 0.0:
        blockers.append(
            {"code": "recall_context_timeout_detected", "field": "recall_context_timeout_rate"}
        )
    if capture_observation_count == 0:
        blockers.append(
            {"code": "hook_capture_observations_missing", "field": "hook_capture_success_rate"}
        )
    elif float(metrics["hook_capture_success_rate"]) != 1.0:
        blockers.append(
            {"code": "hook_capture_success_below_required", "field": "hook_capture_success_rate"}
        )
    if quality_case_count == 0:
        blockers.append(
            {"code": "forbidden_hit_observations_missing", "field": "cross_project_forbidden_hits"}
        )
    promotion = metrics["promotion"]
    if isinstance(promotion, Mapping) and int(promotion["failed"]) != 0:
        blockers.append({"code": "proposal_promotion_failed", "field": "promotion.failed"})
    if int(metrics["recurring_deepsec_findings"]) != 0:
        blockers.append(
            {"code": "deepsec_recurring_findings_present", "field": "recurring_deepsec_findings"}
        )
    return blockers


def evaluate_production_readiness(
    database_path: str | Path,
    *,
    external_evidence: Mapping[str, object],
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a read-only production readiness decision and explicit blockers."""

    if not isinstance(external_evidence, Mapping):
        raise TypeError("external_evidence_must_be_mapping")
    observed_now = _utc(now)
    since = _utc_text(observed_now - OBSERVATION_WINDOW)
    database = Path(database_path).expanduser()
    base: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "collected_at": _utc_text(observed_now),
        "observation_window_seconds": int(OBSERVATION_WINDOW.total_seconds()),
        "database": {
            "path_sha256": hashlib.sha256(str(database).encode("utf-8")).hexdigest(),
            "access": {"mode": "ro", "query_only": None},
        },
    }
    try:
        with closing(_readonly_connection(database)) as connection:
            base["database"]["access"] = {"mode": "ro", "query_only": True}  # type: ignore[index]
            missing_tables = sorted(_REQUIRED_TABLES - _table_names(connection))
            if missing_tables:
                blockers = [
                    {"code": "required_table_missing", "field": table} for table in missing_tables
                ]
                blockers.extend(_evidence_blockers(external_evidence))
                return {
                    **base,
                    "status": "blocked",
                    "ready": False,
                    "metrics": {},
                    "blockers": blockers,
                }

            attestation_blockers = _attestation_blockers(
                connection, external_evidence, database.resolve(), observed_now
            )
            call_rows = _recent_rows(
                connection,
                table="call_spans",
                timestamp_column="started_at",
                since=since,
            )
            runtime_rows = _recent_rows(
                connection,
                table="runtime_events",
                timestamp_column="created_at",
                since=since,
            )
            degradation_rows = _recent_rows(
                connection,
                table="degradation_events",
                timestamp_column="created_at",
                since=since,
            )
            isolation, forbidden, collisions, unknown_rate, quality_cases = _scope_metrics(
                call_rows,
                runtime_rows,
                degradation_rows,
            )
            outbox_count, outbox_lag, invalid_outbox_timestamps = _outbox_metrics(
                connection,
                now=observed_now,
            )
            timeout_rate, recall_observations = _timeout_rate(call_rows, degradation_rows)
            capture_rate, capture_observations = _capture_rate(runtime_rows)
            metrics: dict[str, object] = {
                "project_isolation_violations": isolation,
                "cross_project_forbidden_hits": forbidden,
                "request_scope_collisions": collisions,
                "unknown_project_rate": unknown_rate,
                "active_outbox_count": outbox_count,
                "active_outbox_lag_seconds": outbox_lag,
                "active_outbox_invalid_timestamps": invalid_outbox_timestamps,
                "recall_context_timeout_rate": timeout_rate,
                "hook_capture_success_rate": capture_rate,
                "promotion": _promotion_metrics(connection),
                "recurring_deepsec_findings": _recurring_findings(connection),
            }
    except (OSError, sqlite3.Error, ValueError):
        blockers = [{"code": "database_read_unavailable", "field": "database"}]
        blockers.extend(_evidence_blockers(external_evidence))
        return {
            **base,
            "status": "blocked",
            "ready": False,
            "metrics": {},
            "blockers": blockers,
        }

    blockers = _metric_blockers(
        metrics,
        quality_case_count=quality_cases,
        recall_observation_count=recall_observations,
        capture_observation_count=capture_observations,
    )
    blockers.extend(_evidence_blockers(external_evidence))
    blockers.extend(attestation_blockers)
    return {
        **base,
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "metrics": metrics,
        "blockers": blockers,
    }
