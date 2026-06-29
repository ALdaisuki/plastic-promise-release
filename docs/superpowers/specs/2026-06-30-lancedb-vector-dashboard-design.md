# LanceDB 向量检索 + 仪表盘设计

> Date: 2026-06-30
> Status: approved
> Scope: TODO #6 (向量语义检索) + TODO #7 (监控仪表盘)

## 1. 背景

当前 Plastic Promise 的 `memory_recall` 只做文本匹配（CJK bigram / word split），Ollama `mxbai-embed-large` 虽在运行但向量不持久化，语义检索实际未生效。同时缺少可视化面板查看 Agent 状态和任务管道。

## 2. TODO #6 — LanceDB 向量存储 + 混合检索

### 2.1 架构

```
memory_recall(query)
    |
    +-- embedder.embed(query) ----+
    |  (Ollama / FallbackZero)     |
    |                              v
    |                     +------------------+
    |                     |  LanceDBStore     |
    |                     |  .search(vector)  |--> vectorResults
    |                     |  .search_fts(text)|--> bm25Results
    |                     +------------------+
    |                              |
    v                              v
+-------------------------------------------+
|  Hybrid Fusion (ContextEngine)             |
|  fused = vectorScore*0.7 + textScore*0.3   |
|  (BM25 >= 0.75 bypass semantic filter)     |
+-------------------------------------------+
    |
    v  (existing pipeline)
+-------------------------------------------+
|  符号规则 -> 图谱融合 -> 反馈调整 -> 分层   |
+-------------------------------------------+
    |
    v
  ContextPack (core/related/divergent)
```

### 2.2 LanceDB 表结构

遵循 `docs/superpowers/specs/2026-06-28-storage-retrieval-domain-design.md` 定义：

```
table: memory_vectors
+-- memory_id: str          FK -> SQLite memories.id
+-- vector: list[float]     1024-dim embedding (mxbai-embed-large)
+-- text: str               FTS indexed copy
+-- tier: str               L1/L3 scalar filter
+-- category: str           scalar filter
+-- scope: str              global/domain/agent:* filter
```

### 2.3 优雅降级路径

```
Ollama online?
  Yes --> embedder.embed(query) --> LanceDB.search(vector) --> Hybrid Fusion
  No  --> FallbackEmbedder (zero vec) --> _text_retrieval only --> return
```

任何阶段失败都不 crash，回退到纯文本检索。

### 2.4 混合融合公式

```python
def _hybrid_fuse(vector_results, text_results, vector_weight=0.7):
    combined = {}
    for mid, score, content, source in vector_results:
        combined[mid] = (score * vector_weight, content, source)
    for mid, score, content, source in text_results:
        w = score * (1 - vector_weight)
        # BM25 高分保护
        if score >= 0.75:
            w = max(w, score * 0.9)
        combined[mid] = (max(combined.get(mid, (0,))[0], w), content, source)
    return sorted(combined.items(), key=lambda x: x[1][0], reverse=True)
```

### 2.5 新增/修改文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/core/lancedb_store.py` | **NEW** — LanceDB 表创建、CRUD、ANN 搜索、FTS 搜索 |
| `plastic_promise/core/context_engine.py` | 初始化 LanceDBStore；`_vector_retrieval()` 改用 LanceDB；新增 `_hybrid_fuse()`；`_layered_fuse()` 接入融合结果 |
| `plastic_promise/memory/pipeline.py` | `_process_embedded_to_migrate()` 写入 LanceDB 替代 `_vector` 内存字段 |
| `plastic_promise/mcp/server.py` | `memory_recall` 向量通道始终启用 |

### 2.6 不做的

- IVF_PQ 索引 — 当前 28 条记忆，brute-force 足够
- Cross-encoder reranking — 需要外部 API，留到未来
- Tantivy FTS — LanceDB 内置 FTS 足够
- 存量记忆回填 — 当前 28 条全部经过 pipeline，已有向量

---

## 3. TODO #7 — HTML 仪表盘

### 3.1 架构

```
GET /dashboard --> 自包含 HTML 页面
    |
    |  JS 每 5s 轮询:
    +-- GET /health        --> {status, uptime, version, pid}
    +-- GET /api/stats     --> memory_stats() 输出
    +-- GET /api/issues    --> issue_list() 输出
    +-- GET /api/trust     --> defense(action="get") 输出
```

无新文件，HTML 内联在 `server.py` 中（~120 行）。

### 3.2 仪表盘展示

```
Header: Plastic Promise Dashboard v0.1.0 [status dot]
---
Stats Row:  Memories | Decaying | Trust | Active Issues
Systems:    九大身体系统成熟度条形图
Defense:    防线状态 + 信任分等级
Audit:      最近审计摘要
```

### 3.3 修改文件

| 文件 | 改动 |
|------|------|
| `plastic_promise/mcp/server.py` | 新增 `/dashboard` Route（返回 HTML）、`/api/stats`、`/api/issues`、`/api/trust` JSON 端点 |

### 3.4 不做的

- Streamlit/Gradio 独立 UI — 阶段 2
- WebSocket 实时推送 — dashboard 用轮询，5s 间隔够用
- 历史趋势图 — 当前只展示即时快照

---

## 4. 实现顺序

1. **LanceDBStore** — 新建 `lancedb_store.py`，独立可测试
2. **Pipeline 集成** — 修改迁移阶段写入 LanceDB
3. **ContextEngine 集成** — `_vector_retrieval` 改 LanceDB，加 `_hybrid_fuse`
4. **Dashboard** — server.py 加 `/dashboard` 和 API 端点
5. **E2E 验证** — 存一条记忆 → 语义检索 → 确认返回

## 5. 验收标准

- [ ] `memory_store` → 记忆写入 SQLite + LanceDB 双写
- [ ] `memory_recall` 向量相似搜索返回语义相关结果（不只是字面匹配）
- [ ] Ollama 不可用时回退到纯文本检索，不 crash
- [ ] `/dashboard` 浏览器打开显示实时状态
- [ ] `/api/stats` `/api/issues` `/api/trust` 返回正确 JSON
