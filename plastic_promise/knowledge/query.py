"""Deterministic lexical retrieval over knowledge evidence chunks.

This is the degraded-first retrieval path: no cloud index, no vectors.
Project scope, lifecycle gates, and citation projection are enforced in
SQLite plus a small deterministic ranker.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from plastic_promise.knowledge.contracts import ChunkHit, QueryResult

if TYPE_CHECKING:
    from plastic_promise.knowledge.repository import KnowledgeRepository

_LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,}")
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]")
_SNIPPET_CHARS = 140


def tokenize(text: str) -> tuple[str, ...]:
    """Return deduplicated lowercase lexical tokens for scoring."""
    lowered = (text or "").lower()
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _LATIN_TOKEN_RE.findall(lowered) + _CJK_TOKEN_RE.findall(lowered):
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


class LexicalKnowledgeQuery:
    """Project-scoped lexical search with exact-identifier support."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repository = repository

    def search(
        self,
        project_id: str,
        query: str,
        *,
        space_id: str | None = None,
        limit: int = 10,
        include_stale: bool = False,
    ) -> QueryResult:
        started = time.monotonic()
        tokens = tokenize(query)
        gates: tuple[str, ...] = ("lexical",)
        if not tokens:
            return QueryResult(
                query=query,
                project_id=project_id,
                gates=gates,
                elapsed_ms=_elapsed_ms(started),
            )
        if include_stale:
            gates = gates + ("include_stale",)
        rows = self._repository.iter_searchable_chunks(
            project_id, space_id=space_id, include_stale=include_stale
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            score, snippet = self._score_chunk(row, tokens, query)
            if score <= 0.0:
                continue
            scored.append(
                (
                    score,
                    {
                        "chunk_id": str(row["chunk_id"]),
                        "version_id": str(row["version_id"]),
                        "source_id": str(row["source_id"]),
                        "source_name": str(row["source_name"]),
                        "version_no": int(row["version_no"]),
                        "ordinal": int(row["ordinal"]),
                        "kind": str(row["kind"]),
                        "header_path": tuple(json.loads(str(row["header_path_json"]))),
                        "source_start": int(row["source_start"]),
                        "source_end": int(row["source_end"]),
                        "text": str(row["text"]),
                        "score": score,
                        "snippet": snippet,
                    },
                )
            )
        scored.sort(key=lambda pair: (-pair[0], pair[1]["ordinal"]))
        total_hits = len(scored)
        hits = tuple(ChunkHit(**item) for _, item in scored[:limit])
        return QueryResult(
            query=query,
            project_id=project_id,
            hits=hits,
            total_hits=total_hits,
            degraded=False,
            elapsed_ms=_elapsed_ms(started),
            gates=gates,
        )

    @staticmethod
    def _score_chunk(row: Any, tokens: tuple[str, ...], raw_query: str) -> tuple[float, str]:
        text = str(row["text"])
        lowered = text.lower()
        header_text = " ".join(str(row["header_path_json"]).lower().split())
        title = str(row["document_title"]).lower()
        score = 0.0
        for token in tokens:
            occurrences = lowered.count(token)
            if occurrences:
                score += 1.0 + float(occurrences)
            if token in header_text:
                score += 3.0
            if token in title:
                score += 2.0
        # Exact identifier retrieval: chunk/source/version ids dominate.
        exact = str(raw_query).strip()
        if exact and (
            exact == str(row["chunk_id"])
            or exact == str(row["row_id"])
            or exact == str(row["version_id"])
        ):
            score += 50.0
        if score <= 0.0:
            return 0.0, ""
        snippet = _make_snippet(text, tokens)
        return score, snippet


def _make_snippet(text: str, tokens: tuple[str, ...]) -> str:
    lowered = text.lower()
    first = min((lowered.find(token) for token in tokens if lowered.find(token) >= 0), default=-1)
    if first < 0:
        return text[:_SNIPPET_CHARS]
    start = max(0, first - _SNIPPET_CHARS // 2)
    end = min(len(text), start + _SNIPPET_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def _elapsed_ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))
