# Recall 质量诊断 & 分阶段修复设计

**日期**: 2026-07-03
**状态**: design — pending review
**覆盖**: 写入侧碎片化 + 检索侧元数据丢失 + 噪声过滤 + 检索管道升级
**参考项目**: [memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro) (MIT)

---

## 1. Problem

### 1.1 症状 (2026-07-03 会话实测)

| 症状 | 复现 | 严重度 |
|------|------|--------|
| URL 被拆成碎片记忆 | `https://github` + `com/CortexReach/memory-lancedb-pro` 作为两条独立记忆出现在 top-k | P0 |
| 精确 API 查询命中失败 | `TrustManager boost decay API apply_trust_delta` 返回原则/audit/分支碎片，未命中文件记忆系统中的对应项 | P0 |
| recall 输出 `worth_score` 全为 0 | `memory_recall` 所有 item.worth_score=0.0，但 `memory_list` 真实 worth=0.5~0.94 | P0 |
| telemetry/daemon audit 污染 top-k | `AUDIT trust=0.60 pipeline=1.00...` 出现在正常业务 recall 高位 | P1 |
| noise snippet 污染 | `No file edits`, `md files only` 作为独立记忆存在并被召回 | P1 |
| `memory_gc` 假阴性 | dry_run 报告 candidates=0, health=1.0，但肉眼可见碎片/低信息密度记忆 | P1 |
| 写入侧 `access_count` 和 `last_accessed` 不会被 recall 逆向更新 | 272 条记忆中 234 条 access_count=0 | P2 |
| sp-stage 部分阶段 Unknown skill | `using-git-worktrees` / `test-driven-development` / `verification-before-completion` 无法通过 sp-stage MCP 执行 | P2 |

### 1.2 目标

- Phase 1 (P0 修复): URL / 点号标识符不碎片化，recall worth_score 正确传播，低信息/telemetry 噪声被过滤
- Phase 2 (检索管道): hybrid vector+BM25/FTS 一等融合，hard threshold，MMR 多样性
- Phase 3 (质量体系): recall explain/debug 输出，golden-query 测试集，scope/source 分类隔离

---

## 2. Architecture

总体方案: 三阶段递进，不一次性改架构。Phase 1 只改 boundary logic (写入/召回构造)，不碰 core pipeline；Phase 2 升级 retrieval pipeline；Phase 3 加质量监控。

```
Phase 1: Boundary Fixes (本次完成)
  smart_extractor URL 保护 + noise_filter 增强 + worth_score 计算 + recall 噪声过滤

Phase 2: Retrieval Pipeline Upgrade
  FTS/BM25 一等融合 + hard threshold + MMR + source/type 过滤

Phase 3: Quality Observability
  recall explain/debug + golden-query tests + scope isolation + health metric reform
```

---

## 3. Phase 1: Boundary Fixes (已实现 quick fixes)

### 3.1 组件 A: URL/Token 提取保护

**What**: 分句前用正则保护 URL、点号标识符、路径，分句后恢复。

**Why**: `extract_memories()` 使用 `re.split(r"[。！？.!?\n]+", ...)` 分句，直接打碎 URL (`https://github` / `com/...`) 和点号标识符 (`module.name`, `file.py`)。

**Interface**: `smart_extractor.py` 内部新增 `_PROTECTED_TOKEN_PATTERN` / `_protect_sentence_tokens()` / `_restore_sentence_tokens()` / `_split_memory_sentences()`。

**Insertion**: `extract_memories()` line ~146，将 `re.split(...)` 替换为 `_split_memory_sentences(conversation)`。`_generate_l0_l1()` 也改用新分句函数。

**Env**: 内置保护，无需环境变量。

### 3.2 组件 B: noise filter 扩展

**What**: 新增便携信息片段和 telemetry 模式过滤。

**Why**: 当前 `is_noise()` 只过滤否认/元问题/短样板/emoji，不过滤 `No file edits`、`md files only`、partial URL、`AUDIT trust=...`、skill trace 标签。

**New patterns**:
- `LOW_INFORMATION_SNIPPETS`: `"no file edits"`, `"md files only"`, `"read-only"`, `"no edits"`
- `PARTIAL_URL_PATTERNS`: `^https?://[^\s./]+$`, `^(com|org|net|io|dev|cn|ai)/[\w./-]+$`
- `TELEMETRY_PATTERNS`: `^audit\s+trust=`, `^\[?skill (start|complete|abandoned)\]?`

**Insertion**: `noise_filter.py`，在 length < 5 检查之后，denial/meta 检查之前。

### 3.3 组件 C: worth_score 正确传播

**What**: `ContextItem` 构造时不再直接读取不存在的 `worth_score` 字段，改为从 `worth_success` / `worth_failure` 计数器计算。

**Why**: SQLite / Python in-memory 记录存 `worth_success` 和 `worth_failure`，不存 `worth_score`。`memory_recall` 输出中的 `worth_score=0` 是因为 `mem.get("worth_score", 0.0)` 永远读不到值。

**Formula**: `(worth_success + 1) / (worth_success + worth_failure + 2)`；无计数时中性 0.5；显式字段 cover。

**Interface**: `ContextEngine._calc_worth_score_from_memory(mem: dict | None) -> float`

**Insertion**:
- 排名 worth 计算: `context_engine.py` `_build_items` loop，`worth` 变量改用统一计算
- `ContextItem.worth_score` 赋值: 同上，`worth_score = _calc_worth_score_from_memory(mem)`
- `_apply_feedback()` 内部的 worth 计算: 也切换到统一计算

### 3.4 组件 D: recall 构造时噪声过滤

**What**: 在 `supply()` 构造 `ContextItem` 前，对已有噪声记忆再做一次 `is_noise` 检查。

**Why**: 历史数据库中已存在噪声记忆（旧版 noise filter 覆盖不全）。仅靠写入侧过滤不够，召回侧需再过滤一次。

**Interface**: `ContextEngine._is_recall_noise(content: str) -> bool`

**Insertion**: `context_engine.py` `_build_items` 中 `ContextItem` 构造前。

---

## 4. Phase 2: Retrieval Pipeline Upgrade

### 详细设计见 `2026-07-03-recall-pipeline-upgrade-plan.md`。设计要点：

### 4.1 LanceDB FTS/BM25 一等融合

**当前**: Python `_text_retrieval()` (BM25-like 实现) 运行正常，LanceDB FTS (`lancedb_store.py:150`) 存在但未接入主 supply 路径。

**目标**: 与 `_text_retrieval` 并行运行，与 vector 结果在 `_hybrid_fuse` 中融合。Exact lexical hit 加 preservation floor (不会被 rerank/vector 覆盖)。

**参考**: memory-lancedb-pro `retriever.ts` — vector + BM25/FTS 并行搜索 → weighted fusion → rerank。

### 4.2 检索侧 source/type 过滤

**当前**: recall 混合 user durable preference, project knowledge, task fragments, skill session traces, daemon audit, PR review snippets, telemetry。无 source 过滤。

**目标**: 默认普通 recall 排除或强降权: `maintenance_daemon`, `superpowers` (skill trace), `step-closure` reflect 片段。`context_supply` 可主动选择额外 source。

**参考**: memory-lancedb-pro scope model (global/agent/project/user/custom)。

### 4.3 Hard Threshold & MMR

**当前**: 无 hard min score 过滤，弱相关碎片仍可进入 related。MMR `_apply_mmr` 已存在 (`context_engine.py:1497`) 但之前向量 lookup 路径有 bug（已在 vertical-slice Unit 6 中计划修复）。

**目标**: hard min score filter (0.30~0.35) + MMR cosine threshold 0.85 penalty 0.70 对标 reference 标准。

---

## 5. Phase 3: Quality Observability

### 5.1 recall explain / debug 输出

**目标**: `memory_recall(debug=True)` 返回每阶段计数和每条结果的 score breakdown。对标 memory-lancedb-pro CLI `--debug` 模式。

**Per-stage diagnostics**:
```json
{
  "pipeline": {
    "vector_count": 20,
    "bm25_count": 18,
    "fused_count": 25,
    "after_hard_min_score": 15,
    "after_noise_filter": 12,
    "after_mmr": 10,
    "final": 8
  },
  "per_item": [
    {
      "id": "...",
      "vector_score": 0.72,
      "bm25_score": 0.85,
      "fused_score": 0.81,
      "worth": 0.86,
      "decay": 0.95,
      "final_score": 0.77,
      "penalties": []
    }
  ]
}
```

### 5.2 golden-query 测试集

**Test cases**:
| Query | Expected |
|---|---|
| `TrustManager boost decay API apply_trust_delta` | top-3 命中对应 memory file |
| `SuperPowers worktrees not optional` | top-3 命中 worktree 规则 |
| `CortexReach memory-lancedb-pro` | 一条完整记忆被召回，不被拆碎 |
| 中文 `记忆质量 低` | 命中 recall quality 相关，非原则通用描述 |
| `No file edits` | 不在默认 recall top-k 中 |
| `AUDIT trust=0.60 pipeline=` | 只在显式查询 audit/telemetry 时召回 |
| `remember what user prefers for Rust backend` | 命中用户偏好记忆 |

### 5.3 health metric reform

**当前**: `memory_gc(dry_run=True)` 只检查向量 merge candidates。health=1.0 与实际用户感知 recall 质量脱节。

**目标**: 增加 recall-relevant health 维度: 碎片率 (<30 chars 记忆占比)、重复率 (cos≥0.85 簇数)、噪声率 (telemetry/skill trace/operational snippet 占比)、分类覆盖率 (uncategorized domain 占比)。

---

## 6. Data Flow (目标终态)

```
User Query
  → adaptive retrieval gate
  → query expansion (synonym dictionary)
  → embed query (Ollama mxbai-embed-large → 1024d)
  → parallel: LanceDB ANN search + LanceDB FTS/BM25 + Python BM25
  → hybrid fuse (RRF or weighted, with exact-lexical preservation floor)
  → symbol rule boost
  → feedback/worth multiplier
  → decay-aware score adjustment
  → length normalization
  → hard min score filter
  → source/type filter (排除 telemetry/session/daemon 除非显式查询)
  → optional rerank (multi-provider: Jina→SiliconFlow→Ollama→cosine)
  → MMR diversity (cos>0.85 → ×0.70, defer)
  → noise filter (last-resort, 排除 historical pollution)
  → layer assignment (core≥0.70, related≥0.40, divergent≥0.20)
  → truncate to max_results
  → ContextPack + audit_metadata + gap_signal
```

---

## 7. Error Handling

- Extraction URL 保护失败 → fallback to raw `re.split` (不丢失原始行为)
- noise filter 模式编译失败 → 原始 is_noise 继续工作 (不新增 noise 通过)
- worth_score 计算模块导入失败 → 返回中性 0.5
- FTS/BM25 不可用 → 退回到纯 vector 检索
- rerank 超时或全部 provider 失败 → 保留 fusion 分数
- MMR vector lookup 失败 → 退回到 content-only MMR

---

## 8. Testing

### Phase 1 回归 (已完成)
```bash
pytest -q tests/test_recall_quality_quick_fixes.py
pytest -q tests/test_noise_filter.py
pytest -q tests/test_pipeline_quality.py::TestPipelineQuality::test_store_urgent_extracts_memories
```

### Phase 2 集成测试 (待实现)
```bash
# FTS/BM25 fusion 不破坏已有 vector-only recall
# hard threshold 过滤已知弱匹配
# MMR 降权重复项后 top-k 多样性提升
# source/type 过滤后 daemon audit 不再出现在默认 recall
```

### Phase 3 golden-query (待实现)
```bash
pytest -q tests/test_recall_golden_queries.py
```

---

## 9. Constraints

- No new MCP tools — 内部 pipeline 改进
- No LanceDB schema migration — schema v1 不变
- No API signature changes — `supply()`, `memory_store()`, `memory_recall()` 不变
- Per-phase env var gate — 独立 rollback
- All external providers use free tiers (Jina/SiliconFlow)
- Python path 为主，Rust path 保持作为可选加速
