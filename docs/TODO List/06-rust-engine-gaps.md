# 06 — Rust Engine Parity Roadmap

> Current status: active roadmap.
> Treat worktree-only fix claims as unverified until the current source tree and tests confirm them.

## Status Summary

| Area | Status | Evidence | Remaining work |
|---|---|---|---|
| Principle injection | Needs verification | Rust source contains principles support, but parity was not verified in this docs pass. | Compare Rust context package with Python package for same query. |
| Graph traversal | Planned | Rust graph parity with Python graph was not verified. | Load graph from Python serialization or SQLite. |
| `new_with_backends` path handling | Needs verification | Prior roadmap claimed worktree fixes. | Verify current `rust/context-engine-core/src/context_engine.rs`. |
| `_supply_rust` contract | Needs verification | Prior roadmap claimed worktree fixes. | Verify docstring and actual call arguments in `context_engine.py`. |
| Persistent LanceDB backend | Planned | Roadmap notes describe Rust store as HashMap-backed. | Replace placeholder when dependency constraints allow. |

## 1. Principle Injection

### Goal

Rust context supply should activate the same relevant principles as the Python context supply path for equivalent task type and task description.

### Tasks

- Ensure core principles are available to Rust supply.
- Register principle nodes in the Rust graph before injection.
- Match principles by task type and keyword/domain relevance.
- Return activated principles in the context package.
- Add parity tests against Python for representative task types.

## 2. Graph Traversal

### Goal

Rust context supply should use the same meaningful entity/principle/memory graph signals as Python, or explicitly report degraded graph mode.

### Options

| Option | Description | Trade-off |
|---|---|---|
| Python serialization | Python serializes graph nodes/edges and calls Rust `load_graph`. | Fastest parity path, still coupled to Python. |
| Rust SQLite loader | Rust reads graph state from SQLite directly. | More independent, more implementation work. |

## 3. Backend Path Handling

### Goal

Rust constructors should not silently fall back to `:memory:` when callers expect a real database path.

### Tasks

- Verify `ContextEngine::new()` path behavior.
- Verify `ContextEngine::new_with_backends(sqlite_path, lancedb_path)` actually uses its parameters or clearly labels fallback.
- Add regression tests for missing path, valid path, and invalid path behavior.

## 4. Python/Rust Supply Contract

### Goal

The Python `_supply_rust` docstring and actual call should match.

### Tasks

- Verify whether Python passes an empty memory list or a snapshot.
- Document the current behavior truthfully.
- If Rust reads SQLite directly, remove unnecessary snapshot passing.
- If Rust still needs snapshots, label that as the current integration mode.

## 5. Persistent LanceDB Backend

### Goal

Replace in-memory HashMap-style vector storage with a persistent LanceDB-backed Rust implementation when dependency constraints allow.

### Tasks

- Resolve `lancedb` Rust crate dependency/runtime constraints with PyO3.
- Open the same LanceDB directory used by Python.
- Match schema and migration behavior.
- Remove split-brain vector snapshots.
- Add performance and parity tests.

## Verification Command Sketch

```bash
PP_FORCE_PYTHON_SUPPLY=0 python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.embedder import get_embedder
engine = ContextEngine(); engine._ensure_heavy_init()
embedder = get_embedder(fallback_on_error=True)
pack = engine.supply('code review', embedder.embed('code review'), 'code_generation', 'global')
print(pack.audit_metadata)
print(len(pack.activated_principles))
"
```

Closing any item requires current-source verification, not only notes from an old worktree.
