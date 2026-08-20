"""Knowledge domain registry: candidate activation and reversible lineage.

Knowledge domains are separate from behavior domains (DomainManager is not
reused; see ADR-0002 and the knowledge implementation notes).  Model-created
domains start as candidates and may auto-activate when evidence, distinct
usage, and separation thresholds pass.  Merge/split/retire always append a
reversible lineage event and never move content across knowledge spaces.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from plastic_promise.knowledge.contracts import knowledge_feature_gate
from plastic_promise.knowledge.repository import KnowledgeRepository

AUTO_DOMAINS_GATE = "PP_KNOWLEDGE_AUTO_DOMAINS"
MIN_SOURCES_ENV = "PP_KNOWLEDGE_DOMAIN_ACTIVATION_MIN_SOURCES"
MIN_SPACES_ENV = "PP_KNOWLEDGE_DOMAIN_ACTIVATION_MIN_SPACES"
DEFAULT_MIN_SOURCES = 2
DEFAULT_MIN_SPACES = 1

_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize_domain_name(name: str) -> str:
    """Normalize a domain name for identity and separation checks."""
    return _NORMALIZE_RE.sub("-", str(name or "").strip().lower()).strip("-")


def _int_setting(env_name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(env_name, str(default))))
    except (TypeError, ValueError):
        return default


def _evidence_counts(row: dict[str, Any]) -> tuple[int, int]:
    evidence = json.loads(str(row.get("evidence_json") or "{}"))
    sources = {s for s in str(evidence.get("source_ids") or "").split(",") if s}
    spaces = {s for s in str(evidence.get("space_ids") or "").split(",") if s}
    return len(sources), len(spaces)


def evaluate_domain_activations(repository: KnowledgeRepository, project_id: str) -> dict[str, Any]:
    """Promote candidates that pass evidence and separation thresholds."""
    gate = knowledge_feature_gate(AUTO_DOMAINS_GATE)
    if gate not in {"shadow", "on"}:
        return {"activated": 0, "gate": gate}
    min_sources = _int_setting(MIN_SOURCES_ENV, DEFAULT_MIN_SOURCES)
    min_spaces = _int_setting(MIN_SPACES_ENV, DEFAULT_MIN_SPACES)
    domains = repository.list_domains(project_id)
    claimed: set[str] = set()
    for row in domains:
        if row["kind"] == "active":
            claimed.add(normalize_domain_name(row["name"]))
            claimed.update(
                normalize_domain_name(alias)
                for alias in json.loads(str(row.get("aliases_json") or "[]"))
                if alias
            )
    activated = 0
    for row in domains:
        if row["kind"] != "candidate":
            continue
        name = normalize_domain_name(row["name"])
        if not name or name in claimed:
            continue
        source_count, space_count = _evidence_counts(row)
        if source_count < min_sources or space_count < min_spaces:
            continue
        repository.update_domain_kind(
            str(row["id"]),
            "active",
            event="activated",
            detail={
                "source_count": source_count,
                "distinct_spaces": space_count,
                "min_sources": min_sources,
                "min_spaces": min_spaces,
            },
        )
        claimed.add(name)
        activated += 1
    return {"activated": activated, "gate": gate}


def merge_domains(
    repository: KnowledgeRepository,
    project_id: str,
    *,
    target_id: str,
    source_id: str,
    reason: str,
) -> dict[str, Any]:
    """Merge ``source_id`` into ``target_id`` preserving aliases and lineage."""
    domains = {str(row["id"]): row for row in repository.list_domains(project_id)}
    target = domains.get(target_id)
    source = domains.get(source_id)
    if target is None or source is None:
        raise ValueError("merge_domain_not_found")
    if source_id == target_id:
        raise ValueError("merge_domain_same_target")
    if source["kind"] == "retired" or source["kind"] == "merged":
        raise ValueError("merge_domain_already_inactive")
    repository.append_domain_aliases(target_id, [str(source["name"])])
    repository.update_domain_kind(
        source_id,
        "merged",
        event="merged_into",
        detail={"target_id": target_id, "reason": reason[:500]},
    )
    repository.update_domain_kind(
        target_id,
        "active" if target["kind"] in {"candidate", "active"} else target["kind"],
        event="merge_accepted",
        detail={"source_id": source_id, "reason": reason[:500]},
    )
    return {
        "target_id": target_id,
        "source_id": source_id,
        "merged": True,
    }


def split_domain(
    repository: KnowledgeRepository,
    project_id: str,
    *,
    domain_id: str,
    children: list[dict[str, str]],
    reason: str,
) -> dict[str, Any]:
    """Retire ``domain_id`` and create candidate children with parent lineage."""
    if not children:
        raise ValueError("split_domain_requires_children")
    parent = next(
        (row for row in repository.list_domains(project_id) if row["id"] == domain_id), None
    )
    if parent is None:
        raise ValueError("split_domain_not_found")
    if parent["kind"] in {"retired", "merged"}:
        raise ValueError("split_domain_already_inactive")
    child_ids: list[str] = []
    for child in children:
        name = str(child.get("name") or "").strip()
        if not name:
            raise ValueError("split_domain_child_name_required")
        child_ids.append(
            repository.create_domain(
                project_id=project_id,
                name=name,
                description=str(child.get("description") or "")[:2000],
                kind="candidate",
                parent_domain_id=domain_id,
            )
        )
    repository.update_domain_kind(
        domain_id,
        "retired",
        event="split",
        detail={"children": child_ids, "reason": reason[:500]},
    )
    return {"parent_id": domain_id, "children": child_ids, "split": True}


def retire_domain(
    repository: KnowledgeRepository,
    project_id: str,
    *,
    domain_id: str,
    reason: str,
) -> dict[str, Any]:
    """Retire an inactive-eligible domain, appending a reversible lineage event."""
    row = next((row for row in repository.list_domains(project_id) if row["id"] == domain_id), None)
    if row is None:
        raise ValueError("retire_domain_not_found")
    if row["kind"] in {"retired", "merged"}:
        raise ValueError("retire_domain_already_inactive")
    repository.update_domain_kind(
        domain_id, "retired", event="retired", detail={"reason": reason[:500]}
    )
    return {"domain_id": domain_id, "retired": True}
