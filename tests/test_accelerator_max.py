"""Durable safety tests for the non-generative accelerator-max scheduler."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.core.accelerator_max import (
    AcceleratorCapacityEvidence,
    AcceleratorExecutionFailure,
    AcceleratorExecutionResult,
    AcceleratorMaxCoordinator,
    AcceleratorMaxError,
    AcceleratorTaskRequest,
    budget_from_node_routing_config,
)
from plastic_promise.core.derived_work import DerivedWorkError, DerivedWorkStore
from plastic_promise.core.node_governance import AcceleratorBudget
from plastic_promise.core.node_governance_schema import apply_node_governance_schema


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 6, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _budget(**overrides: object) -> AcceleratorBudget:
    values: dict[str, object] = {
        "enabled": True,
        "max_concurrency": 1,
        "max_queue_depth": 4,
        "max_daily_tasks": 4,
        "min_free_memory_mib": 512,
    }
    values.update(overrides)
    return AcceleratorBudget(**values)  # type: ignore[arg-type]


def _request(
    *,
    project_id: str = "project:alpha",
    task_kind: str = "semantic-dedupe",
    key: str = "one",
) -> AcceleratorTaskRequest:
    return AcceleratorTaskRequest(
        project_id=project_id,
        visibility="project",
        config_revision="cfg-20260806T000000Z-000000000000",
        task_kind=task_kind,
        provider_identity="local-accelerator-v1",
        subject_id="memory:subject-" + key,
        subject_hash=_hash("subject-" + key),
        idempotency_key=key,
        payload={"references": ["memory:subject-" + key]},
    )


def _capacity(clock: Clock, *, free_memory_mib: int = 2048) -> AcceleratorCapacityEvidence:
    return AcceleratorCapacityEvidence(free_memory_mib, clock())


@dataclass
class RecordingExecutor:
    calls: int = 0
    fail_once: bool = False

    def execute(self, lease):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise AcceleratorExecutionFailure("accelerator_provider_unavailable")
        return AcceleratorExecutionResult(
            "proposal",
            artifact={"candidate": "dedupe"},
            evidence={"score": 0.9},
        )


def _scheduler(tmp_path, clock: Clock) -> tuple[DerivedWorkStore, AcceleratorMaxCoordinator]:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "derived.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        apply_node_governance_schema(connection)
        connection.commit()
    store = DerivedWorkStore(database_path, clock=clock)
    return store, AcceleratorMaxCoordinator(derived_work=store, retry_delay_seconds=10, clock=clock)


def test_accelerator_audit_requires_explicit_governance_schema_migration(tmp_path):
    database_path = tmp_path / "derived.sqlite"
    store = DerivedWorkStore(database_path, clock=Clock())

    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "derived_work_accelerator_audit_events" not in tables
    with pytest.raises(DerivedWorkError, match="derived_work_accelerator_audit_schema_missing"):
        store.record_accelerator_audit_event(
            event="admission",
            task_kind="semantic-dedupe",
            decision="denied",
            reason="accelerator_disabled",
        )


def test_only_allowlisted_noncanonical_task_artifacts_are_accepted(tmp_path):
    clock = Clock()
    _store, coordinator = _scheduler(tmp_path, clock)

    with pytest.raises(AcceleratorMaxError, match="accelerator_task_kind_forbidden"):
        coordinator.enqueue(_request(task_kind="canonical-memory-write"), budget=_budget())
    with pytest.raises(AcceleratorMaxError, match="accelerator_canonical_write_forbidden"):
        AcceleratorExecutionResult("proposal", artifact={"canonical_memory": {"id": "x"}})


def test_enqueue_budget_is_atomic_idempotent_and_daily_bounded(tmp_path):
    clock = Clock()
    store, coordinator = _scheduler(tmp_path, clock)
    budget = _budget(max_queue_depth=2, max_daily_tasks=1)

    first = coordinator.enqueue(_request(), budget=budget)
    assert first.created
    reused = coordinator.enqueue(_request(), budget=budget)
    assert not reused.created
    assert store.daily_admissions(job_kind="accelerator-max") == 1
    with pytest.raises(AcceleratorMaxError, match="accelerator_daily_budget_exhausted"):
        coordinator.enqueue(_request(key="two"), budget=budget)


def test_queue_depth_is_enforced_in_the_same_durable_enqueue_transaction(tmp_path):
    clock = Clock()
    _store, coordinator = _scheduler(tmp_path, clock)
    budget = _budget(max_queue_depth=1, max_daily_tasks=5)

    coordinator.enqueue(_request(), budget=budget)
    with pytest.raises(AcceleratorMaxError, match="accelerator_queue_budget_exhausted"):
        coordinator.enqueue(_request(key="two"), budget=budget)


def test_budget_denials_are_durable_daily_deduplicated_and_non_secret(tmp_path):
    clock = Clock()
    _store, coordinator = _scheduler(tmp_path, clock)

    with pytest.raises(AcceleratorMaxError, match="accelerator_disabled"):
        coordinator.enqueue(_request(), budget=_budget(enabled=False))

    queue_budget = _budget(max_queue_depth=1, max_daily_tasks=4)
    coordinator.enqueue(_request(key="queued"), budget=queue_budget)
    with pytest.raises(AcceleratorMaxError, match="accelerator_queue_budget_exhausted"):
        coordinator.enqueue(_request(key="queue-denied"), budget=queue_budget)

    first = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=queue_budget,
        capacity=_capacity(clock, free_memory_mib=1),
    )
    second = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=queue_budget,
        capacity=_capacity(clock, free_memory_mib=1),
    )
    assert first.reason == second.reason == "accelerator_memory_budget_exhausted"

    with sqlite3.connect(tmp_path / "derived.sqlite") as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(derived_work_accelerator_audit_events)"
            )
        }
        rows = {
            tuple(row)
            for row in connection.execute(
                """
                SELECT event_kind, task_kind, decision, reason_code
                FROM derived_work_accelerator_audit_events
                """
            )
        }

    assert rows == {
        ("admission", "semantic-dedupe", "denied", "accelerator_disabled"),
        (
            "admission",
            "semantic-dedupe",
            "denied",
            "accelerator_queue_budget_exhausted",
        ),
        (
            "scheduler",
            "scheduler",
            "deferred",
            "accelerator_memory_budget_exhausted",
        ),
    }
    assert (
        not {"project_id", "subject_id", "payload_json", "result_json", "provider_identity"}
        & columns
    )


def test_foreground_embedding_or_rerank_work_preempts_background_and_projects_remain_isolated(
    tmp_path,
):
    clock = Clock()
    store, coordinator = _scheduler(tmp_path, clock)
    coordinator.enqueue(_request(project_id="project:alpha"), budget=_budget())
    coordinator.enqueue(_request(project_id="project:beta", key="beta"), budget=_budget())
    store.enqueue(
        project_id="project:beta",
        visibility="project",
        config_revision="cfg-20260806T000000Z-000000000000",
        job_kind="node-inference",
        provider_identity="embedding-identity",
        subject_id="outbox:beta",
        subject_hash=_hash("foreground"),
        dedupe_key="foreground",
        payload={"operation": "rerank"},
        priority=100,
    )

    deferred = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=_budget(),
        capacity=_capacity(clock),
    )
    assert deferred.outcome == "deferred"
    assert deferred.reason == "accelerator_foreground_work_pending"
    foreground = store.claim_next(project_id="project:beta", job_kind="node-inference")
    assert foreground is not None
    store.complete(
        job_id=foreground.job.job_id,
        project_id="project:beta",
        lease_token=foreground.lease_token,
        fencing_generation=foreground.job.fencing_generation,
        result={"outcome": "rerank"},
    )

    completed = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=_budget(),
        capacity=_capacity(clock),
    )
    assert completed.outcome == "completed"
    alpha = store.get(job_id=completed.job_id or "", project_id="project:alpha")
    assert alpha.result is not None
    assert alpha.result["artifact_kind"] == "proposal"
    beta_stats = store.stats(project_id="project:beta", job_kind="accelerator-max")
    assert beta_stats["pending"] == 1


def test_concurrency_capacity_failure_retry_and_reconcile_are_durable(tmp_path):
    clock = Clock()
    store, coordinator = _scheduler(tmp_path, clock)
    budget = _budget(max_concurrency=1)
    coordinator.enqueue(_request(), budget=budget)
    coordinator.enqueue(_request(project_id="project:beta", key="beta"), budget=budget)

    first_lease = store.claim_next_accelerator(
        project_id="project:alpha",
        job_kind="accelerator-max",
        max_concurrency=1,
        foreground_priority_floor=200,
        lease_seconds=60,
    )
    assert first_lease.claimed
    blocked = coordinator.run_next(
        project_id="project:beta",
        executor=RecordingExecutor(),
        budget=budget,
        capacity=_capacity(clock),
    )
    assert blocked.reason == "accelerator_concurrency_budget_exhausted"

    clock.advance(seconds=61)
    assert coordinator.reconcile()["derived_work_recovered"] == 1
    retried = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(fail_once=True),
        budget=budget,
        capacity=_capacity(clock),
    )
    assert retried.outcome == "retry_wait"
    retry_job = store.get(job_id=retried.job_id or "", project_id="project:alpha")
    assert retry_job.failure_code == "accelerator_provider_unavailable"
    clock.advance(seconds=10)
    completed = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=budget,
        capacity=_capacity(clock),
    )
    assert completed.outcome == "completed"


def test_memory_capacity_and_stale_observation_fail_closed_without_claiming(tmp_path):
    clock = Clock()
    store, coordinator = _scheduler(tmp_path, clock)
    created = coordinator.enqueue(_request(), budget=_budget())
    stale = AcceleratorCapacityEvidence(4096, clock() - timedelta(seconds=121))
    deferred = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=_budget(),
        capacity=stale,
    )
    assert deferred.reason == "accelerator_capacity_evidence_stale"
    low = coordinator.run_next(
        project_id="project:alpha",
        executor=RecordingExecutor(),
        budget=_budget(),
        capacity=_capacity(clock, free_memory_mib=1),
    )
    assert low.reason == "accelerator_memory_budget_exhausted"
    assert store.get(job_id=created.job.job_id, project_id="project:alpha").status == "pending"


def test_control_plane_safe_config_budget_requires_exact_types():
    config = {
        "accelerator_max_enabled": True,
        "accelerator_max_concurrency": 1,
        "accelerator_max_queue_depth": 2,
        "accelerator_max_daily_tasks": 3,
        "accelerator_min_free_memory_mib": 512,
    }
    assert budget_from_node_routing_config(config) == _budget(
        max_queue_depth=2,
        max_daily_tasks=3,
    )
    config["accelerator_max_enabled"] = "true"
    with pytest.raises(AcceleratorMaxError, match="accelerator_config_invalid"):
        budget_from_node_routing_config(config)
