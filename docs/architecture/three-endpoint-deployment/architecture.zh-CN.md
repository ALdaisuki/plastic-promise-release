# Plastic Promise 三端部署架构

> 状态：截至 2026-08-17 的分阶段架构。本文记录截至 PR 5 migration seam 的当前源码合同与
> 目标运行态部署工作；不表示生产环境已经完成迁移、listener 已启动或任何外部运行态已验证。
> 当前 PR 5 协作证据仅属于源码/测试层；本文不把这些证据表述为真实运行时或生产验证。

> **规范范围：**本文从属于
> [`union-six-pr-contract.json`](../../standards/union-six-pr-contract.json) revision
> `2026-08-18.1`。只有每个 `delivery_scope`、`collaboration_scope` 与
> `required_evidence` 条目均通过，PR 才算完成；任何一半都不得被报告为整个 PR 已完成，
> source/test 证据也不等于 runtime/production 证据。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

英文对等文档：[`architecture.md`](architecture.md)。

## 状态图例

| 标签 | 在本文中的含义 | 证据 / 排除项 |
|---|---|---|
| **current** | 对应 stacked PR 工作树中已有的源码级契约。 | 不代表存在 listener、已构建/加载的 image、持久化 record 或生产 rollout。 |
| **legacy-current** | 既有 repository/runtime path 仍是兼容性基线。 | 不代表任何特定 host 健康或已连接。 |
| **target** | 明确交由 PR 3–PR 6 的工作。 | 不代表已安装、已发行或已运行。 |
| **unverified** | 本任务未证明外部 runtime 状态。 | 不能仅从源码合同推断任何 runtime 结论。 |

## 1. 系统概览

Plastic Promise 的目标拓扑由三个可独立放置的端模块构成。PR 2 提供纯源码、source-only
的 `EndpointAuthority` 深模块及其版本化 endpoint contract。该模块在服务器编译封闭的
`EndpointAuthorityProfile`，且只暴露一个小接口：`resolve` -> `assess` ->
`verify_completion`。源码调用方仍使用旧名时，`EndpointContractRegistry` 只作为兼容名称
保留。PR 3 新增 endpoint artifact 的源码级 build/inspection policy；PR 4 进一步提供
source-level `ProjectWorkingSet` 与 `AgentAwarenessProjection`。endpoint process、placement、
transport、persistence、server feed 与全部 runtime application 仍是后续工作。

| 端模块 | PR 2 合同边界 | Runtime 状态 |
|---|---|---|
| `pp-local-edge` | 只允许提交 intent 与读取有界 projection；没有 canonical、协作、治理、计算持久化或部署权威。PR 4 的 awareness 类型目前只是 repository source contract，尚未封装或接入 edge runtime。 | 位于 `http://127.0.0.1:19021` 的 **target** 静态浏览器入口、container start 与 placement；它不托管 MCP。 |
| `pp-server-backend` | canonical state、Task Queue、inference job 调度/验收、协作、memory/knowledge proposal 与治理的唯一 writer 和决策 owner，包括 SQLite、routing、retry、reconcile、accepted-result validation、receipt 与 LanceDB-promotion decision；不构造或执行 provider。 | **current** 的 authority/ownership resolution 与 server 调度/transport contract；**target** 的 mount、persistence、MCP/runtime operation 与 promotion。 |
| `pp-compute-node` | 只允许租用有界 inference job，并返回派生 `embedding`、`rerank` 与固定 schema `structured-json` result/evidence。CPU/CUDA 以及 local/cloud/hybrid variant 都位于同一 contract 之后。 | **current** 的 protocol/identity/admission/lease contract 与隔离 node-service 源码；**target** 的已验证 placement、runtime transport 与 production evidence。 |

三端按部署责任拆分，不把服务器内部实现拆成用户可见的微服务集合。未来纯本地安装
可以在同一主机运行三个独立 endpoint container；split installation 只改变 placement
和 transport，不会改变所有权。

目标成功标准：

- 只有 `pp-server-backend` 能挂载或写入 canonical SQLite；
- Dashboard 和 Deployment Center 使用回环上的 `pp-local-edge`
  `http://127.0.0.1:19021`；该浏览器入口只提供静态内容；
- 现有 `http://127.0.0.1:19020/mcp` 入口属于 `pp-server-backend`，而非
  `pp-local-edge`；
- `pp-compute-node` 只接收有界 inference lease 并返回派生 result/evidence；不能访问文件、
  Shell、MCP 管理或 canonical SQLite，也不能取得 Task Queue、`AgentRegistry`、work-board、
  collaboration-event-writer、awareness、memory/knowledge-promotion、merge、deployment、
  Maintenance 或 LanceDB-promotion 权威；
- inference compute job 与 developer-Agent 协作工作始终属于不同平面，即使二者携带相同
  `project_id`；
- 一个无 secret 的 Deployment Manifest 是部署唯一真相源；
- 本地和云端 provider 的 embedding identity 不能漂移；
- 可选推理失效时明确退化，不阻断 canonical 写入，也不伪装派生状态已更新；
- 项目级 Agent 协作与长期记忆相互隔离：peer progress 通过有界协作投影可见，
  但不会仅因另一个 Agent 上报就自动晋升；
- 部署、降级、重建、promotion 和回滚全过程可查。

PR 2 声明其中可由 pure endpoint authority contract 表示的部分：封闭 role/action profile、
server-only ownership、版本化 capability/identity evidence、admission、lease/fencing 和
脱敏 record schema。`project_id`、manifest/hello claim 与 advertised capability string 都只是
校验输入，任何一个都不能授予权威。PR 3
新增独立的 build-time contract：无 secret request 解析 role/platform/variant matrix，且只
物化为可检查的 artifact descriptor。PR 4 新增不可变、有界、按角色裁剪的协作读投影；
它不创建 adapter、database、listener、Hook/MCP binding 或 canonical-memory effect。
这些源码合同都不会强制 OS mount、启动 service、持久化 record 或执行 LanceDB promotion。
PR 2 尤其不会激活 runtime、transport、persistence 或 migration、deployment、Maintenance
或 promotion path。

## 2. 架构图

- [`diagrams/architecture.txt`](diagrams/architecture.txt)：不超过 100 列的
  紧凑 ASCII 部署图。
- [`diagrams/workflow.mermaid`](diagrams/workflow.mermaid)：部署、配置和推理
  路由流程。
- [`diagrams/sequence.mermaid`](diagrams/sequence.mermaid)：recall 退化与稳定
  恢复时序。
- [`diagrams/artifact-build.txt`](diagrams/artifact-build.txt) 与
  [`diagrams/artifact-build.mermaid`](diagrams/artifact-build.mermaid)：PR 3 的
  源码级 artifact-plan/materialization 边界，与 runtime authority 明确分离。
- [`diagrams/container-artifact-matrix.zh-CN.svg`](diagrams/container-artifact-matrix.zh-CN.svg)
  及其英文对等 SVG：可视化 artifact matrix；它们只是设计证据，不代表某主机上已有
  image。

## 3. 模块与接缝

三个端都是深模块：外部接口小，平台适配、恢复和治理复杂度隐藏在实现内部。PR 2 将
authority 接缝放在纯 `EndpointAuthority` 模块中，其接口只有 `resolve`、`assess` 与
`verify_completion`；role/action resolution、对 claim 的不信任以及 completion denial 都
隐藏在实现内部。源码调用方仍 import 旧名时，`EndpointContractRegistry` 只作为兼容名称。
PR 3
新增独立的 `ContainerArtifactCompiler` 接口：
`prepare(request) -> ArtifactBuildPlan` 以及
`materialize(plan, executor) -> ArtifactBundle`。Docker/Compose、HTTP/gRPC、SSH、
scheduling、persistence 与 deployment application 仍是 adapter concern，不是任一深模块
的 import。

为保持向后可读，`authorities_for()` 与浏览器 `authorities` 字段继续返回旧的描述性标签。
新的封闭、可执行矩阵单独通过 `EndpointAuthorityProfile.actions` 与新增浏览器 `actions`
字段暴露；调用方不得把描述性标签当成 grant。

| 模块 | 外部接口 | 隐藏的实现 | 不变量 |
|---|---|---|---|
| `EndpointAuthority` | **current source-only** 的 `resolve` -> `assess` -> `verify_completion`，返回服务器编译的 `EndpointAuthorityProfile` 与类型化 decision。 | 封闭 role/action matrix、ownership resolution、capability/identity comparison、lease/fence check 与 fail-closed rejection reason。 | `project_id`、manifest/hello claim 和 advertised capability 都不能授予权威；只有编译后的 profile 才能准入 action。 |
| `ContainerArtifactCompiler` | **current** 的 `prepare` / `materialize` source-level build seam。 | Matrix resolution、OCI policy、image-recipe selection、executor difference 与 descriptor inspection。 | artifact plan/bundle 不能启动 container 或授权 deployment。 |
| `pp-local-edge` | 位于 `http://127.0.0.1:19021` 的 **current source / target runtime** 静态回环浏览器入口和 Deployment Center projection。 | 静态浏览器内容、有界 session cache，以及默认禁用的 no-store bridge-configuration asset。它不托管 MCP，也不代理宿主。 | Browser/cache state 永远不是 deployment 或 runtime 真相，也没有宿主操作权威。 |
| 宿主 `ppctl` 规划适配器 | **current source contract / target runtime binding** 的封闭 `inspect` / `preview` planning adapter；执行不可用（`deferred_to_pr5`）。 | 宿主检查、profile recommendation、V2 validation、plan/preflight shaping、脱敏和后续 operation adapter。 | edge 不会获得 Docker socket、任意宿主 socket、SSH 私钥、path、SQLite 或任意命令接口。 |
| `ProjectWorkingSet` / `AgentAwarenessProjection` | **current PR 4 contract + PR 5 durable binding**：不可变 working set 与认证 durable feed composition。 | 同 project/session 校验、有界 source/event page、role/audience 裁剪、显式 cursor ack、shadow/inject gate 与字段脱敏。 | projection 没有权威性。源码与聚焦测试已覆盖 session 注册、continuation、feed composition 与 cursor resume；真实 runtime/production 证据仍待补齐。 |
| 服务器拥有的 `MigrationOperation` orchestrator | **current PR 5 durable source contract / target live phase adapter**；本文没有证明 live adapter 已启动或验证。 | 类型化 runtime/node/canonical-state/derived-index phase adapter、Migration Operation Plan、Execution Grant、SQLite grant/lease/fence journal、rollback 与 durable secret-free receipt。 | production composition 只有 `pp-core`/`pp-server-backend` 可持有 durable migration lease 或写 canonical SQLite，并且必须使用 SQLite journal；进程内 journal 仅用于测试/非生产环境，Deployment Center/`ppctl` 始终只读。 |
| `pp-server-backend` | **current** 的 resolved authority owner；**target** 的 MCP/control/query 和 compute-job adapter。 | SQLite 事务、durable inference job、routing decision、lease、retry、reconcile、accepted-result validation、协作/治理写入、receipt、Maintenance 和 generation 选择。 | canonical state、inference job 调度/验收、协作与治理的唯一 writer/决策 owner；禁止 provider 执行。 |
| 项目协作织网 | **当前源码已实现 / 聚焦测试已通过 / runtime evidence 待验证**：PR 1–PR 4 地基与 PR 5 server-only durable runtime slice。 | Agent/session/role/plan/work/lease/activity/event/cursor/result/acceptance 持久化、可跨重启的认证 Hook continuation、服务器拥有的有界 work issuance/operation、普通 tool-call reconcile、Stop progress/submitted event、formal stage/result receipt、Maintenance composition、shadow/inject awareness、只读 Dashboard projection，以及 accepted-result 到 pending-only outbox 的原子 promotion enqueue。 | 协作状态按项目隔离；projection 可重建且不具权威性；promotion 只能生成 pending proposal；任何 source/test slice 都不授予 canonical-memory 或 production 权威。 |
| `pp-compute-node` | **current** 的有界 inference authority profile 及 typed protocol/identity/admission contract；**target** 的 service。 | CPU/CUDA runtime、批处理、模型缓存、资源 evidence 和派生结果整形。 | 只能租用 inference 并返回派生 evidence；没有协作、治理、promotion、merge、deployment 或 canonical-write 权威。 |
| 推理适配器接缝 | **current** 的 `embedding`、`rerank` 与固定 schema `structured-json` capability declaration；云 provider 只在 compute-node 内部实现。 | 本地、云端或混合 compute-node；服务器端只做注册节点调度、身份校验和结果持久化。 | 所有适配器遵守同一能力 schema 和 identity 策略；server 不直接调用 provider，也不组装推理上下文。 |
| 传输适配器接缝 | **target** 的受认证私有 endpoint transport。 | Docker 网络、受限 SSH/反向 SSH，以及未来的私有传输。 | 应用接口不依赖具体传输实现。 |

### Generation 准备与切换边界

当前 operator 接缝明确拆分派生计算、宿主生命周期权威与 Control mutation：

```text
准备平面
  质量证据 -> build -> reconcile -> 验证 inactive candidate
    -> 原子 prepare receipt（manifest/index tree、质量与 staged revision digest）

独立宿主生命周期边界
  停止 MCP / inference gateway / Maintenance / Knowledge Ingest

切换平面
  可选的认证 Control revision activation
    -> 使用精确 revision environment promotion
    -> 认证 generation retarget
    -> generation-bound live-root bootstrap + verify
    -> 原子更新 runtime 指针

独立切换后边界
  restart -> health/retrieval smoke -> 单独审查并切换 Maintenance
```

cutover 工具优先从 runtime EnvironmentFile 解析 canonical SQLite 与 generation root，除非
operator 显式覆盖；以特权启动时，generation 命令会降权到 runtime owner；Control mutation
只使用 Bearer + CAS API。cutover 会在任何 mutation 前重新计算 manifest 文件、声明的
manifest/index-tree identity、质量证据与 staged revision digest，因此候选和 revision
EnvironmentFile 都不能在两个阶段之间静默变化。它不会直接写 Control SQLite、不会重启服务、不会改变 Maintenance
策略，也不会制造质量证据。源码与聚焦测试只证明 operator contract，不表示生产切换已经发生。

删除测试能证明两个适配器的价值：删除推理适配器会把 provider 选择和身份校验
扩散到 recall、索引和 Maintenance；删除传输适配器会把平台网络逻辑扩散到三端。

compiler 刻意作为第三个接缝，而不是 deployment helper。若删除它，role/variant rule 与
secret/state exclusion 会在 Dockerfile、CI workflow 与后续 host adapter 之间漂移。完整
matrix、mount policy 和 build-versus-runtime boundary 见
[`container-artifacts.zh-CN.md`](container-artifacts.zh-CN.md)。

PR 4 让 Deployment Center 保持为 deep module，而不是让 browser 变成 deployment
orchestrator：宿主模块只有 `DeploymentCenter.inspect(installation_ref)` 与
`DeploymentCenter.preview(DeploymentPreviewRequest)` 两个 public operation。`ppctl`
executable 只是这两个 operation 的宿主侧 typed planning dispatcher；它不是通用 command
runner。PR 4 的执行不可用（`deferred_to_pr5`）。
这是 **current source contract / target runtime deployment**：本文没有证明任何 listener、
host binding 或 runtime deployment 已启动或验证。

PR 5 增加的是 **current durable source / target live adapter** 的服务器拥有 migration seam，而不是
browser apply path。`MigrationOperation` 会把短生命周期 Migration Operation Plan 绑定到新鲜的
canonical-state、artifact、runtime、node 和 derived-index evidence，并在任何 mutation
前要求独立、绑定 operation 的 Execution Grant。Deployment Center inspection hash 不得
升级为该 grant。类型化 phase adapter 只能执行 preflight、backup/rehearsal、cutover、
shadow rebuild/promotion、Maintenance transition、rollback 与 receipt persistence；它们
不得接收任意 Shell、Docker、SSH 或 SQLite command。canonical SQLite journal 会持久化
issued grant、installation-scoped lease、单调 fence、一次性 operation state 与无 secret
receipt；其表只通过备份门控的版本化 deployment migration 安装。过期 running work 会进入
`recovery-required`，stale owner 的 completion CAS 会失败。当前 source 默认将 plan/grant TTL
设为 300 秒（最大 900 秒），并拒绝超过 120 秒的观测。在 live phase adapter 获得独立授权前，不得声称 listener、container、tunnel、
migration、LanceDB promotion、Maintenance transition 或 MCP restart 已验证。

### 项目级多 Agent 协作三平面

> 状态：**current source implementation / runtime evidence pending / target production
> activation**。除 PR 1 地基与 PR 4 projection 外，PR 5 当前源码已包含 server-only durable
> collaboration schema/store、认证跨 transport Hook continuation、有界 work-board operation、
> formal stage/result receipt、Maintenance composition、shadow/inject awareness、只读 Dashboard
> topology/work/timeline，以及 accepted-work promotion validation/outbox、server-owned work
> issuance、普通 tool-call reconcile 和有界的 `Stop` progress/submitted event。accepted work
> 现在会在同一 canonical writer 事务中进入 pending-only promotion outbox；但尚未证明真实 browser/runtime lifecycle
> smoke，也未在 production 执行 migration 与 Maintenance lifecycle。任何 source/test slice
> 都不向 delegated Agent 授予 canonical memory、deployment、database 或 production 权威，
> 也不构成 PR 5 或 PR 6 完成。

协作模型有意把实时协作与长期项目记忆拆开：

| 平面 | 用途与生命周期 | 权威与记忆规则 |
|---|---|---|
| Coordination Plane | 项目级 Agent presence、intent、work lease、类型化 progress/finding/blocker/artifact event 与 review receipt。事件只按运维和审计策略保留。 | 服务器拥有的 append-only 权威。peer event 只是协作状态证据，不是项目事实或指令。 |
| Project Working Set | 当前 `project-working-set/v1` 源码合同：项目 goal summary、plan revision、active Agent/work、候选 result reference、blocker、conflict 与 cursor delta 的可重建有界投影。 | 每类 source 最多 64 项。result 不能仅因 completed 或带 artifact reference 就成为 accepted；accepted-artifact projection 必须具备 PR1-C06 定义的服务器认证独立 `AcceptanceReceipt`。它不是第二真相源。 |
| Canonical Memory | 跨会话仍有价值的稳定项目事实、决策、原则、故障经验与架构约束。 | 只有已验收且有证据的结果才能成为 pending memory proposal；仍需治理采纳后才是 canonical memory。 |

源码与目标运行时的数据流为：

```text
[current PR 5 source] authenticated session + durable collaboration store
    -> typed event / formal result -> CollaborationEventLog / result store
    -> [current source] Project Working Set / awareness delta
    -> server policy + source lineage + 独立 AcceptanceReceipt 验证
    -> [current source/test] accepted result validation
       -> 原子 pending-only CollaborationMemoryPromoter outbox enqueue
    -> pending memory proposal -> governed adoption -> Canonical Memory

[current source/test] 认证 Hook continuation + 有界 work-board lifecycle
[current source/test] shadow/inject awareness + 只读 Dashboard projection
[current source/test] server-owned work issuance + 普通 tool-call reconcile
    + Stop progress/submitted event + 自动 pending-only promotion enqueue
```

role 只是非权威值投影的可见性输入。只有服务器把精确 active `AgentSession` 绑定到当前
最小权限 policy claim，并验证 event-page source lineage、cursor tuple、projection digest 与
独立 `AcceptanceReceipt` 后，full active-work view 才能被消费。调用方自行构造的
`coordinator`/`reviewer` session 或 role string 不授予任何权威。其他已授权角色只获得 own、
audience-visible event 与 dependency work。project/session/audience mismatch、cursor regression
或 gap、过期 policy/factory revision、source substitution、伪造 page/digest 与 acceptance receipt
不匹配都必须 fail closed。objective、capability、event payload、raw prompt、private reasoning、
credential 与 result body 均被脱敏。

`work.progressed`、heartbeat、临时 blocker、未验证 assumption、raw prompt 与隐藏推理
默认均没有晋升资格。finding 在证据通过 review 前始终只是 finding；多个 Agent 达成一致
本身也不能把它变成事实。

该能力分配到现有六 PR 链：

| 交付切片 | 协作职责 |
|---|---|
| PR 1 `routing-core` | 增加不可变 project/session/agent/work/event/result/cursor 值合同、append-only `CollaborationEventLog` 地基，并让现有 task 的 enqueue/dedupe/claim/heartbeat/complete/review/inbox/release/recovery 全部严格按 `project_id` 隔离；这不是持久 registry 或新的可变 work board。 |
| PR 2 `endpoint-contracts` | 即使 `project_id` 相同，也让 inference compute job 与 developer-Agent 协作保持分离；禁止 compute 取得 Task Queue、`AgentRegistry`、work-board、event-writer、awareness、memory/knowledge-promotion、merge、deployment、Maintenance 或 LanceDB-promotion 权威。 |
| PR 3 `container-artifacts` | 只把 PR 1 协作地基封装进 server role；edge 与 compute artifact 均不含 collaboration package；不激活 persistence 或 listener。 |
| PR 4 `deployment-center` | 增加有界 `ProjectWorkingSet` / `AgentAwarenessProjection` 值合同与认证 feed 边界。`project_for(*, audience: AgentSession, deltas: EventPage)` 本身不授予权威；任何 full-work view 在消费前都必须绑定服务器验证的 session/policy/source/cursor/projection/acceptance tuple。 |
| PR 5 `migration-operations` | 当前源码增加 server-only durable collaboration schema/store、可跨重启的认证 Hook continuation、服务器拥有的有界 ProjectWorkBoard issuance/operation、普通 tool-call reconcile、有界 Stop progress/submitted event、formal result/stage receipt、Maintenance composition、shadow/inject awareness、只读 Dashboard projection，以及 accepted-result 到 pending-only outbox 的原子 promotion enqueue。真实 browser/runtime smoke、migration 执行、production activation 与受治理 runtime/production evidence 仍未验证。 |
| PR 6 `release-readiness` | 验证角色封装、升级/回滚兼容、双语文档一致性，以及最终跨 Agent 端到端验收流程。 |

## 4. Deployment Profile

> 状态：**target runtime profile**。PR 2 只校验 V2 profile name 与 endpoint-role
> constraint；它不安装、检测或运行任何 profile。

| Deployment Profile | 放置方式 | active embedding identity 优先级 |
|---|---|---|
| `local-all-in-one` | 三个端容器位于同一主机。 | 本地受管 compute runtime。 |
| `local-cloud` | local edge 和 backend 位于同一主机，可使用已配置的云推理。 | 云端 identity。 |
| `split-accelerated` | local edge 位于用户主机，backend 位于服务器，compute 位于独立本地主机。 | 本地 compute-node identity。 |

安装器可以只读检测操作系统、Docker/WSL2、CPU、内存、GPU、磁盘、网络和
已有模型，再推荐 profile。用户确认或调整后才解析 manifest。必需磁盘空间不足
属于硬性 preflight 失败。

### 逻辑时区策略

三个 endpoint process 与浏览器 projection 统一使用 `TZ=UTC`。canonical timestamp、
lease/fencing 比较、receipt 与 release evidence 始终保存为 timezone-aware UTC。部署资产只设置
process/container environment 和浏览器 formatter；不会挂载 `/etc/localtime`，不会调用
`timedatectl` 或 Windows `Set-TimeZone`，也不会以其他方式修改宿主时区。这样 local edge、
server backend、原生客户端与 compute node 的显示保持一致，同时 Linux、macOS、Windows
宿主配置均不改变。

## 5. Deployment Manifest 与更新流程

PR 2 提供 `EndpointManifestV2` 和确定性的 resolved-plan digest，属于 **current**
pure contract。operational manifest authority、frontend、quick-install script、`ppctl`
和下方 update flow 是 **target** adapter；本任务中没有启用或验证它们。尤其 PR 5 的
migration path 由服务器拥有：`pp-core` 是 canonical SQLite 唯一 writer，browser 与
`ppctl` 仍是只读规划面。

```text
前端选择
  -> 无 secret EndpointManifestV2 candidate
  -> [current source / target runtime PR 4] 宿主 ppctl inspect / preview
  -> 安全的 resolved-plan、manifest-diff、identity 与 preflight projection
  -> 仅供检查的 plan hash + 已分类的未来更新
  -> [current source / target runtime PR 5] 新鲜 Migration Operation Plan
     + 独立 Execution Grant
  -> [current source / target runtime PR 5] 服务器拥有的类型化 migration operation
  -> [target PR 5 adapter] 持久化的无 secret Deployment/Migration Receipt
```

secret 由宿主凭据适配器或服务器 secret storage 保存，manifest 只保存引用。
**PR 6 target work** 会增加 Release Bundle，把不可变 source revision、profile/variant 与
protocol compatibility matrix、OCI digest evidence、SBOM/provenance reference 以及不透明
Model Catalog reference/digest 绑定在一起。Model Catalog 描述固定 model revision、artifact
hash、capability、resource estimate 和 compatible runtime。这些均是目标 evidence requirement，
不表示 bundle、signature、registry artifact 或 model admission 已存在。

### 分级热更新

| 变更级别 | 应用机制 | 示例 |
|---|---|---|
| Live apply | 不重启，更新 active runtime 策略。 | Provider 优先级、轮询、熔断、队列限制、节点权重、Maintenance 调度。 |
| Rolling restart | 按依赖顺序重启受影响端容器。 | 镜像 digest、runtime 适配器、内部端口、secret reference、GPU runtime。 |
| Shadow rebuild + promotion | 新派生 generation 验证完成后再切换。 | Embedding identity、维度、归一化、metric、持久化切片 identity。 |
| Backup + migration | 备份校验、短维护窗口、迁移、健康门禁和回滚收据。 | SQLite schema 或不兼容 canonical 状态契约。 |

更新失败时旧 manifest revision 保持 active。PR 4 中，`ppctl` 只分类未来计划；执行
不可用（`deferred_to_pr5`）。后续获得授权的 operation adapter 不能把重建或迁移伪装成
普通实时配置修改。

### PR 4 Deployment Center 规划接口

> 状态：**current source contract / target runtime deployment**。类型化 interface 与静态
> projection source 已存在；本文没有证明 listener、host binding、endpoint enrollment 或
> runtime deployment 已启动或验证。

仅宿主侧 `DeploymentCenter` 模块暴露两个 operation。configured bridge base 只能是
`http://127.0.0.1:<port>/ppctl/v1`；本 PR 4 不存在其他 base、operation 或 request shape：

```text
DeploymentCenter.inspect(installation_ref)
POST <configured bridge base>/inspect
Content-Type: application/json
{"installation_ref":"local-installation"}
  -> platform/resource/catalog/status/model/enrollment/receipt projection

DeploymentCenter.preview(DeploymentPreviewRequest)
POST <configured bridge base>/preview
Content-Type: application/json
{"installation_ref":"local-installation","candidate_manifest":<EndpointManifestV2 JSON>}
  -> recommendation + manifest diff + module/resource estimate
     + hard preflight result + 保守的 PR 4 update class
     + 仅供检查的 plan hash
```

`installation_ref` 是宿主拥有的 identifier，绝不能是 browser 提供的 path。candidate
只能按 `EndpointManifestV2` 解析；可能带 SSH host 的 legacy manifest 不接受 edge 输入。
宿主可以在只读检查后推荐 profile；选择另一个受支持 profile 会记录为 user override，
但不能绕过 V2 validity、完整 model-identity check、immutable artifact evidence、high-risk
acknowledgement 或 resource preflight。

bridge 的 JSON 只能经过有界流式读取：超出声明长度的请求会在读取前拒绝，分块 body 超过
固定 128 KiB 上限时会停止读取，之后不会到达 `inspect` 或 `preview`。

`manifest_comparison` 是 digest 级摘要。controller 提供安全的 active-topology projection
时，`manifest_diff` 会报告 profile、module、endpoint 和 compute-capability 变化，同时不
泄露 path、transport、secret 或 raw manifest body；没有此 projection 时会明确标记 diff
不可用。

`update_class` 有封闭词表：`no-change`、`live-apply`、`rolling-restart`、
`shadow-rebuild-promotion`、`backup-migration`、`enrollment-required` 和
`manual-review`。PR 4 只会输出 `no-change`、`enrollment-required` 或
`manual-review`：它的脱敏 controller projection 从不包含 active manifest body 或
persisted receipt，因此不能把不同的 candidate 诚实地分类成 live apply、restart、rebuild 或
migration。那些标签属于目标 PR 5 operation adapter 的输出。plan hash 绑定已观察到的宿主状态，用于检查和 drift
reporting；PR 4 中它既不是 activation token，也不是 mutation authorization。safe-space
failure 是硬性拒绝，不是可覆盖的 warning。

PR 4 中 enrollment 与 receipt 字段只是 projection。它们会区分 contract readiness 或
unpersisted contract receipt 与 server-persisted receipt，并诚实报告 unavailable/unverified
状态。node contact、credential transfer、tunnel creation、enrollment consumption、service
control、SQLite mutation、LanceDB promotion 和 Maintenance 仍然延后。

现有 `http://127.0.0.1:19020/mcp` endpoint 是 server/backend MCP 入口。位于
`http://127.0.0.1:19021` 的 `pp-local-edge` 浏览器入口只提供静态内容。bridge
configuration 缺失时，其 no-store configuration 为 `disabled`，browser 不发出宿主请求。
只有宿主显式配置时才能声明 `http://127.0.0.1:<port>/ppctl/v1`；此时 browser 直接向
`/inspect` 或 `/preview` 发送上面的两种固定 JSON `POST` body。`pp-local-edge` 不代理
bridge、不挂载宿主 socket，也不获得 Docker/SSH credential。Browser/cache state 仍然没有权威。

PR 4 的协作读接口同样只存在于源码：
`ProjectWorkingSet.project_for(*, audience: AgentSession, deltas: EventPage)` 要求 working set、
audience session、`EventPage.after_cursor` 与 next cursor 处于同一 project/coordination session，
并拒绝 audience 不可见 event、cursor regression、非空 page 未推进与空 page cursor gap。
每类 working-set 输入最多 64 项，单页最多 20 条 delta，最终 projection 最多 64 KiB。

该值工厂不是认证边界。可信 feed 必须绑定服务器认证的 active `AgentSession`、当前 policy
revision、event schema/log revision、projection-factory revision、`cursor_from`/`cursor_to`、
source-page digest、generated-at UTC、projection digest 和独立 `AcceptanceReceipt`。调用方自报
`coordinator`/`reviewer` role 或自行构造 session 不能取得 full-work 可见性；completed work +
artifact reference 也不能成为 accepted artifact。objective、capability、event payload、prompt、
private reasoning、credential 与 result body 均被脱敏；`canonical_memory_effect` 固定为 `none`。
PR 5 源码与聚焦测试现在覆盖 durable registry/work-board state、server binding seam、认证
fresh-client Hook continuation、公开有界 work-board lifecycle、shadow-to-inject gate、普通
tool-call reconcile 和有界 `Stop` progress/submitted event 发射。因此本文不声称
可信 feed 已激活，也不声称任何真实 runtime、lifecycle、browser 或 production 路径已验证。

### PR 6 release-readiness 合同（target / unverified）

PR 6 把 release delivery 与 deployment execution 分开。source `ArtifactBundle` 只是不变的
build inspection evidence。目标 Release Bundle 包含 source revision、package version、
protocol/profile/variant compatibility、image digest、SBOM/provenance reference 以及不透明的
Model Catalog reference/digest。它不包含 weight、path、credential、canonical state、runtime
configuration 或 Execution Grant。

```text
Windows/WSL2：仅本地 build/cache 与 derived-inference GPU smoke
  -> GitHub protected workflow：目标 RC/stable evidence + immutable digest
  -> Release Bundle：目标 selected evidence，不是 runtime receipt
  -> server：目标 verified-digest consumer；唯一 SQLite writer
  -> stable-only repository：目标显式 publication，需单独批准
```

目标 server 之后可执行 Migration Operation、重建/推广 derived LanceDB、验证 MCP 和切换
Maintenance。这些动作都需要自己的 authority 与有界 evidence。文档字段、digest 或 bundle
均不证明任何动作已经完成。

Workflow Composer 只以 PR6 的可观测 `shadow-only` 候选 planner 方式纳入。它必须绑定冻结的
plan/compiler/hard-gate/tool-policy digest 与 atomic-skill receipt chain，不得自证 user-only
stage 或删除/重排强制门禁，也不拥有 execution 或 authorization authority。fixed route 始终是
execution authority 与确定性 rollback target。本文不声称 shadow 对比或其对抗证据已经运行。

## 6. Embedding identity 与 provider 路由

一个 active embedding identity 对应一个 LanceDB Knowledge Generation。
本地和云端 embedding 只有在以下字段全部一致时才能互为 fallback：

```text
model + fixed revision + artifact/served identity + dimension
+ normalization + distance metric + tokenization/pooling contract
+ golden-vector compatibility evidence
```

仅维度相同不够。identity 不同时由 active Deployment Profile 二选一：

- `split-accelerated` 和 `local-all-in-one` 保留本地 identity；
- `local-cloud` 保留云端 identity；
- 切换到另一侧需要受控 manifest revision、shadow rebuild、验证和原子
  promotion。

`split-accelerated` 默认本地优先。本地节点失败后，只有云 provider 已配置、
已启用、健康且 identity 兼容时，compute-node 才执行云端或混合 provider；backend
只记录节点、原因和 revision，不直接调用 provider，也不组装推理上下文。云端未配置或也失败时：

- canonical 写入继续；
- embedding 和 rerank 任务进入 durable derived-work/outbox；
- recall 使用当前 verified generation 加 BM25/文本/符号路径；
- 响应明确标记退化，不能宣称向量已实时更新；
- 后台继续轮询所有已配置本地和云 provider；
- provider 恢复后必须连续通过 identity、能力探针和稳定窗口，才能恢复路由。

配置云端即视为操作者授权相应 capability。Plastic Promise 不增加内容级 DLP
或项目内容过滤；凭据脱敏仍是硬性要求。

### 受管索引材料迁移绑定（当前源码/测试）

迁移 planner 不得从旧的服务器环境变量推断 governed 模型身份。active control-plane
注册提供显式 target identity；只读 plan 与可变 apply 都会绑定它的 SHA-256 digest 以及不可变
的 index-outbox watermark/digest/count 快照。这样 canonical SQLite 单写者迁移与派生 LanceDB
generation 始终使用同一模型身份；在 governed route 上，`fallback-zero` 会被拒绝。

普通记忆的项目级修正会在每一次 governed embedding probe 与 retrieval 调用中使用记忆自身的
`project_id`。governed embedder 还会向 health 暴露有界的请求数、token 与成本统计作为证据；
它不会暴露 provider credential 或 canonical 内容。以上仅表示当前源码和聚焦测试证据，不表示
真实 migration、generation 重建、promotion、restart 或生产验收已经发生。

## 7. 类型化 compute capability

> 状态：协议、identity、有界 resource report、由 manifest 绑定的 capability 验收、
> admission、lease/fencing、result validation、安全 receipt schema 与隔离 compute-node
> listener 都是**当前源码 contract**。外部 runtime placement、transport 健康、retry、
> reconcile 与 production evidence 仍是**目标/未验证**。

首版必需能力是 `embedding`、`rerank` 与固定 schema 的 `structured-json`；每个能力都有一个封闭的 capability version
和一个封闭的 body-free result schema。协议后续可以加入
`semantic-chunking`、`memory-classification`、
`domain-naming`、`conflict-analysis`、`knowledge-synthesis` 和
`security-review-inference`。

显式绑定的能力声明携带一个封闭的 `CapabilityBinding`：

- model-identity fingerprint、封闭的 input/result schema identifier；
- capability 粒度的并发上限，以及有界的空闲内存和 model-cache 下限；
- timeout、SHA-256 idempotency-key format、是否支持取消，以及封闭的 terminal-reason
  语义；
- 绑定 input/result schema 的 body-free golden-probe input 和 expected-result hash。

Deployment Manifest 是该 binding 的权威来源。compute `hello` 和 server
requirement 只能与它比对，不能临时注入 binding。admission 会对缺失或漂移的已绑定
capability、identity/binding 不匹配，以及无法满足 manifest 或 requirement 的 lower-minor
protocol 提出隔离建议。此前仅含 `kind`/`contract_version` 的裸声明只保留向后读取兼容，
不提供 binding evidence。后续 transport adapter 才执行 golden probe；本 contract 只绑定其
安全 identifier 和 hash。

接口不接受任意 Prompt、工具、文件路径、Shell 命令、数据库位置或 MCP 管理
调用。宿主 Ollama 是显式兼容适配器，不是生产默认。默认 compute 镜像管理
自己的 CPU 或 CUDA runtime，并挂载受控模型缓存，镜像不内置模型权重。

## 8. 状态、数据流与恢复

canonical SQLite 仅属于 `pp-server-backend`，保存记忆、治理、active manifest
revision、节点注册、租约、provider 健康、持久任务、部署收据和 generation
选择证据。LanceDB 始终是可重建派生状态。`pp-local-edge` 投影服务器状态，
只保留有界临时会话数据。`pp-compute-node` 保存模型物料、runtime 缓存和 active
lease 状态，但不保存 canonical 数据。

派生工作采用至少一次和幂等语义，不宣称分布式 exactly-once。completion 必须同时
匹配自身 lease 以及同一 derived-work job 由 server 提供的当前 `ComputeFence`，因此
旧 lease 在新 generation 已签发后不能提交结果。reconcile 可补齐缺失的 eligible 工作，
但不能修改治理策略。

## 9. 通信与信任

同机安装使用 Docker 内部网络；远程安装默认使用出站发起、回环绑定的受限
SSH tunnel：

- local edge 通过客户端 SSH tunnel 访问 server backend；
- compute 通过 reverse SSH tunnel 暴露到服务器回环端点；
- tunnel 身份不能获得 Shell、SFTP、sudo、agent forwarding 或任意端口转发；
- 传输恢复后仍需重新通过节点、模型和 golden probe，才能恢复调度。

目标态 Deployment Center 引导 enrollment。backend 将签发短期、单次使用的 enrollment
物料，由宿主 `ppctl` 传递和消费；前端不展示或存储长期凭据。PR 4 只 projection
readiness 与 receipt state，不联系 node、不传递 credential material，也不消费 enrollment。

## 10. 可观测性与健康

`pp-server-backend` 是持久聚合点。`pp-compute-node` 只上报有界 heartbeat、
capability、模型 identity、负载和延迟。`pp-local-edge` 订阅快照/事件并展示：

- active Deployment Manifest 与 Release Bundle revision；
- 当前 provider、fallback 原因、健康计数和下次探针时间；
- 队列深度、重试、dead reason、reconcile lag 和租约到期；
- Knowledge Generation identity、watermark、readiness 和 promotion 状态；
- tunnel 状态和最后一次 identity 验证结果；
- 项目 Agent presence、leased work、已验收 artifact、blocker、conflict 与 awareness
  cursor，但不暴露 raw prompt 或隐藏推理；
- 更新计划、受影响端点、回滚物料和 Deployment Receipt。

local edge cache 只用于展示。Deployment Center refresh 必须取得新的 host/server
projection；browser 的 cache result 不能证明 disk space、endpoint enrollment、plan 或
receipt state。

核心 MCP 健康与可选结构化推理健康必须分离。云 structured-JSON 失败不能把
canonical 和文本检索仍可工作的 MCP 错误地统一标记为 503。

## 11. 安全模型

- SQLite 只挂载到 `pp-server-backend`。
- 前端容器没有 Docker socket、宿主凭据、宿主 path access、SQLite access、SSH material
  或任意宿主执行能力。
- PR 3 的 artifact policy 从 image layer 中排除 database、LanceDB generation、model
  weight、credential、private key、runtime state、log 与 build cache；它本身不授予任何
  live mount。
- CPU/CUDA compute artifact 共用 typed result contract。未来 compute runtime 最多获得受控
  read-only model volume 与有界 scratch space，绝不能获得 canonical-state authority。
- PR 4 中宿主 `ppctl` 只接受类型化 `inspect` 与 `preview` planning operation，并暴露脱敏
  projection；执行不可用（`deferred_to_pr5`）。未来 mutation 只能执行来自签名 catalog 或
  已审查本地发行物、且被单独授权的 validated plan。
- 默认 listener 仅回环或私有容器网络可见。
- Manifest、receipt、日志和指标不得包含 API key、Token、私钥、数据库正文、
  用户原文或不受限端点细节。
- 协作投影按项目和角色隔离。delegated Agent 只能在自身 capability policy 内发布
  类型化 event 与 receipt；不能采纳记忆、篡改其他 Agent event、合并、部署或推广
  generation。
- PR 4 awareness projection 还按 audience 与 cursor fail closed，并删除 work objective、
  Agent capability 与 event payload；64 KiB 上限不能被 browser 或调用方放宽。
- 发行镜像固定不可变 digest，模型物料固定 revision 和 hash。
- 不兼容协议版本的端点由 backend 隔离。

## 12. 规模与成本模型

首要目标是个人或小团队安装，因此使用三个端容器，不构建用户可见的微服务
集群。并发先在 `pp-compute-node` 内通过批处理和资源准入扩展；以后可以在同一
类型化接缝后注册多个 compute node，而不改变 canonical 所有权。

文档不宣称固定月度金额。成本取决于云 capability、请求量、模型物料、镜像
保留和硬件开机时长。前端必须在 apply 前估算下载、展开镜像、模型、缓存、
shadow generation 和回滚空间。本地优先、批处理、durable reconcile 和文本
退化可减少无意义云调用与重复工作。

## 13. 交付计划与验收

实现使用六个 stacked PR：

1. `routing-core`
2. `endpoint-contracts`
3. `container-artifacts`——仅 source-level plan、descriptor 与 artifact policy；没有
   runtime action
4. `deployment-center`——当前 source-level working-set/awareness 读合同；server feed、
   persistence 与 Hook/MCP/runtime 接线仍延后
5. `migration-operations`
6. `release-readiness`——目标 Model Catalog / Release Bundle selection、protected immutable
   evidence 与 stable-only handoff；不作 publication 声明

每个 PR 只执行覆盖改变接缝的最小确定性测试与双语文档门禁，并取得独立 Standards、Spec、
DeepSec Shield/代码坏味道 receipt；三者必须绑定同一不可变 source、diff、requirement 集合
与联合合同 revision。上述回执通过且合并获得明确授权后，才可按依赖顺序合并，并通过兼容
或 shadow deployment 切片验证，且必须预先定义回滚；下一个 PR 基于已合并 revision 开发。
无需在每一步重复执行宽泛测试集合。

完整的跨 profile、跨 Agent、migration、recovery 与 rollback E2E 在 PR 6 后集中执行。
它是最终验收门禁，不应成为首次发现基础 seam 或 schema 缺陷的地方；这些问题必须由
每 PR 的聚焦检查提前消除。公开稳定发行和生产迁移仍需要独立授权。

详细工作与回滚契约见
[`implementation-notes.zh-CN.md`](implementation-notes.zh-CN.md)。每个 PR
还必须通过
[`documentation-parity.zh-CN.md`](documentation-parity.zh-CN.md)；文档一致性是
实现结果的一部分，不是代码完成后的清理工作。
