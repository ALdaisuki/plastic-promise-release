# LanceDB 向量检索 + 仪表盘实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 LanceDB 向量持久化存储 + 混合语义/文本融合检索 + HTML 监控仪表盘。

**Architecture:** 新建 `LanceDBStore` 类封装向量表 CRUD 和 ANN/FTS 搜索，集成到 `ContextEngine._vector_retrieval()` 替换内存暴力搜索，新增 `_hybrid_fuse()` 方法融合向量和文本分数。Pipeline 迁移阶段双写 LanceDB。Starlette 加 `/dashboard` 路由返回自包含 HTML 页面。

**Tech Stack:** Python 3.11+, lancedb 0.30.0, pyarrow 23.0.1, Starlette (existing)

## Global Constraints

- 所有写操作 SQLite + LanceDB 双写
- Ollama 不可用时回退到纯文本检索，不 crash
- 向量维度 1024 (mxbai-embed-large)
- LanceDB 表名 `memory_vectors`，路径 `plastic_memory.lancedb`
- Dashboard HTML 内联在 server.py，零外部文件依赖
- 遵循已有设计文档 `2026-06-28-storage-retrieval-domain-design.md` 的表结构

---

### Task 1: LanceDBStore — 向量存储核心

**Files:**
- Create: `plastic_promise/core/lancedb_store.py`

**Interfaces:**
- Consumes: `Embedder` (from `plastic_promise.core.embedder`), `pyarrow`, `lancedb`
- Produces: `LanceDBStore` class with `search()`, `search_fts()`, `insert()`, `update()`, `delete()`, `count_rows()`, `backfill()`

- [ ] **Step 1: 写文件骨架和导入**

```python
"""LanceDBStore — persistent vector storage with ANN + FTS search.

Table: memory_vectors (memory_id, vector, text, tier, category, scope)
Vector dim: 1024 (mxbai-embed-large), configurable via EMBEDDER_MODEL.
"""
import os
import logging
from typing import Optional

import pyarrow as pa
import lancedb

from plastic_promise.core.embedder import Embedder, get_embedder

logger = logging.getLogger("plastic-promise.lancedb")

EMB_DIM = int(os.environ.get("PP_EMBEDDING_DIM", "1024"))
TABLE_NAME = "memory_vectors"

_MEMORY_VECTORS_SCHEMA = pa.schema([
    pa.field("memory_id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), EMB_DIM)),
    pa.field("text", pa.string()),
    pa.field("tier", pa.string()),
    pa.field("category", pa.string()),
    pa.field("scope", pa.string()),
])
```

- [ ] **Step 2: 写构造函数和表初始化**

```python
class LanceDBStore:
    """Persistent vector store backed by LanceDB.

    Provides ANN vector search, FTS text search, and CRUD operations.
    Table is created on first access if it doesn't exist.
    """

    def __init__(self, db_path: str, embedder: Embedder) -> None:
        self._path = db_path
        self._embedder = embedder
        self._db: Optional[lancedb.DBConnection] = None
        self._table: Optional[lancedb.table.Table] = None
        self._fts_ready = False
        self._init_db()

    def _init_db(self) -> None:
        """Open or create LanceDB database and table."""
        os.makedirs(self._path, exist_ok=True)
        self._db = lancedb.connect(self._path)
        existing = self._db.table_names()
        if TABLE_NAME in existing:
            self._table = self._db.open_table(TABLE_NAME)
            logger.info("LanceDB: opened existing table '%s' (%d rows)",
                        TABLE_NAME, self._table.count_rows())
        else:
            self._table = self._db.create_table(
                TABLE_NAME, schema=_MEMORY_VECTORS_SCHEMA, data=[]
            )
            logger.info("LanceDB: created table '%s'", TABLE_NAME)
        self._ensure_fts()

    def _ensure_fts(self) -> None:
        """Create FTS index on 'text' column if not already present."""
        try:
            self._table.create_fts_index("text", replace=False)
            self._fts_ready = True
            logger.info("LanceDB: FTS index ready on 'text'")
        except Exception as e:
            logger.warning("LanceDB: FTS index not available (%s), using fallback", e)
            self._fts_ready = False
```

- [ ] **Step 3: 写 search() — 向量 ANN 搜索**

```python
    def search(
        self,
        vector: list[float],
        k: int = 20,
        scope: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> list[tuple[str, float, str, str, str]]:
        """ANN vector search by cosine similarity.

        Args:
            vector: Query embedding (len == EMB_DIM).
            k: Max results to return.
            scope: Optional scope filter.
            tier: Optional tier filter.

        Returns:
            List of (memory_id, score, text, tier, scope) sorted by similarity descending.
        """
        if self._table is None:
            return []
        try:
            q = self._table.search(vector).metric("cosine").limit(k)
            # LanceDB returns distance for cosine metric; lower is better.
            # Convert to similarity: 1.0 - distance (cosine dist in [0, 2])
            raw = q.to_list()
            results = []
            for row in raw:
                dist = row.get("_distance", 0.0)
                sim = 1.0 - (dist / 2.0)  # normalize to [0, 1]
                mid = row["memory_id"]
                if scope and row.get("scope") != scope:
                    continue
                if tier and row.get("tier") != tier:
                    continue
                results.append((mid, max(0.0, min(1.0, sim)),
                                row.get("text", ""),
                                row.get("tier", "L1"),
                                row.get("scope", "global")))
            return results
        except Exception as e:
            logger.error("LanceDB vector search failed: %s", e)
            return []
```

- [ ] **Step 4: 写 search_fts() — 全文搜索**

```python
    def search_fts(
        self,
        query: str,
        k: int = 20,
        scope: Optional[str] = None,
    ) -> list[tuple[str, float, str, str, str]]:
        """Full-text search with fallback to LIKE-based filtering.

        Args:
            query: Text query string.
            k: Max results to return.
            scope: Optional scope filter.

        Returns:
            List of (memory_id, score, text, tier, scope).
        """
        if self._table is None:
            return []
        try:
            if self._fts_ready:
                raw = self._table.search(query, query_type="fts").limit(k).to_list()
            else:
                # Fallback: substring match via pyarrow compute
                pattern = f"%{query}%"
                raw = self._table.search().where(
                    f"text LIKE '{pattern}'", prefilter=True
                ).limit(k).to_list()
            results = []
            for row in raw:
                mid = row["memory_id"]
                score = row.get("_distance", row.get("_score", 0.5))
                if isinstance(score, (int, float)):
                    score = 1.0 - min(float(score), 1.0)
                else:
                    score = 0.5
                if scope and row.get("scope") != scope:
                    continue
                results.append((mid, max(0.0, min(1.0, score)),
                                row.get("text", ""),
                                row.get("tier", "L1"),
                                row.get("scope", "global")))
            return results
        except Exception as e:
            logger.warning("LanceDB FTS search failed: %s", e)
            return []
```

- [ ] **Step 5: 写 CRUD — insert/update/delete/count**

```python
    def insert(
        self, memory_id: str, vector: list[float], text: str,
        tier: str = "L1", category: str = "other", scope: str = "global",
    ) -> None:
        """Insert a vector row. No-op if memory_id already exists."""
        if self._table is None:
            return
        try:
            existing = self._table.search().where(
                f"memory_id = '{memory_id}'", prefilter=True
            ).limit(1).to_list()
            if existing:
                return  # already exists, skip
            self._table.add([{
                "memory_id": memory_id,
                "vector": vector,
                "text": text,
                "tier": tier,
                "category": category,
                "scope": scope,
            }])
        except Exception as e:
            logger.error("LanceDB insert failed for %s: %s", memory_id, e)

    def update(
        self, memory_id: str, vector: list[float], text: str,
        tier: str = "L1", category: str = "other", scope: str = "global",
    ) -> None:
        """Update or insert a vector row (upsert)."""
        self.delete(memory_id)
        self.insert(memory_id, vector, text, tier, category, scope)

    def delete(self, memory_id: str) -> None:
        """Delete a vector row by memory_id."""
        if self._table is None:
            return
        try:
            self._table.delete(f"memory_id = '{memory_id}'")
        except Exception as e:
            logger.error("LanceDB delete failed for %s: %s", memory_id, e)

    def count_rows(self) -> int:
        """Return total rows in the table."""
        if self._table is None:
            return 0
        try:
            return self._table.count_rows()
        except Exception:
            return 0
```

- [ ] **Step 6: 写 backfill() — 存量回填**

```python
    def backfill(self, engine: object) -> int:
        """Backfill LanceDB from SQLite for memories missing vectors.

        Called once during ContextEngine initialization. Only runs if
        the LanceDB table has fewer entries than SQLite.

        Args:
            engine: ContextEngine instance with list_memories().

        Returns:
            Number of memories backfilled.
        """
        ldb_count = self.count_rows()
        sqlite_count = getattr(engine, 'memory_count', 0)
        if ldb_count >= sqlite_count:
            logger.info("LanceDB backfill: table has %d rows, SQLite has %d — skip",
                        ldb_count, sqlite_count)
            return 0

        logger.info("LanceDB backfill: %d in LDB < %d in SQLite — starting",
                    ldb_count, sqlite_count)
        records = engine.list_memories(limit=10000)
        backfilled = 0
        for r in records:
            mid = r.id
            # Check if already in LanceDB
            try:
                existing = self._table.search().where(
                    f"memory_id = '{mid}'", prefilter=True
                ).limit(1).to_list()
                if existing:
                    continue
            except Exception:
                pass
            # Generate embedding and insert
            try:
                vec = self._embedder.embed(r.content)
                self.insert(
                    memory_id=mid,
                    vector=vec,
                    text=r.content,
                    tier=getattr(r, 'tier', 'L1'),
                    category=getattr(r, 'category', 'other'),
                    scope=getattr(r, 'scope', 'global'),
                )
                backfilled += 1
                if backfilled % 10 == 0:
                    logger.info("LanceDB backfill: %d/%d done", backfilled, len(records))
            except Exception as e:
                logger.warning("LanceDB backfill: skip %s — %s", mid, e)
        logger.info("LanceDB backfill complete: %d memories indexed", backfilled)
        return backfilled
```

- [ ] **Step 7: 提交**

```bash
git add plastic_promise/core/lancedb_store.py
git commit -m "feat: LanceDBStore — persistent vector storage with ANN + FTS + backfill"
```

---

### Task 2: Pipeline 集成 — 迁移阶段写 LanceDB

**Files:**
- Modify: `plastic_promise/memory/pipeline.py:236-281`

**Interfaces:**
- Consumes: `LanceDBStore` from Task 1
- Produces: vectors persisted to LanceDB on migration

- [ ] **Step 1: 在 _process_embedded_to_migrate 中加 LanceDB 写入**

修改 `plastic_promise/memory/pipeline.py` 的 `_process_embedded_to_migrate` 方法。

当前代码 (~line 255)：
```python
if vec:
    engine._memories[stored.memory_id]["_vector"] = vec
```

替换为：
```python
if vec:
    engine._memories[stored.memory_id]["_vector"] = vec
    # Dual-write to LanceDB for persistent vector storage
    try:
        ldb = getattr(engine, '_ldb', None)
        if ldb is not None:
            ldb.insert(
                memory_id=stored.memory_id,
                vector=vec,
                text=record.get("content", ""),
                tier=record.get("tier", "L1"),
                category=record.get("category", "other"),
                scope=record.get("scope", "global"),
            )
    except Exception as e:
        logging.warning("LanceDB dual-write failed for %s: %s", stored.memory_id, e)
```

- [ ] **Step 2: 提交**

```bash
git add plastic_promise/memory/pipeline.py
git commit -m "feat: pipeline dual-write vectors to LanceDB on migration"
```

---

### Task 3: ContextEngine 集成 — 混合融合检索

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (3 处改动)

**Interfaces:**
- Consumes: `LanceDBStore` from Task 1
- Produces: `_hybrid_fuse()` method, modified `supply()` flow

- [ ] **Step 1: 在 __init__ 中初始化 LanceDBStore**

在 `ContextEngine.__init__` 末尾添加 (~line 218):

```python
# Initialize LanceDB vector store
self._ldb: Optional[object] = None
try:
    from plastic_promise.core.lancedb_store import LanceDBStore
    from plastic_promise.core.embedder import get_embedder
    ldb_path = os.environ.get("PLASTIC_LANCEDB_PATH",
                               os.path.join(os.path.dirname(db_path or "plastic_memory.db"),
                                            "plastic_memory.lancedb"))
    embedder = get_embedder(fallback_on_error=True)
    self._ldb = LanceDBStore(ldb_path, embedder)
    # Backfill any memories that bypassed the pipeline
    self._ldb.backfill(self)
    logging.info("ContextEngine: LanceDBStore ready (backfill complete)")
except Exception as e:
    logging.warning("ContextEngine: LanceDBStore init failed — vector search disabled: %s", e)
    self._ldb = None
```

- [ ] **Step 2: 替换 _vector_retrieval 使用 LanceDB**

当前 `_vector_retrieval` (~line 823-840) 用内存暴力搜索。替换为:

```python
    def _vector_retrieval(self, task_vector: list[float]) -> list[tuple]:
        """Semantic vector retrieval via LanceDB ANN search.

        Falls back to empty list if LanceDB is unavailable.
        """
        if self._ldb is None:
            return []
        try:
            raw_results = self._ldb.search(
                vector=task_vector,
                k=20,
                scope=getattr(self, '_domain_hint', None),
            )
            # Convert LanceDB results to internal tuple format
            return [(mid, score, text[:300], "vector") for mid, score, text, _tier, _scope in raw_results]
        except Exception as e:
            logging.warning("_vector_retrieval LanceDB failed, returning empty: %s", e)
            return []
```

- [ ] **Step 3: 添加 _hybrid_fuse 方法**

在 `_vector_retrieval` 方法后添加新方法:

```python
    def _hybrid_fuse(
        self,
        vector_results: list[tuple],
        text_results: list[tuple],
        vector_weight: float = 0.7,
    ) -> list[tuple]:
        """Fuse vector and text retrieval results with weighted combination.

        Formula: fusedScore = vectorScore * 0.7 + textScore * 0.3
        BM25 high-score bypass: if text score >= 0.75, promote via 0.9 weight.

        Args:
            vector_results: [(id, score, content, source), ...] from LanceDB.
            text_results: [(id, score, content, source), ...] from _text_retrieval.
            vector_weight: Weight for vector scores (default 0.7).

        Returns:
            Fused result list sorted by combined score descending.
        """
        combined: dict[str, tuple[float, str, str]] = {}

        # Vector channel: weight × vector_weight
        for mid, score, content, source in vector_results:
            combined[mid] = (score * vector_weight, content, source)

        # Text channel: weight × (1 - vector_weight), with BM25 bypass
        text_weight = 1.0 - vector_weight
        for mid, score, content, source in text_results:
            w = score * text_weight
            # BM25 high-score bypass: keyword results >= 0.75 override semantic
            if score >= 0.75:
                w = max(w, score * 0.9)
            if mid in combined:
                existing_score, existing_content, existing_source = combined[mid]
                combined[mid] = (max(existing_score, w), existing_content, existing_source)
            else:
                combined[mid] = (w, content, source)

        return [(mid, score, content, source)
                for mid, (score, content, source) in
                sorted(combined.items(), key=lambda x: x[1][0], reverse=True)]
```

- [ ] **Step 4: 修改 supply() 接入混合融合**

修改 `supply()` 中 Phase 1-2 (~line 459-462):

当前:
```python
vector_results = self._vector_retrieval(task_vector) if any(v != 0.0 for v in task_vector) else []

# Phase 2: 分层融合 — 细权重最高，粗作为补充
all_results = self._layered_fuse(graph_results, text_results, vector_results)
```

替换为:
```python
vector_results = self._vector_retrieval(task_vector) if any(v != 0.0 for v in task_vector) else []

# Phase 2: Hybrid fusion (vector + text) then layer with graph
if vector_results:
    fused_results = self._hybrid_fuse(vector_results, text_results, vector_weight=0.7)
else:
    # No vector available (Ollama down / zero vector) — use text only
    fused_results = [(mid, score * 0.8, content, source) for mid, score, content, source in text_results]
all_results = self._layered_fuse(graph_results, fused_results, [])
```

并在 audit_metadata 中添加向量检索状态 (~line 499):

```python
pack.audit_metadata = {
    "engine_version": "0.1.0-py",
    "task_type": task_type,
    "principle_injection_count": str(len(activated)),
    "graph_nodes": str(len(self._graph_nodes)),
    "graph_edges": str(len(self._graph_edges)),
    "memory_pool_size": str(len(self._memories)),
    "vector_search": "active" if vector_results else "fallback_text_only",
    "ldb_rows": str(self._ldb.count_rows()) if self._ldb else "0",
}
```

- [ ] **Step 5: 提交**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: hybrid fusion — LanceDB vector + text retrieval with 0.7/0.3 weighting"
```

---

### Task 4: E2E 验证 — 向量检索可用性

**Files:**
- Create: `tests/test_lancedb_store.py`

**Interfaces:**
- Consumes: `LanceDBStore` (Task 1), modified `ContextEngine` (Task 3)

- [ ] **Step 1: 写 LanceDBStore 单元测试**

```python
"""Tests for LanceDBStore — creation, insert, search, backfill."""
import os
import shutil
import tempfile
import pytest

from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.core.lancedb_store import LanceDBStore, EMB_DIM, TABLE_NAME


@pytest.fixture
def tmp_ldb_path():
    path = tempfile.mkdtemp(prefix="pp_test_ldb_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def store(tmp_ldb_path):
    embedder = FallbackEmbedder(dim=EMB_DIM)
    return LanceDBStore(tmp_ldb_path, embedder)


class TestLanceDBStoreCreation:
    def test_creates_table_on_init(self, store):
        assert store.count_rows() == 0
        assert store._table is not None

    def test_reopens_existing_table(self, tmp_ldb_path):
        embedder = FallbackEmbedder(dim=EMB_DIM)
        s1 = LanceDBStore(tmp_ldb_path, embedder)
        s1.insert("m1", [0.1] * EMB_DIM, "test text", "L1", "test", "global")
        del s1
        s2 = LanceDBStore(tmp_ldb_path, embedder)
        assert s2.count_rows() == 1


class TestLanceDBStoreCRUD:
    def test_insert_and_count(self, store):
        store.insert("m1", [0.1] * EMB_DIM, "hello world", "L1", "test", "global")
        assert store.count_rows() == 1

    def test_insert_duplicate_skip(self, store):
        store.insert("m1", [0.1] * EMB_DIM, "hello", "L1", "test", "global")
        store.insert("m1", [0.2] * EMB_DIM, "world", "L1", "test", "global")
        assert store.count_rows() == 1

    def test_delete(self, store):
        store.insert("m1", [0.1] * EMB_DIM, "hello", "L1", "test", "global")
        store.delete("m1")
        assert store.count_rows() == 0

    def test_update_is_upsert(self, store):
        store.insert("m1", [0.1] * EMB_DIM, "hello", "L1", "test", "global")
        store.update("m1", [0.9] * EMB_DIM, "updated", "L3", "other", "agent:1")
        assert store.count_rows() == 1


class TestLanceDBSearch:
    def test_vector_search_returns_results(self, store):
        v1 = [float(i % 10) / 10.0 for i in range(EMB_DIM)]
        store.insert("m1", v1, "first memory", "L1", "test", "global")
        results = store.search(v1, k=5)
        assert len(results) >= 1
        # First result should be the inserted one (highest similarity)
        assert results[0][0] == "m1"

    def test_search_empty_table_returns_empty(self, store):
        results = store.search([0.5] * EMB_DIM, k=5)
        assert results == []
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest tests/test_lancedb_store.py -v
```

Expected: 7 passed

- [ ] **Step 3: 提交**

```bash
git add tests/test_lancedb_store.py
git commit -m "test: LanceDBStore — creation, CRUD, vector search"
```

---

### Task 5: Dashboard API 端点

**Files:**
- Modify: `plastic_promise/mcp/server.py:873-899`

**Interfaces:**
- Consumes: `ContextEngine` (existing singleton)
- Produces: `/api/stats`, `/api/issues`, `/api/trust` JSON endpoints

- [ ] **Step 1: 在 run_sse() 中添加 3 个 API 端点**

在 `health()` 函数之后，`shutdown()` 之前添加：

```python
    async def api_stats(request):
        """Return memory pool + body system statistics."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            stats_raw = engine.memory_stats_json()
            stats = _json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
            from plastic_promise.core.constants import DIGITAL_BODY_SYSTEMS
            systems = {}
            for k, v in DIGITAL_BODY_SYSTEMS.items():
                systems[k] = {
                    "name": v.get("name", k),
                    "maturity": v.get("maturity", 0.0),
                }
            return JSONResponse({
                "memory": stats,
                "body_systems": systems,
                "uptime": round(_time.time() - start_time, 1),
                "version": "0.1.0",
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_issues(request):
        """Return active issue list."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.management import handle_issue_list
            result = await handle_issue_list(engine, {})
            data = _json.loads(result[0].text) if result else {"issues": []}
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_trust(request):
        """Return trust/defense status."""
        import json as _json
        from starlette.responses import JSONResponse
        try:
            engine = get_engine()
            from plastic_promise.mcp.tools.audit_defense import handle_defense
            result = await handle_defense(engine, {"action": "get"})
            data = _json.loads(result[0].text) if result else {}
            # Add audit summary
            try:
                audit_result = await handle_audit_run(engine, {"action": "report"})
                audit_data = _json.loads(audit_result[0].text) if audit_result else {}
            except Exception:
                audit_data = {"message": "No audit run yet"}
            data["audit_summary"] = audit_data
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 2: 注册路由**

修改 `app = Starlette(routes=[...])` (~line 886):

```python
    app = Starlette(routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/events", endpoint=handle_events, methods=["GET"]),
        Route("/notify", endpoint=handle_notify, methods=["POST"]),
        Route("/health", endpoint=health),
        Route("/api/stats", endpoint=api_stats),
        Route("/api/issues", endpoint=api_issues),
        Route("/api/trust", endpoint=api_trust),
    ], on_shutdown=[shutdown])
```

- [ ] **Step 3: 提交**

```bash
git add plastic_promise/mcp/server.py
git commit -m "feat: dashboard API endpoints — /api/stats, /api/issues, /api/trust"
```

---

### Task 6: Dashboard HTML 页面

**Files:**
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Consumes: API endpoints from Task 5
- Produces: `GET /dashboard` returning self-contained HTML

- [ ] **Step 1: 在 api_trust 之后添加 dashboard 路由 handler**

```python
    async def dashboard(request):
        """Serve the monitoring dashboard HTML page."""
        from starlette.responses import HTMLResponse
        html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plastic Promise Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px}
h1{font-size:20px;margin-bottom:4px}
.status{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}
.status-ok{background:#3fb950}.status-err{background:#f85149}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h3{font-size:13px;color:#8b949e;margin-bottom:8px;text-transform:uppercase}
.card .value{font-size:28px;font-weight:700}
.bar{margin-top:8px;height:6px;border-radius:3px;background:#21262d;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.bar-high{background:#3fb950}.bar-mid{background:#d29922}.bar-low{background:#f85149}
.section{margin-top:24px}
.section h2{font-size:16px;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #21262d;font-size:13px}
th{color:#8b949e}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px}
.tag-ok{background:#1b3823;color:#3fb950}.tag-warn{background:#332b00;color:#d29922}
.footer{color:#484f58;font-size:12px;margin-top:32px}
</style>
</head>
<body>
<h1><span class="status status-ok" id="status-dot"></span>Plastic Promise Dashboard <small style="color:#8b949e">v0.1.0</small></h1>

<div class="grid" id="stats-grid">
  <div class="card"><h3>Memories</h3><div class="value" id="mem-total">-</div></div>
  <div class="card"><h3>Decaying</h3><div class="value" id="mem-decaying">-</div></div>
  <div class="card"><h3>Trust Score</h3><div class="value" id="trust-score">-</div></div>
  <div class="card"><h3>Active Issues</h3><div class="value" id="issues-count">-</div></div>
</div>

<div class="section"><h2>Body Systems</h2>
<div id="body-systems"></div>
</div>

<div class="section"><h2>Defense</h2>
<div id="defense-info"></div>
</div>

<div class="section"><h2>Audit</h2>
<div id="audit-info"></div>
</div>

<div class="footer">Auto-refreshes every 5s &middot; Plastic Promise</div>

<script>
async function fetchJSON(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; }
  catch { return null; }
}

function barColor(v) { return v>=0.7?'bar-high':v>=0.5?'bar-mid':'bar-low'; }

async function refresh() {
  const [stats, issues, trust] = await Promise.all([
    fetchJSON('/api/stats'), fetchJSON('/api/issues'), fetchJSON('/api/trust')
  ]);

  if (!stats) { document.getElementById('status-dot').className='status status-err'; return; }
  document.getElementById('status-dot').className='status status-ok';

  document.getElementById('mem-total').textContent = stats.memory?.total || 0;
  document.getElementById('mem-decaying').textContent = stats.memory?.decaying || 0;

  // Body systems
  const systems = stats.body_systems || {};
  let sysHTML = '';
  for (const [key, s] of Object.entries(systems)) {
    const pct = Math.round(s.maturity*100);
    sysHTML += `<div style="display:flex;align-items:center;margin-bottom:6px">
      <span style="width:140px;font-size:13px">${s.name}</span>
      <div class="bar" style="flex:1"><div class="bar-fill ${barColor(s.maturity)}" style="width:${pct}%"></div></div>
      <span style="width:40px;text-align:right;font-size:13px">${pct}%</span></div>`;
  }
  document.getElementById('body-systems').innerHTML = sysHTML;

  // Trust
  if (trust) {
    document.getElementById('trust-score').textContent = (trust.trust||0).toFixed(2);
    const tier = trust.tier || 'unknown';
    document.getElementById('defense-info').innerHTML = `
      <span class="tag tag-${tier==='high'?'ok':'warn'}">${tier} tier</span>
      <span style="margin-left:12px">Target: ${trust.target||'default'}</span>`;
  }

  // Issues
  if (issues) {
    const count = issues.count || issues.issues?.length || 0;
    document.getElementById('issues-count').textContent = count;
  }

  // Audit
  if (trust?.audit_summary) {
    document.getElementById('audit-info').innerHTML = '<pre style="font-size:12px;color:#8b949e">' +
      JSON.stringify(trust.audit_summary, null, 2).slice(0, 500) + '</pre>';
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
        return HTMLResponse(html)
```

- [ ] **Step 2: 注册 /dashboard 路由**

在 `app = Starlette(routes=[...])` 中添加：

```python
        Route("/dashboard", endpoint=dashboard),
```

- [ ] **Step 3: 提交**

```bash
git add plastic_promise/mcp/server.py
git commit -m "feat: dashboard HTML page — memory, body systems, trust, audit"
```

---

### Task 7: E2E 验证

**Files:** 无新文件

- [ ] **Step 1: 验证向量检索功能**

启动服务器后运行：

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder
e = ContextEngine()
# Store a memory
e.register_memory({'content': '项目使用Python和LanceDB做向量检索', 'memory_type': 'experience', 'source': 'user'})
# Verify LanceDB backfill
print('LDB rows:', e._ldb.count_rows() if e._ldb else 'N/A')
# Semantic recall
e.set_current_time('2026-06-30T12:00:00')
pack = e.supply('向量数据库技术选型', [0.0]*1024, 'architecture')
print('Core items:', len(pack.core))
for item in pack.core[:3]:
    print(f'  [{item.relevance:.2f}] {item.content[:80]}')
"
```

Expected: 至少 1 条 core item，`vector_search: "fallback_text_only"`（因为用了零向量），但文本匹配能找到。

- [ ] **Step 2: 完整流水线验证（需要 Ollama）**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder
e = ContextEngine()
# Store with real embedding
emb = get_embedder()
vec = emb.embed('这是一个关于Python向量数据库的测试记忆')
e.register_memory({'content': '这是一个关于Python向量数据库的测试记忆', 'memory_type': 'experience', 'source': 'user'})
# Trigger backfill manually
if e._ldb:
    e._ldb.backfill(e)
# Semantic search with real vector
query_vec = emb.embed('向量数据库')
e.set_current_time('2026-06-30T12:00:00')
pack = e.supply('向量数据库', query_vec, 'architecture')
print('Vector search:', pack.audit_metadata.get('vector_search'))
print('LDB rows:', pack.audit_metadata.get('ldb_rows'))
print('Core items:', len(pack.core))
for item in pack.core[:3]:
    print(f'  [{item.relevance:.2f}] {item.content[:80]}')
"
```

Expected: `vector_search: "active"`, LDB rows >= 1, core items 包含向量数据库相关记忆。

- [ ] **Step 3: 验证 Dashboard**

```bash
# Start SSE server in background
python -m plastic_promise.mcp.server --sse 9020 &
sleep 2
# Test API endpoints
curl -s http://127.0.0.1:9020/health | python -m json.tool
curl -s http://127.0.0.1:9020/api/stats | python -m json.tool
curl -s http://127.0.0.1:9020/api/trust | python -m json.tool
# Fetch dashboard HTML
curl -s http://127.0.0.1:9020/dashboard | head -5
# Cleanup
kill %1
```

Expected: `/health` 返回 `{"status":"ok",...}`, `/api/stats` 返回 memory 统计, `/api/trust` 返回信任分, `/dashboard` 返回 HTML。

- [ ] **Step 4: 提交（如有修改）**

```bash
git status
# 如有修改则 add + commit
```

---

### Task 8: 完成清理

- [ ] **Step 1: 更新 GOAL.md 标记 TODO #6, #7 状态**

```bash
git add GOAL.md
git commit -m "docs: mark TODO #6 (vector search) and #7 (dashboard) as done"
```

- [ ] **Step 2: 存储完成记忆**

```
memory_store(content="TODO #6 DONE: LanceDB vector store + hybrid fusion retrieval. 
  LanceDBStore class (ANN + FTS + backfill), pipeline dual-write, 
  ContextEngine._hybrid_fuse(vectorScore × 0.7 + textScore × 0.3), 
  graceful degradation when Ollama unavailable.",
  memory_type="experience", tags=["task:done", "assignee:claude"])

memory_store(content="TODO #7 DONE: HTML dashboard at /dashboard. 
  Self-contained page polling /api/stats + /api/issues + /api/trust. 
  Memory pool, body system maturity bars, trust score, audit summary.",
  memory_type="experience", tags=["task:done", "assignee:claude"])
```

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "feat: TODO #6 + #7 complete — LanceDB vector retrieval + dashboard"
```
