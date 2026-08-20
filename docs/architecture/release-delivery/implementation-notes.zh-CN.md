# 发行交付实施说明

> **状态（2026-08-20）：已有部分证据。** 当前 stable publisher 仍未接入 PR 6
> selected-evidence gate，但 run `32376104075` 已按 immutable digest 推送四类 stable
> GHCR OCI role。PyPI Trusted Publisher exchange 以 `invalid-publisher` 失败，因此该 run
> 不是完整 stable release，release-manifest job 也被跳过。

## 运行时边界

发行子系统独立于 MCP 与 SQLite/LanceDB 状态。不要把发布凭据、发行副作用或容器构建代码放入
`plastic_promise.mcp`、记忆管道或 deployment controller。

## 必需 GitHub environments

| Environment | 使用方 | 必需保护 |
|---|---|---|
| `release-candidate` | `release-rc.yml` | 默认分支 dispatch；`source_ref` 等于 workflow SHA；`github.ref_protected=true`；显式 reviewer/restriction。 |
| `testpypi` | `release-testpypi.yml` | 显式 reviewer；仅 RC ref。 |
| `production-release` | `release-publish.yml`、release-repository `release-sync.yml` | 显式 reviewer 与受限 stable tag/ref。它保护当前过渡 publisher，但尚未接线 PR 6 Bundle/catalog/attestation gate。 |

为 TestPyPI/PyPI 配置对应仓库、workflow 文件和 environment 的 Trusted Publisher；不要向仓库 secret
加入 API token。GHCR visibility/retention 单独配置；manifest 使用 digest，即使存在 convenience tag。

## 验证命令

```bash
python scripts/validate_release_variant.py --repo-root . release/variants/standard.json
python -m pytest -q --no-cov \
  tests/test_release_variant.py tests/test_release_workflow.py tests/test_release_manifest.py \
  tests/test_model_catalog.py tests/test_release_bundle.py tests/test_create_release_bundle.py
python -m build --outdir dist
python -m twine check dist/*
python -m venv .release-wheel-venv
.release-wheel-venv/bin/python -m pip install --no-deps --only-binary=:all: dist/*.whl
```

本地 build 只确认 Python artifact；不会发布 TestPyPI/PyPI/GHCR、创建 tag 或触及发行仓库。

RC 要求真实、source-controlled、fixed-revision Model Catalog；仓库不提供可误发布的 active 示例。
首个 matrix 为 `split-accelerated` / `remote-inference` + `embedding/v1` / `rerank/v1`，构建
edge/server/CPU amd64+arm64 与 CUDA amd64，并派生 opaque 的逐 artifact CycloneDX receipt set。
workflow 在最终 Bundle 前对 catalog、`artifact-binding/v2`、receipt set、manifest 做 attestation
verification。独立、canonical 的 `artifact-sbom-receipts.json` 绑定每个 OCI-layout root、image
digest、role/platform/variant、SBOM digest 与 SBOM byte size；不携带 SBOM path 或 payload。禁止
在 workflow 外伪造 `verified-evidence.json`。

## Stable handoff

以下是**目标** handoff，不能由当前 `.github/workflows/release-publish.yml` 推定：它只接受
`source_ref`、`release_version`，不会消费或验证 PR 6 Release Bundle、Model Catalog、artifact
binding 或 RC attestation。

RC Buildx 会为每个精确 platform image 生成内嵌 SBOM/provenance，并验证 attestation subject
等于对应 image digest；独立 Syft 扫描仍生成逐 artifact CycloneDX SBOM。**v2** artifact binding
分别记录 OCI-layout、image、label、内嵌 SBOM、provenance、verifier receipt 与独立 Syft SBOM
digest，不能把两类 SBOM 证据合并为一个字段。

1. 新增 selected-evidence gate，验证 requested stable source 对应的精确 RC Bundle/catalog/
   binding/attestation。
2. gate 通过后才运行 protected stable publisher，并保留 release-manifest artifact 与 GitHub
   attestation。
3. 将 manifest、精确 source commit、release evidence 和 reviewed commit range 交给
   release-repository workflow。
4. release-repository workflow 拒绝 RC channel 或 commit/version 不匹配 evidence，再执行审计过的 sync。

每个 protected workflow 都必须由 operator 显式批准；合并 `main` 永远不是发布授权。
