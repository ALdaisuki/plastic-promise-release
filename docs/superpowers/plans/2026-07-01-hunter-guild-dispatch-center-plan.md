# 全域创新调度中心 — 猎人公会委托系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 maintenance_daemon 升级为全域创新调度中心 — SQLite 任务队列 + MCP 工具 + SSE 推送 + 5 个新扫描器。

**Architecture:** 分三层实施：基础委托板（Phase 1 - task_queue 表 + 7 MCP 工具 + 等级系统）、发现引擎（Phase 2 - 5 扫描器 + 惩罚引擎）、感知层（Phase 3 - SSE 事件总线 + 双通道）。每层独立可测试，后一层依赖前一层。

**Tech Stack:** Python 3, SQLite (plastic_memory.db), MCP SSE transport, httpx (daemon→MCP), 现有 TrustStore/ContextEngine

**Design Doc:** `docs/superpowers/specs/2026-07-01-hunter-guild-dispatch-center-design.md`

## Global Constraints

- All new tables go into the existing `plastic_memory.db` SQLite database (PLASTIC_DB_PATH env var)
- MCP tool handlers follow the existing pattern in `plastic_promise/mcp/tools/*.py`: `async def handle_*(engine, args) -> list[TextContent]`
- Tool registration in `plastic_promise/mcp/server.py` follows existing `TOOL_HANDLERS` dict pattern
- Test files go in `tests/`, follow existing naming convention `test_*.py`
- Trust score is the single source of truth; hunter rank is always derived via `trust_to_rank()`, never stored
- Priority 1=S, 2=A, 3=B, 4=C; lower number = higher urgency
- Daemon communicates with MCP via `httpx` async client to `MCP_URL/notify`
- **PRE-IMPLEMENTATION CHECK**: Verify TrustManager persistence is working (TrustStore writes to SQLite and survives restart). Without this, `defense(action="adjust")` calls in task_verify/task_abandon will silently fail.
- All log writes (hunter_failure_log) MUST happen BEFORE TrustManager calls — logs are durable even if TM fails
- Use `match_subscribers()` from `task_subscriptions.py` for all subscription counting; never inline SQL for matching

---

## File Structure Map

```
Phase 1 — Foundation:
  CREATE  plastic_promise/core/hunter_rank.py          # trust_to_rank, priority_to_rank, can_claim
  CREATE  plastic_promise/core/task_queue_schema.py    # SQL DDL + migration helpers
  CREATE  plastic_promise/mcp/tools/task_queue.py      # 7 task_* MCP tool handlers
  CREATE  tests/test_hunter_rank.py                    # Unit tests for rank mapping
  CREATE  tests/test_task_queue.py                     # Integration tests for task lifecycle
  MODIFY  plastic_promise/core/constants.py            # Add TASK_PRIORITY, RANK_THRESHOLDS
  MODIFY  plastic_promise/defense/trust_store.py       # Add task_queue table creation
  MODIFY  plastic_promise/mcp/server.py                # Register 7 new tools

Phase 2 — Discovery Engine:
  CREATE  plastic_promise/core/hunter_penalty.py       # HunterPenaltyEngine
  CREATE  plastic_promise/cron/scan_architecture.py    # Architecture smell scanner
  CREATE  plastic_promise/cron/scan_quality_trends.py  # Quality trends scanner
  CREATE  plastic_promise/cron/scan_coupling.py        # Cross-module coupling scanner
  CREATE  plastic_promise/cron/scan_trust.py           # Trust anomaly scanner
  CREATE  plastic_promise/cron/scan_memory_decay.py    # Memory decay scanner
  CREATE  tests/test_hunter_penalty.py                 # Penalty engine tests
  CREATE  tests/test_scanners.py                       # Scanner tests
  MODIFY  daemons/maintenance_daemon.py                # Integrate 5 scanners + throttle
  MODIFY  plastic_promise/defense/trust_store.py       # Add metric_history + hunter_failure_log tables

Phase 3 — Perception Layer:
  CREATE  plastic_promise/core/task_event_bus.py       # TaskEventBus (SSE broadcaster)
  CREATE  plastic_promise/core/task_subscriptions.py   # Subscription matching
  CREATE  tests/test_task_event_bus.py                 # Event bus tests
  CREATE  tests/test_subscriptions.py                  # Subscription matching tests
  MODIFY  plastic_promise/mcp/tools/task_queue.py      # Add SSE broadcast to task_enqueue + task_claim + task_verify
  MODIFY  plastic_promise/mcp/server.py                # Register TaskEventBus, handle SSE clients
```

---

## Phase 1: Foundation — 委托板 + 工具 + 等级

### Task 1: Hunter Rank System

**Files:**
- Create: `plastic_promise/core/hunter_rank.py`
- Create: `tests/test_hunter_rank.py`
- Modify: `plastic_promise/core/constants.py`

**Interfaces:**
- Consumes: `trust_score` (float from TrustStore)
- Produces: `trust_to_rank(trust_score: float) -> dict`, `priority_to_rank(priority: int) -> str`, `can_claim(agent_trust: float, task_priority: int) -> tuple[bool, str]`
- Constants exported: `RANK_THRESHOLDS = {"S": 0.80, "A": 0.65, "B": 0.50, "C": 0.35, "D": 0.0}`

- [ ] **Step 1: Add rank constants to core/constants.py**

```python
# Append to plastic_promise/core/constants.py

# ── Hunter Guild Rank Thresholds ──────────────────────────────
RANK_THRESHOLDS = {
    "S": 0.80,
    "A": 0.65,
    "B": 0.50,
    "C": 0.35,
    "D": 0.00,
}

RANK_TITLES = {
    "S": "传奇猎人",
    "A": "资深猎人",
    "B": "正式猎人",
    "C": "见习猎人",
    "D": "降级猎人",
}

RANK_ICONS = {
    "S": "⭐",
    "A": "🛡️",
    "B": "⚔️",
    "C": "🔰",
    "D": "⛓️",
}

TASK_PRIORITY = {
    1: {"label": "S级·紧急", "rank": "S", "urgency": "🔴"},
    2: {"label": "A级·优先", "rank": "A", "urgency": "🟠"},
    3: {"label": "B级·日常", "rank": "B", "urgency": "🟡"},
    4: {"label": "C级·低级", "rank": "C", "urgency": "🟢"},
}

RANK_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
```

- [ ] **Step 2: Write failing test for trust_to_rank**

```python
# tests/test_hunter_rank.py
import pytest
from plastic_promise.core.hunter_rank import trust_to_rank, priority_to_rank, can_claim

def test_trust_to_rank_s():
    assert trust_to_rank(0.85) == {"rank": "S", "title": "传奇猎人", "icon": "⭐"}

def test_trust_to_rank_a():
    assert trust_to_rank(0.72) == {"rank": "A", "title": "资深猎人", "icon": "🛡️"}

def test_trust_to_rank_b():
    assert trust_to_rank(0.55) == {"rank": "B", "title": "正式猎人", "icon": "⚔️"}

def test_trust_to_rank_c():
    assert trust_to_rank(0.40) == {"rank": "C", "title": "见习猎人", "icon": "🔰"}

def test_trust_to_rank_d():
    assert trust_to_rank(0.10) == {"rank": "D", "title": "降级猎人", "icon": "⛓️"}

def test_trust_to_rank_boundaries():
    # Exact thresholds: S >= 0.80, A >= 0.65
    assert trust_to_rank(0.80)["rank"] == "S"
    assert trust_to_rank(0.799)["rank"] == "A"

def test_priority_to_rank():
    assert priority_to_rank(1) == "S"
    assert priority_to_rank(2) == "A"
    assert priority_to_rank(3) == "B"
    assert priority_to_rank(4) == "C"

def test_can_claim_match():
    ok, msg = can_claim(0.72, 2)  # A级猎人, A级委托
    assert ok is True
    assert "✅" in msg

def test_can_claim_overreach():
    ok, msg = can_claim(0.55, 2)  # B级猎人, A级委托
    assert ok is False
    assert "⚠️" in msg

def test_can_claim_s_rank_anything():
    ok, msg = can_claim(0.90, 4)  # S级猎人接C级委托
    assert ok is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_hunter_rank.py -v`
Expected: FAIL with "No module named 'plastic_promise.core.hunter_rank'"

- [ ] **Step 4: Write implementation**

```python
# plastic_promise/core/hunter_rank.py
"""Hunter Rank System — trust score → rank mapping (derived view, never stored)."""

from plastic_promise.core.constants import RANK_THRESHOLDS, RANK_TITLES, RANK_ICONS, RANK_ORDER


def trust_to_rank(trust_score: float) -> dict:
    """Derive hunter rank from trust score. Rank is a view, not stored."""
    for rank in ("S", "A", "B", "C", "D"):
        if trust_score >= RANK_THRESHOLDS[rank]:
            return {"rank": rank, "title": RANK_TITLES[rank], "icon": RANK_ICONS[rank]}
    return {"rank": "D", "title": RANK_TITLES["D"], "icon": RANK_ICONS["D"]}


def priority_to_rank(priority: int) -> str:
    """Map task priority to the minimum rank required to claim it."""
    mapping = {1: "S", 2: "A", 3: "B", 4: "C"}
    return mapping.get(priority, "C")


def can_claim(agent_trust: float, task_priority: int) -> tuple:
    """Check if an agent can claim a task of the given priority.

    Returns (ok: bool, message: str).
    """
    agent_rank = trust_to_rank(agent_trust)
    required_rank = priority_to_rank(task_priority)
    if RANK_ORDER[agent_rank["rank"]] > RANK_ORDER[required_rank]:
        return False, (
            f"⚠️ 委托推荐{required_rank}级，你的等级为{agent_rank['rank']}级"
            f"（{agent_rank['title']}），建议申请援助"
        )
    return True, "✅ 等级匹配，可揭榜"
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_hunter_rank.py -v`
Expected: ALL 9 tests PASS

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/hunter_rank.py tests/test_hunter_rank.py plastic_promise/core/constants.py
git commit -m "feat: hunter rank system — trust_to_rank, priority_to_rank, can_claim"
```

---

### Task 2: task_queue + task_subscriptions + hunter_failure_log Tables

**Files:**
- Create: `plastic_promise/core/task_queue_schema.py`
- Modify: `plastic_promise/defense/trust_store.py`

**Interfaces:**
- Consumes: `TrustStore._conn` (SQLite connection)
- Produces: `ensure_task_tables(conn)` — idempotent schema creation, called by TrustStore._create_tables()

- [ ] **Step 1: Write schema module**

```python
# plastic_promise/core/task_queue_schema.py
"""SQL DDL for hunter guild task queue system.

All tables live in the existing plastic_memory.db alongside trust_scores.
Schema creation is idempotent (IF NOT EXISTS).
"""

TASK_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS task_queue (
    id              TEXT PRIMARY KEY,
    task_type       TEXT NOT NULL,
    priority        INTEGER DEFAULT 3,
    status          TEXT DEFAULT 'pending',
    title           TEXT NOT NULL,
    description     TEXT,
    payload         TEXT,
    from_agent      TEXT DEFAULT 'daemon',
    to_agent        TEXT NOT NULL,
    domain          TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT,
    heartbeat_at    TEXT,
    done_at         TEXT,
    verified_at     TEXT,
    verified_by     TEXT,
    verify_verdict  TEXT,
    result          TEXT,
    escalation_count INTEGER DEFAULT 0,
    max_escalations  INTEGER DEFAULT 3,
    last_escalation_at TEXT,
    timeout_seconds  INTEGER DEFAULT 300,
    memory_id       TEXT,
    principle_id    TEXT,
    source_scan     TEXT,
    parent_task_id  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_to_agent ON task_queue(to_agent);
CREATE INDEX IF NOT EXISTS idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_task_parent ON task_queue(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_claimed ON task_queue(claimed_by, status);
"""

TASK_SUBSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS task_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    task_type_filter TEXT,
    priority_min    INTEGER DEFAULT 3,
    keywords        TEXT,
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_name, task_type_filter)
);
"""

HUNTER_FAILURE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS hunter_failure_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    failure_type    TEXT NOT NULL,
    trust_before    REAL,
    trust_after     REAL,
    penalty_applied REAL,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES task_queue(id)
);

CREATE INDEX IF NOT EXISTS idx_failure_agent ON hunter_failure_log(agent_name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_failure_type ON hunter_failure_log(agent_name, task_type, failure_type);
"""

METRIC_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS metric_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name  TEXT NOT NULL,
    metric_value REAL NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    computed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_metric_name_time ON metric_history(metric_name, computed_at);
"""

DEFAULT_SUBSCRIPTIONS = [
    ("pi_fixer",    "fix_*",       3, '["fix","memory","orphan","stale","gc","decay"]'),
    ("pi_fixer",    "gc_*",        3, '["cleanup","decay","zombie"]'),
    ("pi_builder",  "build_*",     3, '["build","implement","scaffold","refactor"]'),
    ("pi_builder",  "refactor_*",  3, '["decouple","module","optimize"]'),
    ("pi_reviewer", "review_*",    3, '["review","audit","quality","trend"]'),
    ("pi_reviewer", "investigate_*", 2, '["recurrence","trust","anomaly"]'),
    ("claude",      "audit_*",     1, '["architecture","coupling","security"]'),
    ("claude",      "investigate_*", 1, '["trust","drop","escalation"]'),
    ("claude",      None,          1, None),
]


def ensure_task_tables(conn):
    """Create all task queue tables if they don't exist. Idempotent."""
    conn.executescript(TASK_QUEUE_DDL)
    conn.executescript(TASK_SUBSCRIPTIONS_DDL)
    conn.executescript(HUNTER_FAILURE_LOG_DDL)
    conn.executescript(METRIC_HISTORY_DDL)

    # Seed default subscriptions
    import json
    for agent, filt, prio, keywords in DEFAULT_SUBSCRIPTIONS:
        conn.execute(
            "INSERT OR IGNORE INTO task_subscriptions "
            "(agent_name, task_type_filter, priority_min, keywords) "
            "VALUES (?, ?, ?, ?)",
            (agent, filt, prio, keywords),
        )
    conn.commit()
```

- [ ] **Step 2: Modify TrustStore to create task tables**

In `plastic_promise/defense/trust_store.py`, add to `_create_tables()`:

```python
# Add at end of TrustStore._create_tables():
def _create_tables(self) -> None:
    # ... existing trust_scores + trust_history creation ...
    
    # Hunter Guild task queue tables
    from plastic_promise.core.task_queue_schema import ensure_task_tables
    ensure_task_tables(self._conn)
```

- [ ] **Step 3: Verify tables exist**

Run: `python -c "from plastic_promise.defense.trust_store import TrustStore; t = TrustStore(); c = t._conn; print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])"`
Expected: output includes `task_queue`, `task_subscriptions`, `hunter_failure_log`, `metric_history`

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/task_queue_schema.py plastic_promise/defense/trust_store.py
git commit -m "feat: task_queue + task_subscriptions + hunter_failure_log + metric_history tables"
```

---

### Task 3: task_enqueue MCP Tool

**Files:**
- Create: `plastic_promise/mcp/tools/task_queue.py`
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Consumes: `engine` (ContextEngine), `args` dict
- Produces: `handle_task_enqueue(engine, args) -> list[TextContent]`

- [ ] **Step 1: Write failing test**

```python
# tests/test_task_queue.py
import json
import sqlite3
import pytest
from plastic_promise.core.task_queue_schema import ensure_task_tables
from plastic_promise.mcp.tools.task_queue import handle_task_enqueue, _generate_task_id

@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    ensure_task_tables(conn)
    return conn

def test_generate_task_id():
    tid = _generate_task_id()
    assert tid.startswith("t_")
    assert len(tid) > 4

def test_task_enqueue_basic(db_conn, monkeypatch):
    # Patch the engine to return our in-memory connection
    class MockEngine:
        pass
    
    engine = MockEngine()
    
    result = handle_task_enqueue(engine, {
        "task_type": "fix_memory",
        "title": "测试委托: 修复重复记忆",
        "to_agent": "pi_fixer",
        "priority": 3,
        "from_agent": "daemon",
        "description": "单元测试创建的委托",
        "source_scan": "test",
    })
    
    text = json.loads(result[0].text)
    assert text["status"] == "pending"
    assert text["task_id"].startswith("t_")
    assert text["sse_broadcast"] is False  # No SSE in Phase 1

def test_task_enqueue_d_rank_rejected(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    result = handle_task_enqueue(engine, {
        "task_type": "fix_memory",
        "title": "D级猎人尝试挂委托",
        "to_agent": "pi_fixer",
        "from_agent": "unknown_agent",
        "from_trust_score": 0.20,  # D级
        "priority": 3,
    })
    text = json.loads(result[0].text)
    assert text["status"] == "rejected"
    assert "降级猎人" in text["reason"]
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/test_task_queue.py::test_task_enqueue_basic -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write task_queue.py with handle_task_enqueue**

```python
# plastic_promise/mcp/tools/task_queue.py
"""MCP Task Queue tools — Hunter Guild dispatch board.

Tools: task_enqueue, task_claim, task_complete, task_verify,
       task_inbox, task_heartbeat, task_abandon
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.hunter_rank import trust_to_rank, can_claim
from plastic_promise.core.constants import RANK_ORDER


def _get_db_path() -> str:
    return os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")


def _generate_task_id() -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"t_{datetime.now().strftime('%Y%m%d%H%M%S')}_{suffix}"


def _get_conn():
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ═══════════════════════════════════════════════════════════════
# task_enqueue
# ═══════════════════════════════════════════════════════════════

async def handle_task_enqueue(engine: Any, args: dict) -> list[TextContent]:
    """Enqueue a task onto the guild board.

    Validates the submitter's trust score and enforces rank-based
    submission rules.
    """
    from_agent = args.get("from_agent", "daemon")
    from_trust_score = args.get("from_trust_score", None)
    priority = args.get("priority", 3)

    # ── Submitter validation ──────────────────────────────
    if from_agent not in ("daemon", "claude") and from_trust_score is not None:
        rank = trust_to_rank(from_trust_score)
        if rank["rank"] == "D":
            return [TextContent(type="text", text=json.dumps({
                "status": "rejected",
                "reason": f"降级猎人（{rank['title']}）无权挂委托，信任分={from_trust_score:.2f}",
            }, ensure_ascii=False))]
        if rank["rank"] == "C" and priority <= 2:
            # Needs Claude review
            task_id = _generate_task_id()
            conn = _get_conn()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, domain, memory_id, principle_id, "
                "source_scan, parent_task_id, timeout_seconds, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, args["task_type"], args["title"], args["to_agent"],
                    priority, from_agent,
                    args.get("description", ""), args.get("domain"),
                    args.get("memory_id"), args.get("principle_id"),
                    args.get("source_scan"), args.get("parent_task_id"),
                    args.get("timeout_seconds", 300),
                    json.dumps(args.get("payload")) if args.get("payload") else None,
                ))
            conn.commit()
            conn.close()
            # Issue 1 fix: Auto-notify Claude by creating a review sub-task
            review_task_id = _generate_task_id()
            conn2 = _get_conn()
            conn2.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, 'notify_review', ?, 'claude', 2, 'system', 'pending', ?, ?, ?)",
                (review_task_id,
                 f"[审批] {args['title']}",
                 f"C级猎人 {from_agent}（{rank['title']}）挂委托需审批。"
                 f"原始委托: {task_id}",
                 task_id,
                 json.dumps({"original_task_id": task_id, "submitter": from_agent,
                             "submitter_rank": rank["rank"]}),
                ))
            conn2.commit()
            conn2.close()
            return [TextContent(type="text", text=json.dumps({
                "task_id": task_id,
                "status": "pending_review",
                "sse_broadcast": False,
                "matched_subscribers": 1,
                "review_required": True,
                "review_task_id": review_task_id,
                "reason": f"C级猎人（{rank['title']}）挂A/B级委托需Claude审批",
            }, ensure_ascii=False))]

    # ── Normal enqueue ─────────────────────────────────────
    task_id = _generate_task_id()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
        "from_agent, status, description, domain, memory_id, principle_id, "
        "source_scan, parent_task_id, timeout_seconds, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, args["task_type"], args["title"], args["to_agent"],
            priority, from_agent,
            args.get("description", ""), args.get("domain"),
            args.get("memory_id"), args.get("principle_id"),
            args.get("source_scan"), args.get("parent_task_id"),
            args.get("timeout_seconds", 300),
            json.dumps(args.get("payload")) if args.get("payload") else None,
        ))
    conn.commit()

    # Issue 2 fix: use match_subscribers() for accurate counting (keywords respected)
    try:
        from plastic_promise.core.task_subscriptions import match_subscribers
        matched = len(match_subscribers({
            "task_type": args["task_type"],
            "to_agent": args["to_agent"],
            "priority": priority,
            "title": args["title"],
            "description": args.get("description", ""),
        }))
    except ImportError:
        matched = 0  # Phase 3 not yet implemented
    conn.close()

    return [TextContent(type="text", text=json.dumps({
        "task_id": task_id,
        "status": "pending",
        "sse_broadcast": False,  # Phase 3
        "matched_subscribers": matched,
        "review_required": False,
    }, ensure_ascii=False))]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_task_queue.py::test_task_enqueue_basic tests/test_task_queue.py::test_task_enqueue_d_rank_rejected -v`
Expected: 2 PASS

- [ ] **Step 5: Register tool in MCP server**

In `plastic_promise/mcp/server.py`, add import and registration:

```python
# In imports section:
from plastic_promise.mcp.tools.task_queue import (
    handle_task_enqueue,
    handle_task_claim,
    handle_task_complete,
    handle_task_verify,
    handle_task_inbox,
    handle_task_heartbeat,
    handle_task_abandon,
)

# In TOOL_HANDLERS dict or equivalent:
TOOL_HANDLERS = {
    # ...existing...
    "task_enqueue":   handle_task_enqueue,
    "task_claim":     handle_task_claim,
    "task_complete":  handle_task_complete,
    "task_verify":    handle_task_verify,
    "task_inbox":     handle_task_inbox,
    "task_heartbeat": handle_task_heartbeat,
    "task_abandon":   handle_task_abandon,
}
```

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py plastic_promise/mcp/server.py tests/test_task_queue.py
git commit -m "feat: task_enqueue MCP tool with submitter rank validation"
```

---

### Task 4: task_claim MCP Tool

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py`
- Modify: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing test**

```python
# Append to tests/test_task_queue.py

def test_task_claim_success(db_conn, monkeypatch):
    # First enqueue a task
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "待揭榜委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]

    # Now claim it
    r2 = handle_task_claim(engine, {
        "agent_name": "pi_fixer",
        "task_id": task_id,
        "trust_score": 0.60,
    })
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert "✅" in data["match"]
    assert data["rank"]["rank"] == "B"

def test_task_claim_rank_mismatch(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "audit_architecture", "title": "A级委托",
        "to_agent": "claude", "priority": 2,  # priority=2 → rank A
    })
    task_id = json.loads(r[0].text)["task_id"]

    r2 = handle_task_claim(engine, {
        "agent_name": "pi_fixer",
        "task_id": task_id,
        "trust_score": 0.55,  # B级接A级 → 越级
    })
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "⚠️" in data["match"]

def test_task_claim_double_prevented(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "单次委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]

    # First claim succeeds
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})
    # Second claim must fail (already claimed)
    r2 = handle_task_claim(engine, {"agent_name": "pi_reviewer", "task_id": task_id, "trust_score": 0.70})
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "已被揭榜" in data["reason"]
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/test_task_queue.py::test_task_claim_success -v`
Expected: FAIL (NameError: handle_task_claim not defined)

- [ ] **Step 3: Implement handle_task_claim**

```python
# Append to plastic_promise/mcp/tools/task_queue.py

async def handle_task_claim(engine: Any, args: dict) -> list[TextContent]:
    """Claim a task from the guild board. Atomic — first-come-first-served."""
    agent_name = args["agent_name"]
    task_id = args["task_id"]
    trust_score = args["trust_score"]
    force = args.get("force", False)

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Read task
    task = conn.execute(
        "SELECT * FROM task_queue WHERE id = ?", (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在"
        }, ensure_ascii=False))]

    if task["status"] != "pending":
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": f"委托已被揭榜 (status={task['status']})"
        }, ensure_ascii=False))]

    # Rank check
    ok, msg = can_claim(trust_score, task["priority"])
    if not ok and not force:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "等级不足",
            "rank": rank_info, "task_priority": task["priority"], "match": msg,
        }, ensure_ascii=False))]

    if not ok and force:
        msg = f"⚠️ 越级揭榜(已记录): {msg}"

    # Atomic claim
    now = datetime.now().isoformat()
    result = conn.execute(
        "UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=?, "
        "heartbeat_at=?, updated_at=? WHERE id=? AND status='pending'",
        (agent_name, now, now, now, task_id)
    )
    conn.commit()

    if result.rowcount == 0:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "揭榜失败: 委托已被其他猎人抢先揭榜"
        }, ensure_ascii=False))]

    conn.close()
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "rank": rank_info,
        "task_priority": task["priority"],
        "match": msg,
        "force_claimed": force and not ok,
    }, ensure_ascii=False))]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_task_queue.py::test_task_claim_success tests/test_task_queue.py::test_task_claim_rank_mismatch tests/test_task_queue.py::test_task_claim_double_prevented -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py tests/test_task_queue.py
git commit -m "feat: task_claim MCP tool — atomic claim with rank check"
```

---

### Task 5: task_complete + task_verify MCP Tools

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py`
- Modify: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_task_queue.py

def test_task_complete_creates_verify_subtask(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "可完成委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})

    r2 = handle_task_complete(engine, {
        "task_id": task_id,
        "agent_name": "pi_fixer",
        "result": "修复完成：移除3条重复记忆",
    })
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["status"] == "done"
    assert data["verification_task_id"] is not None  # Auto-created verify task for Claude

def test_task_complete_wrong_agent(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "我的委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})

    r2 = handle_task_complete(engine, {
        "task_id": task_id,
        "agent_name": "pi_builder",  # Different agent!
        "result": "不是我揭的",
    })
    data = json.loads(r2[0].text)
    assert data["success"] is False

def test_task_verify_accepted_boosts_trust(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "验收测试委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})
    handle_task_complete(engine, {"task_id": task_id, "agent_name": "pi_fixer", "result": "done"})

    r2 = handle_task_verify(engine, {
        "task_id": task_id,
        "verdict": "accepted",
        "verified_by": "claude",
    })
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "verified"
    assert data["trust_adjustment"]["delta"] == 0.02

def test_task_verify_rejected_deducts(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {
        "task_type": "fix_memory", "title": "打回测试委托",
        "to_agent": "pi_fixer", "priority": 3,
    })
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})
    handle_task_complete(engine, {"task_id": task_id, "agent_name": "pi_fixer", "result": "done"})

    r2 = handle_task_verify(engine, {
        "task_id": task_id,
        "verdict": "rejected",
        "verified_by": "claude",
        "comment": "修复不彻底",
    })
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "reassigned"
    assert data["trust_adjustment"]["delta"] == -0.03
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_task_queue.py::test_task_complete_creates_verify_subtask -v`
Expected: FAIL

- [ ] **Step 3: Implement handle_task_complete + handle_task_verify**

```python
# Append to plastic_promise/mcp/tools/task_queue.py

async def handle_task_complete(engine: Any, args: dict) -> list[TextContent]:
    """Submit a completed task for verification."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]
    result_text = args["result"]
    artifacts = args.get("artifacts", [])

    conn = _get_conn()
    task = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在"
        }, ensure_ascii=False))]

    if task["claimed_by"] != agent_name:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": f"委托由 {task['claimed_by']} 揭榜，不是你"
        }, ensure_ascii=False))]

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE task_queue SET status='done', done_at=?, result=?, updated_at=? "
        "WHERE id=?",
        (now, result_text, now, task_id)
    )
    conn.commit()

    # Auto-create verification subtask for Claude (unless task is already for Claude)
    verify_task_id = None
    if task["to_agent"] != "claude":
        verify_task_id = _generate_task_id()
        conn.execute(
            "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
            "from_agent, status, description, parent_task_id, payload) "
            "VALUES (?, 'verify_task', ?, 'claude', ?, 'system', 'pending', ?, ?, ?)",
            (
                verify_task_id,
                f"验收委托: {task['title']}",
                task["priority"],
                f"猎人 {agent_name} 已完成委托 {task_id}，请验收。\n"
                f"结果: {result_text[:500]}",
                task_id,
                json.dumps({
                    "original_task_id": task_id,
                    "original_agent": agent_name,
                    "original_result": result_text[:1000],
                    "artifacts": artifacts,
                }),
            ))
        conn.commit()

    conn.close()
    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "status": "done",
        "verification_task_id": verify_task_id,
        "waiting_for": "verification by claude" if verify_task_id else "self-verified",
    }, ensure_ascii=False))]


async def handle_task_verify(engine: Any, args: dict) -> list[TextContent]:
    """Verify a completed task (长老验收)."""
    task_id = args["task_id"]
    verdict = args["verdict"]  # "accepted" | "rejected" | "reassigned"
    verified_by = args.get("verified_by", "claude")
    comment = args.get("comment", "")

    conn = _get_conn()
    task = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在"
        }, ensure_ascii=False))]

    now = datetime.now().isoformat()

    if verdict == "accepted":
        conn.execute(
            "UPDATE task_queue SET status='verified', verified_at=?, verified_by=?, "
            "verify_verdict='accepted', updated_at=? WHERE id=?",
            (now, verified_by, now, task_id)
        )
        conn.commit()

        # Trust boost for the hunter
        delta = 0.02
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager
            tm = TrustManager()
            tm.boost(delta, f"委托验收通过: {task_id}")
        except Exception:
            pass

        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": True, "new_status": "verified",
            "trust_adjustment": {"agent": task["claimed_by"], "delta": delta,
                                 "reason": "委托验收通过"},
        }, ensure_ascii=False))]

    elif verdict in ("rejected", "reassigned"):
        new_esc = task["escalation_count"] + 1
        conn.execute(
            "UPDATE task_queue SET status='reassigned', verified_at=?, verified_by=?, "
            "verify_verdict=?, escalation_count=?, last_escalation_at=?, updated_at=? "
            "WHERE id=?",
            (now, verified_by, verdict, new_esc, now, now, task_id)
        )
        conn.commit()

        # Trust penalty
        delta = -0.03
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager
            tm = TrustManager()
            tm.decay(delta, f"委托被打回: {task_id} — {comment[:100]}")
        except Exception:
            pass

        # Auto-create reassigned subtask
        reassign_to = args.get("reassign_to_agent", task["to_agent"])
        new_task_id = None
        if new_esc >= task["max_escalations"]:
            # Escalate to Claude
            new_task_id = _generate_task_id()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, 'claude', 1, ?, 'pending', ?, ?, ?)",
                (new_task_id, task["task_type"],
                 f"[S级升级] {task['title']}", verified_by,
                 f"升级原因: {new_esc}次失败/超时, 长老{vtask_id,
                 json.dumps({
                     **(json.loads(task["payload"]) if task["payload"] else {}),
                     "original_claimed_by": task["claimed_by"],
                     "verdict": verdict,
                     "comment": comment,
                 })))     else:
            new_task_id = _generate_task_id()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, priority, "
                "from_agent, status, description, parent_task_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (new_task_id, task["task_type"],
                 f"[重派] {task['title']}", reassign_to,
                 max(1, task["priority"] - 1),  # Upgrade priority
                 verified_by,
                 f"长老{verified_by}打回重做。原因: {comment[:200]}",
                 task_id, task["payload"]))
        conn.commit()
        conn.close()

        return [TextContent(type="text", text=json.dumps({
            "success": True, "new_status": "reassigned",
            "new_task_id": new_task_id,
            "escalation_count": new_esc,
            "escalated_to_claude": new_esc >= task["max_escalations"],
            "trust_adjustment": {"agent": task["claimed_by"], "delta": delta,
                                 "reason": f"委托被打回: {comment[:80]}"},
        }, ensure_ascii=False))]

    conn.close()
    return [TextContent(type="text", text=json.dumps({
        "success": False, "reason": f"无效的verdict: {verdict}"
    }, ensure_ascii=False))]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_task_queue.py::test_task_complete_creates_verify_subtask tests/test_task_queue.py::test_task_complete_wrong_agent tests/test_task_queue.py::test_task_verify_accepted_boosts_trust tests/test_task_queue.py::test_task_verify_rejected_deducts -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py tests/test_task_queue.py
git commit -m "feat: task_complete + task_verify — auto verify subtask + trust adjustment"
```

---

### Task 6: task_inbox + task_heartbeat + task_abandon MCP Tools

**Files:**
- Modify: `plastic_promise/mcp/tools/task_queue.py`
- Modify: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_task_queue.py

def test_task_inbox_default_pending(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    # Enqueue 2 tasks for pi_fixer
    handle_task_enqueue(engine, {"task_type": "fix_memory", "title": "任务A", "to_agent": "pi_fixer", "priority": 3})
    handle_task_enqueue(engine, {"task_type": "gc_cleanup", "title": "任务B", "to_agent": "pi_fixer", "priority": 4})

    r = handle_task_inbox(engine, {"agent_name": "pi_fixer", "trust_score": 0.60})
    data = json.loads(r[0].text)
    assert data["agent_name"] == "pi_fixer"
    assert data["rank"]["rank"] == "B"
    assert data["stats"]["available"] >= 2

def test_task_inbox_rank_match_display(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {"task_type": "audit_architecture", "title": "A级任务", "to_agent": "claude", "priority": 2})
    task_id = json.loads(r[0].text)["task_id"]

    r2 = handle_task_inbox(engine, {"agent_name": "pi_fixer", "trust_score": 0.55, "filter_status": "pending"})
    data = json.loads(r2[0].text)
    task = next(t for t in data["tasks"] if t["id"] == task_id)
    assert task["can_claim"] is False
    assert "⚠️" in task["match"]

def test_task_heartbeat(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {"task_type": "fix_memory", "title": "心跳测试", "to_agent": "pi_fixer", "priority": 3})
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})

    r2 = handle_task_heartbeat(engine, {"task_id": task_id, "agent_name": "pi_fixer"})
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["overdue"] is False

def test_task_abandon(db_conn, monkeypatch):
    engine = type('obj', (object,), {})()
    r = handle_task_enqueue(engine, {"task_type": "fix_memory", "title": "弃单测试", "to_agent": "pi_fixer", "priority": 3})
    task_id = json.loads(r[0].text)["task_id"]
    handle_task_claim(engine, {"agent_name": "pi_fixer", "task_id": task_id, "trust_score": 0.60})

    r2 = handle_task_abandon(engine, {"task_id": task_id, "agent_name": "pi_fixer", "reason": "太难了"})
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["penalty"]["type"] == "abandoned"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_task_queue.py::test_task_inbox_default_pending -v`
Expected: FAIL

- [ ] **Step 3: Implement handle_task_inbox, handle_task_heartbeat, handle_task_abandon**

```python
# Append to plastic_promise/mcp/tools/task_queue.py

async def handle_task_inbox(engine: Any, args: dict) -> list[TextContent]:
    """View the guild board — default shows only claimable tasks."""
    agent_name = args["agent_name"]
    trust_score = args["trust_score"]
    filter_status = args.get("filter_status", "pending")
    limit = args.get("limit", 20)

    rank_info = trust_to_rank(trust_score)
    conn = _get_conn()

    # Stats
    my_active = conn.execute(
        "SELECT COUNT(*) FROM task_queue "
        "WHERE claimed_by=? AND status IN ('claimed','executing')",
        (agent_name,)
    ).fetchone()[0]

    available = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE status='pending'"
    ).fetchone()[0]

    # Task list
    if filter_status == "my_active":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE claimed_by=? "
            "AND status IN ('claimed','executing','done') "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (agent_name, limit)
        ).fetchall()
    elif filter_status == "pending_review":
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status='pending_review' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,)
        ).fetchall()
    elif filter_status == "all":
        rows = conn.execute(
            "SELECT * FROM task_queue ORDER BY priority ASC, created_at ASC LIMIT ?",
            (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status='pending' "
            "ORDER BY priority ASC, created_at ASC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()

    tasks = []
    for row in rows:
        ok, msg = can_claim(trust_score, row["priority"])
        tasks.append({
            "id": row["id"],
            "task_type": row["task_type"],
            "title": row["title"],
            "priority": row["priority"],
            "recommended_rank": {1: "S", 2: "A", 3: "B", 4: "C"}.get(row["priority"], "C"),
            "status": row["status"],
            "from_agent": row["from_agent"],
            "created_at": row["created_at"],
            "match": msg,
            "can_claim": ok and row["status"] == "pending",
            "parent_task_id": row["parent_task_id"] or None,
        })

    return [TextContent(type="text", text=json.dumps({
        "agent_name": agent_name,
        "rank": rank_info,
        "stats": {
            "my_active": my_active,
            "available": available,
        },
        "tasks": tasks,
    }, ensure_ascii=False))]


async def handle_task_heartbeat(engine: Any, args: dict) -> list[TextContent]:
    """Send heartbeat for a claimed task."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]

    conn = _get_conn()
    task = conn.execute("SELECT * FROM task_queue WHERE id=? AND claimed_by=?", 
                        (task_id, agent_name)).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在或非你揭榜"
        }, ensure_ascii=False))]

    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE task_queue SET heartbeat_at=?, updated_at=? WHERE id=?",
        (now, now, task_id)
    )
    conn.commit()

    # Check if overdue
    overdue = False
    if task["heartbeat_at"] and task["timeout_seconds"]:
        try:
            last_hb = datetime.fromisoformat(task["heartbeat_at"])
            elapsed = (datetime.now() - last_hb).total_seconds()
            if elapsed > task["timeout_seconds"]:
                overdue = True
        except (ValueError, TypeError):
            pass

    conn.close()
    return [TextContent(type="text", text=json.dumps({
        "success": True, "overdue": overdue, "next_heartbeat_in": 60,
    }, ensure_ascii=False))]


async def handle_task_abandon(engine: Any, args: dict) -> list[TextContent]:
    """Abandon a claimed task — trust penalty applies."""
    task_id = args["task_id"]
    agent_name = args["agent_name"]
    reason = args.get("reason", "")

    conn = _get_conn()
    task = conn.execute(
        "SELECT * FROM task_queue WHERE id=? AND claimed_by=? "
        "AND status IN ('claimed','executing')",
        (task_id, agent_name)
    ).fetchone()
    if not task:
        conn.close()
        return [TextContent(type="text", text=json.dumps({
            "success": False, "reason": "委托不存在或非你揭榜或已提交"
        }, ensure_ascii=False))]

    # Penalty: -0.02 base
    delta = -0.02
    try:
        from plastic_promise.defense.soul_enforcer import TrustManager
        tm = TrustManager()
        current = tm.get(agent_name)
    except Exception:
        current = 0.50  # fallback if TrustManager unavailable

    # Issue 4 fix: Log failure FIRST (durable even if TrustManager fails)
    conn.execute(
        "INSERT INTO hunter_failure_log "
        "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
        "VALUES (?, ?, ?, 'abandoned', ?, ?, ?)",
        (agent_name, task_id, task["task_type"], current, current + delta, delta)
    )
    conn.commit()

    try:
        tm.decay(delta, f"主动弃单: {task_id} — {reason[:80]}")
    except Exception:
        pass

    # Release task back to pending
    conn.execute(
        "UPDATE task_queue SET status='pending', claimed_by=NULL, claimed_at=NULL, "
        "heartbeat_at=NULL, updated_at=? WHERE id=?",
        (datetime.now().isoformat(), task_id)
    )
    conn.commit()

    # Count repeat abandons
    abandon_count = conn.execute(
        "SELECT COUNT(*) FROM hunter_failure_log "
        "WHERE agent_name=? AND failure_type='abandoned'",
        (agent_name,)
    ).fetchone()[0]
    conn.close()

    return [TextContent(type="text", text=json.dumps({
        "success": True,
        "penalty": {
            "type": "abandoned",
            "trust_delta": delta,
            "repeat_count": abandon_count,
            "warning": f"累计弃单{abandon_count}次，再弃{5-abandon_count}次将降级到D" if abandon_count < 5
                       else "已触发降级审查",
        },
    }, ensure_ascii=False))]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_task_queue.py::test_task_inbox_default_pending tests/test_task_queue.py::test_task_inbox_rank_match_display tests/test_task_queue.py::test_task_heartbeat tests/test_task_queue.py::test_task_abandon -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/task_queue.py tests/test_task_queue.py
git commit -m "feat: task_inbox + task_heartbeat + task_abandon — complete Phase 1 MCP tools"
```

---

## Phase 2: Discovery Engine — 5 Scanners + Penalty Engine

### Task 7: HunterPenaltyEngine

**Files:**
- Create: `plastic_promise/core/hunter_penalty.py`
- Create: `tests/test_hunter_penalty.py`

**Interfaces:**
- Consumes: `defense(action="adjust")`, `hunter_failure_log` table, `TrustManager`
- Produces: `HunterPenaltyEngine.apply_penalty(agent_name, task_id, task_type, failure_type, current_trust) -> dict`

- [ ] **Step 1: Write failing test**

```python
# tests/test_hunter_penalty.py
import pytest
import sqlite3
from plastic_promise.core.task_queue_schema import ensure_task_tables
from plastic_promise.core.hunter_penalty import HunterPenaltyEngine

@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    ensure_task_tables(conn)
    return conn

def test_penalty_timeout():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "timeout", 1)
    assert result["base_penalty"] == -0.01
    assert result["upgrade_triggered"] is False

def test_penalty_timeout_upgrade_on_third():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "timeout", 3)
    assert result["base_penalty"] == -0.01
    assert result["upgrade_triggered"] is True
    assert result["upgrade_penalty"] == -0.03

def test_penalty_abandoned():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_fixer", "abandoned", 5)
    assert result["upgrade_triggered"] is True
    assert result["action"] == "demote_to_D"

def test_penalty_overreach():
    engine = HunterPenaltyEngine()
    result = engine.compute_penalty("pi_builder", "overreach", 1)
    assert result["action"] == "lock_rank_30d"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_hunter_penalty.py -v`
Expected: FAIL

- [ ] **Step 3: Implement HunterPenaltyEngine**

```python
# plastic_promise/core/hunter_penalty.py
"""Hunter Penalty Engine — failure consequence system."""

import sqlite3
import os
from datetime import datetime

PENALTY_RULES = {
    "timeout": {
        "base_penalty": -0.01,
        "repeat_threshold": 3,
        "repeat_penalty": -0.03,
        "repeat_action": "trust_review",
        "description": "心跳超时，委托释放回委托板",
    },
    "rejected": {
        "base_penalty": -0.03,
        "same_type_threshold": 3,
        "same_type_penalty": -0.05,
        "same_type_action": "ban_type_7d",
        "description": "长老验收不通过，委托被打回",
    },
    "abandoned": {
        "base_penalty": -0.02,
        "repeat_threshold": 5,
        "repeat_penalty": -0.05,
        "repeat_action": "demote_to_D",
        "description": "主动放弃委托",
    },
    "overreach": {
        "base_penalty": -0.04,
        "repeat_threshold": 1,
        "repeat_penalty": 0,
        "repeat_action": "lock_rank_30d",
        "description": "越级揭榜后失败",
    },
}


class HunterPenaltyEngine:
    """Compute and apply penalties for task failures."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
        self._db_path = os.path.abspath(db_path)

    def compute_penalty(self, agent_name: str, failure_type: str, 
                        repeat_count: int, same_type_count: int = 0) -> dict:
        """Compute penalty without applying it. Pure function for testability."""
        rule = PENALTY_RULES[failure_type]
        result = {
            "failure_type": failure_type,
            "base_penalty": rule["base_penalty"],
            "repeat_count": repeat_count,
            "upgrade_triggered": False,
            "upgrade_penalty": 0,
            "action": None,
        }

        if repeat_count >= rule["repeat_threshold"]:
            result["upgrade_triggered"] = True
            result["upgrade_penalty"] = rule["repeat_penalty"]
            result["action"] = rule["repeat_action"]

        if failure_type == "rejected" and same_type_count >= rule["same_type_threshold"]:
            result["action"] = rule["same_type_action"]

        return result

    def count_failures(self, agent_name: str, failure_type: str,
                       window_days: int = 30) -> int:
        """Count failures of a given type in the time window."""
        conn = sqlite3.connect(self._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE agent_name=? AND failure_type=? "
            "AND occurred_at >= datetime('now', ?)",
            (agent_name, failure_type, f"-{window_days} days")
        ).fetchone()[0]
        conn.close()
        return count

    def count_same_type_failures(self, agent_name: str, task_type: str,
                                 window_days: int = 30) -> int:
        """Count rejected failures for the same task_type."""
        conn = sqlite3.connect(self._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM hunter_failure_log "
            "WHERE agent_name=? AND failure_type='rejected' AND task_type=? "
            "AND occurred_at >= datetime('now', ?)",
            (agent_name, task_type, f"-{window_days} days")
        ).fetchone()[0]
        conn.close()
        return count

    async def apply_penalty(self, agent_name: str, task_id: str,
                            task_type: str, failure_type: str,
                            current_trust: float) -> dict:
        """Apply penalty: log + trust adjust + check upgrades."""
        repeat_count = self.count_failures(agent_name, failure_type) + 1
        same_type_count = self.count_same_type_failures(agent_name, task_type)
        penalty = self.compute_penalty(agent_name, failure_type, repeat_count, same_type_count)

        new_trust = current_trust + penalty["base_penalty"]
        if penalty["upgrade_triggered"]:
            new_trust += penalty["upgrade_penalty"]

        # Log
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT INTO hunter_failure_log "
            "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_name, task_id, task_type, failure_type,
             current_trust, new_trust,
             penalty["base_penalty"] + penalty["upgrade_penalty"])
        )
        conn.commit()
        conn.close()

        # Apply trust adjustment
        try:
            from plastic_promise.defense.soul_enforcer import TrustManager
            tm = TrustManager()
            tm.decay(penalty["base_penalty"], 
                     f"{failure_type}: {task_id}")
            if penalty["upgrade_triggered"]:
                tm.decay(penalty["upgrade_penalty"],
                         f"{failure_type}_upgrade (x{repeat_count}): {task_id}")
        except Exception:
            pass

        return {
            "penalty_applied": penalty["base_penalty"] + penalty["upgrade_penalty"],
            "trust_before": current_trust,
            "trust_after": new_trust,
            "repeat_count": repeat_count,
            "actions_triggered": [penalty["action"]] if penalty["action"] else [],
        }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_hunter_penalty.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/hunter_penalty.py tests/test_hunter_penalty.py
git commit -m "feat: HunterPenaltyEngine — failure penalty rules + upgrade escalation"
```

---

### Task 8: Five Discovery Scanners

**Files:**
- Create: `plastic_promise/cron/scan_architecture.py`
- Create: `plastic_promise/cron/scan_quality_trends.py`
- Create: `plastic_promise/cron/scan_coupling.py`
- Create: `plastic_promise/cron/scan_trust.py`
- Create: `plastic_promise/cron/scan_memory_decay.py`
- Create: `tests/test_scanners.py`

- [ ] **Step 1: Write failing test for scan_memory_decay**

```python
# tests/test_scanners.py
import pytest
from plastic_promise.cron.scan_memory_decay import scan_memory_decay

@pytest.mark.asyncio
async def test_scan_memory_decay_detects_zombies(monkeypatch):
    class MockEngine:
        pass
    
    # Mock the MCP call to prevent actual HTTP
    async def mock_enqueue(*args, **kwargs):
        return [type('obj', (object,), {"text": '{"task_id":"t_test","status":"pending"}'})()]
    
    monkeypatch.setattr(
        "plastic_promise.cron.scan_memory_decay.handle_task_enqueue",
        mock_enqueue
    )
    
    result = await scan_memory_decay(MockEngine())
    assert result is not None  # Should return scan result dict
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_scanners.py::test_scan_memory_decay_detects_zombies -v`
Expected: FAIL

- [ ] **Step 3: Implement scan_memory_decay (simplest scanner first)**

```python
# plastic_promise/cron/scan_memory_decay.py
"""Memory pool health scanner — zombie memories, influx, distribution imbalance."""

import sqlite3
import os
from datetime import datetime, timedelta


async def scan_memory_decay(engine) -> dict:
    """Scan memory pool for decay signals:
    1. Zombie memories (L3 + 30d inactive)
    2. Memory influx (24h spike)
    3. Domain imbalance (>60%)
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    findings = []

    # 1. Zombie detection: L3 tier, 30+ days no access
    thirty_days = (datetime.now() - timedelta(days=30)).isoformat()
    zombies = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE tier='L3' "
        "AND (last_accessed IS NULL OR last_accessed < ?)",
        (thirty_days,)
    ).fetchone()[0]

    if zombies > 5:
        findings.append({
            "type": "zombie_memories",
            "count": zombies,
            "task_type": "gc_cleanup",
            "to_agent": "pi_fixer",
            "priority": 4,
            "title": f"僵尸记忆清理: {zombies} 条L3记忆超30天未访问",
        })

    # 2. Memory influx: count new memories in last 24h
    yesterday = (datetime.now() - timedelta(hours=24)).isoformat()
    influx = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE created_at > ?", (yesterday,)
    ).fetchone()[0]

    # Dynamic threshold: median + 2*std of daily counts over 7 days
    daily_counts = conn.execute(
        "SELECT DATE(created_at) as d, COUNT(*) as cnt "
        "FROM memories WHERE created_at > datetime('now', '-7 days') "
        "GROUP BY d ORDER BY cnt"
    ).fetchall()
    if len(daily_counts) >= 3:
        counts = [r[1] for r in daily_counts]
        counts.sort()
        median = counts[len(counts) // 2]
        mean = sum(counts) / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)
        std = variance ** 0.5
        threshold = median + 2 * std
        if influx > threshold and influx > 10:
            findings.append({
                "type": "memory_influx",
                "count": influx,
                "threshold": round(threshold, 1),
                "task_type": "investigate_memory_influx",
                "to_agent": "claude",
                "priority": 2,
                "title": f"记忆涌入异常: 24h新增{influx}条 (阈值={threshold:.0f})",
            })

    # 3. Domain imbalance (Issue 5 fix: query memories table directly,
    #    not domain_stats which may not exist)
    domain_counts = conn.execute(
        "SELECT domain, COUNT(*) as cnt FROM memories "
        "WHERE domain IS NOT NULL AND domain != '' "
        "GROUP BY domain"
    ).fetchall()
    if domain_counts:
        total = sum(r[1] for r in domain_counts)
        for domain, cnt in domain_counts:
            ratio = cnt / total if total > 0 else 0
            if ratio > 0.6:
                findings.append({
                    "type": "domain_imbalance",
                    "domain": domain,
                    "ratio": round(ratio, 2),
                    "task_type": "rebalance_domains",
                    "to_agent": "pi_builder",
                    "priority": 3,
                    "title": f"记忆分布失衡: {domain} 占比 {ratio:.0%}",
                })

    conn.close()

    # Dispatch findings
    from plastic_promise.mcp.tools.task_queue import handle_task_enqueue
    dispatched = 0
    for f in findings:
        await handle_task_enqueue(engine, {
            "task_type": f["task_type"],
            "title": f["title"],
            "to_agent": f["to_agent"],
            "priority": f["priority"],
            "source_scan": "scan_memory_decay",
            "payload": f,
        })
        dispatched += 1

    return {"scanner": "scan_memory_decay", "findings": len(findings), "dispatched": dispatched}
```

- [ ] **Step 4: Implement remaining 4 scanners**

For brevity in the plan, each scanner follows the same pattern:
1. Query SQLite for signals
2. Apply dynamic thresholds (median + 2σ)
3. Dispatch findings via `handle_task_enqueue`
4. Return `{"scanner": name, "findings": N, "dispatched": N}`

Implement `scan_architecture.py`, `scan_quality_trends.py`, `scan_coupling.py`, `scan_trust.py`
following the design doc Section 7.1 patterns with full detection logic.

- [ ] **Step 5: Run scanner tests**

Run: `pytest tests/test_scanners.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/cron/scan_memory_decay.py plastic_promise/cron/scan_architecture.py plastic_promise/cron/scan_quality_trends.py plastic_promise/cron/scan_coupling.py plastic_promise/cron/scan_trust.py tests/test_scanners.py
git commit -m "feat: 5 discovery scanners — architecture, quality, coupling, trust, memory_decay"
```

---

### Task 9: Integrate Scanners into Daemon

**Files:**
- Modify: `daemons/maintenance_daemon.py`

- [ ] **Step 1: Add adaptive throttle class**

```python
# Insert into daemons/maintenance_daemon.py

class AdaptiveThrottle:
    """Continuous empty scans → double interval (max 8x). Hit → reset."""
    def __init__(self, base_seconds: int):
        self.base = base_seconds
        self.current = base_seconds
        self.empty_streak = 0

    def on_empty(self):
        self.empty_streak += 1
        if self.empty_streak >= 3:
            self.current = min(self.current * 2, self.base * 8)

    def on_hit(self):
        self.empty_streak = 0
        self.current = self.base

    @property
    def should_run(self) -> bool:
        return True  # Always run, caller manages timing
```

- [ ] **Step 2: Add scanner imports and registration**

```python
# Add to imports in daemon:
from plastic_promise.cron.scan_architecture import scan_architecture_smells
from plastic_promise.cron.scan_quality_trends import scan_code_quality_trends
from plastic_promise.cron.scan_coupling import scan_cross_module_coupling
from plastic_promise.cron.scan_trust import scan_trust_anomalies
from plastic_promise.cron.scan_memory_decay import scan_memory_decay

# Add throttles dict at module level:
_scanner_throttles = {
    "scan_architecture_smells": AdaptiveThrottle(600),
    "scan_code_quality_trends": AdaptiveThrottle(600),
    "scan_cross_module_coupling": AdaptiveThrottle(600),
    "scan_trust_anomalies": AdaptiveThrottle(300),
    "scan_memory_decay": AdaptiveThrottle(600),
}
```

- [ ] **Step 3: Add scanner execution to main loop**

```python
# In main(), in the safety_net block, add before existing scans:

# Priority A: trust anomalies (check more frequently)
if tick % max(1, _scanner_throttles["scan_trust_anomalies"].current // 10) == 0:
    try:
        result = await scan_trust_anomalies()
        if result["findings"] > 0:
            _scanner_throttles["scan_trust_anomalies"].on_hit()
        else:
            _scanner_throttles["scan_trust_anomalies"].on_empty()
    except Exception:
        pass

# Priority B: remaining scanners on SAFETY_NET_INTERVAL
if tick % safety_net_threshold == 0:
    for name, scanner in [
        ("scan_architecture_smells", scan_architecture_smells),
        ("scan_code_quality_trends", scan_code_quality_trends),
        ("scan_cross_module_coupling", scan_cross_module_coupling),
        ("scan_memory_decay", scan_memory_decay),
    ]:
        throttle = _scanner_throttles[name]
        if tick % max(1, throttle.current // 10) == 0:
            try:
                result = await scanner()
                if result["findings"] > 0:
                    throttle.on_hit()
                else:
                    throttle.on_empty()
            except Exception:
                pass

    # ... existing scans continue here ...
```

- [ ] **Step 4: Add scan_task_heartbeats to daemon**

```python
async def scan_task_heartbeats():
    """Check all claimed/executing tasks for heartbeat timeout."""
    conn = sqlite3.connect(DB_PATH)
    overdue = conn.execute("""
        SELECT id, claimed_by, to_agent, escalation_count, timeout_seconds
        FROM task_queue
        WHERE status IN ('claimed','executing')
        AND datetime(heartbeat_at, '+' || timeout_seconds || ' seconds') < datetime('now')
    """).fetchall()

    for task_id, claimed_by, to_agent, esc_count, timeout_sec in overdue:
        if esc_count + 1 >= 3:
            conn.execute(
                "UPDATE task_queue SET status='pending', claimed_by=NULL, "
                "to_agent='claude', priority=1, "
                "escalation_count=escalation_count+1, "
                "last_escalation_at=datetime('now') WHERE id=?",
                (task_id,)
            )
        else:
            conn.execute(
                "UPDATE task_queue SET status='pending', claimed_by=NULL, "
                "escalation_count=escalation_count+1, "
                "last_escalation_at=datetime('now') WHERE id=?",
                (task_id,)
            )
        conn.commit()
        print(f"  [HEARTBEAT] task {task_id[:20]}... overdue → released "
              f"(escalation={esc_count+1})")

        # Apply timeout penalty
        try:
            from plastic_promise.core.hunter_penalty import HunterPenaltyEngine
            from plastic_promise.defense.soul_enforcer import TrustManager
            tm = TrustManager()
            current = tm.get(claimed_by)
            engine = HunterPenaltyEngine()
            await engine.apply_penalty(claimed_by, task_id, "unknown",
                                       "timeout", current)
        except Exception:
            pass
    conn.close()
```

- [ ] **Step 5: Commit**

```bash
git add daemons/maintenance_daemon.py
git commit -m "feat: integrate 5 scanners + adaptive throttle + heartbeat monitor into daemon"
```

---

## Phase 3: Perception Layer — SSE + Subscriptions

### Task 10: TaskEventBus + Subscription Matching

**Files:**
- Create: `plastic_promise/core/task_event_bus.py`
- Create: `plastic_promise/core/task_subscriptions.py`
- Create: `tests/test_task_event_bus.py`

- [ ] **Step 1: Implement TaskEventBus**

```python
# plastic_promise/core/task_event_bus.py
"""TaskEventBus — SSE broadcaster for hunter guild events."""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskEventBus:
    """Manages SSE client connections and broadcasts task events."""

    def __init__(self):
        self._clients: dict[str, list] = {}

    def register(self, agent_name: str, send_func):
        """Register an SSE client connection for an agent."""
        if agent_name not in self._clients:
            self._clients[agent_name] = []
        self._clients[agent_name].append(send_func)
        logger.debug(f"SSE client registered: {agent_name}")

    def unregister(self, agent_name: str, send_func):
        """Remove a disconnected SSE client."""
        if agent_name in self._clients:
            try:
                self._clients[agent_name].remove(send_func)
                if not self._clients[agent_name]:
                    del self._clients[agent_name]
            except ValueError:
                pass

    async def broadcast(self, event_type: str, data: dict, to_agents: list[str]):
        """Broadcast a task event to specified agents."""
        payload = json.dumps({"event": event_type, "data": data}, ensure_ascii=False)
        notified = 0
        for agent in to_agents:
            if agent in self._clients:
                for send_func in self._clients[agent]:
                    try:
                        await send_func(payload)
                        notified += 1
                    except Exception:
                        self.unregister(agent, send_func)
        return notified

    async def broadcast_task_event(self, event_type: str, task: dict):
        """Determine targets from task data and broadcast."""
        to_agents = []

        if event_type == "task:new":
            to_agents = [task["to_agent"]]
            # Also notify subscribers
            try:
                from plastic_promise.core.task_subscriptions import match_subscribers
                subs = match_subscribers(task)
                to_agents.extend(s for s in subs if s not in to_agents)
            except Exception:
                pass

        elif event_type in ("task:claimed", "task:done"):
            to_agents = [task.get("from_agent", "daemon")]
        elif event_type in ("task:reassigned", "task:verified"):
            to_agents = [task.get("claimed_by", "")]
        elif event_type in ("task:overdue",):
            to_agents = [task.get("claimed_by", ""), "claude"]
        elif event_type in ("task:escalated",):
            to_agents = ["claude"]
        elif event_type == "hunter:rank_change":
            to_agents = [task.get("agent", ""), "claude"]

        to_agents = [a for a in to_agents if a]  # Filter empty
        return await self.broadcast(event_type, {
            "task_id": task.get("task_id", task.get("id", "")),
            "task_type": task.get("task_type", ""),
            "priority": task.get("priority", 3),
            "to_agent": task.get("to_agent", ""),
            "title": task.get("title", ""),
            "from_agent": task.get("from_agent", ""),
            "claimed_by": task.get("claimed_by", ""),
        }, to_agents)

    @property
    def client_count(self) -> int:
        return sum(len(v) for v in self._clients.values())


# Module-level singleton
_event_bus: TaskEventBus | None = None


def get_event_bus() -> TaskEventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = TaskEventBus()
    return _event_bus
```

- [ ] **Step 2: Implement subscription matching**

```python
# plastic_promise/core/task_subscriptions.py
"""Subscription matching — determine which agents should be notified."""

import json
import sqlite3
import os


def match_subscribers(task: dict) -> list[str]:
    """Find subscribers matching a task. Returns list of agent names."""
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM task_subscriptions WHERE enabled=1"
    ).fetchall()

    matched = []
    for sub in rows:
        # agent match: subscription targets the task's destination
        if sub["agent_name"] != task.get("to_agent", ""):
            continue

        # task_type filter (GLOB)
        if sub["task_type_filter"]:
            import fnmatch
            if not fnmatch.fnmatch(task.get("task_type", ""), sub["task_type_filter"]):
                continue

        # priority filter
        if task.get("priority", 4) > sub["priority_min"]:
            continue

        # keyword match
        if sub["keywords"]:
            try:
                keywords = json.loads(sub["keywords"])
                title = task.get("title", "")
                desc = task.get("description", "")
                text = f"{title} {desc}".lower()
                if not any(kw.lower() in text for kw in keywords):
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        matched.append(sub["agent_name"])

    conn.close()
    return matched
```

- [ ] **Step 3: Write tests**

```python
# tests/test_task_event_bus.py
import pytest
from plastic_promise.core.task_event_bus import TaskEventBus, get_event_bus
from plastic_promise.core.task_subscriptions import match_subscribers

def test_event_bus_singleton():
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2

@pytest.mark.asyncio
async def test_event_bus_broadcast():
    bus = TaskEventBus()
    received = []

    async def fake_send(payload):
        received.append(payload)

    bus.register("pi_fixer", fake_send)
    notified = await bus.broadcast("task:new", {"task_id": "t_test"}, ["pi_fixer"])
    assert notified == 1
    assert len(received) == 1
    assert "task:new" in received[0]

@pytest.mark.asyncio
async def test_event_bus_offline_agent():
    bus = TaskEventBus()
    notified = await bus.broadcast("task:new", {"task_id": "t_test"}, ["offline_agent"])
    assert notified == 0  # No error, just no delivery
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_task_event_bus.py -v`
Expected: 3 PASS

- [ ] **Step 5: Integrate SSE into task_enqueue**

In `plastic_promise/mcp/tools/task_queue.py`, add event bus broadcast to `handle_task_enqueue`:

```python
# After successful insert in handle_task_enqueue:
try:
    from plastic_promise.core.task_event_bus import get_event_bus
    bus = get_event_bus()
    notified = await bus.broadcast_task_event("task:new", {
        "task_id": task_id,
        "task_type": args["task_type"],
        "priority": priority,
        "to_agent": args["to_agent"],
        "title": args["title"],
        "from_agent": from_agent,
    })
    sse_notified = notified
except Exception:
    sse_notified = 0
```

- [ ] **Step 6: Register SSE endpoint in MCP server**

In `plastic_promise/mcp/server.py`, add SSE client registration endpoint:

```python
# SSE client registration — called when Agent connects to /sse
async def handle_sse_connect(agent_name: str, send_func):
    from plastic_promise.core.task_event_bus import get_event_bus
    bus = get_event_bus()
    bus.register(agent_name, send_func)
```

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/core/task_event_bus.py plastic_promise/core/task_subscriptions.py tests/test_task_event_bus.py plastic_promise/mcp/tools/task_queue.py plastic_promise/mcp/server.py
git commit -m "feat: TaskEventBus + subscription matching + SSE integration"
```

---

### Task 11: End-to-End Integration Test

**Files:**
- Create: `tests/test_hunter_guild_e2e.py`

- [ ] **Step 1: Write E2E test covering full lifecycle**

```python
# tests/test_hunter_guild_e2e.py
import json
import pytest
from plastic_promise.mcp.tools.task_queue import (
    handle_task_enqueue, handle_task_claim, handle_task_complete,
    handle_task_verify, handle_task_inbox, handle_task_abandon,
)

@pytest.mark.asyncio
async def test_full_hunter_guild_lifecycle():
    """End-to-end: daemon discovers → enqueues → hunter claims → completes → verified."""
    engine = type('Engine', (object,), {})()

    # 1. Daemon discovers a memory issue and enqueues
    r = await handle_task_enqueue(engine, {
        "task_type": "fix_memory",
        "title": "修复重复记忆集群 #DUP_042",
        "to_agent": "pi_fixer",
        "priority": 3,
        "from_agent": "daemon",
        "source_scan": "scan_duplicate_clusters",
        "description": "发现3条完全重复的记忆，保留worth最高的一条",
        "payload": {"memory_ids": ["m_001", "m_002", "m_003"]},
    })
    data = json.loads(r[0].text)
    assert data["status"] == "pending"
    task_id = data["task_id"]
    print(f"  ✅ 委托挂板: {task_id}")

    # 2. pi_fixer checks inbox and sees the task
    r = await handle_task_inbox(engine, {
        "agent_name": "pi_fixer",
        "trust_score": 0.60,
        "filter_status": "pending",
    })
    data = json.loads(r[0].text)
    assert data["rank"]["rank"] == "B"
    tasks = [t for t in data["tasks"] if t["id"] == task_id]
    assert len(tasks) == 1
    assert tasks[0]["can_claim"] is True
    print(f"  ✅ 委托板上可见, 等级匹配: {tasks[0]['match']}")

    # 3. pi_fixer claims the task
    r = await handle_task_claim(engine, {
        "agent_name": "pi_fixer",
        "task_id": task_id,
        "trust_score": 0.60,
    })
    data = json.loads(r[0].text)
    assert data["success"] is True
    print(f"  ✅ 揭榜成功: {data['match']}")

    # 4. A lower-ranked hunter tries to claim the same task — must fail
    r = await handle_task_claim(engine, {
        "agent_name": "pi_reviewer",
        "task_id": task_id,
        "trust_score": 0.70,
    })
    data = json.loads(r[0].text)
    assert data["success"] is False
    print(f"  ✅ 重复揭榜被阻止: {data['reason']}")

    # 5. pi_fixer sends heartbeat, then completes
    await handle_task_heartbeat(engine, {"task_id": task_id, "agent_name": "pi_fixer"})
    r = await handle_task_complete(engine, {
        "task_id": task_id,
        "agent_name": "pi_fixer",
        "result": "已清理2条重复记忆，保留 m_001 (worth=0.78)",
        "artifacts": ["memory_id:m_001"],
    })
    data = json.loads(r[0].text)
    assert data["success"] is True
    assert data["status"] == "done"
    verify_task_id = data["verification_task_id"]
    print(f"  ✅ 委托完成, 验收子委托已创建: {verify_task_id}")

    # 6. Claude verifies — accept
    r = await handle_task_verify(engine, {
        "task_id": task_id,
        "verdict": "accepted",
        "verified_by": "claude",
        "comment": "清理正确，LGTM",
    })
    data = json.loads(r[0].text)
    assert data["success"] is True
    assert data["new_status"] == "verified"
    assert data["trust_adjustment"]["delta"] == 0.02
    print(f"  ✅ 验收通过, 信任分 +0.02 → pi_fixer")

    # 7. Verify task status is terminal
    r = await handle_task_inbox(engine, {
        "agent_name": "pi_fixer",
        "trust_score": 0.62,
        "filter_status": "my_active",
    })
    data = json.loads(r[0].text)
    my_ids = [t["id"] for t in data["tasks"]]
    assert task_id not in my_ids  # Verified tasks don't appear in my_active
    print(f"  ✅ 委托已验收, 不在活跃列表中")

    print("🎉 完整猎人公会生命周期测试通过!")
```

- [ ] **Step 2: Run E2E test**

Run: `pytest tests/test_hunter_guild_e2e.py -v -s`
Expected: PASS with full lifecycle trace

- [ ] **Step 3: Commit**

```bash
git add tests/test_hunter_guild_e2e.py
git commit -m "test: hunter guild E2E — full lifecycle from discovery to verification"
```

---

## Summary

| Phase | Tasks | New Files | Modified Files | Commits |
|-------|-------|-----------|----------------|---------|
| Phase 1 | 6 (Tasks 1-6) | 3 | 4 | 6 |
| Phase 2 | 3 (Tasks 7-9) | 6 | 2 | 3 |
| Phase 3 | 2 (Tasks 10-11) | 3 | 2 | 2 |
| **Total** | **11** | **12** | **8** | **11** |

**Test coverage**: 4 test files, 20+ test cases covering rank system, all 7 MCP tools, penalty engine, scanners, event bus, and full E2E lifecycle.

**Each task is independently testable** — you can stop after any task and have working, tested software.
