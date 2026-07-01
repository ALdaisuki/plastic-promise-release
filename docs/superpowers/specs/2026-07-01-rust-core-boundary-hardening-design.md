# Rust Core Engine — Boundary Hardening Design

> **Date**: 2026-07-01
> **Status**: Approved
> **Scope**: Phase 1 (Boundary Solidification) + Phase 2 (Serialization Hardening)
> **Principle**: Rust is the spine (承载重量、保持稳定), Python is the brain (决策、编排、进化)

## Problem

The Rust core (`rust/context-engine-core/`) was designed as the primary engine, but the compiled `.dll`/`.pyd` was never successfully deployed. The Python "fallback" (`plastic_promise/core/context_engine.py`, 1,811 lines) became the de facto implementation, and **13 Python files** now directly access private `engine._*` fields (~100 violations), making it impossible to ever swap in the Rust core.

## Architecture Principle

```
Python (Brain)                    Rust (Spine)
┌─────────────────────┐          ┌──────────────────────────┐
│ SoulLoop             │          │ store/                   │
│ PrincipleManager     │──PyO3──▶│   store_memory()         │
│ DomainManager        │          │   list_memories()        │
│ StepAuditor          │          │   memory_stats()         │
│ MCP tool handlers    │          │                          │
│ Daemon scanners      │          │ supply/                  │
│ SkillEngine          │          │   supply() → ContextPack │
│                      │◀─────────│                          │
│                      │          │ stats/                   │
│                      │          │   memory_stats_json()    │
└─────────────────────┘          └──────────────────────────┘
```

**The rule**: Python never accesses Rust internals. All data crosses the boundary through typed PyO3 interfaces only. Python business logic (SoulLoop, PrincipleManager, DomainManager) stays in Python for iteration speed.

## What we do NOT do

- ❌ Don't move SoulLoop, PrincipleManager, DomainManager, or any business logic to Rust
- ❌ Don't add LanceDB/embedder to Rust (those stay in Python, behind the boundary)
- ❌ Don't change the behavior of `supply()` — only the transport layer
- ❌ Don't touch the Rust `context_engine.rs` retrieval logic
- ❌ Don't maintain dual implementations of the same feature

---

## Phase 1: Boundary Solidification (~2-3 days)

### Goal

Eliminate all direct `engine._*` field accesses from Python code. Replace with public methods on the Python `ContextEngine` class.

### 1a. New Public Methods on Python ContextEngine

These methods replace direct `_memories` / `_graph_*` / `_sqlite` access:

#### Memory CRUD (replace `engine._memories` dict access)

| Method | Replaces |
|--------|----------|
| `update_memory_fields(mid, **fields)` | `engine._memories[mid]["tags"] = x` — handles `tags`, `domain`, `tier`, `worth_success`, `worth_failure`, `access_count`, `last_accessed`, `decay_multiplier`, `effective_half_life`, `entity_ids` (existing `update_memory` only does `content`/`importance`/`category`) |
| `memory_exists(mid) -> bool` | `mid in engine._memories` |
| `get_memory_dict(mid) -> dict \| None` | `engine._memories.get(mid)` — returns raw dict for read-only field access (existing `get_memory` returns `MemoryRecord` object) |
| `iter_memories(scope=None, page_size=200) -> Iterator[dict]` | `for mid, mem in engine._memories.items()` — paginated to avoid full list allocation |
| `memory_ids() -> list[str]` | `engine._memories.keys()` |
| `get_memories_batch(mids: list[str]) -> list[dict]` | Repeated `engine._memories[mid]` in a loop |

#### Graph CRUD (replace `engine._graph_edges` / `engine._graph_nodes`)

| Method | Replaces |
|--------|----------|
| `add_graph_edge(from, to, relation, weight)` | `engine._graph_edges.append({...})` |
| `remove_graph_edge(from, to, relation)` | Manual list filtering |
| `has_graph_edge(edge_dict) -> bool` | `edge not in engine._graph_edges` |
| `get_graph_node(node_id) -> dict \| None` | `engine._graph_nodes.get(nid)` |
| `list_graph_nodes(type=None) -> list[dict]` | `engine._graph_nodes.items()` iteration |
| `list_graph_edges(relation=None) -> list[dict]` | `engine._graph_edges` iteration with filter |

#### Transaction Support

| Method | Replaces |
|--------|----------|
| `batch_update(updates: list[dict]) -> int` | Multiple `engine.update_memory()` calls in a loop |
| `begin_batch() / commit_batch() / rollback_batch()` | Direct `engine._sqlite._conn.commit()` |

### 1b. batch_update Atomicity

`batch_update` uses SQLite `SAVEPOINT` for atomicity:

```python
def batch_update(self, updates: list[dict]) -> int:
    """Apply multiple memory field updates atomically.
    
    Args:
        updates: [{"id": "...", "tags": [...], "domain": "..."}, ...]
    
    Returns:
        Number of records updated.
    
    If any update fails, ALL changes are rolled back via SAVEPOINT.
    """
    if not self._sqlite:
        return self._batch_update_in_memory(updates)
    
    with self._sqlite._conn:
        self._sqlite._conn.execute("SAVEPOINT batch_update")
        try:
            count = 0
            for upd in updates:
                upd_copy = dict(upd)  # don't mutate caller's dict
                mid = upd_copy.pop("id")
                if mid in self._memories:
                    self._memories[mid].update(upd_copy)
                    self._sqlite.upsert(mid, self._memories[mid])
                    count += 1
            self._sqlite._conn.execute("RELEASE batch_update")
            return count
        except Exception:
            self._sqlite._conn.execute("ROLLBACK TO batch_update")
            raise
```

### 1c. Files to Modify

| File | Violation Count | Changes |
|------|----------------|---------|
| `plastic_promise/core/context_engine.py` | N/A | Add ~15 public methods |
| `plastic_promise/mcp/tools/skill_tracking.py` | ~25 | Replace all `_memories`/`_graph_*` access |
| `plastic_promise/memory/pipeline.py` | ~15 | Replace `_memories` mutations |
| `plastic_promise/mcp/tools/memory.py` | ~12 | Replace dict iteration |
| `plastic_promise/mcp/server.py` | ~10 | Replace dict mutations |
| `plastic_promise/core/pack_index.py` | ~8 | Replace `_sqlite`/`_memories` |
| `plastic_promise/mcp/tools/context.py` | ~5 | Replace `_graph_nodes`/`_graph_edges` |
| `plastic_promise/core/principles.py` | ~4 | Replace `_graph_*` access |
| `plastic_promise/mcp/tools/domain_recall.py` | ~2 | Replace `_memories.items()` |
| `plastic_promise/pack.py` | ~2 | Replace `_memories`/`_graph_edges` |
| `plastic_promise/loop/soul_loop.py` | ~2 | Replace `_memories.values()` |
| `plastic_promise/memory/soul_memory.py` | ~6 | Replace `_sqlite._conn` access |
| `plastic_promise/core/lancedb_store.py` | ~1 | Replace `_memories` reference |
| `plastic_promise/core/review_engine.py` | ~2 | Replace `_memories` iteration |

---

## Phase 2: Data Serialization Hardening (~1-2 days)

### Goal

Reduce Python ↔ Rust data conversion overhead at the boundary.

### 2a. Paginated list_memories Iterator

Python-side pagination (not Rust-side cursor) to keep implementation simple:

```python
def list_memories_paginated(self, memory_type=None, source=None,
                             min_worth=None, scope=None,
                             page_size=200) -> Iterator[MemoryRecord]:
    """Yield MemoryRecords one page at a time.
    
    Avoids allocating a full list for large result sets.
    For 10K records at page_size=200: ~50 PyO3 boundary crossings.
    """
    offset = 0
    while True:
        page = self.list_memories(
            memory_type=memory_type, source=source,
            min_worth=min_worth, limit=page_size, scope=scope
        )
        if not page:
            break
        yield from page
        if len(page) < page_size:
            break
        offset += len(page)
```

### 2b. Rust-side batch_update

Add `batch_update` to the Rust `ContextEngine` PyO3 API:

```rust
#[pyo3(signature = (updates_json))]
pub fn batch_update(&mut self, updates_json: String) -> PyResult<usize> {
    let updates: Vec<BatchUpdateEntry> = serde_json::from_str(&updates_json)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    
    // All updates within a single storage transaction
    let count = self.storage.batch_update(&updates)
        .map_err(|e| PyRuntimeError::new_err(e))?;
    
    Ok(count)
}
```

### 2c. memory_stats_json — Compute in Rust, Return JSON Once

Already implemented in Rust (`context_engine.rs:338`). No change needed. The Python fallback's `memory_stats_json` (line 668) is only called when Rust is unavailable.

---

## Phase 3: Degradation & Resilience (with Release)

### Goal

Graceful degradation when Rust core is unavailable.

### 3a. Degradation State Machine

```python
class ContextEngine:
    def __init__(self):
        self._rust_core_healthy: bool | None = None  # None = unchecked
    
    def _check_rust_health(self) -> bool:
        """Probe Rust core. Caches result in _rust_core_healthy."""
        if self._rust_core_healthy is not None:
            return self._rust_core_healthy
        try:
            from context_engine_core import ContextEngine as RustEngine
            self._rust_engine = RustEngine()
            self._rust_engine.memory_stats_json()  # smoke test
            self._rust_core_healthy = True
            return True
        except Exception:
            self._rust_core_healthy = False
            return False
    
    def _with_rust_fallback(self, rust_method, py_fallback, *args, **kwargs):
        """Call rust_method; on failure, degrade and use py_fallback."""
        if self._rust_core_healthy is False:
            return py_fallback(*args, **kwargs)
        try:
            result = rust_method(*args, **kwargs)
            return result
        except Exception as e:
            logging.warning("Rust call failed, degrading: %s", e)
            self._rust_core_healthy = False
            return py_fallback(*args, **kwargs)
    
    def health_check(self) -> dict:
        """Re-probe Rust core health. Resets _rust_core_healthy to None."""
        self._rust_core_healthy = None
        return {
            "rust_available": self._check_rust_health(),
            "engine_version": "0.1.0-py" if not self._rust_core_healthy else "0.2.0-rs",
        }
```

### 3b. Degradation Triggers

| Scenario | Detection | Action |
|----------|-----------|--------|
| A: Rust module import fails | `ImportError` at `_check_rust_health()` | Set `_rust_core_healthy = False`, use Python fallback |
| B: Rust method throws exception | Any exception from a Rust PyO3 method | Catch in `_with_rust_fallback`, set flag False, retry with Python |
| C: Rust returns empty/None unexpectedly | `supply()` returns empty ContextPack with non-empty inputs | Log warning, set flag False, retry with Python |

---

## Phase 4: Performance Verification (Post-Release)

### Goal

Validate with real scenarios, not micro-benchmarks.

### Scenarios

1. **1000-memory retrieval**: `supply()` latency with 1000 memories in pool
2. **10 concurrent Agent writes**: 10 parallel `memory_store()` calls, measure error rate
3. **2-hour daemon stability**: Run maintenance daemon for 2 hours, track memory and error rate

### Metrics

- End-to-end `supply()` latency (p50, p95, p99)
- `batch_update` throughput (records/sec)
- PyO3 boundary crossing count per `supply()` call
- Error rate and degradation event count

---

## Verification Plan

### Phase 1 Verification

1. Run `grep -r "engine\._" plastic_promise/` — must return **zero** results
2. Run existing test suite: `python -m pytest tests/`
3. Manual smoke test: `python -m plastic_promise.mcp.server --sse 9020` and call `session-init` + `memory_store` + `context_supply` via MCP
4. Verify all MCP tools still work: `memory_list`, `context_graph`, `skill_session_trace`, `domain`

### Phase 2 Verification

1. Benchmark `list_memories_paginated(page_size=200)` with 5000 test memories — should complete within 2s
2. `batch_update(updates=100)` — verify atomic rollback on deliberate mid-batch failure
3. Run `memory_store` 100 times, verify no data loss

### Integration Test

```python
# tests/test_boundary.py
def test_no_underscore_access():
    """Verify no external code accesses engine._* fields."""
    import ast, os, glob
    violations = []
    for path in glob.glob("plastic_promise/**/*.py", recursive=True):
        if path.endswith("context_engine.py"):
            continue  # The engine itself is allowed
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if isinstance(node.attr, str) and node.attr.startswith("_"):
                    if isinstance(node.value, ast.Name) and node.value.id == "engine":
                        violations.append(f"{path}:{node.lineno}: engine.{node.attr}")
    assert not violations, f"Boundary violations found:\n" + "\n".join(violations)
```

---

## Summary

| Phase | Duration | Changes | Deliverable |
|-------|----------|---------|-------------|
| 1: Boundary | 2-3 days | ~15 new public methods, ~100 call-site fixes across 13 files | Zero `engine._*` violations |
| 2: Serialization | 1-2 days | `batch_update` (Rust+Python), `list_memories_paginated`, atomic transactions | Measurably fewer PyO3 crossings |
| 3: Degradation | With release | `_rust_core_healthy` state machine, `_with_rust_fallback` wrapper | Graceful Rust→Python fallback |
| 4: Verification | Post-release | 3 scenarios, latency/throughput/error metrics | Performance baseline |

**Core principle**: Rust is the spine, Python is the brain. Don't move business logic. Fix the boundary.
