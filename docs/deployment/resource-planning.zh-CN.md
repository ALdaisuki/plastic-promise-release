# 资源规划

英文对等文档：[`resource-planning.md`](resource-planning.md)。

本文描述由部署控制器拥有的资源边界。每个 manifest 都必须包含完整且不含密钥的
`resource_budget`；缺失时，预检以 `resource_budget_required` 失败关闭。声明的预算
覆盖所选部署的完整写入集合，控制器还会加入自身的 SQLite 写入预留：

- SQLite 在线备份空间；
- 与 SQLite 主文件一起观测到的精确 WAL/SHM 边车文件；
- 镜像层和解包后的镜像空间；
- 模型缓存；
- LanceDB shadow rebuild 空间；
- 回滚版本并存空间；
- 直接从已配置、无密钥的本地路径观测到的既有 Docker 与模型缓存占用，而不是从
  manifest 复制的估计值；
- restore candidate 暂存空间；
- 有界版本化迁移的 scratch 空间；以及
- 空数据库 bootstrap 分配。

manifest 的 `resource_budget` 提供经测量的计划写入量。它的
`resource_locations.container_store` 和 `resource_locations.model_cache` 指向这些
选定写入会落入的本地文件系统。预检递归测量当前占用但不跟随符号链接，按物理文件
系统对 state/container/model 路径分组，并在每个分组卷上应用安装后的可用空间预留。
选定位置缺失或不可读会阻断整个操作；不存在但可写的缓存目录按当前零占用报告，规划
阶段不会创建它。已有 Docker/模型占用仅报告，不会从可用空间中重复扣除。profile
catalog 以机器可读元数据暴露公共策略（`minimum_free_bytes`、
`minimum_free_fraction`、state host 和 `model_artifacts_bundled=false` 保证），避免
后续适配器发明第二套默认值。

每个控制器操作都会在任一受影响卷的预计可用空间低于
`max(20%, 10 GiB)` 时拒绝继续。planning 和 preflight 都是只读操作：不会创建请求
的 state root、SQLite 文件、备份或临时 SQLite 状态。

source-level 的 `pp-local-edge` Deployment Center 是静态、没有权威性的规划 projection。
它的宿主侧 `ppctl` interface 只接受类型化 `inspect` 与 `preview` operation：后者可以展示
estimate、manifest diff、update class 和仅供检查的 plan hash，但不能 apply。在 endpoint 被
显式配置并另行授权监听前，当前 Dashboard 仍是过渡性投影，不能作为生产迁移完成的证据。
两种视图都不能展示本地 path、下载工件、创建 state、迁移 SQLite、联系/登记 node，或宣称
安装器计划已被接受。profile override 不能绕过 resource refusal。宿主控制器 preflight
仍是它所拥有操作的唯一硬门禁；PR 5 才拥有单独授权的 mutation。

为避免混淆，`manifest_comparison` 只做 digest 级比较。只有 controller 能安全投影 active
topology 时，才提供结构化 V2 `manifest_diff`；其中仅含 profile/module/endpoint/capability
identifier，否则明确报告 unavailable。`update_class` 是保守的 PR 4 inspection 输出
（`no-change`、`enrollment-required` 或 `manual-review`），不是 action decision。展示的
plan hash 绑定这份安全 observed projection 用于 drift reporting，不能授权执行。

## 硬件规划基线

以下是单用户部署的保守起点，不能替代控制器实测的 preflight。磁盘数值是在控制器为
每个受影响卷预留 `max(20%, 10 GiB)` 前的可用空间。固定模型 revision、镜像层、备份
和 shadow rebuild 都可能显著提高所需空间；应将实测值记录到无密钥
`resource_budget`，而不要把该表当作承诺的估计值。

| Profile 与角色 | 最低 CPU / RAM / VRAM / 可用磁盘 | 推荐 CPU / RAM / VRAM / 可用磁盘 | Docker、GPU、网络和模型前提 |
| --- | --- | --- | --- |
| `local-all-in-one` 三端 | 4 逻辑 CPU、16 GiB RAM、0 GiB VRAM、50 GiB | 8 逻辑 CPU、32 GiB RAM、本地 GPU 模型选中时 8 GiB VRAM、100 GiB | 目标容器拓扑需要 Docker/Compose；GPU 可选。`pp-local-edge` 保持 loopback-only，且只有 `pp-server-backend` 挂载 SQLite。 |
| `local-cloud` edge + backend | 2 逻辑 CPU、8 GiB RAM、0 GiB VRAM、30 GiB | 4 逻辑 CPU、16 GiB RAM、0 GiB VRAM、60 GiB | 目标容器拓扑需要 Docker/Compose 与到所选 provider 的出站访问。云端 identity 为权威；凭据保留在 manifest 外。 |
| `split-accelerated` `pp-server-backend` | 4 逻辑 CPU、16 GiB RAM、0 GiB VRAM、80 GiB | 8 逻辑 CPU、32 GiB RAM、0 GiB VRAM、160 GiB | 需要私有 tunnel。它是唯一 SQLite writer，拥有 LanceDB generation verification/promotion；不会公开 node inference 端口。 |
| `split-accelerated` `pp-compute-node` | 4 逻辑 CPU、16 GiB RAM、8 GiB VRAM、50 GiB | 8 逻辑 CPU、32 GiB RAM、16 GiB VRAM、100 GiB | 需要带兼容 runtime 的 Docker/Compose、固定 embedding/rerank revision 和受控 model mount。它不保存 canonical SQLite 或 LanceDB generation。 |

## 运行时资源避让

计算节点还拥有运行时 admission guard；安装 preflight 不是唯一的资源边界。默认通过
`PP_LOCAL_NODE_RESOURCE_GUARD=on` 启用，并在开始新的推理请求前采样聚合 GPU 利用率。
如果其他设备、游戏、渲染器或无关加速任务超过配置阈值（默认 70%），节点不会参与
竞争：embedding 和 structured JSON 返回带 `Retry-After` 的 HTTP 429 `node_overloaded`，
而 server 的 rerank 合同保持原始顺序。health projection 暴露有界的 `resource_guard` 状态，但不会
暴露进程名、路径、凭据或模型正文。

外部 llama.cpp worker launcher 在创建 worker 前也使用同一套十秒只读资源门禁。运维者可以
用 `--status` 进行只读检查，用 `--stop` 可逆停止服务，模型文件保持不变。只有在受控维护
期间才允许显式设置 `PP_LLAMA_CPP_RESOURCE_GATE=off`；普通安装和发行 profile 保持门禁开启。

## 费用证据

本表是容量基线，不是价格表。provider 价格、出口流量、CI 分钟数、registry retention、
电力和硬件摊销都会在仓库外变化。任何用于部署决策的估算，都必须作为带日期的动态
证据记录，包含 provider/catalog revision、region、currency、model identity、预计量、
缓存命中假设和 fallback policy。文档不得将某个供应商的当前价格复制为无期限的默认值。
