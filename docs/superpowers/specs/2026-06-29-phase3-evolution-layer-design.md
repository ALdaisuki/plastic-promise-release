# Phase 3 — 演化层设计

**日期**: 2026-06-29
**服务原则**: #10 自演化闭环, #9 信任驱动约束, #4 上下文驱动决策

## 组件 1：worth 反馈闭环

**方案**: 自动追踪 + 显式确认

### 自动追踪
- `memory_recall` 返回的每条记忆 `access_count++`
- `access_count >= 5` 且未被遗忘 → `worth_success += 1`

### 显式确认
- `memory_correct(mark_as="corrected")` → `worth_success++`, `record_adopted()`
- `memory_correct(mark_as="wrong")` → `worth_failure++`, `record_rejected()`
- 反馈后触发 `EvolveR.evolve_cycle()`

### 改动
- `context_engine.py`: `_text_retrieval` 中每条命中 `access_count++` 并写回
- `mcp/tools/memory.py`: `memory_correct` 成功后触发 EvolveR

## 组件 2：行为模式学习

**文件**: 新建 `plastic_promise/behavior.py`

### AgentBehaviorTracker
- `record(task_type, principles, memory_types, owner)` — post_task 调用
- `stats() -> dict` — {top_task_types, principle_heatmap, memory_type_distribution, session_count}
- `pattern() -> str` — 自然语言行为摘要

### 数据存储
- 内存 dict，不持久化（Phase 4 考虑）
- 挂载在 `engine._behavior_tracker`

## 组件 3：curiosity 闭环

**文件**: 增强 `plastic_promise/reflection/soul_curiosity.py`

### 新增
- `curiosity_act(suggestion_id, outcome)` — 记录探索结果
- `curiosity_stats() -> dict` — {explore_rate, total_explorations, adopted_rate}
- 自适应探索率：adopted_rate > 0.7 → explore_rate + 0.02; < 0.3 → explore_rate - 0.02; 限制 [0.05, 0.30]

## 组件 4：原则遵守历史

**文件**: 增强 `plastic_promise/core/principles.py`

### PrincipleTracker 增强
- `trends(limit=20) -> dict` — 每条原则的近期 vs 总体遵守率 + 趋势
- `weakest(n=3) -> list` — 遵守率最低的 N 条原则
- 趋势判定: recent_rate > total_rate + 0.1 → "↑上升"; recent_rate < total_rate - 0.1 → "↓下降"; else "→稳定"

## 验证

```python
# 组件 1: 检索后 access_count++
results = engine._text_retrieval("test")
for mid, _, _, _ in results:
    assert engine._memories[mid]["access_count"] > 0

# 组件 2: 行为记录
bt = AgentBehaviorTracker()
bt.record("code_generation", ["奥卡姆剃刀"], ["experience"], "claude")
s = bt.stats()
assert s["session_count"] == 1

# 组件 3: curiosity 闭环
curiosity_act("sug_1", "adopted")
s = curiosity_stats()
assert s["total_explorations"] == 1

# 组件 4: 原则趋势
pt.trends()  # -> 每条原则的 trend 字段
```
