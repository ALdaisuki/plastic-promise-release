# 记忆生命周期引擎 — 方向 A 设计文档

> Date: 2026-06-30
> Status: approved
> Scope: Weibull 时间衰减 + 访问间隔强化 + Wilson worth 三因素融合评分

## 1. 背景

当前 Plastic Promise 的记忆评分仅基于 `MemoryWorthCalculator` 的威尔逊下界公式——纯反馈计数器驱动，无时间维度。所有记忆的 worth_score 不会随时间自然衰减，高频记忆不能长寿，低频记忆不会萎缩。这导致：

- 记忆池无限膨胀，GC 只靠 `access_count == 0 && worth_score < 0.15` 清理
- 检索排序无法区分"新鲜但低价值"和"古老但高频使用"的记忆
- 没有间隔重复机制让常用记忆衰减更慢

参考 memory-lancedb-pro 的 Weibull 衰减引擎和访问强化系统，构建记忆生命周期管理。

## 2. 架构

```
新文件: plastic_promise/core/decay_engine.py
  ├── WeibullDecayCalculator  — 时间衰减 (decay_multiplier ∈ [0,1])
  └── AccessReinforcement     — 访问强化 (effective_half_life 延长)

修改: plastic_promise/memory/soul_memory.py
  ├── MemoryRecord            — 新增 decay_multiplier, effective_half_life 字段
  ├── MemoryWorthCalculator   — 新增 calculate_composite_score()
  ├── MemoryTierManager       — 衰减驱动晋升/降级阈值
  └── EvolveR                 — evolve_cycle() 使用 composite_score

修改: plastic_promise/core/context_engine.py
  └── supply()                — 检索结果标记 is_auto_recall=True

修改: plastic_promise/core/constants.py
  └── 新增 DECAY_CONFIG 常量
```

## 3. WeibullDecayCalculator

### 3.1 公式

```
raw_decay = exp(-λ × days_since_created^β)
decay_multiplier = clamp(raw_decay, 0.05, 1.0)  # 下限 0.05，不完全归零
freshness = 1.0 - decay_multiplier                 # 用于复合评分
```

其中 `λ = ln(2) / half_life_days`。

### 3.2 层参数

| 层 | β | half_life_days | 行为 |
|----|-----|----------------|------|
| **L1（工作记忆）** | 1.5 | 3 | 超指数衰减，24h 后 ~64%，3 天后 50% |
| **L3（长期记忆）** | 0.7 | 90 | 次指数衰减，90 天后 50%，约 1 年归零 |
| **无层位** | 1.0 | 14 | 默认标准衰减 |

存为 `DECAY_CONFIG`：

```python
DECAY_CONFIG = {
    "L1": {"beta": 1.5, "half_life_days": 3},
    "L3": {"beta": 0.7, "half_life_days": 90},
    "default": {"beta": 1.0, "half_life_days": 14},
}
```

### 3.3 接口

```python
class WeibullDecayCalculator:
    def __init__(self, config: dict = DECAY_CONFIG) -> None
    def compute_decay(self, tier: str, created_at: str, current_time: str) -> float
        # → decay_multiplier ∈ [0.05, 1.0]
    def evaluate_all(records: list[MemoryRecord], current_time: str) -> list[tuple[str, float]]
        # → [(memory_id, decay_multiplier), ...] 批量更新，供 GC 使用
```

## 4. AccessReinforcement

### 4.1 公式

```
effective_access = access_count × exp(-days_since_last_access / 30)
extension = base_half_life × reinforcement_factor × ln(1 + effective_access)
effective_half_life = min(base_half_life + extension, base_half_life × max_multiplier)
```

### 4.2 配置

```python
REINFORCEMENT_CONFIG = {
    "reinforcement_factor": 0.5,    # 半衰期延长强度
    "max_multiplier": 3.0,          # 半衰期上限倍数
    "access_decay_days": 30,        # 访问次数的衰减半衰期
}
```

### 4.3 触发规则

- `memory_recall` 主动查询 → 触发 `boost()` → access_count + 1 + 更新 effective_half_life
- `ContextEngine.supply()` 内部检索 → 设置 `is_auto_recall=True` → `boost()` 检查并跳过
- 实现方式：`supply()` 返回结果中的 ContextItem 携带 `is_auto_recall` 标记，调用方在 `memory_recall` handler 中区分主动/自动

### 4.4 接口

```python
class AccessReinforcement:
    def __init__(self, config: dict = REINFORCEMENT_CONFIG) -> None
    def compute_boost(self, access_count: int, last_accessed: str, current_time: str,
                      base_half_life: float, is_auto_recall: bool = False) -> float
        # → reinforcement_score ∈ [0.0, 1.0]，归一化
    def effective_half_life(self, base_half_life: float, access_count: int,
                            last_accessed: str, current_time: str) -> float
        # → 更新后的 effective_half_life
```

## 5. MemoryWorthCalculator 升级

### 5.1 保留向后兼容

```python
class MemoryWorthCalculator:
    def calculate_worth(success, failure) -> float
        # 纯 Wilson 值，不变，向后兼容

    def calculate_composite_score(record, decay_multiplier, reinforcement_score) -> float
        # 三因素融合
```

### 5.2 三因素融合公式（最终版）

```
composite = wilson_worth × 0.6 + freshness × 0.25 + reinforcement × 0.15
# wilson_worth  ∈ [0,1]  反馈质量（Wilson lower bound）
# freshness     ∈ [0,1]  freshness = 1.0 - decay_multiplier
# reinforcement ∈ [0,1]  归一化访问强化分数
# composite     ∈ [0,1]  综合生命周期分数
```

### 5.3 各使用场景

| 场景 | 使用的分数 |
|------|-----------|
| `memory_recall` 排序 | composite_score |
| `MemoryTierManager.classify_tier()` | composite_score ≥ 0.5 → L3 候选 |
| `EvolveR.evolve_cycle()` | composite_score，决定晋升/降级/驱逐 |
| `MemoryGC.collect()` | composite_score < 0.15 && access_count == 0 → 标记衰退 |
| 向后兼容查询 | calculate_worth() 仍可用 |

## 6. MemoryRecord 新增字段

### 6.1 字段

```python
# soul_memory.py MemoryRecord.__init__
decay_multiplier: float = 1.0        # Weibull 衰减因子 [0.05, 1.0]
effective_half_life: float = 3.0     # 当前有效半衰期（天）
```

### 6.2 SQLite 迁移

```sql
ALTER TABLE memories ADD COLUMN decay_multiplier REAL NOT NULL DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN effective_half_life REAL NOT NULL DEFAULT 3.0;
```

在 `_SQLiteStorage.__init__` 中执行迁移，与现有 `tags`/`domain` 列迁移模式一致。

### 6.3 更新策略

| 字段 | 更新频率 | 更新者 |
|------|---------|--------|
| `decay_multiplier` | GC 周期批量更新 | `WeibullDecayCalculator.evaluate_all()` |
| `effective_half_life` | 每次主动 recall 实时更新 | `AccessReinforcement.boost()` |

## 7. MemoryTierManager 升级

### 7.1 当前阈值

```python
# 当前 (soul_memory.py:269)
if record.worth_score >= 0.5 and record.access_count >= 3:
    return "L3"
return "L1"
```

### 7.2 升级后

```python
# 使用 composite_score 替代 worth_score
composite = record.composite_score if hasattr(record, 'composite_score') else record.worth_score
if composite >= 0.5 and record.access_count >= 3:
    return "L3"
return "L1"
```

### 7.3 降级规则增强

```python
def should_demote(record, composite_score, decay_multiplier):
    if decay_multiplier < 0.2:          # 衰减到只剩 20% → 降级
        return True
    if composite_score < 0.15:          # 综合分数极低 → 驱逐候选
        return True
    return False
```

## 8. EvolveR 集成

`EvolveR.evolve_cycle()` 在 GC 周期执行：

```
1. 调用 WeibullDecayCalculator.evaluate_all() → 批量更新 decay_multiplier
2. 对每条记忆计算 composite_score
3. composite_score < 0.15 → 标记衰退（现有 MemoryGC 处理）
4. decay_multiplier < 0.2 → demote_to_l1（长期记忆衰减到临界点）
5. composite_score >= 0.5 && access_count >= 3 → promote_to_l3
```

## 9. 优雅降级

- `WeibullDecayCalculator` 或 `AccessReinforcement` 初始化失败 → `composite_score` 回退为 `calculate_worth()` 纯 Wilson 值
- 存量记忆缺少 `decay_multiplier` 字段 → 默认 1.0（视为全新）
- 存量记忆缺少 `effective_half_life` 字段 → 使用 `DECAY_CONFIG[tier]["half_life_days"]`

## 10. 新增/修改文件清单

| 文件 | 改动 |
|------|------|
| `plastic_promise/core/decay_engine.py` | **NEW** — WeibullDecayCalculator + AccessReinforcement |
| `plastic_promise/core/constants.py` | 新增 `DECAY_CONFIG`、`REINFORCEMENT_CONFIG` |
| `plastic_promise/memory/soul_memory.py` | MemoryRecord 新增 2 字段；MemoryWorthCalculator 新增 `calculate_composite_score()`；MemoryTierManager 升级阈值；EvolveR 集成 |
| `plastic_promise/core/context_engine.py` | `supply()` 返回加 `is_auto_recall` 标记；`_SQLiteStorage` 新增 2 列迁移 |
| `plastic_promise/memory/pipeline.py` | `_process_tagged_to_classified` 初始化 decay/access 字段 |
| `tests/test_decay_engine.py` | **NEW** — Weibull 公式验证、访问强化、复合评分、优雅降级 |

## 11. 验收标准

- [ ] Weibull 衰减：L1 记忆 3 天后 `decay_multiplier ≈ 0.5`，L3 记忆 90 天后 `decay_multiplier ≈ 0.5`
- [ ] 访问强化：access_count=3 的记忆半衰期延长至 `base × 1.5~2.0`
- [ ] 三因素复合评分：`composite = wilson×0.6 + freshness×0.25 + reinforcement×0.15`
- [ ] 主动 recall 触发强化，auto_recall 不触发
- [ ] SQLite 迁移：`decay_multiplier` 和 `effective_half_life` 列自动添加
- [ ] 存量记忆兼容：缺少新字段时默认值正确（1.0 / 3.0）
- [ ] 优雅降级：Weibull 组件故障时回退到纯 Wilson 评分
- [ ] GC 周期：EvolveR 使用 composite_score 驱赶衰减记忆
- [ ] 检索排序：memory_recall 使用 composite_score 排序
