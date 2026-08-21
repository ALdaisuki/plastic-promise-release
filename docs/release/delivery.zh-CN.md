# 发行交付与安装 Profile

> **截至 2026 年 8 月 20 日的状态：**`v0.2.15` stable workflow 已经通过 GitHub
> 构建并按 immutable digest 推送四类 GHCR OCI role。Python wheel/sdist 已构建、检查并
> 生成 attestation，但 PyPI Trusted Publishing 被 PyPI 以 `invalid-publisher` 拒绝；在
> 外部 publisher 配置完成前，不宣称 PyPI publication 或完整 release manifest 已完成。

对应 run 为
[`32376104075`](https://github.com/ALdaisuki/plastic-promise-release/actions/runs/32376104075)，
已验证的 OCI digest 如下：

| Role | Immutable digest |
| --- | --- |
| `local-edge` | `sha256:65c6d54a5c1cbbf96f837bef05f26e4756283b1060c98223dc108682aaf433ea` |
| `server` | `sha256:40e0d61d1efb59899ab4160b9dd34001a2198781966564cae95aca47534340dd` |
| `inference-cpu` | `sha256:947ffcfe0c43b9fc228ecb99755bc7113cdc49a881ce1baf9b95b68e5a20d189` |
| `inference-node`（CUDA 控制镜像） | `sha256:fb48386f34bcd6693ea6f1de9ad829c64ad3e772f3ca87a3859d7b023dd41d2a` |

这些 digest 是构建证据，不代表 Mac canonical runtime 或 Windows 节点已经切换。真正部署仍
需要成功拉取、健康检查和 runtime receipt。

Plastic Promise 有一份 source distribution contract 和三个受支持的目标 runtime profile。
Profile 只改变 runtime 与 inference work 的放置位置；不会改变所有权：
`pp-server-backend` 是 SQLite 唯一 writer，LanceDB 是可重建的派生状态。

## 安装选择

| 需求 | 基础目标选择 | 推荐目标选择 |
| --- | --- | --- |
| 使用 hosted inference 的 MCP runtime | 基础 package 加 `local-cloud` profile | 精确 reviewed wheel 加 controlled configuration revision |
| 全本地开发 | `local-all-in-one` profile | 仅在 Python behavior 验证后再使用 optional Rust core |
| 服务器加本地加速器 | `split-accelerated` profile | 有界 compute-node identity 和受限私有 transport |
| OCI server runtime | 按 digest 选择 server image | 由 reviewed Release Bundle 引用 verified digest |
| Linux NVIDIA compute node | 按 digest 选择 compute-node image | 只读、node-local model mount 和匹配的 identity evidence |

manifest 字段、resource preflight 和 profile boundary 见[部署 Profile](../deployment/profiles.zh-CN.md)
及[三端架构](../architecture/three-endpoint-deployment/architecture.zh-CN.md)。

## 交付通道与权威

| 通道 | 目标输出 | 外部权威 | 非声明 |
| --- | --- | --- | --- |
| Pull-request verification | test result 和 no-push artifact | 无 | 不创建 tag、image publication 或 deployment。 |
| RC artifacts / candidate | 精确 wheel/sdist hash、OCI-layout evidence、SBOM/provenance reference；可选的**目标** TestPyPI candidate | 受保护 candidate workflow | 不等同于 stable publication，也不代表 TestPyPI 已收到 package。 |
| Stable immutable evidence | digest 固定的 OCI evidence 加 selected Release Bundle | **目标**受保护的 selected-evidence gate；当前 stable workflow 尚未接线 | 不等同于 live server。 |
| Stable-only repository handoff | selected stable manifest 和 release receipt projection | 独立 protected release-repository workflow | 仅当该 workflow 成功时才可宣称 PyPI/GHCR publication。 |

PR 不会自动创建 stable tag、上传 package、push image 或同步
`plastic-promise-release`。
Release Bundle 与发布工件不得包含 SQLite 数据库或其他 canonical memory 正文。

目标 `production-release` selected-evidence gate 属于 **GitHub 受保护工作流**，不能由 PR 合并推定。
它必须独立验证 selected Release Bundle、Model Catalog、artifact binding 与 RC attestation 后，
并在目标支持 trusted publishing 时使用 GitHub OIDC 身份而非持久化 registry/PyPI token，
才**可能**发布规范的 `ghcr.io/aldaisuki/plastic-promise-server` 和
`ghcr.io/aldaisuki/plastic-promise-compute` OCI package，
或把 TestPyPI candidate 推进到 stable channel。

**当前实现边界：**`.github/workflows/release-publish.yml` 是一个独立受保护的过渡期 stable
publisher。它只接受 `source_ref` 与 `release_version`，目前**不会**消费或验证 PR 6 Release
Bundle、Model Catalog、artifact binding 或 RC attestation。因此它不是上文的 selected-evidence
gate，不能作为 PR 6 stable handoff 已完成的证据。上述 release role 与 selected-evidence gate
在该 workflow 完成接线并获得独立验证前，仍都只是目标。

历史部署 receipt 仍可能引用旧的
`ghcr.io/aldaisuki/plastic-promise-local-inference-node` 仓库。Release manifest
校验器为读取兼容继续接受它；新的 stable 发布统一使用上面的规范 compute package。

## PR 6 目标：Model Catalog 与 Release Bundle

source-level `ArtifactBundle` 只证明 build-policy inspection。PR 6 引入独立的**目标**
Release Bundle contract，用于选择 candidate 并在 protected release boundary 之间携带
immutable evidence。

| 项目 | 必需目标字段 | 明确排除 |
| --- | --- | --- |
| Model Catalog | 不透明 catalog ref/digest；固定 model revision；identity/capability；compatibility 与 resource metadata | weight、local path、provider token、node address 和 deployment authority |
| Artifact Bundle | **v2** role/platform/variant descriptor；不可变 image evidence；独立 attested `artifact-sbom-receipts.json` 的 digest | container start、registry push、service control、migration 和 promotion authority |
| Release Bundle | source revision；package version；protocol compatibility；支持的 profile/variant matrix；image/逐 artifact SBOM-receipt evidence 与已验证的 protected-build provenance；Model Catalog ref/digest | mutable tag、runtime config、canonical state、credential 和 candidate 已 live 的声明 |
| Release manifest | 供 protected workflow 消费的 selected、reviewable Release Bundle projection | execution grant、Migration Operation evidence 或 health receipt |

本文提到的所有 digest 与 signature 都是目标证据要求。只有相应 protected workflow
产生并独立验证后，文档字段才可成为证据。

当前 RC workflow 有意只实现一套完整 candidate matrix：`split-accelerated` 配合
`remote-inference`、`embedding/v1` 和 `rerank/v1`；local edge、server 与 CPU compute
覆盖 `linux/amd64` 和 `linux/arm64`，CUDA compute 仅覆盖 `linux/amd64`。其他任何
profile/runtime/capability 组合都会在 OCI build 前 fail closed。本目标合同不提交 active
model catalog：启动 RC 时必须由 operator 提供真实、受 Git 跟踪且 fixed-revision 的 catalog；
在 workflow 成功运行前仍不存在任何 attestation。

RC workflow 必须从仓库默认分支发起，且工作流对应的不可变 SHA 必须与输入的
`source_ref` 完全相同；如果 GitHub 未报告该 ref 受 branch protection rule 或 ruleset
保护，workflow 会 fail closed。耗时 build job 使用 `release-candidate` environment，
仓库设置必须在该环境上配置所需 reviewer/restriction。OCI work 前它会验证 source package
version 与请求的 RC version 一致；bundle creation 会拒绝 Git identity 与声明 source revision
不完全相同的 checkout、tracked catalog 或 catalog blob。

package SBOM（`package.sbom.cdx.json`）在一个新鲜环境中安装精确构建的 wheel 及其 runtime
dependency 后生成，再由 release manifest 单独绑定。Buildx 还会为每个 OCI platform image
生成内嵌 SBOM 与 provenance attestation；verifier 会把其 subject 绑定到精确 image digest，
并记录 OCI-layout、image、label、内嵌 SBOM、provenance 与 verifier receipt digest。
与此独立地，对于 candidate OCI matrix，workflow 会扫描
每个 OCI archive 中的精确 platform entry，并为每个展开 artifact 派生一份 opaque CycloneDX
SBOM receipt。独立 attested、canonical 的 `artifact-sbom-receipts.json` 绑定 OCI-layout root
digest、image digest、role/platform/variant、SBOM digest 与 SBOM byte size；其中不含 SBOM path
或 payload。`artifact-binding/v2` 同时保存每个 artifact 的 BuildKit 内嵌证据 digest、独立
Syft SBOM digest 以及 receipt-set digest，并拒绝不匹配的 receipt matrix。
bundle parser 在接受 label 或 image digest 前还会核对 OCI descriptor 原始字节的 SHA-256 与 size。

## Source-only 验证边界

source distribution 有意包含 `scripts/init_and_start.py`，但它仅是供 operator 使用的**可选
运行时启动器**。它不是默认安装入口，PR 或 source-only release verification 绝不会调用它。
这些检查只会解包精确 sdist，并针对 example manifest 运行无副作用的
`scripts/verify_release_deployment.py` proof。它们不得启动 MCP 或 Maintenance、调用 Docker、
创建 backup、迁移 SQLite、推广 LanceDB，或改变任何 runtime state。

解包 sdist 后的 proof 使用 `--no-deps` 安装，并且只 import parse、plan、preflight
和内存 asset rendering 所需的纯 deployment leaf module。因此 server runtime dependency
不是这个 source-only 边界的隐式前提。

## 构建权威与运行时分离

现在所有 Docker/OCI 镜像构建都由 GitHub Actions 负责。受保护的
`release-verify.yml`、`release-rc.yml` 与 `release-publish.yml` 工作流统一负责
Buildx、多架构产物、SBOM/provenance 证明以及 registry 发布。Mac、Windows 或
WSL2 工作区不得再本地构建发行镜像，也不能把本地构建当作等价的发行证据。

本地机器仍可执行源码检查、recipe 校验、资源预检，并使用已经发布的、按 digest
固定的计算节点镜像进行派生推理 smoke。它们只消费已验证的镜像 digest，不产生
该 digest。

```text
源码 SHA -> GitHub 受保护 Buildx -> SBOM/provenance -> 不可变 digest
         -> 选定发行证据 -> server/edge/compute 部署
```

仓库中的本地构建辅助脚本为兼容旧环境而保留，但它们不属于发行权威，其输出不得
晋升为 RC 或 stable 证据。新的发行操作应调度 GitHub 工作流，而不是执行本地
`docker build` 或 `docker buildx build`。

### 运行时拓扑

```text
+------------------- Windows / WSL2 --------------------+
| 源码检查 + digest 固定的运行时/GPU smoke              |
| 不构建镜像；不写 SQLite；不拥有发行权威               |
+------------------------+-------------------------------+
                         | 仅 candidate input
                         v
+---------------- GitHub 受保护工作流 -------------------+
| RC/stable build -> SBOM/provenance -> immutable digest |
| -> 目标 Release Bundle 和 release-manifest evidence    |
+------------------------+-------------------------------+
                         | 仅 verified selected digest
                         v
+--------------------- Server runtime -------------------+
| pp-server-backend：canonical SQLite single writer      |
| LanceDB：derived/rebuildable；目标 MCP/Maintenance     |
+------------------------+-------------------------------+
                         | 有界、无 secret receipt
                         v
+--------------- Stable-only 发行仓库 -------------------+
| 目标：显式同步 + 单独批准的 publication                |
+---------------------------------------------------------+
```

Windows/WSL2 现在只用于源码检查和 digest 固定的运行时/GPU smoke。它可以产生
**派生推理** smoke evidence；不能构建发行镜像、成为 canonical writer，也不能替代
protected release evidence。GitHub protected automation 是 immutable release evidence 的生产者。服务器只拉取
manifest 固定且已验证的 digest，绝不是 build authority；服务器必须返回有界的 **MCP E2E 回执**，
之后才可考虑向 **stable-only 发行仓库**交接。

## 受控服务器消费

目标 server 接受 selected digest 前，composition 必须保持一个 canonical runtime lock
和一个 SQLite writer。随后服务器可以执行彼此独立、已授权的 Migration Operation、
derived shadow rebuild/promotion、MCP verification 与 Maintenance transition。它们均独立于
bundle 本身，只有先返回有界、无 secret 结果，才可考虑 stable-only handoff。

image digest 或 RC artifact 都不能恢复数据。后续 gate 失败时，保留 SQLite 和
audit/source evidence，选择此前的 immutable bundle/digest，并从 canonical state 重建
LanceDB。不要从派生索引恢复 memory text。

参见配套的[六 PR 就绪度计划](six-pr-readiness.zh-CN.md)和
[部署指南](../deployment/README.zh-CN.md)。
