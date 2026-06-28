# context_inject + context_graph + Auto-Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 2 `pass` stubs in MCP context tools with real implementations, add auto-injection of principles to the entity graph during `context_supply`, and fill 8 `pass` stubs in soul_principles.

**Architecture:** Follows existing `PrincipleManager.inject_to_graph()` pattern — `context_inject` routes through Manager classes where available, `context_graph` reads from `ContextEngine._graph_nodes`/`_graph_edges` via new public methods. Auto-injection adds a single call in `supply()` Phase 0 to write activated principles into the graph, making `_graph_traversal` actually useful.

**Tech Stack:** Python 3.10+, existing `ContextEngine` dict-based graph, `PrincipleManager`, MCP `mcp.types.TextContent`

## Global Constraints

- All MCP tool handlers must return `list[TextContent]` with JSON body
- Follow existing patterns in `mcp/tools/context.py` (import style, error handling)
- Use `engine._graph_nodes` (Dict[str, Dict]) and `engine._graph_edges` (List[Dict]) as backing store
- Node IDs use `{entity_type}:{entity_id}` format (e.g., `principle:4`, `task:debug`)
- Edge relation types: `activates`, `supports`, `references`
- Default edge weight: 0.7 (matching `PRINCIPLE_INHERITANCE_DECAY`)
- `max_hops` clamped to [1, 10]
- 6 module-level convenience functions in `soul_principles.py` delegate to `PrincipleManager()` instance

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `plastic_promise/core/context_engine.py` | Modify | + `register_entity()`, + `query_graph()`, + `_inject_activated_to_graph()` |
| `plastic_promise/mcp/tools/context.py` | Modify | Replace `handle_context_inject` and `handle_context_graph` `pass` stubs |
| `plastic_promise/principles/soul_principles.py` | Modify | Fill `get_all_principles()`, `get_by_domain()`, 4 module-level functions |
| `plastic_promise/core/constants.py` | Modify | Add CORE_PRINCIPLES 5-11, AUDIT_DIMENSIONS 4-7 |

---

### Task 1: Add `register_entity()` to ContextEngine

**Files:**
- Modify: `plastic_promise/core/context_engine.py:342` (after `supply()`, before internal methods)

**Interfaces:**
- Produces: `ContextEngine.register_entity(entity_type: str, entity_id: str, entity_name: str, entity_description: str = "", related_entities: list[str] = None) -> dict`

- [ ] **Step 1: Add `register_entity()` method**

Insert after the `supply()` method (line 421, before `# ========== 内部方法 ==========` comment):

```python
    # ========== 实体注册 ==========

    def register_entity(
        self,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        entity_description: str = "",
        related_entities: list[str] = None,
    ) -> dict:
        """Register an entity node and optionally create edges to related entities.

        Args:
            entity_type: One of "principle", "task", "memory", "code_module".
            entity_id: Unique identifier for this entity.
            entity_name: Human-readable name.
            entity_description: Optional description text.
            related_entities: Optional list of entity IDs to link to.

        Returns:
            dict with keys: node_id, type, edges_created
        """
        # Validate entity_type
        valid_types = {"principle", "task", "memory", "code_module"}
        if entity_type not in valid_types:
            raise ValueError(
                f"Unknown entity_type '{entity_type}'. "
                f"Valid: {', '.join(sorted(valid_types))}"
            )

        node_id = f"{entity_type}:{entity_id}"
        is_new = node_id not in self._graph_nodes

        # Create or update node
        self._graph_nodes[node_id] = {
            "type": entity_type,
            "name": entity_name,
            "description": entity_description or "",
        }

        # Create edges to related entities
        edges_created = 0
        if related_entities:
            for related_id in related_entities:
                edge = {
                    "from": node_id,
                    "to": related_id,
                    "relation": "supports",
                    "weight": PRINCIPLE_INHERITANCE_DECAY,
                }
                # Avoid exact duplicate edges
                if edge not in self._graph_edges:
                    self._graph_edges.append(edge)
                    edges_created += 1

        return {
            "node_id": node_id,
            "type": entity_type,
            "name": entity_name,
            "is_new": is_new,
            "edges_created": edges_created,
        }
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add ContextEngine.register_entity() public method"
```

---

### Task 2: Add `query_graph()` to ContextEngine

**Files:**
- Modify: `plastic_promise/core/context_engine.py:342` (after `register_entity()`)

**Interfaces:**
- Produces: `ContextEngine.query_graph(query_type: str, start_node: str = None, max_hops: int = 3) -> dict`

- [ ] **Step 1: Add `query_graph()` method**

Insert after `register_entity()`:

```python
    def query_graph(
        self,
        query_type: str,
        start_node: str = None,
        max_hops: int = 3,
    ) -> dict:
        """Query the entity association graph.

        Args:
            query_type: "node_info" | "traverse" | "full_graph" | "neighbors"
            start_node: Node ID for node_info/traverse/neighbors queries.
            max_hops: Max traversal depth (clamped to [1, 10]).

        Returns:
            dict with nodes, edges, and optional traversal_path.
        """
        max_hops = max(1, min(max_hops, 10))

        if query_type == "full_graph":
            return {
                "nodes": dict(self._graph_nodes),
                "edges": list(self._graph_edges),
            }

        if query_type == "node_info":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            node = self._graph_nodes[start_node]
            in_edges = [e for e in self._graph_edges if e.get("to") == start_node]
            out_edges = [e for e in self._graph_edges if e.get("from") == start_node]
            return {
                "nodes": {start_node: node},
                "edges": in_edges + out_edges,
                "in_degree": len(in_edges),
                "out_degree": len(out_edges),
            }

        if query_type == "neighbors":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                }
            neighbor_ids = set()
            edges = []
            for e in self._graph_edges:
                if e.get("from") == start_node:
                    neighbor_ids.add(e.get("to"))
                    edges.append(e)
                elif e.get("to") == start_node:
                    neighbor_ids.add(e.get("from"))
                    edges.append(e)
            nodes = {
                nid: self._graph_nodes[nid]
                for nid in neighbor_ids
                if nid in self._graph_nodes
            }
            return {"nodes": nodes, "edges": edges, "neighbor_count": len(nodes)}

        if query_type == "traverse":
            if not start_node or start_node not in self._graph_nodes:
                return {
                    "error": f"Node '{start_node}' not found",
                    "nodes": {},
                    "edges": [],
                    "traversal_path": [],
                }
            # BFS traversal
            visited = set()
            queue = [(start_node, 0)]
            traversal_path = []
            all_nodes = {}
            all_edges = []

            from collections import deque
            q = deque([(start_node, 0)])
            while q:
                current, depth = q.popleft()
                if current in visited or depth > max_hops:
                    continue
                visited.add(current)
                traversal_path.append(current)
                if current in self._graph_nodes:
                    all_nodes[current] = self._graph_nodes[current]
                # Follow outgoing edges
                for e in self._graph_edges:
                    if e.get("from") == current:
                        all_edges.append(e)
                        target = e.get("to")
                        if target and target not in visited:
                            q.append((target, depth + 1))

            return {
                "nodes": all_nodes,
                "edges": all_edges,
                "traversal_path": traversal_path,
                "hops": max_hops,
            }

        return {
            "error": f"Unknown query_type '{query_type}'. "
                     f"Valid: node_info, traverse, full_graph, neighbors"
        }
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add ContextEngine.query_graph() public method"
```

---

### Task 3: Add auto-injection to `supply()`

**Files:**
- Modify: `plastic_promise/core/context_engine.py:368` (Phase 0 in `supply()`)

**Interfaces:**
- Consumes: `_activate_principles()` (existing, returns `List[str]`)
- Produces: `_inject_activated_to_graph()` (new private method, writes to `_graph_nodes`/`_graph_edges`)

- [ ] **Step 1: Add `_inject_activated_to_graph()` private method**

Insert after `query_graph()`, before `_activate_principles()`:

```python
    def _inject_activated_to_graph(
        self, activated_names: List[str], task_type: str
    ) -> int:
        """Write activated principles into the entity graph.

        Called automatically during supply() Phase 0. Creates/updates
        principle nodes and adds task_type -> principle edges so
        _graph_traversal has data to work with.

        Args:
            activated_names: List of principle names from _activate_principles().
            task_type: Task type label for the source edge.

        Returns:
            Number of edges created.
        """
        from plastic_promise.core.constants import CORE_PRINCIPLES

        edges_created = 0
        for p in CORE_PRINCIPLES:
            if p["name"] not in activated_names:
                continue

            node_id = f"principle:{p['id']}"
            # Ensure principle node exists
            if node_id not in self._graph_nodes:
                self._graph_nodes[node_id] = {
                    "type": "principle",
                    "name": p["name"],
                    "description": p["content"],
                    "domain": p["domain"],
                }

            # Create edge: task_type -> principle
            edge = {
                "from": f"task_type:{task_type}",
                "to": node_id,
                "relation": "activates",
                "weight": 0.85,
            }
            if edge not in self._graph_edges:
                self._graph_edges.append(edge)
                edges_created += 1

        return edges_created
```

- [ ] **Step 2: Modify `supply()` Phase 0 to call injection**

In `supply()`, replace line 368:
```python
        # Phase 0: 原则注入
        activated = self._activate_principles(task_type, task_description)
```

With:
```python
        # Phase 0: 原则注入 + 图谱自动注入
        activated = self._activate_principles(task_type, task_description)
        if self.enable_principles:
            self._inject_activated_to_graph(activated, task_type)
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: auto-inject activated principles into graph during supply()"
```

---

### Task 4: Implement `handle_context_inject` MCP tool

**Files:**
- Modify: `plastic_promise/mcp/tools/context.py:56-70`

**Interfaces:**
- Consumes: `engine.register_entity()`, `PrincipleManager(engine).inject_to_graph()`
- Produces: `list[TextContent]` JSON response

- [ ] **Step 1: Replace `pass` with real implementation**

Replace the entire `handle_context_inject` function (lines 56-70):

```python
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
            from plastic_promise.principles.soul_principles import PrincipleManager
            pm = PrincipleManager(engine)
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
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/mcp/tools/context.py
git commit -m "feat: implement context_inject MCP tool (was pass stub)"
```

---

### Task 5: Implement `handle_context_graph` MCP tool

**Files:**
- Modify: `plastic_promise/mcp/tools/context.py:73-87`

**Interfaces:**
- Consumes: `engine.query_graph()`
- Produces: `list[TextContent]` JSON response

- [ ] **Step 1: Replace `pass` with real implementation**

Replace the entire `handle_context_graph` function (lines 73-87):

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/mcp/tools/context.py
git commit -m "feat: implement context_graph MCP tool (was pass stub)"
```

---

### Task 6: Fill `PrincipleManager.get_all_principles()` and `get_by_domain()`

**Files:**
- Modify: `plastic_promise/principles/soul_principles.py:373-398`

**Interfaces:**
- Consumes: `CORE_PRINCIPLES`, `PRINCIPLE_DOMAINS` from constants
- Produces: `get_all_principles() -> List[Dict]`, `get_by_domain(domain: str) -> List[Dict]`

- [ ] **Step 1: Replace both `pass` stubs**

Replace lines 377-398 (`get_all_principles` and `get_by_domain`):

```python
    def get_all_principles(self) -> List[Dict[str, Any]]:
        """获取所有核心原则的完整信息。

        Returns:
            所有原则列表，每项包含 id, name, content, domain, keywords
            以及运行时状态。
        """
        from plastic_promise.core.constants import CORE_PRINCIPLES
        return [dict(p) for p in CORE_PRINCIPLES]

    def get_by_domain(self, domain: str) -> List[Dict[str, Any]]:
        """按域筛选原则。

        Args:
            domain: 域名称 ("work", "life", "all")

        Returns:
            属于指定域的原则列表

        Raises:
            ValueError: 如果 domain 不在 PRINCIPLE_DOMAINS 中
        """
        from plastic_promise.core.constants import (
            CORE_PRINCIPLES,
            PRINCIPLE_DOMAINS,
        )
        if domain not in PRINCIPLE_DOMAINS:
            raise ValueError(
                f"Unknown domain '{domain}'. "
                f"Valid: {', '.join(PRINCIPLE_DOMAINS)}"
            )
        return [dict(p) for p in CORE_PRINCIPLES if p["domain"] == domain]
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/principles/soul_principles.py
git commit -m "feat: implement PrincipleManager.get_all_principles() and get_by_domain()"
```

---

### Task 7: Fill 6 module-level convenience functions in soul_principles

**Files:**
- Modify: `plastic_promise/principles/soul_principles.py:401-468`

**Interfaces:**
- Consumes: `PrincipleManager()` default instance
- Produces: 4 principle convenience functions + 2 query functions

- [ ] **Step 1: Replace all 6 module-level `pass` stubs**

Replace lines 405-468 (the 4 principle functions + note that get_all/get_by_domain are already covered by Task 6):

```python
def principle_activate(
    task_type: str,
    task_description: str = "",
    max_principles: int = 5,
) -> List[Dict[str, Any]]:
    """便捷函数：使用默认 PrincipleManager 激活原则。

    Args:
        task_type: 任务类型
        task_description: 任务描述文本
        max_principles: 最多激活原则数

    Returns:
        激活的原则列表
    """
    return PrincipleManager().activate(task_type, task_description, max_principles)


def principle_inherit(
    source_domain: str,
    target_domain: str = "all",
    principle_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 执行单向继承。

    Args:
        source_domain: 源域
        target_domain: 目标域
        principle_ids: 要继承的原则 ID 列表

    Returns:
        继承结果字典
    """
    return PrincipleManager().inherit(source_domain, target_domain, principle_ids)


def principle_diffuse(
    principle_id: Optional[int] = None,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 执行原则扩散。

    Args:
        principle_id: 要扩散的原则 ID，None 表示全部

    Returns:
        扩散结果字典
    """
    return PrincipleManager().diffuse(principle_id)


def principle_evaluate(
    principle_id: int,
    scenario: str,
) -> Dict[str, Any]:
    """便捷函数：使用默认 PrincipleManager 评价原则有效性。

    Args:
        principle_id: 原则 ID
        scenario: 场景描述

    Returns:
        评价结果字典
    """
    return PrincipleManager().evaluate(principle_id, scenario)
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/principles/soul_principles.py
git commit -m "feat: implement 6 module-level convenience functions in soul_principles"
```

---

### Task 8: Complete CORE_PRINCIPLES (5-11) and AUDIT_DIMENSIONS (4-7)

**Files:**
- Modify: `plastic_promise/core/constants.py:279-308` (CORE_PRINCIPLES) and `plastic_promise/core/constants.py:133-152` (AUDIT_DIMENSIONS)

**Interfaces:**
- Consumes: existing consequence texts in `soul_principles.py` and `tools/principles.py` for principles 5-11
- Produces: 7 new CORE_PRINCIPLES entries, 4 new AUDIT_DIMENSIONS entries

- [ ] **Step 1: Add principles 5-11 to CORE_PRINCIPLES**

Append after principle #4 (after line 308):

```python
    {
        "id": 5,
        "name": "约定优于约束——检验存在不等于有效",
        "content": "不要混淆存在与有效。有测试不等于测得好，有审计不等于审得对。每个机制必须经受反事实检验：如果它不存在，结果会不同吗？验证手段必须独立于被验证对象。",
        "domain": "all",
        "keywords": ["存在", "有效", "测试", "验证", "反事实", "独立", "机制", "不等于", "检验", "表面", "实质"],
    },
    {
        "id": 6,
        "name": "数据流驱动——追踪真实的数据流动",
        "content": "系统设计的依据必须是实际的数据流，而非假设的架构图。追踪代码执行的真实路径，记录模块间的实际耦合，用数据流图替代静态依赖分析。看不见的耦合是最危险的。",
        "domain": "all",
        "keywords": ["数据流", "追踪", "耦合", "依赖", "实际", "路径", "执行", "静态", "动态", "流动", "连接"],
    },
    {
        "id": 7,
        "name": "器官互保——每个子系统保护整个系统",
        "content": "没有一个子系统可以独自保证系统安全。每个模块都有责任检测上游的异常输入、保护下游的调用方。防线是网状的，不是链式的——一环断了，其他的要撑住。",
        "domain": "all",
        "keywords": ["互保", "子系统", "模块", "防线", "网状", "上游", "下游", "检测", "防护", "协同", "冗余"],
    },
    {
        "id": 8,
        "name": "工具即感官——LLM 的能力边界由工具决定",
        "content": "大语言模型本身只有文本输入输出。它的真正能力边界由工具链决定：没有代码执行工具就不能验证逻辑，没有搜索工具就不能获取新信息，没有记忆工具就会遗忘。不断扩展工具就是不断扩展能力。",
        "domain": "all",
        "keywords": ["工具", "感官", "能力", "边界", "扩展", "MCP", "API", "接口", "限制", "文本", "行动"],
    },
    {
        "id": 9,
        "name": "信任驱动约束——动态信任分调节自主权",
        "content": "约束不应是二元的（允许/禁止），而应是连续的、基于信任的动态调整。高信任时放宽约束释放效率，低信任时收紧约束保护安全。信任分由每次互动的反馈累积，可升可降。",
        "domain": "all",
        "keywords": ["信任", "约束", "动态", "自主权", "衰减", "宽松", "收紧", "连续", "反馈", "累积", "效率"],
    },
    {
        "id": 10,
        "name": "自演化闭环——评价驱动行为修正",
        "content": "系统必须能够观察自己的行为、评价行为的结果、根据评价修正未来的行为。这个闭环如果断裂，系统就会在不知不觉中退化。每一次交互都是一个训练样本，每一个错误都是一个改进机会。",
        "domain": "all",
        "keywords": ["演化", "闭环", "评价", "修正", "反馈", "退化", "观察", "改进", "学习", "自省", "迭代"],
    },
    {
        "id": 11,
        "name": "原则遗传——核心约定跨代传递",
        "content": "核心原则必须在 Agent 实例之间传递，不能每次启动都从零开始。新 Agent 应继承已有原则体系，通过单向扩散（work→all, life→all）和同步衰减确保核心约定在代际间延续。没有遗传就没有文化。",
        "domain": "all",
        "keywords": ["遗传", "继承", "传递", "约定", "扩散", "衰减", "代际", "文化", "延续", "启动", "初始化"],
    },
```

- [ ] **Step 2: Add audit dimensions 4-7 to AUDIT_DIMENSIONS**

Replace the comment line `# 维度映射自 SCARF 五维度扩展为七维度审计框架` and the entire AUDIT_DIMENSIONS dict (lines 133-152):

```python
# 维度映射自 SCARF 五维度扩展为七维度审计框架

AUDIT_DIMENSIONS = {
    "simplicity": {
        "name": "奥卡姆剃刀",
        "weight": 0.15,
        "description": "方案是否最简洁？是否存在不必要的实体或步骤？每一步只做当前最必要的事。",
        "principle_id": 1,
    },
    "transparency": {
        "name": "全过程可查可透明",
        "weight": 0.15,
        "description": "每步是否有完整 git 痕迹？审计日志是否可追溯？中间产物是否可验证？",
        "principle_id": 2,
    },
    "audit_closure": {
        "name": "自我审计闭环",
        "weight": 0.15,
        "description": "是否有根因分析？是否有改良措施？是否提炼了可迁移教训？量化评分是否准确？",
        "principle_id": 3,
    },
    "principle_activation": {
        "name": "原则激活率",
        "weight": 0.15,
        "description": "每次任务是否自动激活了相关原则？激活的原则是否被实际遵循？是否存在原则\"休眠\"？",
        "principle_id": 4,
    },
    "memory_supply": {
        "name": "记忆供给质量",
        "weight": 0.15,
        "description": "上下文供给是否充分？记忆召回的相关性和时效性如何？三层上下文包的比例是否合理？",
        "principle_id": 4,
    },
    "constraint_compliance": {
        "name": "约束合规度",
        "weight": 0.15,
        "description": "L0 硬边界是否有违规？L1 动态约束是否按信任分正确调整？L2 免疫巡检是否按时执行？",
        "principle_id": 9,
    },
    "feedback_closure": {
        "name": "反馈闭环率",
        "weight": 0.10,
        "description": "每次交互是否产生了反馈信号？adopted/rejected/ignored 的分布是否健康？反馈是否驱动了行为修正？",
        "principle_id": 10,
    },
}
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/constants.py
git commit -m "feat: complete CORE_PRINCIPLES (11/11) and AUDIT_DIMENSIONS (7/7)"
```

---

### Task 9: Verification

**Files:**
- Verify: All modified files, MCP server restart

- [ ] **Step 1: Verify module imports**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine()
# Test register_entity
r = engine.register_entity('task', 'test1', 'Test Task', 'A test', ['principle:1'])
assert r['node_id'] == 'task:test1'
print('register_entity:', r)

# Test query_graph
q = engine.query_graph('full_graph')
assert 'nodes' in q
print('query_graph nodes:', len(q['nodes']), 'edges:', len(q['edges']))

# Test auto-injection
engine.enable_principles = True
pack = engine.supply('测试任务', [0.1]*1024, 'general')
print('activated_principles:', pack.activated_principles)
print('graph nodes after supply:', len(engine._graph_nodes))
print('graph edges after supply:', len(engine._graph_edges))

print('ALL CHECKS PASSED')
"
```

- [ ] **Step 2: Verify principle manager**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.principles.soul_principles import (
    PrincipleManager, principle_activate, principle_inherit,
    principle_diffuse, principle_evaluate,
)
pm = PrincipleManager()
# Test get_all
all_p = pm.get_all_principles()
print(f'Total principles: {len(all_p)}')
assert len(all_p) == 11, f'Expected 11, got {len(all_p)}'

# Test get_by_domain
work_p = pm.get_by_domain('all')
print(f'All domain: {len(work_p)}')

# Test convenience functions
act = principle_activate('general')
print(f'Activated: {[a[\"name\"] for a in act]}')

diff = principle_diffuse()
print(f'Diffuse count: {diff[\"diffused_count\"]}')

# principle_evaluate needs string id
eval_result = principle_evaluate(4, '缺少上下文时的决策')
print(f'Evaluate principle 4: {eval_result[\"recommendation\"]}')

print('ALL CHECKS PASSED')
"
```

- [ ] **Step 3: Restart MCP server and test end-to-end**

Restart the MCP server via `! /mcp reconnect plastic-promise` or session restart, then verify:

```
1. principle_diffuse → returns 11 principles
2. context_inject {entity_type:"task", entity_id:"verify", entity_name:"Verification Task"} → success
3. context_graph {query_type:"node_info", start_node:"task:verify"} → returns node
4. context_supply("实现一个用户登录功能") → activated_principles non-empty
5. context_graph {query_type:"full_graph"} → has nodes from auto-injection
```

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "verify: all context/graph/principle tests pass"
```
