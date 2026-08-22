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
  云端 Profile 使用独立 9030 推理网关和 9040 远程配置控制面，均只监听 127.0.0.1
  Rust context-engine-core 可选加速路径，Python 管线仍是权威完整路径；Rust snapshot 入口过滤 audit telemetry，Python 转换边界保留最终防线
```

## 三、当前状态 (2026-08-22)

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
- `session-init`、`smart-remember`、`step-closure`、`sp-stage` 已作为程序化技能暴露；官方复合 Skill 的 receipt 会显式声明内部调用，并以确定性 entity-only 子链记录，而不会把 caller attestation 伪装成 Hook 观测。
- TrustStore 将信任分持久化到 SQLite。
- Hunter Guild 任务生命周期工具已接入 MCP。
- 插件/市场命令已作为实验性扩展面暴露。
- 独立远程配置控制面提供只读服务器状态、角色分离和云配置的 validate -> immutable stage -> CAS activate；它不共享 9020/9030 监听，也没有 sudo/systemd 权限。

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
| 治理工作流 | 固定版本官方 Skill 的 `sp-stage` 指导/receipt 入口；客户端执行 Skill，服务端校验权限、route、步号、幂等 receipt 并持久化游标 |

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

当前未完成事项见 [TODO List/README.zh-CN.md](TODO%20List/README.zh-CN.md)。其中带日期的 comparison 文档保留为研究基线；README 中的 Roadmap Status 才是当前未完成工作的索引。

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

## 2026-08-02 Workflow and Passive Memory Pipeline Note

- Official workflow state now separates immutable run generations from the parent Codex session. Completed runs remain history, unfinished runs resume on continuation, and explicit roots create a new generation.
- Deterministic routing remains authoritative for explicit commands and intent boundaries. Optional cloud JSON routing can select only model-authority routes; invalid or unavailable output falls back to `routing/ask-matt`.
- Passive rule misses enqueue durable semantic jobs instead of blocking Stop. Batches are isolated by project, visibility, configuration revision, and Provider identity; only original user text and grounded evidence are accepted.
- Semantic proposals reuse `ProposalAutomation`, including per-source observation signals. Eligible score revisions enqueue durable promotion jobs with bounded retry/dead outcomes; reconciliation repairs post-commit enqueue gaps.
- Semantic or promotion worker initialization failures retain stable reason codes in the durable Maintenance stage span; the stage becomes degraded and the parent cycle partial instead of reporting success.
- `evaluate_auto_promotion()` remains the sole promotion policy authority. SQLite is canonical, vector evidence is derived, and no pending proposal enters ordinary recall or LanceDB.
- New gates default off: `PP_PASSIVE_SEMANTIC_ROUTING=off`, `PP_PASSIVE_SEMANTIC_CAPTURE=off`, and `PP_MEMORY_PROPOSAL_AUTO_ADOPT=off`. This branch does not enable production Maintenance or promote a LanceDB generation.

## 2026-08-03 Cloud, Workflow, and Passive Memory Release Note

- Release version `0.2.15` follows the highest immutable public release tag
  `v0.2.14` and carries the governed-memory/knowledge v2 baseline plus the
  composable deployment range through PR #107.
- Hosted embedding and reranking now run behind a loopback-only inference
  gateway with durable reservations, bounded leases, server-owned provider
  credentials, optional client-local rerank packages, and an explicitly
  non-authoritative process-memory cache.
- Remote provider configuration uses a separate loopback-only control plane with
  role-separated tokens, immutable revisions, write-only secrets, ETag/CAS
  activation, and exact generation evidence for embedding identity changes.
- LanceDB generation management supports isolated shadow rebuilds, verified
  promotion evidence, active-generation replay, and recovery without making the
  vector index a second truth source.
- Passive memory and structured fusion use durable project-isolated work queues.
  Semantic capture and proposal promotion are asynchronous, retryable, and
  reconcile post-commit gaps; pending proposals remain outside canonical recall
  and LanceDB until the governing policy admits them.
- The engineering workflow is pinned to the official Matt Pocock skill revision
  and separates project/session/flow scope, immutable run generations,
  user/model invocation authority, bounded execution receipts, and composite
  skill evidence.
- Cloud inference, remote configuration, semantic routing/capture, automatic
  proposal adoption, structured fusion, Maintenance activation, and LanceDB
  generation promotion remain independently gated. This release preparation
  does not enable production Maintenance or promote a generation.
- Release verification for `0.2.15` is **audited and approved**. Final whole-repository verification and mandatory high-risk review completed before release synchronization. Release-specific benchmark and runtime evidence are recorded in the release notes.

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

## 2026-07-21 Dashboard and Structured Memory Release Note

- Release version `0.1.19` follows the immutable public `v0.1.18` release and adds the operator-facing Dashboard V2 plus explainable structured-memory projections.
- Dashboard V2 provides a Chinese, loopback-only, project-scoped and read-only operator surface for overview, memories, request traces, synthesis, detailed lineage, retrieval explanation, operations, trust issues, and runtime configuration. The local operator may select a project ID discovered from canonical SQLite activity; each data or review request remains bound to exactly one selected project, and the selector is not a remote tenant-authorization mechanism.
- Deterministic `structure-v1` manifests now have Python and Rust implementations. Memory and lineage projections retain bounded chunk anchors, heading paths, source spans, hashes, parent identity, and explicit truncation/integrity state.
- Retrieval explanation preserves lexical/vector/graph scores, ranking and filter reasons, chunk evidence, and measured request/stage durations. Missing evidence stays unavailable instead of becoming a fabricated `0 ms`.
- Focused Python coverage passes with `242 passed, 18 skipped`; the final whole-repository run passes with `2177 passed, 22 skipped` while the current release PyO3 artifact is importable. Rust release suites pass `36 + 7 + 22`; scoped static checks and wheel/sdist construction also pass.
- The automated audit score is `0.6752`, above the `0.60` gate, and the high-risk checklist passes with zero blocking or major findings. An isolated candidate `rust-full` process passes health, runtime identity, store, Rust snapshot recall, and context supply without degradation.
- Verification status is **Draft/BLOCK**. Final publication remains blocked until the merged-range release-sync dry run and post-publication restart verification pass.
- Release synchronization remains fail-closed and may publish only the reviewed merged range after every gate passes.

## 2026-07-23 Cloud Inference Gateway Note

- The server cloud profile installs no local model runtime. Hosted embedding and hosted reranking are explicit, fail-closed provider selections; client-local reranking remains optional and request-scoped.
- A dedicated inference gateway binds only `127.0.0.1:9030`, uses a server-owned project plus Bearer token, and keeps provider credentials out of frontend requests and bundles.
- Durable preflight reservations occur before missing embeddings are generated, so concurrent devices sharing an idempotency key do not duplicate cloud embedding calls. A short renewable preparation lease allows a retry to take over after a process crash without waiting for the full job TTL. Different request material under the same key conflicts.
- The authoritative `client-local-rerank/v2` package is stored in a separate SQLite job database and binds an immutable client model identity into the idempotency input. An executor with a different model rejects the package before inference, while TTL, lease capability hashes and transactional CAS make the first valid completion win without granting any client result authority over LanceDB.
- The job database enforces transactional per-project limits for active work, retained rows and retained JSON bytes, and prunes elapsed retention during write traffic.
- Canonical memory, vector, audit, outbox and job data remain server-only. The optional process-memory hot cache holds only bounded server-response text keyed by project, memory ID, memory version and content hash, with no persistence, database import, LanceDB replica, vector set or offline write queue. Agent retain/priority/TTL values are active-project-session recommendations: a lazy per-operation cadence independently scores access frequency, recency and size, requires a minimum system-signal floor, and gives that score 70% of the joint retention/capacity decision while Agent priority is capped at 30%. Agent preference is therefore never a pin and remains bounded by TTL and hard capacity. Each cache instance owns one active project session; starting a session always clears prior preferences and invalidates prior request contexts, including same-project rotation. A same-key Agent refresh may change priority or shorten TTL without resetting system heat or cadence; system eviction enforces a cadence cooldown. Requests capture the login/project generation before transport, so logout, clear or project switching rejects late responses. A bounded version high-watermark protects active entries and the maximum response window, then safely prunes cold state; saturation fails closed rather than forgetting protected state. `retain=false` is an Agent-local eviction and immediately invalidates every cached version of the identity without pretending to be a server deletion tombstone.
- Gateway capabilities expose distinct all-supplied, mixed, and missing-vector contracts. Mixed requests bind every supplied vector to the runtime embedding identity used to fill missing vectors; the target-level identity is null when that differs from the all-supplied client-vector identity. Capabilities also expose the exact cloud-provider host allowlist requirement and non-authoritative client-cache policy; clients do not derive these contracts from provider names.
- Request cancellation is isolated from in-process preparation and reranking. Graceful shutdown drains for at most 30 seconds; over-deadline or process failure can occur after provider billing but before SQLite completion, so a client retry resumes durable pending work with at-least-once billing while CAS rejects late overwrite.
- MCP remains on loopback port `9020`. Production forwards `9030` separately and does not mount gateway routes in the MCP listener; the maintenance daemon remains disabled until its pending task queue is reviewed.

## 2026-07-24 Remote Configuration Control Plane Note

- 远程配置控制面是独立的无界面管理 API，只监听 `127.0.0.1:9040`；Mac 使用 `19040 -> 127.0.0.1:9040` SSH LocalForward。9040 不得进入公网安全组、UFW 放行规则或公共反向代理，也不挂载到 MCP 9020 或推理网关 9030。现有 Dashboard V2 是唯一运维前端，并从浏览器直接调用 19040；控制 Token 不经过 MCP。
- 权限按 `viewer < operator < secret-admin` 分级。控制 Token 的 EnvironmentFile 只保存 SHA-256，Dashboard 中的明文 Token 只存在 JavaScript 内存，不进入 Cookie、URL、`localStorage` 或 `sessionStorage`。Embedding、Rerank 和切片推理 Key 只能写入或清除，所有读取、修订元数据和审计仅返回是否已配置。
- 配置变更流程固定为读取 safe config/ETag -> validate -> 创建不可变 revision -> `If-Match` CAS activate。所有 POST 都需要 JSON 和当前 `If-Match`；stage、activate 和 generation retarget 还需要按操作稳定的 `Idempotency-Key`，同一物料重试时复用、物料变化后更换。并发旧 ETag 和同 Key 不同输入都 fail closed。激活只原子替换 `PP_CONTROL_ROOT/managed.env`，返回 `restart_required=true`，不调用 sudo、systemctl 或任何服务管理器。
- 已由主机运维推广且通过质量门的 current generation 可通过独立的 operator-only retarget 操作与 desired 指针对齐。该操作不创建或修改配置 revision，不写 `managed.env`，但会用 `If-Match`、`Idempotency-Key` 和审计事件原子更新控制面元数据 SQLite；generation ID、manifest、运行时 embedding identity 和质量证据必须与已验证 current generation 完全一致。
- Revision 的安全元数据、幂等结果和审计历史保持不可变；含 secret 的私有 EnvironmentFile 只保留当前 active revision 与仍基于当前 ETag 的 staged candidates。激活提交后，控制面在同一进程锁和文件锁下回收新 CAS 状态已不可能激活的旧文件，崩溃后启动会幂等续做；历史回滚必须从当前 ETag 新建 revision 并重新提供 write-only secret。
- 数据库/LanceDB 路径、所有监听地址、控制面认证、systemd 策略、Maintenance 状态、gateway project ID/token/job DB 和 Provider host allowlist 均为 bootstrap-only；远程配置不能扩张 Provider 出站主机边界。revision 私下绑定进程可见的 bootstrap EnvironmentFile 指纹，激活和崩溃恢复发现漂移即 fail closed，且 API 不返回该 secret-dependent 指纹。运行单元按 base EnvironmentFile 后加载 `managed.env`，服务重启由另行授权的主机运维流程完成。
- 精确运行 embedding-index identity 的任何变化（包括 structure-v1 切片预算）都必须绑定同一 staged revision 的 Provider smoke、完整 shadow LanceDB rebuild、固定中英文质量门和 verified generation evidence；即使维度同为 1024 也不能复用旧 LanceDB。证据缺失时返回 `embedding_generation_required` 或前置条件错误，控制面本身不调用 Provider、不重建索引、不伪造证据。
- 托管 embedding 的每百万 tokens 单价、币种和价格版本由远程配置作为完整三元组审计并渲染到受保护 EnvironmentFile；价格元数据不改变向量身份，但 generation 质量证据缺少任一项时不能推广。
- 状态采集保持只读：SQLite 使用 `mode=ro + query_only`，LanceDB 只读 generation manifest，Maintenance 只读 heartbeat，9020/9030/9040 仅做回环可达检查；GET status/config 不产生 Provider 调用或数据库写入。
- 服务器云 Profile 不安装本地模型。前端可以省略 embedding，由服务器在持久化幂等预留后用云 Provider 补齐；客户端提供的向量仍是 request-scoped，不能写正式 LanceDB。可选客户端本地 rerank 只能处理服务器持久化的权威 package，通过 lease/CAS 让多设备下首个合法结果获胜，不能选择 Provider、补 embedding、提交 Key 或修改 SQLite/LanceDB。
- 完整启动、EnvironmentFile 顺序、SSH 隧道、门禁和验收步骤见 [Remote Configuration Control Plane](remote-control-plane.md)。

## 2026-07-26 Durable Derived Memory Work Note

- 结构化融合不再依赖进程内队列作为可靠性边界。`DerivedWorkStore` 使用服务器 SQLite 保存项目隔离、幂等键、可续租 capability、fencing generation、重试预算和 attempt history；SQLite 仍是唯一真相源。
- canonical memory 与派生工作收据可在同一 SQLite 事务提交。Provider 调用始终在写事务外，governed synthesis draft 与整批 completion receipt 在同一事务落地，失败时共同回滚。
- 批处理键固定为 `(project_id, visibility, config_revision, job_kind, provider_identity)`；不会为了凑满 20 条而跨项目、跨可见性、跨修订或跨 Provider 混批。
- `PP_STRUCTURED_MEMORY_FUSION=off` 仍为默认值。`shadow` 不创建 synthesis draft，`on` 也只创建未验证 draft；proposal promotion、synthesis verification、Maintenance Daemon 与 LanceDB generation promotion 不由该 worker 执行。
- Dashboard V2 只读显示当前项目的 queue depth、oldest age、leased/retry/dead 计数。当前改动没有迁移或启用生产服务器数据库。

## 2026-08-21 Compute-Node Handshake Release Note

- Release version `0.2.16` follows the highest immutable public release tag
  `v0.2.14` and carries the one-click compute-node private-transport handshake,
  one-time onboard trust bootstrap with a launchd-managed tunnel service,
  canonical endpoints-file resolution, and bearer-prefix idempotency fixes
  through PR #10.
- Release verification for `0.2.16` is **audited and approved**. Final whole-repository verification and mandatory high-risk review completed before release synchronization. Release-specific benchmark and runtime evidence are recorded in the release notes.


## 2026-08-22 Release Pipeline and Governed Vector Smoke Note

- v0.2.16 已通过完整受控链发布：GitHub Actions 构建摘要固定镜像并发布 PyPI；
  `release-sync.py --push` 原子推送 `main` 与附注标签（sync commit + tag 对象重审）。
- `scripts/release_pipeline.py` 提供一键发行验证链：`doctor`（环境预检）、
  `handshake`（计算节点接线：控制面材料同步、Keychain bearer、governance schema
  迁移、节点注册断言）、`e2e`（治理路由金丝雀 + strict 只读冒烟）、`receipt`
  （wheel 原生回执，8 检查全真）、`evidence`（12 字段证据，dry-run 抓取 scope）、
  `all`（以上串联）与 `publish`（经受证路径推送）。该管线只固化操作编排，不弱化
  任何质量门禁；`publish` 仍走 release-sync 的完整 fail-closed 校验。
- 发行容器可通过控制面路由接入异构推理节点：`PP_CONTROL_PLANE=1` + 固定节点身份 +
  私有传输探针；健康策略 strict 时 `vector_ready=true` 且 provider 为 `governed-node`
  （Qwen3-Embedding-4B, 2560 维, L2）。server 进程保持 `pp-server-backend` 角色，
  永远不是推理执行面；text-only 冒烟仅作为无计算节点环境的显式降级路径保留。
- 节点接线材料以 Mac 控制面为源：deployment manifest、私有端点文件（0600）、
  keychain bearer；容器内以 uid 1000 运行，挂载材料需对应属主。
