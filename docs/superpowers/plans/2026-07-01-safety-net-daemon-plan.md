# Safety-Net Daemon → 全域创新引擎

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** verified ✅ → **Phase 3: 全域创新引擎 (标签调度 + 打回区 + 模式识别)** | **Date:** 2026-07-01

**Goal:** 将 [maintenance_daemon.py](file:///f:/Agent/Memory system/daemons/maintenance_daemon.py) 从被动审计升级为主动自治安全网 — 自动检测孤儿 step、问题记忆、未闭环 issue，通过 Pi Agent + 标签机 + 记忆系统自动修复。**Phase 2: 免疫系统化** 引入记忆池质量工程师。**Phase 3: 全域创新引擎** 引入多 Agent 标签调度 + 打回区 + 跨域模式识别。

**Root Cause:** 检测能力已有（`skill_session_trace`、`memory_gc`、`issue_list`），但无人主动调用。检测→修复链路断裂——daemon 停留在报告阶段，不调度修复。

**Tech Stack:** Python 3.11+, httpx, SQLite (existing), MCP SSE (existing)

---

## 已建成扫描器 (as-built, 2026-07-01)

### 主循环架构

```
main() 主循环 (10s 粒度):
  ├─ recover_stuck_tasks()               ← 每10s: task:active>5min→pending
  ├─ 审计节拍 (每 300s):
  │    ├─ cleanup_old_tags()             ← 7天旧标签清理
  │    ├─ run_audit()                    ← 五维审计(trust/pipeline/domain/bridge/memory_quality)
  │    │    └─ scan_self_noise           ← 审计内容去重(不存储重复报告)
  │    └─ scan_llm_classify()            ← 后台 LLM 分类(Ollama qwen2.5:3b)
  └─ 安全网 + 创新节拍 (每 600s):
       ├─ scan_innovation_opportunities()  ← Phase 3: 全域模式识别
       ├─ scan_duplicate_clusters()        ← Phase 2: 大数据清理
       ├─ scan_stale_worth()               ← Phase 2: worth复活
       ├─ scan_tier_migration()            ← Phase 2: tier升级
       ├─ scan_category_stuck()            ← Phase 2: 分类监控
       ├─ scan_redo_queue()                ← Phase 3: 打回区超时升级
       ├─ scan_orphan_steps()              ← Phase 1: 孤儿step
       └─ scan_unclosed_issues()           ← Phase 1: 未闭环issue
```

### Phase 1: 兜底审查 (3 扫描器)

| 扫描器 | 检测 | 自动修复 | 标签调度 |
|--------|------|---------|---------|
| `scan_orphan_steps` | `skill_session_trace` → orphans | idle>120min→自动 closed | 30~120min→dispatch fixer |
| `scan_unclosed_issues` | `issue_list` → open issues | age>48h→自动 closed | 24~48h→dispatch fixer |
| `recover_stuck_tasks` | SQLite task tags | 5min reset | — |

### Phase 2: 免疫系统化 (4 扫描器)

| 扫描器 | 检测 | 操作 |
|--------|------|------|
| `scan_duplicate_clusters` | SQL GROUP BY 完全相同内容 | 保留最高 worth，DELETE 其余 |
| `scan_stale_worth` | (0,0) worth 记录 | 基于 last_accessed 计算真实 worth |
| `scan_tier_migration` | access_count + last_accessed | L1→L2(7天), L2→L3(5次+3天) |
| `scan_category_stuck` | llm_pending 卡住 + stale 'other' | 触发 memory_reclassify |

**Phase 2 实际清理成果（首次运行）：**
- 重复记忆: 16x "Rust偏好" + 7x "daemon审计" + 8x "模板垃圾" = 31 条清理
- Worth 复活: 124→77 (0,0) 条
- Tier 升级: 126 L1→L2, 3 L2→L3

### Phase 3: 全域创新引擎 (标签调度 + 打回区 + 模式识别)

**标签调度引擎 `dispatch_fix_task()`:**

不再只调 `pi_fixer`。Daemon 根据问题类型自动路由到 4 种 Agent：

| 发现 | assignee | 域 | 标签 |
|------|---------|------|------|
| 重复Bug模式 | reviewer | reflecting | `task:pending, assignee:pi_reviewer` |
| 记忆退化 | fixer | fixing | `task:pending, assignee:pi_fixer` |
| 技能链断裂 | reviewer | reflecting | `task:pending, assignee:pi_reviewer` |
| 信任分异常 | claude | governing | `task:pending, assignee:claude` |
| 僵尸域 | claude | governing | `task:pending, assignee:claude` |
| 分类瓶颈 | fixer | fixing | `task:pending, assignee:pi_fixer` |

**打回区 `tag_for_redo()` + `scan_redo_queue()`:**

```
发现可疑记忆 → tag_for_redo(memory_id, reason, assignee=reviewer)
  → [redo:required, redo:assigned:pi_reviewer, reason:...]
    ├─ 12h 无人处理 → 追加 dispatch:claude (提醒Claude)
    └─ 24h 无人处理 → 升级 task:pending + assignee:pi_fixer (强制调度)
```

**全域创新扫描器 `scan_innovation_opportunities()`:**

6 维跨域模式识别，每次扫描最多 6 条创新提案：

| 维度 | 检测条件 | 路由 |
|------|---------|------|
| 重复 Bug | 同一类 fix 出现 ≥3 次 | reviewer |
| 记忆退化 | 最近50条 avg_worth < 0.45 | fixer |
| 技能链断裂 | >5 个未闭环 task:active | reviewer |
| 信任分异常 | Agent trust < 0.5 | claude |
| 僵尸域 | 域 3 天无活动 | claude |
| 分类瓶颈 | >15 条 other 超 2h 未分类 | fixer |

---

## 原则对齐

| # | 原则 | 如何满足 |
|---|------|---------|
| 1 | 奥卡姆剃刀 | 零新文件、零新依赖 — 只改 maintenance_daemon.py + test |
| 2 | 全过程可查 | 每步有 git commit + tag_audit_finding 存储审计发现 |
| 4 | 上下文驱动 | 所有扫描基于 MCP 工具 + SQLite 实时数据，不猜测 |
| 7 | 器官互保 | daemon 是免疫系统 + 创新引擎——自我检测、自我调度、自我创新 |
| 9 | 信任驱动 | 调度路由基于 Agent 信任分(低信任→claude审核) |
| 10 | 自演化闭环 | 检测→分级(自动/打回/调度)→验证→创新提案 完整闭环 |

## 边界定义

**Daemon 做的事：**
- 定期扫描全系统健康 → 发现异常 → 分级处理
- Tier 1 (明确可自动) → 直接修(orphan/duplicate/issue close)
- Tier 2 (需审查) → `tag_for_redo` 打回区 (让 Reviewer 审查)
- Tier 3 (需调度) → `dispatch_fix_task` 标签调度 (fixer/reviewer/builder/claude)
- Tier 4 (需决策) → 创新提案 → dispatch claude (Claude 决策)
- 记忆池自我质控: 去重、worth复活、tier升级、分类监控

**Daemon 不做的事：**
- 不做复杂代码修改(那是 Pi Builder 的活)
- 不修改原则(那是 Claude/governing 域的活)
- 不批量操作(一次扫描最多 1 条修复 — 安全网不是推土机)
- 不直接 spawn Pi — 只发标签任务，让 Pi Daemon 自己认领
