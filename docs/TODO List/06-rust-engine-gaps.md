# Rust Engine — Missing Feature Gaps

> Identified during 2026-07-03 Rust engine production verification.
> Engine: `0.2.0-rs`, 158 vectors, real vector+FTS retrieval, score-based fusion.

---

## Gap Inventory (5 items)

| # | Gap | Priority | Effort | Impact |
|---|-----|----------|--------|--------|
| 1 | Principle Injection (`principle_injection_count: 0`) | 🟡 P1 | S | Medium |
| 2 | Graph Traversal (`graph_nodes: 11`, Python: 70) | 🟢 P2 | M | Low |
| 3 | `new_with_backends` hardcodes `:memory:` | 🔴 P0 | XS | High |
| 4 | `_supply_rust` passes snapshot — should pass empty | 🟡 P1 | S | Medium |
| 5 | Rust LanceDbStore is HashMap, not real LanceDB | 🟢 P2 | L | Low |

---

## 1. Principle Injection

### Current State

Python engine: `_supply_python()` calls `_activate_principles(task_type, task_description)` → returns `List[dict]` with `name/content/consequence/domain/keywords` per principle → 3 principles activated → injected into ContextPack.

Rust engine: `supply()` has Phase 0 stub (`principles::core_principles()` + `graph.inject_principles()`) but `activated_principle_names` stays empty because the graph has no principle nodes pre-loaded. The Rust engine's EntityGraph is created fresh (`EntityGraph::new()`) without loading principle nodes from constants.

### Root Cause

`principles::core_principles()` returns `Vec<Principle>` but `graph.inject_principles()` requires the graph to have principle nodes pre-loaded. The Python engine loads principles via `_inject_activated_to_graph()` which calls `_ensure_heavy_init()` → `_build_principle_anchors()`. The Rust engine skips this step.

### Fix (Priority: P1, Effort: Small)

In Rust `supply()`, before Phase 0:
1. Load `CORE_PRINCIPLES` from `principles::core_principles()`
2. Ensure principle nodes exist in the EntityGraph (`graph.register_node()` for each principle)
3. Match activated principles by task_type + task_description keywords
4. Populate `activated_principle_names` as `Vec<Principle>` (dict-equivalent struct)
5. Set `pack.activated_principles`

```rust
// Pseudocode
let core = principles::core_principles();
let mut activated = Vec::new();
for p in &core {
    // Match by task_type mapping + keyword overlap
    if task_matches(task_type, p) || keyword_matches(&task_description, &p.keywords) {
        activated.push(p.clone());
    }
}
pack.activated_principles = activated;
```

**Dependency**: `principles.rs` already has `core_principles()` and `Principle` struct.

---

## 2. Graph Traversal

### Current State

Python engine: EntityGraph has 70 nodes and ~850 edges. Graph traversal (`_graph_traversal`) finds principle→memory edges and entity→memory references, providing supplementary results.

Rust engine: EntityGraph has 11 nodes (default principle nodes) and 0 edges. Graph traversal returns empty results. Memories are NOT linked to principles or entities.

### Root Cause

The Rust engine creates a fresh `EntityGraph::new()` on each `RustEngine()` constructor. No edges are loaded from Python (the `load_graph()` method exists but is never called). The Python engine builds edges during `_ensure_heavy_init()` from `_memories` entity_ids and principle references.

### Fix (Priority: P2, Effort: Medium)

**Option A (preferred)**: Python `_supply_rust()` serializes graph to JSON and calls `rust.load_graph(json)` before `rust.supply()`.
- Python: `graph_json = json.dumps(self._graph_nodes) + edges` 
- Rust: existing `load_graph(&self, graph_json: String)` method ready
- Effort: ~20 lines Python, 0 Rust changes

**Option B**: Rust loads graph from SQLite `entities` + `entity_edges` tables on init.
- Requires SQL queries to populate graph
- Decouples from Python entirely
- More work but more robust

**Dependency**: `entity_graph.rs` already has `load_graph()`, `traverse()`, `inject_principles()`.

---

## Implementation Order

```
Sprint 1 (P1): Principle Injection
  → ~50 lines Rust, ~0 lines Python
  → Immediate improvement to recall context quality
  → No API changes

Sprint 2 (P2): Graph Traversal  
  → Option A: ~20 lines Python + call existing Rust method
  → Supplements recall with principle↔memory associations
  → Enables richer core layer context
```

---

## 3. `new_with_backends` hardcodes `:memory:`

### Current State

`ContextEngine::new()` (line 242) correctly uses `PLASTIC_DB_PATH` and calls `SqliteStorage::open_readonly()`. However, `ContextEngine::new_with_backends(_sqlite_path, _lancedb_path)` (line 292) still hardcodes `SqliteStorage::open(":memory:")` and ignores both parameters entirely (the `_` prefix suppresses unused-variable warnings).

### Root Cause

PR #9 fixed `new()` but `new_with_backends` was left with the old code. Any future path that constructs via `new_with_backends` expecting real database access will silently fall back to empty memory.

### Fix (Priority: P0, Effort: XS — already done in worktree)

Already fixed in `worktree-rust-engine-phase2` v2 — commit `9e933c6`:
- `new_with_backends` uses the same `PLASTIC_DB_PATH` + `open_readonly()` logic as `new()`
- Falls back to `:memory:` only when `plastic_memory.db` doesn't exist
- Graceful degradation: no crash, just empty results

**File**: `rust/context-engine-core/src/context_engine.rs`, line 292
**Worktree fix**: `worktree-rust-engine-phase2`, commit `9e933c6`

---

## 4. `_supply_rust` docstring/code mismatch

### Current State

`_supply_rust()` docstring (line 1615) says: "Rust engine reads from its own read-only SQLite connection to plastic_memory.db. Memories are NOT passed from Python — the Rust engine is self-contained. Passes empty list."

Actual code (lines 1821-1834): Loads all LanceDB vectors, builds `memories` list from `self._memories` snapshot, passes full list to `rust.supply()`.

### Root Cause

Docstring was updated to reflect the target architecture (self-contained Rust) but the code wasn't changed to match. The Rust engine's `:memory:` bug (Gap #3) means the snapshot pass is necessary for now — but the documentation should be truthful about current state.

### Fix (Priority: P1, Effort: S — already done in worktree)

Already fixed in `worktree-rust-engine-phase2` v2 — commits `9e933c6` + `2b463fe`:
- Once Rust reads real SQLite (`open_readonly`), the snapshot is unnecessary
- `_supply_rust` passes empty list: `rust.supply(task_description, task_vector, task_type, scope, [])`
- Docstring updated to match: "Passes empty list for backward compatibility with the PyO3 signature"
- Rust dispatch uncommented as PRIMARY path with `PP_FORCE_PYTHON_SUPPLY` rollback gate

**File**: `plastic_promise/core/context_engine.py`, lines 1615-1631
**Worktree fix**: `worktree-rust-engine-phase2`, commit `2b463fe`

---

## 5. Rust LanceDbStore is HashMap, not real LanceDB

### Current State

`rust/context-engine-core/src/storage/lancedb_impl.rs` line 4: "Upgrade path: swap for lancedb crate when protobuf/tokio deps resolve."

The current `LanceDbStore` uses `HashMap`-backed in-memory storage with brute-force cosine similarity search (line 17: "Suitable for <10K entries"). It is not backed by a persistent LanceDB database file. The `open()` method creates a directory on disk but stores everything in `HashMap`s — no persistence.

Meanwhile, Python's `_supply_rust()` loads vectors from real LanceDB and passes them as a snapshot, creating a split-brain: Rust has its own in-memory store while Python holds the source of truth.

### Root Cause

The `lancedb` Rust crate has dependency conflicts (protobuf, tokio versions incompatible with PyO3's runtime). The HashMap implementation is a functional stand-in that produces correct results at <10K scale, just without persistence.

### Fix (Priority: P2, Effort: Large)

1. Resolve protobuf/tokio dependency conflicts with PyO3
2. Replace `HashMap` storage with `lancedb` crate connection
3. Open same `plastic_memory.lancedb` directory that Python uses
4. Remove the Python snapshot pass in `_supply_rust` (Gap #4 becomes real)
5. Add table migration: ensure Rust-compatible schema

**Dependency**: Gap #3 (`new_with_backends` fix) must be completed first.
**Fallback**: Current HashMap approach is correct for <10K memories. This is a polish item.

---

## Updated Implementation Order

```
Sprint 1 (P0): new_with_backends :memory: fix
  → Already done in worktree-rust-engine-phase2 v2
  → Merge 4 commits: daa6703 + 9e933c6 + 662e345 + 2b463fe
  → XS effort, maximum impact

Sprint 2 (P1): _supply_rust docstring/code match + Principle Injection
  → Already done in worktree-rust-engine-phase2 v2
  → ~50 lines Rust + ~10 lines Python

Sprint 3 (P2): Graph Traversal + Real LanceDB
  → Option A: ~20 lines Python (serialize graph to JSON)
  → LanceDB: resolve deps, swap HashMap for real crate
```

---

## Verification

```bash
# After worktree-rust-engine-phase2 v2 merge:
PP_FORCE_PYTHON_SUPPLY=0 python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder
e = ContextEngine(); e._ensure_heavy_init()
embedder = get_embedder(fallback_on_error=True)
pack = e.supply('code review', embedder.embed('code review'), 'code_generation', 'global')
print(f'engine: {pack.audit_metadata.get(\"engine_version\")}')
print(f'principles: {len(pack.activated_principles)}')  # after fix: >= 2
print(f'graph_nodes: {pack.audit_metadata.get(\"graph_nodes\")}')  # after fix: >= 50
print(f'vector_search: {pack.audit_metadata.get(\"vector_search\")}')  # should be \"active\"
"
```
