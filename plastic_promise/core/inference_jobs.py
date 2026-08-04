"""Durable, project-scoped rerank jobs for an authenticated gateway.

The store keeps the backend-generated client-local package as the authority.
Callers may fetch that package, but retries never replace it with client-echoed
material.  Lease capabilities are returned once and are persisted only as
SHA-256 hashes.  All state transitions that decide ownership use
``BEGIN IMMEDIATE`` and compare-and-swap predicates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plastic_promise.core.backend_inference import (
    CLIENT_LOCAL_RERANK_CONTRACT,
    CLIENT_LOCAL_SCORING_VERSION,
    RERANK_REQUEST_CONTRACT,
    RERANK_SCORING_VERSION,
    RerankRequestBinding,
)

_ACTIVE_JOB_STATUSES = frozenset({"pending", "leased"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "expired"})
_STATUSES = _ACTIVE_JOB_STATUSES | _TERMINAL_JOB_STATUSES
_ACTIVE_RESERVATION_STATUSES = frozenset({"reserved", "preparing"})
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAILURE_CODE_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,96}\Z")
_MAX_IDENTIFIER_BYTES = 512
_MAX_BINDING_BYTES = 64 * 1024
_DEFAULT_MAX_PACKAGE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
_HARD_MAX_JSON_BYTES = 64 * 1024 * 1024
_DEFAULT_TTL_SECONDS = 15 * 60
_MAX_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 15 * 60
_DEFAULT_MAX_ACTIVE_JOBS = 1_000
_MAX_ACTIVE_JOBS = 100_000
_DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
_MAX_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_RETAINED_ROWS_PER_PROJECT = 1_000_000
_DEFAULT_MAX_RETAINED_JSON_BYTES_PER_PROJECT = 512 * 1024 * 1024
_MAX_RETAINED_JSON_BYTES_PER_PROJECT = 64 * 1024 * 1024 * 1024
_CLEANUP_BATCH_SIZE = 1_000
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 200_000
_MAX_PACKAGE_CANDIDATES = 1_000
_MAX_PACKAGE_QUERY_BYTES = 256 * 1024
_MAX_PACKAGE_TEXT_BYTES = 1024 * 1024
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)
_TARGETS = frozenset({"cloud", "client-local"})

_BINDING_FIELDS = frozenset(
    {
        "contract_version",
        "scoring_version",
        "project_id",
        "request_id",
        "idempotency_key_hash",
        "candidate_set_version",
        "candidate_set_hash",
        "query_hash",
        "input_hash",
        "provider_policy_revision",
        "top_k",
    }
)
_PACKAGE_FIELDS = frozenset(
    {
        "contract_version",
        "scoring_version",
        "project_id",
        "request_id",
        "candidate_set_version",
        "candidate_set_hash",
        "query",
        "query_hash",
        "embedding_identity",
        "embedding_dimension",
        "model_identity",
        "top_k",
        "candidates",
        "package_hash",
    }
)
_CANDIDATE_FIELDS = frozenset({"id", "text", "base_score", "material_sha256", "embedding_sha256"})
_DATACLASS_CANDIDATE_FIELDS = frozenset(
    {"item_id", "text", "base_score", "material_sha256", "embedding_sha256"}
)


class InferenceJobError(RuntimeError):
    """An error with a stable, non-sensitive machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InferenceJobConflictError(InferenceJobError):
    """The requested transition conflicts with durable job state."""


class InferenceJobNotFoundError(InferenceJobError):
    """No job is visible in the authenticated project scope."""


@dataclass(frozen=True)
class InferenceJob:
    job_id: str
    project_id: str
    idempotency_key_hash: str
    input_hash: str
    execution_hash: str
    request_id: str
    target: str
    status: str
    created_at: str
    updated_at: str
    expires_at: str
    lease_expires_at: str | None
    completed_at: str | None
    binding: Mapping[str, object] = field(repr=False)
    package: Mapping[str, object] = field(repr=False)
    request_material: Mapping[str, object] | None = field(default=None, repr=False)
    result: Mapping[str, object] | None = field(default=None, repr=False)
    failure_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a detached JSON-compatible representation."""

        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "idempotency_key_hash": self.idempotency_key_hash,
            "input_hash": self.input_hash,
            "execution_hash": self.execution_hash,
            "request_id": self.request_id,
            "target": self.target,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "lease_expires_at": self.lease_expires_at,
            "completed_at": self.completed_at,
            "binding": _json_copy(self.binding),
            "package": _json_copy(self.package),
            "request_material": (
                None if self.request_material is None else _json_copy(self.request_material)
            ),
            "result": None if self.result is None else _json_copy(self.result),
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True)
class InferenceJobCreateResult:
    job: InferenceJob
    created: bool

    @property
    def reused(self) -> bool:
        return not self.created

    @property
    def disposition(self) -> str:
        return "created" if self.created else "reused"

    def __getattr__(self, name: str) -> object:
        """Allow gateway adapters to treat the result as its durable record."""

        return getattr(self.job, name)


@dataclass(frozen=True)
class InferenceJobLease:
    job: InferenceJob
    lease_token: str = field(repr=False)


@dataclass(frozen=True)
class _Submission:
    project_id: str
    target: str
    binding_data: dict[str, object]
    binding_json: str
    package_data: dict[str, object]
    package_json: str
    request_material_data: dict[str, object] | None
    request_material_json: str | None
    request_material_bytes: int | None
    execution_hash: str
    ttl_seconds: int


class InferenceJobStore:
    """SQLite source of truth for asynchronous rerank jobs."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        default_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_ttl_seconds: int = _MAX_TTL_SECONDS,
        default_lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_lease_seconds: int = _MAX_LEASE_SECONDS,
        max_package_bytes: int = _DEFAULT_MAX_PACKAGE_BYTES,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        max_active_jobs: int = _DEFAULT_MAX_ACTIVE_JOBS,
        retention_seconds: int = _DEFAULT_RETENTION_SECONDS,
        max_retained_rows_per_project: int | None = None,
        max_retained_json_bytes_per_project: int = (_DEFAULT_MAX_RETAINED_JSON_BYTES_PER_PROJECT),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        raw_path = str(db_path)
        if not raw_path or raw_path == ":memory:" or "\x00" in raw_path:
            raise InferenceJobError("inference_job_db_path_invalid")
        self._db_path = Path(raw_path)
        self._busy_timeout_ms = _bounded_integer(
            busy_timeout_ms,
            minimum=1,
            maximum=120_000,
            code="inference_job_busy_timeout_invalid",
        )
        self._max_ttl_seconds = _bounded_integer(
            max_ttl_seconds,
            minimum=1,
            maximum=7 * 24 * 60 * 60,
            code="inference_job_max_ttl_invalid",
        )
        self._default_ttl_seconds = _bounded_integer(
            default_ttl_seconds,
            minimum=1,
            maximum=self._max_ttl_seconds,
            code="inference_job_ttl_invalid",
        )
        self._max_lease_seconds = _bounded_integer(
            max_lease_seconds,
            minimum=1,
            maximum=24 * 60 * 60,
            code="inference_job_max_lease_invalid",
        )
        self._default_lease_seconds = _bounded_integer(
            default_lease_seconds,
            minimum=1,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        self._max_package_bytes = _bounded_integer(
            max_package_bytes,
            minimum=1,
            maximum=_HARD_MAX_JSON_BYTES,
            code="inference_job_max_package_bytes_invalid",
        )
        self._max_result_bytes = _bounded_integer(
            max_result_bytes,
            minimum=1,
            maximum=_HARD_MAX_JSON_BYTES,
            code="inference_job_max_result_bytes_invalid",
        )
        self._max_active_jobs = _bounded_integer(
            max_active_jobs,
            minimum=1,
            maximum=_MAX_ACTIVE_JOBS,
            code="inference_job_max_active_jobs_invalid",
        )
        self._retention_seconds = _bounded_integer(
            retention_seconds,
            minimum=1,
            maximum=_MAX_RETENTION_SECONDS,
            code="inference_job_retention_invalid",
        )
        derived_retained_rows = min(self._max_active_jobs * 4, _MAX_RETAINED_ROWS_PER_PROJECT)
        self._max_retained_rows_per_project = _bounded_integer(
            derived_retained_rows
            if max_retained_rows_per_project is None
            else max_retained_rows_per_project,
            minimum=1,
            maximum=_MAX_RETAINED_ROWS_PER_PROJECT,
            code="inference_job_max_retained_rows_per_project_invalid",
        )
        self._max_retained_json_bytes_per_project = _bounded_integer(
            max_retained_json_bytes_per_project,
            minimum=1,
            maximum=_MAX_RETAINED_JSON_BYTES_PER_PROJECT,
            code="inference_job_max_retained_json_bytes_per_project_invalid",
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()
        self._secure_permissions()

    def create_or_get(
        self,
        binding: RerankRequestBinding | Mapping[str, object],
        package: Mapping[str, object],
        target: str = "client-local",
        request_material: Mapping[str, object] | None = None,
        ttl_seconds: int | None = None,
        *,
        project_id: str | None = None,
    ) -> InferenceJobCreateResult:
        """Create once, or return the original job for an exact retry.

        ``project_id`` must come from the authenticated server context.  The
        package on a retry is validated but never replaces the stored package.
        """

        submission = self._normalize_submission(
            binding=binding,
            package=package,
            project_id=project_id,
            target=target,
            request_material=request_material,
            ttl_seconds=ttl_seconds,
        )
        project_id = submission.project_id

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._maintain_project(connection, now=now, project_id=project_id)
            row = connection.execute(
                "SELECT * FROM inference_rerank_jobs "
                "WHERE project_id = ? AND idempotency_key_hash = ?",
                (project_id, submission.binding_data["idempotency_key_hash"]),
            ).fetchone()
            if row is not None:
                self._refresh_job(connection, job_id=row["job_id"], project_id=project_id, now=now)
                row = self._select_job(connection, row["job_id"], project_id)
                if row["execution_hash"] != submission.execution_hash:
                    connection.commit()
                    raise InferenceJobConflictError("inference_job_idempotency_conflict")
                job = self._row_to_job(row)
                connection.commit()
                return InferenceJobCreateResult(job=job, created=False)

            job = self._insert_job(
                connection,
                submission=submission,
                now=now,
            )
            connection.commit()
            return InferenceJobCreateResult(job=job, created=True)
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def reserve_submission(
        self,
        project_id: str,
        idempotency_key_hash: str,
        request_hash: str,
        target: str,
        ttl_seconds: int | None = None,
    ) -> tuple[str, str | None]:
        """Reserve an idempotency key before provider preparation starts.

        The first caller receives ``("reserved", token)``.  Concurrent callers
        with the same request receive ``("preparing", None)`` and should poll
        the job/reservation rather than invoke an expensive provider again.
        A finalized retry returns ``("existing", None)``.  Reservation tokens
        are capabilities and are never written in plaintext.
        """

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if not _is_sha256(idempotency_key_hash):
            raise InferenceJobError("inference_job_idempotency_hash_invalid")
        if not _is_sha256(request_hash):
            raise InferenceJobError("inference_job_request_hash_invalid")
        target = _identifier(target, "inference_job_target_invalid")
        if target not in _TARGETS:
            raise InferenceJobError("inference_job_target_invalid")
        ttl = self._duration(
            ttl_seconds,
            default=self._default_ttl_seconds,
            maximum=self._max_ttl_seconds,
            code="inference_job_ttl_invalid",
        )
        preparation_lease_seconds = self._duration(
            None,
            default=self._default_lease_seconds,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        reservation_token = secrets.token_urlsafe(32)
        token_hash = _lease_token_hash(reservation_token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            expires_at = _utc_text(now + timedelta(seconds=ttl))
            preparation_lease_expires_at = _utc_text(
                min(now + timedelta(seconds=preparation_lease_seconds), _parse_utc(expires_at))
            )
            self._maintain_project(connection, now=now, project_id=project_id)
            row = connection.execute(
                "SELECT * FROM inference_rerank_reservations "
                "WHERE project_id = ? AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash or row["target"] != target:
                    connection.commit()
                    raise InferenceJobConflictError("inference_job_idempotency_conflict")
                if row["status"] == "finalized":
                    if not isinstance(row["job_id"], str) or not row["job_id"]:
                        connection.rollback()
                        raise InferenceJobError("inference_job_store_corrupt")
                    connection.commit()
                    return "existing", None
                if row["status"] in {"reserved", "preparing"}:
                    connection.commit()
                    return "preparing", None
                self._ensure_project_capacity(
                    connection,
                    project_id=project_id,
                    now=now,
                )
                self._ensure_project_storage(
                    connection,
                    project_id=project_id,
                    now=now,
                )
                takeover_expires_at = expires_at
                if row["status"] == "expired" and row["expires_at"] > now_text:
                    takeover_expires_at = row["expires_at"]
                takeover_lease_expires_at = _utc_text(
                    min(
                        now + timedelta(seconds=preparation_lease_seconds),
                        _parse_utc(takeover_expires_at),
                    )
                )
                connection.execute(
                    "UPDATE inference_rerank_reservations SET request_hash = ?, target = ?, "
                    "status = 'reserved', reservation_token_hash = ?, job_id = NULL, "
                    "expires_at = ?, preparation_lease_expires_at = ?, updated_at = ? "
                    "WHERE reservation_id = ?",
                    (
                        request_hash,
                        target,
                        token_hash,
                        takeover_expires_at,
                        takeover_lease_expires_at,
                        now_text,
                        row["reservation_id"],
                    ),
                )
                connection.commit()
                return "reserved", reservation_token

            self._ensure_project_capacity(
                connection,
                project_id=project_id,
                now=now,
            )
            self._ensure_project_storage(
                connection,
                project_id=project_id,
                now=now,
                row_delta=1,
            )
            reservation_id = f"rs_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO inference_rerank_reservations ("
                "reservation_id, project_id, idempotency_key_hash, request_hash, target, "
                "status, reservation_token_hash, job_id, created_at, updated_at, expires_at, "
                "preparation_lease_expires_at"
                ") VALUES (?, ?, ?, ?, ?, 'reserved', ?, NULL, ?, ?, ?, ?)",
                (
                    reservation_id,
                    project_id,
                    idempotency_key_hash,
                    request_hash,
                    target,
                    token_hash,
                    now_text,
                    now_text,
                    expires_at,
                    preparation_lease_expires_at,
                ),
            )
            connection.commit()
            return "reserved", reservation_token
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def get_reservation(
        self,
        project_id: str,
        idempotency_key_hash: str,
    ) -> dict[str, object] | None:
        """Return non-secret reservation state for a poller."""

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if not _is_sha256(idempotency_key_hash):
            raise InferenceJobError("inference_job_idempotency_hash_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_reservations(connection, now=self._now(), project_id=project_id)
            row = connection.execute(
                "SELECT reservation_id, project_id, idempotency_key_hash, request_hash, target, "
                "status, job_id, created_at, updated_at, expires_at, preparation_lease_expires_at "
                "FROM inference_rerank_reservations WHERE project_id = ? "
                "AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            result = None if row is None else dict(row)
            connection.commit()
            return result
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def renew_submission(
        self,
        project_id: str,
        idempotency_key_hash: str,
        reservation_token: str,
        lease_seconds: int | None = None,
    ) -> dict[str, object]:
        """Renew an in-flight preparation reservation with its capability token.

        The preparation lease is intentionally independent from ``expires_at``:
        a crashed preparer can be taken over promptly while the overall job TTL
        remains stable.  A token is accepted only while its current lease is
        still live; once another caller takes over, the old token cannot renew
        or finalize the reservation.
        """

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if not _is_sha256(idempotency_key_hash):
            raise InferenceJobError("inference_job_idempotency_hash_invalid")
        token_hash = _lease_token_hash(reservation_token)
        lease = self._duration(
            lease_seconds,
            default=self._default_lease_seconds,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            self._maintain_project(connection, now=now, project_id=project_id)
            row = connection.execute(
                "SELECT * FROM inference_rerank_reservations WHERE project_id = ? "
                "AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            if row is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_reservation_not_found")
            if row["status"] not in {"reserved", "preparing"}:
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_expired")
            stored_hash = row["reservation_token_hash"]
            if not isinstance(stored_hash, str):
                connection.rollback()
                raise InferenceJobError("inference_job_store_corrupt")
            if not hmac.compare_digest(stored_hash, token_hash):
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_token_invalid")
            current_lease = row["preparation_lease_expires_at"]
            if not isinstance(current_lease, str) or current_lease <= now_text:
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_expired")
            overall_expiry = row["expires_at"]
            if not isinstance(overall_expiry, str) or overall_expiry <= now_text:
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_expired")
            renewed_lease = _utc_text(
                min(now + timedelta(seconds=lease), _parse_utc(overall_expiry))
            )
            cursor = connection.execute(
                "UPDATE inference_rerank_reservations SET preparation_lease_expires_at = ?, "
                "updated_at = ? WHERE reservation_id = ? AND status IN ('reserved', 'preparing') "
                "AND reservation_token_hash = ? AND preparation_lease_expires_at > ? "
                "AND expires_at > ?",
                (
                    renewed_lease,
                    now_text,
                    row["reservation_id"],
                    token_hash,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise InferenceJobConflictError("inference_job_reservation_conflict")
            renewed = connection.execute(
                "SELECT reservation_id, project_id, idempotency_key_hash, request_hash, target, "
                "status, job_id, created_at, updated_at, expires_at, "
                "preparation_lease_expires_at FROM inference_rerank_reservations "
                "WHERE reservation_id = ?",
                (row["reservation_id"],),
            ).fetchone()
            if renewed is None:
                connection.rollback()
                raise InferenceJobError("inference_job_store_corrupt")
            result = dict(renewed)
            connection.commit()
            return result
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def finalize_submission(
        self,
        project_id: str,
        idempotency_key_hash: str,
        reservation_token: str,
        binding: RerankRequestBinding | Mapping[str, object],
        package: Mapping[str, object],
        target: str,
        ttl_seconds: int | None = None,
        request_material: Mapping[str, object] | None = None,
    ) -> InferenceJobCreateResult:
        """Atomically bind a preflight reservation to one durable job."""

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if not _is_sha256(idempotency_key_hash):
            raise InferenceJobError("inference_job_idempotency_hash_invalid")
        token_hash = _lease_token_hash(reservation_token)
        submission = self._normalize_submission(
            binding=binding,
            package=package,
            project_id=project_id,
            target=target,
            request_material=request_material,
            ttl_seconds=ttl_seconds,
        )
        if submission.binding_data["idempotency_key_hash"] != idempotency_key_hash:
            raise InferenceJobConflictError("inference_job_idempotency_conflict")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            self._maintain_project(connection, now=now, project_id=project_id)
            reservation = connection.execute(
                "SELECT * FROM inference_rerank_reservations WHERE project_id = ? "
                "AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            if reservation is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_reservation_not_found")
            request_material_hash = (
                None
                if submission.request_material_data is None
                else submission.request_material_data.get("request_hash")
            )
            if request_material_hash != reservation["request_hash"]:
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_request_mismatch")
            if reservation["target"] != submission.target:
                connection.commit()
                raise InferenceJobConflictError("inference_job_target_conflict")
            if reservation["status"] == "finalized":
                existing_id = reservation["job_id"]
                row = self._select_job(connection, existing_id, project_id, required=False)
                if row is None:
                    connection.rollback()
                    raise InferenceJobError("inference_job_store_corrupt")
                if row["execution_hash"] != submission.execution_hash:
                    connection.commit()
                    raise InferenceJobConflictError("inference_job_idempotency_conflict")
                job = self._row_to_job(row)
                connection.commit()
                return InferenceJobCreateResult(job=job, created=False)
            if reservation["status"] not in {"reserved", "preparing"}:
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_expired")
            if (
                reservation["expires_at"] <= now_text
                or reservation["preparation_lease_expires_at"] <= now_text
            ):
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_expired")
            stored_reservation_hash = reservation["reservation_token_hash"]
            if not isinstance(stored_reservation_hash, str):
                connection.rollback()
                raise InferenceJobError("inference_job_store_corrupt")
            if not hmac.compare_digest(stored_reservation_hash, token_hash):
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_token_invalid")

            existing = connection.execute(
                "SELECT * FROM inference_rerank_jobs WHERE project_id = ? "
                "AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            if existing is not None:
                if existing["execution_hash"] != submission.execution_hash:
                    connection.commit()
                    raise InferenceJobConflictError("inference_job_idempotency_conflict")
                job = self._row_to_job(existing)
                created = False
            else:
                remaining = int((_parse_utc(reservation["expires_at"]) - now).total_seconds())
                if remaining < 1:
                    connection.commit()
                    raise InferenceJobConflictError("inference_job_reservation_expired")
                if submission.ttl_seconds > remaining:
                    submission = replace(submission, ttl_seconds=remaining)
                job = self._insert_job(
                    connection,
                    submission=submission,
                    now=now,
                    replaces_active_reservation=True,
                )
                created = True
            cursor = connection.execute(
                "UPDATE inference_rerank_reservations SET status = 'finalized', job_id = ?, "
                "reservation_token_hash = NULL, preparation_lease_expires_at = NULL, "
                "updated_at = ? WHERE reservation_id = ? "
                "AND status IN ('reserved', 'preparing') AND reservation_token_hash = ? "
                "AND preparation_lease_expires_at > ? AND expires_at > ?",
                (
                    job.job_id,
                    now_text,
                    reservation["reservation_id"],
                    token_hash,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise InferenceJobConflictError("inference_job_reservation_conflict")
            connection.commit()
            return InferenceJobCreateResult(job=job, created=created)
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def release_submission(
        self,
        project_id: str,
        idempotency_key_hash: str,
        reservation_token: str,
    ) -> bool:
        """Release a failed preflight without deleting its audit row."""

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if not _is_sha256(idempotency_key_hash):
            raise InferenceJobError("inference_job_idempotency_hash_invalid")
        token_hash = _lease_token_hash(reservation_token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._maintain_project(connection, now=now, project_id=project_id)
            row = connection.execute(
                "SELECT reservation_id, reservation_token_hash, status FROM "
                "inference_rerank_reservations WHERE project_id = ? "
                "AND idempotency_key_hash = ?",
                (project_id, idempotency_key_hash),
            ).fetchone()
            if row is None or row["status"] not in {"reserved", "preparing"}:
                connection.commit()
                return False
            stored_reservation_hash = row["reservation_token_hash"]
            if not isinstance(stored_reservation_hash, str):
                connection.rollback()
                raise InferenceJobError("inference_job_store_corrupt")
            if not hmac.compare_digest(stored_reservation_hash, token_hash):
                connection.commit()
                raise InferenceJobConflictError("inference_job_reservation_token_invalid")
            connection.execute(
                "UPDATE inference_rerank_reservations SET status = 'released', "
                "reservation_token_hash = NULL, preparation_lease_expires_at = NULL, "
                "updated_at = ? WHERE reservation_id = ?",
                (_utc_text(now), row["reservation_id"]),
            )
            connection.commit()
            return True
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def get(self, job_id: str, project_id: str) -> InferenceJob | None:
        """Fetch current state and the server-saved package within one project."""

        job_id = _identifier(job_id, "inference_job_id_invalid")
        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_job(connection, job_id=job_id, project_id=project_id, now=self._now())
            row = connection.execute(
                "SELECT * FROM inference_rerank_jobs WHERE job_id = ? AND project_id = ?",
                (job_id, project_id),
            ).fetchone()
            job = None if row is None else self._row_to_job(row)
            connection.commit()
            return job
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def require(self, job_id: str, project_id: str) -> InferenceJob:
        job = self.get(job_id, project_id)
        if job is None:
            raise InferenceJobNotFoundError("inference_job_not_found")
        return job

    def claim(
        self,
        job_id: str,
        *,
        project_id: str,
        lease_seconds: int | None = None,
    ) -> InferenceJobLease:
        """Claim a specific job, reclaiming it after an expired lease."""

        job_id = _identifier(job_id, "inference_job_id_invalid")
        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        lease_for = self._duration(
            lease_seconds,
            default=self._default_lease_seconds,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        return self._claim(job_id=job_id, project_id=project_id, lease_seconds=lease_for)

    def claim_next(
        self,
        project_id: str,
        *,
        target: str | None = None,
        lease_seconds: int | None = None,
    ) -> InferenceJobLease | None:
        """Atomically claim the oldest available project job, optionally by target."""

        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        if target is not None:
            target = _identifier(target, "inference_job_target_invalid")
            if target not in _TARGETS:
                raise InferenceJobError("inference_job_target_invalid")
        lease_for = self._duration(
            lease_seconds,
            default=self._default_lease_seconds,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._maintain_project(connection, now=now, project_id=project_id)
            target_clause = " AND target = ?" if target is not None else ""
            parameters: tuple[object, ...] = (project_id, _utc_text(now))
            if target is not None:
                parameters += (target,)
            row = connection.execute(
                "SELECT job_id FROM inference_rerank_jobs "
                "WHERE project_id = ? AND status = 'pending' AND expires_at > ?"
                + target_clause
                + " ORDER BY created_at, job_id LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            lease = self._claim_in_transaction(
                connection,
                job_id=row["job_id"],
                project_id=project_id,
                lease_seconds=lease_for,
                now=now,
            )
            connection.commit()
            return lease
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def lease(
        self,
        job_id: str,
        project_id: str,
        lease_seconds: int | None = None,
    ) -> tuple[InferenceJob, str] | None:
        """Claim one job and return its state plus the one-time lease capability."""

        try:
            lease = self.claim(
                job_id,
                project_id=project_id,
                lease_seconds=lease_seconds,
            )
        except InferenceJobNotFoundError:
            return None
        return lease.job, lease.lease_token

    def renew_lease(
        self,
        job_id: str,
        project_id: str,
        lease_token: str,
        lease_seconds: int | None = None,
    ) -> InferenceJob:
        """Extend an active lease without changing its capability token."""

        job_id = _identifier(job_id, "inference_job_id_invalid")
        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        token_hash = _lease_token_hash(lease_token)
        lease_for = self._duration(
            lease_seconds,
            default=self._default_lease_seconds,
            maximum=self._max_lease_seconds,
            code="inference_job_lease_seconds_invalid",
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            self._maintain_project(connection, now=now, project_id=project_id)
            row = self._select_job(connection, job_id, project_id, required=False)
            if row is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_not_found")
            if row["status"] != "leased":
                connection.commit()
                raise InferenceJobConflictError("inference_job_not_leased")
            if row["expires_at"] <= now_text:
                connection.commit()
                raise InferenceJobConflictError("inference_job_expired")
            if row["lease_expires_at"] <= now_text:
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_expired")
            if not hmac.compare_digest(row["lease_token_hash"], token_hash):
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_token_invalid")
            deadline = min(
                now + timedelta(seconds=lease_for),
                _parse_utc(row["expires_at"]),
            )
            cursor = connection.execute(
                "UPDATE inference_rerank_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE job_id = ? AND project_id = ? AND status = 'leased' "
                "AND lease_token_hash = ? AND lease_expires_at > ? AND expires_at > ?",
                (
                    _utc_text(deadline),
                    now_text,
                    job_id,
                    project_id,
                    token_hash,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise InferenceJobConflictError("inference_job_lease_renewal_conflict")
            renewed = self._row_to_job(self._select_job(connection, job_id, project_id))
            connection.commit()
            return renewed
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def complete(
        self,
        job_id: str,
        project_id: str,
        lease_token: str,
        result: Mapping[str, object],
    ) -> InferenceJob:
        """Persist the first valid lease completion using a transactional CAS."""

        job_id = _identifier(job_id, "inference_job_id_invalid")
        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        token_hash = _lease_token_hash(lease_token)
        _result_data, result_json = _canonical_json_mapping(
            result,
            kind="result",
            maximum_bytes=self._max_result_bytes,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            self._maintain_project(connection, now=now, project_id=project_id)
            row = self._select_job(connection, job_id, project_id, required=False)
            if row is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_not_found")
            status = row["status"]
            if status == "completed":
                connection.commit()
                raise InferenceJobConflictError("inference_job_already_completed")
            if status == "expired" or row["expires_at"] <= now_text:
                connection.execute(
                    "UPDATE inference_rerank_jobs SET status = 'expired', updated_at = ?, "
                    "lease_token_hash = NULL, lease_expires_at = NULL "
                    "WHERE job_id = ? AND project_id = ? AND status IN ('pending', 'leased')",
                    (now_text, job_id, project_id),
                )
                connection.commit()
                raise InferenceJobConflictError("inference_job_expired")
            if status != "leased":
                connection.commit()
                raise InferenceJobConflictError("inference_job_not_leased")
            if row["lease_expires_at"] <= now_text:
                connection.execute(
                    "UPDATE inference_rerank_jobs SET status = 'pending', updated_at = ?, "
                    "lease_token_hash = NULL, lease_expires_at = NULL "
                    "WHERE job_id = ? AND project_id = ? AND status = 'leased'",
                    (now_text, job_id, project_id),
                )
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_expired")
            stored_token_hash = row["lease_token_hash"]
            if not isinstance(stored_token_hash, str) or not hmac.compare_digest(
                stored_token_hash,
                token_hash,
            ):
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_token_invalid")

            result_bytes = len(result_json.encode("utf-8"))
            self._ensure_project_storage(
                connection,
                project_id=project_id,
                now=now,
                json_bytes_delta=result_bytes,
            )
            cursor = connection.execute(
                "UPDATE inference_rerank_jobs SET status = 'completed', result_json = ?, "
                "result_bytes = ?, completed_at = ?, updated_at = ?, "
                "lease_token_hash = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND project_id = ? AND status = 'leased' "
                "AND lease_token_hash = ? AND lease_expires_at > ? AND expires_at > ?",
                (
                    result_json,
                    result_bytes,
                    now_text,
                    now_text,
                    job_id,
                    project_id,
                    token_hash,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise InferenceJobConflictError("inference_job_completion_conflict")
            completed = self._row_to_job(self._select_job(connection, job_id, project_id))
            connection.commit()
            return completed
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def fail(
        self,
        job_id: str,
        project_id: str,
        lease_token: str,
        failure_code: str,
    ) -> InferenceJob:
        """Finalize a leased job with a stable, non-sensitive failure code.

        The original schema only had ``completed`` and ``expired`` terminal
        states.  A policy conflict is not a lease timeout and must not remain
        leased until its TTL, so it is recorded as an ``expired`` row with an
        explicit ``failure_code``.  The gateway exposes that combination as a
        terminal ``failed`` response while remaining compatible with existing
        databases and cleanup rules.
        """

        job_id = _identifier(job_id, "inference_job_id_invalid")
        project_id = _identifier(project_id, "inference_job_project_id_invalid")
        token_hash = _lease_token_hash(lease_token)
        if not isinstance(failure_code, str) or _FAILURE_CODE_RE.fullmatch(failure_code) is None:
            raise InferenceJobError("inference_job_failure_code_invalid")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            now_text = _utc_text(now)
            self._maintain_project(connection, now=now, project_id=project_id)
            row = self._select_job(connection, job_id, project_id, required=False)
            if row is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_not_found")
            if row["status"] == "completed":
                connection.commit()
                raise InferenceJobConflictError("inference_job_already_completed")
            if row["status"] == "expired":
                connection.commit()
                return self._row_to_job(row)
            if row["status"] != "leased":
                connection.commit()
                raise InferenceJobConflictError("inference_job_not_leased")
            if row["expires_at"] <= now_text:
                connection.execute(
                    "UPDATE inference_rerank_jobs SET status = 'expired', updated_at = ?, "
                    "lease_token_hash = NULL, lease_expires_at = NULL "
                    "WHERE job_id = ? AND project_id = ? AND status = 'leased'",
                    (now_text, job_id, project_id),
                )
                connection.commit()
                raise InferenceJobConflictError("inference_job_expired")
            if row["lease_expires_at"] is None or row["lease_expires_at"] <= now_text:
                connection.execute(
                    "UPDATE inference_rerank_jobs SET status = 'pending', updated_at = ?, "
                    "lease_token_hash = NULL, lease_expires_at = NULL "
                    "WHERE job_id = ? AND project_id = ? AND status = 'leased'",
                    (now_text, job_id, project_id),
                )
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_expired")
            stored_token_hash = row["lease_token_hash"]
            if not isinstance(stored_token_hash, str) or not hmac.compare_digest(
                stored_token_hash,
                token_hash,
            ):
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_token_invalid")

            cursor = connection.execute(
                "UPDATE inference_rerank_jobs SET status = 'expired', failure_code = ?, "
                "updated_at = ?, lease_token_hash = NULL, lease_expires_at = NULL "
                "WHERE job_id = ? AND project_id = ? AND status = 'leased' "
                "AND lease_token_hash = ? AND lease_expires_at > ? AND expires_at > ?",
                (
                    failure_code,
                    now_text,
                    job_id,
                    project_id,
                    token_hash,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise InferenceJobConflictError("inference_job_failure_conflict")
            failed = self._row_to_job(self._select_job(connection, job_id, project_id))
            connection.commit()
            return failed
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def expire_due(self, *, project_id: str | None = None) -> int:
        """Persist TTL expiry, release stale leases, and prune retained terminal rows."""

        if project_id is not None:
            project_id = _identifier(project_id, "inference_job_project_id_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            expired = self._refresh_due(connection, now=now, project_id=project_id)
            self._refresh_reservations(connection, now=now, project_id=project_id)
            self._cleanup_retained(connection, now=now, project_id=project_id)
            connection.commit()
            return expired
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def cleanup_retained(self, *, project_id: str | None = None) -> dict[str, int]:
        """Delete one bounded batch of terminal rows older than the retention window."""

        if project_id is not None:
            project_id = _identifier(project_id, "inference_job_project_id_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._refresh_due(connection, now=now, project_id=project_id)
            self._refresh_reservations(connection, now=now, project_id=project_id)
            removed = self._cleanup_retained(connection, now=now, project_id=project_id)
            connection.commit()
            return removed
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def _claim(
        self,
        *,
        job_id: str,
        project_id: str,
        lease_seconds: int,
    ) -> InferenceJobLease:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._maintain_project(connection, now=now, project_id=project_id)
            row = self._select_job(connection, job_id, project_id, required=False)
            if row is None:
                connection.commit()
                raise InferenceJobNotFoundError("inference_job_not_found")
            if row["status"] == "leased":
                connection.commit()
                raise InferenceJobConflictError("inference_job_lease_active")
            if row["status"] == "completed":
                connection.commit()
                raise InferenceJobConflictError("inference_job_already_completed")
            if row["status"] == "expired":
                connection.commit()
                raise InferenceJobConflictError("inference_job_expired")
            lease = self._claim_in_transaction(
                connection,
                job_id=job_id,
                project_id=project_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            connection.commit()
            return lease
        except InferenceJobError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            connection.close()

    def _claim_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        project_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> InferenceJobLease:
        row = self._select_job(connection, job_id, project_id)
        if row["status"] != "pending":
            raise InferenceJobConflictError("inference_job_not_claimable")
        job_deadline = _parse_utc(row["expires_at"])
        lease_deadline = min(now + timedelta(seconds=lease_seconds), job_deadline)
        if lease_deadline <= now:
            raise InferenceJobConflictError("inference_job_expired")
        lease_token = secrets.token_urlsafe(32)
        token_hash = _lease_token_hash(lease_token)
        now_text = _utc_text(now)
        cursor = connection.execute(
            "UPDATE inference_rerank_jobs SET status = 'leased', lease_token_hash = ?, "
            "lease_expires_at = ?, updated_at = ? "
            "WHERE job_id = ? AND project_id = ? AND status = 'pending' AND expires_at > ?",
            (
                token_hash,
                _utc_text(lease_deadline),
                now_text,
                job_id,
                project_id,
                now_text,
            ),
        )
        if cursor.rowcount != 1:
            raise InferenceJobConflictError("inference_job_claim_conflict")
        leased = self._row_to_job(self._select_job(connection, job_id, project_id))
        return InferenceJobLease(job=leased, lease_token=lease_token)

    def _normalize_submission(
        self,
        *,
        binding: RerankRequestBinding | Mapping[str, object],
        package: Mapping[str, object],
        project_id: str | None,
        target: str,
        request_material: Mapping[str, object] | None,
        ttl_seconds: int | None,
    ) -> _Submission:
        binding_project_id = _binding_project_id(binding)
        resolved_project_id = _identifier(
            binding_project_id if project_id is None else project_id,
            "inference_job_project_id_invalid",
        )
        resolved_target = _identifier(target, "inference_job_target_invalid")
        if resolved_target not in _TARGETS:
            raise InferenceJobError("inference_job_target_invalid")
        binding_data, binding_json = self._binding_json(binding, project_id=resolved_project_id)
        package_data, package_json = self._package_json(
            package,
            project_id=resolved_project_id,
            binding=binding_data,
        )
        if request_material is None:
            request_material_data = None
            request_material_json = None
            request_material_bytes = None
            request_material_hash = _canonical_hash({"request_material": None})
        else:
            request_material_data, request_material_json = _canonical_json_mapping(
                request_material,
                kind="request_material",
                maximum_bytes=self._max_package_bytes,
            )
            request_material_bytes = len(request_material_json.encode("utf-8"))
            request_material_hash = _canonical_hash(request_material_data)
        execution_hash = _canonical_hash(
            {
                "input_hash": binding_data["input_hash"],
                "request_material_hash": request_material_hash,
                "target": resolved_target,
            }
        )
        ttl = self._duration(
            ttl_seconds,
            default=self._default_ttl_seconds,
            maximum=self._max_ttl_seconds,
            code="inference_job_ttl_invalid",
        )
        return _Submission(
            project_id=resolved_project_id,
            target=resolved_target,
            binding_data=binding_data,
            binding_json=binding_json,
            package_data=package_data,
            package_json=package_json,
            request_material_data=request_material_data,
            request_material_json=request_material_json,
            request_material_bytes=request_material_bytes,
            execution_hash=execution_hash,
            ttl_seconds=ttl,
        )

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        *,
        submission: _Submission,
        now: datetime,
        replaces_active_reservation: bool = False,
    ) -> InferenceJob:
        binding_bytes = len(submission.binding_json.encode("utf-8"))
        package_bytes = len(submission.package_json.encode("utf-8"))
        json_bytes = binding_bytes + package_bytes + (submission.request_material_bytes or 0)
        if not replaces_active_reservation:
            self._ensure_project_capacity(
                connection,
                project_id=submission.project_id,
                now=now,
            )
        self._ensure_project_storage(
            connection,
            project_id=submission.project_id,
            now=now,
            row_delta=1,
            json_bytes_delta=json_bytes,
        )
        job_id = f"rj_{uuid.uuid4().hex}"
        now_text = _utc_text(now)
        expires_at = _utc_text(now + timedelta(seconds=submission.ttl_seconds))
        connection.execute(
            "INSERT INTO inference_rerank_jobs ("
            "job_id, project_id, idempotency_key_hash, input_hash, execution_hash, "
            "request_id, target, binding_json, binding_bytes, package_json, package_bytes, "
            "request_material_json, request_material_bytes, status, "
            "created_at, updated_at, expires_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                job_id,
                submission.project_id,
                submission.binding_data["idempotency_key_hash"],
                submission.binding_data["input_hash"],
                submission.execution_hash,
                submission.binding_data["request_id"],
                submission.target,
                submission.binding_json,
                binding_bytes,
                submission.package_json,
                package_bytes,
                submission.request_material_json,
                submission.request_material_bytes,
                now_text,
                now_text,
                expires_at,
            ),
        )
        return self._row_to_job(self._select_job(connection, job_id, submission.project_id))

    def _binding_json(
        self,
        binding: RerankRequestBinding | Mapping[str, object],
        *,
        project_id: str,
    ) -> tuple[dict[str, object], str]:
        if isinstance(binding, RerankRequestBinding):
            raw: object = asdict(binding)
        elif isinstance(binding, Mapping):
            raw = binding
        else:
            raise InferenceJobError("inference_job_binding_invalid")
        data, encoded = _canonical_json_mapping(
            raw,
            kind="binding",
            maximum_bytes=_MAX_BINDING_BYTES,
        )
        if set(data) != _BINDING_FIELDS:
            raise InferenceJobError("inference_job_binding_schema_invalid")
        if data.get("contract_version") != RERANK_REQUEST_CONTRACT:
            raise InferenceJobError("inference_job_binding_contract_invalid")
        if data.get("scoring_version") != RERANK_SCORING_VERSION:
            raise InferenceJobError("inference_job_binding_scoring_invalid")
        if data.get("project_id") != project_id:
            raise InferenceJobError("inference_job_project_mismatch")
        for field_name in (
            "idempotency_key_hash",
            "candidate_set_hash",
            "query_hash",
            "input_hash",
        ):
            if not _is_sha256(data.get(field_name)):
                raise InferenceJobError("inference_job_binding_hash_invalid")
        for field_name in ("request_id", "candidate_set_version", "provider_policy_revision"):
            _identifier(data.get(field_name), "inference_job_binding_identifier_invalid")
        _positive_integer(data.get("top_k"), "inference_job_binding_top_k_invalid")
        return data, encoded

    def _package_json(
        self,
        package: Mapping[str, object],
        *,
        project_id: str,
        binding: Mapping[str, object],
    ) -> tuple[dict[str, object], str]:
        if not isinstance(package, Mapping):
            raise InferenceJobError("inference_job_package_mapping_required")
        raw_data, _unused = _canonical_json_mapping(
            package,
            kind="package",
            maximum_bytes=self._max_package_bytes,
        )
        if set(raw_data) != _PACKAGE_FIELDS:
            raise InferenceJobError("inference_job_package_schema_invalid")
        candidates = raw_data.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or len(candidates) > _MAX_PACKAGE_CANDIDATES
        ):
            raise InferenceJobError("inference_job_package_schema_invalid")
        normalized_candidates: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise InferenceJobError("inference_job_package_schema_invalid")
            fields = set(candidate)
            if fields == _DATACLASS_CANDIDATE_FIELDS:
                candidate = {**candidate, "id": candidate["item_id"]}
                del candidate["item_id"]
            elif fields != _CANDIDATE_FIELDS:
                raise InferenceJobError("inference_job_package_schema_invalid")
            item_id = _identifier(candidate.get("id"), "inference_job_package_schema_invalid")
            if item_id in seen_ids:
                raise InferenceJobError("inference_job_package_schema_invalid")
            seen_ids.add(item_id)
            text = candidate.get("text")
            if not isinstance(text, str) or not text.strip():
                raise InferenceJobError("inference_job_package_schema_invalid")
            try:
                text_bytes = len(text.encode("utf-8"))
            except UnicodeError:
                raise InferenceJobError("inference_job_package_schema_invalid") from None
            if text_bytes > _MAX_PACKAGE_TEXT_BYTES:
                raise InferenceJobError("inference_job_package_text_too_large")
            score = candidate.get("base_score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0.0 <= float(score) <= 1.0
            ):
                raise InferenceJobError("inference_job_package_schema_invalid")
            if candidate.get("material_sha256") != _material_hash(text):
                raise InferenceJobError("inference_job_package_hash_invalid")
            if not _is_sha256(candidate.get("embedding_sha256")):
                raise InferenceJobError("inference_job_package_hash_invalid")
            normalized_candidates.append(dict(candidate))

        normalized = {**raw_data, "candidates": normalized_candidates}
        if normalized.get("contract_version") != CLIENT_LOCAL_RERANK_CONTRACT:
            raise InferenceJobError("inference_job_package_contract_invalid")
        if normalized.get("scoring_version") != CLIENT_LOCAL_SCORING_VERSION:
            raise InferenceJobError("inference_job_package_scoring_invalid")
        if normalized.get("project_id") != project_id:
            raise InferenceJobError("inference_job_project_mismatch")
        _identifier(normalized.get("request_id"), "inference_job_package_schema_invalid")
        _identifier(normalized.get("candidate_set_version"), "inference_job_package_schema_invalid")
        _identifier(normalized.get("embedding_identity"), "inference_job_package_schema_invalid")
        _identifier(normalized.get("model_identity"), "inference_job_package_schema_invalid")
        query = normalized.get("query")
        if not isinstance(query, str) or not query.strip():
            raise InferenceJobError("inference_job_package_schema_invalid")
        try:
            query_bytes = len(query.encode("utf-8"))
        except UnicodeError:
            raise InferenceJobError("inference_job_package_schema_invalid") from None
        if query_bytes > _MAX_PACKAGE_QUERY_BYTES:
            raise InferenceJobError("inference_job_package_query_too_large")
        if normalized.get("query_hash") != _material_hash(query):
            raise InferenceJobError("inference_job_package_hash_invalid")
        if not _is_sha256(normalized.get("candidate_set_hash")):
            raise InferenceJobError("inference_job_package_hash_invalid")
        _positive_integer(
            normalized.get("embedding_dimension"),
            "inference_job_package_schema_invalid",
        )
        top_k = _positive_integer(normalized.get("top_k"), "inference_job_package_schema_invalid")
        if top_k > len(normalized_candidates):
            raise InferenceJobError("inference_job_package_schema_invalid")
        expected_package_hash = _canonical_hash(
            {key: value for key, value in normalized.items() if key != "package_hash"}
        )
        if normalized.get("package_hash") != expected_package_hash:
            raise InferenceJobError("inference_job_package_hash_invalid")
        correlations = (
            ("request_id", "request_id"),
            ("candidate_set_version", "candidate_set_version"),
            ("candidate_set_hash", "candidate_set_hash"),
            ("query_hash", "query_hash"),
            ("top_k", "top_k"),
        )
        if any(
            normalized[package_key] != binding[binding_key]
            for package_key, binding_key in correlations
        ):
            raise InferenceJobError("inference_job_package_binding_mismatch")
        normalized, encoded = _canonical_json_mapping(
            normalized,
            kind="package",
            maximum_bytes=self._max_package_bytes,
        )
        return normalized, encoded

    def _duration(
        self,
        value: int | None,
        *,
        default: int,
        maximum: int,
        code: str,
    ) -> int:
        return _bounded_integer(
            default if value is None else value, minimum=1, maximum=maximum, code=code
        )

    def _maintain_project(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        project_id: str,
    ) -> dict[str, int]:
        """Refresh deadlines and prune elapsed retention before a project write."""

        self._refresh_due(connection, now=now, project_id=project_id)
        self._refresh_reservations(connection, now=now, project_id=project_id)
        return self._cleanup_retained(connection, now=now, project_id=project_id)

    def _ensure_project_storage(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        now: datetime,
        row_delta: int = 0,
        json_bytes_delta: int = 0,
    ) -> None:
        """Prune eligible batches, then enforce the committed project footprint."""

        cleanup_batches_remaining: int | None = None
        maintenance_changed_rows = connection.total_changes > 0
        while True:
            job_rows = connection.execute(
                "SELECT COUNT(*) FROM inference_rerank_jobs WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            reservation_rows = connection.execute(
                "SELECT COUNT(*) FROM inference_rerank_reservations WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            row_limit_exceeded = (
                job_rows + reservation_rows + row_delta > self._max_retained_rows_per_project
            )

            stored_json_bytes = connection.execute(
                "SELECT COALESCE(SUM(binding_bytes + package_bytes + "
                "COALESCE(request_material_bytes, 0) + COALESCE(result_bytes, 0)), 0) "
                "FROM inference_rerank_jobs WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            json_limit_exceeded = (
                stored_json_bytes + json_bytes_delta > self._max_retained_json_bytes_per_project
            )
            if not row_limit_exceeded and not json_limit_exceeded:
                return

            if cleanup_batches_remaining is None:
                retained_rows = job_rows + reservation_rows
                cleanup_batches_remaining = (
                    retained_rows + _CLEANUP_BATCH_SIZE - 1
                ) // _CLEANUP_BATCH_SIZE + 1
            if cleanup_batches_remaining <= 0:
                removed = {"jobs": 0, "reservations": 0}
            else:
                removed = self._cleanup_retained(
                    connection,
                    now=now,
                    project_id=project_id,
                )
                cleanup_batches_remaining -= 1
            if removed["jobs"] or removed["reservations"]:
                maintenance_changed_rows = True
                continue

            # Quota rejection must not resurrect terminal rows already pruned
            # in earlier batches.  No caller mutates durable job state before
            # this admission check, so committing maintenance here is safe.
            if maintenance_changed_rows and connection.in_transaction:
                connection.commit()
            if row_limit_exceeded:
                raise InferenceJobConflictError("inference_job_project_retained_rows_exceeded")
            raise InferenceJobConflictError("inference_job_project_retained_json_bytes_exceeded")

    def _ensure_project_capacity(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        now: datetime,
    ) -> None:
        """Reserve one active work slot while the caller holds ``BEGIN IMMEDIATE``."""

        self._refresh_due(connection, now=now, project_id=project_id)
        self._refresh_reservations(connection, now=now, project_id=project_id)
        active_job_statuses = tuple(sorted(_ACTIVE_JOB_STATUSES))
        active_jobs = connection.execute(
            "SELECT COUNT(*) FROM inference_rerank_jobs WHERE project_id = ? AND status IN (?, ?)",
            (project_id, *active_job_statuses),
        ).fetchone()[0]
        active_reservation_statuses = tuple(sorted(_ACTIVE_RESERVATION_STATUSES))
        active_reservations = connection.execute(
            "SELECT COUNT(*) FROM inference_rerank_reservations "
            "WHERE project_id = ? AND status IN (?, ?)",
            (project_id, *active_reservation_statuses),
        ).fetchone()[0]
        if active_jobs + active_reservations >= self._max_active_jobs:
            raise InferenceJobConflictError("inference_job_project_capacity_exceeded")

    def _now(self) -> datetime:
        try:
            now = self._clock()
        except Exception:
            raise InferenceJobError("inference_job_clock_invalid") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise InferenceJobError("inference_job_clock_invalid")
        return now.astimezone(timezone.utc)

    def _initialize(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            journal_row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal_row is None or str(journal_row[0]).casefold() != "wal":
                raise InferenceJobError("inference_job_wal_unavailable")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_rerank_jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    execution_hash TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    binding_json TEXT NOT NULL,
                    binding_bytes INTEGER NOT NULL CHECK(binding_bytes > 0),
                    package_json TEXT NOT NULL,
                    package_bytes INTEGER NOT NULL CHECK(package_bytes > 0),
                    request_material_json TEXT,
                    request_material_bytes INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'leased', 'completed', 'expired')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    lease_token_hash TEXT,
                    lease_expires_at TEXT,
                    completed_at TEXT,
                    result_json TEXT,
                    result_bytes INTEGER,
                    failure_code TEXT,
                    UNIQUE(project_id, idempotency_key_hash),
                    CHECK(
                        (request_material_json IS NULL AND request_material_bytes IS NULL)
                        OR
                        (request_material_json IS NOT NULL AND request_material_bytes > 0)
                    ),
                    CHECK(
                        (status = 'leased' AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL)
                        OR
                        (status != 'leased' AND lease_token_hash IS NULL AND lease_expires_at IS NULL)
                    ),
                    CHECK(
                        (status = 'completed' AND completed_at IS NOT NULL AND result_json IS NOT NULL AND result_bytes > 0)
                        OR
                        (status != 'completed' AND completed_at IS NULL AND result_json IS NULL AND result_bytes IS NULL)
                    )
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_jobs_claim "
                "ON inference_rerank_jobs(project_id, status, created_at, job_id)"
            )
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(inference_rerank_jobs)").fetchall()
            }
            if "failure_code" not in job_columns:
                # Additive migration keeps existing job rows and the original
                # status CHECK constraint intact.
                connection.execute("ALTER TABLE inference_rerank_jobs ADD COLUMN failure_code TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_jobs_target_claim "
                "ON inference_rerank_jobs(project_id, target, status, created_at, job_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_jobs_expiry "
                "ON inference_rerank_jobs(status, expires_at, lease_expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_jobs_retention "
                "ON inference_rerank_jobs(project_id, status, completed_at, expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_rerank_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('reserved', 'preparing', 'finalized', 'released', 'expired')
                    ),
                    reservation_token_hash TEXT,
                    job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    preparation_lease_expires_at TEXT,
                    UNIQUE(project_id, idempotency_key_hash),
                    CHECK(
                        (status IN ('reserved', 'preparing')
                            AND reservation_token_hash IS NOT NULL
                            AND preparation_lease_expires_at IS NOT NULL)
                        OR
                        (status IN ('finalized', 'released', 'expired')
                            AND reservation_token_hash IS NULL
                            AND preparation_lease_expires_at IS NULL)
                    )
                )
                """
            )
            reservation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(inference_rerank_reservations)"
                ).fetchall()
            }
            if "preparation_lease_expires_at" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE inference_rerank_reservations "
                    "ADD COLUMN preparation_lease_expires_at TEXT"
                )
                # Existing active reservations have no short-lease proof.  Make
                # them immediately reclaimable rather than preserving a stale
                # capability until the historical overall TTL elapses.
                connection.execute(
                    "UPDATE inference_rerank_reservations "
                    "SET preparation_lease_expires_at = updated_at "
                    "WHERE status IN ('reserved', 'preparing')"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_reservations_expiry "
                "ON inference_rerank_reservations(status, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_reservations_preparation_lease "
                "ON inference_rerank_reservations(status, preparation_lease_expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_inference_rerank_reservations_retention "
                "ON inference_rerank_reservations(project_id, status, updated_at, expires_at)"
            )
            connection.commit()
        except InferenceJobError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise InferenceJobError("inference_job_store_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._secure_permissions()
        except InferenceJobError:
            connection.close()
            raise
        return connection

    def _secure_permissions(self) -> None:
        if os.name == "nt":
            return
        for path in (
            self._db_path,
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                continue
            except OSError:
                raise InferenceJobError("inference_job_file_permissions_unavailable") from None

    @staticmethod
    def _select_job(
        connection: sqlite3.Connection,
        job_id: str,
        project_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM inference_rerank_jobs WHERE job_id = ? AND project_id = ?",
            (job_id, project_id),
        ).fetchone()
        if row is None and required:
            raise InferenceJobNotFoundError("inference_job_not_found")
        return row

    def _refresh_job(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        project_id: str,
        now: datetime,
    ) -> None:
        now_text = _utc_text(now)
        connection.execute(
            "UPDATE inference_rerank_jobs SET status = 'expired', updated_at = ?, "
            "lease_token_hash = NULL, lease_expires_at = NULL "
            "WHERE job_id = ? AND project_id = ? AND status IN ('pending', 'leased') "
            "AND expires_at <= ?",
            (now_text, job_id, project_id, now_text),
        )
        connection.execute(
            "UPDATE inference_rerank_jobs SET status = 'pending', updated_at = ?, "
            "lease_token_hash = NULL, lease_expires_at = NULL "
            "WHERE job_id = ? AND project_id = ? AND status = 'leased' "
            "AND lease_expires_at <= ? AND expires_at > ?",
            (now_text, job_id, project_id, now_text, now_text),
        )

    def _refresh_due(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        project_id: str | None,
    ) -> int:
        now_text = _utc_text(now)
        project_clause = " AND project_id = ?" if project_id is not None else ""
        params: tuple[object, ...] = (now_text, now_text)
        if project_id is not None:
            params += (project_id,)
        expired = connection.execute(
            "UPDATE inference_rerank_jobs SET status = 'expired', updated_at = ?, "
            "lease_token_hash = NULL, lease_expires_at = NULL "
            "WHERE status IN ('pending', 'leased') AND expires_at <= ?" + project_clause,
            params,
        ).rowcount
        release_params: tuple[object, ...] = (now_text, now_text, now_text)
        if project_id is not None:
            release_params += (project_id,)
        connection.execute(
            "UPDATE inference_rerank_jobs SET status = 'pending', updated_at = ?, "
            "lease_token_hash = NULL, lease_expires_at = NULL "
            "WHERE status = 'leased' AND lease_expires_at <= ? AND expires_at > ?" + project_clause,
            release_params,
        )
        return expired

    @staticmethod
    def _refresh_reservations(
        connection: sqlite3.Connection,
        *,
        now: datetime,
        project_id: str | None,
    ) -> int:
        """Expire abandoned preflight reservations without deleting evidence."""

        now_text = _utc_text(now)
        clause = " AND project_id = ?" if project_id is not None else ""
        params: tuple[object, ...] = (now_text, now_text, now_text)
        if project_id is not None:
            params += (project_id,)
        return connection.execute(
            "UPDATE inference_rerank_reservations SET status = 'expired', "
            "reservation_token_hash = NULL, preparation_lease_expires_at = NULL, updated_at = ? "
            "WHERE status IN ('reserved', 'preparing') "
            "AND (expires_at <= ? OR preparation_lease_expires_at IS NULL "
            "OR preparation_lease_expires_at <= ?)" + clause,
            params,
        ).rowcount

    def _cleanup_retained(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        project_id: str | None,
    ) -> dict[str, int]:
        cutoff = _utc_text(now - timedelta(seconds=self._retention_seconds))
        reservation_scope = " AND r.project_id = ?" if project_id is not None else ""
        reservation_params: tuple[object, ...] = (cutoff, cutoff)
        if project_id is not None:
            reservation_params += (project_id,)
        reservation_params += (cutoff, cutoff, _CLEANUP_BATCH_SIZE)
        reservations = connection.execute(
            "DELETE FROM inference_rerank_reservations WHERE rowid IN ("
            "SELECT r.rowid FROM inference_rerank_reservations AS r WHERE ("
            "(r.status = 'expired' AND r.expires_at <= ?) OR "
            "(r.status IN ('finalized', 'released') AND r.updated_at <= ?))"
            + reservation_scope
            + " AND (r.status != 'finalized' OR r.job_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM inference_rerank_jobs AS retained_job "
            "WHERE retained_job.job_id = r.job_id AND ("
            "retained_job.status IN ('pending', 'leased') OR "
            "(retained_job.status = 'completed' AND "
            "(retained_job.completed_at IS NULL OR retained_job.completed_at > ?)) OR "
            "(retained_job.status = 'expired' AND retained_job.expires_at > ?)))) "
            "ORDER BY r.updated_at, r.reservation_id LIMIT ?)",
            reservation_params,
        ).rowcount

        job_scope = " AND j.project_id = ?" if project_id is not None else ""
        job_params: tuple[object, ...] = (cutoff, cutoff)
        if project_id is not None:
            job_params += (project_id,)
        job_params += (_CLEANUP_BATCH_SIZE,)
        jobs = connection.execute(
            "DELETE FROM inference_rerank_jobs WHERE rowid IN ("
            "SELECT j.rowid FROM inference_rerank_jobs AS j WHERE ("
            "(j.status = 'completed' AND j.completed_at <= ?) OR "
            "(j.status = 'expired' AND j.expires_at <= ?))"
            + job_scope
            + " AND NOT EXISTS (SELECT 1 FROM inference_rerank_reservations AS reservation "
            "WHERE reservation.job_id = j.job_id) "
            "ORDER BY j.updated_at, j.job_id LIMIT ?)",
            job_params,
        ).rowcount
        return {"jobs": max(jobs, 0), "reservations": max(reservations, 0)}

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> InferenceJob:
        if row is None:
            raise InferenceJobNotFoundError("inference_job_not_found")
        try:
            binding = json.loads(row["binding_json"])
            package = json.loads(row["package_json"])
            request_material = (
                None
                if row["request_material_json"] is None
                else json.loads(row["request_material_json"])
            )
            result = None if row["result_json"] is None else json.loads(row["result_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise InferenceJobError("inference_job_store_corrupt") from None
        if (
            not isinstance(binding, dict)
            or not isinstance(package, dict)
            or (request_material is not None and not isinstance(request_material, dict))
            or (result is not None and not isinstance(result, dict))
            or row["status"] not in _STATUSES
            or (
                row["failure_code"] is not None
                and (
                    not isinstance(row["failure_code"], str)
                    or _FAILURE_CODE_RE.fullmatch(row["failure_code"]) is None
                )
            )
        ):
            raise InferenceJobError("inference_job_store_corrupt")
        return InferenceJob(
            job_id=row["job_id"],
            project_id=row["project_id"],
            idempotency_key_hash=row["idempotency_key_hash"],
            input_hash=row["input_hash"],
            execution_hash=row["execution_hash"],
            request_id=row["request_id"],
            target=row["target"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            lease_expires_at=row["lease_expires_at"],
            completed_at=row["completed_at"],
            binding=binding,
            package=package,
            request_material=request_material,
            result=result,
            failure_code=row["failure_code"],
        )


def _canonical_json_mapping(
    value: object,
    *,
    kind: str,
    maximum_bytes: int,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping):
        raise InferenceJobError(f"inference_job_{kind}_mapping_required")
    _validate_json(value, kind=kind)
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        byte_count = len(encoded.encode("utf-8"))
        normalized = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise InferenceJobError(f"inference_job_{kind}_json_invalid") from None
    if byte_count > maximum_bytes:
        raise InferenceJobError(f"inference_job_{kind}_too_large")
    if not isinstance(normalized, dict):
        raise InferenceJobError(f"inference_job_{kind}_mapping_required")
    return normalized, encoded


def _validate_json(value: object, *, kind: str) -> None:
    remaining = _MAX_JSON_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _MAX_JSON_DEPTH:
            raise InferenceJobError(f"inference_job_{kind}_json_invalid")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise InferenceJobError(f"inference_job_{kind}_json_invalid")
            return
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise InferenceJobError(f"inference_job_{kind}_json_invalid")
            if any(key.casefold() in _SECRET_FIELD_NAMES for key in item):
                raise InferenceJobError(f"inference_job_{kind}_secret_field_forbidden")
            for child in item.values():
                visit(child, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        raise InferenceJobError(f"inference_job_{kind}_json_invalid")

    visit(value, 0)


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise InferenceJobError(code)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise InferenceJobError(code) from None
    if size > _MAX_IDENTIFIER_BYTES:
        raise InferenceJobError(code)
    return value


def _binding_project_id(binding: RerankRequestBinding | Mapping[str, object]) -> object:
    if isinstance(binding, RerankRequestBinding):
        return binding.project_id
    if isinstance(binding, Mapping):
        return binding.get("project_id")
    return None


def _bounded_integer(value: object, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InferenceJobError(code)
    return value


def _positive_integer(value: object, code: str) -> int:
    return _bounded_integer(value, minimum=1, maximum=2**31 - 1, code=code)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _material_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _lease_token_hash(lease_token: object) -> str:
    if not isinstance(lease_token, str) or not lease_token or "\x00" in lease_token:
        raise InferenceJobError("inference_job_lease_token_invalid")
    try:
        encoded = lease_token.encode("utf-8")
    except UnicodeError:
        raise InferenceJobError("inference_job_lease_token_invalid") from None
    if len(encoded) > 2_048:
        raise InferenceJobError("inference_job_lease_token_invalid")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InferenceJobError("inference_job_store_corrupt")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise InferenceJobError("inference_job_store_corrupt") from None
    if parsed.utcoffset() != timedelta(0):
        raise InferenceJobError("inference_job_store_corrupt")
    return parsed


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise InferenceJobError("inference_job_store_corrupt") from None
    if not isinstance(copied, dict):
        raise InferenceJobError("inference_job_store_corrupt")
    return copied


# Explicit aliases keep gateway naming concise without creating a second store.
RerankJob = InferenceJob
RerankJobLease = InferenceJobLease
RerankJobStore = InferenceJobStore

__all__ = [
    "InferenceJob",
    "InferenceJobConflictError",
    "InferenceJobCreateResult",
    "InferenceJobError",
    "InferenceJobLease",
    "InferenceJobNotFoundError",
    "InferenceJobStore",
    "RerankJob",
    "RerankJobLease",
    "RerankJobStore",
]
