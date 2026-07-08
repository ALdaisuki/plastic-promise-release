# Plastic Promise 中文指南

> 本文件是面向发行版用户的中文快速指南。英文默认入口见 [../README.md](../README.md)，更完整的项目目标与状态见 [GOAL.md](GOAL.md)。

## 这是什么

Plastic Promise 是一个本地优先的 MCP Agent 记忆、上下文、审计与任务调度系统。它把约定工程、记忆生命周期、信任分、自审计、任务调度和技能工作流组合成一个 Agent 治理底座。

它适合：

- Claude Code 或其他 MCP 客户端需要共享长期记忆时。
- 多 Agent 团队需要可追踪的任务派发、验收和信任分时。
- 项目希望把“先查上下文、再行动、后闭环”的工作方式固化为运行时工具时。

## 适用对象

Plastic Promise 面向需要长期上下文、明确治理规则和可审计任务交接的开发者与 Agent 团队。它不是单纯的记忆库，而是把记忆、原则、上下文、审计、防线、信任分和任务调度组合成一个本地优先的运行时。

| 需求 | Plastic Promise 的回答 |
|---|---|
| Agent 跨会话遗忘决策 | 用 worth、衰减、去重和图谱关联管理长期记忆。 |
| 上下文检索不稳定 | 用 `context_supply` 生成核心、关联、发散三层上下文包。 |
| 自动化需要防线 | 在共享状态变更前执行原则、审计、信任和防线检查。 |
| 多 Agent 工作难验收 | 通过 Hunter Guild 的认领、心跳、完成、验收状态机追踪任务。 |
| 工作流只停留在提示词里 | 将启动、记忆、闭环、审查和 SuperPowers 阶段暴露为 MCP 工具。 |

## 快速开始

### 安装

```bash
pip install plastic-promise
```

源码安装：

```bash
git clone https://github.com/ALdaisuki/plastic-promise-release.git
cd plastic-promise-release
pip install -e ".[dev]"
```

可选 Rust 加速器：

```bash
cd rust/context-engine-core
pip install maturin
maturin develop --release
```

### 启动

```bash
# 一键启动：MCP Server (:9020) + Maintenance Daemon + Watchdog
python scripts/init_and_start.py

# 自动化/后台启动时可显式指定运行模式
python scripts/init_and_start.py --mode rust-full

# Ollama 不可用时，使用 fallback embedder 降级模式
python scripts/init_and_start.py --skip-ollama-check
```

交互式终端未传 `--mode` 时，启动器会先询问启动模式；非交互启动默认使用 `rust-full`，保持 Rust 优先和完整 LanceDB 预热/维护路径。

| 模式 | Rust 加速 | 启动 LanceDB 预热 | 适用场景 |
|---|---:|---:|---|
| `light` | 否 | 否 | 最快启动；延迟 LanceDB，使用 Python 路径。 |
| `normal` | 否 | 否 | Python 路径，后续需要时再懒初始化 LanceDB。 |
| `rust-normal` | 是 | 否 | Rust 优先的上下文供给，不做启动重建。 |
| `full` | 否 | 是 | Python 路径，并在启动时执行 LanceDB init/backfill/rebuild。 |
| `rust-full` | 是 | 是 | Rust 优先，并执行完整 LanceDB 启动维护。 |

对 `full` 和 `rust-full` 而言，backfill/rebuild 属于启动器的启动预热工作。MCP 进程启动后，请求期 heavy init 只打开 LanceDB/domain 后端，并应保持 `LDB_BACKFILL_ON_INIT=0`、`LDB_REBUILD_ON_INIT=0`，避免普通 `context_supply` 或 debug recall 在热请求路径里重复跑维护。

启动后可通过 MCP 工具热更新当前进程模式：

```text
runtime_mode(action="get")
runtime_mode(action="set", mode="rust-normal")
```

启动器会将项目根目录放在子进程 `PYTHONPATH` 最前面，因此 Maintenance Daemon 等脚本式服务会导入当前源码树。Daemon 脚本在直接启动时也会自举项目根路径。

仅启动 MCP Server：

```bash
# stdio 模式
python -m plastic_promise

# SSE 模式
python -m plastic_promise --sse 9020
```

MCP Server 已启动时，也可以单独启动 Maintenance Daemon：

```bash
python daemons/maintenance_daemon.py
```

健康检查：

```bash
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
```

## MCP 配置

stdio 示例：

```json
{
  "mcpServers": {
    "plastic-promise": {
      "command": "python",
      "args": ["-m", "plastic_promise"]
    }
  }
}
```

SSE 客户端连接：

```text
http://127.0.0.1:9020/sse
```

## 核心能力

| 能力 | 说明 |
|---|---|
| 记忆质量管道 | 对经验、事实、决策、实体、事件、模式进行提取、分类、去重、门控、嵌入和衰减。 |
| 上下文供给 | `context_supply` 根据当前任务生成核心、关联、发散三层上下文。 |
| 审计与防线 | `audit_pre_check`、`audit_run`、`defense` 在写操作和风险动作前提供检查。 |
| 信任分驱动自治 | 信任分越高，自主权越大；信任分下降时需要更多显式确认。 |
| Hunter Guild 委托系统 | 通过 `task_enqueue -> task_claim -> task_complete -> task_verify` 管理多 Agent 协作。 |
| Skills / SuperPowers | `session-init`、`smart-remember`、`step-closure`、16 阶段 `sp-stage` 把工作流变成可追踪工具。 |
| Maintenance Daemon | 执行扫描、恢复、GC、任务生命周期维护和调度健康检查。 |
| 插件与市场 | 通过 pack 元数据加载知识、工作流、能力和适配器扩展。 |

`sp-stage` 当前覆盖完整 SuperPowers 技能面：`using-superpowers`、`brainstorming`、`exemplar-research`、`using-git-worktrees`、`writing-plans`、`executing-plans`、`subagent-driven-development`、`test-driven-development`、`verification-before-completion`、`finishing-a-development-branch`、`requesting-code-review`、`receiving-code-review`、`audit`、`systematic-debugging`、`dispatching-parallel-agents`、`writing-skills`。其中 `using-superpowers` 和 `writing-skills` 是元技能阶段，用于启动技能选择和技能编写/验证流程。

重型 `memory_recall` / `context_supply` 调用可携带 `stage_session_id`、`flow_line_id` 和 `request_id`。系统会派生 `request_scope_id`，写入审计元数据并显示在 `context_supply` 输出中，同时用它隔离重叠 SuperPowers 阶段或多 Agent 流程中的召回缓存。

在 `rust-full` 模式下，`memory_recall(debug=true)` 在 Rust 健康且优先时仍走 Rust snapshot 热路径，并返回 Rust `pipeline_stats` / `per_item_stats`；只有 Rust 不可用或异常时才回退 Python。当 LanceDB 中已有向量行时，debug `pipeline_stats` 应显示非零 `vector_count`；只有查询没有向量命中时，`vector_hits` 才可能为 0。

## 架构概览

<p align="center">
  <img src="architecture/plastic-promise-flow.zh-CN.svg" alt="Plastic Promise 本地治理运行时架构" width="960">
</p>

上方矢量图把运行时分成五层：参与者、MCP 入口、治理核心、自动化闭环、本地持久化与加速。README 中保留的是一眼可读的总览；更细的 C4、时序和组件图仍放在架构目录中。

更多架构文档：

- [SYSTEM_FULL_CHAIN.md](SYSTEM_FULL_CHAIN.md)
- [architecture/architecture.md](architecture/architecture.md)
- [architecture/plastic-promise-flow.svg](architecture/plastic-promise-flow.svg)
- [architecture/plastic-promise-flow.zh-CN.svg](architecture/plastic-promise-flow.zh-CN.svg)
- [architecture/diagrams/c4-level1-context.txt](architecture/diagrams/c4-level1-context.txt)
- [architecture/diagrams/c4-level2-container.txt](architecture/diagrams/c4-level2-container.txt)
- [architecture/diagrams/c4-level3-component.txt](architecture/diagrams/c4-level3-component.txt)

## 核心概念

### 约定工程

约定工程不是只在入口处拦截动作，而是让 Agent 在行动前主动检索相关约定、历史决策和上下文，并在行动后沉淀经验。

### 记忆不是档案

记忆会被使用、强化、合并、衰减。系统目标不是保存一切，而是让当前真正有用的上下文更容易被检索。

### 每步闭环

实质产出后应执行 `step-closure`，记录经验、改进、根因和下一步优化动作。闭环结果会影响未来记忆和信任分。

### 显式降级

默认数据存储在本地。外部 Agent、托管 embedding、托管 reranker 或 LLM 集成只有在配置后才会发生网络调用。可选服务不可用时，系统应明确标注降级状态，而不是静默假装完整路径成功。

### 多 Agent 可追踪协作

Hunter Guild 把任务发布、认领、心跳、完成、验收变成可追踪状态机，避免多 Agent 工作变成不可审计的提示词堆叠。

## 配置要点

| 项 | 默认 |
|---|---|
| SSE 端口 | `9020` |
| MCP 入口 | `python -m plastic_promise` |
| 一键启动 | `python scripts/init_and_start.py` |
| 启动模式 | `light`、`normal`、`rust-normal`、`full`、`rust-full`，非交互默认 `rust-full` |
| 守护进程 | `daemons/maintenance_daemon.py` |
| 默认 embedding | Ollama `mxbai-embed-large`，可降级 fallback embedder |
| SQLite | `data/db/plastic_memory.db`，可用 `PLASTIC_DB_PATH` 覆盖 |
| LanceDB | `data/lancedb`，可用 `PLASTIC_LANCEDB_PATH` 覆盖 |
| 运行日志 | `var/log/` |
| PID/心跳 | `var/run/` |

## 路线图快照

当前路线图入口仍是 [TODO List/README.md](TODO%20List/README.md)。高层方向包括：

| 方向 | 当前重点 |
|---|---|
| 运行时可靠性 | 保持 `session-init`、`context_supply`、`runtime_mode`、守护进程启动和降级路径可预测。 |
| Rust 加速 | 继续让可选 Rust Context Core 与 Python 权威管线语义收敛。 |
| Hunter Guild | 强化任务队列策略、扫描质量、重派、验收和信任分影响。 |
| 插件市场 | 稳定 pack 校验、安装、启用、禁用和元数据边界。 |
| 公开文档 | 让 README、架构图、快速开始和路线图与源码真相保持一致；后续发布文档需要英文和中文同步维护。 |

## 开发与贡献

```bash
pip install -e ".[dev]"
pytest
ruff check plastic_promise/
```

贡献约定：

- 使用 Conventional Commits。
- PR 保持小粒度、可审查。
- 行为变化必须同步更新文档。
- PR 描述中包含验证结果。
- 未经维护者明确授权不得合并 PR。
- 项目文件保持专业文本风格，不使用 emoji 作为状态标记。

## 路线图

当前未完成事项见 [TODO List/README.md](TODO%20List/README.md)。长期目标和系统状态见 [GOAL.md](GOAL.md)。
