"""Durable, non-generative background work for the accelerator-max module.

This module deliberately owns no canonical-memory repository and exposes no
SQLite write capability to executors.  It uses the existing derived-work
outbox for every durable lifecycle transition.  The separate daily-admission
counter is only a scheduler budget ledger, never a second task queue or a
source of canonical memory truth.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from plastic_promise.core.derived_work import (
    DerivedWorkCreateResult,
    DerivedWorkLease,
    DerivedWorkStore,
)
from plastic_promise.core.node_governance import (
    AcceleratorBudget,
    NodeGovernanceError,
    is_accelerator_task_kind,
)

_IDENTIFIER_RE = re.compile(r"\A[a-z][a-z0-9_.:-]{1,127}\Z")
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_ACCELERATOR_JOB_KIND = "accelerator-max"
_FOREGROUND_PRIORITY_FLOOR = 100
_DEFAULT_LEASE_SECONDS = 60
_MAX_LEASE_SECONDS = 15 * 60
_MAX_RESULT_BYTES = 1024 * 1024
_ALLOWED_ARTIFACT_KINDS = frozenset({"proposal", "outbox", "evidence", "derived-result"})
_AUDITED_ADMISSION_REASONS = frozenset(
    {
        "accelerator_disabled",
        "accelerator_queue_budget_exhausted",
        "accelerator_daily_budget_exhausted",
    }
)
_AUDITED_SCHEDULER_REASONS = frozenset(
    {
        "accelerator_disabled",
        "accelerator_capacity_evidence_stale",
        "accelerator_memory_budget_exhausted",
        "accelerator_foreground_work_pending",
        "accelerator_concurrency_budget_exhausted",
    }
)
_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "canonical-memory",
        "canonical_memory",
        "canonical-write",
        "canonical_write",
        "memory-store",
        "memory_store",
        "memory-update",
        "memory_update",
        "memory-forget",
        "memory_forget",
        "sqlite-write",
        "sqlite_write",
    }
)


class AcceleratorMaxError(RuntimeError):
    """A stable non-sensitive accelerator-max failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AcceleratorTaskRequest:
    """One project-scoped, non-canonical background task request."""

    project_id: str
    visibility: str
    config_revision: str
    task_kind: str
    provider_identity: str
    subject_id: str
    subject_hash: str
    idempotency_key: str
    payload: Mapping[str, object] = field(repr=False)
    max_attempts: int = 4

    def __post_init__(self) -> None:
        _identifier(self.project_id, "accelerator_project_id_invalid")
        if self.visibility not in {"private", "project", "shared", "global"}:
            raise AcceleratorMaxError("accelerator_visibility_invalid")
        _text(self.config_revision, "accelerator_config_revision_invalid")
        if not is_accelerator_task_kind(self.task_kind):
            raise AcceleratorMaxError("accelerator_task_kind_forbidden")
        _text(self.provider_identity, "accelerator_provider_identity_invalid")
        _text(self.subject_id, "accelerator_subject_id_invalid")
        if (
            not isinstance(self.subject_hash, str)
            or _SHA256_RE.fullmatch(self.subject_hash) is None
        ):
            raise AcceleratorMaxError("accelerator_subject_hash_invalid")
        _text(self.idempotency_key, "accelerator_idempotency_key_invalid")
        if not isinstance(self.payload, Mapping):
            raise AcceleratorMaxError("accelerator_payload_invalid")
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool):
            raise AcceleratorMaxError("accelerator_max_attempts_invalid")
        if not 1 <= self.max_attempts <= 32:
            raise AcceleratorMaxError("accelerator_max_attempts_invalid")


@dataclass(frozen=True)
class AcceleratorCapacityEvidence:
    """Fresh non-secret capacity observation captured before a worker claim."""

    free_memory_mib: int
    observed_at: datetime
    max_age_seconds: int = 120

    def __post_init__(self) -> None:
        if (
            not isinstance(self.free_memory_mib, int)
            or isinstance(self.free_memory_mib, bool)
            or self.free_memory_mib < 0
        ):
            raise AcceleratorMaxError("accelerator_free_memory_invalid")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise AcceleratorMaxError("accelerator_capacity_observed_at_invalid")
        if (
            not isinstance(self.max_age_seconds, int)
            or isinstance(self.max_age_seconds, bool)
            or not 1 <= self.max_age_seconds <= 3600
        ):
            raise AcceleratorMaxError("accelerator_capacity_max_age_invalid")

    def is_fresh(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise AcceleratorMaxError("accelerator_clock_invalid")
        age_seconds = (
            now.astimezone(timezone.utc) - self.observed_at.astimezone(timezone.utc)
        ).total_seconds()
        return 0 <= age_seconds <= self.max_age_seconds


@dataclass(frozen=True)
class AcceleratorWorkLease:
    """An opaque derived-work lease plus a validated immutable request."""

    derived_lease: DerivedWorkLease
    request: AcceleratorTaskRequest


@dataclass(frozen=True)
class AcceleratorExecutionResult:
    """One bounded artifact for later server-side promotion or reconciliation."""

    artifact_kind: str
    artifact: Mapping[str, object] = field(default_factory=dict)
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.artifact_kind not in _ALLOWED_ARTIFACT_KINDS:
            raise AcceleratorMaxError("accelerator_artifact_kind_invalid")
        if not isinstance(self.artifact, Mapping) or not isinstance(self.evidence, Mapping):
            raise AcceleratorMaxError("accelerator_result_invalid")
        _reject_canonical_write_keys(self.artifact)
        _reject_canonical_write_keys(self.evidence)
        _bounded_json(
            {
                "artifact_kind": self.artifact_kind,
                "artifact": self.artifact,
                "evidence": self.evidence,
            }
        )


class AcceleratorExecutionFailure(RuntimeError):
    """A retry classification without endpoint, payload, or model details."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        _identifier(code, "accelerator_failure_code_invalid")
        if not isinstance(retryable, bool):
            raise AcceleratorMaxError("accelerator_failure_retryable_invalid")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class AcceleratorTaskExecutor(Protocol):
    """Worker seam with no canonical SQLite repository or mutation API."""

    def execute(self, lease: AcceleratorWorkLease) -> AcceleratorExecutionResult: ...


@dataclass(frozen=True)
class AcceleratorTaskRun:
    """A compact, externally safe scheduler outcome."""

    job_id: str | None
    project_id: str
    outcome: str
    reason: str | None = None


class AcceleratorMaxCoordinator:
    """Admission, priority protection, retry and reconciliation for background work.

    It cannot invoke a canonical-memory mutation.  A completed result remains
    an inert derived artifact until an independently governed server path
    validates and promotes it.
    """

    def __init__(
        self,
        *,
        derived_work: DerivedWorkStore,
        retry_delay_seconds: int = 30,
        foreground_priority_floor: int = _FOREGROUND_PRIORITY_FLOOR,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(derived_work, DerivedWorkStore):
            raise AcceleratorMaxError("accelerator_derived_work_store_invalid")
        if not isinstance(retry_delay_seconds, int) or not 0 <= retry_delay_seconds <= 3600:
            raise AcceleratorMaxError("accelerator_retry_delay_invalid")
        if (
            not isinstance(foreground_priority_floor, int)
            or isinstance(foreground_priority_floor, bool)
            or not -1000 <= foreground_priority_floor <= 1000
        ):
            raise AcceleratorMaxError("accelerator_foreground_priority_invalid")
        if clock is not None and not callable(clock):
            raise AcceleratorMaxError("accelerator_clock_invalid")
        self._derived_work = derived_work
        self._retry_delay_seconds = retry_delay_seconds
        self._foreground_priority_floor = foreground_priority_floor
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enqueue(
        self,
        request: AcceleratorTaskRequest,
        *,
        budget: AcceleratorBudget,
    ) -> DerivedWorkCreateResult:
        """Atomically admit one allowed task or fail with a stable budget reason."""

        if not isinstance(request, AcceleratorTaskRequest):
            raise AcceleratorMaxError("accelerator_request_invalid")
        _budget(budget)
        if not budget.enabled:
            self._record_audit(
                event="admission",
                task_kind=request.task_kind,
                decision="denied",
                reason="accelerator_disabled",
            )
            raise AcceleratorMaxError("accelerator_disabled")
        try:
            return self._derived_work.enqueue_accelerator(
                project_id=request.project_id,
                visibility=request.visibility,
                config_revision=request.config_revision,
                job_kind=_ACCELERATOR_JOB_KIND,
                provider_identity=request.provider_identity,
                subject_id=request.subject_id,
                subject_hash=request.subject_hash,
                dedupe_key="accelerator-max:" + request.idempotency_key,
                payload={"task_kind": request.task_kind, "payload": dict(request.payload)},
                priority=0,
                max_attempts=request.max_attempts,
                max_queue_depth=budget.max_queue_depth,
                max_daily_tasks=budget.max_daily_tasks,
            )
        except NodeGovernanceError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "accelerator_enqueue_failed")
            if code in _AUDITED_ADMISSION_REASONS:
                self._record_audit(
                    event="admission",
                    task_kind=request.task_kind,
                    decision="denied",
                    reason=str(code),
                )
            raise AcceleratorMaxError(str(code)) from exc

    def run_next(
        self,
        *,
        project_id: str,
        executor: AcceleratorTaskExecutor,
        budget: AcceleratorBudget,
        capacity: AcceleratorCapacityEvidence,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> AcceleratorTaskRun:
        """Execute at most one low-priority task after durable resource checks."""

        _identifier(project_id, "accelerator_project_id_invalid")
        _budget(budget)
        if not callable(getattr(executor, "execute", None)):
            raise AcceleratorMaxError("accelerator_executor_invalid")
        if not isinstance(capacity, AcceleratorCapacityEvidence):
            raise AcceleratorMaxError("accelerator_capacity_invalid")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise AcceleratorMaxError("accelerator_lease_seconds_invalid")
        if not budget.enabled:
            return self._deferred(project_id, "accelerator_disabled")
        now = self._now()
        if not capacity.is_fresh(now):
            return self._deferred(project_id, "accelerator_capacity_evidence_stale")
        if capacity.free_memory_mib < budget.min_free_memory_mib:
            return self._deferred(project_id, "accelerator_memory_budget_exhausted")
        decision = self._derived_work.claim_next_accelerator(
            project_id=project_id,
            job_kind=_ACCELERATOR_JOB_KIND,
            max_concurrency=budget.max_concurrency,
            foreground_priority_floor=self._foreground_priority_floor,
            lease_seconds=lease_seconds,
        )
        if decision.lease is None:
            return self._deferred(project_id, decision.reason)
        request = _request_from_job(decision.lease)
        work_lease = AcceleratorWorkLease(decision.lease, request)
        try:
            result = executor.execute(work_lease)
            if not isinstance(result, AcceleratorExecutionResult):
                raise AcceleratorExecutionFailure("accelerator_execution_result_invalid")
            self._derived_work.complete(
                job_id=work_lease.derived_lease.job.job_id,
                project_id=project_id,
                lease_token=work_lease.derived_lease.lease_token,
                fencing_generation=work_lease.derived_lease.job.fencing_generation,
                result={
                    "outcome": "completed",
                    "task_kind": request.task_kind,
                    "artifact_kind": result.artifact_kind,
                    "artifact": dict(result.artifact),
                    "evidence": dict(result.evidence),
                },
            )
            return AcceleratorTaskRun(work_lease.derived_lease.job.job_id, project_id, "completed")
        except AcceleratorExecutionFailure as exc:
            return self._fail(work_lease, exc.code, exc.retryable)
        except AcceleratorMaxError as exc:
            return self._fail(work_lease, exc.code, False)
        except Exception:
            return self._fail(work_lease, "accelerator_execution_failed", True)

    def reconcile(self, *, project_id: str | None = None) -> dict[str, int]:
        """Recover only expired derived leases; canonical data is never changed."""

        if project_id is not None:
            _identifier(project_id, "accelerator_project_id_invalid")
        return {"derived_work_recovered": self._derived_work.recover_expired(project_id=project_id)}

    def status(self, *, project_id: str | None = None) -> dict[str, object]:
        """Return queue lifecycle and current UTC admission consumption."""

        if project_id is not None:
            _identifier(project_id, "accelerator_project_id_invalid")
        metrics = self._derived_work.stats(project_id=project_id, job_kind=_ACCELERATOR_JOB_KIND)
        return {
            "job_kind": _ACCELERATOR_JOB_KIND,
            "derived_work": metrics,
            "daily_admissions": self._derived_work.daily_admissions(job_kind=_ACCELERATOR_JOB_KIND),
        }

    def _fail(
        self,
        lease: AcceleratorWorkLease,
        failure_code: str,
        retryable: bool,
    ) -> AcceleratorTaskRun:
        self._derived_work.fail(
            job_id=lease.derived_lease.job.job_id,
            project_id=lease.derived_lease.job.project_id,
            lease_token=lease.derived_lease.lease_token,
            fencing_generation=lease.derived_lease.job.fencing_generation,
            failure_code=failure_code,
            retryable=retryable,
            retry_delay_seconds=self._retry_delay_seconds,
        )
        return AcceleratorTaskRun(
            lease.derived_lease.job.job_id,
            lease.derived_lease.job.project_id,
            "retry_wait" if retryable else "dead",
            failure_code,
        )

    def _deferred(self, project_id: str, reason: str) -> AcceleratorTaskRun:
        if reason in _AUDITED_SCHEDULER_REASONS:
            self._record_audit(
                event="scheduler",
                task_kind="scheduler",
                decision="deferred",
                reason=reason,
            )
        return AcceleratorTaskRun(None, project_id, "deferred", reason)

    def _record_audit(
        self,
        *,
        event: str,
        task_kind: str,
        decision: str,
        reason: str,
    ) -> None:
        try:
            self._derived_work.record_accelerator_audit_event(
                event=event,
                task_kind=task_kind,
                decision=decision,
                reason=reason,
            )
        except Exception as exc:
            code = getattr(exc, "code", "accelerator_audit_unavailable")
            raise AcceleratorMaxError(str(code)) from exc

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AcceleratorMaxError("accelerator_clock_invalid")
        return value.astimezone(timezone.utc)


def budget_from_node_routing_config(config: Mapping[str, object]) -> AcceleratorBudget:
    """Build a scheduler budget from the already safe control-plane projection."""

    if not isinstance(config, Mapping):
        raise AcceleratorMaxError("accelerator_config_invalid")
    try:
        return AcceleratorBudget(
            enabled=_boolean(config["accelerator_max_enabled"]),
            max_concurrency=_nonnegative_int(config["accelerator_max_concurrency"]),
            max_queue_depth=_nonnegative_int(config["accelerator_max_queue_depth"]),
            max_daily_tasks=_nonnegative_int(config["accelerator_max_daily_tasks"]),
            min_free_memory_mib=_nonnegative_int(config["accelerator_min_free_memory_mib"]),
        )
    except (KeyError, NodeGovernanceError, AcceleratorMaxError) as exc:
        raise AcceleratorMaxError("accelerator_config_invalid") from exc


def _request_from_job(lease: DerivedWorkLease) -> AcceleratorTaskRequest:
    job = lease.job
    payload = job.payload
    task_kind = payload.get("task_kind")
    task_payload = payload.get("payload")
    if not isinstance(task_payload, Mapping):
        raise AcceleratorMaxError("accelerator_job_payload_invalid")
    return AcceleratorTaskRequest(
        project_id=job.project_id,
        visibility=job.visibility,
        config_revision=job.config_revision,
        task_kind=str(task_kind),
        provider_identity=job.provider_identity,
        subject_id=job.subject_id,
        subject_hash=job.subject_hash,
        idempotency_key="persisted:" + job.job_id,
        payload=task_payload,
        max_attempts=job.max_attempts,
    )


def _budget(value: object) -> AcceleratorBudget:
    if not isinstance(value, AcceleratorBudget):
        raise AcceleratorMaxError("accelerator_budget_invalid")
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise AcceleratorMaxError(code)
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise AcceleratorMaxError(code)
    return value


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AcceleratorMaxError("accelerator_config_invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise AcceleratorMaxError("accelerator_config_invalid")
    return value


def _reject_canonical_write_keys(value: Mapping[str, object]) -> None:
    for key, child in value.items():
        if not isinstance(key, str):
            raise AcceleratorMaxError("accelerator_result_invalid")
        if key.casefold() in _FORBIDDEN_ARTIFACT_KEYS:
            raise AcceleratorMaxError("accelerator_canonical_write_forbidden")
        if isinstance(child, Mapping):
            _reject_canonical_write_keys(child)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, Mapping):
                    _reject_canonical_write_keys(item)


def _bounded_json(value: Mapping[str, object]) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise AcceleratorMaxError("accelerator_result_invalid") from None
    if len(encoded.encode()) > _MAX_RESULT_BYTES:
        raise AcceleratorMaxError("accelerator_result_too_large")
