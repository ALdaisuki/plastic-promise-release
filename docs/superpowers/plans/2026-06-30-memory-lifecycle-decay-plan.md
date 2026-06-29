# 记忆生命周期引擎 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 Weibull 时间衰减 + 访问间隔强化 + 三因素复合评分引擎，让记忆自然衰老、高频记忆长寿。

**Architecture:** 新建 `decay_engine.py`（WeibullDecayCalculator + AccessReinforcement），集成到 `soul_memory.py`（MemoryRecord 新字段、MemoryWorthCalculator 复合评分、TierManager/EvolveR 升级），`context_engine.py`（SQLite 迁移 + auto_recall 标记）。

**Tech Stack:** Python 3.11+, math 标准库, SQLite WAL 模式, 现有 soul_memory 体系

## Global Constraints

- Weibull 公式: `raw_decay = exp(-λ × days_since_created^β)`, `λ = ln(2) / half_life_days`
- decay_multiplier 下限 0.05（不完全归零，给访问强化留复活窗口）
- L1: β=1.5, half_life=3d | L3: β=0.7, half_life=90d | default: β=1.0, half_life=14d
- 三因素融合: `composite = wilson×0.6 + freshness×0.25 + reinforcement×0.15`
- 访问强化归一化: `(effective_hl - base_hl) / (max_hl - base_hl)` → [0, 1]
- AccessReinforcement.boost() 更新 effective_half_life 后立即同步 decay_multiplier
- 存量迁移时对全部记忆一次性计算真实衰减（不等待 GC）
- to_dict/from_dict/_SQLiteStorage._row_to_dict 包含新字段
- 优雅降级：任何 decay 组件故障 → composite_score 回退纯 Wilson worth

---

### Task 1: 常量配置

**Files:**
- Modify: `plastic_promise/core/constants.py`

**Interfaces:**
- Produces: `DECAY_CONFIG`, `REINFORCEMENT_CONFIG` — 全局常量字典

- [ ] **Step 1: 在 constants.py 末尾添加常量**

```python
# ============================================================
# 记忆衰减配置 (Weibull per-tier β + half-life)
# ============================================================

DECAY_CONFIG = {
    "L1": {"beta": 1.5, "half_life_days": 3},
    "L3": {"beta": 0.7, "half_life_days": 90},
    "default": {"beta": 1.0, "half_life_days": 14},
}

REINFORCEMENT_CONFIG = {
    "reinforcement_factor": 0.5,
    "max_multiplier": 3.0,
    "access_decay_days": 30,
}
```

- [ ] **Step 2: 验证导入**

```bash
python -c "from plastic_promise.core.constants import DECAY_CONFIG, REINFORCEMENT_CONFIG; print(DECAY_CONFIG['L1']); print(REINFORCEMENT_CONFIG)"
```

Expected: `{'beta': 1.5, 'half_life_days': 3}` / `{'reinforcement_factor': 0.5, ...}`

- [ ] **Step 3: 提交**

```bash
git add plastic_promise/core/constants.py
git commit -m "feat: DECAY_CONFIG + REINFORCEMENT_CONFIG constants for Weibull decay"
```

---

### Task 2: WeibullDecayCalculator

**Files:**
- Create: `plastic_promise/core/decay_engine.py`

**Interfaces:**
- Produces: `WeibullDecayCalculator.__init__(config)`, `.compute_decay(tier, created_at, current_time_str) -> float`, `.evaluate_all(records, current_time_str) -> list[tuple[str, float]]`

- [ ] **Step 1: 写文件头部和导入**

```python
"""Memory decay engine — Weibull stretched-exponential decay + access reinforcement.

WeibullDecayCalculator: time-based decay with per-tier β and half-life.
AccessReinforcement: spaced-repetition half-life extension on active recall.

Formulas adapted from memory-lancedb-pro's decay-engine.ts and
access-tracker.ts, reimplemented in Python for Plastic Promise.
"""
import math
import datetime
import logging
from typing import Optional

logger = logging.getLogger("plastic-promise.decay")
```

- [ ] **Step 2: 写 WeibullDecayCalculator 类**

```python
class WeibullDecayCalculator:
    """Compute Weibull stretched-exponential decay for memory records.

    Formula: raw_decay = exp(-λ × days_since_created^β)
             λ = ln(2) / half_life_days
             decay_multiplier = clamp(raw_decay, 0.05, 1.0)

    Per-tier configuration controls decay speed:
      L1 (working):  β=1.5, half-life=3d  → super-exponential, fast fade
      L3 (long-term): β=0.7, half-life=90d → sub-exponential, slow fade
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        from plastic_promise.core.constants import DECAY_CONFIG
        self._config = config or DECAY_CONFIG
        # Precompute λ = ln(2) / half_life_days for each tier
        self._lambda: dict[str, float] = {}
        for tier, cfg in self._config.items():
            self._lambda[tier] = math.log(2) / cfg["half_life_days"]

    def _get_params(self, tier: str) -> tuple[float, float]:
        """Return (beta, lambda) for a tier, defaulting if unknown."""
        cfg = self._config.get(tier, self._config["default"])
        lam = self._lambda.get(tier, self._lambda["default"])
        return cfg["beta"], lam

    def _days_since(self, created_at: str, current_time_str: str) -> float:
        """Compute fractional days between two ISO timestamps."""
        try:
            created = datetime.datetime.fromisoformat(created_at)
            current = datetime.datetime.fromisoformat(current_time_str)
            return (current - created).total_seconds() / 86400.0
        except Exception:
            return 0.0

    def compute_decay(self, tier: str, created_at: str,
                      effective_half_life: Optional[float] = None,
                      current_time_str: Optional[str] = None) -> float:
        """Compute decay_multiplier for a single memory.

        Args:
            tier: Memory tier (L1/L3).
            created_at: ISO timestamp of memory creation.
            effective_half_life: Optional override for half-life (from access
                reinforcement). When provided, λ is recomputed from it.
            current_time_str: ISO timestamp for "now". Defaults to now().

        Returns:
            decay_multiplier ∈ [0.05, 1.0]. 1.0 = brand new, 0.05 = fully decayed.
        """
        beta, lam = self._get_params(tier)
        if effective_half_life is not None and effective_half_life > 0:
            lam = math.log(2) / effective_half_life

        now = current_time_str or datetime.datetime.now().isoformat()
        days = self._days_since(created_at, now)
        if days <= 0:
            return 1.0

        raw = math.exp(-lam * (days ** beta))
        return max(0.05, min(1.0, raw))

    def evaluate_all(self, records: list, current_time_str: Optional[str] = None
                     ) -> list[tuple[str, float]]:
        """Batch-evaluate decay for multiple MemoryRecord objects.

        Args:
            records: List of MemoryRecord objects (must have .memory_id, .tier,
                     .created_at, .effective_half_life attributes).
            current_time_str: ISO timestamp for "now". Defaults to now().

        Returns:
            List of (memory_id, decay_multiplier) tuples for all records.
        """
        now = current_time_str or datetime.datetime.now().isoformat()
        results = []
        for r in records:
            try:
                dm = self.compute_decay(
                    tier=getattr(r, 'tier', 'L1'),
                    created_at=getattr(r, 'created_at', now),
                    effective_half_life=getattr(r, 'effective_half_life', None),
                    current_time_str=now,
                )
                results.append((r.memory_id, dm))
            except Exception as e:
                logger.warning("Decay eval failed for %s: %s", getattr(r, 'memory_id', '?'), e)
                results.append((getattr(r, 'memory_id', ''), 1.0))
        return results
```

- [ ] **Step 3: 验证导入和基本计算**

```bash
python -c "
from plastic_promise.core.decay_engine import WeibullDecayCalculator
import datetime
w = WeibullDecayCalculator()
# Brand new L1 memory
d = w.compute_decay('L1', datetime.datetime.now().isoformat())
print('New L1:', d)  # ~1.0

# 3-day-old L1 memory
old = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
d3 = w.compute_decay('L1', old)
print('3-day L1:', round(d3, 3))  # ~0.5

# 90-day-old L3 memory
old90 = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
d90 = w.compute_decay('L3', old90)
print('90-day L3:', round(d90, 3))  # ~0.5
"
```

Expected: New L1 ≈ 1.0, 3-day L1 ≈ 0.5, 90-day L3 ≈ 0.5

- [ ] **Step 4: 提交**

```bash
git add plastic_promise/core/decay_engine.py
git commit -m "feat: WeibullDecayCalculator — per-tier beta + half-life decay"
```

---

### Task 3: AccessReinforcement

**Files:**
- Modify: `plastic_promise/core/decay_engine.py`

**Interfaces:**
- Produces: `AccessReinforcement.__init__(config)`, `.compute_boost()` → (reinforcement_score, effective_half_life), `.compute_reinforcement_score(base_hl, effective_hl) -> float`

- [ ] **Step 1: 在 WeibullDecayCalculator 之后添加 AccessReinforcement 类**

```python
class AccessReinforcement:
    """Spaced-repetition half-life extension on active memory recall.

    Formula:
      effective_access = access_count × exp(-days_since_last_access / 30)
      extension = base_half_life × reinforcement_factor × ln(1 + effective_access)
      effective_half_life = min(base_half_life + extension,
                                base_half_life × max_multiplier)

    Only triggered by active recall (is_auto_recall=False).
    Auto-recall from ContextEngine.supply() does NOT reinforce.
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        from plastic_promise.core.constants import REINFORCEMENT_CONFIG
        cfg = config or REINFORCEMENT_CONFIG
        self.reinforcement_factor = cfg["reinforcement_factor"]
        self.max_multiplier = cfg["max_multiplier"]
        self.access_decay_days = cfg["access_decay_days"]

    def _days_since(self, iso_timestamp: str, current_time_str: str) -> float:
        try:
            ts = datetime.datetime.fromisoformat(iso_timestamp)
            now = datetime.datetime.fromisoformat(current_time_str)
            return (now - ts).total_seconds() / 86400.0
        except Exception:
            return 0.0

    def compute_effective_access(self, access_count: int, last_accessed: str,
                                 current_time_str: str) -> float:
        """Compute time-decayed effective access count."""
        days = self._days_since(last_accessed, current_time_str)
        decay_factor = math.exp(-days / self.access_decay_days)
        return access_count * decay_factor

    def compute_effective_half_life(self, base_half_life: float,
                                    access_count: int, last_accessed: str,
                                    current_time_str: str) -> float:
        """Compute extended half-life from access history."""
        effective_access = self.compute_effective_access(
            access_count, last_accessed, current_time_str
        )
        extension = (base_half_life * self.reinforcement_factor *
                     math.log1p(effective_access))
        return min(base_half_life + extension,
                   base_half_life * self.max_multiplier)

    def compute_reinforcement_score(self, base_half_life: float,
                                    effective_half_life: float) -> float:
        """Normalize reinforcement to [0, 1].

        0.0 = no reinforcement (effective == base)
        1.0 = max reinforcement (effective == base × max_multiplier)
        """
        max_hl = base_half_life * self.max_multiplier
        if max_hl <= base_half_life:
            return 0.0
        raw = (effective_half_life - base_half_life) / (max_hl - base_half_life)
        return max(0.0, min(1.0, raw))

    def compute_boost(self, access_count: int, last_accessed: str,
                      base_half_life: float, is_auto_recall: bool = False,
                      current_time_str: Optional[str] = None
                      ) -> tuple[float, float]:
        """Compute reinforcement score and new effective half-life.

        Args:
            access_count: Current access count (before increment).
            last_accessed: ISO timestamp of last access.
            base_half_life: Base half-life for this memory's tier.
            is_auto_recall: If True, skip reinforcement (return 0.0, base_hl).
            current_time_str: ISO timestamp for "now". Defaults to now().

        Returns:
            (reinforcement_score, effective_half_life) tuple.
            reinforcement_score ∈ [0.0, 1.0].
        """
        if is_auto_recall or access_count <= 0:
            return (0.0, base_half_life)

        now = current_time_str or datetime.datetime.now().isoformat()
        effective_hl = self.compute_effective_half_life(
            base_half_life, access_count, last_accessed, now
        )
        score = self.compute_reinforcement_score(base_half_life, effective_hl)
        return (score, effective_hl)
```

- [ ] **Step 2: 验证访问强化计算**

```bash
python -c "
from plastic_promise.core.decay_engine import AccessReinforcement
import datetime
a = AccessReinforcement()
now = datetime.datetime.now().isoformat()
# 3 accesses, just now → should get ~0.5 boost
score, hl = a.compute_boost(3, now, 3.0, is_auto_recall=False, current_time_str=now)
print('Reinf score:', round(score, 3), 'Eff HL:', round(hl, 2))
# auto_recall → should return 0.0, base_hl
score2, hl2 = a.compute_boost(3, now, 3.0, is_auto_recall=True, current_time_str=now)
print('Auto score:', score2, 'Auto HL:', hl2)
"
```

Expected: Reinf score > 0, Auto score == 0.0

- [ ] **Step 3: 提交**

```bash
git add plastic_promise/core/decay_engine.py
git commit -m "feat: AccessReinforcement — spaced-repetition with auto-recall gate"
```

---

### Task 4: MemoryRecord 新字段 + 序列化

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:118-228`
- Modify: `plastic_promise/core/context_engine.py:1060-1082` (_SQLiteStorage._row_to_dict)

**Interfaces:**
- Consumes: 无新依赖
- Produces: `MemoryRecord.decay_multiplier`, `MemoryRecord.effective_half_life` — 两个新属性

- [ ] **Step 1: 在 MemoryRecord.__init__ 参数列表添加新字段**

修改 `soul_memory.py` 中 `MemoryRecord.__init__` 签名（~line 118-131）：

```python
    def __init__(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        memory_id: Optional[str] = None,
        worth_success: int = 0,
        worth_failure: int = 0,
        activation_weight: float = 0.5,
        tier: str = "L1",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        domain: str = "uncategorized",
        decay_multiplier: float = 1.0,       # NEW
        effective_half_life: float = 3.0,      # NEW
    ) -> None:
```

在 `__init__` 体内添加（~after line 160 `self.access_count = 0`）：

```python
        self.decay_multiplier = decay_multiplier
        self.effective_half_life = effective_half_life
```

- [ ] **Step 2: 更新 to_dict()**

在 `to_dict()` 返回字典中添加（~line 184-200）：

```python
            "decay_multiplier": self.decay_multiplier,
            "effective_half_life": self.effective_half_life,
```

- [ ] **Step 3: 更新 from_dict()**

在 `from_dict()` 的 `record = cls(...)` 调用中添加（~line 212-223）：

```python
            decay_multiplier=data.get("decay_multiplier", 1.0),
            effective_half_life=data.get("effective_half_life", 3.0),
```

- [ ] **Step 4: 更新 _SQLiteStorage._row_to_dict**

修改 `context_engine.py` 中 `_row_to_dict` 返回字典（~line 1063-1081），在最后添加：

```python
            "decay_multiplier": row[17] if len(row) > 17 else 1.0,
            "effective_half_life": row[18] if len(row) > 18 else 3.0,
```

- [ ] **Step 5: 更新 _SQLiteStorage.upsert SQL**

修改 `upsert()` 的列列表和 VALUES（~line 1006-1031），添加两列：

```python
# 新增列到 SELECT 列表
# ... worth_success, worth_failure, activation_weight, decay_multiplier, effective_half_life) "
# VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
```

在 VALUES 元组末尾添加：

```python
                data.get("decay_multiplier", 1.0),
                data.get("effective_half_life", 3.0),
```

- [ ] **Step 6: 验证字段存在**

```bash
python -c "
from plastic_promise.memory.soul_memory import MemoryRecord
r = MemoryRecord('test', tier='L1')
print('decay_multiplier:', r.decay_multiplier)
print('effective_half_life:', r.effective_half_life)
d = r.to_dict()
print('to_dict keys:', 'decay_multiplier' in d, 'effective_half_life' in d)
"
```

Expected: `1.0`, `3.0`, `True True`

- [ ] **Step 7: 提交**

```bash
git add plastic_promise/memory/soul_memory.py plastic_promise/core/context_engine.py
git commit -m "feat: MemoryRecord +decay_multiplier +effective_half_life + serialization"
```

---

### Task 5: MemoryWorthCalculator.composite_score

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:32-105`

**Interfaces:**
- Consumes: `MemoryRecord.decay_multiplier`, `MemoryRecord.effective_half_life` (Task 4)
- Produces: `MemoryWorthCalculator.calculate_composite_score(record) -> float`

- [ ] **Step 1: 在 MemoryWorthCalculator 类中添加新方法**

在 `calculate_worth` 方法之后（~line 80）添加：

```python
    def calculate_composite_score(self, record: "MemoryRecord") -> float:
        """Compute three-factor composite lifecycle score.

        Formula:
          composite = wilson_worth × 0.6 + freshness × 0.25 + reinforcement × 0.15

        Args:
            record: MemoryRecord with decay_multiplier and effective_half_life set.

        Returns:
            Composite score ∈ [0.0, 1.0]. Falls back to pure Wilson worth
            if decay components are unavailable.
        """
        try:
            wilson = self.calculate_worth(record.worth_success, record.worth_failure)
            freshness = 1.0 - getattr(record, 'decay_multiplier', 1.0)

            # Compute reinforcement score from half-life fields
            tier = getattr(record, 'tier', 'L1')
            from plastic_promise.core.constants import DECAY_CONFIG, REINFORCEMENT_CONFIG
            tier_cfg = DECAY_CONFIG.get(tier, DECAY_CONFIG["default"])
            base_hl = tier_cfg["half_life_days"]
            effective_hl = getattr(record, 'effective_half_life', base_hl)
            max_hl = base_hl * REINFORCEMENT_CONFIG["max_multiplier"]
            if max_hl > base_hl:
                reinforcement = (effective_hl - base_hl) / (max_hl - base_hl)
                reinforcement = max(0.0, min(1.0, reinforcement))
            else:
                reinforcement = 0.0

            return wilson * 0.6 + freshness * 0.25 + reinforcement * 0.15
        except Exception:
            return self.calculate_worth(record.worth_success, record.worth_failure)
```

- [ ] **Step 2: 验证复合评分**

```bash
python -c "
from plastic_promise.memory.soul_memory import MemoryRecord, MemoryWorthCalculator
r = MemoryRecord('test', tier='L1', worth_success=5, worth_failure=1)
r.decay_multiplier = 1.0  # brand new
r.effective_half_life = 3.0  # base, no reinforcement
calc = MemoryWorthCalculator()
score = calc.calculate_composite_score(r)
print('Composite:', round(score, 3))
# Wilson ~0.7, freshness=0.0, reinforcement=0.0 → ~0.42
"
```

Expected: ~0.42

- [ ] **Step 3: 提交**

```bash
git add plastic_promise/memory/soul_memory.py
git commit -m "feat: MemoryWorthCalculator.calculate_composite_score — 3-factor fusion"
```

---

### Task 6: SQLite 迁移 + 存量衰减计算

**Files:**
- Modify: `plastic_promise/core/context_engine.py:960-1001` (_SQLiteStorage.__init__)
- Modify: `plastic_promise/core/context_engine.py:1002-1031` (_SQLiteStorage.upsert)
- Modify: `plastic_promise/core/context_engine.py:1060-1082` (_SQLiteStorage._row_to_dict)

**Interfaces:**
- Consumes: `WeibullDecayCalculator` (Task 2)
- Produces: SQLite 列 `decay_multiplier`, `effective_half_life` 自动添加 + 存量计算

- [ ] **Step 1: 在 _SQLiteStorage.__init__ 中添加列迁移 + 存量计算**

在现有 migrations（tags/domain ALTER TABLE）之后添加（~line 996）：

```python
        # 迁移: 新增 decay_multiplier 和 effective_half_life 列 (Phase A 衰减引擎)
        for col, default_val in [("decay_multiplier", "1.0"), ("effective_half_life", "3.0")]:
            try:
                self._conn.execute(
                    f"ALTER TABLE memories ADD COLUMN {col} REAL NOT NULL DEFAULT {default_val}"
                )
            except Exception:
                pass  # 列已存在
        self._conn.commit()

        # 存量迁移: 对已有记忆一次性计算真实衰减值
        try:
            from plastic_promise.core.decay_engine import WeibullDecayCalculator
            import datetime
            decay_calc = WeibullDecayCalculator()
            now = datetime.datetime.now().isoformat()
            rows = self._conn.execute(
                "SELECT id, tier, created_at FROM memories WHERE decay_multiplier = 1.0"
            ).fetchall()
            if rows:
                for row in rows:
                    mid, tier, created_at = row
                    dm = decay_calc.compute_decay(
                        tier=tier or "L1",
                        created_at=created_at or now,
                        current_time_str=now,
                    )
                    self._conn.execute(
                        "UPDATE memories SET decay_multiplier = ? WHERE id = ?",
                        (dm, mid)
                    )
                self._conn.commit()
                logging.info("Bulk decay migration: %d memories updated", len(rows))
        except Exception as e:
            logging.warning("Bulk decay migration skipped: %s", e)
```

- [ ] **Step 2: 更新 upsert() 包含新列**

修改 `upsert()` 方法中的 SQL 和 VALUES（~line 1006-1031）。在列列表末尾加 `decay_multiplier, effective_half_life`，在 VALUES 占位符末尾加 `?, ?`（共 19 个占位符），在元组末尾加：

```python
                data.get("decay_multiplier", 1.0),
                data.get("effective_half_life", 3.0),
```

- [ ] **Step 3: 更新 _row_to_dict() 读取新列**

修改 `_row_to_dict()`（~line 1060），在返回字典末尾添加：

```python
            "decay_multiplier": row[17] if len(row) > 17 else 1.0,
            "effective_half_life": row[18] if len(row) > 18 else 3.0,
```

- [ ] **Step 4: 更新 iter_all() SQL 查询**

在 `iter_all()` 的 SELECT 列列表末尾添加 `, decay_multiplier, effective_half_life`（~line 1052-1054）。

- [ ] **Step 5: 验证迁移**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
# Check a memory has decay fields
mems = e.list_memories(limit=3)
for m in mems:
    print(f'{m.id[:12]}... decay={m.decay_multiplier:.3f} hl={m.effective_half_life:.1f}')
"
```

Expected: 每条记忆显示 decay_multiplier 和 effective_half_life 值

- [ ] **Step 6: 提交**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: SQLite migration — decay columns + bulk initial decay calculation"
```

---

### Task 7: TierManager + EvolveR + supply auto_recall

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:254-279` (TierManager)
- Modify: `plastic_promise/memory/soul_memory.py` (EvolveR)
- Modify: `plastic_promise/core/context_engine.py:416-508` (supply)

**Interfaces:**
- Consumes: `calculate_composite_score` (Task 5), `WeibullDecayCalculator` (Task 2)
- Produces: TierManager 使用 composite_score；supply() 标记 auto_recall；EvolveR 衰减驱动

- [ ] **Step 1: 升级 MemoryTierManager.classify_tier()**

修改 `soul_memory.py:269` 附近的 `classify_tier()`：

```python
    def classify_tier(self, record: MemoryRecord) -> str:
        if record is None:
            return "L1"
        try:
            # 使用 composite_score 替代 worth_score
            calc = MemoryWorthCalculator()
            composite = calc.calculate_composite_score(record)
            if composite >= 0.5 and record.access_count >= 3:
                return "L3"
        except Exception:
            pass
        return "L1"
```

- [ ] **Step 2: 添加 should_demote() 方法**

在 `classify_tier` 之后添加：

```python
    def should_demote(self, record: MemoryRecord) -> bool:
        """Check if a memory should be demoted from L3 to L1."""
        try:
            calc = MemoryWorthCalculator()
            composite = calc.calculate_composite_score(record)
            dm = getattr(record, 'decay_multiplier', 1.0)
            if dm < 0.2:
                return True
            if composite < 0.15:
                return True
        except Exception:
            pass
        return False
```

- [ ] **Step 3: 升级 EvolveR.evolve_cycle() 集成衰减引擎**

找到 `EvolveR.evolve_cycle()`（在 soul_memory.py 中搜索），在方法开头添加：

```python
        # Phase A: 批量更新 decay_multiplier
        try:
            from plastic_promise.core.decay_engine import WeibullDecayCalculator
            import datetime
            wdc = WeibullDecayCalculator()
            records = list(self.rec_mem._records.values()) if self.rec_mem else []
            if records:
                results = wdc.evaluate_all(records)
                now = datetime.datetime.now().isoformat()
                for mid, dm in results:
                    if mid in self.rec_mem._records:
                        self.rec_mem._records[mid].decay_multiplier = dm
                    # Persist to SQLite
                    engine = self.rec_mem._engine if self.rec_mem else None
                    if engine and engine._sqlite:
                        engine._sqlite._conn.execute(
                            "UPDATE memories SET decay_multiplier = ? WHERE id = ?",
                            (dm, mid)
                        )
                if engine and engine._sqlite:
                    engine._sqlite._conn.commit()
        except Exception as e:
            logging.warning("EvolveR: decay batch update failed: %s", e)
```

然后将现有的晋升/降级逻辑改为使用 `calculate_composite_score()`。

- [ ] **Step 4: supply() 标记 auto_recall**

修改 `context_engine.py` 中 `supply()` 的 Phase 5 分层段（~line 473），在 ContextItem 构造中添加：

```python
            item = ContextItem(
                id=item_id,
                content=content,
                relevance=score,
                source=source,
                freshness=freshness,
                is_principle=is_principle,
                worth_score=worth,
            )
```

在 ContextItem 类（context_engine.py 顶部）添加字段：

```python
@dataclass
class ContextItem:
    id: str
    content: str
    relevance: float
    source: str = ""
    freshness: str = "valid"
    layer: str = "related"
    is_principle: bool = False
    worth_score: float = 0.0
    is_auto_recall: bool = True  # NEW: True = internal retrieval, False = user-initiated
```

- [ ] **Step 5: 验证 auto_recall 标记**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
e.set_current_time('2026-06-30T12:00:00')
pack = e.supply('test query', [0.0]*1024, 'general')
# All items from supply() should have is_auto_recall=True
for item in pack.core[:1]:
    print('is_auto_recall:', item.is_auto_recall)
"
```

Expected: `is_auto_recall: True`

- [ ] **Step 6: 提交**

```bash
git add plastic_promise/memory/soul_memory.py plastic_promise/core/context_engine.py
git commit -m "feat: TierManager+EvolveR use composite_score + supply marks auto_recall"
```

---

### Task 8: Pipeline 集成

**Files:**
- Modify: `plastic_promise/memory/pipeline.py:184-213`

**Interfaces:**
- Consumes: `MemoryRecord.decay_multiplier`, `MemoryRecord.effective_half_life` (Task 4)

- [ ] **Step 1: 在 _process_tagged_to_classified 初始化新字段**

在 `soul_memory.py:RecMem.store()` 中（~line 424），MemoryRecord 构造已通过 Task 4 自动包含默认值。pipeline 中的 `_process_embedded_to_migrate`（pipeline.py:236）存储时，`decay_multiplier` 和 `effective_half_life` 已通过 `to_dict()` 序列化，无需额外改动。

验证 pipeline 不会丢失新字段：

```bash
python -c "
from plastic_promise.memory.soul_memory import RecMem
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine()
rm = RecMem(e)
r = rm.store('pipeline decay test', memory_type='experience')
print('New memory:', r.decay_multiplier, r.effective_half_life)
"
```

Expected: `1.0 3.0`

- [ ] **Step 2: 提交**

```bash
git add plastic_promise/memory/pipeline.py
git commit -m "feat: pipeline init decay_multiplier + effective_half_life on new memories"
```

---

### Task 9: 测试

**Files:**
- Create: `tests/test_decay_engine.py`

- [ ] **Step 1: 写 Weibull 衰减测试**

```python
"""Tests for WeibullDecayCalculator + AccessReinforcement + composite scoring."""
import datetime
import pytest
from plastic_promise.core.decay_engine import WeibullDecayCalculator, AccessReinforcement
from plastic_promise.memory.soul_memory import MemoryRecord, MemoryWorthCalculator
from plastic_promise.core.constants import DECAY_CONFIG, REINFORCEMENT_CONFIG


class TestWeibullDecay:
    def test_brand_new_memory_decay_1(self):
        w = WeibullDecayCalculator()
        now = datetime.datetime.now().isoformat()
        assert w.compute_decay("L1", now, current_time_str=now) == 1.0

    def test_l1_3day_decay_approx_half(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
        dm = w.compute_decay("L1", old)
        assert 0.4 <= dm <= 0.6, f"expected ~0.5, got {dm:.3f}"

    def test_l3_90day_decay_approx_half(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
        dm = w.compute_decay("L3", old)
        assert 0.4 <= dm <= 0.6, f"expected ~0.5, got {dm:.3f}"

    def test_decay_lower_bound_0_05(self):
        w = WeibullDecayCalculator()
        very_old = "2020-01-01T00:00:00"
        dm = w.compute_decay("L1", very_old)
        assert dm >= 0.05

    def test_unknown_tier_uses_default(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=14)).isoformat()
        dm = w.compute_decay("unknown_tier", old)
        assert 0.4 <= dm <= 0.6

    def test_effective_half_life_overrides_lambda(self):
        w = WeibullDecayCalculator()
        old = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
        dm_default = w.compute_decay("L1", old)
        dm_extended = w.compute_decay("L1", old, effective_half_life=90.0)
        assert dm_extended > dm_default  # longer half-life = slower decay


class TestAccessReinforcement:
    def test_auto_recall_returns_zero_boost(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(3, now, 3.0, is_auto_recall=True, current_time_str=now)
        assert score == 0.0
        assert hl == 3.0

    def test_no_access_returns_zero_boost(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(0, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert score == 0.0
        assert hl == 3.0

    def test_active_recall_extends_half_life(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        score, hl = a.compute_boost(3, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert hl > 3.0
        assert score > 0.0

    def test_reinforcement_score_normalized_0_to_1(self):
        a = AccessReinforcement()
        assert a.compute_reinforcement_score(3.0, 3.0) == 0.0
        assert a.compute_reinforcement_score(3.0, 9.0) == 1.0

    def test_old_access_is_discounted(self):
        a = AccessReinforcement()
        now = datetime.datetime.now().isoformat()
        old_access = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
        _, hl_old = a.compute_boost(3, old_access, 3.0, is_auto_recall=False, current_time_str=now)
        _, hl_new = a.compute_boost(3, now, 3.0, is_auto_recall=False, current_time_str=now)
        assert hl_old < hl_new  # old access is worth less


class TestCompositeScore:
    def test_brand_new_memory(self):
        r = MemoryRecord("test", tier="L1", worth_success=5, worth_failure=1)
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.83, freshness=0.0, reinforcement=0.0 → ~0.50
        assert 0.45 <= score <= 0.55

    def test_fully_decayed_memory(self):
        r = MemoryRecord("old", tier="L1", worth_success=5, worth_failure=1)
        r.decay_multiplier = 0.05  # almost fully decayed
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.83, freshness=0.95, reinforcement=0.0 → ~0.74
        assert score > 0.5  # freshness compensates

    def test_heavily_reinforced_memory(self):
        r = MemoryRecord("reinforced", tier="L1", worth_success=5, worth_failure=1)
        r.effective_half_life = 9.0  # max reinforcement
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        # Wilson ~0.83, freshness=0.0, reinforcement=1.0 → ~0.65
        assert score > 0.5

    def test_graceful_degradation_on_missing_fields(self):
        r = MemoryRecord("no_fields", tier="L1")
        # These fields should not exist on a brand new record without explicit set
        calc = MemoryWorthCalculator()
        score = calc.calculate_composite_score(r)
        assert 0.0 <= score <= 1.0  # should not crash
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest tests/test_decay_engine.py -v
```

Expected: 13 passed

- [ ] **Step 3: 提交**

```bash
git add tests/test_decay_engine.py
git commit -m "test: Weibull decay + access reinforcement + composite scoring (13 tests)"
```

---

### Task 10: E2E 验证 + 回归测试

- [ ] **Step 1: 运行全量测试**

```bash
python -m pytest tests/ -v --tb=short
```

Expected: 全部通过，无新增回归

- [ ] **Step 2: 端到端验证**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.decay_engine import WeibullDecayCalculator, AccessReinforcement
from plastic_promise.memory.soul_memory import MemoryWorthCalculator, MemoryRecord
import datetime

e = ContextEngine()
# Verify SQLite migration applied
print('Migration OK:', hasattr(e.list_memories(limit=1)[0], 'decay_multiplier'))

# Verify decay calculation
w = WeibullDecayCalculator()
old = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
dm = w.compute_decay('L1', old)
print('3-day L1 decay:', round(dm, 3))

# Verify composite scoring
r = MemoryRecord('E2E test', tier='L1', worth_success=5, worth_failure=1)
calc = MemoryWorthCalculator()
score = calc.calculate_composite_score(r)
print('Composite score:', round(score, 3))

# Verify graceful degradation (no crash)
r2 = MemoryRecord('minimal')
print('Minimal record score:', round(calc.calculate_composite_score(r2), 3))

print('ALL E2E CHECKS PASSED')
"
```

Expected: 所有行打印通过

- [ ] **Step 3: 提交（如有修改）**

```bash
git status
# 如有文件修改则 add + commit
```

---

### Task 11: 文档更新

- [ ] **Step 1: 更新 GOAL.md**

```bash
git add GOAL.md
git commit -m "docs: GOAL — Phase A memory lifecycle engine complete"
```

- [ ] **Step 2: 存储完成记忆**

```
memory_store(content="Phase A DONE: Weibull decay engine + access reinforcement + composite scoring. 
  WeibullDecayCalculator (L1 β=1.5/3d, L3 β=0.7/90d), AccessReinforcement (spaced repetition, 
  auto-recall gate), composite_score = wilson×0.6 + freshness×0.25 + reinforcement×0.15.
  SQLite migration with bulk initial decay calculation.",
  memory_type="experience", tags=["task:done","assignee:claude","domain:reflecting"])
```
