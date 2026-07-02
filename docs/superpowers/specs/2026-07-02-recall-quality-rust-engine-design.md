# Recall 质量修复 & Rust 全量计算引擎设计

**日期**: 2026-07-02
**状态**: 待审核
**参考项目**: [memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro) (MIT)

---

## 1. 问题陈述

### 1.1 现状

`memory_recall` 于 2026-07-02 修复 `get_engine()` 从 Rust stub 切回 Python 引擎后，从"完全不可用"（0 结果）恢复到"勉强有结果"（3 条 related，core 为空）。根因诊断：

| 问题 | 数据 | 严重度 |
|------|------|--------|
| LanceDB 幽灵向量 | 335 条 vs SQLite 192 条 — 143 条测试残留 | P0 |
| 文本检索过弱 | 简单单词重叠，仅 11/192 命中 | P0 |
| 向量检索污染 | "Performance test memory" 占满 top-10 | P0 |
| 阈值过严 | core≥0.8，实际最高 0.75 | P1 |
| 缺少多样性控制 | 无 MMR，类似内容堆叠 | P2 |
| 缺少长度归一化 | 长文档垄断高分 | P2 |
| Rust 引擎是 stub | `:memory:` 存储，Noop 检索器 | P1 |

### 1.2 目标

- Phase 1：Recall 返回正确的相关记忆，core 层有 ≥3 条匹配项
- Phase 2：Rust 接管全部计算路径，Python 退化为薄胶水 + fallback

---

## 2. 架构

### 2.1 三层架构（Phase 2 终态）

```
┌─────────────────────────────────────────────────┐
│  Python 胶水层（薄）                              │
│  • MCP 协议 (server.py)                          │
│  • Embedding API (Ollama mxbai-embed-large)      │
│  • SQLite 写 (memory_store → plastic_memory.db)  │
│  • 全局版本号管理（每次写操作递增）                │
│  • skill 追踪 / 猎人公会 / step-closure          │
└──────────────────┬──────────────────────────────┘
                   │ supply(query, vector, version)
┌──────────────────▼──────────────────────────────┐
│  Rust 计算引擎（厚）                              │
│  • SQLite 只读 (plastic_memory.db, WAL 模式)     │
│  • 向量检索：内存 brute-force cosine (<10K 条)    │
│  • BM25：内存 IDF 加权 + 版本号懒刷新             │
│  • 13 阶段检索管道                                │
│  • 领域模型 (WeibullDecay / WilsonWorth / Tier)  │
└─────────────────────────────────────────────────┘
```

### 2.2 关键决策：不引入 `lancedb` Rust crate

**理由**: `lancedb` Rust crate 依赖 tokio async runtime，与 pyo3 同步架构冲突。当前 192~1000 条规模下内存 brute-force <5ms。扩展路径：mmap 读取 LanceDB 磁盘格式，而非走 tokio API。

**数据流**: Python 写 LanceDB（已有），Rust 读 SQLite 文本 + 内存向量索引。两条路径不冲突——SQLite WAL 模式下读写并发安全。

---

## 3. 13 阶段检索管道

对标 memory-lancedb-pro 的完整管道，按执行顺序排列：

| # | 阶段 | 类型 | 算法 | 参数 |
|---|------|------|------|------|
| 0 | 自适应检索门 | gate | 短问候/命令跳过：CJK<6 chars 或 EN<15 chars 跳过检索 | 强制词（/remember/、"你记得"）绕过 |
| 1 | 查询扩展 | transform | 中英同义词字典，最多 5 个扩展词 | 不去重已有词 |
| 2 | 嵌入查询 | embed | Ollama mxbai-embed-large → 1024d | Python 生成，Rust 接收 |
| 3 | 并行向量+BM25 | search | 向量: cosine brute-force; BM25: IDF 加权 | 各取 top-K (K=candidatePoolSize) |
| 4 | RRF 融合 | fusion | 标准 Reciprocal Rank Fusion | K=60 |
| 5 | 最低分过滤 | filter | score < 0.3 → 丢弃 | minScore=0.3 |
| 6 | Rerank（可选） | rerank | Ollama cross-encoder, 5s 超时, 失败静默回退 | 默认关闭, `PP_RECALL_RERANK=1`。返回 `rerank_status`: `"completed"` / `"skipped_disabled"` / `"skipped_timeout"` / `"skipped_ollama_down"` / `"skipped_no_model"` |
| 7 | 新鲜度加成 | boost | additive: exp(-ageDays/14) × 0.1 | recencyHalfLifeDays=14 |
| 8 | 重要性加权 | weight | multiplicative: 0.7 + 0.3 × importance | floor: score × 0.7 |
| 9 | 长度归一化 | norm | score × 1/(1 + 0.5 × log₂(len/500)) | anchor=500, floor: score × 0.3 |
| 10 | 时间衰减 | decay | access-reinforced exponential | baseHL=60d, reinforcement=0.5, cap=3x |
| 11 | 硬最低分 | filter | score < 0.35 → 丢弃 | hardMinScore=0.35 |
| 12 | MMR 多样性 | diversity | greedy: cos>0.85 → score × 0.70, deferred | threshold=0.85, penalty=0.70 |
| — | 分层 | layer | core≥0.70, related≥0.40, divergent≥0.20 | 环境变量可覆盖 |
| — | 截断 | slice | 按层截断到 max_results | core 优先 |

### 3.1 阶段间数据流顺序

```
向量 + BM25 并行搜索
  ↓
RRF 融合
  ↓
符号规则 boost（关键词加权 1.2~1.5×）
  ↓
反馈 multiplier（worth_score → multiplier）
  ↓
最低分过滤 (0.3)
  ↓
Rerank（可选，60/40 hybrid fusion）→ 返回 rerank_status 字段
  ↓
新鲜度加成（additive boost）
  ↓
重要性加权（multiplicative）
  ↓
长度归一化（multiplicative, floor 0.3）
  ↓
时间衰减（multiplicative, floor 0.5）
  ↓
硬最低分 (0.35)
  ↓
MMR 多样性（cos>0.85 → ×0.70）
  ↓
分层（core/related/divergent）
```

顺序的依据：归一化在 MMR 之前（先让分数可比，再降权重复项）；MMR 在分层之前（让分层看到多样化后的分数分布）。

### 3.2 阶段 0：自适应检索门

低于阈值则**跳过检索**（不触发 recall pipeline，直接返回空）：

```python
if contains_cjk(query) and len(query) < 6:  return empty
if is_english(query) and len(query) < 15:   return empty
```

强制检索关键词（绕过门禁）：`/remember/`、`/last time/`、`"你记得"`、`"之前"`、`"上次"`。

### 3.3 阶段 6：Rerank 降级行为

`rerank_status` 字段（Phase 2 加入，Phase 1 可选）：

| 值 | 含义 |
|----|------|
| `"completed"` | Rerank 正常完成 |
| `"skipped_disabled"` | `PP_RECALL_RERANK=0`，未开启 |
| `"skipped_timeout"` | API 超时（>5s），跳过 |
| `"skipped_ollama_down"` | Ollama 不可用，跳过 |
| `"skipped_no_model"` | 指定 rerank 模型未找到，跳过 |

### 3.4 阶段 12：MMR 多样性 — 向量来源

- **Phase 1**（Python）：`ScoredItem` 扩展 `.vector` 字段，从 LanceDB 或 `ContextEngine._vectors` 读取
- **Phase 2**（Rust）：从 `LanceDbStore.vectors` HashMap 读取

零向量（`all(v==0 for v in vector)`）在 MMR 阶段跳过余弦相似度计算，直接加入 selected。

### 3.5 分层验收标准

| 层 | 预期行为 | 反例 |
|----|---------|------|
| core (≥0.70) | 与查询主题直接相关 | 不应包含完全不相关的记忆 |
| related (≥0.40) | 间接相关或同领域 | 不应包含垃圾/测试数据 |
| divergent (≥0.20) | 低相关但有潜在价值 | **不应包含完全不相关的记忆** |

---

## 4. Schema 对齐

### 4.1 Rust MemoryRecord 补齐

Python SQLite (`plastic_memory.db`) 有 20 列，Rust 现有 14 列。需补齐 4 列：

```rust
pub struct MemoryRecord {
    // === 现有字段（保持不变）===
    pub id: String,
    pub content: String,
    pub memory_type: String,
    pub source: String,
    pub tier: String,
    pub scope: String,
    pub category: String,
    pub importance: f64,
    pub worth_success: u32,
    pub worth_failure: u32,
    pub access_count: u32,
    pub last_accessed_at: String,
    pub created_at: String,
    pub metadata_json: String,
    pub entity_ids: Vec<String>,
    pub activation_weight: f64,
    pub last_accessed: String,
    pub attributes: HashMap<String, String>,

    // === 新增字段 ===
    pub tags: Vec<String>,         // SQL: tags TEXT DEFAULT '[]'
    pub domain: String,            // SQL: domain TEXT DEFAULT 'uncategorized'
    pub decay_multiplier: f64,     // SQL: decay_multiplier REAL DEFAULT 1.0
    pub effective_half_life: f64,  // SQL: effective_half_life REAL DEFAULT 3.0
}
```

### 4.2 tags 解析边界情况

```rust
fn parse_tags(raw: &str) -> Vec<String> {
    if raw.is_empty() || raw == "[]" || raw == "null" {
        return vec![];
    }
    serde_json::from_str(raw).unwrap_or_default()
}
```

---

## 5. BM25 实现

### 5.1 Bm25Index 结构

```rust
struct Bm25Index {
    doc_freq: HashMap<String, usize>,          // 词 → 包含该词的文档数
    term_freqs: HashMap<String, HashMap<String, usize>>, // 记忆ID → (词 → 词频)
    avg_doc_len: f64,                           // 平均文档长度（词数）
    total_docs: usize,
    version: u64,                               // 全局版本号
}
```

### 5.2 词条化策略

```rust
fn tokenize(text: &str) -> Vec<String> {
    if text.is_empty() { return vec![]; }

    let has_cjk = text.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c));

    if has_cjk {
        // CJK: bigram 分词，最小长度 ≥2
        text.chars().collect::<Vec<_>>()
            .windows(2)
            .map(|w| w.iter().collect::<String>())
            .filter(|s| !s.contains(|c: char| c.is_whitespace()))
            .collect()
    } else {
        // English: split by whitespace, filter stopwords, min length ≥3
        text.split_whitespace()
            .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase())
            .filter(|w| w.len() >= 3)
            .collect()
    }
}
```

### 5.3 BM25 评分公式

Okapi BM25: `score = Σ IDF(t) × tf(t,d) × (k1+1) / (tf(t,d) + k1 × (1-b + b × |d|/avgdl))`

参数: `k1=1.2`, `b=0.75`。

### 5.4 版本号刷新

- **存储位置**：SQLite 单格表 `CREATE TABLE IF NOT EXISTS memory_version (version INTEGER DEFAULT 0)`。初始值为 0，每次写操作递增。
- Python 端：`memory_store` / `memory_update` / `memory_forget` / `memory_correct` 执行 `UPDATE memory_version SET version = version + 1`
- Rust 端：`supply()` 入口从 SQLite 读取 `SELECT version FROM memory_version`，与本地缓存版本比较
- 192 条重建耗时 <20ms，在 supply 延迟预算内
- 好处：Rust 可直接从 SQLite 读取，不需要 Python 传递参数

---

## 6. MMR 多样性

### 6.1 算法

```rust
fn mmr_diversity(
    mut results: Vec<ScoredItem>,
    threshold: f64,   // 0.85
    penalty: f64,     // 0.70
) -> Vec<ScoredItem> {
    // 1. 按分数降序排列
    results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());

    // 2. Greedy MMR
    let mut selected: Vec<ScoredItem> = Vec::new();
    let mut deferred: Vec<ScoredItem> = Vec::new();

    for item in results {
        let too_similar = selected.iter().any(|s| {
            cosine_similarity(&s.vector, &item.vector) > threshold
        });
        if too_similar {
            let mut penalized = item;
            penalized.score *= penalty;
            deferred.push(penalized);
        } else {
            selected.push(item);
        }
    }

    // 3. 降权项仍保留，拼接到末尾
    deferred.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
    selected.append(&mut deferred);
    selected
}
```

### 6.2 向量比较优化

- 向量数 <10：完整 1024d 余弦相似度
- 向量数 ≥10：降采样到前 64 维

---

## 7. Phase 1：Recall 质量修复（Python 侧）

### 7.1 步骤清单

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1.1 | `lancedb_store.py` | `rebuild_all()` — 清空 LanceDB 表，从 SQLite 192 条重建全部向量 |
| 1.2 | `context_engine.py:_text_retrieval` | 替换简单词重叠为 BM25 IDF 加权。英文：porter stem + 停用词过滤；CJK：保持 bigram |
| 1.3 | `context_engine.py:CONTEXT_LAYERS` | 阈值可配置：`PP_CORE_MIN_RELEVANCE`(default 0.70), `PP_RELATED_MIN_RELEVANCE`(default 0.40) |
| 1.4 | `context_engine.py:_supply_python` | 在符号规则/反馈之后、分层之前插入 MMR 多样性（cos>0.85 → ×0.70） |
| 1.5 | `context_engine.py:_supply_python` | 在 MMR 之前插入长度归一化（anchor=500，floor 0.3） |
| 1.6 | `context_engine.py:_supply_python` | Rerank 可选开关：`PP_RECALL_RERANK=1` 时，top-30 送 Ollama cross-encoder 重排，5s 超时，失败静默回退 |
| 1.7 | 手动验证 | `memory_recall("code review scanner data quality fix")` 预期 core≥3 条相关 |

### 7.2 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PP_CORE_MIN_RELEVANCE` | `0.70` | core 层最低分数 |
| `PP_RELATED_MIN_RELEVANCE` | `0.40` | related 层最低分数 |
| `PP_DIVERGENT_MIN_RELEVANCE` | `0.20` | divergent 层最低分数 |
| `PP_RECALL_RERANK` | `0` | 设为 `1` 开启 cross-encoder 重排 |
| `PP_RERANK_MODEL` | `""` | 重排模型名（不设则自动检测 Ollama 可用模型） |
| `PP_RERANK_TIMEOUT` | `5.0` | 重排超时秒数 |

---

## 8. Phase 2：Rust 引擎重构

### 8.1 步骤清单

| 步骤 | 内容 |
|------|------|
| 2.1 | **Schema 补齐** — `MemoryRecord` 加 4 列，`row_to_record`、DDL、`from_storage` 全部适配 |
| 2.2 | **只读连接** — `SqliteStorage::open_readonly("plastic_memory.db")`，`OpenFlags::SQLITE_OPEN_READ_ONLY`，不启用 WAL（只读不需要） |
| 2.3 | **BM25 索引** — `Bm25Index` 结构 + `supply()` 入口版本号检查 + 懒刷新 |
| 2.4 | **13 阶段管道** — 按第 3 节顺序在 `supply()` 中实现全部阶段 |
| 2.5 | **`_supply_rust` dispatch** — 取消 `context_engine.py:supply()` 中 Rust dispatch 的注释 |
| 2.6 | **领域模型激活** — WeibullDecay / WilsonWorth / DefaultTierManager 接入管道 |
| 2.7 | **保留 Python fallback** — `_supply_python` 保留（含 Phase 1 全部改进），标记 `@deprecated` 仅用于 Rust 不可用时的降级路径 |

### 8.2 Rust 不可用时的 fallback 链

```
supply() 
  ├─ PP_FORCE_PYTHON_SUPPLY=1? → _supply_python()  (强制 Python 路径，A/B 测试)
  ├─ Rust healthy?              → _supply_rust()     (Phase 2 终态主路径)
  └─ Rust down?                 → _supply_python()   (Phase 1 改进保留的备用路径)
```

`PP_FORCE_PYTHON_SUPPLY=1` 环境变量用于紧急回退和 A/B 测试（比较 Python vs Rust 输出一致性）。
HEALTH_CHECK 逻辑（已有 `_check_rust_health()`）：检查 Rust `.pyd` 是否可导入 + 引擎实例是否正常，缓存 300s。

---

## 9. 验证计划

### 9.1 Phase 1 验证

```
查询: "code review scanner data quality fix"
预期: core ≥ 3 条，包含 scan_data_quality 实现 / data-quality-chain-fix 设计 / pipeline 修复相关记忆
指标: core 不为空，top-5 中有 ≥3 条与查询主题直接相关
```

### 9.2 Phase 2 验证

```
- Python supply() vs Rust supply() 对同一查询返回结果的 Kendall Tau 相关系数 ≥ 0.90
  （排名一致性度量，不直接比较分数绝对值——两个引擎的分数尺度可能不同）
- 192 条记忆 supply() 耗时 <50ms
- Rust 引擎 OOM 或 crash 后自动回退 Python fallback，不影响 MCP 工具可用性
- PP_FORCE_PYTHON_SUPPLY=1 可立即回退到 Python 路径
```

Kendall Tau 计算方法：
```python
from scipy.stats import kendalltau
python_ranks = [item.id for item in sorted(python_pack.all_items, key=lambda x: -x.relevance)]
rust_ranks = [item.id for item in sorted(rust_pack.all_items, key=lambda x: -x.relevance)]
tau, p = kendalltau(python_ranks, rust_ranks)
assert tau >= 0.90, f"Rank correlation too low: tau={tau:.3f}"
```

---

## 10. 参考资料

- [memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro) (MIT) — 13 阶段检索管道参考实现
- LanceDB FTS: `table.create_fts_index()`, BM25 with k1=1.2, b=0.75
- LanceDB Hybrid Search: `query_type="hybrid"`, RRF reranker (K=60)
- `rusqlite` read-only: `Connection::open_with_flags(path, SQLITE_OPEN_READ_ONLY)`
