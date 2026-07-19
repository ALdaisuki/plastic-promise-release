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
  MCP Server: stdio / Streamable HTTP `/mcp` 工具入口（保留 SSE 兼容）
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
  Ollama mxbai-embed-large 默认本地 embedding，长文本切块池化，可降级 fallback embedder
  Ollama qwen2.5:3b 默认本地 reranker，失败时回退 cosine/original 排序
  Rust context-engine-core 可选加速路径，Python 管线仍是权威完整路径；Rust snapshot 入口过滤 audit telemetry，Python 转换边界保留最终防线
```

## 三、当前状态 (2026-07-04)

### 稳定/活跃

- MCP Server 支持 stdio 与 Streamable HTTP `/mcp` 模式，`--sse` 仅作为旧脚本兼容别名保留。
- 一键启动器 `scripts/init_and_start.py` 可启动 MCP Server、Maintenance Daemon 与 Watchdog，并支持 `light`、`normal`、`rust-normal`、`full`、`rust-full` 五种运行模式。
- 记忆质量管道已接入提取、分类、向量去重、QualityGate、衰减初始化与 LanceDB 双写。
- ContextEngine Python 路径仍是完整回退和写侧权威路径；`rust-full` 下正常召回和 `memory_recall(debug=true)` 在 Rust 健康时走 Rust snapshot 热路径。
- `memory_recall` / `context_supply` 支持 `stage_session_id`、`flow_line_id`、`request_id`，通过 `request_scope_id` 隔离并发重型上下文请求、审计元数据和 `context_supply` 可见 trace。
- Context Recommender 已接入 `memory_recall` / `context_supply`，返回推荐原因与排序元数据，但不覆盖 project policy、硬排除或信任边界。
- Tool Manifest Graph 已覆盖 MCP 工具语义，`defense(action="evaluate_tool")` 可基于能力、风险、副作用、信任要求与 fallback 返回 `allow|ask|deny`。
- Unified Event Protocol 已落地 `runtime_events`，记录 task/tool/agent 调用的 `pending/running/completed/error` 状态、request scope、trust tier、defense decision 与 audit trace。
- `mgp_shadow_bridge` 已作为审计优先的 MGP 兼容桥暴露，P1 只映射治理语义并记录审计事件，不改写长期记忆。
- `session-init`、`smart-remember`、`step-closure`、`sp-stage` 已作为程序化技能暴露。
- TrustStore 将信任分持久化到 SQLite。
- Hunter Guild 任务生命周期工具已接入 MCP。
- 插件/市场命令已作为实验性扩展面暴露。

### 实验/仍需验证

- Rust context-engine-core 是可选加速路径，仍需持续补齐与 Python 管线的语义一致性；当前已对 daemon audit telemetry 建立 Rust snapshot 入口过滤与 Python native-result 边界过滤。
- MGP Shadow Bridge 的 `inject` 模式仍是后续阶段预留；当前不向 recall/context 请求注入外部治理策略。
- Hunter Guild 的扫描器信噪比、惩罚策略和任务路由仍在迭代。
- 插件市场生态处于早期阶段。
- 发行版文档正在从内部操作手册整理为公开用户文档。

### 当前 MCP 工具面

当前 `plastic_promise/mcp/server.py` 中暴露 58 个 MCP 工具，其中包含 `session_init` / `sp_stage` 等兼容别名。旧文档中的 40、41、48、51、56、57 等数字是阶段性历史记录，发行版文档以后以源码声明为准。

主要分组：

| 分组 | 说明 |
|---|---|
| Memory | 记忆检索、存储、更新、纠正、GC、重分类、文件同步 |
| Principles | 原则激活与反事实评估 |
| Context | 上下文供给、图谱、注入、自动上下文注入与推荐元数据 |
| Audit/Defense | 审计、防线、信任分与工具语义决策 |
| Commercial Audit | 商业审计导出：call spans、降级事件、store outbox |
| MGP Shadow | MGP 兼容语义桥：shadow 审计、模式查询与 inject 预留 |
| Reflection | SCARF 自省与反馈应用 |
| System/Runtime | 系统状态、运行模式热更新、Issue 生命周期 |
| Pack | 经验包导入导出 |
| Domain | 域联邦管理 |
| Dispatch | Hunter Guild 委托生命周期 |
| Skill Tracking | 技能执行链追踪 |
| Skills | session-init、smart-remember、step-closure |
| Review | 结构化代码审查入口 |
| Market | 插件市场管理 |
| 治理工作流 | 16 阶段统一入口 `sp-stage`，只暴露精简程序化合同，保留链校验、产物、闭环和审计 |

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

# 仅启动 MCP Server（Streamable HTTP /mcp）
python -m plastic_promise --streamable-http 9020

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
- Shared runtime startup now defaults `EMBEDDER_TIMEOUT=30` unless the operator overrides it, so cold Ollama embedding calls do not make full MCP smoke unstable.
- On Windows, `scripts/init_and_start.py --stop` must only terminate command lines that match Plastic Promise MCP or `maintenance_daemon.py`; it must not kill every `python.exe` process.

## 2026-07-09 Memory Summary Index Note

- `PP_MEMORY_SUMMARY_INDEX=1` enables the feature-gated summary-index write path.
- SQLite remains the truth source for raw memory text, L0/L1/L2 summary layers, summary-only `embedding_text`, and `embedding_hash`.
- LanceDB remains a derived index and receives compact `search_text` instead of raw turns or full L2 narrative while the gate is enabled.
- With the flag unset, the legacy LanceDB `text=content` behavior is preserved.

## 2026-07-09 HTTP MCP Release Smoke Note

- Release verification should exercise the live Streamable HTTP MCP process at `http://127.0.0.1:9020/mcp`, not only the Codex-exposed MCP tool surface.
- `scripts/smoke_http_mcp.py` verifies `/health`, `runtime_mode`, `memory_store`, `memory_recall(debug=true)`, `context_supply(debug=true)`, and optional SQLite/LanceDB summary-index boundaries.
- Use `http://127.0.0.1:9020/health` for browser/probe checks. `/mcp` is an MCP protocol endpoint, so plain browser GETs and closed long-poll/SSE clients can produce benign 404 or client-disconnect logs.
- Windows Proactor client-disconnect tracebacks are filtered at the MCP server event-loop boundary; plain `/mcp` GET 404s remain visible because they identify protocol-mismatched probes.
- After an MCP process restart, Codex desktop sessions may keep stale dynamic tool handles until the session/tool registry refreshes; the server can be healthy while the current client session still needs reconnect.

## 2026-07-11 Governed Synthesis Retrieval Note

- SQLite remains canonical for synthesis lifecycle, provenance snapshots, proposal review, and exact index material; LanceDB remains derived and rebuildable.
- New behavior is off by default: `PP_SYNTHESIS_ARTIFACTS=off`, `PP_SYNTHESIS_RETRIEVAL=0`, `PP_MEMORY_PROPOSALS=off`, and `PP_MEMORY_INDEX_TEXT_POLICY=legacy`.
- Synthesis follows `draft -> verified -> stale|contested`; refresh creates the next draft revision and requires a new actor/call/timestamp verification record before recall.
- Pending, rejected, and expired proposals are never ordinary recall candidates or LanceDB rows.
- Governed maintenance order is memory lifecycle, proposal expiry, synthesis integrity, synthesis index replay, then audit.
- Deterministic bilingual reports test metric and gate behavior only. Publishable evidence requires isolated versioned corpus seeding, a real non-fallback model, complete comparable split sets and environment metadata, plus a successful store-recall-context smoke.
- Rollback disables all four gates above without deleting canonical control, evidence, proposal, lineage, or audit rows.

## 2026-07-12 Canonical Mutation and Release Note

- Release version `0.1.15` follows the active release-repository `main` package line at `0.1.14` and carries the governed-synthesis corrective hardening.
- Release warning: the public repository still contains historical `v0.2.14`, which SemVer sorts above `v0.1.15`. Keep `v0.2.14` untouched and do not mark `v0.1.15` as latest; automated SemVer selectors may continue to prefer `v0.2.14`.
- Retrieval-visible ordinary-memory content and availability changes use one field-scoped SQLite transaction that records lineage, stales dependent synthesis, increments `memory_version`, and persists checked `memory-index/v3` jobs before commit.
- GC rejects empty and cross-project candidates, checks declared project equality before the transaction, and rechecks canonical source/peer project equality inside it. Spoofed project declarations fail without partial state.
- Public mutation identity and authority are server-owned. Both `smart-remember` aliases require `memory_update`; public `memory_forget` remains critical at `0.80`, while internal `audit_rollover` uses `0.60` without exposing a weaker public delete path.
- Upgrade keeps all four synthesis/proposal/index gates at their legacy defaults. Restart MCP Server and Maintenance Daemon together, then run the live HTTP smoke with `--expected-version 0.1.15` before enabling opt-in behavior.
- This release removes no public MCP tool or parameter. The change is not classified as breaking; SQLite remains canonical and LanceDB remains derived and repairable.
- Dependency compatibility note: governed retrieval requires LanceDB `>=0.34.0`.
- Release verification for `0.1.15` is **audited and approved**. Tasks 6-12 and the public HTTP calibration/held-out runner are implemented, including canonical CAS migration, recovery, versioned fusion, opaque held-out binding, and strict comparison. The one-shot public calibration completed with no eligible WRRF candidate; held-out queries remained unopened and `legacy-auto` stayed active.

## 2026-07-14 Context Supply Reliability Release Note

- Release version `0.1.17` follows the immutable public `v0.1.16` release and carries structure-aware embedding chunking behind an opt-in flag.
- Synchronous context assembly runs behind a bounded worker pool with explicit embedding and supply deadlines; timeout responses are degraded and traceable rather than blocking the MCP HTTP event loop.
- Rust snapshot enrichment reads LanceDB vectors in admitted-ID-only batches, preserving canonical admission while removing the per-memory N+1 query pattern.
- No public MCP tool or parameter changed, no dependency changed, and retrieval fusion remains `legacy-auto`.
- `PP_MEMORY_CHUNKING=shadow` is the default evaluation path; `structure-v1` remains opt-in until versioned real-model recall evidence passes the release gates.
- Release verification for `0.1.17` is **audited and approved**. Targeted chunking, full regression, live HTTP, restart, and release-sync gates completed before publication.

## 2026-07-19 Semantic Chunk Enrichment Release Note

- Release version `0.1.18` follows the immutable public `v0.1.17` release and adds optional local semantic metadata after deterministic `structure-v1` chunking.
- `structure-v1` remains the sole owner of chunk boundaries. The local model cannot change source text, order, heading paths, or source spans.
- `PP_MEMORY_CHUNK_ENRICHMENT=shadow` performs bounded background analysis without changing vectors or index identity. `on` is activated by an offline rebuild and remains enabled for matching writes and repairs.
- Active plans bind the Ollama model digest, prompt hash, schema hash, exact embedding inputs, and fallback state. Query embeddings never call the enrichment model.
- Default behavior remains `off`; rollback disables enrichment and rebuilds the derived LanceDB index while preserving canonical SQLite content and audit material.
- Whole-repository regression passes on LanceDB `0.34.0` with `2024 passed, 22 skipped`; the formal system audit score is `0.6752`, and the high-risk ten-item code checklist has no blocking finding.
- Release verification for `0.1.18` is **audited and approved**. Final whole-repository verification and mandatory high-risk review completed before release synchronization. Release-specific benchmark and runtime evidence are recorded in the release notes.
