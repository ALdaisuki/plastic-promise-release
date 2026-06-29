# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](GOAL.md)** —— 那是唯一真相源。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动（每次必须执行）

每次会话开始，依次执行：

1. `principle_activate(task_type="general")` — 查阅 12 条核心约定
2. `memory_recall(query="<当前任务关键词>", domain_hint=None)` — 查阅相关记忆（可指定域过滤）
3. `memory_stats` — 检查记忆池健康度 + 流水线状态
4. `memory_store(content="会话启动：<本会话目标任务>", memory_type="experience")` — **记录会话启动记忆**（可溯源）
5. `defense(action="get")` — 了解当前信任分 + 防线状态

**会话收尾约定**（Agent 自主选择——不是强制门禁）：
- 如果本次有值得沉淀的经验 → `memory_store(content="<关键发现>", memory_type="reflection")`
- 如果想了解系统自演化状态 → `post_task("<摘要>", "<git_commit>")` 触发六联闭环
- 如果想检查约定健康度 → `audit_run` 七维审计
- 如果想检查域健康度 → `domain(action="stats")` 全域统计
- 如果记忆池臃肿 → `memory_gc(dry_run=false)` 清理衰退记忆

这些不是必须完成的检查单。它们是工具——你用，系统就演化；不用，系统保持现状。约定，是比约束更深的力量。

## 可用 MCP 工具（29 个，8 域）

| 域 | 数量 | 工具 |
|------|------|------|
| Memory | 10 | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, memory_correct, fuzzy_status, fuzzy_process |
| Domain | 1 | domain(action=stats\|merge\|unmerge\|rename\|rebuild) |
| Principles | 4 | principle_activate(+domain_hint), principle_inherit, principle_diffuse, principle_evaluate |
| Context | 4 | context_supply, context_inject, context_graph, context_ready |
| Audit | 4 | audit_run(action=full\|report), audit_pre_check, defense(action=get\|history\|adjust\|status) |
| Reflection | 2 | scarf_reflect(mode=standard\|inertia), feedback_apply |
| System | 4 | system_stats, issue_create, issue_transition, issue_list, system(action=backup\|migrate) |
| Pack | 3 | pack_export(streaming), pack_import(strategy), pack_recall(strict) |

> 工具从 39 个合并精简至 29 个（韧性格局）。fuzzy_status/fuzzy_process 已内嵌于 memory_stats/memory_store。
> domain 统一入口替代 4 个独立 domain_* 工具。defense 统一入口替代 defense_trust + defense_status。
> scarf_reflect(mode="inertia") 替代 inertia_check。audit_run(action="report") 替代 audit_report。

## 域联邦快速参考

```
6 行为域 + 1 通用原则域:
  building / fixing / designing / reflecting / governing / connecting / all

检索加权: 同域记忆 ×1.3, 联邦(共享标签) ×1.1
高置信阈值: (|C|≥5 AND 命中率≥50%) OR |C|≥20

灾难恢复: domain(action="rebuild") — 从所有记忆 tags 全量逆向重建
域衰减: audit_run 触发, 7天无活动 → score×0.8, <0.1 → 萎缩合并
```

## 标准工作流

```
决策前: principle_activate → 查阅约定（后果 + 建议）
        context_supply / memory_recall(domain_hint=...) → 域过滤上下文

执行中: memory_store → 记录发现（owner 隔离 + 自动域分配）
        context_inject → 注册实体关联

完成后: post_task(description, git_commit) → 六联闭环
          → 约定对齐检查 → PrincipleTracker
          → SCARF 自省
          → 激素更新
          → 信任联动（遵守→boost，违反→decay）
          → 反思存储（lesson → reflection memory）
          → CEI 更新
          → 域衰减检测（domain_decay 钩子）

定期:   memory_gc → 清理衰退记忆
        audit_run → 七维审计 + 域重叠度检测
        domain(action="stats") → 域健康度
```

## 关键约定

- **原则是参考，不是门禁** — 查阅后自主决策，不强制拦截
- **上下文预备，不自动注入** — 预备好放在那，Agent 决定看不看
- **每步有 git commit** — 可追溯、可复现
- **先查再问** — 决策前先查原则和记忆，不凭空猜测
- **信任动态** — 信任分影响检索范围（high=1.3×, critical=0.5×），不是二元开关
- **域联邦** — 同名域（如 building 在原则和记忆中）自动融合，信号 ≤200 字符不深入细节
- **快速失败** — DomainManager 不可用时降级为全量检索 + uncategorized，Agent 主任务不受影响

## 多 Agent

- Claude Code: stdio 模式（`.mcp.json` 自动启动）
- Pi / N.E.K.O: SSE 模式
  ```bash
  set AGENT_OWNER=pi
  python -m plastic_promise.mcp.server --sse 9020
  ```
- 共享域：12 原则 + 实体图谱 + 审计引擎 + 域联邦图谱
- 独立域：记忆检索自动按 owner 隔离

## 当前阶段

四阶段路线图 + 域联邦 + 韧性专项 全部交付 ✅

| 阶段 | 交付 |
|------|------|
| Phase 1-4 | 全栈基础设施 + 六联闭环 + 演化层 |
| 域联邦 | 6 行为域 + 1 通用原则域 + 自演化三层闭环 + 4 个 MCP 工具 |
| 韧性专项 | 灾难恢复(rebuild_from_memories) + 跨版本兼容(schema_version 迁移链) + 静默失效防护(_dm_ok 降级) |
| 工具精简 | 35 → 39 → 29 个 MCP 工具 |
