"""Durable, project-scoped leases for asynchronous derived memory work.

Canonical memories remain the source of truth.  This module records only the
derived work that follows a canonical write, such as embedding, structured
chunking, governed fusion, or a LanceDB projection.  It intentionally has no
provider dependency so that lease ownership survives process restarts and a
cloud call never runs while SQLite holds a write transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_STATUSES = frozenset({"pending", "retry_wait", "leased", "completed", "dead", "cancelled"})
_VISIBLE_STATUSES = tuple(sorted(_STATUSES))
_VISIBILITIES = frozenset({"private", "project", "shared", "global"})
_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 15 * 60
_DEFAULT_MAX_ATTEMPTS = 4
_MAX_ATTEMPTS = 32
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_ACCELERATOR_AUDIT_TASK_KINDS = frozenset(
    {
        "embedding-reconcile",
        "vector-relations",
        "semantic-dedupe",
        "conflict-risk",
        "preclassification",
        "scoring-evidence",
        "scheduler",
    }
)
_ACCELERATOR_AUDIT_EVENTS = frozenset({"admission", "scheduler"})
_ACCELERATOR_AUDIT_DECISIONS = frozenset({"denied", "deferred"})


class DerivedWorkError(RuntimeError):
    """A derived-work error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DerivedWorkConflictError(DerivedWorkError):
    """A requested lifecycle transition conflicts with durable state."""


class DerivedWorkNotFoundError(DerivedWorkError):
    """No job is visible in the requested project scope."""


@dataclass(frozen=True)
class DerivedWorkJob:
    """A detached, non-secret snapshot of one durable derived-work job."""

    job_id: str
    project_id: str
    visibility: str
    config_revision: str
    job_kind: str
    provider_identity: str
    subject_id: str
    subject_hash: str
    status: str
    priority: int
    attempt_count: int
    max_attempts: int
    fencing_generation: int
    created_at: str
    updated_at: str
    not_before_at: str
    lease_expires_at: str | None
    completed_at: str | None
    failure_code: str | None
    payload: Mapping[str, object] = field(repr=False)
    result: Mapping[str, object] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, object]:
        """Return a detached JSON-compatible representation."""

        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "visibility": self.visibility,
            "config_revision": self.config_revision,
            "job_kind": self.job_kind,
            "provider_identity": self.provider_identity,
            "subject_id": self.subject_id,
            "subject_hash": self.subject_hash,
            "status": self.status,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "fencing_generation": self.fencing_generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "not_before_at": self.not_before_at,
            "lease_expires_at": self.lease_expires_at,
            "completed_at": self.completed_at,
            "failure_code": self.failure_code,
            "payload": _json_copy(self.payload),
            "result": None if self.result is None else _json_copy(self.result),
        }


@dataclass(frozen=True)
class DerivedWorkCreateResult:
    """The durable job selected by an idempotent enqueue operation."""

    job: DerivedWorkJob
    created: bool

    @property
    def reused(self) -> bool:
        return not self.created


@dataclass(frozen=True)
class DerivedWorkLease:
    """A one-time lease capability.  Only its SHA-256 hash is persisted."""

    job: DerivedWorkJob
    lease_token: str = field(repr=False)


@dataclass(frozen=True)
class DerivedWorkClaimDecision:
    """A bounded worker-claim decision with an observable deferral reason.

    The lease itself remains the only capability that can transition a job.
    ``reason`` is deliberately a stable non-sensitive code for scheduling
    diagnostics; it never includes a project identifier, payload, or provider
    response.
    """

    lease: DerivedWorkLease | None
    reason: str

    @property
    def claimed(self) -> bool:
        return self.lease is not None


class DerivedWorkStore:
    """SQLite lease state for project-isolated derived work.

    The caller may run many local workers, but every ownership decision uses a
    short ``BEGIN IMMEDIATE`` transaction and completion requires both the
    opaque lease token and the fencing generation.  The store deliberately
    offers at-least-once provider work and exactly-once local state transition.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        default_lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_lease_seconds: int = _MAX_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        if not str(self._db_path) or str(self._db_path) == ":memory:":
            raise DerivedWorkError("derived_work_db_path_invalid")
        self._busy_timeout_ms = _bounded_int(
            busy_timeout_ms,
            minimum=1,
            maximum=120_000,
            code="derived_work_busy_timeout_invalid",
        )
        self._max_lease_seconds = _bounded_int(
            max_lease_seconds,
            minimum=1,
            maximum=_MAX_LEASE_SECONDS,
            code="derived_work_max_lease_invalid",
        )
        self._default_lease_seconds = _bounded_int(
            default_lease_seconds,
            minimum=1,
            maximum=self._max_lease_seconds,
            code="derived_work_lease_seconds_invalid",
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def enqueue(
        self,
        *,
        project_id: str,
        visibility: str,
        config_revision: str,
        job_kind: str,
        provider_identity: str,
        subject_id: str,
        subject_hash: str,
        dedupe_key: str,
        payload: Mapping[str, object],
        priority: int = 0,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        not_before_at: datetime | None = None,
        max_active_jobs: int | None = None,
    ) -> DerivedWorkCreateResult:
        """Create or return a project-scoped idempotent derived-work job."""

        values = self._validated_enqueue_values(
            project_id=project_id,
            visibility=visibility,
            config_revision=config_revision,
            job_kind=job_kind,
            provider_identity=provider_identity,
            subject_id=subject_id,
            subject_hash=subject_hash,
            dedupe_key=dedupe_key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            not_before_at=not_before_at,
            max_active_jobs=max_active_jobs,
        )
        now = _utc_text(self._now())
        with self._write_transaction() as conn:
            return self._enqueue_in_transaction(conn, values=values, now=now)

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        **kwargs: object,
    ) -> DerivedWorkCreateResult:
        """Enqueue a receipt inside a caller-owned canonical SQLite transaction.

        This is the integration point for a future memory mutation path: the
        canonical row and its derived-work receipt either commit together or
        roll back together.  The caller owns transaction boundaries and must
        not perform provider work until after committing.
        """

        if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
            raise DerivedWorkError("derived_work_transaction_required")
        normalized_kwargs = dict(kwargs)
        normalized_kwargs.setdefault("priority", 0)
        normalized_kwargs.setdefault("max_attempts", _DEFAULT_MAX_ATTEMPTS)
        normalized_kwargs.setdefault("not_before_at", None)
        normalized_kwargs.setdefault("max_active_jobs", None)
        values = self._validated_enqueue_values(**normalized_kwargs)
        with _sqlite_rows(connection):
            return self._enqueue_in_transaction(
                connection,
                values=values,
                now=_utc_text(self._now()),
            )

    def enqueue_accelerator(
        self,
        *,
        project_id: str,
        visibility: str,
        config_revision: str,
        job_kind: str,
        provider_identity: str,
        subject_id: str,
        subject_hash: str,
        dedupe_key: str,
        payload: Mapping[str, object],
        priority: int,
        max_attempts: int,
        max_queue_depth: int,
        max_daily_tasks: int,
    ) -> DerivedWorkCreateResult:
        """Enqueue one background job with SQLite-enforced global budgets.

        This is intentionally narrower than :meth:`enqueue`: the queue and
        daily-admission checks execute in the same ``BEGIN IMMEDIATE``
        transaction as the idempotent job creation.  A concurrent caller
        therefore cannot observe spare capacity and then exceed it.  The
        counter tracks newly admitted jobs rather than executions, so a retry
        or idempotent replay cannot consume the daily allowance twice.
        """

        bounded_queue = _bounded_int(
            max_queue_depth,
            minimum=1,
            maximum=1_000_000,
            code="derived_work_accelerator_queue_limit_invalid",
        )
        bounded_daily = _bounded_int(
            max_daily_tasks,
            minimum=1,
            maximum=1_000_000,
            code="derived_work_accelerator_daily_limit_invalid",
        )
        values = self._validated_enqueue_values(
            project_id=project_id,
            visibility=visibility,
            config_revision=config_revision,
            job_kind=job_kind,
            provider_identity=provider_identity,
            subject_id=subject_id,
            subject_hash=subject_hash,
            dedupe_key=dedupe_key,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            not_before_at=None,
            max_active_jobs=None,
        )
        now = self._now()
        now_text = _utc_text(now)
        day = now.date().isoformat()
        with self._write_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM derived_work_jobs WHERE project_id = ? AND dedupe_key_hash = ?",
                (values["project_id"], values["dedupe_key_hash"]),
            ).fetchone()
            if existing is not None:
                return DerivedWorkCreateResult(job=_row_to_job(existing), created=False)
            queued = conn.execute(
                "SELECT COUNT(*) FROM derived_work_jobs WHERE job_kind = ? "
                "AND status IN ('pending', 'retry_wait')",
                (values["job_kind"],),
            ).fetchone()[0]
            if int(queued) >= bounded_queue:
                raise DerivedWorkConflictError("accelerator_queue_budget_exhausted")
            usage = conn.execute(
                "SELECT admitted_count FROM derived_work_daily_admissions "
                "WHERE job_kind = ? AND day_utc = ?",
                (values["job_kind"], day),
            ).fetchone()
            if usage is not None and int(usage["admitted_count"]) >= bounded_daily:
                raise DerivedWorkConflictError("accelerator_daily_budget_exhausted")
            created = self._enqueue_in_transaction(conn, values=values, now=now_text)
            if not created.created:
                return created
            conn.execute(
                """
                INSERT INTO derived_work_daily_admissions (job_kind, day_utc, admitted_count)
                VALUES (?, ?, 1)
                ON CONFLICT(job_kind, day_utc) DO UPDATE
                SET admitted_count = admitted_count + 1
                """,
                (values["job_kind"], day),
            )
            return created

    def record_accelerator_audit_event(
        self,
        *,
        event: str,
        task_kind: str,
        decision: str,
        reason: str,
    ) -> bool:
        """Persist one bounded, daily-deduplicated accelerator decision.

        This is an audit ledger, not another work queue: it deliberately
        records no project, subject, provider, payload, result, or lease data.
        Its explicit node-governance schema migration creates the table; this
        runtime method only writes an already-authorized table. The daily
        uniqueness key prevents a disabled or resource-constrained worker loop
        from growing SQLite indefinitely while retaining durable proof that
        the policy gate made a decision.
        """

        normalized_event = _accelerator_audit_value(
            event,
            "derived_work_accelerator_audit_event_invalid",
            _ACCELERATOR_AUDIT_EVENTS,
        )
        normalized_task_kind = _accelerator_audit_value(
            task_kind,
            "derived_work_accelerator_audit_task_kind_invalid",
            _ACCELERATOR_AUDIT_TASK_KINDS,
        )
        normalized_decision = _accelerator_audit_value(
            decision,
            "derived_work_accelerator_audit_decision_invalid",
            _ACCELERATOR_AUDIT_DECISIONS,
        )
        normalized_reason = _identifier(
            reason,
            "derived_work_accelerator_audit_reason_invalid",
        )
        now = self._now()
        try:
            with self._read_connection() as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'derived_work_accelerator_audit_events'"
                ).fetchone()
        except sqlite3.Error:
            raise DerivedWorkError("derived_work_accelerator_audit_unavailable") from None
        if table is None:
            raise DerivedWorkError("derived_work_accelerator_audit_schema_missing")
        try:
            with self._write_transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO derived_work_accelerator_audit_events (
                        event_id, event_kind, task_kind, decision, reason_code,
                        day_utc, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "dwae_" + uuid.uuid4().hex,
                        normalized_event,
                        normalized_task_kind,
                        normalized_decision,
                        normalized_reason,
                        now.date().isoformat(),
                        _utc_text(now),
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            raise DerivedWorkError("derived_work_accelerator_audit_unavailable") from None

    def get(self, *, job_id: str, project_id: str) -> DerivedWorkJob:
        """Return the project-authorized durable job snapshot."""

        normalized_project = _required_text(project_id, "derived_work_project_id_required")
        with self._read_connection() as conn:
            row = _select_job(conn, str(job_id), normalized_project)
            return _row_to_job(row)

    def claim(
        self,
        *,
        job_id: str,
        project_id: str,
        lease_seconds: int | None = None,
    ) -> DerivedWorkLease:
        """Claim one job, recovering its own expired lease when applicable."""

        normalized_project = _required_text(project_id, "derived_work_project_id_required")
        lease_for = self._lease_duration(lease_seconds)
        now = self._now()
        now_text = _utc_text(now)
        with self._write_transaction() as conn:
            self._recover_expired_in_transaction(
                conn, project_id=normalized_project, now_text=now_text
            )
            row = _select_job(conn, str(job_id), normalized_project)
            if row["status"] not in {"pending", "retry_wait"}:
                raise DerivedWorkConflictError("derived_work_not_claimable")
            if row["not_before_at"] > now_text:
                raise DerivedWorkConflictError("derived_work_not_due")
            return self._claim_row(conn, row, now=now, lease_seconds=lease_for)

    def claim_next(
        self,
        *,
        project_id: str,
        job_kind: str | None = None,
        lease_seconds: int | None = None,
    ) -> DerivedWorkLease | None:
        """Atomically claim the next due job without crossing project scope."""

        normalized_project = _required_text(project_id, "derived_work_project_id_required")
        normalized_kind = (
            None if job_kind is None else _identifier(job_kind, "derived_work_kind_invalid")
        )
        lease_for = self._lease_duration(lease_seconds)
        now = self._now()
        now_text = _utc_text(now)
        with self._write_transaction() as conn:
            self._recover_expired_in_transaction(
                conn, project_id=normalized_project, now_text=now_text
            )
            clauses = [
                "project_id = ?",
                "status IN ('pending', 'retry_wait')",
                "not_before_at <= ?",
            ]
            values: list[object] = [normalized_project, now_text]
            if normalized_kind is not None:
                clauses.append("job_kind = ?")
                values.append(normalized_kind)
            row = conn.execute(
                "SELECT * FROM derived_work_jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY priority DESC, created_at, job_id LIMIT 1",
                tuple(values),
            ).fetchone()
            return (
                None
                if row is None
                else self._claim_row(conn, row, now=now, lease_seconds=lease_for)
            )

    def claim_next_accelerator(
        self,
        *,
        project_id: str,
        job_kind: str,
        max_concurrency: int,
        foreground_priority_floor: int,
        lease_seconds: int | None = None,
    ) -> DerivedWorkClaimDecision:
        """Claim bounded background work without overtaking foreground work.

        The global foreground and accelerator-lease checks happen in the same
        SQLite transaction as the lease acquisition.  This makes the resource
        concurrency budget durable across workers and process restarts.  It
        deliberately blocks new accelerator work whenever a high-priority
        foreground job is queued *or* leased anywhere in the shared store.
        """

        normalized_project = _required_text(project_id, "derived_work_project_id_required")
        normalized_kind = _identifier(job_kind, "derived_work_kind_invalid")
        bounded_concurrency = _bounded_int(
            max_concurrency,
            minimum=1,
            maximum=1_000_000,
            code="derived_work_accelerator_concurrency_limit_invalid",
        )
        priority_floor = _bounded_int(
            foreground_priority_floor,
            minimum=-1_000,
            maximum=1_000,
            code="derived_work_accelerator_foreground_priority_invalid",
        )
        lease_for = self._lease_duration(lease_seconds)
        now = self._now()
        now_text = _utc_text(now)
        with self._write_transaction() as conn:
            self._recover_expired_in_transaction(conn, project_id=None, now_text=now_text)
            foreground = conn.execute(
                "SELECT 1 FROM derived_work_jobs WHERE job_kind != ? "
                "AND status IN ('pending', 'retry_wait', 'leased') AND priority >= ? LIMIT 1",
                (normalized_kind, priority_floor),
            ).fetchone()
            if foreground is not None:
                return DerivedWorkClaimDecision(None, "accelerator_foreground_work_pending")
            active = conn.execute(
                "SELECT COUNT(*) FROM derived_work_jobs WHERE job_kind = ? AND status = 'leased'",
                (normalized_kind,),
            ).fetchone()[0]
            if int(active) >= bounded_concurrency:
                return DerivedWorkClaimDecision(None, "accelerator_concurrency_budget_exhausted")
            row = conn.execute(
                """
                SELECT * FROM derived_work_jobs
                WHERE project_id = ? AND job_kind = ?
                  AND status IN ('pending', 'retry_wait') AND not_before_at <= ?
                ORDER BY priority DESC, created_at, job_id
                LIMIT 1
                """,
                (normalized_project, normalized_kind, now_text),
            ).fetchone()
            if row is None:
                return DerivedWorkClaimDecision(None, "accelerator_no_due_work")
            return DerivedWorkClaimDecision(
                self._claim_row(conn, row, now=now, lease_seconds=lease_for),
                "accelerator_claimed",
            )

    def claim_batch(
        self,
        *,
        limit: int,
        project_id: str | None = None,
        visibility: str | None = None,
        config_revision: str | None = None,
        job_kind: str | None = None,
        provider_identity: str | None = None,
        min_batch_size: int = 1,
        max_wait_seconds: float = 0.0,
        lease_seconds: int | None = None,
    ) -> tuple[DerivedWorkLease, ...]:
        """Claim one ready partition without mixing project or provider state.

        Partition selection and every row transition happen inside one short
        write transaction.  Network or model work must begin only after this
        method returns.
        """

        bounded_limit = _bounded_int(
            limit,
            minimum=1,
            maximum=1_000,
            code="derived_work_batch_limit_invalid",
        )
        bounded_minimum = _bounded_int(
            min_batch_size,
            minimum=1,
            maximum=bounded_limit,
            code="derived_work_batch_minimum_invalid",
        )
        bounded_wait = _bounded_float(
            max_wait_seconds,
            minimum=0.0,
            maximum=24 * 60 * 60,
            code="derived_work_batch_wait_invalid",
        )
        normalized_project = (
            None
            if project_id is None
            else _required_text(project_id, "derived_work_project_id_required")
        )
        normalized_visibility = None
        if visibility is not None:
            normalized_visibility = str(visibility or "").strip().casefold()
            if normalized_visibility not in _VISIBILITIES:
                raise DerivedWorkError("derived_work_visibility_invalid")
        normalized_revision = (
            None
            if config_revision is None
            else _required_text(
                config_revision,
                "derived_work_config_revision_required",
            )
        )
        normalized_kind = (
            None if job_kind is None else _identifier(job_kind, "derived_work_kind_invalid")
        )
        normalized_provider = (
            None
            if provider_identity is None
            else _required_text(
                provider_identity,
                "derived_work_provider_identity_required",
            )
        )
        lease_for = self._lease_duration(lease_seconds)
        now = self._now()
        now_text = _utc_text(now)
        wait_cutoff = _utc_text(now - timedelta(seconds=bounded_wait))
        with self._write_transaction() as conn:
            self._recover_expired_in_transaction(
                conn,
                project_id=normalized_project,
                now_text=now_text,
            )
            clauses = [
                "status IN ('pending', 'retry_wait')",
                "not_before_at <= ?",
            ]
            values: list[object] = [now_text]
            for column, value in (
                ("project_id", normalized_project),
                ("visibility", normalized_visibility),
                ("config_revision", normalized_revision),
                ("job_kind", normalized_kind),
                ("provider_identity", normalized_provider),
            ):
                if value is not None:
                    clauses.append(f"{column} = ?")
                    values.append(value)
            partition = conn.execute(
                """
                SELECT project_id, visibility, config_revision, job_kind,
                       provider_identity, COUNT(*) AS ready_count,
                       MAX(priority) AS highest_priority, MIN(created_at) AS oldest_created_at
                FROM derived_work_jobs
                WHERE """
                + " AND ".join(clauses)
                + """
                GROUP BY project_id, visibility, config_revision, job_kind, provider_identity
                HAVING COUNT(*) >= ? OR MIN(created_at) <= ?
                ORDER BY highest_priority DESC, oldest_created_at,
                         project_id, visibility, config_revision, job_kind, provider_identity
                LIMIT 1
                """,
                (*values, bounded_minimum, wait_cutoff),
            ).fetchone()
            if partition is None:
                return ()
            rows = conn.execute(
                """
                SELECT * FROM derived_work_jobs
                WHERE project_id = ? AND visibility = ? AND config_revision = ?
                  AND job_kind = ? AND provider_identity = ?
                  AND status IN ('pending', 'retry_wait') AND not_before_at <= ?
                ORDER BY priority DESC, created_at, job_id
                LIMIT ?
                """,
                (
                    partition["project_id"],
                    partition["visibility"],
                    partition["config_revision"],
                    partition["job_kind"],
                    partition["provider_identity"],
                    now_text,
                    bounded_limit,
                ),
            ).fetchall()
            return tuple(
                self._claim_row(conn, row, now=now, lease_seconds=lease_for) for row in rows
            )

    def renew_lease(
        self,
        *,
        job_id: str,
        project_id: str,
        lease_token: str,
        fencing_generation: int,
        lease_seconds: int | None = None,
    ) -> DerivedWorkJob:
        """Renew one valid lease without changing its opaque capability."""

        lease_for = self._lease_duration(lease_seconds)
        now = self._now()
        now_text = _utc_text(now)
        with self._write_transaction() as conn:
            row = _select_job(
                conn, str(job_id), _required_text(project_id, "derived_work_project_id_required")
            )
            self._require_valid_lease(
                row,
                lease_token=lease_token,
                fencing_generation=fencing_generation,
                now_text=now_text,
            )
            renewed = _utc_text(now + timedelta(seconds=lease_for))
            cursor = conn.execute(
                """
                UPDATE derived_work_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND project_id = ? AND status = 'leased'
                  AND lease_token_hash = ? AND fencing_generation = ? AND lease_expires_at > ?
                """,
                (
                    renewed,
                    now_text,
                    row["job_id"],
                    row["project_id"],
                    _token_hash(lease_token),
                    fencing_generation,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise DerivedWorkConflictError("derived_work_lease_renewal_conflict")
            return _row_to_job(_select_job(conn, row["job_id"], row["project_id"]))

    def complete(
        self,
        *,
        job_id: str,
        project_id: str,
        lease_token: str,
        fencing_generation: int,
        result: Mapping[str, object],
    ) -> DerivedWorkJob:
        """Persist a successful local side effect using token and fencing CAS."""

        result_json = _json_object(result, "derived_work_result_invalid", _MAX_RESULT_BYTES)
        with self._write_transaction() as conn:
            return self._complete_in_transaction(
                conn,
                job_id=job_id,
                project_id=project_id,
                lease_token=lease_token,
                fencing_generation=fencing_generation,
                result_json=result_json,
                now_text=_utc_text(self._now()),
            )

    def complete_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        project_id: str,
        lease_token: str,
        fencing_generation: int,
        result: Mapping[str, object],
    ) -> DerivedWorkJob:
        """Complete one lease inside a caller-owned local-side-effect transaction."""

        if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
            raise DerivedWorkError("derived_work_transaction_required")
        with _sqlite_rows(connection):
            return self._complete_in_transaction(
                connection,
                job_id=job_id,
                project_id=project_id,
                lease_token=lease_token,
                fencing_generation=fencing_generation,
                result_json=_json_object(result, "derived_work_result_invalid", _MAX_RESULT_BYTES),
                now_text=_utc_text(self._now()),
            )

    def fail(
        self,
        *,
        job_id: str,
        project_id: str,
        lease_token: str,
        fencing_generation: int,
        failure_code: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
    ) -> DerivedWorkJob:
        """Record a classified failure and schedule bounded retry or a dead job."""

        normalized_failure = _identifier(failure_code, "derived_work_failure_code_invalid")
        delay = _bounded_int(
            retry_delay_seconds,
            minimum=0,
            maximum=24 * 60 * 60,
            code="derived_work_retry_delay_invalid",
        )
        now = self._now()
        now_text = _utc_text(now)
        with self._write_transaction() as conn:
            row = _select_job(
                conn, str(job_id), _required_text(project_id, "derived_work_project_id_required")
            )
            self._require_valid_lease(
                row,
                lease_token=lease_token,
                fencing_generation=fencing_generation,
                now_text=now_text,
            )
            attempts = int(row["attempt_count"]) + 1
            will_retry = retryable and attempts < int(row["max_attempts"])
            status = "retry_wait" if will_retry else "dead"
            not_before = _utc_text(now + timedelta(seconds=delay)) if will_retry else now_text
            cursor = conn.execute(
                """
                UPDATE derived_work_jobs
                SET status = ?, updated_at = ?, not_before_at = ?, attempt_count = ?,
                    failure_code = ?, lease_token_hash = NULL, lease_expires_at = NULL
                WHERE job_id = ? AND project_id = ? AND status = 'leased'
                  AND lease_token_hash = ? AND fencing_generation = ? AND lease_expires_at > ?
                """,
                (
                    status,
                    now_text,
                    not_before,
                    attempts,
                    normalized_failure,
                    row["job_id"],
                    row["project_id"],
                    _token_hash(lease_token),
                    fencing_generation,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise DerivedWorkConflictError("derived_work_failure_conflict")
            _finish_attempt(
                conn,
                job_id=row["job_id"],
                fencing_generation=fencing_generation,
                finished_at=now_text,
                disposition=status,
                failure_code=normalized_failure,
            )
            return _row_to_job(_select_job(conn, row["job_id"], row["project_id"]))

    def recover_expired(self, *, project_id: str | None = None) -> int:
        """Return expired leases to pending without discarding their work."""

        normalized_project = (
            None
            if project_id is None
            else _required_text(project_id, "derived_work_project_id_required")
        )
        now_text = _utc_text(self._now())
        with self._write_transaction() as conn:
            return self._recover_expired_in_transaction(
                conn,
                project_id=normalized_project,
                now_text=now_text,
            )

    def stats(
        self,
        *,
        project_id: str | None = None,
        job_kind: str | None = None,
    ) -> dict[str, int]:
        """Return an explicit zero-filled status snapshot for a durable scope.

        ``job_kind`` keeps observability adapters from reporting unrelated
        derived jobs as node work.  Both selectors remain optional only for
        server-owned aggregate diagnostics; callers that expose work to a user
        must continue passing their authenticated ``project_id``.
        """

        normalized_project = (
            None
            if project_id is None
            else _required_text(project_id, "derived_work_project_id_required")
        )
        normalized_kind = (
            None if job_kind is None else _identifier(job_kind, "derived_work_kind_invalid")
        )
        clauses: list[str] = []
        values: list[object] = []
        if normalized_project is not None:
            clauses.append("project_id = ?")
            values.append(normalized_project)
        if normalized_kind is not None:
            clauses.append("job_kind = ?")
            values.append(normalized_kind)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._read_connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM derived_work_jobs "
                + where
                + " GROUP BY status",
                tuple(values),
            ).fetchall()
        result = dict.fromkeys(_VISIBLE_STATUSES, 0)
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def daily_admissions(self, *, job_kind: str) -> int:
        """Return UTC-day durable admissions for one budgeted work kind."""

        normalized_kind = _identifier(job_kind, "derived_work_kind_invalid")
        day = self._now().date().isoformat()
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT admitted_count FROM derived_work_daily_admissions "
                "WHERE job_kind = ? AND day_utc = ?",
                (normalized_kind, day),
            ).fetchone()
        return 0 if row is None else int(row["admitted_count"])

    def status(self, *, project_id: str) -> dict[str, int | str | None]:
        """Return project-scoped queue depth, age, and lifecycle counters."""

        normalized_project = _required_text(project_id, "derived_work_project_id_required")
        counts = self.stats(project_id=normalized_project)
        with self._read_connection() as conn:
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM derived_work_jobs "
                "WHERE project_id = ? AND status IN ('pending', 'retry_wait')",
                (normalized_project,),
            ).fetchone()[0]
        age_seconds: int | None = None
        if isinstance(oldest, str) and oldest:
            try:
                created = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                age_seconds = max(0, int((self._now() - created).total_seconds()))
            except (TypeError, ValueError):
                oldest = None
        return {
            **counts,
            "queue_depth": counts["pending"] + counts["retry_wait"],
            "oldest_queued_at": oldest,
            "oldest_queued_age_seconds": age_seconds,
        }

    def _validated_enqueue_values(self, **values: object) -> dict[str, object]:
        project_id = _required_text(values["project_id"], "derived_work_project_id_required")
        visibility = str(values["visibility"] or "").strip().casefold()
        if visibility not in _VISIBILITIES:
            raise DerivedWorkError("derived_work_visibility_invalid")
        config_revision = _required_text(
            values["config_revision"], "derived_work_config_revision_required"
        )
        job_kind = _identifier(values["job_kind"], "derived_work_kind_invalid")
        provider_identity = _required_text(
            values["provider_identity"], "derived_work_provider_identity_required"
        )
        subject_id = _required_text(values["subject_id"], "derived_work_subject_id_required")
        subject_hash = str(values["subject_hash"] or "").strip().casefold()
        if not _SHA256_RE.fullmatch(subject_hash):
            raise DerivedWorkError("derived_work_subject_hash_invalid")
        dedupe_key = _required_text(values["dedupe_key"], "derived_work_dedupe_key_required")
        payload_json = _json_object(
            values["payload"], "derived_work_payload_invalid", _MAX_PAYLOAD_BYTES
        )
        priority = _bounded_int(
            values["priority"],
            minimum=-1_000,
            maximum=1_000,
            code="derived_work_priority_invalid",
        )
        max_attempts = _bounded_int(
            values["max_attempts"],
            minimum=1,
            maximum=_MAX_ATTEMPTS,
            code="derived_work_max_attempts_invalid",
        )
        not_before = values["not_before_at"]
        if not_before is None:
            not_before_text = _utc_text(self._now())
        elif isinstance(not_before, datetime):
            not_before_text = _utc_text(_require_utc_datetime(not_before))
        else:
            raise DerivedWorkError("derived_work_not_before_invalid")
        max_active = values["max_active_jobs"]
        if max_active is not None:
            max_active = _bounded_int(
                max_active,
                minimum=1,
                maximum=1_000_000,
                code="derived_work_queue_limit_invalid",
            )
        return {
            "project_id": project_id,
            "visibility": visibility,
            "config_revision": config_revision,
            "job_kind": job_kind,
            "provider_identity": provider_identity,
            "subject_id": subject_id,
            "subject_hash": subject_hash,
            "dedupe_key_hash": _hash_text(dedupe_key),
            "payload_json": payload_json,
            "payload_bytes": len(payload_json.encode()),
            "priority": priority,
            "max_attempts": max_attempts,
            "not_before_at": not_before_text,
            "max_active_jobs": max_active,
        }

    def _complete_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        project_id: str,
        lease_token: str,
        fencing_generation: int,
        result_json: str,
        now_text: str,
    ) -> DerivedWorkJob:
        row = _select_job(
            conn,
            str(job_id),
            _required_text(project_id, "derived_work_project_id_required"),
        )
        self._require_valid_lease(
            row,
            lease_token=lease_token,
            fencing_generation=fencing_generation,
            now_text=now_text,
        )
        cursor = conn.execute(
            """
            UPDATE derived_work_jobs
            SET status = 'completed', updated_at = ?, completed_at = ?, result_json = ?,
                result_bytes = ?, lease_token_hash = NULL, lease_expires_at = NULL,
                failure_code = NULL
            WHERE job_id = ? AND project_id = ? AND status = 'leased'
              AND lease_token_hash = ? AND fencing_generation = ? AND lease_expires_at > ?
            """,
            (
                now_text,
                now_text,
                result_json,
                len(result_json.encode()),
                row["job_id"],
                row["project_id"],
                _token_hash(lease_token),
                fencing_generation,
                now_text,
            ),
        )
        if cursor.rowcount != 1:
            raise DerivedWorkConflictError("derived_work_completion_conflict")
        _finish_attempt(
            conn,
            job_id=row["job_id"],
            fencing_generation=fencing_generation,
            finished_at=now_text,
            disposition="completed",
        )
        return _row_to_job(_select_job(conn, row["job_id"], row["project_id"]))

    def _claim_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> DerivedWorkLease:
        token = secrets.token_urlsafe(32)
        now_text = _utc_text(now)
        expires = _utc_text(now + timedelta(seconds=lease_seconds))
        fencing_generation = int(row["fencing_generation"]) + 1
        cursor = conn.execute(
            """
            UPDATE derived_work_jobs
            SET status = 'leased', updated_at = ?, lease_token_hash = ?, lease_expires_at = ?,
                fencing_generation = ?
            WHERE job_id = ? AND project_id = ? AND status IN ('pending', 'retry_wait')
              AND not_before_at <= ?
            """,
            (
                now_text,
                _token_hash(token),
                expires,
                fencing_generation,
                row["job_id"],
                row["project_id"],
                now_text,
            ),
        )
        if cursor.rowcount != 1:
            raise DerivedWorkConflictError("derived_work_claim_conflict")
        conn.execute(
            """
            INSERT INTO derived_work_attempts (
                attempt_id, job_id, project_id, fencing_generation, claimed_at,
                lease_expires_at, finished_at, disposition, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'leased', NULL)
            """,
            (
                "dwa_" + uuid.uuid4().hex,
                row["job_id"],
                row["project_id"],
                fencing_generation,
                now_text,
                expires,
            ),
        )
        claimed = _row_to_job(_select_job(conn, row["job_id"], row["project_id"]))
        return DerivedWorkLease(job=claimed, lease_token=token)

    @staticmethod
    def _enqueue_in_transaction(
        conn: sqlite3.Connection,
        *,
        values: Mapping[str, object],
        now: str,
    ) -> DerivedWorkCreateResult:
        row = conn.execute(
            "SELECT * FROM derived_work_jobs WHERE project_id = ? AND dedupe_key_hash = ?",
            (values["project_id"], values["dedupe_key_hash"]),
        ).fetchone()
        if row is not None:
            return DerivedWorkCreateResult(job=_row_to_job(row), created=False)
        max_active_jobs = values.get("max_active_jobs")
        if max_active_jobs is not None:
            active = conn.execute(
                "SELECT COUNT(*) FROM derived_work_jobs WHERE project_id = ? "
                "AND status IN ('pending', 'retry_wait', 'leased')",
                (values["project_id"],),
            ).fetchone()[0]
            if int(active) >= int(max_active_jobs):
                raise DerivedWorkConflictError("derived_work_queue_full")
        job_id = "dwj_" + uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO derived_work_jobs (
                job_id, project_id, visibility, config_revision, job_kind,
                provider_identity, subject_id, subject_hash, dedupe_key_hash,
                payload_json, payload_bytes, status, priority, attempt_count,
                max_attempts, fencing_generation, created_at, updated_at,
                not_before_at, lease_token_hash, lease_expires_at, completed_at,
                result_json, result_bytes, failure_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, 0, ?, ?, ?,
                      NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                job_id,
                values["project_id"],
                values["visibility"],
                values["config_revision"],
                values["job_kind"],
                values["provider_identity"],
                values["subject_id"],
                values["subject_hash"],
                values["dedupe_key_hash"],
                values["payload_json"],
                values["payload_bytes"],
                values["priority"],
                values["max_attempts"],
                now,
                now,
                values["not_before_at"],
            ),
        )
        row = _select_job(conn, job_id, str(values["project_id"]))
        return DerivedWorkCreateResult(job=_row_to_job(row), created=True)

    def _recover_expired_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str | None,
        now_text: str,
    ) -> int:
        clauses = ["status = 'leased'", "lease_expires_at <= ?"]
        values: list[object] = [now_text]
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        expired = conn.execute(
            "SELECT job_id, project_id, fencing_generation, attempt_count, max_attempts "
            "FROM derived_work_jobs WHERE " + " AND ".join(clauses),
            tuple(values),
        ).fetchall()
        if not expired:
            return 0
        for row in expired:
            attempts = int(row["attempt_count"]) + 1
            status = "dead" if attempts >= int(row["max_attempts"]) else "pending"
            cursor = conn.execute(
                "UPDATE derived_work_jobs SET status = ?, updated_at = ?, "
                "not_before_at = ?, attempt_count = ?, failure_code = ?, "
                "lease_token_hash = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND project_id = ? AND status = 'leased' "
                "AND fencing_generation = ? AND lease_expires_at <= ?",
                (
                    status,
                    now_text,
                    now_text,
                    attempts,
                    "lease_expired",
                    row["job_id"],
                    row["project_id"],
                    row["fencing_generation"],
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise DerivedWorkConflictError("derived_work_recovery_conflict")
            _finish_attempt(
                conn,
                job_id=str(row["job_id"]),
                fencing_generation=int(row["fencing_generation"]),
                finished_at=now_text,
                disposition="dead" if status == "dead" else "lease_expired",
                failure_code="lease_expired",
            )
        return len(expired)

    def _require_valid_lease(
        self,
        row: sqlite3.Row,
        *,
        lease_token: str,
        fencing_generation: int,
        now_text: str,
    ) -> None:
        if row["status"] != "leased":
            raise DerivedWorkConflictError("derived_work_not_leased")
        if int(row["fencing_generation"]) != int(fencing_generation):
            raise DerivedWorkConflictError("derived_work_fencing_generation_invalid")
        lease_expires_at = row["lease_expires_at"]
        if not isinstance(lease_expires_at, str) or lease_expires_at <= now_text:
            raise DerivedWorkConflictError("derived_work_lease_expired")
        stored_hash = str(row["lease_token_hash"] or "")
        if not hmac.compare_digest(stored_hash, _token_hash(lease_token)):
            raise DerivedWorkConflictError("derived_work_lease_token_invalid")

    def _lease_duration(self, value: int | None) -> int:
        return _bounded_int(
            self._default_lease_seconds if value is None else value,
            minimum=1,
            maximum=self._max_lease_seconds,
            code="derived_work_lease_seconds_invalid",
        )

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal is None or str(journal[0]).casefold() != "wal":
                raise DerivedWorkError("derived_work_wal_unavailable")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS derived_work_jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('private', 'project', 'shared', 'global')),
                    config_revision TEXT NOT NULL,
                    job_kind TEXT NOT NULL,
                    provider_identity TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_hash TEXT NOT NULL,
                    dedupe_key_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL CHECK(payload_bytes > 0),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'retry_wait', 'leased', 'completed', 'dead', 'cancelled')),
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
                    fencing_generation INTEGER NOT NULL DEFAULT 0 CHECK(fencing_generation >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    not_before_at TEXT NOT NULL,
                    lease_token_hash TEXT,
                    lease_expires_at TEXT,
                    completed_at TEXT,
                    result_json TEXT,
                    result_bytes INTEGER,
                    failure_code TEXT,
                    UNIQUE(project_id, dedupe_key_hash),
                    CHECK(
                        (status = 'leased' AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL)
                        OR (status != 'leased' AND lease_token_hash IS NULL AND lease_expires_at IS NULL)
                    ),
                    CHECK(
                        (status = 'completed' AND completed_at IS NOT NULL AND result_json IS NOT NULL AND result_bytes > 0)
                        OR (status != 'completed' AND completed_at IS NULL AND result_json IS NULL AND result_bytes IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_derived_work_jobs_claim
                ON derived_work_jobs(project_id, status, not_before_at, priority DESC, created_at, job_id);
                CREATE INDEX IF NOT EXISTS idx_derived_work_jobs_scope
                ON derived_work_jobs(project_id, visibility, config_revision, job_kind, provider_identity);
                CREATE INDEX IF NOT EXISTS idx_derived_work_jobs_expiry
                ON derived_work_jobs(status, lease_expires_at);
                CREATE TABLE IF NOT EXISTS derived_work_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES derived_work_jobs(job_id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL,
                    fencing_generation INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    finished_at TEXT,
                    disposition TEXT NOT NULL,
                    failure_code TEXT,
                    UNIQUE(job_id, fencing_generation)
                );
                CREATE INDEX IF NOT EXISTS idx_derived_work_attempts_project
                ON derived_work_attempts(project_id, job_id, fencing_generation);
                CREATE TABLE IF NOT EXISTS derived_work_daily_admissions (
                    job_kind TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    admitted_count INTEGER NOT NULL CHECK(admitted_count >= 0),
                    PRIMARY KEY(job_kind, day_utc)
                );
                """
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _now(self) -> datetime:
        try:
            return _require_utc_datetime(self._clock())
        except DerivedWorkError:
            raise
        except Exception as exc:
            raise DerivedWorkError("derived_work_clock_invalid") from exc


def _select_job(conn: sqlite3.Connection, job_id: str, project_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM derived_work_jobs WHERE job_id = ? AND project_id = ?",
        (job_id, project_id),
    ).fetchone()
    if row is None:
        raise DerivedWorkNotFoundError("derived_work_job_not_found")
    return row


@contextmanager
def _sqlite_rows(connection: sqlite3.Connection) -> Iterator[None]:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        yield
    finally:
        connection.row_factory = previous


def _finish_attempt(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    fencing_generation: int,
    finished_at: str,
    disposition: str,
    failure_code: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE derived_work_attempts
        SET finished_at = ?, disposition = ?, failure_code = ?
        WHERE job_id = ? AND fencing_generation = ? AND finished_at IS NULL
        """,
        (finished_at, disposition, failure_code, job_id, fencing_generation),
    )


def _row_to_job(row: sqlite3.Row) -> DerivedWorkJob:
    payload = _load_json_mapping(row["payload_json"], "derived_work_payload_corrupt")
    result_json = row["result_json"]
    result = (
        None
        if result_json is None
        else _load_json_mapping(result_json, "derived_work_result_corrupt")
    )
    return DerivedWorkJob(
        job_id=str(row["job_id"]),
        project_id=str(row["project_id"]),
        visibility=str(row["visibility"]),
        config_revision=str(row["config_revision"]),
        job_kind=str(row["job_kind"]),
        provider_identity=str(row["provider_identity"]),
        subject_id=str(row["subject_id"]),
        subject_hash=str(row["subject_hash"]),
        status=str(row["status"]),
        priority=int(row["priority"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        fencing_generation=int(row["fencing_generation"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        not_before_at=str(row["not_before_at"]),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
        failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
        payload=payload,
        result=result,
    )


def _required_text(value: object, code: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized.encode()) > 512:
        raise DerivedWorkError(code)
    return normalized


def _identifier(value: object, code: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise DerivedWorkError(code)
    return normalized


def _accelerator_audit_value(value: object, code: str, allowed: frozenset[str]) -> str:
    normalized = _identifier(value, code)
    if normalized not in allowed:
        raise DerivedWorkError(code)
    return normalized


def _bounded_int(value: object, *, minimum: int, maximum: int, code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise DerivedWorkError(code) from None
    if isinstance(value, bool) or parsed < minimum or parsed > maximum:
        raise DerivedWorkError(code)
    return parsed


def _bounded_float(value: object, *, minimum: float, maximum: float, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise DerivedWorkError(code) from None
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise DerivedWorkError(code)
    return parsed


def _json_object(value: object, code: str, max_bytes: int) -> str:
    if not isinstance(value, Mapping):
        raise DerivedWorkError(code)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise DerivedWorkError(code) from None
    if len(serialized.encode()) > max_bytes:
        raise DerivedWorkError(code)
    return serialized


def _load_json_mapping(value: object, code: str) -> Mapping[str, object]:
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        raise DerivedWorkError(code) from None
    if not isinstance(loaded, dict):
        raise DerivedWorkError(code)
    return _json_copy(loaded)


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        raise DerivedWorkError("derived_work_json_copy_invalid") from None
    if not isinstance(copied, dict):
        raise DerivedWorkError("derived_work_json_copy_invalid")
    return copied


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _token_hash(token: str) -> str:
    return _hash_text(_required_text(token, "derived_work_lease_token_required"))


def _require_utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DerivedWorkError("derived_work_clock_invalid")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
