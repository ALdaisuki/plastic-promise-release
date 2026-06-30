# 全域创新调度中心 — 猎人公会委托系统 设计文档

> **状态**: 设计完成，待实施
> **日期**: 2026-07-01
> **核心比喻**: 异世界猎人工会委托系统 → Daemon 全域创新调度中心

## 一、概述

将现有 `maintenance_daemon.py` 从"标签调度引擎"升级为完整的**全域创新调度中心**（猎人公会委托系统）：

1. **发现层升级** — 5 个新扫描器（架构坏味道、代码质量趋势、跨模块耦合、信任分异常、记忆衰减）
2. **路由层升级** — 从被动标签轮询升级为`task_queue` 委托板 + SSE 实时推送 + 轮询兜底
3. **治理层升级** — 猎人等级（信任分视图）、失败惩罚、越级控制、长老验收

**核心隐喻映射**:

| 猎人公会概念 | 系统实现 |
|-------------|---------|
| 工会委托板 | `task_queue` 表 |
| 委托书 | 一条任务记录 (payload + priority + status) |
| 猎人 | Agent (Claude, pi_builder, pi_fixer, reviewer) |
| 揭榜/认领 | `task_claim()` 原子操作 |
| 猎人失联 | 心跳超时 → 委托释放回委托板 |
| 委托升级 | escalation_count → 超3次升级给 Claude (S级猎人) |
| 长老验收 | `task_verify(verdict)` |
| 猎人查看委托板 | `task_inbox(agent_name)` |
| 挂出委托 | `task_enqueue()` |
| 公会传令使 | MCP SSE 广播 |

---

## 二、数据库表设计

### 2.1 `task_queue` — 公会委托板（核心表）

```sql
CREATE TABLE task_queue (
    -- ══ 核心标识 ══
    id              TEXT PRIMARY KEY,          -- "t_20260701_0001" 格式
    task_type       TEXT NOT NULL,             -- fix_memory | close_orphan | review_code
                                              -- audit_architecture | investigate_recurrence | ...
    priority        INTEGER DEFAULT 3,         -- 1=紧急(S级) 2=优先(A级) 3=日常(B级) 4=低优先级(C级)
    status          TEXT DEFAULT 'pending',    -- pending|pending_review|claimed|executing|done|verified|reassigned

    -- ══ 委托内容 ══
    title           TEXT NOT NULL,             -- 委托标题（一句话）
    description     TEXT,                      -- 详细描述 / 上下文
    payload         TEXT,                      -- JSON: 结构化参数

    -- ══ 路由信息 ══
    from_agent      TEXT DEFAULT 'daemon',     -- 委托人: daemon|claude|reviewer|pi_builder|...
    to_agent        TEXT NOT NULL,             -- 目标猎人类型: pi_builder|pi_fixer|pi_reviewer|claude
    domain          TEXT,                      -- 域标签: fixing|building|reflecting|governing

    -- ══ 生命周期 ══
    claimed_by      TEXT,                      -- 揭榜的 Agent 实例 ID
    claimed_at      TEXT,                      -- ISO8601 揭榜时间
    heartbeat_at    TEXT,                      -- ISO8601 最后心跳时间
    done_at         TEXT,                      -- ISO8601 完成回报时间
    verified_at     TEXT,                      -- ISO8601 验收时间
    verified_by     TEXT,                      -- 验收人
    verify_verdict  TEXT,                      -- 验收结论: accepted|rejected|reassigned
    result          TEXT,                      -- 猎人回报的结果/产物

    -- ══ 超时与升级 ══
    escalation_count INTEGER DEFAULT 0,        -- 超时/打回次数
    max_escalations  INTEGER DEFAULT 3,        -- 升级阈值（默认3次→上交给Claude）
    last_escalation_at TEXT,                   -- 上次升级时间
    timeout_seconds  INTEGER DEFAULT 300,      -- 心跳超时秒数（默认5分钟）

    -- ══ 关联体系 ══
    memory_id       TEXT,                      -- 关联的记忆 ID
    principle_id    TEXT,                      -- 关联的原则 ID
    source_scan     TEXT,                      -- 来源扫描器: scan_innovation|scan_orphan|...
    parent_task_id  TEXT,                      -- 父任务（如验收子任务指向原任务）

    -- ══ 审计字段 ══
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_task_status ON task_queue(status);
CREATE INDEX idx_task_to_agent ON task_queue(to_agent);
CREATE INDEX idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX idx_task_parent ON task_queue(parent_task_id);
```

### 2.2 `task_subscriptions` — 猎人订阅表（通知精准推送）

```sql
CREATE TABLE task_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,             -- pi_builder | pi_fixer | pi_reviewer | claude
    task_type_filter TEXT,                     -- 关注的委托类型（NULL=全部），支持 GLOB 如 'fix_*'
    priority_min    INTEGER DEFAULT 3,         -- 最低优先级（只收 ≤ 此值的委托）
    keywords        TEXT,                      -- JSON: ["memory","orphan","audit"]
    enabled         INTEGER DEFAULT 1,         -- 0=暂停接收
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_name, task_type_filter)
);
```

**匹配规则**（`task_enqueue` 入队时执行）:
1. `to_agent` == subscription.agent_name
2. `task_type` GLOB subscription.task_type_filter（NULL filter → 全部通过）
3. `priority` <= subscription.priority_min
4. `title` 或 `description` 包含 keywords 中任一关键词（空 keywords → 全部通过）

**默认订阅**（系统初始化时自动创建）:

```sql
INSERT OR IGNORE INTO task_subscriptions (agent_name, task_type_filter, priority_min, keywords) VALUES
    ('pi_fixer',   'fix_*',      3, '["fix","memory","orphan","stale","gc","decay"]'),
    ('pi_fixer',   'gc_*',       3, '["cleanup","decay","zombie"]'),
    ('pi_builder', 'build_*',    3, '["build","implement","scaffold","refactor"]'),
    ('pi_builder', 'refactor_*', 3, '["decouple","module","optimize"]'),
    ('pi_reviewer','review_*',   3, '["review","audit","quality","trend"]'),
    ('pi_reviewer','investigate_*', 2, '["recurrence","trust","anomaly"]'),
    ('claude',     'audit_*',    1, '["architecture","coupling","security"]'),
    ('claude',     'investigate_*', 1, '["trust","drop","escalation"]'),
    ('claude',     NULL,         1, NULL);  -- Claude 兜底接收所有 S/A 级
```

### 2.3 `hunter_failure_log` — 失败记录表

```sql
CREATE TABLE hunter_failure_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    failure_type    TEXT NOT NULL,             -- timeout | rejected | abandoned | overreach
    trust_before    REAL,
    trust_after     REAL,
    penalty_applied REAL,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES task_queue(id)
);

CREATE INDEX idx_failure_agent ON hunter_failure_log(agent_name, occurred_at);
CREATE INDEX idx_failure_type  ON hunter_failure_log(agent_name, task_type, failure_type);
```

### 2.4 `metric_history` — 指标历史表

```sql
CREATE TABLE metric_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,               -- rejection_rate | worth_velocity | fix_recurrence | tag_cooccurrence
    metric_value REAL NOT NULL,
    window_start TEXT NOT NULL,               -- 统计周期起始
    window_end   TEXT NOT NULL,               -- 统计周期结束
    computed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_metric_name_time ON metric_history(metric_name, computed_at);
```

---

## 三、生命周期状态机

```
                    ┌──────────────┐
                    │   pending    │  ← 挂上委托板，等待揭榜
                    │  (待揭榜)     │
                    └──────┬───────┘
                           │ task_claim() 原子操作
                           │ WHERE status='pending' (先到先得)
                    ┌──────▼───────┐
                    │   claimed    │  ← 已揭榜，准备执行
                    │  (已认领)     │
                    └──────┬───────┘
                           │ 开始执行 + 首次心跳
                    ┌──────▼───────┐
           ┌────────│  executing   │  ← 每60s心跳 (task_heartbeat)
           │        │  (执行中)     │
           │        └──────┬───────┘
           │               │ task_complete()
           │        ┌──────▼───────┐
           │        │    done      │  ← 等待工会长老验收
           │        │  (已完成)     │    自动创建 verify_task 子委托给 Claude
           │        └──────┬───────┘
           │               │ task_verify()
           │        ┌──────▼───────┐      ┌──────────────┐
           │        │   verified   │      │  reassigned  │ ← 长老打回
           │        │  (已验收)     │      │  (打回重做)   │
           │        └──────────────┘      └──────┬───────┘
           │                                     │ 自动 task_enqueue 子委托
           │                                     └──→ pending (escalation_count++)
           │
           │  超时触发 (heartbeat_at + timeout_seconds < now):
           │  claimed 超时 → 释放回 pending，escalation_count++，惩罚 timeout
           │  executing 超时 → 释放回 pending，escalation_count++，惩罚 timeout
           │  escalation_count >= max_escalations → to_agent='claude'，priority=1
           │
           │  主动放弃:
           │  task_abandon() → pending，惩罚 abandoned
           └──────────────────────────────────────┘
```

**状态转换表**:

| 当前状态 | 触发动作 | 目标状态 | 副作用 |
|---------|---------|---------|--------|
| pending | task_claim (成功) | claimed | SSE: task:claimed |
| pending | task_claim (等级不足) | pending | 拒绝，返回原因 |
| claimed | 心跳超时 (daemon) | pending | escalation_count++, SSE: task:overdue, 惩罚 timeout |
| claimed | task_abandon | pending | 惩罚 abandoned, SSE: task:new |
| claimed | 开始执行 | executing | heartbeat_at 更新 |
| executing | task_complete | done | SSE: task:done, 自动创建验收子委托 |
| executing | 心跳超时 (daemon) | pending | escalation_count++, SSE: task:overdue, 惩罚 timeout |
| done | task_verify(accepted) | verified | SSE: task:verified, defense boost |
| done | task_verify(rejected) | reassigned | SSE: task:reassigned, 惩罚 rejected, 自动子委托 |
| reassigned | task_claim | claimed | 子委托被揭榜，循环继续 |
| * | escalation_count >= 3 | pending (to:claude, prio:1) | SSE: task:escalated |
| pending_review | task_verify(accepted) | pending | Claude 审批通过，正常入队 |

---

## 四、猎人等级系统

### 4.1 信任分 → 等级映射（实时计算，不存储）

```python
def trust_to_rank(trust_score: float) -> dict:
    """信任分 → 猎人等级。唯一真相源是 trust_score，等级是派生视图。"""
    if trust_score >= 0.80:
        return {"rank": "S", "title": "传奇猎人", "icon": "⭐"}
    if trust_score >= 0.65:
        return {"rank": "A", "title": "资深猎人", "icon": "🛡️"}
    if trust_score >= 0.50:
        return {"rank": "B", "title": "正式猎人", "icon": "⚔️"}
    if trust_score >= 0.35:
        return {"rank": "C", "title": "见习猎人", "icon": "🔰"}
    return {"rank": "D", "title": "降级猎人", "icon": "⛓️"}

def priority_to_rank(priority: int) -> str:
    return {1: "S", 2: "A", 3: "B", 4: "C"}.get(priority, "C")

def can_claim(agent_trust: float, task_priority: int) -> tuple[bool, str]:
    """检查 Agent 是否可以揭此优先级的委托"""
    agent_rank = trust_to_rank(agent_trust)
    required_rank = priority_to_rank(task_priority)
    rank_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    if rank_order[agent_rank["rank"]] > rank_order[required_rank]:
        return False, f"⚠️ 委托推荐{required_rank}级，你的等级为{agent_rank['rank']}级，建议申请援助"
    return True, "✅ 等级匹配，可揭榜"
```

### 4.2 优先级 ↔ 等级门槛对照表

| priority | 紧急度 | 推荐等级 | 所需信任分 | 谁可以接 |
|----------|--------|----------|-----------|---------|
| 1 | 🔴 紧急 | S | ≥ 0.80 | 仅 Claude |
| 2 | 🟠 优先 | A | ≥ 0.65 | Claude, Reviewer |
| 3 | 🟡 日常 | B | ≥ 0.50 | 所有正式猎人 |
| 4 | 🟢 低级 | C | ≥ 0.35 | 全员(含见习) |

### 4.3 等级应用场景

- **`task_enqueue`**: `priority` 字段同时定义紧急度和等级门槛，一字段两用
- **`task_claim`**: 检查等级匹配，跨级揭榜需 `force=true`（记录在案，失败有额外惩罚）
- **`task_inbox`**: 返回 Agent 等级、每项委托的推荐等级和匹配度提示
- **`hunter:rank_change` SSE 事件**: 信任分跨阈值时广播等级变动

---

## 五、MCP 工具签名（7 个）

### 5.1 `task_enqueue` — 挂委托

```
task_enqueue(
    task_type: str,           # fix_memory | close_orphan | review_code | audit_architecture | ...
    title: str,               # "修复 memory_id=m_xxx 的重复记忆集群"
    to_agent: str,            # pi_builder | pi_fixer | pi_reviewer | claude
    priority: int = 3,        # 1(S)~4(C)，同时定义紧急度和等级门槛
    from_agent: str = "daemon",
    from_trust_score: float = None,  # 委托人信任分（低于0.35需审批）
    description: str = "",
    payload: dict = None,
    domain: str = None,
    memory_id: str = None,
    principle_id: str = None,
    source_scan: str = None,
    parent_task_id: str = None,
    timeout_seconds: int = 300,
)
→ {
    "task_id": "t_20260701_0001",
    "status": "pending" | "pending_review",
    "sse_broadcast": true,
    "matched_subscribers": 2,
    "review_required": true/false
}
```

**入队前验证**:
- Daemon/Claude: 不受限制
- D 级猎人: 拒绝挂委托
- C 级猎人 + priority ≤ 2: 进入 `pending_review` (需 Claude 审批)
- 从 from_agent 非 daemon/claude 且 from_trust_score < 0.35: 进入 `pending_review`

### 5.2 `task_claim` — 揭榜

```
task_claim(
    agent_name: str,
    task_id: str,
    trust_score: float,
    force: bool = False,      # 越级强制揭榜
)
→ {
    "success": true/false,
    "reason": "...",
    "rank": {"rank": "B", "title": "正式猎人"},
    "task_priority": 2,
    "match": "✅ 等级匹配，揭榜成功" | "⚠️ 越级揭榜(已记录)"
}
```

**原子操作**: `UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=now(), heartbeat_at=now() WHERE id=? AND status='pending'`

### 5.3 `task_complete` — 交委托

```
task_complete(
    task_id: str,
    agent_name: str,
    result: str,
    artifacts: list = None,
)
→ {
    "success": true,
    "status": "done",
    "verification_task_id": "t_20260701_0002",  // 自动创建的验收子委托
    "waiting_for": "verification by claude"
}
```

**内部行为**:
1. 验证 `claimed_by == agent_name`
2. 更新 `status='done'`, `done_at=now()`, `result`
3. 如果 `to_agent != 'claude'`，自动创建验收子委托（task_type="verify_task", to_agent="claude", parent_task_id=task_id）

### 5.4 `task_verify` — 长老验收

```
task_verify(
    task_id: str,
    verdict: str,              # "accepted" | "rejected" | "reassigned"
    verified_by: str = "claude",
    comment: str = "",
    reassign_to_agent: str = None,  # reassigned 时指定新猎人
    reassign_reason: str = "",
)
→ {
    "success": true,
    "new_status": "verified" | "reassigned",
    "trust_adjustment": {
        "agent": "pi_builder",
        "delta": +0.02 | -0.03,
        "reason": "..."
    }
}
```

**内部行为**:
- `accepted`: status='verified' → `defense adjust +0.02` → SSE: task:verified
- `rejected`: status='reassigned', escalation_count++ → `defense adjust -0.03` → HunterPenaltyEngine.apply → 自动创建子委托 → SSE: task:reassigned
- `reassigned`: 同上 + `reassign_to_agent` 指定新目标 → 自动创建子委托

### 5.5 `task_inbox` — 查看委托板

```
task_inbox(
    agent_name: str,
    trust_score: float,
    filter_status: str = "pending",  # pending|my_active|pending_review|all
    limit: int = 20,
)
→ {
    "agent_name": "pi_builder",
    "rank": {"rank": "B", "title": "正式猎人", "icon": "⚔️"},
    "stats": {
        "my_active": 2,              // 我揭了但还没交的
        "available": 5,              // pending 且等级匹配
        "overdue": 1                 // 已超时的
    },
    "tasks": [
        {
            "id": "t_001",
            "task_type": "fix_memory",
            "title": "修复重复记忆集群",
            "priority": 2,
            "recommended_rank": "A",
            "status": "pending",
            "from_agent": "daemon",
            "created_at": "...",
            "match": "⚠️ 越级委托，建议申请S级猎人协助",
            "can_claim": false,
            "parent_task_id": null
        }
    ]
}
```

### 5.6 `task_heartbeat` — 心跳保活

```
task_heartbeat(
    task_id: str,
    agent_name: str,
)
→ {
    "success": true,
    "overdue": false,
    "next_heartbeat_in": 60
}
```

**内部行为**: `UPDATE task_queue SET heartbeat_at=datetime('now') WHERE id=? AND claimed_by=?`
返回 overdue=true 表示心跳超时，Agent 应立即检查状态。

### 5.7 `task_abandon` — 主动弃单

```
task_abandon(
    task_id: str,
    agent_name: str,
    reason: str,
)
→ {
    "success": true,
    "penalty": {
        "type": "abandoned",
        "trust_delta": -0.02,
        "trust_after": 0.58,
        "repeat_count": 2,
        "warning": "累计弃单2次，再弃3次将降级到D"
    }
}
```

**内部行为**:
1. 验证 `claimed_by == agent_name`
2. 调用 `HunterPenaltyEngine.apply_penalty(failure_type="abandoned")`
3. 释放委托: `UPDATE task_queue SET status='pending', claimed_by=NULL`
4. SSE broadcast task:new（通知其他猎人可接此委托）

---

## 六、任务失败惩罚体系

### 6.1 惩罚规则

| 失败类型 | 触发条件 | 基础惩罚 | 升级阈值 | 升级惩罚 | 升级动作 |
|---------|---------|---------|---------|---------|---------|
| timeout | 心跳超时 | -0.01 | 3次 | -0.03 | 触发信任分审查委托 |
| rejected | 长老打回 | -0.03 | 3次(同类型) | -0.05 | 禁接该类型7天 |
| abandoned | 主动弃单 | -0.02 | 5次 | -0.05 | 降级到D |
| overreach | 越级揭榜后失败 | -0.04 | 1次 | — | 锁定等级30天 |

### 6.2 惩罚执行流程

```
HunterPenaltyEngine.apply_penalty(agent_name, task_id, task_type, failure_type, current_trust):

1. 写 hunter_failure_log (记录失败)
2. defense(action="adjust", delta=base_penalty) (执行扣分)
3. 检查 repeat_count >= threshold? (累计触发?)
   → 是: defense adjust repeat_penalty + 创建审查委托给 Claude
4. 检查同类型被拒次数 (仅 rejected)
   → 是: 禁接该 task_type 7天
5. 检查等级是否跨阈值变动
   → 是: SSE broadcast hunter:rank_change
```

### 6.3 MCP 工具中的惩罚触发点

| 工具 | 触发场景 | failure_type | 执行者 |
|------|---------|-------------|--------|
| task_claim | 越级揭榜标记（失败时触发） | overreach | — |
| task_complete | 完成 → 被驳回 | rejected | task_verify 中触发 |
| task_heartbeat | 心跳超时 → daemon 检测 | timeout | Daemon scan_task_heartbeats |
| task_abandon | Agent 主动放弃 | abandoned | Agent 自己调用 |
| task_verify | 长老驳回 | rejected | 长老 Claude |

---

## 七、Daemon 升级 — 全域创新发现引擎

### 7.1 新增扫描器

#### scan_architecture_smells() — 架构坏味道

```python
检测维度:
  1. 循环依赖 — domain 关系图 A→B→C→A
  2. 上帝模块 — entity edge_count > 中位数 + 2σ (动态阈值)
  3. 散弹式修改 — 同一类 fix 涉及 >5 个不同文件
  4. (僵尸代码移至 scan_memory_decay)
```

**委托输出**:

| 检测项 | task_type | 目标 | 优先级 |
|--------|-----------|------|--------|
| 循环依赖 | audit_architecture | claude | A(2) |
| 上帝模块 | refactor_module | pi_builder | B(3) |
| 散弹修改 | audit_architecture | claude | A(2) |

#### scan_code_quality_trends() — 代码质量趋势

```python
检测维度:
  1. 复发率 — 同一 memory_id 24h内被 fix 两次+
  2. 审查拒绝率 — reassigned/total_verified 趋势 (7天窗口)
  3. 记忆池衰减速率 — worth 均值斜率 (7天窗口)
```

**依赖**: `metric_history` 表提供历史窗口数据

**委托输出**:

| 检测项 | task_type | 目标 | 优先级 |
|--------|-----------|------|--------|
| 复发问题 | investigate_recurrence | pi_reviewer | A/B(2-3) |
| 拒绝率>30% | review_quality_audit | claude | S/A(1-2) |
| 衰减加速 | optimize_gc_params | pi_fixer | A(2) |

#### scan_cross_module_coupling() — 跨模块耦合

```python
检测维度:
  1. 标签异常共现 — 历史不相关domain标签频繁共现 (zscore > 2.5)
  2. 桥接节点膨胀 — 跨域边数增长 > 5/周
  3. 隐式依赖 — memory.content 中引用但 entity_graph 中未注册的边
```

**隐式依赖实现**:
```python
def _detect_implicit_dependencies():
    # 1. 从 memories.content 提取所有 file:// 和 module:// 引用
    # 2. 从 entity_graph 获取所有已注册的边
    # 3. 取差集 → 隐式依赖
    # 4. 按频率排序，Top 10
    # 5. 7天内出现 >3次 → 挂委托
```

**委托输出**:

| 检测项 | task_type | 目标 | 优先级 |
|--------|-----------|------|--------|
| 意外耦合 | audit_coupling | claude | A(2) |
| 桥接膨胀 | refactor_decouple | pi_builder | B(3) |
| 隐式依赖 | audit_coupling | claude | B(3) |

#### scan_trust_anomalies() — 信任分深度异常

```python
检测维度:
  1. 信任分骤降 — 24h 下降 > 0.15
  2. 信任分停滞 — 14天波动 < 0.01
  3. 等级不对等协作 — S级和C级频繁协作同一任务
  4. 时间衰减积压 — 多个Agent同时进入衰减期
```

**委托输出**:

| 检测项 | task_type | 目标 | 优先级 |
|--------|-----------|------|--------|
| 信任骤降 | investigate_trust_drop | claude | S(1) |
| 信任停滞 | review_stagnant_trust | pi_reviewer | B(3) |

#### scan_memory_decay() — 记忆池健康（从架构扫描器独立）

```python
检测维度:
  1. 僵尸记忆 — tier='L3' + 30天未访问
  2. 记忆涌入 — 24h新增 > 动态阈值 (中位数+2σ)
  3. 记忆分布失衡 — 某domain占比 > 60%
```

**委托输出**:

| 检测项 | task_type | 目标 | 优先级 |
|--------|-----------|------|--------|
| 僵尸记忆 | gc_cleanup | pi_fixer | C(4) |
| 记忆涌入 | investigate_memory_influx | claude | A(2) |
| 分布失衡 | rebalance_domains | pi_builder | B(3) |

### 7.2 自适应节流

```python
class AdaptiveThrottle:
    """连续空扫描 → 间隔翻倍 (最多8x)；命中一次 → 恢复初始间隔"""
    
    def __init__(self, base_interval: int):
        self.base = base_interval
        self.current = base_interval
        self.empty_streak = 0
    
    def on_empty_scan(self):
        self.empty_streak += 1
        if self.empty_streak >= 3:
            self.current = min(self.current * 2, self.base * 8)
    
    def on_hit_scan(self):
        self.empty_streak = 0
        self.current = self.base
    
    @property
    def interval(self) -> int:
        return self.current
```

### 7.3 完整扫描器 → 委托映射表

```python
SCANNER_TASK_MAP = {
    "scan_architecture_smells": [
        {"detect": "循环依赖", "task_type": "audit_architecture", "to_agent": "claude", "priority": 2},
        {"detect": "上帝模块", "task_type": "refactor_module", "to_agent": "pi_builder", "priority": 3},
        {"detect": "散弹修改", "task_type": "audit_architecture", "to_agent": "claude", "priority": 2},
    ],
    "scan_code_quality_trends": [
        {"detect": "复发问题", "task_type": "investigate_recurrence", "to_agent": "pi_reviewer", "priority": 2},
        {"detect": "拒绝率上升", "task_type": "review_quality_audit", "to_agent": "claude", "priority": 1},
        {"detect": "衰减加速", "task_type": "optimize_gc_params", "to_agent": "pi_fixer", "priority": 2},
    ],
    "scan_cross_module_coupling": [
        {"detect": "意外耦合", "task_type": "audit_coupling", "to_agent": "claude", "priority": 2},
        {"detect": "桥接膨胀", "task_type": "refactor_decouple", "to_agent": "pi_builder", "priority": 3},
        {"detect": "隐式依赖", "task_type": "audit_coupling", "to_agent": "claude", "priority": 3},
    ],
    "scan_trust_anomalies": [
        {"detect": "信任骤降", "task_type": "investigate_trust_drop", "to_agent": "claude", "priority": 1},
        {"detect": "信任停滞", "task_type": "review_stagnant_trust", "to_agent": "pi_reviewer", "priority": 3},
    ],
    "scan_memory_decay": [
        {"detect": "僵尸记忆", "task_type": "gc_cleanup", "to_agent": "pi_fixer", "priority": 4},
        {"detect": "记忆涌入", "task_type": "investigate_memory_influx", "to_agent": "claude", "priority": 2},
        {"detect": "分布失衡", "task_type": "rebalance_domains", "to_agent": "pi_builder", "priority": 3},
    ],
    # 现有扫描器（升级为 task_enqueue 输出）
    "scan_innovation_opportunities": [...],  # 保留，输出方式从 dispatch_fix_task 改为 task_enqueue
    "scan_orphan_steps": [...],
    "scan_redo_queue": [...],
    "scan_unclosed_issues": [...],
    "scan_duplicate_clusters": [...],
    "scan_stale_worth": [...],
    "scan_tier_migration": [...],
    "scan_category_stuck": [...],
    "scan_llm_classify": [...],
}
```

### 7.4 调度优先级

```
优先级 S (信任分骤降，立即检查):  scan_trust_anomalies

优先级 A (每 SAFETY_NET_INTERVAL): scan_innovation_opportunities
                                   scan_code_quality_trends
                                   scan_cross_module_coupling
                                   scan_architecture_smells

优先级 B (每 SAFETY_NET_INTERVAL): scan_memory_decay
                                   scan_duplicate_clusters
                                   scan_stale_worth
                                   scan_tier_migration
                                   scan_category_stuck
                                   scan_redo_queue
                                   scan_orphan_steps
                                   scan_unclosed_issues
                                   scan_llm_classify
                                   scan_task_heartbeats (超时检测)
```

---

## 八、SSE 推送 + 轮询兜底双通道

### 8.1 架构模型

```
                     SSE 广播层                    轮询兜底层
                   "传令使喊一嗓子"              "回来看委托板"
─────────────────────────────────────────────────────────────────────
可靠性              尽力而为，不保证送达            SQLite 持久化，绝对可靠
延迟                毫秒级 (实时)                  30s 间隔 (准实时)
离线行为            Agent 不在线 → 静默丢失        Agent 启动时拉取全部积压
资源消耗            极低 (事件驱动)                周期性 SQL 查询
适用场景            在线 Agent 实时感知            离线恢复 + 心跳保活
失败模式            SSE 连接断开                   数据库挂了 (系统已不可用)
```

### 8.2 SSE 事件类型

| 事件 | 触发时机 | 推送目标 | 数据 |
|------|---------|---------|------|
| `task:new` | task_enqueue | to_agent + 订阅匹配者 | task_id, task_type, priority, to_agent, title |
| `task:claimed` | task_claim | from_agent (委托人) | task_id, claimed_by |
| `task:done` | task_complete | claude (长老) | task_id, claimed_by, result 摘要 |
| `task:verified` | task_verify(accepted) | claimed_by (猎人) | task_id, verdict |
| `task:reassigned` | task_verify(rejected) | claimed_by (猎人) | task_id, reason |
| `task:overdue` | 心跳超时 | claimed_by + claude | task_id, escalation_count |
| `task:escalated` | escalation_count >= 3 | claude | task_id, history |
| `hunter:rank_change` | 信任分跨阈值 | agent + claude | agent, old_rank, new_rank, reason |

### 8.3 SSE 广播格式

```json
{
  "event": "task:new",
  "data": {
    "task_id": "t_20260701_0001",
    "task_type": "fix_memory",
    "priority": 2,
    "to_agent": "pi_fixer",
    "title": "修复重复记忆集群",
    "from_agent": "daemon"
  }
}
```

**设计原则**: payload 仅携带最小信息（id + type + priority + target），接收方需要详情时调用 `task_inbox` 或 `task_claim` 获取。

### 8.4 Agent 双通道实现

```python
class HunterAgent:
    def __init__(self, name):
        self.name = name
        self.sse_connected = True
        self._seen_task_ids: set = set()    # 去重集合
    
    async def on_sse_event(self, event):
        """SSE 实时通道"""
        if event.type == "task:new":
            if event.data["to_agent"] == self.name:
                await self._evaluate_and_claim(event.data)
        elif event.type == "task:reassigned":
            if event.data.get("claimed_by") == self.name:
                print(f"🔄 委托被打回: {event.data['task_id']}")
        elif event.type == "task:verified":
            if event.data.get("claimed_by") == self.name:
                print(f"✅ 委托验收通过: {event.data['task_id']}")
        elif event.type == "hunter:rank_change":
            if event.data.get("agent") == self.name:
                print(f"⬆️ 等级变动: {event.data['old_rank']} → {event.data['new_rank']}")
    
    async def poll_inbox(self):
        """轮询兜底 — 30s 一次"""
        result = await task_inbox(
            agent_name=self.name,
            trust_score=self.trust_score,
            filter_status="pending"
        )
        for task in result["tasks"]:
            tid = task["id"]
            if tid not in self._seen_task_ids:
                self._seen_task_ids.add(tid)
                if task["can_claim"]:
                    await task_claim(self.name, tid, self.trust_score)
    
    async def on_sse_disconnect(self):
        """SSE 断开 → 切换到纯轮询模式，每 5s 尝试重连"""
        self.sse_connected = False
        while not self.sse_connected:
            await self.poll_inbox()
            await asyncio.sleep(30)
    
    async def on_startup(self):
        """启动时拉取全部积压"""
        # 1. 检查是否有未完成的委托
        active = await task_inbox(self.name, self.trust_score, filter_status="my_active")
        if active["stats"]["overdue"] > 0:
            print(f"⚠️ 你有 {active['stats']['overdue']} 个超时委托!")
        
        # 2. 拉取可接的新委托
        result = await task_inbox(self.name, self.trust_score, filter_status="pending")
        for task in result["tasks"]:
            self._seen_task_ids.add(task["id"])
```

### 8.5 MCP Server 端事件总线

```python
class TaskEventBus:
    """公会传令使 — SSE 客户端管理 + 事件广播"""
    
    def __init__(self):
        self._clients: dict[str, list] = {}
    
    async def register(self, agent_name: str, connection):
        if agent_name not in self._clients:
            self._clients[agent_name] = []
        self._clients[agent_name].append(connection)
    
    async def unregister(self, agent_name: str, connection):
        if agent_name in self._clients:
            self._clients[agent_name].remove(connection)
    
    async def broadcast(self, event_type: str, data: dict, to_agents: list[str]):
        for agent in to_agents:
            if agent in self._clients:
                for conn in self._clients[agent]:
                    try:
                        await conn.send({"event": event_type, "data": data})
                    except Exception:
                        await self.unregister(agent, conn)
        
        # 关键事件永远抄送 Claude
        critical = {"task:overdue", "task:escalated", "hunter:rank_change"}
        if event_type in critical and "claude" not in to_agents:
            await self.broadcast(event_type, data, ["claude"])
```

---

## 九、完整端到端流程

```
Daemon scan_code_quality_trends()
  │ 发现: 修复复发率 40% (超阈值)
  │
  ▼
task_enqueue(
  task_type="investigate_recurrence",
  to_agent="pi_reviewer",
  priority=2
)
  │
  ├─→ 1. INSERT INTO task_queue  ← 持久化
  ├─→ 2. SELECT task_subscriptions WHERE ...  ← 匹配订阅
  ├─→ 3. SSE broadcast event:task:new
  │       to: pi_reviewer → 📨 "新A级委托"
  │       cc: claude       → 📨 "新A级委托 (cc)"
  └─→ 4. return {task_id, sse_broadcast:true, matched_subscribers:2}

pi_reviewer 收到 SSE → 评估等级匹配
  │ ✅ A级猎人接A级委托 → task_claim("t_xxx", trust=0.72)
  ├─→ SSE: task:claimed → daemon 知道已被揭榜
  │
  ▼ 执行调查...
  │ task_heartbeat ("t_xxx", "pi_reviewer") 每60s
  │
  ▼ task_complete("t_xxx", "复发原因: Ollama 间歇超时")
  ├─→ 自动创建验收子委托给 Claude
  ├─→ SSE: task:done → Claude 收到验收通知
  │
  ▼ Claude task_verify("t_xxx", verdict="accepted")
  ├─→ defense adjust +0.02 → pi_reviewer 信任分 0.72→0.74
  ├─→ SSE: task:verified → pi_reviewer 收到通过通知
  ├─→ 信任分仍在A级 → 不触发 hunter:rank_change
  │
  ▼ 闭环完成 ✅
```

## 十、实施范围总结

### 新增文件
| 文件 | 用途 |
|------|------|
| `plastic_promise/mcp/tools/task_queue.py` | 7 个 task_* MCP 工具实现 |
| `plastic_promise/core/task_event_bus.py` | SSE 事件总线 |
| `plastic_promise/core/hunter_penalty.py` | 猎人失败惩罚引擎 |
| `plastic_promise/cron/scan_architecture.py` | 架构坏味道扫描器 |
| `plastic_promise/cron/scan_quality_trends.py` | 质量趋势扫描器 |
| `plastic_promise/cron/scan_coupling.py` | 跨模块耦合扫描器 |
| `plastic_promise/cron/scan_trust.py` | 信任分异常扫描器 |
| `plastic_promise/cron/scan_memory_decay.py` | 记忆衰减扫描器 |

### 修改文件
| 文件 | 改动 |
|------|------|
| `daemons/maintenance_daemon.py` | 集成5个新扫描器 + 自适应节流 + task_enqueue 替代 dispatch_fix_task |
| `plastic_promise/mcp/server.py` | 注册 TaskEventBus + 7个新 MCP 工具 |
| `plastic_promise/core/constants.py` | 新增优先级/等级/失败类型常量 |
| `plastic_promise/defense/trust_store.py` | 新增 hunter_failure_log 操作 |

### 新增数据库表（4张）
1. `task_queue` — 委托板
2. `task_subscriptions` — 订阅表
3. `hunter_failure_log` — 失败记录
4. `metric_history` — 指标历史

### 新增 MCP 工具（7个）
1. `task_enqueue` — 挂委托
2. `task_claim` — 揭榜
3. `task_complete` — 交委托
4. `task_verify` — 长老验收
5. `task_inbox` — 查看委托板
6. `task_heartbeat` — 心跳保活
7. `task_abandon` — 主动弃单

### 新增扫描器（5个）
1. `scan_architecture_smells` — 架构坏味道
2. `scan_code_quality_trends` — 代码质量趋势
3. `scan_cross_module_coupling` — 跨模块耦合
4. `scan_trust_anomalies` — 信任分深度异常
5. `scan_memory_decay` — 记忆池健康独立扫描

---

## 十一、过渡策略

- **标签→委托板迁移**: `task_enqueue` 内部同时调用旧的 `_store_tagged_memory` 写入标签记忆（向后兼容），过渡期 6 个月
- **现有扫描器**: 保留，逐步从 `dispatch_fix_task` 输出改为 `task_enqueue` 输出
- **信任分体系**: 无变更，等级系统是信任分的纯视图映射
