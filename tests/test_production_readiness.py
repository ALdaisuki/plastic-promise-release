import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plastic_promise.core.production_readiness import evaluate_production_readiness

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def _timestamp(*, seconds_ago: int = 0) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _create_readiness_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE call_spans (
                call_id TEXT PRIMARY KEY,
                request_scope_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                degraded INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL
            );
            CREATE TABLE runtime_events (
                event_id TEXT PRIMARY KEY,
                event_name TEXT NOT NULL,
                status TEXT NOT NULL,
                request_scope_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE degradation_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                request_scope_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                error_class TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE store_outbox (
                outbox_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE derived_work_jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE memory_proposal_promotion_tasks (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE security_finding_versions (
                version_id TEXT PRIMARY KEY,
                finding_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                security_state TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE production_evidence_attestations (
                attestation_id TEXT PRIMARY KEY,
                subject_path_sha256 TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                issuer TEXT NOT NULL,
                signature TEXT NOT NULL,
                status TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO call_spans VALUES (?, ?, ?, 'memory_recall', 'success', 0, ?, ?, ?)",
            (
                "call-ready",
                "scope-ready",
                "project:plastic-promise",
                json.dumps(
                    {
                        "project_isolation_violation": False,
                        "live_recall_quality_v1": {"forbidden_hit": False},
                    }
                ),
                _timestamp(seconds_ago=10),
                _timestamp(seconds_ago=9),
            ),
        )
        connection.execute(
            "INSERT INTO runtime_events VALUES (?, 'passive_memory_after_invoke', "
            "'completed', ?, ?, ?, ?)",
            (
                "event-ready",
                "scope-ready",
                "project:plastic-promise",
                json.dumps({"status": "queued"}),
                _timestamp(seconds_ago=8),
            ),
        )
        connection.execute(
            "INSERT INTO memory_proposal_promotion_tasks VALUES "
            "('promotion-complete', 'project:plastic-promise', 'completed', ?, ?)",
            (_timestamp(seconds_ago=7), _timestamp(seconds_ago=6)),
        )
        connection.execute(
            "INSERT INTO security_finding_versions VALUES "
            "('finding-version', 'finding-1', 'project:plastic-promise', 'resolved', ?)",
            (_timestamp(seconds_ago=5),),
        )
        evidence = _external_evidence(path)
        attestation = evidence["attestation"]
        connection.execute(
            "INSERT INTO production_evidence_attestations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attestation["attestation_id"],
                attestation["subject_path_sha256"],
                attestation["evidence_sha256"],
                attestation["issuer"],
                attestation["signature"],
                "verified",
                attestation["issued_at"],
                attestation["expires_at"],
            ),
        )


def _external_evidence(database: Path | None = None) -> dict[str, object]:
    digest = hashlib.sha256(b"backup").hexdigest()
    evidence: dict[str, object] = {
        "generation": {
            "verified": True,
            "reconciled": True,
            "stale": False,
            "shadow_passed": True,
            "canary_passed": True,
        },
        "sqlite": {
            "backup_sha256": digest,
            "integrity_check": "ok",
            "migration_dry_run_passed": True,
        },
        "rollback": {"drill_passed": True},
        "mcp": {
            "health_ok": True,
            "restart_recovery_ok": True,
            "listener_host": "127.0.0.1",
            "listener_port": 9020,
            "public_reachable": False,
        },
        "validation": {
            "local_tests_passed": True,
            "independent_project_passed": True,
        },
    }
    if database is not None:
        body = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence["attestation"] = {
            "attestation_id": "attestation-ready",
            "subject_path_sha256": hashlib.sha256(
                str(database.resolve()).encode("utf-8")
            ).hexdigest(),
            "evidence_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "issuer": "local-test-verifier",
            "signature": "test-signature",
            "issued_at": _timestamp(seconds_ago=30),
            "expires_at": _timestamp(seconds_ago=-3600),
        }
    return evidence


def _blockers(report: dict[str, object]) -> set[tuple[str, str]]:
    return {(str(item["code"]), str(item["field"])) for item in report["blockers"]}


def test_ready_snapshot_passes_and_does_not_modify_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    before = database.read_bytes()

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert report["schema"] == "plastic-promise/production-readiness/v1"
    assert report["status"] == "ready"
    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["metrics"] == {
        "project_isolation_violations": 0,
        "cross_project_forbidden_hits": 0,
        "request_scope_collisions": 0,
        "unknown_project_rate": 0.0,
        "active_outbox_count": 0,
        "active_outbox_lag_seconds": 0.0,
        "active_outbox_invalid_timestamps": 0,
        "recall_context_timeout_rate": 0.0,
        "hook_capture_success_rate": 1.0,
        "promotion": {"completed": 1, "failed": 0, "active": 0},
        "recurring_deepsec_findings": 0,
    }
    assert report["database"]["access"] == {"mode": "ro", "query_only": True}
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("section", "field", "value", "expected"),
    [
        ("generation", "verified", False, ("evidence_missing_or_failed", "generation.verified")),
        ("generation", "reconciled", None, ("evidence_missing_or_failed", "generation.reconciled")),
        ("generation", "stale", True, ("generation_stale_or_unknown", "generation.stale")),
        (
            "generation",
            "shadow_passed",
            False,
            ("evidence_missing_or_failed", "generation.shadow_passed"),
        ),
        (
            "generation",
            "canary_passed",
            False,
            ("evidence_missing_or_failed", "generation.canary_passed"),
        ),
        (
            "sqlite",
            "backup_sha256",
            "not-a-hash",
            ("backup_hash_missing_or_invalid", "sqlite.backup_sha256"),
        ),
        (
            "sqlite",
            "integrity_check",
            "failed",
            ("sqlite_integrity_unproven", "sqlite.integrity_check"),
        ),
        (
            "sqlite",
            "migration_dry_run_passed",
            False,
            ("evidence_missing_or_failed", "sqlite.migration_dry_run_passed"),
        ),
        (
            "rollback",
            "drill_passed",
            False,
            ("evidence_missing_or_failed", "rollback.drill_passed"),
        ),
        ("mcp", "health_ok", False, ("evidence_missing_or_failed", "mcp.health_ok")),
        (
            "mcp",
            "restart_recovery_ok",
            False,
            ("evidence_missing_or_failed", "mcp.restart_recovery_ok"),
        ),
        ("mcp", "listener_host", "0.0.0.0", ("mcp_listener_not_loopback", "mcp.listener_host")),
        ("mcp", "listener_port", 19020, ("mcp_listener_port_invalid", "mcp.listener_port")),
        (
            "mcp",
            "public_reachable",
            True,
            ("mcp_public_reachability_not_denied", "mcp.public_reachable"),
        ),
        (
            "validation",
            "local_tests_passed",
            False,
            ("evidence_missing_or_failed", "validation.local_tests_passed"),
        ),
        (
            "validation",
            "independent_project_passed",
            False,
            ("evidence_missing_or_failed", "validation.independent_project_passed"),
        ),
    ],
)
def test_each_external_evidence_failure_blocks_independently(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    expected: tuple[str, str],
) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    evidence = deepcopy(_external_evidence(database))
    evidence[section][field] = value

    report = evaluate_production_readiness(database, external_evidence=evidence, now=NOW)

    assert report["status"] == "blocked"
    assert report["ready"] is False
    assert expected in _blockers(report)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            "UPDATE call_spans SET metadata_json = "
            '\'{"project_isolation_violation":true,"live_recall_quality_v1":{"forbidden_hit":false}}\'',
            ("project_isolation_violations_detected", "project_isolation_violations"),
        ),
        (
            "UPDATE call_spans SET metadata_json = "
            '\'{"live_recall_quality_v1":{"forbidden_hit":true}}\'',
            ("cross_project_forbidden_hits_detected", "cross_project_forbidden_hits"),
        ),
        (
            "INSERT INTO call_spans VALUES "
            "('call-collision','scope-ready','project:other','context_supply','success',0,"
            '\'{"live_recall_quality_v1":{"forbidden_hit":false}}\','
            "'2026-08-05T11:59:50Z','2026-08-05T11:59:51Z')",
            ("request_scope_collisions_detected", "request_scope_collisions"),
        ),
        (
            "INSERT INTO runtime_events VALUES "
            "('event-unknown','passive_context_skipped','completed','scope-unknown',"
            "'project:unknown','{}','2026-08-05T11:59:52Z')",
            ("unknown_project_rate_nonzero", "unknown_project_rate"),
        ),
        (
            "INSERT INTO store_outbox VALUES "
            "('outbox-old','project:plastic-promise','pending','{}',"
            "'2026-08-05T11:54:59Z','2026-08-05T11:54:59Z')",
            ("active_outbox_lag_exceeded", "active_outbox_lag_seconds"),
        ),
        (
            "INSERT INTO store_outbox VALUES "
            "('outbox-invalid','project:plastic-promise','pending','{}',"
            "'not-a-time','not-a-time')",
            ("active_outbox_timestamp_invalid", "active_outbox_invalid_timestamps"),
        ),
        (
            "UPDATE call_spans SET status = 'timeout'",
            ("recall_context_timeout_detected", "recall_context_timeout_rate"),
        ),
        (
            "UPDATE runtime_events SET status = 'error', metadata_json = '{\"status\":\"degraded\"}'",
            ("hook_capture_success_below_required", "hook_capture_success_rate"),
        ),
        (
            "UPDATE runtime_events SET status = 'completed', metadata_json = '{}'",
            ("hook_capture_success_below_required", "hook_capture_success_rate"),
        ),
        (
            "INSERT INTO memory_proposal_promotion_tasks VALUES "
            "('promotion-failed','project:plastic-promise','failed',"
            "'2026-08-05T11:59:53Z','2026-08-05T11:59:54Z')",
            ("proposal_promotion_failed", "promotion.failed"),
        ),
        (
            "INSERT INTO security_finding_versions VALUES "
            "('finding-version-2','finding-1','project:plastic-promise','recurring',"
            "'2026-08-05T11:59:56Z')",
            ("deepsec_recurring_findings_present", "recurring_deepsec_findings"),
        ),
    ],
)
def test_each_database_risk_blocks_independently(
    tmp_path: Path,
    mutation: str,
    expected: tuple[str, str],
) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert report["status"] == "blocked"
    assert expected in _blockers(report)


def test_latest_security_finding_version_controls_recurring_count(tmp_path: Path) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO security_finding_versions VALUES "
            "('finding-recurring','finding-1','project:plastic-promise','recurring',?)",
            (_timestamp(seconds_ago=4),),
        )
        connection.execute(
            "INSERT INTO security_finding_versions VALUES "
            "('finding-resolved','finding-1','project:plastic-promise','resolved',?)",
            (_timestamp(seconds_ago=3),),
        )

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert report["metrics"]["recurring_deepsec_findings"] == 0
    assert report["ready"] is True


def test_missing_table_fails_closed_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE derived_work_jobs")
    before = database.read_bytes()

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert report["ready"] is False
    assert ("required_table_missing", "derived_work_jobs") in _blockers(report)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='derived_work_jobs'"
            ).fetchone()
            is None
        )


def test_missing_observation_evidence_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "plastic_memory.db"
    _create_readiness_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM call_spans")
        connection.execute("DELETE FROM runtime_events")

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert {
        ("recall_context_observations_missing", "recall_context_timeout_rate"),
        ("hook_capture_observations_missing", "hook_capture_success_rate"),
        ("forbidden_hit_observations_missing", "cross_project_forbidden_hits"),
    }.issubset(_blockers(report))


def test_missing_database_fails_closed_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    report = evaluate_production_readiness(
        database,
        external_evidence=_external_evidence(database),
        now=NOW,
    )

    assert report["ready"] is False
    assert ("database_read_unavailable", "database") in _blockers(report)
    assert not database.exists()
