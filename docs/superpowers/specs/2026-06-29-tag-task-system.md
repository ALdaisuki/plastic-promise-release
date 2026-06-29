# Tag-Based Task System — 标签状态机替代子串匹配

> 状态: 已确认 | 日期: 2026-06-29

## 一、目标

用标签状态机替代 "TASK for pi_builder" 子串匹配。Daemon 零 LLM 开销检查任务——直接查 SQLite tags。

## 二、标签规范

```
task:pending    — 等待认领
task:active     — 执行中
task:done       — 已完成，等待验收
task:reviewed   — 已验收通过

owner:pi_builder    — 谁在做
assignee:pi_builder — 分配给谁
reviewer:claude     — 谁验收

issue_id:12         — 桥接 Issue 表（可选）
```

## 三、生命周期

```
Claude → memory_store(tags=["task:pending","assignee:pi_builder","domain:building"])
Pi     → memory_recall → 找到 → memory_store(tags=["task:active","owner:pi_builder"])
       → 执行 → memory_store(tags=["task:done","owner:pi_builder"])
Claude → 验收 → memory_store(tags=["task:reviewed","reviewer:claude"])
```

## 四、Daemon 零 LLM 查询

```python
# pi_daemon.py — 直接用 ContextEngine 查 SQLite，不调 LLM
from plastic_promise.core.context_engine import ContextEngine
engine = ContextEngine()

def has_pending_task(role, domain):
    for mid, mem in engine._memories.items():
        tags = mem.get("tags", [])
        if "task:pending" in tags and f"assignee:{role}" in tags:
            return True
    return False

# 主循环:
while True:
    if has_pending_task(ROLE, DOMAIN):
        spawn pi --print "执行任务..."   ← 只在有任务时才调 LLM
    sleep 10
```

**Token 节省：** 8640 次/天 LLM 调用 → 仅在任务到达时调用（<10 次/天）。

## 五、改动面

| 文件 | 改动 | 说明 |
|------|------|------|
| `pi_daemon.py` | 重写 | Python 直查 SQLite + spawn Pi |
| `pi_worker.ps1` | 保留 | 旧模式兼容 |
| CLAUDE.md | 更新 | 任务发布规范 |

## 六、与 Issue 表关系

Issue 表保留为 Claude 的只读仪表盘。标签状态机是 Pi 的工作视图。通过 `issue_id:N` 标签桥接。

路径 A——纯记忆池，不依赖 Issue 表。
