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

## MCP 工具目录 (58 暴露工具, 以源码 `plastic_promise/mcp/server.py` 为准)

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
| `defense` | 防线管理：get/history/adjust/status/evaluate_tool — 信任分读写与工具语义决策 |

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
| `skill_auto_track` | 外部客户端 Hook 兼容追踪；记录 scoped lifecycle entity，不推进官方工作流游标 |

### Phase 1 程序化技能 (3)
| 工具 | 用途 |
|------|------|
| `session-init` | 统一会话启动 — 原则激活+SCARF基线+域健康+信任分+GC预览+chain_state；`context_mode` 默认 light，仅返回轻量预览，任务上下文仍按需显式调用 `context_supply` |
| `smart-remember` | 智能记忆存储 — 自动去重（相似度≥0.85则更新）+ 完整质量管道 |
| `step-closure` | 六联闭环 — 原则对齐→SCARF→激素→信任→反思(执行者提供lesson/improvement/root_cause/optimization)→CEI，结构化记忆入池 |

### 官方工程工作流 (1)
| 工具 | 用途 |
|------|------|
| `sp-stage` | 保留名称的兼容入口；仅接受固定版本 Matt Pocock 工程技能，校验 route、调用权限和相邻阶段 |

> **性能**: 热调用 0.2~0.4s，冷启动 ~3s。`context_supply` 已从 `session-init` / `sp-stage` 原子中移除；`session-init(context_mode="light")` 只做 1-2 条轻量记忆预览，`context_mode="full"` 才显式运行完整 `context_supply`，启动后仍按需显式调用。
> **并发隔离**: 重型 `memory_recall` / `context_supply` 调用支持 `stage_session_id`、`flow_line_id`、`request_id`。并行官方流程或子 Agent 派发时应传入这些 ID，服务端会派生 `request_scope_id` 用于缓存隔离、审计追踪，并在 `context_supply` 输出中显示。
> **调试热路径**: `rust-full` 下 `memory_recall(debug=true)` 在 Rust 健康且优先时仍走 Rust snapshot 热路径，并返回 Rust `pipeline_stats` / `per_item_stats`；只有 Rust 不可用或异常时才回退 Python。
> **链约束**: `SKILL_CHAIN_MAP` 定义前置/后继，跳步返回 `chain_violation` + 正确下一步提示。
> **固定上游**: `mattpocock/skills@ed37663cc5fbef691ddfecd080dff42f7e7e350d`。旧 SuperPowers 阶段返回 `unknown_stage`。
> **调用策略**: user-only 技能只能提示或由用户明确调用；model 技能可由 Hook 推荐并通过 `sp-stage(invocation_source="model")` 进入。`invocation_source` 只是调用方声明，MCP 传输层不认证该字段；可信客户端必须先确认真实用户意图，服务端只校验声明与阶段策略是否一致。
> **执行声明**: `sp-stage` 首次调用只返回 `awaiting_receipt` 和固定 revision/hash 的执行合同，不运行官方 Codex Skill，也不推进游标。实际运行对应 Codex Skill 后，调用方必须再次提交无 secret 的 `execution_receipt`；receipt 是调用方声明，不是 Skill 已运行的密码学证明。服务端校验固定物料、JSON 边界和敏感值，再执行治理适配器并原子持久化 receipt + cursor。
> **项目隔离**: Hook 与客户端应同时传 `project_id`、`stage_session_id` 和 `flow_line_id`。必须把 Hook 注入的这组 ID 原样传给 `session-init` 和后续 `sp-stage`，不得为同一任务另建 scope。持久 workflow scope 使用项目摘要隔离；同名 session/flow 不会跨项目续接。
> **复合 Skill**: `/implement` 已内含其测试和审查工作，`/grill-me` 已内含其提问循环；外层 route 不再重复追加 `/tdd`、`/code-review` 或 `/grilling`。复合 receipt 必须在 `evidence.invoked_skills` 声明实际内部调用；服务端以 `composite_receipt` 证据建立确定性、entity-only 子链，不把它伪装成独立 Hook 观测。
> **分支切换**: 已持久化的父 route 只能在当前阶段与声明分支的相邻阶段对齐时切换到该分支（例如 `idea-to-ship/grill-with-docs -> small-build/implement` 或 `prototype-detour/handoff`）；任意 route 切换仍返回 `route_mismatch`。
> **有界注入**: canonical memory、临时 proposal 与路由文本的合并结果严格服从总字符预算。可以省略完整可选区块，但不得截断 XML-like 合同；默认预算下路由优先保留 project/session/flow scope 与可执行调用。
> **Codex Hook 边界**: 当前 Codex 自动 Hook 只有 `UserPromptSubmit`、`Stop`、`SessionEnd`。不要宣称 Codex 会通过 `PreToolUse/PostToolUse` 自动调用 `skill_auto_track`；该工具只保留给明确实现了此生命周期的外部客户端。

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

### Commercial Audit 域 (1)
| 工具 | 用途 |
|------|------|
| `commercial_audit_export` | 导出商业审计包：按 project_id/since/until 过滤 call_spans、degradation_events，可选包含 store_outbox |

### MGP Shadow 域 (1)
| 工具 | 用途 |
|------|------|
| `mgp_shadow_bridge` | MGP 兼容语义桥：off/shadow/inject 模式；P1 只做审计映射，不改写长期记忆 |

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
1. 使用 Hook 注入的 project_id/stage_session_id/flow_line_id 调用 session-init(..., context_mode="light")
   → 获取同一 scope 的 chain_state + 原则 + SCARF基线 + 信任分 + context_status
2. 读取 Hook 注入的 official flow、full chain、current/next stage 和 [user]/[model] 权限
3. 仅自动进入 [model] 阶段；[user] 阶段等待用户明确调用
```

### 2. 官方阶段推进
```
idea-to-ship:
  grill-with-docs [user] → to-spec [user] → to-tickets [user]
  → implement [user, composite]

small-build:
  grill-with-docs [user] → implement [user, composite]

prototype-detour:
  grill-with-docs [user] → handoff [user] → prototype [model]
  → handoff [user] → grill-with-docs [user]

bug-onramp:
  diagnosing-bugs [model] → tdd [model] → code-review [model]

research-feed:
  research [model] → grill-with-docs [user] → to-spec [user]

merge-conflict:
  resolving-merge-conflicts [model] → code-review [model]

standalone:
  grill-me [user, composite]
  to-spec [user] → to-tickets [user] → implement [user, composite]
  to-tickets [user] → implement [user, composite]
  implement [user, composite]
  tdd [model] → code-review [model]
  prototype [model]
  handoff [user]
  teach [user]
  writing-great-skills [user]
  domain-modeling [model]
  codebase-design [model]
```

显式 `/skill-name` 会为每个固定版本官方 Skill 选择以该 Skill 为首节点的合法 route；
自然语言 Skill 短语只有位于句首且表达正向命令时才算显式调用；疑问、否定、状态陈述和引用不生成 user attestation。普通 `code_generation`（以及从 `general` 文本解析出的正向实现命令）进入 `tdd-to-review`；`architecture` / `refactoring` 进入 `codebase-design`；故障、审查、研究、原型和冲突命令进入各自 model route。只读解释、状态句和没有正向后续动作的否定 `general` 输入保留在 `routing`；`Fix ... but do not refactor ...` 这类后置范围约束不会取消前面的正向任务。该任务分类不会把常用名词伪造成 user-only 或 model Skill 调用。
`implement` 和 `grill-me` 是上游复合 Skill，内部子流程不作为外层 receipt/cursor 的重复阶段。
本地路由器只承诺上述有界命令语法；歧义自然语言必须 fail closed 到 `routing`。`PP_PASSIVE_SEMANTIC_ROUTING=shadow|on` 可复用结构化切片 JSON Provider 做有界语义分类，但只能增强 model route，不得生成 user-only attestation；超时、无效 JSON、低置信度或 Provider 不可用时必须回退 `routing/ask-matt`。确定性命令永不调用 Provider。
父 route 只允许切换到其声明分支且必须与当前/下一阶段对齐；其他 route 切换会被拒绝。

调用示例：
```
sp-stage(
  stage="diagnosing-bugs",
  route="bug-onramp",
  invocation_source="model",
  task_description="..."
)
# 返回 execution_status="awaiting_receipt" 和 execution_contract

# 实际运行 /diagnosing-bugs 后，再提交同一 scope：
sp-stage(
  stage="diagnosing-bugs",
  route="bug-onramp",
  invocation_source="model",
  task_description="...",
  execution_receipt={
    "skill": "diagnosing-bugs",
    "upstream_revision": "<execution_contract.upstream_revision>",
    "content_sha256": "<execution_contract.content_sha256>",
    "status": "completed",
    "evidence": {"verification": "focused regression passed"}
  }
)
```
跳步返回 `chain_violation`；未知旧阶段返回 `unknown_stage`；未知 route 返回 `unknown_route`。同一 `(project_id, stage_session_id, flow_line_id, route, step)` 的相同 receipt 幂等返回 `already_completed`，不同 receipt 返回 `execution_receipt_conflict`。
同一 MCP 进程内，相同 `(project_id, stage_session_id, flow_line_id)` 的 `sp-stage` 调用会串行覆盖状态读取、治理适配器执行和 receipt/cursor 原子提交；不同 flow lane 不共享该锁。生产边界仍是“同一 SQLite 只运行一个 MCP 写入进程”。该 async lock 不是跨进程租约；进程在治理适配器结束、SQLite 提交前崩溃时，适配器可能重跑，因此适配器必须保持幂等或只读，不得宣称分布式 exactly-once。

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

## 治理综合记忆运维约定

治理综合记忆默认关闭并采用 fail-closed 语义。SQLite 保存记忆正文、生命周期、来源证据、审核记录和精确索引物料；LanceDB 仅是可重建派生索引。

| 开关 | 默认值 | 说明 |
|---|---|---|
| `PP_SYNTHESIS_ARTIFACTS` | `off` | `shadow` 只评估不落综合记忆，`on` 才允许创建受治理草稿。 |
| `PP_SYNTHESIS_RETRIEVAL` | `0` | `1` 也只允许审核证据完整且来源仍有效的 `verified` 综合记忆。 |
| `PP_MEMORY_PROPOSALS` | `off` | `shadow` 仅返回哈希诊断，`on` 将公开用户事实、偏好和决策送入审核队列。 |
| `PP_PASSIVE_SEMANTIC_CAPTURE` | `off` | `shadow` 只保存分类结果，`on` 才将规则未命中的用户原文异步批量转为 proposal。 |
| `PP_MEMORY_PROPOSAL_AUTO_ADOPT` | `off` | `shadow` 持久化 would-promote 结果，`on` 仅在评分、向量证据与既有治理门禁全部通过后原子晋升。 |
| `PP_MEMORY_INDEX_TEXT_POLICY` | `legacy` | `compact-v2` 是需要固定语料 live gate 验收的实验策略。 |

综合记忆状态机为 `draft -> verified -> stale|contested`；修复 stale/contested 会产生下一修订的 `draft`，必须重新审核。审核必须同时记录 `last_verified_at`、`verified_by_actor`、`verified_by_call_id`，缺失任何一项都不可检索。高影响任务只为最终进入三层上下文的综合记忆优先展开来源证据。

规则未命中的 Stop 事件只把原始用户文本写入 durable derived-work；Hook 不同步等待云模型。语义批次严格按 project、visibility、配置修订和 Provider identity 分区，可合并或拆分输出，但每条 evidence 必须逐字来自对应用户输入。启用 proposal automation 后，每个来源 turn 通过 `ProposalAutomation` 形成可重建评分证据；eligible score revision 生成幂等晋升任务，失败进入有界 retry/dead 状态，reconcile 补齐崩溃窗口遗漏的任务。

提案审核记录 actor、call ID、审核时间和稳定原因码。自动晋升仍复用唯一的 `evaluate_auto_promotion()` 策略权威，LanceDB 仅提供可重建向量证据，不能授予权限。`pending`、`rejected`、`expired` 提案不得进入普通记忆检索或 LanceDB。维护顺序固定为：memory lifecycle -> passive outbox replay -> semantic jobs -> promotion reconcile/process -> proposal expiry -> synthesis integrity -> memory/synthesis index replay -> audit；所有新增步骤在门禁关闭时保持旧行为。

deterministic benchmark 只验证指标和门禁，不可用于发布质量结论。可发布对比必须使用同一份隔离安装的版本化双语语料、同一真实非 fallback 模型与维度、相同 runtime/warmup/repeat 元数据、完整相同的 split 集，并通过 store -> recall -> context smoke。

回滚只关闭新路径，保留 SQLite 中的控制、来源、提案和审计记录：

```text
PP_SYNTHESIS_RETRIEVAL=0
PP_SYNTHESIS_ARTIFACTS=off
PP_MEMORY_PROPOSALS=off
PP_PASSIVE_SEMANTIC_ROUTING=off
PP_PASSIVE_SEMANTIC_CAPTURE=off
PP_MEMORY_PROPOSAL_AUTO_ADOPT=off
PP_MEMORY_INDEX_TEXT_POLICY=legacy
```

---

## 快速开始

```bash
# 推荐：一键启动 MCP Server + Maintenance Daemon + Watchdog
python scripts/init_and_start.py

# 显式指定启动运行模式
python scripts/init_and_start.py --mode rust-full

# 仅启动共享 MCP Server（Streamable HTTP /mcp）
python -m plastic_promise --streamable-http 9020

# 单独启动维护守护进程
python daemons/maintenance_daemon.py

# 新任务建议使用 Hunter Guild 委托系统
task_enqueue(task_type="build_feature", title="...", to_agent="pi_builder")
```

## 架构

```
Claude Code / Pi Agent / 外部 Agent
        │
        ▼ MCP (stdio | Streamable HTTP)
┌──────────────────────────────────────┐
│ Plastic Promise MCP Server (58工具)   │
│  ┌────┐┌────┐┌────┐┌────┐┌────┐┌────┐│
│  │记忆││原则││上下文││审计││技能││SP  ││  17 组
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
