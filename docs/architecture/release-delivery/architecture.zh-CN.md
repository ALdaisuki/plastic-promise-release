# Plastic Promise 发行交付架构

> **状态（2026-08-11）：目标 / 未验证。** 本文描述 PR 6 的 selected-evidence
> 发行门禁，不声明 RC、签名、证明、PyPI/GHCR 发布或生产部署已经发生。

## 1. 系统概览

发行子系统把已审查源码修订转换为可验证的分发证据，但绝不把发行权限交给 MCP
运行时。它覆盖 wheel、sdist、local-edge/server OCI 镜像与 CPU/CUDA 推理节点镜像。
PR 6 的目标规则是：只有 release manifest 与 Release Bundle 将 SemVer、不可变源码提交、
Python SHA-256、SBOM、OCI digest 和固定 Model Catalog identity 绑定后，发行才可被认为有效。

当前 `release-publish.yml` 是独立受保护的过渡期 stable publisher；它尚未消费或验证
PR 6 Release Bundle、Model Catalog、artifact binding 或 RC attestation。因此它不能证明
本文所述 selected-evidence gate 已经完成。

## 2. 架构图

对应流程图见 [workflow.mermaid](workflow.mermaid) 与
[中文流程图](workflow.zh-CN.mermaid)。README 提供面向贡献者的紧凑 C4 图。

## 3. 模块清单

| 模块 | 边界 | 职责 | 不做什么 |
|---|---|---|---|
| `plastic_promise.release_manifest` | 纯包合同 | 校验 SemVer/PEP 440、hash、SBOM、源码提交与 OCI digest。 | 构建、发布、调用 GitHub/Docker/MCP。 |
| `plastic_promise.deployment.release_bundle` | 纯发行合同 | 绑定 Model Catalog identity/resource、完整 OCI matrix 与已验证 evidence projection。 | 下载模型、查询 registry、自验签名或取得部署权限。 |
| `scripts/create_release_manifest.py` | 脚本适配 | 将完成的 CI evidence 转为新的不可变 manifest。 | 覆盖 evidence 或推测缺失 digest。 |
| `scripts/create_release_bundle.py` | 脚本适配 | 解析 OCI layout、写 canonical catalog/binding，并只从外部验证证据构造 Bundle。 | 构建/发布镜像、自证 evidence 或改写已证明字节。 |
| `artifact-sbom-receipts.json` | 独立 attested evidence | canonical 绑定每个展开 artifact 的 OCI-layout root、image digest、role/platform/variant、opaque CycloneDX SBOM digest 与 SBOM byte size。 | 携带 SBOM path、SBOM payload、model path 或发布权限。 |
| Release workflows | CI 权威 | 构建、证明并上传 RC candidate；当前 stable publisher 尚未接线 PR 6 selected evidence。 | 从 PR/普通 `main` 自动发布，或把 stable 发布当作 Bundle 验证。 |
| `scripts/release-sync.py` | 发行仓库交接 | 复制审计过的 stable tree 并强制来源/发行证据。 | 由普通 PR CI 调用。 |

## 4. 通信模式与当前边界

- PR verification 同步执行源码校验、artifact build、精确本地 wheel 安装与 no-push OCI build。
- RC 仅接受受 Git 跟踪、fixed-revision 的 `split-accelerated` /
  `remote-inference` catalog，且要求 `embedding/v1` + `rerank/v1`；默认分支工作流
  SHA 必须等于输入 SHA，并由 `release-candidate` environment 保护。
- TestPyPI 是独立受保护环境，之后精确安装已请求版本。
- Stable OCI 仅经 `workflow_dispatch` 与 `production-release`；当前 publisher 记录 digest
  evidence 和 convenience tag，但只接受 `source_ref`、`release_version`，尚未校验 PR 6 Bundle。
- stable-only sync 与 PyPI 发布是独立 release-repository 职责，不能接受 RC channel。

## 5. 数据流

1. RC workflow 将默认分支 SHA 与固定 `source_ref` 绑定，并验证受保护 ref。
2. 源码包版本必须匹配请求 RC version；wheel/sdist 需携带一致 metadata。
3. 新鲜环境安装精确 wheel 后生成 package SBOM，并由 manifest 绑定。
4. 首个 RC matrix 构建 edge/server/CPU 的 amd64+arm64，以及 CUDA compute 的 amd64。
5. Buildx 为每个 platform image 生成内嵌 SBOM 与 provenance attestation；verifier 把其
   subject 绑定精确 image digest，并记录 OCI-layout、label、内嵌 SBOM、provenance 与 verifier
   receipt digest。与此独立地，Syft 为每个 role/platform/variant 生成 opaque CycloneDX SBOM。
   canonical、独立 attested 的
   `artifact-sbom-receipts.json` 将其 digest/size 绑定到 OCI-layout root、精确 image digest
   与 role/platform/variant。
6. manifest 模块验证 artifact/source/digest 格式后写入新 manifest。
7. Bundle 脚本校验 checkout/catalog/source 一致、descriptor SHA-256/size 后写入 receipt set。
   `artifact-binding/v2` 为每个 artifact 携带 OCI-layout、image、label、内嵌 SBOM、provenance、
   verifier receipt 与独立 Syft SBOM digest，同时携带 receipt-set digest，并拒绝
   OCI/image/platform/SBOM 关联不一致的 receipt；随后写入 canonical catalog/binding。
8. GitHub 对 manifest/catalog/**v2** binding/receipt set 证明并验证后才生成最终 RC Release Bundle。
9. **未来** selected-evidence gate 必须验证该 Bundle/catalog/binding/attestation 后才可 stable
   发布；当前 `release-publish.yml` 尚未接线。发行仓库再单独校验 stable manifest 的版本与源码。

## 6. 状态与数据

发行交付没有 MCP-memory、task queue、SQLite 或 LanceDB 写路径。持久发行证据仅包括
artifact、opaque SBOM receipt set、GitHub attestation、manifest、Model Catalog、**v2** artifact
binding 与 Release Bundle。
运行时容器保持无状态，部署预检后才由 operator 挂载 SQLite/LanceDB 与 node configuration。

## 7. 失败策略

- 缺失、重复、损坏或 metadata 不匹配的 Python artifact 会使 manifest 失败。
- 可变 OCI reference、缺镜像、非法 SHA、缺 SBOM、缺 receipt set，或 receipt 与
  `artifact-binding/v2` 的 layout/image/platform/SBOM 关联不一致都会 fail closed。
- 缺失、未跟踪、逃逸路径、不兼容或 placeholder catalog 会在 RC OCI build 前失败。
- 三个 pre-bundle subject 未通过 GitHub attestation verification 前，不能生成 Bundle。
- PR/RC 不得 registry publication；stable approval 也不等于 PR 6 selected-evidence proof。
- stable sync 失败不修改 source evidence、MCP runtime 或 canonical data。

## 8. 安全模型

源码不保存包 token、registry password、私钥、模型、数据库、运行态或日志。TestPyPI/PyPI
使用配置好的 OIDC Trusted Publishing；release manifest 只包含公开引用和 hash，不含 secret。

## 9. 可观测性

发行以 workflow run URL、digest、attestation、artifact hash 与 manifest validation error 为主要
信号。stable manifest 只是 source/artifact identity 索引，不包含用户内容、基础设施地址或日志。

## 10. 成本与扩展

PR 不推镜像；RC artifact 仅保留 14 天；昂贵 stable multi-architecture build 需显式批准。
服务器镜像多架构；CUDA/NVIDIA 推理节点保持 Linux/amd64。

## 11. 技术栈

| 关注点 | 选择 |
|---|---|
| Python packaging | `python -m build`、`twine check`、隔离 wheel 安装 |
| OCI | Docker Buildx 与 OCI labels |
| SBOM | CycloneDX JSON |
| Provenance | GitHub artifact attestation |
| Registry | GHCR digest reference |
| 包仓库 | TestPyPI 演练、PyPI OIDC stable 发布 |

## 12. 实施阶段

1. 合同与 build verification：manifest、wheel/sdist、精确安装、Dockerfile、no-push PR。
2. candidate rehearsal：手动 RC artifact 与受批准 TestPyPI 精确版本安装。
3. **未来 PR 6 stable gate：**先验证 selected RC Bundle/catalog/binding/attestation，再进行
   protected GHCR evidence build、manifest 与 stable-only sync/PyPI OIDC。当前
   `release-publish.yml` 不是此 gate。

发行权始终在 MCP 工具面之外，这是最小权限边界。
