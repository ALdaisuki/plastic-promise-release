# OCI 包命名

Plastic Promise 的 OCI 发布包统一使用一个可预测的 GHCR 命名空间。PyPI
发行包仍然使用 `plastic-promise`；版本由 wheel/sdist 元数据和发行 tag
共同表达。

## 规范名称

```text
ghcr.io/aldaisuki/plastic-promise-edge:vX.Y.Z
ghcr.io/aldaisuki/plastic-promise-server:vX.Y.Z
ghcr.io/aldaisuki/plastic-promise-compute:vX.Y.Z-cpu
ghcr.io/aldaisuki/plastic-promise-compute:vX.Y.Z-cuda
```

每个已发布镜像还会获得不可变源码 tag：

```text
sha-<完整源码提交>
sha-<完整源码提交>-cpu
sha-<完整源码提交>-cuda
```

部署始终使用构建返回的 digest，不使用可变的版本 tag 或源码 tag。规范映射
由 `plastic_promise/release_package_naming.py` 维护，并由 stable workflow
消费，从而避免包改名后发行 manifest 静默漂移。

## 兼容性

读取历史 release manifest 和部署 receipt 时，仍接受旧的扁平仓库：

```text
ghcr.io/aldaisuki/plastic-promise-local-edge
ghcr.io/aldaisuki/plastic-promise-server
ghcr.io/aldaisuki/plastic-promise-local-inference-node
```

新发行不再向旧仓库发布新 tag。已经按 digest 固定的部署继续有效；迁移时将仓库
替换为规范名称，并重新核对新的 release manifest。
