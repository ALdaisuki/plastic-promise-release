# SuperPowers Chain Scope — 并发 Agent 流程隔离热修

**日期**: 2026-07-03
**状态**: draft
**分支**: worktree-fix-sp-chain-scope

## 背景

SuperPowers `sp-stage` 当前使用进程级 `current_stage` 做运行前硬链约束。当多个 Agent 共用同一个 MCP server 时，A Agent 完成 `requesting-code-review` 后会把全局阶段推进到 review，B Agent 再开启新的 `systematic-debugging` 会被误判为非法跳步。

典型失败：

```text
current_stage = requesting-code-review
B calls sp-stage(stage="systematic-debugging")
-> chain_violation: valid next is receiving-code-review
```

## 根因

| 问题 | 位置 | 原因 |
|------|------|------|
| 跨 Agent 阻塞 | `plastic_promise/mcp/server.py` | `sp-stage` 校验读取全局 `get_current_stage()`，没有 Agent/task/chain 作用域 |
| 新 root 流程被误挡 | `SKILL_CHAIN_MAP` + `server.py` | `systematic-debugging` 的 `predecessors=[]` 表示可作为流程入口，但校验逻辑只看当前 stage 的 successors |
| 并发 parent 可能错接 | `plastic_promise/mcp/tools/skill_tracking.py` | `_current_skill` / `_parent_entity_id` 是模块级单例，start/complete 无 chain_id 隔离 |

## 设计目标

1. 立即解除不同 Agent 之间的 root 流程互相阻塞。
2. 保留非 root stage 的链约束，避免任意跳步。
3. 最小化改动范围，为后续 `chain_id` scoped state 保留演进路径。

## 非目标

本次热修不完整实现 `chain_id` 持久化状态，不修改 MCP 工具 schema，不重构 `skill_auto_track` 的全局状态。该部分作为后续正式方案。

## 方案

### A. Root stage bypass

定义：`SKILL_CHAIN_MAP[stage].predecessors == []` 的 stage 是 root stage，可以作为新流程入口。

当 `current_stage` 存在且目标 stage 不是当前 successor 时：

1. 查询目标 stage 的 chain 定义。
2. 如果目标 stage 是 root stage，允许执行，视为新链起点。
3. 如果目标 stage 不是 root stage，继续按当前 successor 规则硬阻断。

这样：

| 当前 stage | 目标 stage | 结果 | 原因 |
|------------|------------|------|------|
| requesting-code-review | systematic-debugging | allow | target 是 root stage |
| requesting-code-review | brainstorming | allow | target 是 root stage |
| requesting-code-review | test-driven-development | reject | target 不是 root stage |
| writing-plans | executing-plans | allow | target 是 successor |

### B. 测试覆盖

新增测试：

1. `test_sp_stage_allows_root_stage_to_start_new_chain`
   - mock `current_stage=requesting-code-review`
   - 调用 `sp-stage(systematic-debugging)`
   - 期望成功执行 `sp-systematic-debugging`

2. `test_sp_stage_still_rejects_invalid_non_root_transition`
   - mock `current_stage=requesting-code-review`
   - 调用 `sp-stage(test-driven-development)`
   - 期望返回 `chain_violation`

## 后续正式方案

引入 `chain_id`：

```python
@dataclass
class ChainState:
    current_stage: str | None
    parent_entity_id: str | None
    current_entity_id: str | None
    updated_at: str
```

将当前模块级状态：

```python
_current_stage
_current_skill
_parent_entity_id
_current_entity_id
```

升级为：

```python
_chain_states: dict[str, ChainState]
```

`sp-stage`、`skill_auto_track`、`skill_session_trace` 增加可选 `chain_id`，默认从 branch/task/agent/session 派生。最终实现每个 Agent/任务/分支独立链状态。

## 验收标准

- review 当前状态下可开启新的 systematic-debugging root 流程。
- review 当前状态下仍不能直接进入 test-driven-development。
- 原有合法 successor 逻辑不变。
- 新增测试通过。
