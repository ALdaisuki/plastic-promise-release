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


async def handle_context_inject(engine: Any, args: dict) -> list[TextContent]:
    """Handle context_inject tool call.

    Manually injects principle-association edges into the EntityGraph,
    or registers new entity nodes (task, memory, code_module).

    Args:
        engine: ContextEngine instance.
        args: {"entity_type": str, "entity_id": str, "entity_name": str,
               "entity_description"?: str, "related_entities"?: list[str]}.

    Returns:
        list[TextContent]: MCP response with injected entity info.
    """
    try:
        entity_type = args.get("entity_type", "")
        entity_id = args.get("entity_id", "")
        entity_name = args.get("entity_name", "")
        entity_description = args.get("entity_description", "")
        related_entities = args.get("related_entities", [])

        # Validate required fields
        if not entity_type:
            return [TextContent(type="text", text=json.dumps(
                {"error": "entity_type is required. Valid: principle, task, memory, code_module"},
                ensure_ascii=False))]
        if not entity_id:
            return [TextContent(type="text", text=json.dumps(
                {"error": "entity_id is required"},
                ensure_ascii=False))]

        valid_types = {"principle", "task", "memory", "code_module"}
        if entity_type not in valid_types:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown entity_type '{entity_type}'. Valid: {', '.join(sorted(valid_types))}"},
                ensure_ascii=False))]

        # Route through existing PrincipleManager for principle type
        if entity_type == "principle":
            # Build a single-principle inject: reuse inject_to_graph logic
            node_id = f"principle:{entity_id}"
            is_new = node_id not in engine._graph_nodes
            engine._graph_nodes[node_id] = {
                "type": "principle",
                "name": entity_name,
                "description": entity_description,
            }
            edges_created = 0
            if related_entities:
                for rel_id in related_entities:
                    edge = {
                        "from": node_id,
                        "to": rel_id,
                        "relation": "supports",
                        "weight": 0.7,
                    }
                    if edge not in engine._graph_edges:
                        engine._graph_edges.append(edge)
                        edges_created += 1

            return [TextContent(type="text", text=json.dumps({
                "injected": {
                    "node_id": node_id,
                    "type": entity_type,
                    "name": entity_name,
                    "is_new": is_new,
                    "edges_created": edges_created,
                }
            }, ensure_ascii=False, indent=2))]

        # All other entity types: use engine.register_entity()
        try:
            result = engine.register_entity(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                entity_description=entity_description,
                related_entities=related_entities,
            )
        except ValueError as ve:
            return [TextContent(type="text", text=json.dumps(
                {"error": str(ve)}, ensure_ascii=False))]

        return [TextContent(type="text", text=json.dumps({
            "injected": result,
        }, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "context_inject"}, ensure_ascii=False))]


async def handle_context_graph(engine: Any, args: dict) -> list[TextContent]:
    """Handle context_graph tool call.

    Queries entity association graph: node list, edge relationships,
    multi-hop traversal, activation path visualization data.

    Args:
        engine: ContextEngine instance.
        args: {"start_node"?: str, "max_hops"?: int,
               "query_type"?: str}.

    Returns:
        list[TextContent]: MCP response with graph data.
    """
    try:
        query_type = args.get("query_type", "full_graph")
        start_node = args.get("start_node")
        max_hops = args.get("max_hops", 3)

        valid_queries = {"node_info", "traverse", "full_graph", "neighbors"}
        if query_type not in valid_queries:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown query_type '{query_type}'. "
                          f"Valid: {', '.join(sorted(valid_queries))}"},
                ensure_ascii=False))]

        result = engine.query_graph(
            query_type=query_type,
            start_node=start_node,
            max_hops=max_hops,
        )

        return [TextContent(type="text", text=json.dumps(
            result, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": "context_graph"}, ensure_ascii=False))]
