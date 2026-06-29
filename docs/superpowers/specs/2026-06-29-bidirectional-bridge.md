# Bidirectional Bridge — A+C 双向事件广播

> 状态: 已确认 | 日期: 2026-06-29 | 改动 <30 行

## 一、架构

Daemon 从"任务启动器"升级为"状态变更监听器 + 事件分发器"。

```
Pi 完成 → Daemon 检测 exit → SQLite UPDATE tag → SSE broadcast
                                                       ├→ Reviewer Daemon 自动触发
                                                       ├→ Fixer Daemon 按需响应
                                                       └→ Claude 可见进度
```

## 二、改动

### pi_daemon.py — 加 post-task 钩子

```python
# 在 mark_task_accepted 之后:
await notify_state_change({
    "type": "tag_transition",
    "from_tag": "task:accepted",
    "to_tag": "task:active",
    "agent": ROLE,
    "domain": DOMAIN,
    "task_id": task_id,
    "content": task_content[:100],
    "tags": ["task:active", f"owner:{ROLE}", f"domain:{DOMAIN}"],
})

async def notify_state_change(event):
    """推送标签变更到 SSE /events。"""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                "http://127.0.0.1:9020/notify",
                json=event,
                timeout=5
            )
    except Exception:
        pass  # 非关键路径
```

### server.py — 加 /notify 端点

```python
# 新增路由: POST /notify — 接收外部推送并广播到 SSE
async def handle_notify(request: Request):
    import json as _json
    body = await request.body()
    event = _json.loads(body.decode())
    await _notify_queue.put(event)
    return Response(status_code=202)
```

### server.py — memory_store push 加 tags

```python
# handle_memory_store 已有 notify_issue_change()
# 确保 payload 包含 tags 字段
```

## 三、订阅过滤

每个监听方按 tag 过滤：

| 订阅方 | 关注 tag | 动作 |
|--------|---------|------|
| Reviewer Daemon | `task:active + domain:building` | spawn Pi --mode=reviewer |
| Fixer Daemon | `task:rejected + domain:fixing` | spawn Pi --mode=fixer |
| Claude | `task:review + domain:reflecting` | 终端提示验收 |
| Claude | `task:active` | 进度可见 |

## 四、改动面

| 文件 | 改动 | 行数 |
|------|------|------|
| `pi_daemon.py` | +notify_state_change + httpx post | ~15 |
| `server.py` | +/notify endpoint | ~8 |
| `server.py` | memory_store push 加 tags 字段 | ~3 |
