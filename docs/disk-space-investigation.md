# 工作目录磁盘空间调查报告

**日期**: 2026-07-01
**调查范围**: `F:\Agent\Memory system`
**总占用**: 1.3 GB

---

## 一、总览

```
1.3G  rust/context-engine-core/target/     ← ❶ 99.5% 的空间在这里
14M   其他所有文件合计                       ← ❷ Python 源码、文档、测试等
```

整个仓库 220 个 Git 跟踪文件仅占 **~14 MB**。1.3 GB 全部来自 Rust 编译产物。

---

## 二、罪魁祸首：Rust `target/` 编译缓存

`rust/context-engine-core/target/` 是 Rust 的构建输出目录，相当于 Node.js 的 `node_modules` + `dist`，但 Rust 还额外缓存了增量编译中间产物。

### 2.1 三层结构

| 目录 | 大小 | 文件数 | 内容 |
|------|------|--------|------|
| `deps/` | **840 MB** | ~1,700 | 116 个 `.rlib` (依赖预编译库) + 42 个 `.pdb` (Windows 调试符号) + 445 个 `.rmeta` (元数据) + `.d` (依赖描述文件) |
| `build/` | **280 MB** | ~2,000 | 原生库构建脚本输出 (SQLite、Zstd、Lance 等 C/C++ 库的编译中间文件) |
| `incremental/` | **185 MB** | ~1,600 | 增量编译缓存 — 5 个历史构建会话的快照，用于加速后续编译 |

### 2.2 依赖爆炸

`Cargo.toml` 仅声明了 5 个直接依赖：

```toml
pyo3, serde, serde_json, chrono, rusqlite
```

但 `Cargo.lock` 解析出 **74 个 crate**（656 行），实际编译了远超预期的库。最大的几个：

| 编译产物 | 大小 | 来源 |
|----------|------|------|
| `libcontext_engine_core.rlib` | 23 MB | 本项目 |
| `libpyo3-*.rlib` | 16 MB | Python 绑定框架 |
| `libzerocopy-*.rlib` | 14 MB | 零拷贝序列化 |
| `libsyn-*` × 3 版本 | 6–12 MB each | Rust 过程宏框架（多版本共存） |
| `librusqlite-*.rlib` | 6 MB | SQLite 绑定（bundled 模式自带 C 源码） |
| `libsqlite3_sys-*.rlib` | 5 MB | SQLite C 库编译产物 |
| `libserde_json-*.rlib` | 5.5 MB | JSON 解析 |
| `libregex_*-*.rlib` | 11 MB (合计) | 正则引擎 |
| `libwindows_sys-*.rlib` | 4.2 MB | Windows 系统调用绑定 |

### 2.3 为什么这么大？

1. **Debug 模式编译** — 当前只构建了 `debug` profile（无 `release` 目录）。Debug 模式保留完整符号表、不优化体积、不裁剪未使用代码。
2. **Windows PDB 调试符号** — `deps/` 下 42 个 `.pdb` 文件，每个对应一个 crate 的完整调试信息。
3. **Bundled 原生库** — `rusqlite` 的 `bundled` feature 会从源码编译 SQLite（约 230K 行 C 代码），产生大量 `.o` 中间文件。
4. **过程宏多版本共存** — `syn` 这个宏框架以 3 个不同版本编译了 3 次（不同依赖要求不同 semver），每次 ~10 MB。
5. **增量编译缓存** — 保留了 5 个历史构建的快照（185 MB），每次 `cargo build` 生成一个新的。

---

## 三、其他目录（合计 ~14 MB）

| 目录 | 大小 | 说明 |
|------|------|------|
| `plastic_promise/` | 1.7 MB | Python 主包（MCP 服务端、记忆引擎） |
| `docs/` | 1.4 MB | 项目文档 |
| `plastic_memory.lancedb/` | 1.3 MB | LanceDB 向量数据库（3 个 `.lance` 文件） |
| `tests/` | 940 KB | 测试代码 |
| `bridge/` | 146 KB | Python↔Rust 桥接层 |
| `.git/` | 7.5 MB | Git 仓库元数据 |

全部正常，无需处理。

---

## 四、Git 跟踪情况

`.gitignore` 已正确配置 `rust/context-engine-core/target/`，该目录不会被提交。仓库本身（220 个跟踪文件）完全健康。

---

## 五、清理方案

### 方案 A：彻底清理（推荐，回收 1.3 GB）

```bash
cd "F:\Agent\Memory system\rust\context-engine-core"
cargo clean
```

下次 `cargo build` 会重新下载和编译全部依赖（首次约 3–8 分钟），但之后增量编译恢复正常。

### 方案 B：仅清理增量缓存（回收 ~185 MB）

```bash
rm -r "F:\Agent\Memory system\rust\context-engine-core\target\debug\incremental"
```

保留已编译的 `.rlib`，下次编译只需重新生成增量缓存。

### 方案 C：Release 构建减半

在 `Cargo.toml` 中已配置了 `[profile.release]`（opt-level=3, lto=true），但目前没有 release 构建产物。Release 模式编译约为 debug 的 1/3–1/2 大小，且无 PDB 符号。如果日常开发不需要调试 Rust 内部，可切换为：

```bash
cargo build --release
```

### 方案 D：添加到 `.gitignore` 之外再加 `clean-on-session`

在项目脚本中加入清理钩子，定期自动 `cargo clean`（适合不频繁修改 Rust 代码的阶段）。

---

## 六、结论

| 项目 | 数据 |
|------|------|
| 问题根源 | Rust debug 编译缓存 `target/` |
| 占用空间 | 1.3 GB / 1.3 GB 总计 |
| Git 影响 | 无（已 ignore） |
| 安全性 | 可随时删除，纯构建产物 |
| 恢复成本 | 首次 `cargo build` 约 3–8 分钟 |
| 建议操作 | **执行 `cargo clean`，回收 1.3 GB** |

> 注：用户提到"1.5G"，`du` 实测为 1.3 GB。差异可能来自文件系统簇大小（5,461 个小文件在 NTFS 上额外占用）或 Windows 资源管理器以 GB（10^9 bytes）计算而 `du` 以 GiB（2^30 bytes）计算。
