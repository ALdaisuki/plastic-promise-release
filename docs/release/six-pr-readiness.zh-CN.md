# 联合六 PR 就绪度与受控发行计划

英文对等页：[`six-pr-readiness.md`](six-pr-readiness.md)。

> **截至 2026 年 8 月 14 日的状态：**本文是机器可读
> [联合六 PR 合同](../standards/union-six-pr-contract.json) revision `2026-08-18.1`
> 的派生就绪度投影。范围以规范 JSON 为准，而不是本文；证据状态以
> [证据台账](../standards/union-six-pr-evidence-ledger.json)为准。本文不声称任何 PR、
> runtime、生产迁移、发行、公开发布、promotion、Maintenance transition、tunnel 或
> MCP restart 已完成。

## 联合完成规则

六个 PR 是同时承载两个强制范围的一条依赖链：

```text
PR1 -> PR2 -> PR3 -> PR4 -> PR5 -> PR6
       delivery_scope + collaboration_scope + required_evidence
```

只有某个 PR 的全部 `delivery_scope`、`collaboration_scope` 和
`required_evidence` 条目都具备匹配证据，且所有适用完成门禁都通过，该 PR 才算完成。
仅完成交付侧、协作侧、源码、测试、制品、合并或部署切片，都不等于整个 PR 完成。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

证据必须保持四个独立层级：

| 证据层级 | 可以证明 | 单独不能证明 |
| --- | --- | --- |
| `implementation` | 指定不可变源码 revision 包含所述实现。 | 测试通过、runtime 激活或生产验收。 |
| `test` | 绑定该不可变源码 revision 的检查已通过。 | live listener、迁移、重启、promotion 或部署。 |
| `runtime` | 具名 runtime 操作产生了匹配回执。 | 生产验收或公开发行，除非回执明确覆盖。 |
| `production` | 单独授权的生产动作产生了匹配回执。 | 其他生产动作、公开发布或发行授权。 |

任何措辞、review 意见、聚合、branch、tag、path 或 artifact reference 都不能把一个证据
层级升格为另一个层级。

## 当前联合矩阵

下表只用于导航；精确 requirement ID 与原文始终以规范 JSON 为准。

| PR | 依赖 | `delivery_scope` | `collaboration_scope` | `required_evidence` 与门禁 | 就绪度声明 |
| --- | --- | --- | --- | --- | --- |
| **PR1 — 路由内核与协作地基** | 无 | 4 项：统一 query/index/rerank 路由、精确 embedding identity 与有界恢复、派生任务持久 queue/reconcile、所有 task 路径强制项目隔离及旧数据隔离。 | 6 项：不可变协作值、append-only event log 地基、ProjectWorkBoard 边界、最小权限 policy 合同、三平面分离，以及服务器认证且 reviewer 独立的 `AcceptanceReceipt`。 | 6 项证据；适用 source/revision/document/asset/Standards/Spec/DeepSec/evidence-ledger 门禁。 | 只由台账回执决定；本文不声称 runtime 或 production 已完成。 |
| **PR2 — 端点合同与共享租约语义** | PR1 | 3 项：封闭三端权限、服务器编译的 role/action profile，以及不激活 runtime persistence 的 protocol/model/resource/terminal identity。 | 4 项：Agent work 与 compute job 共用 Lease/Fence/Heartbeat/ResultReceipt/Retry/Reconcile 语义，但记录、policy、capability、result body 与 authorization plane 保持分离；compute 不具备协作或 canonical 权威。 | 5 项证据，包括同一 conformance suite 分别验证两个 adapter，以及跨平面负向证明；适用全部通用 review/drift/evidence 门禁。 | 只有源码合同或 fake adapter 不能完成 PR2。 |
| **PR3 — 容器制品与 Passive Memory 事件** | PR2 | 4 项：无 secret artifact compiler 与 role/platform/variant matrix；不可变 OCI/SBOM/provenance/rootfs/role 证据；协作包仅进入 server；不执行 runtime、push、migration、promotion、Maintenance、restart 或 production mutation。 | 4 项：有界 Stop event；只有独立服务器验收后才发 accepted-result event；semantic capture 保持 pending-only；promoter input 绑定 receipt/evidence/conflict；Hook payload 拒绝 prompt、隐藏推理、credential、result body、lease token 与 provider secret。 | 5 项证据，包括 artifact isolation、Hook 行为、promotion 负向矩阵、no-runtime 声明和三条独立审查。 | build policy 或 completed + artifact reference 都不是 accepted work，也不是 runtime 证据。 |
| **PR4 — Deployment Center 与协作检索** | PR3 | 3 项：宿主只开放 `inspect`/`preview`；有界、脱敏、profile 感知的规划；local edge 无权威，status/plan 必须来自新鲜 host/server projection。 | 5 项：有界 Project Working Set/awareness、memory 与 peer 分离检索、role-aware relevance、服务器认证的 audience/policy/source/cursor/digest tuple，以及 accepted artifact 的独立 `AcceptanceReceipt` 谱系。 | 5 项证据，包括伪造 role/audience/receipt/source/cursor 负向测试和 `context_supply` 分离证明；适用全部通用门禁。 | 调用方自报 coordinator/reviewer role、session、page 或 result string 都不授予可见性或验收权威。可信 feed 与 consumer 证据缺失时 PR4 仍不完整。 |
| **PR5 — 迁移操作与协作运行时** | PR4 | 8 项：服务器拥有的持久 migration 编排与类型化 phase adapter；canonical SQLite/LanceDB/retention 边界；严格 server/compute provider 分离；`local`/`cloud`/`hybrid` 热路由；compute-only credential、原子 profile activation 与 identity revalidation；有界 structured JSON；以及按 operation 区分的 fail-closed 降级。 | 10 项：持久 AgentRegistry/ProjectWorkBoard 与认证 session lifecycle；Hook/MCP/stage/closure event；Maintenance reconcile；shadow-to-inject awareness 与前端 projection；accepted-result 的 pending-only promotion；语义共享但权限分离的 compute work；有界无 secret dispatch；以及保留 in-flight work 的项目级热模式切换。 | 9 项证据，覆盖 migration/recovery、持久协作 lifecycle、Hook/MCP E2E、promotion 负向测试、前端 smoke、三条独立审查、endpoint-role denial、routing/credential/profile activation，以及稳定降级与恢复。 | source 与聚焦测试切片不能证明真实持久 runtime、生产迁移、provider activation 或 publication。 |
| **PR6 — 发行就绪与角色能力合同** | PR5 | 4 项：跨平台 installer/profile、不可变 RC/release bundle、Windows/WSL2 本地 build/cache/GPU-smoke 边界与 protected GitHub evidence/verified-digest server、upgrade/rollback/recovery/retention/UTC 行为。 | 4 项：OCI 权限 label、最终 server/edge/compute 隔离、跨 Agent E2E，以及可观测的 `shadow-only` Workflow Composer；后者必须确定性回退固定 route，且不拥有执行或授权权威。 | 7 项证据。PR6 在通用门禁之外还需 Workflow Composer 对抗门禁；RC、内部部署、生产迁移、公开发布、stable 发布和发行仓库同步各自需要单独授权回执。 | RC contract、artifact bundle、role label 或候选 workflow 都不等于 runtime E2E、生产验收或 stable 发布。 |

### 当前 PR5 证据边界

当前源码树包含服务器拥有的协作运行时、仅 compute node 执行的 embedding/rerank/structured
JSON、`local`/`cloud`/`hybrid` 热路由、compute-only credential projection 与 profile
activation、有界降级，以及既有 migration-operation contract 的 implementation/focused-test
切片。完成度仍由证据台账决定：只有受治理的同层级 receipt 绑定最终不可变 source revision
与精确 changed-path 集后，PR5 requirement 才能离开 `not-evidenced`。本文不绑定某个 source
commit，也不声称独立 review、真实 browser/runtime lifecycle、生产 migration、provider
activation、publication 或 production acceptance 已完成。任何 source 或 focused-test 结果都
不能填充 runtime/production evidence，因此 PR5 联合完成规则仍未满足。

## 每个 PR 都适用的完成门禁

- `U6-GATE-SOURCE-01`：所有回执绑定不可变 source revision 与相关 diff/artifact digest。
- `U6-GATE-REVISION-01`：生成视图、ledger、manifest 与 review receipt 绑定 revision
  `2026-08-18.1` 和规范源原始字节 SHA-256；后续 revision 还必须证明来自上一规范源的
  不可变谱系。
- `U6-GATE-DOCUMENT-01` 与 `U6-GATE-ASSET-01`：受影响的中英文文档、图、SVG、badge、
  link、资源表和价格表保持语义一致，且不存在阻塞 tracked drift。
- `U6-GATE-REVIEW-01` 与 `U6-GATE-DEEPSEC-01`：独立 Standards、Spec 与 DeepSec
  Shield/代码坏味道回执绑定同一不可变 source、diff、requirement 集合和合同 revision。
  DeepSec 只读，其 finding 绝不自动成为 canonical memory。
- `U6-GATE-EVIDENCE-01`：每个 requirement ID 都有 implementation、test、runtime 与
  production 的显式状态；禁止跨层级升格。
- `U6-GATE-COMPOSER-01`：PR6 还必须证明 Workflow Composer 保持 shadow-only，不能移除
  硬门或自证 user-only stage，并能确定性回退 fixed route。

## 不可协商的权威边界

- `pp-server-backend` / `pp-core` 是 canonical SQLite 唯一 writer，也是 coordination、
  governance、accepted-result、receipt persistence 与 LanceDB promotion decision 的唯一权威。
- LanceDB 是可重建派生状态，绝不是恢复权威。
- Edge 只提交有界 intent/event 并读取有界 projection。
- Compute 只执行有界派生推理；相同 `project_id`、模型维度、role、capability、manifest 或
  result shape 都不能授予权威。
- Coordination、Project Working Set 与 Canonical Memory 保持分离。peer progress、agreement、
  finding、semantic capture 或 submitted work 都不会自动成为 canonical memory。
- 持久时间与 lease/fence 比较统一使用 timezone-aware UTC；installer 不修改 Linux、macOS、
  Windows、WSL2、edge、server 或 compute 的宿主时区。

## PR6 目标发行权威流水线

```text
Windows / WSL2
  -> 仅本地 build、cache、GPU smoke 与派生推理
  -> 没有 canonical write 或 release authority

受保护 GitHub workflow
  -> 不可变 RC/stable build evidence、OCI digest、SBOM、provenance

Server
  -> 只拉取并运行 verified digest
  -> 拥有 MCP、SQLite、LanceDB promotion decision 与 Maintenance

Stable 发行通道
  -> PyPI、GHCR、GitHub Release 与发行仓库同步
  -> 每项都需要独立明确授权回执
```

这只是目标权威模型，不证明任何环境已经配置、健康、部署、迁移、promotion、restart 或发布。

## 受控交接与回滚

1. 按规范 requirement 集合与对应 ledger 条目验证每个 PR；不得从单个源码切片推断整个 PR 状态。
2. Standards、Spec 与 DeepSec review receipt 必须绑定同一不可变 source revision、diff digest、
   requirement 集合和 contract revision。
3. PR6 证据完整后才能评估不可变 RC candidate；RC 创建仍需单独授权。
4. 内部部署、生产迁移、公开发布、stable 发布和发行仓库同步是彼此独立的动作，需要不同授权与回执。
5. 失败时保留 canonical SQLite，选择此前 verified 的不可变 bundle/digest，并且只从 canonical
   state 重建 LanceDB。

派生运维细节见[发行交付与安装 Profile](delivery.zh-CN.md)和
[三端架构](../architecture/three-endpoint-deployment/architecture.zh-CN.md)。
