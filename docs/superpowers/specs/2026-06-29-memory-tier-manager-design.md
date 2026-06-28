# MemoryTierManager — L1/L3 分层迁移 (P3 子项目 2)

**日期**: 2026-06-29
**依赖**: MemoryRecord 序列化 (已完成)

## 背景

`soul_memory.py` 中 MemoryTierManager 的 5 个方法全是 `pass` 空壳。分层管理器负责记忆在 L1（工作记忆，max 200，TTL 24h）和 L3（长期记忆，max 2000，永久）之间的迁移。

## 设计

### __init__(rec_mem=None)
加载 MEMORY_TIERS 配置。可选接收 RecMem 实例用于容量驱逐时访问记忆列表。

### classify_tier(record) → "L1" | "L3"
- worth_score >= 0.5 且 access_count >= 3 → "L3"
- 否则 → "L1"

### promote_to_l3(record)
1. 检查 L3 容量：若已满（>= max_items），驱逐 L3 中 worth_score 最低的记录回 L1
2. 将 record.tier 设为 "L3"

### demote_to_l1(record)
- 将 record.tier 设为 "L1"

### evict_l1_overflow(records) → List[str]
1. 按 worth_score 升序排序
2. 从最低分开始删除直到 records 数量 <= L1 max_items
3. 返回被删除的 memory_id 列表
4. 若提供 rec_mem，调用 rec_mem.forget() 彻底删除

## 验证
- classify_tier: 高 worth + 高 access → L3；低 worth → L1
- promote_to_l3 + L3 满 → 最低 worth 记录被逐回 L1
- evict_l1_overflow: 超出容量后最低分被删除
