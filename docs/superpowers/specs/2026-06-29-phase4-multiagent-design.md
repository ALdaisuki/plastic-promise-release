# Phase 4 — 多Agent联动 + 收尾

**日期**: 2026-06-29
**服务原则**: #4 上下文驱动, #7 器官互保, #1 奥卡姆剃刀

## 组件 1：上下文预备

**方案**: post_task 自动预取 + MCP 手动刷新

### 自动预取
- `post_task` 完成后，根据 `AgentBehaviorTracker` 推算最可能的 next_task_type
- 调用 `context_supply(next_description)` 预取上下文
- 缓存到 `engine._context_ready: Dict[str, ContextPack]`
- TTL 5 分钟

### MCP 工具 context_ready
- `context_ready(task_hint)` → 返回预备上下文
- Agent 主动调用时手动刷新

### 文件
- `loop/soul_loop.py`: post_task 末尾 + 预取逻辑
- `mcp/tools/context.py`: 新增 handle_context_ready
- `mcp/server.py`: 注册工具

## 组件 2：Bridge TODO

### bus_client.py:229
接收 Pi task → memory_recall 获取上下文 → 返回 result

### bus_client.py:281
监听 memory:sync → ZMQ SESSION_PUB 转发到 N.E.K.O

## 组件 3：SSE 生产化

### server.py run_sse()
- 启动日志（端口、PID）
- GET /health → {"status":"ok","uptime":N}
- SIGINT/SIGTERM → 优雅关闭
