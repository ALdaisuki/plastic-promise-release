---
name: step-closure-ci-fix-2026-07-03
description: Step closure for CI fix — all 7 jobs repaired, ruff format, syntax fixes, MCP restart
metadata:
  type: feedback
---

## Step Closure: CI 修复 (a532f01)

**Task**: 修复全部 7 个 CI job 失败 + ruff format 112 文件 + 修复 2 个语法错误 (domain_recall.py, pack.py) + 重启 MCP 服务器

**Mode**: full

### Lesson
CI 配置文件中的命令必须与实际工具版本匹配。cargo-audit 升级后移除了 --manifest-path，但 CI 没人更新——每个外部工具升级都可能是 CI 断裂点。ruff format 应该在项目初期就跑一次，fork 出去的所有 worktree 分支也应该在合并前跑 format。

**Why**: CI 配置文件写好后就再没人维护过，工具版本升级导致命令行参数不兼容。

**How to apply**: 每次新增 CLI 工具或升级依赖后，先在 CI 上跑一次 dry-run。CI 的非阻塞门应该有定期审查周期。

### Improvement
每次新增 CLI 工具或升级依赖后，先在 CI 上跑一次 dry-run。CI 的 continue-on-error job 应该有每周审查周期。

### Root Cause
CI 从一开始就没人维护——配置写好后再没更新过。7 个 job 全部失败但没有一个人修，因为"全部失败"变成了常态，失去了信号价值。核心问题是 CI 缺乏渐进式修复机制。

### Optimization
下周检查一次 CI 运行结果，挑一个 continue-on-error 的 job（建议从 test-python 开始，mock Ollama/LanceDB），把它升级为硬门。

[[ci-configuration]] [[mcp-server-health]] [[ruff-format]]

[[synced-to-mcp]]
