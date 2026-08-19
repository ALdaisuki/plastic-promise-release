# 容器制品边界

> **PR 3/PR 6 源码边界——仅构建与验证（2026-08-11）。**
> `ContainerArtifactCompiler` 是源码级的计划/物化接缝。本文或
> `ArtifactBundle` 都不能证明镜像已经构建、加载、推送、启动、部署或用于生产。
> 当前 collaboration contract/projection/lease/policy/bridge module 与
> `CollaborationEventLog` 地基只封装在 `pp-server-backend`；在本源码证据层中仍保持
> unwired，不打开 listener 或 SQLite connection。

英文对等文档：[`container-artifacts.md`](container-artifacts.md)。

关联图：

- [`diagrams/artifact-build.txt`](diagrams/artifact-build.txt)：紧凑 ASCII
  组件图；
- [`diagrams/artifact-build.mermaid`](diagrams/artifact-build.mermaid)：仅构建的
  流程与权限边界；
- [`diagrams/container-artifact-matrix.zh-CN.svg`](diagrams/container-artifact-matrix.zh-CN.svg)：
  可视化矩阵；旁边保留英文对等 SVG。

## 1. 范围与权限

PR 3 为三端设计提供一个**构建期**深模块。其公开表面刻意保持很小：

```text
ContainerArtifactCompiler.prepare(request) -> ArtifactBuildPlan
ContainerArtifactCompiler.materialize(plan, executor) -> ArtifactBundle
静态 recipe-policy preflight(repository root) -> RecipePolicyReceipt
RolePackageCompiler.materialize(role, output root, version) -> RolePackageMaterialization
```

`prepare` 解析确定性、无 secret 的制品策略。`materialize` 把该策略交给构建期
`ArtifactBuildExecutor`，并返回可检查的 descriptor bundle。executor 接收已准备的 plan
和 descriptor，因此无需第二份 out-of-band configuration 也能得到 pinned revision/package
metadata 与 expected-label policy。本地 Docker/Buildx 适配器、受保护 CI 适配器和测试 fake
的内部实现可以不同，但不会改变策略合同。P1 修复加入了纯源码静态 recipe-policy preflight
与每个计划 descriptor 的结构化、plan-bound、body-free evidence receipt。它绑定 immutable
OCI layout/image、SBOM、provenance、expected label、source/package identity、recipe-policy
digest、base-image digest、`collaboration_surface_digest` 与
`application_inventory_digest`。inventory 是应用 layer 与 whiteout 语义后最终 OCI rootfs 的
规范化 file-path view。这两个必填 surface/inventory 绑定使 receipt 升级为
`plastic-promise-container-evidence/v2`；v1 receipt 会被拒绝，不能按新结构静默解释。类型化
role surface 同时成为 module name、source path 与 OCI inventory expectation 的唯一来源。
该 receipt 是 local/CI verification contract。在受保护 CI 中，只有
确认 Buildx 生成的 SBOM/provenance attestation layer 绑定到准确 OCI image 后才
生成；它不是 release 签名、publication、runtime activation 或 deployment proof。

Plastic Promise 仍是一个 monorepo 和一条版本线。根 `pyproject.toml` 是完整开发安装；
生产 recipe 不直接安装它，而是从同一 source revision 编译三个角色构建包：静态
`pp-local-edge`、Python `pp-server-backend` 与 Python `pp-compute-node`。CPU/CUDA
只是同一个 compute 角色包的两个 image variant，不是独立仓库或独立版本的 Python 包。

该模块不是部署控制器。它不拥有 Deployment Manifest、Deployment Receipt、canonical
SQLite、LanceDB promotion decision、发行凭据、server SSH 权限或面向用户的 `ppctl`
操作。这些 runtime 与发行权限仍由后续 PR 承担。

| 关注点 | PR 3 所有者 / 结果 | PR 3 明确不授予 |
|---|---|---|
| 制品策略 | `ContainerArtifactCompiler` 产生 plan、policy digest 与计划的 base-image identity。静态 preflight 返回规范化的 `RecipePolicyReceipt`。 | 部署决策或 active configuration revision。 |
| 镜像物化 | `ArtifactBuildExecutor` 返回 plan-bound OCI/SBOM/provenance evidence；`ContainerArtifactCompiler` 拒绝 identity、label、recipe-policy 或 receipt 不匹配。 | `docker run`、`docker compose up`、镜像 push、发行签名或凭据使用。 |
| Collaboration application surface | 封闭 role policy 与 final-rootfs inventory 证明当前 collaboration package 只存在于 `pp-server-backend`；edge 不含 Python collaboration runtime，compute 不含 collaboration package 或 writer configuration。 | 构造 event log、绑定 listener、创建/打开 SQLite schema 或 durable collaboration 接线。 |
| 角色包表面 | `RolePackageCompiler` 只复制该角色由仓库拥有的 source、dependency 与 console-script allowlist。final-rootfs 与 SPDX inventory 会拒绝 allowlist 外的 package path，并要求角色 import 地基存在。 | 安装完整根开发包、把 CPU/CUDA 当作分别治理的包，或通过 package presence 获得权限。 |
| 制品检查 | `ArtifactBundle` 投影 immutable/local descriptor、role/variant metadata 与 body-free verification receipt。不透明 Model Catalog reference 仅保留在 prepared plan，不进入只读 bundle projection。 | 任一 descriptor 已存在于某主机或已获生产批准的证明。 |
| Runtime 应用 | 刻意不存在。 | listener 绑定、服务激活、端点 enrollment、调度、tunnel 建立、migration、promotion、Maintenance 或 MCP restart。 |

不透明的 Model Catalog reference/digest 只是输入证据。PR 3 不下载 model weight，也不把
catalog 解释为选择 provider、发布 image 或运行 model 的权限。

## 2. 无 secret 的请求与可检查输出

`ArtifactRequest` 仅包含构建选择数据：

- immutable `source_revision` 与 `package_version`；
- selected target platform；
- selected compute variant；
- 不透明的 Model Catalog reference/digest；以及
- selected deployment profile。

它不得携带 Docker socket path、host path、URL、SSH material、private key、API token、
registry credential、model weight 或 arbitrary metadata。被拒绝的请求返回稳定、脱敏的
reason，不能回显不安全输入。

`ArtifactBuildPlan` 记录 endpoint × platform × variant matrix、预期 OCI label、
entrypoint、listener/mount policy、版本化 immutable base-image catalog entry、
recipe-policy digest、collaboration-surface digest 与 policy digest。`ArtifactBundle` 仅保留
后续经验证的 release/deployment flow 所需的 descriptor 与 inspection evidence，包括已检查
final-rootfs application inventory 的 digest；它不是 Release Bundle、active manifest 或
持久化 Deployment Receipt。

当前定义的 selection set 是 `linux/amd64` 与 `linux/arm64`；CUDA 仅对
`linux/amd64` 有效。这只是 matrix validation，不能表示任一平台 image 已经构建或可用。

expected OCI label 绑定 immutable source revision、package version、endpoint role、
endpoint variant、endpoint-contract revision、精确 role-authority matrix、所选 base-image digest、build-policy digest 与
recipe-policy digest。descriptor 及其 policy digest 绑定 `collaboration_surface_digest`；OCI
inspector 再从所选 image 的 final rootfs 派生 `application_inventory_digest`，并把两个值记录在
与 image/SBOM/provenance 绑定的 evidence receipt 中。二者都不是独立 OCI label。server
inventory 必须精确等于 `endpoint_role_contract(PP_SERVER_BACKEND)` 发布的当前封闭
collaboration allowlist。container compiler 直接消费同一份 manifest，不再维护第二套
module 清单。仅包含其中一部分仍不够，出现额外 module 会 fail closed。SBOM attestation 必须携带有效的 SPDX 2.2/2.3 document predicate。原生
BuildKit attestation 可以是 package-level 且不带 file entry；验证器接受这一有界缺省，但它一旦携带 collaboration 文件条目，就必须与 final rootfs 精确一致。
RC/stable 发布阶段会额外生成每个 artifact 的 Syft file inventory，并单独绑定这份完整清单。compute
同一比较也覆盖实际出现的完整 `plastic_promise` namespace：每个已安装 source path
必须属于所选角色 allowlist，每个 required import 地基必须存在，SPDX package view 必须与
final-rootfs package view 相等。materializer 另行证明 staged source tree 等于完整 allowlist，
因此局部 synthetic OCI inventory 不能重新定义 package policy。
recipe 还声明 CPU/CUDA variant、typed capability 与 operator-mounted read-only model source。
compiler 按该 policy 校验返回的 label digest；adapter 不能用任意 configuration source 的
label 替换它。

## 3. 制品矩阵

即使以后 local profile 把三端放在同一主机，compiler 仍将 public role 分开。下表的
“允许的运行数据”属于策略声明，不能作为 live bind mount 的证据。

精确的 `org.plastic-promise.authority` 值同时进入 descriptor、Dockerfile、Compose、
OCI label 与 SBOM/provenance-bound evidence chain：

| 制品角色 | 精确 authority label |
|---|---|
| `pp-server-backend` | `agent-registry-authority,work-board-authority,canonical-memory-authority,collaboration-event-writer` |
| `pp-local-edge` | `local-edge,bounded-awareness-display,bounded-event-submission` |
| `pp-compute-node` | `compute-execution` |

这些 label 声明的是经审查的角色表面；它们不能证明 registry、work board、event listener
或生产 runtime 已经激活。

| 制品角色 | 变体 | 类型化边界 | 允许的运行数据策略 | 镜像与角色必须排除 |
|---|---|---|---|---|
| `pp-local-edge` | `standard` | 只有静态 Nginx/browser asset、有界 awareness 展示与类型化有界 event submission；它不是 raw-history proxy 或 MCP authority。 | 逻辑 `edge-session-cache` 仅可读写有界 ephemeral edge cache；不存在 eligible canonical-state mount。 | Python runtime/package、raw collaboration history、Docker socket、SQLite、LanceDB、model weight、private key、任意 host command channel。 |
| `pp-server-backend` | `standard` | 唯一 registry/work-board/canonical-memory/event-writer 角色；安装 server role allowlist，并把当前 collaboration package 作为 `source-only-unwired` 封装。 | **唯一**具有逻辑 `canonical-state` read-write 资格的角色，另有有界 `backend-tmp` tmpfs。本源码层不构造 `CollaborationEventLog`，不创建 schema、不为其打开 SQLite，也不绑定 listener。 | `plastic_promise.local_inference_node`、本地 model-worker dependency、`release_builder`、Docker socket、model weight、写入 layer 的 user source text、compute-node credential 与任意 host command channel。 |
| `pp-compute-node` | `cpu` | 同一个 compute role package，仅含根 package identity 与 `plastic_promise.local_inference_node`，并暴露 `embedding/v1`、`rerank/v1` 以及可选的 `structured-json/v1` capability。structured JSON 在 model/revision 与有界 provider 配置激活前保持关闭。 | 后续 runtime 应用时仅允许 read-only `model-catalog`、有界 read-write `node-runtime` 与 `node-tmp` tmpfs。 | MCP/server、canonical SQLite、memory、knowledge、collaboration、deployment/migration/Maintenance、release builder、LanceDB、Docker socket、private key、credential file 与 shell/tool administration。 |
| `pp-compute-node` | `cuda` | 与 CPU 使用同一个 compute role package 和 typed contract；CUDA 只改变 image/runtime variant；显式配置时同样可提供 `structured-json/v1`。 | 与 CPU 相同的 `model-catalog` / `node-runtime` / `node-tmp` policy；accelerator 细节保持为 variant 内部实现。 | 与 CPU 相同的 server/canonical 禁止表面；layer 中不得嵌入 model weight。 |

`structured-json/v1` 是矩阵中的一等 compute-node capability，但 label 本身不会激活它。
node 必须广告与 active revision 匹配的 model 和 immutable revision，并由经认证的 server
route 完成同一 identity revalidation。server 永远不构造或调用 provider；没有 eligible node
时，structured JSON 进入 retry/reconcile defer，不回退到 server-local provider。

本矩阵不锁定 model family。任何未来 compatible provider 仍必须满足 Endpoint Contract V2
建立的完整 identity tuple：model name、immutable revision、dimension、normalization、
metric、tokenization、pooling、artifact SHA-256 和 golden-vector SHA-256。

## 4. 镜像与 mount 安全策略

在后续 runtime adapter 消费 descriptor 之前，compiler 使下列检查可被检查：

- 镜像排除 canonical database、LanceDB generation、model weight、credential、private
  key、API token、runtime state、log 与 build cache；
- 全部 endpoint descriptor 在目标 recipe 中要求 non-root runtime identity 与
  read-only root filesystem；
- `pp-local-edge` 不能获得 Docker 或 canonical-state authority；
- 只有 `pp-server-backend` 可以声明拥有 canonical SQLite runtime mount 的*资格*；
- 只有 `pp-server-backend` 封装当前封闭 collaboration source surface 与
  `CollaborationEventLog`；本源码层不调用 constructor、不创建 `collaboration_events`、不为其
  打开 SQLite、不绑定 listener，也不接入 MCP/Hook path；
- compute variant 可声明只读 model-cache 资格与有界 scratch space；其角色包不含 MCP、
  SQLite、memory、knowledge、collaboration、deployment、migration、Maintenance 或
  release-builder implementation；以及
- listener policy 默认是 private-container/loopback。build plan 不发布 port，也不建立
  connectivity。

宿主 Ollama 仍是 image policy 之外的显式 compatibility adapter。它不得获得
canonical-state authority，也不是 production image default。

## 5. 源码 recipe 映射

下列仓库中的 source recipe 是 policy 输入；其存在不代表已经 build 或可用 runtime：

| Endpoint artifact | Source recipe / companion file | 声明的 entrypoint |
|---|---|---|
| `pp-local-edge` | `deploy/local-edge/Dockerfile`、`entrypoint.sh`、`nginx.conf` 与 `compose.yaml` | `plastic-promise-local-edge` |
| `pp-server-backend` | `deploy/server/Dockerfile` 与 `compose.yaml`；丢弃的 `server-package` stage 把 monorepo 交给 `RolePackageCompiler`，fresh final stage 只安装 server allowlist，并清理 staged source tree 与 pip 生成的 `/app/build` tree | `plastic-promise-canonical-runtime` |
| `pp-compute-node` CPU/CUDA | `deploy/local-inference-node/Dockerfile`、兼容 `compose.yaml`、`compose.cpu.yaml` 与 `compose.cuda.yaml`；`compute-runtime` 提供依赖，丢弃的 `compute-package` 编译唯一 compute allowlist，fresh final stage 为任一 variant 安装它 | `plastic-promise-local-inference-node` |

Compose file 是后续经审查 activation 的 recipe input。其存在不构成 activation、listener
binding、tunnel creation 或 runtime-asset generation 的授权。

## 6. 静态 recipe preflight 与不可变 base-image catalog

P1 修复引入了针对仓库 Dockerfile、Compose file 与 `.dockerignore` 的**纯静态
preflight**。它只读取这些源码文件并输出规范化的 `RecipePolicyReceipt`；不会调用
Docker、联系 registry、读取 credential、启动 container 或检查 host。

该 receipt 覆盖完整三角色 recipe matrix；只要下列任一条件不成立就 fail closed：

- 每个 `FROM` 都通过版本化 base-image catalog 解析到 pinned `@sha256` digest；该 catalog
  是 source evidence，不是 registry lookup；
- 每个 Dockerfile 只接受已检查的 instruction vocabulary。edge 只有一个 static final
  stage；server 使用一个丢弃的 `server-package` stage 和一个 fresh final stage；compute
  使用 reusable dependency stage、一个丢弃的 `compute-package` source stage，以及从
  dependency stage 派生的 fresh final stage。两个 Python recipe 都调用同一仓库拥有的
  `RolePackageCompiler`；完整源码不会被复制到 final stage，也不依赖 delete/whiteout prune。
  其他 stage、role selection、copy、BuildKit flag 或 mount type 都会被拒绝；唯一允许的
  mount 是 compute recipe 中指定的 build-cache mount；
- Dockerfile 拒绝 `SOURCE_REVISION=unknown` 与 `PACKAGE_VERSION=unknown`，保留最终 non-root
  `USER`，且不使用 floating base image；
- CPU/CUDA Compose build argument 传递 concrete source revision、package version、所选 catalog
  base-image digest 与 selected variant；
- Compose service 保持 `read_only: true`、只暴露 loopback/private listener、不 mount Docker
  socket，并且仅声明有界、与 role 相符的 runtime state；以及
- 每个 recipe 只携带精确、经审查的 role-authority label；edge/compute 声称任何 server
  authority 都会 fail closed，所有 role 也都会拒绝 collaboration writer environment
  configuration；每个 Python recipe 必须在安装之后移除已经 allowlisted 的
  `/app/plastic_promise` staging copy；final OCI rootfs inventory 会拒绝精确 role surface
  之外的重复或额外 package path；以及
- `.dockerignore` 排除 canonical SQLite、derived LanceDB、model weight、credential、runtime
  state、log 与 cache，避免它们进入 image。

base-image catalog identity 与 `RecipePolicyReceipt` digest 会进入 prepared policy。因此改变
image base、Dockerfile、Compose file 或相关 ignore boundary 会使旧 plan 失效，而不是静默复用
其 label 或 evidence。

## 7. 严格的构建与 runtime 分离

下列边界刻意设计为机械约束，避免未来部署 UI 把 build descriptor 误当为授权：

```text
source + selection
        -> static recipe-policy preflight
        -> RecipePolicyReceipt + pinned base-image catalog entry
        -> RolePackageCompiler -> static/server/compute allowlist package
        -> ContainerArtifactCompiler
        -> ArtifactBuildPlan
        -> ArtifactBuildExecutor
        -> final OCI rootfs application inventory（layer + whiteout semantics）
        -> application_inventory_digest + collaboration_surface_digest
        -> ArtifactEvidenceReceipt set
        -> ArtifactBundle + inspection receipt

ArtifactBundle 是后续工作的输入证据，不是 runtime command。
```

PR 3 不向其 local/runtime 或 production surface 授予下列任一权限：

- 启动 container、为了 activation 调用 Compose、分配 runtime GPU、绑定 listener 或加载
  model；
- push 或 publish OCI image、发布到 GHCR/PyPI、签署 release 或使用 release credential；
- 创建 SSH 或 reverse-SSH tunnel、注册/enroll node，或联系 remote endpoint；
- mount SQLite、迁移 SQLite、推广 LanceDB、启动 Maintenance、重启 MCP 或写入
  Deployment Receipt；或
- 改变生产、stable release state 或外部 runtime configuration。

PR 3 不新增也不调用本地 runtime activation。它只为既有、由 operator 显式调用的
Release Builder 加固 resolved immutable build identity 与构建后 label check。该 builder
仍在本 compiler 的 authority boundary 之外；PR 3 source policy 与受保护 verification
workflow 均不会运行它。

受保护 CI verification adapter 只能进行 **verify-only**、不 push 的 OCI 工作。它执行相同的
静态 preflight，请求 Buildx SBOM/provenance attestation layer，并且只有在其 subject 与已检查的
OCI image 一致时才生成/校验 receipt。它不 publish、不施加 release signing、不 deploy、不联系
production host，也不创建 runtime receipt。本文不声称这类 workflow run 已完成。

PR 4 通过受限 Deployment Center / host-adapter path 消费已验证 descriptor。PR 5
拥有 backup-bound migration、实际 runtime mount、tunnel activation、LanceDB promotion
和 operational restart evidence。PR 6 拥有 release-readiness packaging 及任何需独立授权的
publication。

## 8. 证据与回滚的解释

PR 3 证据限于确定性的 source/build-policy check：matrix coverage、stable rejection
reason、recipe-policy receipt、OCI label/entrypoint/mount/listener policy inspection 与
final-rootfs application inventory，以及 fake-executor inspection receipt。每个计划 artifact
都绑定一份 body-free `ArtifactEvidenceReceipt`，其中记录 target、source revision、package
version、base-image digest、plan 与 recipe-policy digest、
`collaboration_surface_digest`、`application_inventory_digest`、OCI layout/image digest、
label digest 以及 SBOM/provenance digest。inventory 只在应用 OCI layer、普通 whiteout 与
opaque whiteout 后计算。验证要求 server 精确包含 role contract 绑定的 collaboration allowlist，拒绝
edge 或 compute 中的任意 collaboration package path，并拒绝所选 role allowlist 外的任何
Python package path。server 不得携带 compute-node 或 release-builder package；compute
不得携带 MCP、SQLite/memory/knowledge/collaboration/deployment/Maintenance/
release-builder package。SBOM package namespace 必须等于 final-rootfs namespace。
receipt set 会拒绝缺失 target、重复 target，或与 plan/label/identity 不匹配的内容。

`provenance_digest` 标识已经在 local OCI layout 内检查的 Buildx provenance-attestation layer。
PR 3 不声称它已被 release-signed、uploaded、GitHub-attested 或被 registry accepted。同样，若运行
CI build smoke，它只证明该次 build invocation，不能作为 deployment evidence。

由于 PR 3 没有获授权的 runtime mutation，回退其 source change 不需要删除已有 image、
volume、model cache、database file 或 host state。生产回滚仍是需要 verified backup 与
显式授权的 PR 5 操作。

PR 3 证据不使用 MCP health、endpoint reachability、local-node registration、container
status 或 production state；这些仍属于独立 runtime check。
