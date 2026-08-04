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
| 工作流只停留在提示词里 | 自动注入固定版本 Matt Pocock 工作流、调用权限和 MCP 衔接。 |

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

基础安装和 `dev` extra 不包含进程内本地模型运行时，云服务器可保持轻量。
只有明确需要 `sentence-transformers` 本地 Embedding Provider 时才安装：

```bash
pip install -e ".[dev,local-inference]"
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

# Streamable HTTP 模式（共享 MCP Server，端口 9020）
python -m plastic_promise --streamable-http 9020

# 旧脚本兼容别名，仍可用
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

`/health` 同时是部署身份契约。响应包含 `pid`、`source_root`、
`source_revision`、`fusion_policy` 和 `fusion_attestation`；其中 attestation
包含 `schema=retrieval-fusion-identity/v1`、请求策略、候选 ID 和配置哈希。
启动器接纳新进程前会核对 health PID 与刚启动的 PID、当前源码根，以及可用时
的预期 Git revision；复用已占用 9020 的进程时也必须通过源码根/revision 校验，
仅返回 HTTP 200 不能证明进程归属。

Windows 上执行 `python scripts/init_and_start.py --stop` 时，只读取当前工作树的
`var/run/mcp_server.pid` 和 `var/run/maintenance_daemon.pid`，并再次核对进程命令行
中的 source root；不会扫描或终止其他 Python 进程及其他工作树。

连接排查时请用 `/health` 判断服务是否存活；`/mcp` 是 Streamable HTTP MCP 协议端点，浏览器直接 GET 或普通探针访问可能出现 404，这不等价于 MCP 断线。Windows 客户端关闭长连接时的 Proactor 断连 traceback 会在服务端过滤。真实端到端验证使用 `python scripts/smoke_http_mcp.py --expected-version <version> --timeout 60 --sse-read-timeout 360 --json`。本地默认启动会设置 `EMBEDDER_TIMEOUT=30`，避免 Ollama 冷启动或首次 embedding 请求在 5 秒默认值下误判失败；已有环境变量会被保留。重启 MCP Server 后，部分 Codex 会话不会热重载动态 MCP 工具句柄，需要刷新/重开会话让工具表重新注册。

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

Claude Code 项目级 `.mcp.json` 示例：

```json
{
  "mcpServers": {
    "plastic-promise": {
      "type": "http",
      "url": "http://127.0.0.1:9020/mcp"
    }
  }
}
```

现代共享 MCP 客户端连接：

```text
http://127.0.0.1:9020/mcp
```

旧 SSE 客户端仍可连接：

```text
http://127.0.0.1:9020/sse
```

### 中文运维控制台

设置 `PP_DASHBOARD_V2=1` 后，可直接打开：

```text
http://127.0.0.1:9020/dashboard
```

Dashboard V2 是仅限本机回环地址、按项目隔离、只读且有界的运维界面。本机运维者可从
服务器 SQLite 活动中发现并选择项目，但每次请求仍只绑定一个项目；该选择器不是远程
多租户鉴权边界。界面包含总览、
记忆、请求、综合记忆、记忆谱系、检索解释、运行操作、信任问题和配置。记忆详情会
显示 `structure-v1` 结构化切片的标题路径、块类型、父记忆、source span、内容哈希和
截断状态；谱系页显示类型化节点、有向关系以及来源/目标切片锚点。

设置 `PP_RETRIEVAL_EXPLAIN=1` 后，可查看词法、向量、图通道分数、排序/过滤原因、
切片证据和实际请求/阶段耗时。没有计时证据时界面显示“暂无数据”，不会伪造 `0 ms`。

## 核心能力

| 能力 | 说明 |
|---|---|
| 记忆质量管道 | 对经验、事实、决策、实体、事件、模式进行提取、分类、去重、门控、嵌入和衰减。 |
| 上下文供给 | `context_supply` 根据当前任务生成核心、关联、发散三层上下文，并返回推荐原因与 project/global 来源标记。 |
| 审计与防线 | `audit_pre_check`、`audit_run`、`defense` 在写操作和风险动作前提供检查；`defense(action="evaluate_tool")` 可解释工具语义决策。 |
| 信任分驱动自治 | 信任分越高，自主权越大；信任分下降时需要更多显式确认。 |
| Hunter Guild 委托系统 | 通过 `task_enqueue -> task_claim -> task_complete -> task_verify` 管理多 Agent 协作。 |
| Skills / 治理工作流 | `session-init`、`smart-remember`、`step-closure` 和官方工作流兼容入口 `sp-stage` 把调用链变成可追踪工具。 |
| Maintenance Daemon | 执行扫描、恢复、GC、任务生命周期维护和调度健康检查。 |
| P1 治理运行时 | 工具清单图、`runtime_events`、`mgp_shadow_bridge`、Context Recommender 为审计和推荐提供可解释元数据。 |
| 插件与市场 | 通过 pack 元数据加载知识、工作流、能力和适配器扩展。 |

长文本嵌入默认仍使用兼容的 legacy 切片。设置 `PP_MEMORY_CHUNKING=shadow` 时，正式向量请求和索引身份不变，只生成结构感知候选诊断；设置 `PP_MEMORY_CHUNKING=structure-v1` 才启用标题路径、段落、代码块、列表和表格感知的嵌入输入，并在有界请求预算内保留尾部。超过 `EMBEDDER_STRUCTURE_MAX_CHUNKS` 时会保留开头和尾部，并标记中间覆盖受资源限制。shadow 报告是只读观测，不调用 embedding 模型、不写 SQLite/LanceDB，也不能单独作为召回质量结论：

```powershell
python scripts/benchmark_chunking_shadow.py --source data/db/plastic_memory.db
python scripts/benchmark_chunking_shadow.py --source tests/fixtures/recall_quality/v1.json
```

在 `structure-v1` 产生权威切片后，可选的本地语义富化层只生成派生索引元数据，不修改 chunk 正文、顺序、标题路径或 source span。`PP_MEMORY_CHUNK_ENRICHMENT=shadow` 使用有界 daemon 队列调用本地 Ollama `qwen3:8b`，正式向量和索引身份不变；验证通过的结果写入默认位于主数据库旁的内容寻址 SQLite 缓存。`PP_MEMORY_CHUNK_ENRICHMENT=on` 应先在离线窗口完成索引重建或迁移，之后保持开启以便新写入和索引修复同步生成同一份精确 embedding plan；查询 embedding 永远不会调用富化模型。摘要、关键词、实体和标识符通过验证后才会前置到 embedding 输入，同时模型、提示词和 schema 版本会绑定到索引身份。需要可复现部署时可设置 `PP_MEMORY_CHUNK_ENRICHMENT_MODEL_DIGEST` 固定 Ollama digest，否则从 `/api/tags` 解析。

本地调用固定 `think=false`、`temperature=0` 并请求严格 JSON Schema，但不会信任 schema 本身。未知或缺失字段、摘要/证据/关键词/实体无法逐字回指原文、标识符不一致、JSON 截断、超时和模型不可用都会 fail-closed 回退原 chunk。默认模式仍为 `off`，且没有同时启用 `PP_MEMORY_CHUNKING=structure-v1` 时富化不会生效。

当前 `plastic_promise/mcp/server.py` 暴露 58 个 MCP 工具，包含 `session_init` / `sp_stage` 等兼容别名。`mgp_shadow_bridge` 是 MGP 兼容语义桥；P1 阶段只做 off/shadow/inject 模式管理与审计映射，不直接改写长期记忆。

`sp-stage` 仅为已有客户端保留名称；可注册阶段和 route 只来自固定版本 `mattpocock/skills@ed37663cc5fbef691ddfecd080dff42f7e7e350d`。`UserPromptSubmit` Hook 会自动注入官方 flow、完整主链、声明分支、当前/下一阶段、`[user]`/`[model]` 调用权限，以及必须由 `session-init` 和后续 `sp-stage` 原样复用的 project/session/flow ID。canonical memory、临时 proposal 与 route 共用一个严格总字符预算：可以省略完整可选区块，但不会输出截断的 XML-like 合同，默认预算优先保留带 scope 的 route 调用。每个显式 `/skill-name` 都会选择以对应官方 Skill 为首节点的合法 route；自然语言 Skill 短语只有在句首表达正向命令时才生成 user attestation，疑问、否定、状态陈述和引用仍按任务类型路由。`implement` 与 `grill-me` 是复合 Skill，其内部测试/审查或提问循环不会重复成为外层游标阶段；复合 receipt 必须通过 `evidence.invoked_skills` 声明实际内部调用，服务端据此创建 `tracking_basis=composite_receipt` 的确定性 entity-only 子链，而不会伪装成独立 Hook 观测。`small-build` 与 `prototype-detour` 都共享父链 `grill-with-docs` 分支点；未声明的 route 切换仍会被拒绝。无 `execution_receipt` 的首次调用只返回固定 revision/hash 的执行合同，不运行 Codex Skill、不推进游标；客户端实际运行对应 Skill 后，再提交包含 Skill 名、上游 revision、`SKILL.md` SHA256、completed 状态和无 secret JSON evidence 的 caller attestation，服务端不能以密码学方式证明客户端已运行 Skill。治理适配器成功后，receipt 与 cursor 按 project/session/flow scope 在 SQLite 中原子提交；receipt-scoped 确定性 tracking ID 保证提交窗口崩溃后的重试不会重复生成实体。相同 receipt 重放是幂等的，同一 scope/route step 的不同物料会被拒绝。`skill_auto_track` 仅是外部客户端显式兼容入口，不能推进官方游标；Codex 当前不存在自动 `PreToolUse`/`PostToolUse` Skill 追踪。生产只允许一个 MCP 写入进程连接同一 SQLite；进程内锁不是分布式 exactly-once 租约。

重型 `memory_recall` / `context_supply` 调用可携带 `stage_session_id`、`flow_line_id` 和 `request_id`。系统会派生 `request_scope_id`，写入审计元数据并显示在 `context_supply` 输出中，同时用它隔离重叠官方工作流阶段或多 Agent 流程中的召回缓存。

`runtime_events` 会记录工具调用和 Hunter Guild 任务流转的 `pending`、`running`、`completed`、`error` 状态，并携带 request scope、trust tier、defense decision 和 audit trace，方便回放与审计。

在 `rust-full` 模式下，`memory_recall(debug=true)` 在 Rust 健康且优先时仍走 Rust snapshot 热路径，并返回 Rust `pipeline_stats` / `per_item_stats`；只有 Rust 不可用或异常时才回退 Python。当 LanceDB 中已有向量行时，debug `pipeline_stats` 应显示非零 `vector_count`；只有查询没有向量命中时，`vector_hits` 才可能为 0。

### 受治理的综合记忆与提案

综合记忆默认关闭并采用 fail-closed 策略。SQLite 保存普通记忆、综合记忆生命周期、来源快照、提案审核和精确索引材料的权威状态；LanceDB 只是可重建的派生索引。

| 开关 | 默认值 | 启用后的行为 |
|---|---|---|
| `PP_SYNTHESIS_ARTIFACTS` | `off` | `shadow` 只评估资格，`on` 才允许创建受治理草稿。 |
| `PP_SYNTHESIS_RETRIEVAL` | `0` | `1` 只接纳证据完整且仍为当前版本的 `verified` 综合记忆。 |
| `PP_MEMORY_PROPOSALS` | `off` | `shadow` 只输出哈希诊断，`on` 将公开事实、偏好和决策送审。 |
| `PP_MEMORY_INDEX_TEXT_POLICY` | `legacy` | `compact-v2` 是有界 L0/L1 索引文本的实验候选。 |

生命周期为 `draft -> verified -> stale|contested`。刷新会创建新的 `draft` 修订版，必须重新记录审核者、调用 ID 和时间后才能进入召回。待审、拒绝和过期提案不会成为普通召回候选，也不会写入 LanceDB。

普通记忆中会影响召回的内容或可用性变更统一经过字段级权威事务。事务在提交前同时记录来源 lineage、将依赖的综合记忆标为 `stale`、递增权威记忆版本并写入带校验的索引任务。GC 合并要求候选项目非空且一致，并在事务内再次核对源记录和目标记录的权威项目；不一致时不会留下记忆、lineage、版本、outbox 或缓存的部分变更。

公开写操作的 actor、call、project 和 trust 证据由服务端运行时上下文提供，调用者声明的同名字段只用于审计。两个 `smart-remember` 别名在读取或修改已有记录前都必须取得 `memory_update` 权限。公开 `memory_forget` 仍是信任阈值 `0.80` 的关键操作；信任阈值 `0.60` 的 `audit_rollover` 只供内部审计轮转使用。

融合策略默认是 `legacy-auto`，`max-v1` 是固定比较基线；候选加权策略使用 `wrrf-v1:<sha256>` 不可变 ID，并必须与冻结 manifest 一致。未知、未带哈希、manifest 不匹配或配置非法时会 fail closed。校准阶段只读取 held-out 文件字节生成指纹，在 manifest 冻结前不会加载或查询 held-out case。

`0.1.15` 的一次性公开校准没有产生合格的 WRRF 候选，因此 held-out case 保持未开启，发行策略继续使用 `legacy-auto`，不声明已取得融合质量提升。

维护与恢复可通过生产同构的一次性入口验证：

```bash
python daemons/maintenance_daemon.py --once --json
python scripts/smoke_restart_recovery.py --artifact-dir .artifacts/recovery-smoke --json
```

`maintenance-heartbeat/v1` 将心跳绑定到 daemon PID；旧心跳仅保留 mtime 兼容。索引重放继续读取既有合法 `memory-index/v2` upsert，但所有新 upsert/delete 都写为带 action、project、memory version、material revision 和 expected embedding hash 的 `memory-index/v3`。

升级到 `0.1.20` 时，应先保持治理综合记忆、提案、Dashboard、检索解释、云推理、被动语义捕获与自动晋升开关为默认值，同时重启所有已启用的写入服务，避免不同版本的进程混用事务和派生工作约定。公开 MCP 工具和参数没有删除；已有 SQLite 记忆继续作为权威数据，LanceDB 可由持久化校验任务修复。LanceDB 最低版本仍为 `0.34.0`，固定在更早版本的环境必须先升级依赖。重启后按需逐项启用功能，再执行：

```bash
python scripts/smoke_http_mcp.py --expected-version 0.1.20 --expected-mode rust-full
```

回滚时关闭四个开关即可，不要删除 SQLite 中的控制、来源、提案、lineage 或审计记录：

```bash
PP_SYNTHESIS_RETRIEVAL=0
PP_SYNTHESIS_ARTIFACTS=off
PP_MEMORY_PROPOSALS=off
PP_MEMORY_INDEX_TEXT_POLICY=legacy
PP_RETRIEVAL_FUSION_POLICY=legacy-auto
```

同时取消 `PP_RETRIEVAL_RRF_K`、`PP_RETRIEVAL_RRF_WEIGHTS_JSON`、`PP_RETRIEVAL_RRF_WINDOWS_JSON`。保留 SQLite、来源与 outbox 数据，重启两类进程，运行 one-shot maintenance 重放默认索引策略，再执行 HTTP 与 restart-recovery smoke。

切换 `PP_MEMORY_CHUNKING` 后，应在放量前重建派生 LanceDB；回滚到 `off` 后也要再次重建，确保不会混用不同切片身份的向量：

```powershell
$env:PP_MEMORY_CHUNKING = "structure-v1"
python scripts/rebuild_lancedb.py
$env:PP_MEMORY_CHUNKING = "off"
python scripts/rebuild_lancedb.py
```

语义富化建议先 shadow 验证，再在离线窗口启用 on 并重建派生索引；重建完成后保持 on，使在线写入和修复沿用同一索引身份：

```powershell
$env:PP_MEMORY_CHUNKING = "structure-v1"
$env:PP_MEMORY_CHUNK_ENRICHMENT = "shadow"
# 用代表性写入或回填预热并检查富化诊断/缓存。

$env:PP_MEMORY_CHUNK_ENRICHMENT = "on"
python scripts/rebuild_lancedb.py

# 回滚保留 SQLite 权威正文；关闭富化后必须重建派生索引以恢复 legacy 身份。
$env:PP_MEMORY_CHUNK_ENRICHMENT = "off"
python scripts/rebuild_lancedb.py
```

## 架构概览

<p align="center">
  <img src="architecture/plastic-promise-flow.zh-CN.svg" alt="Plastic Promise 本地治理运行时架构" width="960">
</p>

上方矢量图把运行时分成五层：参与者、MCP 入口、治理核心、自动化闭环、本地持久化与加速。README 中保留的是一眼可读的总览；更细的 C4、时序和组件图仍放在架构目录中。

### C4 部署视图

标准发行版共用一套模块，只改变客户端与 Runtime 的部署位置。
`local-all-in-one` 中上下两个框位于同一台本机；默认 `split-async` 中，
上框位于客户端，下框位于通过安全本地隧道访问的服务器。

```text
+-------------------------- 客户端主机 --------------------------+
| Codex / MCP Client | 仪表盘 | 可选有界本地缓存               |
+------------------------------+---------------------------------+
                               | loopback HTTP 或 SSH LocalForward
                               v
+-------------------------- Runtime 主机 ------------------------+
| MCP Gateway | 治理核心 | 异步控制面                           |
| Context Engine | Memory Pipeline | Maintenance Daemon          |
+----------------------+--------------------+---------------------+
                       |                    |
                       v                    v
             +----------------+    +----------------+
             | SQLite WAL     |    | LanceDB        |
             | canonical 真相 |    | 派生索引       |
             +----------------+    +----------------+
```

<p align="center">
  <img src="architecture/distribution-profiles.zh-CN.svg" alt="Plastic Promise 全本地与前后端分离异步发行部署 profile" width="960">
</p>

<details>
<summary>查看信息图生成说明</summary>

```text
画布：1280 x 760，深色高对比架构信息图。
目标：对比同一发行契约下的两种部署 profile。

分区：
1. 标题：Plastic Promise 发行部署 Profile。
2. 全本地：客户端、仪表盘、MCP Worker、SQLite 真相源、LanceDB 索引。
3. 前后端分离：有界客户端缓存、安全隧道、服务器 Runtime 与状态。
4. 异步链：canonical enqueue => durable outbox ~> bounded batch => retry/reconcile。

约束：SQLite 是 canonical；LanceDB 是派生索引；客户端缓存不是可写真相源；
部署只改变模块位置，不改变模块所有权。
```

</details>

### 持久异步时序

```text
客户端/Hook => MCP Gateway       : 提交捕获或派生工作请求
MCP Gateway => SQLite transaction: 持久化 canonical intent + outbox
SQLite      => MCP Gateway       : commit + request_id
MCP Gateway => 客户端/Hook       : 持久准入后返回 accepted
Maintenance ~> SQLite            : 认领有界、按项目隔离的批次
Maintenance => Provider Adapter  : embedding / 富化 / rerank
Provider    => Maintenance       : 返回结果或显式失败
Maintenance => SQLite + LanceDB  : 提交任务状态并更新派生索引
Reconcile   ~> SQLite            : 重试未完成工作，不混合不同项目
```

客户端缓存永远不能成为第二个可写真相源。SQLite 保存 canonical 记忆与治理状态，
LanceDB 始终是可重建派生索引。

更多架构文档：

- [SYSTEM_FULL_CHAIN.md](SYSTEM_FULL_CHAIN.md)
- [architecture/architecture.md](architecture/architecture.md)
- [architecture/plastic-promise-flow.svg](architecture/plastic-promise-flow.svg)
- [architecture/plastic-promise-flow.zh-CN.svg](architecture/plastic-promise-flow.zh-CN.svg)
- [architecture/distribution-profiles.svg](architecture/distribution-profiles.svg)
- [architecture/distribution-profiles.zh-CN.svg](architecture/distribution-profiles.zh-CN.svg)
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

### 云 embedding、重排与切片分析

云 Provider 默认关闭。评审完成前保持 `EMBEDDER_PROVIDER=ollama`、
`PP_RERANK_PROVIDERS=ollama,cosine` 和
`PP_MEMORY_CHUNK_ENRICHMENT=off`。托管调用统一经过 OpenAI-compatible
传输层，具备输入/输出大小限制、重试、deadline、熔断、响应校验、内容
hash 缓存和脱敏诊断。API Key 只能放在权限为 `600` 的环境文件或交互式
密钥存储中，不能提交、写进命令行或日志。

远程配置中的 `embedding.cost_per_million_tokens`、`cost_currency` 和
`pricing_revision` 是一组必须同时填写的费用证据，当前支持 `USD` 与 `CNY`。价格变化
不会改变向量内容，因此不参与 embedding/index identity；但正式 generation 的质量门
仍要求非空单价、币种和可追溯的价格版本。Dashboard 推荐模板故意保留空值，操作员必须
按模型广场的当前价格填写，不能用零值冒充未知价格。

云优先服务器配置不安装、不启动 Ollama。只有在 Key 完成轮换并
通过受保护的服务器环境文件配置后，才启用托管 embedding、托管 rerank 和托管结构化
分析。本地 loopback Provider 传输只保留为后续部署的兼容代码，不作为云优先配置的
健康或验收条件。

必须配置 API 根地址，不能把文档站当 API。`https://wiki.syuan.org/` 会被
主动拒绝；应从服务商文档获得真正的 `/v1` API 根地址，再配置
`EMBEDDER_BASE_URL`、`PP_RERANK_BASE_URL` 或 `PP_INFERENCE_BASE_URL`。
地址可用不代表认证和模型权限可用，运行时只报告 provider、model、revision、
dimension、有限 usage 和安全 reason，不会把失败伪装成成功。

部分固定原生维度的 OpenAI-compatible embedding API 会拒绝可选的
`dimensions` 请求字段。只有在独立验证原生输出与 `PP_EMBEDDING_DIM` 完全一致后，
才可设置 `EMBEDDER_SEND_DIMENSIONS=0`；响应维度仍会严格校验，且 native 请求模式
会写入派生索引身份。

把凭据写入服务器环境文件前，先用合成数据探测候选 Provider：

```bash
python scripts/smoke_cloud_providers.py
```

该 smoke 只通过隐藏提示读取 Key，不输出凭据、向量或原文。`--keys-from-stdin`
仅用于受保护的交互管道；脚本刻意不接受命令行 Key 参数或环境变量 Key。

后端输入契约与 Provider 解耦。前端只提交规范化的 `id`、`text`、`base_score`，
`embedding` 可以省略；后端仅对缺失项批量调用当前配置的云或本地 embedder。
前端提供的向量只有在维度、有限非零值、完整 embedding 身份和原文 SHA-256
全部匹配时，才允许在本次请求中复用。这只是结构和声明校验，并不能以密码学方式
证明该向量真的由所声明的模型生成；因此它不会获得正式 LanceDB 写入权限，正式
索引物料仍由后端生成。Provider、模型、base URL、path 和 Key 都属于后端配置，
输入 DTO 会拒绝这些字段。

结构化 JSON 分析对 OpenAI-compatible 云端和 loopback-only Ollama 使用同一份
Mapping 输入。本地传输不需要 Key，不读取系统代理、拒绝重定向，并受总超时和
响应大小限制。DeepSeek 官方默认使用 `https://api.deepseek.com` 与
`deepseek-v4-flash`；后端为确定性 JSON 分析显式关闭 thinking，业务 schema 仍在
本地严格校验。

同步 rerank 作为无状态请求本身不会造成多设备冲突，危险发生在旧结果覆盖新状态。
后端结果会绑定 project、query、candidate set、embedding 物料、Provider policy 和
scoring 的版本哈希，客户端必须拒绝 stale 结果。`project_id` 必须由鉴权后的 gateway
派生，不能信任前端字段。后端可在调用 Provider 前生成纯请求绑定；若改为异步任务，
应持久化唯一 `(project_id, idempotency_key)`：同一 input hash 返回已有任务，不同
input hash 返回冲突。多 worker 仍需原子 claim、lease 和 CAS 完成；进程内缓存不能
冒充 durable job queue。异步包装只负责避免阻塞事件循环，不能代替持久幂等。云与
Ollama 回退链分别使用
`PP_RERANK_CLOUD_MODEL` 和 `PP_RERANK_OLLAMA_MODEL`。

前端可以发起 rerank，但不能提交权威最终排序或 Provider 凭据；只有当前 project 与
candidate-set version 仍匹配时，前端才应用返回结果。否则即使不破坏状态，也可能因
重复请求产生重复云费用。

后续若在前端设备运行本地模型，创建请求必须选择不可变模型身份（建议包含 revision 或
digest），后端会把它同时绑定到幂等请求 hash 与 `client-local-rerank/v2` 数据包。数据包
包含精确 query、候选文本、base score、原文 hash 和向量 hash，但不包含向量、Provider
配置或凭据。执行器配置的模型身份必须与数据包完全一致，否则在调用模型前拒绝任务；
同一幂等键更换模型也返回冲突。前端返回值只携带 package hash、已绑定的模型身份和候选分数。后端仅在鉴权
project、当前 request ID、query、`top_k`、candidate-set version/hash、embedding 身份和
维度都与服务端状态一致时接收；该结果只在本次请求内有效，不能写入 LanceDB。异步或
多设备 gateway 必须把权威 package 保存到按 project 隔离的 durable job，并用 CAS 完成
状态保证第一个合法结果胜出。不得根据客户端回传重建 package；无状态方案必须改用
服务端签名或 HMAC。该身份绑定解决并发一致性，不冒充远程模型证明；客户端结果仍然只在
当前请求内有效且非权威。核心纯校验器本身不冒充这些持久并发保证。

这里新增的是后端核心边界，不是未鉴权公网接口。现有 MCP/Dashboard Starlette
进程继续只监听 loopback；未来公网 API 必须由独立的鉴权、项目隔离 gateway 提供，
不能直接暴露完整 MCP 路由面。在该 gateway 完成前，这些前端 DTO 是核心集成契约，
并不是浏览器可直接调用的 API。

切片语义富化也是显式 opt-in。`PP_MEMORY_CHUNKING=structure-v1` 下先使用
`PP_MEMORY_CHUNK_ENRICHMENT=shadow`，它只运行有界队列而不改变正式向量；
通过 shadow 证据评审后，才在离线窗口设置 `on` 并重建派生索引。后续写入和
修复必须保持相同的 Provider/model/prompt/schema 身份；查询 embedding 永远
不会调用富化模型。

### 远程配置控制面

服务器状态和云配置由独立的无界面控制 API 提供，只监听 `127.0.0.1:9040`；Mac
通过 `LocalForward 19040 127.0.0.1:9040` 访问。9040 不得进入公网安全组、UFW
放行规则或公共反向代理，也不挂载到 MCP 9020 或推理网关 9030。控制面使用
`viewer < operator < secret-admin` 分级 Token；Provider Key 只能写入或清除，
任何 safe config、revision、审计或浏览器存储都不返回明文。

Dashboard V2 是唯一的运维前端：记忆、检索和运行证据继续来自 9020，服务器状态、
desired config、修订和审计由浏览器经 19040 直接调用 9040。控制 Token 只保留在
浏览器内存，不经过 MCP。产品前端只能使用更窄的 inference gateway 合同，不能
获得控制面 Token。

普通变更使用 `GET safe config/ETag -> validate -> immutable stage -> If-Match
CAS activate -> 主机运维重启与 smoke`。所有 POST 都需要 JSON 和当前
`If-Match`；只有 `stage` 与 `activate` 还需要按操作稳定的 `Idempotency-Key`，
同一物料重试时复用，物料变化后更换。`activate` 只原子选择权限为 `0600` 的
`managed.env` 并返回
`restart_required=true`，不会调用 sudo、systemctl、Provider 或 LanceDB rebuild。
ETag 是随机的 256-bit 不透明 CAS Token，不由环境内容或 Provider Key 派生；只有
Embedding 身份变化时才接受 generation evidence，其他激活请求携带 evidence 会被拒绝。
数据库和 generation 路径、监听地址、控制面认证、systemd/Maintenance 策略、
gateway 身份与 Provider host allowlist 仍是 bootstrap-only。每个 revision 会私下
绑定进程可见的 bootstrap EnvironmentFile 指纹；激活和崩溃恢复发现漂移即拒绝，
该 secret-dependent 指纹不会通过 API 或对象表示返回。

Embedding 精确运行索引身份变化（包括 structure-v1 切片预算）必须先完成 Provider smoke、shadow rebuild、reconciliation、
固定中英文质量门和 `verify-candidate`。候选验证成功后仍保持 inactive；主机运维
随后停止 MCP/推理 worker、激活 revision 记录 desired generation、在相同
`managed.env` 下 promote 完整匹配的 generation，再重启并执行 health/retrieval
smoke。`/status` 同时显示 desired 与 current 的 generation ID 和完整 manifest
SHA-256；两者任一不一致都表示尚未完成切换。完整 API、systemd、SSH 隧道和验收
说明见 [Remote Configuration Control Plane](remote-control-plane.md)。

### 不可变 LanceDB generation

SQLite 是唯一真相源，LanceDB 是可重建投影。更换云模型必须从 SQLite
Backup API 快照构建 inactive shadow generation，不能复制运行中的数据库
WAL，也不能原地覆盖 current index。构建会记录源指纹、embedding 身份、质量
证据以及 index-outbox watermark/digest。评审 watermark 后，必须对同一个
SQLite 数据库显式执行 reconciliation，才能 promote：

```bash
python scripts/rebuild_lancedb.py \
  --generation-root data/lancedb-generations \
  --generation-id candidate-<utc> \
  --source-db data/db/plastic_memory.db \
  --quality-report path/to/publishable-quality-report.json \
  --candidate-manifest path/to/frozen-candidate-manifest.json

python scripts/manage_lancedb_generations.py \
  --root data/lancedb-generations reconcile candidate-<utc> \
  --db data/db/plastic_memory.db
python scripts/manage_lancedb_generations.py \
  --root data/lancedb-generations verify-candidate candidate-<utc> \
  --db data/db/plastic_memory.db \
  --embedding-index-identity '<与 staged revision 完全一致的索引身份>'

# 停止服务并激活匹配 revision 后，加载其 managed EnvironmentFile 再 promote。
python scripts/manage_lancedb_generations.py \
  --root data/lancedb-generations promote candidate-<utc> \
  --db data/db/plastic_memory.db
```

`reconcile` 会写 SQLite：只把快照覆盖的索引任务标记为 done 并保存 receipt。
出现更新任务、processing 任务、缺少不可变 outbox 列、WAL 变化或 receipt/数据库
不匹配时必须 fail closed。`verify-candidate` 会重新核验 artifact、质量报告、
SQLite freshness、embedding 身份和 staged runtime environment，但不会移动
`current`。生产顺序固定为 `verify-candidate -> stop -> activate desired state ->
promote -> restart/smoke`。`promote` 与 `rollback` 要求 generation 已验证且
已 reconciliation，并加载目标 MCP 的 EnvironmentFile；运行时以只读方式打开
所选索引，除非显式配置了与 generation 绑定的可写 live view。

需要让新的 checked `memory_index` / `synthesis_index` outbox 任务实时进入派生索引时，
从已验证的 current generation 创建私有 live root。current manifest 必须包含已完成
reconciliation 且能在数据库中验证 receipt 的 outbox 证据。先创建私有父目录，目标路径
本身必须不存在：

```bash
python scripts/manage_generation_live_index.py \
  --live-root data/lancedb-live/generation-<utc> \
  bootstrap --generation-root data/lancedb-generations
python scripts/manage_generation_live_index.py \
  --live-root data/lancedb-live/generation-<utc> \
  verify --generation-root data/lancedb-generations
```

重启 MCP 前，在 bootstrap EnvironmentFile 中同时设置
`PLASTIC_LANCEDB_GENERATION_ROOT` 和 `PLASTIC_LANCEDB_LIVE_ROOT`。Python 与 Rust
读取同一个 live index；runtime refresh 只报告有界 outbox lag，不再对 live view
执行全量 `sync_with_engine()`。Maintenance 只允许把 watermark 之后通过检查的 outbox
任务增量回放到副本，immutable generation 保持不变。每次 promotion 或 rollback 都会
创建并永久保留一个一次性的 `selections/<activation-id>` 链接，再原子切换 `current`；
live binding 包含该 activation ID，因此 A -> B -> A 回滚也不会让旧 A live root 重新
生效。selection 链接不得删除或复用，每次激活都必须创建新的 live root；清理旧 live
root 是需要单独授权的运维操作。旧式 `current -> generations/<id>` 仍可只读解析，但在
显式 promotion 或 rollback 创建 activation link 前不能作为 live view 的基线。

无参数的旧版
`rebuild_lancedb.py` 和 `smoke_http_mcp.py` 都可能写入索引、smoke 记忆或
outbox；除非明确要这些副作用，否则不要对生产数据库运行。

### 多 Agent 可追踪协作

Hunter Guild 把任务发布、认领、心跳、完成、验收变成可追踪状态机，避免多 Agent 工作变成不可审计的提示词堆叠。

## 配置要点

| 项 | 默认 |
|---|---|
| Streamable HTTP 端口 | `9020`，默认端点 `/mcp` |
| MCP 入口 | `python -m plastic_promise` |
| 一键启动 | `python scripts/init_and_start.py` |
| 启动模式 | `light`、`normal`、`rust-normal`、`full`、`rust-full`，非交互默认 `rust-full` |
| 守护进程 | `daemons/maintenance_daemon.py` |
| 远程配置 | 独立回环 `127.0.0.1:9040`；Mac SSH forward `19040`；只写 desired state，重启和 generation promote 由主机运维执行 |
| 默认 embedding | Ollama `mxbai-embed-large`，长文本切块池化，可降级 fallback embedder |
| 默认 embedding 超时 | `EMBEDDER_TIMEOUT=30`，可用环境变量覆盖 |
| 可选切片富化 | 默认关闭；本地 Ollama `qwen3:8b`、严格来源校验、SQLite 缓存；先离线重建启用 `on`，服务期间保持 `on` |
| Dashboard V2 | `PP_DASHBOARD_V2=1`；中文、仅本机、按项目隔离、只读，入口 `/dashboard` |
| 检索解释 | `PP_RETRIEVAL_EXPLAIN=1`；保存有界快照并显示真实请求/阶段耗时，不生成虚假零耗时 |
| 默认 reranker | Ollama `qwen2.5:3b`，失败时回退 cosine/original 排序 |
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

### 标准发行版变体

`release/variants/standard.json` 是 Plastic Promise 标准发行版的版本化契约，
描述公开能力、支持平台与运行模式、SQLite/LanceDB 的真相源与派生索引角色、
配置名称、禁止进入发行版的运行状态、构建制品和发布证明门禁。它是发行版变体，
不是独立的知识库版本。

同一份标准发行契约支持两种部署 profile：

- `local-all-in-one`：前端、MCP Runtime、SQLite、LanceDB 和异步 worker 全部
  运行在一台本地机器上，只通过 loopback HTTP 连接。
- `split-async`（默认）：客户端承载 Codex/仪表盘访问和可选有界缓存，服务器独占
  可写 SQLite、LanceDB 与异步 worker，客户端通过安全隧道访问。

两种 profile 共用同一异步准入契约：canonical enqueue 成功后才确认请求，后台使用
durable outbox、有限批处理、持久重试状态和 reconcile，并强制项目隔离。分离模式的
客户端缓存不得包含可写 canonical database。

该配置只记录环境变量名称，不记录秘密值。密码、Token、私钥、数据库、派生索引、
日志、备份和生产 EnvironmentFile 均禁止进入发行仓库。可在本地执行：

```bash
python scripts/validate_release_variant.py release/variants/standard.json --repo-root .
```

`release-sync.py` 会在编译和测试前执行同一套 fail-closed 校验，且
`release/variants/` 已纳入公开同步白名单。

```bash
pip install -e ".[dev]"
pytest
ruff check plastic_promise/
```

仅在开发或测试进程内本地 Embedding Provider 时使用
`pip install -e ".[dev,local-inference]"`；云服务开发和服务器部署保持 `dev` Profile。

发行版 live sync 会先确认发行仓库工作树干净、位于 `main`、`origin` 与预期一致，
且当前版本 tag 在本地和远端均不存在。校验完成后只暂存计算得到的发行路径；任何
额外 staged、unstaged 或 untracked 路径都会阻止发行。先执行 dry-run；第一次且唯一
一次 live 调用必须带 `--push`；不带 `--push` 的 live 调用会被拒绝。push 路径还必须使用
`--validation-profile full`，并提供绑定精确版本与源码 HEAD 的有界
`--release-evidence` JSON。该无自由文本、无秘密字段的维护者证明必须确认：自动审计分数
至少为 `0.60`、blocking/major 均为零，且高风险审查、秘密扫描、限定范围 Ruff、JavaScript 语法、
live HTTP、重启恢复、diff check 与 release-sync preview 全部通过。该进程会创建 commit
与 annotated tag，重新校验固定的 commit/tag 对象和远端状态，再原子推送 `main` 与
精确 tag。不要执行不带 `--push` 的 live 调用，也不要改用手工 push 或
`git push --tags` 绕过发行证明。

```bash
python scripts/release-sync.py --from <base>..<merged> --audit-range <base>..<merged> \
  --version v0.1.20 --release-repo ../plastic-promise-release \
  --expected-source-branch main \
  --expected-source-origin https://github.com/ALdaisuki/plastic-promise.git \
  --expected-origin https://github.com/ALdaisuki/plastic-promise-release.git \
  --validation-profile full --dry-run
# 全部门禁通过后，以相同参数改用 --push，并添加：
#   --release-evidence <path-to-release-evidence.json>
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
