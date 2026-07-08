from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

CanonicalKind = Literal[
    "principle",
    "mcp_tool",
    "task_state",
    "project_alias",
    "code_symbol",
    "bilingual_synonym",
]


@dataclass(frozen=True)
class CanonicalHit:
    key: str
    target_id: str
    content: str
    kind: CanonicalKind
    confidence: float
    source_class: str = "system"


TASK_STATE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("task_pending", ("task:pending", "pending", "待处理")),
    ("task_accepted", ("task:accepted", "accepted", "已接受")),
    ("task_active", ("task:active", "active", "执行中")),
    ("task_done", ("task:done", "done", "完成")),
    ("task_review", ("task:review", "review", "审查")),
    ("task_reviewed", ("task:reviewed", "reviewed", "已审查")),
    ("task_rejected", ("task:rejected", "rejected", "打回")),
)


def lookup_canonical_hot(
    query: str,
    *,
    code_index: Any = None,
    domain_hint: str | None = None,
    limit: int = 12,
) -> list[CanonicalHit]:
    """Return deterministic high-confidence hits for stable symbols.

    This lookup is intentionally small and local. It does not replace BM25,
    vector search, or graph traversal; it surfaces exact stable keys for debug
    instrumentation and later gated enforcement.
    """

    if not query or limit <= 0:
        return []

    hits: list[CanonicalHit] = []
    hits.extend(_principle_hits(query))
    hits.extend(_task_state_hits(query))
    hits.extend(_synonym_hits(query, domain_hint))
    if code_index is not None:
        hits.extend(_code_index_hits(query, code_index))

    deduped: dict[str, CanonicalHit] = {}
    for hit in hits:
        existing = deduped.get(hit.key)
        if existing is None or hit.confidence > existing.confidence:
            deduped[hit.key] = hit
    return sorted(deduped.values(), key=lambda item: item.confidence, reverse=True)[:limit]


def canonical_hits_to_results(hits: Iterable[CanonicalHit]) -> list[tuple[str, float, str, str]]:
    return [(hit.target_id, float(hit.confidence), hit.content, "canonical_hot") for hit in hits]


def _principle_hits(query: str) -> list[CanonicalHit]:
    from plastic_promise.core.constants import CORE_PRINCIPLES

    hits: list[CanonicalHit] = []
    for principle in CORE_PRINCIPLES:
        principle_id = str(principle.get("id", ""))
        name = str(principle.get("name", ""))
        keywords = principle.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.split(",")]
        terms = [name, *[str(keyword) for keyword in keywords]]
        matched = next((term for term in terms if _matches_term(query, term)), "")
        if not matched:
            continue
        key = f"principle:{_slug(name or principle_id)}"
        hits.append(
            CanonicalHit(
                key=key,
                target_id=f"principle:{principle_id}",
                content=f"principle {name}: {principle.get('content', '')}",
                kind="principle",
                confidence=0.96,
                source_class="principle",
            )
        )
    return hits


def _task_state_hits(query: str) -> list[CanonicalHit]:
    hits: list[CanonicalHit] = []
    for state, aliases in TASK_STATE_ALIASES:
        if not any(_matches_term(query, alias) for alias in aliases):
            continue
        hits.append(
            CanonicalHit(
                key=f"task_state:{state}",
                target_id=f"task_state:{state}",
                content=f"task state {state}: aliases={', '.join(aliases)}",
                kind="task_state",
                confidence=0.90,
                source_class="system",
            )
        )
    return hits


def _synonym_hits(query: str, domain_hint: str | None) -> list[CanonicalHit]:
    from plastic_promise.core.query_expander import SYNONYM_MAP

    hits: list[CanonicalHit] = []
    for entry in SYNONYM_MAP:
        domains = entry.get("domains", [])
        if domain_hint and domain_hint not in domains:
            continue
        triggers = [*entry.get("cn", []), *entry.get("en", [])]
        if not any(_matches_term(query, str(trigger)) for trigger in triggers):
            continue
        expansions = [str(term) for term in entry.get("expansions", [])]
        if not expansions:
            continue
        canonical = _slug(expansions[0])
        hits.append(
            CanonicalHit(
                key=f"bilingual_synonym:{canonical}",
                target_id=f"bilingual_synonym:{canonical}",
                content=f"bilingual synonym {canonical}: expansions={', '.join(expansions)}",
                kind="bilingual_synonym",
                confidence=0.72,
                source_class="system",
            )
        )
    return hits


def _code_index_hits(query: str, code_index: Any) -> list[CanonicalHit]:
    evidence = getattr(code_index, "evidence", []) or []
    hits: list[CanonicalHit] = []
    for item in evidence:
        kind = str(item.get("kind", ""))
        if kind not in {"mcp_tool", "function", "method", "class", "test", "file", "doc"}:
            continue
        name = str(item.get("name", ""))
        item_id = str(item.get("id", ""))
        path = str(item.get("path", ""))
        terms = {name, item_id, path, name.replace("_", "-"), name.replace(".", " ")}
        if not any(_matches_term(query, term) for term in terms if term):
            continue
        canonical_kind: CanonicalKind = "mcp_tool" if kind == "mcp_tool" else "code_symbol"
        key_prefix = "mcp_tool" if kind == "mcp_tool" else "code_symbol"
        confidence = 0.95 if kind == "mcp_tool" else 0.86
        content = f"{kind} {name} in {path}: {str(item.get('content', ''))[:180]}"
        hits.append(
            CanonicalHit(
                key=f"{key_prefix}:{_slug(name or item_id)}",
                target_id=item_id,
                content=content,
                kind=canonical_kind,
                confidence=confidence,
                source_class="code",
            )
        )
    return hits


def _matches_term(query: str, term: str) -> bool:
    term = (term or "").strip()
    if not term:
        return False
    query_folded = query.casefold()
    term_folded = term.casefold()
    if _contains_cjk(term_folded):
        return term_folded in query_folded
    if re.search(r"[A-Za-z0-9]", term_folded):
        variants = {
            term_folded,
            term_folded.replace("_", "-"),
            term_folded.replace("-", "_"),
            term_folded.replace(".", " "),
        }
        normalized_query = _separator_normalized(query_folded)
        for variant in variants:
            if not variant:
                continue
            if variant in query_folded:
                return True
            if _separator_normalized(variant) in normalized_query:
                return True
            try:
                if re.search(r"\b" + re.escape(variant) + r"\b", query_folded):
                    return True
            except re.error:
                continue
    return term_folded in query_folded


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _separator_normalized(text: str) -> str:
    return re.sub(r"[\s_.\-:\/\\]+", "_", text.strip().casefold())


def _slug(text: str) -> str:
    slug = _separator_normalized(text)
    slug = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "", slug)
    return slug.strip("_") or "unknown"
