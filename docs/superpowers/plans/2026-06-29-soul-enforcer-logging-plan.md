# SoulEnforcer Logging Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Replace 3 `pass` stubs: TrustManager.history, SoulEnforcer.log_violation, SoulEnforcer.get_violation_stats.

**Architecture:** Pure data access — all backing data structures already populated.

**Tech Stack:** Python 3.10+, datetime.timezone

## Global Constraints
- File: `plastic_promise/defense/soul_enforcer.py`
- history returns last `limit` entries (already sorted by time from boost/decay)
- log_violation uses same format as pre_check's existing log entries
- get_violation_stats aggregates from _violation_log

---

### Task 1: Implement 3 logging stubs

**Files:**
- Modify: `plastic_promise/defense/soul_enforcer.py:120-340`

**Verification:**
```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.defense.soul_enforcer import TrustManager, SoulEnforcer

# TrustManager.history
tm = TrustManager()
tm.boost(0.1, 'test boost')
tm.decay(0.05, 'test decay')
h = tm.history(10)
assert len(h) == 2
assert h[0]['direction'] == 'decay'
print('history OK:', len(h), 'entries')

# SoulEnforcer.log_violation
se = SoulEnforcer(tm)
se.log_violation('test action', 'L2', 'manual test')
assert len(se._violation_log) == 1
print('log_violation OK')

# SoulEnforcer.get_violation_stats
se.log_violation('another', 'L2', 'test')
se.log_violation('l0 test', 'L0', 'danger')
stats = se.get_violation_stats()
assert stats['total'] == 3
assert stats['by_layer']['L2'] == 2
assert stats['by_layer']['L0'] == 1
assert 'today' in stats
assert 'recent' in stats
print('violation_stats OK:', stats['total'], 'total, by_layer:', stats['by_layer'])
print('ALL TESTS PASSED')
"
```

**Commit:** `feat: implement TrustManager.history + SoulEnforcer.log_violation + get_violation_stats`
