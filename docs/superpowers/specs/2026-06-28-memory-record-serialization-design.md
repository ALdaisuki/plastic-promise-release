# MemoryRecord 序列化 — 子项目 1

**日期**: 2026-06-28
**父项目**: P3 记忆系统 — MemoryTierManager / EvolveR / MemoryGC

## 背景

`soul_memory.py` 中 `MemoryRecord.to_dict()` 和 `MemoryRecord.from_dict()` 是 `pass` 空壳。这两个方法是后续 MemoryTierManager、EvolveR、MemoryGC 的数据基础——分层迁移和演化回收都需要序列化记忆。

## 设计

### to_dict()

返回全部实例属性的字典，外加计算属性 `worth_score`：

```python
def to_dict(self) -> Dict[str, Any]:
    return {
        "memory_id": self.memory_id,
        "content": self.content,
        "memory_type": self.memory_type,
        "source": self.source,
        "worth_success": self.worth_success,
        "worth_failure": self.worth_failure,
        "activation_weight": self.activation_weight,
        "tier": self.tier,
        "metadata": dict(self.metadata),
        "created_at": self.created_at,
        "last_accessed": self.last_accessed,
        "access_count": self.access_count,
        "worth_score": self.worth_score,
    }
```

### from_dict(data)

从字典重建 MemoryRecord，缺失字段使用默认值：

```python
@classmethod
def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
    record = cls(
        content=data.get("content", ""),
        memory_type=data.get("memory_type", "experience"),
        source=data.get("source", "user"),
        memory_id=data.get("memory_id"),
        worth_success=data.get("worth_success", 0),
        worth_failure=data.get("worth_failure", 0),
        activation_weight=data.get("activation_weight", 0.5),
        tier=data.get("tier", "L1"),
        metadata=data.get("metadata", {}),
    )
    record.created_at = data.get("created_at", record.created_at)
    record.last_accessed = data.get("last_accessed", record.last_accessed)
    record.access_count = data.get("access_count", 0)
    return record
```

**关键行为**：
- 不抛异常：所有字段都有默认值，`from_dict({})` 返回一个合法的空 MemoryRecord
- `worth_score` 是计算属性，不存入 `from_dict`
- `metadata` 做浅拷贝防止引用泄漏

## 验证

```python
# 往返测试
original = MemoryRecord(content="test", memory_type="experience")
d = original.to_dict()
restored = MemoryRecord.from_dict(d)
assert restored.content == "test"
assert restored.memory_id == original.memory_id
assert restored.tier == "L1"

# 空字典
empty = MemoryRecord.from_dict({})
assert empty.content == ""
assert empty.tier == "L1"
```
