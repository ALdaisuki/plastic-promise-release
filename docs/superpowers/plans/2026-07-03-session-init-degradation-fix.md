# Session-Init Degradation Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate three session-init degradations — DomainManager `_dm_ok=False`, LanceDB `lancedb_unavailable`, SCARF all-0.65 — via concurrent guards, LanceDB graceful degradation, and SCARF text heuristics, plus add a unified `component_health` field.

**Architecture:** Five independent fixes applied to 7 existing files. Each fix adds defensive guards at the entry point of functions that depend on lazily-initialized components. No new files, no API changes, no structural refactoring. A new `component_health` response field aggregates health status from all four components.

**Tech Stack:** Python 3.10+, asyncio, LanceDB, Ollama (optional), SQLite

## Global Constraints

- All changes are additive guards — no existing behavior is modified
- `_ensure_heavy_init()` double-checked lock ensures zero-cost subsequent calls
- FallbackEmbedder detection uses string comparison `getattr(emb, "model_name", "") == "fallback-zero"` — no `isinstance` to avoid circular imports
- `session-init` `concurrent=True` is preserved — no serialization
- `degrade_map` entries for `domain`, `system`, `memory_gc` are unchanged (`"skip"`)

---

### Task 1: Fix 1 — Concurrent Guards for Heavy Init

**Files:**
- Modify: `plastic_promise/mcp/tools/domain.py:168-170`
- Modify: `plastic_promise/mcp/tools/memory.py:543-545`
- Modify: `plastic_promise/mcp/tools/management.py:107-109`

**Interfaces:**
- Consumes: `ContextEngine._ensure_heavy_init()` (existing, `context_engine.py:328-412`)
- Produces: No new interfaces — existing handlers behave identically when heavy init succeeds

- [ ] **Step 1: Add `_ensure_heavy_init()` to `handle_domain()` in domain.py**

Edit `plastic_promise/mcp/tools/domain.py`, lines 168-170. Insert one line before the `dm = getattr(...)` check:

```python
async def handle_domain(engine: Any, args: dict) -> list[TextContent]:
    """域联邦统一入口。action: stats|merge|unmerge|rename|rebuild|reset_throttle"""
    action = args.get("action", "stats")
    engine._ensure_heavy_init()  # NEW: ensure DomainManager is initialized before access
    dm = getattr(engine, "_dm", None)
    if dm is None:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "DomainManager not available (_dm_ok=False)"}, ensure_ascii=False
                ),
            )
        ]
```

- [ ] **Step 2: Add `_ensure_heavy_init()` to `handle_memory_gc()` in memory.py**

Edit `plastic_promise/mcp/tools/memory.py`, lines 542-545. Insert one line at the top of the function body:

```python
async def handle_memory_gc(engine: Any, args: dict) -> list[TextContent]:
    engine._ensure_heavy_init()  # NEW: ensure LanceDB is initialized before GC access
    dry_run = args.get("dry_run", True)
    ...
```

- [ ] **Step 3: Add `_ensure_heavy_init()` to `handle_system()` in management.py**

Edit `plastic_promise/mcp/tools/management.py`, lines 106-109. Insert one line at the top of the function body:

```python
async def handle_system(engine: Any, args: dict) -> list[TextContent]:
    """系统工具统一入口。action: stats|backup|migrate"""
    engine._ensure_heavy_init()  # NEW: ensure DomainManager + embedder are initialized
    action = args.get("action", "stats")
    if action == "backup":
        return await handle_system_backup(engine, args)
    elif action == "migrate":
        return await handle_system_migrate(engine, args)
    else:
        ...
```

- [ ] **Step 4: Run existing tests to verify no regressions**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/test_session_lifecycle.py -v -x
```

Expected: All existing tests pass. The `test_session_init_degraded_domain_skip` test should still pass — the mock engine still has `_dm = None`, and `_ensure_heavy_init()` on a mock is harmless (AttributeError caught by test setup or mock provides it).

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/domain.py plastic_promise/mcp/tools/memory.py plastic_promise/mcp/tools/management.py
git commit -m "fix(session-init): add _ensure_heavy_init() guards to domain, memory_gc, system handlers"
```

---

### Task 2: Fix 2a — LanceDBStore `_vectors_disabled` Flag

**Files:**
- Modify: `plastic_promise/core/lancedb_store.py:40-46` (init), `97` (search), `145` (search_fts), `239` (insert), `225` (check_duplicate)

**Interfaces:**
- Produces: `LanceDBStore._vectors_disabled: bool` — set once in `__init__`, read by all vector-dependent methods
- Consumes: `Embedder.model_name` attribute (string, `"fallback-zero"` for FallbackEmbedder)

- [ ] **Step 1: Add `_vectors_disabled` to `__init__`**

Edit `plastic_promise/core/lancedb_store.py`, in `__init__` (line 40-46), add one line after `self._embedder = embedder`:

```python
def __init__(self, db_path: str, embedder: Embedder) -> None:
    self._path = db_path
    self._embedder = embedder
    self._vectors_disabled = getattr(embedder, "model_name", "") == "fallback-zero"
    if self._vectors_disabled:
        logger.warning("LanceDBStore: FallbackEmbedder detected — vector operations disabled")
    self._db: Optional[lancedb.DBConnection] = None
    self._table: Optional[lancedb.table.Table] = None
    self._fts_ready = False
    self._init_db()
```

- [ ] **Step 2: Add `_vectors_disabled` guard to `search()`**

Edit `plastic_promise/core/lancedb_store.py`, line 97, insert at top of method body:

```python
def search(
    self, vector: list[float], k: int = 10, tier: str | None = None,
    category: str | None = None, scope: str | None = None,
) -> list[dict]:
    if self._vectors_disabled:
        return []
    # ... existing logic unchanged
```

- [ ] **Step 3: Add `_vectors_disabled` guard to `search_fts()`**

Edit `plastic_promise/core/lancedb_store.py`, line 145, insert at top of method body:

```python
def search_fts(self, query: str, k: int = 10) -> list[dict]:
    if self._vectors_disabled:
        return []
    # ... existing logic unchanged
```

- [ ] **Step 4: Add `_vectors_disabled` guard to `insert()`**

Edit `plastic_promise/core/lancedb_store.py`, line 239, insert at top of method body:

```python
def insert(
    self, memory_id: str, vector: list[float], text: str,
    tier: str, category: str, scope: str = "",
) -> None:
    if self._vectors_disabled:
        logger.debug("LanceDBStore.insert(%s): vectors disabled, skipping write", memory_id)
        return
    # ... existing logic unchanged
```

- [ ] **Step 5: Add `_vectors_disabled` guard to `check_duplicate()`**

Edit `plastic_promise/core/lancedb_store.py`, line 225, insert at top of method body:

```python
def check_duplicate(
    self, vector: list[float], threshold: float = 0.85,
) -> dict | None:
    if self._vectors_disabled:
        return None
    # ... existing logic unchanged
```

- [ ] **Step 6: Run LanceDB tests**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/ -k "lancedb" -v --tb=short
```

Expected: Tests pass or skip (if LanceDB not installed).

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/core/lancedb_store.py
git commit -m "feat(lancedb): add _vectors_disabled flag for graceful degradation on FallbackEmbedder"
```

---

### Task 3: Fix 2b — merge_similar() Semantic Distinction

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:1239-1242`

**Interfaces:**
- Consumes: `LanceDBStore._vectors_disabled` (bool, from Task 2)
- Produces: `merge_result["error"]` — now returns `"vectors_disabled"` (not `"lancedb_unavailable"`) when vectors are unavailable but LanceDB is alive

- [ ] **Step 1: Add `_vectors_disabled` detection to `merge_similar()`**

Edit `plastic_promise/memory/soul_memory.py`, around line 1239. Replace the existing `ldb is None` check block:

```python
# OLD:
ldb = getattr(engine, "_ldb", None)
if ldb is None:
    result["error"] = "lancedb_unavailable"
    return result

# NEW:
ldb = getattr(engine, "_ldb", None)
if ldb is None:
    result["error"] = "lancedb_unavailable"
    return result
if getattr(ldb, "_vectors_disabled", False):
    result["candidates_found"] = 0
    result["would_merge"] = 0
    result["would_free"] = 0
    result["merged_pairs"] = []
    result["error"] = "vectors_disabled"
    return result
```

- [ ] **Step 2: Run GC tests**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/ -k "gc" -v --tb=short
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/memory/soul_memory.py
git commit -m "fix(gc): distinguish vectors_disabled from lancedb_unavailable in merge_similar()"
```

---

### Task 4: Fix 3 — SCARF Text Heuristic Fallback

**Files:**
- Modify: `plastic_promise/reflection/soul_scarf.py:336-350` (fallback branch in `_compute_dimension_score`)

**Interfaces:**
- Produces: `_text_heuristic_signal(context: str, dim_key: str) -> float` — new module-level function
- Consumes: `_compute_dimension_score()` calls it when both keywords and embedding are unavailable

- [ ] **Step 1: Add `_text_heuristic_signal()` function**

Insert after line 310 (end of `_compute_semantic_signal`) in `plastic_promise/reflection/soul_scarf.py`:

```python
def _text_heuristic_signal(context: str, dim_key: str) -> float:
    """Fallback when both keywords and embedding are unavailable.
    
    Returns a small signal in [-0.05, +0.05] based on text structure alone.
    Intentional kept narrow — this is a last resort, not a replacement for
    real signals.
    """
    signals = {
        "Status": 0.01 if len(context) > 50 else 0.0,
        "Certainty": -0.02 if any(c in context for c in "?？") else 0.0,
        "Autonomy": 0.02 if any(w in context for w in ["优化", "改进", "自主", "选择"]) else 0.0,
        "Relatedness": 0.03 if any(w in context for w in ["原则", "约定", "流程", "规范"]) else 0.0,
        "Fairness": 0.0,  # cannot infer from text alone
    }
    return signals.get(dim_key, 0.0)
```

- [ ] **Step 2: Modify fallback branch in `_compute_dimension_score()`**

Edit `plastic_promise/reflection/soul_scarf.py`, lines 336-350. Replace the `pos_count == 0 and neg_count == 0` branch:

```python
# OLD (lines 336-350):
if pos_count == 0 and neg_count == 0:
    # No keyword hits — try semantic embedding signal
    semantic_signal = _compute_semantic_signal(context_original, dim_key)
    if semantic_signal != 0.0:
        score = _DEFAULT_SCORE + semantic_signal
        direction = "正面" if semantic_signal > 0 else "负面"
        assessment = (
            f"{dim_label}：无显式关键词，语义信号{direction}倾向"
            f"（偏移 {semantic_signal:+.3f}）。"
        )
        suggestion = f"建议主动检查{dim_label}状态：{dim_question}"
    else:
        score = _DEFAULT_SCORE
        assessment = f"{dim_label}：无明显信号，维持默认评估。"
        suggestion = f"建议主动检查{dim_label}状态：{dim_question}"

# NEW:
if pos_count == 0 and neg_count == 0:
    semantic_signal = _compute_semantic_signal(context_original, dim_key)
    if semantic_signal != 0.0:
        score = _DEFAULT_SCORE + semantic_signal
        direction = "正面" if semantic_signal > 0 else "负面"
        assessment = (
            f"{dim_label}：无显式关键词，语义信号{direction}倾向"
            f"（偏移 {semantic_signal:+.3f}）。"
        )
        suggestion = f"建议主动检查{dim_label}状态：{dim_question}"
    else:
        # Layer 3: text heuristic fallback
        heuristic = _text_heuristic_signal(context_original, dim_key)
        score = _DEFAULT_SCORE + heuristic
        # Detect degradation cause
        try:
            from plastic_promise.core.embedder import get_embedder
            emb = get_embedder()
            if getattr(emb, "model_name", "") == "fallback-zero":
                degrade_note = "嵌入服务不可用，使用文本启发式评估——可信度降低"
            else:
                degrade_note = "无显式信号，使用文本启发式评估"
        except Exception:
            degrade_note = "无显式信号，使用文本启发式评估"
        assessment = f"{dim_label}：{degrade_note}。"
        suggestion = f"建议主动检查{dim_label}状态：{dim_question}"
```

- [ ] **Step 3: Run SCARF tests**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/ -k "scarf" -v --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/reflection/soul_scarf.py
git commit -m "feat(scarf): add text heuristic fallback with explicit degradation annotation"
```

---

### Task 5: Component Health — Unified Health Compilation

**Files:**
- Modify: `plastic_promise/skills/session_lifecycle.py:62-81` (handler return block)

**Interfaces:**
- Produces: `data["component_health"]` in session-init response — dict with four keys
- Consumes: `ctx._dm_ok`, `ctx._ldb`, `ctx._ldb._vectors_disabled`, `get_embedder().model_name`

- [ ] **Step 1: Add `_compile_component_health()` helper**

Insert before `_session_init_handler` in `plastic_promise/skills/session_lifecycle.py`:

```python
def _compile_component_health(ctx) -> dict:
    """Compile health status for all four session-init components."""
    health = {}

    # domain_manager
    health["domain_manager"] = "healthy" if getattr(ctx, "_dm_ok", False) else "degraded_no_init"

    # lancedb
    ldb = getattr(ctx, "_ldb", None)
    if ldb is None:
        health["lancedb"] = "unavailable"
    elif getattr(ldb, "_vectors_disabled", False):
        health["lancedb"] = "degraded_vectors"
    else:
        health["lancedb"] = "healthy"

    # embedder
    try:
        from plastic_promise.core.embedder import get_embedder
        emb = get_embedder()
        health["embedder"] = "fallback_zero" if getattr(emb, "model_name", "") == "fallback-zero" else "healthy"
    except Exception:
        health["embedder"] = "fallback_zero"

    # scarf — degraded if embedder is zero-vector
    health["scarf"] = "degraded_text_only" if health["embedder"] == "fallback_zero" else "healthy"

    return health
```

- [ ] **Step 2: Add `component_health` to handler return**

Edit `plastic_promise/skills/session_lifecycle.py`, in `_session_init_handler`, add to the `SkillResult` data dict (line 63-81):

```python
# Add after the chain_state block (line 61), before the return:
component_health = _compile_component_health(ctx)

return SkillResult(
    skill_name="session-init",
    success=True,
    data={
        "principles": principle_data.get("activated", []),
        "scarf_baseline": scarf_data,
        "context": context_data,
        "inject_memory_id": memory_data.get("memory_id", ""),
        "domain_health": domain_data,
        "system_stats": system_data,
        "trust": defense_data,
        "gc_preview": gc_data,
        "chain_state": chain_state,
        "component_health": component_health,  # NEW
    },
    ...
)
```

- [ ] **Step 3: Run session-lifecycle tests**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/test_session_lifecycle.py -v -x
```

Expected: `test_session_init_degraded_domain_skip` may need update — the test mock engine may not have `_dm_ok` or `_ldb` or `get_embedder`. If the test fails on `_compile_component_health`, update the test's mock to provide:

```python
mock_engine._dm_ok = False
mock_engine._ldb = None
# And patch get_embedder to return FallbackEmbedder
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/skills/session_lifecycle.py
git commit -m "feat(session-init): add component_health field aggregating DomainManager, LanceDB, embedder, SCARF status"
```

---

### Task 6: Integration Verification

**Files:**
- Modify: `tests/test_session_lifecycle.py` (update test expectations if needed)

**Interfaces:**
- Consumes: All changes from Tasks 1-5

- [ ] **Step 1: Run full test suite**

```bash
cd "F:/Agent/Memory system" && python -m pytest tests/ -v --tb=short 2>&1 | Select-Object -Last 50
```

Expected: No test regressions. Any test failures from updated behavior should be fixed.

- [ ] **Step 2: Manual verification — check component_health presence**

Run session-init and verify the new field exists:

```bash
cd "F:/Agent/Memory system" && python -c "
import json
from plastic_promise.skills.session_lifecycle import skill_session_init
# Verify the skill definition includes component_health compilation
print('Skill atoms:', skill_session_init.atoms)
print('Degrade map:', skill_session_init.degrade_map)
print('Concurrent:', skill_session_init.concurrent)
print('OK: skill definition intact')
"
```

- [ ] **Step 3: Commit any test updates**

```bash
git add tests/
git commit -m "test(session-init): update test expectations for component_health field"
```
