# MemoryTierManager Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Replace 5 `pass` stubs in MemoryTierManager with real L1/L3 tier migration logic.

**Architecture:** Loads MEMORY_TIERS config, classifies by worth_score+access_count, manages capacity-aware promote/demote/evict with optional RecMem integration for hard deletes.

**Tech Stack:** Python 3.10+, MEMORY_TIERS constant, MemoryRecord, RecMem

## Global Constraints

- File: `plastic_promise/memory/soul_memory.py`
- All methods tolerate None/invalid inputs gracefully (no crashes)
- promote_to_l3 checks L3 capacity before promoting
- evict_l1_overflow sorts by worth_score ascending, removes lowest first
- Optional rec_mem parameter for hard-delete support

---

### Task 1: Implement MemoryTierManager (all 5 methods)

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:197-257`

**Interfaces:**
- Produces: `MemoryTierManager.__init__(rec_mem=None)`, `classify_tier(record) -> str`, `promote_to_l3(record) -> None`, `demote_to_l1(record) -> None`, `evict_l1_overflow(records) -> List[str]`

- [ ] **Step 1: Replace __init__ pass**

Replace line 208:
```python
        from plastic_promise.core.constants import MEMORY_TIERS
        self.l1_config = MEMORY_TIERS.get("L1", {"max_items": 200, "ttl_hours": 24})
        self.l3_config = MEMORY_TIERS.get("L3", {"max_items": 2000, "ttl_hours": None})
        self.rec_mem = rec_mem
```

- [ ] **Step 2: Replace classify_tier pass**

Replace line 222:
```python
        if record is None:
            return "L1"
        try:
            if record.worth_score >= 0.5 and record.access_count >= 3:
                return "L3"
        except Exception:
            pass
        return "L1"
```

- [ ] **Step 3: Replace promote_to_l3 pass**

Replace line 233:
```python
        if record is None:
            return
        l3_max = self.l3_config.get("max_items", 2000)
        # If RecMem available, check L3 count and evict lowest if full
        if self.rec_mem is not None:
            try:
                l3_records = [r for r in self.rec_mem._records.values() if r.tier == "L3"]
                if len(l3_records) >= l3_max:
                    l3_records.sort(key=lambda r: r.worth_score)
                    self.demote_to_l1(l3_records[0])
            except Exception:
                pass
        record.tier = "L3"
```

- [ ] **Step 4: Replace demote_to_l1 pass**

Replace line 243:
```python
        if record is not None:
            record.tier = "L1"
```

- [ ] **Step 5: Replace evict_l1_overflow pass**

Replace line 257:
```python
        if not records:
            return []
        l1_max = self.l1_config.get("max_items", 200)
        if len(records) <= l1_max:
            return []
        sorted_records = sorted(records, key=lambda r: r.worth_score)
        overflow = len(sorted_records) - l1_max
        evicted = []
        for r in sorted_records[:overflow]:
            evicted.append(r.memory_id)
            if self.rec_mem is not None:
                try:
                    self.rec_mem.forget(r.memory_id, reason="L1 overflow eviction")
                except Exception:
                    pass
        return evicted
```

- [ ] **Step 6: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.memory.soul_memory import MemoryRecord, MemoryTierManager, RecMem

tm = MemoryTierManager()

# classify_tier: high worth + high access → L3
r1 = MemoryRecord(content='important', worth_success=10, worth_failure=1, tier='L1')
r1.access_count = 5
assert tm.classify_tier(r1) == 'L3'
print('classify L3 OK')

# classify_tier: low access → L1
r2 = MemoryRecord(content='new', worth_success=5, worth_failure=5, tier='L1')
r2.access_count = 0
assert tm.classify_tier(r2) == 'L1'
print('classify L1 OK')

# promote_to_l3
tm.promote_to_l3(r2)
assert r2.tier == 'L3'
print('promote OK')

# demote_to_l1
tm.demote_to_l1(r2)
assert r2.tier == 'L1'
print('demote OK')

# evict_l1_overflow with 5 records, L1 max=200 → no eviction
records = [MemoryRecord(content=f'r{i}') for i in range(5)]
evicted = tm.evict_l1_overflow(records)
assert len(evicted) == 0
print('no overflow OK')

# Test with small capacity (override)
tm.l1_config['max_items'] = 3
r_low = MemoryRecord(content='low', worth_success=0, worth_failure=10, tier='L1')
r_high = MemoryRecord(content='high', worth_success=10, worth_failure=0, tier='L1')
records2 = [r_low, r_high, MemoryRecord(content='mid', worth_success=1, worth_failure=1, tier='L1'), MemoryRecord(content='x', tier='L1')]
evicted2 = tm.evict_l1_overflow(records2)
assert len(evicted2) == 1
print(f'overflow evicted: {evicted2}')
print('ALL TESTS PASSED')
"
```

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/memory/soul_memory.py
git commit -m "feat: implement MemoryTierManager — classify, promote, demote, evict"
```
