# 部署档案与端点 Manifest 合同

> **规范范围：**机器可读的
> [联合六 PR 合同](../standards/union-six-pr-contract.json) revision `2026-08-18.1`
> 治理 endpoint 职责与 PR 完成条件。本文只是派生 source-contract 投影，不证明 runtime 或
> production activation。

> **PR 2 状态——仅合同与记录。** 本文定义版本化端点词汇、纯解析结果和有界证据
> 记录；它不会创建容器、建立隧道、调用 `ppctl`、写入 SQLite、迁移数据库、推广
> LanceDB 或启动 Maintenance。这些运行操作仍是 PR 3–PR 6 的目标工作。

Plastic Promise 只有一套代码与一份 canonical-data contract。部署档案只选择职责将来
运行的位置；它不会创建第二个记忆产品，也不会产生第二个 SQLite 真相源。

## 边界：Legacy V1 与 Endpoint V2

| 合同 | PR 2 中的状态 | 预期用途 | 位置与密钥策略 |
| --- | --- | --- | --- |
| `plastic-promise-deployment/v1` | **Legacy 兼容输入** | 既有 controller 时代的本地 profile 声明 | 它可能包含节点 `ssh_host` 与具体本地资源路径。必须留在 node-local；它不是 V2 endpoint contract，也不是可移植 receipt。 |
| `plastic-promise-deployment/v2` | **当前 endpoint contract** | 无密钥端点放置与纯 Resolved Plan 输入 | 它只携带 opaque reference；会拒绝 `ssh_host`、host/IP/URL 字段、文件系统路径、隧道细节、凭据和形似密钥的值。 |

V1 不会被静默转换、作为 V2 record 发布，或用于推导私有 transport。operator 或后续
deployment adapter 必须显式决定如何处理 legacy 本地输入。存在 V1 parser 不代表它拥有
canonical state 的任何权限。

### Legacy V1 兼容示例

此处有意先给出 JSON 示例，使既有 V1 文档使用者仍可验证 legacy input。它**不是**
V2 template；一旦含有 host-local path，就不得复制进仓库、Dashboard revision、公开
receipt 或跨主机 configuration record。

```json
{
  "schema_version": "plastic-promise-deployment/v1",
  "deployment_id": "local-laptop",
  "profile": "local-all-in-one",
  "modules": {
    "local-ollama": {"enabled": true}
  },
  "nodes": [],
  "resource_locations": {
    "container_store": null,
    "model_cache": "/var/lib/plastic-promise/model-cache"
  },
  "resource_budget": {
    "image_layers_bytes": 0,
    "image_unpack_bytes": 0,
    "model_cache_bytes": 1,
    "lancedb_shadow_rebuild_bytes": 1,
    "rollback_coexistence_bytes": 1
  }
}
```

### Endpoint V2 合同示例

V2 声明 role、版本化 protocol、capability、有界 concurrency 与 opaque policy
reference。`transport_ref` 和 `resource_policy_ref` 是稳定标签，不是 host、URL、path、
用户名或 secret。完整模型 identity 由 typed endpoint observation 单独证明；不能从
profile 或“向量维度相同”猜测。

```json
{
  "schema_version": "plastic-promise-deployment/v2",
  "deployment_id": "developer-laptop",
  "profile": "local-all-in-one",
  "modules": {},
  "endpoints": [
    {
      "id": "local-edge",
      "role": "pp-local-edge",
      "protocol": {"family": "edge", "major": 1, "minor": 0},
      "capabilities": [],
      "transport_ref": "loopback",
      "resource_policy_ref": "edge-default"
    },
    {
      "id": "server-backend",
      "role": "pp-server-backend",
      "protocol": {"family": "backend", "major": 1, "minor": 0},
      "capabilities": [],
      "transport_ref": "backend-private",
      "resource_policy_ref": "backend-default"
    },
    {
      "id": "compute-node",
      "role": "pp-compute-node",
      "protocol": {"family": "compute", "major": 1, "minor": 2},
      "capabilities": [
        {"kind": "embedding", "contract_version": "embedding/v1"},
        {"kind": "rerank", "contract_version": "rerank/v1"},
        {"kind": "structured-json", "contract_version": "structured-json/v1"}
      ],
      "max_concurrency": 4,
      "transport_ref": "compute-registry",
      "resource_policy_ref": "compute-default"
    }
  ]
}
```

V2 恰好接收一个 `pp-local-edge` 与一个 `pp-server-backend`；
`split-accelerated` profile 还至少需要一个 `pp-compute-node`。其可选
`resource_locations` 只能使用类似 `"container_store": "server-containers"` 的
opaque label，绝不能写绝对或相对 filesystem path。后续 preflight adapter 只在所属
host 私下解析这些 label。

## 端点部署档案词汇（源码合同；runtime evidence 待验证）

| Profile | 放置意图 | Canonical owner | 派生推理意图 |
| --- | --- | --- | --- |
| `local-all-in-one` | 三个 endpoint role 可同机放置 | 仅 `pp-server-backend` | 受管 `pp-compute-node` 默认使用 local，只公布精确配置的 capability |
| `local-cloud` | local edge 与 backend 本地运行，并存在受管 compute execution plane | 仅 `pp-server-backend` | `pp-compute-node` 执行已配置的 hosted embedding、rerank 与 structured JSON；provider credential 不进入 server |
| `split-accelerated` | edge、server backend 与 compute role 可在不同主机运行 | 仅服务器 `pp-server-backend` | 已登记的 `pp-compute-node` 可在 capability 与 model identity 精确匹配时使用 `local`、`cloud` 或 `hybrid` |

Profile 改变的是放置位置，不是 authority。每种档案都保留 project isolation、
durable-outbox admission、有界 failure reason、retry state 与 reconcile。`pp-local-edge` 只返回
有界 projection；所有 local、hosted 与 raw provider adapter 都在 `pp-compute-node` 内执行，
compute 只返回有界派生 result 与 receipt。两端都没有 canonical SQLite、LanceDB promotion、
协作或 canonical-write authority。

PR 5 源码把 structured JSON 与 embedding、rerank 一样作为 compute 的一等 capability。
它默认关闭，只有 backend、model、固定 revision、有界 provider 配置与 identity-revalidation
receipt 均存在时才启用。active project control revision 可以为新 work 选择 `local`、`cloud`
或 `hybrid`，但 provider 构造与 credential 不会因此进入 `pp-server-backend`。这些 source
contract 与 focused test 不证明真实 provider activation、runtime evidence、production
acceptance 或 publication。

structured-JSON 的 intent/schema 组合只能通过 compute node 自有的封闭注册表解析；server
绝不构造 provider prompt。embedding 与 structured-JSON 的 defer 使用不含内容的持久重试
marker，重试时由 caller 重新注入原始输入。canonical SQLite 只保留有界 intent、identity、
digest、failure 与 receipt reference。调度只消费显式 identity-revalidation receipt，绝不会
根据缓存 health 自行生成该证据。

## Resolved Plan 与所有权

`resolve(manifest)` 是 PR 2 的 deep-module seam。它返回 typed、零副作用的
`ResolvedEndpointDeploymentPlan`，或返回经过脱敏的 `EndpointContractError`。resolved
plan 固定一个 server-backend endpoint，同时作为：

- canonical SQLite owner；
- LanceDB-promotion owner；以及
- deployment-receipt persistence owner。

该 plan 的 browser projection 有意比 private runtime view 更窄：其中不包含 path、
address、credential、transport material、lease、原始 resource payload 或 health payload。

## PR 4 Deployment Center projection

> 这是当前 source 规划合同，不表示 local edge bridge 已配置、endpoint 已监听或
> production deployment 已经 live。

browser 只能向宿主提交无 secret 的 `EndpointManifestV2` candidate。`ppctl` 接受固定的
typed allowlist——`inspect` 与 `preview`——并返回脱敏 projection。它不接受 legacy V1
manifest、SSH host、filesystem path、credential、private key、Docker request、Shell command
或 generic operation selector。

`inspect` 报告 platform/resource/catalog/status、recommendation、model 与 enrollment
readiness，以及 receipt state。`preview` 解析 V2 candidate，记录受支持 selected profile
是否为 user override，并返回 manifest diff、module/resource estimate、完整 identity
comparison、hard preflight result、update class 与仅供检查的 plan hash。宿主把 raw path 和
legacy compatibility data 保持为私有；browser cache 永远不能证明 plan 或 host state 当前。

recommendation 是 advisory，但任何 override 都不能绕过 module/profile compatibility、
high-risk acknowledgement、精确 embedding/rerank identity evidence、immutable artifact
evidence 或 resource gate。`max(20%, 10 GiB)` free-space check 失败是拒绝，不是确认对话框。
update class 只能是 `no-change`、`live-apply`、`rolling-restart`、
`shadow-rebuild-promotion`、`backup-migration`、`enrollment-required` 或 `manual-review`；
PR 4 只报告它。apply、node contact、enrollment consumption、tunnel/service management、
SQLite mutation、LanceDB promotion、Maintenance 和 receipt persistence 延后至后续 operation。

## Identity 与兼容性证据

只有当一个 embedding route 的 typed identity 与 active generation 的**每一个**字段
相同，它才可被视为兼容。仅维度相同远远不够。

| 必需 embedding-identity 字段 | 含义 |
| --- | --- |
| `model` | 声明的模型名 |
| `revision` | 固定不可变 revision，不能是 `latest`、`main` 或 `stable` |
| `dimension` | 精确输出向量维度 |
| `normalization` | 归一化策略 |
| `metric` | 检索 distance/similarity metric |
| `tokenization` | Tokenization contract |
| `pooling` | Pooling contract |
| `artifact_sha256` | 不可变模型工件证据 |
| `golden_vector_sha256` | 已声明 vector space 的 golden-vector proof |

Rerank evidence 独立版本化，包含 model、固定 revision、artifact SHA-256 与
scoring-schema version。完整 identity 会为 capability 计算 fingerprint。模型、
revision、dimension、normalization、metric、tokenization、pooling、artifact 或
golden vector 任一变化都需要新的 identity evidence；派生 shadow generation 是否可
重建与推广，由后续运行阶段决定。

## PR 6 release-readiness 的选择边界

> **仅目标。**profile/manifest 解析 endpoint placement；它不是 release selection、
> registry authority 或 runtime receipt。

后续 PR 6 Release Bundle 必须把选定 profile/variant matrix 绑定到 immutable image
evidence 和不透明 Model Catalog reference/digest。只有 bundle 声明了该 profile，且
endpoint-role/compute-variant matrix 兼容时，profile 才可被选择。catalog 提供固定 model
identity、capability 和 resource metadata；绝不携带 model weight、path、endpoint、credential
或 canonical-write authority。

| Profile 后果 | Release-readiness 要求 |
| --- | --- |
| `local-all-in-one` | Bundle 列出同机 role/variant set；backend 仍是 SQLite single writer。 |
| `local-cloud` | Bundle 不会把 provider configuration 变为 image state；provider identity 仍是 node-local 且单独治理。 |
| `split-accelerated` | Bundle 列出兼容的 compute CPU/CUDA variant 和 Model Catalog reference/digest；compute node 只返回 derived result。 |

`ArtifactBundle` 可以证明自身 immutable descriptor evidence，但不能替代 Release Bundle、
Execution Grant、Migration Operation evidence 或 verified server receipt。mutable image tag
或仅维度相同的 model 都不足以进行 profile selection。

## Typed compute 与治理记录

PR 2 只定义 transport-independent record。后续 stage 的 adapter 可持久化或传输它们，
但合同本身不执行 I/O。

| Record 或 decision | 合同边界 |
| --- | --- |
| Endpoint protocol 与 capability | 版本化 protocol family/major/minor 加上当前 `embedding`、`rerank` 或 `structured-json` capability 声明；structured JSON 默认关闭，准入前必须具备完整 backend、model、固定 revision、provider setting 与重新验证的 identity 信息；此 source-level 边界不声称 live provider activation 或 runtime/production evidence |
| Hello、heartbeat 与 resource report | 静态 identity attestation、server-observed freshness 与有界 capacity report；不含 device serial、host address 或 path |
| Admission 与 binding | 在 derived work eligible 前验证 role、protocol compatibility、freshness、capacity 与完整 identity fingerprint |
| Compute lease 与 fencing generation | 把 derived job 绑定到 endpoint、capability、result schema、expiry、idempotency key 与单调递增 fence，避免旧工作获胜 |
| Completion decision | 接收或拒绝 body-free result envelope，返回稳定 reason 与 retry/quarantine 建议 |
| Manifest revision 与 deployment receipt | 由 server 拥有、以 manifest digest 关联的 record schema；PR 2 不声称已具备 persistence engine，也不会触发写入 |

Error 只能暴露稳定 code、category、retryability 与可选 retry delay，不能回显
configuration body、provider response、host、path、credential 或 user payload。

## PR 2 非目标与分阶段交接

| 阶段 | 目标职责——不由 PR 2 contract 实现 |
| --- | --- |
| PR 3 — container artifacts | Source-level `ContainerArtifactCompiler`、role/platform/variant policy、静态 recipe 与 immutable inspection evidence；受保护 CI 可以进行不 push 的 OCI build verification，但没有 local/runtime Compose activation、tunnel、registry publication 或 deployment action |
| PR 4 — deployment center | `ppctl`、Deployment Center inspection/preview、local/private adapter planning 与经审核的 activation-plan UX；没有 apply 或 runtime mutation |
| PR 5 — migration 与 runtime operations | 实测 preflight enforcement、节点 activation/enrollment、受限 transport、backup、SQLite migration、LanceDB shadow/promotion 与 Maintenance operation |
| PR 6 — release readiness | 跨平台 installer、目标 RC/stable evidence、Release Bundle/Model Catalog selection 与最终 controlled-release check；未独立验证前不得作 publication 声明 |

在这些阶段落地并被显式激活之前，V2 contract 只能视作 validation 与 record
vocabulary。它不能启动服务、创建 tunnel、下载模型、变更数据库、推广 index 或启用
Maintenance。

## 参考

- [Deployment and Runtime Guide（英文）](README.md)
- [部署与运行指南](README.zh-CN.md)
- [Local heterogeneous inference node contract（英文）](local-inference-node.md)
- [本地异机推理节点合同](local-inference-node.zh-CN.md)
- [资源规划与硬性门槛](resource-planning.zh-CN.md)
