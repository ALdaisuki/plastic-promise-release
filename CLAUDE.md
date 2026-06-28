# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](GOAL.md)** —— 那是唯一真相源。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动（每次必须执行）

每次会话开始，依次执行：

1. `principle_activate(task_type="general")` — 查阅 12 条核心约定
2. `memory_recall(query="<当前任务关键词>")` — 查阅相关记忆
3. `fuzzy_status` — 检查模糊缓存区是否有积压（有则 `fuzzy_process`）
4. `memory_store(content="会话启动：<本会话目标任务>", memory_type="experience")` — **记录会话启动记忆**（可溯源）
5. 信任分检查：`defense_trust(action="get")` — 了解当前自主权级别

**会话收尾约定**（Agent 自主选择——不是强制门禁）：
- 如果本次有值得沉淀的经验 → `memory_store(content="<关键发现>", memory_type="reflection")`
- 如果想了解系统自演化状态 → `post_task("<摘要>", "<git_commit>")` 触发六联闭环
- 如果想检查约定健康度 → `audit_run` 七维审计
- 如果记忆池臃肿 → `memory_gc(dry_run=false)` 清理衰退记忆

这些不是必须完成的检查单。它们是工具——你用，系统就演化；不用，系统保持现状。约定，是比约束更深的力量。

## 可用 MCP 工具（32 个）

| 域 | 数量 | 工具 |
|------|------|------|
| Memory | 10 | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, fuzzy_status, fuzzy_process, memory_correct |
| Principles | 4 | principle_activate, principle_inherit, principle_diffuse, principle_evaluate |
| Context | 4 | context_supply, context_inject, context_graph, context_ready |
| Audit | 5 | audit_run, audit_pre_check, audit_report, defense_trust, defense_status |
| Reflection | 3 | scarf_reflect, inertia_check, feedback_apply |
| System | 6 | system_stats, system_backup, system_migrate, issue_create, issue_transition, issue_list |

## 标准工作流

```
决策前: principle_activate → 查阅约定（后果 + 建议）
        context_supply / memory_recall → 获取上下文

执行中: memory_store → 记录发现（owner 自动隔离）
        context_inject → 注册实体关联

完成后: post_task(description, git_commit) → 六联闭环
          → 约定对齐检查 → PrincipleTracker
          → SCARF 自省
          → 激素更新
          → 信任联动（遵守→boost，违反→decay）
          → 反思存储（lesson → reflection memory）
          → CEI 更新

定期:   fuzzy_process → 清空缓存区积压
        memory_gc → 清理衰退记忆
        audit_run → 七维审计检查
```

## 关键约定

- **原则是参考，不是门禁** — 查阅后自主决策，不强制拦截
- **上下文预备，不自动注入** — 预备好放在那，Agent 决定看不看
- **每步有 git commit** — 可追溯、可复现
- **先查再问** — 决策前先查原则和记忆，不凭空猜测
- **信任动态** — 信任分影响检索范围（high=1.3×, critical=0.5×），不是二元开关

## 多 Agent

- Claude Code: stdio 模式（`.mcp.json` 自动启动）
- Pi / N.E.K.O: SSE 模式
  ```bash
  set AGENT_OWNER=pi
  python -m plastic_promise.mcp.server --sse 9020
  ```
- 共享域：12 原则 + 实体图谱 + 审计引擎
- 独立域：记忆检索自动按 owner 隔离

## 当前阶段

Phase 1 完成（post_task 六联闭环）。
Phase 2 待做：SQLite 持久化 + Issue 生命周期 + 依赖关系管理。
