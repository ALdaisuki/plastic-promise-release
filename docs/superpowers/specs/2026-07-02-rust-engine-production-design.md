# Rust Core Engine — Production Deployment Design

> **Date**: 2026-07-02
> **Status**: Design — awaiting approval
> **Scope**: Phase 3 (Degradation & Resilience) + Phase 4 (Performance Verification)
> **Depends on**: Phase 1+2 (Boundary Hardening) — ✅ complete
> **Principle**: Rust is the spine, Python is the brain. Rust accelerates supply(); Python retains ownership of storage and orchestration.

## Problem

Rust `context_engine_core` compiled successfully (2.4MB `.pyd`, importable), but:
- Not integrated into the running system — Python `ContextEngine.supply()` never calls Rust
- Rust `supply()` reads from its own `:memory:` SQLite, unaware of Python's memory pool
- Rust `HybridRetriever` is a placeholder — retrieval quality unknown
- No degradation mechanism exists if Rust is unavailable

## Architecture

```
Python ContextEngine.supply()
  │
  ├─ 1. 生成 task_vector (embedder)
  │
  ├─ 2. 候选记忆快照 → list[dict] (PyO3 原生传递，无 JSON 序列化)
  │
  ├─ 3. _rust_healthy? ─── 是 ──→ Rust supply(memories)
  │       │                              │
  │       │                     ┌────────┴────────┐
  │       │                     │ 检索管道成功     │ → 返回 ContextPack ✅
  │       │                     │ 检索管道失败     │ → 抛异常              │
  │       │                     └────────┬────────┘                       │
  │       │                              │                                │
  │       └── 否 ────────────────────────┘                                │
  │                                                                       │
  ├─ 4. 捕获异常 → 标记 healthy=False (TTL/2 快速重试)                      │
  │              → 降级 Python supply()                                    │
  │                                                                       │
  └─ 5. 返回 ContextPack（Rust 和 Python 统一格式）                         │
```

### Key Decisions

| Decision | Rationale |
|----------|-----------|
| Rust is **stateless** | No persistence in Rust — all memories come from Python via `Vec<PyObject>` |
| **PyO3 native passing** (not JSON) | Avoid 50-100ms serialize/deserialize overhead for 1000 memories |
| Rust `supply()` is `&self` | Pure computation — no internal state mutation, fully reentrant |
| Python owns storage | `plastic_memory.db` + LanceDB remain Python's responsibility |
| Health check with **TTL/2 backoff** | Failed → retry in 2.5min (not 5min), healthy → recheck every 5min |
| Manual reset available | `engine.reset_rust_health()` for ops to force re-probe |

---

## Phase 3: Degradation & Resilience (~1.5 days)

### 3a. Rust `supply()` Interface Change

Current signature (self-contained):
```rust
fn supply(&mut self, task: String, task_vector: Vec<f32>, 
          task_type: String, scope: String) -> ContextPack
// reads memories from self.storage.list()
```

New signature (stateless, memories from Python):
```rust
#[pyo3(signature = (task_description, task_vector, task_type, scope, memories))]
fn supply(
    &self,                                    // &self not &mut self
    task_description: String,
    task_vector: Vec<f32>,
    task_type: String,
    scope: String,
    memories: Vec<PyObject>,                  // PyO3 native — no JSON
) -> PyResult<ContextPack>
```

Rust-side field extraction (no serde):
```rust
let mut item_lookup: HashMap<String, (String, String)> = HashMap::new();
let mut memory_index: HashMap<String, MemoryRecord> = HashMap::new();

for py_mem in &memories {
    let id: String = py_mem.get_item("id")?.extract()?;
    let content: String = py_mem.get_item("content")?.extract()?;
    let source: String = py_mem.get_item("source")?.extract()?;
    let memory_type: String = py_mem.get_item("memory_type")?.extract()?;
    let worth_success: f64 = py_mem.get_item("worth_success")?.extract()?;
    let worth_failure: f64 = py_mem.get_item("worth_failure")?.extract()?;
    let created_at: String = py_mem.get_item("created_at")?.extract()?;
    let last_accessed_at: String = py_mem.get_item("last_accessed")?.extract()?;
    
    item_lookup.insert(id.clone(), (content.clone(), source.clone()));
    memory_index.insert(id.clone(), MemoryRecord { 
        id, content, source, memory_type, worth_success, worth_failure,
        created_at, last_accessed_at, ..Default::default() 
    });
}
// ... rest of supply() pipeline unchanged
```

**Files**: `rust/context-engine-core/src/context_engine.rs`

### 3b. HybridRetriever — Staged Approach

The Rust `HybridRetriever` is currently a placeholder. Rather than block on a full Rust reimplementation, we stage it:

**Stage 0 (this phase)**: Pass-through — Rust's retrieval pipeline runs with placeholder, validates the end-to-end flow works (degradation, health check, ContextPack conversion). Retrieval quality may be lower than Python — acceptable because:
- Goal is proving the architecture works
- Degradation mechanism handles any quality issues transparently

**Stage 1 (follow-up)** — NOT in this plan:
- Vector retrieval: in-memory cosine similarity over embedded memories
- BM25: basic term frequency with IDF from tantivy or hand-rolled
- RRF fusion: `rrf_fuse()` implementation

### 3c. Python Health Check + Degradation

```python
import time
import threading
import logging

logger = logging.getLogger(__name__)

class ContextEngine:
    def __init__(self):
        # ... existing init ...
        self._rust_healthy: bool | None = None       # None = unchecked
        self._rust_health_checked_at: float = 0.0     # epoch timestamp
        self._rust_health_ttl: float = 300.0          # 5 minutes
        self._rust_engine_instance = None              # cached Rust engine

    def _check_rust_health(self) -> bool:
        """Probe Rust core availability. Caches result for TTL seconds.
        
        On failure, sets checked_at to TTL/2 ago so retry happens sooner
        (2.5min instead of 5min). On success, full TTL applies.
        """
        now = time.time()
        if self._rust_healthy is not None and \
           (now - self._rust_health_checked_at) < self._rust_health_ttl:
            return self._rust_healthy
        
        try:
            from context_engine_core import ContextEngine as RustEngine
            # Smoke test: supply with empty memories
            engine = RustEngine()
            engine.set_current_time(datetime.now().isoformat())
            pack = engine.supply("test", [0.0]*768, "general", "global", [])
            # Validate response shape
            assert hasattr(pack, 'core')
            assert hasattr(pack, 'related')
            self._rust_engine_instance = engine
            self._rust_healthy = True
        except Exception as e:
            logger.warning("Rust engine health check failed: %s", e)
            self._rust_healthy = False
            self._rust_engine_instance = None
        
        self._rust_health_checked_at = now
        return self._rust_healthy

    def reset_rust_health(self):
        """Force re-probe Rust health on next supply() call.
        
        Use when: Rust .pyd was deployed, environment changed,
        or health was falsely marked unhealthy.
        """
        self._rust_healthy = None
        self._rust_health_checked_at = 0.0
        self._rust_engine_instance = None
        logger.info("Rust health reset — will re-probe on next supply()")

    def _supply_rust(self, task: str, task_vector: list,
                     task_type: str, scope: str) -> ContextPack:
        """Rust-accelerated supply path."""
        from context_engine_core import ContextEngine as RustEngine
        
        # Build memory list for PyO3 — pass raw dicts, no JSON
        memories = [
            self._memories[mid]
            for mid in self._memories
        ]
        
        rust = RustEngine()
        rust.set_current_time(datetime.now().isoformat())
        rust_pack = rust.supply(task, task_vector, task_type, scope, memories)
        return self._convert_rust_pack(rust_pack)

    def _convert_rust_pack(self, rust_pack) -> ContextPack:
        """Convert Rust ContextPack to Python ContextPack format.
        
        Rust returns PyO3 objects with .core/.related/.divergent/.activated_principles.
        We convert to the Python dict-based format that callers expect.
        """
        pack = ContextPack()
        pack.core = [
            {"id": item.id, "content": item.content, "relevance": item.relevance,
             "source": item.source, "freshness": item.freshness, "layer": item.layer,
             "is_principle": item.is_principle, "worth_score": item.worth_score}
            for item in rust_pack.core
        ]
        pack.related = [
            {"id": item.id, "content": item.content, "relevance": item.relevance,
             "source": item.source, "freshness": item.freshness, "layer": item.layer,
             "is_principle": item.is_principle, "worth_score": item.worth_score}
            for item in rust_pack.related
        ]
        pack.divergent = [
            {"id": item.id, "content": item.content, "relevance": item.relevance,
             "source": item.source, "freshness": item.freshness, "layer": item.layer,
             "is_principle": item.is_principle, "worth_score": item.worth_score}
            for item in rust_pack.divergent
        ]
        pack.activated_principles = list(rust_pack.activated_principles)
        pack.audit_metadata = dict(rust_pack.audit_metadata) if rust_pack.audit_metadata else {}
        return pack

    def supply(self, task_description: str, task_type: str = "general",
               scope: str = "global"):
        """Supply context for a task. Rust-accelerated when available.
        
        Consistency: Returns a snapshot of the memory pool at call time.
        Concurrent writes (batch_update, register_memory) may not be
        reflected — this is eventual consistency by design. Retrieval
        results are advisory, not transactional.
        """
        task_vector = self._embed(task_description)
        
        if self._check_rust_health():
            try:
                return self._supply_rust(
                    task_description, task_vector, task_type, scope
                )
            except Exception as e:
                logger.warning(
                    "Rust supply failed, falling back to Python: %s", e
                )
                self._rust_healthy = False
                # Accelerate retry: check again in TTL/2 (2.5min)
                self._rust_health_checked_at = (
                    time.time() - self._rust_health_ttl / 2
                )
                self._rust_engine_instance = None
        
        return self._supply_python(task_description, task_vector, task_type, scope)
```

### 3d. Degradation State Machine

```
         ┌──────────────┐
         │ _rust_healthy │
         │   = None      │── 首次 supply() ──→ 探测
         └──────────────┘                       ↓
                                    ┌───────────┴───────────┐
                                    │   import context_     │
                                    │   engine_core OK?     │
                                    └───────────┬───────────┘
                                   ✅            │          ❌
                              ┌─────┘            └─────┐
                         healthy=True            healthy=False
                         ttl_full=300s           ttl_full=300s
                              │                       │
                         supply_rust()          supply_python()
                              │                       │
                     ┌───────┴────────┐         到期 │
                     │                │         重新探测
                  成功返回          失败 ↓
                                    healthy=False
                              checked_at -= TTL/2
                              (2.5min 后重试而非 5min)
                                    ↓
                              supply_python() (本次)
```

Edge cases:
- **Cold start with no Rust**: `ImportError` → `healthy=False` → Python path. Retry in 5min.
- **Rust crashes mid-supply**: Exception caught → mark unhealthy → backoff 2.5min → Python fallback.
- **Rust recovers 1min after failure**: System waits 1.5min more (2.5min - 1min elapsed) → re-probes → restores.
- **Manual intervention**: `engine.reset_rust_health()` → immediate re-probe on next `supply()`.
- **TTL expiry while healthy**: Re-probes → if still healthy, extends cache; if Rust disappeared, flips to unhealthy.

### 3e. Files

| File | Change |
|------|--------|
| `rust/context-engine-core/src/context_engine.rs` | `supply()`: `&mut self` → `&self`, add `memories: Vec<PyObject>` param, remove `self.storage.list()` call |
| `plastic_promise/core/context_engine.py` | Add `_check_rust_health()`, `reset_rust_health()`, `_supply_rust()`, `_convert_rust_pack()`; modify `supply()` dispatch |
| `rust/context-engine-core/Cargo.toml` | Rebuild after changes → copy `.dll` → `.pyd` |

---

## Phase 4: Performance Verification (~0.5 day)

### 4a. Benchmark Script

```python
"""tests/test_rust_supply_perf.py — Rust vs Python supply() benchmarks."""
import time
import statistics

def benchmark_1000_memories(engine, iterations=10):
    """Measure supply() latency with 1000 memories in pool."""
    # Pre-load 1000 synthetic memories
    for i in range(1000):
        engine.register_memory({
            "id": f"perf_{i:04d}",
            "content": f"Performance test memory {i} with some varied content "
                       f"about {'code' if i%3==0 else 'design' if i%3==1 else 'testing'}",
            "memory_type": "task",
            "source": "benchmark",
        })
    
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        pack = engine.supply("performance optimization task", "code_generation", "global")
        elapsed = (time.perf_counter() - start) * 1000  # ms
        latencies.append(elapsed)
    
    latencies.sort()
    return {
        "p50": latencies[len(latencies)//2],
        "p95": latencies[int(len(latencies)*0.95)],
        "p99": latencies[int(len(latencies)*0.99)],
        "min": latencies[0],
        "max": latencies[-1],
    }

def test_json_serialize_overhead():
    """Measure JSON serialize cost for 1000 memories."""
    import json
    memories = [{"id": f"m{i}", "content": f"test {i}", "source": "test",
                  "memory_type": "task", "worth_success": 1, "worth_failure": 0,
                  "created_at": "2026-01-01T00:00:00", "last_accessed_at": "2026-01-01T00:00:00"}
                for i in range(1000)]
    
    start = time.perf_counter()
    s = json.dumps(memories)
    serialize_ms = (time.perf_counter() - start) * 1000
    
    start = time.perf_counter()
    _ = json.loads(s)
    deserialize_ms = (time.perf_counter() - start) * 1000
    
    return {"serialize_ms": serialize_ms, "deserialize_ms": deserialize_ms,
            "total_ms": serialize_ms + deserialize_ms, "bytes": len(s)}
```

### 4b. Test Scenarios

| # | Scenario | Metric | Threshold |
|---|----------|--------|-----------|
| 1 | 1000-memory `supply()` | Rust vs Python p50/p95 latency | Rust ≤ Python p50 |
| 2 | 10 concurrent `supply()` calls | Error rate (threaded) | < 1% errors |
| 3 | Cold start — `_check_rust_health()` | First-call latency | < 200ms |
| 4 | Degradation recovery | Healthy → fail → unhealthy → TTL expire → re-probe → healthy | Automatic, no errors |
| 5 | PyO3 memory passing overhead | 1000-memory `Vec<PyObject>` pass time | < 5ms |
| 6 | Empty pool `supply()` | Latency with 0 memories | < 50ms |

### 4c. Manual Verification

```bash
# 1. Verify Rust import
python -c "from context_engine_core import ContextEngine; print('OK')"

# 2. Verify supply() with empty memories
python -c "
from context_engine_core import ContextEngine as RustEngine
e = RustEngine()
e.set_current_time('2026-07-02T00:00:00')
pack = e.supply('test task', [0.0]*768, 'general', 'global', [])
print(f'Core: {len(pack.core)}, Related: {len(pack.related)}, Principles: {len(pack.activated_principles)}')
"

# 3. Verify degradation on missing .pyd
# (rename .pyd → .pyd.bak, run supply, verify Python fallback, restore .pyd)

# 4. Run full test suite
python -m pytest tests/ -v --ignore=tests/test_safety_net_daemon.py
```

---

## Implementation Order

```
Phase 3a: Rust supply() signature change     (~2h)
  → commit: "refactor(rust): make supply() stateless — &self + Vec<PyObject>"

Phase 3b: Python health check + degradation  (~3h)
  → commit: "feat: health-checked Rust supply() with TTL/2 degradation"

Phase 3c: Integration test + smoke test      (~1h)
  → commit: "test: Rust supply integration — smoke + degradation"

Phase 4a: Benchmark script                    (~1h)
  → commit: "perf: Rust vs Python supply() benchmark"

Phase 4b: Run all 6 scenarios, record results (~2h)
  → commit: "docs: Phase 4 performance verification results"

Phase 4c: Document eventual consistency       (~0.5h)
  → commit: "docs: supply() consistency guarantees"
```

---

## Non-Goals (explicitly out of scope)

- ❌ Rust `batch_update`, `update_memory_fields`, graph CRUD — Python retains these (Phase 1 public API)
- ❌ Full `HybridRetriever` implementation — Stage 1 follow-up
- ❌ Shared SQLite database — Rust is stateless, Python owns storage
- ❌ Rust-side LanceDB or embedder — stays in Python
- ❌ SoulLoop/PrincipleManager migration to Rust — Python business logic stays in Python

---

## Verification

| Check | Method |
|-------|--------|
| Rust supply() returns valid ContextPack | Unit test with 0/10/100 memories |
| Python fallback works when Rust missing | Rename .pyd → run supply → verify Python path |
| Health cache TTL works | Force unhealthy → wait > TTL → verify re-probe |
| TTL/2 backoff on failure | Force Rust exception → verify checked_at = now - TTL/2 |
| `reset_rust_health()` works | Call reset → verify immediate re-probe |
| Concurrent supply() safety | 10 threads, 100 iterations each, verify 0 crashes |
| Boundary CI guard still passes | `python -m pytest tests/test_boundary.py -v` |
