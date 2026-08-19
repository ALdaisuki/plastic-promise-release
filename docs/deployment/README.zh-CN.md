# 部署与运行指南

> 本文是 Plastic Promise 当前发行版部署合同的中文入口。它与英文
> [deployment documentation](README.md) 共享同一组不变量；命令细节、
> 模板字段和错误码仍以实现与英文逐项运行手册为准。

> **规范联合范围：**[联合六 PR 合同](../standards/union-six-pr-contract.json)
> revision `2026-08-18.1` 同时治理部署交付与项目协作。只有每个
> `delivery_scope`、`collaboration_scope` 与 `required_evidence` 条目均通过，PR 才算完成；
> 任一单侧完成都不等于 PR 完成。本文只是派生部署指导，不能缩减该合同，也不能把
> source/test 证据升格为 runtime/production 证据。

只有交付范围、协作范围和所需证据全部通过，PR 才算完成；任一单侧完成都不等于 PR 完成。

## 先记住五条所有权规则

1. **SQLite WAL 只属于 `pp-server-backend`，并实行严格单写者（single-writer）策略。** 它保存记忆、提案、任务、
   审计和配置状态；local edge、compute node、云 provider 或第二个 backend
   都不能写入同一 canonical state。
2. **LanceDB 是可重建的派生检索索引。** 它可以 shadow rebuild、校验和
   原子推广，但不能用于恢复或覆盖 SQLite 正文。
3. **所有 local、hosted 与 raw provider adapter 都只能在 `pp-compute-node`
   内执行并返回派生结果。**`pp-server-backend` 可以调度和验收 compute work，但不得构造或
   调用 embedding、rerank 或 structured-JSON provider。compute 没有 SQLite、LanceDB
   promotion、协作或 canonical-write 权威。
4. **无 secret 的 Deployment Manifest 是部署真相。** Dashboard 与
   Deployment Center 位于 `pp-local-edge`；PR 4 的宿主 `ppctl` 只执行类型化
   `inspect` / `preview` 规划。任何 PR 5 mutation 都由 `pp-core` 服务器拥有的
   operation 执行，不是 browser 或 `ppctl` 的 apply command。
5. **先计划与预检，再安装或迁移。** 失败的预检必须零副作用；数据库迁移
   前必须有可校验的在线 SQLite 备份。

[三端架构](../architecture/three-endpoint-deployment/architecture.zh-CN.md)是分阶段
PR 栈的目标。本文不表示当前生产安装已经迁移到这些容器。

**PR 2–4 源码状态：**`plastic-promise-deployment/v2` 定义无密钥 endpoint contract 与
typed record；`ContainerArtifactCompiler` 新增可检查的 role/platform/variant policy 和
immutable evidence boundary；只读 `DeploymentCenter` 与 `ppctl` 规划接缝只增加
`inspect` / `preview` projection。Legacy V1 manifest 只是兼容 input，不是 V2 record。该
source policy 不能证明 image 已存在，也不授予 Docker/Compose execution。runtime activation、
SQLite migration、LanceDB promotion 与 Maintenance 仍是 PR 5–PR 6 的目标工作。PR 4 的
宿主规划接缝仅为 inspect/preview；详见[部署档案与端点 Manifest 合同](profiles.zh-CN.md)、
[三端目标架构](../architecture/three-endpoint-deployment/architecture.zh-CN.md)
以及[PR 3 制品边界](../architecture/three-endpoint-deployment/container-artifacts.zh-CN.md)。

## PR 4 Deployment Center 规划边界

这是当前 source 接口，不是宿主 adapter 已配置或 deployment 已运行的证据。edge 只渲染
静态、没有权威性的 projection，并只把无 secret 的 `EndpointManifestV2` candidate 发送给
宿主 `ppctl`。它绝不发送宿主 path、legacy SSH-host manifest、credential、private key、
Docker request 或 Shell command。

`ppctl` 只有封闭 operation allowlist：

```text
inspect -> platform/resource/catalog/status/model/enrollment/receipt projection
preview -> V2 diff、profile recommendation 或 user override、estimate、
           hard resource refusal、保守的 PR 4 update class 与仅供检查的 plan hash
```

`manifest_comparison` 故意只做 digest 级摘要。若 controller 还提供安全的
active-topology projection，`manifest_diff` 才会给出 profile、module ID、endpoint ID
和 compute capability kind 的脱敏结构化 V2 对比；它绝不包含 path、transport、credential
或 active manifest body。没有该 projection 时，diff 会明确标记为不可用。enrollment
readiness 同样只是 controller 拥有的、无 secret projection，而不是 enrollment material。

PR 4 的 `update_class` 只用于检查，并且只能输出 `no-change`、
`enrollment-required` 或 `manual-review`。可执行 action class 仍属于 PR 5。它的
plan hash 绑定安全的 observed state、candidate 和 profile，因而可报告 drift，但绝不是
activation 或 execution token。

可选 edge-to-host bridge 默认禁用。宿主启用它时，configured base 只能是
`http://127.0.0.1:<port>/ppctl/v1`，且只允许 JSON `POST`；edge 只能组成两个固定
operation，暴露 fresh/no-store bridge configuration，并且绝不 proxy 宿主 interface。

profile recommendation 是 advisory；受支持的 user override 不能绕过 V2 validation、
完整 model identity、artifact evidence 或 preflight。每个选定卷上的
`max(20%, 10 GiB)` 仍是硬性 free-space floor。得到的 plan hash 只用于展示和 drift
reporting，不是 activation token。PR 4 没有 apply、enrollment consumption、tunnel、
service action、SQLite mutation、LanceDB promotion、Maintenance action 或 receipt persistence。

本地或 CI builder 获得 Docker argument 前，仓库内的
[静态 recipe-policy validator](../../scripts/validate_container_artifact_policy.py) 会读取
Dockerfile、Compose template、`.dockerignore` 与版本化
[immutable base-image catalog](../../deploy/oci-base-images.json)。随后
[identity resolver](../../scripts/resolve_container_artifact_identity.py) 按精确
role/platform/variant 选择条目，只输出 pinned base reference/digest、source revision、
package version、build-policy digest、recipe-policy digest 与 expected OCI label。调用方必须
使用该 resolved identity，不能传入 floating tag 或独立的 base image。

这是 source/CI verification contract，不是 image availability、signing、publication、
deployment 或 production approval proof。

## PR 5 migration-operations 边界（durable source / target live adapter）

PR 5 已有 **durable source seam**，但 live phase-adapter composition 与 runtime activation
仍是 **target**。服务器拥有的类型化 `MigrationOperation` orchestrator 是 systemd 到三端容器
转换的唯一预期协调者。它会创建新鲜的无 secret Migration Operation Plan，并在 mutation
前校验独立、绑定 operation 的 Execution Grant。`SQLiteMigrationExecutionJournal` 会在
canonical pp-core SQLite 中持久化 server-issued grant、installation-scoped lease、单调递增
fencing generation、终态与无 secret receipt；其 schema 只通过备份门控的版本化 deployment
migration 安装。PR 4 Deployment Center inspection hash 只用于 drift reporting，永远不能授权执行。

orchestrator 会通过固定的 edge/compute、canonical-state、runtime、derived-index、
Maintenance 与 retention/cache adapter 执行类型化阶段：stage/verify、rehearsal、stop legacy、
canonical backup/migration、start backend、shadow rebuild/verify/promote、enable Maintenance，
最后执行 policy。其有界 rollback 会 disable Maintenance、revert derived selection、stop new
backend，且只在 canonical migration 成功后 restore canonical state，最后 restart legacy。
adapter 只收到类型化阶段输入与稳定 reason code，不会收到任意 Shell、Docker、SSH 或 SQLite
command。在 production composition 中，`pp-core`/`pp-server-backend` 仍是
canonical SQLite 唯一 writer 和持久 migration lease 持有者。测试与显式 non-production
composition 可以使用 in-memory journal，但 production 不得用它替代 SQLite adapter。
Deployment Center 与 `ppctl` 始终只读。

可变 `apply` 会拒绝 digest-only transport projection，只接受创建 plan 时已检查过的
server-memory topology 与 artifact binding。journal 会拒绝并发/重放 operation，在第一个
可变阶段精确消费一次 grant，并在 lease/fence 丢失后拒绝 stale completion。过期 running
operation 会进入 `recovery-required`，绝不会静默重放。

当前 source contract 默认将 plan/grant 窗口限制为 300 秒（最大 900 秒），并拒绝超过
120 秒的观测。它的 policy 目标包括生产备份最多保留五天、临时缓存每日清理，并将 LanceDB
视为可重建派生状态而非恢复权威。journal 会把相同的无 secret phase result 持久化为终态
receipt。在 live mutable phase adapter 获得单独授权并完成 composition 前，不验证任何
live listener、container、tunnel、production migration、LanceDB promotion、Maintenance
transition 或 MCP restart。

## 三种端点部署档案（源码合同；runtime evidence 待验证）

| 档案 | 端模块放置 | canonical state | 推理职责 |
| --- | --- | --- | --- |
| `local-all-in-one` | 三个端容器位于同一主机 | 仅 `pp-server-backend` | 受管 `pp-compute-node` 默认使用 local，只公布精确配置的 capability |
| `local-cloud` | local edge 与 backend 位于同一主机，并存在受管 compute execution plane | 仅 `pp-server-backend` | `pp-compute-node` 执行已配置的 hosted embedding、rerank 与 structured JSON；provider credential 不进入 server |
| `split-accelerated` | edge、server、compute 位于不同主机 | 仅服务器 `pp-server-backend` | 已登记 `pp-compute-node` 可在 capability 与 model identity 精确匹配时使用 `local`、`cloud` 或 `hybrid` |

档案只改变职责的**放置位置**，不会产生另一套记忆产品或第二个 SQLite 写入者。
所有档案都沿用项目隔离、durable outbox、失败原因、重试和 reconcile。
本地与云端 embedding 只有在模型、固定 revision、维度、归一化、distance
metric、tokenization/pooling contract、artifact hash 和 golden-vector evidence 全部一致时
才能互为 fallback。否则由 profile 二选一，切换 identity 必须 shadow rebuild 并原子推广。

### 当前 compute provider 与 structured-JSON 配置

仓库内 CPU、CUDA 与兼容 Compose template 都由 `pp-compute-node` 暴露
`embedding/v1`、`rerank/v1` 与 `structured-json/v1`。这是当前源码 capability，不证明
cloud provider、真实 node 或 production route 已激活。structured JSON 默认关闭，只有配置
backend、model 与固定 revision 后才启用。

| 用途 | Compose 环境变量 | 合同 |
| --- | --- | --- |
| 执行平面与路由 | `PP_ENDPOINT_ROLE=pp-compute-node`；`PP_LOCAL_NODE_PROVIDER_MODE=local|cloud|hybrid` | provider mode 必须与已配置的 local/cloud backend 组合一致；不一致时 fail closed。 |
| compute-only credential | `PP_LOCAL_NODE_CLOUD_API_KEY` | 只投影到 compute；control plane 只写不读，不得进入 identity、response、diagnostic 或 receipt。 |
| hosted embedding | `PP_LOCAL_NODE_EMBEDDING_BACKEND=cloud|openai-compatible`；`PP_LOCAL_NODE_EMBEDDING_CLOUD_BASE_URL`；`PP_LOCAL_NODE_EMBEDDING_CLOUD_PATH`（默认 `/embeddings`） | model、revision、dimension 与 normalization 仍是强制 identity 字段。 |
| hosted rerank | `PP_LOCAL_NODE_RERANK_BACKEND=cloud|openai-compatible`；`PP_LOCAL_NODE_RERANK_CLOUD_BASE_URL`；`PP_LOCAL_NODE_RERANK_CLOUD_PATH`（默认 `/rerank`） | server 只接收有界 score/receipt；路由不可用时保持原始顺序。 |
| structured JSON | `PP_LOCAL_NODE_STRUCTURED_JSON_BACKEND=off|cloud|openai-compatible`；`PP_LOCAL_NODE_STRUCTURED_JSON_MODEL`；`PP_LOCAL_NODE_STRUCTURED_JSON_REVISION`；`PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL`；`PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH`（默认 `/chat/completions`）；`PP_LOCAL_NODE_MAX_STRUCTURED_TOKENS`（`0` 表示不追加本地 ceiling） | 启用后由 compute node 强制 model/revision identity，以及有界 prompt、payload、output、timeout 与 UTF-8 限制；token 请求由 provider 自适应。 |

对于 `cloud` 或 `hybrid`，active project control revision 必须先原子激活 compute profile，
并记录匹配的 identity-revalidation receipt，之后才能派发新 work。mode 变更只影响新 work；
in-flight lease 与 receipt 继续绑定原 identity。任何 source configuration 或 focused test 都不
构成 runtime 或 production evidence。

structured-JSON 的 intent/schema 组合只能通过 compute node 自有的封闭注册表解析；server
绝不构造 provider prompt。embedding 与 structured-JSON 的 defer 使用不含内容的持久重试
marker，重试时由 caller 重新注入原始输入。canonical SQLite 只保留有界 intent、identity、
digest、failure 与 receipt reference。调度只消费显式 identity-revalidation receipt，绝不会
根据缓存 health 自行生成该证据。

#### 被动语义捕获路由

启用 `PP_PASSIVE_SEMANTIC_CAPTURE=shadow|on` 后，Stop Hook 会先把符合条件的用户文本
持久化为 durable work。server worker 再经由已登记的 node 提交有界批次，使用
`plastic-promise/structured-json/passive-semantic-v1` 与
`plastic-promise/structured-json/passive-semantic-memory-v1`。node 拥有固定 schema prompt、
本地/云 provider 选择、URL 与 credential；用户文本始终是非可信数据，不能替换该合同。

被动语义 work 默认使用 32K token 请求预算，让具备推理能力的 structured-JSON 模型有足够空间
完成私有 reasoning 后再输出最终 JSON object。现在不再有 Plastic Promise 自己的 8192 token上限；需要更长输出时可以提高 `PP_PASSIVE_SEMANTIC_MAX_TOKENS`。prompt/payload/output 字节
限制、provider 自身策略、请求超时、重试和队列容量仍然是安全边界。生产验收必须证明最终严格
JSON 位于 `message.content`。不得把 `reasoning_content` 当作结果、持久化或晋升为记忆。

启用前必须通过经过认证的 Control Dashboard/API 事务绑定 allowed node，以及精确的
embedding、rerank、structured-JSON identity。不得手工编辑 `managed.env` 或 Control SQLite。
identity admission 或 routing 不可用时，canonical SQLite 保持不变：embedding 降级为
text-only，semantic work 进入 retry/reconcile，不会创建 server-local provider，也不会自动采纳
memory。验证通过的 `on` 结果也只创建 pending proposal；promotion gate 仍然独立。

operator CLI 使用同一套 Bearer、ETag 与幂等事务。对于“本地 embedding/rerank + hosted
structured JSON”的节点，必须在同一个 hybrid revision 中绑定三个实际观测到的 identity digest：

```bash
.venv/bin/python scripts/activate_compute_node_routing.py \
  --token-file /root/.config/plastic-promise/control.token \
  --node-id <technical-node-id> \
  --embedding-identity <sha256-embedding-identity> \
  --rerank-identity <sha256-rerank-identity> \
  --structured-json-identity <sha256-structured-json-identity> \
  --embedding-model <model> \
  --embedding-revision <fixed-revision> \
  --embedding-dimension 2560 \
  --rerank-model <model> \
  --rerank-revision <fixed-revision> \
  --structured-json-model <model> \
  --structured-json-revision <fixed-revision> \
  --inference-mode hybrid
```

三个 structured-JSON 参数是原子参数组。全部省略时使用 local-only 默认值，并清空旧的
structured-JSON pin；请求 `hybrid` 却未提供完整参数组会 fail closed。identity 必须来自经过
认证的 node observation/receipt，不能根据 model alias 猜测。

### 受管索引身份与项目级修正（当前源码/测试）

canonical index-material migration 接缝现在显式接收受管计算节点的 active
identity。在 governed route 中，migration plan 以及 apply 阶段的
compare-and-swap 都会把目标模型身份及其 SHA-256 digest 绑定到 control
plane 注册；诸如 `fallback-zero` 的旧环境 fallback 不能作为替代。允许在
index-outbox 尚未完全收敛时只读检查计划，但 apply 必须重新提交并匹配不可变的
outbox watermark、digest、job count 和 active-job count。

普通记忆修正全程保持项目作用域。embedding probe 与 governed retrieval 调用都使用
记忆自身的 `project_id`；受管路由激活时不能跨项目做 identity probe，也不能静默退回未
登记的 provider。受管 embedder 只向 health/quality receipt 暴露有界使用证据（请求数、估算
输入 token、本地零成本与 pricing revision）。

以上属于**当前源码实现 / 聚焦测试已通过**，不等于真实 migration、LanceDB generation
重建或 promotion、MCP/Maintenance 运行，亦不等于生产验收。canonical migration 完成后仍必须
按顺序执行：带匹配证据 inspect/apply、重建新的 shadow generation、verify、reconcile outbox、
原子 promotion，最后重启并收集 runtime/production receipt。

## PR 6 release-readiness 权威（target / unverified）

Windows/WSL2 可用于本地 build cache 与仅派生 embedding/rerank 的 GPU smoke test；它绝不是
canonical-state、release 或 publishing authority。目标 GitHub protected workflow 生成 RC
artifact，并在单独批准后为 Release Bundle 生成 immutable OCI evidence。目标 server 只消费与
selected-evidence gate 独立验证一致的 digest，拥有 MCP/SQLite/LanceDB，并可在之后创建有界
receipt。当前 `release-publish.yml` stable publisher **尚未接线**消费或验证 PR 6 Release Bundle、
Model Catalog、artifact binding 或 RC attestation，不能作为 selected-evidence gate 的证明。Release
Bundle 或本文均不证明这些动作已经发生。Model Catalog reference 保持不透明，不含 weight、path、
credential 或 endpoint detail。

完整 target contract 见[发行交付](../release/delivery.zh-CN.md)。

受保护的 **verify-only** PR 路径会用 Buildx 的 `--sbom=true` 与
`--provenance=mode=max` 生成 OCI layout。local verifier 校验 descriptor hash、resolved 的
revision/base-image/build-policy/recipe-policy label，并确认 Buildx 的 SBOM 与 provenance
attestation layer 都将所选 platform image digest 写为 subject。它不 load image、不联系
registry、不验证 signer 或 certificate、不发布 artifact、不部署 container，也不作出生产信任
结论。

### 通用一键计算节点构建

一键构建脚本会自动检测源码 revision（git HEAD）、Docker 与 CUDA（`nvidia-smi`），
解析计算 variant，生成不含密钥的 compose `.env`（模型身份固定），执行不可变
本地构建，启动 Compose 并记录性能证据。仓库不硬编码任何机器专属的用户、路径、模型或
revision：所有值要么自动检测，要么由 operator profile / `PP_LOCAL_NODE_*` 环境变量显式提供。
不可变构建完成后，脚本会把构建期间解析出的容器身份（基础镜像引用/摘要、源码
revision、包版本、构建/recipe 策略摘要）补齐到 compose `.env`，把构建出的镜像别名到
compose 镜像名，并以 `--no-build` 启动 Compose，确保已校验镜像不会被重复构建。

外部 llama.cpp worker 要求 `PP_LLAMA_CPP_IMAGE` 使用带
`@sha256:<64-hex>` 的 registry 引用；浮动 tag 会被拒绝。Windows 环境写入器还要求传入
独立计算的 embedding 与 rerank artifact SHA-256，将其和预期模型身份一起保存，并把继承
ACL 替换为仅当前用户、SYSTEM 与本地 Administrators 可访问。Windows 与服务器烟测都会
比较观察到的 artifact digest 与这些预期值，不接受仅“长得像 digest”的自报身份。

```bash
# POSIX（macOS / Linux / WSL2）
./scripts/build_compute_node.sh

# Windows（PowerShell）
./scripts/build_compute_node.ps1
```

该构建与既有 Windows/WSL2 preflight 是同一治理路径：进入所选 WSL2 发行版（或原生
Docker Desktop），在创建 Buildx、清理缓存或构建镜像之前**固定执行** 10 秒 CPU、内存、
GPU、BuildKit/模型锁与磁盘的只读资源观测。资源繁忙时返回 `deferred_resource_busy`，
不会排队、清缓存或创建 builder；随后执行 Plastic Promise 范围内的受限清理。该清理不会
删除容器、卷、模型、数据库、网络或其他项目镜像；不会推送 GHCR。CUDA 构建会为选定的
llama.cpp worker 预留资源。只有 operator 显式选择旧版 `ollama` 兼容后端时，才会执行
Ollama 停止/恢复阶段；默认路径不会启动或探测 Ollama。

两个平台脚本都使用专用通用 Buildx builder `plastic-promise-local`，保留 24 小时内的项目
缓存，在 Buildx 前 resolve immutable base/policy identity，并校验 image 的 source
revision、base-image reference/digest、build-policy digest 与 recipe-policy digest label。
该本地 label check 不能替代受保护 CI 对 OCI-layout SBOM/provenance subject 的验证，
也不是 signing、publication、deployment 或 production proof。CUDA 容器烟测默认必需，但
`--skip-gpu-smoke` / `-SkipGpuSmoke` 是显式降级的 operator override：其报告**不是**
GPU-smoke evidence，不能用于 GPU 节点就绪或 release 决策。本地报告写入
`artifacts/local-node-build/`。

镜像构建通过 PyPI 解析 Python 依赖。位于受限或易丢包网络路径后的 operator 可以通过
`--pip-index-url` / `-PipIndexUrl` 让构建使用可达的镜像源（例如
`https://mirrors.aliyun.com/pypi/simple/`）；该值作为不可变构建参数嵌入，常规的
哈希校验安装流程仍然生效。

模型身份不会硬编码在仓库中。operator profile 必须声明 embedding 模型、固定 revision、
输出维度与归一化，以及 rerank 模型
和其固定 revision；缺失即 fail-closed 并给出明确修复提示。Compose 启动后，一键流程会运行
[`scripts/pp_node_smoke.py`](../../scripts/pp_node_smoke.py)：校验 `/health`、
`/v1/identity`、embedding 维度与 L2 归一化、有界 rerank 批次，记录中位延迟证据，并写出
doctor 可读的 `runtime-status.json`（`plastic-promise/local-inference-runtime-status/v1`）。
烟测仅从受 ACL 保护的 compose 环境读取私有节点授权以认证这些探针；该值不会写入报告或
日志，并且 structured-JSON 云 credential 会被完全忽略。

### 初始部署阶段的引导集成

在初始部署阶段，部署控制器把同一一键构建暴露为显式 operator 命令：

```bash
plastic-promise-deploy build-node --dry-run   # 打印已解析的命令
plastic-promise-deploy build-node             # 构建、启动并烟测
```

它会解析当前源码 checkout 的 revision（或 `--source-revision`），检测平台与 variant，
并执行对应的平台脚本。`--no-start` 只构建不启动；`--skip-gpu-smoke` 是显式降级覆盖；
`--node-config` / `--runtime-status` 指定 compose `.env` 与 doctor 证据的落点。
`split-accelerated` profile 的初始部署因此拥有 `apply` 之后、节点注册路由之前的文档化
构建步骤。

### 持久化 Windows 计算节点引导

[`scripts/setup_windows_compute_node.ps1`](../../scripts/setup_windows_compute_node.ps1)
把临时性的宿主恢复和节点构建变成幂等且持久化的引导流程。给定精确源码 revision
与 node-local operator profile（见
[`windows-compute-node.env.example`](../../deploy/local-inference-node/windows-compute-node.env.example)），
一次调用注册三个计划任务：

| 任务 | 用途 |
| --- | --- |
| `PPOllamaServe` | 常驻本地 Ollama registry（SYSTEM、失败自动重启、`OLLAMA_HOST=0.0.0.0:11434`） |
| `PPNodeModelSync` | 把 rerank 模型树按精确 HF revision 固定下载到只读 `/models` 来源 |
| `PPNodeBuild` | 通过 Docker Desktop 或探测到的 WSL2 原生 daemon 执行不可变 CUDA 镜像构建（交互用户） |

```powershell
# 只做宿主预检；仅在确实需要迁移时添加 -MigrateVhdxTo D:\WSL。
./scripts/preflight_windows_node_host.ps1 `
  -ProfilePath D:\PlasticPromise\node.env `
  -OutputPath D:\PlasticPromise\logs\preflight-report.json

# 持久化 Ollama、模型同步、构建任务与 compose identity。
./scripts/setup_windows_compute_node.ps1 `
  -SourceRevision <精确的40字符源码SHA> `
  -ProfilePath D:\PlasticPromise\node.env

# 启动已解析的 compose variant，并运行 identity/embedding/rerank 烟测。
./scripts/setup_windows_compute_node.ps1 `
  -SourceRevision <精确的40字符源码SHA> `
  -ProfilePath D:\PlasticPromise\node.env `
  -Stage verify
```

`verify` 会先通过 `configure_windows_compute_env.ps1` 完成私有 compose
EnvironmentFile：保留不可变镜像/构建身份，把 operator profile 中精确的 embedding、
rerank artifact SHA-256 与模型文件引用绑定进去，并移除继承 ACL。混合节点可传入
`-StructuredJsonBackend openai-compatible`，并同时提供模型、固定的
40 位十六进制或 `sha256:` 部署 revision、真实 HTTPS API 根地址以及
`-CloudApiKeyFile`。脚本会写入 `hybrid` provider mode，统一使用 Compose 与 Control 的
`..._STRUCTURED_JSON_CLOUD_BASE_URL/PATH` 变量名，收紧 ACL，并删除一次性 key 文件。
Chat provider 必须回显精确模型名；若 provider 不返回 revision，配置中的固定 revision
只代表部署身份，不虚构云端模型权重不可变。若 provider 返回 revision，则仍必须精确匹配。
随后跨平台 smoke 会用同一文件逐项比较实际 node ID、模型 revision、artifact digest、
向量维度/归一化与 rerank
方向。`PP_LOCAL_NODE_ID` 是 Control 协议使用的技术标识，因此使用 `inference-node` 这类
小写 ASCII；“推理节点”只作为 Dashboard 的本地化展示名，不作为协议 ID。

预检会同时探测 Docker Desktop 与 WSL2 原生 Docker。命中后者时，它记录
`PP_DOCKER_COMMAND=wsl.exe -d <distro> -e docker`；构建和验收直接使用这个前缀，
因此 loopback `socat` context 只会在显式传入 `-EnableDockerBridge` 时启用，从来不是
正确性依赖。预检还会检查系统盘余量，从 Lxss registry 定位所选发行版 VHDX，按
显式参数迁移 VHDX，更新 `.wslconfig` 中的自适应
`memory`/`processors`/`swap`，在发行版自己的 `/etc/wsl.conf` 配置 systemd，并以
root 身份启用和启动 `docker.service`。profile 中的值既覆盖自适应默认值，也覆盖已有
的受管资源键。旧版本误写进 `.wslconfig` 的 `[boot]` 块会先转换成注释，再把 systemd
配置迁到正确文件。

联网探测从实际选中的 runtime 发出，而不只检查 Windows。WSL 直连失败时，loopback
代理会在需要时转换为 WSL host gateway，并通过真实 HTTPS 请求验证；有效代理会写入
`/etc/profile.d/pp-proxy.sh`、Docker systemd drop-in，并显式传给 BuildKit build args。
VHDX 迁移失败、Docker service 未就绪、低空间下 VHDX 仍位于系统盘，或必要代理配置
失败时，JSON 报告会返回 `ready=false`，脚本以非零状态退出。

该引导还会写入 `deploy/local-inference-node/.env`（compose 运行时身份；已
gitignore），并在 `PP_LOCAL_NODE_EMBEDDING_REVISION` 为空时从 `/api/tags` 自动
派生 Ollama embedding digest。构建资源门禁会在模型同步进行时 defer，operator
需等待 `D:\PlasticPromise\logs\model-sync.log` 中的
`PP_NODE_MODEL_SYNC_COMPLETE`，再运行构建任务（`PPNodeBuild`）或
`-Stage build`。支持分阶段重入：
`-Stage preflight|ollama|models|build|env|verify`。

仓库内 compute Dockerfile 已包含 Triton JIT 所需的 `python3-dev`、`gcc` 和
`g++`。旧缓存镜像缺少依赖时，Windows 构建会增加一个小型 overlay 修复层，验证
修复后的镜像，并且即使选择 `-NoStart` 也会写入对应 CUDA/CPU compose alias，不必
重建完整依赖图。CUDA compose 将 `/tmp`
挂载为 `exec`；改回 `noexec` 会阻止 Triton 生成的共享对象加载，并由资源合同测试
拒绝。

Python、Ollama 可执行文件、Ollama 模型目录与交互用户 profile 均自动探测，
并可通过 profile 覆盖（`PP_PYTHON_EXECUTABLE`、`PP_OLLAMA_EXECUTABLE`、
`PP_OLLAMA_MODELS_DIR`、`PP_WINDOWS_USER_PROFILE`）。Docker 选择与宿主资源可用
`PP_DOCKER_COMMAND`、`PP_WSL_DISTRO`、`PP_WSL_VHDX_TARGET`、`PP_PROXY_URL`、
`PP_WSL_MEMORY`、`PP_WSL_PROCESSORS` 和 `PP_WSL_SWAP` 覆盖；而
`D:\PlasticPromise\remote-builds\<SHA>\source` 布局是仓库不可变的 Windows
builder 契约。
`PP_PROXY_URL` 只接受不含凭据的绝对 HTTP(S) URL。所有公开 Windows 构建入口都会
在导出代理环境变量或构造 Docker/BuildKit 参数之前拒绝 URI userinfo。

这仍然是 operator tooling，不是安装器：它不会创建 tunnel、不会联系 governed
server、不会 promotion generation，也不会持久化 canonical state。

### 生产 generation 切换

[`scripts/cutover_lancedb_generation.py`](../../scripts/cutover_lancedb_generation.py)
实现两个显式阶段。`prepare` 优先从 runtime EnvironmentFile 读取 canonical database 与
generation root（operator 显式传入路径时以显式值为准）；任一身份缺失时 fail closed。
随后构建、reconcile 并验证一个 inactive candidate；它要求 live quality report 与精确
managed/revision environment 绑定。
`cutover` 只能在独立授权的宿主 operator 已停止 MCP、inference gateway、Maintenance 与
Knowledge Ingest 后执行。它可以通过 Bearer 认证及 ETag/Idempotency-Key CAS 激活不可变
staged Control revision，随后 promotion、通过同一认证 API retarget Control、bootstrap 并
验证 generation-bound live root，最后只原子更新 generation/live-root EnvironmentFile
指针。

所有 generation 命令都会先降权到 control root 的 owner，避免再次产生 `root:root 0600`
资料。默认只输出零写入 JSON 计划。脚本不会重启服务、不会改变 Maintenance 策略、不会
制造质量报告，也不会直接编辑 Control SQLite。停服、重启、切换后烟测及 Maintenance
transition 都是独立的宿主授权操作。

```bash
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <新的-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json

.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <新的-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --apply

# embedding identity 变化时，prepare 也必须加载 staged revision。
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase prepare \
  --generation-id <新的-generation-id> \
  --quality-report /srv/plastic-promise/state/quality/<live-report>.json \
  --revision-id <revision> \
  --revision-env /srv/plastic-promise/state/control/revisions/<revision>.env \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --apply

# 独立停止必需的 runtime unit 后，先审查 cutover 计划。
.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase cutover \
  --generation-id <新的-generation-id> \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --token-file /root/.config/plastic-promise/control.token

.venv/bin/python scripts/cutover_lancedb_generation.py \
  --phase cutover \
  --generation-id <新的-generation-id> \
  --prepare-receipt /srv/plastic-promise/state/evidence/<generation>.prepare.json \
  --token-file /root/.config/plastic-promise/control.token \
  --revision-id <revision> \
  --revision-env /srv/plastic-promise/state/control/revisions/<revision>.env \
  --evidence-file /srv/plastic-promise/state/evidence/<revision>.json \
  --apply
```

prepare 会原子写入私有 receipt，绑定 generation ID、generation manifest 文件字节、
manifest/index-tree digest、质量报告路径与 digest、revision ID 及 revision EnvironmentFile
digest；cutover 会重新计算这些身份，对 receipt 缺失、候选被替换或任一不一致 fail closed。embedding
revision 变化时，在 **prepare 与 cutover 两阶段**传入相同的 `--revision-id` 与
`--revision-env`，使 build、reconcile、verify、activation 与 promotion 使用完全相同的
staged identity；cutover 另传 `--evidence-file`。切换后，
独立授权的宿主 operator 才重启必需 unit，并验证 MCP `/health`、`runtime_mode`、
`context_supply`、`memory_recall`、服务器私有 compute transport 与 Windows/WSL2 节点烟测；
通过后再审查或调整 Maintenance 状态。任一步失败都不会继续应用后续步骤；canonical
SQLite 始终是唯一真相源。

英文逐项说明：

三个 endpoint container、生成的原生 runtime asset、仓库提供的 systemd unit/drop-in 与
浏览器 timestamp formatter 统一使用 `TZ=UTC`，canonical timestamp 始终保存为
timezone-aware UTC。这只是逻辑 runtime/display 策略：安装器不会修改宿主时区，不会挂载
`/etc/localtime`，也不会调用 `timedatectl` 或 Windows `Set-TimeZone`。安装或更新原生 unit
后，应执行 `systemctl daemon-reload`、重启受影响服务，并只验证进程的 `TZ` 变量，不得为了
取证输出完整环境。

- [部署档案与端点 Manifest 合同](profiles.zh-CN.md) · [English](profiles.md)
- [启动与运行模式](startup-modes.md)
- [部署控制器](deploy-controller.md)
- [资源规划和硬门禁](resource-planning.zh-CN.md)
- [配置基线](config-baselines.md)
- [本地异机推理节点合同](local-inference-node.zh-CN.md) · [English](local-inference-node.md)
- [故障排查](troubleshooting.md)

## 标准部署顺序

1. 复制一个无密钥 manifest 模板，声明档案、可选模块、资源位置和实测
   `resource_budget`。不要把 API key、模型下载 token、SSH 凭据、私有端点或
   本机路径复制进 Git。

2. 用 `plastic-promise deploy plan` 生成 operation-bound plan，并记录它的
   `sha256:` 哈希。计划只读，不能替代安装、升级或恢复操作。
3. 运行部署控制器 `preflight`。它会按实际物理文件系统计算 SQLite 备份、
   WAL/SHM、镜像层、模型缓存、LanceDB shadow rebuild、回滚共存和迁移暂存的
   需要量；任何目标卷预计低于 `max(20%, 10 GiB)` 可用空间即拒绝执行。
4. 需要升级时，先创建在线 SQLite 备份，验证 `integrity_check` 和 SHA-256，
   再执行已审查、带版本号的迁移。不要用“重新建 LanceDB”替代备份或迁移。
5. 用该平台的受控启动入口激活**一个**持有 canonical runtime lock 的运行时。
   不得让原生服务与 Compose 针对同一 SQLite 状态目录同时启动。
6. 启动后验证 MCP `initialize`、`tools/list`、项目隔离 recall、outbox
   reconcile、降级行为和运行时 lock；派生索引需先 shadow rebuild 与质量门禁，
   后原子推广。

## 固定 revision 的原生服务器切换

对于仓库提供的原生 systemd 部署，应使用受保护的切换脚本，而不是让生产服务器持有
GitHub 认证：

```bash
python scripts/deploy_server_revision.py \
  --ssh-target root@server.example \
  --expected-current-revision <当前完整-40-hex-sha> \
  --revision <目标完整-40-hex-sha>
```

默认调用只输出无密钥计划，不产生修改；审核计划后再追加 `--apply`。脚本要求本地与远端
worktree 均为干净状态，证明目标是当前生产 revision 的 fast-forward 后代，只传输带前置
提交约束的离线 Git 薄包；随后创建 canonical SQLite 在线备份并记录 `integrity_check` 与
SHA-256 证据、备份已安装 unit、安装仓库 unit，只重启切换前已经 active 的服务，并且仅在
健康响应中的 source revision 与目标 SHA 完全一致时通过。健康观察最长十秒。checkout 后
任一步失败都会恢复原 revision 和 unit 文件，并重启原有 active 服务。脚本不会修改宿主
时区，成功后只删除精确命名的远端临时 bundle。

该脚本只适用于依赖不变的源码 revision。如果审核后的发行变更了 Python 环境、wheel、
数据库 schema 或 OCI digest，应先使用对应 release bundle 或迁移工作流，不能把未经审核的
安装命令塞进切换步骤。

## `pp-compute-node`

compute endpoint 是推理加速器，不是数据库副本，也不是另一台 MCP 服务器；它是
现有本地异机推理节点契约的目标容器形式。它可运行在 Windows/WSL2、Linux 或
macOS 的 Docker/Compose 环境中，并且必须同时满足以下要求：

- 节点 API 仅监听 `127.0.0.1`；服务器侧反向转发也仅绑定 loopback。不得把
  节点 API、MCP `:9020` 或隧道端点公开到局域网或互联网。
- 注册、健康检查与每次返回都绑定完整模型身份：**模型名、固定 revision、
  输出维度、归一化、metric、tokenization、pooling、模型工件哈希、golden-vector proof
  和传输证据**。仅“维度相同”不能证明向量可混用。
- 模型挂载只读；节点不保存用户记忆、SQLite、LanceDB 或 canonical 写入凭据。
- 反向隧道账号仅允许必要的转发：无 shell、sudo、SFTP、agent/X11 forwarding
  和 public forwarding；连接应使用 `ServerAlive`、`ExitOnForwardFailure` 和
  受监督的恢复机制。
- 节点配置、私钥、端点、账号、模型路径与 token 都是 node-local 环境资料，
  不进入 manifest、日志、发布工件或文档示例。

变更模型、revision、维度、归一化、metric 或工件时，必须建立新的身份证据并
重建对应派生 generation。本地和云端 identity 不同时不能同时服务同一个 active
generation：`split-accelerated` 保留本地，`local-cloud` 保留云端。

本地 compute 失败后，只有显式配置、启用、健康且 identity 兼容的云 provider
可以带可见 fallback evidence 接管。云端缺失或也失败时，派生任务继续排队，
recall 使用当前 verified generation 加文本/BM25/符号检索，backend 持续轮询已配置
provider。只有连续 identity/capability 探针通过稳定窗口后才恢复路由。

## 配置、运行与清理

配置分为三个层次：发行版的安全默认值、无密钥的 deployment manifest、以及
节点本地环境。`accelerator-max` 和 `maintenance-daemon` 默认关闭；它们不能因
为另一个模块依赖就被隐式打开，仍需显式 acknowledgement 与服务器端受控配置
修订。

模型/镜像缓存清理也不是普通构建步骤的副作用。先运行只读清理计划，保留活动
revision 与已验证回滚 revision；只对未引用且空闲至少 24 小时的工件走单独、
可审计的 apply。所有未来拉取、构建或解压之前都要重新经过资源预检。

## 故障时的恢复原则

- provider 或节点不可用时，以显式 degraded mode 返回；不要偷偷转移 canonical
  写入者或混用不同 embedding 身份。
- LanceDB generation stale 时，保留 SQLite 与审计证据，重建 shadow generation，
  通过质量/隔离门禁后再推广；不要从派生索引反写 canonical memory。
- 升级失败时，先停掉新选中的 routing/configuration revision，回到前一个不可变
  包或镜像与已验证节点身份。若迁移已获批准，只能在停掉运行时后用控制器生成的
  备份恢复，并重新做完整性验证。

发行证据与生产推广顺序见 [发行与受控推广指南](../release/delivery.zh-CN.md)。

## 资源与费用证据

所有容量数字统一使用二进制 `GiB`，表示控制器在每个受影响卷保留
`max(20%, 10 GiB)` 前的可用空间。权威假设见
[资源规划](resource-planning.zh-CN.md)。

仓库不固化 provider 价格、网络流量费、registry 保留费或 CI 分钟价格。这些都
属于动态外部证据。费用估算必须记录 provider/catalog revision、区域与币种、
观测时间、模型 identity、预计请求量，以及缓存/fallback 假设；复制来的过期价格
不能作为部署事实。
