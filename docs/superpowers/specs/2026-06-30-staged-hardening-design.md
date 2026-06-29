# Staged Hardening — 三阶段基础设施加固设计

> 状态: 已审查 ✅ | 日期: 2026-06-30 | 基于审计建议 + 前沿实例调研 + 审查修正

## 一、背景

Plastic Promise 已交付 33 个 MCP 工具、8 维审计、完整记忆生命周期、Skill Tracking 和 Auto Context Inject。当前瓶颈不在功能数量，而在于：

1. **不可观测** — 没有分布式追踪，问题定位靠 grep 日志
2. **无集成验证** — 154 个单元测试通过，但 Claude+Pi+N.E.K.O 三方协作从未端到端跑过
3. **无性能基线** — 不知道 LanceDB 检索延迟、GC 开销、SQLite 吞吐
4. **无 RAG 质量量化** — 审计第 5 维 "记忆供给质量" 是手工评分，缺少自动化指标

## 二、总体方案：三阶段分步交付

```
S1: OpenTelemetry (1-2天)  →  S2: 集成测试 (1-2天)  →  S3: 性能+RAGAS (1-2天)
   可追踪                      可验证                     可度量
```

每阶段独立交付、独立验收，不互相阻塞。

---

## 三、Stage 1: OpenTelemetry 可观测性

### 3.1 目标

为 Plastic Promise 的关键路径添加分布式追踪，使每次 MCP 调用可追溯、可度量。

### 3.2 埋点范围

**8 个核心 MCP 工具（手动 Span）：**

| 工具 | Span 名 | 关键属性 |
|------|---------|---------|
| `memory_recall` | `plastic.memory.recall` | query, max_results, latency_ms, hit_count |
| `memory_store` | `plastic.memory.store` | memory_type, tier, quality_score, dup_detected |
| `context_supply` | `plastic.context.supply` | task_type, layers_returned, total_items |
| `skill_session_start` | `plastic.skill.start` | skill_name, parent_id, branch |
| `skill_session_complete` | `plastic.skill.complete` | skill_name, outcome, duration_ms |
| `audit_run` | `plastic.audit.run` | scope, dimensions_count, overall_score |
| `principle_activate` | `plastic.principle.activate` | task_type, activated_count |
| `issue_transition` | `plastic.issue.transition` | issue_id, from_state, to_state |

**内部关键路径手动 Span（不受外部库兼容性影响）：**

| 模块 | 方法 | Span 名 | 关键属性 |
|------|------|---------|---------|
| `OllamaEmbedder` | `embed()` | `plastic.embedding.encode` | batch_size, dim, latency_ms, model |
| `OllamaEmbedder` | `embed_batch()` | `plastic.embedding.batch_encode` | batch_count, total_tokens, latency_ms |
| `LanceDBStore` | `search()` | `plastic.lancedb.ann_search` | k, scope, tier, latency_ms, hit_count |
| `LanceDBStore` | `search_similar()` | `plastic.lancedb.similar_search` | k, threshold, latency_ms, hit_count |
| `LanceDBStore` | `fts_search()` | `plastic.lancedb.fts_search` | query_len, latency_ms, hit_count |
| `LanceDBStore` | `upsert()` | `plastic.lancedb.upsert` | memory_id, latency_ms |
| `LanceDBStore` | `delete()` | `plastic.lancedb.delete` | memory_id, latency_ms |
| `_SQLiteStorage` | `store_memory()` | `plastic.sqlite.store` | memory_type, tier, latency_ms |
| `_SQLiteStorage` | `get_memory()` | `plastic.sqlite.get` | memory_id, latency_ms |

**HTTP 调用自动埋点（opentelemetry-instrumentation-requests）：**

安装 `opentelemetry-instrumentation-requests` 后，所有通过 `requests` 库发出的 HTTP 调用（包括 Ollama API）自动获得 Span。这样 embedding 调用的网络层零代码获得追踪。

> ⚠️ **不用 genai-otel-instrument**：该项目主要支持 OpenAI SDK 和 LangChain，Plastic Promise 使用 `requests` 直接调用 Ollama API，不会被自动捕获。改为手动 Span + requests 自动埋点覆盖全部路径。

### 3.3 技术选型

```
OpenTelemetry SDK (Python)                 ← 手动 Span (核心路径)
  + opentelemetry-instrumentation-requests  ← HTTP 自动埋点 (Ollama API)
  + OTLP Console Exporter                  ← 本地开发（默认）
  + OTLP HTTP Exporter                     ← 生产环境（可选）
```

依赖列表：
- `opentelemetry-api >= 1.28.0`
- `opentelemetry-sdk >= 1.28.0`
- `opentelemetry-instrumentation-requests >= 0.49b0`
- `opentelemetry-exporter-otlp` (可选，仅生产环境)

### 3.4 集成方式

```python
# plastic_promise/core/tracing.py — 新增模块
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

_tracer: trace.Tracer | None = None
_tracing_enabled: bool = True

def init_tracing(service_name="plastic-promise", exporter="console", enabled=True):
    """初始化 OpenTelemetry，返回 Tracer。

    exporter="console" 时输出到 stdout，适合本地开发。
    exporter="otlp" 时输出到 OTLP Collector。
    enabled=False 时静默跳过所有埋点（用于测试/CI）。
    """
    ...

def get_tracer() -> trace.Tracer | None:
    """获取当前 Tracer 实例。追踪未启用时返回 None。"""
    ...

def is_tracing_enabled() -> bool:
    """检查追踪是否已启用。调用方可用此开关跳过 Span 创建。"""
    ...
```

**开关控制：**

- 环境变量 `PP_TRACING_ENABLED=0` 禁用追踪（测试/CI 环境）
- `init_tracing(enabled=False)` 编程式禁用
- 禁用后 `get_tracer()` 返回 None，所有 Span 创建静默跳过

### 3.5 仪表盘增强

在现有 `/dashboard` 中增加一个 **可观测性卡片**，展示：
- 最近 24h MCP 调用计数（按工具分）
- P50/P95 延迟
- 错误率

数据来源：从 OTel Span 导出的内存聚合（不依赖外部存储）。

### 3.6 验收标准

- [ ] 8 个 MCP 工具调用 + 内部关键路径 (OllamaEmbedder, LanceDBStore, _SQLiteStorage) 在控制台输出 OTel Span
- [ ] `opentelemetry-instrumentation-requests` 自动捕获 Ollama HTTP 调用
- [ ] `PP_TRACING_ENABLED=0` 禁用追踪后无 Span 输出
- [ ] `/dashboard` 显示调用统计卡片
- [ ] 新增 `plastic_promise/core/tracing.py` 模块（含 `is_tracing_enabled()` 开关）
- [ ] 新增 `tests/test_tracing.py` (3+ 用例：启用/禁用/span 属性验证)
- [ ] CLAUDE.md 更新（可观测性说明）

---

## 四、Stage 2: 端到端集成测试

### 4.1 目标

验证 Claude PM + Pi Agent + N.E.K.O 三方协作的完整链路。

### 4.2 测试场景

**场景 1: Issue 完整生命周期**
```
memory_store(task:pending) → Daemon 检测 → Pi Builder 认领
  → Pi 执行 → memory_store(task:done) → Reviewer 审查
  → Claude 验收 → defense(adjust) → task:reviewed
```
验证点：标签状态机每步转换正确、记忆在组件间共享、信任分更新。

**场景 2: 修复循环**
```
Reviewer 打回 → task:rejected → Fixer 认领 → 修复 → 重新审查 → 通过
```
验证点：rejected→accepted→done→review→reviewed 完整链路。

**场景 3: 超时恢复**
```
task:active 超过 5 分钟 → 自动重置为 task:pending → 重新分配
```
验证点：超时检测 + 状态恢复。

**场景 4: 跨组件上下文供给**
```
Claude 发任务 → context_supply 自动注入 → Pi 收到完整上下文
  → Pi 执行 → 结果写入记忆池 → Claude context_supply 可召回
```
验证点：上下文在 Agent 间正确传递。

**场景 5: Skill 追踪端到端**
```
brainstorming.start → writing-plans.start → brainstorming.complete
  → executing-plans.start → verification-before-completion.start
  → executing-plans.complete → verification-before-completion.complete
  → skill_session_trace(session_scope="branch")
```
验证点：调用链完整、chain_valid=true、无 orphan_active。

### 4.3 测试基础设施

```
tests/
  integration/
    conftest.py              ← 共享 fixtures (session-level MCP server + 初始化 SQLite/LanceDB)
    test_issue_lifecycle.py  ← 场景 1+2+3
    test_context_flow.py     ← 场景 4
    test_skill_tracking_e2e.py ← 场景 5
```

**Fixture 策略：**

- **Session-level MCP server**：`conftest.py` 中使用 `@pytest.fixture(scope="session")` 启动一次 MCP server，所有测试共享，避免每个测试 2-5 秒启动开销
- **动态端口**：使用 `socket.bind(('localhost', 0))` 自动分配空闲端口，避免 9020 端口冲突。端口号通过 fixture 传递给测试
- **进程清理**：使用 `pytest.fixture` 的 `yield` + `atexit` 双重保障，确保测试结束后 MCP server 进程被 SIGTERM → SIGKILL 正确终止
- **独立数据库**：每个测试 session 使用 `tempfile.TemporaryDirectory` 创建独立 SQLite + LanceDB 目录，确保测试间隔离
- **直接 API 调用优先**：尽可能直接调用 Python API（绕过子进程 + SSE），仅在测试 MCP 协议层面时使用 subprocess

### 4.5 验收标准

- [ ] 5 个集成测试场景全部通过
- [ ] 每个场景的标签状态机转换正确
- [ ] Session-level fixture 正确工作（server 只启动一次）
- [ ] 动态端口分配无冲突
- [ ] 测试可在 Windows 上运行（不依赖 Unix socket）
- [ ] `tests/integration/` 目录建立

---

## 五、Stage 3: 性能基准 + RAGAS 指标

### 5.1 性能基准

**基准场景：**

| 基准 | 测量对象 | 目标阈值 |
|------|---------|---------|
| LanceDB ANN 检索延迟 | 1000/10000 向量库中 top-20 检索 | P95 < 50ms (1000), P95 < 200ms (10000) |
| LanceDB FTS 检索延迟 | 全文搜索 10000 条记录 | P95 < 100ms |
| SQLite 写入吞吐 | 批量 memory_store 100 条 | > 50 writes/s |
| GC collect 开销 | 1000 条记忆的 mark_decaying + merge_similar | < 5s total |
| memory_recall 端到端延迟 | 混合检索 + 上下文组装 | P95 < 500ms |
| memory_store 端到端延迟 | embedding + SQLite + LanceDB 双写全路径 | P95 < 800ms |
| ContextEngine.supply 对比 | 向量检索 vs 纯文本检索延迟 | 向量检索不超过文本 3x |

**标准数据集定义**（在 `conftest.py` 中固定）：

- 向量维度: 1024（`PP_EMBEDDING_DIM` 环境变量一致）
- 数据规模: `SMALL=1000`, `LARGE=10000`
- 预热: 每个基准运行前执行 3 次预热查询（排除冷启动偏差）
- 轮次: 每个基准至少 10 轮取 P50/P95

**实现方式：**
```
tests/
  benchmarks/
    conftest.py              ← 共享 fixtures (填充测试数据, 固定向量维度/规模)
    test_bench_lancedb.py    ← LanceDB ANN + FTS 检索基准
    test_bench_sqlite.py     ← SQLite 写入基准
    test_bench_gc.py         ← GC 基准 (mark_decaying + merge_similar)
    test_bench_recall.py     ← recall 端到端基准
    test_bench_store.py      ← memory_store 端到端基准
    test_bench_context.py    ← ContextEngine.supply 向量 vs 文本对比
```

使用 `pytest-benchmark` 插件，结果输出到 JSON 供 CI 历史对比。

### 5.2 RAGAS 评估指标

在审计中新增两个自动化维度：

**维度 9: Context Precision（上下文精度）**
```
每次 memory_recall 后自动计算：
  precision = |相关记忆| / |召回记忆|
```
通过 LanceDB 向量相似度 > 0.7 自动判定相关性（无需人工标注）。

**维度 10: Context Recall（上下文召回率）**
```
每次 memory_recall 后自动计算：
  recall = |召回的应召回记忆| / |所有应召回记忆|
```

**expected_ids 来源策略（三级降级）：**

| 优先级 | 来源 | 适用场景 | 可靠性 |
|--------|------|---------|--------|
| 1 | 测试数据集（人工标注） | 基准测试/CI | 高 |
| 2 | `entity_ids` 标签匹配 | 生产环境，有 entity 关联的记忆 | 中 |
| 3 | 跳过（返回 null） | 无法确定 ground truth | — |

> ⚠️ **关键设计决策**：在无法确定 expected_ids 时，`compute_context_recall()` 返回 `null` 而非 `0.0`。审计报告中 `null` 表示"无数据"，`0.0` 表示"召回率为零"——两者含义完全不同，不可混淆。

**实现方式：**
```python
# plastic_promise/core/ragas_metrics.py — 新增模块
def compute_context_precision(retrieved: list, query_vector, threshold=0.7) -> float
    """基于向量相似度自动计算精度。始终有值（不需要 ground truth）。"""

def compute_context_recall(retrieved: list, expected_ids: set | None) -> float | None
    """基于已知标签匹配计算召回率。expected_ids 为 None 时返回 None。"""

def get_expected_ids(memory_ids: list, entity_graph: EntityGraph) -> set | None
    """从 entity_ids 标签中提取 ground truth。无标签时返回 None。"""
```

### 5.3 审计集成

在 `audit_run` 中增加 `include_ragas=True` 选项（默认 true，允许调用方显式 `include_ragas=False` 跳过），在 AuditReport.dimensions 中增加两个维度：

```python
AUDIT_DIMENSIONS["context_precision"] = {
    "name": "上下文精度 (RAGAS)",
    "weight": 0.05,
    "description": "每次检索返回的相关记忆占比，自动计算",
}
AUDIT_DIMENSIONS["context_recall"] = {
    "name": "上下文召回率 (RAGAS)",
    "weight": 0.05,
    "description": "应被检索到的记忆实际被检索到的比例",
}
```

**权重调整（10 维总和 = 1.00）：**

| 维度 | 原权重 | 新权重 | 变化 |
|------|--------|--------|------|
| simplicity | 0.13 | 0.117 | -0.013 |
| transparency | 0.13 | 0.117 | -0.013 |
| audit_closure | 0.13 | 0.117 | -0.013 |
| principle_activation | 0.13 | 0.117 | -0.013 |
| memory_supply | 0.13 | 0.117 | -0.013 |
| constraint_compliance | 0.13 | 0.117 | -0.013 |
| feedback_closure | 0.09 | 0.081 | -0.009 |
| skill_trace | 0.10 | 0.090 | -0.010 |
| context_precision | — | 0.050 | 新增 |
| context_recall | — | 0.050 | 新增 |
| **总和** | **0.97** | **1.000** | ✅ 对齐 |

> 注意：原 8 维总和为 0.97（非严格 1.0），此次调整同时修复为精确 1.000。

### 5.4 验收标准

- [ ] 6 个性能基准可运行，结果输出到 JSON（pytest-benchmark 格式）
- [ ] `plastic_promise/core/ragas_metrics.py` 模块完成（含三级降级策略）
- [ ] Context Precision 自动计算（无需 ground truth）
- [ ] Context Recall 在 expected_ids 缺失时返回 null（非 0.0）
- [ ] audit_run 输出 10 维评分（含 RAGAS 2 维，include_ragas 可关闭）
- [ ] `/dashboard` 显示 RAGAS 趋势图（precision/recall 时间序列）
- [ ] `constants.py` 中 AUDIT_DIMENSIONS 权重总和修正为 1.000
- [ ] 新增 `tests/test_ragas_metrics.py` (3+ 用例：precision/recall/null-skip)
- [ ] 新增 `tests/benchmarks/` 目录

---

## 六、文件变更清单

```
新增:
  plastic_promise/core/tracing.py              ← S1: OTel 初始化 + Tracer
  plastic_promise/core/ragas_metrics.py        ← S3: RAGAS 指标计算
  tests/test_tracing.py                        ← S1: OTel 测试
  tests/test_ragas_metrics.py                  ← S3: RAGAS 测试
  tests/integration/conftest.py                ← S2: 集成测试 fixtures (session-level)
  tests/integration/test_issue_lifecycle.py    ← S2: Issue 生命周期
  tests/integration/test_context_flow.py       ← S2: 上下文流动
  tests/integration/test_skill_tracking_e2e.py ← S2: Skill 追踪
  tests/benchmarks/conftest.py                 ← S3: 基准 fixtures (标准数据集)
  tests/benchmarks/test_bench_lancedb.py       ← S3: LanceDB ANN + FTS 基准
  tests/benchmarks/test_bench_sqlite.py        ← S3: SQLite 写入基准
  tests/benchmarks/test_bench_gc.py            ← S3: GC 基准
  tests/benchmarks/test_bench_recall.py        ← S3: Recall 端到端基准
  tests/benchmarks/test_bench_store.py         ← S3: memory_store 端到端基准
  tests/benchmarks/test_bench_context.py       ← S3: ContextEngine 向量 vs 文本对比

修改:
  plastic_promise/core/tracing.py              ← S1: (新增文件, 见上)
  plastic_promise/core/embedder.py             ← S1: OllamaEmbedder 加手动 Span
  plastic_promise/core/lancedb_store.py        ← S1: LanceDBStore 加手动 Span
  plastic_promise/memory/soul_memory.py        ← S1: _SQLiteStorage 加手动 Span
  plastic_promise/mcp/tools/memory.py          ← S1: MCP 工具加 OTel Span
  plastic_promise/mcp/tools/skill_tracking.py  ← S1: MCP 工具加 OTel Span
  plastic_promise/mcp/tools/audit_defense.py   ← S1+S3: OTel Span + RAGAS 维度
  plastic_promise/mcp/tools/principles.py      ← S1: MCP 工具加 OTel Span
  plastic_promise/mcp/tools/context.py         ← S1: MCP 工具加 OTel Span
  plastic_promise/mcp/tools/management.py      ← S1: MCP 工具加 OTel Span
  plastic_promise/core/constants.py            ← S3: 审计维度 9+10 + 权重修正
  plastic_promise/defense/soul_audit.py        ← S3: 集成 RAGAS 指标 + include_ragas 开关
  plastic_promise/mcp/server.py                ← S1: 初始化 OTel + requests instrument
  GOAL.md                                      ← 更新状态
  CLAUDE.md                                    ← 更新可观测性说明
```

## 七、不做什么（YAGNI）

- 不引入外部 OTel Collector（S1 用 Console Exporter 即可）
- 不实现完整的 RAGAS 库集成（只取 Context Precision/Recall 两个最核心指标）
- 不做 GPU/CO2 指标（不适用）
- 不做 Temporal 迁移（P2 远期）
- 不做 AutoGen/CrewAI 集成（P2 远期）
- 不做 Recall/Archival 记忆分区（等 Core Memory 独立设计）
