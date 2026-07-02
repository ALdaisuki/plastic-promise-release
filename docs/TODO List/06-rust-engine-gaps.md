# Rust Engine — Missing Feature Gaps

> Identified during 2026-07-03 Rust engine production verification.
> Engine: `0.2.0-rs`, 158 vectors, real vector+FTS retrieval, score-based fusion.

---

## Gap Inventory (2 items)

| # | Gap | Priority | Effort | Impact |
|---|-----|----------|--------|--------|
| 1 | Principle Injection (`principle_injection_count: 0`) | 🟡 P1 | S | Medium |
| 2 | Graph Traversal (`graph_nodes: 11`, Python: 70) | 🟢 P2 | M | Low |

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

## Verification

```bash
PP_PREFER_RUST_SUPPLY=1 python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder
e = ContextEngine(); e._ensure_heavy_init()
embedder = get_embedder(fallback_on_error=True)
pack = e.supply('code review', embedder.embed('code review'), 'code_generation', 'global')
print(f'engine: {pack.audit_metadata.get(\"engine_version\")}')
print(f'principles: {len(pack.activated_principles)}')  # after fix: >= 2
print(f'graph_nodes: {pack.audit_metadata.get(\"graph_nodes\")}')  # after fix: >= 50
"
```
