# CLAUDE.md — Plastic Promise 操作指令

> 📋 完整架构、当前状态、路线图见 **[GOAL.md](docs/GOAL.md)**。
> 核心范式：**约定工程** — 内化约定替代外部约束。

## 会话启动

每次会话开始，依次执行：

0. **server up check** — `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health')"`
   - 不可用（报错）→ 启动:
     - 完整模式: `python scripts/init_and_start.py` (需要 Ollama 提供 embedding)
     - 降级模式: `python scripts/init_and_start.py --skip-ollama-check` (Ollama 不可用时使用 FallbackEmbedder)
   - 启动器同时拉起 MCP Server (:9020) + Maintenance Daemon + Watchdog 守护
   - 仍不可用 → 告警，本次会话使用文件系统降级（写入 `.md` 需加 `[[pending-sync]]` 标记）

1. `session-init(task_description="<当前任务>")` — **Phase 1 技能：一条调用替代原有 5 步**（原则激活 + SCARF 基线自省 + context_supply + memory_store 注入 + domain stats + system stats + defense + memory_gc preview）。报告 `data.principles`、`data.scarf_baseline`、`data.domain_health`、`data.system_stats`、`data.trust`、`data.gc_preview`。

> **重要**: 具体任务时重新调用 `context_supply(task_description, task_type, scope)` 获取针对性上下文。
> - 编码/实施 → `task_type="code_generation"`
> - 修复/调试 → `task_type="debugging"`
> - 设计/规划 → `task_type="architecture"`, `scope="designing"`
> - 审查/复盘 → `task_type="code_review"`, `scope="reflecting"`
> - 发布/合入 → `scope="governing"`
>
> `principle_activate` 使用 `domain_hint` 参数限定原则域: `building` | `fixing` | `designing` | `reflecting` | `governing` | `connecting` | `all`

## MCP 工具 (48 个, 11 域 + 1 SuperPowers)

| 域 | 工具 |
|------|------|
| Memory (10) | memory_recall, memory_store, memory_update, memory_forget, memory_stats, memory_list, memory_gc, memory_correct, memory_reclassify, memory_sync_files |
| Domain (1) | domain(action=stats\|merge\|unmerge\|rename\|rebuild) |
| Principles (4) | principle_activate(+domain_hint), principle_inherit, principle_diffuse, principle_evaluate |
| Context (5) | context_supply, context_inject, context_graph, context_ready, auto_context_inject |
| Audit (3) | audit_run(action=full\|report), audit_pre_check, defense(action=get\|history\|adjust\|status) |
| Reflection (2) | scarf_reflect(mode=standard\|inertia), feedback_apply |
| System (4) | system(action=stats\|backup\|migrate), issue_create, issue_transition, issue_list |
| Pack (3) | pack_export(streaming), pack_import(strategy), pack_recall(strict) |
| **Skill Track (5)** | **skill_session_start, skill_session_complete, skill_session_trace, skill_session_audit, skill_auto_track** |
| **Skills (3)** | **session-init, smart-remember, step-closure** |
| **Dispatch (7)** | **task_enqueue, task_claim, task_complete, task_verify, task_inbox, task_heartbeat, task_abandon** |
| **SuperPowers (1)** | **sp-stage(stage, task_description) — 12 阶段统一入口** |

## SuperPowers 流水线

`sp-stage` 是 SuperPowers 12 阶段的统一 MCP 入口。Claude Code 用 SuperPowers 插件 + `Skill` 工具，MCP 客户端用 `sp-stage` 工具，统一走同一 `skill_auto_track` 追踪管道。

### 工作流

```
session-init → chain_state 初始化
  ↓
brainstorming → exemplar-research → using-git-worktrees → writing-plans
                                                             ├→ executing-plans → TDD → verify → finish
                                                             └→ subagent-driven → TDD → request-review → receive-review → finish
```

辅助: `systematic-debugging`, `dispatching-parallel-agents`

### 阶段总览

| 阶段 | 描述 | 实现文件 |
|------|------|---------|
| brainstorming | 需求澄清 + 架构设计 + 方案生成 | brainstorming.py |
| exemplar-research | 搜索成熟实现 + 三问法分析 + 质量审核 | exemplar_research.py |
| using-git-worktrees | 创建隔离工作分支 | using_git_worktrees.py |
| writing-plans | 拆解任务为可执行步骤 | writing_plans.py |
| executing-plans | 逐步骤实现代码 | executing_plans.py |
| test-driven-development | 先写测试再写实现 | test_driven_development.py |
| subagent-driven-development | 派发子 Agent 并行执行 | subagent_driven_development.py |
| verification-before-completion | 变更验证与端到端测试 | verification_before_completion.py |
| requesting-code-review | 提交变更请求审查 | requesting_code_review.py |
| receiving-code-review | 处理审查反馈 | receiving_code_review.py |
| finishing-a-development-branch | 分支合并前最终验收 | finishing_a_development_branch.py |
| systematic-debugging | 结构化诊断与修复 | systematic_debugging.py |
| dispatching-parallel-agents | 并行派发多个子 Agent | dispatching_parallel_agents.py |

### 使用方式

```
# MCP 客户端 (sp-stage MCP 工具)
sp-stage(stage="brainstorming", task_description="澄清需求")
sp-stage(stage="exemplar-research", task_description="搜索成熟工程实现并分析")
sp-stage(stage="using-git-worktrees", task_description="创建分支")
sp-stage(stage="writing-plans", task_description="拆解任务")

# Claude Code (SuperPowers 插件)
/SuperPowers:brainstorming
/SuperPowers:exemplar-research
```

每次调用耗时 **0.2~0.4s**（冷启动 ~3s 加载 embedding 模型）。

### 链约束（跳步自动拒绝）

`SKILL_CHAIN_MAP` 定义前置/后继关系。`sp-stage` handler 在执行前校验 —— 跳步返回 `chain_violation` 并提示正确下一步。worktrees 是强制必经阶段。

```
# 跳步被拒示例
sp-stage(stage="executing-plans", ...)  ← 当前在 brainstorming
→ error: "chain_violation: requires ['sp-writing-plans'], but current='brainstorming'"
→ "Valid next: ['sp-using-git-worktrees']"
```

### 追踪管道

```
Claude: Skill() → PreToolUse hook → mcp_tool: skill_auto_track → skill_session_start
                                                          ↓
                  SkillEngine.exec: principle_activate + memory_store (无 context_supply)
                                                          ↓
                  PostToolUse hook → skill_session_complete → 更新 _current_stage

MCP:    sp-stage → skill_auto_track → skill_session_start/complete
```

> **注**: `context_supply` 已从 sp-stage 原子中移除 — 其 `engine.supply()` 三路检索 + Ollama rerank 耗时 5~60s，且结果已不返回给调用方。context 在 `session-init` 时注入一次。

## 记忆质量管道 (方向 A + B)

所有记忆写入自动经过 6 层质量保障：

```
memory_store(content)
  └─ store_urgent() → extract_memories() [Dir B: 6类提取 + L0/L1/L2 + LLM fallback]
       └─ raw → tagged → classified(tier) → embedded → migrate
            └─ check_duplicate() cos≥0.85 → 去重 (access_count↑, worth_success↑, last_accessed, effective_half_life↑)
            └─ QualityGate.score(tier) [Dir B: 4维×0.25 等权]:
                 ≥0.5 → 入库 | 0.3-0.5 → low_quality | <0.3 → 丢弃
            └─ RecMem.store() → decay_multiplier + effective_half_life 初始化 [Dir A+B]
            └─ LanceDB 双写

MemoryGC.collect() (~7天)
  └─ mark_decaying() → Weibull 批量衰减更新 [Dir A]
  └─ merge_similar() cos≥0.70 → composite_score 选择幸存者 [Dir A+B]
  └─ forget() → 清理 decayed + merged
```

### 记忆写入即检查

```python
# 每个 memory_store 自动触发:
#   1. smart_extractor 6类提取 (preference/fact/decision/entity/event/pattern)
#   2. 向量去重 (LanceDB ANN cos≥0.85 → 更新已有记录)
#   3. QualityGate 四维门控 (等权 0.25: 置信度+相关性+新鲜度+信息密度)
#   4. Weibull 衰减初始化 (decay_multiplier + effective_half_life)
#   5. LanceDB 向量双写
```

### 质量监控命令

```bash
# 查看记忆池质量分布
python -c "from plastic_promise.memory.soul_memory import RecMem; r=RecMem(); print(r.stats())"

# 触发 GC (dry run 预览合并候选)
memory_gc(dry_run=True)  # 查看 merge.candidates_found, merge.merged_pairs

# 真正执行合并
memory_gc(dry_run=False)
```

## 猎人公会委托系统（全域调度中心）

Daemon 是**全域创新调度中心**，发现记忆问题/任务异常/架构缺陷/代码坏味道 → 按类型路由到对应猎人（Agent）。

### 猎人等级（信任分视图，实时计算不存储）

| 信任分 | 等级 | 称号 | 可接委托优先级 |
|--------|------|------|--------------|
| ≥ 0.80 | S | 传奇猎人 ⭐ | 1-4（全部） |
| ≥ 0.65 | A | 资深猎人 🛡️ | 2-4 |
| ≥ 0.50 | B | 正式猎人 ⚔️ | 3-4 |
| ≥ 0.35 | C | 见习猎人 🔰 | 4 |
| < 0.35 | D | 降级猎人 ⛓️ | 不可接新委托 |

### 委托生命周期

```
task_enqueue → pending → task_claim → claimed → executing → task_complete → done → task_verify → verified
                 ↑ 委托人挂榜   ↑ 猎人揭榜(原子)  ↑ 心跳保活   ↑ 交委托(自动创建验收子委托)  ↑ 长老验收
                                                                                    ↓ rejected → reassigned → 自动重派子委托
心跳超时 → 释放回 pending（escalation_count++） → 超3次升级给 Claude（S级兜底）
```

### 委托类型

| 委托类型 | 描述 | 默认路由 | 优先级 |
|---------|------|---------|--------|
| `fix_memory` | 修复记忆质量问题 | pi_fixer / claude | B (priority=3) |
| `fix_*` | 通用修复委托 | pi_fixer / claude | B (priority=3) |
| `build_*` | 新功能构建 | pi_builder / claude | B (priority=3) |
| `refactor_*` | 代码重构 | pi_builder / claude | B (priority=3) |
| `review_*` | 代码审查 | pi_reviewer / claude | B (priority=3) |
| `investigate_*` | 问题调查 | claude | B (priority=3) |
| `gc_*` | 垃圾回收/清理 | pi_fixer / claude | B (priority=3) |
| `research_exemplar` | 研究工程典范实现 | claude | B (priority=3) |
| `verify_exemplar` | 审核典范分析质量 | claude | B (priority=3) |

### 7 个 MCP 工具

| 工具 | 公会比喻 | 用途 |
|------|---------|------|
| `task_enqueue` | 挂委托 | Daemon/Claude 发现问题，挂上委托板。支持委托人信任分验证 |
| `task_claim` | 揭榜 | 猎人认领委托（原子操作，先到先得），自动检查等级匹配 |
| `task_complete` | 交委托 | 猎人完成委托，自动创建验收子委托给 Claude |
| `task_verify` | 长老验收 | Claude 验收，通过→信任分+0.02，打回→信任分-0.03+自动重派 |
| `task_inbox` | 查看委托板 | 显示可接委托、等级匹配度、我的进行中任务 |
| `task_heartbeat` | 心跳保活 | 猎人每60s汇报存活，超时释放委托 |
| `task_abandon` | 主动弃单 | 放弃委托，信任分-0.02，累计5次降级到D |

### 5 个发现扫描器（Daemon 定时执行）

| 扫描器 | 检测维度 | 委托路由 |
|--------|---------|---------|
| `scan_architecture_smells` | 循环依赖/上帝模块/散弹修改 | claude / pi_builder |
| `scan_code_quality_trends` | 复发率/拒绝率/衰减速率 | pi_reviewer / claude / pi_fixer |
| `scan_cross_module_coupling` | 标签异常共现/桥接膨胀/隐式依赖 | claude / pi_builder |
| `scan_trust_anomalies` | 信任骤降/信任停滞 | claude / pi_reviewer |
| `scan_memory_decay` | 僵尸记忆/记忆涌入/分布失衡 | pi_fixer / claude / pi_builder |

### 失败惩罚

| 失败类型 | 触发 | 基础惩罚 | 升级阈值 | 升级动作 |
|---------|------|---------|---------|---------|
| timeout | 心跳超时 | -0.01 | 3次 | 信任审查委托 |
| rejected | 长老打回 | -0.03 | 同类型3次 | 禁接该类型7天 |
| abandoned | 主动弃单 | -0.02 | 5次 | 降级到D |
| overreach | 越级揭榜后失败 | -0.04 | 1次 | 锁定等级30天 |

### 启动 Daemon

```bash
python -m plastic_promise.mcp.server --sse 9020   # MCP Server（委托板 + SSE 推送）
python daemons/maintenance_daemon.py                # 全域调度守护进程（扫描 + 心跳 + 惩罚）
```

### 数据库

委托系统使用 4 张新表（全部在 `plastic_memory.db`）：
- `task_queue` — 委托板
- `task_subscriptions` — 猎人订阅（9条默认规则）
- `hunter_failure_log` — 失败记录
- `metric_history` — 扫描器指标历史

## 标签状态机

```
task:pending  → task:accepted → task:active → task:done → task:review → task:reviewed
                    ↑ Daemon认领    ↑ Pi执行      ↑ 完成   ↑ Reviewer审   ↑ Claude验收

超时恢复: task:active>5min → task:pending | task:reviewed>10min → task:active
清理: task:accepted/reviewed >7天 → 移除标签
```

## 信任-自由度矩阵（含猎人等级）

| 信任分 | 等级 | 猎人等级 | 写文件 | 发Issue | 分配任务 | 接委托优先级 |
|--------|------|---------|--------|---------|----------|------------|
| 0.80+ | autonomous | S 传奇 | ✅ | ✅ | ✅ | 1-4 全部 |
| 0.60+ | standard | A 资深 | ✅ | ✅ | ❌ | 2-4 |
| 0.50+ | standard | B 正式 | ✅ | ✅ | ❌ | 3-4 |
| 0.35+ | restricted | C 见习 | ⚠️审批 | ❌ | ❌ | 4 |
| 0.00+ | readonly | D 降级 | ❌ | ❌ | ❌ | 不可接 |

## 子 Agent 派发协议

派发任何子 Agent（Agent tool / SDD / Workflow）前，**必须**执行上下文注入：

```
1. memory_recall(query="<任务关键词>", task_type="code_generation", max_results=5)
2. context_supply(task_description="<任务描述>", task_type="code_generation")
3. 将结果中的 🔵核心上下文 + 🟡关联上下文 + 🧬激活原则 写入派发 prompt 的 "Context from Memory System" 段
```

**最低要求**: 至少包含激活的原则列表 + 2 条最相关的核心记忆。

**为什么**: 子 Agent 有独立上下文窗口，看不到当前会话的记忆和历史。不注入上下文 = 让 Agent 盲目编码。违反此约定会导致子 Agent 重复已修复的 bug、忽略已有设计决策。

## 每步闭环（自演化引擎）

**每次产生实质产出（git commit / 设计决策 / 修复完成 / 记忆写入）后，必须执行**：

```
step-closure(
  task_description="<本步做了什么>",
  git_commit="<关联的 commit hash>",
  mode="full",
  lesson="<本次学到的具体经验>",
  improvement="<下次可以改进的具体做法>",
  root_cause="<如果存在问题，根本原因是什么；状态良好则说明为什么好>",
  optimization="<立即可执行的一个具体改进动作>",
)
```

**反思四字段是执行者的核心责任**——只有做事的人知道真正发生了什么、学到了什么、根因是什么。不填模板、不委托 Agent、不留给 daemon。原则 13：反思是事后产物——猜出来的全是垃圾。

六联闭环内容：
1. **原则对齐检查** → PrincipleTracker 记录遵守情况
2. **SCARF 五维自省** → 地位/确定性/自主/关联/公平 评分
3. **激素更新** → 根据 SCARF 调整 dopamine/cortisol 等
4. **信任分联动** → SCARF ≥ 0.80 → boost(+0.02)，SCARF < 0.40 → decay(-0.02)
5. **反思记忆存储** → 执行者提供的四字段以结构化格式 `[经验]/[优化]/[根因]/[动作]` 走 smart-remember 管线入池
6. **CEI 复合执行指数** → 综合评分

**轻量模式**（纯查询/阅读/会话启动等无产出的步骤）：
```
step-closure(task_description="...", mode="light")
```
仅执行原则对齐 + 上下文注入，跳过 SCARF/激素/信任联动。

**为什么**: 没有闭环就没有自演化。每步完成后的反馈信号是信任分波动、记忆 worth 分化、SCARF 趋势的唯一数据源。不闭环 = 79 条记忆全部 L1、信任分永远 0.6、系统退化为被动档案库。

## 信任分驱动权限（奖惩机制）

**每次写操作前检查信任分**：

```
defense(action="get") → 根据 tier 决定行为:
```

| 信任分 | 等级 | 写文件 | 删文件 | 发 Issue | 分配任务 | 行为 |
|--------|------|--------|--------|----------|----------|------|
| 0.80+ | autonomous | ✅ | ✅ | ✅ | ✅ | 自主执行 |
| 0.60+ | standard | ✅ | ⚠️确认 | ✅ | ❌ | 正常执行 |
| 0.30+ | restricted | ⚠️审批 | ❌ | ❌ | ❌ | 每次写前向用户确认 |
| 0.00+ | readonly | ❌ | ❌ | ❌ | ❌ | 只读，写操作直接拒绝 |

**信任分调整规则**：

| 触发事件 | 操作 | 幅度 |
|----------|------|------|
| 单步 SCARF ≥ 0.80（step-closure 自动） | `defense(action="adjust", delta=+0.02)` | +0.02 |
| 单步 SCARF < 0.40（step-closure 自动） | `defense(action="adjust", delta=-0.02)` | -0.02 |
| 用户明确表扬/通过验收 | `defense(action="adjust", delta=+0.05)` | +0.05 |
| 用户打回/指出错误 | `defense(action="adjust", delta=-0.03)` | -0.03 |
| 连续 5 步无失败 | `defense(action="adjust", delta=+0.01)` | +0.01 |

## 减分机制（已生效）

信任分通过 TrustStore 持久化到 SQLite，支持以下减分触发器：

| 触发条件 | 幅度 | 触发方式 |
|---------|------|---------|
| SCARF < 0.40（step-closure 自动） | -0.02 | SoulLoop.post_task 自动 |
| L0 防线违规（危险操作被拦截） | -0.05 | SoulEnforcer.pre_check 自动 |
| L1 信任临界（< 0.15 被封锁） | -0.02 | SoulEnforcer.pre_check 自动 |
| 时间衰减（24h 无活动） | -0.005/天 | TrustStore.get() 惰性触发 |
| 用户打回/指出错误 | -0.03 | 手动 defense(action="adjust") |

**信任分持久化**：信任分现在存储在 `plastic_memory.db` 的 `trust_scores` 表中，MCP 服务重启后不丢失。变更历史记录在 `trust_history` 表中。

**为什么**: 信任分 0.6 从未波动意味着系统没有在"学习"——不区分好步骤和坏步骤。信任分是自演化的唯一量化指标，必须在每一步后更新。

## Git 治理规范 (Enterprise Git Governance)

本项目遵循 Plastic Promise Flow 企业级 Git 治理框架。发行版只保留高层原则，完整操作以本文件和 [SYSTEM_FULL_CHAIN.md](docs/SYSTEM_FULL_CHAIN.md) 为准。

### 分支策略

| 前缀 | 用途 | 映射委托类型 |
|------|------|-------------|
| `feat/` | 新功能 | `build_*` |
| `fix/` | Bug 修复 | `fix_memory` / `fix_*` |
| `refactor/` | 重构（不改行为） | `refactor_*` |
| `docs/` | 文档 | `docs_*` |
| `perf/` | 性能优化 | `perf_*` |
| `chore/` | 构建/CI/工具 | `chore_*` |
| `worktree/<agent>/` | Agent 工作隔离 | — |

- `Dev` 为唯一长期分支，始终可部署
- 分支名全小写，`-` 分隔
- Agent 分支由 Daemon 自动生成: `<type>/<task_id>-<slug>`
- 合并使用 **Squash Merge**，保持线性历史
- 分支超过 7 天未合并 → Daemon 通知 → 24h 后自动删除 → 委托设为 abandoned → 信任分 -0.02

### 提交规范

所有 commit 必须遵循 Conventional Commits:

```
<type>(<scope>): <subject>
```

| Type | 用途 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | Bug 修复 |
| `refactor:` | 重构 |
| `docs:` | 文档 |
| `perf:` | 性能 |
| `test:` | 测试 |
| `chore:` | 构建/CI/工具 |
| `revert:` | 回滚 |

- `scope` 可选，`subject` 英文小写开头，不加句号
- 每次提交应为逻辑完整的最小单元

### PR 流程

```
创建分支 → 开发 → 提交 → git push → 创建 PR
  → CI 自动运行 (P0: lint/test/security, P1: style/coverage)
  → Code Review (至少 1 人 approve)
  → Squash Merge → task_verify → 闭环
```

- PR 必须关联 Hunter Guild 委托 (task_id)
- CI P0 失败 → 阻止合并 → 自动生成 fix_ci 委托 (30分钟窗口)
- 审查评论分类: nit/design/blocking/praise，影响信任分

### 信任分全生命周期联动

| 事件 | 信任分变动 |
|------|-----------|
| 扫描器发现问题 | -0.01 ~ -0.03 (追溯责任人) |
| CI P0 失败 | -0.02 |
| CI P1 警告 | -0.005 |
| CI 全部通过 | +0.01 |
| PR 合并 | +0.02 |
| 审查打回 | -0.03 |
| 分支超时未合并 | -0.02 |

### PR 合并硬规则

**禁止在未经用户明确授权的情况下合并任何 PR。** 创建 PR 是安全的——合并必须等待用户用明确的文字指令确认（如 "merge it"、"合并"、"合入"）。`gh pr merge`、`git merge`、`git push origin Dev` 等合并操作在无用户明文确认前一律不得执行。这条规则高于所有其他约定。

## 系统架构整合 (2026-07-02)

### 服务启动 (One-Click Launcher)

```bash
python scripts/init_and_start.py
```

自动启动 MCP Server (:9020) + Maintenance Daemon，watchdog 守护崩溃自动恢复。

### 元审计扫描器 (scan_scheduler_health)

每 1200s 运行，6 维自审计 Hunter Guild 调度系统本身：

| 维度 | 检测内容 | red 触发自动 fix 委托 |
|------|---------|---------------------|
| Scanner SNR | 扫描器 reject 率 | `fix_scanner` → `fix/<scanner>-noise` 分支 |
| Agent timeout | Agent 超时聚合 | `fix_timeout` → `fix/<agent>-timeout` |
| Dispatch latency | 委托认领等待 | `fix_latency` |
| Priority balance | 优先级分布 | `fix_priority` |
| Verification | 验收吞吐量 | `fix_verification` |
| Trend comparison | vs 历史上次审计 | — |

### 整合流水线

```
scan_scheduler_health 发现问题
  → task_enqueue(type="fix_scanner", to="pi_fixer", priority=2)
  → Agent claim → git checkout -b fix/<scanner>-noise
  → 修复 → commit → push → PR
  → CI (P0: lint/test/security) → Code Review → Squash Merge
  → task_verify(accepted) → 信任分 +0.02
```

### 记忆衰减 (Weibull)

- L1: beta=1.5, hl=3d | L2: beta=1.2, hl=7d | L3: beta=0.7, hl=90d
- Daemon 审计周期自动调用 `RecMem.update_all_decay()`
- Embedder: Ollama mxbai-embed-large (0.7GB)，默认优先

## 关键约定

- **先查再问** — 决策前先 principle_activate + memory_recall
- **子Agent必带上下文** — 派发前必须 memory_recall + context_supply，结果写入派发 prompt
- **每步有 git** — 可追溯、可复现
- **每步有闭环** — 实质产出后必须 `step-closure`，不跳过
- **写前查信任** — 写操作前 `defense(action="get")`，低于阈值拒绝或确认
- **信任动态** — 信任分影响检索范围 (high=1.3x, critical=0.5x)
- **域联邦** — 同名域自动融合, 信号 ≤200字符不深入细节
- **宪法人人遵守** — 12条原则统一约束 Claude 和 Pi，无例外
- **快速失败** — 子系统不可用时优雅降级，不阻塞主流程
- **不重复造轮子** — 先查记忆, 再查网上, 没有再创新

## Skill 调用追踪

Skill 调用自动通过 hook (`PreToolUse/PostToolUse` → `mcp_tool: skill_auto_track`) 追踪，**无需手动调用** `skill_session_start/complete`。
会话上下文通过 `session-init` 注入（见上方会话启动）。子 Agent 派发时使用 `auto_context_inject` 或手动 `memory_recall + context_supply`。

## 开发分支完成前验收

finishing-a-development-branch 执行前，**必须**执行三重验收：

### 1. Skill 链完整性

```bash
skill_session_trace(session_scope="branch")
```

验收标准 (全部满足才能继续):
1. `chain_complete = true` — 所有 skill 形成完整闭环
2. `gaps` 为空 — 无 orphan_active
3. `chain_valid = true` — 调用链合法
4. 链首为 brainstorming / systematic-debugging / requesting-code-review 之一
5. 链尾为 finishing-a-development-branch 或 receiving-code-review

验收不通过时的修复:
- orphan_active → `skill_session_complete(entity_id, "abandoned: 分支完成时未闭环")`
- chain_broken → 检查是否应调用后续 skill
- chain_violation → 调用 `skill_session_audit` 评估

### 2. 记忆质量扫描

```bash
memory_gc(dry_run=True)
```

确认:
- `merge.candidates_found` — 无大量未合并的相似记忆
- `candidates_count` — 衰减记忆数量合理（非异常增长）

### 3. 经验包导出（跨 Agent 知识传递）

```bash
pack_export(name="<feature>-<date>", tags=["domain:<域>", "task:done"], author="claude")
```

确认导出成功后将包文件提交到 `experience_packs/` 目录。
