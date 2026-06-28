# Sub-Project C (Batch 1): Core Memory Pipeline — soul_memory + soul_loop + soul_principles

> Date: 2026-06-28
> Status: draft
> Scope: 3 个核心模块的 method 实现，深度 C（核心方法完整实现，辅助方法保持骨架）

## 1. Goal

填充 `soul_memory.py`、`soul_loop.py`、`soul_principles.py` 的核心方法，使 `pre_task_v2() → post_task()` 完整链路可用。当前状态：533+222+270=1025 行骨架（签名+docstring+pass）。目标：核心方法填逻辑 → ~2000-2500 行。

## 2. Implementation Plan

### 2.1 soul_memory.py（核心方法）

**File:** `plastic_promise/memory/soul_memory.py`  
**当前:** 533 行骨架  
**目标:** ~800 行（核心方法+辅助骨架）

核心方法（实现真实逻辑）：

| Method | 逻辑 |
|--------|------|
| `RecMem.store()` | Rust `engine.storage.store()` + `embedder.embed()` + `engine.retriever.vector.insert()` |
| `RecMem.recall()` | `embedder.embed(query)` → `engine.supply(text, vec, type, scope)` → return ContextPack |
| `RecMem.update()` | `engine.storage.update(id, UpdateFields{...})`，可选 re-embed |
| `RecMem.forget()` | `engine.storage.delete(id)` |
| `RecMem.stats()` | `engine.storage.stats(scope)` → format as dict |
| `RecMem.list_records()` | `engine.storage.list(filter)` → List[MemoryRecord] |
| `RecMem.apply_feedback()` | `record.record_adopted/rejected()` → `engine.storage.update(worth)` |
| `MemoryWorthCalculator.calculate_worth()` | Wilson lower bound from domain layer |
| `MemoryWorthCalculator.update_counters()` | Increment success/failure per feedback_type |

辅助方法（保持骨架签名）：
`MemoryTierManager` 全部方法、`EvolveR` 全部方法、`MemoryGC` 全部方法、`MemoryRecord.to_dict/from_dict`

### 2.2 soul_principles.py（核心方法）

**File:** `plastic_promise/principles/soul_principles.py`  
**当前:** 270 行骨架  
**目标:** ~500 行

核心方法：

| Method | 逻辑 |
|--------|------|
| `PrincipleManager.activate()` | 查 CORE_PRINCIPLES 映射 + 关键词匹配 → 返回激活原则列表 |
| `PrincipleManager.inject_to_graph()` | `engine.graph.add_edge(task_node, principle_node, "activates", weight)` |
| `PrincipleManager.inherit()` | work/life → all: 过滤源域原则 → 应用 PRINCIPLE_INHERITANCE_DECAY |
| `PrincipleManager.diffuse()` | 查询原则当前域状态和传播路径 |
| `PrincipleManager.evaluate()` | 查原则 ID → 返回反事实后果文本 |

辅助方法（保持骨架）：
`get_all_principles()`, `get_by_domain()`

### 2.3 soul_loop.py（核心方法）

**File:** `plastic_promise/loop/soul_loop.py`  
**当前:** 222 行骨架  
**目标:** ~600 行

核心方法：

| Method | 逻辑 |
|--------|------|
| `SoulLoop.pre_task_v2()` | `embedder.embed(task)` → `engine.supply(text, vec, type, scope)` → 返回 ContextPack |
| `SoulLoop.post_task()` | 收集反馈 → `RecMem.apply_feedback()` → 记录 audit → 返回 dict |
| `SoulLoop.calculate_cei()` | 七维度加权平均 (0.20+0.15+0.15+0.15+0.10+0.10+0.15) → float |
| `pre_task_v2()` (module-level) | 委托 SoulLoop 单例 |
| `post_task()` (module-level) | 委托 SoulLoop 单例 |

辅助方法（保持骨架）：
`cei_tier` property

## 3. Integration Points

- `soul_memory.RecMem` 持有 `ContextEngine` 引用（Rust PyO3 对象）
- `soul_loop.SoulLoop` 持有 `RecMem` + `PrincipleManager` + `ContextEngine`
- 所有模块调用 `plastic_promise.embedder.get_embedder()` 获取嵌入
- 原则激活使用 `plastic_promise.core.constants.CORE_PRINCIPLES`

## 4. Acceptance Criteria

1. `RecMem.store("test", "experience")` → 返回 MemoryRecord，Rust SQLite 可查
2. `RecMem.recall("test query", "general")` → 返回 ContextPack with core/related/divergent
3. `RecMem.apply_feedback("mem_id", "adopted")` → worth_success 递增
4. `PrincipleManager.activate("code_generation")` → 返回 >=3 条原则
5. `PrincipleManager.inherit("work", "all")` → 返回带衰减权重的原则列表
6. `PrincipleManager.evaluate(4, "skip testing")` → 返回反事实后果
7. `SoulLoop.pre_task_v2("test task", "general")` → 返回 ContextPack（端到端）
8. `SoulLoop.calculate_cei()` → 返回 0.0-1.0 浮点数
9. 所有 Python 导入链不报错
10. Rust `cargo check` 零错误

## 5. Out of Scope

- MemoryTierManager promote/demote 逻辑（辅助骨架）
- EvolveR.evolve_cycle() 逻辑（辅助骨架）
- MemoryGC.collect() 逻辑（辅助骨架）
- MemoryRecord.to_dict/from_dict 序列化
- SCARF、Enforcer、Audit 等 P2 Batch 2/3 模块
