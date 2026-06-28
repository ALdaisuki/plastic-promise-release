"""MCP Context 工具 — 上下文域 3 个工具

工具列表:
- context_supply : 【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包
- context_inject : 手动向 EntityGraph 注入原则关联边或注册新实体节点
- context_graph : 查询实体关联图谱数据
"""

import json
from typing import Any


async def handle_context_supply(engine: Any, args: dict) -> Any:
    """Handle context_supply tool call.

    Core tool: calls ContextEngine.supply() and returns a three-layer
    structured context pack: Core/Related/Divergent layers.

    Args:
        engine: ContextEngine instance.
        args: {"task_description": str, "task_type": str,
               "pre_context"?: str}.

    Returns:
        list[TextContent]: MCP response.
    """
    pass


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
