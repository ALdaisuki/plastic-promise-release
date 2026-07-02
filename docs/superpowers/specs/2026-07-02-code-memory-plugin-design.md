# Codebase Memory MCP — Optional Plugin Design

**Date**: 2026-07-02
**Status**: Design — approved
**Scope**: Optional plugin integration of codebase-memory-mcp as 🟣 Code Context layer

## Problem

Subagents modify code without seeing downstream consumers. T3 changed `_activate_principles()` return type; `_inject_activated_to_graph` broke silently. T5 scanner used `engine._*` private methods; boundary CI caught it post-hoc. Every cross-task integration bug shares the same root cause: **no pre-flight impact analysis**.

## Architecture

```
pip install plastic-promise[code-memory]     ← optional
PP_ENABLE_CODE_MEMORY=1                     ← single switch
  ↓
context_supply() adds:
  🟣 Code Context (independent 4th layer, after 🟢 divergent, before audit)
     └─ CodebaseMemoryBridge.trace_downstream(task)
        └─ subprocess: codebase-memory-mcp cli trace_path '{json_args}'

CLAUDE.md subagent dispatch adds:
  Step 0: Impact Analysis
     └─ trace_downstream(target_function) → task brief "Consumers" section
```

## Design Principles

1. **No cognitive load on core users** — 41 core tools unchanged; flag off = zero difference
2. **No dependency bloat** — `pip install plastic-promise` does NOT pull codebase-memory-mcp
3. **Reserved capability switch** — PP_ENABLE_CODE_MEMORY=1 gates everything, one place

## Components

### 1. Optional dependency (`pyproject.toml`)

```toml
[project.optional-dependencies]
code-memory = ["codebase-memory-mcp>=0.7.0"]
```

### 2. Feature flag (`context_engine.py`)

```python
self._code_memory_enabled = os.environ.get("PP_ENABLE_CODE_MEMORY", "0") == "1"
```

### 3. CLI wrapper (`code_context/bridge.py`)

`CodebaseMemoryBridge` — thin subprocess wrapper. Three methods:

- `trace_downstream(function_name, depth=3) → List[dict]` — BFS call graph
- `detect_changes() → List[dict]` — git diff blast radius
- `search_related(name_pattern) → List[dict]` — pattern search

All methods return `[]` on any failure. Binary missing, project not indexed, timeout, JSON parse error — all degrade gracefully.

### 4. Context injection (`context.py`)

In `context_supply()` handler, after existing layers:

```python
if engine._code_memory_enabled:
    try:
        code_insights = engine._inject_code_context(task_description)
        if code_insights:
            context_pack["code_context"] = code_insights
    except Exception:
        pass
```

Rendered as independent `## 🟣 Code Context` section in prompt output.

### 5. Dispatch protocol (`CLAUDE.md`)

Step 0 before any code-modifying subagent dispatch:

```
trace_downstream(<target_function>) → task brief "Consumers" section
detect_changes() → if HIGH risk, expand task scope
```

## Degradation Paths

| Failure | Behavior |
|---------|----------|
| Binary not on PATH | `FileNotFoundError` → log debug → `[]` |
| Project not indexed | CLI exit ≠ 0 → log debug → `[]` |
| Subprocess timeout (30s) | `TimeoutExpired` → log warning → `[]` |
| Invalid JSON output | `JSONDecodeError` → log warning → `[]` |
| Any other exception | log warning → `[]` |

No failure ever blocks `context_supply`.

## Non-Goals

- No MCP-to-MCP bridge — CLI subprocess only, no protocol coupling
- No auto-indexing — user runs `codebase-memory-mcp cli index_repository` manually
- No vector embedding of code — codebase-memory-mcp handles that internally
- No graph storage in Plastic Promise — binary owns its own DB

## Verification

- Flag off: `context_supply` output unchanged
- Flag on + binary missing: graceful degradation, no crash
- Flag on + indexed project: 🟣 Code Context section present
- All existing tests pass unchanged
