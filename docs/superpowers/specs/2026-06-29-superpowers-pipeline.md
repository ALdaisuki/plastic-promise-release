# SuperPowers Pipeline — 完整标准化流程

> 状态: 已确认 | 更新: 2026-07-01 | 对齐: [obra/superpowers](https://github.com/obra/superpowers)

## 一、核心设计

完整继承 SuperPowers 12 阶段流水线，通过 `sp-stage` MCP 工具统一入口。
Claude Code 使用 SuperPowers 插件 + `Skill` 工具，Trae 使用 `sp-stage` MCP 工具。
两者通过 hook 桥接走同一 `skill_auto_track` 追踪管道。

## 二、完整流水线

```
using-superpowers
       │
       ▼
brainstorming ──→ using-git-worktrees ──→ writing-plans
       ↑                                        │
       │  (chain enforcement: 跳过 worktrees     ├──────────────┐
       │   会被 sp-stage 拒绝)                   │              │
       │                                        ▼              ▼
       │                               subagent-driven   executing-plans
       │                               -development          │
       │                                        │            │
       │                                        ▼            ▼
       │                               test-driven-development
       │                                        │
       │                                        ▼
       │                               verification-before-completion
       │                                        │
       │                                        ▼
       └────────────────── finishing-a-development-branch
                                         ▲
                                         │
                   requesting-code-review ──→ receiving-code-review
```

**链约束**：`SKILL_CHAIN_MAP` 定义前置/后继关系。`sp-stage` 调用时自动校验 —— 跳步直接拒绝并返回正确下一步。

**辅助阶段（随时可切入，不参与主线链校验）**：

| 阶段 | 切入时机 | 切出 |
|------|---------|------|
| `systematic-debugging` | 任何阶段遇到 bug | → `test-driven-development` |
| `dispatching-parallel-agents` | 需要并行任务时 | 独立，不链入主线 |
| `writing-skills` | 需要创建新 Skill 时 | 独立 |

## 三、阶段定义

| # | 阶段 | 域 | 原子操作 | 谁做 | 输出物 |
|---|------|-----|---------|------|--------|
| 1 | `brainstorming` | designing | principle + memory | Claude / Trae | 需求澄清 |
| 2 | `using-git-worktrees` | building | principle + memory | Claude / Trae | 独立 worktree 分支 |
| 3 | `writing-plans` | designing | principle + memory | Claude / Trae / Pi-Planner | 原子任务列表 |
| 4a | `executing-plans` | building | principle + memory | Pi-Builder | 代码实施 |
| 4b | `subagent-driven-development` | building | principle + memory | Claude（派发子 Agent） | 并行执行结果 |
| 5 | `test-driven-development` | building | principle + memory | Pi-Builder | 测试+代码+重构 |
| 6 | `verification-before-completion` | reflecting | principle + memory | Claude / Trae | 三重验收报告 |
| 7 | `finishing-a-development-branch` | governing | principle + defense + memory | Claude / Trae | 合入 + 信任分调整 |
| 8 | `requesting-code-review` | reflecting | principle + audit + memory | Claude / Trae | 审查请求 |
| 9 | `receiving-code-review` | reflecting | principle + audit + memory | Claude / Trae | 审查反馈处理 |

> **注**：`context_supply` 已从 sp-stage 原子中移除。context 在 `session-init` 时注入一次，sp-stage 不再重复计算（原因为 Ollama rerank 耗时 5~60s，且结果已不返回给调用方）。

**两条主路径**：

- **单人路径**：`brainstorming → worktrees → plans → executing → TDD → verify → finish`
- **多人路径**：`brainstorming → worktrees → plans → subagent → TDD → request-review → receive-review → finish`

## 四、使用方式

### 新会话启动

```
session-init(task_description="<当前任务>")
  → 返回 chain_state: { current_stage: null, valid_next: ["brainstorming", ...] }
```

### 阶段推进（Trae sp-stage）

```
sp-stage(stage="brainstorming", task_description="...")
sp-stage(stage="using-git-worktrees", task_description="...")
sp-stage(stage="writing-plans", task_description="...")
```

每次调用耗时约 **0.2~0.4s**（冷启动首次 ~3s 加载 embedding 模型）。

### 链校验响应

成功:
```json
{"stage":"brainstorming","success":true,"data":{"stage":"brainstorming","domain":"designing","principles":[...],"memory_id":"...","transition":"→ brainstorming"}}
```

跳步被拒:
```json
{"stage":"executing-plans","success":false,"errors":["chain_violation: 'executing-plans' requires one of ['sp-using-git-worktrees','sp-writing-plans'], but current stage is 'brainstorming'. Valid next: ['sp-using-git-worktrees']"]}
```

### 追踪验证

```
skill_session_trace(session_scope="full")  # 检查调用链完整性
```

## 五、Hook 追踪管道

```
Trae:
  sp-stage(stage="brainstorming", ...)
  → PreToolUse hook → sp_hook.py → POST /api/skill-track {"phase":"start","skill_name":"brainstorming"}
  → MCP 服务端: handle_skill_auto_track → skill_session_start (创建追踪实体)
  → SkillEngine.exec: principle_activate + memory_store (无 context_supply)
  → PostToolUse hook → sp_hook.py → POST /api/skill-track {"phase":"complete",...}
  → MCP 服务端: handle_skill_auto_track → skill_session_complete

Claude Code:
  Skill("brainstorming", ...)
  → PreToolUse hook → mcp_tool: skill_auto_track(phase="start")
  → SuperPowers 插件执行 SKILL.md
  → PostToolUse hook → mcp_tool: skill_auto_track(phase="complete")
  → (同上 MCP 服务端处理)
```

两者走完全相同的 `skill_auto_track` → `skill_session_start/complete` 管道。

## 六、链约束设计

`SKILL_CHAIN_MAP` 定义在 [constants.py](../../plastic_promise/core/constants.py)，包含 14 概念层 + 12 Programmatic 层映射。

**强制必经**：`brainstorming → using-git-worktrees → writing-plans` 是线性链，worktrees 不可跳过。
**分支选择**：writing-plans 后可选 `executing-plans` 或 `subagent-driven-development`。

`sp-stage` handler (`server.py`) 在 SkillEngine 执行前校验：
1. 提取 `_current_stage`（内存变量，同会话内有效）
2. 查 `SKILL_CHAIN_MAP` 的 `predecessors`
3. 如果 `_current_stage` 不在 `predecessors` 列表中 → 返回 `chain_violation`
4. 通过后执行 atom，PostToolUse hook 更新 `_current_stage`

## 七、多 Agent 标签状态机

| SuperPowers 阶段 | 标签 | 域 | Pi Mode |
|-----------------|------|-----|---------|
| brainstorming | `stage:brainstorming` | designing | - |
| using-git-worktrees | `stage:worktrees` | building | - |
| writing-plans | `task:plan` | designing | `planner` |
| executing-plans | `task:active` | building | `builder` |
| test-driven-development | `task:active` | building | `builder` |
| subagent-driven-development | `task:active` | building | - |
| verification-before-completion | `task:verify` | reflecting | - |
| requesting-code-review | `task:review` | reflecting | `reviewer` |
| receiving-code-review | `task:reviewed` | reflecting | - |
| finishing-a-development-branch | `task:reviewed` | governing | - |

**状态流转**：`task:pending → task:accepted → task:active → task:done → task:review → task:reviewed`

## 八、Pi Mode 语义

| Mode | 输入查询 | 输出标签 | 域 |
|------|---------|---------|-----|
| `planner` | `tag:spec domain:designing` | `task:plan` | building |
| `builder` | `tag:plan domain:building` | `task:active` | building |
| `fixer` | `tag:rejected domain:fixing` | `task:fixed` | fixing |
| `reviewer` | `tag:active domain:building` | `task:review` | reflecting |

## 九、性能

| 组件 | 耗时 | 说明 |
|------|------|------|
| `sp-stage` 调用（热） | 0.2~0.4s | principle_activate(0.05s) + memory_store(0.1s) + hook round-trip(0.2s) |
| `sp-stage` 调用（冷） | ~3.2s | 首次加载 sentence-transformers embedding 模型 |
| `context_supply` | 5~60s | 三路检索 + Ollama rerank（已从 sp-stage 原子中移除） |

`context_supply` 在 `session-init` 时执行一次，sp-stage 不再重复计算。如需阶段性重新注入上下文，手动调用 `context_supply`。

## 十、启动方式

```powershell
# MCP 服务器
python -m plastic_promise.mcp.server --sse 9020

# Planner + Builder + Reviewer + Fixer
.\pi_worker.ps1 -mode planner
.\pi_worker.ps1 -mode builder
.\pi_worker.ps1 -mode reviewer
.\pi_worker.ps1 -mode fixer
```

## 十一、示例：JWT 登录模块全流程

```
1. session-init            → 上下文注入 + chain_state 初始化
2. brainstorming           → sp-stage(stage="brainstorming", task="JWT 登录需求")
3. using-git-worktrees     → sp-stage(stage="using-git-worktrees", task="创建 feat/jwt 分支")
4. writing-plans           → sp-stage(stage="writing-plans", task="拆解任务")
5. executing-plans         → Pi-Builder 执行 → auth/jwt.py + tests
6. test-driven-development → TDD 循环
7. verification-before-completion → 三重验收
8. finishing-a-development-branch → defense(adjust, +0.02, target="pi_builder")
```
