"""Wiki artifact lifecycle: draft -> validated -> active | pending_review.

Artifacts are derived projections (ADR-0002): SQLite lifecycle and citation
rows are canonical; Markdown is only an export/operator projection.  Low-risk
artifacts auto-promote when citation coverage, schema, project, and source
validity gates pass.  High-risk artifacts (finance, medical, legal, security,
production_operations) require explicit curator approval.
"""

from __future__ import annotations

import os
from typing import Any

from plastic_promise.knowledge.contracts import knowledge_feature_gate
from plastic_promise.knowledge.repository import KnowledgeRepository

WIKI_GATE = "PP_KNOWLEDGE_WIKI"
MIN_CITATION_COVERAGE_ENV = "PP_KNOWLEDGE_ARTIFACT_MIN_CITATION_COVERAGE"
DEFAULT_MIN_CITATION_COVERAGE = 0.8
ACTOR = "knowledge-artifacts"


def _coverage_setting() -> float:
    try:
        value = float(os.getenv(MIN_CITATION_COVERAGE_ENV, str(DEFAULT_MIN_CITATION_COVERAGE)))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_CITATION_COVERAGE
    return min(1.0, max(0.0, value))


def promote_eligible_artifacts(repository: KnowledgeRepository, project_id: str) -> dict[str, Any]:
    """Advance drafts through validation and (low-risk) activation."""
    gate = knowledge_feature_gate(WIKI_GATE)
    if gate not in {"shadow", "on"}:
        return {"promoted": 0, "pending_review": 0, "gate": gate}
    minimum = _coverage_setting()
    result = {"promoted": 0, "pending_review": 0, "validated": 0, "gate": gate}
    for artifact in repository.list_artifacts(project_id):
        if artifact["status"] != "draft":
            continue
        artifact_id = str(artifact["id"])
        coverage = repository.artifact_citation_coverage(artifact_id)
        repository.update_artifact_status(artifact_id, "validated", citation_coverage=coverage)
        result["validated"] += 1
        if coverage < minimum:
            continue
        if str(artifact["risk_tier"]) == "high":
            repository.update_artifact_status(artifact_id, "pending_review")
            repository.audit(
                project_id=project_id,
                actor=ACTOR,
                action="artifact_pending_review",
                object_type="knowledge_artifact",
                object_id=artifact_id,
                detail={"kind": artifact["kind"], "coverage": coverage},
            )
            result["pending_review"] += 1
        else:
            repository.update_artifact_status(artifact_id, "active")
            repository.audit(
                project_id=project_id,
                actor=ACTOR,
                action="artifact_activated",
                object_type="knowledge_artifact",
                object_id=artifact_id,
                detail={"kind": artifact["kind"], "coverage": coverage},
            )
            result["promoted"] += 1
    return result


def review_artifact(
    repository: KnowledgeRepository,
    artifact_id: str,
    *,
    decision: str,
    actor: str = ACTOR,
) -> dict[str, Any]:
    """Curator decision for a pending_review artifact (approve | reject)."""
    if decision not in {"approve", "reject"}:
        raise ValueError("review_decision_unsupported")
    artifact = repository.artifact_by_id(artifact_id)
    if artifact is None:
        raise ValueError("artifact_not_found")
    if artifact["status"] != "pending_review":
        raise ValueError("artifact_not_pending_review")
    next_status = "active" if decision == "approve" else "rejected"
    repository.update_artifact_status(artifact_id, next_status)
    repository.audit(
        project_id=str(artifact["project_id"]),
        actor=actor,
        action="artifact_reviewed",
        object_type="knowledge_artifact",
        object_id=artifact_id,
        detail={"decision": decision},
    )
    return {"artifact_id": artifact_id, "status": next_status}
