"""Deterministic post-store relation and conflict organization for ordinary memories."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/+#-]{1,}|[\u4e00-\u9fff]{2,}")
_EXPLICIT_SUPERSEDE_RE = re.compile(
    r"(?:替代|取代|不再使用|改用|由.+改为|supersedes?|replace[sd]?|instead of)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:不|不要|不再|禁止|never|not|no longer|disable[sd]?)", re.IGNORECASE)
_POSITIVE_RE = re.compile(
    r"(?:使用|采用|启用|允许|prefer|use|enable[sd]?|allow(?:ed)?)", re.IGNORECASE
)
_STOP_TOKENS = {
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "记忆",
    "项目",
    "系统",
    "使用",
    "采用",
    "决定",
    "需要",
    "should",
    "memory",
    "system",
    "project",
    "file",
    "sync",
    "use",
    "using",
    "decided",
}


@dataclass(frozen=True)
class RelationCandidate:
    memory_id: str
    relation: str
    score: float
    shared_tokens: tuple[str, ...]


def _tokens(text: object) -> set[str]:
    value = str(text or "")
    normalized_tokens = (token.casefold().strip("._:/-") for token in _TOKEN_RE.findall(value))
    tokens = {token for token in normalized_tokens if token and token not in _STOP_TOKENS}
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    for run in cjk_runs:
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return {token for token in tokens if token and token not in _STOP_TOKENS}


def _topic_tags(record: dict[str, Any]) -> list[str]:
    existing = {str(tag) for tag in record.get("tags") or [] if str(tag).strip()}
    candidates = sorted(
        (
            token
            for token in _tokens(record.get("content"))
            if 2 <= len(token) <= 48 and not token.isdigit()
        ),
        key=lambda token: (-len(token), token),
    )
    for token in candidates[:5]:
        existing.add(f"topic:{token}")
    return sorted(existing)


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _contradicts(new_text: str, old_text: str, shared: set[str]) -> bool:
    if not shared:
        return False
    new_negative = bool(_NEGATION_RE.search(new_text))
    old_negative = bool(_NEGATION_RE.search(old_text))
    if new_negative == old_negative:
        return False
    return bool(_POSITIVE_RE.search(new_text) or _POSITIVE_RE.search(old_text))


def _relation_id(source: str, relation: str, target: str) -> str:
    digest = hashlib.sha256(f"{source}\x1f{relation}\x1f{target}".encode()).hexdigest()
    return f"memory-relation:{digest[:24]}"


def _load_record(conn: Any, memory_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, content, project_id, domain, category, tags, memory_type "
        "FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        tags = json.loads(row[5] or "[]")
    except (TypeError, json.JSONDecodeError):
        tags = []
    return {
        "id": str(row[0]),
        "content": str(row[1] or ""),
        "project_id": str(row[2] or ""),
        "domain": str(row[3] or ""),
        "category": str(row[4] or ""),
        "tags": list(tags) if isinstance(tags, list) else [],
        "memory_type": str(row[6] or ""),
    }


def _candidate_rows(conn: Any, record: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, content, domain, category FROM memories "
        "WHERE project_id = ? AND id <> ? "
        "AND LOWER(TRIM(COALESCE(memory_type, ''))) <> 'synthesis' "
        "ORDER BY created_at DESC, id LIMIT ?",
        (record["project_id"], record["id"], limit),
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "content": str(row[1] or ""),
            "domain": str(row[2] or ""),
            "category": str(row[3] or ""),
        }
        for row in rows
    ]


def _classify_relations(
    record: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[RelationCandidate]:
    new_text = record["content"]
    new_tokens = _tokens(new_text)
    explicit_supersede = bool(_EXPLICIT_SUPERSEDE_RE.search(new_text))
    relations: list[RelationCandidate] = []
    for candidate in candidates:
        candidate_tokens = _tokens(candidate["content"])
        shared = new_tokens & candidate_tokens
        similarity = _similarity(new_tokens, candidate_tokens)
        same_topic = bool(shared) and (
            record["domain"] == candidate["domain"]
            or record["category"] == candidate["category"]
            or similarity >= 0.25
        )
        if not same_topic:
            continue
        if explicit_supersede and similarity >= 0.18:
            relation = "supersedes"
            score = max(0.75, similarity)
        elif _contradicts(new_text, candidate["content"], shared) and similarity >= 0.18:
            relation = "contradicts"
            score = max(0.65, similarity)
        elif similarity >= 0.22:
            relation = "related_to"
            score = similarity
        else:
            continue
        relations.append(
            RelationCandidate(
                memory_id=candidate["id"],
                relation=relation,
                score=min(1.0, score),
                shared_tokens=tuple(sorted(shared)[:8]),
            )
        )
    relations.sort(key=lambda item: (-item.score, item.memory_id))
    return relations[:5]


def organize_memory_relations(
    engine: Any,
    memory_id: str,
    *,
    call_id: str = "",
    max_candidates: int = 40,
) -> dict[str, Any]:
    """Add conservative links for a newly stored ordinary memory without rewriting old content."""
    storage = getattr(engine, "_sqlite", None)
    conn = getattr(storage, "_conn", None)
    if conn is None:
        return {"status": "skipped", "reason": "canonical_store_unavailable", "relations": []}
    record = _load_record(conn, memory_id)
    if record is None or record["memory_type"].casefold() == "synthesis":
        return {"status": "skipped", "reason": "ordinary_memory_unavailable", "relations": []}

    topic_tags = _topic_tags(record)
    if topic_tags != sorted(record["tags"]):
        patch = getattr(engine, "patch_ordinary_memory", None)
        if callable(patch):
            patch(
                memory_id,
                replacements={"tags": topic_tags},
                expected_project_id=record["project_id"],
                expected_tags=record["tags"],
                bump_memory_version=False,
            )
            record["tags"] = topic_tags

    relations = _classify_relations(
        record,
        _candidate_rows(conn, record, limit=max(1, min(int(max_candidates), 200))),
    )
    if not relations:
        return {
            "status": "completed",
            "relations": [],
            "relation_count": 0,
            "topic_tags": [tag for tag in topic_tags if tag.startswith("topic:")],
        }

    from plastic_promise.core.traceability import record_memory_lineage

    created: list[dict[str, Any]] = []
    lock = getattr(engine, "_write_lock", None)
    context = lock if lock is not None else _NullContext()
    with context:
        for relation in relations:
            edge_id = _relation_id(memory_id, relation.relation, relation.memory_id)
            edge = {
                "id": edge_id,
                "from": memory_id,
                "to": relation.memory_id,
                "relation": relation.relation,
                "weight": relation.score,
                "source_kind": "memory_store_organizer",
                "evidence_id": call_id,
                "metadata": {
                    "shared_tokens": list(relation.shared_tokens),
                    "conflict_resolution": "review_required"
                    if relation.relation == "contradicts"
                    else "",
                },
            }
            existing_edge = conn.execute(
                "SELECT source, target, relation, weight FROM behavior_graph_edges WHERE id = ?",
                (edge_id,),
            ).fetchone()
            edge_current = bool(
                existing_edge
                and str(existing_edge[0]) == memory_id
                and str(existing_edge[1]) == relation.memory_id
                and str(existing_edge[2]) == relation.relation
                and abs(float(existing_edge[3]) - relation.score) <= 1e-9
            )
            if not edge_current:
                writer = getattr(storage, "upsert_graph_edge_ordinary", None)
                if not callable(writer) or not writer(edge):
                    continue
            runtime_edges = getattr(engine, "_graph_edges", None)
            if isinstance(runtime_edges, list):
                runtime_edge = next(
                    (
                        candidate
                        for candidate in runtime_edges
                        if str(candidate.get("from") or "") == memory_id
                        and str(candidate.get("to") or "") == relation.memory_id
                        and str(candidate.get("relation") or "") == relation.relation
                    ),
                    None,
                )
                if runtime_edge is None:
                    runtime_edges.append(dict(edge))
                else:
                    runtime_edge.update(edge)
            lineage_exists = conn.execute(
                "SELECT 1 FROM memory_lineage WHERE memory_id = ? AND parent_memory_id = ? "
                "AND relation = ? LIMIT 1",
                (memory_id, relation.memory_id, relation.relation),
            ).fetchone()
            if lineage_exists is None:
                record_memory_lineage(
                    conn,
                    memory_id=memory_id,
                    parent_memory_id=relation.memory_id,
                    relation=relation.relation,
                    call_id=call_id,
                    metadata={
                        "score": round(relation.score, 4),
                        "shared_tokens": list(relation.shared_tokens),
                    },
                )
                conn.commit()
            created.append(
                {
                    "memory_id": relation.memory_id,
                    "relation": relation.relation,
                    "score": round(relation.score, 4),
                }
            )
    return {
        "status": "completed",
        "relation_count": len(created),
        "relations": created,
        "conflict_count": sum(item["relation"] == "contradicts" for item in created),
        "topic_tags": [tag for tag in topic_tags if tag.startswith("topic:")],
    }


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False
