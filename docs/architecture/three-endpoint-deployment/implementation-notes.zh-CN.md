# 三端部署实施说明

> 本文是实施契约，不是目标 runtime 已部署的证据。状态日期为 2026-08-14；
> 当前 PR 5 协作能力已在源码实现并通过聚焦测试，真实运行时与生产证据仍待验证。

> **规范范围：**自动生成的联合合同文档及其规范源
> [`union-six-pr-contract.json`](../../standards/union-six-pr-contract.json) revision
> `2026-08-18.1` 定义完成条件。下方 PR 标题只用于组织实施工作，不得删除或推迟联合合同
> 分配给同一 PR 的任何职责。

只有每个 `delivery_scope`、`collaboration_scope` 与 `required_evidence` 条目均通过，PR
才算完成；任一单侧完成都不等于 PR 完成，source/test 证据也不等于
runtime/production 证据。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

英文对等文档：[`implementation-notes.md`](implementation-notes.md)。

## 状态图例

| 标签 | 在本计划中的含义 |
|---|---|
| **current** | 对应 stacked PR 工作树中已有源码/契约；不是 deployment evidence。 |
| **legacy-current** | 既有 repository/runtime path 仍为兼容性基线，不代表 host health。 |
| **target** | 延后到具名后续 PR 的工作。 |
| **unverified** | 未证明 live external state；源码合同本身不是 runtime evidence。 |

## 交付纪律

本次工作是六个 PR 的依赖链。每个阶段完成后可以自行 commit、push 和创建 PR；只有聚焦
证据通过，且独立 Standards、Spec、DeepSec Shield/代码坏味道三条 review receipt 均绑定
同一不可变 source、diff、requirement 集合与联合合同 revision，并且用户在当前任务中对该
PR 的合并给出明文授权后，才可按 PR 1 到 PR 6 顺序合并。下一个 PR 基于已合并 revision
开发，不再长期维护六层未合并堆栈。

每个 PR 必须：

- PR 1 从最新 `main` 开始；后续 PR 必须从包含前一 PR merge 或 squash-merge revision 的
  最新 `main` 开始，不能继续以前一个仍开放的功能分支作为长期 base；
- 明确升级、退化和回滚方式；
- 在同一提交集合中同步英文和中文文档；
- 通过
  [`documentation-parity.zh-CN.md`](documentation-parity.zh-CN.md)；
- 只在改变的接缝增加聚焦契约测试，不重复执行无关的大型测试集合；
- PR 改变 runtime 行为时，在合并后执行可回滚的兼容或 shadow deployment 切片；
- 保持 SQLite 为 canonical truth、LanceDB 为可重建派生状态；
- 不执行生产激活、stable 发布或自动合并。

完整的跨 profile、跨 Agent、migration、recovery 与 rollback E2E 在 PR 6 后集中执行。
它是验收门禁，不是主要的缺陷发现循环：最小确定性检查和快速 review 必须先消除基础
schema、scope、authority 与 rollback 问题。

最终堆叠链中，endpoint container、生成的原生 runtime asset 和浏览器 timestamp 显示统一使用
逻辑 UTC。它不会修改 Linux、macOS 或 Windows 宿主时区；持久化 timestamp 与 lease/fencing
比较仍保持 timezone-aware UTC。

## PR 1：`routing-core`

### 目标

把受治理推理路由做成查询 embedding、索引 embedding 和 rerank 共用的深模块。
可选 provider 失败不能让 canonical MCP runtime 统一变为不健康。

PR 1 建立 retrieval-routing seam，并将它用于实时 query probe、原则语义激活以及
现有 LanceDB/embedder 集成。当前 PR5 runtime 将云端和本地推理统一放在已登记的
`pp-compute-node` seam 后；没有符合条件的节点时，服务器只返回稳定的 defer/原顺序
退化，不会在本地重新发现 provider。类型化 rerank 路由、provider 生命周期恢复与
完整跨 provider 兼容性仍作为独立证据门禁。
同一 PR 还会建立最小项目协作地基，避免并行 Agent 在后续协作能力落地前共享或认领
没有项目作用域的工作。

### 必需修改

- 建立单一类型化 capability 请求接口，返回派生结果或稳定退化原因。
- 把本地节点、云端和确定性文本退化适配器放到同一接缝后；只有 compute-node
  endpoint 可以构造推理 adapter。
- 强制 fallback 的 embedding identity 完全兼容。
- identity 不同时按 profile 二选一。
- 对不可用派生任务增加 durable queue/reconcile。
- 增加项目 scope、Agent identity、coordination/agent-session snapshot、work/result receipt、
  typed event、audience 与 cursor 的不可变、无 secret 协作值合同。这些值不证明注册、
  不授予权威，也不实现可变 lease lifecycle。
- 增加项目/会话作用域的 append-only `CollaborationEventLog` adapter 地基，支持幂等 event
  identity、causal-parent scope 检查、有界 cursor 读取、role/agent audience filtering、expiry，
  并要求显式注入 SQLite connection 或 path。PR 1 不把它接入 MCP、Hook 或生产认证边界。
- 迁移现有 task row，并让现有 enqueue、dedupe、claim、heartbeat、complete、review、inbox、
  release 与 recovery 全部强制携带 `project_id`；无法确定项目的 legacy row 进入不可领取
  quarantine，直到服务器治理重新分配。这是在加固现有 task lifecycle，不是新增
  `ProjectWorkBoard` service。
- developer-Agent collaboration value 与 compute job 保持不同类型。持久
  `AgentRegistry`/`ProjectWorkBoard` adapter 及其新的可变 lease/session lifecycle 仍是 PR 5
  的目标工作。
- 定义三平面不变量：coordination event 不是 Project Working Set 的真相，二者也都不是
  canonical memory；peer progress 默认永远没有自动晋升资格。
- 为 PR 2 的 endpoint contract 和 lifecycle 实现定义有界健康轮询、熔断状态、
  连续成功恢复和回切稳定窗口。
- 分离核心 MCP readiness 与可选结构化推理 readiness。

### 聚焦证据

- query embedding、index/outbox 和 rerank 各一个路由契约测试；
- PR 1 的 query identity 与 project-scope routing 测试；后续 PR 2 覆盖
  mismatch/profile 选择和稳定恢复；
- 可选云失败不会导致核心 503 的健康投影测试；
- secret/private-reasoning field 的不可变合同拒绝矩阵；
- event log 的 append/idempotency/causal-scope/audience/cursor 聚焦测试；以及
- 现有 task lifecycle 的跨项目负向矩阵及其增量 schema/recovery transaction 测试。

### 文档门禁

审计并同步 `README.md`、`docs/README.zh-CN.md`、所有现有中英文架构 SVG、
deployment/profile 文档、价格/资源表、GitHub badges 和仓库链接。本规格阶段不
修改这些现有文件；PR 1 完成前必须列出并修复基线漂移。

### 回滚

停用新路由策略，保留 durable work 和审计证据，调用方回到确定性的 defer/原顺序退化。
不得删除排队任务、项目级 row、append-only collaboration event 或重写 canonical memory。
PR 1 没有 awareness injection、持久 Agent registry 或协作记忆晋升可供停用。

## PR 2：`endpoint-contracts`

### 目标

通过一个纯源码、source-only 的 `EndpointAuthority` 深模块在 pure-contract 层定义三端。
它用 `resolve` -> `assess` -> `verify_completion` 接口编译并执行封闭 role/action profile；
OS/runtime enforcement 被有意延后。

### 当前源码级范围

- 定义 `pp-local-edge`、`pp-server-backend`、`pp-compute-node` 的版本化接口。
- 增加纯 `EndpointAuthority` 深模块；其小接口只有 `resolve`、`assess` 与
  `verify_completion`。源码调用方仍使用旧名时，`EndpointContractRegistry` 只作为兼容名称。
- 在服务器根据封闭 role/action matrix 编译 `EndpointAuthorityProfile`。`project_id`、
  manifest/hello claim 与 advertised capability string 都只是校验输入，绝不能授予权威。
- 将 `pp-local-edge` 限制为提交 intent 与读取有界 projection；它不能写 canonical state，
  也不能取得协作、治理、计算持久化或部署权威。
- 将 canonical SQLite ownership、LanceDB-promotion-decision ownership 和
  receipt-persistence ownership 解析到 `pp-server-backend`，并把它声明为 canonical
  inference、协作与治理的唯一 writer/decision owner；纯 contract 不执行 mount 或写入。
- 定义 Deployment Manifest v2、Resolved Deployment Plan、端点协议版本和
  兼容性错误。
- 定义类型化 `embedding` 与 `rerank` schema、封闭 capability/result version、
  identity evidence，以及可向后读取的可选 `CapabilityBinding`。已绑定 capability
  携带 model-identity fingerprint、input/result schema identifier、资源下限、并发、
  lease timeout、SHA-256 idempotency format、取消/terminal-reason 语义和 body-free
  golden-probe hash。manifest 是权威来源；hello 和 requirement 必须与之比对，不能
  提供临时 binding。固定 schema 的 `structured-json` 现已作为 compute-node
  capability 提供；backend 只调度注册节点并校验有界结果，不直接调用云 provider，
  也不组装推理上下文。
- 定义 lease、由 server 提供的当前 fence、heartbeat、有界资源报告，以及在 binding
  存在时强制校验 schema、fingerprint、timeout 和 terminal-reason 的 completion check。
- 将 PR 1 developer-Agent 协作值与 inference compute job 保持为不同平面，即使二者携带
  相同 `project_id`。compute endpoint 只能租用有界 inference work，并返回派生的
  `embedding`/`rerank` result 及 health、resource、model、timing evidence；固定 schema 的
  structured-semantic inference 已由当前 `structured-json` compute-node capability
  覆盖，并继续受 identity 与有界 payload 门禁。它不能取得 Task Queue、
  `AgentRegistry`、work-board、collaboration-event-writer、awareness、
  memory/knowledge-promotion、merge、deployment、Maintenance 或 LanceDB-promotion 权威。
- 定义 server-owned 的 `ManifestRevisionRecord` 与 `DeploymentReceipt` schema；
  pure decision 可以生成 receipt value，但 PR 2 不持久化 record。

### 升级与退化

- **升级：**这一纯源码变更让 adapter 能解析封闭 authority profile，并验证、重新解析无
  secret 的有效 V2 manifest，
  在 manifest/hello/requirement 间比对已绑定 capability，对 lower-minor protocol 和
  binding drift 提出隔离建议，并拒绝 lease 已不再匹配 server 当前 fence 或 bound
  capability contract 的 completion。
- **退化：**PR 2 不激活 routing、listener、scheduler 或 persistence。既有 runtime path
  保持 **legacy-current**；调用这些 pure validator 的调用方只会得到稳定 rejection reason，
  contract 不会发起任何 fallback execution。

### 明确延后到 PR 3–PR 6

- Container image、Compose、volume/mount enforcement、endpoint listener、private
  transport、`ppctl` 和 Deployment Center。
- Manifest/receipt persistence、实际 durable-job scheduling/lease issuance、result
  storage、retry/reconcile side effect 和 runtime health proof。
- authority profile、transport adapter 或 persistence adapter 的 runtime activation；PR 2
  只返回 pure decision 与 value。
- 生产 SQLite migration、LanceDB shadow rebuild/promotion、Maintenance、MCP restart、
  RC/stable release 和 production acceptance。

### 聚焦证据

- schema parse/round-trip 与 secret rejection 测试，包含 bound capability 的
  identity/schema/resource/lease/golden-probe facts；
- SQLite 和 LanceDB promotion 的 resolved server-only ownership assertion，明确不作
  container/mount 声明；
- 封闭 role/action profile assertion，证明 claim 与 capability string 不能授予权威，并覆盖
  local-edge 与 compute denial；
- 一条跨平面检查，证明匹配的 `project_id` 不能把 inference lease 变成 developer-Agent
  协作工作；
- 协议兼容和 quarantine 测试，包含 lower-minor mismatch；
- lease 到期、binding timeout/schema/terminal-reason mismatch、result/lease 不匹配与旧
  current-fence result 拒绝测试。

### 回滚

将 source-only endpoint-authority contract commit 作为一个 review unit 回退。PR 2 不会激活
runtime、持久化 record 或需要 migration；未来的 backward-readability 和 verified-backup
rollback obligation 属于 PR 5。

## PR 3：`container-artifacts`

### 目标

实现三个独立 endpoint artifact 的 build-time policy 与 inspection boundary，不引入
用户可见的微服务集合，也不激活任何 endpoint runtime。

### 必需修改

- 增加 `ContainerArtifactCompiler.prepare(request) -> ArtifactBuildPlan` 与
  `materialize(plan, executor) -> ArtifactBundle` 作为无 secret source seam。
- 为 `pp-local-edge`、`pp-server-backend`、compute CPU 与 compute CUDA 产生
  role × platform × variant policy matrix；CUDA 限于其支持的 platform policy，且两个
  compute variant 只暴露 `embedding/v1` / `rerank/v1`。
- 要求窄 `ArtifactBuildExecutor` adapter 返回 immutable OCI、SBOM 与 provenance
  evidence；fake executor 是有效的聚焦证据。
- 声明 non-root、read-only-rootfs、listener、layer-exclusion 与逻辑 mount policy。
  只有 `pp-server-backend` 可获得 canonical-state read-write 资格；compute 只能拥有
  read-only model-catalog 加有界 runtime scratch；edge 只有有界 ephemeral
  status-projection cache。
- 增加/对齐三个 role 和两个 compute variant 的 source recipe；recipe 的存在只是
  source evidence，不是已经完成 build 的结果。
- 只把已有 PR 1 collaboration contract 与 event-log 地基封装进 server artifact 的
  application surface。edge 最多获得有界 read-projection contract，compute 不获得协作权威。
- 通过 role packaging/SBOM inspection 证明 edge 与 compute 不能 import 或配置 collaboration
  event writer。PR 3 不增加持久 `AgentRegistry`/`ProjectWorkBoard` adapter，不绑定 listener，
  也不在 runtime 激活 event log。
- 保持 Model Catalog reference/digest 不透明，model weight 不进入 image layer，且宿主
  Ollama 仅是显式 compatibility adapter。
- **不得**在本地 activation Docker/Compose、绑定 listener、分配 runtime GPU、创建 tunnel
  asset、使用 release credential、部署、迁移 SQLite、推广 LanceDB、重启 MCP 或改变生产。
  受保护 CI adapter 只能进行不 push 的 OCI build verification；它不启动 container、不运行
  GPU inference，也不创建 runtime/deployment receipt。

### 聚焦证据

- 确定性的 request/matrix/rejection test，以及 fake-executor 的 immutable
  evidence/inspection-receipt check；
- entrypoint、listener、mount、authority、non-root/read-only-rootfs 与
  capability-policy inspection；
- 对 model、database、credential、runtime state、log 与 build cache 的 source-recipe 和
  layer-exclusion check；
- artifact inspection：证明 PR 1 event-writer 地基只属于 server，edge/compute 不能 import
  或配置它；以及
- 显式断言 PR 3 从不在本地 activation Docker/Compose，或调用 listener、tunnel、migration、
  promotion、Maintenance、MCP restart 或 production endpoint；受保护 CI verification 仅限
  不 push 的 OCI build evidence。

### 回滚

将 source-level artifact policy 与 recipe 作为一个 review unit 回退。PR 3 不会修改
runtime、image registry、model volume、canonical state 或 Deployment Receipt，因此不存在
operational image rollback step。任何经独立授权 migration 后的 verified runtime rollback
由 PR 5 负责。

## PR 4：`deployment-center`

### 目标

`pp-local-edge` 只作为位于 `http://127.0.0.1:19021` 的静态浏览器入口，宿主
`ppctl` 是 planning adapter。现有 `http://127.0.0.1:19020/mcp` endpoint 仍是
server/backend MCP 入口。其 planning contract 已在 source 中实现，runtime deployment
仍只属于 target：本文没有证明任何 listener、host binding 或 runtime deployment 已启动或验证。
PR 4 没有执行 surface：执行不可用（`deferred_to_pr5`）。

### 当前源码级协作投影（2026-08-14）

- `ProjectWorkingSet` 是不可变、可重建、非权威的项目快照；它只接收同一
  project/coordination session 下的 `AgentSession`、`WorkReceipt`、有界候选
  `ResultReceipt`、`blocker.raised` 与 `conflict.detected` event。每一类输入最多 64 项，
  duplicate、跨 project/session、未来时间与错误 event/result 类型全部 fail closed。
  completed result + artifact reference 不等于 accepted work；accepted-artifact projection
  必须具备 PR1-C06 定义的服务器认证、reviewer 独立 `AcceptanceReceipt`。
- `AgentAwarenessProjection` 是按 audience 生成的只读视图。单次 delta page 最多 20 条，
  完整 canonical JSON 最多 64 KiB；project/session scope、audience visibility、cursor regression、
  cursor 未推进与空 page cursor gap 均 fail closed。
- `project_for(*, audience: AgentSession, deltas: EventPage)` 是值工厂，不是认证边界。role
  string 或调用方自行构造的 `coordinator`/`reviewer` session 不授予可见性。full-work 消费
  必须绑定服务器认证的 active session、当前 policy claim、event-log source lineage、
  audience-bound `EventPage.after_cursor`、projection digest 与独立 `AcceptanceReceipt` 谱系。
  其他已授权角色只读取 owned work、owned work dependency 和可见 event 关联 work。
- projection 保留项目级 `goal_summary`，但删除具体 `WorkReceipt.objective`、
  `AgentIdentity.capabilities` 与 `CollaborationEvent.payload`；raw prompt、隐藏推理、credential
  和 result body 也不会进入该视图。
- 两个合同都声明 `canonical_memory_effect: none`；working set 不是第二真相源，projection
  不能授予 lease、review、acceptance、execution 或 memory authority。可信 feed 必须拒绝
  source substitution、cursor gap/regression、过期 policy/factory revision、伪造 page/digest、
  self-issued receipt 与跨作用域数据。持久化、`AgentRegistry`、`ProjectWorkBoard`、Hook/MCP
  与 runtime binding 仍全部延后到 PR 5。

### 必需修改

- 增加一个只在宿主侧运行的 `DeploymentCenter` deep module，且只有
  `inspect(installation_ref)` 与 `preview(DeploymentPreviewRequest)`。唯一可配置的
  HTTP body 是 `POST <base>/inspect` 的 `{"installation_ref":"local-installation"}`，以及
  `POST <base>/preview` 的
  `{"installation_ref":"local-installation","candidate_manifest":<EndpointManifestV2 JSON>}`。
- 让 `ppctl` 成为仅分派 `inspect` 与 `preview` 的封闭 typed dispatcher；拒绝
  `apply`、enrollment consumption、Shell、Docker、SSH、path 和 generic command request。
- JSON 只能经过有界流式读取：在读取前拒绝超出声明长度的请求，并在分块 body 超过固定
  128 KiB 上限时停止读取，之后两个 operation 都不会 dispatch。
- configuration 缺失时，可选 edge-to-host bridge 默认禁用。只有宿主显式配置时才能
  声明 `http://127.0.0.1:<port>/ppctl/v1`；browser 随后直接发送上面的两种固定 JSON
  `POST` body。no-store bridge configuration 默认报告 `disabled`；`pp-local-edge`
  从不代理它，也不挂载宿主 socket。
- 对 macOS、Linux、Windows/WSL2 增加环境/资源检测；推荐 Deployment Profile，并记录
  受支持的 user override。
- 展示模块、展开镜像、模型、缓存、shadow 与回滚空间估算；宿主 preflight 预计可用空间
  低于安全阈值时 fail closed。
- 展示 enrollment readiness、endpoint status、完整 model-identity comparison、V2
  manifest diff 以及仅供检查的 update classification。由于 PR 4 的 controller projection
  最多只提供脱敏的 active topology，绝不提供 active manifest body 或 persisted receipt，
  因此它只能给出 `no-change`、`enrollment-required` 或 `manual-review`；其余 action class
  需要 PR 5 的 authorized adapter。还要展示仅供检查的
  plan hash 和 receipt projection，并区分 unpersisted contract receipt 与
  server-persisted receipt。
- candidate 只接受无 secret 的 `EndpointManifestV2`；host path、legacy node record 和
  local evidence 只在宿主 adapter 内解析。edge 永远收不到 raw path、credential、Docker
  access、SSH material、SQLite access 或任意宿主执行权。
- 明确 local-edge view/cache 没有权威性。每次 status/plan claim 都必须取得新的 host 或
  server projection。
- 保持当前有界 `ProjectWorkingSet` 与按角色裁剪的 `AgentAwarenessProjection` 源码合同；
  任何 edge/runtime consumer 都只能在 PR 5 受认证 feed 接线后接收同一脱敏投影，不能直接
  读取 event log、raw prompt、隐藏推理、credential、result body 或不受限 event history。
  feed 必须绑定 active `AgentSession`、当前 policy claim、event schema/log/factory revision、
  audience-bound cursor range、source-page/projection digest 与独立 `AcceptanceReceipt` 谱系。
- 保持只读 source/projection contract；持久 registry/work state、`AgentRegistry`、
  `ProjectWorkBoard` 与受认证 server feed、MCP/Hook/runtime binding 仍属于 PR 5。
- collaboration retrieval 与 canonical-memory relevance 必须分离。peer delta 可以改变当前
  work plan，但不能覆盖用户指令，也不能表示为已采纳的项目事实。
- 不联系 node、不传递/消费 enrollment material、不创建 tunnel、不创建 service asset、
  不启动/停止 service、不变更 SQLite、不推广 LanceDB、不启用 Maintenance，也不持久化
  deployment receipt。

### 聚焦证据

- 两个 operation 的接口与 fixed allowlist 测试；
- V2-only candidate、host-path/credential rejection 与 response-redaction 测试；
- profile recommendation/override、module/resource estimate、model-identity、
  manifest-diff、安全的 PR 4 update-class 与 hard safe-space-refusal 测试；
- 对 bridge state 与非权威 projection 做 static browser-asset check；完整 update class 的
  runtime-adapter smoke 由 PR 5 负责；
- 每类 working-set 输入最多 64 项、delta page 最多 20 条、projection 最多 64 KiB，
  以及 cursor resume、role visibility、audience/project isolation、字段脱敏与非权威
  working-set projection 测试；负向用例还必须覆盖调用方自造 coordinator/reviewer session、
  跨 audience cursor 复用、伪造 source/page/projection digest、过期 policy/factory revision、
  self-issued acceptance，以及 completed + artifact 但缺少 `AcceptanceReceipt`；
- 明确断言 PR 4 没有 apply、node contact、credential transfer、tunnel、service、database、
  promotion、Maintenance 或 receipt persistence action。

### 回滚

若规划 UI 被单独激活，则保留 active server manifest 并恢复上一 local-edge 镜像。PR 4
没有 production mutation 需要回滚。前端不可用时 backend 仍可通过窄宿主适配器管理，
canonical state 不受影响。

## PR 5：`migration-operations`

### 目标

在短维护窗口内从现有 systemd runtime 迁移到三端容器，并提供 verified rollback。

### 权限边界与源码状态

PR 5 的类型化 orchestrator 与 durable journal **source contract 已在当前 worktree**；其 live
phase-adapter composition 与 runtime activation 仍标为 **target**。本节不是 listener、container、tunnel、
生产迁移、LanceDB promotion 或 MCP restart 已经运行的证据。服务器拥有的
`MigrationOperation` orchestrator 通过类型化适配器协调整个转换。
浏览器 Deployment Center 与宿主 `ppctl` 仍是 **current read-only** 规划面：不得接受
apply command、打开 canonical SQLite、消费 enrollment material 或持久化 migration receipt。
production composition 中 `pp-core`/`pp-server-backend` 仍是 canonical SQLite 的唯一 writer，
并独占 durable migration lease。`SQLiteMigrationExecutionJournal` 提供跨进程
grant/lease/fence/receipt CAS；in-memory adapter 仅供测试与显式 non-production composition。

该 operation 有意拆成两个不可互换的记录：

- 短生命周期、无 secret 的 **Migration Operation Plan**：绑定源/目标 artifact digest、
  runtime/node/derived-index 观测、canonical-state fingerprint、备份与回滚容量以及 drift fence；
- 明确、绑定 operation 的 **Execution Grant**：只有服务器 admission/risk policy 满足后才
  签发，并匹配新鲜 plan。Deployment Center 的 inspection `plan_hash` 永远不是 Execution Grant。

可变 `apply` 会拒绝 digest-only transport projection，只接受创建 plan 时已检查的
server-memory topology 与 artifact binding。journal 会拒绝并发/重放 operation，在第一个
可变边界精确消费一次 grant，并持久化一次性状态、单调 fence 与无 secret 终态 receipt。
过期 running work 会标记为 `recovery-required`，不能静默重放。

类型化 adapter seam 只允许固定阶段（preflight、backup、rehearsal、enrollment/tunnel 与
capability 检查、cutover、shadow rebuild 与 promotion、Maintenance transition、rollback 和
receipt persistence）。适配器只接收类型化输入和稳定 reason code，绝不接收任意 Shell、Docker、
SSH 或 SQLite command。预期合同覆盖 preflight/drift 拒绝、已验证的在线备份/integrity 证据、
回滚物料选择、shadow generation 质量门禁、Maintenance 启用，以及生产备份五天/临时缓存每日
清理的 retention policy。当前 source contract 仅通过类型化/fake adapter 进行测试，不表示
任何 production 阶段已经成功运行。

### 当前源码切片与剩余 runtime 工作

- **当前源码：**提供服务器拥有的类型化 `MigrationOperations.plan`、`preflight`、`apply` 接缝，
  以及 runtime、node、canonical-state 和 derived-index adapter protocol。
- **当前源码：**创建并校验分离的 Migration Operation Plan，校验类型化 Execution Grant；拒绝
  stale plan、观测变化和不可用节点，并提供 replay/idempotency/fencing 检查。持久的
  server-owned grant/lease/fence 及协作 stores 已有 source/test slice；这不等于 runtime 或
  production 完成。
- **当前源码：**提供 server-only durable collaboration schema/store，覆盖 Agent/session/role/
  plan/work/lease/activity/event/cursor/result/acceptance，包含 server-issued formal-result
  submitter assignment、typed stage event、reconcile algorithm 与 promotion validation/outbox。
- **当前源码 / 聚焦测试已通过：**认证 fresh-client Hook continuation 保留 `session-init`
  authentication lineage，并覆盖 `agent.closed`、lease release、cursor resume 与
  shadow-to-inject gate；真实认证 lifecycle E2E 仍未验证。
- **当前源码 / 聚焦测试已通过：**公开有界 list/claim/heartbeat/review/accept
  `ProjectWorkBoard` 入口和只读 Dashboard Agent topology/work-board/event-timeline projection
  已存在。register 现在从精确认证 session 派生服务器拥有的 `WorkReceipt`；真实
  browser/runtime smoke 仍未验证。
- **当前源码 / 聚焦测试已通过：**Maintenance 协作 composition 与 lifecycle 接线已存在；
  真实 production Maintenance transition 仍未验证。
- **当前源码 / 聚焦测试已通过：**普通认证 tool call 会在同一个 canonical writer 事务中
  reconcile presence、精确 session 的全部 active lease 与增量 feed。
- **当前源码 / 聚焦测试已通过：**`Stop` 对 live leased work 发布有界 `work.progressed`；
  只有 canonical server 已持久化 result 时才发布 `work.submitted`，不会复制 prompt 或
  assistant body。
- **当前源码 / 聚焦测试已通过：**accepted work 会原子进入绑定 receipt/evidence/conflict 的
  pending-only promotion job；adoption 仍是独立治理动作。
- **目标 runtime：**维护窗口前预拉取并验证不可变镜像 digest。
- **目标 runtime：**创建并校验在线备份，在备份副本上演练迁移。
- **目标 runtime：**先启动 local edge 和 compute，验证 enrollment、tunnel、capability 和
  embedding identity。
- **运行时证据待验证：**通过真实 browser/runtime E2E 验证有界 work-board、认证 Hook
  continuation、awareness gate、Dashboard 与 Maintenance 协作 lifecycle；local edge 只能消费
  有界 awareness delta。
- **运行时证据待验证：**在真实认证 lifecycle 中验证 server-owned work issuance、普通 tool
  reconcile、有界 Stop progress/submitted event 与 accepted-result pending-only promotion。
  progress、heartbeat、assumption、raw prompt 与单纯 peer agreement 均没有资格；proposal
  adoption 仍是独立治理动作。
- **目标 runtime：**停止旧 MCP/Maintenance，取得最终备份、迁移，并只把 canonical SQLite
  挂载到 `pp-server-backend`/`pp-core`。
- **目标 runtime：**原子 promotion 前构建并验证 LanceDB shadow generation，成功切换后
  Maintenance 默认开启。
- **目标 runtime：**生产备份最多保留五天，临时缓存每日清理；LanceDB 不能作为恢复权威。
- **目标 runtime：**持久化无 secret 的 Migration Receipt，记录有序阶段结果、回滚状态、安全证据
  hash 和稳定失败原因。

- **当前源码 / 聚焦测试已通过：**Dashboard Agent topology、project work board 与
  collaboration event timeline 已包含 project/session/role filter、cursor refresh 和
  empty/error/stale state；真实 frontend/browser smoke 与 runtime lifecycle 证据仍待验证。

当前 source 只在内存中返回 receipt 形状的 phase result；server persistence adapter 仍是
**target**，不能从 source tests 推断其已经存在。

### 聚焦证据

- 类型化 orchestrator/adapter 合同测试：plan/grant 绑定、阶段顺序、drift fence 与稳定拒绝
  reason code；
- 在隔离状态中演练 backup/integrity/migration/restore；
- single-writer lock 和重复 runtime 拒绝测试；
- tunnel 断开/恢复、outbox replay、generation promotion/rollback smoke；
- 服务重启和 active manifest 恢复 smoke；
- 持久 Agent/work restart recovery、stale-lease reconcile、MCP/Hook scope binding、shadow
  awareness、conflict visibility、accepted result 到 pending proposal，以及 peer progress
  不晋升的 smoke。

上述都属于源码或隔离测试证据，直到另行授权的 production adapter 接线完成。更新本文时，
没有验证 live listener、container、tunnel、production migration、LanceDB promotion、
Maintenance transition 或 MCP restart。

### 回滚

停止新容器，恢复切换前 verified backup 和上一 generation selection，再启动旧
systemd runtime。失败部署的 receipt 和 reason 必须保留用于审计。

## PR 6：`release-readiness`

### 目标

让安装和协调升级可用，同时不削弱受保护的发行权限。

### 目标范围与非声明

PR 6 是**目标/未验证**的 release-readiness contract，不是已完成的发行。source-level Model
Catalog 是不透明 metadata evidence：固定 model identity/revision 与 compatibility/resource
metadata，不包含 weight、path、token、node address，也不声称已签名或已发布。Release Bundle 是
目标 selection/evidence record，绑定 source/package compatibility、profile/variant matrix、immutable
artifact reference 与 Model Catalog ref/digest。Artifact Bundle 仍只是 build-inspection evidence；
它不是 release authority、deployment grant、migration proof 或 server receipt。

- **目标：**提供 macOS、Linux、Windows/WSL2 快速部署入口，以及 `local-all-in-one`、
  `local-cloud`、`split-accelerated` manifest。
- **目标：**记录基础/推荐硬件配置和 update class，但不声称 install 或 upgrade 已成功。
- **目标：**Windows/WSL2 只执行派生推理的本地 build/cache/GPU smoke；它永远不能成为 SQLite
  writer 或 release authority。
- **目标：**受保护 GitHub workflow 产生 immutable OCI/SBOM/provenance evidence 和 selected
  Release Bundle；本文不声称它已经运行、签名、发布 RC 或发布 stable artifact。
- **目标：**server 只作为 verified digest runtime consumer，保持 `pp-server-backend`/`pp-core`
  为 canonical SQLite single writer，并让 LanceDB 保持 rebuildable derived state。本文不声称
  deployment、migration、promotion、MCP restart 或 Maintenance transition 已发生。
- **目标：**公开 PyPI、GHCR stable、GitHub Release 与 release-repository sync 必须在内部验收后
  另行明确授权。
- **目标：**绑定协作角色能力证据：server 拥有 registry/work/event/promotion 权威，edge 只拥有
  有界 awareness display，compute 不拥有上述任何 capability。
- **目标：**Workflow Composer 只以 `shadow-only`、可观测、非权威候选 planner 方式纳入，
  绑定冻结 plan/compiler/hard-gate/tool-policy digest 与 atomic-skill receipt chain；fixed route
  始终是执行权威和确定性 rollback target。
- **目标：**六个 PR 全部合并后运行完整跨 Agent E2E：join、项目级 claim、peer delta、
  review/conflict、accepted receipt、pending proposal、受治理的采纳/不采纳、restart recovery
  与 rollback。

### 聚焦证据

- catalog/bundle identity、compatibility 与无 secret validation 的 source/contract test；
- Windows/WSL2 -> protected GitHub -> verified-digest server -> stable-only authority split 的
  documentation-only validation；
- 最终双语文档一致性报告与 diagram check；
- role-capability/SBOM check 与最终跨 Agent acceptance receipt，其中包含 peer progress 永远
  不会成为 canonical memory 的负向证明；
- Workflow Composer 对抗证据必须拒绝 stale completion、执行中 revision mutation、user-only
  self-attestation、tool escalation、hard-gate 删除/重排、receipt-chain gap、source/contract
  mismatch、取得 execution authority 以及 fixed-route rollback 失败。

### 回滚

若后续 target release gate 失败，选择此前 reviewed 的 immutable Release Bundle/digest，并且
只从 canonical state 重建派生 LanceDB。未经独立授权，不触碰 stable publication 或任何 server
mutation。

## 全栈审计与合并

对 PR 1 到 PR 6 逐一执行：

1. 更新到包含前一 PR merge 或 squash-merge revision 的最新 `main`，不压平自身 review
   boundary，也不把前一功能分支继续作为长期 base。
2. 只运行自身的确定性 seam test 与双语文档门禁。
3. 取得独立 Standards、Spec 与 DeepSec Shield/代码坏味道 receipt，三者绑定同一不可变
   source revision、diff digest、requirement 集合与联合合同 revision，并解决所有 blocking
   finding。DeepSec 保持只读，其 finding 不自动进入 canonical memory。
4. 证据通过后，先取得用户对该 PR 合并的明文授权；授权后才可合并，再执行可回滚的
   compatibility/shadow deployment 切片并验证 rollback，然后才开始下一个 PR。

PR 6 合并后：

5. 集中执行完整跨 profile、跨 Agent E2E 以及 migration/recovery/rollback 演练；它用于最终
   验收，不用于宽泛探索式调试。
6. 检查中英文文档、架构图、badges、资源数字、链接与 role-capability evidence 的一致性。
7. 创建 RC 并执行内部部署验收。
8. 生产迁移和公开 stable 发布分别申请独立授权。

## 本栈非目标

- Kubernetes、service mesh 或公网推理 listener。
- 多个 canonical SQLite 写入者、数据库复制或双写切换。
- compute node 上的任意 Agent Prompt 或工具执行。
- 内容级云数据拦截或 DLP 策略。
- 从 open PR 自动合并、公开发行或修改生产。
