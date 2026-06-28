# Sub-Project A: Embedder + LanceDB + MCP Handlers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the end-to-end memory pipeline — Ollama embedding → Rust hybrid retrieval → MCP tool calls return real results.

**Architecture:** 4 sequential tasks: LanceDB Rust impl → Python Embedder → MCP handlers → End-to-end verification. LanceDB and Embedder are independent (can run in parallel), MCP handlers depend on both.

**Tech Stack:** Rust (PyO3 0.20 + ABI3, lancedb 0.30, rusqlite 0.31), Python 3.13 (Ollama API via requests), mxbai-embed-large (1024 dim)

## Global Constraints

- All new Python files must have full type annotations and docstrings
- Every Rust `pub` item must have `///` doc comments
- All `cargo check` must pass with zero errors before commit
- Each task ends with a single git commit with descriptive message
- MSVC toolchain: `stable-x86_64-pc-windows-msvc` with PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
- Ollama must be running at `http://localhost:11434` with `mxbai-embed-large` loaded
- MCP handlers return `list[TextContent]` with JSON body

---

### Task 1: LanceDB Rust implementation (real, not stub)

**Files:**
- Modify: `rust/context-engine-core/Cargo.toml` — add lancedb dep
- Modify: `rust/context-engine-core/src/storage/lancedb_impl.rs` — real impl

**Interfaces:**
- Consumes: `storage::{VectorIndex, FtsIndex, SearchFilter, IndexMetadata, EMB_DIM}`
- Produces: Working `LanceDbStore` implementing both `VectorIndex` and `FtsIndex`

- [ ] **Step 1: Add lancedb to Cargo.toml**

Read Cargo.toml first, then add:
```toml
lancedb = "0.30"
```

Run: `cargo check 2>&1 | tail -5`
Expected: may fail with lancedb compilation — that's OK, try with specific features

If `lancedb` 0.30 fails to compile on Windows MSVC (it likely needs protobuf/tokio), **fall back to approach B**: use a simpler vector implementation with cosine similarity on in-memory arrays, and keep LanceDB as a future upgrade path. Add a comment noting this.

For the fallback: implement `VectorIndex` and `FtsIndex` using:
- Vector: in-memory `Vec<Vec<f32>>` + brute-force cosine similarity (fast enough for <1000 vectors)
- FTS: simple word-overlap-based scoring (same as the old `text_retrieval()`)

- [ ] **Step 2: Write the real/fallback impl**

If lancedb crate compiles: use it. If not: write a self-contained impl.

```rust
//! Vector and FTS index implementation.
//!
//! Currently uses in-memory brute-force search (fast for <10K vectors).
//! Upgrade path: replace with lancedb crate when protobuf/tokio deps resolve.

use std::collections::HashMap;
use crate::storage::{FtsIndex, IndexMetadata, SearchFilter, VectorIndex, EMB_DIM};

pub struct LanceDbStore {
    /// In-memory vector store: memory_id -> vector
    vectors: HashMap<String, Vec<f32>>,
    /// In-memory text store for FTS: memory_id -> text
    texts: HashMap<String, String>,
    /// Metadata per entry
    metadata: HashMap<String, IndexMetadata>,
}

impl LanceDbStore {
    pub fn open<P: AsRef<std::path::Path>>(path: P) -> Result<Self, String> {
        let p = path.as_ref();
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("mkdir: {}", e))?;
        }
        Ok(Self { vectors: HashMap::new(), texts: HashMap::new(), metadata: HashMap::new() })
    }

    fn cosine_similarity(a: &[f32], b: &[f32]) -> f64 {
        let dot: f64 = a.iter().zip(b).map(|(x,y)| (*x as f64) * (*y as f64)).sum();
        let na: f64 = a.iter().map(|x| (*x as f64).powi(2)).sum::<f64>().sqrt();
        let nb: f64 = b.iter().map(|x| (*x as f64).powi(2)).sum::<f64>().sqrt();
        if na < 1e-9 || nb < 1e-9 { return 0.0; }
        (dot / (na * nb)).clamp(-1.0, 1.0)
    }

    fn bm25_score(query: &str, text: &str) -> f64 {
        let q_words: Vec<&str> = query.split_whitespace().collect();
        let t_lower = text.to_lowercase();
        let hits = q_words.iter().filter(|w| t_lower.contains(*w)).count();
        if q_words.is_empty() { return 0.0; }
        hits as f64 / q_words.len() as f64
    }
}
```

Implement all trait methods: `search`, `insert`, `update`, `delete` for both VectorIndex and FtsIndex. Vector search: compute cosine to all stored vectors, sort, return top-k. FTS search: compute BM25-like score, sort, return top-k. Both respect SearchFilter (scope, tier, category).

Keep existing unit tests but update them to test real behavior:
```rust
#[test]
fn test_vector_search_returns_results() {
    let tmp = std::env::temp_dir().join("pp_test_lancedb_real");
    let _ = std::fs::remove_dir_all(&tmp);
    let mut store = LanceDbStore::open(&tmp).unwrap();
    let meta = IndexMetadata { memory_id: "m1".into(), tier: "working".into(), category: "fact".into(), scope: "global".into() };
    let v = vec![1.0f32; 1024];
    store.insert("m1", &v, &meta).unwrap();
    let results = store.search(&v, 5, &SearchFilter::default()).unwrap();
    assert!(!results.is_empty());
    assert_eq!(results[0].0, "m1");
    assert!(results[0].1 > 0.99); // self-similarity
    std::fs::remove_dir_all(&tmp).ok();
}
```

- [ ] **Step 3: Run cargo check and tests**

```bash
cd rust/context-engine-core && cargo check 2>&1 | tail -5
cargo test --lib storage::lancedb_impl 2>&1
```
Expected: zero errors, tests pass

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: LanceDB real impl — in-memory vector cosine + FTS word-overlap search"
```

---

### Task 2: Python Embedder (Ollama + factory)

**Files:**
- Create: `plastic_promise/embedder.py`

**Interfaces:**
- Consumes: `requests`, `os`
- Produces: `get_embedder() -> Embedder`, `OllamaEmbedder`, `Embedder` base class

- [ ] **Step 1: Write the test**

Create a test file (inline in embedder.py as `if __name__ == "__main__":` or as a separate test script):

```python
# test_embedder.py (temporary, run once)
import sys; sys.path.insert(0, '.')
from plastic_promise.embedder import get_embedder

embedder = get_embedder()
v = embedder.embed("测试文本")
assert len(v) == 1024, f"Expected 1024, got {len(v)}"
assert isinstance(v[0], float)
print(f"OK: dim={len(v)}, model={embedder.model_name}")
```
Run: `python test_embedder.py`
Expected: `OK: dim=1024, model=mxbai-embed-large`

- [ ] **Step 2: Write plastic_promise/embedder.py**

```python
"""Plastic Promise Embedder — text-to-vector with provider abstraction.

Default: Ollama with mxbai-embed-large (1024 dim).
Env overrides:
  EMBEDDER_PROVIDER=ollama|openai|jina  (default: ollama)
  OLLAMA_HOST=http://localhost:11434
  EMBEDDER_MODEL=mxbai-embed-large
"""

import os
from abc import ABC, abstractmethod

import requests


class Embedder(ABC):
    """Abstract text-to-vector embedder."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Convert text to an embedding vector."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed multiple texts."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier."""


class OllamaEmbedder(Embedder):
    """Local Ollama embedding provider.

    Default model: mxbai-embed-large (1024 dim, MTEB top-tier, multilingual).
    """

    def __init__(self, host: str | None = None, model: str | None = None) -> None:
        self._host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self._model = model or os.getenv("EMBEDDER_MODEL", "mxbai-embed-large")

    def embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dim(self) -> int:
        return 1024  # mxbai-embed-large

    @property
    def model_name(self) -> str:
        return self._model


class OpenAIEmbedder(Embedder):
    """OpenAI embedding fallback (text-embedding-3-small, 1536 dim)."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = model or os.getenv("EMBEDDER_MODEL", "text-embedding-3-small")

    def embed(self, text: str) -> list[float]:
        from openai import OpenAI
        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        client = OpenAI(api_key=self._key)
        resp = client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]

    @property
    def dim(self) -> int:
        return 1536

    @property
    def model_name(self) -> str:
        return self._model


def get_embedder() -> Embedder:
    """Factory: returns embedder based on EMBEDDER_PROVIDER env var.

    Default: OllamaEmbedder(mxbai-embed-large).
    Set EMBEDDER_PROVIDER=openai for OpenAI.
    """
    provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()
    if provider == "openai":
        return OpenAIEmbedder()
    return OllamaEmbedder()
```

- [ ] **Step 4: Run test**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.embedder import get_embedder
e = get_embedder()
v = e.embed('测试 Plastic Promise 记忆嵌入')
assert len(v) == 1024
print(f'OK: dim={len(v)}, model={e.model_name}, first3={v[:3]}')
"
```
Expected: `OK: dim=1024, model=mxbai-embed-large, first3=[...]`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: Python Embedder — Ollama mxbai-embed-large + OpenAI fallback + get_embedder factory"
```

---

### Task 3: MCP Memory handlers (7 tools)

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py` — full implementation

**Interfaces:**
- Consumes: `plastic_promise.embedder.get_embedder`, Rust `ContextEngine` via `engine` parameter
- Produces: 7 async handler functions returning `list[TextContent]`

- [ ] **Step 1: Write the handlers**

Read current `plastic_promise/mcp/tools/memory.py` first.

Replace with ~200 lines implementing every handler with real logic:

```python
"""Memory domain MCP tool handlers (7 tools)."""

import json
from typing import Any

from mcp.types import TextContent


async def handle_memory_recall(engine: Any, args: dict) -> list[TextContent]:
    """Hybrid memory retrieval using ContextEngine.supply()."""
    from plastic_promise.embedder import get_embedder
    query = args["query"]
    task_type = args.get("task_type", "general")
    max_results = args.get("max_results", 20)
    scope = args.get("scope", "global")

    embedder = get_embedder()
    vec = embedder.embed(query)
    pack = engine.supply(query, vec, task_type, scope)

    return [TextContent(type="text", text=json.dumps({
        "core": [{"id": i.id, "content": i.content[:300], "relevance": i.relevance,
                  "source": i.source, "freshness": i.freshness}
                 for i in pack.core[:max_results]],
        "related": [{"id": i.id, "content": i.content[:300], "relevance": i.relevance}
                    for i in pack.related[:max_results]],
        "divergent": [{"id": i.id, "content": i.content[:300], "relevance": i.relevance}
                      for i in pack.divergent[:max_results]],
        "activated_principles": pack.activated_principles,
        "audit": pack.audit_metadata,
    }, ensure_ascii=False, indent=2))]


async def handle_memory_store(engine: Any, args: dict) -> list[TextContent]:
    """Store a memory and create its vector index."""
    from plastic_promise.embedder import get_embedder
    import datetime

    content = args["content"]
    memory_type = args.get("memory_type", "experience")
    source = args.get("source", "user")
    scope = args.get("scope", "global")

    # Create MemoryRecord
    memory_id = f"mem_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    record = type(engine).__module__  # will get MemoryRecord from engine context
    # ... use engine.storage.store() with MemoryRecord

    # Embed and index
    embedder = get_embedder()
    vec = embedder.embed(content)

    return [TextContent(type="text", text=json.dumps({
        "stored": True, "memory_id": memory_id,
        "content_preview": content[:200],
        "memory_type": memory_type,
        "vector_dim": len(vec),
    }, ensure_ascii=False))]


async def handle_memory_stats(engine: Any, args: dict) -> list[TextContent]:
    """Return memory pool statistics."""
    scope = args.get("scope")
    stats = engine.storage.stats(scope)
    return [TextContent(type="text", text=json.dumps({
        "total": stats.total,
        "healthy": stats.healthy,
        "decaying": stats.decaying,
        "by_tier": stats.by_tier,
        "by_type": stats.by_type,
        "average_worth": stats.average_worth,
    }, ensure_ascii=False))]


# Also implement: handle_memory_update, handle_memory_forget,
# handle_memory_list, handle_memory_gc — each calling engine.storage methods
```

IMPORTANT: The `handle_memory_store` needs to create a `MemoryRecord` and pass it to `engine.storage.store()`. Since Python has access to Rust structs via PyO3, import and use `context_engine_core.MemoryRecord`.

All handlers must handle errors gracefully with try/except and return error JSON.

- [ ] **Step 3: Verify Python imports**

```bash
python -c "from plastic_promise.mcp.tools.memory import handle_memory_recall, handle_memory_store, handle_memory_stats; print('memory handlers OK')"
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: 7 MCP memory handlers — recall/store/update/forget/stats/list/gc"
```

---

### Task 4: MCP Principles + Audit + Reflection + Management handlers (9 tools)

**Files:**
- Modify: `plastic_promise/mcp/tools/principles.py`
- Modify: `plastic_promise/mcp/tools/audit_defense.py`
- Modify: `plastic_promise/mcp/tools/reflection.py`
- Modify: `plastic_promise/mcp/tools/management.py`

- [ ] **Step 1: Write principles.py (4 handlers)**

```python
async def handle_principle_activate(engine, args):
    task_type = args["task_type"]
    from plastic_promise.core.constants import CORE_PRINCIPLES
    recommendations = {
        "code_generation": [1,3,8,10], "code_review": [1,5,6,9],
        "debugging": [1,5,10], "architecture": [2,7,8],
        "refactoring": [5,6,7], "learning": [1,10,11],
        "collaboration": [2,7,9], "general": [1,2,3,4],
    }
    ids = recommendations.get(task_type, [1,2,3,4])
    principles = [p for p in CORE_PRINCIPLES if p["id"] in ids]
    return [TextContent(type="text", text=json.dumps({
        "task_type": task_type, "activated": principles, "count": len(principles)
    }, ensure_ascii=False, indent=2))]
```

Also implement: `handle_principle_inherit`, `handle_principle_diffuse`, `handle_principle_evaluate` — each with meaningful logic using CORE_PRINCIPLES and PRINCIPLE_INHERITANCE constants.

- [ ] **Step 2: Write audit_defense.py (2 handlers)**

`handle_audit_pre_check`: check action against L0 hard boundaries
`handle_defense_status`: return current defense layer states

- [ ] **Step 3: Write reflection.py (1 handler)**

`handle_feedback_apply`: update MemoryRecord worth counters via `engine.storage`

- [ ] **Step 4: Write management.py (1 handler)**

`handle_system_stats`: aggregate all stats in one JSON response

- [ ] **Step 5: Verify all imports**

```bash
python -c "
from plastic_promise.mcp.tools.principles import handle_principle_activate
from plastic_promise.mcp.tools.audit_defense import handle_audit_pre_check
from plastic_promise.mcp.tools.reflection import handle_feedback_apply
from plastic_promise.mcp.tools.management import handle_system_stats
print('all 9 handlers OK')
"
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: 9 MCP handlers — principles(4) + audit(2) + reflection(1) + management(1)"
```

---

### Task 5: Wire Embedder into context_supply + text fallback in HybridRetriever

**Files:**
- Modify: `plastic_promise/mcp/tools/context.py` — add embedder call
- Modify: `rust/context-engine-core/src/retrieval/mod.rs` — add fallback

- [ ] **Step 1: Update handle_context_supply**

The current handler already has the basic structure. Add embedder:

```python
async def handle_context_supply(engine: Any, args: dict) -> list[TextContent]:
    from plastic_promise.embedder import get_embedder
    task_description = args["task_description"]
    task_type = args.get("task_type", "general")
    scope = args.get("scope", "global")

    embedder = get_embedder()
    task_vector = embedder.embed(task_description)

    pack = engine.supply(task_description, task_vector, task_type, scope)
    return [TextContent(type="text", text=pack.to_prompt())]
```

- [ ] **Step 2: Add text fallback in HybridRetriever.retrieve()**

In `retrieval/mod.rs`, update `retrieve()`: when vector search or FTS search returns an error, fall back to scanning `item_lookup` for keyword matches (simple text overlap). This ensures the pipeline works even without LanceDB.

```rust
// After vector_results and bm25_results:
// If vector search returned error, use text fallback
let vector_results = match self.vector.search(query_vector, self.candidate_pool_size, &filter) {
    Ok(r) => r,
    Err(_) => {
        // Text fallback: rank items by keyword overlap
        let mut fallback: Vec<(String, f64)> = item_lookup.iter()
            .filter_map(|(id, (content, _))| {
                let score = content.split_whitespace()
                    .filter(|w| query_text.contains(w))
                    .count() as f64 / query_text.split_whitespace().count().max(1) as f64;
                if score > 0.0 { Some((id.clone(), score)) } else { None }
            })
            .collect();
        fallback.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        fallback
    }
};
```

- [ ] **Step 3: Verify cargo check**

```bash
cd rust/context-engine-core && cargo check 2>&1 | tail -5
```
Expected: zero errors

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: wire embedder into context_supply + text fallback in HybridRetriever"
```

---

### Task 6: End-to-end verification

- [ ] **Step 1: Full Rust compilation**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: zero errors

- [ ] **Step 2: Full Python import chain**

```bash
python -c "
from plastic_promise.embedder import get_embedder
from plastic_promise.core import ContextEngine, CORE_PRINCIPLES
from plastic_promise.memory import RecMem
from plastic_promise.loop import SoulLoop
from plastic_promise.mcp.tools.memory import handle_memory_recall, handle_memory_store
from plastic_promise.mcp.tools.principles import handle_principle_activate
from plastic_promise.mcp.tools.context import handle_context_supply
print('Full chain OK')
"
```

- [ ] **Step 3: Embedding flow test**

```bash
python -c "
from plastic_promise.embedder import get_embedder
e = get_embedder()
v = e.embed('Plastic Promise 是一个约定工程的AI行为治理系统')
print(f'Embedding OK: dim={len(v)}')
assert len(v) == 1024
"
```

- [ ] **Step 4: Push**

```bash
git push origin main
```
