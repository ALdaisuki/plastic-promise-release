"""Durable project-scoped lease state for asynchronous derived memory work."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.core.derived_work import (
    DerivedWorkConflictError,
    DerivedWorkStore,
)


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 26, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _enqueue(store: DerivedWorkStore, *, project_id: str = "project:alpha", **overrides):
    values = {
        "project_id": project_id,
        "visibility": "project",
        "config_revision": "cloud-v1",
        "job_kind": "structured_fusion",
        "provider_identity": "openai-compatible:cloud-v1",
        "subject_id": "memory:one",
        "subject_hash": "sha256:" + "a" * 64,
        "dedupe_key": "fusion:memory:one:cloud-v1",
        "payload": {"memory_ids": ["memory:one"]},
    }
    values.update(overrides)
    return store.enqueue(**values)


def test_enqueue_is_idempotent_within_project_but_not_across_projects(tmp_path):
    store = DerivedWorkStore(tmp_path / "derived-work.db")

    first = _enqueue(store)
    reused = _enqueue(store)
    other_project = _enqueue(store, project_id="project:beta")

    assert first.created is True
    assert reused.created is False
    assert reused.job.job_id == first.job.job_id
    assert other_project.created is True
    assert other_project.job.job_id != first.job.job_id


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_complete(tmp_path):
    clock = ManualClock()
    store = DerivedWorkStore(tmp_path / "derived-work.db", clock=clock)
    job = _enqueue(store).job

    first = store.claim(job_id=job.job_id, project_id=job.project_id, lease_seconds=5)
    assert first.job.fencing_generation == 1

    clock.advance(6)
    assert store.recover_expired(project_id=job.project_id) == 1
    second = store.claim(job_id=job.job_id, project_id=job.project_id, lease_seconds=5)
    assert second.job.fencing_generation == 2

    with pytest.raises(DerivedWorkConflictError, match="derived_work_fencing_generation_invalid"):
        store.complete(
            job_id=job.job_id,
            project_id=job.project_id,
            lease_token=first.lease_token,
            fencing_generation=first.job.fencing_generation,
            result={"chunks": []},
        )

    completed = store.complete(
        job_id=job.job_id,
        project_id=job.project_id,
        lease_token=second.lease_token,
        fencing_generation=second.job.fencing_generation,
        result={"chunks": []},
    )
    assert completed.status == "completed"


def test_retryable_failure_waits_then_becomes_dead_at_attempt_limit(tmp_path):
    clock = ManualClock()
    store = DerivedWorkStore(tmp_path / "derived-work.db", clock=clock)
    job = _enqueue(store, max_attempts=2).job

    first = store.claim(job_id=job.job_id, project_id=job.project_id)
    waiting = store.fail(
        job_id=job.job_id,
        project_id=job.project_id,
        lease_token=first.lease_token,
        fencing_generation=first.job.fencing_generation,
        failure_code="provider_timeout",
        retryable=True,
        retry_delay_seconds=10,
    )
    assert waiting.status == "retry_wait"
    assert store.claim_next(project_id=job.project_id) is None

    clock.advance(10)
    second = store.claim_next(project_id=job.project_id)
    assert second is not None
    dead = store.fail(
        job_id=job.job_id,
        project_id=job.project_id,
        lease_token=second.lease_token,
        fencing_generation=second.job.fencing_generation,
        failure_code="provider_timeout",
        retryable=True,
    )
    assert dead.status == "dead"
    assert dead.attempt_count == 2
    assert store.stats(project_id=job.project_id)["dead"] == 1


def test_claim_next_cannot_observe_another_project_job(tmp_path):
    store = DerivedWorkStore(tmp_path / "derived-work.db")
    _enqueue(store, project_id="project:alpha")
    beta = _enqueue(store, project_id="project:beta").job

    claimed = store.claim_next(project_id="project:beta")

    assert claimed is not None
    assert claimed.job.job_id == beta.job_id
    assert store.stats(project_id="project:alpha")["pending"] == 1


def test_enqueue_in_transaction_rolls_back_with_the_canonical_mutation(tmp_path):
    db_path = tmp_path / "derived-work.db"
    store = DerivedWorkStore(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = store.enqueue_in_transaction(
            connection,
            project_id="project:alpha",
            visibility="project",
            config_revision="cloud-v1",
            job_kind="embedding",
            provider_identity="openai-compatible:embed-v1",
            subject_id="memory:rolled-back",
            subject_hash="sha256:" + "b" * 64,
            dedupe_key="embedding:rolled-back:cloud-v1",
            payload={"memory_ids": ["memory:rolled-back"]},
        )
        assert result.created is True
        connection.rollback()
    finally:
        connection.close()

    assert store.stats(project_id="project:alpha")["pending"] == 0


def test_claim_batch_waits_for_size_then_keeps_the_exact_partition(tmp_path):
    clock = ManualClock()
    store = DerivedWorkStore(tmp_path / "derived-work.db", clock=clock)
    alpha = [
        _enqueue(store, subject_id=f"memory:a{index}", dedupe_key=f"alpha:{index}").job
        for index in range(2)
    ]
    _enqueue(
        store,
        project_id="project:beta",
        subject_id="memory:beta",
        dedupe_key="beta:one",
    )
    _enqueue(
        store,
        subject_id="memory:other-provider",
        dedupe_key="alpha:other-provider",
        provider_identity="openai-compatible:other",
    )

    assert store.claim_batch(limit=20, min_batch_size=3, max_wait_seconds=10) == ()

    clock.advance(10)
    leases = store.claim_batch(limit=20, min_batch_size=3, max_wait_seconds=10)

    assert {lease.job.job_id for lease in leases} == {job.job_id for job in alpha}
    assert {
        (
            lease.job.project_id,
            lease.job.visibility,
            lease.job.config_revision,
            lease.job.job_kind,
            lease.job.provider_identity,
        )
        for lease in leases
    } == {
        (
            "project:alpha",
            "project",
            "cloud-v1",
            "structured_fusion",
            "openai-compatible:cloud-v1",
        )
    }


def test_concurrent_batch_claimers_never_receive_the_same_job(tmp_path):
    db_path = tmp_path / "derived-work.db"
    first_store = DerivedWorkStore(db_path)
    second_store = DerivedWorkStore(db_path)
    for index in range(4):
        _enqueue(
            first_store,
            subject_id=f"memory:{index}",
            dedupe_key=f"fusion:{index}",
        )

    def claim(store):
        return store.claim_batch(limit=2, min_batch_size=2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(claim, (first_store, second_store)))

    first_ids = {lease.job.job_id for lease in first}
    second_ids = {lease.job.job_id for lease in second}
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)


def test_complete_in_transaction_rolls_back_local_receipt(tmp_path):
    db_path = tmp_path / "derived-work.db"
    store = DerivedWorkStore(db_path)
    job = _enqueue(store).job
    lease = store.claim(job_id=job.job_id, project_id=job.project_id)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        completed = store.complete_in_transaction(
            connection,
            job_id=job.job_id,
            project_id=job.project_id,
            lease_token=lease.lease_token,
            fencing_generation=lease.job.fencing_generation,
            result={"batch_id": "fusion:one"},
        )
        assert completed.status == "completed"
        connection.rollback()
    finally:
        connection.close()

    assert store.get(job_id=job.job_id, project_id=job.project_id).status == "leased"


def test_invalid_token_cannot_renew_a_lease(tmp_path):
    store = DerivedWorkStore(tmp_path / "derived-work.db")
    job = _enqueue(store).job
    lease = store.claim(job_id=job.job_id, project_id=job.project_id)

    with pytest.raises(DerivedWorkConflictError, match="derived_work_lease_token_invalid"):
        store.renew_lease(
            job_id=job.job_id,
            project_id=job.project_id,
            lease_token="not-the-capability",
            fencing_generation=lease.job.fencing_generation,
        )


def test_expired_leases_consume_retry_budget_and_surface_queue_age(tmp_path):
    clock = ManualClock()
    store = DerivedWorkStore(tmp_path / "derived-work.db", clock=clock)
    job = _enqueue(store, max_attempts=1).job
    store.claim(job_id=job.job_id, project_id=job.project_id, lease_seconds=5)

    clock.advance(6)
    assert store.recover_expired(project_id=job.project_id) == 1

    status = store.status(project_id=job.project_id)
    assert status["dead"] == 1
    assert status["queue_depth"] == 0
    assert status["oldest_queued_at"] is None
    assert status["oldest_queued_age_seconds"] is None


def test_project_queue_limit_is_atomic_but_idempotent_replay_still_succeeds(tmp_path):
    store = DerivedWorkStore(tmp_path / "derived-work.db")
    first = _enqueue(store, max_active_jobs=1)

    assert _enqueue(store, max_active_jobs=1).job.job_id == first.job.job_id
    with pytest.raises(DerivedWorkConflictError, match="derived_work_queue_full"):
        _enqueue(
            store,
            subject_id="memory:two",
            dedupe_key="fusion:memory:two",
            max_active_jobs=1,
        )
