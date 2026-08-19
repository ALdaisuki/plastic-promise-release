"""Bridge validated DeepSec remediation candidates into shadow proposal scoring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from plastic_promise.core.memory_proposals import (
    ProposalCandidate,
    ProposalPolicyError,
    trusted_memory_origin,
)
from plastic_promise.core.proposal_promotion import (
    ProposalAutomation,
    VectorEvidenceRequest,
    collect_vector_evidence,
    collect_vector_evidence_batch,
)

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from plastic_promise.core.proposal_promotion import ObservationResult
    from plastic_promise.core.shield_scan_store import RemediationPatternCandidate


@dataclass(frozen=True)
class ShadowProposalLink:
    candidate_id: str
    proposal_id: str
    created: bool
    score: ObservationResult


class ShieldCandidateProposalBridge:
    """Create a pending, project-scoped proposal projection only after canary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _assert_candidate_binding(
        conn: sqlite3.Connection,
        candidate: RemediationPatternCandidate,
    ) -> None:
        row = conn.execute(
            "SELECT project_id, status, finding_id, source_version_id, redacted_pattern, "
            "severity, validation_projects_json, shadow_generation, shadow_proposal_id "
            "FROM security_remediation_candidates WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()
        if row is None:
            raise ProposalPolicyError("security_candidate_not_found")
        stored_validation_projects = tuple(str(item) for item in json.loads(str(row[6] or "[]")))
        if (
            str(row[0]) != str(candidate.project_id)
            or str(row[1]) != str(candidate.status)
            or str(row[2]) != str(candidate.finding_id)
            or str(row[3]) != str(candidate.source_version_id)
            or str(row[4]) != str(candidate.redacted_pattern)
            or str(row[5]) != str(candidate.severity)
            or stored_validation_projects != tuple(candidate.validation_project_ids)
            or str(row[7]) != str(candidate.shadow_generation)
            or str(row[8]) != str(candidate.shadow_proposal_id)
        ):
            raise ProposalPolicyError("security_candidate_ownership_invalid")
        generation = conn.execute(
            "SELECT project_id, status FROM security_shadow_generations WHERE generation_id = ?",
            (candidate.shadow_generation,),
        ).fetchone()
        if generation is None:
            raise ProposalPolicyError("security_candidate_shadow_generation_not_found")
        expected_generation_status = (
            "canary_passed" if candidate.status == "canary_passed" else "shadow"
        )
        if (
            str(generation[0]) != str(candidate.project_id)
            or str(generation[1]) != expected_generation_status
        ):
            raise ProposalPolicyError("security_candidate_shadow_generation_scope_mismatch")
        if candidate.shadow_proposal_id:
            proposal = conn.execute(
                "SELECT project_id, status, visibility, origin_role, origin_visibility, "
                "metadata_json FROM memory_proposals WHERE proposal_id = ?",
                (candidate.shadow_proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ProposalPolicyError("security_candidate_shadow_proposal_not_found")
            if (
                str(proposal[0]) != str(candidate.project_id)
                or str(proposal[1]) != "pending"
                or str(proposal[2]) != "project"
                or str(proposal[4]) != "project"
                or str(proposal[3]) != "system"
            ):
                raise ProposalPolicyError("security_candidate_shadow_proposal_scope_mismatch")
            metadata = json.loads(str(proposal[5] or "{}"))
            if (
                metadata.get("origin") != "deepsec_shield"
                or metadata.get("security_candidate_id") != candidate.candidate_id
                or metadata.get("source_version_id") != candidate.source_version_id
                or metadata.get("shadow_generation") != candidate.shadow_generation
            ):
                raise ProposalPolicyError("security_candidate_shadow_proposal_provenance_mismatch")

    def link(
        self,
        candidate: RemediationPatternCandidate,
        *,
        now: datetime | str | None = None,
    ) -> ShadowProposalLink:
        if candidate.status != "canary_passed":
            raise ProposalPolicyError("security_candidate_canary_required")
        if not candidate.shadow_generation:
            raise ProposalPolicyError("security_candidate_shadow_generation_required")
        if (
            not candidate.validation_project_ids
            or candidate.project_id not in candidate.validation_project_ids
        ):
            raise ProposalPolicyError("security_candidate_scope_invalid")
        self._assert_candidate_binding(self._conn, candidate)

        proposal_candidate = ProposalCandidate(
            content=candidate.redacted_pattern,
            category="fact",
            project_id=candidate.project_id,
            visibility="project",
            origin_role="system",
            origin_turn_hash=f"security-candidate:{candidate.candidate_id}",
            origin_call_id=f"security-shield:{candidate.candidate_id}",
            origin_visibility="project",
            metadata={
                "origin": "deepsec_shield",
                "security_candidate_id": candidate.candidate_id,
                "source_version_id": candidate.source_version_id,
                "shadow_generation": candidate.shadow_generation,
                "validation_project_count": len(candidate.validation_project_ids),
                "promotion_mode": "shadow",
            },
        )
        with trusted_memory_origin("security_shield"):
            observation = ProposalAutomation(self._conn).observe_candidate(
                proposal_candidate,
                now=now,
                observation_signal_type="shadow_observation",
            )
        attach_shadow_proposal(
            self._conn,
            candidate_id=candidate.candidate_id,
            proposal_id=str(observation.proposal["proposal_id"]),
        )
        return ShadowProposalLink(
            candidate_id=candidate.candidate_id,
            proposal_id=str(observation.proposal["proposal_id"]),
            created=observation.created,
            score=observation,
        )

    @staticmethod
    def collect_vector_evidence(
        engine: object,
        candidate: RemediationPatternCandidate,
        *,
        query: str = "",
        session_id: str = "",
        turn_id: str = "",
        call_id: str = "",
    ) -> dict[str, object]:
        """Reuse proposal vector evidence without widening candidate scope."""

        if candidate.status != "canary_passed":
            raise ProposalPolicyError("security_candidate_canary_required")
        if not candidate.shadow_proposal_id:
            raise ProposalPolicyError("security_candidate_shadow_proposal_required")
        conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
        if conn is None:
            raise ProposalPolicyError("security_candidate_store_unavailable")
        ShieldCandidateProposalBridge._assert_candidate_binding(conn, candidate)
        result = collect_vector_evidence(
            engine,
            candidate.shadow_proposal_id,
            query=query,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
        )
        return {
            "candidate_id": candidate.candidate_id,
            "project_id": candidate.project_id,
            "shadow_proposal_id": candidate.shadow_proposal_id,
            **result,
        }

    @staticmethod
    def collect_vector_evidence_batch(
        engine: object,
        candidates: list[RemediationPatternCandidate],
        *,
        query_by_candidate_id: dict[str, str] | None = None,
        session_id: str = "",
        turn_id: str = "",
        call_id: str = "",
    ) -> list[dict[str, object]]:
        """Batch candidate evidence without crossing project scope.

        The existing proposal batch implementation owns embedding reuse and
        signal persistence.  This adapter only validates the Shield shadow
        boundary and adds candidate provenance to each result.
        """

        if not candidates:
            return []
        project_ids = {str(candidate.project_id) for candidate in candidates}
        if len(project_ids) != 1:
            raise ProposalPolicyError("security_candidate_batch_scope_mismatch")
        query_map = query_by_candidate_id or {}
        requests: list[VectorEvidenceRequest] = []
        for candidate in candidates:
            if candidate.status != "canary_passed":
                raise ProposalPolicyError("security_candidate_canary_required")
            if not candidate.shadow_generation:
                raise ProposalPolicyError("security_candidate_shadow_generation_required")
            if not candidate.shadow_proposal_id:
                raise ProposalPolicyError("security_candidate_shadow_proposal_required")
            conn = getattr(getattr(engine, "_sqlite", None), "_conn", None)
            if conn is None:
                raise ProposalPolicyError("security_candidate_store_unavailable")
            ShieldCandidateProposalBridge._assert_candidate_binding(conn, candidate)
            requests.append(
                VectorEvidenceRequest(
                    proposal_id=candidate.shadow_proposal_id,
                    query=str(query_map.get(candidate.candidate_id, "") or ""),
                    session_id=session_id,
                    turn_id=turn_id,
                    call_id=call_id,
                )
            )
        results = collect_vector_evidence_batch(engine, requests)
        return [
            {
                "candidate_id": candidate.candidate_id,
                "project_id": candidate.project_id,
                "shadow_proposal_id": candidate.shadow_proposal_id,
                **result,
            }
            for candidate, result in zip(candidates, results, strict=True)
        ]


def attach_shadow_proposal(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    proposal_id: str,
) -> None:
    """Persist only the candidate-to-proposal projection relation."""

    normalized_candidate = str(candidate_id or "").strip()
    normalized_proposal = str(proposal_id or "").strip()
    if not normalized_candidate:
        raise ProposalPolicyError("security_candidate_required")
    if not normalized_proposal:
        raise ProposalPolicyError("security_candidate_shadow_proposal_required")
    candidate = conn.execute(
        "SELECT project_id, status FROM security_remediation_candidates WHERE candidate_id = ?",
        (normalized_candidate,),
    ).fetchone()
    if candidate is None:
        raise ProposalPolicyError("security_candidate_not_found")
    if str(candidate[1]) != "canary_passed":
        raise ProposalPolicyError("security_candidate_canary_required")
    generation = conn.execute(
        "SELECT project_id, status FROM security_shadow_generations "
        "WHERE generation_id = (SELECT shadow_generation "
        "FROM security_remediation_candidates WHERE candidate_id = ?)",
        (normalized_candidate,),
    ).fetchone()
    if generation is None:
        raise ProposalPolicyError("security_candidate_shadow_generation_not_found")
    if str(generation[0]) != str(candidate[0]):
        raise ProposalPolicyError("security_candidate_shadow_generation_scope_mismatch")
    if str(generation[1]) != "canary_passed":
        raise ProposalPolicyError("security_candidate_shadow_generation_not_canary_passed")
    proposal = conn.execute(
        "SELECT project_id, status, visibility, origin_role, origin_visibility, "
        "metadata_json FROM memory_proposals WHERE proposal_id = ?",
        (normalized_proposal,),
    ).fetchone()
    if proposal is None:
        raise ProposalPolicyError("security_candidate_shadow_proposal_not_found")
    if (
        str(proposal[0]) != str(candidate[0])
        or str(proposal[1]) != "pending"
        or str(proposal[2]) != "project"
        or str(proposal[4]) != "project"
        or str(proposal[3]) != "system"
    ):
        raise ProposalPolicyError("security_candidate_shadow_proposal_scope_mismatch")
    candidate_row = conn.execute(
        "SELECT source_version_id, shadow_generation FROM security_remediation_candidates "
        "WHERE candidate_id = ?",
        (normalized_candidate,),
    ).fetchone()
    metadata = json.loads(str(proposal[5] or "{}"))
    if (
        metadata.get("origin") != "deepsec_shield"
        or metadata.get("security_candidate_id") != normalized_candidate
        or candidate_row is None
        or metadata.get("source_version_id") != str(candidate_row[0])
        or metadata.get("shadow_generation") != str(candidate_row[1] or "")
    ):
        raise ProposalPolicyError("security_candidate_shadow_proposal_provenance_mismatch")

    conn.execute(
        "UPDATE security_remediation_candidates SET shadow_proposal_id = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE candidate_id = ? AND status = 'canary_passed'",
        (normalized_proposal, normalized_candidate),
    )


__all__ = ["ShadowProposalLink", "ShieldCandidateProposalBridge", "attach_shadow_proposal"]
