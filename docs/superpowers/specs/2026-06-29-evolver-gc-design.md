# EvolveR + MemoryGC — 自演化与垃圾回收 (P3 子项目 3)

**日期**: 2026-06-29
**依赖**: MemoryRecord 序列化 + MemoryTierManager (已完成)

## 背景

`soul_memory.py` 中 EvolveR（3 空壳）和 MemoryGC（3 空壳）是记忆系统自维护的最后一环。

## EvolveR 设计

### __init__(rec_mem, decay_threshold=MEMORY_DECAY_THRESHOLD)
存储 RecMem 引用和衰减阈值。内部创建 MemoryTierManager 实例用于分层操作。

### evolve_cycle() → Dict
全周期演化：
1. 获取 L3 记录 → worth < threshold → demote_to_l1
2. 获取 L1 记录 → worth >= 0.6 → promote_to_l3
3. 调用 decay_stale(7) 衰减长期未激活的 L1
4. L1 溢出驱逐
5. 返回 {promoted, demoted, decayed, evicted, health_before, health_after}

### decay_stale(days_threshold=7) → int
- 遍历 L1 记录，last_accessed 超过 days_threshold 天
- activation_weight *= 0.7
- 返回衰减数量

## MemoryGC 设计

### __init__(rec_mem)
存储 RecMem 引用。记录上次回收时间戳用于间隔控制。

### mark_decaying() → List[str]
- 扫描所有记录，worth_score < MEMORY_DECAY_THRESHOLD
- 按 worth_score 升序返回 memory_id 列表

### collect(dry_run=True, force=False) → Dict
- dry_run=True：只调用 mark_decaying，报告候选，不删除
- dry_run=False：从最低 worth 开始 forget，直到 health_ratio >= 80%
- force：忽略 GC 间隔（MEMORY_GC_INTERVAL_DAYS）
- 返回 {dry_run, candidates_count, removed, health_before, health_after, freed_slots}
