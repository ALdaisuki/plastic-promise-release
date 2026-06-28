# SoulEnforcer 违规日志 — P4

**日期**: 2026-06-29

## 背景

`soul_enforcer.py` 中 3 个 pass 空壳，但底层数据已完整：
- `TrustManager._history` 已由 boost/decay 写入
- `SoulEnforcer._violation_log` 已由 pre_check 写入

## 设计

### TrustManager.history(limit=50)
```python
return self._history[-limit:] if self._history else []
```
纯数据读取，按时间倒序返回最近 N 条。

### SoulEnforcer.log_violation(action, layer, reason)
```python
self._violation_log.append({
    "action": action,
    "layer": layer,
    "reason": reason,
    "timestamp": datetime.now(timezone.utc).isoformat(),
})
```

### SoulEnforcer.get_violation_stats()
```python
# 按层级分组计数 + 今日计数 + 最近5条
```
遍历 `_violation_log` 聚合统计。
