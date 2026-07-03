# Recall Pipeline Upgrade — Phase 2 执行计划

**日期**: 2026-07-03
**状态**: plan — pending review
**依赖**: [recall-quality-diagnosis-design](2026-07-03-recall-quality-diagnosis-design.md) Phase 1 完成
**参考项目**: [memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro) (MIT), [CortexReach 分析](engineering-patterns/2026-07-03-cortexreach-memory-lancedb-pro.md)

---

## 1. 变更范围

Phase 2 不改 LanceDB schema、不加 MCP 工具。默认升级 `context_engine.py` 的 Python 检索管道和 `lancedb_store.py` 的 FTS 接入；`memory_recall` 增加可选 `debug` 参数用于显式诊断。

### Rust parity boundary

当前 `ContextEngine.supply()` 只有在 `PP_PREFER_RUST_SUPPLY=1` 且 Rust engine healthy 时才走 `_supply_rust()`；默认仍走 Python supply。Phase 2 的 FTS fusion、source filter、hard threshold、debug explain 先落在 Python supply 路径。`debug=True` 会强制回退 Python 路径，因为 Rust `ContextPack` 还没有 `pipeline_stats` / `per_item_stats` 等 explain 字段。Rust parity 作为后续独立计划处理，不在本 Phase 混入，避免同时修改两套检索实现导致回归不可定位。

### 变更文件

| 文件 | 变更类型 | 描述 |
|------|---------|------|
| `plastic_promise/core/context_engine.py` | MODIFY | 4 处插入: FTS fusion, source filter, hard threshold, debug |
| `plastic_promise/core/lancedb_store.py` | MODIFY | 1 处: FTS accessor for supply |
| `plastic_promise/core/constants.py` | MODIFY | 新增 HARD_MIN_SCORE, SOURCE_INCLUDE/DOWNWEIGHT/EXCLUDE 配置 |
| `plastic_promise/mcp/tools/memory.py` | MODIFY | 1 处: `debug=True` 参数传递 |
| `plastic_promise/mcp/server.py` | MODIFY | `memory_recall` schema 暴露可选 `debug` 参数 |
| `plastic_promise/memory/soul_memory.py` | MODIFY | `MemoryRecord` 接受并序列化 `category`，匹配 pipeline store 参数 |
| `tests/test_recall_pipeline_upgrade.py` | NEW | 集成测试 |
| `tests/test_recall_quality_quick_fixes.py` | NEW | Phase 1 quick fix 回归测试 |

---

## 2. 组件设计

### 2.1 LanceDB FTS 接入

**What**: `supply()` 中 `_vector_retrieval()` 和 `_text_retrieval()` 并行运行的同时，接入 `lancedb_store.search_fts()` 作为第三检索通道。

**Why**: LanceDB FTS (`lancedb_store.py:150`) 使用原生 BM25 索引，当前 Python `_text_retrieval()` 是手工 BM25-like 实现。LanceDB FTS 天然支持 BM25 scoring 且受益于 FTS 索引结构。三个通道并行后融合，增大候选池覆盖。

**Interface**:
```python
# lancedb_store.py — 新增 accessor
def fts_search(self, query: str, k: int = 20) -> list[tuple[str, float, str, str, str]]:
    """Full-text search via LanceDB FTS index. Falls back to empty on failure."""
    return self.search_fts(query, k)

# context_engine.py — supply() 中新增
fts_results = self._fts_retrieval(query) if self._ldb else []

def _fts_retrieval(self, query: str) -> list[tuple]:
    """LanceDB FTS retrieval, fallback empty on failure."""
    try:
        return [
            (mid, score, text[:300], "fts")
            for mid, score, text, tier, scope in self._ldb.fts_search(query)
        ]
    except Exception:
        return []
```

**Insertion**: `context_engine.py` supply() loop，在 `_text_retrieval` 和 `_vector_retrieval` 之后。Fusion 使用三通道 weighted max: `fts_results` + `text_results` + `vector_results` → `_hybrid_fuse` 改为三输入。

**FTS-lexical preservation floor**: 如果某 item 在 FTS 结果中 score ≥ 0.85，在 rerank 阶段不能被降到 0.7 以下。

**Env**: `PP_FTS_FUSION=1` (默认 on), `PP_FTS_DISABLED=1` (紧急关闭)

### 2.2 Source/Type 过滤

**What**: 在 `_build_items` 中，对已知噪声 source 做降权或排除。

**Why**: 当前 recall 混合 user/project/code/daemon/skill-session/telemetry/PR-review 片段。默认业务 recall 不应包含 daemon audit 和 skill trace。

**Classification**:

| Source | 默认行为 | 降权系数 |
|--------|---------|---------|
| `user` | include | 1.0 |
| `system` | include | 1.0 |
| `claude_code` | include | 1.0 |
| `pi_builder` | include | 1.0 |
| `pi_fixer` | include | 1.0 |
| `pi_reviewer` | include | 1.0 |
| `superpowers` | downweight | 0.3 |
| `maintenance_daemon` | downweight | 0.3 |
| `skill_session` | downweight | 0.1 |
| `step_closure` | downweight | 0.3 |
| `auto_context_inject` | downweight | 0.3 |

`context_supply` 可通过 `include_sources=[]` 参数显式拉入非默认 source。

**Insertion**: `context_engine.py` `_build_items` loop，在 noise filter 之后、symbol boost 之前。

**Env**: `PP_SOURCE_FILTER=1` (默认 on), `PP_SOURCE_FILTER_STRICT=1` (严格模式: 非 include source 直接排除而非降权)

### 2.3 Hard Threshold 标准化

**What**: 在 `_build_items` 最后，新增硬阈值过滤：score < HARD_MIN_SCORE → 丢弃。

**Why**: 对标 memory-lancedb-pro 的 hard min score (0.35)。当前 soft check 仅在 layer assignment (core≥0.70, related≥0.40, divergent≥0.20)，但 related/divergent 仍可含极低质量项。

**Insertion**: `_build_items` loop，在 length normalization 之后、layer assignment 之前。

**默认值**:
```python
HARD_MIN_SCORE = 0.30  # 可覆盖: PP_HARD_MIN_SCORE=0.35
```

### 2.4 Recall Explain / Debug

**What**: `memory_recall` 新增 `debug=True` kwarg，返回每阶段计数和每条结果的 score breakdown。

**Why**: 当前 recall 质量和健康评分都是黑盒。对标 memory-lancedb-pro CLI `--debug` 和行为。

**Interface**:
```python
# memory_recall(debug=True) → 额外 debug 字段
{
  "core": [...],
  "related": [...],
  "divergent": [...],
  "pipeline": {
    "query": "原始查询",
    "expanded_query": "扩展后查询",
    "vector_count": 20,
    "bm25_count": 18,
    "fts_count": 15,
    "graph_count": 8,
    "fused_count": 30,
    "after_source_filter": 25,
    "after_noise_filter": 20,
    "after_hard_score_filter": 15,
    "after_mmr": 12,
    "after_rerank": 12,
    "core_count": 5,
    "related_count": 7,
    "divergent_count": 0,
    "rerank_status": "completed"
  },
  "per_item": [
    {
      "id": "...",
      "content": "(first 120 chars)",
      "vector_score": 0.72,
      "bm25_score": 0.85,
      "fts_score": null,
      "graph_score": null,
      "fused_score": 0.81,
      "worth": 0.86,
      "decay_multiplier": 0.95,
      "length_norm_factor": 0.88,
      "source_penalty": 1.0,
      "final_score": 0.77,
      "source": "user",
      "memory_type": "experience",
      "tier": "L1",
      "category": "fact"
    }
  ]
}
```

**Insertion**:
- `memory.py` `handle_memory_recall`: `debug = args.get("debug", False)`，传递给 `engine.supply(debug=debug)`
- `context_engine.py` `supply()`: 收集 pipeline 计数器，填充 `pack.pipeline_stats` 和 `pack.per_item_stats`

**Env**: 无特殊 env gate。debug 模式仅在显式请求时返回额外数据，不影响性能。

---

## 3. 测试计划

### 集成测试 (`tests/test_recall_pipeline_upgrade.py`)

```python
def test_fts_channel_contributes_to_recall():
    """FTS results appear in fused candidate pool."""
    ...

def test_exact_lexical_hit_survives_rerank():
    """Exact keyword match score >= 0.85 is not dropped below 0.7 by rerank."""
    ...

def test_source_filter_downweights_daemon_audit():
    """maintenance_daemon source memories ranked lower than equivalent-score user source."""
    ...

def test_hard_threshold_filters_weak_scores():
    """Scores below HARD_MIN_SCORE are excluded from all layers."""
    ...

def test_debug_mode_returns_pipeline_stats():
    """debug=True adds pipeline and per_item fields."""
    ...
```

### 回归测试

```bash
# 已有 test suite 全部通过
pytest -q tests/test_recall_quality_quick_fixes.py
pytest -q tests/test_skill_engine.py tests/test_skill_tracking.py
pytest -q tests/test_pipeline_quality.py

# 新增
pytest -q tests/test_recall_pipeline_upgrade.py
```

---

## 4. Rollback 策略

每个组件独立 env gate:

| 组件 | Env Gate | Emergency Off |
|------|---------|---------------|
| FTS fusion | `PP_FTS_FUSION=1` | `PP_FTS_DISABLED=1` |
| Source filter | `PP_SOURCE_FILTER=1` | `PP_SOURCE_FILTER=0` |
| Hard threshold | `PP_HARD_MIN_SCORE=0.30` | `PP_HARD_MIN_SCORE=0` (disabled) |
| Debug output | N/A (caller opt-in) | N/A |

---

## 5. 实施顺序

1. **FTS fusion** — 最大收益：增加检索通道，exact lexical 保护。风险最低：已存在代码，仅需接入。
2. **Hard threshold** — 简单减法：过滤弱噪声。与 FTS 配合最佳。
3. **Source filter** — 默认 not-too-aggressive: downweight 而非删除。可后续收紧。
4. **Debug output** — 最后加：帮助验证前三项的生效情况，不做排序/质量假设。

每步独立 commit，独立 test。
