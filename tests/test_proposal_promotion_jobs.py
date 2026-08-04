from __future__ import annotations

import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.derived_work import DerivedWorkStore
from plastic_promise.core.memory_proposals import ProposalCandidate
from plastic_promise.core.proposal_promotion import ProposalAutomation
from plastic_promise.core.proposal_promotion_jobs import (
    PROMOTION_JOB_KIND,
    DurableProposalPromotionWorker,
    close_proposal_promotion_runtime,
    process_proposal_promotion_jobs,
    proposal_promotion_identity,
    reconcile_proposal_promotion_jobs,
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", str(tmp_path / "promotion-jobs.db"))
    monkeypatch.setenv("PLASTIC_LANCEDB_PATH", str(tmp_path / "lancedb"))
    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", "shadow")
    monkeypatch.setenv("PP_PROPOSAL_PROMOTION_WORKER_AUTOSTART", "0")
    instance = ContextEngine(use_sqlite=True)
    try:
        yield instance
    finally:
        close_proposal_promotion_runtime(instance, timeout=0)
        instance._sqlite._conn.close()


def _candidate(*, session, turn, content="Prefer concise technical explanations."):
    return ProposalCandidate(
        content=content,
        category="preference",
        project_id="project:alpha",
        visibility="project",
        origin_role="user",
        origin_turn_hash=f"sha256:{turn}",
        origin_call_id=f"call:{turn}",
        origin_visibility="project",
        metadata={
            "stage_session_id": session,
            "request_id": turn,
            "classification_confidence": 0.95,
            "principle_relevance_score": 0.7,
        },
    )


def _eligible_proposal(engine, *, content="Prefer concise technical explanations."):
    automation = ProposalAutomation(engine._sqlite._conn)
    first = automation.observe_candidate(
        _candidate(session="session:a", turn="turn:1", content=content)
    )
    automation.record_context_exposure(
        first.proposal["proposal_id"],
        session_id="session:b",
        turn_id="turn:2",
        query_hash="sha256:query-two",
        retrieval_score=0.9,
    )
    second = automation.observe_candidate(
        _candidate(session="session:b", turn="turn:2", content=content)
    )
    assert second.score.eligible is True
    engine._sqlite._conn.commit()
    return second.proposal, second.score


def test_reconcile_creates_one_durable_job_for_each_eligible_score_revision(engine):
    proposal, score = _eligible_proposal(engine)
    ineligible = ProposalAutomation(engine._sqlite._conn).observe_candidate(
        _candidate(
            session="session:only",
            turn="turn:only",
            content="Prefer examples in Python.",
        )
    )
    engine._sqlite._conn.commit()

    first = reconcile_proposal_promotion_jobs(engine)
    second = reconcile_proposal_promotion_jobs(engine)

    assert {key: first[key] for key in ("eligible", "created", "reused", "skipped")} == {
        "eligible": 1,
        "created": 1,
        "reused": 0,
        "skipped": 0,
    }
    assert {key: second[key] for key in ("eligible", "created", "reused", "skipped")} == {
        "eligible": 1,
        "created": 0,
        "reused": 1,
        "skipped": 0,
    }
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    job = store.get(job_id=first["job_ids"][0], project_id="project:alpha")
    assert job.job_kind == PROMOTION_JOB_KIND
    assert job.subject_id == proposal["proposal_id"]
    assert job.payload["score_revision"] == score.score_revision
    assert ineligible.score.eligible is False


class FakeEmbedder:
    model_name = "cloud-embedding"
    index_model_name = "cloud-embedding:test-revision"

    def embed_batch(self, texts):
        return [[1.0, 0.0] for _text in texts]


def test_promotion_worker_persists_vector_and_shadow_evaluation_outcome(engine):
    proposal, _score = _eligible_proposal(engine)
    engine._embedder = FakeEmbedder()
    engine._ldb = None
    engine._principle_anchors = {"principle:test": [1.0, 0.0]}
    report = reconcile_proposal_promotion_jobs(engine)
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    worker = DurableProposalPromotionWorker(
        engine,
        store,
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once(raise_errors=True) is True

    job = store.get(job_id=report["job_ids"][0], project_id="project:alpha")
    assert job.status == "completed"
    assert job.result["promotion_status"] == "would_promote"
    assert job.result["promotion_reason"] is None
    assert job.result["vector_status"] == "recorded"
    assert job.result["evaluated_score_revision"] >= job.payload["score_revision"]
    stored = engine._sqlite._conn.execute(
        "SELECT status FROM memory_proposals WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    ).fetchone()
    assert stored == ("pending",)
    assert engine._sqlite._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    replay = reconcile_proposal_promotion_jobs(engine)
    assert replay["created"] == 0
    assert replay["reused"] == 1
    assert replay["job_ids"] == [job.job_id]


def test_reconcile_prioritizes_proposals_without_current_jobs(engine):
    for index in range(101):
        _eligible_proposal(engine, content=f"Prefer concise explanation variant {index}.")

    first = reconcile_proposal_promotion_jobs(engine, limit=100)
    second = reconcile_proposal_promotion_jobs(engine, limit=100)

    assert first["created"] == 100
    assert second["created"] == 1
    assert second["reused"] == 99
    assert (
        engine._sqlite._conn.execute(
            "SELECT COUNT(DISTINCT subject_id) FROM derived_work_jobs WHERE job_kind = ?",
            (PROMOTION_JOB_KIND,),
        ).fetchone()[0]
        == 101
    )


def test_promotion_worker_recovers_success_committed_before_job_completion(engine):
    proposal, _score = _eligible_proposal(engine)
    report = reconcile_proposal_promotion_jobs(engine)
    memory_id = "memory:promotion-crash-recovery"
    engine._sqlite._conn.execute(
        """
        UPDATE memory_proposals
           SET status = 'adopted', approval_actor = 'system:auto-proposal-promoter',
               approval_call_id = 'auto-proposal:crash-window', promoted_memory_id = ?
         WHERE proposal_id = ?
        """,
        (memory_id, proposal["proposal_id"]),
    )
    engine._sqlite._conn.commit()
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    worker = DurableProposalPromotionWorker(
        engine,
        store,
        batch_size=20,
        max_wait_seconds=0,
        autostart=False,
    )

    assert worker.run_once(raise_errors=True) is True

    job = store.get(job_id=report["job_ids"][0], project_id="project:alpha")
    assert job.status == "completed"
    assert job.result["promotion_status"] == "promoted"
    assert job.result["promotion_reason"] is None
    assert job.result["memory_id"] == memory_id
    assert job.result["recovered_after_commit"] is True


def test_promotion_identity_changes_when_shadow_is_promoted_to_on(engine, monkeypatch):
    shadow_identity = proposal_promotion_identity(engine)

    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", "on")

    assert proposal_promotion_identity(engine) != shadow_identity


def test_promotion_worker_retries_vector_failure_then_records_dead_reason(engine, monkeypatch):
    class FailingEmbedder(FakeEmbedder):
        def embed_batch(self, texts):
            raise RuntimeError("provider unavailable")

    monkeypatch.setenv("PP_PROPOSAL_PROMOTION_MAX_ATTEMPTS", "2")
    _eligible_proposal(engine)
    engine._embedder = FailingEmbedder()
    engine._ldb = None
    report = reconcile_proposal_promotion_jobs(engine)
    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    store = DerivedWorkStore(db_path)
    worker = DurableProposalPromotionWorker(
        engine,
        store,
        batch_size=20,
        max_wait_seconds=0,
        retry_delay_seconds=0,
        autostart=False,
    )

    assert worker.run_once() is True
    retry = store.get(job_id=report["job_ids"][0], project_id="project:alpha")
    assert retry.status == "retry_wait"
    assert retry.failure_code == "proposal_promotion_evaluation_failed"
    assert retry.attempt_count == 1

    assert worker.run_once() is True
    dead = store.get(job_id=report["job_ids"][0], project_id="project:alpha")
    assert dead.status == "dead"
    assert dead.failure_code == "proposal_promotion_evaluation_failed"
    assert dead.attempt_count == 2


def test_process_promotion_jobs_uses_registered_runtime(engine, monkeypatch):
    monkeypatch.setenv("PP_PROPOSAL_PROMOTION_MAX_WAIT_SECONDS", "0")
    _eligible_proposal(engine)
    engine._embedder = FakeEmbedder()
    engine._ldb = None
    engine._principle_anchors = {"principle:test": [1.0, 0.0]}
    report = reconcile_proposal_promotion_jobs(engine)

    processed = process_proposal_promotion_jobs(engine, max_batches=2)

    db_path = engine._sqlite._conn.execute("PRAGMA database_list").fetchone()[2]
    job = DerivedWorkStore(db_path).get(job_id=report["job_ids"][0], project_id="project:alpha")
    assert processed == {"processed_batches": 1}
    assert job.status == "completed"


def test_process_promotion_jobs_reports_runtime_initialization_failure(engine, monkeypatch):
    from plastic_promise.core import proposal_promotion_jobs

    close_proposal_promotion_runtime(engine, timeout=0)

    class BrokenWorker:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("embedding runtime unavailable")

    monkeypatch.setattr(proposal_promotion_jobs, "DurableProposalPromotionWorker", BrokenWorker)

    assert process_proposal_promotion_jobs(engine) == {
        "skipped": "proposal_promotion_runtime_unavailable",
        "failure_code": "proposal_promotion_runtime_init_failed",
        "processed_batches": 0,
    }
