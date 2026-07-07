# 06 — Rust Engine Parity Roadmap

> Current status: active roadmap.
> Treat worktree-only fix claims as unverified until the current source tree and tests confirm them.

## Status Summary

| Area | Status | Evidence | Remaining work |
|---|---|---|---|
| Principle injection | Partial | `cargo test --manifest-path rust/context-engine-core/Cargo.toml` and `python -B -m pytest -p no:cacheprovider tests/test_rust_release_import.py::test_release_context_engine_core_import_contract -q` verify non-empty activated principles and matching `principle_injection_count`, but not full Python/Rust principle set parity. | Compare Rust activation against the canonical Python task-type mapping before closing R18. |
| Graph traversal | Planned | Rust has `load_graph()` / traversal hooks, but full Python/Rust graph parity remains out of this slice. | Add serialized graph parity tests in a separate R19 task. |
| `new_with_backends` path handling | Done | `cargo test --manifest-path rust/context-engine-core/Cargo.toml` verifies valid paths are read, `:memory:` still works, and missing explicit paths error. | None. |
| `_supply_rust` contract | Done | `python -B -m pytest -p no:cacheprovider tests/test_rust_integration.py::test_supply_rust_preserves_memory_db_path_for_new_with_backends tests/test_rust_integration.py::test_supply_rust_uses_new_with_backends_and_project_context tests/test_rust_integration.py::test_debug_supply_uses_rust_path_when_rust_is_preferred -q` verifies `_supply_rust` preserves the `:memory:` SQLite sentinel, uses `new_with_backends`, and passes project-aware snapshot context. | None. |
| Persistent LanceDB backend | Planned | Roadmap notes describe Rust store as HashMap-backed. | Replace placeholder when dependency constraints allow. |

## 1. Principle Injection

Status: Partial for R18. Evidence: `rust/context-engine-core/tests/integration_test.rs` verifies Rust principle injection audit count, and `tests/test_rust_release_import.py` verifies the same contract through the release PyO3 artifact. This proves the activation/audit-count contract, not full principle set/content parity; Python currently maps `code_generation` differently from Rust.

Verification commands:

- `cargo test --manifest-path rust/context-engine-core/Cargo.toml`
- `python -B -m pytest -p no:cacheprovider tests/test_rust_release_import.py::test_release_context_engine_core_import_contract -q`

### Goal

Rust context supply should activate the same relevant principles as the Python context supply path for equivalent task type and task description.

### Tasks

- Ensure core principles are available to Rust supply.
- Register principle nodes in the Rust graph before injection.
- Match principles by task type and keyword/domain relevance.
- Return activated principles in the context package.
- Add parity tests against Python for representative task types.

## 2. Graph Traversal

Status: Planned. R19 remains a separate graph traversal parity task; this R18/R20 slice does not close serialized graph parity.

### Goal

Rust context supply should use the same meaningful entity/principle/memory graph signals as Python, or explicitly report degraded graph mode.

### Options

| Option | Description | Trade-off |
|---|---|---|
| Python serialization | Python serializes graph nodes/edges and calls Rust `load_graph`. | Fastest parity path, still coupled to Python. |
| Rust SQLite loader | Rust reads graph state from SQLite directly. | More independent, more implementation work. |

## 3. Backend Path Handling

Status: Done for R20 constructor handling. Evidence: Rust source tests cover valid SQLite paths, missing explicit paths, and `:memory:` behavior for `ContextEngine::new_with_backends`.

Verification command:

- `cargo test --manifest-path rust/context-engine-core/Cargo.toml`

### Goal

Rust constructors should not silently fall back to `:memory:` when callers expect a real database path.

### Tasks

- Verify `ContextEngine::new()` path behavior.
- Verify `ContextEngine::new_with_backends(sqlite_path, lancedb_path)` actually uses its parameters or clearly labels fallback.
- Add regression tests for missing path, valid path, and invalid path behavior.

## 4. Python/Rust Supply Contract

Status: Done for R20 Python boundary handling. Evidence: `tests/test_rust_integration.py::test_supply_rust_preserves_memory_db_path_for_new_with_backends` verifies `:memory:` is preserved at the Python-to-Rust boundary, and `tests/test_rust_integration.py::test_supply_rust_uses_new_with_backends_and_project_context` verifies constructor selection and project-aware snapshot arguments.

Verification command:

- `python -B -m pytest -p no:cacheprovider tests/test_rust_integration.py::test_supply_rust_preserves_memory_db_path_for_new_with_backends tests/test_rust_integration.py::test_supply_rust_uses_new_with_backends_and_project_context tests/test_rust_integration.py::test_debug_supply_uses_rust_path_when_rust_is_preferred -q`

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
