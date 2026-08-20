<!-- GENERATED FILE — DO NOT EDIT. Source: docs/standards/union-six-pr-contract.json; SHA-256: 2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722 -->

# 联合六 PR 交付合同

> **生成视图，请勿直接编辑。请修改规范 JSON、递增 revision，并重新生成两种语言视图。**

- **规范源:** `docs/standards/union-six-pr-contract.json`
- **Revision:** `2026-08-18.1`
- **源文件 SHA-256:** `2c7e4a532e17cde229830479712aabe3ca36a13e21fafbe9fcf781cd91305722`

## 权威与变更控制

Plastic Promise 可组合部署与项目级多 Agent 协作的唯一联合六 PR 合同。

- **`AUTH-01`** — 任何规范范围变更都必须修改此 JSON、递增 revision、重新生成两份 Markdown，并在同一变更集中通过合同漂移门禁。
- **`AUTH-02`** — 生成的 Markdown 不得被作为独立权威手工编辑；发生不一致时，以此 JSON 为准并重新生成。
- **`AUTH-03`** — 实现状态、PR 描述、路线图、架构文档、测试和发行说明可以报告证据，但不得静默缩减、替代或重排本合同。
- **`AUTH-04`** — 每份生成视图、证据台账、审查回执和派生文档清单都必须绑定本合同的精确 revision 与规范 JSON 原始字节 SHA-256。Revision 2026-08-18.1 记录本分支的 structured JSON token 合同修订；由于 repository authority 已在更早的基线引入，previous_canonical 仍为 null。之后每次改变字节的 revision 都必须由不可变 previous bytes 或 Git base object 证明。源码字节变化但 revision 未变、缺少 previous-source 证据或 digest 过期时，必须 fail closed。
- **`AUTH-05`** — 证据台账必须精确覆盖全部 PR requirement ID，并将 implementation、test、runtime、production 保持为独立证据层级。较低或不同层级的证据绝不能满足更高层级。
- **`AUTH-06`** — 派生文档清单是关键中英文文档与资产的完整跟踪族。已知漂移必须记录为阻塞证据，绝不能转换为通过的一致性声明。

### Revision 谱系

- **谱系模式:** `repository-authority-introduction`
- **上一规范源:** 无 — 首次引入 repository authority

#### 仅溯源 audit 记录

| 声称的 revision | SHA-256 | 规范源 | 分类 | 可验证规范源 |
|---|---|---|---|---|
| `2026-08-11.1` | `c5c3b197f6fa192d8a7248b3b37e65c6622d55c4bd09fcd05bc4df23373e7b02` | `audit-preimage:union-six-pr-contract/2026-08-11.1/c5c3b197f6fa192d8a7248b3b37e65c6622d55c4bd09fcd05bc4df23373e7b02` | `provenance-only` | `false` |

## 来源优先级

| 优先级 | 规范源 | 分类 | 规则 |
|---:|---|---|---|
| 1 | Explicit user-approved amendment captured by a new revision of this JSON | `normative-amendment` | 后续明确决策只有在写入此 JSON 并形成新 revision 后才改变联合合同；单次执行授权可以收窄动作，但不会静默改写范围。 |
| 2 | docs/standards/union-six-pr-contract.json | `canonical-normative-source` | 本文件定义强制的交付范围、协作范围、证据、不变量和完成规则。 |
| 3 | Generated English and Chinese union-contract Markdown views | `generated-normative-view` | 这些文件是规范 JSON 的可读投影，仅在其内嵌 digest 与 JSON 原始字节一致时有效。 |
| 4 | Roadmaps, architecture documents, TODO lists, ADRs, diagrams, and release documentation | `derived-guidance` | 派生指导必须引用并符合本合同；不得在遗漏 PR 另一半时宣布其中一半已完成。 |
| 5 | Code, tests, PR descriptions, commits, receipts, and deployed artifacts | `implementation-evidence` | 这些材料证明当前实现状态，但不重新定义强制范围；源码级证据不得被改称为运行时或生产证据。 |
| 6 | Historical conversations, attachments, and superseded planning notes | `provenance-only` | 本 revision 采用后，历史材料只用于解释来源，未经审查的修订不得覆盖规范 JSON。 |

## 治理制品与证据策略

- **证据台账:** `docs/standards/union-six-pr-evidence-ledger.json`
- **派生文档清单:** `docs/standards/union-six-pr-derived-documents.json`
- **证据层级:** `implementation`, `test`, `runtime`, `production`
- **证据状态:** `not-evidenced`, `partial`, `verified`, `not-applicable`

- **`U6-GOV-01`** — 证据台账的 requirement 集合必须与本合同全部 PR 的 delivery、collaboration 和 required-evidence ID 精确相等，并绑定 PR、分组、序号和 statement digest。
- **`U6-GOV-02`** — 每个 evidence receipt 只能属于一个证据层级，并绑定本合同 revision 与 digest、真实且具祖先关系的 Git base/source commit 与 tree object、确定性的 source-tree/diff digest、精确 changed-path 集、有界无 secret 的 evidence-artifact 引用和 UTC 时间。解析后的 artifact 必须逐项重复 receipt 字段。禁止跨层级升格。
- **`U6-GOV-03`** — 派生文档清单枚举关键中英文文档与资产族。tracked-drift 条目是阻塞失败记录，不是豁免或通过状态。
- **`U6-GOV-04`** — 修改合同后必须先递增 revision 再重新生成。本 revision 通过 previous_canonical=null 明确首次引入 repository authority；仅用于溯源的 audit digest 绝不能充当 preimage。之后每次改变字节的 revision 都必须提供可验证的上一规范字节或 Git base object 来证明旧 revision 与 digest。即使当前所有 digest 一致，在源码字节已变化时沿用旧 revision 仍属无效。

## 完成门禁

| ID | 适用范围 | 强制证据层级 | 规则 |
|---|---|---|---|
| `U6-GATE-SOURCE-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `implementation`, `test` | Implementation、test、review、artifact 和 release receipt 必须绑定真实 Git base/source commit object，证明 base 祖先关系，写明两端 tree object，绑定确定性的 source-tree material digest 与 raw diff digest，并枚举精确 changed paths。可变 branch、tag、path、仅具 hash 外形的字符串、status 或文字说明都不是 source identity。 |
| `U6-GATE-REVISION-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `test` | 生成视图、证据台账、派生文档清单及全部受治理 review receipt 必须写明本合同精确 revision 与 raw-source SHA-256。本次 repository-authority introduction 由 Git base 中不存在规范合同来验证；previous_canonical 保持为 null，provenance-only digest 不是 preimage。之后每次改变字节的 revision 具有同样义务。缺少 base evidence、源码变化但 revision 未变，或任何 revision/digest 不一致时都必须 fail closed。 |
| `U6-GATE-DOCUMENT-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `test` | 派生文档清单中每个受影响的关键中英文文档对都必须存在；需要时引用规范合同；保留联合完成规则；且不得存在阻塞 tracked drift。 |
| `U6-GATE-ASSET-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `test` | 每个 PR 影响到的中英文图、SVG、徽章集合、链接集合、资源表和价格表都必须保持结构与语义同步；资产承载状态或架构声明时还必须绑定当前合同 revision。 |
| `U6-GATE-REVIEW-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `test` | 独立 Standards review 与独立 Spec review 必须针对同一已验证 Git base→source 边界、tree/material digest、确定性 diff、changed-path 集、requirement 集合和 contract revision 完成；两者的 evidence artifact 必须深度绑定这些 receipt 字段。两种审查不能互相替代。 |
| `U6-GATE-DEEPSEC-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `test` | 每次正式 PR review 都必须包含独立 DeepSec Shield 与代码坏味道 receipt，并绑定同一 source 与 contract revision。DeepSec reviewer 仅可读取 repository/diff/web 和只读 MCP；不得拥有 shell、文件、数据库、发行或生产写权限；其 finding 绝不自动成为 canonical memory。 |
| `U6-GATE-COMPOSER-01` | `PR6` | `test` | Workflow Composer 以仅 shadow 行为纳入 PR6，并必须通过 fail-closed 对抗风险审计，覆盖冻结 plan revision/digest、compiler revision、mandatory hard-gate-set digest、原子 skill receipt 链、tool-policy digest、stale completion、revision mutation、user-only self-attestation、tool escalation、gate removal 和 fixed-route rollback。 |
| `U6-GATE-EVIDENCE-01` | `PR1`, `PR2`, `PR3`, `PR4`, `PR5`, `PR6` | `implementation`, `test` | 每个 PR requirement 必须在四个证据层级中都有显式 evidence-ledger 状态。partial、verified 与 not-applicable 都必须具有同层级 receipt，且其 Git 边界和解析后的 evidence artifact 均通过验证；runtime 或 production 标记为 verified 时还必须具有该层级要求的精确 authority。implementation/test 证据不得通过措辞、聚合或 reviewer 意见被升格。 |

## 实验功能 disposition

| 功能 | PR | Disposition | Activation | Rollback | 规则 |
|---|---|---|---|---|---|
| `workflow-composer` | `PR6` | `included-shadow` | `shadow-only` | `fixed-route` | Workflow Composer 作为可观测、非权威的 shadow planner 纳入本次交付。它可以输出候选原子 skill 计划，但在后续单独审查的合同 revision 改变 disposition 前，固定 route 始终是执行权威。 |

## 跨 PR 不变量

### `U6-INV-01` — 唯一联合交付线

部署与协作是同一条 PR1 到 PR6 依赖链中的两个强制范围，不是平行路线、可选插件，也不能互相替代。

### `U6-INV-02` — 禁止半完成 PR

只要交付范围、协作范围或强制证据中的任一项缺失，该 PR 就不完整。使用“完成、合并、发行、部署”等表述时，必须指出实际证明的证据层级。

### `U6-INV-03` — 三个数据平面保持分离

Coordination 保存短期 presence、租约、进度、finding、blocker、conflict 和 receipt；Project Working Set 是面向当前任务或 PR 的有界可重建投影；Canonical Memory 保存经治理的稳定事实、决策、原则和经验。任何平面都不得静默取得另一平面的权威。

### `U6-INV-04` — 服务器单写者权威

服务器是 coordination、规范 SQLite、治理、accepted-result 验证、receipt 持久化和 LanceDB 推广决策的唯一权威。Edge/compute 声明、project ID、manifest、capability 和模型身份都只是校验输入，绝不是授权。

### `U6-INV-05` — 严格项目与会话作用域

每个 Agent session、work item、lease、event、cursor、receipt、retrieval、promotion candidate 和生命周期动作都必须绑定规范 project_id 与 coordination_session_id。跨项目或歧义旧状态必须 fail closed 或进入不可认领隔离区。

### `U6-INV-06` — 协作必须有类型且有界，不共享完整 prompt

Agent 只交换有类型、按 audience 裁剪的摘要、引用、证据 digest、状态迁移和 receipt。原始 prompt、私有或隐藏推理、凭据、lease token、provider secret、完整结果正文和无限制 peer history 不得进入协作投影或记忆提案。

### `U6-INV-07` — 事件游标拉取是正确性路径

增量 awareness 必须使用绑定项目、会话和 audience 的 cursor pull，并支持重放、单调 from/to 位置、gap 检测和有界分页。SSE 等推送只能优化前端体验，不能成为唯一正确性通道。

### `U6-INV-08` — 证据治理的记忆晋升

peer progress、heartbeat、assumption、finding、多 Agent 一致和 semantic capture 都不得成为 canonical memory。只有经服务器验证、带稳定证据和独立 reviewer 权威、项目作用域明确且冲突状态已处理的 accepted result 才能成为 pending proposal；adoption 仍是独立治理动作。

### `U6-INV-09` — 共享租约原语，分离权限平面

开发 Agent work 与 compute inference job 复用同一套 lease、fence、heartbeat、result receipt、retry、reconcile、幂等和项目作用域语义合同；但记录类型、持久化表、operation policy、capability 和授权 adapter 必须分离，且不能互相转换。

### `U6-INV-10` — 最小权限与独立验收

Agent identity 必须由服务器绑定到版本化 role/tool/capability policy。被委托 Agent 不得自授 reviewer/coordinator 可见性、验收自己的 finding、修改 canonical memory、合并 PR、部署、推广 generation、启动 Maintenance 或修改其他 Agent 的事件。

### `U6-INV-11` — 不可变谱系与回执

accepted artifact、awareness projection、deployment plan、migration、release 和 promotion 必须绑定不可变的 source identity、revision、digest、cursor、fence 和 typed receipt。仅有 completed 状态与 artifact ref 不足以构成 accepted result。

### `U6-INV-12` — 源码证据不等于运行时证据

合同、fake adapter、源码 recipe、单元测试、image descriptor 或文档 receipt 只证明其声明层级。没有匹配的外部 receipt，就不能证明 listener、持久 runtime、migration、restart、promotion、Maintenance transition、RC 发布、stable release 或生产验收。

### `U6-INV-13` — 双语生成一致性

中英文联合合同必须由同一份 JSON 原始字节生成并携带相同 SHA-256。每个 PR 还必须同步受影响的中英文文档、架构图、徽章、链接以及资源或价格表。

### `U6-INV-14` — 统一 UTC 语义但不修改宿主时区

服务器后端是唯一 canonical 时钟权威。每个持久化时间戳或权威 wire 时间戳都必须由服务器签发，并使用带时区的规范 UTC Z 形式。Edge、client 与 compute 时间戳只能作为诊断性的 source observation，绝不得影响排序、租约过期、fence、幂等、回执、晋升或游标推进。前端可以按本地时区展示，但不得把本地化时间回写为权威值。安装过程不得修改 Linux、macOS、Windows、WSL2、edge、server 或 compute 宿主机时区。

### `U6-INV-15` — Workflow Composer 服从硬门

Workflow Composer 以仅 shadow 行为纳入 PR6。它可以提出原子 skill 计划和新的冻结 plan revision，但确定性编译器必须补回不可移除的安全、审查、证据、迁移、发行和授权硬门。它不得篡改正在执行的 revision、自证 user-only 阶段、豁免本合同任何条目或替代固定执行 route。

### `U6-INV-16` — 独立 Standards、Spec 与 DeepSec 审查

每个 PR 都必须具备三条独立审查通道，并绑定同一不可变 source revision、diff digest、requirement 集合和 contract revision：Standards 一致性、Spec 一致性及 DeepSec Shield/代码坏味道审查。缺失、过期、自签、跨 revision 或不匹配的 receipt 必须阻塞完成。

### `U6-INV-17` — AcceptanceReceipt 是认证决策

AcceptanceReceipt 是服务器认证、绑定项目/会话、针对一个 WorkReceipt 与 ResultReceipt 的决策；它必须包含独立 submitter/reviewer session、不可变 work/result/evidence digest、policy revision、conflict decision 和 UTC 签发时间。result status、artifact reference、reviewer 字符串或自我声明都不能替代它。

## 完成矩阵

> **所有分组均为强制项；缺失任一项即表示 PR 未完成。**

| PR | 依赖 | 强制门禁 | 目标 | 交付范围 | 协作范围 | 强制证据 |
|---|---|---:|---|---:|---:|---:|
| **PR1** | — | 7 | 建立受治理的推理路由接缝，以及供后续所有 PR 安全复用的不可变项目级协作合同。 | 4 项 | 6 项 | 6 项 |
| **PR2** | `PR1` | 7 | 定义封闭的三端点权限，并让 compute job 与 Agent work 遵循相同租约语义，同时保持权限平面分离。 | 3 项 | 4 项 | 5 项 |
| **PR3** | `PR2` | 7 | 证明按角色划分的制品边界，并将 Passive Memory Hook 接入类型化协作事件，同时防止瞬时 Agent 活动晋升为记忆。 | 4 项 | 4 项 | 5 项 |
| **PR4** | `PR3` | 7 | 交付只读部署规划面，以及经认证且有界的协作投影；context_supply 将其放在 canonical memory 检索旁边合成，而不是混入其中。 | 3 项 | 5 项 | 5 项 |
| **PR5** | `PR4` | 7 | 激活服务器拥有的持久三端迁移运行时，并完整接通 Agent registry、work board、Hook、MCP、Maintenance、promotion 与前端生命周期，且可逆恢复。所有 embedding、rerank 与 structured-JSON 推理统一通过 pp-compute-node；pp-server-backend 保持为 canonical 读写、调度、租约和 reconcile 权威。 | 8 项 | 10 项 | 9 项 |
| **PR6** | `PR5` | 8 | 让三端系统可安装、可升级、可审计、可发行，同时证明最终协作权限边界和跨 Agent 行为。 | 4 项 | 4 项 | 7 项 |

## PR 合同明细

## PR1 — 路由内核与协作地基

**目标:** 建立受治理的推理路由接缝，以及供后续所有 PR 安全复用的不可变项目级协作合同。

- **依赖:** —
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR1-D01`** — 为 query embedding、index/outbox embedding 和 rerank 引入统一类型化路由接口，并将本地节点、云端与确定性降级 adapter 放在其后。
- **`PR1-D02`** — 强制精确 embedding identity 兼容；身份不一致时显式选择 profile；提供有界 health/circuit 恢复；分离 MCP 核心 readiness 与可选结构化推理 readiness。
- **`PR1-D03`** — 为不可用的派生任务提供持久 queue/reconcile 语义，不新增规范写入者，也不把 LanceDB 当作恢复权威。
- **`PR1-D04`** — 将所有现有 task enqueue、dedupe、claim、heartbeat、complete、verify/review、inbox、release/abandon、recovery 路径及 schema/index/trigger 约束迁移为强制 project_id；歧义旧行进入隔离且不可认领。

### 协作范围

- **`PR1-C01`** — 定义无 secret 的类型化合同：AgentIdentity、AgentSession、ProjectScope、CoordinationSession、WorkItem、WorkLease、CollaborationEvent、EventCursor/EventPage、WorkReceipt、ResultReceipt、ReviewReceipt 和 AcceptanceReceipt。
- **`PR1-C02`** — 增加按项目/会话隔离的 append-only CollaborationEventLog 地基，具备幂等 event identity、因果父级检查、过期、audience 过滤、有界 cursor 读取、损坏检测和显式 SQLite 注入；此阶段不接生产 listener。
- **`PR1-C03`** — 在已加固 task lifecycle 上定义 ProjectWorkBoard 边界，避免 handler 长期成为 SQL 权威；持久化 adapter 与可变 registry/work-board 生命周期留给 PR5。
- **`PR1-C04`** — 将 Agent identity/session 值绑定到版本化最小权限 role/tool/capability policy 合同。调用方自报 role 或 capability 不授予权限；认证 runtime binding 在 PR5 完成。
- **`PR1-C05`** — 编码 Coordination、Project Working Set 与 Canonical Memory 的分离，包括 peer progress 与未验证 finding 绝不自动具备晋升资格。
- **`PR1-C06`** — 将 AcceptanceReceipt 定义为服务器认证的不可变决策，绑定 acceptance_receipt_id、project_id、coordination_session_id、work_item_id、WorkReceipt、ResultReceipt、submitter_agent_session_id、独立 reviewer_agent_session_id、review_policy_revision、source_revision、work/result/evidence digest、decision、conflict_state 和 issued_at_utc。自我验收、调用方自报 reviewer 身份、未解决 conflict、过期 policy、跨作用域 receipt 或 digest 不匹配必须 fail closed。

### 强制证据

- **`PR1-E01`** — 聚焦 query/index/rerank 路由测试，以及 exact identity、profile selection、降级、circuit recovery 和可选 provider health 证据。
- **`PR1-E02`** — 覆盖全部强制协作值、WorkItem/WorkLease 生命周期边界和最小权限 policy binding 的合同校验及 secret/private-reasoning 拒绝矩阵。
- **`PR1-E03`** — Event log 的 append/idempotency/causal scope/audience/expiry/cursor/corruption 测试，以及覆盖全部加固 task operation 和迁移/恢复事务的跨项目负向矩阵。
- **`PR1-E04`** — 双语文档与生成式联合合同漂移门禁，并明确 upgrade、degradation、rollback、quarantine 和未激活生产的声明。
- **`PR1-E05`** — AcceptanceReceipt 一致性与负向证据，覆盖服务器认证签发、独立 reviewer session、WorkReceipt/ResultReceipt/digest 绑定、policy revision、conflict decision、replay/idempotency、自我验收、过期 policy、跨项目/会话作用域及被篡改或不匹配证据。
- **`PR1-E06`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有路由/task 交付接缝与完整不可变协作地基都实现并有证据时，PR1 才完成。仅有 task 项目隔离或协作值类都不充分。

## PR2 — 端点合同与共享租约语义

**目标:** 定义封闭的三端点权限，并让 compute job 与 Agent work 遵循相同租约语义，同时保持权限平面分离。

- **依赖:** `PR1`
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR2-D01`** — 通过封闭的 EndpointAuthority resolve/assess/verify_completion 接口定义版本化 pp-local-edge、pp-server-backend 和 pp-compute-node 合同。
- **`PR2-D02`** — 编译服务器拥有的 role/action 权限 profile：edge 只提交 intent 和读取有界投影；server 拥有 canonical state、推理任务调度与校验，以及协作/治理决策；compute 可执行有界 embedding、rerank 与 structured-JSON 工作，并且只能返回派生证据。
- **`PR2-D03`** — 定义 manifest/hello/capability/result 身份、精确模型兼容、资源下限、schema version、幂等、timeout、cancellation、terminal reason、resource report 和 golden-probe 证据，但不激活 runtime 持久化。

### 协作范围

- **`PR2-C01`** — 为 Lease、Fence、Heartbeat、ResultReceipt、RetryDecision、ReconcileDecision、幂等、过期和 ProjectScope 创建统一语义合同，并为 Agent work 与 compute job 提供 conformance adapter。
- **`PR2-C02`** — 保持 Agent WorkItem/WorkLease 与 compute job/lease 的记录、表、operation policy、capability、result body 和授权 adapter 分离；相同 project_id 或结构转换都不能跨越边界。
- **`PR2-C03`** — 使用服务器签发的 current fence 和服务器拥有的 retry/reconcile 决策。stale、expired、mismatched、replayed 或跨平面 completion 必须以稳定 reason code fail closed。
- **`PR2-C04`** — 拒绝 compute 获得任何 Task Queue、AgentRegistry、ProjectWorkBoard、collaboration-event-writer、awareness、memory/knowledge promotion、merge、deployment、Maintenance、canonical SQLite 和 LanceDB promotion 权限。

### 强制证据

- **`PR2-E01`** — Endpoint schema round-trip、secret rejection、封闭权限矩阵、exact identity、protocol compatibility、resource floor 和角色拒绝测试。
- **`PR2-E02`** — 同一套共享租约语义 conformance suite 必须分别对 Agent-work adapter 与 compute-job adapter 运行，覆盖 fence、heartbeat、receipt、retry、reconcile、expiry、idempotency 和 project scope。
- **`PR2-E03`** — 负向证明：compute completion 不能完成 Agent work，Agent receipt 不能满足 compute job，调用方声明不能授予任一权限。
- **`PR2-E04`** — 源码级 upgrade/degradation/rollback 和双语文档证据，并明确不声称 listener、persistence、migration、deployment 或 production 已完成。
- **`PR2-E05`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有 endpoint authority 与 Agent/compute 共用租约语义都通过 conformance 与隔离证明时，PR2 才完成。只有 compute lease 类并不充分。

## PR3 — 容器制品与 Passive Memory 协作事件

**目标:** 证明按角色划分的制品边界，并将 Passive Memory Hook 接入类型化协作事件，同时防止瞬时 Agent 活动晋升为记忆。

- **依赖:** `PR2`
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR3-D01`** — 提供无 secret 的 ContainerArtifactCompiler prepare/materialize 接缝，以及 edge、server、compute CPU、compute CUDA 的 role × platform × variant 矩阵。
- **`PR3-D02`** — 强制不可变 OCI、SBOM、provenance、application inventory、role label、non-root、read-only-rootfs、listener、mount 与 layer exclusion 证据；模型权重、数据库、secret、runtime state、日志和 build cache 不得进入镜像层。
- **`PR3-D03`** — collaboration writer/runtime module 只封装进 server artifact；edge 只获得有界 projection/submission contract；compute 物理上不包含 collaboration package 或配置面。
- **`PR3-D04`** — PR3 不得执行本地 runtime 激活、listener 绑定、tunnel 创建、registry push、migration、promotion、Maintenance、MCP restart 或生产修改；受保护的 no-push 构建验证只属于证据。

### 协作范围

- **`PR3-C01`** — Stop 时发布有界且绑定项目/会话的 work.progressed 或 work.submitted event；只有已存在服务器认证的独立 acceptance receipt 时才发布 accepted-result event。
- **`PR3-C02`** — semantic capture 仍只生成 pending proposal。普通进度、peer event、assumption、finding 和尚未 accepted 的 submitted work 绝不成为 memory proposal 或 canonical memory。
- **`PR3-C03`** — 只有带 typed receipt、稳定证据、项目作用域和冲突状态的 accepted work 才能进入 CollaborationMemoryPromoter 输入合同；输出只能是 pending proposal，adoption 仍然分离。
- **`PR3-C04`** — Hook event payload 只包含类型化摘要、artifact/evidence ref 与 causal link；拒绝 raw prompt、hidden reasoning、credential、result body、lease token 和 provider secret。

### 强制证据

- **`PR3-E01`** — Artifact policy、final-rootfs inventory、SBOM/provenance、role label、mount/layer 与 import-denial 检查，证明 collaboration 仅在 server 且 compute 完全排除。
- **`PR3-E02`** — Stop Hook 的 progress/submission/accepted event 选择、scope binding、idempotency、fail-open retry、有界 payload 与 secret/private-reasoning 拒绝测试。
- **`PR3-E03`** — Promotion 负向矩阵，证明 peer progress、多 Agent 一致、completed-plus-artifact、自我验收、assumption、finding 和 semantic capture 都不能绕过认证 acceptance receipt 或创建 canonical memory。
- **`PR3-E04`** — 显式 no-runtime/no-production 激活断言，以及双语 artifact/Hook 边界文档。
- **`PR3-E05`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有 artifact isolation 与 Passive Memory 协作事件/晋升边界都实现并测试后，PR3 才完成。仅有 server-only packaging 不能完成 PR3。

## PR4 — Deployment Center 与协作检索投影

**目标:** 交付只读部署规划面，以及经认证且有界的协作投影；context_supply 将其放在 canonical memory 检索旁边合成，而不是混入其中。

- **依赖:** `PR3`
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR4-D01`** — 提供只含 inspect/preview 的 host-only DeploymentCenter 和封闭 ppctl dispatcher；apply、shell、Docker、SSH、任意 path/command、credential transfer、node contact、service、database mutation、promotion 与 receipt persistence 均不可用。
- **`PR4-D02`** — 使用有界 JSON 输入、无 secret manifest candidate、脱敏新鲜投影、macOS/Linux/Windows-WSL2 资源检查、profile 推荐/覆盖、模型身份比较、空间不足拒绝和仅 inspection 的 update class。
- **`PR4-D03`** — 保持 pp-local-edge 为非权威静态浏览器入口，保持现有 backend MCP endpoint 为权威入口；每个 status 或 plan 声明都需要新鲜 host/server projection。

### 协作范围

- **`PR4-C01`** — 实现有界、可重建、非权威的 ProjectWorkingSet，以及按 role/audience 裁剪的 AgentAwarenessProjection delta view；二者都不授予 lease、execution、review 或 memory 权限。
- **`PR4-C02`** — 项目记忆与 peer collaboration event 必须通过分离的 ranking path 检索。context_supply 在记忆检索后合成独立 collaboration 字段；peer relevance 不得改变 canonical-memory score 或覆盖用户指令。
- **`PR4-C03`** — 提供 role-aware relevance，考虑 work dependency、same module/symbol/artifact/decision、causal distance、freshness 和 severity；开放 conflict 与 blocker event 优先于普通 progress。
- **`PR4-C04`** — 将每个 collaboration delta 绑定到服务器认证的精确 source tuple：source_kind=collaboration-event-log、source_authority=server、project_id、coordination_session_id、audience、AgentSession policy_revision、event_schema_revision、event_log_revision、cursor_from、cursor_to、source_page_digest、projection_factory_revision 和 generated_at_utc。拒绝 role/audience 自报、source substitution、cursor 回退/gap、伪造 page/digest、过期 policy/factory revision 和调用方任意构造的 projection。
- **`PR4-C05`** — Accepted artifact 必须使用 PR1-C06 的精确服务器认证独立 AcceptanceReceipt 合同，并在 source tuple 中包含 acceptance_receipt_id 与 receipt digest。completed 加 artifact_refs、reviewer 字符串或未绑定 ResultReceipt 都不充分。objective、capability、event payload、prompt、private reasoning、credential 和 result body 由服务器拥有的 projection factory 脱敏。

### 强制证据

- **`PR4-E01`** — DeploymentCenter 两操作 allowlist、有界 streaming input、V2-only candidate、redaction、profile/resource/model comparison、安全空间拒绝和 no-mutation 测试。
- **`PR4-E02`** — Working set 与 awareness 边界测试，覆盖 project/session 隔离及精确服务器认证 source tuple：source kind/authority、认证 role/audience、policy/event-schema/event-log/projection-factory revision、source page digest、cursor_from/to resume 与 gap 行为、generated_at_utc、有界 source/page/output 大小、存在 accepted artifact 时的 AcceptanceReceipt 谱系，以及脱敏。
- **`PR4-E03`** — context_supply 集成证明：memory 与 collaboration 分别检索/排序后再合成，conflict/blocker 优先，且无 canonical-memory effect。
- **`PR4-E04`** — 负向测试覆盖伪造 reviewer/coordinator role 或 audience、伪造/自签 AcceptanceReceipt、source kind/authority/revision/digest 替换、调用方构造 projection、cursor skip/replay 歧义、raw prompt/private reasoning 泄漏、过期 policy/projection-factory revision，以及 peer 覆盖用户或 canonical context。
- **`PR4-E05`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有只读 Deployment Center 与经认证的 collaboration retrieval/context_supply 合成都可用时，PR4 才完成。只有 projection value class 而没有可信 feed 与 retrieval consumer 不充分。

## PR5 — 迁移操作与协作运行时

**目标:** 激活服务器拥有的持久三端迁移运行时，并完整接通 Agent registry、work board、Hook、MCP、Maintenance、promotion 与前端生命周期，且可逆恢复。所有 embedding、rerank 与 structured-JSON 推理统一通过 pp-compute-node；pp-server-backend 保持为 canonical 读写、调度、租约和 reconcile 权威。

- **依赖:** `PR4`
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR5-D01`** — 提供服务器拥有的类型化 MigrationOperation plan/grant/lease/fence/phase/receipt orchestrator，具备备份门禁持久 journal、drift rejection、one-shot grant、稳定失败和重启恢复；browser/ppctl 仍只读。
- **`PR5-D02`** — 通过类型化 adapter 执行经授权的 preflight、不可变镜像验证、在线备份与完整性演练、单写者切换、endpoint/tunnel/capability/model 检查、SQLite migration、shadow LanceDB rebuild/atomic promotion、Maintenance transition、rollback 和无 secret receipt 持久化。
- **`PR5-D03`** — canonical SQLite 仅由 pp-server-backend/pp-core 挂载，LanceDB 保持可重建派生状态；成功切换后默认启用 Maintenance；生产备份最多保留五天，临时缓存每日清理。
- **`PR5-D04`** — 强制端点职责分离：pp-server-backend 不得构造或调用 hosted/local/raw embedding、rerank 或 structured-JSON provider。只有 pp-compute-node 可以持有 provider adapter、模型运行时、云端 URL 与 provider credential；服务器请求必须使用经认证的私有 compute-node transport，否则返回稳定的 defer/original-order 结果。
- **`PR5-D05`** — 支持 control plane 的 inference_mode：local、cloud 和 hybrid。路由必须在每次请求时读取 active control revision，使模式切换无需重启 MCP 即可在下一请求生效；hybrid 只能选择 provider class 与 capability identity 满足操作要求的已注册 compute node。
- **`PR5-D06`** — 项目 compute credential 只能投影到 compute：Dashboard 只写不读，任何 response 或 receipt 都不得回显；只有在原子 compute profile 激活并产生 identity revalidation receipt，且 model、revision、dimension、normalization 与 structured-JSON identity 字段匹配后，凭据才可激活。
- **`PR5-D07`** — 将 structured JSON 作为 compute-node 的一等 capability，强制有界 prompt、payload、output、timeout 与 UTF-8 限制；token 请求由 provider 自适应，不再设置任意的 8192 token 本地上限。同时保持严格 model/revision identity 检查、无 secret diagnostics 和持久 result receipt；服务器端可以请求该 capability，但不得执行推理或组装 provider 上下文。
- **`PR5-D08`** — 当没有具备所需精确 identity 的健康 compute node 时，必须以操作专属的稳定降级 fail closed：embedding 与 structured JSON defer 并进入 retry/reconcile，rerank 返回 original-order，服务器端不得回退到 provider。

### 协作范围

- **`PR5-C01`** — 在服务器单写者权威后持久化 AgentRegistry 与 ProjectWorkBoard，包括 join/active/idle/stale/closed Agent 生命周期，以及 proposed/ready/leased/in_progress/submitted/reviewing/accepted/rework/expired work 生命周期。
- **`PR5-C02`** — 扩展 session-init 以注册认证 Agent session，并返回绑定 policy、working-set summary、assigned work、peer delta 和 cursor；heartbeat/tool call 负责 reconcile presence、lease 与增量 feed 状态。
- **`PR5-C03`** — UserPromptSubmit 分别加载有界项目记忆与 collaboration delta；Stop 发布类型化 progress/result event 及稳定用户事实 pending proposal；SessionEnd 发布 agent.closed、释放 lease、推进/记录 cursor state 并清理本地 turn state。
- **`PR5-C04`** — sp-stage 发布类型化 stage started/completed/blocked/receipt event；step-closure 将 collaboration result receipt 与可选 memory proposal 分别输出。
- **`PR5-C05`** — Maintenance 清理 stale presence、超过 retention 的 event 与 abandoned lease；将 expired work reconcile 到正确状态；保留 append-only audit evidence；清理过程绝不 adopt proposal 或删除 canonical memory。
- **`PR5-C06`** — 先以 shadow 激活认证 server feed 与 collaboration awareness，再进入注入；前端展示 Agent topology、project work board 和 event timeline，并支持项目/会话/角色过滤与 cursor-based refresh。
- **`PR5-C07`** — 实现 CollaborationMemoryPromoter，只有 accepted、带证据且完成冲突检查的 work 才能产生 pending proposal。Promoter 不得 adopt、overwrite、forget、merge 或直接更新 canonical memory。
- **`PR5-C08`** — compute inference work 与 Agent work 共用 lease、fence、heartbeat、retry、reconcile、project scope 和 result receipt 语义，同时保持独立 operation policy 与 adapter 类；compute identity 不得进入 Agent 协作权威体系。
- **`PR5-C09`** — 服务器调度只记录有界 operation intent、capability requirement、identity expectation 和 receipt ref。Provider prompt、原始文档、API key、模型响应与 hidden reasoning 必须留在 compute-node 边界内，绝不作为协作事件发出。
- **`PR5-C10`** — active inference mode 具有项目作用域并支持热路由。模式转换只影响新 work，保留进行中的 lease 与 receipt；新选中的 compute node 在接收 work 前必须先完成 identity revalidation。

### 强制证据

- **`PR5-E01`** — 在隔离及经授权 runtime slice 中提供 migration plan/grant binding、backup/integrity/rehearsal、drift/fence、single-writer、phase order、restart、rollback、outbox replay、shadow generation、promotion、Maintenance、retention 和稳定拒绝证据。
- **`PR5-E02`** — 持久 Agent/session/work/lease/event/cursor 的重启恢复、heartbeat timeout、stale presence、abandoned lease、retention、idempotency、fencing 和 reconcile 测试。
- **`PR5-E03`** — 认证 MCP/Hook 生命周期 E2E，覆盖 session-init、UserPromptSubmit、Stop、SessionEnd、sp-stage、step-closure、cursor resume、shadow-to-inject gate、scope enforcement、event emission 和 lease release。
- **`PR5-E04`** — Promotion 测试证明 accepted-result-to-pending-proposal、conflict blocking、独立 reviewer authority，以及 progress、assumption、peer agreement、semantic capture、自我验收或 raw prompt 不晋升。
- **`PR5-E05`** — 前端 Agent topology、work board 和 event timeline 烟测证据，覆盖有界 role-aware 数据、项目隔离、cursor refresh、empty/error/stale 状态，且无 canonical mutation 控件。
- **`PR5-E06`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。
- **`PR5-E07`** — 端点角色与 import/runtime denial 证据必须证明 server 无法构造 direct cloud、Ollama、Jina、SiliconFlow 或 structured-JSON provider 路径，同时 compute-node 只能通过私有 transport 暴露其声明的 local/cloud/hybrid capability。
- **`PR5-E08`** — 路由证据必须覆盖 local、cloud、hybrid control revision、无需重启 MCP 的热切换、精确 capability/model identity 匹配、compute-only credential 投影、原子 profile 激活和 identity revalidation receipt，并证明无 secret 泄漏。
- **`PR5-E09`** — 降级与恢复证据必须证明无节点或 identity mismatch 时行为稳定且按操作区分：embedding/structured JSON defer 并进入 retry/reconcile，rerank 保持 original-order，且无 server provider fallback 或上下文执行。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有 migration operation 与完整持久协作生命周期都已接线、可恢复、可观测且受治理时，PR5 才完成。仅有 source journal、Hook contract 或前端 mock 不充分。

## PR6 — 发行就绪与角色能力合同

**目标:** 让三端系统可安装、可升级、可审计、可发行，同时证明最终协作权限边界和跨 Agent 行为。

- **依赖:** `PR5`
- **强制门禁:** `U6-GATE-SOURCE-01`, `U6-GATE-REVISION-01`, `U6-GATE-DOCUMENT-01`, `U6-GATE-ASSET-01`, `U6-GATE-REVIEW-01`, `U6-GATE-DEEPSEC-01`, `U6-GATE-COMPOSER-01`, `U6-GATE-EVIDENCE-01`

### 交付范围

- **`PR6-D01`** — 提供跨 macOS、Linux、Windows/WSL2 的安装入口，支持 local-all-in-one、local-cloud、split-accelerated 可选 profile、模块大小/资源估算、安全空间拒绝、基础/推荐配置和可逆热切换。
- **`PR6-D02`** — 构建 RC/release bundle，绑定 source revision、Python artifact、Model Catalog identity、OCI digest、SBOM、provenance、profile/variant compatibility、installer asset、文档一致性和不可变 rollback reference。
- **`PR6-D03`** — Windows/WSL2 只负责本地 build/cache/GPU smoke 与派生推理；受保护 GitHub workflow 生成不可变发行证据；server 只拉取并运行已验证 digest。Stable PyPI、GHCR、GitHub Release、发行仓库同步和生产 rollout 均需单独明确授权。
- **`PR6-D04`** — 验证 upgrade、rollback、restart recovery、旧客户端兼容、single-writer 保持、LanceDB 可重建、五天备份保留、每日缓存清理和带时区 UTC 行为，且不修改宿主机时区。

### 协作范围

- **`PR6-C01`** — 声明并验证 OCI role label：agent-registry-authority、work-board-authority、canonical-memory-authority、collaboration-event-writer、bounded-awareness-display、bounded-event-submission、compute-execution 和 local-edge。
- **`PR6-C02`** — 证明 server 是唯一 registry/work-board/event/promotion/canonical 权威；edge 可以展示有界投影和提交类型化有界 event，但不能读取 raw history 或修改 canonical state；compute 不含开发 Agent 协作 package、tool 或 authority。
- **`PR6-C03`** — 运行最终跨 Agent E2E：认证 join、scoped claim、lease/fence heartbeat、peer delta、conflict/review、独立 accepted receipt、pending proposal、治理 adoption/rejection、restart/cursor recovery、stale cleanup 和 rollback。
- **`PR6-C04`** — 将 Workflow Composer 以仅 shadow 行为纳入交付，绑定冻结 plan_revision 与 plan_digest、compiler_revision、mandatory_hard_gate_set_digest、原子 skill receipt 链、tool_policy_digest、有界 Agent tool、可观测 candidate 与 fixed route 对比，以及确定性回退固定 route。它不拥有执行或授权权威。

### 强制证据

- **`PR6-E01`** — 跨平台 installer/profile matrix、资源与磁盘 preflight、hot-switch/upgrade/rollback、模型身份兼容和旧客户端兼容 receipt。
- **`PR6-E02`** — 不可变 RC bundle 验证，覆盖 source、package、OCI digest、SBOM、provenance、Model Catalog、role label、final rootfs、文档一致性、声称存在时的 signature/attestation 和 rollback reference。
- **`PR6-E03`** — Role-capability 负向矩阵，证明 edge/compute 不能获得 server 权限、compute 不含 collaboration surface，且未审查声明、manifest、capability、project_id 或 model identity 都不能授予权限。
- **`PR6-E04`** — 完整 cross-profile 与 cross-Agent acceptance receipt，包括 peer progress 绝不成为 canonical memory 的负向证明、restart/cursor recovery、Maintenance cleanup、migration rollback 和 derived-index rebuild。
- **`PR6-E05`** — 强制 Workflow Composer included-shadow 对抗证据必须证明精确 plan/compiler/hard-gate/tool-policy 绑定，并拒绝 stale completion、执行中 revision 修改、user-only 自签、tool escalation、gate 删除或重排、receipt 链缺口、source/contract 不匹配、获取执行权威及 fixed-route rollback 失败。
- **`PR6-E06`** — RC 创建、内部部署、生产迁移、公开 PyPI/GHCR/GitHub Release、stable 发布和发行仓库同步分别需要明确授权 receipt；任何一种授权都不隐含其他授权。
- **`PR6-E07`** — 独立 Standards、Spec 与 DeepSec Shield/代码坏味道 review receipt，绑定同一不可变 source revision、diff digest、requirement 集合和联合合同 revision，并证明 DeepSec 最小权限及 finding 不进入 canonical memory。

### 完成规则

- `operator`: `all`
- `required_groups`: `delivery_scope`, `collaboration_scope`, `required_evidence`
- `prohibits_partial_completion`: `true`
- 只有安装/发行就绪与最终角色/跨 Agent 验收都具备证据时，PR6 才完成。只有 RC contract、artifact bundle 或 role label 而没有 runtime E2E 不充分；stable publication 仍需单独授权。
