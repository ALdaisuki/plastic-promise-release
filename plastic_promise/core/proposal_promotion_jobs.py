"""Durable evaluation jobs for eligible passive-memory proposals."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any

from plastic_promise.core.derived_work import DerivedWorkLease, DerivedWorkStore
from plastic_promise.core.proposal_promotion import (
    VectorEvidenceRequest,
    auto_promotion_mode,
    collect_vector_evidence_batch,
    ensure_proposal_automation_schema,
    evaluate_auto_promotion,
)

PROMOTION_JOB_KIND = "proposal_promotion"
PROMOTION_SCHEMA_VERSION = "proposal-promotion-job-v1"
PROMOTION_CONFIG_REVISION = "proposal-promotion-policy-v1"

_RUNTIMES: dict[int, DurableProposalPromotionWorker] = {}
_RUNTIMES_LOCK = threading.Lock()
_RUNTIME_FAILURES: dict[int, str] = {}
_LOGGER = logging.getLogger(__name__)


class PromotionJobBindingError(RuntimeError):
    pass


class PromotionJobEvaluationError(RuntimeError):
    pass


class DurableProposalPromotionWorker:
    """Evaluate durable eligible-proposal jobs with bounded retry."""

    def __init__(
        self,
        engine: Any,
        store: DerivedWorkStore,
        *,
        batch_size: int = 20,
        max_wait_seconds: float = 5.0,
        lease_seconds: int = 180,
        retry_delay_seconds: int = 10,
        poll_seconds: float = 1.0,
        autostart: bool = True,
    ) -> None:
        self._engine = engine
        self._store = store
        self._batch_size = min(64, max(1, int(batch_size)))
        self._max_wait_seconds = min(3600.0, max(0.0, float(max_wait_seconds)))
        self._lease_seconds = min(15 * 60, max(1, int(lease_seconds)))
        self._retry_delay_seconds = min(3600, max(0, int(retry_delay_seconds)))
        self._poll_seconds = min(60.0, max(0.05, float(poll_seconds)))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        if autostart:
            self.start()

    @property
    def store(self) -> DerivedWorkStore:
        return self._store

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="proposal-promotion-durable",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self, *, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, float(timeout)))
        drained = self._thread is None or not self._thread.is_alive()
        if drained:
            self._thread = None
        return drained

    def run_once(self, *, raise_errors: bool = False) -> bool:
        leases = self._store.claim_batch(
            limit=self._batch_size,
            job_kind=PROMOTION_JOB_KIND,
            provider_identity=proposal_promotion_identity(self._engine),
            min_batch_size=self._batch_size,
            max_wait_seconds=self._max_wait_seconds,
            lease_seconds=self._lease_seconds,
        )
        if not leases:
            return False
        first_error: BaseException | None = None
        try:
            active, completed = self._validate_bindings(leases)
            for lease, result in completed:
                self._complete(lease, result)
            if active:
                vector_results = collect_vector_evidence_batch(
                    self._engine,
                    [VectorEvidenceRequest(lease.job.subject_id) for lease in active],
                )
                for lease, vector_result in zip(active, vector_results, strict=True):
                    try:
                        if vector_result.get("status") not in {"recorded", "skipped"}:
                            raise PromotionJobEvaluationError(
                                str(
                                    vector_result.get("reason") or "proposal_vector_evidence_failed"
                                )
                            )
                        promotion = evaluate_auto_promotion(self._engine, lease.job.subject_id)
                        if promotion.get("status") == "degraded":
                            raise PromotionJobEvaluationError(
                                str(promotion.get("reason") or "proposal_promotion_degraded")
                            )
                        self._complete(
                            lease,
                            {
                                "schema": PROMOTION_SCHEMA_VERSION,
                                "proposal_id": lease.job.subject_id,
                                "vector_status": vector_result.get("status"),
                                "promotion_status": promotion.get("status"),
                                "promotion_reason": promotion.get("reason"),
                                "evaluated_score_revision": int(
                                    promotion.get("score_revision")
                                    or lease.job.payload.get("score_revision")
                                    or 0
                                ),
                                "memory_id": promotion.get("memory_id"),
                            },
                        )
                    except BaseException as exc:
                        self._fail(lease, exc)
                        first_error = first_error or exc
        except BaseException as exc:
            for lease in leases:
                try:
                    current = self._store.get(
                        job_id=lease.job.job_id,
                        project_id=lease.job.project_id,
                    )
                    if current.status == "leased":
                        self._fail(lease, exc)
                except Exception:
                    continue
            first_error = first_error or exc
        if raise_errors and first_error is not None:
            raise first_error
        return True

    def _validate_bindings(
        self,
        leases: tuple[DerivedWorkLease, ...],
    ) -> tuple[list[DerivedWorkLease], list[tuple[DerivedWorkLease, dict[str, Any]]]]:
        connection = getattr(getattr(self._engine, "_sqlite", None), "_conn", None)
        if not isinstance(connection, sqlite3.Connection):
            raise PromotionJobBindingError("proposal_promotion_store_unavailable")
        active: list[DerivedWorkLease] = []
        completed: list[tuple[DerivedWorkLease, dict[str, Any]]] = []
        lock = getattr(self._engine, "_write_lock", threading.RLock())
        with lock:
            for lease in leases:
                job = lease.job
                row = connection.execute(
                    """
                    SELECT p.project_id, p.visibility, p.content_hash, p.status,
                           s.eligible, s.score_revision, s.blocked_reason,
                           p.promoted_memory_id, p.approval_actor
                      FROM memory_proposals AS p
                      LEFT JOIN memory_proposal_scores AS s ON s.proposal_id = p.proposal_id
                     WHERE p.proposal_id = ?
                    """,
                    (job.subject_id,),
                ).fetchone()
                if (
                    row is None
                    or job.payload.get("schema") != PROMOTION_SCHEMA_VERSION
                    or job.payload.get("proposal_id") != job.subject_id
                    or str(job.payload.get("content_hash") or "") != job.subject_hash
                    or str(row[0]) != job.project_id
                    or str(row[1]) != job.visibility
                    or str(row[2]) != job.subject_hash
                    or str(job.payload.get("policy_mode") or "") != auto_promotion_mode()
                    or job.provider_identity != proposal_promotion_identity(self._engine)
                ):
                    raise PromotionJobBindingError("proposal_promotion_binding_changed")
                if str(row[3]) == "adopted" and str(row[8]) == "system:auto-proposal-promoter":
                    completed.append(
                        (
                            lease,
                            {
                                "schema": PROMOTION_SCHEMA_VERSION,
                                "proposal_id": job.subject_id,
                                "promotion_status": "promoted",
                                "promotion_reason": None,
                                "vector_status": "recovered",
                                "evaluated_score_revision": int(row[5] or 0),
                                "memory_id": str(row[7]),
                                "recovered_after_commit": True,
                            },
                        )
                    )
                elif str(row[3]) != "pending":
                    completed.append(
                        (
                            lease,
                            {
                                "schema": PROMOTION_SCHEMA_VERSION,
                                "proposal_id": job.subject_id,
                                "promotion_status": "skipped",
                                "promotion_reason": "proposal_not_pending",
                                "vector_status": "skipped",
                                "evaluated_score_revision": int(row[5] or 0),
                            },
                        )
                    )
                elif not bool(row[4]):
                    completed.append(
                        (
                            lease,
                            {
                                "schema": PROMOTION_SCHEMA_VERSION,
                                "proposal_id": job.subject_id,
                                "promotion_status": "ineligible",
                                "promotion_reason": str(row[6] or "proposal_not_eligible"),
                                "vector_status": "skipped",
                                "evaluated_score_revision": int(row[5] or 0),
                            },
                        )
                    )
                else:
                    active.append(lease)
        return active, completed

    def _complete(self, lease: DerivedWorkLease, result: dict[str, Any]) -> None:
        self._store.complete(
            job_id=lease.job.job_id,
            project_id=lease.job.project_id,
            lease_token=lease.lease_token,
            fencing_generation=lease.job.fencing_generation,
            result=result,
        )

    def _fail(self, lease: DerivedWorkLease, error: BaseException) -> None:
        binding_failure = isinstance(error, PromotionJobBindingError)
        self._store.fail(
            job_id=lease.job.job_id,
            project_id=lease.job.project_id,
            lease_token=lease.lease_token,
            fencing_generation=lease.job.fencing_generation,
            failure_code=(
                "proposal_promotion_binding_changed"
                if binding_failure
                else "proposal_promotion_evaluation_failed"
            ),
            retryable=not binding_failure,
            retry_delay_seconds=self._retry_delay_seconds,
        )

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            if self.run_once():
                continue
            self._wake.wait(self._poll_seconds)
            self._wake.clear()


def proposal_promotion_identity(engine: Any) -> str:
    embedder = getattr(engine, "_embedder", None)
    embedding_identity = str(
        getattr(embedder, "index_model_name", "")
        or getattr(embedder, "model_name", "")
        or "unresolved"
    ).strip()
    material = {
        "schema": PROMOTION_SCHEMA_VERSION,
        "policy_mode": auto_promotion_mode(),
        "embedding_identity": embedding_identity,
        "require_vector": os.getenv("PP_MEMORY_PROPOSAL_REQUIRE_VECTOR", "1") != "0",
        "threshold": os.getenv("PP_MEMORY_PROPOSAL_AUTO_THRESHOLD", "0.82"),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"proposal-promotion:sha256:{digest}"


def enqueue_proposal_promotion_job(
    engine: Any,
    proposal_id: str,
    *,
    store: DerivedWorkStore | None = None,
) -> dict[str, Any]:
    connection = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if not isinstance(connection, sqlite3.Connection):
        return {"status": "skipped", "reason": "canonical_store_unavailable"}
    ensure_proposal_automation_schema(connection)
    row = connection.execute(
        """
        SELECT p.proposal_id, p.project_id, p.visibility, p.content_hash,
               p.status, s.eligible, s.score_revision
          FROM memory_proposals AS p
          JOIN memory_proposal_scores AS s ON s.proposal_id = p.proposal_id
         WHERE p.proposal_id = ?
        """,
        (str(proposal_id or "").strip(),),
    ).fetchone()
    if row is None:
        return {"status": "skipped", "reason": "proposal_score_missing"}
    if str(row[4]) != "pending" or not bool(row[5]):
        return {"status": "skipped", "reason": "proposal_not_eligible"}
    selected_store = store or DerivedWorkStore(_canonical_db_path(connection))
    provider_identity = proposal_promotion_identity(engine)
    policy_mode = auto_promotion_mode()
    score_revision = int(row[6])
    completed = connection.execute(
        """
        SELECT job_id
          FROM derived_work_jobs
         WHERE project_id = ? AND job_kind = ? AND config_revision = ?
           AND provider_identity = ? AND subject_id = ? AND subject_hash = ?
           AND status = 'completed'
           AND CAST(json_extract(result_json, '$.evaluated_score_revision') AS INTEGER) = ?
         ORDER BY completed_at DESC, job_id DESC
         LIMIT 1
        """,
        (
            str(row[1]),
            PROMOTION_JOB_KIND,
            PROMOTION_CONFIG_REVISION,
            provider_identity,
            str(row[0]),
            str(row[3]),
            score_revision,
        ),
    ).fetchone()
    if completed is not None:
        return {
            "status": "reused",
            "job_id": str(completed[0]),
            "project_id": str(row[1]),
            "score_revision": score_revision,
            "worker_available": initialize_proposal_promotion_runtime(engine) is not None,
        }
    material = {
        "schema": PROMOTION_SCHEMA_VERSION,
        "proposal_id": str(row[0]),
        "content_hash": str(row[3]),
        "score_revision": score_revision,
        "policy_mode": policy_mode,
        "provider_identity": provider_identity,
    }
    dedupe_key = (
        "proposal-promotion:"
        + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    result = selected_store.enqueue(
        project_id=str(row[1]),
        visibility=str(row[2]),
        config_revision=PROMOTION_CONFIG_REVISION,
        job_kind=PROMOTION_JOB_KIND,
        provider_identity=provider_identity,
        subject_id=str(row[0]),
        subject_hash=str(row[3]),
        dedupe_key=dedupe_key,
        payload={
            "schema": PROMOTION_SCHEMA_VERSION,
            "proposal_id": str(row[0]),
            "content_hash": str(row[3]),
            "score_revision": score_revision,
            "policy_mode": policy_mode,
        },
        max_attempts=_bounded_int("PP_PROPOSAL_PROMOTION_MAX_ATTEMPTS", 4, 1, 20),
        max_active_jobs=_bounded_int("PP_PROPOSAL_PROMOTION_MAX_QUEUE", 4096, 1, 100_000),
    )
    runtime = initialize_proposal_promotion_runtime(engine)
    if runtime is not None:
        runtime.wake()
    return {
        "status": "created" if result.created else "reused",
        "job_id": result.job.job_id,
        "project_id": result.job.project_id,
        "score_revision": score_revision,
        "worker_available": runtime is not None,
    }


def reconcile_proposal_promotion_jobs(engine: Any, *, limit: int = 100) -> dict[str, Any]:
    if auto_promotion_mode() == "off":
        return {
            "eligible": 0,
            "created": 0,
            "reused": 0,
            "skipped": 0,
            "job_ids": [],
            "reason": "auto_promotion_disabled",
        }
    connection = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
        return {
            "eligible": 0,
            "created": 0,
            "reused": 0,
            "skipped": 0,
            "job_ids": [],
            "reason": "canonical_store_unavailable",
        }
    ensure_proposal_automation_schema(connection)
    bounded_limit = min(1000, max(1, int(limit)))
    store = DerivedWorkStore(_canonical_db_path(connection))
    provider_identity = proposal_promotion_identity(engine)
    policy_mode = auto_promotion_mode()
    rows = connection.execute(
        """
        SELECT p.proposal_id,
               CASE WHEN EXISTS (
                   SELECT 1
                     FROM derived_work_jobs AS j
                    WHERE j.project_id = p.project_id
                      AND j.job_kind = ?
                      AND j.config_revision = ?
                      AND j.provider_identity = ?
                      AND j.subject_id = p.proposal_id
                      AND j.subject_hash = p.content_hash
                      AND json_extract(j.payload_json, '$.schema') = ?
                      AND json_extract(j.payload_json, '$.policy_mode') = ?
                      AND (
                          CAST(json_extract(j.payload_json, '$.score_revision') AS INTEGER)
                              = s.score_revision
                          OR (
                              j.status = 'completed'
                              AND CAST(
                                  json_extract(j.result_json, '$.evaluated_score_revision')
                                  AS INTEGER
                              ) = s.score_revision
                          )
                      )
               ) THEN 1 ELSE 0 END AS has_current_job
          FROM memory_proposals AS p
          JOIN memory_proposal_scores AS s ON s.proposal_id = p.proposal_id
         WHERE p.status = 'pending' AND s.eligible = 1
         ORDER BY has_current_job, s.updated_at, p.proposal_id
         LIMIT ?
        """,
        (
            PROMOTION_JOB_KIND,
            PROMOTION_CONFIG_REVISION,
            provider_identity,
            PROMOTION_SCHEMA_VERSION,
            policy_mode,
            bounded_limit,
        ),
    ).fetchall()
    report: dict[str, Any] = {
        "eligible": len(rows),
        "created": 0,
        "reused": 0,
        "skipped": 0,
        "job_ids": [],
    }
    for row in rows:
        result = enqueue_proposal_promotion_job(engine, str(row[0]), store=store)
        status = str(result.get("status") or "")
        if status in {"created", "reused"}:
            report[status] += 1
            report["job_ids"].append(str(result["job_id"]))
        else:
            report["skipped"] += 1
    return report


def initialize_proposal_promotion_runtime(
    engine: Any,
    *,
    autostart: bool | None = None,
) -> DurableProposalPromotionWorker | None:
    """Return the process worker only when proposal automation is enabled."""

    if auto_promotion_mode() == "off":
        return None
    engine_id = id(engine)
    with _RUNTIMES_LOCK:
        if engine_id in _RUNTIMES:
            return _RUNTIMES[engine_id]
        if engine_id in _RUNTIME_FAILURES:
            return None
        connection = getattr(getattr(engine, "_sqlite", None), "_conn", None)
        if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
            return None
        db_path = _canonical_db_path(connection)
        if not db_path or db_path == ":memory:":
            _RUNTIME_FAILURES[engine_id] = "proposal_promotion_runtime_db_unavailable"
            return None
        try:
            should_start = (
                os.getenv("PP_PROPOSAL_PROMOTION_WORKER_AUTOSTART", "1").strip() == "1"
                if autostart is None
                else bool(autostart)
            )
            lease_seconds = _bounded_int("PP_PROPOSAL_PROMOTION_LEASE_SECONDS", 180, 1, 15 * 60)
            runtime = DurableProposalPromotionWorker(
                engine,
                DerivedWorkStore(db_path, default_lease_seconds=lease_seconds),
                batch_size=_bounded_int("PP_PROPOSAL_PROMOTION_BATCH_SIZE", 20, 1, 64),
                max_wait_seconds=_bounded_float(
                    "PP_PROPOSAL_PROMOTION_MAX_WAIT_SECONDS", 5.0, 0.0, 3600.0
                ),
                lease_seconds=lease_seconds,
                retry_delay_seconds=_bounded_int(
                    "PP_PROPOSAL_PROMOTION_RETRY_SECONDS", 10, 0, 3600
                ),
                poll_seconds=_bounded_float("PP_PROPOSAL_PROMOTION_POLL_SECONDS", 1.0, 0.05, 60.0),
                autostart=should_start,
            )
        except Exception as exc:
            _RUNTIME_FAILURES[engine_id] = "proposal_promotion_runtime_init_failed"
            _LOGGER.error(
                "proposal_promotion_runtime_init_failed exception_type=%s",
                exc.__class__.__name__,
            )
            return None
        _RUNTIMES[engine_id] = runtime
        return runtime


def process_proposal_promotion_jobs(engine: Any, *, max_batches: int = 1) -> dict[str, Any]:
    """Synchronously drain a bounded number of batches for Maintenance."""

    runtime = initialize_proposal_promotion_runtime(engine, autostart=False)
    if runtime is None:
        reason = (
            "auto_promotion_disabled"
            if auto_promotion_mode() == "off"
            else "proposal_promotion_runtime_unavailable"
        )
        result = {"skipped": reason, "processed_batches": 0}
        if reason != "auto_promotion_disabled":
            result["failure_code"] = _RUNTIME_FAILURES.get(
                id(engine), "proposal_promotion_runtime_unavailable"
            )
        return result
    processed = 0
    for _index in range(min(100, max(0, int(max_batches)))):
        if not runtime.run_once():
            break
        processed += 1
    return {"processed_batches": processed}


def close_proposal_promotion_runtime(engine: Any, *, timeout: float = 5.0) -> bool:
    """Stop and forget one process-local worker during controlled shutdown."""

    with _RUNTIMES_LOCK:
        runtime = _RUNTIMES.pop(id(engine), None)
        _RUNTIME_FAILURES.pop(id(engine), None)
    return runtime.close(timeout=timeout) if runtime is not None else False


def _canonical_db_path(connection: sqlite3.Connection) -> str:
    return next(
        (
            str(row[2] or "").strip()
            for row in connection.execute("PRAGMA database_list").fetchall()
            if len(row) >= 3 and str(row[1]) == "main"
        ),
        "",
    )


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))
