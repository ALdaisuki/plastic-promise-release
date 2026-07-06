# AGENTS.md — Plastic Promise 多 Agent 互操作协议

> 本文档面向所有接入 Plastic Promise MCP Server 的 Agent（Pi Builder/Fixer/Reviewer、子 Agent、外部 Agent）。
> Claude Code 操作指令见 **[CLAUDE.md](CLAUDE.md)**，架构与状态见 **[GOAL.md](docs/GOAL.md)**。

## 项目概述

Plastic Promise 是以「约定工程」替代「约束工程」的 AI 行为治理系统。通过共享 MCP Server 实现：

- **共享记忆**: 所有 Agent 读写同一记忆池（SQLite + LanceDB）
- **共享原则**: 12 条核心原则在所有 Agent 同时生效
- **上下文供应**: 任一方调用 `context_supply` 获取智能三层上下文包
- **审计同步**: 11 维审计结果所有 Agent 可见
- **自治流水线**: 标签驱动、零 Token Daemon、自动衔接

## MCP 工具目录 (56 暴露工具, 以源码 `plastic_promise/mcp/server.py` 为准)

> 计数包含 `session_init` / `sp_stage` 等兼容别名。下方按主工具面分组列出；兼容别名只用于客户端命名差异，不另列业务域。

### 记忆域 (9)
| 工具 | 用途 |
|------|------|
| `memory_recall` | 混合检索记忆（文本 + 图遍历双通道），返回三层上下文包 |
| `memory_store` | 存储记忆 → 自动经过质量管道（提取→去重→门控→衰减→双写） |
| `memory_update` | 更新已有记忆，可选重置 worth 计数器 |
| `memory_forget` | 软删除记忆（标记衰退，7天后 GC） |
| `memory_list` | 按条件列出记忆（类型/来源/时间范围/worth） |
| `memory_gc` | 触发垃圾回收（dry_run 预览 / 实际执行合并+清理） |
| `memory_correct` | 纠正记忆：编辑内容、标记为错误/废弃/已纠正 |
| `memory_reclassify` | 强制已有记忆重跑分类管线（tier/domain/category） |
| `memory_sync_files` | 同步文件系统 .md 记忆到 MCP 管道 |

### 原则域 (2)
| 工具 | 用途 |
|------|------|
| `principle_activate` | 根据任务类型激活相关原则，支持 domain_hint 限定域 |
| `principle_evaluate` | 反事实评估：「如果违反会怎样」预演 |

### 上下文域 (4)
| 工具 | 用途 |
|------|------|
| `context_supply` | **核心工具** — 调用 ContextEngine.supply()，返回三层结构化上下文包 |
| `context_inject` | 向 EntityGraph 注入原则关联边或注册新实体节点 |
| `context_graph` | 查询实体关联图谱（遍历/节点信息/边列表/激活原则） |
| `auto_context_inject` | 统一自动化上下文注入（SoulBridge/Pi Daemon/Claude Code 三路径） |

### 审计防线域 (3)
| 工具 | 用途 |
|------|------|
| `audit_run` | 执行七维审计（action=full/report），含时间范围过滤 |
| `audit_pre_check` | 实时合规检查：L0 硬边界 + L1 约束衰减 |
| `defense` | 防线管理：get/history/adjust/status — 信任分读写，支持持久化 |

### 自省演化域 (2)
| 工具 | 用途 |
|------|------|
| `scarf_reflect` | SCARF 五维自省（地位/确定性/自主/关联/公平），mode=standard/inertia |
| `feedback_apply` | 向记忆或上下文条目手动应用反馈（adopted/ignored/rejected） |

### 系统管理域 (4)
| 工具 | 用途 |
|------|------|
| `system` | 系统操作：stats/backup/migrate |
| `issue_create` | 创建 Issue，关联原则和依赖关系 |
| `issue_transition` | 推进 Issue 状态：open→in_progress→resolved→closed |
| `issue_list` | 列出 Issue，按状态和 owner 筛选 |

### 运行模式域 (1)
| 工具 | 用途 |
|------|------|
| `runtime_mode` | 查询或热更新当前 MCP 进程运行模式：light/normal/rust-normal/full/rust-full；更新后刷新 Rust health 与重型初始化状态 |

### 经验包域 (2)
| 工具 | 用途 |
|------|------|
| `pack_export` | 导出记忆为可分享 JSON 经验包（流式，按 tags/memory_ids 筛选） |
| `pack_import` | 导入经验包（strategy: skip/replace/merge） |

### 技能追踪域 (5)
| 工具 | 用途 |
|------|------|
| `skill_session_start` | 创建技能执行实例实体，激活关联原则 |
| `skill_session_complete` | 标记技能完成，处理标签转换和 worth 更新 |
| `skill_session_trace` | 追踪技能执行链（完整性检测/违反警告） |
| `skill_session_audit` | 事后间隙扫描：检测缺失 session 实体，支持自动补录 |
| `skill_auto_track` | Hook 自动追踪（PreToolUse/PostToolUse），零摩擦 |

### Phase 1 程序化技能 (3)
| 工具 | 用途 |
|------|------|
| `session-init` | 统一会话启动 — 原则激活+SCARF基线+域健康+信任分+GC预览+chain_state；`context_mode` 默认 light，仅返回轻量预览，任务上下文仍按需显式调用 `context_supply` |
| `smart-remember` | 智能记忆存储 — 自动去重（相似度≥0.85则更新）+ 完整质量管道 |
| `step-closure` | 六联闭环 — 原则对齐→SCARF→激素→信任→反思(执行者提供lesson/improvement/root_cause/optimization)→CEI，结构化记忆入池 |

### SuperPowers 流水线 (1)
| 工具 | 用途 |
|------|------|
| `sp-stage` | SuperPowers 16 阶段统一入口 — 覆盖 using-superpowers、normal-development、review/audit、bug-hunt、parallel dispatch、writing-skills。链校验自动拒绝跳步，hook 自动追踪 |

> **性能**: 热调用 0.2~0.4s，冷启动 ~3s。`context_supply` 已从 `session-init` / `sp-stage` 原子中移除；`session-init(context_mode="light")` 只做 1-2 条轻量记忆预览，`context_mode="full"` 才显式运行完整 `context_supply`，启动后仍按需显式调用。
> **并发隔离**: 重型 `memory_recall` / `context_supply` 调用支持 `stage_session_id`、`flow_line_id`、`request_id`。并行 SuperPowers 流程或子 Agent 派发时应传入这些 ID，服务端会派生 `request_scope_id` 用于缓存隔离、审计追踪，并在 `context_supply` 输出中显示。
> **链约束**: `SKILL_CHAIN_MAP` 定义前置/后继，跳步返回 `chain_violation` + 正确下一步提示。
> **追踪**: Claude Code hook 与 MCP `sp-stage` 统一进入 `skill_auto_track → skill_session_start/complete`。

### 域联邦域 (1)
| 工具 | 用途 |
|------|------|
| `domain` | 域联邦管理：stats/merge/unmerge/rename/rebuild |

### 委托调度域 (7)
| 工具 | 用途 |
|------|------|
| `task_enqueue` | 挂委托 — Daemon/Claude 发现需求，挂上委托板；支持委托人信任分验证 + C级审批队列 |
| `task_claim` | 揭榜 — 猎人认领委托（原子 UPDATE WHERE status='pending'），自动等级匹配检查 |
| `task_complete` | 交委托 — 完成回报，自动创建验收子委托给 Claude |
| `task_verify` | 长老验收 — Claude 确认委托完成，通过→信任分+0.02，打回→信任分-0.03+自动重派 |
| `task_inbox` | 查看委托板 — 显示可接委托 + 等级匹配度 + 我的活跃任务 |
| `task_heartbeat` | 心跳保活 — 每60s汇报存活，超时自动释放委托 + 惩罚 |
| `task_abandon` | 主动弃单 — 放弃委托，信任分-0.02，累计5次降级到D |

### Review 域 (1)
| 工具 | 用途 |
|------|------|
| `review_run` | 结构化代码审查：prepare/evaluate/apply/full 管线 |

### Market 域 (7)
| 工具 | 用途 |
|------|------|
| `market_list` | 列出可用插件包 |
| `market_install` | 安装插件包 |
| `market_upgrade` | 检查或升级插件包 |
| `market_remove` | 卸载插件包 |
| `market_enable` | 启用插件包 |
| `market_disable` | 禁用插件包 |
| `market_status` | 显示已安装插件状态 |

---

## 工作流约定

### 1. 每次任务开始
Codex 工具暴露约定：Codex 可能把 MCP 工具放在 deferred/dynamic metadata 中，初始显式工具列表未出现不代表 MCP 未连接。若 `session-init` / `sp-stage` / `runtime_mode` 等 Plastic Promise MCP 工具未展开，必须先调用 `tool_search` 查询 `Plastic Promise MCP session-init sp-stage defense memory_recall context_supply runtime_mode`；只有 `tool_search` 仍找不到、且配置/健康检查也不可用时，才明确说明 MCP 未加载或未连接并进入本地文件、shell、测试和显式上下文降级。不要因 MCP 缺失而卡死当前工作。

```
1. session-init(task_description="<任务描述>", context_mode="light")  → 获取 chain_state + 原则 + SCARF基线 + 信任分 + context_status
2. sp-stage(stage="brainstorming", task_description="<任务描述>")  → 进入 SuperPowers 流水线
3. 按 SKILL_CHAIN_MAP 顺序推进后续阶段
```

### 2. SuperPowers 阶段推进
```
sp-stage(stage="brainstorming", task_description="...")      → 阶段 1: 需求澄清
sp-stage(stage="using-git-worktrees", task_description="...") → 阶段 2: 创建 worktree（强制必经）
sp-stage(stage="writing-plans", task_description="...")       → 阶段 3: 任务拆解
sp-stage(stage="executing-plans", task_description="...")     → 阶段 4: 执行实施
sp-stage(stage="test-driven-development", task_description="...") → 阶段 5: TDD
sp-stage(stage="verification-before-completion", task_description="...") → 阶段 6: 验收
sp-stage(stage="finishing-a-development-branch", task_description="...") → 阶段 7: 合入
```
补充入口/分支阶段：
```
sp-stage(stage="using-superpowers", task_description="...")  → 元阶段: 技能启动与选择
sp-stage(stage="subagent-driven-development", task_description="...") → writing-plans 后的子 Agent 实施分支
sp-stage(stage="requesting-code-review", task_description="...") → Review 链入口
sp-stage(stage="receiving-code-review", task_description="...") → Review 反馈处理
sp-stage(stage="audit", task_description="...") → 高风险或显式要求的结构化审计
sp-stage(stage="systematic-debugging", task_description="...") → Debug 链入口
sp-stage(stage="dispatching-parallel-agents", task_description="...") → 并行派发辅助阶段
sp-stage(stage="writing-skills", task_description="...") → 技能编写/验证元阶段
```
跳步会被 `sp-stage` handler 自动拒绝并返回正确下一步。

### 3. 每次决策前
```
principle_activate(task_type="<类型>") → 检查对齐状态
必要时 principle_evaluate(principle_id, scenario) → 反事实评估
```

### 4. 重要操作后
```
memory_store(content="<做了什么+为什么>", memory_type="experience", source="<agent_name>")
```

### 5. 每步闭环（有实质产出时）

执行者必须提供反思四字段——不填模板、不委托 Agent：
```
step-closure(
  task_description="<本步操作>",
  git_commit="<关联 commit>",
  mode="full",
  lesson="<本次学到的具体经验>",
  improvement="<下次可以改进的具体做法>",
  root_cause="<如果存在问题，根本原因是什么>",
  optimization="<立即可执行的一个具体改进动作>",
)
```
轻量步骤（查询/阅读）：`mode="light"`

### 6. 写操作前检查信任分
```
defense(action="get") → 根据 tier 决定行为
```

---

## 信任-自由度矩阵

| 信任分 | 等级 | 写文件 | 删文件 | 发Issue | 分配任务 | 行为 |
|--------|------|--------|--------|---------|----------|------|
| 0.80+ | autonomous | 允许 | 允许 | 允许 | 允许 | 自主执行 |
| 0.60+ | standard | 允许 | 需确认 | 允许 | 不允许 | 正常执行 |
| 0.30+ | restricted | 需审批 | 不允许 | 不允许 | 不允许 | 每次写前确认 |
| 0.00+ | readonly | 不允许 | 不允许 | 不允许 | 不允许 | 只读，写操作直接拒绝 |

### 信任分调整规则

| 触发事件 | 幅度 |
|----------|------|
| 单步 SCARF ≥ 0.80（step-closure 自动） | +0.02 |
| 单步 SCARF < 0.40（step-closure 自动） | -0.02 |
| 用户明确表扬/通过验收 | +0.05 |
| 用户打回/指出错误 | -0.03 |
| 连续 5 步无失败 | +0.01 |

### 减分机制（已生效）

信任分通过 TrustStore 持久化到 `plastic_memory.db`，MCP 服务重启后不丢失。变更历史记录在 `trust_history` 表中。

| 触发条件 | 幅度 | 触发方式 |
|---------|------|---------|
| SCARF < 0.40（step-closure 自动） | -0.02 | SoulLoop.post_task 自动 |
| L0 防线违规（危险操作被拦截） | -0.05 | SoulEnforcer.pre_check 自动 |
| L1 信任临界（< 0.15 被封锁） | -0.02 | SoulEnforcer.pre_check 自动 |
| 时间衰减（24h 无活动） | -0.005/天 | TrustStore.get() 惰性触发，上限 -0.30 |

---

## 子 Agent 派发协议

派发任何子 Agent 前，**必须**注入上下文：

```
1. memory_recall(query="<任务关键词>", task_type="<类型>", max_results=5)
2. context_supply(task_description="<任务描述>", task_type="<类型>")
3. 将核心上下文、关联上下文、激活原则写入派发 prompt
```

**最低要求**: 至少包含激活的原则列表 + 2 条最相关的核心记忆。

---

## 外部 Agent 接入约定

> 适用于通过 MCP 协议接入 Plastic Promise 的独立 Agent。
> 核心关系：**Claude Code + Plastic Promise = 战略指挥中心，外部 Agent = 前线作战部队**。

### 标签命名空间

所有外部 Agent 使用统一的标签命名空间实现会话隔离和项目归属：

```
session:<agent>:<uuid>     → 会话级别隔离，启动时生成
project:<agent>:<name>     → 跨会话项目归属（可选）
source:<agent>             → 身份标识（已有字段）
```

**示例（外部 Agent 执行一条 building 任务）**：

```
domain:building           ← 行为域（现有 7 域体系）
source:agent              ← 身份标识（现有字段）
session:agent:a1b2c3      ← 会话隔离
project:agent:feature-x   ← 项目归属（可选）
```

### 通用启动流程

外部 Agent 使用现有 `session-init` 即可，无需专用技能：

```
1. session-init(task_description, context_mode="light")  → 获取原则 + 信任分 + chain_state + context_status；light 只作预览，任务上下文另行按需调用 `context_supply`
2. memory_recall / context_supply  → 按需获取针对性上下文
3. defense(action="get")           → 执行前检查信任分
4. 执行代码操作                     → 读写、终端、诊断
5. step-closure(mode="full", lesson="...", improvement="...", root_cause="...", optimization="...")  → 执行者回流经验+记忆池
```

### 边界定义

**Plastic Promise 独占（外部 Agent 不越界）**：
- 原则的创建、修改、删除
- 治理决策（任务分配、架构决策）
- 长期记忆的主动存储（`memory_store`、`smart-remember`）

**外部 Agent 独占（Plastic Promise 不越界）**：
- 代码文件的读写执行
- 终端命令执行
- IDE 诊断信息获取
- 用户直接交互（问答、澄清、确认）

**MCP 桥接（双向通信）**：
- 外部 Agent → Plastic Promise：`step-closure` 回流结果、`memory_recall` 查询上下文、`context_supply` 获取上下文包、`defense` 查询/调整信任分
- Plastic Promise → 外部 Agent：上下文供应、原则激活、信任分查询

### 设计原则

- **不建新域**：走现有 7 行为域体系，通过 `source` 字段区分 Agent 身份
- **不建专用技能**：现有 `session-init` 已覆盖通用启动流程
- **标签命名空间隔离**：`session:<agent>:<id>` 提供轻量会话隔离
- **零代码改动**：纯约定层，DomainManager、SkillEngine、Rust Core 均不动
- **预留扩展**：后续外部 Agent 直接复用此约定，`<agent>` 替换为对应名称即可

### 接入方式

| Agent | 接入方式 | 状态 |
|-------|----------|------|
| *(预留)* | MCP (SSE/stdio) | 待接入 |

---

## 标签状态机 (向后兼容，建议迁移到委托系统)

> **新项目请使用猎人公会委托系统**（下方 §猎人公会委托协议）。旧标签状态机保留 6 个月过渡期。

```
task:pending → task:accepted → task:active → task:done → task:review → task:reviewed
    ↑ Claude发布  ↑ Daemon认领   ↑ Pi执行    ↑ 完成   ↑ Reviewer审  ↑ Claude验收

task:rejected → Fixer认领 → task:accepted → 修复循环

超时: task:active>5min → pending | task:reviewed>10min → active
清理: task:accepted/reviewed>7天 → 移除标签
```

---

## 猎人公会委托协议（新·推荐）

> 全域调度中心：Daemon 发现 → 挂委托板 → SSE 推送 → 猎人揭榜 → 心跳保活 → 长老验收。

### Agent 猎人身份

| Agent | 角色 | 典型委托人 | 订阅类型 |
|-------|------|-----------|---------|
| `pi_fixer` | 修复猎人 | daemon | fix_*, gc_* |
| `pi_builder` | 建造猎人 | claude, daemon | build_*, refactor_* |
| `pi_reviewer` | 审查猎人 | daemon | review_*, investigate_* |
| `claude` | S级传奇猎人/长老 | system, daemon | audit_*, investigate_*, 全部S/A级 |

### 委托板协议

```
作为委托人 (Claude/Daemon):
  task_enqueue(task_type="fix_memory", title="...", to_agent="pi_fixer", priority=3)
  → SSE 推送通知 pi_fixer + 匹配订阅者

作为猎人 (Agent):
  1. task_inbox(agent_name, trust_score)           → 查看委托板
  2. task_claim(agent_name, task_id, trust_score)  → 揭榜（原子防重复）
  3. task_heartbeat(task_id, agent_name)           → 每60s保活
  4. task_complete(task_id, agent_name, result)    → 交委托

作为长老 (Claude):
  task_verify(task_id, verdict="accepted|rejected", comment="...")
  → accepted: 信任分+0.02 + SSE通知猎人
  → rejected: 信任分-0.03 + 自动重派子委托
```

### 委托生命周期（SQLite 真相源）

```
pending → claimed → executing → done → verified
              ↑ 揭榜(原子)  ↑ 心跳   ↑ 完成   ↑ 验收
              ✗ 超时→释放回pending (escalation_count++) → 超3次→Claude兜底
              ✗ task_abandon→释放+惩罚
              ✗ task_verify(rejected)→reassigned→自动子委托
```

### 发现→调度→验收闭环

```
Daemon 扫描器(5个) → 发现问题
  → task_enqueue(to_agent, priority, source_scan)
    → SSE task:new 推送
      → 猎人 task_inbox → task_claim → 执行 → task_complete
        → 自动创建验收子委托给 Claude
          → task_verify(accepted|rejected)
            → defense adjust ±信任分
            → SSE 通知猎人结果
```

---

## 12 条核心约定

| # | 原则 | 域 | 一句话 |
|---|------|------|--------|
| 1 | 奥卡姆剃刀 | all | 如无必要，勿增实体 |
| 2 | 全过程可查可透明 | all | 每步有 git 痕迹、可追溯审计日志 |
| 3 | 自我审计闭环 | reflecting | 根因→改良→教训→评分 |
| 4 | 上下文驱动决策 | designing | 无上下文不行动，不足时标注而非猜测 |
| 5 | 约定优于约束 | governing | 检验存在不等于有效 |
| 6 | 数据流驱动 | designing | 追踪真实数据流，非假设架构图 |
| 7 | 器官互保 | building | 每个子系统保护整个系统 |
| 8 | 工具即感官 | all | LLM 能力边界由工具链决定 |
| 9 | 信任驱动约束 | governing | 动态信任分调节自主权 |
| 10 | 自演化闭环 | reflecting | 评价驱动行为修正 |
| 11 | 原则遗传 | governing | 核心约定跨 Agent 代际传递 |
| 12 | 代码即文档 | building | 代码本身是最权威的文档 |

---

## 快速开始

```bash
# 推荐：一键启动 MCP Server + Maintenance Daemon + Watchdog
python scripts/init_and_start.py

# 显式指定启动运行模式
python scripts/init_and_start.py --mode rust-full

# 仅启动共享 MCP Server
python -m plastic_promise --sse 9020

# 单独启动维护守护进程
python daemons/maintenance_daemon.py

# 新任务建议使用 Hunter Guild 委托系统
task_enqueue(task_type="build_feature", title="...", to_agent="pi_builder")
```

## 架构

```
Claude Code / Pi Agent / 外部 Agent
        │
        ▼ MCP (stdio | SSE)
┌──────────────────────────────────────┐
│ Plastic Promise MCP Server (56工具)   │
│  ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐│
│  │记忆││原则││上下文││审计││技能││SP  ││  14 组
│  │ 9  ││ 2  ││ 4  ││ 3  ││ 5  ││ 1  ││
│  └────┘└────┘└────┘└────┘└────┘└────┘│
│  ┌────┐┌────┐┌────┐┌────┐┌────┐    │
│  │自省││管理││经验包││联邦││委托│    │
│  │ 2  ││ 4  ││ 2  ││ 1  ││ 7  │    │
│  └────┘└────┘└────┘└────┘└────┘    │
│  ┌────┐┌────┐┌────┐                │
│  │技能3││Review││Market│                │
│  │ 3  ││ 1  ││ 7  │                │
│  └────┘└────┘└────┘                │
│        共享 ContextEngine            │
│   ├ 实体图谱 EntityGraph             │
│   ├ 混合检索 (LanceDB向量 + BM25)    │
│   ├ Memory Worth 双计数器            │
│   └ RRF 融合 / 符号规则双通道        │
└──────────────────────────────────────┘
        ↓ SQLite + LanceDB
```
