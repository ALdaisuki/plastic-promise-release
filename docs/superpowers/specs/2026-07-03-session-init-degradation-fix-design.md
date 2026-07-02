# Session-Init Degradation Fix — Design Spec

> 日期: 2026-07-03 | 状态: approved | 范围: Fix 1 + Fix 2 + Fix 3

## 背景

`session-init` 返回三个降级：(1) DomainManager `_dm_ok=False`，(2) LanceDB `lancedb_unavailable`，(3) SCARF 五维全部 0.65 默认值。根因是并发竞态条件 + Ollama 不可用导致 FallbackEmbedder 零向量。详见 [session-init 分析报告]。

## 设计目标

1. 消除并发竞态 — `domain`/`memory_gc`/`system` atom 在 heavy init 完成前访问未初始化组件
2. LanceDB 优雅降级 — 当 embedder 为零向量时仍完成初始化，通过 `_vectors_disabled` 标志区分"不可用"和"向量不可用"
3. SCARF 三层回退 — 关键词 → 语义向量 → 文本启发式，每层降级时显式标注原因
4. 统一降级通知 — 新增 `component_health` 字段，一次查看所有组件健康状态

## Fix 1: 并发守卫

### 修改文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/mcp/tools/domain.py` | `handle_domain()` 入口加 `engine._ensure_heavy_init()` |
| `plastic_promise/mcp/tools/memory.py` | `handle_memory_gc()` 入口加 `engine._ensure_heavy_init()` |
| `plastic_promise/mcp/tools/management.py` | `handle_system()` 入口加 `engine._ensure_heavy_init()` |

### 机制

`_ensure_heavy_init()` 使用双检锁（double-checked locking），首次调用执行 DomainManager + LanceDBStore + embedder 初始化，后续调用检测 `_heavy_init_done=True` 直接返回。在 `concurrent=True` 模式下，第一个到达的 atom 触发初始化，其余 atom 在锁上短暂等待后获得已完成的结果。

### 降级路径

如果 heavy init 真正失败（非竞态导致），`_dm_ok` 保持 `False`，`_ldb` 保持 `None`。此时 `domain` 返回 `{"error": "DomainManager not available (_dm_ok=False)"}`，`memory_gc` 返回 `{"merge": {"error": "lancedb_unavailable"}}`。session-init 的 `degrade_map` 中这两个 atom 都是 `"skip"`，不影响整体成功。

## Fix 2: LanceDB 优雅降级

### 修改文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/core/lancedb_store.py` | `__init__` 检测 FallbackEmbedder → `_vectors_disabled=True`；`search()`/`insert()`/`check_duplicate()` 加守卫 |
| `plastic_promise/memory/soul_memory.py` | `merge_similar()` 检测 `_vectors_disabled` → 返回 `"vectors_disabled"` |
| `plastic_promise/skills/session_lifecycle.py` | 编译 `component_health["lancedb"]` |

### LanceDBStore 改动

```python
class LanceDBStore:
    def __init__(self, db_path, embedder):
        self._vectors_disabled = _is_fallback_embedder(embedder)
        # 其余初始化不变：建表、FTS 索引正常建立

    def search(self, vector, k=10, **filters):
        if self._vectors_disabled:
            return []
        # ... 现有逻辑

    def insert(self, memory_id, vector, text, tier, category, scope=""):
        if self._vectors_disabled:
            logger.debug(f"LanceDBStore.insert({memory_id}): vectors disabled, skipping write")
            return
        # ... 现有逻辑

    def check_duplicate(self, vector, threshold=0.85):
        if self._vectors_disabled:
            return None
        # ... 现有逻辑
```

`_is_fallback_embedder()` 检测 embedder 类型避免循环导入：检查 `getattr(emb, 'model_name', '') == 'fallback-zero'` 或 `isinstance` 延迟导入。

### 设计决策：insert() 早退而非写零向量

- LanceDB schema 要求 `vector` 列为 `list[float]`，不可为 None
- 写零向量会污染索引，后续 ANN 搜索返回无意义结果
- 写占位向量 + search() 过滤方案过于复杂
- 早退是诚实的降级：`"degraded_vectors"` 语义 = LanceDB 层全部不可用
- 上层 `context_supply._text_retrieval()` 走 SQLite 文本匹配，不依赖 LanceDB FTS

### merge_similar() 区分

| ldb 状态 | 返回值 | component_health |
|----------|--------|-----------------|
| `ldb is None` | `error: "lancedb_unavailable"` | `"unavailable"` |
| `_vectors_disabled` | `candidates_found: 0, error: "vectors_disabled"` | `"degraded_vectors"` |
| 正常 | 正常合并结果 | `"healthy"` |

## Fix 3: SCARF 三层回退

### 修改文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/reflection/soul_scarf.py` | 新增 `_text_heuristic_signal()`；修改 `_compute_dimension_score()` 的回退分支 |
| `plastic_promise/skills/session_lifecycle.py` | 编译 `component_health["scarf"]` + `component_health["embedder"]` |

### 三层回退逻辑

```
_compute_dimension_score(context, dim_key)
  │
  ├─ Layer 1: 关键词匹配（中英文 ~40 词/维度）
  │   命中 → 强信号 ±0.06~0.08/词
  │   未命中 ↓
  │
  ├─ Layer 2: 语义向量（Ollama mxbai-embed-large）
  │   cos(ctx, pos_anchor) - cos(ctx, neg_anchor) → ±0.15
  │   零向量 → 0.0 ↓
  │
  └─ Layer 3: 文本启发式（新增）
      文本长度、问号、自主性词汇、约定词汇 → ±0.05
      assessment 显式标注："嵌入服务不可用，使用文本启发式评估——可信度降低"
```

### 文本启发式信号

```python
def _text_heuristic_signal(context: str, dim_key: str) -> float:
    signals = {
        "Status":       0.01 if len(context) > 50 else 0.0,
        "Certainty":   -0.02 if any(c in context for c in "?？") else 0.0,
        "Autonomy":     0.02 if any(w in context for w in ["优化","改进","自主","选择"]) else 0.0,
        "Relatedness":  0.03 if any(w in context for w in ["原则","约定","流程","规范"]) else 0.0,
        "Fairness":     0.0,  # 无法从纯文本推断
    }
    return signals.get(dim_key, 0.0)
```

信号幅度 ±0.05，小于关键词（±0.06~0.08）和语义（±0.15），确保只在不具备更强信号时起微调作用。

### FallbackEmbedder 检测

SCARF 不依赖 ContextEngine 的 embedder，独立调用 `get_embedder()`。检测方法：`isinstance(emb, FallbackEmbedder)` 或检查 `emb.model_name == 'fallback-zero'`。当检测到时，在 assessment 中显式标注降级原因。

## component_health 字段

### 位置

`session-init` 响应 `data.component_health`，由 `_session_init_handler` 编译。

### Schema

```json
{
  "component_health": {
    "domain_manager": "healthy" | "degraded_no_init",
    "lancedb": "healthy" | "degraded_vectors" | "unavailable",
    "embedder": "healthy" | "fallback_zero",
    "scarf": "healthy" | "degraded_text_only"
  }
}
```

| 字段 | healthy 条件 | 降级值 | 降级触发条件 |
|------|-------------|--------|-------------|
| `domain_manager` | `_dm_ok=True` | `degraded_no_init` | DomainManager 初始化抛异常 |
| `lancedb` | `_ldb` 存在且 `_vectors_disabled=False` | `degraded_vectors` / `unavailable` | FallbackEmbedder / init 失败 |
| `embedder` | Ollama/Local/OpenAI 任一可用 | `fallback_zero` | 全部不可用 → FallbackEmbedder |
| `scarf` | 关键词或语义信号可用 | `degraded_text_only` | FallbackEmbedder 激活 |

### 编译逻辑（session_lifecycle.py）

在 `_session_init_handler` 中，atom 结果收集完成后：

```python
def _compile_component_health(ctx):
    health = {}

    # domain_manager
    health["domain_manager"] = "healthy" if getattr(ctx, "_dm_ok", False) else "degraded_no_init"

    # lancedb
    ldb = getattr(ctx, "_ldb", None)
    if ldb is None:
        health["lancedb"] = "unavailable"
    elif getattr(ldb, "_vectors_disabled", False):
        health["lancedb"] = "degraded_vectors"
    else:
        health["lancedb"] = "healthy"

    # embedder
    try:
        from plastic_promise.core.embedder import get_embedder
        emb = get_embedder()
        health["embedder"] = "fallback_zero" if getattr(emb, "model_name", "") == "fallback-zero" else "healthy"
    except Exception:
        health["embedder"] = "fallback_zero"

    # scarf
    health["scarf"] = "degraded_text_only" if health["embedder"] == "fallback_zero" else "healthy"

    return health
```

## 影响范围

| 维度 | 评估 |
|------|------|
| 修改文件数 | 6 个（domain.py, memory.py, management.py, lancedb_store.py, soul_scarf.py, session_lifecycle.py） |
| 新增代码 | ~100 行 |
| 破坏性变更 | 无 — 所有改动为增量守卫 |
| 并发模型 | 保持 concurrent=True |
| 测试影响 | session_lifecycle 测试需更新以覆盖 component_health |
| 回滚风险 | 极低 — 可逐 Fix 回滚 |

## 验证标准

1. Ollama 离线时执行 `session-init` → `component_health.embedder = "fallback_zero"`, `lancedb = "degraded_vectors"`, `scarf = "degraded_text_only"`
2. Ollama 在线时执行 `session-init` → 全部 `"healthy"`
3. 竞态条件不再触发：重复执行 10 次 `session-init`，`domain_manager` 始终 `"healthy"`（假定 DomainManager 初始化无真正错误）
4. SCARF assessment 包含 "嵌入服务不可用" 或 "文本启发式" 标注
5. `merge_similar()` 在 `_vectors_disabled` 时返回 `"vectors_disabled"` 而非 `"lancedb_unavailable"`
