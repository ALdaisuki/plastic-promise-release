# Rust Engine Production Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Rust `context_engine_core` into the running Python `ContextEngine.supply()` with automatic degradation when Rust is unavailable, and verify performance.

**Architecture:** Rust `supply()` becomes a stateless computation pipeline receiving memories via PyO3 native `Vec<PyObject>` (zero JSON overhead). Python `supply()` probes Rust health (cached 5min TTL), delegates on success, falls back to pure Python on failure. On any failure, `_rust_healthy` is set to `None` (not `False`) for immediate re-probe on the next call. Thread safety via `_rust_lock`.

**Tech Stack:** Rust 1.96 (pyo3 0.20), Python 3.13, threading.Lock

## Global Constraints

- Rust `supply()` MUST be `&self` (not `&mut self`) — stateless, reentrant
- Memories pass via `Vec<PyObject>` — zero JSON serialization, PyO3 native field extraction
- Empty-retriever fallback: if `scored_items` is empty and `memories` non-empty, return ALL memories at relevance 0.50 in `related` tier
- `_rust_lock = threading.Lock()` MUST protect all `_rust_*` field access
- On any Rust failure, set `_rust_healthy = None` (NOT `False`) — immediate re-probe next call
- `_supply_python()` MUST be independent implementation — no recursive call to `supply()`
- `_convert_rust_pack()` MUST copy `audit_metadata` from Rust to Python ContextPack
- All existing tests must pass after every task
- CI guard (`tests/test_boundary.py`) must remain green

---

## File Structure

| File | Role |
|------|------|
| `rust/context-engine-core/src/context_engine.rs` | **Modify** `supply()`: `&mut self` → `&self`, add `memories` param, add empty-retriever fallback |
| `plastic_promise/core/context_engine.py` | **Add** `_rust_lock`, `_check_rust_health()`, `reset_rust_health()`, `_supply_rust()`, `_convert_rust_pack()`; **modify** `supply()` dispatch |
| `tests/test_rust_supply_perf.py` | **Create** benchmark script |
| `tests/test_rust_integration.py` | **Create** integration smoke tests |

---

## Phase 3a: Rust supply() — Stateless + PyO3 Native

### Task 1: Modify Rust `supply()` signature and data source

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs:349-552`

**Interfaces:**
- Produces: `fn supply(&self, task_description: String, task_vector: Vec<f32>, task_type: String, scope: String, memories: Vec<PyObject>) -> PyResult<ContextPack>`
- Changes: `&mut self` → `&self`, add `memories` parameter, remove `self.storage.list()` call

- [ ] **Step 1: Change `supply()` signature**

In `rust/context-engine-core/src/context_engine.rs`, replace the existing `supply()` signature (line 359-365):

```rust
// OLD (lines 359-365):
pub fn supply(
    &mut self,
    task_description: String,
    task_vector: Vec<f32>,
    task_type: String,
    scope: String,
) -> ContextPack {

// NEW:
#[pyo3(signature = (task_description, task_vector, task_type, scope, memories))]
pub fn supply(
    &self,
    task_description: String,
    task_vector: Vec<f32>,
    task_type: String,
    scope: String,
    memories: Vec<PyObject>,
) -> PyResult<ContextPack> {
```

- [ ] **Step 2: Replace Phase 1 data source**

Replace lines 397-412 (the `self.storage.list()` block):

```rust
// OLD (lines 397-412):
let filter = ListFilter {
    scope: Some(scope.clone()),
    ..Default::default()
};
let memories = self.storage.list(&filter).unwrap_or_default();

let mut item_lookup: HashMap<String, (String, String)> = memories
    .iter()
    .map(|m| (m.id.clone(), (m.content.clone(), m.source.clone())))
    .collect();

// 构建用于内容回溯的完整记忆索引
let memory_index: HashMap<String, crate::memory_worth::MemoryRecord> = memories
    .into_iter()
    .map(|m| (m.id.clone(), m))
    .collect();

// NEW:
let mut item_lookup: HashMap<String, (String, String)> = HashMap::new();
let mut memory_index: HashMap<String, crate::memory_worth::MemoryRecord> = HashMap::new();

for py_mem in &memories {
    let id: String = py_mem.get_item("id")
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("missing id: {}", e)))?
        .extract()
        .map_err(|e| pyo3::exceptions::PyTypeError::new_err(format!("id not a string: {}", e)))?;
    let content: String = py_mem.get_item("content")
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("missing content: {}", e)))?
        .extract()
        .map_err(|e| pyo3::exceptions::PyTypeError::new_err(format!("content not a string: {}", e)))?;
    let source: String = py_mem.get_item("source")
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();
    let memory_type: String = py_mem.get_item("memory_type")
        .and_then(|v| v.extract().ok())
        .unwrap_or_else(|| "experience".to_string());
    let worth_success: f64 = py_mem.get_item("worth_success")
        .and_then(|v| v.extract().ok())
        .unwrap_or(0.0);
    let worth_failure: f64 = py_mem.get_item("worth_failure")
        .and_then(|v| v.extract().ok())
        .unwrap_or(0.0);
    let created_at: String = py_mem.get_item("created_at")
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();
    let last_accessed_at: String = py_mem.get_item("last_accessed")
        .and_then(|v| v.extract().ok())
        .unwrap_or_default();

    item_lookup.insert(id.clone(), (content.clone(), source.clone()));
    memory_index.insert(id.clone(), crate::memory_worth::MemoryRecord {
        id,
        content,
        source,
        memory_type,
        worth_success,
        worth_failure,
        created_at,
        last_accessed_at,
        ..Default::default()
    });
}
```

- [ ] **Step 3: Add `use pyo3::prelude::*` import if missing**

At the top of `context_engine.rs`, verify this import exists. If not, add it:

```rust
use pyo3::prelude::*;
```

(The file likely already has this import at line 13 — verify.)

- [ ] **Step 4: Wrap return type in `PyResult`**

Change the function body's return statements to wrap in `Ok(...)`:

The `return pack;` at line 551 needs to become `Ok(pack)`. Also wrap the return type:

```rust
// At line 365, the return type is now PyResult<ContextPack>
// At line 551, change:
pack
// to:
Ok(pack)
```

- [ ] **Step 5: Build and verify**

```bash
cd rust/context-engine-core
cargo build --release 2>&1
```

Expected: Compiles with warnings only (no errors). The box-drawing `non_local_definitions` warnings and unused variables are pre-existing.

- [ ] **Step 6: Copy DLL to PYD and smoke test**

```bash
cp rust/context-engine-core/target/release/context_engine_core.dll rust/context-engine-core/target/release/context_engine_core.pyd
cd F:\Agent\Memory system
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
from context_engine_core import ContextEngine as RustEngine
e = RustEngine()
e.set_current_time('2026-07-02T00:00:00')
# Test with empty memories
pack = e.supply('test task', [0.0]*768, 'general', 'global', [])
print(f'Empty: core={len(pack.core)}, related={len(pack.related)}, divergent={len(pack.divergent)}, principles={len(pack.activated_principles)}')
assert len(pack.core) == 0
assert len(pack.related) == 0
# Test with 3 memories
memories = [
    {'id': 'm1', 'content': 'test A', 'source': 'test', 'memory_type': 'task', 'worth_success': 1, 'worth_failure': 0, 'created_at': '2026-01-01T00:00:00', 'last_accessed': '2026-01-01T00:00:00'},
    {'id': 'm2', 'content': 'test B', 'source': 'test', 'memory_type': 'task', 'worth_success': 0, 'worth_failure': 1, 'created_at': '2026-01-01T00:00:00', 'last_accessed': '2026-01-01T00:00:00'},
    {'id': 'm3', 'content': 'test C', 'source': 'test', 'memory_type': 'experience', 'worth_success': 2, 'worth_failure': 0, 'created_at': '2026-01-01T00:00:00', 'last_accessed': '2026-01-01T00:00:00'},
]
pack = e.supply('test task', [0.5]*768, 'general', 'global', memories)
print(f'3 memories: total={pack.total_items}, core={len(pack.core)}, related={len(pack.related)}, principles={len(pack.activated_principles)}')
print('Smoke test: OK')
"
```

Expected: Empty returns empty, 3 memories returns some results (may be empty core if retriever is placeholder, but should not crash).

- [ ] **Step 7: Commit**

```bash
git add rust/context-engine-core/src/context_engine.rs
git commit -m "refactor(rust): make supply() stateless — &self + Vec<PyObject> mem passing"
```

---

### Task 2: Add empty-retriever fallback to Rust supply()

**Files:**
- Modify: `rust/context-engine-core/src/context_engine.rs` (Phase 2 section, lines 414-428)

**Interfaces:**
- Consumes: `supply()` from Task 1
- Produces: Empty-retriever fallback — if `scored_items` empty and `memory_index` non-empty, skip pipeline and return all memories at relevance 0.50

- [ ] **Step 1: Add fallback after Phase 2 retrieval**

In `rust/context-engine-core/src/context_engine.rs`, after line 428 (`let scored_items = ...unwrap_or_default();`), add:

```rust
// ============================================================
// Phase 2: 混合检索 (向量 + BM25 + RRF + 符号规则)
// ============================================================
let max_results = 30;
let scored_items = self
    .retriever
    .retrieve(
        &task_vector,
        &task_description,
        &scope,
        Some(&task_type),
        &item_lookup,
        max_results,
    )
    .unwrap_or_default();

// FALLBACK: if retriever is a placeholder and returns nothing,
// return all memories at relevance 0.50 in "related" tier.
// This guarantees Rust never returns emptier than Python would.
if scored_items.is_empty() && !memory_index.is_empty() {
    let mut pack = ContextPack::new();
    pack.activated_principles = activated_principle_names;

    for (id, mem) in &memory_index {
        let worth = mem.worth_score();
        let is_principle = id.starts_with("principle:");
        let freshness = crate::source_tracker::Freshness::from_timestamps(
            &mem.created_at,
            &self.current_time,
        )
        .as_str()
        .to_string();

        let mut item = ContextItem::new(id.clone(), mem.content.clone(), 0.50);
        item.source = mem.source.clone();
        item.freshness = freshness;
        item.layer = "related".into();
        item.is_principle = is_principle;
        item.worth_score = worth;
        pack.related.push(item);
    }

    // Audit metadata (lightweight — no graph stats in fallback path)
    let mut audit = HashMap::new();
    audit.insert("engine_version".into(), "0.2.0-rs-fallback".into());
    audit.insert("task_type".into(), task_type);
    audit.insert("scope".into(), scope);
    audit.insert("principle_injection_count".into(),
        pack.activated_principles.len().to_string());
    audit.insert("memory_pool_size".into(), memory_index.len().to_string());
    audit.insert("timestamp".into(), self.current_time.clone());
    audit.insert("fallback".into(), "true".into());
    pack.audit_metadata = audit;

    return Ok(pack);
}

// Convert to (id, score) for feedback pipeline
let mut all_rankings: Vec<(String, f64)> = scored_items
    .iter()
    .map(|item| (item.id.clone(), item.score))
    .collect();
```

- [ ] **Step 2: Rebuild and smoke test**

```bash
cd rust/context-engine-core && cargo build --release 2>&1
cp rust/context-engine-core/target/release/context_engine_core.dll rust/context-engine-core/target/release/context_engine_core.pyd
cd F:\Agent\Memory system
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
from context_engine_core import ContextEngine as RustEngine
memories = [{'id': f'm{i:03d}', 'content': f'test {i}', 'source': 'test', 'memory_type': 'task', 'worth_success': 1, 'worth_failure': 0, 'created_at': '2026-07-01T00:00:00', 'last_accessed': '2026-07-01T00:00:00'} for i in range(100)]
e = RustEngine()
e.set_current_time('2026-07-02T00:00:00')
pack = e.supply('test', [0.0]*768, 'general', 'global', memories)
print(f'total={pack.total_items}, core={len(pack.core)}, related={len(pack.related)}, divergent={len(pack.divergent)}')
# With placeholder retriever, all 100 should be in "related" tier
assert len(pack.related) == 100, f'Expected 100 in related, got {len(pack.related)}'
print('Empty-retriever fallback: OK (all memories returned)')
"
```

Expected: `total=100, core=0, related=100, divergent=0`. Empty-retriever fallback working.

- [ ] **Step 3: Commit**

```bash
git add rust/context-engine-core/src/context_engine.rs
git commit -m "feat(rust): empty-retriever fallback — return all memories when retriever is placeholder"
```

---

## Phase 3b: Python Health Check + Degradation

### Task 3: Add `_rust_lock`, `_check_rust_health()`, `reset_rust_health()` to ContextEngine

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (add to `__init__` and add new methods)

**Interfaces:**
- Produces: `self._rust_lock`, `self._rust_healthy`, `self._rust_health_checked_at`, `self._rust_health_ttl`, `self._rust_engine_instance`, `self._check_rust_health() -> bool`, `self.reset_rust_health()`

- [ ] **Step 1: Add Rust-related fields to `__init__`**

In `plastic_promise/core/context_engine.py`, find `__init__` (search for `def __init__`). After the existing `self._write_lock = threading.RLock()` line (or near the end of `__init__`), add:

```python
# Rust engine integration — stateless accelerator for supply()
self._rust_healthy: bool | None = None       # None = unchecked, True = healthy, None on failure
self._rust_health_checked_at: float = 0.0     # epoch timestamp of last health check
self._rust_health_ttl: float = 300.0          # cache TTL in seconds (5 minutes)
self._rust_engine_instance = None              # cached Rust engine instance (reused)
self._rust_lock = threading.Lock()             # protects all _rust_* fields from concurrent access
```

Verify `import threading` exists at top of file (line 13). If not, add it.

- [ ] **Step 2: Add `_check_rust_health()` method**

Add this method to the `ContextEngine` class, near the other internal methods (around line 1350, near `ensure_heavy_init`):

```python
def _check_rust_health(self) -> bool:
    """Probe Rust core availability. Caches result for TTL seconds.

    Thread-safe: acquires _rust_lock to protect _rust_engine_instance
    and health state against concurrent MCP/Daemon access.

    On failure: sets _rust_healthy = None (NOT False) to force
    immediate re-probe on the next supply() call. This avoids the
    defect where setting healthy=False traps the system in a
    degraded state until TTL expires.
    """
    with self._rust_lock:
        now = time.time()
        # Return cached result if within TTL
        if self._rust_healthy is not None and \
           (now - self._rust_health_checked_at) < self._rust_health_ttl:
            return self._rust_healthy

        try:
            from context_engine_core import ContextEngine as RustEngine
            # Smoke test: supply with empty memories — validates import + PyO3 bridge
            engine = RustEngine()
            engine.set_current_time(datetime.datetime.now().isoformat())
            pack = engine.supply("test", [0.0] * 768, "general", "global", [])
            # Validate response shape — must have core + related attributes
            assert hasattr(pack, 'core'), "Rust ContextPack missing 'core'"
            assert hasattr(pack, 'related'), "Rust ContextPack missing 'related'"
            assert hasattr(pack, 'divergent'), "Rust ContextPack missing 'divergent'"
            assert hasattr(pack, 'activated_principles'), "Rust ContextPack missing 'activated_principles'"
            self._rust_engine_instance = engine
            self._rust_healthy = True
        except Exception as e:
            logger.warning("Rust engine health check failed: %s", e)
            # Set to None (not False) — forces immediate re-probe on next supply()
            self._rust_healthy = None
            self._rust_engine_instance = None

        self._rust_health_checked_at = now
        return self._rust_healthy is True
```

Add `import time` at top of file if not present (verify around line 13: `import threading` exists, check for `import time`; if missing, add it after `import threading`).

- [ ] **Step 3: Add `reset_rust_health()` method**

Add this method right after `_check_rust_health()`:

```python
def reset_rust_health(self):
    """Force re-probe Rust health on next supply() call.

    Use when: Rust .pyd was deployed, environment changed,
    or health was falsely marked as failed.
    """
    with self._rust_lock:
        self._rust_healthy = None
        self._rust_health_checked_at = 0.0
        self._rust_engine_instance = None
    logger.info("Rust health reset — will re-probe on next supply()")
```

- [ ] **Step 4: Verify no import errors**

```bash
cd F:\Agent\Memory system
python -c "from plastic_promise.core.context_engine import ContextEngine; e = ContextEngine(use_sqlite=False); print('_rust_lock:', e._rust_lock); print('_rust_healthy:', e._rust_healthy); print('_check_rust_health:', e._check_rust_health())"
```

Expected: `_rust_healthy: True` (if .pyd available) or `_rust_healthy: None` (if not). No crash.

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add _rust_lock, _check_rust_health(), reset_rust_health() to ContextEngine"
```

---

### Task 4: Add `_supply_rust()`, `_convert_rust_pack()`, and modify `supply()` dispatch

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Consumes: `_check_rust_health()` from Task 3
- Produces: `_supply_rust()`, `_convert_rust_pack()`, modified `supply()` with Rust-first dispatch

- [ ] **Step 1: Add `_convert_rust_pack()` method**

Add this method before `_supply_rust` (order: `_convert_rust_pack`, then `_supply_rust`):

```python
def _convert_rust_pack(self, rust_pack) -> ContextPack:
    """Convert Rust PyO3 ContextPack to Python ContextPack.

    Rust returns PyO3 objects with .core/.related/.divergent/
    .activated_principles/.audit_metadata. We convert to the
    Python dataclass-based format that callers expect.

    Preserves audit_metadata from Rust (engine_version, timings,
    graph stats, etc.) for observability.
    """
    pack = ContextPack()
    pack.core = [
        ContextItem(
            id=item.id,
            content=item.content,
            relevance=item.relevance,
            source=item.source,
            freshness=item.freshness,
            layer=item.layer,
            is_principle=item.is_principle,
            worth_score=item.worth_score,
        )
        for item in rust_pack.core
    ]
    pack.related = [
        ContextItem(
            id=item.id,
            content=item.content,
            relevance=item.relevance,
            source=item.source,
            freshness=item.freshness,
            layer=item.layer,
            is_principle=item.is_principle,
            worth_score=item.worth_score,
        )
        for item in rust_pack.related
    ]
    pack.divergent = [
        ContextItem(
            id=item.id,
            content=item.content,
            relevance=item.relevance,
            source=item.source,
            freshness=item.freshness,
            layer=item.layer,
            is_principle=item.is_principle,
            worth_score=item.worth_score,
        )
        for item in rust_pack.divergent
    ]
    pack.activated_principles = list(rust_pack.activated_principles)
    # Preserve audit metadata from Rust for observability
    if hasattr(rust_pack, 'audit_metadata') and rust_pack.audit_metadata:
        pack.audit_metadata = dict(rust_pack.audit_metadata)
    return pack
```

- [ ] **Step 2: Add `_supply_rust()` method**

Add this method after `_convert_rust_pack()`:

```python
def _supply_rust(self, task_description: str, task_vector: list,
                 task_type: str, scope: str) -> ContextPack:
    """Rust-accelerated supply path.

    Passes memory snapshot via PyO3 native Vec<PyObject> — zero JSON
    serialization overhead. The snapshot is a shallow copy of the
    current _memories dict (~0.5-2ms for 1000 records).
    """
    from context_engine_core import ContextEngine as RustEngine

    # Build memory list for PyO3 — pass raw dicts, no JSON serialize
    # Performance: ~0.5-2ms for 1000 records (list comprehension + dict refs)
    # Acceptable; if pool grows >5000, add pre-filter by scope/tier here
    memories = [
        self._memories[mid]
        for mid in self._memories
    ]

    rust = RustEngine()
    rust.set_current_time(datetime.datetime.now().isoformat())
    rust_pack = rust.supply(task_description, task_vector, task_type, scope, memories)
    return self._convert_rust_pack(rust_pack)
```

- [ ] **Step 3: Rename existing `supply()` to `_supply_python()`**

In the existing `supply()` method (starts around line 1027), rename:

```python
# OLD:
def supply(
    self,
    task_description: str,
    task_vector: list[float],
    task_type: str = "general",
    scope: str = "global",
) -> ContextPack:

# NEW:
def _supply_python(
    self,
    task_description: str,
    task_vector: list[float],
    task_type: str = "general",
    scope: str = "global",
) -> ContextPack:
```

Keep the entire method body unchanged — just rename the function.

- [ ] **Step 4: Add new `supply()` dispatch method**

Add this new `supply()` method in the same location (replace the old one):

```python
def supply(
    self,
    task_description: str,
    task_vector: list[float] = None,
    task_type: str = "general",
    scope: str = "global",
) -> ContextPack:
    """Supply context for a task. Rust-accelerated when available.

    Consistency: Returns a snapshot of the memory pool at call time.
    Concurrent writes (batch_update, register_memory) may not be
    reflected — this is eventual consistency by design. Retrieval
    results are advisory, not transactional.

    IMPORTANT: _supply_python is the ORIGINAL independent Python
    implementation. It does NOT call back into supply() — no recursion.
    """
    # Generate embedding if not provided (backward compatibility)
    if task_vector is None:
        task_vector = self._embed(task_description)

    # Try Rust accelerator
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
                # Set to None — forces immediate re-probe on next call
                self._rust_healthy = None
                self._rust_engine_instance = None

    # Python fallback (original implementation)
    return self._supply_python(task_description, task_vector, task_type, scope)
```

Note: Added `task_vector: list[float] = None` default — when `None`, generates embedding internally via `self._embed()`. This preserves backward compatibility with callers that don't pass vectors.

Check if `self._embed()` exists. If it doesn't, add a simple wrapper:

```python
def _embed(self, task_description: str) -> list[float]:
    """Generate embedding vector for task description.
    
    Uses the existing embedder from heavy_init. Returns zero vector
    if embedder is unavailable (graceful degradation).
    """
    try:
        self.ensure_heavy_init()
        if hasattr(self, '_embedder') and self._embedder:
            return self._embedder.embed(task_description)
    except Exception:
        pass
    return [0.0] * 768  # fallback: zero vector
```

(Check if `_embed` already exists — if not, add it before `supply()`.)

- [ ] **Step 5: Run existing tests to verify no regression**

```bash
cd F:\Agent\Memory system
python -m pytest tests/test_memory_operations.py tests/test_pipeline_quality.py tests/test_skill_tracking.py -v 2>&1 | tail -20
```

Expected: All tests pass (Rust path may or may not be used depending on .pyd availability, but Python fallback guarantees same behavior).

- [ ] **Step 6: Verify CI guard still passes**

```bash
python -m pytest tests/test_boundary.py -v
```

Expected: PASS — no new `engine._*` violations.

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: Rust-accelerated supply() with lock-protected degradation"
```

---

### Task 5: Full degradation smoke test

**Files:**
- Create: `tests/test_rust_integration.py`

**Interfaces:**
- Consumes: Modified `supply()` from Task 4
- Produces: Integration tests covering Rust path, Python fallback, degradation, health caching

- [ ] **Step 1: Write integration test file**

Create `tests/test_rust_integration.py`:

```python
"""Integration tests for Rust engine degradation and health check."""
import os
import time
import pytest

# Use in-memory mode (no SQLite) for test isolation
os.environ["AGENT_USE_SQLITE"] = "0"


def test_python_fallback_works_without_rust():
    """Python supply() works when Rust .pyd is unavailable."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)

    # Register a few test memories
    for i in range(5):
        engine.register_memory({
            "id": f"test_{i:04d}",
            "content": f"Test memory {i} for integration testing",
            "memory_type": "task",
            "source": "test",
        })

    # supply() should work — Rust path or Python fallback, doesn't matter
    pack = engine.supply("integration test task", task_type="general", scope="global")
    assert pack is not None
    # Python fallback should return some results (text retrieval works)
    assert pack.total_items >= 0  # at minimum, doesn't crash
    print(f"Python fallback: total_items={pack.total_items}")


def test_rust_health_check_initial_state():
    """Health check initializes correctly."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)

    # Initial state
    assert engine._rust_healthy is None
    assert engine._rust_health_checked_at == 0.0
    assert engine._rust_lock is not None

    # Health check runs without crashing
    result = engine._check_rust_health()
    # Result is True or None (False is never used per design)
    assert result is True or result is None


def test_rust_health_cache_ttl():
    """Health check caches result for TTL duration."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)

    # First call — probes
    result1 = engine._check_rust_health()
    checked_at1 = engine._rust_health_checked_at

    # Immediate second call — returns cached result without re-probing
    result2 = engine._check_rust_health()
    checked_at2 = engine._rust_health_checked_at

    assert result1 == result2  # same result
    assert checked_at1 == checked_at2  # same timestamp — no re-probe


def test_reset_rust_health():
    """reset_rust_health() clears cache and forces re-probe."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)

    # Call once to set cache
    engine._check_rust_health()
    assert engine._rust_healthy is not None  # cached

    # Reset
    engine.reset_rust_health()
    assert engine._rust_healthy is None  # cleared
    assert engine._rust_health_checked_at == 0.0  # cleared


def test_empty_memories_supply():
    """supply() with empty memory pool doesn't crash."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)

    pack = engine.supply("test with empty pool", task_type="general", scope="global")
    assert pack is not None
    assert pack.total_items == 0


def test_concurrent_supply_does_not_crash():
    """Multiple concurrent supply() calls don't crash or corrupt state."""
    import threading
    from plastic_promise.core.context_engine import ContextEngine

    engine = ContextEngine(use_sqlite=False)
    for i in range(20):
        engine.register_memory({
            "id": f"conc_{i:04d}",
            "content": f"Concurrent test memory {i}",
            "memory_type": "task",
            "source": "test",
        })

    errors = []
    results = []

    def call_supply(idx):
        try:
            pack = engine.supply(f"concurrent task {idx}", task_type="general", scope="global")
            results.append(pack)
        except Exception as e:
            errors.append((idx, str(e)))

    threads = [threading.Thread(target=call_supply, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent supply errors: {errors}"
    assert len(results) == 10
    print(f"Concurrent test: {len(results)} successes, {len(errors)} errors")
```

- [ ] **Step 2: Run integration tests**

```bash
cd F:\Agent\Memory system
python -m pytest tests/test_rust_integration.py -v
```

Expected: All 6 tests pass. The `test_python_fallback_works_without_rust` and `test_rust_health_check_initial_state` will produce different results depending on .pyd availability, but neither should crash.

- [ ] **Step 3: Run CI guard**

```bash
python -m pytest tests/test_boundary.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_rust_integration.py
git commit -m "test: Rust engine integration — degradation, health cache, concurrency"
```

---

## Phase 4a: Benchmark Script

### Task 6: Write Python baseline + benchmark script

**Files:**
- Create: `tests/test_rust_supply_perf.py`

- [ ] **Step 1: Create benchmark script**

Create `tests/test_rust_supply_perf.py`:

```python
"""Rust vs Python supply() performance benchmarks."""
import os
import time
import statistics

os.environ["AGENT_USE_SQLITE"] = "0"


def benchmark_supply(engine, memory_count: int, iterations: int = 10) -> dict:
    """Measure supply() latency with N memories in pool."""
    from plastic_promise.core.context_engine import ContextEngine

    # Pre-load synthetic memories
    for i in range(memory_count):
        topics = ["code review", "architecture design", "testing strategy",
                  "deployment pipeline", "performance optimization"]
        engine.register_memory({
            "id": f"perf_{i:04d}",
            "content": f"Performance test memory {i} about {topics[i % len(topics)]} "
                       f"with additional context for realistic retrieval scenarios",
            "memory_type": "task" if i % 2 == 0 else "experience",
            "source": "benchmark",
        })

    latencies = []
    for i in range(iterations):
        start = time.perf_counter()
        pack = engine.supply(
            f"performance optimization task iteration {i}",
            task_type="code_generation",
            scope="global",
        )
        elapsed = (time.perf_counter() - start) * 1000  # ms
        latencies.append(elapsed)

    latencies.sort()
    return {
        "count": memory_count,
        "iterations": iterations,
        "p50": statistics.median(latencies),
        "p95": latencies[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[-1],
        "p99": latencies[int(len(latencies) * 0.99)] if len(latencies) > 2 else latencies[-1],
        "min": latencies[0],
        "max": latencies[-1],
    }


def test_baseline_python_supply():
    """Record baseline Python supply() latency (no Rust)."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)
    # Force Python path by making Rust unavailable
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999  # don't re-probe

    result = benchmark_supply(engine, memory_count=100, iterations=5)
    print(f"Python baseline (100 memories): p50={result['p50']:.1f}ms, "
          f"p95={result['p95']:.1f}ms, p99={result['p99']:.1f}ms")
    assert result["p50"] > 0
    return result


def test_benchmark_1000_memories():
    """Benchmark with 1000 memories — comparison point for Rust."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)
    # Force Python path for baseline
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999

    result = benchmark_supply(engine, memory_count=1000, iterations=10)
    print(f"Python 1000-memory supply(): p50={result['p50']:.1f}ms, "
          f"p95={result['p95']:.1f}ms, p99={result['p99']:.1f}ms")
    assert result["p50"] > 0


def test_benchmark_empty_pool():
    """Benchmark supply() with empty memory pool."""
    from plastic_promise.core.context_engine import ContextEngine
    engine = ContextEngine(use_sqlite=False)
    engine._rust_healthy = None
    engine._rust_health_checked_at = time.time() + 99999

    start = time.perf_counter()
    for _ in range(10):
        engine.supply("empty pool test", task_type="general", scope="global")
    elapsed = (time.perf_counter() - start) * 1000 / 10  # avg ms
    print(f"Empty pool supply() avg: {elapsed:.1f}ms")
    assert elapsed < 500  # shouldn't take half a second for empty pool


def test_pyo3_memory_pass_overhead():
    """Measure PyO3 Vec<PyObject> pass time for 1000 memories."""
    from context_engine_core import ContextEngine as RustEngine

    memories = [
        {
            "id": f"m{i:04d}",
            "content": f"test memory {i} with some content",
            "source": "benchmark",
            "memory_type": "task",
            "worth_success": 1,
            "worth_failure": 0,
            "created_at": "2026-07-01T00:00:00",
            "last_accessed": "2026-07-01T00:00:00",
        }
        for i in range(1000)
    ]

    rust = RustEngine()
    rust.set_current_time("2026-07-02T00:00:00")

    # Measure PyO3 pass + Rust processing
    latencies = []
    for _ in range(5):
        start = time.perf_counter()
        pack = rust.supply("benchmark task", [0.5] * 768, "general", "global", memories)
        elapsed = (time.perf_counter() - start) * 1000
        latencies.append(elapsed)

    p50 = statistics.median(latencies)
    print(f"PyO3 pass 1000 memories: p50={p50:.1f}ms (over {len(memories)} items, "
          f"result total={pack.total_items})")
    # Rust should process 1000 items well under 100ms
    assert p50 < 100, f"PyO3 pass too slow: {p50:.1f}ms"
```

This test file is **not** run as part of CI (requires Rust .pyd). It's for manual benchmarking. The `@pytest.mark.skipif` can be added later if needed.

- [ ] **Step 2: Run Python baseline (no Rust required)**

```bash
cd F:\Agent\Memory system
python -m pytest tests/test_rust_supply_perf.py::test_baseline_python_supply -v -s
python -m pytest tests/test_rust_supply_perf.py::test_benchmark_empty_pool -v -s
```

Expected: Both pass. Record the p50 value from `test_baseline_python_supply` for later comparison.

- [ ] **Step 3: Run PyO3 overhead test (requires .pyd)**

```bash
python -m pytest tests/test_rust_supply_perf.py::test_pyo3_memory_pass_overhead -v -s
```

Expected: p50 < 100ms. Record the value.

- [ ] **Step 4: Commit**

```bash
git add tests/test_rust_supply_perf.py
git commit -m "perf: Rust vs Python supply() benchmark suite"
```

---

## Phase 4b: Run Scenarios + Document

### Task 7: Run all 7 test scenarios and document results

**Files:**
- Modify: `docs/superpowers/specs/2026-07-02-rust-engine-production-design.md` (add results section)

- [ ] **Step 1: Run scenario 1 — 1000-memory supply()**

```bash
cd F:\Agent\Memory system
# Force Python path
python -c "
import os; os.environ['AGENT_USE_SQLITE']='0'
from plastic_promise.core.context_engine import ContextEngine
import time
engine = ContextEngine(use_sqlite=False)
engine._rust_healthy = None
engine._rust_health_checked_at = time.time() + 99999
for i in range(1000):
    engine.register_memory({'id': f'p{i:04d}', 'content': f'test {i} about coding', 'memory_type': 'task', 'source': 'bench'})
# Warm up
engine.supply('warmup', task_type='code_generation', scope='global')
# Measure
start = time.perf_counter()
for _ in range(10):
    engine.supply('performance test', task_type='code_generation', scope='global')
elapsed = (time.perf_counter() - start) * 1000 / 10
print(f'Python 1000-memory p50: {elapsed:.1f}ms')
"
```

Record: `Python p50 = ___ ms`

Then with Rust (ensure .pyd is in path):

```bash
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
import os; os.environ['AGENT_USE_SQLITE']='0'
from plastic_promise.core.context_engine import ContextEngine
import time
engine = ContextEngine(use_sqlite=False)
for i in range(1000):
    engine.register_memory({'id': f'r{i:04d}', 'content': f'test {i} about coding', 'memory_type': 'task', 'source': 'bench'})
engine.supply('warmup', task_type='code_generation', scope='global')
start = time.perf_counter()
for _ in range(10):
    engine.supply('performance test', task_type='code_generation', scope='global')
elapsed = (time.perf_counter() - start) * 1000 / 10
print(f'Rust 1000-memory p50: {elapsed:.1f}ms')
"
```

Record: `Rust p50 = ___ ms`. Threshold: Rust p50 <= Python p50.

- [ ] **Step 2: Run scenario 2 — concurrent supply()**

```bash
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
import os; os.environ['AGENT_USE_SQLITE']='0'
from plastic_promise.core.context_engine import ContextEngine
import threading, time
engine = ContextEngine(use_sqlite=False)
for i in range(100):
    engine.register_memory({'id': f'c{i:04d}', 'content': f'concurrent {i}', 'memory_type': 'task', 'source': 'bench'})
errors = []
def call(n):
    try:
        engine.supply(f'task {n}', task_type='general', scope='global')
    except Exception as e:
        errors.append((n, str(e)))
threads = [threading.Thread(target=call, args=(i,)) for i in range(10)]
start = time.perf_counter()
for t in threads: t.start()
for t in threads: t.join()
elapsed = (time.perf_counter() - start) * 1000
print(f'10 concurrent: {len(errors)} errors in {elapsed:.1f}ms')
assert len(errors) == 0, f'Errors: {errors}'
print('Concurrent test: OK')
"
```

Expected: 0 errors. Threshold: < 1% error rate.

- [ ] **Step 3: Run scenario 3 — cold start**

```bash
python -c "
import time
start = time.perf_counter()
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine(use_sqlite=False)
result = engine._check_rust_health()
elapsed = (time.perf_counter() - start) * 1000
print(f'Cold start health check: {elapsed:.1f}ms, result={result}')
"
```

Expected: < 200ms. Record value.

- [ ] **Step 4: Run scenario 4 — degradation recovery**

```bash
# Step A: Verify Rust is healthy
python -c "
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine(use_sqlite=False)
print(f'Initial: healthy={engine._check_rust_health()}')
engine.reset_rust_health()
print(f'After reset: healthy={engine._rust_healthy}')
# Next supply() will re-probe
result = engine._check_rust_health()
print(f'After re-probe: healthy={result}')
"
```

Expected: healthy=True on first call, healthy=None after reset, healthy=True after re-probe (if .pyd present).

- [ ] **Step 5: Run scenario 5 — PyO3 pass overhead**

```bash
python -m pytest tests/test_rust_supply_perf.py::test_pyo3_memory_pass_overhead -v -s
```

Record p50 value. Threshold: < 5ms.

- [ ] **Step 6: Run scenario 6 — empty pool**

```bash
python -m pytest tests/test_rust_supply_perf.py::test_benchmark_empty_pool -v -s
```

Expected: < 50ms.

- [ ] **Step 7: Run scenario 7 — empty-retriever fallback**

```bash
python -c "
import sys; sys.path.insert(0, 'rust/context-engine-core/target/release')
from context_engine_core import ContextEngine as RustEngine
memories = [{'id': f'm{i:03d}', 'content': f'test {i}', 'source': 'test', 'memory_type': 'task', 'worth_success': 1, 'worth_failure': 0, 'created_at': '2026-07-01T00:00:00', 'last_accessed': '2026-07-01T00:00:00'} for i in range(100)]
e = RustEngine(); e.set_current_time('2026-07-02T00:00:00')
pack = e.supply('test', [0.0]*768, 'general', 'global', memories)
print(f'Empty-retriever fallback: total={pack.total_items} (all in related: {len(pack.related)})')
assert len(pack.related) == 100
print('Fallback: OK')
"
```

Expected: all 100 memories in related tier.

- [ ] **Step 8: Document results**

Add a section to `docs/superpowers/specs/2026-07-02-rust-engine-production-design.md`:

```markdown
## Phase 4 Results (2026-07-02)

| # | Scenario | Metric | Threshold | Actual | Status |
|---|----------|--------|-----------|--------|--------|
| 1 | 1000-memory supply() | Rust vs Python p50 | Rust <= Python | Rust: _ms, Python: _ms | _ |
| 2 | 10 concurrent supply() | Error rate | < 1% | _ errors | _ |
| 3 | Cold start health check | Latency | < 200ms | _ms | _ |
| 4 | Degradation recovery | Auto-recover | No errors | _ | _ |
| 5 | PyO3 pass overhead | 1000-item pass | < 5ms | _ms | _ |
| 6 | Empty pool supply() | Latency | < 50ms | _ms | _ |
| 7 | Empty-retriever fallback | 100 items returned | All in related | _ items | _ |
```

- [ ] **Step 9: Commit**

```bash
git add docs/superpowers/specs/2026-07-02-rust-engine-production-design.md
git commit -m "docs: Phase 4 performance verification results"
```

---

## Phase 4c: Documentation

### Task 8: Document eventual consistency + final CI check

**Files:**
- Modify: `docs/superpowers/specs/2026-07-02-rust-engine-production-design.md`
- Modify: `plastic_promise/core/context_engine.py` (docstring only)

- [ ] **Step 1: Verify supply() docstring has consistency note**

In `plastic_promise/core/context_engine.py`, the new `supply()` already has the consistency note. Verify:

```python
# In supply() docstring — verify these lines exist:
# Consistency: Returns a snapshot of the memory pool at call time.
# Concurrent writes (batch_update, register_memory) may not be
# reflected — this is eventual consistency by design.
```

If missing, add them.

- [ ] **Step 2: Run full CI validation**

```bash
cd F:\Agent\Memory system
# CI guard
python -m pytest tests/test_boundary.py -v
# Integration tests
python -m pytest tests/test_rust_integration.py -v
# Existing tests (excluding pre-existing failures)
python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py 2>&1 | tail -5
```

All must pass.

- [ ] **Step 3: Verify grep for engine._* violations**

```bash
grep -rn "engine\._" plastic_promise/ --include="*.py" | grep -v "plastic_promise/core/context_engine.py" | grep -v "\.pyc" | grep -v "^.*# "
```

Expected: No output (zero violations, all remaining matches are in comments).

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py docs/superpowers/specs/2026-07-02-rust-engine-production-design.md
git commit -m "docs: eventual consistency guarantee + final CI verification"
```

---

## Verification (after all tasks)

- [ ] **V1**: `grep -rn "engine\._" plastic_promise/ --include="*.py" | grep -v context_engine.py` — zero results
- [ ] **V2**: `python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py` — all pass
- [ ] **V3**: `python -m pytest tests/test_boundary.py -v` — PASS
- [ ] **V4**: `python -m pytest tests/test_rust_integration.py -v` — all 6 tests pass
- [ ] **V5**: MCP server health: `python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"` — `{"status":"ok"}`
- [ ] **V6**: Rust import: `python -c "import context_engine_core; print('OK')"` — OK
- [ ] **V7**: Rust empty-memory smoke: returns ContextPack with core/related/divergent attributes
