"""Durable, project-scoped tasks for eligible memory-proposal promotion.

The task queue is deliberately separate from the canonical memory mutation.
It records intent, lease/fencing and failure evidence; a worker must still
call the existing atomic proposal promoter to create canonical memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

TASK_STATUSES = frozenset({"queued", "leased", "retry_wait", "completed", "failed"})
RISK_TIERS = frozenset({"low", "medium", "high", "critical"})
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_LEASE_SECONDS = 60


def _text(value: object) -> str:
    return str(value or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    raw = _text(value)
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _scope_required(project_id: object) -> str:
    normalized = _text(project_id)
    if not normalized or normalized == "project:unknown":
        raise ValueError("promotion_task_project_scope_required")
    return normalized


def _risk_tier(value: object) -> str:
    normalized = _text(value).casefold() or "medium"
    if normalized not in RISK_TIERS:
        raise ValueError("promotion_task_risk_tier_invalid")
    return normalized


def risk_tier_for_proposal(proposal: Any) -> str:
    """Derive a conservative task tier from proposal metadata and scope."""

    metadata = proposal.get("metadata", {}) if isinstance(proposal, dict) else {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    explicit = _text(metadata.get("risk_tier"))
    if explicit:
        return _risk_tier(explicit)
    visibility = _text(proposal.get("visibility") if isinstance(proposal, dict) else "")
    category = _text(proposal.get("category") if isinstance(proposal, dict) else "")
    if visibility in {"global", "shared"}:
        return "high"
    if category == "decision":
        return "high"
    if category == "preference":
        return "medium"
    return "low"


def _task_id(project_id: str, proposal_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join((project_id, proposal_id, idempotency_key)).encode("utf-8")
    ).hexdigest()
    return f"promotion-task:{digest}"


def _lease_digest(token: object) -> str:
    """Persist only a one-way lease digest; the capability stays in memory."""

    return hashlib.sha256(_text(token).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromotionTask:
    task_id: str
    proposal_id: str
    project_id: str
    risk_tier: str
    status: str
    attempt_count: int
    max_attempts: int
    lease_token: str = field(repr=False)
    fencing_generation: int
    lease_expires_at: str
    next_attempt_at: str
    last_failure_code: str
    last_failure_detail: str
    memory_id: str
    idempotency_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PromotionTaskLease:
    task: PromotionTask
    lease_token: str = field(repr=False)


def ensure_promotion_task_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_proposal_promotion_tasks (
            task_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            risk_tier TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 4,
            lease_token_hash TEXT NOT NULL DEFAULT '',
            fencing_generation INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT NOT NULL DEFAULT '',
            next_attempt_at TEXT NOT NULL,
            last_failure_code TEXT NOT NULL DEFAULT '',
            last_failure_detail TEXT NOT NULL DEFAULT '',
            memory_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, proposal_id, idempotency_key),
            CHECK(status IN ('queued', 'leased', 'retry_wait', 'completed', 'failed')),
            CHECK(risk_tier IN ('low', 'medium', 'high', 'critical')),
            CHECK(attempt_count >= 0 AND max_attempts >= 1),
            CHECK(project_id != 'project:unknown')
        )
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(memory_proposal_promotion_tasks)").fetchall()
    }
    if "lease_token_hash" not in columns:
        conn.execute(
            "ALTER TABLE memory_proposal_promotion_tasks "
            "ADD COLUMN lease_token_hash TEXT NOT NULL DEFAULT ''"
        )
        if "lease_token" in columns:
            for task_id, token in conn.execute(
                "SELECT task_id, lease_token FROM memory_proposal_promotion_tasks "
                "WHERE lease_token <> ''"
            ).fetchall():
                conn.execute(
                    "UPDATE memory_proposal_promotion_tasks SET lease_token_hash = ? "
                    "WHERE task_id = ?",
                    (_lease_digest(token), task_id),
                )
            conn.execute(
                "UPDATE memory_proposal_promotion_tasks SET lease_token = '' "
                "WHERE lease_token <> ''"
            )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_promotion_tasks_claim
        ON memory_proposal_promotion_tasks(project_id, status, next_attempt_at, task_id)
        """
    )


class PromotionTaskStore:
    """SQLite-backed queue with compare-and-swap leases and retry evidence."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        ensure_promotion_task_schema(conn)

    def enqueue(
        self,
        *,
        proposal_id: str,
        project_id: str,
        risk_tier: str = "medium",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        idempotency_key: str = "",
        now: str | None = None,
    ) -> PromotionTask:
        project = _scope_required(project_id)
        proposal = _text(proposal_id)
        if not proposal:
            raise ValueError("promotion_task_proposal_required")
        proposal_row = self.conn.execute(
            "SELECT project_id, status FROM memory_proposals WHERE proposal_id = ?",
            (proposal,),
        ).fetchone()
        if proposal_row is None:
            raise ValueError("promotion_task_proposal_not_found")
        if str(proposal_row[0]) != project:
            raise ValueError("promotion_task_project_mismatch")
        if str(proposal_row[1]) != "pending":
            raise ValueError("promotion_task_proposal_not_pending")
        tier = _risk_tier(risk_tier)
        if isinstance(max_attempts, bool) or int(max_attempts) < 1 or int(max_attempts) > 32:
            raise ValueError("promotion_task_max_attempts_invalid")
        key = _text(idempotency_key) or f"eligible:{proposal}"
        if len(key) > 256:
            raise ValueError("promotion_task_idempotency_key_invalid")
        row = self.conn.execute(
            "SELECT project_id, status, risk_tier, max_attempts FROM memory_proposal_promotion_tasks "
            "WHERE project_id = ? AND proposal_id = ? AND idempotency_key = ?",
            (project, proposal, key),
        ).fetchone()
        if row is not None:
            if str(row[0]) != project or str(row[2]) != tier:
                raise ValueError("promotion_task_replay_conflict")
            return self.get(
                self.conn.execute(
                    "SELECT task_id FROM memory_proposal_promotion_tasks "
                    "WHERE project_id = ? AND proposal_id = ? AND idempotency_key = ?",
                    (project, proposal, key),
                ).fetchone()[0]
            )
        now_text = _text(now) or _utc_now()
        task_id = _task_id(project, proposal, key)
        self.conn.execute(
            """
            INSERT INTO memory_proposal_promotion_tasks (
                task_id, proposal_id, project_id, risk_tier, status,
                attempt_count, max_attempts, next_attempt_at, idempotency_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                proposal,
                project,
                tier,
                int(max_attempts),
                now_text,
                key,
                now_text,
                now_text,
            ),
        )
        return self.get(task_id)

    def get(self, task_id: str) -> PromotionTask:
        row = self.conn.execute(
            "SELECT * FROM memory_proposal_promotion_tasks WHERE task_id = ?",
            (_text(task_id),),
        ).fetchone()
        if row is None:
            raise ValueError("promotion_task_not_found")
        return self._row(row)

    def claim(
        self,
        *,
        project_id: str | None = None,
        limit: int = 1,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: str | None = None,
    ) -> tuple[PromotionTaskLease, ...]:
        project = _scope_required(project_id) if project_id is not None else None
        bounded_limit = max(1, min(int(limit), 64))
        if isinstance(lease_seconds, bool) or int(lease_seconds) < 1 or int(lease_seconds) > 3600:
            raise ValueError("promotion_task_lease_seconds_invalid")
        now_text = _text(now) or _utc_now()
        now_dt = _parse_utc(now_text)
        lease_until = (
            (now_dt + timedelta(seconds=int(lease_seconds))).isoformat().replace("+00:00", "Z")
        )
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            clauses = [
                "(status IN ('queued', 'retry_wait') AND next_attempt_at <= ?)",
                "(status = 'leased' AND lease_expires_at <= ?)",
            ]
            values: list[object] = [now_text, now_text]
            rows = conn.execute(
                "SELECT * FROM memory_proposal_promotion_tasks WHERE ("
                + " OR ".join(clauses[:2])
                + ")"
                + (" AND project_id = ?" if project is not None else "")
                + " ORDER BY created_at, task_id LIMIT ?",
                (*values, bounded_limit)
                if project is None
                else (values[0], values[1], project, bounded_limit),
            ).fetchall()
            leases: list[PromotionTaskLease] = []
            for row in rows:
                token = secrets.token_hex(24)
                generation = int(row["fencing_generation"] or 0) + 1
                updated = conn.execute(
                    """
                    UPDATE memory_proposal_promotion_tasks
                    SET status = 'leased', attempt_count = attempt_count + 1,
                        lease_token_hash = ?, fencing_generation = ?, lease_expires_at = ?,
                        updated_at = ?
                    WHERE task_id = ? AND (
                        status IN ('queued', 'retry_wait')
                        OR (status = 'leased' AND lease_expires_at <= ?)
                    )
                    """,
                    (
                        _lease_digest(token),
                        generation,
                        lease_until,
                        now_text,
                        row["task_id"],
                        now_text,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                task = self.get(str(row["task_id"]))
                leases.append(PromotionTaskLease(task=task, lease_token=token))
            conn.commit()
            return tuple(leases)
        except Exception:
            conn.rollback()
            raise

    def complete(self, lease: PromotionTaskLease, *, memory_id: str) -> PromotionTask:
        normalized_memory = _text(memory_id)
        if not normalized_memory:
            raise ValueError("promotion_task_memory_required")
        now_text = _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(lease)
            cursor = self.conn.execute(
                """
                UPDATE memory_proposal_promotion_tasks
                SET status = 'completed', memory_id = ?, lease_token_hash = '',
                    lease_expires_at = '', updated_at = ?
                WHERE task_id = ? AND status = 'leased'
                  AND lease_token_hash = ? AND fencing_generation = ?
                """,
                (
                    normalized_memory,
                    now_text,
                    lease.task.task_id,
                    _lease_digest(lease.lease_token),
                    lease.task.fencing_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("promotion_task_lease_conflict")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get(lease.task.task_id)

    def fail(
        self,
        lease: PromotionTaskLease,
        *,
        failure_code: str,
        failure_detail: str = "",
        retryable: bool,
        retry_delay_seconds: int = 0,
        now: str | None = None,
    ) -> PromotionTask:
        code = _text(failure_code)
        if not code:
            raise ValueError("promotion_task_failure_code_required")
        if len(code) > 128 or len(_text(failure_detail)) > 512:
            raise ValueError("promotion_task_failure_evidence_invalid")
        now_value = _text(now) or _utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._assert_lease(lease)
            retry = bool(retryable) and current.attempt_count < current.max_attempts
            next_status = "retry_wait" if retry else "failed"
            next_at = (
                (_parse_utc(now_value) + timedelta(seconds=max(0, int(retry_delay_seconds))))
                .isoformat()
                .replace("+00:00", "Z")
            )
            cursor = self.conn.execute(
                """
                UPDATE memory_proposal_promotion_tasks
                SET status = ?, lease_token_hash = '', lease_expires_at = '',
                    next_attempt_at = ?, last_failure_code = ?, last_failure_detail = ?,
                    updated_at = ?
                WHERE task_id = ? AND status = 'leased'
                  AND lease_token_hash = ? AND fencing_generation = ?
                """,
                (
                    next_status,
                    next_at,
                    code,
                    _text(failure_detail),
                    now_value,
                    lease.task.task_id,
                    _lease_digest(lease.lease_token),
                    lease.task.fencing_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("promotion_task_lease_conflict")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get(lease.task.task_id)

    def reconcile(self, *, now: str | None = None, limit: int = 100) -> dict[str, int]:
        now_text = _text(now) or _utc_now()
        bounded_limit = max(1, min(int(limit), 1000))
        rows = self.conn.execute(
            "SELECT * FROM memory_proposal_promotion_tasks "
            "WHERE status = 'leased' AND lease_expires_at <= ? "
            "ORDER BY updated_at, task_id LIMIT ?",
            (now_text, bounded_limit),
        ).fetchall()
        retried = 0
        failed = 0
        for row in rows:
            current = self.get(str(row["task_id"]))
            next_status = "retry_wait" if current.attempt_count < current.max_attempts else "failed"
            self.conn.execute(
                """
                UPDATE memory_proposal_promotion_tasks
                SET status = ?, lease_token_hash = '', lease_expires_at = '',
                    next_attempt_at = ?, last_failure_code = ?,
                    last_failure_detail = ?, updated_at = ?
                WHERE task_id = ? AND status = 'leased' AND fencing_generation = ?
                """,
                (
                    next_status,
                    now_text,
                    "lease_expired",
                    "lease expired before promotion completed",
                    now_text,
                    current.task_id,
                    current.fencing_generation,
                ),
            )
            if next_status == "retry_wait":
                retried += 1
            else:
                failed += 1
        self.conn.commit()
        return {"requeued": retried, "failed": failed, "inspected": len(rows)}

    @staticmethod
    def _row(row: sqlite3.Row) -> PromotionTask:
        return PromotionTask(
            task_id=str(row["task_id"]),
            proposal_id=str(row["proposal_id"]),
            project_id=str(row["project_id"]),
            risk_tier=str(row["risk_tier"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"] or 0),
            max_attempts=int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS),
            lease_token="",
            fencing_generation=int(row["fencing_generation"] or 0),
            lease_expires_at=str(row["lease_expires_at"] or ""),
            next_attempt_at=str(row["next_attempt_at"] or ""),
            last_failure_code=str(row["last_failure_code"] or ""),
            last_failure_detail=str(row["last_failure_detail"] or ""),
            memory_id=str(row["memory_id"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _assert_lease(self, lease: PromotionTaskLease) -> PromotionTask:
        row = self.conn.execute(
            "SELECT * FROM memory_proposal_promotion_tasks WHERE task_id = ?",
            (lease.task.task_id,),
        ).fetchone()
        if row is None:
            raise ValueError("promotion_task_lease_conflict")
        if (
            str(row["status"]) != "leased"
            or int(row["fencing_generation"] or 0) != lease.task.fencing_generation
            or not hmac.compare_digest(
                str(row["lease_token_hash"] or ""),
                _lease_digest(lease.lease_token),
            )
        ):
            raise ValueError("promotion_task_lease_conflict")
        return self._row(row)


__all__ = [
    "PromotionTask",
    "PromotionTaskLease",
    "PromotionTaskStore",
    "RISK_TIERS",
    "TASK_STATUSES",
    "ensure_promotion_task_schema",
    "risk_tier_for_proposal",
]
