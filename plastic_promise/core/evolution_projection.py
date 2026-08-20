"""Idempotent cross-store projection of evolution evidence into knowledge truth."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectionReconcileResult:
    processed: int
    completed: int
    failed: int
    evidence_ids: tuple[str, ...]


class KnowledgeSecurityFindingStore:
    """Repository for quarantined security-finding projections."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def project(self, record: dict[str, Any]) -> bool:
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_security_findings (
                    evidence_id, project_id, independence_group, lifecycle_state,
                    projection_state, sensor_name, sensor_version, source_revision,
                    source_sha256, subject_type, subject_id, rule_id, severity,
                    finding_type, path, region_json, message, suggestion,
                    raw_evidence_sha256, occurred_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'quarantined', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["evidence_id"],
                    record["project_id"],
                    record["independence_group"],
                    record["lifecycle_state"],
                    record["sensor_name"],
                    record["sensor_version"],
                    record["source_revision"],
                    record["source_sha256"],
                    record["subject_type"],
                    record["subject_id"],
                    record["rule_id"],
                    record["severity"],
                    record["finding_type"],
                    record["path"],
                    _canonical_json(record["region"]),
                    record["message"],
                    record["suggestion"],
                    record["raw_evidence_sha256"],
                    record["occurred_at"],
                    now,
                    now,
                ),
            )
            return cursor.rowcount > 0

    def get(self, evidence_id: str, *, project_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT evidence_id, project_id, independence_group, lifecycle_state,
                       projection_state, sensor_name, sensor_version, source_revision,
                       source_sha256, subject_type, subject_id, rule_id, severity,
                       finding_type, path, region_json, message, suggestion,
                       raw_evidence_sha256, occurred_at, created_at, updated_at
                FROM knowledge_security_findings
                WHERE evidence_id = ? AND project_id = ?
                """,
                (evidence_id, project_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["region"] = json.loads(result.pop("region_json"))
        return result

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_security_findings (
                    evidence_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    independence_group TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    projection_state TEXT NOT NULL,
                    sensor_name TEXT NOT NULL,
                    sensor_version TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    finding_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    region_json TEXT NOT NULL,
                    message TEXT NOT NULL,
                    suggestion TEXT NOT NULL,
                    raw_evidence_sha256 TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_security_findings_project_state
                    ON knowledge_security_findings(project_id, projection_state, severity);
                CREATE INDEX IF NOT EXISTS idx_knowledge_security_findings_rule
                    ON knowledge_security_findings(project_id, rule_id, source_revision);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


class EvolutionProjectionWorker:
    """Reconcile pending memory-ledger outbox rows into knowledge SQLite."""

    def __init__(
        self,
        memory_database_path: str | Path,
        knowledge_database_path: str | Path,
    ) -> None:
        self.memory_database_path = Path(memory_database_path)
        self.knowledge_store = KnowledgeSecurityFindingStore(knowledge_database_path)

    def reconcile(self, *, limit: int = 100) -> ProjectionReconcileResult:
        bounded_limit = max(1, min(int(limit), 1000))
        rows = self._pending_rows(bounded_limit)
        completed_ids: list[str] = []
        failed = 0
        for row in rows:
            try:
                record = self._projection_record(row)
                self.knowledge_store.project(record)
                self._mark_done(str(row["outbox_id"]))
                completed_ids.append(str(row["evidence_id"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                failed += 1
                self._mark_failed(str(row["outbox_id"]), "projection_payload_invalid")
            except sqlite3.Error:
                failed += 1
                self._mark_failed(str(row["outbox_id"]), "projection_storage_error")
        return ProjectionReconcileResult(
            processed=len(rows),
            completed=len(completed_ids),
            failed=failed,
            evidence_ids=tuple(completed_ids),
        )

    def _pending_rows(self, limit: int) -> list[sqlite3.Row]:
        with self._connect_memory() as connection:
            return list(
                connection.execute(
                    """
                    SELECT o.outbox_id, o.evidence_id, o.project_id, o.payload_json,
                           e.independence_group, e.lifecycle_state
                    FROM evolution_projection_outbox AS o
                    JOIN evolution_evidence AS e ON e.evidence_id = o.evidence_id
                    WHERE o.event_type = 'knowledge_projection'
                      AND o.status IN ('pending', 'retry')
                    ORDER BY o.created_at, o.outbox_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    @staticmethod
    def _projection_record(row: sqlite3.Row) -> dict[str, Any]:
        event = json.loads(str(row["payload_json"]))
        if event.get("origin_kind") != "security_scanner":
            raise ValueError("knowledge_projection_origin_unsupported")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("knowledge_projection_payload_invalid")
        region = payload.get("region") if isinstance(payload.get("region"), dict) else {}
        return {
            "evidence_id": str(row["evidence_id"]),
            "project_id": str(row["project_id"]),
            "independence_group": str(row["independence_group"]),
            "lifecycle_state": str(row["lifecycle_state"]),
            "sensor_name": str(event["sensor_name"]),
            "sensor_version": str(event["sensor_version"]),
            "source_revision": str(event["source_revision"]),
            "source_sha256": str(event["source_sha256"]),
            "subject_type": str(event["subject_type"]),
            "subject_id": str(event["subject_id"]),
            "rule_id": str(event["rule_id"]),
            "severity": _bounded(payload.get("severity"), 20),
            "finding_type": _bounded(payload.get("type"), 100),
            "path": _bounded(payload.get("path") or event["subject_id"], 1000),
            "region": {
                "start_line": _optional_int(region.get("start_line")),
                "end_line": _optional_int(region.get("end_line")),
            },
            "message": _bounded(payload.get("message"), 1000),
            "suggestion": _bounded(payload.get("suggestion"), 1000),
            "raw_evidence_sha256": str(event["raw_evidence_sha256"]),
            "occurred_at": str(event.get("occurred_at") or _utc_now()),
        }

    def _mark_done(self, outbox_id: str) -> None:
        with self._connect_memory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE evolution_projection_outbox
                SET status = 'done', attempts = attempts + 1,
                    last_error = '', updated_at = ?
                WHERE outbox_id = ? AND status IN ('pending', 'retry')
                """,
                (_utc_now(), outbox_id),
            )

    def _mark_failed(self, outbox_id: str, reason: str) -> None:
        with self._connect_memory() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE evolution_projection_outbox
                SET status = 'retry', attempts = attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE outbox_id = ? AND status IN ('pending', 'retry')
                """,
                (reason, _utc_now(), outbox_id),
            )

    def _connect_memory(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.memory_database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
