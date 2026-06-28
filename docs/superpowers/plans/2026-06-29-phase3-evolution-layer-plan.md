# Phase 3 — Evolution Layer Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Build the four-component evolution layer: worth feedback loop, behavior tracker, curiosity loop, principle trends.

**Architecture:** Four independent modules that each enhance an existing file. No cross-dependencies between tasks.

**Tech Stack:** Python 3.10+, existing ContextEngine, PrincipleTracker, CuriosityExplorer, EvolveR

## Global Constraints

- Task 1: auto access_count++ in `_text_retrieval`, EvolveR trigger in `memory_correct`
- Task 2: new file `plastic_promise/behavior.py` — AgentBehaviorTracker
- Task 3: enhance `soul_curiosity.py` — add curiosity_act(), curiosity_stats(), adaptive rate
- Task 4: enhance `core/principles.py` PrincipleTracker — add trends(), weakest()
- Each task ends with an independently testable deliverable

---

### Task 1: worth 反馈闭环

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (_text_retrieval + access_count)
- Modify: `plastic_promise/mcp/tools/memory.py` (memory_correct → EvolveR trigger)

- [ ] **Step 1: Auto access_count++ on retrieval**

In `_text_retrieval`, after matching a memory, increment access_count:

```python
            if score > 0:
                # Auto access tracking — 越用越聪明
                mem["access_count"] = mem.get("access_count", 0) + 1
                if mem.get("access_count", 0) >= 5:
                    mem["worth_success"] = mem.get("worth_success", 0) + 1
```

- [ ] **Step 2: EvolveR trigger in memory_correct**

In `handle_memory_correct`, after applying mark_as, trigger EvolveR:

```python
        # Trigger EvolveR after correction — 自演化闭环
        try:
            from plastic_promise.memory.soul_memory import RecMem, EvolveR
            rm = RecMem(engine)
            evolver = EvolveR(rm)
            evolver.evolve_cycle()
        except Exception:
            pass
```

- [ ] **Step 3: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine()
engine.register_memory({'id':'t1','content':'test memory','source':'user'})
results = engine._text_retrieval('test memory')
mem = engine._memories.get('t1',{})
assert mem.get('access_count',0) > 0
print(f'access_count: {mem[\"access_count\"]}')
print('WORTH FEEDBACK LOOP PASSED')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/context_engine.py plastic_promise/mcp/tools/memory.py
git commit -m "feat: worth feedback loop — auto access_count++ on retrieval + EvolveR trigger on correct"
```

---

### Task 2: AgentBehaviorTracker

**Files:**
- Create: `plastic_promise/behavior.py`

```python
"""AgentBehaviorTracker — 行为模式学习引擎

Serves 演化层: 追踪 Agent 行为模式，让系统"越用越默契"。
"""

import datetime
from typing import Any, Dict, List


class AgentBehaviorTracker:
    """Tracks Agent behavior patterns across sessions."""

    def __init__(self):
        self._events: List[dict] = []

    def record(
        self,
        task_type: str,
        principles: List[str],
        memory_types: List[str],
        owner: str = "",
    ):
        self._events.append({
            "task_type": task_type,
            "principles": principles,
            "memory_types": memory_types,
            "owner": owner,
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def stats(self) -> dict:
        total = len(self._events)
        if total == 0:
            return {"session_count": 0, "top_task_types": [], "principle_heatmap": {}, "memory_type_distribution": {}}

        task_counts: Dict[str, int] = {}
        principle_counts: Dict[str, int] = {}
        memory_counts: Dict[str, int] = {}

        for e in self._events:
            task_counts[e["task_type"]] = task_counts.get(e["task_type"], 0) + 1
            for p in e["principles"]:
                principle_counts[p] = principle_counts.get(p, 0) + 1
            for m in e["memory_types"]:
                memory_counts[m] = memory_counts.get(m, 0) + 1

        return {
            "session_count": total,
            "top_task_types": sorted(task_counts.items(), key=lambda x: -x[1])[:5],
            "principle_heatmap": principle_counts,
            "memory_type_distribution": memory_counts,
        }

    def pattern(self) -> str:
        s = self.stats()
        if s["session_count"] == 0:
            return "尚未积累足够的行为数据。"
        top_task = s["top_task_types"][0] if s["top_task_types"] else ("未知", 0)
        top_p = max(s["principle_heatmap"].items(), key=lambda x: x[1]) if s["principle_heatmap"] else ("无", 0)
        top_m = max(s["memory_type_distribution"].items(), key=lambda x: x[1]) if s["memory_type_distribution"] else ("无", 0)
        return (
            f"在过去 {s['session_count']} 次交互中：\n"
            f"  最常做 {top_task[0]} 类任务（{top_task[1]}次），\n"
            f"  最常遵循原则「{top_p[0]}」（{top_p[1]}次），\n"
            f"  最常检索 {top_m[0]} 类记忆（{top_m[1]}次）。"
        )
```

- [ ] **Step 2: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.behavior import AgentBehaviorTracker
bt = AgentBehaviorTracker()
bt.record('code_generation', ['奥卡姆剃刀', '上下文驱动决策'], ['experience', 'reflection'], 'claude')
bt.record('architecture', ['器官互保'], ['experience'], 'claude')
s = bt.stats()
assert s['session_count'] == 2
assert s['top_task_types'][0][0] == 'code_generation'
print(f'Stats: {s}')
print('Pattern:', bt.pattern()[:80])
print('BEHAVIOR TRACKER PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/behavior.py
git commit -m "feat: AgentBehaviorTracker — record task patterns and generate behavior summaries"
```

---

### Task 3: curiosity 闭环

**Files:**
- Modify: `plastic_promise/reflection/soul_curiosity.py`

Add after the existing `curiosity_explore` function:

```python
_exploration_log: List[Dict[str, Any]] = []
_explore_rate = 0.15


def curiosity_act(suggestion_id: str, outcome: str) -> Dict[str, Any]:
    """Record exploration outcome and adapt explore rate.

    Args:
        suggestion_id: ID from curiosity_explore result.
        outcome: "adopted" | "ignored" | "failed"
    """
    global _explore_rate, _exploration_log
    _exploration_log.append({
        "suggestion_id": suggestion_id,
        "outcome": outcome,
        "timestamp": __import__('datetime').datetime.now().isoformat(),
    })
    # Adaptive explore rate
    adopted = sum(1 for e in _exploration_log if e["outcome"] == "adopted")
    total = len(_exploration_log)
    adopted_rate = adopted / total if total > 0 else 0.5
    if adopted_rate > 0.7:
        _explore_rate = min(0.30, _explore_rate + 0.02)
    elif adopted_rate < 0.3:
        _explore_rate = max(0.05, _explore_rate - 0.02)
    return {"explore_rate": _explore_rate, "adopted_rate": adopted_rate, "total": total}


def curiosity_stats() -> Dict[str, Any]:
    """Return curiosity exploration statistics."""
    adopted = sum(1 for e in _exploration_log if e["outcome"] == "adopted")
    total = len(_exploration_log)
    return {
        "explore_rate": _explore_rate,
        "total_explorations": total,
        "adopted": adopted,
        "ignored": sum(1 for e in _exploration_log if e["outcome"] == "ignored"),
        "failed": sum(1 for e in _exploration_log if e["outcome"] == "failed"),
        "adopted_rate": round(adopted / total, 3) if total > 0 else 0.0,
    }
```

- [ ] **Step 2: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.reflection.soul_curiosity import curiosity_act, curiosity_stats
curiosity_act('sug_1', 'adopted')
curiosity_act('sug_2', 'adopted')
curiosity_act('sug_3', 'ignored')
curiosity_act('sug_4', 'adopted')
s = curiosity_stats()
assert s['total_explorations'] == 4
assert s['adopted'] == 3
assert s['adopted_rate'] == 0.75
assert s['explore_rate'] > 0.15  # boosted for high adoption
print(f'Stats: {s}')
print('CURIOSITY LOOP PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/reflection/soul_curiosity.py
git commit -m "feat: curiosity closed loop — curiosity_act + adaptive explore_rate"
```

---

### Task 4: PrincipleTracker 趋势分析

**Files:**
- Modify: `plastic_promise/core/principles.py`

Add to PrincipleTracker:

```python
    def trends(self, recent_n: int = 10) -> dict:
        """Return per-principle adherence trends."""
        stats = self.stats()
        for pid_str, s in stats.items():
            pid = int(pid_str)
            events = self._records.get(pid, [])
            if len(events) >= 2:
                recent = events[-recent_n:]
                recent_total = len(recent)
                recent_adhered = sum(1 for e in recent if e["adhered"])
                recent_rate = round(recent_adhered / recent_total, 3) if recent_total > 0 else 0
                total_rate = s["rate"] or 0
                if recent_rate > total_rate + 0.1:
                    trend = "↑上升"
                elif recent_rate < total_rate - 0.1:
                    trend = "↓下降"
                else:
                    trend = "→稳定"
                s["recent_rate"] = recent_rate
                s["trend"] = trend
        return stats

    def weakest(self, n: int = 3) -> list:
        """Return the N principles with lowest adherence rates."""
        stats = self.stats()
        ranked = sorted(
            [(pid, s) for pid, s in stats.items() if s["rate"] is not None],
            key=lambda x: x[1]["rate"]
        )
        return [{"id": pid, "name": s["name"], "rate": s["rate"]} for pid, s in ranked[:n]]
```

- [ ] **Step 2: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.principles import PrincipleTracker
pt = PrincipleTracker()
for i in range(5):
    pt.record(1, True, 'test')
for i in range(3):
    pt.record(1, False, 'test')
pt.record(4, True, 'test')
pt.record(4, False, 'test')
t = pt.trends()
assert t['1']['trend'] in ('↑上升', '→稳定', '↓下降')
assert t['1']['recent_rate'] is not None
print(f'Principle 1 trend: {t[\"1\"][\"trend\"]}, rate={t[\"1\"][\"rate\"]}')
w = pt.weakest(2)
assert len(w) <= 2
print(f'Weakest: {w}')
print('PRINCIPLE TRENDS PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/principles.py
git commit -m "feat: PrincipleTracker trends() and weakest() — adherence trend analysis"
```
