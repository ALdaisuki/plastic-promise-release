# System Activity Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Activate the three dead subsystems found in the 2026-07-02 audit: Weibull decay, worth feedback, and tier-aware half-life configuration. Fix CEI property access and daemon evolve scheduling.

**Architecture:** Five independent fixes across `constants.py` (config), `soul_memory.py` (evolve trigger), `context_engine.py` (CEI access), and `maintenance_daemon.py` (scheduling). All fixes are additive — no API changes, no breaking behavior.

**Tech Stack:** Python 3.11+, SQLite, LanceDB, Weibull decay math (existing).

## Global Constraints

- All changes in worktree `worktree-system-activity-fix`
- Follow Conventional Commits: `fix:` prefix for bug fixes
- No emoji in any file
- TDD: write failing test first, then implement fix
- pytest `tests/` (skip `test_safety_net_daemon.py` — pre-existing config issue)

---

### Task 1: Add L2 Tier to DECAY_CONFIG

**Files:**
- Modify: `plastic_promise/core/constants.py:604-607`

**Interfaces:**
- Consumes: `DECAY_CONFIG` dict (existing)
- Produces: `DECAY_CONFIG["L2"] = {"beta": 1.2, "half_life_days": 7}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_decay_config.py`:

```python
"""Test that DECAY_CONFIG covers all active tiers."""
import pytest
from plastic_promise.core.constants import DECAY_CONFIG


def test_decay_config_has_l2():
    """L2 tier must exist — 146 of 186 memories are L2."""
    assert "L2" in DECAY_CONFIG, (
        "DECAY_CONFIG missing L2 entry. 146 memories fall through to 'default' "
        "instead of getting L2-specific decay parameters."
    )


def test_decay_config_has_all_tiers():
    """Every tier that appears in the memory pool needs a decay config."""
    required = {"L1", "L2", "L3"}
    missing = required - set(DECAY_CONFIG.keys())
    assert not missing, f"DECAY_CONFIG missing tiers: {missing}"


def test_l2_params_sane():
    """L2 half-life should be between L1 (3d) and L3 (90d)."""
    cfg = DECAY_CONFIG.get("L2", {})
    hl = cfg.get("half_life_days", 0)
    assert 3 < hl < 90, f"L2 half_life={hl} not between L1(3) and L3(90)"
    beta = cfg.get("beta", 0)
    assert 0.5 < beta < 2.0, f"L2 beta={beta} out of sane range [0.5, 2.0]"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_decay_config.py -v`
Expected: `FAILED test_decay_config_has_l2 — DECAY_CONFIG missing L2 entry`

- [ ] **Step 3: Add L2 config**

Edit `plastic_promise/core/constants.py`, find `DECAY_CONFIG`:

```python
DECAY_CONFIG = {
    "L1": {"beta": 1.5, "half_life_days": 3},
    "L2": {"beta": 1.2, "half_life_days": 7},   # <-- ADD THIS LINE
    "L3": {"beta": 0.7, "half_life_days": 90},
    "default": {"beta": 1.0, "half_life_days": 14},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_decay_config.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/constants.py tests/test_decay_config.py
git commit -m "fix: add L2 tier to DECAY_CONFIG — beta=1.2, half_life=7d"
```

---

### Task 2: Fix effective_half_life Initialization — Tier-Aware Defaults

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py` (MemoryRecord.__init__ or factory)
- Test: `tests/test_memory_record.py`

**Interfaces:**
- Consumes: `MemoryRecord(tier, ...)` constructor
- Produces: `effective_half_life` matches tier: L1=3d, L2=7d, L3=90d

- [ ] **Step 1: Locate where effective_half_life is set to 3.0**

Search for `effective_half_life` initialization in `soul_memory.py`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_memory_record.py`:

```python
"""Test that MemoryRecord gets tier-appropriate half-life."""
from plastic_promise.memory.soul_memory import MemoryRecord


def test_l1_half_life_is_3_days():
    r = MemoryRecord("test1", "content", tier="L1")
    assert r.effective_half_life == 3.0, f"L1 half-life={r.effective_half_life}"


def test_l2_half_life_is_7_days():
    r = MemoryRecord("test2", "content", tier="L2")
    assert r.effective_half_life == 7.0, f"L2 half-life={r.effective_half_life}"


def test_l3_half_life_is_90_days():
    r = MemoryRecord("test3", "content", tier="L3")
    assert r.effective_half_life == 90.0, f"L3 half-life={r.effective_half_life}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_memory_record.py -v`
Expected: 2 of 3 FAIL (L2 and L3 both return 3.0)

- [ ] **Step 4: Fix the initialization**

In `soul_memory.py`, the `MemoryRecord.__init__` or factory method where `effective_half_life` defaults to `3.0`. Replace the hardcoded `3.0` with a tier lookup:

```python
# Before (hardcoded):
self.effective_half_life = effective_half_life if effective_half_life is not None else 3.0

# After (tier-aware):
if effective_half_life is not None:
    self.effective_half_life = effective_half_life
else:
    from plastic_promise.core.constants import DECAY_CONFIG
    hl_map = {"L1": 3, "L2": 7, "L3": 90}
    self.effective_half_life = float(hl_map.get(self.tier, 14))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_memory_record.py -v`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/memory/soul_memory.py tests/test_memory_record.py
git commit -m "fix: tier-aware effective_half_life — L1=3d, L2=7d, L3=90d"
```

---

### Task 3: Trigger Weibull Batch Decay Update on GC Cycle

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py` (evolve or collect method)
- Modify: `daemons/maintenance_daemon.py` (add evolve to scan cycle)

**Interfaces:**
- Consumes: `MemoryGC.evolve()` (existing), `maintenance_daemon` main loop
- Produces: decay_multiplier updated on every GC cycle for all records

- [ ] **Step 1: Write the failing test**

Create `tests/test_decay_update.py`:

```python
"""Test that Weibull decay is actually applied to memories."""
import datetime
from plastic_promise.core.decay_engine import WeibullDecayCalculator
from plastic_promise.memory.soul_memory import MemoryRecord


def test_decay_applied_to_old_memory():
    """A 5-day-old L1 memory should have decay < 0.5 (half-life=3d)."""
    r = MemoryRecord("test_decay", "old content", tier="L1")
    r.created_at = (datetime.datetime.now() - datetime.timedelta(days=5)).isoformat()
    r.effective_half_life = 3.0

    calc = WeibullDecayCalculator()
    dm = calc.compute_decay("L1", r.created_at, effective_half_life=3.0)

    assert dm < 0.5, (
        f"5-day-old L1 memory (half-life=3d) should have decay < 0.5, "
        f"got {dm:.4f}"
    )


def test_decay_batch_update_changes_values():
    """After evolve(), records should have non-1.0 decay values."""
    # This tests the evolve() pipeline end-to-end
    from plastic_promise.memory.soul_memory import RecMem
    rm = RecMem()

    # Count records with non-trivial decay before
    records_before = list(rm._records.values())
    stuck_before = sum(
        1 for r in records_before
        if getattr(r, 'decay_multiplier', 1.0) > 0.999
    )

    # Trigger evolve — this should recompute all decay values
    result = rm.evolve()
    assert result is not None, "evolve() returned None"

    # Count records with non-trivial decay after
    records_after = list(rm._records.values())
    stuck_after = sum(
        1 for r in records_after
        if getattr(r, 'decay_multiplier', 1.0) > 0.999
    )

    assert stuck_after < stuck_before, (
        f"evolve() should reduce stuck records: "
        f"{stuck_before} -> {stuck_after}"
    )
```

- [ ] **Step 2: Run test to verify decay math works**

Run: `pytest tests/test_decay_update.py::test_decay_applied_to_old_memory -v`
Expected: PASS (Weibull formula itself is correct)

- [ ] **Step 3: Run test to verify evolve doesn't update decay**

Run: `pytest tests/test_decay_update.py::test_decay_batch_update_changes_values -v`
Expected: FAIL (evolve() exists but doesn't reduce stuck count — all still ~1.0)

- [ ] **Step 4: Fix evolve() decay update**

In `soul_memory.py`, find the `evolve()` method. The Phase A decay batch update code (lines 906-926) exists but may not be persisting correctly. Verify it calls `evaluate_all()` and writes back to both `_records` and SQLite.

Add explicit decay update trigger in `collect()` or add a new public method:

```python
def update_all_decay(self) -> int:
    """Recompute and persist decay_multiplier for all records. Returns count."""
    from plastic_promise.core.decay_engine import WeibullDecayCalculator
    import datetime

    wdc = WeibullDecayCalculator()
    records = list(self._records.values())
    now = datetime.datetime.now().isoformat()
    updated = 0

    for r in records:
        dm = wdc.compute_decay(
            tier=r.tier,
            created_at=r.created_at,
            effective_half_life=getattr(r, 'effective_half_life', None),
            current_time_str=now,
        )
        if abs(r.decay_multiplier - dm) > 0.001:
            r.decay_multiplier = dm
            updated += 1

    # Persist to SQLite
    if updated > 0 and self._engine:
        for r in records:
            self._engine.execute_sql(
                "UPDATE memories SET decay_multiplier = ? WHERE id = ?",
                (r.decay_multiplier, r.memory_id)
            )
        self._engine.commit_sql()

    return updated
```

- [ ] **Step 5: Run test to verify evolve now works**

Run: `pytest tests/test_decay_update.py -v`
Expected: Both PASS

- [ ] **Step 6: Add evolve to daemon scan cycle**

In `daemons/maintenance_daemon.py`, add a periodic decay update to the main loop. Find the `while True` loop and add every 60 minutes:

```python
# Decay batch update — every 60 minutes
if loop_count % 360 == 0:  # 10s * 360 = 60min
    try:
        from plastic_promise.memory.soul_memory import RecMem
        rm = RecMem()
        updated = rm.update_all_decay()
        if updated > 0:
            print(f"[decay] Updated {updated} decay values")
    except Exception as e:
        print(f"[decay] Update failed: {e}")
```

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/memory/soul_memory.py daemons/maintenance_daemon.py tests/test_decay_update.py
git commit -m "fix: trigger Weibull decay batch update on GC cycle and daemon heartbeat"
```

---

### Task 4: Fix CEI Property Access

**Files:**
- Modify: `plastic_promise/loop/soul_loop.py` (CEI property)
- Test: `tests/test_cei_access.py`

**Interfaces:**
- Produces: `SoulLoop.current_cei` accessible as `SoulLoop.current_cei` (property, no `.fget` needed), or static method `SoulLoop.get_cei()`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cei_access.py`:

```python
"""Test that CEI can be accessed without internal knowledge."""
import pytest
from plastic_promise.loop.soul_loop import SoulLoop


def test_cei_accessible():
    """CEI should be readable via a public API, not require .fget hack."""
    # Must work without AttributeError or TypeError
    try:
        cei = SoulLoop.current_cei
        assert isinstance(cei, (int, float)), f"CEI should be numeric, got {type(cei)}"
    except TypeError as e:
        pytest.fail(f"CEI property access failed: {e}")


def test_cei_in_range():
    """CEI should be in [0, 1] range."""
    cei = SoulLoop.current_cei
    assert 0.0 <= cei <= 1.0, f"CEI={cei} out of [0,1] range"
```

- [ ] **Step 2: Fix CEI accessor**

In `soul_loop.py`, find the `current_cei` property definition. If it's a `@property` on the class, add a class-level fallback:

```python
# In SoulLoop class:
_cei_cache: float = 0.70  # default

@classmethod
def get_cei(cls) -> float:
    """Return current CEI. Safe for class-level access."""
    try:
        return cls.current_cei
    except (TypeError, AttributeError):
        return cls._cei_cache
```

Or convert `current_cei` from an instance `@property` to a `@classmethod` + `@property` combo using a descriptor, or simply make it a module-level function:

```python
# In soul_loop.py, at module level:
def get_current_cei() -> float:
    """Return cached CEI value."""
    return getattr(SoulLoop, '_cei_cache', 0.70)
```

- [ ] **Step 3: Run test to verify**

Run: `pytest tests/test_cei_access.py -v`
Expected: `2 passed`

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/loop/soul_loop.py tests/test_cei_access.py
git commit -m "fix: CEI property accessible via SoulLoop.get_cei() classmethod"
```

---

### Task 5: Integration Verification — Run evolve and Validate

**Files:**
- Verify: all test files
- Run: full evolve cycle on live data

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/test_decay_config.py tests/test_memory_record.py tests/test_decay_update.py tests/test_cei_access.py -v
```
Expected: all tests pass

- [ ] **Step 2: Run evolve on live data**

```bash
cd F:/Agent/Memory system/.claude/worktrees/system-activity-fix
python -c "
from plastic_promise.memory.soul_memory import RecMem
rm = RecMem()
updated = rm.update_all_decay()
print(f'Decay updated: {updated} records')
# Verify values changed
records = list(rm._records.values())
stuck = sum(1 for r in records if getattr(r,'decay_multiplier',1.0) > 0.999)
print(f'Still stuck: {stuck}/{len(records)}')
"
```
Expected: `updated > 0`, `stuck < 186`

- [ ] **Step 3: Verify tier-aware half-life**

```bash
python -c "
from plastic_promise.memory.soul_memory import RecMem
rm = RecMem()
for r in list(rm._records.values())[:10]:
    print(f'tier={r.tier} hl={r.effective_half_life} decay={r.decay_multiplier:.4f}')
"
```
Expected: L1 records have hl=3, L2 have hl=7, L3 have hl=90

- [ ] **Step 4: Final commit (if any remaining changes)**

```bash
git status
git add -A
git commit -m "chore: integration verification — decay update, tier-aware HL, CEI access"
```
