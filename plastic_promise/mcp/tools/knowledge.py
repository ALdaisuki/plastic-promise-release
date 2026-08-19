"""MCP knowledge tool handlers — Markdown-first knowledge retrieval."""

from __future__ import annotations

import json

from mcp.types import TextContent

from plastic_promise.knowledge.contracts import knowledge_db_path, knowledge_feature_gate
from plastic_promise.knowledge.query import LexicalKnowledgeQuery
from plastic_promise.knowledge.repository import KnowledgeRepository


async def handle_knowledge_search(engine: object, args: dict) -> list[TextContent]:
    """Execute a project-scoped lexical knowledge search with citations.

    The knowledge system is gated by PP_KNOWLEDGE_RETRIEVAL (off|shadow|on)
    and PP_KNOWLEDGE_SYSTEM; when either is off the tool returns a bounded
    degraded response instead of failing the caller.
    """
    if knowledge_feature_gate("PP_KNOWLEDGE_SYSTEM") not in {"shadow", "on"}:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "knowledge_search_disabled",
                        "tool": "knowledge_search",
                        "degraded": True,
                        "degrade_level": "warning",
                        "warnings": ["PP_KNOWLEDGE_SYSTEM is off"],
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    if knowledge_feature_gate("PP_KNOWLEDGE_RETRIEVAL") not in {"shadow", "on"}:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "knowledge_search_disabled",
                        "tool": "knowledge_search",
                        "degraded": True,
                        "degrade_level": "warning",
                        "warnings": ["PP_KNOWLEDGE_RETRIEVAL is off"],
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    project_id = str(args.get("project_id") or "project:unknown").strip()
    query_text = str(args.get("query") or "").strip()
    if not query_text:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "query_required",
                        "tool": "knowledge_search",
                        "degraded": True,
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, 50))
    include_stale = bool(args.get("include_stale") or False)
    space_id = args.get("space_id") or None

    repository = KnowledgeRepository(knowledge_db_path(), read_only=True)
    engine_query = LexicalKnowledgeQuery(repository)
    result = engine_query.search(
        project_id,
        query_text,
        space_id=space_id,
        limit=limit,
        include_stale=include_stale,
    )
    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "query": result.query,
                    "project_id": result.project_id,
                    "total_hits": result.total_hits,
                    "elapsed_ms": result.elapsed_ms,
                    "degraded": result.degraded,
                    "gates": list(result.gates),
                    "hits": [
                        {
                            "chunk_id": hit.chunk_id,
                            "source_id": hit.source_id,
                            "source_name": hit.source_name,
                            "version_no": hit.version_no,
                            "header_path": list(hit.header_path),
                            "score": round(hit.score, 3),
                            "snippet": hit.snippet,
                            "text": hit.text[:400],
                        }
                        for hit in result.hits
                    ],
                },
                ensure_ascii=False,
            ),
        )
    ]
