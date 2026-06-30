# MVP Usage Bootstrap — 记忆系统使用率提升设计

## Context

全链路检修完成后 (`a77b7db`)，SQLite 中仅 5 条测试数据、0 条真实 Skill 追踪记录。5 个根因中，Batch 1 (MVP) 聚焦两个最阻碍使用的：

| # | 问题 | 现状 | 目标 |
|---|------|------|------|
| 2 | 文件系统绕过 MCP | 6 条 `.md` 在文件系统，0 条在 MCP | 所有记忆经 MCP 管道 |
| 5 | 审计频率过高 | 22 次/24min (~1/min) | 1 次/小时 |

## Fix 1: 文件系统绕过 → MCP 强制通道

### 1a. CLAUDE.md 启动序列硬性检查

在 CLAUDE.md 会话启动步骤中，`auto_context_inject` 之前加一步：

```
0. server_health_check — curl http://127.0.0.1:9020/health
   不可用 → 启动服务器（python -m plastic_promise.mcp.server --sse 9020）
   仍不可用 → 告警，降级到文件系统模式（带 [[pending-sync]] 标记）
```

### 1b. 存量同步工具

新增 `memory_sync_files` MCP 工具（或 CLI 脚本）：

- 扫描 `~/.claude/projects/F--Agent-Memory-system/memory/*.md`
- 解析 frontmatter（name, description, metadata.type）
- 对每条调用 `memory_store(content, tags, entity_ids)`
- 去重：已包含 `[[memory-system-primary-channel]]` 标记的跳过
- 同步后更新源 `.md`，追加 `[[synced-to-mcp]]` 标记

### 1c. 写入拦截（轻量版）

`handle_memory_store` 加服务器可用性检查。失败时返回明确错误（当前返回 `stored: True` 即使 SQLite 写入失败）。

## Fix 2: 审计频率 → 每小时一次

### 2a. Daemon 轮询间隔

**文件**: `audit_daemon.py`（或在 `plastic_promise/defense/soul_audit.py`）

- 当前推断间隔 ~60s
- 改为 3600s (1小时)
- 加 ±300s jitter 防惊群
- 环境变量覆盖: `AUDIT_INTERVAL_SECONDS`

### 2b. 启动时首次审计

服务器启动后 30s 执行首次审计（冷启动引导），之后按 3600s 间隔。

## 验收标准

1. `memory_sync_files` 执行后，6 条文件系统记忆出现在 MCP SQLite 中
2. 新会话中 `memory_store` 正常工作，`.md` 写入带 `[[synced-to-mcp]]`
3. `audit_log` 增长速率 ≤ 2 条/小时
4. 端到端：CLAUDE.md 启动序列 0→1→2→3→4 步骤全部成功

## 不变更

- Batch 2 范围（编码修复、域污染）本次不动
- 现有记忆质量管道逻辑不变
- MCP 工具签名向后兼容
