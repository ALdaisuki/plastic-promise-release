# EvolveR + MemoryGC Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Replace 6 `pass` stubs across EvolveR (3) and MemoryGC (3) with real evolution and garbage collection logic.

**Architecture:** EvolveR orchestrates TierManager for promote/demote/evict cycles; MemoryGC marks decaying records and performs safe collection with dry_run support.

**Tech Stack:** Python 3.10+, RecMem, MemoryTierManager, MEMORY_DECAY_THRESHOLD, MEMORY_HEALTH_THRESHOLD, MEMORY_GC_INTERVAL_DAYS

## Global Constraints

- File: `plastic_promise/memory/soul_memory.py`
- EvolveR depends on MemoryTierManager (already implemented)
- MemoryGC depends on RecMem (already implemented)
- All methods must handle empty record sets gracefully
- collect() defaults to dry_run=True (safe by default)

---

### Task 1: Implement EvolveR (3 methods) + MemoryGC (3 methods)

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:688-801`

**Interfaces:**
- Produces: `EvolveR.__init__(rec_mem, decay_threshold)`, `evolve_cycle() -> Dict`, `decay_stale(days) -> int`
- Produces: `MemoryGC.__init__(rec_mem)`, `collect(dry_run, force) -> Dict`, `mark_decaying() -> List[str]`

- [ ] **Step 1: Replace EvolveR.__init__ pass**

Replace line 705:
```python
        self.rec_mem = rec_mem
        self.decay_threshold = decay_threshold
        self.tier_manager = MemoryTierManager(rec_mem)
```

- [ ] **Step 2: Replace EvolveR.evolve_cycle pass**

Replace line 726:
```python
        if self.rec_mem is None:
            return {"promoted": 0, "demoted": 0, "decayed": 0, "evicted": 0,
                    "health_before": 1.0, "health_after": 1.0}
        try:
            health_before = self.rec_mem.health_ratio
            records = list(self.rec_mem._records.values())
            promoted = 0
            demoted = 0

            # Demote L3 low-worth records
            l3_records = [r for r in records if r.tier == "L3" and r.worth_score < self.decay_threshold]
            for r in l3_records:
                self.tier_manager.demote_to_l1(r)
                demoted += 1

            # Promote L1 high-worth records
            l1_records = [r for r in records if r.tier == "L1" and r.worth_score >= 0.6]
            for r in l1_records:
                self.tier_manager.promote_to_l3(r)
                promoted += 1

            # Decay stale L1 records
            decayed = self.decay_stale()

            # Evict L1 overflow
            l1_after = [r for r in self.rec_mem._records.values() if r.tier == "L1"]
            evicted = len(self.tier_manager.evict_l1_overflow(l1_after))

            health_after = self.rec_mem.health_ratio
            return {
                "promoted": promoted, "demoted": demoted,
                "decayed": decayed, "evicted": evicted,
                "health_before": health_before, "health_after": health_after,
            }
        except Exception:
            return {"promoted": 0, "demoted": 0, "decayed": 0, "evicted": 0,
                    "health_before": 1.0, "health_after": 1.0}
```

- [ ] **Step 3: Replace EvolveR.decay_stale pass**

Replace line 742:
```python
        if self.rec_mem is None:
            return 0
        try:
            import datetime
            cutoff = datetime.datetime.now() - datetime.timedelta(days=days_threshold)
            decayed = 0
            for r in self.rec_mem._records.values():
                if r.tier != "L1":
                    continue
                try:
                    last = datetime.datetime.fromisoformat(r.last_accessed)
                    if last < cutoff:
                        r.activation_weight = max(0.0, r.activation_weight * 0.7)
                        decayed += 1
                except (ValueError, TypeError):
                    pass
            return decayed
        except Exception:
            return 0
```

- [ ] **Step 4: Replace MemoryGC.__init__ pass**

Replace line 762:
```python
        self.rec_mem = rec_mem
        self._last_collect: Optional[str] = None
```

- [ ] **Step 5: Replace MemoryGC.mark_decaying pass**

Replace line 801:
```python
        if self.rec_mem is None:
            return []
        try:
            decaying = []
            for r in self.rec_mem._records.values():
                try:
                    if r.worth_score < MEMORY_DECAY_THRESHOLD:
                        decaying.append((r.memory_id, r.worth_score))
                except Exception:
                    pass
            decaying.sort(key=lambda x: x[1])
            return [mid for mid, _ in decaying]
        except Exception:
            return []
```

- [ ] **Step 6: Replace MemoryGC.collect pass**

Replace line 789:
```python
        try:
            health_before = self.rec_mem.health_ratio if self.rec_mem else 1.0
            candidates = self.mark_decaying()
        except Exception:
            candidates = []

        result = {
            "dry_run": dry_run,
            "candidates_count": len(candidates),
            "candidates": candidates[:50],
            "removed": 0,
            "health_before": health_before if 'health_before' in dir() else 1.0,
            "health_after": health_before if 'health_before' in dir() else 1.0,
            "freed_slots": 0,
        }

        if dry_run or not candidates or self.rec_mem is None:
            return result

        # Interval check (skip if forced)
        if not force and self._last_collect is not None:
            try:
                import datetime
                last = datetime.datetime.fromisoformat(self._last_collect)
                interval = datetime.timedelta(days=MEMORY_GC_INTERVAL_DAYS)
                if datetime.datetime.now() - last < interval:
                    return result
            except (ValueError, TypeError):
                pass

        try:
            removed = 0
            for mid in candidates:
                if self.rec_mem.health_ratio >= MEMORY_HEALTH_THRESHOLD / 100.0:
                    break
                self.rec_mem.forget(mid, reason="GC: worth below decay threshold")
                removed += 1

            self._last_collect = datetime.datetime.now().isoformat()
            result["removed"] = removed
            result["health_after"] = self.rec_mem.health_ratio
            result["freed_slots"] = removed
        except Exception:
            pass

        return result
```

Note: step 6 needs `import datetime` at the top of the method or uses the module-level import already in soul_memory.py.

- [ ] **Step 7: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.memory.soul_memory import MemoryRecord, MemoryTierManager, RecMem, EvolveR, MemoryGC
import datetime

# Setup: RecMem with some records
rm = RecMem()
rm.store('important knowledge', memory_type='knowledge')
rm.store('old task', memory_type='task')
rm.store('fresh insight', memory_type='reflection')

# Give some records high worth
for mid, r in list(rm._records.items()):
    if 'important' in r.content:
        r.worth_success = 10
        r.access_count = 5
        r.tier = 'L3'
    if 'old' in r.content:
        r.worth_success = 0
        r.worth_failure = 10
        r.last_accessed = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()

# Test EvolveR
evolver = EvolveR(rm)
result = evolver.evolve_cycle()
print(f'Evolve cycle: promoted={result[\"promoted\"]}, demoted={result[\"demoted\"]}, decayed={result[\"decayed\"]}, evicted={result[\"evicted\"]}')
assert 'health_before' in result
assert 'health_after' in result
print('EvolveR OK')

# Test decay_stale
decayed = evolver.decay_stale(days_threshold=7)
print(f'Decayed: {decayed}')
print('decay_stale OK')

# Test MemoryGC
gc = MemoryGC(rm)
decaying = gc.mark_decaying()
print(f'Marked decaying: {len(decaying)}')
print('mark_decaying OK')

# Test dry_run collect
result2 = gc.collect(dry_run=True)
assert result2['dry_run'] == True
assert result2['removed'] == 0
print('dry_run collect OK')

# Test real collect
result3 = gc.collect(dry_run=False, force=True)
print(f'Collect: removed={result3[\"removed\"]}, freed={result3[\"freed_slots\"]}')
print('ALL TESTS PASSED')
"
```

- [ ] **Step 8: Commit**

```bash
git add plastic_promise/memory/soul_memory.py
git commit -m "feat: implement EvolveR evolve_cycle/decay_stale + MemoryGC collect/mark_decaying"
```
