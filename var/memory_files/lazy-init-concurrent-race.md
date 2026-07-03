---
name: lazy-init-concurrent-race
description: Lazy initialization with concurrent execution causes TOCTOU race conditions on component access
metadata:
  type: experience
---

ContextEngine 的 `_ensure_heavy_init()` 延迟到首次 `supply()` 调用才执行，若 `session-init` 使用 `concurrent=True`，其他 atom（domain/memory_gc/system）会在初始化完成前竞态访问 `_dm`/`_ldb`，返回虚假的 "not available" 错误。

修复模式：在所有访问 `engine._dm` 或 `engine._ldb` 的 MCP handler 入口处调用 `engine.ensure_heavy_init()`（公共方法，双检锁保证后续调用零成本）。已修复：domain.py, memory.py, management.py。待 review：domain_recall.py 等约 5 个 handler。

**Why:** 2026-07-03 session-init 报告中 DomainManager `_dm_ok=False` 和 LanceDB `lancedb_unavailable` 都是竞态导致的虚假降级，非真正的初始化失败。

**How to apply:** (1) 任何新 MCP handler 访问 `engine._dm` 或 `engine._ldb` 前必须调用 `engine.ensure_heavy_init()`；(2) 考虑抽取为装饰器统一模式；(3) 新增懒初始化组件时同步更新所有调用方。
