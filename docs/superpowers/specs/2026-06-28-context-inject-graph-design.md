# context_inject + context_graph 实现 & 自动注入 & 便捷函数补全

**日期**: 2026-06-28
**状态**: 已批准
**方案**: B — 走现有 PrincipleManager.inject_to_graph() 模式

---

## 一、背景与目标

### 现状
- `context_inject` 和 `context_graph` 两个 MCP 工具是 `pass` 空壳，返回 None
- `context_supply` 的 `_graph_traversal` 阶段没有数据可遍历（图从未被填充）
- `soul_principles.py` 中 6 个模块级便捷函数是 `pass` 空壳
- `CORE_PRINCIPLES` 只有 4/11 条原则

### 目标
1. 实现 `context_inject` → 注册实体节点和图边
2. 实现 `context_graph` → 查询节点、边、多跳遍历
3. `context_supply` 自动注入激活的原则到图谱
4. 补全 6 个原则便捷函数
5. 补全 CORE_PRINCIPLES 第 5-11 条（数据补全 — C 优先级）

---

## 二、受影响文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/core/context_engine.py` | +2 公开方法: `register_entity()`, `query_graph()` |
| `plastic_promise/mcp/tools/context.py` | 替换 2 个 `pass` 为真实实现 |
| `plastic_promise/principles/soul_principles.py` | 补 6 个模块级便捷函数 |
| `plastic_promise/core/constants.py` | 补原则 5-11 + 审计维度 4-7（C 优先级，最后执行） |

---

## 三、context_inject — 实体注入

### 输入参数
```json
{
  "entity_type": "principle | task | memory | code_module",
  "entity_id": "string",
  "entity_name": "string",
  "entity_description": "string (optional)",
  "related_entities": ["entity_id", ...]
}
```

### 处理流程
```
MCP → handle_context_inject(engine, args)
        │
        ├─ entity_type = "principle"
        │     └─ PrincipleManager(engine).inject_single_principle(...)
        │           └─ engine._graph_nodes[f"principle:{id}"] = {...}
        │           └─ engine._graph_edges.append({from, to, relation, weight: 0.7})
        │
        ├─ entity_type = "task" | "memory" | "code_module"
        │     └─ engine.register_entity(type, id, name, desc, related)
        │           └─ 创建类型化节点 + 与 related_entities 的 supports/activates 边
        │
        └─ 实体已存在 → 合并更新节点属性，不重复建边
```

### 返回格式
```json
{
  "injected": {
    "node_id": "principle:4",
    "type": "principle",
    "name": "上下文驱动决策",
    "edges_created": 2
  }
}
```

---

## 四、context_graph — 图谱查询

### 输入参数
```json
{
  "query_type": "node_info | traverse | full_graph | neighbors",
  "start_node": "string (optional)",
  "max_hops": 3
}
```

### 四种查询模式

| 模式 | 行为 | 需要 start_node |
|------|------|:---:|
| `node_info` | 返回单个节点属性 + 所有关联边 | 是 |
| `traverse` | BFS 多跳遍历，返回路径 + 节点列表 | 是 |
| `full_graph` | 返回全部节点 + 全部边 | 否 |
| `neighbors` | 返回 1-hop 邻居节点 + 连接边 | 是 |

### 内部实现
在 `ContextEngine` 上新增 `query_graph(query_type, start_node, max_hops=3)` 公开方法：
- 读 `_graph_nodes` dict → 构建节点信息
- 读 `_graph_edges` list → 按 from/to 过滤
- BFS 遍历 → 从 start_node 出发，按边方向逐层扩展

---

## 五、自动注入 — context_supply 链路

在 `ContextEngine.supply()` 的阶段 1（原则激活）之后插入图谱注入：

```python
# supply() 内部，第 ~375 行
activated = self._activate_principles(task_type, task_description)
self._inject_activated_to_graph(activated, task_type)  # 新增一行
```

`_inject_activated_to_graph` 复用 `PrincipleManager.inject_to_graph()` 的已有逻辑：
- 为每条激活的原则创建/更新 `principle:{id}` 节点
- 创建 `task_type:{type}` → `principle:{id}` 的 `activates` 边

效果：每次 `context_supply` 调用自动填充图谱，`_graph_traversal` 阶段有数据可遍历。

---

## 六、soul_principles 便捷函数

6 个模块级 `pass` 函数委托到已有 `PrincipleManager` 实例方法：

```python
def principle_activate(task_type, task_description="", max_principles=5):
    return PrincipleManager().activate(task_type, task_description, max_principles)

def principle_inherit(source_domain, target_domain="all", principle_ids=None):
    return PrincipleManager().inherit(source_domain, target_domain, principle_ids)

def principle_diffuse(principle_id=None):
    return PrincipleManager().diffuse(principle_id)

def principle_evaluate(principle_id, scenario):
    return PrincipleManager().evaluate(principle_id, scenario)

def get_all_principles():
    return PrincipleManager().get_all_principles()

def get_by_domain(domain):
    return PrincipleManager().get_by_domain(domain)
```

`get_all_principles` 和 `get_by_domain` 需要先在 `PrincipleManager` 中实现（当前也是 pass）。

---

## 七、错误处理

| 场景 | 行为 |
|------|------|
| entity_type 非法 | `{"error": "Unknown entity_type 'X'. Valid: principle, task, memory, code_module"}` |
| entity_id 为空 | `{"error": "entity_id is required"}` |
| query_type 非法 | `{"error": "Unknown query_type 'X'. Valid: node_info, traverse, full_graph, neighbors"}` |
| start_node 不存在 | `{"error": "Node 'X' not found"}` |
| 空图 | 正常返回 `{"nodes": {}, "edges": []}` |
| max_hops 越界 | 自动裁剪到 [1, 10] |
| 图写入异常 | 捕获返回 `{"error": "..."}`, 不抛异常 |

---

## 八、实现顺序

### 阶段 1：A+B — context 链路 + 核心工作流
1. `ContextEngine.register_entity()` — 公开实体注册方法
2. `ContextEngine.query_graph()` — 公开图查询方法
3. `handle_context_inject` — 替换 pass
4. `handle_context_graph` — 替换 pass
5. `supply()` 自动注入 — 加一行调用
6. `PrincipleManager.get_all_principles()` + `get_by_domain()` — 替换 pass
7. 6 个模块级便捷函数 — 委托到 PrincipleManager

### 阶段 2：C — 数据补全（最后）
8. `CORE_PRINCIPLES` 补全 5-11 条
9. `AUDIT_DIMENSIONS` 补全 4-7 维度

---

## 九、验证清单

- [ ] 重启 MCP Server → `principle_diffuse` 返回 4 条原则
- [ ] `context_inject` 注入 task 实体 → `context_graph node_info` 查回
- [ ] `context_inject` 注入 principle 实体 → `context_graph traverse` 确认边
- [ ] `context_supply("实现一个函数")` → `activated_principles` 非空
- [ ] `context_supply` 后 → `context_graph full_graph` 有新节点
- [ ] `principle_evaluate(4, ...)` → 返回 `violation_consequence`
- [ ] 6 个便捷函数逐个调用 → 全部返回非空
- [ ] 非法参数 → 返回明确错误，不崩溃
