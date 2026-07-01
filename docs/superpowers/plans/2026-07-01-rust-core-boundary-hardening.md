# Rust Core Boundary Hardening — Implementation Plan

> ✅ **Phase 1+2 Complete** (2026-07-02) — 12/12 tasks, 12 commits (`00c09a8` → `970b6f7`).
> **Remaining**: Phase 3 (Degradation & Resilience), Phase 4 (Performance Verification) — see [design doc](../specs/2026-07-01-rust-core-boundary-hardening-design.md).
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** ~~Eliminate all direct `engine._*` field accesses from Python code (~100 violations across 13 files) and harden the Rust↔Python boundary with atomic batch operations and paginated iteration.~~ **DONE.**

**Architecture:** Add ~15 public methods to the Python `ContextEngine` class (`update_memory_fields`, `add_graph_edge`, `batch_update`, `iter_memories`, etc.) that encapsulate all internal state access. Then systematically replace every `engine._memories[...]`, `engine._graph_edges.append(...)`, and `engine._sqlite._conn.execute(...)` call site with these public methods. Phase 2 adds `list_memories_paginated` for memory-efficient iteration and a CI guard test.

**Tech Stack:** Python 3.11+, SQLite (WAL mode), threading.Lock

## Global Constraints

- Python `ContextEngine` internals (`_memories`, `_graph_nodes`, `_graph_edges`, `_sqlite`, `_ldb`, `_dm`, `_embedder`) MUST NOT be accessed from outside `plastic_promise/core/context_engine.py`
- All writes to the engine MUST go through `update_memory_fields()`, `batch_update()`, or existing public methods
- `get_memory_dict()` returns a deep copy — mutations to the returned dict have no effect on engine state
- `batch_update()` MUST use SAVEPOINT for atomicity within a single connection
- `batch_update()` MUST acquire `_write_lock` (threading.Lock) to serialize in-process writes
- `iter_memories()` and `list_memories_paginated()` use offset-based pagination — not consistent under concurrent writes (documented limitation)
- Existing test suite must continue to pass after all changes
- CI guard (`tests/test_boundary.py`) must fail on any new `engine._*` violation outside `context_engine.py`

---

## File Structure

| File | Role |
|------|------|
| `plastic_promise/core/context_engine.py` | **Add** ~17 new public methods; **add** `_write_lock` in `__init__` |
| `plastic_promise/memory/pipeline.py` | **Fix** ~15 `engine._memories[...]` violations |
| `plastic_promise/mcp/tools/skill_tracking.py` | **Fix** ~25 `engine._memories[...]` / `engine._graph_*` violations |
| `plastic_promise/mcp/tools/memory.py` | **Fix** ~12 `engine._memories` / `engine._graph_*` violations |
| `plastic_promise/mcp/server.py` | **Fix** ~10 `engine._memories[...]` violations |
| `plastic_promise/core/pack_index.py` | **Fix** ~8 `engine._memories` / `engine._sqlite` violations |
| `plastic_promise/mcp/tools/context.py` | **Fix** ~5 `engine._graph_nodes` / `engine._graph_edges` violations |
| `plastic_promise/memory/soul_memory.py` | **Fix** ~6 `engine._sqlite._conn` / `engine._memories` violations |
| `plastic_promise/core/principles.py` | **Fix** ~4 `engine._graph_*` violations |
| `plastic_promise/pack.py` | **Fix** ~2 `engine._memories` / `engine._graph_edges` violations |
| `plastic_promise/loop/soul_loop.py` | **Fix** ~2 `engine._memories.values()` violations |
| `plastic_promise/mcp/tools/domain_recall.py` | **Fix** ~2 `engine._memories.items()` violations |
| `plastic_promise/core/lancedb_store.py` | **Fix** ~1 `engine._memories` reference |
| `plastic_promise/core/review_engine.py` | **Fix** ~2 `engine._memories` iteration |
| `tests/test_boundary.py` | **Create** CI guard — AST-based scan for `engine._*` violations |

---

## Phase 1a: New Public Methods on ContextEngine

### Task 1: Add `_write_lock` + core memory mutation methods

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine._write_lock` (threading.Lock), `engine.update_memory_fields(mid, **fields) -> bool`, `engine.increment_field(mid, field, delta) -> bool`

- [x] **Step 1: Add `_write_lock` to `__init__`**

In `ContextEngine.__init__`, after the existing `_heavy_init_lock` initialization (around line 227), add:

```python
# Write serialization lock — all write paths acquire this.
# RLock (reentrant) because increment_field calls update_memory_fields,
# and both acquire the lock.
self._write_lock = threading.RLock()
```

- [x] **Step 2: Add `update_memory_fields` method**

Add this method to the `ContextEngine` class (in the "Memory CRUD" section, near existing `update_memory` around line 604):

```python
def update_memory_fields(self, mid: str, **fields) -> bool:
    """Update arbitrary fields of a memory record.
    
    Unlike update_memory() which only handles content/importance/category,
    this method handles ALL fields: tags, domain, tier, worth_success,
    worth_failure, access_count, last_accessed, decay_multiplier,
    effective_half_life, entity_ids.
    
    All writes go through the _write_lock for thread safety.
    """
    with self._write_lock:
        if mid not in self._memories:
            return False
        mem = self._memories[mid]
        for key, value in fields.items():
            if key in ("tags", "entity_ids"):
                mem[key] = list(value)  # defensive copy
            else:
                mem[key] = value
        if self._sqlite:
            self._sqlite.upsert(mid, mem)
        return True
```

- [x] **Step 3: Add `increment_field` convenience method**

Add this method to the `ContextEngine` class:

```python
def increment_field(self, mid: str, field: str, delta: float = 1) -> bool:
    """Atomically increment a numeric field.
    
    Convenience wrapper around update_memory_fields for the common
    pattern: engine._memories[mid]["access_count"] += 1
    """
    with self._write_lock:
        if mid not in self._memories:
            return False
        current = self._memories[mid].get(field, 0)
        return self.update_memory_fields(mid, **{field: current + delta})
```

Note: `increment_field` acquires `_write_lock` then calls `update_memory_fields` which also tries to acquire `_write_lock`. To avoid deadlock, use `threading.RLock` instead of `Lock`:

```python
# In __init__:
self._write_lock = threading.RLock()  # reentrant — increment_field calls update_memory_fields
```

- [x] **Step 4: Run existing tests to verify no regression**

```bash
python -m pytest tests/ -x -q
```

Expected: All existing tests pass (no callers use the new methods yet).

- [x] **Step 5: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add update_memory_fields + increment_field + _write_lock to ContextEngine"
```

---

### Task 2: Add memory read-access methods

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine.memory_exists(mid) -> bool`, `engine.get_memory_dict(mid) -> dict | None`, `engine.memory_ids() -> list[str]`, `engine.get_memories_batch(mids) -> list[dict]`

- [x] **Step 1: Add `memory_exists` method**

```python
def memory_exists(self, mid: str) -> bool:
    """Check if a memory id exists in the pool."""
    return mid in self._memories
```

- [x] **Step 2: Add `get_memory_dict` method (deep copy)**

```python
def get_memory_dict(self, mid: str) -> dict | None:
    """Get a memory record as a dict (deep copy).
    
    Returns a copy so callers can read fields freely,
    but mutations have NO effect on engine state.
    Use update_memory_fields() to modify data.
    """
    import copy
    mem = self._memories.get(mid)
    if mem is None:
        return None
    return copy.deepcopy(mem)
```

- [x] **Step 3: Add `memory_ids` method**

```python
def memory_ids(self) -> list[str]:
    """Return all memory IDs in the pool."""
    return list(self._memories.keys())
```

- [x] **Step 4: Add `get_memories_batch` method**

```python
def get_memories_batch(self, mids: list[str]) -> list[dict]:
    """Get multiple memory records by id. Missing ids are skipped."""
    import copy
    results = []
    for mid in mids:
        mem = self._memories.get(mid)
        if mem is not None:
            results.append(copy.deepcopy(mem))
    return results
```

- [x] **Step 5: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 6: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add memory_exists, get_memory_dict, memory_ids, get_memories_batch to ContextEngine"
```

---

### Task 3: Add `iter_memories` paginated read method

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine.iter_memories(scope=None, page_size=200) -> Iterator[dict]`

- [x] **Step 1: Add `iter_memories` method**

```python
def iter_memories(self, scope=None, page_size=200) -> "Iterator[dict]":
    """Iterate memory records as dicts, one page at a time.
    
    Uses offset-based pagination over the in-memory dict keys.
    NOT consistent under concurrent writes — suitable for snapshots
    (pack_export, memory_stats) not real-time retrieval under load.
    
    Args:
        scope: Optional domain filter (applied in Python after yield).
               Pass None for all memories.
        page_size: Number of records per page (default 200).
    """
    import copy
    all_ids = list(self._memories.keys())
    offset = 0
    while offset < len(all_ids):
        page_ids = all_ids[offset:offset + page_size]
        for mid in page_ids:
            mem = self._memories.get(mid)
            if mem is None:
                continue
            if scope and mem.get("scope", "global") != scope:
                continue
            yield copy.deepcopy(mem)
        offset += page_size
```

- [x] **Step 2: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 3: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add iter_memories with pagination to ContextEngine"
```

---

### Task 4: Add graph CRUD methods

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine.add_graph_edge(from, to, relation, weight) -> bool`, `engine.remove_graph_edge(from, to, relation) -> bool`, `engine.has_graph_edge(edge_dict) -> bool`, `engine.get_graph_node(node_id) -> dict | None`, `engine.list_graph_nodes(type=None) -> list[dict]`, `engine.list_graph_edges(relation=None) -> list[dict]`

- [x] **Step 1: Add `add_graph_edge` method**

```python
def add_graph_edge(self, source: str, target: str,
                   relation: str = "references",
                   weight: float = 0.5) -> bool:
    """Add an edge to the entity graph. No-op if duplicate exists.
    
    Returns True if the edge was added, False if it already existed.
    """
    edge = {
        "from": source,
        "to": target,
        "relation": relation,
        "weight": weight,
    }
    if edge not in self._graph_edges:
        self._graph_edges.append(edge)
        return True
    return False
```

- [x] **Step 2: Add `remove_graph_edge` method**

```python
def remove_graph_edge(self, source: str, target: str,
                      relation: str = None) -> int:
    """Remove matching edges. If relation is None, removes all edges
    between source and target regardless of relation.
    
    Returns the number of edges removed.
    """
    removed = 0
    self._graph_edges[:] = [
        e for e in self._graph_edges
        if not (
            e.get("from") == source
            and e.get("to") == target
            and (relation is None or e.get("relation") == relation)
        )
    ]
    # Count removed
    remaining = len(self._graph_edges)
    return removed  # actual count computed via diff; simplified here
    
    # Correct implementation:
    # original_len = len(self._graph_edges)
    # self._graph_edges = [...] # as above
    # return original_len - len(self._graph_edges)
```

Simplify to:

```python
def remove_graph_edge(self, source: str, target: str,
                      relation: str = None) -> int:
    """Remove matching edges. Returns number of edges removed."""
    before = len(self._graph_edges)
    self._graph_edges[:] = [
        e for e in self._graph_edges
        if not (
            e.get("from") == source
            and e.get("to") == target
            and (relation is None or e.get("relation") == relation)
        )
    ]
    return before - len(self._graph_edges)
```

- [x] **Step 3: Add `has_graph_edge` method**

```python
def has_graph_edge(self, edge_dict: dict) -> bool:
    """Check if an exact edge dict exists in the graph."""
    return edge_dict in self._graph_edges
```

- [x] **Step 4: Add `get_graph_node` method**

```python
def get_graph_node(self, node_id: str) -> dict | None:
    """Get a graph node by id. Returns a copy."""
    import copy
    node = self._graph_nodes.get(node_id)
    if node is None:
        return None
    return copy.deepcopy(node)
```

- [x] **Step 5: Add `list_graph_nodes` method**

```python
def list_graph_nodes(self, node_type: str = None) -> list[dict]:
    """List graph nodes, optionally filtered by type field."""
    import copy
    results = []
    for nid, node in self._graph_nodes.items():
        if node_type and node.get("type") != node_type:
            continue
        node_copy = copy.deepcopy(node)
        node_copy["id"] = nid
        results.append(node_copy)
    return results
```

- [x] **Step 6: Add `list_graph_edges` method**

```python
def list_graph_edges(self, relation: str = None) -> list[dict]:
    """List graph edges, optionally filtered by relation."""
    if relation is None:
        return list(self._graph_edges)
    return [e for e in self._graph_edges if e.get("relation") == relation]
```

- [x] **Step 7: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 8: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add graph CRUD methods to ContextEngine"
```

---

### Task 5: Add `batch_update` with SAVEPOINT atomicity

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine.batch_update(updates) -> int`, `engine.begin_batch()`, `engine.commit_batch()`, `engine.rollback_batch()`

- [x] **Step 1: Add `batch_update` method**

Add to the ContextEngine class (near the Transaction Support area):

```python
def batch_update(self, updates: list[dict]) -> int:
    """Apply multiple memory field updates atomically.
    
    Args:
        updates: [{"id": "mem_001", "tags": [...], "domain": "code"}, ...]
            Each dict MUST contain "id". Other keys are field updates.
    
    Returns:
        Number of records updated.
    
    If any update fails, ALL changes are rolled back via SAVEPOINT.
    Thread-safe: acquires _write_lock.
    """
    with self._write_lock:
        if not self._sqlite:
            return self._batch_update_in_memory(updates)
        
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
    
def _batch_update_in_memory(self, updates: list[dict]) -> int:
    """Fallback batch_update when SQLite is unavailable."""
    count = 0
    for upd in updates:
        upd_copy = dict(upd)
        mid = upd_copy.pop("id")
        if mid in self._memories:
            self._memories[mid].update(upd_copy)
            count += 1
    return count
```

- [x] **Step 2: Add `begin_batch` / `commit_batch` / `rollback_batch`**

For callers that need manual transaction control (e.g., multi-step GC operations):

```python
def begin_batch(self):
    """Begin a manual batch transaction. Acquires _write_lock."""
    self._write_lock.acquire()
    if self._sqlite:
        self._sqlite._conn.execute("SAVEPOINT manual_batch")

def commit_batch(self):
    """Commit a manual batch transaction. Releases _write_lock."""
    try:
        if self._sqlite:
            self._sqlite._conn.execute("RELEASE manual_batch")
            self._sqlite._conn.commit()
    finally:
        self._write_lock.release()

def rollback_batch(self):
    """Rollback a manual batch transaction. Releases _write_lock."""
    try:
        if self._sqlite:
            self._sqlite._conn.execute("ROLLBACK TO manual_batch")
    finally:
        self._write_lock.release()
```

- [x] **Step 3: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add batch_update with SAVEPOINT + begin/commit/rollback_batch to ContextEngine"
```

---

## Phase 1b: Fix `engine._memories` Violations (highest priority, ~60% of total)

### Task 6: Fix `plastic_promise/memory/pipeline.py` (~15 violations)

**Files:**
- Modify: `plastic_promise/memory/pipeline.py:351-536`

**Interfaces:**
- Consumes: `engine.update_memory_fields()`, `engine.increment_field()`, `engine.memory_exists()`, `engine.get_memory_dict()`
- Produces: Same behavior, zero `engine._*` accesses

- [x] **Step 1: Fix lines 351-365 — dup_id field mutations (duplicate check block)**

Replace:
```python
# Fix #2: Guard against dup_id missing from engine._memories (SQLite-only memory)
engine._memories[dup_id]["access_count"] = (
    engine._memories[dup_id].get("access_count", 0) + 1
)
engine._memories[dup_id]["worth_success"] = (
    engine._memories[dup_id].get("worth_success", 0) + 1
)
engine._memories[dup_id]["last_accessed"] = now_iso
existing_eids = set(engine._memories[dup_id].get("entity_ids", []))
# ... new_eids computed ...
engine._memories[dup_id]["entity_ids"] = list(existing_eids | new_eids)
```

With:
```python
# Fix #2: Guard against dup_id missing from engine (SQLite-only memory)
if engine.memory_exists(dup_id):
    engine.increment_field(dup_id, "access_count", 1)
    engine.increment_field(dup_id, "worth_success", 1)
    engine.update_memory_fields(dup_id, last_accessed=now_iso)
    mem = engine.get_memory_dict(dup_id)
    existing_eids = set(mem.get("entity_ids", []) if mem else [])
    # ... new_eids computed ...
    engine.update_memory_fields(dup_id, entity_ids=list(existing_eids | new_eids))
```

- [x] **Step 2: Fix lines 391-392 — effective_half_life update**

Replace:
```python
if dup_id in engine._memories:
    engine._memories[dup_id]["effective_half_life"] = new_hl
```

With:
```python
engine.update_memory_fields(dup_id, effective_half_life=new_hl)
```

Note: `update_memory_fields` returns False if the id doesn't exist, which is equivalent to the `if dup_id in engine._memories` guard.

- [x] **Step 3: Fix lines 505-507 — decay_multiplier + effective_half_life**

Replace:
```python
if engine is not None and stored.memory_id in engine._memories:
    engine._memories[stored.memory_id]["decay_multiplier"] = dm
    engine._memories[stored.memory_id]["effective_half_life"] = base_hl
```

With:
```python
if engine is not None:
    engine.update_memory_fields(stored.memory_id, decay_multiplier=dm, effective_half_life=base_hl)
```

- [x] **Step 4: Fix line 521 — _vector storage**

Replace:
```python
engine._memories[stored.memory_id]["_vector"] = vec
```

With:
```python
engine.update_memory_fields(stored.memory_id, _vector=vec)
```

- [x] **Step 5: Fix lines 535-536 — tags + domain**

Replace:
```python
engine._memories[stored.memory_id]["tags"] = tags
engine._memories[stored.memory_id]["domain"] = domain_hint
```

With:
```python
engine.update_memory_fields(stored.memory_id, tags=tags, domain=domain_hint)
```

- [x] **Step 6: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 7: Commit**

```bash
git add plastic_promise/memory/pipeline.py
git commit -m "fix: replace engine._memories violations in pipeline.py with public API"
```

---

### Task 7: Fix `plastic_promise/mcp/tools/skill_tracking.py` (~25 violations)

**Files:**
- Modify: `plastic_promise/mcp/tools/skill_tracking.py`

**Interfaces:**
- Consumes: `engine.update_memory_fields()`, `engine.add_graph_edge()`, `engine.has_graph_edge()`, `engine.list_graph_nodes()`, `engine.list_graph_edges()`, `engine.iter_memories()`, `engine.memory_exists()`
- Produces: Same behavior, zero `engine._*` accesses

- [x] **Step 1: Fix lines 232-233 — graph edge append**

Replace:
```python
if parent_edge not in engine._graph_edges:
    engine._graph_edges.append(parent_edge)
```

With:
```python
engine.add_graph_edge(source=parent_edge["from"], target=parent_edge["to"],
                      relation=parent_edge.get("relation", "parent_of"),
                      weight=parent_edge.get("weight", 0.8))
```

- [x] **Step 2: Fix lines 286, 884, 945 — graph node iteration**

Replace:
```python
for node_id, node in engine._graph_nodes.items():
```

With:
```python
for node in engine.list_graph_nodes():
    node_id = node["id"]
```

- [x] **Step 3: Fix lines 366, 395 — graph edge iteration**

Replace:
```python
for edge in engine._graph_edges:
```

With:
```python
for edge in engine.list_graph_edges():
```

- [x] **Step 4: Fix lines 303, 460, 653, 854, 904 — memory iteration**

Replace:
```python
for mid, mem in engine._memories.items():
```

With:
```python
for mem in engine.iter_memories():
    mid = mem["id"]
```

- [x] **Step 5: Fix lines 703-705 — memory field writes**

Replace:
```python
engine._memories[memory_id]["tags"] = tags
engine._memories[memory_id]["content"] = new_content
```

With:
```python
engine.update_memory_fields(memory_id, tags=tags, content=new_content)
```

- [x] **Step 6: Fix lines 733-736 — memory field writes with last_accessed**

Replace:
```python
engine._memories[memory_id]["content"] = new_content
engine._memories[memory_id]["tags"] = tags
engine._memories[memory_id]["last_accessed"] = (
    ...some expression...
)
```

With:
```python
engine.update_memory_fields(memory_id, content=new_content, tags=tags, last_accessed=(
    ...some expression...
))
```

- [x] **Step 7: Fix lines 776, 782 — remaining memory field writes**

Same pattern — replace `engine._memories[mid]["field"] = val` with `engine.update_memory_fields(mid, field=val)`.

- [x] **Step 8: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 9: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py
git commit -m "fix: replace all engine._* violations in skill_tracking.py with public API"
```

---

### Task 8: Fix `plastic_promise/mcp/tools/memory.py` (~12 violations)

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py`

**Interfaces:**
- Consumes: `engine.update_memory_fields()`, `engine.add_graph_edge()`, `engine.list_graph_nodes()`, `engine.iter_memories()`, `engine.memory_ids()`
- Produces: Same behavior, zero `engine._*` accesses

- [x] **Step 1: Fix line 204-205 — graph edge append**

Replace:
```python
if edge not in engine._graph_edges:
    engine._graph_edges.append(edge)
```

With:
```python
engine.add_graph_edge(source=edge["from"], target=edge["to"],
                      relation=edge.get("relation", "governs"),
                      weight=edge.get("weight", 1.0))
```

- [x] **Step 2: Fix lines 464-465 — graph node iteration**

Replace:
```python
for nid in engine._graph_nodes:
    name = engine._graph_nodes[nid].get("name", "")
```

With:
```python
for node in engine.list_graph_nodes():
    nid = node["id"]
    name = node.get("name", "")
```

- [x] **Step 3: Fix line 619 — memory iteration**

Replace:
```python
for mid, mem in engine._memories.items():
```

With:
```python
for mem in engine.iter_memories():
    mid = mem["id"]
```

- [x] **Step 4: Fix lines 691-694 — 4-field write**

Replace:
```python
engine._memories[mid]["category"] = new_category
engine._memories[mid]["tier"] = new_tier
engine._memories[mid]["domain"] = new_domain
engine._memories[mid]["tags"] = new_tags
```

With:
```python
engine.update_memory_fields(mid, category=new_category, tier=new_tier,
                            domain=new_domain, tags=new_tags)
```

- [x] **Step 5: Fix line 727 — memory count**

Replace:
```python
"total": len(engine._memories),
```

With:
```python
"total": engine.memory_count,
```

(Note: `engine.memory_count` property already exists at line 388)

- [x] **Step 6: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 7: Commit**

```bash
git add plastic_promise/mcp/tools/memory.py
git commit -m "fix: replace all engine._* violations in memory.py MCP tools with public API"
```

---

### Task 9: Fix `plastic_promise/mcp/server.py` (~10 violations)

**Files:**
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Consumes: `engine.update_memory_fields()`, `engine.get_memory_dict()`, `engine.memory_exists()`
- Produces: Same behavior, zero `engine._*` accesses

- [x] **Step 1: Fix lines 1402-1407 — tag mutation with read-then-write**

Replace:
```python
for mid, mem in engine._memories.items():
    # ... filtering logic ...
    engine._memories[mid]["tags"] = mtags
```

With:
```python
for mem in engine.iter_memories():
    mid = mem["id"]
    # ... filtering logic ...
    engine.update_memory_fields(mid, tags=mtags)
```

- [x] **Step 2: Fix lines 1423-1433 — category + tags read-then-write**

Replace:
```python
if mid and mid in engine._memories:
    engine._memories[mid]["category"] = new_category
    tags = list(engine._memories[mid].get("tags", []))
    # ... tag manipulation ...
    engine._memories[mid]["tags"] = tags
```

With:
```python
if mid and engine.memory_exists(mid):
    engine.update_memory_fields(mid, category=new_category)
    mem = engine.get_memory_dict(mid)
    if mem:
        tags = list(mem.get("tags", []))
        # ... tag manipulation ...
        engine.update_memory_fields(mid, tags=tags)
```

- [x] **Step 3: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 4: Commit**

```bash
git add plastic_promise/mcp/server.py
git commit -m "fix: replace all engine._* violations in server.py with public API"
```

---

### Task 10: Fix remaining 7 files (~24 violations)

**Files:**
- Modify: `plastic_promise/core/pack_index.py`, `plastic_promise/mcp/tools/context.py`, `plastic_promise/core/principles.py`, `plastic_promise/mcp/tools/domain_recall.py`, `plastic_promise/pack.py`, `plastic_promise/loop/soul_loop.py`, `plastic_promise/memory/soul_memory.py`, `plastic_promise/core/lancedb_store.py`, `plastic_promise/core/review_engine.py`

**Interfaces:**
- Consumes: All public methods from Tasks 1-5
- Produces: Same behavior, zero `engine._*` accesses

- [x] **Step 1: Fix `plastic_promise/core/pack_index.py` — lines 62-163**

Patterns to fix:
- `engine._sqlite._conn.execute(...)` → Use `engine.update_memory_fields()` or `engine.get_memory_dict()` + `engine.batch_update()`
- `engine._memories.get(mid)` → `engine.get_memory_dict(mid)`
- `engine._memories[mid] = {...}` → `engine.register_memory({...})` (already a public method)
- `engine._sqlite.upsert(mid, data)` → `engine.update_memory_fields(mid, **data)`

- [x] **Step 2: Fix `plastic_promise/mcp/tools/context.py` — lines 105-121**

Replace:
```python
is_new = node_id not in engine._graph_nodes
engine._graph_nodes[node_id] = {...}
if edge not in engine._graph_edges:
    engine._graph_edges.append(edge)
```

With `engine.register_entity(...)` (already exists at line 862 as a public method) and `engine.add_graph_edge(...)`.

- [x] **Step 3: Fix `plastic_promise/core/principles.py` — lines 136-151**

Replace `engine._graph_nodes[nid] = {...}` and `engine._graph_edges.append(edge)` with `engine.add_graph_edge(...)`.

- [x] **Step 4: Fix `plastic_promise/mcp/tools/domain_recall.py` — lines 128, 203**

Replace `engine._memories.items()` / `engine._memories.values()` with `engine.iter_memories()`.

- [x] **Step 5: Fix `plastic_promise/pack.py` — lines 84, 107**

Replace:
- `engine._memories` → `engine.iter_memories()` or `engine.memory_ids()` + `engine.get_memories_batch()`
- `engine._graph_edges` → `engine.list_graph_edges()`

- [x] **Step 6: Fix `plastic_promise/loop/soul_loop.py` — line 308-310**

Replace:
```python
if engine._memories:
    for mem in engine._memories.values():
```

With:
```python
for mem in engine.iter_memories():
```

- [x] **Step 7: Fix `plastic_promise/memory/soul_memory.py` — lines 920-926, 1131, 1272**

Replace:
- `engine._sqlite._conn.execute(...)` → `engine.update_memory_fields(...)`
- `engine._sqlite._conn.commit()` → `engine.commit_batch()` or remove (individual ops auto-commit)
- `engine._memories` → `engine.update_memory_fields(...)` for writes, `engine.iter_memories()` for reads

- [x] **Step 8: Fix `plastic_promise/core/lancedb_store.py` — line 300**

Replace `engine._memories` reference with `engine.iter_memories()`.

- [x] **Step 9: Fix `plastic_promise/core/review_engine.py` — lines 372-373**

Replace `engine._memories.items()` with `engine.iter_memories()`.

- [x] **Step 10: Verify zero violations**

```bash
grep -rn "engine\._" plastic_promise/ --include="*.py" | grep -v "plastic_promise/core/context_engine.py" | grep -v "\.pyc"
```

Expected: No output (zero violations outside context_engine.py).

- [x] **Step 11: Run full test suite**

```bash
python -m pytest tests/ -v
```

- [x] **Step 12: Commit**

```bash
git add plastic_promise/core/pack_index.py plastic_promise/mcp/tools/context.py plastic_promise/core/principles.py plastic_promise/mcp/tools/domain_recall.py plastic_promise/pack.py plastic_promise/loop/soul_loop.py plastic_promise/memory/soul_memory.py plastic_promise/core/lancedb_store.py plastic_promise/core/review_engine.py
git commit -m "fix: replace all remaining engine._* violations with public API — zero violations achieved"
```

---

## Phase 2: Serialization Hardening

### Task 11: Add `list_memories_paginated` to ContextEngine

**Files:**
- Modify: `plastic_promise/core/context_engine.py`

**Interfaces:**
- Produces: `engine.list_memories_paginated(memory_type, source, min_worth, scope, page_size) -> Iterator[MemoryRecord]`

- [x] **Step 1: Add `list_memories_paginated` method**

Add this method after the existing `list_memories` method (around line 656):

```python
def list_memories_paginated(
    self,
    memory_type: str = None,
    source: str = None,
    min_worth: float = None,
    scope: str = None,
    page_size: int = 200,
):
    """Yield MemoryRecords one page at a time via offset pagination.
    
    Avoids allocating a full list for large result sets.
    For 10K records at page_size=200: ~50 PyO3 boundary crossings.
    
    Consistency: Uses offset-based pagination — NOT guaranteed consistent
    under concurrent writes. Records inserted or deleted between pages may
    cause duplicates or omissions. Suitable for snapshot operations
    (pack_export, memory_gc, memory_stats). Real-time retrieval uses
    supply() via LanceDB ANN + text matching, not pagination.
    
    Yields:
        MemoryRecord objects, one at a time.
    """
    offset = 0
    while True:
        page = self.list_memories(
            memory_type=memory_type,
            source=source,
            min_worth=min_worth,
            limit=page_size,
            scope=scope,
        )
        if not page:
            break
        yield from page
        if len(page) < page_size:
            break
        offset += len(page)
```

Note: This delegates to the existing `list_memories()` which already handles the Python-side filtering. The pagination is provided by the `limit` parameter.

- [x] **Step 2: Run tests**

```bash
python -m pytest tests/ -x -q
```

- [x] **Step 3: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add list_memories_paginated with offset pagination"
```

---

### Task 12: Write `tests/test_boundary.py` CI guard

**Files:**
- Create: `tests/test_boundary.py`

**Interfaces:**
- Produces: `test_no_underscore_access()` — AST-based boundary violation check

- [x] **Step 1: Write the test file**

```python
"""CI guard: ensure no external code accesses engine._* private fields.

All access to ContextEngine internals (_memories, _graph_nodes,
_graph_edges, _sqlite, _ldb, _dm, _embedder) MUST go through
public methods defined in plastic_promise/core/context_engine.py.
"""

import ast
import glob
import os


def test_no_underscore_access():
    """Verify no external code accesses engine._* fields.
    
    Scans all Python files under plastic_promise/ for Attribute
    nodes where the value is `engine` and the attribute starts
    with `_`. The context_engine.py file itself is exempt.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plastic_promise_dir = os.path.join(project_root, "plastic_promise")
    
    if not os.path.isdir(plastic_promise_dir):
        # Not running from the right directory; skip gracefully
        return
    
    violations = []
    
    for py_file in glob.glob(
        os.path.join(plastic_promise_dir, "**", "*.py"), recursive=True
    ):
        rel_path = os.path.relpath(py_file, project_root)
        
        # The engine itself is allowed to access its own internals
        if rel_path.endswith("context_engine.py"):
            continue
        
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
        except (IOError, UnicodeDecodeError):
            continue
        
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            continue
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                # Check if this is engine._something
                if (
                    isinstance(node.attr, str)
                    and node.attr.startswith("_")
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "engine"
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno}: engine.{node.attr}"
                    )
    
    if violations:
        msg = (
            f"Boundary violations found ({len(violations)}):\n"
            + "\n".join(violations[:20])
            + ("\n... (truncated)" if len(violations) > 20 else "")
            + "\n\nUse public methods on ContextEngine instead of "
            + "accessing engine._* fields directly."
        )
        raise AssertionError(msg)
```

- [x] **Step 2: Run the test — expect PASS (zero violations after Phase 1)**

```bash
python -m pytest tests/test_boundary.py -v
```

Expected: PASS (all `engine._*` violations already fixed in Tasks 6-10).

- [x] **Step 3: Verify the test catches violations (self-test)**

Temporarily add a violation to a non-engine file and confirm the test catches it:

```bash
# Add a temporary violation
echo "engine._memories['test'] = {}" >> plastic_promise/__init__.py
# Run test — should fail
python -m pytest tests/test_boundary.py -v
# Expected: FAIL with "Boundary violations found (1):"
# Clean up
git checkout plastic_promise/__init__.py
```

- [x] **Step 4: Commit**

```bash
git add tests/test_boundary.py
git commit -m "test: add CI guard — AST scan for engine._* boundary violations"
```

---

## Verification

After all tasks complete, run the full verification:

- [x] **V1: Zero violations scan**

```bash
grep -rn "engine\._" plastic_promise/ --include="*.py" | grep -v "plastic_promise/core/context_engine.py"
```
Expected: No output.

- [x] **V2: Full test suite**

```bash
python -m pytest tests/ -v
```
Expected: All tests pass.

- [ ] **V3: MCP smoke test**

```bash
# Start MCP server
python -m plastic_promise.mcp.server --sse 9020 &
sleep 2
# Test session-init
python -c "
import urllib.request, json
# Simple health check
resp = urllib.request.urlopen('http://127.0.0.1:9020/health')
print(json.loads(resp.read()))
"
```

Expected: Server starts and health check returns OK.

- [ ] **V4: MCP tool functional test**

Run `session-init`, `memory_store`, `context_supply`, `memory_list`, `context_graph`, `skill_session_trace`, `domain` — all tools should return valid results without errors.

- [ ] **V5: Final commit (if any remaining cleanup)**

```bash
git add -A
git commit -m "chore: final verification — zero engine._* violations, all tests pass"
```

---

## Completion Summary (2026-07-02)

### 12 Commits

| # | Commit | Task |
|---|--------|------|
| 1 | `00c09a8` | `_write_lock` + `update_memory_fields` + `increment_field` |
| 2 | `158231d` | `memory_exists` + `get_memory_dict` + `memory_ids` + `get_memories_batch` |
| 3 | `ac0041c` | `iter_memories` paginated read |
| 4 | `49d8c65` | Graph CRUD ×6 (`add_graph_edge`, `remove_graph_edge`, `has_graph_edge`, `get_graph_node`, `list_graph_nodes`, `list_graph_edges`) |
| 5 | `36dbcc3` | `batch_update` + `begin/commit/rollback_batch` (SAVEPOINT) |
| 6 | `6421915` | Fix `pipeline.py` (~15 violations) |
| 7 | `2be04c3` | Fix `skill_tracking.py` (~25 violations) |
| 8 | `bd9b80b` | Fix `memory.py` (~12 violations) |
| 9 | `02793e1` | Fix `server.py` (~10 violations) |
| 10 | `900f14a` | Fix remaining 8 files (~30 violations) |
| 11 | `4b8a0c0` | `list_memories_paginated` + `list_memories(offset=)` |
| 12 | `970b6f7` | `tests/test_boundary.py` CI guard (AST scan) |

### Verified
- ✅ V1: Zero `engine._*` violations (grep confirms, all remaining matches in comments)
- ✅ V2: 100 existing tests pass (2 pre-existing failures unrelated)
- ⏳ V3-V5: MCP smoke test pending (server not running in session)

### Remaining Future Work (Phase 3+4 — not in this plan)
See [design doc](../specs/2026-07-01-rust-core-boundary-hardening-design.md):
- **Phase 3**: Degradation & Resilience — `_rust_core_healthy` state machine, `_with_rust_fallback` wrapper
- **Phase 4**: Performance Verification — 1000-memory retrieval, 10-concurrent-agent writes, 2-hour daemon stability

