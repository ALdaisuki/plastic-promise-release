# Plastic Promise — 项目目标与指令

> 核心范式：约定工程 (Commitment Engineering) — 内化约定替代外部约束。

## 一、项目定位

Plastic Promise 是一个本地优先的 AI Agent 行为治理与协作运行时。它通过 MCP Server 把记忆、上下文供给、原则、审计、防线、信任分、技能追踪和任务调度连接为一条可追踪的工作链。

它不是单纯的“记忆库”，也不是只靠规则门禁拦截 Agent 的约束系统。它的目标是让 Agent 在行动前主动检索约定和历史上下文，在行动中接受审计和信任分约束，在行动后通过闭环反思改进未来行为。

## 二、架构总览

```text
约定层 — 内化于心
  12 条核心原则
  原则激活与反事实评估
  原则遵守量化追踪
  原则和记忆的图谱关联

实践层 — 外显于行
  MCP Server: stdio / SSE 工具入口
  ContextEngine: 记忆、文本、向量、图谱、原则融合检索
  Memory Pipeline: 提取、分类、去重、QualityGate、嵌入、衰减、双写
  Trust/Defense: L0 硬边界、L1 信任约束、L2 免疫巡检
  Skills: session-init、smart-remember、step-closure、sp-stage
  Hunter Guild: task_enqueue -> claim -> heartbeat -> complete -> verify
  Maintenance Daemon: 扫描、恢复、GC、任务生命周期维护

演化层 — 迭代进步
  worth 反馈闭环
  SCARF 五维自省
  CEI 复合执行指数
  Weibull 记忆衰减
  经验包导入导出
  插件与市场扩展

基础设施
  SQLite WAL 结构化状态
  LanceDB 向量存储
  Ollama mxbai-embed-large 默认本地 embedding，可降级 fallback embedder
  Rust context-engine-core 可选加速路径，Python 管线仍是权威完整路径；Rust snapshot 入口过滤 audit telemetry，Python 转换边界保留最终防线
```

## 三、当前状态 (2026-07-04)

### 稳定/活跃

- MCP Server 支持 stdio 与 SSE 模式。
- 一键启动器 `scripts/init_and_start.py` 可启动 MCP Server、Maintenance Daemon 与 Watchdog，并支持 `light`、`normal`、`rust-normal`、`full`、`rust-full` 五种运行模式。
- 记忆质量管道已接入提取、分类、向量去重、QualityGate、衰减初始化与 LanceDB 双写。
- ContextEngine Python 路径仍是完整回退和写侧权威路径；`rust-full` 下正常召回和 `memory_recall(debug=true)` 在 Rust 健康时走 Rust snapshot 热路径。
- `memory_recall` / `context_supply` 支持 `stage_session_id`、`flow_line_id`、`request_id`，通过 `request_scope_id` 隔离并发重型上下文请求、审计元数据和 `context_supply` 可见 trace。
- `session-init`、`smart-remember`、`step-closure`、`sp-stage` 已作为程序化技能暴露。
- TrustStore 将信任分持久化到 SQLite。
- Hunter Guild 任务生命周期工具已接入 MCP。
- 插件/市场命令已作为实验性扩展面暴露。

### 实验/仍需验证

- Rust context-engine-core 是可选加速路径，仍需持续补齐与 Python 管线的语义一致性；当前已对 daemon audit telemetry 建立 Rust snapshot 入口过滤与 Python native-result 边界过滤。
- Hunter Guild 的扫描器信噪比、惩罚策略和任务路由仍在迭代。
- 插件市场生态处于早期阶段。
- 发行版文档正在从内部操作手册整理为公开用户文档。

### 当前 MCP 工具面

当前 `plastic_promise/mcp/server.py` 中暴露 57 个 MCP 工具，其中包含 `session_init` / `sp_stage` 等兼容别名。旧文档中的 40、41、48、51、56 等数字是阶段性历史记录，发行版文档以后以源码声明为准。

主要分组：

| 分组 | 说明 |
|---|---|
| Memory | 记忆检索、存储、更新、纠正、GC、重分类、文件同步 |
| Principles | 原则激活与反事实评估 |
| Context | 上下文供给、图谱、注入与自动上下文注入 |
| Audit/Defense | 审计、防线、信任分 |
| Commercial Audit | 商业审计导出：call spans、降级事件、store outbox |
| Reflection | SCARF 自省与反馈应用 |
| System/Runtime | 系统状态、运行模式热更新、Issue 生命周期 |
| Pack | 经验包导入导出 |
| Domain | 域联邦管理 |
| Dispatch | Hunter Guild 委托生命周期 |
| Skill Tracking | 技能执行链追踪 |
| Skills | session-init、smart-remember、step-closure |
| Review | 结构化代码审查入口 |
| Market | 插件市场管理 |
| SuperPowers | 16 阶段统一入口 `sp-stage`，覆盖完整已安装 SuperPowers 技能面 |

## 四、12 条核心约定

| # | 原则 | 域 | 一句话 |
|---|---|---|---|
| 1 | 奥卡姆剃刀 | all | 如无必要，勿增实体 |
| 2 | 全过程可查可透明 | all | 每步有 git 痕迹、可追溯审计日志 |
| 3 | 自我审计闭环 | reflecting | 根因、改良、教训、评分 |
| 4 | 上下文驱动决策 | designing | 无上下文不行动，不足时标注而非猜测 |
| 5 | 约定优于约束 | governing | 检验存在不等于有效 |
| 6 | 数据流驱动 | designing | 追踪真实数据流，而非假设架构图 |
| 7 | 器官互保 | building | 每个子系统保护整个系统 |
| 8 | 工具即感官 | all | LLM 能力边界由工具链决定 |
| 9 | 信任驱动约束 | governing | 动态信任分调节自主权 |
| 10 | 自演化闭环 | reflecting | 评价驱动行为修正 |
| 11 | 原则遗传 | governing | 核心约定跨 Agent 传递 |
| 12 | 代码即文档 | building | 代码本身是最权威的文档 |

## 五、多 Agent 标签状态机

```text
task_enqueue
  -> pending
  -> task_claim
  -> executing + heartbeat
  -> task_complete
  -> pending review
  -> task_verify
  -> accepted / rejected / reassigned
```

旧版标签式描述仍可作为心智模型：

```text
task:pending -> task:accepted -> task:active -> task:done -> task:review -> task:reviewed
```

## 六、信任-自由度矩阵

| 信任分 | 等级 | 写文件 | 发 Issue | 分配任务 | 行为 |
|---|---|---|---|---|---|
| 0.80+ | autonomous | 允许 | 允许 | 允许 | 自主执行 |
| 0.60+ | standard | 允许 | 允许 | 不允许 | 正常执行 |
| 0.30+ | restricted | 需审批 | 不允许 | 不允许 | 写前确认 |
| 0.00+ | readonly | 不允许 | 不允许 | 不允许 | 只读 |

## 七、操作方法

### 启动系统

```bash
# 推荐：一键启动 MCP Server + Maintenance Daemon + Watchdog
python scripts/init_and_start.py

# 显式指定运行模式（自动化/后台启动推荐）
python scripts/init_and_start.py --mode rust-full

# Ollama 不可用时使用 fallback embedder
python scripts/init_and_start.py --skip-ollama-check

# 仅启动 MCP Server
python -m plastic_promise --sse 9020

# 单独启动维护守护进程
python daemons/maintenance_daemon.py
```

启动模式：

| 模式 | 含义 |
|---|---|
| `light` | 最快启动，延迟 LanceDB，强制 Python 供给路径 |
| `normal` | Python 供给路径，允许后续懒初始化 LanceDB |
| `rust-normal` | Rust 优先供给，跳过启动 LanceDB backfill/rebuild |
| `full` | Python 供给路径，启动时执行完整 LanceDB 维护 |
| `rust-full` | Rust 优先供给，启动时执行完整 LanceDB 维护；非交互默认 |

运行中可通过 MCP `runtime_mode(action="get")` 查看模式，或 `runtime_mode(action="set", mode="light")` 热切换当前 MCP 进程模式。

### Claude / MCP 客户端开始任务

```text
session-init(task_description="当前任务", context_mode="light")
context_supply(task_description="当前任务", task_type="architecture|code_generation|debugging|code_review")
audit_pre_check(action_description="即将执行的操作", action_type="write|edit|exec")
```

### 任务完成后闭环

```text
step-closure(
  task_description="本步做了什么",
  mode="full",
  lesson="学到什么",
  improvement="下次如何更好",
  root_cause="问题或良好结果的根因",
  optimization="立即可执行的改进动作"
)
```

## 八、发行版边界

发行版文档应保留用户可运行、可理解、可复现的信息：README、快速开始、架构概览、安全策略、贡献指南、路线图和开发指南。

内部临时计划、运行时日志、缓存、私有 worktree 状态、未整理的设计草稿不应作为公共入口的一部分。

## 九、路线图

当前未完成事项见 [TODO List/README.md](TODO%20List/README.md)。其中 dated comparison 文档保留为研究基线；README 中的 Roadmap Status 才是当前未完成工作的索引。

## 2026-07-06 Runtime Startup Note

- Launcher-managed services prepend the project root to child-process `PYTHONPATH`.
- `maintenance_daemon.py` self-bootstraps `_project_root` into `sys.path`, so direct script starts and one-click launcher starts use the same source checkout imports.
