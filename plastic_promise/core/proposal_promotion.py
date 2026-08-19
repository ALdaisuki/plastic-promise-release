"""Auditable signal ledger and conservative automation for memory proposals."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from plastic_promise.core.memory_proposals import (
    MemoryProposalStore,
    ProposalCandidate,
    ProposalPolicyError,
    ensure_memory_proposal_schema,
)
from plastic_promise.core.proposal_promotion_tasks import (
    PromotionTaskLease,
    PromotionTaskStore,
    risk_tier_for_proposal,
)

AUTO_PROMOTION_MODE_ENV = "PP_MEMORY_PROPOSAL_AUTO_ADOPT"
AUTO_PROMOTION_THRESHOLD_ENV = "PP_MEMORY_PROPOSAL_AUTO_THRESHOLD"
PROMOTION_QUEUE_ENV = "PP_MEMORY_PROPOSAL_PROMOTION_QUEUE"
DEFAULT_AUTO_PROMOTION_THRESHOLD = 0.82
MAX_SIGNAL_METADATA_BYTES = 2048
SIGNAL_TYPES = frozenset(
    {
        "user_observation",
        "shadow_observation",
        "quality_score",
        "principle_relevance",
        "principle_similarity",
        "memory_similarity",
        "retrieval_match",
        "query_similarity",
        "rerank_score",
        "context_exposure",
        "response_completed",
        "outcome_success",
        "conflict",
        "rejection",
        "would_promote",
    }
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")


@dataclass(frozen=True)
class ProposalScore:
    proposal_id: str
    project_id: str
    observation_count: int
    distinct_session_count: int
    distinct_turn_count: int
    exposure_count: int
    distinct_call_count: int
    quality_score: float
    principle_score: float
    memory_similarity: float
    query_similarity: float
    conflict_count: int
    composite_score: float
    eligible: bool
    blocked_reason: str
    score_revision: int


@dataclass(frozen=True)
class ObservationResult:
    proposal: dict[str, Any]
    score: ProposalScore
    created: bool


@dataclass(frozen=True)
class VectorEvidenceRequest:
    proposal_id: str
    query: str = ""
    session_id: str = ""
    turn_id: str = ""
    call_id: str = ""


def auto_promotion_mode() -> str:
    mode = os.environ.get(AUTO_PROMOTION_MODE_ENV, "off").strip().casefold()
    return mode if mode in {"off", "shadow", "on"} else "off"


def promotion_queue_enabled() -> bool:
    return os.environ.get(PROMOTION_QUEUE_ENV, "off").strip().casefold() in {
        "1",
        "true",
        "on",
        "yes",
    }


def ensure_proposal_automation_schema(conn: Any) -> None:
    ensure_memory_proposal_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_proposal_signals (
            signal_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            evidence_hash TEXT NOT NULL,
            value REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(proposal_id, signal_type, evidence_hash),
            CHECK(value >= 0.0 AND value <= 1.0),
            FOREIGN KEY(proposal_id) REFERENCES memory_proposals(proposal_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_proposal_signals_projection
        ON memory_proposal_signals(proposal_id, signal_type, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_proposal_scores (
            proposal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            observation_count INTEGER NOT NULL DEFAULT 0,
            distinct_session_count INTEGER NOT NULL DEFAULT 0,
            distinct_turn_count INTEGER NOT NULL DEFAULT 0,
            exposure_count INTEGER NOT NULL DEFAULT 0,
            distinct_call_count INTEGER NOT NULL DEFAULT 0,
            quality_score REAL NOT NULL DEFAULT 0.0,
            principle_score REAL NOT NULL DEFAULT 0.0,
            memory_similarity REAL NOT NULL DEFAULT 0.0,
            query_similarity REAL NOT NULL DEFAULT 0.0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            composite_score REAL NOT NULL DEFAULT 0.0,
            eligible INTEGER NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '',
            score_revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES memory_proposals(proposal_id)
        )
        """
    )
    score_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(memory_proposal_scores)")
    }
    if "distinct_call_count" not in score_columns:
        conn.execute(
            "ALTER TABLE memory_proposal_scores "
            "ADD COLUMN distinct_call_count INTEGER NOT NULL DEFAULT 0"
        )


class ProposalAutomation:
    """Store temporary proposal evidence and derive a rebuildable score projection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        ensure_proposal_automation_schema(conn)

    def observe_candidate(
        self,
        candidate: ProposalCandidate,
        *,
        now: datetime | str | None = None,
        observation_signal_type: str = "user_observation",
    ) -> ObservationResult:
        if observation_signal_type not in {"user_observation", "shadow_observation"}:
            raise ProposalPolicyError("proposal_observation_signal_invalid")
        content_hash = _content_hash(candidate.content)
        existing = self.conn.execute(
            """
            SELECT proposal_id FROM memory_proposals
            WHERE project_id = ? AND content_hash = ? AND category = ?
              AND visibility = ? AND origin_visibility = ? AND status = 'pending'
            ORDER BY created_at, proposal_id LIMIT 1
            """,
            (
                candidate.project_id,
                content_hash,
                candidate.category,
                candidate.visibility,
                candidate.origin_visibility,
            ),
        ).fetchone()
        created = existing is None
        if existing is None:
            proposal = MemoryProposalStore(self.conn).create_many([candidate], now=now)[0]
        else:
            proposal = MemoryProposalStore(self.conn).get(str(existing[0]))
            if proposal is None:
                raise ProposalPolicyError("proposal_not_found")

        metadata = dict(candidate.metadata or {})
        session_id = str(metadata.get("stage_session_id") or metadata.get("session_id") or "")
        turn_id = str(metadata.get("request_id") or metadata.get("turn_id") or "")
        call_id = str(candidate.origin_call_id or metadata.get("call_id") or "")
        observation_key = "\x1f".join(
            (session_id, turn_id or candidate.origin_turn_hash, content_hash)
        )
        self.record_signal(
            proposal["proposal_id"],
            signal_type=observation_signal_type,
            evidence_key=observation_key,
            value=1.0,
            session_id=session_id,
            turn_id=turn_id or candidate.origin_turn_hash,
            call_id=call_id,
            metadata={
                "origin": "security_shield"
                if observation_signal_type == "shadow_observation"
                else "user",
                "category": candidate.category,
            },
            now=now,
        )
        self.record_signal(
            proposal["proposal_id"],
            signal_type="quality_score",
            evidence_key=observation_key,
            value=_candidate_quality(candidate),
            session_id=session_id,
            turn_id=turn_id or candidate.origin_turn_hash,
            call_id=call_id,
            now=now,
        )
        principle_score = _bounded_score(metadata.get("principle_relevance_score"), 0.0)
        if principle_score > 0.0:
            self.record_signal(
                proposal["proposal_id"],
                signal_type="principle_relevance",
                evidence_key=observation_key,
                value=principle_score,
                session_id=session_id,
                turn_id=turn_id or candidate.origin_turn_hash,
                call_id=call_id,
                now=now,
            )
        rerank_score = metadata.get("rerank_score")
        if rerank_score is not None:
            self.record_signal(
                proposal["proposal_id"],
                signal_type="rerank_score",
                evidence_key=observation_key,
                value=_bounded_score(rerank_score, 0.0),
                session_id=session_id,
                turn_id=turn_id or candidate.origin_turn_hash,
                call_id=call_id,
                now=now,
            )
        return ObservationResult(
            proposal=proposal,
            score=self.refresh_score(proposal["proposal_id"]),
            created=created,
        )

    def record_signal(
        self,
        proposal_id: str,
        *,
        signal_type: str,
        evidence_key: str,
        value: float,
        session_id: str = "",
        turn_id: str = "",
        call_id: str = "",
        metadata: dict[str, Any] | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        normalized_type = str(signal_type or "").strip().casefold()
        if normalized_type not in SIGNAL_TYPES:
            raise ProposalPolicyError("proposal_signal_type_invalid")
        row = MemoryProposalStore(self.conn).get(proposal_id)
        if row is None:
            raise ProposalPolicyError("proposal_not_found")
        if row["status"] != "pending" and normalized_type not in {"rejection"}:
            return False
        score = _bounded_score(value, -1.0)
        if score < 0.0:
            raise ProposalPolicyError("proposal_signal_value_invalid")
        evidence_hash = _hash_text(str(evidence_key or ""))
        if not evidence_key:
            raise ProposalPolicyError("proposal_signal_evidence_required")
        metadata_json = _signal_metadata_json(metadata)
        created_at = _utc_text(now)
        signal_id = (
            "proposal_signal_"
            + _hash_text("\x1f".join((proposal_id, normalized_type, evidence_hash)))[:24]
        )
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_proposal_signals (
                signal_id, proposal_id, project_id, signal_type, session_id,
                turn_id, call_id, evidence_hash, value, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                proposal_id,
                row["project_id"],
                normalized_type,
                str(session_id or "")[:256],
                str(turn_id or "")[:256],
                str(call_id or "")[:256],
                evidence_hash,
                score,
                metadata_json,
                created_at,
            ),
        )
        return cursor.rowcount == 1

    def record_context_exposure(
        self,
        proposal_id: str,
        *,
        session_id: str,
        turn_id: str,
        query_hash: str,
        retrieval_score: float,
        call_id: str = "",
        exposed: bool = True,
    ) -> ProposalScore:
        evidence_key = "\x1f".join((session_id, turn_id, query_hash))
        self.record_signal(
            proposal_id,
            signal_type="retrieval_match",
            evidence_key=evidence_key,
            value=_bounded_score(retrieval_score, 0.0),
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            metadata={"query_hash": str(query_hash or "")[:96]},
        )
        if exposed:
            self.record_signal(
                proposal_id,
                signal_type="context_exposure",
                evidence_key=evidence_key,
                value=1.0,
                session_id=session_id,
                turn_id=turn_id,
                call_id=call_id,
            )
        return self.refresh_score(proposal_id)

    def refresh_score(self, proposal_id: str) -> ProposalScore:
        proposal = MemoryProposalStore(self.conn).get(proposal_id)
        if proposal is None:
            raise ProposalPolicyError("proposal_not_found")
        rows = self.conn.execute(
            """
            SELECT signal_type, session_id, turn_id, call_id, value
            FROM memory_proposal_signals WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchall()
        grouped: dict[str, list[tuple[str, str, str, float]]] = {}
        for signal_type, session_id, turn_id, call_id, value in rows:
            grouped.setdefault(str(signal_type), []).append(
                (
                    str(session_id or ""),
                    str(turn_id or ""),
                    str(call_id or ""),
                    float(value or 0.0),
                )
            )
        observations = grouped.get("user_observation", [])
        observation_count = len(observations)
        sessions = {session for session, _turn, _call, _value in observations if session}
        turns = {turn for _session, turn, _call, _value in observations if turn}
        exposures = grouped.get("context_exposure", [])
        calls = {
            call or f"turn:{session}:{turn}"
            for session, turn, call, _value in exposures
            if call or turn
        }
        outcomes = grouped.get("outcome_success", [])
        quality = _average(grouped.get("quality_score", []))
        principle = max(
            _max_value(grouped.get("principle_relevance", [])),
            _max_value(grouped.get("principle_similarity", [])),
        )
        memory_similarity = _max_value(grouped.get("memory_similarity", []))
        query_similarity = max(
            _max_value(grouped.get("query_similarity", [])),
            _max_value(grouped.get("rerank_score", [])),
            _max_value(grouped.get("retrieval_match", [])),
        )
        conflict_count = len(grouped.get("conflict", [])) + len(grouped.get("rejection", []))
        composite = min(
            1.0,
            0.32 * min(1.0, observation_count / 2.0)
            + 0.20 * min(1.0, len(sessions) / 2.0)
            + 0.20 * quality
            + 0.10 * min(1.0, len(calls) / 2.0)
            + 0.07 * principle
            + 0.03 * memory_similarity
            + 0.03 * query_similarity
            + 0.05 * min(1.0, len(outcomes) / 2.0)
            + 0.05 * (1.0 if proposal["status"] == "pending" else 0.0),
        )
        blocked_reason = _blocked_reason(
            proposal,
            observations=observation_count,
            sessions=len(sessions),
            turns=len(turns),
            quality=quality,
            conflicts=conflict_count,
            composite=composite,
        )
        score = ProposalScore(
            proposal_id=proposal_id,
            project_id=str(proposal["project_id"]),
            observation_count=observation_count,
            distinct_session_count=len(sessions),
            distinct_turn_count=len(turns),
            exposure_count=len(exposures),
            distinct_call_count=len(calls),
            quality_score=round(quality, 6),
            principle_score=round(principle, 6),
            memory_similarity=round(memory_similarity, 6),
            query_similarity=round(query_similarity, 6),
            conflict_count=conflict_count,
            composite_score=round(composite, 6),
            eligible=not blocked_reason,
            blocked_reason=blocked_reason,
            score_revision=sum(1 for signal_type, *_rest in rows if signal_type != "would_promote"),
        )
        self._persist_score(score)
        return score

    def rank_pending(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        query_terms = _terms(query)
        if not query_terms:
            return []
        bounded_limit = min(8, max(1, int(limit)))
        rows = self.conn.execute(
            """
            SELECT proposal_id, content, category, visibility, expires_at
            FROM memory_proposals
            WHERE project_id = ? AND status = 'pending' AND expires_at > ?
            ORDER BY created_at, proposal_id LIMIT 100
            """,
            (project_id, _utc_text(None)),
        ).fetchall()
        ranked: list[dict[str, Any]] = []
        for proposal_id, content, category, visibility, expires_at in rows:
            content_terms = _terms(str(content or ""))
            overlap = len(query_terms & content_terms)
            if overlap == 0:
                continue
            lexical = overlap / max(1.0, math.sqrt(len(query_terms) * len(content_terms)))
            score_row = self.conn.execute(
                "SELECT composite_score FROM memory_proposal_scores WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            maturity = float(score_row[0] or 0.0) if score_row else 0.0
            ranked.append(
                {
                    "proposal_id": str(proposal_id),
                    "content": str(content),
                    "category": str(category),
                    "visibility": str(visibility),
                    "expires_at": str(expires_at),
                    "retrieval_score": round(min(1.0, 0.8 * lexical + 0.2 * maturity), 6),
                    "temporary": True,
                    "canonical": False,
                }
            )
        ranked.sort(key=lambda item: (-item["retrieval_score"], item["proposal_id"]))
        return ranked[:bounded_limit]

    def _persist_score(self, score: ProposalScore) -> None:
        self.conn.execute(
            """
            INSERT INTO memory_proposal_scores (
                proposal_id, project_id, observation_count, distinct_session_count,
                distinct_turn_count, exposure_count, distinct_call_count,
                quality_score, principle_score,
                memory_similarity, query_similarity, conflict_count, composite_score,
                eligible, blocked_reason, score_revision, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                project_id = excluded.project_id,
                observation_count = excluded.observation_count,
                distinct_session_count = excluded.distinct_session_count,
                distinct_turn_count = excluded.distinct_turn_count,
                exposure_count = excluded.exposure_count,
                distinct_call_count = excluded.distinct_call_count,
                quality_score = excluded.quality_score,
                principle_score = excluded.principle_score,
                memory_similarity = excluded.memory_similarity,
                query_similarity = excluded.query_similarity,
                conflict_count = excluded.conflict_count,
                composite_score = excluded.composite_score,
                eligible = excluded.eligible,
                blocked_reason = excluded.blocked_reason,
                score_revision = excluded.score_revision,
                updated_at = excluded.updated_at
            """,
            (
                score.proposal_id,
                score.project_id,
                score.observation_count,
                score.distinct_session_count,
                score.distinct_turn_count,
                score.exposure_count,
                score.distinct_call_count,
                score.quality_score,
                score.principle_score,
                score.memory_similarity,
                score.query_similarity,
                score.conflict_count,
                score.composite_score,
                int(score.eligible),
                score.blocked_reason,
                score.score_revision,
                _utc_text(None),
            ),
        )


def collect_vector_evidence(
    engine: Any,
    proposal_id: str,
    *,
    query: str = "",
    session_id: str = "",
    turn_id: str = "",
    call_id: str = "",
) -> dict[str, Any]:
    """Compatibility wrapper over the bounded batch evidence interface."""

    return collect_vector_evidence_batch(
        engine,
        [
            VectorEvidenceRequest(
                proposal_id=proposal_id,
                query=query,
                session_id=session_id,
                turn_id=turn_id,
                call_id=call_id,
            )
        ],
    )[0]


def collect_vector_evidence_batch(
    engine: Any,
    requests: list[VectorEvidenceRequest],
) -> list[dict[str, Any]]:
    """Batch semantic evidence generation without persisting pending vectors."""

    if not requests:
        return []
    if len(requests) > 64:
        raise ValueError("proposal_vector_batch_too_large")

    conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if conn is None:
        return [
            {"status": "degraded", "reason": "canonical_store_unavailable"} for _request in requests
        ]

    results: list[dict[str, Any] | None] = [None] * len(requests)
    prepared: list[tuple[int, VectorEvidenceRequest, dict[str, Any]]] = []
    with _engine_write_guard(engine):
        automation = ProposalAutomation(conn)
        store = MemoryProposalStore(conn)
        for index, request in enumerate(requests):
            proposal = store.get(request.proposal_id)
            if proposal is None or proposal["status"] != "pending":
                results[index] = {"status": "skipped", "reason": "proposal_not_pending"}
                continue
            prepared.append((index, request, proposal))
        if conn.in_transaction:
            conn.commit()
    if not prepared:
        return [dict(result or {}) for result in results]

    embedder = getattr(engine, "_embedder", None)
    if embedder is None:
        ensure_embedder = getattr(engine, "ensure_runtime_embedder", None)
        embedder = ensure_embedder() if callable(ensure_embedder) else None
    if embedder is None:
        for index, _request, _proposal in prepared:
            results[index] = {"status": "degraded", "reason": "embedder_unavailable"}
        return [dict(result or {}) for result in results]
    identity = _embedding_identity(embedder)
    model_name = str(getattr(embedder, "model_name", "") or "")
    if (
        not identity
        or identity.startswith("fallback-zero")
        or model_name.startswith("fallback-zero")
    ):
        for index, _request, _proposal in prepared:
            results[index] = {
                "status": "degraded",
                "reason": "embedding_fallback_unavailable",
            }
        return [dict(result or {}) for result in results]

    texts: list[str] = []
    vector_spans: dict[int, tuple[int, int | None]] = {}
    for index, request, proposal in prepared:
        proposal_vector_index = len(texts)
        texts.append(str(proposal["content"]))
        query_vector_index = None
        if request.query.strip():
            query_vector_index = len(texts)
            texts.append(request.query)
        vector_spans[index] = (proposal_vector_index, query_vector_index)
    try:
        embed_batch = getattr(embedder, "embed_batch", None)
        if callable(embed_batch):
            vectors = [list(vector) for vector in embed_batch(texts)]
        else:
            vectors = [list(embedder.embed(text)) for text in texts]
    except Exception as exc:
        for index, _request, _proposal in prepared:
            results[index] = {
                "status": "degraded",
                "reason": "proposal_embedding_batch_failed",
                "error_class": exc.__class__.__name__,
            }
        return [dict(result or {}) for result in results]
    if len(vectors) != len(texts):
        for index, _request, _proposal in prepared:
            results[index] = {
                "status": "degraded",
                "reason": "embedding_response_count_mismatch",
            }
        return [dict(result or {}) for result in results]

    eligible: list[
        tuple[int, VectorEvidenceRequest, dict[str, Any], list[float], list[float] | None]
    ] = []
    for index, request, proposal in prepared:
        proposal_vector_index, query_vector_index = vector_spans[index]
        vector = vectors[proposal_vector_index]
        if not vector or not any(value != 0.0 for value in vector):
            results[index] = {"status": "degraded", "reason": "embedding_zero_vector"}
            continue
        query_vector = vectors[query_vector_index] if query_vector_index is not None else None
        if query_vector is not None and (
            not query_vector or not any(value != 0.0 for value in query_vector)
        ):
            results[index] = {"status": "degraded", "reason": "query_embedding_zero_vector"}
            continue
        eligible.append((index, request, proposal, vector, query_vector))

    anchors = getattr(engine, "_principle_anchors", {})
    lancedb = getattr(engine, "_ldb", None)
    similar_by_index: dict[int, list[tuple[str, float]]] = {}
    candidate_memory_ids: set[str] = set()
    searchable: list[
        tuple[int, VectorEvidenceRequest, dict[str, Any], list[float], list[float] | None]
    ] = []
    for item in eligible:
        index, _request, _proposal, vector, _query_vector = item
        try:
            similar = lancedb.search_similar(vector, k=12) if lancedb is not None else []
        except Exception as exc:
            results[index] = {
                "status": "degraded",
                "reason": "proposal_memory_similarity_failed",
                "error_class": exc.__class__.__name__,
            }
            continue
        normalized_similar = [
            (str(memory_id), _bounded_score(similarity, 0.0)) for memory_id, similarity in similar
        ]
        similar_by_index[index] = normalized_similar
        candidate_memory_ids.update(memory_id for memory_id, _score in normalized_similar)
        searchable.append(item)

    memory_projects: dict[str, str] = {}
    if candidate_memory_ids:
        placeholders = ", ".join("?" for _memory_id in candidate_memory_ids)
        with _engine_write_guard(engine):
            rows = conn.execute(
                f"SELECT id, project_id FROM memories WHERE id IN ({placeholders})",
                tuple(sorted(candidate_memory_ids)),
            ).fetchall()
        memory_projects = {str(row[0]): str(row[1]) for row in rows}

    computed_by_index: dict[
        int,
        list[tuple[str, str, float, str, str, str, dict[str, Any]]],
    ] = {}
    for index, request, proposal, vector, query_vector in searchable:
        evidence_base = f"{identity}\x1f{proposal['content_hash']}"
        computed: list[tuple[str, str, float, str, str, str, dict[str, Any]]] = []
        principle_similarity = max(
            (
                _cosine(vector, list(anchor))
                for anchor in (anchors.values() if isinstance(anchors, dict) else [])
            ),
            default=0.0,
        )
        if principle_similarity > 0.0:
            computed.append(
                (
                    "principle_similarity",
                    f"principles:{evidence_base}",
                    principle_similarity,
                    "",
                    "",
                    "",
                    {"embedding_identity": identity},
                )
            )

        project_scores = [
            similarity
            for memory_id, similarity in similar_by_index.get(index, [])
            if memory_projects.get(memory_id) == str(proposal["project_id"])
        ]
        memory_similarity = max(project_scores, default=0.0)
        if memory_similarity > 0.0:
            computed.append(
                (
                    "memory_similarity",
                    f"memories:{evidence_base}",
                    memory_similarity,
                    "",
                    "",
                    "",
                    {
                        "embedding_identity": identity,
                        "candidate_count": len(project_scores),
                    },
                )
            )

        if query_vector is not None:
            query_similarity = _cosine(vector, query_vector)
            if query_similarity > 0.0:
                query_hash = _hash_text(request.query)
                computed.append(
                    (
                        "query_similarity",
                        f"query:{evidence_base}:{query_hash}",
                        query_similarity,
                        request.session_id,
                        request.turn_id,
                        request.call_id,
                        {"embedding_identity": identity, "query_hash": query_hash},
                    )
                )
        computed_by_index[index] = computed

    try:
        with _engine_write_guard(engine):
            automation = ProposalAutomation(conn)
            store = MemoryProposalStore(conn)
            for index, request, _proposal, _vector, _query_vector in searchable:
                current = store.get(request.proposal_id)
                if current is None or current["status"] != "pending":
                    results[index] = {"status": "skipped", "reason": "proposal_not_pending"}
                    continue
                computed = computed_by_index[index]
                for (
                    signal_type,
                    evidence_key,
                    value,
                    signal_session,
                    signal_turn,
                    signal_call,
                    metadata,
                ) in computed:
                    automation.record_signal(
                        request.proposal_id,
                        signal_type=signal_type,
                        evidence_key=evidence_key,
                        value=value,
                        session_id=signal_session,
                        turn_id=signal_turn,
                        call_id=signal_call,
                        metadata=metadata,
                    )
                score = automation.refresh_score(request.proposal_id)
                recorded = {
                    signal_type: value for signal_type, _key, value, _s, _t, _c, _m in computed
                }
                results[index] = {
                    "status": "recorded",
                    "embedding_identity": identity,
                    "signals": {key: round(value, 6) for key, value in recorded.items()},
                    "score_revision": score.score_revision,
                    "pending_vector_persisted": False,
                    "embedding_batch_size": len(texts),
                }
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [dict(result or {}) for result in results]


def evaluate_auto_promotion(engine: Any, proposal_id: str) -> dict[str, Any]:
    """Evaluate one projection and optionally reuse the canonical atomic promoter."""

    conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if conn is None:
        return {"status": "degraded", "reason": "canonical_store_unavailable"}
    mode = auto_promotion_mode()
    with _engine_write_guard(engine):
        try:
            automation = ProposalAutomation(conn)
            score = automation.refresh_score(proposal_id)
            result = {
                "status": "ineligible",
                "mode": mode,
                "proposal_id": proposal_id,
                "score": score.composite_score,
                "score_revision": score.score_revision,
                "reason": score.blocked_reason or None,
            }
            if mode == "off":
                conn.commit()
                return {**result, "status": "disabled", "reason": "auto_promotion_disabled"}
            if not score.eligible:
                conn.commit()
                return result
            embedder = getattr(engine, "_embedder", None)
            current_identity = _embedding_identity(embedder)
            vector_rows = conn.execute(
                """
                SELECT metadata_json FROM memory_proposal_signals
                WHERE proposal_id = ? AND signal_type IN (
                    'principle_similarity', 'memory_similarity', 'query_similarity'
                )
                """,
                (proposal_id,),
            ).fetchall()
            vector_identity_matches = any(
                _signal_embedding_identity(row[0]) == current_identity for row in vector_rows
            )
            require_vector = os.environ.get("PP_MEMORY_PROPOSAL_REQUIRE_VECTOR", "1") != "0"
            if require_vector and (not current_identity or not vector_identity_matches):
                conn.commit()
                return {**result, "reason": "vector_evidence_required"}
            evidence_key = f"score:{score.score_revision}:{score.composite_score:.6f}"
            if mode == "shadow":
                automation.record_signal(
                    proposal_id,
                    signal_type="would_promote",
                    evidence_key=evidence_key,
                    value=score.composite_score,
                    metadata={"score_revision": score.score_revision},
                )
                conn.commit()
                return {**result, "status": "would_promote", "reason": None}

            proposal = MemoryProposalStore(conn).get(proposal_id)
            if proposal is None:
                conn.rollback()
                return {**result, "reason": "proposal_not_found"}
            if promotion_queue_enabled():
                task = PromotionTaskStore(conn).enqueue(
                    proposal_id=proposal_id,
                    project_id=str(proposal["project_id"]),
                    risk_tier=risk_tier_for_proposal(proposal),
                    idempotency_key=evidence_key,
                )
                conn.commit()
                return {
                    **result,
                    "status": "queued",
                    "reason": None,
                    "task_id": task.task_id,
                    "risk_tier": task.risk_tier,
                }

            conn.commit()
            from plastic_promise.core.memory_proposals import promote_memory_proposal

            call_digest = _hash_text(f"{proposal_id}\x1f{evidence_key}")[:24]
            promoted = promote_memory_proposal(
                engine,
                proposal_id,
                actor="system:auto-proposal-promoter",
                call_id=f"auto-proposal:{call_digest}",
            )
            return {
                **result,
                "status": "promoted",
                "reason": None,
                "memory_id": promoted.memory_id,
                "created": promoted.created,
                "index_job_id": promoted.index_job_id,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def execute_promotion_task(
    engine: Any,
    lease: PromotionTaskLease,
    *,
    actor: str = "system:auto-proposal-promoter",
) -> dict[str, Any]:
    """Run one leased promotion task and persist success/failure evidence."""

    conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
    if conn is None:
        raise ProposalPolicyError("canonical_store_unavailable")
    tasks = PromotionTaskStore(conn)
    call_id = f"auto-proposal-task:{lease.task.task_id}:{lease.task.fencing_generation}"
    if lease.task.risk_tier in {"high", "critical"}:
        blocked = tasks.fail(
            lease,
            failure_code="risk_gate_required",
            failure_detail="high-risk proposal requires an explicit governance gate",
            retryable=False,
        )
        return {
            "status": blocked.status,
            "task_id": blocked.task_id,
            "failure_code": blocked.last_failure_code,
            "attempt_count": blocked.attempt_count,
        }

    def _safe_failure_detail(error: Exception) -> str:
        """Keep retry evidence useful without persisting provider/path secrets."""

        name = type(error).__name__.casefold()
        if "timeout" in name:
            return "provider_timeout"
        if isinstance(error, ProposalPolicyError):
            return "governance_policy_blocked"
        if "connection" in name or "network" in name:
            return "provider_unavailable"
        return "promotion_execution_failed"

    try:
        from plastic_promise.core.memory_proposals import promote_memory_proposal

        promoted = promote_memory_proposal(
            engine,
            lease.task.proposal_id,
            actor=actor,
            call_id=call_id,
        )
    except Exception as exc:
        retryable = not isinstance(exc, ProposalPolicyError)
        failed = tasks.fail(
            lease,
            failure_code=type(exc).__name__[:128],
            failure_detail=_safe_failure_detail(exc),
            retryable=retryable,
        )
        return {
            "status": failed.status,
            "task_id": failed.task_id,
            "failure_code": failed.last_failure_code,
            "attempt_count": failed.attempt_count,
        }
    completed = tasks.complete(lease, memory_id=promoted.memory_id)
    return {
        "status": completed.status,
        "task_id": completed.task_id,
        "memory_id": completed.memory_id,
        "attempt_count": completed.attempt_count,
    }


def _blocked_reason(
    proposal: dict[str, Any],
    *,
    observations: int,
    sessions: int,
    turns: int,
    quality: float,
    conflicts: int,
    composite: float,
) -> str:
    if proposal["status"] != "pending":
        return "proposal_not_pending"
    if observations < 2:
        return "insufficient_user_observations"
    if turns < 2:
        return "insufficient_distinct_turns"
    if sessions < 2:
        return "insufficient_distinct_sessions"
    if quality < 0.60:
        return "quality_below_threshold"
    if conflicts:
        return "unresolved_conflict"
    if composite < _promotion_threshold():
        return "score_below_threshold"
    return ""


def _promotion_threshold() -> float:
    try:
        value = float(os.environ.get(AUTO_PROMOTION_THRESHOLD_ENV, ""))
    except (TypeError, ValueError):
        value = DEFAULT_AUTO_PROMOTION_THRESHOLD
    if not math.isfinite(value):
        value = DEFAULT_AUTO_PROMOTION_THRESHOLD
    return min(0.99, max(0.70, value))


def _candidate_quality(candidate: ProposalCandidate) -> float:
    from plastic_promise.core.quality_gate import QualityGate

    metadata = dict(candidate.metadata or {})
    confidence = _bounded_score(metadata.get("classification_confidence"), 0.5)
    content = str(candidate.content or "")
    return _bounded_score(
        QualityGate().score(
            extracted={
                "category": candidate.category,
                "confidence": confidence,
                "l0_abstract": content[:80],
                "l1_summary": content[:300],
                "l2_content": content,
            },
            tags=[f"cat:{candidate.category}"],
            domain_hint=str(metadata.get("domain_hint") or "") or None,
        ),
        0.0,
    )


def _signal_metadata_json(metadata: dict[str, Any] | None) -> str:
    values = dict(metadata or {})
    forbidden = {"content", "prompt", "query", "raw_content", "transcript", "vector"}
    if any(str(key).casefold() in forbidden for key in values):
        raise ProposalPolicyError("proposal_signal_metadata_forbidden")
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_SIGNAL_METADATA_BYTES:
        raise ProposalPolicyError("proposal_signal_metadata_too_large")
    return encoded


def _embedding_identity(embedder: object | None) -> str:
    if embedder is None:
        return ""
    index_identity = str(getattr(embedder, "index_model_name", "") or "").strip()
    if index_identity:
        return index_identity
    return str(getattr(embedder, "model_name", "") or "").strip()


def _signal_embedding_identity(metadata_json: object) -> str:
    try:
        metadata = json.loads(str(metadata_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("embedding_identity") or "").strip()


def _engine_write_guard(engine: object):
    lock = getattr(engine, "_write_lock", None)
    return lock if lock is not None else nullcontext()


def _terms(text: object) -> set[str]:
    terms: set[str] = set()
    for token in _WORD_RE.findall(str(text or "").casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            terms.update(token[index : index + 2] for index in range(max(1, len(token) - 1)))
        else:
            terms.add(token)
    return {term for term in terms if term}


def _content_hash(content: object) -> str:
    normalized = " ".join(str(content or "").split()).strip()
    return "sha256:" + _hash_text(normalized)


def _hash_text(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _utc_text(value: datetime | str | None) -> str:
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_score(value: object, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(score):
        return default
    return min(1.0, max(0.0, score))


def _average(rows: list[tuple[str, str, str, float]]) -> float:
    return sum(value for _session, _turn, _call, value in rows) / len(rows) if rows else 0.0


def _max_value(rows: list[tuple[str, str, str, float]]) -> float:
    return max((value for _session, _turn, _call, value in rows), default=0.0)


def _cosine(first: list[float], second: list[float]) -> float:
    if not first or len(first) != len(second):
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0.0 or second_norm == 0.0:
        return 0.0
    return _bounded_score(dot / (first_norm * second_norm), 0.0)
