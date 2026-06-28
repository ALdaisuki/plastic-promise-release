# MemoryRecord Serialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) to implement this plan task-by-task.

**Goal:** Replace 2 `pass` stubs in MemoryRecord (to_dict/from_dict) with real implementations for JSON/Rust bridge serialization.

**Architecture:** Pure data transformation — no external dependencies. to_dict reads self attributes into a dict; from_dict constructs a MemoryRecord from a dict with defaults for missing keys.

**Tech Stack:** Python 3.10+, existing MemoryRecord class in soul_memory.py

## Global Constraints

- File: `plastic_promise/memory/soul_memory.py:172-190`
- to_dict returns Dict[str, Any] with all 12 fields + computed worth_score
- from_dict is @classmethod, returns MemoryRecord, tolerates empty dict
- from_dict({}) must return a valid MemoryRecord with defaults (no exceptions)
- worth_score is computed, not stored — present in to_dict output, ignored by from_dict

---

### Task 1: Implement to_dict() + from_dict()

**Files:**
- Modify: `plastic_promise/memory/soul_memory.py:172-190`

**Interfaces:**
- Produces: `MemoryRecord.to_dict() -> Dict[str, Any]`
- Produces: `MemoryRecord.from_dict(cls, data: Dict[str, Any]) -> MemoryRecord`

- [ ] **Step 1: Replace `pass` in to_dict()**

Replace line 178 (`pass`) with:

```python
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

- [ ] **Step 2: Replace `pass` in from_dict()**

Replace line 190 (`pass`) with:

```python
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

- [ ] **Step 3: Verify**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.memory.soul_memory import MemoryRecord

# Round-trip test
orig = MemoryRecord(content='test_experience', memory_type='experience', source='agent')
d = orig.to_dict()
assert d['content'] == 'test_experience'
assert d['memory_type'] == 'experience'
assert d['source'] == 'agent'
assert d['tier'] == 'L1'
assert 'worth_score' in d
print(f'to_dict OK: {len(d)} fields')

restored = MemoryRecord.from_dict(d)
assert restored.content == 'test_experience'
assert restored.memory_id == orig.memory_id
assert restored.tier == 'L1'
assert restored.created_at == orig.created_at
print('Round-trip OK')

# Empty dict
empty = MemoryRecord.from_dict({})
assert empty.content == ''
assert empty.tier == 'L1'
assert empty.memory_id is not None
print('Empty dict OK')

# Partial dict
partial = MemoryRecord.from_dict({'content': 'hello', 'tier': 'L3'})
assert partial.content == 'hello'
assert partial.tier == 'L3'
assert partial.memory_type == 'experience'  # default
print('Partial dict OK')

# worth_score is computed, not stored
d2 = orig.to_dict()
r2 = MemoryRecord.from_dict(d2)
# worth_score should be recalculated from counters
print(f'worth_score preserved: {abs(r2.worth_score - orig.worth_score) < 0.001}')
assert abs(r2.worth_score - orig.worth_score) < 0.001
print('ALL TESTS PASSED')
"
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/memory/soul_memory.py
git commit -m "feat: implement MemoryRecord.to_dict() and from_dict() serialization"
```
