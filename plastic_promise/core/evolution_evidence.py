"""Immutable evolution-evidence ledger with idempotent projection outboxes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "raw_evidence",
        "secret",
        "token",
    }
)
_MAX_PAYLOAD_BYTES = 32 * 1024


class EvolutionEvidenceError(RuntimeError):
    """Base error for evidence admission failures."""


class EvolutionEvidenceConflictError(EvolutionEvidenceError):
    """Raised when one idempotency key is reused for different evidence."""


@dataclass(frozen=True)
class EvolutionEvidenceEvent:
    """Bounded observation admitted to the immutable evolution ledger."""

    project_id: str
    causal_scope: str
    origin_kind: str
    sensor_name: str
    sensor_version: str
    source_revision: str
    source_sha256: str
    subject_type: str
    subject_id: str
    rule_id: str
    payload: dict[str, Any]
    raw_evidence_sha256: str
    idempotency_key: str
    raw_evidence_ref: str = ""
    parent_evidence_id: str = ""
    parent_rule_revision: str = ""
    occurred_at: str = field(default_factory=lambda: _utc_now())

    def __post_init__(self) -> None:
        for name, limit in (
            ("project_id", 300),
            ("causal_scope", 500),
            ("origin_kind", 100),
            ("sensor_name", 200),
            ("sensor_version", 200),
            ("source_revision", 300),
            ("subject_type", 100),
            ("subject_id", 1000),
            ("rule_id", 300),
            ("raw_evidence_ref", 1000),
            ("parent_evidence_id", 300),
            ("parent_rule_revision", 300),
            ("occurred_at", 100),
        ):
            value = str(getattr(self, name) or "").strip()
            if (
                name
                in {
                    "project_id",
                    "causal_scope",
                    "origin_kind",
                    "sensor_name",
                    "sensor_version",
                    "source_revision",
                    "subject_type",
                    "subject_id",
                    "rule_id",
                    "occurred_at",
                }
                and not value
            ):
                raise ValueError(f"evolution_evidence_{name}_required")
            if len(value) > limit:
                raise ValueError(f"evolution_evidence_{name}_too_long")
            object.__setattr__(self, name, value)

        if not _SHA256_RE.fullmatch(str(self.source_sha256 or "")):
            raise ValueError("evolution_evidence_source_sha256_invalid")
        if not _SHA256_RE.fullmatch(str(self.raw_evidence_sha256 or "")):
            raise ValueError("evolution_evidence_raw_sha256_invalid")
        idempotency_key = str(self.idempotency_key or "").strip()
        if not 16 <= len(idempotency_key) <= 512:
            raise ValueError("evolution_evidence_idempotency_key_invalid")
        object.__setattr__(self, "idempotency_key", idempotency_key)

        if not isinstance(self.payload, dict):
            raise ValueError("evolution_evidence_payload_invalid")
        _validate_redacted_payload(self.payload)
        if len(_canonical_json(self.payload).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("evolution_evidence_payload_too_large")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "causal_scope": self.causal_scope,
            "origin_kind": self.origin_kind,
            "sensor_name": self.sensor_name,
            "sensor_version": self.sensor_version,
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "rule_id": self.rule_id,
            "payload": self.payload,
            "raw_evidence_sha256": self.raw_evidence_sha256,
            "idempotency_key": self.idempotency_key,
            "raw_evidence_ref": self.raw_evidence_ref,
            "parent_evidence_id": self.parent_evidence_id,
            "parent_rule_revision": self.parent_rule_revision,
            "occurred_at": self.occurred_at,
        }

    def canonical_content(self) -> dict[str, Any]:
        content = self.to_dict()
        content.pop("idempotency_key", None)
        content.pop("occurred_at", None)
        return content


@dataclass(frozen=True)
class SubmissionResult:
    evidence_id: str
    status: Literal["created", "deduplicated"]
    independence_group: str
    lifecycle_state: str
    projection_outbox_id: str
    reconciliation_outbox_id: str


class EvolutionEvidence:
    """Deep module owning evidence identity, admission, and durable publication."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def submit(self, event: EvolutionEvidenceEvent) -> SubmissionResult:
        content_json = _canonical_json(event.canonical_content())
        request_hash = _sha256(content_json)
        idempotency_key_hash = _sha256(event.idempotency_key)
        evidence_id = "evolution_" + request_hash[:24]
        independence_group = _independence_group(event)
        projection_outbox_id = _outbox_id(evidence_id, "knowledge_projection")
        reconciliation_outbox_id = _outbox_id(evidence_id, "evidence_reconciliation")
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_submission = connection.execute(
                """
                SELECT request_hash, evidence_id
                FROM evolution_evidence_submissions
                WHERE project_id = ? AND idempotency_key_hash = ?
                """,
                (event.project_id, idempotency_key_hash),
            ).fetchone()
            if existing_submission is not None:
                if existing_submission["request_hash"] != request_hash:
                    raise EvolutionEvidenceConflictError(
                        "evolution_evidence_idempotency_key_conflict"
                    )
                return self._result_for(
                    connection,
                    str(existing_submission["evidence_id"]),
                    status="deduplicated",
                )

            created = (
                connection.execute(
                    "SELECT 1 FROM evolution_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                is None
            )
            if created:
                connection.execute(
                    """
                    INSERT INTO evolution_evidence (
                        evidence_id, project_id, request_hash, independence_group,
                        lifecycle_state, causal_scope, origin_kind, sensor_name,
                        sensor_version, source_revision, source_sha256, subject_type,
                        subject_id, rule_id, payload_json, raw_evidence_sha256,
                        raw_evidence_ref, parent_evidence_id, parent_rule_revision,
                        occurred_at, created_at
                    ) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        event.project_id,
                        request_hash,
                        independence_group,
                        event.causal_scope,
                        event.origin_kind,
                        event.sensor_name,
                        event.sensor_version,
                        event.source_revision,
                        event.source_sha256,
                        event.subject_type,
                        event.subject_id,
                        event.rule_id,
                        _canonical_json(event.payload),
                        event.raw_evidence_sha256,
                        event.raw_evidence_ref,
                        event.parent_evidence_id,
                        event.parent_rule_revision,
                        event.occurred_at,
                        now,
                    ),
                )

            connection.execute(
                """
                INSERT INTO evolution_evidence_submissions (
                    project_id, idempotency_key_hash, request_hash, evidence_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.project_id,
                    idempotency_key_hash,
                    request_hash,
                    evidence_id,
                    now,
                ),
            )
            self._queue_outbox(
                connection,
                outbox_id=projection_outbox_id,
                evidence_id=evidence_id,
                project_id=event.project_id,
                event_type="knowledge_projection",
                payload_json=content_json,
                now=now,
            )
            self._queue_outbox(
                connection,
                outbox_id=reconciliation_outbox_id,
                evidence_id=evidence_id,
                project_id=event.project_id,
                event_type="evidence_reconciliation",
                payload_json=content_json,
                now=now,
            )
            return SubmissionResult(
                evidence_id=evidence_id,
                status="created" if created else "deduplicated",
                independence_group=independence_group,
                lifecycle_state="observed",
                projection_outbox_id=projection_outbox_id,
                reconciliation_outbox_id=reconciliation_outbox_id,
            )

    def _result_for(
        self,
        connection: sqlite3.Connection,
        evidence_id: str,
        *,
        status: Literal["created", "deduplicated"],
    ) -> SubmissionResult:
        row = connection.execute(
            """
            SELECT independence_group, lifecycle_state
            FROM evolution_evidence
            WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise EvolutionEvidenceError("evolution_evidence_submission_orphaned")
        return SubmissionResult(
            evidence_id=evidence_id,
            status=status,
            independence_group=str(row["independence_group"]),
            lifecycle_state=str(row["lifecycle_state"]),
            projection_outbox_id=_outbox_id(evidence_id, "knowledge_projection"),
            reconciliation_outbox_id=_outbox_id(evidence_id, "evidence_reconciliation"),
        )

    @staticmethod
    def _queue_outbox(
        connection: sqlite3.Connection,
        *,
        outbox_id: str,
        evidence_id: str,
        project_id: str,
        event_type: str,
        payload_json: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO evolution_projection_outbox (
                outbox_id, evidence_id, project_id, event_type, payload_json,
                status, attempts, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, '', ?, ?)
            """,
            (outbox_id, evidence_id, project_id, event_type, payload_json, now, now),
        )

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evolution_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    independence_group TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    causal_scope TEXT NOT NULL,
                    origin_kind TEXT NOT NULL,
                    sensor_name TEXT NOT NULL,
                    sensor_version TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    raw_evidence_sha256 TEXT NOT NULL,
                    raw_evidence_ref TEXT NOT NULL,
                    parent_evidence_id TEXT NOT NULL,
                    parent_rule_revision TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_evolution_evidence_project_state
                    ON evolution_evidence(project_id, lifecycle_state, created_at);
                CREATE INDEX IF NOT EXISTS idx_evolution_evidence_independence
                    ON evolution_evidence(project_id, independence_group, rule_id);

                CREATE TABLE IF NOT EXISTS evolution_evidence_submissions (
                    project_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, idempotency_key_hash),
                    FOREIGN KEY(evidence_id) REFERENCES evolution_evidence(evidence_id)
                );

                CREATE TABLE IF NOT EXISTS evolution_projection_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(evidence_id, event_type),
                    FOREIGN KEY(evidence_id) REFERENCES evolution_evidence(evidence_id)
                );

                CREATE INDEX IF NOT EXISTS idx_evolution_projection_outbox_status
                    ON evolution_projection_outbox(status, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def _validate_redacted_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(f"evolution_evidence_sensitive_field_forbidden:{path}.{key}")
            _validate_redacted_payload(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_redacted_payload(item, f"{path}[{index}]")
    elif isinstance(value, str) and len(value) > 4000:
        raise ValueError(f"evolution_evidence_payload_string_too_long:{path}")


def _independence_group(event: EvolutionEvidenceEvent) -> str:
    if event.parent_rule_revision:
        root = f"rule-descendant:{event.parent_rule_revision}"
    elif event.origin_kind in {"knowledge_projection", "memory_projection", "rule_feedback"}:
        root = f"derived:{event.parent_evidence_id or event.causal_scope}"
    elif event.source_sha256:
        root = f"source:{event.origin_kind}:{event.source_sha256}"
    else:
        root = f"causal:{event.origin_kind}:{event.causal_scope}"
    return "independence_" + _sha256(f"{event.project_id}\x1f{root}")[:24]


def _outbox_id(evidence_id: str, event_type: str) -> str:
    return "evolution_outbox_" + _sha256(f"{evidence_id}\x1f{event_type}")[:24]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
