# Plastic Promise 中文指南

> 本文件是面向发行版用户的中文快速指南。英文默认入口见 [../README.md](../README.md)，更完整的项目目标与状态见 [GOAL.md](GOAL.md)。

## 这是什么

Plastic Promise 是一个本地优先的 MCP Agent 记忆、上下文、审计与任务调度系统。它把约定工程、记忆生命周期、信任分、自审计、任务调度和技能工作流组合成一个 Agent 治理底座。

它适合：

- Claude Code 或其他 MCP 客户端需要共享长期记忆时。
- 多 Agent 团队需要可追踪的任务派发、验收和信任分时。
- 项目希望把“先查上下文、再行动、后闭环”的工作方式固化为运行时工具时。

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

# Ollama 不可用时，使用 fallback embedder 降级模式
python scripts/init_and_start.py --skip-ollama-check
```

仅启动 MCP Server：

```bash
# stdio 模式
python -m plastic_promise

# SSE 模式
python -m plastic_promise --sse 9020
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
| Skills / SuperPowers | `session-init`、`smart-remember`、`step-closure`、`sp-stage` 把工作流变成可追踪工具。 |
| Maintenance Daemon | 执行扫描、恢复、GC、任务生命周期维护和调度健康检查。 |
| 插件与市场 | 通过 pack 元数据加载知识、工作流、能力和适配器扩展。 |

## 架构概览

```text
+--------------------------------------------------------------------------+
| Plastic Promise Local Governance Runtime                                 |
|                                                                          |
| +------------------+      +-------------------+      +----------------+ |
| | MCP Server       | ---> | Context Engine    | ---> | Storage Layer  | |
| | stdio / SSE      |      | recall + supply   |      | SQLite/LanceDB | |
| +--------+---------+      +---------+---------+      +--------+-------+ |
|          |                          ^                         ^         |
|          v                          |                         |         |
| +------------------+      +---------+---------+      +--------+-------+ |
| | Memory Pipeline  | ---> | Principles/Graph  |      | Trust/Defense  | |
| | extract/dedup/GC |      | activate/evaluate |      | audit + tiers  | |
| +--------+---------+      +-------------------+      +--------+-------+ |
|          |                                                     ^         |
|          v                                                     |         |
| +------------------+      +-------------------+      +--------+-------+ |
| | Skills/Tracking  | ---> | Daemon/Guild      | ---> | Agent Bridge   | |
| | workflow stages  |      | scans/task queue  |      | events/notify  | |
| +------------------+      +-------------------+      +----------------+ |
|                                                                          |
+--------------------------------------------------------------------------+
```

更多架构文档：

- [SYSTEM_FULL_CHAIN.md](SYSTEM_FULL_CHAIN.md)
- [architecture/architecture.md](architecture/architecture.md)
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
| 守护进程 | `daemons/maintenance_daemon.py` |
| 默认 embedding | Ollama `mxbai-embed-large`，可降级 fallback embedder |
| SQLite | `data/db/plastic_memory.db`，可用 `PLASTIC_DB_PATH` 覆盖 |
| LanceDB | `data/lancedb`，可用 `PLASTIC_LANCEDB_PATH` 覆盖 |
| 运行日志 | `var/log/` |
| PID/心跳 | `var/run/` |

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
