from __future__ import annotations

import hashlib
import sqlite3

import pytest

from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
    ensure_memory_proposal_schema,
)
from plastic_promise.core.proposal_promotion_tasks import PromotionTaskStore


def _proposal(conn: sqlite3.Connection, *, project_id: str = "project:alpha") -> dict[str, object]:
    ensure_memory_proposal_schema(conn)
    return MemoryProposalStore(conn).create_many(
        [
            ProposalCandidate(
                content="The user prefers bounded retries.",
                category="preference",
                project_id=project_id,
                visibility="project",
                origin_role="user",
                origin_turn_hash=f"sha256:{project_id}:turn",
                origin_call_id=f"call:{project_id}",
                origin_visibility="project",
            )
        ],
        now="2026-08-05T00:00:00Z",
    )[0]


def test_enqueue_is_project_scoped_and_idempotent():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    first = tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        risk_tier="medium",
        idempotency_key="score:1",
    )
    replay = tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        risk_tier="medium",
        idempotency_key="score:1",
    )
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM memory_proposal_promotion_tasks").fetchone()[0] == 1

    with pytest.raises(ValueError, match="promotion_task_project_mismatch"):
        tasks.enqueue(
            proposal_id=proposal["proposal_id"],
            project_id="project:other",
            idempotency_key="score:other",
        )
    with pytest.raises(ValueError, match="promotion_task_project_scope_required"):
        tasks.enqueue(
            proposal_id=proposal["proposal_id"],
            project_id="project:unknown",
        )


def test_claim_complete_and_fencing_rejects_stale_lease():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        idempotency_key="score:1",
        now="2026-08-05T00:00:00Z",
    )
    conn.commit()
    (first,) = tasks.claim(
        project_id="project:alpha",
        now="2026-08-05T00:00:00Z",
        lease_seconds=1,
    )
    (second,) = tasks.claim(
        project_id="project:alpha",
        now="2026-08-05T00:00:02Z",
        lease_seconds=30,
    )
    assert second.task.fencing_generation > first.task.fencing_generation
    with pytest.raises(ValueError, match="promotion_task_lease_conflict"):
        tasks.complete(first, memory_id="memory:stale")
    completed = tasks.complete(second, memory_id="memory:canonical")
    assert completed.status == "completed"
    assert completed.memory_id == "memory:canonical"
    assert completed.attempt_count == 2
    assert first.lease_token not in repr(first)
    assert second.lease_token not in repr(second)


def test_claim_persists_only_a_lease_digest():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        idempotency_key="score:digest",
        now="2026-08-05T00:00:00Z",
    )
    conn.commit()
    (lease,) = tasks.claim(project_id="project:alpha", now="2026-08-05T00:00:00Z")
    stored = conn.execute(
        "SELECT lease_token_hash FROM memory_proposal_promotion_tasks WHERE task_id = ?",
        (lease.task.task_id,),
    ).fetchone()[0]
    assert stored == hashlib.sha256(lease.lease_token.encode("utf-8")).hexdigest()
    assert stored != lease.lease_token
    assert lease.lease_token not in repr(lease)


def test_lease_token_is_hashed_at_rest():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        idempotency_key="score:hash",
        now="2026-08-05T00:00:00Z",
    )
    conn.commit()

    (lease,) = tasks.claim(project_id="project:alpha", now="2026-08-05T00:00:00Z")
    stored = conn.execute(
        "SELECT lease_token_hash FROM memory_proposal_promotion_tasks"
    ).fetchone()[0]

    assert stored
    assert stored != lease.lease_token
    assert len(stored) == 64
    assert lease.lease_token not in repr(lease)


def test_fail_retries_then_reconcile_exhausts_attempts():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        max_attempts=2,
        idempotency_key="score:retry",
        now="2026-08-05T00:00:00Z",
    )
    conn.commit()
    (lease,) = tasks.claim(project_id="project:alpha", now="2026-08-05T00:00:00Z")
    retried = tasks.fail(
        lease,
        failure_code="provider_timeout",
        failure_detail="cloud provider timed out",
        retryable=True,
        retry_delay_seconds=0,
        now="2026-08-05T00:00:00Z",
    )
    assert retried.status == "retry_wait"
    assert retried.last_failure_code == "provider_timeout"

    (lease2,) = tasks.claim(project_id="project:alpha", now="2026-08-05T00:00:01Z")
    failed = tasks.fail(
        lease2,
        failure_code="provider_timeout",
        retryable=True,
        now="2026-08-05T00:00:01Z",
    )
    assert failed.status == "failed"
    assert failed.attempt_count == 2
    assert tasks.reconcile(now="2026-08-05T00:01:00Z") == {
        "requeued": 0,
        "failed": 0,
        "inspected": 0,
    }


def test_reconcile_expired_lease_records_failure_reason():
    conn = sqlite3.connect(":memory:")
    proposal = _proposal(conn)
    tasks = PromotionTaskStore(conn)
    tasks.enqueue(
        proposal_id=proposal["proposal_id"],
        project_id="project:alpha",
        max_attempts=1,
        idempotency_key="score:lease",
        now="2026-08-05T00:00:00Z",
    )
    conn.commit()
    tasks.claim(
        project_id="project:alpha",
        now="2026-08-05T00:00:00Z",
        lease_seconds=1,
    )
    result = tasks.reconcile(now="2026-08-05T00:00:02Z")
    assert result == {"requeued": 0, "failed": 1, "inspected": 1}
    row = tasks.get(
        conn.execute("SELECT task_id FROM memory_proposal_promotion_tasks").fetchone()[0]
    )
    assert row.status == "failed"
    assert row.last_failure_code == "lease_expired"
