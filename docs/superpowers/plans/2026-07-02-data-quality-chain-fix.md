# Data Quality Chain — Complete Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 broken links in the data quality chain (embedder recovery, pipeline zero-vector guard, principle injection, Rust dispatch) + add data quality scanner + repair historical corruption + daemon self-healing.

**Architecture:** Layer-by-layer bottom-up repair. Phase 1 embeds runtime fallback into the embedder singleton so Ollama takes over when sentence-transformers fails. Phase 2 adds zero-vector guards at the pipeline's embed and migrate stages. Phase 3 enriches principle activation to carry full content. Phase 4 reconnects the Rust engine. Phase 5 fills the scanner blind spot. Phase 6 repairs existing LanceDB corruption. Phase 7 wraps MCP+Daemon in a reliable startup script.

**Tech Stack:** Python 3.13, Ollama (mxbai-embed-large), Rust/pyo3, LanceDB, SQLite

## Global Constraints

- Embedder default provider changes from `"local"` to `"ollama"` — no HuggingFace download dependency
- `CachedEmbedder.embed()` detects zero-vector output and falls back to Ollama at runtime
- `MemoryPipeline._process_classified_to_embedded()` defers items on embed failure rather than filling zeros
- `MemoryPipeline._process_embedded_to_migrate()` rejects zero vectors before LanceDB write
- `_activate_principles()` returns `List[dict]` with name+content+domain — downstream code receives full principle text
- `scan_data_quality` checks: embedder type, zero-vector ratio, principle injection, Rust health, pipeline buffer
- Rust `.pyd` must exist at `rust/context-engine-core/target/release/context_engine_core.pyd`
- All existing tests must pass after every task
- CI guard (`tests/test_boundary.py`) must remain green

---

## File Structure

| File | Role |
|------|------|
| `plastic_promise/core/embedder.py` | **Modify**: runtime fallback in `CachedEmbedder`, add `reset_embedder()`, change default provider |
| `plastic_promise/memory/pipeline.py` | **Modify**: defer-on-fail in `_process_classified_to_embedded`, zero-vector guard in `_process_embedded_to_migrate` |
| `plastic_promise/core/context_engine.py` | **Modify**: `_activate_principles()` returns dicts, uncomment Rust dispatch |
| `rust/context-engine-core/src/context_engine.rs` | **Modify**: add `#[staticmethod] new_with_backends` for Python |
| `plastic_promise/cron/scan_data_quality.py` | **Create**: 6-dimension data quality scanner |
| `daemons/maintenance_daemon.py` | **Modify**: register `scan_data_quality` in main loop |
| `scripts/repair_zero_vectors.py` | **Create**: one-shot LanceDB zero-vector repair |
| `scripts/start_all.bat` | **Create**: MCP server + daemon startup with health-wait |

---

### Task 1: Embedder — Runtime fallback when delegate returns zero vectors

**Files:**
- Modify: `plastic_promise/core/embedder.py` (lines 88-108, 349-414)

**Interfaces:**
- Consumes: `OllamaEmbedder`, `FallbackEmbedder`, `LocalSentenceEmbedder`
- Produces: `CachedEmbedder.embed()` with runtime Ollama fallback, `reset_embedder()` public function

- [ ] **Step 1: Add runtime fallback in `CachedEmbedder.embed()`**

In `plastic_promise/core/embedder.py`, replace the `embed()` method (lines 88-108):

```python
def embed(self, text: str) -> list[float]:
    if self._max_size <= 0:
        return self._delegate.embed(text)
    key = self._key(text)
    now = time.time()
    with self._lock:
        if key in self._cache:
            vec, ts = self._cache[key]
            if now - ts < self._ttl:
                self._hits += 1
                return vec
            del self._cache[key]
    self._misses += 1
    vec = self._delegate.embed(text)

    # Runtime fallback: if delegate returns zero vectors and is not
    # already FallbackEmbedder, try Ollama as live recovery path.
    # This detects lazy-init failures (e.g., LocalSentenceEmbedder
    # constructor succeeded but _lazy_load() failed at embed time).
    if vec and not any(v != 0.0 for v in vec):
        if not isinstance(self._delegate, FallbackEmbedder):
            import logging
            _log = logging.getLogger("plastic-promise.embedder")
            _log.warning(
                "CachedEmbedder: delegate %s returned zero vector, "
                "attempting runtime fallback to Ollama",
                type(self._delegate).__name__,
            )
            try:
                ollama_vec = OllamaEmbedder().embed(text)
                if ollama_vec and any(v != 0.0 for v in ollama_vec):
                    _log.info(
                        "CachedEmbedder: Ollama runtime fallback succeeded, "
                        "switching delegate permanently"
                    )
                    self._delegate = OllamaEmbedder()
                    vec = ollama_vec
            except Exception as e:
                _log.warning(
                    "CachedEmbedder: Ollama runtime fallback also failed: %s", e
                )

    with self._lock:
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[key] = (vec, now)
    return vec
```

- [ ] **Step 2: Add `reset_embedder()` public function**

Add after `get_embedder()` (before line 349 or after line 414):

```python
def reset_embedder():
    """Clear the embedder singleton so the next call to get_embedder() re-probes.

    Use when: Ollama becomes available after a FallbackEmbedder lock-in,
    or after deploying a new embedding model.
    """
    global _embedder_singleton
    with _embedder_lock:
        _embedder_singleton = None
    logging.getLogger("plastic-promise.embedder").info(
        "Embedder singleton reset — will re-probe on next get_embedder()"
    )
```

- [ ] **Step 3: Change default provider from `"local"` to `"ollama"`**

In `get_embedder()`, change line 374:

```python
# OLD:
provider = os.getenv("EMBEDDER_PROVIDER", "local").lower()

# NEW:
provider = os.getenv("EMBEDDER_PROVIDER", "ollama").lower()
```

- [ ] **Step 4: Verify embedder works with Ollama**

```bash
python -c "
from plastic_promise.core.embedder import get_embedder, reset_embedder
reset_embedder()
e = get_embedder()
v = e.embed('hello world')
print(f'dim={len(v)}, non_zero={sum(1 for x in v if x != 0)}')
assert len(v) == 1024
assert any(x != 0.0 for x in v)
print('OK')
"
```

Expected: `dim=1024, non_zero=1024` followed by `OK`.

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/embedder.py
git commit -m "feat(embedder): runtime Ollama fallback on zero vectors + reset_embedder() + default to ollama"
```

---

### Task 2: Pipeline — Defer on embed failure instead of zero-fill

**Files:**
- Modify: `plastic_promise/memory/pipeline.py` (lines 288-317, 319-390)

**Interfaces:**
- Consumes: `MemoryPipeline._process_classified_to_embedded()`, `_process_embedded_to_migrate()`
- Produces: embed failure defers items (stays in `classified` + tag `embed:deferred`), migrate gate rejects zero vectors

- [ ] **Step 1: Defer on embed failure in `_process_classified_to_embedded()`**

In `plastic_promise/memory/pipeline.py`, replace lines 300-311.

Note: `skip_set` is populated from items with `skip_embed=True` (e.g., `auto_inject` structured content). In normal operation this set is empty or small. When non-empty, the defer-on-failure logic tags ALL items in the batch (including skip_set items) with `embed:deferred` and leaves them in `classified` stage. The skip_set items will be re-processed with zero vectors on the next cycle — which is intentional (they were marked skip_embed for a reason).

```python
# OLD (lines 300-311):
            try:
                if skip_set:
                    vectors = []
                    for mid, r in batch:
                        if mid in skip_set:
                            vectors.append([0.0] * self.embedder.dim)
                        else:
                            vectors.append(self.embedder.embed(r["content"]))
                else:
                    vectors = self.embedder.embed_batch(contents)
            except Exception:
                vectors = [[0.0] * self.embedder.dim for _ in batch]

# NEW:
            try:
                if skip_set:
                    vectors = []
                    for mid, r in batch:
                        if mid in skip_set:
                            vectors.append([0.0] * self.embedder.dim)
                        else:
                            vectors.append(self.embedder.embed(r["content"]))
                else:
                    vectors = self.embedder.embed_batch(contents)
            except Exception as e:
                logging.warning(
                    "Embed batch failed, deferring %d items (skip_set=%d): %s",
                    len(batch), len(skip_set), e
                )
                for _mid, record in batch:
                    tags = record.setdefault("tags", [])
                    if "embed:deferred" not in tags:
                        tags.append("embed:deferred")
                continue  # stay in classified stage, retry next cycle
```

- [ ] **Step 2: Add zero-vector guard in `_process_embedded_to_migrate()`**

In the same file, in `_process_embedded_to_migrate()`, add after line 340 (after `vec = record.get("vector")`):

```python
                # ---- Zero-vector guard: reject fallback embeddings ----
                if vec and not any(v != 0.0 for v in vec):
                    logging.warning(
                        "Zero vector detected for %s, deferring back to classified",
                        mid,
                    )
                    record["tags"] = record.get("tags", [])
                    if "embed:fallback" not in record["tags"]:
                        record["tags"].append("embed:fallback")
                    record["stage"] = "classified"  # rollback
                    continue
```

- [ ] **Step 3: Verify pipeline defers correctly**

```bash
python -c "
from plastic_promise.core.embedder import FallbackEmbedder
from plastic_promise.memory.pipeline import MemoryPipeline
# Create pipeline with FallbackEmbedder (always returns zeros)
fb = FallbackEmbedder(dim=1024)
p = MemoryPipeline(embedder=fb)
# Store a test memory
mid = p.store_urgent('test memory content', memory_type='experience', source='test')
print(f'Stored: {mid}')
# Manually advance to classified
for r in p._buffer.values():
    r['stage'] = 'classified'
# Process — should defer, not fill zeros
result = p.process_pipeline()
stats = p.stats()
print(f'Pipeline result: {result}')
print(f'Buffer stats: {stats}')
# Check that items stayed in classified (not migrated to empty buffer)
assert stats['total'] > 0, 'Items should still be in buffer'
for r in p._buffer.values():
    assert r['stage'] == 'classified', f'Expected classified, got {r[\"stage\"]}'
    assert 'embed:deferred' in r.get('tags', []), 'Missing embed:deferred tag'
print('OK — pipeline defers on embed failure')
"
```

Expected: items stay in `classified` stage with `embed:deferred` tag. Buffer not emptied.

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/memory/pipeline.py
git commit -m "fix(pipeline): defer on embed failure + zero-vector gate before migrate"
```

---

### Task 3: Principle injection — Return full dict with content

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (lines 1732-1737, 74-87 already correct)

**Interfaces:**
- Consumes: `CORE_PRINCIPLES` from `plastic_promise.core.constants`
- Produces: `_activate_principles()` returns `List[dict]` with `name`, `content`, `domain`, `keywords`

- [ ] **Step 1: Change return type of `_activate_principles()`**

In `plastic_promise/core/context_engine.py`, replace lines 1732-1737:

```python
# OLD (lines 1732-1737):
        # Resolve IDs to names
        result = []
        for p in CORE_PRINCIPLES:
            if p["id"] in activated_ids:
                result.append(p["name"])
        return result

# NEW:
        # Resolve IDs to full principle dicts
        result = []
        for p in CORE_PRINCIPLES:
            if p["id"] in activated_ids:
                result.append({
                    "name": p["name"],
                    "content": p["content"],
                    "consequence": p.get("consequence", ""),
                    "domain": p.get("domain", "all"),
                    "keywords": p.get("keywords", ""),
                })
        return result
```

- [ ] **Step 2: Update `ContextPack.to_prompt()` for dict-format principles**

`to_prompt()` at line 74-87 already does reverse-lookup by name. Updated to handle both `str` and `dict` formats:

```python
# In ContextPack.to_prompt(), replace lines 76-88:
        if self.activated_principles:
            lines.append("## 🧬 核心约定参考（约定优于约束——决策前主动查阅）")
            from plastic_promise.core.constants import CORE_PRINCIPLES
            for p in self.activated_principles:
                if isinstance(p, dict):
                    name = p.get("name", "?")
                    content = p.get("content", "")
                    consequence = p.get("consequence", "违反约定可能导致系统退化")
                else:
                    name = p
                    match = next((cp for cp in CORE_PRINCIPLES if cp["name"] == name), None)
                    content = match["content"] if match else ""
                    consequence = match.get("consequence", "违反约定可能导致系统退化") if match else ""
                lines.append(f"### {name}")
                lines.append(f"> {content}")
                lines.append(f"**⚠️ 违反后果**：{consequence}")
            lines.append("")
```

- [ ] **Step 3: Verify principles carry full content**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine(use_sqlite=False)
principles = engine._activate_principles('code_generation', 'implement a new feature')
print(f'Activated {len(principles)} principles:')
for p in principles:
    assert isinstance(p, dict), f'Expected dict, got {type(p)}'
    assert 'name' in p, 'Missing name'
    assert 'content' in p, 'Missing content'
    print(f'  - {p[\"name\"]}: {p[\"content\"][:60]}...')
print('OK')
"
```

Expected: Each principle is a dict with `name` and `content`.

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat(principles): return full dict (name+content+domain) from _activate_principles()"
```

---

### Task 4: Rust engine — Expose real engine factory + uncomment dispatch

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs` (add `new_with_backends`)
- Modify: `plastic_promise/core/context_engine.py` (lines 1077-1093)

**Interfaces:**
- Produces: `ContextEngine::new_with_backends(sqlite_path, lancedb_path) -> PyResult<ContextEngine>`
- Modifies: `supply()` dispatch uncomment to use Rust path

- [ ] **Step 1: Add `new_with_backends` static method to Rust**

Confirmed: `NoopVectorIndex` and `NoopFtsIndex` exist at `rust/context-engine-core/src/retrieval/mod.rs:194,210`. `LanceDbStore` does NOT derive Clone — cannot use it as both VectorIndex and FtsIndex simultaneously. Strategy: use Noop stubs for vector/FTS channels (Python owns LanceDB per architecture contract), inject REAL `WeibullDecay`, `WilsonWorthCalculator`, and `DefaultTierManager` to replace the noop domain models. This gives correct worth scoring and tier classification even in the fallback path.

In `rust/context-engine-core/src/context_engine.rs`, add after `set_current_time()` (line 254):

```rust
    /// Create a ContextEngine with real domain models (not placeholders).
    ///
    /// Vector + FTS channels use Noop stubs per architecture contract
    /// (Python owns LanceDB). Domain models use real implementations:
    /// WeibullDecay for tier-aware decay, WilsonWorthCalculator for
    /// statistically-sound worth scoring, DefaultTierManager for
    /// access-count-based tier promotion.
    ///
    /// The keyword-overlap BM25 fallback in HybridRetriever.retrieve()
    /// (retrieval/mod.rs:131-147) provides text-based retrieval when
    /// vector indices are unavailable.
    #[staticmethod]
    pub fn new_with_backends(_sqlite_path: String, _lancedb_path: String) -> PyResult<Self> {
        use crate::domain::decay::WeibullDecay;
        use crate::domain::worth::WilsonWorthCalculator;
        use crate::domain::tier::DefaultTierManager;
        // NoopVectorIndex / NoopFtsIndex are defined in retrieval/mod.rs:194,210
        use crate::retrieval::NoopVectorIndex;
        use crate::retrieval::NoopFtsIndex;
        use crate::retrieval::NoopConsolidator;

        let storage = crate::storage::sqlite_impl::SqliteStorage::open(":memory:")
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))?;

        let retriever = HybridRetriever::new(
            Box::new(NoopVectorIndex),                  // vector search: Python-side
            Box::new(NoopFtsIndex),                     // FTS: Python-side
            Box::new(WeibullDecay::default()),          // REAL decay model
            Box::new(WilsonWorthCalculator::default()), // REAL worth model
            Box::new(DefaultTierManager),               // REAL tier manager
            Box::new(NoopConsolidator),
        );

        Ok(Self {
            graph: RefCell::new(EntityGraph::new()),
            source_tracker: SourceTracker::new(),
            feedback: AssociationFeedback::new(),
            retriever,
            storage: Box::new(storage),
            enable_principles: true,
            current_time: String::new(),
            last_consolidation: Cell::new(Utc::now()),
        })
    }
```

Note: `NoopVectorIndex`, `NoopFtsIndex`, and `NoopConsolidator` are currently `struct` (not `pub`). They need `pub` added:

```rust
// In retrieval/mod.rs, change lines 194, 210, 251:
pub struct NoopVectorIndex;     // was: struct NoopVectorIndex
pub struct NoopFtsIndex;        // was: struct NoopFtsIndex
pub struct NoopConsolidator;    // was: struct NoopConsolidator
```

- [ ] **Step 2: Make Noop types public + Build Rust**

First, make `NoopVectorIndex`, `NoopFtsIndex`, `NoopConsolidator` public in `rust/context-engine-core/src/retrieval/mod.rs`:

```rust
// Lines 194, 210, 251 — add 'pub' prefix:
pub struct NoopVectorIndex;
pub struct NoopFtsIndex;
pub struct NoopConsolidator;
```

Then build:

```bash
cd rust/context-engine-core
cargo build --release 2>&1
```

Expected: compiles with warnings only. The `non_local_definitions` and unused-variable warnings are pre-existing.

- [ ] **Step 3: Copy .dll to .pyd and verify**

```bash
cp rust/context-engine-core/target/release/context_engine_core.dll rust/context-engine-core/target/release/context_engine_core.pyd
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
from context_engine_core import ContextEngine as RustEngine
# Test new_with_backends
e = RustEngine.new_with_backends(':memory:', '/tmp/test_lancedb')
e.set_current_time('2026-07-02T00:00:00')
pack = e.supply('test', [0.5]*1024, 'general', 'global', [
    {'id': 'm1', 'content': 'test A', 'source': 'test', 'memory_type': 'task',
     'worth_success': 20, 'worth_failure': 2,
     'created_at': '2026-07-01T00:00:00', 'last_accessed': '2026-07-01T00:00:00'},
])
print(f'new_with_backends: total={pack.total_items}')
assert pack.total_items >= 0
print('OK')
"
```

Expected: `total_items >= 0`, no crash.

- [ ] **Step 4: Uncomment Rust dispatch in Python**

In `plastic_promise/core/context_engine.py`, uncomment lines 1081-1092:

```python
# OLD (lines 1077-1093):
        # Rust accelerator bypassed — placeholder engine returns uniform 0.50 scores.
        # Python path has real LanceDB vector + BM25 + RRF retrieval.
        # To re-enable Rust, uncomment the block below when retriever backends are implemented.
        #
        # if self._check_rust_health():
        #     try:
        #         return self._supply_rust(
        #             task_description, task_vector, task_type, scope
        #         )
        #     except Exception as e:
        #         logger.warning(
        #             "Rust supply failed, falling back to Python: %s", e
        #         )
        #         with self._rust_lock:
        #             self._rust_healthy = None
        #             self._rust_engine_instance = None

# NEW:
        if self._check_rust_health():
            try:
                return self._supply_rust(
                    task_description, task_vector, task_type, scope
                )
            except Exception as e:
                logger.warning(
                    "Rust supply failed, falling back to Python: %s", e
                )
                with self._rust_lock:
                    self._rust_healthy = None
                    self._rust_engine_instance = None
```

- [ ] **Step 5: Update `_supply_rust()` to use `new_with_backends`**

In `_supply_rust()` (line 1604-1628), replace `RustEngine()` with `RustEngine.new_with_backends(...)`:

```python
    def _supply_rust(self, task_description: str, task_vector: list,
                     task_type: str, scope: str) -> ContextPack:
        from context_engine_core import ContextEngine as RustEngine
        import tempfile, os as _os

        with self._write_lock:
            memories = [
                self._memories[mid]
                for mid in self._memories
            ]

        # Use real domain models (not placeholders)
        lancedb_tmp = _os.path.join(tempfile.gettempdir(), "pp_rust_lancedb")
        rust = RustEngine.new_with_backends(":memory:", lancedb_tmp)
        rust.set_current_time(datetime.datetime.now().isoformat())
        rust_pack = rust.supply(task_description, task_vector, task_type, scope, memories)
        return self._convert_rust_pack(rust_pack)
```

- [ ] **Step 6: Verify Rust dispatch works end-to-end**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine(use_sqlite=False)
engine.register_memory({'id': 'test1', 'content': 'Rust engine integration test', 'memory_type': 'task', 'source': 'test'})
pack = engine.supply('test task', task_type='general', scope='global')
print(f'Supply result: core={len(pack.core)}, related={len(pack.related)}, principles={pack.activated_principles}')
assert pack is not None
assert pack.total_items >= 0
print('OK')
"
```

Expected: returns valid ContextPack, no crash (Rust or Python path depending on .pyd availability).

- [ ] **Step 7: Run existing tests**

```bash
python -m pytest tests/test_boundary.py tests/test_rust_integration.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 8: Commit**

```bash
git add rust/context-engine-core/src/context_engine.rs plastic_promise/core/context_engine.py
git commit -m "feat(rust): expose new_with_backends() + uncomment Rust dispatch in supply()"
```

---

### Task 5: Data quality scanner — Fill the scanner blind spot

**Files:**
- Create: `plastic_promise/cron/scan_data_quality.py`
- Modify: `daemons/maintenance_daemon.py` (add scanner to main loop)

**Interfaces:**
- Produces: `scan_data_quality(engine) -> List[dict]` — findings with severity and suggested fix
- Consumed by: daemon main loop, results enqueued to task_queue

- [ ] **Step 1: Create the scanner**

Create `plastic_promise/cron/scan_data_quality.py`:

```python
"""scan_data_quality — 6-dimension data quality health check.

Dimensions:
  1. embedder_health  — FallbackEmbedder active? real embedder working?
  2. zero_vector_ratio — what % of LanceDB rows are all zeros?
  3. principle_injection — are principles carrying full content?
  4. rust_engine_health — is Rust engine importable and healthy?
  5. pipeline_buffer_health — MemoryPipeline backlog + embed:deferred count
  6. mcp_server_alive — can we reach the MCP health endpoint?
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger("plastic-promise.scan_data_quality")


def scan_data_quality(engine: Any) -> List[Dict[str, Any]]:
    """Run all 6 data quality checks and return actionable findings."""
    findings: List[Dict[str, Any]] = []

    # --- 1. Embedder health ---
    _check_embedder(engine, findings)

    # --- 2. Zero-vector ratio ---
    _check_zero_vectors(engine, findings)

    # --- 3. Principle injection ---
    _check_principle_injection(engine, findings)

    # --- 4. Rust engine health ---
    _check_rust_health(engine, findings)

    # --- 5. Pipeline buffer health ---
    _check_pipeline_buffer(engine, findings)

    # --- 6. MCP server alive ---
    _check_mcp_alive(findings)

    if findings:
        logger.warning("scan_data_quality: %d issues found", len(findings))
        for f in findings:
            logger.warning("  [%s] %s: %s", f["severity"], f["dimension"], f["summary"])
    else:
        logger.info("scan_data_quality: all checks passed")

    return findings


def _check_embedder(engine: Any, findings: List[Dict]):
    """Check if embedder is healthy (not FallbackEmbedder, produces non-zero vectors)."""
    try:
        from plastic_promise.core.embedder import FallbackEmbedder, get_embedder
        embedder = get_embedder(fallback_on_error=False)
        if isinstance(embedder, FallbackEmbedder):
            findings.append({
                "dimension": "embedder_health",
                "severity": "critical",
                "summary": "FallbackEmbedder active — all vectors are zeros",
                "fix": "Ensure Ollama is running: ollama serve; then call reset_embedder()",
            })
            return
        # Actually test embedding
        vec = embedder.embed("health check probe")
        if not vec or not any(v != 0.0 for v in vec):
            findings.append({
                "dimension": "embedder_health",
                "severity": "critical",
                "summary": "Embedder returns zero vectors despite not being FallbackEmbedder",
                "fix": "Check embedder logs, try reset_embedder() to re-probe",
            })
    except Exception as e:
        findings.append({
            "dimension": "embedder_health",
            "severity": "critical",
            "summary": f"Embedder probe failed: {e}",
            "fix": "Check EMBEDDER_PROVIDER env var and Ollama connectivity",
        })


def _check_zero_vectors(engine: Any, findings: List[Dict]):
    """Check LanceDB for zero-vector entries."""
    ldb = getattr(engine, '_ldb', None)
    if ldb is None:
        findings.append({
            "dimension": "zero_vector_ratio",
            "severity": "high",
            "summary": "LanceDB store not initialized",
            "fix": "Restart MCP server to trigger _ensure_heavy_init()",
        })
        return
    try:
        table = getattr(ldb, '_table', None)
        if table is None:
            return
        total = table.count_rows()
        if total == 0:
            return
        # Sample first 100 rows for zero-vector check
        sample = table.search().limit(min(total, 100)).to_list()
        zero_count = 0
        for row in sample:
            vec = row.get("vector", [])
            if vec and not any(v != 0.0 for v in vec):
                zero_count += 1
        if sample:
            ratio = zero_count / len(sample)
            if ratio > 0.1:  # >10% zero vectors
                findings.append({
                    "dimension": "zero_vector_ratio",
                    "severity": "critical" if ratio > 0.5 else "high",
                    "summary": f"{ratio:.0%} of sampled LanceDB rows are zero vectors "
                               f"({zero_count}/{len(sample)} sampled, {total} total)",
                    "fix": "Run scripts/repair_zero_vectors.py to re-embed corrupted rows",
                })
    except Exception as e:
        logger.warning("zero_vector check failed: %s", e)


def _check_principle_injection(engine: Any, findings: List[Dict]):
    """Check that principle activation returns full content."""
    try:
        principles = engine._activate_principles("code_generation", "test probe")
        if not principles:
            findings.append({
                "dimension": "principle_injection",
                "severity": "medium",
                "summary": "No principles activated for code_generation task type",
                "fix": "Check CORE_PRINCIPLES and TASK_TYPE_PRINCIPLE_MAP in constants.py",
            })
            return
        for p in principles:
            if isinstance(p, str):
                findings.append({
                    "dimension": "principle_injection",
                    "severity": "high",
                    "summary": "Principles are strings, not dicts — content not injected",
                    "fix": "Update _activate_principles() to return dicts with name+content",
                })
                return
            if not p.get("content"):
                findings.append({
                    "dimension": "principle_injection",
                    "severity": "medium",
                    "summary": f"Principle '{p.get('name')}' has empty content",
                    "fix": "Check CORE_PRINCIPLES entry for missing content field",
                })
                return
    except Exception as e:
        findings.append({
            "dimension": "principle_injection",
            "severity": "medium",
            "summary": f"Principle injection check failed: {e}",
        })


def _check_rust_health(engine: Any, findings: List[Dict]):
    """Check Rust engine availability."""
    try:
        healthy = engine._check_rust_health()
        if not healthy:
            findings.append({
                "dimension": "rust_engine_health",
                "severity": "low",
                "summary": "Rust engine not available — using Python fallback",
                "fix": "Build Rust: cd rust/context-engine-core && cargo build --release",
            })
    except Exception as e:
        findings.append({
            "dimension": "rust_engine_health",
            "severity": "low",
            "summary": f"Rust health check failed: {e}",
        })


def _check_pipeline_buffer(engine: Any, findings: List[Dict]):
    """Check MemoryPipeline buffer for stuck/deferred items."""
    try:
        from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer
        fb = _get_fuzzy_buffer(engine)
        stats = fb.stats()
        total = stats.get("total", 0)
        if total > 10:
            # Count embed:deferred tags
            deferred = sum(
                1 for r in getattr(fb, '_buffer', {}).values()
                if "embed:deferred" in r.get("tags", [])
            )
            findings.append({
                "dimension": "pipeline_buffer_health",
                "severity": "high" if deferred > 5 else "medium",
                "summary": f"Pipeline buffer has {total} items ({deferred} with embed:deferred)",
                "fix": "Check embedder health; if recovered, run fuzzy_process MCP tool",
            })
    except Exception as e:
        logger.warning("pipeline buffer check failed: %s", e)


def _check_mcp_alive(findings: List[Dict]):
    """Check MCP server health endpoint."""
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:9020/health", timeout=5)
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status}")
    except Exception as e:
        findings.append({
            "dimension": "mcp_server_alive",
            "severity": "critical",
            "summary": f"MCP server unreachable: {e}",
            "fix": "Start MCP: python -m plastic_promise.mcp.server --sse 9020",
        })
```

- [ ] **Step 2: Register scanner in daemon**

In `daemons/maintenance_daemon.py`, add import (after line 43):

```python
from plastic_promise.cron.scan_data_quality import scan_data_quality
```

Add throttle (after line 86):

```python
    "scan_data_quality": AdaptiveThrottle(600),
```

Add to main loop (after line 1309, before `scan_llm_classify`):

```python
                await _run_scan(
                    "scan_data_quality",
                    scan_data_quality,
                    engine,
                    throttles["scan_data_quality"],
                )
```

And add `scan_data_quality` to the 5-scanner list in the startup log (line 1196-1197).

- [ ] **Step 3: Verify scanner works standalone**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.cron.scan_data_quality import scan_data_quality
engine = ContextEngine(use_sqlite=False)
# Register a test memory to trigger heavy init
engine.register_memory({'id': 'scan_test', 'content': 'scanner probe', 'memory_type': 'task', 'source': 'test'})
findings = scan_data_quality(engine)
print(f'Findings: {len(findings)}')
for f in findings:
    print(f'  [{f[\"severity\"]}] {f[\"dimension\"]}: {f[\"summary\"]}')
print('Scanner OK')
"
```

Expected: runs without crash, reports finding count (may vary by environment).

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/cron/scan_data_quality.py daemons/maintenance_daemon.py
git commit -m "feat(scanner): add scan_data_quality — 6-dimension data health check"
```

---

### Task 6: LanceDB repair script — Fix existing zero vectors

**Files:**
- Create: `scripts/repair_zero_vectors.py`

- [ ] **Step 1: Write the repair script**

Create `scripts/repair_zero_vectors.py`:

```python
"""Repair zero-vector entries in LanceDB by re-embedding with current embedder.

Usage:
    python scripts/repair_zero_vectors.py [--dry-run] [--limit N]

One-shot script. Run after embedder recovery to fix existing corrupted vectors.
"""

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("repair_zero_vectors")


def main():
    parser = argparse.ArgumentParser(description="Repair LanceDB zero-vector entries")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--limit", type=int, default=0, help="Max entries to repair (0 = unlimited)")
    args = parser.parse_args()

    # Import after arg parsing to keep --help fast
    from plastic_promise.core.context_engine import ContextEngine
    from plastic_promise.core.embedder import get_embedder, FallbackEmbedder

    embedder = get_embedder(fallback_on_error=False)
    if isinstance(embedder, FallbackEmbedder):
        logger.error("Embedder is FallbackEmbedder — cannot repair. Fix embedder first.")
        sys.exit(1)

    # Verify embedder produces real vectors
    test_vec = embedder.embed("test")
    if not test_vec or not any(v != 0.0 for v in test_vec):
        logger.error("Embedder returns zero vectors — cannot repair. Fix embedder first.")
        sys.exit(1)

    logger.info("Embedder OK (%s, dim=%d)", embedder.model_name, len(test_vec))

    engine = ContextEngine(use_sqlite=False)
    # Trigger heavy init to load LanceDB
    engine.register_memory({
        "id": "__repair_probe__",
        "content": "repair probe",
        "memory_type": "task",
        "source": "repair",
    })

    ldb = getattr(engine, '_ldb', None)
    if ldb is None or getattr(ldb, '_table', None) is None:
        logger.error("LanceDB not available")
        sys.exit(1)

    table = ldb._table
    total = table.count_rows()
    logger.info("LanceDB has %d total rows", total)

    # Scan for zero vectors
    repaired = 0
    skipped = 0
    to_fix = []

    rows = table.search().limit(total).to_list()
    for row in rows:
        mid = row["memory_id"]
        vec = row.get("vector", [])
        if mid == "__repair_probe__":
            continue
        if vec and not any(v != 0.0 for v in vec):
            to_fix.append((mid, row.get("text", "")))

    logger.info("Found %d zero-vector entries out of %d rows", len(to_fix), total)

    if args.dry_run:
        for mid, text in to_fix[:10]:
            logger.info("  [DRY RUN] would repair: %s (%s...)", mid, text[:60])
        logger.info("Dry run complete. %d entries would be repaired.", len(to_fix))
        return

    for mid, text in to_fix:
        if args.limit > 0 and repaired >= args.limit:
            logger.info("Limit reached (%d), stopping", args.limit)
            break
        try:
            new_vec = embedder.embed(text)
            if not new_vec or not any(v != 0.0 for v in new_vec):
                logger.warning("  SKIP %s: embedder returned zero vector", mid)
                skipped += 1
                continue
            # Update LanceDB
            ldb.update(
                memory_id=mid,
                vector=new_vec,
                text=text,
                tier=row.get("tier", "L1") if 'row' in dir() else "L1",
                category=row.get("category", "other") if 'row' in dir() else "other",
                scope=row.get("scope", "global") if 'row' in dir() else "global",
            )
            repaired += 1
            if repaired % 10 == 0:
                logger.info("  %d/%d repaired...", repaired, len(to_fix))
        except Exception as e:
            logger.warning("  FAIL %s: %s", mid, e)
            skipped += 1

    # Clean up probe
    try:
        ldb.delete("__repair_probe__")
    except Exception:
        pass

    logger.info("Repair complete: %d repaired, %d skipped, %d remaining",
                repaired, skipped, len(to_fix) - repaired - skipped)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test with --dry-run**

```bash
python scripts/repair_zero_vectors.py --dry-run
```

Expected: reports number of zero-vector entries without modifying anything.

- [ ] **Step 3: Commit**

```bash
git add scripts/repair_zero_vectors.py
git commit -m "feat(repair): add LanceDB zero-vector repair script"
```

---

### Task 7: Daemon self-healing startup script

**Files:**
- Create: `scripts/start_all.bat`

- [ ] **Step 1: Write the startup script**

Create `scripts/start_all.bat`:

```bat
@echo off
echo === Plastic Promise — Full System Startup ===

REM Start MCP Server
echo [1/2] Starting MCP Server on port 9020...
start /B python -m plastic_promise.mcp.server --sse 9020

REM Wait for health endpoint
echo [*] Waiting for MCP server health check...
:wait_mcp
timeout /t 2 >nul
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health', timeout=5)" 2>nul
if errorlevel 1 (
    echo     ... still waiting
    goto wait_mcp
)
echo     MCP Server ready.

REM Start Maintenance Daemon
echo [2/2] Starting Maintenance Daemon...
start /B python daemons/maintenance_daemon.py
timeout /t 3 >nul

echo === Plastic Promise fully started ===
echo MCP Server: http://127.0.0.1:9020
echo Daemon: running in background
echo Health: python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
```

- [ ] **Step 2: Commit**

```bash
git add scripts/start_all.bat
git commit -m "chore: add start_all.bat — MCP server + daemon with health-wait"
```

---

## Verification (after all tasks)

- [ ] **V1**: `python -c "from plastic_promise.core.embedder import get_embedder, reset_embedder; reset_embedder(); e=get_embedder(); v=e.embed('test'); assert any(x!=0 for x in v); print('OK:', len(v), 'dim')"`
- [ ] **V2**: `python -m pytest tests/test_boundary.py tests/test_rust_integration.py -v` — all 7 pass
- [ ] **V3**: `python -c "from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(use_sqlite=False); p=e._activate_principles('code_generation','test'); assert all(isinstance(x,dict) for x in p); print('OK')"`
- [ ] **V4**: `python -c "from plastic_promise.memory.pipeline import MemoryPipeline; from plastic_promise.core.embedder import FallbackEmbedder; fb=FallbackEmbedder(1024); p=MemoryPipeline(embedder=fb); mid=p.store_urgent('test'); [r.update({'stage':'classified'}) for r in p._buffer.values()]; p.process_pipeline(); s=p.stats(); assert s['total']>0; print('OK')"`
- [ ] **V5**: `python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py` — all pass
- [ ] **V6**: `python scripts/repair_zero_vectors.py --dry-run` — runs without crash
- [ ] **V7**: `python -c "from plastic_promise.cron.scan_data_quality import scan_data_quality; from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(use_sqlite=False); e.register_memory({'id':'q','content':'q','memory_type':'task','source':'test'}); f=scan_data_quality(e); print(f'Scanner: {len(f)} findings')"` — runs without crash
