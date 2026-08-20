from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from plastic_promise.core.memory_proposals import ProposalCandidate
from plastic_promise.core.proposal_promotion import (
    ProposalAutomation,
    VectorEvidenceRequest,
    collect_vector_evidence,
    collect_vector_evidence_batch,
    ensure_proposal_automation_schema,
    evaluate_auto_promotion,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def _candidate(
    *,
    session: str,
    turn: str,
    content: str = "The user prefers concise technical explanations.",
) -> ProposalCandidate:
    return ProposalCandidate(
        content=content,
        category="preference",
        project_id="project:repo:github.com/example/project",
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


def test_signal_schema_and_observation_are_idempotent():
    conn = sqlite3.connect(":memory:")
    ensure_proposal_automation_schema(conn)
    ensure_proposal_automation_schema(conn)
    automation = ProposalAutomation(conn)

    first = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))
    replay = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))

    assert replay.proposal["proposal_id"] == first.proposal["proposal_id"]
    assert replay.score.observation_count == 1
    assert replay.score.distinct_turn_count == 1
    assert replay.score.distinct_session_count == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_proposal_signals").fetchone()[0] == 3


def test_repeated_cross_session_observation_and_use_become_eligible():
    conn = sqlite3.connect(":memory:")
    automation = ProposalAutomation(conn)
    observed = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))
    automation.record_context_exposure(
        observed.proposal["proposal_id"],
        session_id="session:b",
        turn_id="turn:2",
        query_hash="sha256:query-two",
        retrieval_score=0.9,
    )
    reinforced = automation.observe_candidate(_candidate(session="session:b", turn="turn:2"))

    assert reinforced.proposal["proposal_id"] == observed.proposal["proposal_id"]
    assert reinforced.score.observation_count == 2
    assert reinforced.score.distinct_turn_count == 2
    assert reinforced.score.distinct_session_count == 2
    assert reinforced.score.exposure_count == 1
    assert reinforced.score.composite_score >= 0.82
    assert reinforced.score.eligible is True
    assert reinforced.score.blocked_reason == ""
    assert conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 1


def test_principle_and_rerank_scores_cannot_bypass_confirmation_gates():
    conn = sqlite3.connect(":memory:")
    automation = ProposalAutomation(conn)
    observed = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))
    automation.record_signal(
        observed.proposal["proposal_id"],
        signal_type="rerank_score",
        evidence_key="rerank:max",
        value=1.0,
    )
    automation.record_signal(
        observed.proposal["proposal_id"],
        signal_type="principle_relevance",
        evidence_key="principle:max",
        value=1.0,
    )

    score = automation.refresh_score(observed.proposal["proposal_id"])

    assert score.eligible is False
    assert score.blocked_reason == "insufficient_user_observations"


def test_conflict_blocks_otherwise_eligible_proposal():
    conn = sqlite3.connect(":memory:")
    automation = ProposalAutomation(conn)
    observed = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))
    automation.record_context_exposure(
        observed.proposal["proposal_id"],
        session_id="session:b",
        turn_id="turn:2",
        query_hash="sha256:query-two",
        retrieval_score=0.9,
    )
    automation.observe_candidate(_candidate(session="session:b", turn="turn:2"))
    automation.record_signal(
        observed.proposal["proposal_id"],
        signal_type="conflict",
        evidence_key="conflict:turn-three",
        value=1.0,
    )

    score = automation.refresh_score(observed.proposal["proposal_id"])

    assert score.eligible is False
    assert score.blocked_reason == "unresolved_conflict"


def test_rank_pending_is_project_scoped_and_returns_no_canonical_rows():
    conn = sqlite3.connect(":memory:")
    automation = ProposalAutomation(conn)
    observed = automation.observe_candidate(_candidate(session="session:a", turn="turn:1"))

    matches = automation.rank_pending(
        project_id="project:repo:github.com/example/project",
        query="Please keep the technical explanation concise.",
    )
    foreign = automation.rank_pending(
        project_id="project:repo:github.com/example/other",
        query="Please keep the technical explanation concise.",
    )

    assert [item["proposal_id"] for item in matches] == [observed.proposal["proposal_id"]]
    assert matches[0]["temporary"] is True
    assert foreign == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
        ).fetchone()[0]
        == 0
    )


class _FakeEmbedder:
    model_name = "cloud-embedding"
    index_model_name = "cloud-embedding:revision-a:2"

    def __init__(self, *, zero_query: bool = False):
        self.zero_query = zero_query
        self.batch_calls: list[list[str]] = []

    def embed(self, text: str) -> list[float]:
        if self.zero_query and text == "current query":
            return [0.0, 0.0]
        return [1.0, 0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [self.embed(text) for text in texts]


class _FakeLanceDB:
    def search_similar(self, _vector: list[float], *, k: int):
        assert k == 12
        return [("same-project", 0.91), ("foreign-project", 0.99)]


def _vector_engine(conn, *, embedder=None, lancedb=None):
    return SimpleNamespace(
        _sqlite=SimpleNamespace(_conn=conn),
        _embedder=embedder or _FakeEmbedder(),
        _ldb=lancedb,
        _principle_anchors={"principle-1": [0.8, 0.2]},
    )


def _observed_proposal(conn) -> str:
    return (
        ProposalAutomation(conn)
        .observe_candidate(_candidate(session="session:a", turn="turn:1"))
        .proposal["proposal_id"]
    )


def _make_eligible(conn, proposal_id: str) -> None:
    automation = ProposalAutomation(conn)
    automation.record_context_exposure(
        proposal_id,
        session_id="session:b",
        turn_id="turn:2",
        query_hash="sha256:query-two",
        retrieval_score=0.9,
    )
    automation.observe_candidate(_candidate(session="session:b", turn="turn:2"))


def test_vector_evidence_filters_foreign_memories_and_never_persists_pending_vector():
    conn = sqlite3.connect(":memory:")
    proposal_id = _observed_proposal(conn)
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO memories (id, project_id) VALUES (?, ?)",
        [
            ("same-project", "project:repo:github.com/example/project"),
            ("foreign-project", "project:repo:github.com/example/other"),
        ],
    )
    result = collect_vector_evidence(
        _vector_engine(conn, lancedb=_FakeLanceDB()),
        proposal_id,
        query="current query",
        session_id="session:a",
        turn_id="turn:1",
    )

    assert result["status"] == "recorded"
    assert result["signals"]["memory_similarity"] == 0.91
    assert result["signals"]["principle_similarity"] > 0.9
    assert result["signals"]["query_similarity"] == 1.0
    assert result["pending_vector_persisted"] is False
    metadata = conn.execute(
        "SELECT metadata_json FROM memory_proposal_signals "
        "WHERE proposal_id = ? AND signal_type = 'memory_similarity'",
        (proposal_id,),
    ).fetchone()[0]
    assert "foreign-project" not in metadata
    assert "vector" not in metadata.casefold()


def test_vector_evidence_batch_uses_one_embedding_batch_for_proposals_and_queries():
    conn = sqlite3.connect(":memory:")
    automation = ProposalAutomation(conn)
    first_id = automation.observe_candidate(
        _candidate(session="session:a", turn="turn:1", content="Prefer concise answers")
    ).proposal["proposal_id"]
    second_id = automation.observe_candidate(
        _candidate(session="session:b", turn="turn:2", content="Prefer Python examples")
    ).proposal["proposal_id"]
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO memories (id, project_id) VALUES (?, ?)",
        ("same-project", "project:repo:github.com/example/project"),
    )
    embedder = _FakeEmbedder()

    results = collect_vector_evidence_batch(
        _vector_engine(conn, embedder=embedder, lancedb=_FakeLanceDB()),
        [
            VectorEvidenceRequest(first_id, query="concise response", call_id="call:one"),
            VectorEvidenceRequest(second_id, query="python sample", call_id="call:two"),
        ],
    )

    assert [result["status"] for result in results] == ["recorded", "recorded"]
    assert len(embedder.batch_calls) == 1
    assert embedder.batch_calls[0] == [
        "Prefer concise answers",
        "concise response",
        "Prefer Python examples",
        "python sample",
    ]
    assert conn.in_transaction is False


def test_fallback_or_zero_query_vector_produces_no_semantic_signal():
    conn = sqlite3.connect(":memory:")
    proposal_id = _observed_proposal(conn)
    fallback = _FakeEmbedder()
    fallback.model_name = "fallback-zero"
    fallback.index_model_name = "cloud-looking-identity"

    fallback_result = collect_vector_evidence(_vector_engine(conn, embedder=fallback), proposal_id)
    zero_query_result = collect_vector_evidence(
        _vector_engine(conn, embedder=_FakeEmbedder(zero_query=True)),
        proposal_id,
        query="current query",
    )

    assert fallback_result["reason"] == "embedding_fallback_unavailable"
    assert zero_query_result["reason"] == "query_embedding_zero_vector"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM memory_proposal_signals WHERE signal_type IN "
            "('principle_similarity', 'memory_similarity', 'query_similarity')"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("off", "disabled"), ("shadow", "would_promote")],
)
def test_auto_promotion_modes_do_not_create_canonical_memory(conn, monkeypatch, mode, expected):
    proposal_id = _observed_proposal(conn)
    _make_eligible(conn, proposal_id)
    ProposalAutomation(conn).record_signal(
        proposal_id,
        signal_type="query_similarity",
        evidence_key="query:eligible",
        value=0.9,
        metadata={"embedding_identity": _FakeEmbedder.index_model_name},
    )
    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", mode)

    result = evaluate_auto_promotion(_vector_engine(conn), proposal_id)

    assert result["status"] == expected
    assert (
        conn.execute(
            "SELECT status FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert conn.in_transaction is False


def test_auto_promotion_requires_current_vector_identity(conn, monkeypatch):
    proposal_id = _observed_proposal(conn)
    _make_eligible(conn, proposal_id)
    ProposalAutomation(conn).record_signal(
        proposal_id,
        signal_type="query_similarity",
        evidence_key="query:stale",
        value=0.9,
        metadata={"embedding_identity": "old-model:revision:2"},
    )
    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", "on")

    result = evaluate_auto_promotion(_vector_engine(conn), proposal_id)

    assert result["status"] == "ineligible"
    assert result["reason"] == "vector_evidence_required"


def test_auto_promotion_on_calls_atomic_promoter_once(conn, monkeypatch):
    proposal_id = _observed_proposal(conn)
    _make_eligible(conn, proposal_id)
    ProposalAutomation(conn).record_signal(
        proposal_id,
        signal_type="query_similarity",
        evidence_key="query:eligible",
        value=0.9,
        metadata={"embedding_identity": _FakeEmbedder.index_model_name},
    )
    calls = []

    def fake_promote(_engine, observed_id, *, actor, call_id):
        assert conn.in_transaction is False
        calls.append((observed_id, actor, call_id))
        return SimpleNamespace(
            memory_id="memory:promoted",
            created=True,
            index_job_id="index:job",
        )

    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", "on")
    monkeypatch.setattr(
        "plastic_promise.core.memory_proposals.promote_memory_proposal",
        fake_promote,
    )

    result = evaluate_auto_promotion(_vector_engine(conn), proposal_id)

    assert result["status"] == "promoted"
    assert result["memory_id"] == "memory:promoted"
    assert len(calls) == 1


def test_auto_promotion_queue_mode_persists_eligible_task_without_canonical_write(
    conn, monkeypatch
):
    proposal_id = _observed_proposal(conn)
    _make_eligible(conn, proposal_id)
    ProposalAutomation(conn).record_signal(
        proposal_id,
        signal_type="query_similarity",
        evidence_key="query:queued",
        value=0.9,
        metadata={"embedding_identity": _FakeEmbedder.index_model_name},
    )
    monkeypatch.setenv("PP_MEMORY_PROPOSAL_AUTO_ADOPT", "on")
    monkeypatch.setenv("PP_MEMORY_PROPOSAL_PROMOTION_QUEUE", "on")

    result = evaluate_auto_promotion(_vector_engine(conn), proposal_id)

    assert result["status"] == "queued"
    assert result["risk_tier"] == "medium"
    task = tuple(
        conn.execute(
            "SELECT proposal_id, project_id, status, risk_tier FROM "
            "memory_proposal_promotion_tasks WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()
    )
    assert task == (
        proposal_id,
        "project:repo:github.com/example/project",
        "queued",
        "medium",
    )
    assert (
        conn.execute(
            "SELECT status FROM memory_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()[0]
        == "pending"
    )
