"""MCP Context 工具 — 上下文域 3 个工具

工具列表:
- context_supply : 【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包
- context_inject : 手动向 EntityGraph 注入原则关联边或注册新实体节点
- context_graph : 查询实体关联图谱数据
"""

import json
from typing import Any

from mcp.types import TextContent


async def handle_context_supply(engine: Any, args: dict) -> list[TextContent]:
    """Handle context_supply tool call.

    Core tool: calls ContextEngine.supply() and returns a three-layer
    structured context pack: Core/Related/Divergent layers.

    Args:
        engine: ContextEngine instance.
        args: {"task_description": str, "task_type"?: str, "scope"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    try:
        from plastic_promise.embedder import get_embedder
        task_description = args["task_description"]
        task_type = args.get("task_type", "general")
        scope = args.get("scope", "global")

        embedder = get_embedder()
        task_vector = embedder.embed(task_description)

        pack = engine.supply(task_description, task_vector, task_type, scope)

        try:
            from plastic_promise.reranker import cross_encode_rerank
            if pack.core:
                candidates = [(i.id, i.content, i.relevance) for i in pack.core]
                reranked = cross_encode_rerank(task_description, candidates)
                # Reorder pack.core to match reranked order
                score_map = dict(reranked)
                pack.core.sort(key=lambda i: score_map.get(i.id, i.relevance), reverse=True)
        except Exception:
            pass  # reranking is optional enhancement

        return [TextContent(type="text", text=pack.to_prompt())]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "context_supply"}, ensure_ascii=False))]


async def handle_context_inject(engine: Any, args: dict) -> Any:
    """Handle context_inject tool call.

    Manually injects principle-association edges into the EntityGraph,
    or registers new entity nodes.

    Args:
        engine: ContextEngine instance.
        args: {"entity_type": str, "entity_id": str, "entity_name": str,
               "entity_description"?: str, "related_entities"?: list[str]}.

    Returns:
        list[TextContent]: MCP response.
    """
    pass


async def handle_context_graph(engine: Any, args: dict) -> Any:
    """Handle context_graph tool call.

    Queries entity association graph: node list, edge relationships,
    multi-hop traversal, activation path visualization data.

    Args:
        engine: ContextEngine instance.
        args: {"start_node"?: str, "max_hops"?: int,
               "query_type"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    pass
