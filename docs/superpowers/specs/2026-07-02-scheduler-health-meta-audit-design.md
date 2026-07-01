# 调度器健康元审计 — 设计文档

> **状态**: 设计完成，待实施
> **日期**: 2026-07-02
> **核心命题**: 委托系统能否驱动自己进化？——元委托作为最小闭环证明

## 一、概述

现有 Hunter Guild 委托系统已经跑通了"发现→调度→执行→验收"的完整链路。但它缺少一个关键能力：**审视自己的调度质量**。

`scan_scheduler_health` 是第 6 个发现扫描器，与其他 5 个扫描器平级但职责不同——它不扫描记忆池或代码，而是**扫描委托系统自身**。它回答的核心问题是：

> 委托系统这个"引擎"本身运行得怎么样？

它通过一个最小闭环证明"委托系统能驱动自己进化"：

```
审计 → 发现问题 → 报告(委托给Claude) → 写入记忆池
    → 高置信度问题自动调节 → 下轮审计看到变化
    → 恶化项生成 follow-up 委托 → 持续跟踪
```

**设计原则**:
- **零新表** — 复用 `task_queue` + `hunter_failure_log` + `metric_history`
- **零新 Agent** — 仅生成委托给已有的 Claude/Reviewer
- **零新 MCP 工具** — 仅复用 `task_enqueue` + `defense`
- **最小侵入** — 一个新增文件 + 两处修改

### 跨模块接口约定

`scan_scheduler_health(engine, throttles)` 遵循现有扫描器签名模式：

1. **输入**: `engine` (ContextEngine) + `throttles` (dict[str, AdaptiveThrottle]) — 由 daemon 主循环传入
2. **输出**: `{"scanner": "scan_scheduler_health", "findings": N, "dispatched": N, "auto_actions": [...]}` — 标准扫描器返回格式
3. **Throttle 修改**: 由 daemon 主循环根据 `auto_actions` 执行，**扫描器不直接修改 throttle**（保持职责分离，与现有 `AdaptiveThrottle.on_hit()/on_empty()` 的 daemon 侧调用模式一致）

---

## 二、架构

```
                        ┌──────────────────────────┐
                        │  scan_scheduler_health()  │ ← 新增(第6个扫描器)
                        │  6维审计 + 1自动动作       │
                        └──────────┬───────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
            生成审计报告     自动调节 throttle   趋势对比
            (audit_scheduler  (扫描器打回率       (读上次报告
             委托给 Claude)    >50%→降频)         →发现变化)
                    │              │              │
                    ▼              ▼              ▼
              写入记忆池     通知 Claude     生成 follow-up
             (下次对比用)    (可手动回滚)      (如果需要)
```

**与现有系统的关系**：
- 读：`task_queue` + `hunter_failure_log` + `metric_history`
- 写：`task_enqueue`（和其他扫描器平级）
- 调：`AdaptiveThrottle.current`（已有节流机制）
- 防御：`defense(action="reset_throttle")`（手动回滚入口）

---

## 三、六维审计逻辑

### 维度 1：扫描器信噪比 🔴（唯一触发自动动作的维度）

**数据源**: `task_queue` WHERE source_scan IS NOT NULL

```sql
SELECT source_scan,
       COUNT(*) as total,
       SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) as rejected,
       SUM(CASE WHEN status='verified' THEN 1 ELSE 0 END) as verified,
       ROUND(CAST(SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) AS REAL) / MAX(COUNT(*),1), 2) as reject_rate
FROM task_queue
WHERE created_at >= datetime('now', '-7 days')
  AND source_scan IS NOT NULL
  AND status IN ('verified', 'reassigned')
GROUP BY source_scan
ORDER BY reject_rate DESC
LIMIT 3
```

**信号分级**:
| 打回率 | 级别 | 动作 |
|--------|------|------|
| > 50% 且 total ≥ 10 | 🔴 噪音扫描器 | 自动降频 + 通知 Claude |
| 30%-50% | 🟡 关注 | 报告中标记 |
| < 30% | 🟢 健康 | 无需动作 |

**自动降频触发门槛（三重保护）**:
1. `total >= 10` — 防止小样本噪声
2. `rate > 0.50` — 固定阈值（v1；后续根据数据分布决定是否改为动态阈值）
3. `status IN ('verified','reassigned')` — 只统计已验收的委托

### 维度 2：Agent 超时率

**数据源**: `hunter_failure_log` WHERE failure_type='timeout'

```sql
SELECT claimed_by,
       COUNT(DISTINCT task_id) as timeout_tasks,
       ROUND(AVG(escalation_count), 1) as avg_escalation
FROM hunter_failure_log
WHERE failure_type='timeout'
  AND occurred_at >= datetime('now', '-7 days')
GROUP BY claimed_by
ORDER BY timeout_tasks DESC
LIMIT 3
```

**信号分级**:
| 超时数 | 级别 | 动作 |
|--------|------|------|
| > 5 | 🔴 | 报告中建议信任审查 |
| 2-5 | 🟡 | 标记关注 |
| < 2 | 🟢 | 健康 |

### 维度 3：调度延迟

**数据源**: `task_queue` WHERE claimed_at IS NOT NULL

```sql
SELECT task_type,
       ROUND(AVG((julianday(claimed_at) - julianday(created_at)) * 86400), 0) as avg_wait_seconds,
       COUNT(*) as total
FROM task_queue
WHERE status IN ('claimed','executing','done','verified')
  AND claimed_at IS NOT NULL
  AND created_at >= datetime('now', '-7 days')
GROUP BY task_type
ORDER BY avg_wait_seconds DESC
LIMIT 3
```

**信号分级**:
| 等待时间 | 级别 | 动作 |
|----------|------|------|
| > 3600s (1h) | 🔴 | 报告建议调整优先级分配 |
| 600-3600s | 🟡 | 标记关注 |
| < 600s (10min) | 🟢 | 健康 |

### 维度 4：优先级分布失衡

**数据源**: `task_queue` GROUP BY priority

```sql
SELECT priority,
       COUNT(*) as total,
       ROUND(CAST(COUNT(*) AS REAL) / 
         (SELECT COUNT(*) FROM task_queue WHERE created_at >= datetime('now', '-7 days')), 2) as pct
FROM task_queue
WHERE created_at >= datetime('now', '-7 days')
GROUP BY priority
ORDER BY priority
```

**信号分级**:
| 分布 | 级别 | 含义 |
|------|------|------|
| priority=1 占比 > 50% | 🔴 | S级通胀——所有扫描器都在挂紧急委托 |
| priority=4 占比 > 80% | 🟡 | 缺乏高优先级任务 |
| 1-4 均匀分布 | 🟢 | 健康 |

### 维度 5：验收吞吐

**数据源**: `task_queue` WHERE status='verified'

```sql
SELECT verified_by,
       COUNT(*) as verified_total,
       COUNT(DISTINCT DATE(verified_at)) as active_days,
       ROUND(CAST(COUNT(*) AS REAL) / MAX(COUNT(DISTINCT DATE(verified_at))), 1) as avg_per_day
FROM task_queue
WHERE status='verified'
  AND verified_at >= datetime('now', '-7 days')
GROUP BY verified_by
```

**信号分级**:
| 吞吐 | 级别 | 含义 |
|------|------|------|
| avg_per_day > 20 | 🟡 | 验收可能成为瓶颈 |
| active_days < 2 | 🟡 | 验收不及时，委托积压 |
| 均匀 3-5/天 | 🟢 | 健康 |

### 维度 6：趋势对比

**数据源**: 上一轮审计报告（从记忆池通过 `memory_recall` 检索）

**首次运行**: 跳过趋势对比，输出 `first_audit` 标记，仅建立基线。

**后续运行**: 对比本轮与上轮各维度数据：

| 对比项 | 改善 → | 恶化 → |
|--------|--------|--------|
| 打回率变化 (Δ < -0.1) | 报告中记录改善 | 生成 `investigate_recurrence` follow-up 委托 |
| 超时率变化 (timeout_tasks 增加) | 报告中记录改善 | 生成 `review_agent_timeout` follow-up 委托 |
| 延迟变化 (avg_wait 增加 > 2x) | 报告中记录改善 | 生成 `review_dispatch_latency` follow-up 委托 |

---

## 四、自动动作与回滚

### 4.1 触发条件

```python
def _check_auto_throttle(conn) -> list[dict]:
    """返回需要自动降频的扫描器列表"""
    rows = conn.execute("""
        SELECT source_scan,
               COUNT(*) as total,
               SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) as rejected,
               ROUND(CAST(SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) AS REAL) / COUNT(*), 2) as rate
        FROM task_queue
        WHERE created_at >= datetime('now', '-7 days')
          AND source_scan IS NOT NULL
          AND status IN ('verified', 'reassigned')
        GROUP BY source_scan
        HAVING rate > 0.50 AND total >= 10
    """).fetchall()
    return [{"scanner": r[0], "total": r[1], "rejected": r[2], "rate": r[3]} for r in rows]
```

### 4.2 自动降频

```python
async def _apply_auto_throttle(scanner_name: str, rate: float, engine):
    """对高打回率扫描器执行自动降频"""
    throttle = _scanner_throttles.get(scanner_name)
    if not throttle:
        return

    old_interval = throttle.current
    new_interval = min(throttle.current * 2, throttle.base * 8)
    if new_interval == old_interval:
        return  # 已达上限

    # 1. 执行降频
    throttle.current = new_interval

    # 2. 记录到 metric_history（可审计）
    conn.execute(
        "INSERT INTO metric_history (metric_name, metric_value, window_start, window_end) "
        "VALUES (?, ?, datetime('now', '-7 days'), datetime('now'))",
        (f"auto_throttle:{scanner_name}", new_interval)
    )

    # 3. 通知 Claude（可手动回滚）
    await task_enqueue(task_type="notify_throttle_change", to_agent="claude", ...)
```

### 4.3 手动回滚

Claude 收到通知后，通过 `defense` MCP 工具手动重置：

```
defense(action="reset_throttle", scanner="scan_architecture")
```

内部直接恢复 `throttle.current = throttle.base` 并清空 `empty_streak`。

**实现**: `defense` MCP 工具的 `action` 分发中新增 `reset_throttle` 分支，接收 `scanner` 参数，从 daemon 的 `_scanner_throttles` 字典中查找对应 throttle 并重置。

### 4.4 自动回滚

下轮审计时检查：如果该扫描器降频后 7 天打回率 < 30%（且 total ≥ 5），自动恢复原始频率。48h 冷静期（降频后 48h 内不自动回滚，避免振荡）留作 Phase 2。

**回滚决策表**:

| 场景 | 触发方 | 动作 | 通知 |
|------|--------|------|------|
| Claude 判断误判 | 手动 `defense action=reset_throttle` | 立即恢复 base | 通知扫描器 |
| 打回率降到 <30% | 下轮审计自动检测 | 自动恢复 base | 通知 Claude |
| 降频后打回率继续 >50% | 下轮审计 | 再次翻倍（上限 base×8） | 通知 Claude |

### 4.5 日志与追踪

自动动作写入 `metric_history` 表：

```
metric_name:  auto_throttle:scan_architecture
metric_value: 1200    (新间隔秒数)
window_start: -7d
window_end:   now
```

所有自动动作在 `audit_scheduler` 委托的 payload 中汇总，Claude 可直接查看：
- 本轮触发了哪些自动动作
- 每个动作的触发数据
- 历史上该扫描器的节流变更记录

---

## 五、审计报告结构

生成的 `audit_scheduler` 委托包含以下 payload：

```json
{
  "audit_id": "audit_20260702_143000",
  "is_first_audit": false,
  "previous_audit_id": "audit_20260701_143000",
  "dimensions": {
    "scanner_snr": {
      "top3": [
        {"scanner": "scan_architecture", "reject_rate": 0.55, "total": 12, "level": "red", "auto_action": "throttled_1200s"}
      ],
      "auto_actions": ["scan_architecture: 600s→1200s"]
    },
    "agent_timeout": {
      "top3": [
        {"agent": "pi_fixer", "timeout_tasks": 7, "avg_escalation": 2.1, "level": "red"}
      ]
    },
    "dispatch_latency": {
      "top3": [
        {"task_type": "audit_architecture", "avg_wait_seconds": 5400, "level": "red"}
      ]
    },
    "priority_balance": {
      "distribution": {"1": 0.15, "2": 0.25, "3": 0.45, "4": 0.15},
      "level": "green"
    },
    "verification_throughput": {
      "avg_per_day": 4.2,
      "active_days": 6,
      "level": "green"
    },
    "trends": {
      "compared_to": "audit_20260701_143000",
      "improvements": ["scanner_snr improved from 0.55 to 0.35"],
      "degradations": ["agent_timeout increased from 2 to 7 tasks"],
      "follow_up_tasks": ["review_agent_timeout:pi_fixer"]
    }
  },
  "generated_by": "scan_scheduler_health",
  "generated_at": "2026-07-02T14:30:00"
}
```

---

## 六、文件变更清单

### 新增文件

| 文件 | 用途 | 行数估计 |
|------|------|---------|
| `plastic_promise/cron/scan_scheduler_health.py` | 6维审计 + 自动降频 + 趋势对比 + 报告生成 | ~250行 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `daemons/maintenance_daemon.py` | 注册 scan_scheduler_health 到主循环 + `_scanner_throttles` 新增条目 + 导入 |
| `plastic_promise/defense/trust_store.py` | 注册 `reset_throttle` 防御动作 |

### 测试文件

| 文件 | 内容 |
|------|------|
| `tests/test_scheduler_health.py` | 6维查询测试 + 自动降频测试 + 回滚测试 + 首次运行测试 + E2E |

---

## 七、闭环验证清单

- [ ] 本轮审计 → `audit_scheduler` 委托生成成功
- [ ] Claude 审阅委托 → 报告写入记忆池
- [ ] 下轮审计 → `memory_recall("scheduler_health")` 检索到上轮报告
- [ ] 趋势对比功能正常（改善/恶化识别）
- [ ] 自动降频触发条件正确（≥10 total, >50% rate）
- [ ] 自动降频执行正确（throttle.current ×2, 上限 base×8）
- [ ] `metric_history` 记录自动动作
- [ ] `notify_throttle_change` 委托通知 Claude
- [ ] 手动回滚 `defense(action="reset_throttle")` 正常
- [ ] 自动回滚条件正确（rate <30%, total ≥5）
- [ ] 首次运行跳过趋势对比，输出 `first_audit` 标记
- [ ] 恶化项正确生成 follow-up 委托
