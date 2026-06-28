# Agent Interop — AGENTS.md (Claude Code)

## 项目概述

Agent Interop 是 Pi Agent 与 Claude Code 的互通桥梁。
通过 Plastic Promise MCP Server 实现：

- **共享记忆**: Pi 和 Claude 读写同一记忆池
- **共享原则**: 11 条核心原则在两边同时生效
- **上下文供应**: 任一方调用 context_supply 获取智能上下文包
- **审计同步**: 七维度审计结果双方可见

## Plastic Promise MCP 工具

Claude Code 可以直接调用以下 Plastic Promise MCP 工具：

### 记忆域 (7 工具)
- `memory_recall` — 混合检索记忆，返回三层上下文包
- `memory_store` — 存储记忆到记忆池
- `memory_update` — 更新已有记忆
- `memory_forget` — 软删除记忆
- `memory_stats` — 记忆池统计信息
- `memory_list` — 按条件列出记忆
- `memory_gc` — 手动触发垃圾回收

### 原则域 (4 工具)
- `principle_activate` — 根据任务类型激活核心原则
- `principle_inherit` — 原则单向扩散
- `principle_diffuse` — 查询原则传播状态
- `principle_evaluate` — 反事实评估

### 上下文域 (3 工具)
- `context_supply` — 调用 ContextEngine 返回三层上下文包
- `context_inject` — 注入实体关联
- `context_graph` — 查询实体关联图谱

### 审计与防线 (5 工具)
- `audit_run` / `audit_pre_check` / `audit_report`
- `defense_trust` / `defense_status`

### 自省与演化 (3 工具)
- `scarf_reflect` / `inertia_check` / `feedback_apply`

### 管理域 (3 工具)
- `system_stats` / `system_backup` / `system_migrate`

## 工作流约定

1. **每次任务前**: 调用 `context_supply` 获取相关上下文
2. **每次决策前**: 调用 `principle_activate` 检查原则对齐
3. **重要操作后**: 调用 `memory_store` 记录经验
4. **每日/每会话结束**: 调用 `audit_run` 执行审计
