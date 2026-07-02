"""MCP Context 工具 — 上下文域 4 个工具

工具列表:
- context_supply      : 【核心工具】调用 ContextEngine.supply()，返回三层结构化上下文包
- context_inject      : 手动向 EntityGraph 注入原则关联边或注册新实体节点
- context_graph       : 查询实体关联图谱数据
- auto_context_inject : 统一自动化上下文注入
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
        from plastic_promise.core.embedder import get_embedder, FallbackEmbedder

        task_description = args["task_description"]
        task_type = args.get("task_type", "general")
        scope = args.get("scope", "global")

        try:
            embedder = get_embedder(fallback_on_error=False)
            task_vector = await embedder.aembed(task_description)
        except Exception:
            # Embedding service unavailable — use zero-vector fallback.
            # ContextEngine._text_retrieval uses pure text matching
            # (CJK bigrams / word split) which works without embeddings.
            embedder = FallbackEmbedder()
            task_vector = await embedder.aembed(task_description)

        pack = engine.supply(task_description, task_vector, task_type, scope)

        try:
            from plastic_promise.core.reranker import cross_encode_rerank

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
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "context_supply"}, ensure_ascii=False),
            )
        ]


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
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "entity_type is required. Valid: principle, task, memory, code_module"
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        if not entity_id:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "entity_id is required"}, ensure_ascii=False),
                )
            ]

        valid_types = {"principle", "task", "memory", "code_module"}
        if entity_type not in valid_types:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Unknown entity_type '{entity_type}'. Valid: {', '.join(sorted(valid_types))}"
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        # Route through existing PrincipleManager for principle type
        if entity_type == "principle":
            result = engine.register_entity(
                entity_type="principle",
                entity_id=entity_id,
                entity_name=entity_name,
                entity_description=entity_description,
                related_entities=related_entities,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "injected": {
                                "node_id": result["node_id"],
                                "type": entity_type,
                                "name": entity_name,
                                "is_new": result["is_new"],
                                "edges_created": result["edges_created"],
                            }
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

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
            return [
                TextContent(type="text", text=json.dumps({"error": str(ve)}, ensure_ascii=False))
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "injected": result,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "context_inject"}, ensure_ascii=False),
            )
        ]


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
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"Unknown query_type '{query_type}'. "
                            f"Valid: {', '.join(sorted(valid_queries))}"
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        result = engine.query_graph(
            query_type=query_type,
            start_node=start_node,
            max_hops=max_hops,
        )

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "context_graph"}, ensure_ascii=False),
            )
        ]


# ---------------------------------------------------------------------------
# auto_context_inject — 统一自动化上下文注入
# ---------------------------------------------------------------------------


async def handle_auto_context_inject(engine: Any, args: dict) -> list[TextContent]:
    """Unified automated context injection across Pi Agent, Claude Code, and SoulBridge.

    Chains: skill_session_start → SoulLoop.pre_task_v2 → memory_store → skill_session_complete.
    Graceful degradation: any internal failure returns partial data, never blocks.

    Args:
        engine: ContextEngine instance.
        args:
            task_description: str (required) — Current task description
            task_type: str — Task type (default "general")
            source: str — "pi_agent" | "claude_code" | "manual" (default "manual")
            scope: str — Retrieval scope (default "global")

    Returns:
        list[TextContent]: entity_id, context_pack, principles, inject_memory_id, stats
    """
    task_description = args.get("task_description", "")
    task_type = args.get("task_type", "general")
    source = args.get("source", "manual")
    scope = args.get("scope", "global")

    skill_name = f"auto_inject:{source}"
    entity_id = None
    context_pack = None
    principles: list[dict] = []
    inject_memory_id = None
    errors: list[str] = []

    # ── Step 1: skill_session_start ──
    try:
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        start_result = await handle_skill_session_start(
            engine,
            {
                "skill_name": skill_name,
                "task_description": task_description,
                "parent_entity_id": None,
            },
        )
        start_data = json.loads(start_result[0].text)
        entity_id = start_data.get("entity_id")
        principles = start_data.get("activated_principles", [])
    except Exception as e:
        errors.append(f"skill_session_start: {e}")

    # ── Step 2: SoulLoop.pre_task_v2 → ContextEngine.supply() ──
    try:
        from plastic_promise.loop.soul_loop import SoulLoop

        loop = SoulLoop(engine=engine)
        pack = loop.pre_task_v2(task_description, task_type)
        context_pack = {
            "core": [
                {"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                for i in getattr(pack, "core", [])
            ],
            "related": [
                {"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                for i in getattr(pack, "related", [])
            ],
            "divergent": [
                {"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                for i in getattr(pack, "divergent", [])
            ],
        }
        # Extract principles from pack if not already populated
        if not principles:
            pack_principles = getattr(pack, "activated_principles", [])
            if pack_principles:
                principles = pack_principles
    except Exception as e:
        errors.append(f"pre_task_v2: {e}")
        # Fallback: call principle_activate directly as safety net
        try:
            from plastic_promise.mcp.tools.principles import handle_principle_activate

            pa_result = await handle_principle_activate(
                engine,
                {
                    "task_type": task_type,
                    "task_description": task_description,
                },
            )
            pa_data = json.loads(pa_result[0].text)
            principles = pa_data.get("activated", [])
        except Exception:
            pass

    # ── Step 3: memory_store — inject record into memory pool ──
    try:
        from plastic_promise.mcp.tools.memory import handle_memory_store

        core_count = len(context_pack.get("core", [])) if context_pack else 0
        principle_names = ", ".join(p.get("name", "?") for p in principles[:5])
        content = (
            f"[AUTO INJECT] {task_description}\n"
            f"core_items: {core_count}\n"
            f"activated_principles: {principle_names}"
        )
        tags = [
            "auto_inject",
            f"source:{source}",
            f"skill:{skill_name}",
            "task:done",
        ]
        if entity_id:
            tags.append(f"entity:{entity_id}")
        store_result = await handle_memory_store(
            engine,
            {
                "content": content,
                "memory_type": "experience",
                "source": "auto_inject",
                "entity_ids": [entity_id] if entity_id else [],
                "tags": tags,
                "max_llm_calls": 0,  # skip LLM classify — auto_inject content is structured already
            },
        )
        if store_result and len(store_result) > 0:
            store_data = json.loads(store_result[0].text)
            inject_memory_id = store_data.get("memory_id") if isinstance(store_data, dict) else None
        else:
            inject_memory_id = None
    except Exception as e:
        errors.append(f"memory_store: {e}")

    # ── Step 4: skill_session_complete — auto-complete (inject is instant) ──
    if entity_id:
        try:
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

            await handle_skill_session_complete(
                engine,
                {
                    "entity_id": entity_id,
                    "outcome": "注入完成",
                    "artifacts": [],
                },
            )
        except Exception as e:
            errors.append(f"skill_session_complete: {e}")

    # ── Build response ──
    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        "context_pack": context_pack,
        "principles": principles,
        "inject_memory_id": inject_memory_id,
        "errors": errors if errors else None,
        "partial": len(errors) > 0,
    }

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
