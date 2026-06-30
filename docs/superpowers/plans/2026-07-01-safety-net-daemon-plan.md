# Safety-Net Daemon — 升级为兜底审查Agent

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.
> **Status:** writing-plans | **Date:** 2026-07-01

**Goal:** 将 [maintenance_daemon.py](file:///f:/Agent/Memory system/daemons/maintenance_daemon.py) 从被动审计升级为主动自治安全网——自动检测孤儿 step、问题记忆、未闭环 issue，通过 Pi Agent + 标签机 + 记忆系统自动修复。

**Root Cause:** 检测能力已有（`skill_session_trace`、`memory_gc`、`issue_list`），但无人主动调用。检测→修复链路断裂——daemon 停留在报告阶段，不调度修复。

**Tech Stack:** Python 3.11+, httpx, SQLite (existing), MCP SSE (existing)

---

## 现状诊断

| 能力 | 检测工具 (MCP) | 自动修复 | 当前状态 |
|------|---------------|---------|---------|
| 超时任务恢复 | `recover_stuck_tasks()` | SQLite 直写 tags | 已有 ✓ |
| 旧标签清理 | `cleanup_old_tags()` | SQLite 直写 tags | 已有 ✓ |
| 4维健康审计 | `run_audit()` | `/notify` 写入报告 | 已有但被动 |
| **孤儿 step** | `skill_session_trace` | ❌ 无人调用 | 可检测不可修 |
| **问题记忆** | `memory_gc(dry_run)` + worth 扫描 | ❌ 无人调用 | 可检测不可修 |
| **未闭环 issue** | `issue_list` + 标签扫描 | ❌ 无人调用 | 无检测无修复 |

---

## Global Constraints

- 零新文件 — 只改 `daemons/maintenance_daemon.py`
- 零新依赖 — httpx 已在用，SQLite 直查已有
- 不改 MCP 工具签名 — daemon 通过 HTTP `/messages` 调现有 MCP 工具
- 不改标签状态机 — 修复任务走 `task:pending → assignee:pi_fixer` 标准流程
- 一次扫描修复一条 → 不批量操作（安全优先，奥卡姆剃刀）
- daemon 自己不做复杂决策 — 复杂修复发 `task:pending` 让 Pi Agent 处理

---

## File Structure

```
daemons/
└── maintenance_daemon.py         ← MODIFY: +3 扫描函数 + 主循环扩展 (~+80 行)

零新文件
```

---

## 架构：三个扫描周期

```
main() 主循环 (10s 粒度):
  ├─ recover_stuck_tasks()           ← 已有，每10s
  └─ audit ticks:
       ├─ cleanup_old_tags()         ← 已有，每300s
       ├─ run_audit()                ← 已有，每300s
       ├─ ★ scan_orphan_steps()      ← 新增，每600s (10min)
       ├─ ★ scan_memory_health()     ← 新增，每600s
       └─ ★ scan_unclosed_issues()    ← 新增，每600s
```

每个扫描器独立运行，互不阻塞。每次扫描最多触发一次修复（安全限制）。

---

### Task 1: `scan_orphan_steps()` — 检测孤儿 step

**检测逻辑：**
1. 通过 MCP `skill_session_trace(session_scope="all")` 获取所有 session
2. 解析 `gaps[type="orphan_active"]`
3. 对每个孤儿按 idle 时间分级处理

**自动修复分级：**

| idle 时长 | 动作 | 说明 |
|----------|------|------|
| > 120 min | 自动 `skill_session_complete(entity_id, "abandoned: 超时未闭环")` | 自动关 |
| 30~120 min | 发 `task:pending + assignee:pi_fixer + type:close_orphan_step` | 让 Pi 处理 |
| < 30 min | 忽略 | 可能正在执行中 |

**MCP 调用链：**
```
skill_session_trace(session_scope="all") → 解析 response → 对每个 orphan:
  if idle > 120min → skill_session_complete(entity_id, "abandoned: ...")
  elif idle > 30min → memory_store(修复任务 tags)  # 走标签调度
```

**代码位置：** `daemons/maintenance_daemon.py` 新增函数 `async def scan_orphan_steps()`

---

### Task 2: `scan_memory_health()` — 检测问题记忆

**检测逻辑：**
1. 通过 MCP `memory_gc(dry_run=true)` 获取合并候选
2. 直接 SQLite 扫描 `worth_score < 0.3` 的记忆（不调 MCP，daemon 已有 DB 直连）
3. 发现候选 → 按类型分级处理

**自动修复分级：**

| 问题类型 | 检测方式 | 动作 |
|---------|---------|------|
| 重复记忆 (cos ≥ 0.70) | `memory_gc(dry_run)` → `merge.candidates` | 自动 `memory_forget` 较低 worth 的那条 |
| 低 worth (< 0.15) | SQLite 扫描 | 自动 `memory_forget` — 接近无用 |
| 中低 worth (0.15~0.30) | SQLite 扫描 | 发 `task:pending + assignee:pi_fixer + type:correct_memory` |
| 大量衰减记忆 (candidates > 10) | `memory_gc(dry_run)` → `candidates_count` | 不自动合并，发 `task:pending` 让 Pi 审查后执行 `memory_gc(dry_run=false)` |

**代码位置：** `daemons/maintenance_daemon.py` 新增函数 `async def scan_memory_health()`

---

### Task 3: `scan_unclosed_issues()` — 检测未闭环 issue

**检测逻辑：**
1. 通过 MCP `issue_list(status="open")` 获取所有 open issue
2. 按创建时间分级处理

**自动修复分级：**

| 超时 | 动作 |
|------|------|
| > 48h | 自动 `issue_transition → closed` (stale) |
| > 24h | 发 `task:pending + assignee:pi_fixer + type:close_stale_issue` |
| < 24h | 忽略 |

**代码位置：** `daemons/maintenance_daemon.py` 新增函数 `async def scan_unclosed_issues()`

---

### Task 4: `dispatch_fix_task()` — 修复任务发布器（辅助函数）

统一的任务发布接口，通过 `/notify` + memory 标签走标准调度：

```python
async def dispatch_fix_task(task_type: str, detail: str, target_id: str = ""):
    """发布修复任务，走标签状态机调度。"""
    tags = [
        "task:pending",
        "assignee:pi_fixer",
        "domain:fixing",
        f"type:{task_type}",
        f"ts:{datetime.now().strftime('%Y%m%dT%H%M%S')}",
    ]
    if target_id:
        tags.append(f"target:{target_id}")
    
    async with httpx.AsyncClient() as client:
        await client.post(f"{MCP_URL}/notify", json={
            "type": "fix_task",
            "task_type": task_type,
            "content": detail,
            "tags": tags,
            "source": "safety_net_daemon",
            "ts": datetime.now().isoformat(),
        }, timeout=5)
```

**代码位置：** `daemons/maintenance_daemon.py` 新增函数

---

### Task 5: 主循环修改 — 慢周期计数器

在主循环中新增慢周期计数器（每 600s = 10min 一轮安全网扫描）：

```python
# 现有变量
tick = 0
audit_threshold = max(1, INTERVAL // 10)  # 300/10 = 30 → 每300s审计

# 新增
safety_net_interval = 600  # 10min 安全网扫描间隔
safety_net_threshold = safety_net_interval // 10  # 60 ticks
```

在 while 循环中插入安全网扫描：

```python
while True:
    tick += 1
    if tick >= audit_threshold:
        # ... 现有 audit 逻辑 ...
    elif tick % safety_net_threshold == 0:
        # 安全网扫描 — 三个扫描器顺序执行，互不阻塞
        try:
            await scan_orphan_steps()
        except Exception as e:
            print(f"  [SAFETY_NET] scan_orphan_steps error: {e}")
        try:
            await scan_memory_health()
        except Exception as e:
            print(f"  [SAFETY_NET] scan_memory_health error: {e}")
        try:
            await scan_unclosed_issues()
        except Exception as e:
            print(f"  [SAFETY_NET] scan_unclosed_issues error: {e}")
    else:
        recover_stuck_tasks()
    await asyncio.sleep(10)
```

**代码位置：** `daemons/maintenance_daemon.py` `main()` 函数内修改

---

### Task 6: 端到端验证

- [ ] **Step 1:** 启动 daemon 验证无崩溃
- [ ] **Step 2:** 模拟孤儿 step → 验证 daemon 检测并自动补闭环
- [ ] **Step 3:** 制造低 worth 记忆 → 验证 daemon 发现并清理/发任务
- [ ] **Step 4:** 验证 audit 日志记录了安全网操作
- [ ] **Step 5:** 验证 `/notify` 发出的修复任务被 MCP 正确持久化

---

## 原则对齐

| # | 原则 | 如何满足 |
|---|------|---------|
| 1 | 奥卡姆剃刀 | 零新文件，扩展现有 daemon 而非新建 |
| 4 | 上下文驱动 | 每次扫描通过 MCP 工具获取最新数据 |
| 7 | 器官互保 | daemon 是免疫系统——自己检测、自己调度修复 |
| 9 | 信任驱动 | 修复任务走 Pi Agent 标签调度，信任分控制执行权 |
| 10 | 自演化闭环 | 检测→修复→验证形成完整闭环，修复结果回流记忆池 |

## 边界定义

**Daemon 不做的事（超出兜底审查 Agent 职权）：**
- 不做复杂代码修改（那是 Pi Builder 的活）
- 不修改原则（那是 Claude/governing 域的事）
- 不批量操作（一次扫描最多一条修复——安全网不是推土机）
- 不直接调用 `spawn Pi` — 只发标签任务，让 Pi Daemon 自己认领

**Daemon 做的事：**
- 定期扫描系统健康 → 发现异常 → 分级处理
- Tier 1 (明确可自动) → 自己修
- Tier 2 (需判断) → 发 `task:pending` 给 Pi
- Tier 3 (需决策) → 写入 audit_log，由 Claude 注意
