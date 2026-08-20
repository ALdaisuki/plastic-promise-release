# 可组合部署路线图

英文对应页：[`composable-deployment.md`](composable-deployment.md)。

> **规范范围：**机器可读的
> [`union-six-pr-contract.json`](../standards/union-six-pr-contract.json) revision
> `2026-08-18.1` 是六 PR 交付合同的唯一权威源。本文只是非规范的实施投影。只有每个
> `delivery_scope`、`collaboration_scope` 与 `required_evidence` 条目均通过，某个 PR
> 才算完成；某一源码切片标记为“当前已实现”不等于整个 PR 完成，也不证明
> runtime/production 状态。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

## 状态图例

- **当前已实现**：可由本仓代码和聚焦测试验证的纯契约或兼容能力。
- **现有兼容**：保留的 V1 路径，不自动取得 V2 的跨提供方或生产推广权限。
- **目标**：后续 PR 才会实际启用的镜像、执行器、迁移或发行工作。
- **未验证**：本轮没有可用运行时证据的能力；不得作为已验收功能宣传。

## 当前 — 第一阶段

- PR 1 `routing-core`：受治理的检索 embedding 路由、健康语义、不可变协作值合同、项目/会话
  作用域的 append-only `CollaborationEventLog` adapter 地基，以及现有 task 路径的严格
  `project_id` 隔离。它尚不提供持久 Agent registry/work board、新的可变协作租约生命周期、
  Hook/MCP 接线、awareness injection 或记忆晋升。
- PR 2 `endpoint-contracts`：**当前 source-only** 的纯深模块 `EndpointAuthority`，接口为
  `resolve` -> `assess` -> `verify_completion`；源码调用方仍保留旧名时，
  `EndpointContractRegistry` 只作为兼容名称。服务器编译封闭的
  `EndpointAuthorityProfile`：`pp-local-edge` 只有 intent 与有界只读权威；
  `pp-server-backend` 是 canonical state、inference job 调度/验收、协作与治理的唯一
  writer/决策 owner；`pp-compute-node` 独占有界 inference lease 的执行权，并只返回派生
  result/evidence。`project_id`、
  manifest/hello claim 与 advertised
  capability 都不能授予权威。该 PR 不激活 runtime、transport、persistence 或 migration，
  不启动容器、不启用 Maintenance，也不推广 LanceDB。

现有兼容路径包括 V1 manifest、`/v1/identity`、`/v1/embeddings`、
`/v1/rerank`、现有 node governance 与 durable outbox。它们不会因维度相同而被
自动视为可与 V2 identity 混用。

## 项目协作与记忆边界

项目级多 Agent 协作属于同一个六 PR 交付链，但使用三个相互分离的平面：

- **Coordination Plane：**服务器拥有、按项目隔离的 Agent presence、work lease、
  类型化 event、review/conflict receipt 与基于 cursor 的 history；
- **Project Working Set：**当前 plan、active work、候选 result reference、blocker 与 peer
  delta 的有界、可重建投影；artifact 只有绑定 PR1-C06 定义的服务器认证、reviewer 独立
  `AcceptanceReceipt` 后才算 accepted；
- **Canonical Memory：**稳定事实、决策、原则与经验。只有已验收且有证据的结果才能生成
  pending proposal，采纳仍是独立治理动作。

peer progress、heartbeat、临时 blocker、assumption、raw prompt 与隐藏推理默认没有晋升
资格。多个 Agent 达成一致本身也不能把 finding 变成项目事实。

PR 1 实现不可变值/event-log/隔离地基，PR 4 增加 source-level 读投影。当前 PR 5 的
source/focused-test 切片增加 durable collaboration runtime、server-owned work issuance、普通
tool reconcile、有界 Stop progress/submitted event、accepted-result 到 pending-only outbox 的
原子 promotion enqueue，以及仅由 compute node 执行的 embedding/rerank/structured JSON 与
项目级 `local`/`cloud`/`hybrid` 路由；structured JSON 默认关闭。真实 browser/runtime
lifecycle、可变 migration 执行、provider activation、production acceptance 与 publication
仍未验证。交付分配如下：

- PR 1 增加不可变 project/session/agent/work/event/result/cursor 值、append-only event-log
  地基，并让现有 task lifecycle 严格按项目隔离；它不增加持久 `AgentRegistry` 或
  `ProjectWorkBoard` service；
- PR 2 即使在 `project_id` 相同时也让 inference compute job 与 developer-Agent 协作工作
  保持分离；compute 不获得 Task Queue、`AgentRegistry`、work-board、event-writer、awareness、
  memory/knowledge-promotion、merge、deployment、Maintenance 或 LanceDB-promotion 权威；
- PR 3 只把 PR 1 地基封装进 server role，并让 edge/compute artifact 不含 collaboration
  package；不激活 persistence；
- PR 4 增加 source-level `ProjectWorkingSet` 与按角色裁剪的 `AgentAwarenessProjection`
  只读视图。当前合同将每类 working-set source 限制为 64 项、每页 delta 限制为 20 条、
  projection 限制为 64 KiB。`project_for(*, audience: AgentSession, deltas: EventPage)`
  只是没有权威性的值投影；role string 或调用方自行构造的 session 不授予可见性。full-work
  消费必须绑定服务器认证的 active session、当前 policy claim、source-lineage/cursor/
  projection digest 与独立 `AcceptanceReceipt` 谱系；其他已授权角色只获得 own、可见 event
  与 dependency work。project/session、audience、cursor、source identity 以及 policy/factory
  revision 均 fail closed；work objective、Agent capability、event payload、prompt、private
  reasoning、credential 与 result body 被脱敏；runtime feed binding 继续延后；
- PR 5 当前源码增加 server-only durable registry/work-board/session/lease/event/result/acceptance
  store、可跨重启的认证 Hook continuation、服务器拥有的有界 work issuance/operation、普通
  tool reconcile、有界 Stop progress/submitted event、formal result/stage receipt、
  replay/idempotency/fencing、Maintenance composition、shadow/inject awareness、只读 Dashboard
  projection，以及 accepted-result 到 pending-only outbox 的原子 promotion enqueue。它还把
  local、hosted 与 raw embedding/rerank/structured-JSON provider 限制在
  `pp-compute-node`，structured JSON 默认关闭，并支持项目级热路由。真实 browser/runtime
  smoke、可变 migration 执行、provider activation 与 production evidence 仍未验证。
  `CollaborationMemoryPromoter` 只能为独立验收、证据完整且 conflict 已处理的 work 生成 pending
  proposal，不能执行采纳；
- PR 6 验证 role packaging、升级/回滚兼容、文档一致性与最终跨 Agent E2E。

## PR 3 源码边界 — 第二阶段

- PR 3 `container-artifacts`：**当前源码级**的 `ContainerArtifactCompiler` policy 与可检查
  descriptor，覆盖 edge/standard、backend/standard、compute/CPU 与 compute/CUDA。它校验
  immutable OCI/SBOM/provenance evidence 与静态 recipe policy，并排除 model、SQLite、
  LanceDB、secret 与 runtime state。受保护 CI 已配置不 push 的 OCI build verification；
  compiler 不 activation host/container runtime、不创建 tunnel、不 publish registry，也不
  改变 deployment/production。协作新增内容只把 PR 1 event-log 地基封装为 server-only；
  edge 与 compute 不获得 writer 权威，也不激活 collaboration persistence。
- PR 4 `deployment-center`：当前源码已包含 local edge Deployment Center planning contract、
  受限宿主 `ppctl`，以及不可变 `ProjectWorkingSet`/`AgentAwarenessProjection` 读合同；
  projection 明确不含 raw peer prompt，且 `canonical_memory_effect` 为 `none`。持久化、
  `AgentRegistry`、`ProjectWorkBoard`、受认证 server feed、Hook/MCP/runtime 接线仍属于 PR 5。
- PR 5 `migration-operations`：当前源码包含 backup-gated migration contract、server-owned
  manifest/receipt 持久化、持久 Agent/work state、可变 lease/session lifecycle、MCP/Hook
  接线、shadow awareness、pending-only promotion 编排，以及仅 compute node 执行的
  embedding/rerank/structured-JSON contract。真实 SQLite migration、LanceDB shadow
  promotion、Maintenance cutover、rollback 执行、provider activation 与 production evidence
  仍是 target/unverified operation。
- PR 6 `release-readiness`：跨平台安装入口、RC bundle、最终 profile/cross-agent E2E、
  文档一致性、role-capability evidence、受保护的发行准备，以及仅作 shadow 候选规划、
  保持 fixed route 为执行权威的 Workflow Composer。公开 PyPI、GHCR stable、GitHub Release
  与发行仓库同步仍需独立授权。

## 交付节奏

每个 PR 只运行最小确定性 seam test 与双语文档门禁，再取得独立 Standards、Spec 与
DeepSec Shield/代码坏味道 receipt，三者绑定同一不可变 source、diff、requirement 集合与
联合合同 revision；之后仍需明确授权才能按依赖顺序合并。改变 runtime 的合并在下一个 PR
开始前执行可回滚的 compatibility 或 shadow deployment 切片。完整跨 profile、跨 Agent、
migration、recovery 与 rollback E2E 在 PR6 后作为最终验收集中执行；它不应成为首次发现
基础 scope、schema 或 authority 缺陷的地方。

## 不在本栈范围内

- 多个 canonical SQLite 写入者、SQLite 复制或计算节点上的 writable state。
- 把 `project_id`、manifest/hello claim 或 advertised capability 当成权威 grant。
- 计算节点上的任意 Agent prompt、Shell、文件执行或 MCP 管理接口。
- 从未审查 PR 自动合并、自动公开发行或自动修改生产。
- 把 peer activity、中间推理或 pending proposal 当成 canonical memory。

部署档案、模型 identity 和状态所有权的细节见
[`../deployment/README.zh-CN.md`](../deployment/README.zh-CN.md) 与
[`../architecture/three-endpoint-deployment/architecture.zh-CN.md`](../architecture/three-endpoint-deployment/architecture.zh-CN.md)。
