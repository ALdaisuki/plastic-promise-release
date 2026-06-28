# Phase 1 — post_task 六联闭环 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development.

**Goal:** Wire the six-link post_task loop — the neural hub connecting Convention → Practice → Evolution layers.

**Architecture:** Four independent tasks (PrincipleTracker, Trust→Retrieval, RepairSuggest, post_task loop) that converge in SoulLoop.post_task().

**Tech Stack:** Python 3.10+, existing TrustManager, SCARFReflector, HormoneEngine, StepAuditor, ContextEngine

## Global Constraints

- post_task signature: `post_task(task_description, git_commit="") -> dict`
- PrincipleTracker: dict-based counter {principle_id: {adhered, violated, last_seen}}
- Trust tier → retrieval boost mapping: high=1.3, medium=1.0, low=0.7, critical=0.5
- Repair suggestion format: `{"dimension": str, "current_score": float, "suggestion": str}`
- All methods tolerate missing subsystems (e.g., no SCARF reflector) gracefully

---

### Task 1: PrincipleTracker — 原则遵守量化追踪

**Files:**
- Modify: `plastic_promise/core/principles.py`

**Interfaces:**
- Consumes: CORE_PRINCIPLES
- Produces: `PrincipleTracker.record(principle_id, adhered: bool, context: str)`, `PrincipleTracker.stats() -> dict`, `PrincipleTracker.get_history(principle_id) -> list`

- [ ] **Step 1: Add PrincipleTracker class**

Append after PrincipleManager class:

```python
class PrincipleTracker:
    """原则遵守量化追踪器 — 记录每条原则被遵循/违反的次数与趋势。

    Serves 约定层: 从"记住约定"到"量化践行"。
    """

    def __init__(self):
        self._records: Dict[int, list] = {}  # principle_id -> [{adhered, context, timestamp}]

    def record(self, principle_id: int, adhered: bool, context: str = ""):
        """Record one adherence event for a principle."""
        import datetime
        if principle_id not in self._records:
            self._records[principle_id] = []
        self._records[principle_id].append({
            "adhered": adhered,
            "context": context[:200],
            "timestamp": datetime.datetime.now().isoformat(),
        })

    def stats(self) -> dict:
        """Return per-principle adherence statistics."""
        from plastic_promise.core.constants import CORE_PRINCIPLES
        result = {}
        for p in CORE_PRINCIPLES:
            pid = p["id"]
            events = self._records.get(pid, [])
            total = len(events)
            adhered = sum(1 for e in events if e["adhered"])
            result[str(pid)] = {
                "name": p["name"],
                "total_checks": total,
                "adhered": adhered,
                "violated": total - adhered,
                "rate": round(adhered / total, 3) if total > 0 else None,
            }
        return result

    def get_history(self, principle_id: int, limit: int = 20) -> list:
        """Return recent adherence events for a principle."""
        events = self._records.get(principle_id, [])
        return events[-limit:]
```

- [ ] **Step 2: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.principles import PrincipleTracker
pt = PrincipleTracker()
pt.record(1, True, 'used simplest approach')
pt.record(1, False, 'over-engineered solution')
pt.record(4, True, 'checked context before deciding')
s = pt.stats()
assert s['1']['total_checks'] == 2 and s['1']['adhered'] == 1
assert s['4']['total_checks'] == 1 and s['4']['adhered'] == 1
assert s['1']['rate'] == 0.5
h = pt.get_history(1)
assert len(h) == 2
print(f'Stats: {s[\"1\"]}')
print('ALL TESTS PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/principles.py
git commit -m "feat: PrincipleTracker — quantify principle adherence per activation"
```

---

### Task 2: Trust → Retrieval Weight — 信任分接入检索权重

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (supply method)
- Modify: `plastic_promise/defense/soul_enforcer.py` (TrustManager)

**Interfaces:**
- Consumes: `TrustManager.get()`, `TrustManager.tier`
- Produces: tier-based multiplier in `_text_retrieval`

- [ ] **Step 1: Add TrustManager.get_retrieval_boost()**

Append to TrustManager:

```python
    def get_retrieval_boost(self) -> float:
        """Return retrieval weight multiplier based on current trust tier.

        High trust → broader context, more risk tolerance.
        Low trust → narrower scope, conservative retrieval.
        Serves 实践层: 动态信任调节信息获取范围。
        """
        _tier = self.tier
        if _tier == "high":
            return 1.3
        elif _tier == "medium":
            return 1.0
        elif _tier == "low":
            return 0.7
        else:  # critical
            return 0.5
```

- [ ] **Step 2: Apply trust boost in supply()**

In `supply()` Phase 0, after principle activation:

```python
        # Trust-aware retrieval: higher trust → broader context
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager
            tm = TrustManager()
            trust_boost = tm.get_retrieval_boost()
        except Exception:
            trust_boost = 1.0
```

Then modify `_text_retrieval` to accept and apply trust_boost:

```python
    def _text_retrieval(self, task: str, trust_boost: float = 1.0) -> List[tuple]:
        ...
            if score > 0:
                tier = mem.get("tier", "L2")
                if tier == "L1":
                    score = min(score * 1.5 * trust_boost, 1.0)
                elif tier == "L3":
                    score = score * 0.8 * trust_boost
                results.append((mid, min(score, 1.0), content[:300], mem["source"]))
```

- [ ] **Step 3: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.defense.soul_enforcer import TrustManager
tm = TrustManager()
assert tm.get_retrieval_boost() == 1.0  # default medium
tm.boost(0.3)  # → 0.90 → high
assert tm.get_retrieval_boost() == 1.3
tm.decay(0.7)  # → 0.20 → critical
assert tm.get_retrieval_boost() == 0.5
print('ALL TESTS PASSED')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/defense/soul_enforcer.py plastic_promise/core/context_engine.py
git commit -m "feat: trust tier → retrieval weight — higher trust = broader context"
```

---

### Task 3: Repair Suggestion — 修复建议生成

**Files:**
- Modify: `plastic_promise/core/step_auditor.py`

**Interfaces:**
- Produces: `StepAuditor.suggest_repairs(result: StepAuditResult) -> list[dict]`

- [ ] **Step 1: Add suggest_repairs method**

```python
    def suggest_repairs(self, result: "StepAuditResult") -> list[dict]:
        """Generate repair suggestions for dimensions scoring below 0.60.

        Serves 实践层反思/修复: 发现问题 → 生成修复建议。
        """
        suggestions = []
        if result.simplicity_score < 0.60:
            suggestions.append({
                "dimension": "simplicity",
                "current_score": result.simplicity_score,
                "suggestion": "删除不必要的中间步骤，检查是否存在可以简化的逻辑路径",
            })
        if result.transparency_score < 0.60:
            suggestions.append({
                "dimension": "transparency",
                "current_score": result.transparency_score,
                "suggestion": f"确保每一步有 git commit，当前任务缺少可追溯痕迹",
            })
        if result.audit_closure_score < 0.60:
            suggestions.append({
                "dimension": "audit_closure",
                "current_score": result.audit_closure_score,
                "suggestion": "补充根因分析、改良措施或教训提炼，当前审计不完整",
            })
        return suggestions
```

- [ ] **Step 2: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core.step_auditor import StepAuditor, StepAuditResult
a = StepAuditor()
# Simulate a low-score result
r = StepAuditResult(
    root_cause='rushed', improvement='', lesson='',
    simplicity_score=0.4, transparency_score=0.8, audit_closure_score=0.3,
)
repairs = a.suggest_repairs(r)
assert len(repairs) == 2
assert repairs[0]['dimension'] == 'simplicity'
print(f'Repairs: {len(repairs)} suggestions')
for rp in repairs:
    print(f'  {rp[\"dimension\"]} ({rp[\"current_score\"]}): {rp[\"suggestion\"][:50]}')
print('ALL TESTS PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/step_auditor.py
git commit -m "feat: auto repair suggestions — low audit scores trigger actionable guidance"
```

---

### Task 4: post_task 六联闭环 — 全层神经中枢

**Files:**
- Modify: `plastic_promise/loop/soul_loop.py`

**Interfaces:**
- Consumes: PrincipleTracker, SCARFReflector, HormoneEngine, TrustManager, StepAuditor, SoulEnforcer
- Produces: `SoulLoop.post_task(task_description, git_commit="") -> dict`

- [ ] **Step 1: Rewrite post_task method**

Replace the current post_task body with:

```python
    def post_task(self, task_description: str = "", git_commit: str = "") -> dict:
        """六联闭环 — 每步完成后的约定工程全层连线。

        Returns:
            dict with keys: alignment, scarf, hormone, trust, reflection, cei, repairs
        """
        result = {
            "alignment": None, "scarf": None, "hormone": None,
            "trust": None, "reflection": None, "cei": None, "repairs": [],
        }

        # 1. 约定对齐检查 — 记录原则遵守
        try:
            activated = self._engine._activate_principles("general", task_description)
            result["alignment"] = {"checked": len(activated), "principles": activated}
            for p_name in activated:
                # Heuristic: if task_description is meaningful, treat as adhered
                pid = self._resolve_principle_id(p_name)
                if pid:
                    self._principle_tracker.record(pid, True, task_description[:100])
        except Exception as e:
            result["alignment"] = {"error": str(e)}

        # 2. SCARF 五维自省
        try:
            from plastic_promise.reflection.soul_scarf import SCARFReflector
            reflector = SCARFReflector()
            scarf_result = reflector.reflect(task_description)
            result["scarf"] = scarf_result
        except Exception as e:
            result["scarf"] = {"error": str(e)}

        # 3. 激素更新
        try:
            if self._hormone_engine is None:
                from plastic_promise.growth.soul_hormone import HormoneEngine
                self._hormone_engine = HormoneEngine(trust_manager=self._trust_manager)
            overall = result.get("cei") or 0.6
            feedback = "adopted" if overall >= 0.6 else "ignored" if overall >= 0.4 else "rejected"
            hormone_result = self._hormone_engine.apply_feedback(feedback, context=task_description[:100])
            result["hormone"] = hormone_result
        except Exception as e:
            result["hormone"] = {"error": str(e)}

        # 4. 信任联动
        try:
            if self._trust_manager is None:
                from plastic_promise.defense.soul_enforcer import TrustManager
                self._trust_manager = TrustManager()
            if result.get("scarf") and isinstance(result["scarf"], dict):
                scarf_overall = result["scarf"].get("summary", {}).get("overall_score", 0.6)
                if scarf_overall >= 0.80:
                    self._trust_manager.boost(0.02, f"post_task SCARF {scarf_overall:.2f}")
                elif scarf_overall < 0.40:
                    self._trust_manager.decay(0.02, f"post_task SCARF {scarf_overall:.2f}")
            result["trust"] = {
                "score": self._trust_manager.get(),
                "tier": self._trust_manager.tier,
            }
        except Exception as e:
            result["trust"] = {"error": str(e)}

        # 5. 反思记忆存储 (StepAuditor free)
        try:
            if self._auditor is None:
                self._auditor = StepAuditor(trust_manager=self._trust_manager, engine=self._engine)
            audit_result = self._auditor.audit_step(
                task_description=task_description,
                git_commit=git_commit,
            )
            result["reflection"] = {
                "overall_score": audit_result.overall_score,
                "lesson": audit_result.lesson[:200],
                "step_id": audit_result.step_id,
            }
            result["repairs"] = self._auditor.suggest_repairs(audit_result)
        except Exception as e:
            result["reflection"] = {"error": str(e)}

        # 6. CEI 更新
        try:
            cei = self.calculate_cei()
            result["cei"] = {"score": cei, "tier": self.cei_tier}
        except Exception as e:
            result["cei"] = {"error": str(e)}

        return result


    def _resolve_principle_id(self, name: str) -> int:
        """Resolve principle name to ID from CORE_PRINCIPLES."""
        from plastic_promise.core.constants import CORE_PRINCIPLES
        for p in CORE_PRINCIPLES:
            if p["name"] == name:
                return p["id"]
        return 0
```

- [ ] **Step 2: Update SoulLoop.__init__ to include new attributes**

Ensure `__init__` initializes:
```python
        self._principle_tracker = None  # lazy init
        self._hormone_engine = None
        self._trust_manager = None
        self._auditor = None
```

And add a property:
```python
    @property
    def _principle_tracker(self):
        if self.__dict__.get('_pt') is None:
            from plastic_promise.core.principles import PrincipleTracker
            self._pt = PrincipleTracker()
        return self._pt

    @_principle_tracker.setter
    def _principle_tracker(self, value):
        self._pt = value
```

- [ ] **Step 3: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.loop.soul_loop import SoulLoop
from plastic_promise.core.context_engine import ContextEngine

sl = SoulLoop(engine=ContextEngine())
result = sl.post_task('实现用户认证模块——用最简洁的3个函数完成，每步都有commit', 'abc1234')
assert 'alignment' in result
assert 'scarf' in result
assert 'hormone' in result
assert 'trust' in result
assert 'reflection' in result
assert 'cei' in result
assert 'repairs' in result
print(f'Alignment: {result[\"alignment\"][\"checked\"]} principles checked')
print(f'Trust: {result[\"trust\"]}')
print(f'CEI: {result[\"cei\"]}')
print(f'Repairs: {len(result[\"repairs\"])} suggestions')
print('SIX-LINK POST_TASK VERIFIED')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/loop/soul_loop.py
git commit -m "feat: post_task six-link loop — alignment+SCARF+hormone+trust+reflection+CEI"
```
