# 本地异机推理节点合同

> **PR 5 源码状态——运行时合同已实现，生产激活仍需独立证据。** 本页定义
> compute-node identity、admission、lease 与 result-record boundary。源码构建可以提供受治理
> 的 listener 与 adapter，但部署、迁移、tunnel 注册和生产验收仍必须分别生成 receipt。

`split-accelerated` 把 SQLite、LanceDB、outbox、lease、audit、promotion 与 project
isolation 留在 governed server。local heterogeneous inference node 只返回有界的
derived inference。下表是逻辑 contract operation，不是 PR 2 已绑定的 listener route：

| 逻辑 operation | 目的 |
| --- | --- |
| `GET /health` | Liveness 与 protocol version |
| `GET /v1/identity` | 完整固定 embedding/rerank identity evidence，维度必须声明而不是绑定某个固定模型维度 |
| `POST /v1/embeddings` | 最多 64 条有界 text input |
| `POST /v1/rerank` | 对每一条有界 candidate 恰好给出一次 score |
| `POST /v1/structured-json` | 可选的有界结构化结果；只有在 model/revision 激活并完成 identity revalidation 后才可用 |

Node 不拥有 SQLite、LanceDB、MCP、Dashboard、task queue、lease、outbox 或
canonical-memory dependency。server scheduler 决定 derived output 是否可用；node
永远不能 promotion 或直接写入 memory。

V2 endpoint manifest 不包含 `ssh_host`、private address/URL、filesystem path、
tunnel material、credential 或 authorization value；它只使用 opaque 的
`transport_ref` 与 `resource_policy_ref` label。Legacy V1 input 是独立的兼容资料，
V1/V2 的准确边界见[部署档案与端点 Manifest 合同](profiles.zh-CN.md)。

## 目标 server-side admission 与 scheduling（PR 4–5）

后续 governed-server adapter 将拥有 `NodeGovernanceStore`。它只能在
`split-accelerated` deployment manifest 已声明 node ID，且 server 独立验证 transport
evidence digest 并记录 server-produced verification receipt 后，才可接纳 local node。
server-owned canonical record 将 receipt 关联到 resolved deployment ID 或 controlled
configuration revision。记录只能保存 opaque transport label/digest，不能保存
operator 输入的 endpoint 或 credential。

Private tunnel 可以在 pinned model identity 未变化时轮换。观察到 model、revision、
dimension、normalization、metric、tokenization、pooling、artifact、golden-vector 或
reranker identity 漂移时，node 必须被 quarantine；只有新的匹配 health proof 才能恢复
scheduler 可用性。

server-owned SQLite `DerivedWorkStore` 是唯一 durable task outbox：它保存
project-scoped canonical reference、idempotency、lease-token hash、failure reason、
retry window 与 reconcile state。node governance 仅增加 registry evidence 和绑定
derived-work fencing generation 的短期 reservation，不会创建第二个 task queue。
只有 server 可以把 durable lease 绑定到 node；有效容量必须同时考虑 node 上报的 free
slot 与未到期的 server reservation，避免 refresh 导致 oversubscription。

在 PR 5 的显式 backup-and-migration flow 可用前，缺少 node-governance schema 的 runtime
应返回 `schema_missing` 并对 node registration/scheduling fail closed。MCP 或 control
plane status request 永远不得隐式创建 schema 或迁移 canonical SQLite。

Scheduling 不得把“dimension 匹配”当作兼容性：

- embedding 只有在完整 model/revision/dimension/normalization/metric/tokenization/
  pooling/artifact/golden-vector identity 与 active generation 一致时才可使用；
- rerank 有独立 model identity 与显式 `original-order` terminal fallback；
- structured JSON 是 `node_routing` 的一等 capability。经认证的 compute-node transport
  只接收有界、带 intent 绑定的 payload，并且必须返回与配置完全一致的 model/revision
  identity。只有 structured-json profile 激活并完成 revalidation 后才启用；没有健康且
  identity 匹配的 node 时，操作进入 retry/reconcile defer，server 不调用 cloud provider
  或 deterministic local fallback，node 也没有 canonical-memory write capability；
- `fastest-estimated` 只有在同一 node、operation 和 identity 下积累至少 20 次成功后，
  才可依据 median latency 与 queue/capacity 选择；此前确定性回退到
  `remote-node-first`；
- `pinned-node` 不得静默回落到另一 node。

`accelerator-max` 仍由 server 治理且默认关闭。其 admission 只允许有硬 concurrency、
queue、daily-work 与 memory budget 的有界非生成式 derived work。queue 和 UTC-day
admission counter 必须与 job creation 在同一 SQLite write transaction 中校验；lease
跨进程重启保持 concurrency。后台任务先全局让位于前台 embedding/rerank，并要求新鲜的
capacity evidence。可接受的 task 仅包括 index compensation、vector-relation candidate、
semantic-deduplication candidate、conflict-risk candidate、preclassification 与 scoring
evidence。结果仅是有界 proposal/outbox/evidence/derived artifact，不能写 canonical
memory 或 promotion LanceDB generation。

后续 control-plane status projection 只显示有界且无密钥的 registry/derived-work
counter。authenticated Dashboard 的 **推理节点** 页可显示每 node 的 health freshness、
declared/observed capability、expected embedding/rerank identity、dimension、available
capacity、bounded latency aggregate、quarantine reason、稳定 routing/degradation code、
node/accelerator queue counter、UTC-day admission counter 与有界 lifecycle audit。它不得
泄露 endpoint、credential、transport evidence、raw health payload、user content、task
input reference、result payload、project identifier、lease token 或 provider response。

后续 Dashboard 中 operator 显式选择 **脱敏诊断** 时，control plane 可以从严格 allowlist
生成 browser-local JSON download。它只包含稳定 component state、有界 counter 与
configuration-presence boolean；不发送 telemetry，也不含 node ID、model/revision、
endpoint/host/port、filesystem path、configuration value、credential、request/task payload
或 SQLite row。生成 bundle 不得创建 revision 或修改 runtime/database/deployment state。

## 目标 server-private bootstrap boundary（PR 4–5）

当后续 active `node_routing` revision 启用时，MCP startup 将在把 memory-index outbox
item 或 foreground rerank 路由至 node 前执行 fail-closed bootstrap；Maintenance adapter
也须采用相同 bootstrap，防止 canonical index replay 绕过 controlled route：

1. 以只读方式打开现有 control-plane store，要求 active revision 与匹配的
   `split-accelerated` deployment manifest；
2. 加载由 `PP_NODE_PRIVATE_ENDPOINTS_FILE` 指向的 server-only endpoint document；该文件
   必须是绝对、普通、非 symlink 且 POSIX `0600` 的私有运行资料；
3. document 只包含 opaque node ID、opaque transport ID 和 `127.0.0.1`/`::1` tunnel URL；
   authorization 必须存在，并且只能引用 `PP_NODE_AUTH_*` environment variable，不能写入
   document；
4. server 经 tunnel 发现 identity，将完整 embedding/rerank identity 与 active revision
   核验，然后再次 probe，同时记录 controlled registration receipt 与 health。

Private endpoint document 绝不能成为 configuration revision、Dashboard value、receipt
payload、public status field 或日志值。任一 bootstrap check 失败时，canonical SQLite
write 继续，相关 index outbox 保持 durable/retryable，但 route 必须被阻断，不能悄悄
调用 legacy ungoverned embedder。

Foreground rerank 使用与 durable index work 相同的 verified registration、capacity
reservation、完整 rerank-identity re-probe 与 latency evidence。live query/candidate text
保持 process-local，不得为了 scheduling 复制进 SQLite。没有 eligible node 或 private
call 失败时，操作记录有界的 defer/retry reason，并使用合同规定的终端排序策略；server
不得调用 cloud provider 或 server-local provider。execution-time identity 或
response-identity drift 应立即 quarantine node；需要后续匹配健康 probe 才能重新选择。

Private document 的 schema 仅用于未来 node-local runtime asset，不能提交进仓库或复制到
control revision；其中的 `authorization_env` 只能是环境变量名，不是 credential：

```json
{
  "schema": "private-node-endpoints/v1",
  "nodes": [
    {
      "node_id": "opaque-node-id",
      "transport_id": "opaque-transport-id",
      "base_url": "http://127.0.0.1:port",
      "authorization_env": "PP_NODE_AUTH_EXAMPLE"
    }
  ]
}
```

## 目标 node-local configuration（PR 3）

PR 3 将其作为 source-level artifact-policy input，而不是安装或 activation instruction。
后续 runtime adapter 若准备 node-local environment file，embedding/rerank revision 均须是
固定 identifier，不能是 `latest`、`main` 或 `stable`：

```text
PP_LOCAL_NODE_ID=workstation-inference
PP_LOCAL_NODE_AUTHORIZATION=Bearer <random-private-node-token>
PP_LOCAL_NODE_EMBEDDING_BACKEND=llama.cpp
PP_LOCAL_NODE_EMBEDDING_MODEL=Qwen3-Embedding-4B-GGUF
PP_LOCAL_NODE_EMBEDDING_REVISION=<fixed-40-hex-revision>
PP_LOCAL_NODE_EMBEDDING_DIMENSION=2560
PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=l2
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=http://127.0.0.1:19131
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=/v1/embeddings
PP_LOCAL_NODE_RERANK_BACKEND=llama.cpp
PP_LOCAL_NODE_RERANK_MODEL=Qwen3-Reranker-4B-GGUF
PP_LOCAL_NODE_RERANK_REVISION=<fixed-40-hex-revision>
PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=http://127.0.0.1:19132
PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=/rerank
PP_LOCAL_NODE_MODEL_CACHE_DIR=/models
PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=/models/embedding/<精确模型>.gguf
PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=/models/rerank/<精确模型>.gguf
```

node ID 是 Control 接受的小写 ASCII 协议标识，与 Dashboard 的本地化展示名相互独立。
runtime artifact identity 会哈希精确引用的 GGUF 文件字节（或显式引用的模型目录树）。

目标 config check 会在不加载 model weight、不下载、不打开 listener 的条件下验证此
contract。可选 `local-inference` package extra 在同一个固定 contract 后提供治理化
adapter：

- `llama.cpp` embedding 与 rerank：两个独立 loopback llama-server 返回结构化向量和
  分数；两个 worker 复用同一个不可变 `PP_LLAMA_CPP_IMAGE` digest 和一个只读模型根目录，
  Docker 只存一份共享的 llama.cpp/CUDA runtime layer。两个 GGUF 仍保持独立，因为它们
  是不同权重；合并文件不会降低显存驻留，还会破坏独立生命周期和过载控制。模型、revision、
  维度与 digest 仍绑定到 Plastic Promise 节点身份，生成文本会被拒绝；
- `bge-local` embedding 与 `bge-local` rerank（sentence-transformers /
  `AutoModelForSequenceClassification`，`local_files_only=True`）；
- `ollama` embedding（例如 `qwen3-embedding:4b`，2560 维、L2）：节点在每批请求前后
  通过配置的本地 `/api/tags`（loopback 或显式 `host.docker.internal` Docker Desktop
  网关）绑定 Ollama 模型 digest，digest 漂移时拒绝请求。Ollama 没有请求级 artifact
  绑定，因此中批 A->B->A 替换无法被完全排除；把这些向量交给 LanceDB generation
  仍需要单独验证的 shadow rebuild 与 promotion 门控。Ollama 永远不会获得
  canonical-state access；
- `qwen3-cross-encoder` rerank（`Qwen/Qwen3-Reranker-4B`）：可由独立部署的可选
  worker 通过 `local_files_only=True` 与官方 raw-logit CrossEncoder 算法加载。
  `pp-compute-node` 控制容器本身不内置 PyTorch、Triton、CUDA library 或模型权重。

Model weight 必须已经位于操作者选择的本地缓存，或由安装向导显式下载；节点启动时
不会静默拉取或替换模型。

### 一键构建与就绪证据

[`scripts/build_compute_node.sh`](../../scripts/build_compute_node.sh)（POSIX）
与 [`scripts/build_compute_node.ps1`](../../scripts/build_compute_node.ps1)
（Windows）会生成此 `.env`（Ollama digest 自动派生），执行不可变本地构建、启动
Compose，然后运行 [`scripts/pp_node_smoke.py`](../../scripts/pp_node_smoke.py)。
不可变构建完成后，脚本会把构建期间解析出的容器身份（基础镜像引用/摘要、源码
revision、包版本、构建/recipe 策略摘要）补齐到 `.env`，把构建出的镜像别名到
compose 镜像名，并以 `--no-build` 启动 Compose，确保已校验镜像不会被重复构建。
烟测会校验 `/health`、`/v1/identity`、embedding 维度与 L2 归一化、有界 rerank 批次，
并记录每个 endpoint 的中位延迟。它写出烟测报告
（`plastic-promise/local-inference-node-smoke/v1`）以及 doctor 可读的
`runtime-status.json`（`plastic-promise/local-inference-runtime-status/v1`，键为
`schema_version`、`running`、`node_healthy`），因此部署 `doctor --runtime-status`
可以直接消费就绪证据。部署控制器在初始部署阶段以 `plastic-promise-deploy build-node`
暴露同一流程。
烟测只把受 ACL 保护的 compose 环境中的私有授权作为这些探针的 HTTP header；该授权不会
被序列化，structured-JSON 云 credential 也会被忽略，不会留存在烟测配置中。

Windows 上的
[`preflight_windows_node_host.ps1`](../../scripts/preflight_windows_node_host.ps1)
是当前源码提供的宿主恢复入口，同时支持 Docker Desktop 与 WSL2 原生 daemon。它会
定位并可显式迁移所选发行版 VHDX，更新 `.wslconfig` 中受管资源键，在
`/etc/wsl.conf` 配置 systemd，启用 Docker service，从 WSL 内验证联网，并在直连不可用
时为 WSL shell、Docker daemon 与 BuildKit 持久化经过验证的代理。正常原生路径直接
调用 `wsl.exe -d <distro> -e docker`；宿主全局 `socat` context 只允许显式选择。JSON
报告采用 fail-closed 合同，只有 `ready=true` 才允许持久化引导继续。

Windows 构建会在 compose 前检查 Python package toolchain。模型 worker 的依赖由其
独立安装流程提供并验证，不再修补进 compute 控制容器；在 `-NoStart` 返回前，镜像
已经别名到所选 CPU/CUDA control image。WSL 原生 compose env 使用 `/mnt/*` 模型路径，
Windows 宿主操作仍保留原生盘符路径。

## Source recipe / 目标 Docker 和 WSL2 boundary（PR 3）

仓库中的 [`Dockerfile`](../../deploy/local-inference-node/Dockerfile)、兼容
[`compose.yaml`](../../deploy/local-inference-node/compose.yaml)、
[`compose.cpu.yaml`](../../deploy/local-inference-node/compose.cpu.yaml) 和
[`compose.cuda.yaml`](../../deploy/local-inference-node/compose.cuda.yaml) 是 PR 3 artifact
matrix 的 recipe input。其存在既不证明 build 已完成，也不证明 local runtime 可用。

build policy 要求 non-root、read-only-rootfs descriptor 与 loopback listener scope。它只
允许逻辑 `model-catalog`（read-only）、有界 `node-runtime`（read-write）和 `node-tmp`
（tmpfs）mount。它禁止 SQLite、LanceDB、Docker socket、credential、private key、写入
layer 的 model weight、任意 shell/tool access 与 canonical authority。CPU/CUDA 保持在相同
`embedding/v1` / `rerank/v1` contract 之后；CUDA 限于其支持的 platform policy。

兼容 adapter 合同与可选 JIT worker 集成要求有界的 `node-tmp` 保持可执行；
`node-runtime` 仍保持不可执行。CUDA library 与模型执行仍由操作者管理的 llama.cpp
worker 负责。

Buildx 获得任何 argument 前，builder 会通过
[`validate_container_artifact_policy.py`](../../scripts/validate_container_artifact_policy.py)
与 [`resolve_container_artifact_identity.py`](../../scripts/resolve_container_artifact_identity.py)
从版本化 [`oci-base-images.json`](../../deploy/oci-base-images.json) catalog 推导 CPU 或 CUDA
identity。resolver 提供 immutable base reference/digest、source revision、package version、
build-policy digest、recipe-policy digest 与 expected label；不得以 local tag 或自行选择的
base image 替换它。
两个 control-node 变体都刻意使用精简 Python 基础镜像。CUDA 变体的区别是 GPU 可见性、
资源遥测与路由策略；CUDA library 与模型执行由操作者管理的 llama.cpp worker 负责，
避免在不执行 kernel 的控制容器中重复携带 CUDA/cuDNN 大层。

Windows/WSL2 local builder 会校验构建后 image 的 source revision、base-image
reference/digest、build-policy digest 与 recipe-policy digest label。该检查只确认 local image
的 plan-bound metadata，不是对 SBOM/provenance attestation 的检查、signature verification、
image publication、deployment authorization，亦不能证明 node 正在运行。

PR 3 不在本地 activation Docker 或 Compose、不分配 runtime GPU、不绑定 listener、不写入
`runtime-assets/`、不联系 node，也不创建 tunnel。其受保护 PR verification 可以对 CPU/CUDA
recipe 执行 **verify-only**、不 push 的 OCI layout build，并由 Buildx 生成 SBOM 与
provenance attestation layer。其
[`verify_oci_artifact_evidence.py`](../../scripts/verify_oci_artifact_evidence.py) 步骤校验
OCI descriptor hash、resolved 的 revision/base/policy/recipe-policy label，并确认两种
attestation layer 都将所选 platform image digest 写为 subject。verifier 不 load image、不联系
registry、不验证 signer 或 certificate、不 publish、不 deploy，也不作出 production trust
decision；本页同样不声称任何 verification job 已运行。PR 4 可以检查资源并生成经审核、
不执行 mutation 的 activation plan，但不能真正激活或注册节点。实测 preflight enforcement、
no-pull activation、enrollment、restricted tunnel configuration 与 runtime service ownership
属于 PR 5；跨平台 installer 与 release evidence 属于 PR 6。runtime 工作获得授权时，tunnel
account 必须没有 shell、sudo、SFTP、agent forwarding 或 public forwarding，且 server 侧只能
绑定 loopback。

## 目标 cache 与 capacity contract（PR 5）

cache manifest 只追踪 Plastic Promise-managed model-cache artifact。在本地时间
**04:30**，提供的 host-systemd timer 将根据 manifest 与 supervisor status file 运行
只读 `plastic-promise-local-inference-cache-plan`。仅 node 健康且没有 model download/
index rebuild 时，才会计划清理；必须保留 active 与 verified rollback revision，只有
未引用且至少空闲 24 小时的 artifact 才是 candidate。planner 输出 JSON，永不删除；PR 5
installer 才拥有显式、单独授权的 apply path。

未来任何 pull、build 或 image unpack 前，必须以实测 resource budget 和实际 local
container/model-store path 运行 plan-bound preflight。它按 physical filesystem 对这些路径
与 canonical state 分组；只要任一 selected volume 预计低于 `max(20%, 10 GiB)` 可用
空间，就拒绝整个 install。node activation 是 no-pull 且 model weight 只读，不能绕过
resource gate。

## 参考

- [Local heterogeneous inference node contract（英文）](local-inference-node.md)
- [部署档案与端点 Manifest 合同](profiles.zh-CN.md)
- [Deployment profiles and endpoint manifest contracts（英文）](profiles.md)
