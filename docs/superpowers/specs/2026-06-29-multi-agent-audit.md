# Multi-Agent Audit — 11 维审计 + 自动修复

> 状态: 已确认 | 日期: 2026-06-29

## 一、目标

扩展现有 7 维 audit_run 到 11 维，覆盖多 Agent 特有维度。审计结果通过标签状态机自动流转——可自动修复的问题由 Daemon 即时处理，需 Claude 介入的写入 `task:pending + assignee:claude`。

## 二、11 维审计矩阵

### 现有 7 维（保持）

| 维度 | 分数 | 数据来源 |
|------|------|---------|
| simplicity | auto | 代码/架构分析 |
| transparency | auto | git log, audit_log |
| audit_closure | auto | 历史 audit 记录 |
| principle_activation | auto | PrincipleTracker |
| memory_supply | auto | 记忆检索质量 |
| constraint_compliance | auto | 约束违规日志 |
| feedback_closure | auto | boost/decay 频率 |

### 新增 4 维

| 维度 | 检查逻辑 | 数据来源 |
|------|---------|---------|
| **trust_health** | 各 Agent trust 均值、最低分、降级趋势 | `defense(action="get")` × N |
| **pipeline_health** | 卡住任务数、完成率、平均耗时 | SQLite tags 扫描 |
| **domain_health** | 域标签分布、过期候选域 | `domain(action="stats")` |
| **bridge_health** | SSE /notify 连通性、Daemon 心跳、事件积压 | HTTP /health + _notify_queue.qsize() |

## 三、执行模型

```
触发: Daemon 每小时 / Pi 超时恢复 / Claude 手动 audit_run

流程:
  1. 收集 → 拉 4 个数据源 (SQLite + defense + domain + HTTP)
  2. 评分 → 每维 0.0-1.0, overall = weighted avg
  3. 诊断 → score < 0.4 → finding + severity
  4. 自动修复 → Tier 1 即时执行
  5. 报告 → memory_store + /notify SSE 广播

自动修复分级:
  Tier 1 (自动): 重置超时 task:active → task:pending
                  清理 7 天前已验收记忆
                  重启失联 Daemon
  Tier 2 (需Claude): trust 调整 >0.1, 原则冲突仲裁, 架构变更
  Tier 3 (观察): 分数下降趋势但未达阈值
```

## 四、实现

### audit_daemon.py (~60 行)

```python
"""Audit Daemon — 定期 11 维审计 + 自动修复"""

async def run_audit():
    scores = {}
    
    # Trust health
    tm = TrustManager()
    trusts = {role: tm.get(role) for role in AGENT_ROLES}
    scores["trust_health"] = sum(trusts.values()) / len(trusts)
    
    # Pipeline health
    stuck = count_stuck_tasks()
    total = count_all_tasks()
    scores["pipeline_health"] = 1.0 - (stuck / max(total, 1))
    
    # Domain health
    dm = DomainManager()
    stats = dm.stats()
    active = sum(1 for d in stats.values() if d["status"] == "active")
    scores["domain_health"] = active / max(len(stats), 1)
    
    # Bridge health
    try:
        httpx.get("http://127.0.0.1:9020/health", timeout=3)
        scores["bridge_health"] = 1.0
    except:
        scores["bridge_health"] = 0.0
    
    overall = sum(scores.values()) / len(scores)
    
    # Store report
    memory_store(
        content=f"Audit: {overall:.2f} | trust={scores['trust_health']:.2f} "
                f"pipeline={scores['pipeline_health']:.2f} "
                f"domain={scores['domain_health']:.2f} "
                f"bridge={scores['bridge_health']:.2f}",
        tags=["audit", "domain:governing"]
    )
    
    # Auto-fix Tier 1
    recover_stuck_tasks()
    cleanup_old_memories()
    
    return scores
```

### 集成到 pi_daemon.py

```python
# main loop 中加审计计数:
_audit_counter = 0
# ...
_audit_counter += 1
if _audit_counter >= 360:  # 每小时
    await run_audit()
    _audit_counter = 0
```

## 五、输出示例

```
[23:00] AUDIT #7
  trust_health:    0.65  (builder .62, fixer .62, reviewer .62)
  pipeline_health: 0.90  (0 stuck, 4 resolved)
  domain_health:   1.00  (7/7 active)
  bridge_health:   1.00  (SSE OK)
  OVERALL:         0.89  ↑ from 0.85

  Auto-fixes: recovered 1 stuck task:active → task:pending
  Needs Claude: none
```

## 六、改动面

| 文件 | 改动 |
|------|------|
| `audit_daemon.py` | 新建 (~60 行) |
| `pi_daemon.py` | +8 行 (审计计数 + run_audit 调用) |
