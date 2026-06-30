# Plastic Promise — Trust Score Persistence & Decay Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** writing-plans | **Date:** 2026-07-01

**Goal:** Fix TrustManager statelessness (P0) + add time decay and violation-driven decay (P1), making the trust score a real governance signal instead of a frozen 0.6.

**Root Cause:** `TrustManager` is instantiated fresh per MCP call — `handle_defense_trust()` at [audit_defense.py#L172](file:///f:/Agent/Memory system/plastic_promise/mcp/tools/audit_defense.py#L172) creates `TrustManager()` every time. All boost/decay changes are lost on next call.

**Tech Stack:** Python 3.11+, SQLite (existing `plastic_memory.db`), `TrustManager` (existing), `SoulEnforcer` (existing)

---

## Global Constraints

- Zero change to existing MCP tool signatures (backward compatible)
- All persistence uses the existing `plastic_memory.db` SQLite (same `PLASTIC_DB_PATH`)
- `TrustManager` API unchanged — `boost()`, `decay()`, `adjust()`, `get()`, `history()`, `tier()`, `autonomy_level()` all keep same signatures
- `SoulEnforcer` integration is additive — `pre_check()` gains optional decay side effect
- Time decay runs lazily on `get()`, not via background thread (no new daemon)
- Negative delta is already supported in `adjust()`, no API change needed

---

## File Structure

```
plastic_promise/
├── defense/
│   ├── __init__.py              ← (existing)
│   ├── soul_enforcer.py         ← MODIFY: add violation-driven decay to pre_check
│   └── trust_store.py           ← NEW: TrustStore — SQLite persistence for TrustManager
├── mcp/tools/
│   └── audit_defense.py         ← MODIFY: use TrustStore singleton instead of new TrustManager
└── loop/
    └── soul_loop.py             ← MODIFY: SoulLoop.post_task → use TrustStore

tests/
└── test_trust_store.py          ← NEW: unit tests for TrustStore
```

---

### Task 1: Create TrustStore — SQLite persistence layer

**Files:**
- Create: `plastic_promise/defense/trust_store.py`
- Create: `tests/test_trust_store.py`

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS trust_scores (
    target TEXT PRIMARY KEY,       -- "" = default, "pi_builder", etc.
    trust REAL NOT NULL DEFAULT 0.6,
    tier TEXT NOT NULL DEFAULT 'medium',
    autonomy_level TEXT NOT NULL DEFAULT 'standard',
    last_updated TEXT NOT NULL,    -- ISO timestamp for time-decay calculation
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    delta REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    direction TEXT NOT NULL,       -- 'boost' | 'decay'
    timestamp TEXT NOT NULL
);
```

**Interfaces:**

```python
class TrustStore:
    def __init__(self, db_path: str = None):
        """Same db_path as _SQLiteStorage (PLASTIC_DB_PATH env)."""

    def get(self, target: str = "") -> dict:
        """Returns {trust, tier, autonomy_level, last_updated}. Auto-applies time decay."""

    def save(self, target: str, trust: float, tier: str, autonomy_level: str):
        """Upsert current trust score."""

    def log_history(self, target: str, delta: float, reason: str,
                    old_value: float, new_value: float, direction: str):
        """Append to trust_history table."""

    def history(self, target: str = "", limit: int = 50) -> list[dict]:
        """Query trust_history for a target."""

    def _apply_time_decay(self, target: str, current: dict) -> dict:
        """Lazy time decay: if last_updated > 24h, apply -0.005/day decay."""
```

**Time Decay Formula:**
```
days_since = (now - last_updated).days
if days_since >= 1:
    decay = min(days_since * 0.005, 0.30)  # cap at -0.30 (never below 0.30 from time alone)
    new_trust = max(0.10, current_trust - decay)  # TRUST_MIN = 0.10
```

- [ ] **Step 1: Create TrustStore class**

Write `TrustStore` with `__init__`, `get`, `save`, `log_history`, `history`, `_apply_time_decay` methods. Use existing `PLASTIC_DB_PATH` or default `plastic_memory.db`.

- [ ] **Step 2: Write tests**

```python
class TestTrustStore:
    def test_get_default_returns_initial_trust(self):
        """Fresh store returns trust=0.6 for unknown target."""

    def test_save_and_get(self):
        """After save, get returns the updated value."""

    def test_history_logging(self):
        """After boost/decay, history is queryable."""

    def test_time_decay_applied(self):
        """When last_updated > 24h ago, trust decreases by 0.005/day."""

    def test_time_decay_capped(self):
        """Time decay never drops trust below 0.10."""

    def test_multi_target_isolation(self):
        """Different targets have independent trust scores."""
```

---

### Task 2: Integrate TrustStore into TrustManager

**Files:**
- Modify: `plastic_promise/defense/soul_enforcer.py` (TrustManager class)

**Changes:**
- `TrustManager.__init__` accepts optional `trust_store: TrustStore = None`
- `TrustManager.get()` delegates to `TrustStore.get()` (which applies time decay)
- `TrustManager.boost()` / `decay()` persist via `TrustStore.save()` + `TrustStore.log_history()`
- `TrustManager.history()` delegates to `TrustStore.history()`
- Backward compatible: if `trust_store` is None, fall back to in-memory mode (existing behavior)

- [ ] **Step 1: Add trust_store parameter to TrustManager**

```python
class TrustManager:
    def __init__(self, initial_trust: float = TRUST_INITIAL,
                 trust_store: TrustStore = None) -> None:
        self._trusts: Dict[str, float] = {}
        self._history: List[Dict[str, Any]] = []
        self._store = trust_store  # None = in-memory only (legacy)

    def get(self, target: str = "") -> float:
        if self._store:
            data = self._store.get(target)
            self._trusts[target] = data["trust"]  # sync cache
            return data["trust"]
        return self._trusts.get(target, TRUST_INITIAL)
```

- [ ] **Step 2: Modify boost/decay to persist**

```python
def boost(self, delta: float, reason: str = "", target: str = "") -> float:
    # ... existing logic ...
    if self._store:
        self._store.save(target, new, self.tier(target), self.autonomy_level(target))
        self._store.log_history(target, delta, reason, old, new, "boost")
    return new
```

Same pattern for `decay()`.

---

### Task 3: Wire TrustStore into MCP tools

**Files:**
- Modify: `plastic_promise/mcp/tools/audit_defense.py`

**Changes:**
- Replace `TrustManager()` with `TrustManager(trust_store=TrustStore())` 
- Use a module-level singleton: `_get_trust_manager()` → cached instance

- [ ] **Step 1: Create singleton helper**

```python
_trust_manager = None

def _get_trust_manager() -> TrustManager:
    global _trust_manager
    if _trust_manager is None:
        from plastic_promise.defense.trust_store import TrustStore
        _trust_manager = TrustManager(trust_store=TrustStore())
    return _trust_manager
```

- [ ] **Step 2: Replace all `TrustManager()` calls**

In `handle_defense_trust`: replace `tm = TrustManager()` with `tm = _get_trust_manager()`.

---

### Task 4: Wire TrustStore into SoulLoop.post_task

**Files:**
- Modify: `plastic_promise/loop/soul_loop.py`

**Changes:**
- `SoulLoop.post_task` step 4 (trust linkage) already uses `self._trust_manager`
- Change lazy-init to use `TrustStore`:

```python
if self._trust_manager is None:
    from plastic_promise.defense.trust_store import TrustStore
    from plastic_promise.defense.soul_enforcer import TrustManager
    self._trust_manager = TrustManager(trust_store=TrustStore())
```

- [ ] **Step 1: Update lazy-init in post_task**

---

### Task 5: Add violation-driven decay to SoulEnforcer

**Files:**
- Modify: `plastic_promise/defense/soul_enforcer.py`

**Changes:**
- `SoulEnforcer.pre_check()` already detects L0/L1 violations and logs them
- Add optional decay side effect: when L0 violation detected → `trust_manager.decay(0.05, "L0 violation")`
- When L1 violation detected (trust < 0.15) → `trust_manager.decay(0.02, "L1 critical trust")`

- [ ] **Step 1: Add decay triggers to pre_check**

```python
# In pre_check(), after L0 violation detected:
if self.trust_manager:
    self.trust_manager.decay(0.05, f"L0 violation: {pattern}")

# After L1 critical trust block:
if self.trust_manager:
    self.trust_manager.decay(0.02, f"L1 critical trust: {trust:.2f}")
```

---

### Task 6: Update CLAUDE.md & AGENTS.md — document new behavior

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`

**Changes:**
- Update 信任-自由度矩阵 to note time decay is now active
- Add section: "减分机制" documenting all decay triggers

- [ ] **Step 1: Update CLAUDE.md trust section**

Add:
```
## 减分机制（已生效）

| 触发条件 | 幅度 | 说明 |
|---------|------|------|
| SCARF < 0.40（step-closure 自动） | -0.02 | 低质量步骤 |
| L0 防线违规 | -0.05 | 危险操作被拦截 |
| L1 信任临界（< 0.15） | -0.02 | 信任过低封锁 |
| 时间衰减（24h 无活动） | -0.005/天 | 长期不活跃自然退化 |
| 用户打回/指出错误 | -0.03 | 手动调整 |
```

- [ ] **Step 2: Update AGENTS.md trust matrix**

---

### Task 7: Verification — end-to-end test

- [ ] **Step 1: Verify persistence across calls**

```
1. defense(action="adjust", delta=+0.05, reason="test boost")
2. defense(action="get")  → should show 0.65 (not 0.6)
3. Restart MCP server
4. defense(action="get")  → should still show 0.65
```

- [ ] **Step 2: Verify time decay (manual test)**

```
1. Directly set last_updated in DB to 48h ago
2. defense(action="get")  → should show 0.6 - 0.01 = 0.59
```

- [ ] **Step 3: Verify history is recorded**

```
1. defense(action="adjust", delta=-0.03, reason="test decay")
2. defense(action="history")  → should show the decay entry
```

- [ ] **Step 4: Verify step-closure trust linkage works**

```
1. step-closure(task_description="test", mode="full")
2. defense(action="get")  → trust should be updated (not necessarily 0.6)
```

---

## Risk Assessment

| Risk | Probability | Mitigation |
|------|------------|------------|
| Time decay too aggressive | Low | Cap at -0.30 total, 0.005/day is conservative |
| Violation decay double-counts | Medium | Only trigger on L0 block (not L1 warning), L1 only on critical |
| Existing tests break | Low | TrustManager API unchanged, TrustStore is optional |
| DB migration failure | Low | CREATE TABLE IF NOT EXISTS, same DB as existing |

---

## Success Metrics

- [ ] `defense(action="get")` returns the same value after MCP server restart
- [ ] `defense(action="history")` shows non-empty history after boost/decay
- [ ] Trust score changes (up or down) at least once within 24h of active use
- [ ] Time decay activates when agent is idle for 24h+