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
                                              │
                               ┌──────────────┼──────────────┐
                               │              │              │
                               ▼              ▼              │
                      subagent-driven   executing-plans      │
                      -development          │               │
                               │            │               │
                               ▼            ▼               │
                      test-driven-development               │
                               │                            │
                               ▼                            │
                      verification-before-completion        │
                               │                            │
                               ▼                            ▼
                      finishing-a-development-branch
                               ▲
                               │
               requesting-code-review ──→ receiving-code-review
```

**辅助阶段（随时可切入）**：

| 阶段 | 切入时机 | 切出 |
|------|---------|------|
| `systematic-debugging` | 任何阶段遇到 bug | → `test-driven-development` |
| `dispatching-parallel-agents` | 需要并行任务时 | 独立，不链入主线 |
| `writing-skills` | 需要创建新 Skill 时 | 独立 |

## 三、阶段定义

| # | 阶段 | 域 | 原子操作 | 谁做 | 输出物 |
|---|------|-----|---------|------|--------|
| 1 | `brainstorming` | designing | principle + context + memory | Claude / Trae | 需求澄清 |
| 2 | `using-git-worktrees` | building | principle + context + memory | Claude / Trae | 独立 worktree 分支 |
| 3 | `writing-plans` | designing | principle + context + memory | Claude / Trae / Pi-Planner | 原子任务列表 |
| 4a | `executing-plans` | building | principle + context + memory | Pi-Builder | 代码实施 |
| 4b | `subagent-driven-development` | building | principle + context + memory | Claude（派发子 Agent） | 并行执行结果 |
| 5 | `test-driven-development` | building | principle + context + memory | Pi-Builder | 测试+代码+重构 |
| 6 | `verification-before-completion` | reflecting | principle + context + memory | Claude / Trae | 三重验收报告 |
| 7 | `finishing-a-development-branch` | governing | + defense | Claude / Trae | 合入 + 信任分调整 |
| 8 | `requesting-code-review` | reflecting | + audit_run | Claude / Trae | 审查请求 |
| 9 | `receiving-code-review` | reflecting | + audit_run | Claude / Trae | 审查反馈处理 |

**两条主路径**：

- **单人路径**：`brainstorming → worktrees → plans → executing → TDD → verify → finish`
- **多人路径**：`brainstorming → worktrees → plans → subagent → TDD → request-review → receive-review → finish`

## 四、使用方式

### Claude Code（SuperPowers 插件）

```
/SuperPowers:brainstorming     # 自动触发 Skill 工具 + hook → skill_auto_track
/SuperPowers:writing-plans
/SuperPowers:executing-plans
```

### Trae（sp-stage MCP 工具）

```
sp-stage(stage="brainstorming", task_description="...")
sp-stage(stage="using-git-worktrees", task_description="...")
sp-stage(stage="writing-plans", task_description="...")
```

### 追踪验证

```
skill_session_trace(session_scope="full")  # 检查调用链完整性
```

## 五、Hook 追踪管道

```
Claude Code:  Skill("brainstorming") → PreToolUse hook → mcp_tool: skill_auto_track("start")
Trae:         sp-stage(stage="brainstorming") → PreToolUse hook → sp_hook.py → POST /api/skill-track
                                                          ↓
                                  MCP 服务端: handle_skill_auto_track → skill_session_start
                                                          ↓
                                  阶段执行 (principle_activate + context_supply + memory_store)
                                                          ↓
Claude Code:  PostToolUse hook → mcp_tool: skill_auto_track("complete")
Trae:         PostToolUse hook → sp_hook.py → POST /api/skill-track
                                                          ↓
                                  MCP 服务端: handle_skill_auto_track → skill_session_complete
```

## 六、多 Agent 标签状态机

| SuperPowers 阶段 | 标签 | 域 | Pi Mode |
|-----------------|------|-----|---------|
| brainstorming | `stage:brainstorming` | designing | - |
| writing-plans | `task:plan` | designing | `planner` |
| executing-plans | `task:active` | building | `builder` |
| test-driven-development | `task:active` | building | `builder` |
| subagent-driven-development | `task:active` | building | - |
| verification-before-completion | `task:verify` | reflecting | - |
| requesting-code-review | `task:review` | reflecting | `reviewer` |
| receiving-code-review | `task:reviewed` | reflecting | - |
| finishing-a-development-branch | `task:reviewed` | governing | - |

**状态流转**：`task:pending → task:accepted → task:active → task:done → task:review → task:reviewed`

## 七、Pi Mode 语义

| Mode | 输入查询 | 输出标签 | 域 |
|------|---------|---------|-----|
| `planner` | `tag:spec domain:designing` | `task:plan` | building |
| `builder` | `tag:plan domain:building` | `task:active` | building |
| `fixer` | `tag:rejected domain:fixing` | `task:fixed` | fixing |
| `reviewer` | `tag:active domain:building` | `task:review` | reflecting |

## 八、启动方式

```powershell
# MCP 服务器
python -m plastic_promise.mcp.server --sse 9020

# Planner + Builder + Reviewer 并行
.\pi_worker.ps1 -mode planner
.\pi_worker.ps1 -mode builder
.\pi_worker.ps1 -mode reviewer

# Fixer 按需
.\pi_worker.ps1 -mode fixer
```

## 九、示例：JWT 登录模块全流程

```
1. brainstorming          → sp-stage(stage="brainstorming", task="JWT 登录需求")
2. using-git-worktrees    → sp-stage(stage="using-git-worktrees", task="创建 feat/jwt 分支")
3. writing-plans          → memory_store("Task 1: auth/jwt.py\nTask 2: login()\nTask 3: tests", tags=["task:plan"])
4. executing-plans        → Pi-Builder 执行 → auth/jwt.py + tests
5. test-driven-development → TDD 循环
6. verification-before-completion → 三重验收
7. finishing-a-development-branch → defense(adjust, +0.02, target="pi_builder")
```