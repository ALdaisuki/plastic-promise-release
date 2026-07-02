# Scheduler Health Meta-Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 6th discovery scanner (`scan_scheduler_health`) that audits the Hunter Guild dispatch system itself across 6 dimensions, with auto-throttle for noisy scanners.

**Architecture:** One new scanner file following the existing `async def scan_*(engine) -> dict` pattern, registered in the daemon's scanner loop with AdaptiveThrottle. A new `reset_throttle` action on the existing `domain` MCP tool lets Claude manually revert auto-throttle decisions.

**Tech Stack:** Python 3.10+, sqlite3, asyncio. Zero new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-02-scheduler-health-meta-audit-design.md`

## Global Constraints

- Zero new DB tables — reuse `task_queue`, `hunter_failure_log`, `metric_history`
- Zero new MCP tools — reuse `task_enqueue`, add one `action` branch to existing `domain` tool
- Zero new agents — dispatch findings to existing `claude` agent
- Scanner signature: `async def scan_scheduler_health(engine) -> dict` — matches existing 5 scanners
- Follow existing scanner patterns: `scan_architecture.py` as reference
- Test pattern: `tests/test_scanners.py` as reference — pytest async, tempfile DB, MockEngine, monkeypatch `handle_task_enqueue`

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `plastic_promise/cron/scan_scheduler_health.py` | Create | 6-dimension audit queries + auto-throttle detection + report dispatch |
| `daemons/maintenance_daemon.py` | Modify | Register scanner import, throttle entry, loop invocation |
| `plastic_promise/mcp/tools/domain.py` | Modify | Add `reset_throttle` action branch |
| `tests/test_scheduler_health.py` | Create | 6 dimension tests + auto-throttle + first-run + edge cases |

---

### Task 1: Create scan_scheduler_health.py — Core Scanner

**Files:**
- Create: `plastic_promise/cron/scan_scheduler_health.py`

**Interfaces:**
- Produces: `async def scan_scheduler_health(engine) -> dict`
  - Returns: `{"scanner": "scan_scheduler_health", "findings": int, "dispatched": int, "auto_actions": list[dict]}`
- Consumes: `handle_task_enqueue(engine, args)` from `plastic_promise.mcp.tools.task_queue`
- Consumes: DB tables `task_queue`, `hunter_failure_log`, `metric_history`, `memories`

- [ ] **Step 1: Create the scanner file with all 6 dimension queries and dispatch logic**

```python
"""Scheduler health meta-audit scanner — 6-dimension audit of the dispatch system itself.

Dimensions:
  1. Scanner SNR — source_scan reject rates, auto-throttle noisy scanners
  2. Agent timeout — hunter_failure_log timeout aggregation
  3. Dispatch latency — task_queue claim wait time
  4. Priority balance — priority distribution health
  5. Verification throughput — verification cadence
  6. Trend comparison — compare vs previous audit from memory pool
"""

import sqlite3
import os
import json
from datetime import datetime, timedelta
from collections import defaultdict


async def scan_scheduler_health(engine) -> dict:
    """Audit the dispatch system itself across 6 dimensions.

    Returns dict with:
      - scanner: "scan_scheduler_health"
      - findings: total issue count
      - dispatched: number of tasks enqueued
      - auto_actions: list of auto-throttle actions taken
      - dimensions: per-dimension audit data
      - is_first_audit: bool
      - audit_id: str timestamp-based ID
    """
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    audit_id = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    auto_actions = []
    dimensions = {}

    try:
        # ── Dimension 1: Scanner SNR ──────────────────────────────
        snr_rows = conn.execute("""
            SELECT source_scan,
                   COUNT(*) as total,
                   SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) as rejected,
                   SUM(CASE WHEN status='verified' THEN 1 ELSE 0 END) as verified,
                   ROUND(CAST(SUM(CASE WHEN verify_verdict='rejected' THEN 1 ELSE 0 END) AS REAL)
                         / MAX(COUNT(*), 1), 2) as reject_rate
            FROM task_queue
            WHERE created_at >= datetime('now', '-7 days')
              AND source_scan IS NOT NULL
              AND status IN ('verified', 'reassigned')
            GROUP BY source_scan
            ORDER BY reject_rate DESC
        """).fetchall()

        snr_top3 = []
        for row in snr_rows[:3]:
            entry = {
                "scanner": row["source_scan"],
                "reject_rate": row["reject_rate"],
                "total": row["total"],
                "rejected": row["rejected"],
                "level": "green",
            }
            if row["reject_rate"] > 0.50 and row["total"] >= 10:
                entry["level"] = "red"
                entry["auto_action"] = f"throttled_{int(row['source_scan'].replace('scan_',''))}"
                auto_actions.append({
                    "scanner": row["source_scan"],
                    "total": row["total"],
                    "rejected": row["rejected"],
                    "rate": row["reject_rate"],
                    "action": "throttle_double",
                })
            elif row["reject_rate"] >= 0.30:
                entry["level"] = "yellow"
            snr_top3.append(entry)

        dimensions["scanner_snr"] = {
            "top3": snr_top3,
            "auto_actions": [a["scanner"] for a in auto_actions],
        }

        # ── Dimension 2: Agent Timeout Rate ────────────────────────
        timeout_rows = conn.execute("""
            SELECT hf.agent_name as claimed_by,
                   COUNT(DISTINCT hf.task_id) as timeout_tasks,
                   ROUND(AVG(CAST(tq.escalation_count AS REAL)), 1) as avg_escalation
            FROM hunter_failure_log hf
            JOIN task_queue tq ON hf.task_id = tq.id
            WHERE hf.failure_type = 'timeout'
              AND hf.occurred_at >= datetime('now', '-7 days')
            GROUP BY hf.agent_name
            ORDER BY timeout_tasks DESC
        """).fetchall()

        timeout_top3 = []
        for row in timeout_rows[:3]:
            entry = {
                "agent": row["claimed_by"],
                "timeout_tasks": row["timeout_tasks"],
                "avg_escalation": row["avg_escalation"],
                "level": "green",
            }
            if row["timeout_tasks"] > 5:
                entry["level"] = "red"
            elif row["timeout_tasks"] >= 2:
                entry["level"] = "yellow"
            timeout_top3.append(entry)

        dimensions["agent_timeout"] = {"top3": timeout_top3}

        # ── Dimension 3: Dispatch Latency ──────────────────────────
        latency_rows = conn.execute("""
            SELECT task_type,
                   ROUND(AVG((julianday(claimed_at) - julianday(created_at)) * 86400), 0)
                       as avg_wait_seconds,
                   COUNT(*) as total
            FROM task_queue
            WHERE status IN ('claimed', 'executing', 'done', 'verified')
              AND claimed_at IS NOT NULL
              AND created_at >= datetime('now', '-7 days')
            GROUP BY task_type
            ORDER BY avg_wait_seconds DESC
        """).fetchall()

        latency_top3 = []
        for row in latency_rows[:3]:
            avg_wait = row["avg_wait_seconds"] or 0
            entry = {
                "task_type": row["task_type"],
                "avg_wait_seconds": avg_wait,
                "total": row["total"],
                "level": "green",
            }
            if avg_wait > 3600:
                entry["level"] = "red"
            elif avg_wait >= 600:
                entry["level"] = "yellow"
            latency_top3.append(entry)

        dimensions["dispatch_latency"] = {"top3": latency_top3}

        # ── Dimension 4: Priority Balance ──────────────────────────
        priority_rows = conn.execute("""
            SELECT priority,
                   COUNT(*) as total,
                   ROUND(CAST(COUNT(*) AS REAL) /
                     (SELECT COUNT(*) FROM task_queue
                      WHERE created_at >= datetime('now', '-7 days')), 2) as pct
            FROM task_queue
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY priority
            ORDER BY priority
        """).fetchall()

        distribution = {}
        for row in priority_rows:
            distribution[str(row["priority"])] = row["pct"]

        level = "green"
        p1_pct = distribution.get("1", 0)
        p4_pct = distribution.get("4", 0)
        if p1_pct > 0.50:
            level = "red"
        elif p4_pct > 0.80:
            level = "yellow"

        dimensions["priority_balance"] = {
            "distribution": distribution,
            "level": level,
        }

        # ── Dimension 5: Verification Throughput ───────────────────
        verify_rows = conn.execute("""
            SELECT verified_by,
                   COUNT(*) as verified_total,
                   COUNT(DISTINCT DATE(verified_at)) as active_days,
                   ROUND(CAST(COUNT(*) AS REAL)
                         / MAX(CAST(COUNT(DISTINCT DATE(verified_at)) AS REAL), 1), 1)
                       as avg_per_day
            FROM task_queue
            WHERE status = 'verified'
              AND verified_at >= datetime('now', '-7 days')
            GROUP BY verified_by
        """).fetchall()

        avg_per_day = 0.0
        active_days = 0
        if verify_rows:
            avg_per_day = verify_rows[0]["avg_per_day"]
            active_days = verify_rows[0]["active_days"]

        level = "green"
        if avg_per_day > 20:
            level = "yellow"
        elif active_days < 2 and verify_rows:
            level = "yellow"

        dimensions["verification_throughput"] = {
            "avg_per_day": avg_per_day,
            "active_days": active_days,
            "level": level,
        }

        # ── Dimension 6: Trend Comparison ──────────────────────────
        is_first_audit = False
        previous_audit_id = None
        improvements = []
        degradations = []
        follow_up_tasks = []

        # Search memory pool for previous audit report
        prev_rows = conn.execute("""
            SELECT id, content, created_at FROM memories
            WHERE memory_type = 'experience'
              AND content LIKE '%"audit_id"%'
              AND content LIKE '%"scanner":"scan_scheduler_health"%'
              AND created_at >= datetime('now', '-14 days')
            ORDER BY created_at DESC
            LIMIT 1
        """).fetchall()

        if prev_rows:
            try:
                prev_content = json.loads(prev_rows[0]["content"]) if prev_rows[0]["content"] else {}
                if isinstance(prev_content, dict) and "audit_id" in prev_content:
                    previous_audit_id = prev_content["audit_id"]
                    prev_dims = prev_content.get("dimensions", {})

                    # Compare SNR
                    prev_snr = prev_dims.get("scanner_snr", {}).get("top3", [])
                    prev_snr_map = {s["scanner"]: s["reject_rate"] for s in prev_snr}
                    for s in snr_top3:
                        prev_rate = prev_snr_map.get(s["scanner"])
                        if prev_rate is not None:
                            delta = s["reject_rate"] - prev_rate
                            if delta < -0.1:
                                improvements.append(
                                    f"scanner_snr {s['scanner']}: {prev_rate}→{s['reject_rate']}"
                                )
                            elif delta > 0.1:
                                degradations.append(
                                    f"scanner_snr {s['scanner']}: {prev_rate}→{s['reject_rate']}"
                                )

                    # Compare timeout
                    prev_timeout = prev_dims.get("agent_timeout", {}).get("top3", [])
                    prev_timeout_map = {t["agent"]: t["timeout_tasks"] for t in prev_timeout}
                    for t in timeout_top3:
                        prev_count = prev_timeout_map.get(t["agent"], 0)
                        if t["timeout_tasks"] < prev_count:
                            improvements.append(
                                f"agent_timeout {t['agent']}: {prev_count}→{t['timeout_tasks']}"
                            )
                        elif t["timeout_tasks"] > prev_count:
                            degradations.append(
                                f"agent_timeout {t['agent']}: {prev_count}→{t['timeout_tasks']}"
                            )
                            if t["timeout_tasks"] > 5:
                                follow_up_tasks.append({
                                    "task_type": "review_agent_timeout",
                                    "agent": t["agent"],
                                    "reason": f"timeout increased {prev_count}→{t['timeout_tasks']}",
                                })

                    # Compare latency
                    prev_lat = prev_dims.get("dispatch_latency", {}).get("top3", [])
                    prev_lat_map = {l["task_type"]: l["avg_wait_seconds"] for l in prev_lat}
                    for l in latency_top3:
                        prev_wait = prev_lat_map.get(l["task_type"], 0)
                        if prev_wait > 0 and l["avg_wait_seconds"] > prev_wait * 2:
                            degradations.append(
                                f"dispatch_latency {l['task_type']}: {prev_wait}s→{l['avg_wait_seconds']}s"
                            )
                            follow_up_tasks.append({
                                "task_type": "review_dispatch_latency",
                                "for_task_type": l["task_type"],
                                "reason": f"latency 2x+ increase {prev_wait}s→{l['avg_wait_seconds']}s",
                            })
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        else:
            is_first_audit = True

        dimensions["trends"] = {
            "compared_to": previous_audit_id,
            "improvements": improvements,
            "degradations": degradations,
            "follow_up_tasks": follow_up_tasks,
        }

    finally:
        conn.close()

    # ── Build findings list and dispatch audit report ──
    findings = []
    total_issues = 0

    for s in snr_top3:
        if s["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "scanner_snr",
                "level": s["level"],
                "data": s,
            })
    for t in timeout_top3:
        if t["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "agent_timeout",
                "level": t["level"],
                "data": t,
            })
    for l in latency_top3:
        if l["level"] in ("red", "yellow"):
            total_issues += 1
            findings.append({
                "dimension": "dispatch_latency",
                "level": l["level"],
                "data": l,
            })
    if dimensions["priority_balance"]["level"] in ("red", "yellow"):
        total_issues += 1
        findings.append({
            "dimension": "priority_balance",
            "level": dimensions["priority_balance"]["level"],
            "data": dimensions["priority_balance"],
        })
    if dimensions["verification_throughput"]["level"] in ("red", "yellow"):
        total_issues += 1
        findings.append({
            "dimension": "verification_throughput",
            "level": dimensions["verification_throughput"]["level"],
            "data": dimensions["verification_throughput"],
        })
    if degradations:
        total_issues += 1
        findings.append({
            "dimension": "trends",
            "level": "yellow",
            "data": {"degradations": degradations},
        })

    # Build audit report payload (for memory storage and Claude review)
    report = {
        "audit_id": audit_id,
        "is_first_audit": is_first_audit,
        "previous_audit_id": previous_audit_id,
        "scanner": "scan_scheduler_health",
        "generated_at": datetime.now().isoformat(),
        "dimensions": dimensions,
        "auto_actions": auto_actions,
        "findings_summary": {
            "total_issues": total_issues,
            "auto_actions_count": len(auto_actions),
            "is_first_audit": is_first_audit,
        },
    }

    # Dispatch audit report to Claude via task_enqueue
    from plastic_promise.mcp.tools.task_queue import handle_task_enqueue

    dispatched = 0
    try:
        await handle_task_enqueue(engine, {
            "task_type": "audit_scheduler",
            "title": f"调度器健康审计 {audit_id}"
                     f"{' (首次)' if is_first_audit else ''} — {total_issues}项发现",
            "to_agent": "claude",
            "priority": 3,
            "source_scan": "scan_scheduler_health",
            "description": (
                f"6维调度器自审计完成。\n"
                f"发现: {total_issues} 项 | 自动动作: {len(auto_actions)} 项\n"
                f"SNR: {len(snr_top3)} scanners tracked | "
                f"Timeout: {len(timeout_top3)} agents | "
                f"Latency: {len(latency_top3)} task_types\n"
            ),
            "payload": report,
        })
        dispatched += 1
    except Exception:
        pass

    # Dispatch auto-throttle notification separately if needed
    if auto_actions:
        try:
            await handle_task_enqueue(engine, {
                "task_type": "notify_throttle_change",
                "title": f"自动节流: {len(auto_actions)}个扫描器降频",
                "to_agent": "claude",
                "priority": 2,
                "source_scan": "scan_scheduler_health",
                "description": json.dumps(auto_actions, ensure_ascii=False, indent=2),
                "payload": {
                    "auto_actions": auto_actions,
                    "rollback": "domain(action='reset_throttle', scanner='<name>')",
                },
            })
            dispatched += 1
        except Exception:
            pass

    return {
        "scanner": "scan_scheduler_health",
        "findings": total_issues,
        "dispatched": dispatched,
        "auto_actions": auto_actions,
    }
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/cron/scan_scheduler_health.py
git commit -m "feat: add scan_scheduler_health — 6-dimension dispatch system meta-audit scanner"
```

---

### Task 2: Register Scanner in Maintenance Daemon

**Files:**
- Modify: `daemons/maintenance_daemon.py:39-43` (import block)
- Modify: `daemons/maintenance_daemon.py:81-87` (_scanner_throttles dict)
- Modify: `daemons/maintenance_daemon.py:1254-1270` (scanner loop)

**Interfaces:**
- Consumes: `scan_scheduler_health` from `plastic_promise.cron.scan_scheduler_health`
- Produces: `_scanner_throttles["scan_scheduler_health"]` = `AdaptiveThrottle(1200)`
- Produces: scanner loop entry for scan_scheduler_health

- [ ] **Step 1: Add import (line 43, after existing imports)**

```python
# After line 43: from plastic_promise.cron.scan_memory_decay import scan_memory_decay
# Add:
from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
```

- [ ] **Step 2: Add throttle entry (line 87, after scan_memory_decay entry)**

```python
# After line 86: "scan_memory_decay": AdaptiveThrottle(600),
# Add:
    "scan_scheduler_health": AdaptiveThrottle(1200),  # meta-audit: runs less frequently
```

- [ ] **Step 3: Register in scanner loop — add after existing 4-scanner loop (after line 1270)**

The scheduler health scanner runs at a lower frequency (base 1200s = 20min). Add it as a separate entry after the existing 4-scanner loop:

```python
            # Priority B.5: scheduler health meta-audit (runs infrequently)
            sched_throttle = _scanner_throttles.get("scan_scheduler_health")
            if sched_throttle and tick % max(1, sched_throttle.current // 10) == 0:
                try:
                    result = await scan_scheduler_health(engine)
                    if result.get("findings", 0) > 0:
                        sched_throttle.on_hit()
                    else:
                        sched_throttle.on_empty()
                    # Apply auto-throttle actions from audit
                    for action in result.get("auto_actions", []):
                        scanner_name = action["scanner"]
                        target_throttle = _scanner_throttles.get(scanner_name)
                        if target_throttle:
                            old_interval = target_throttle.current
                            new_interval = min(target_throttle.current * 2, target_throttle.base * 8)
                            target_throttle.current = new_interval
                            # Record to metric_history
                            try:
                                db_conn = sqlite3.connect(DB_PATH)
                                db_conn.execute(
                                    "INSERT INTO metric_history (metric_name, metric_value, window_start, window_end) "
                                    "VALUES (?, ?, datetime('now', '-7 days'), datetime('now'))",
                                    (f"auto_throttle:{scanner_name}", new_interval)
                                )
                                db_conn.commit()
                                db_conn.close()
                            except Exception:
                                pass
                            print(f"  [AUTO-THROTTLE] {scanner_name}: {old_interval}s -> {new_interval}s "
                                  f"(reject_rate={action['rate']})")
                except Exception:
                    pass
```

- [ ] **Step 4: Update startup banner to reflect 6 scanners**

Update line 1196:
```python
# Change:
    print(f"  5 scanners: scan_trust scan_architecture scan_quality_trends "
          f"scan_coupling scan_memory_decay")
# To:
    print(f"  6 scanners: scan_trust scan_architecture scan_quality_trends "
          f"scan_coupling scan_memory_decay scan_scheduler_health")
```

- [ ] **Step 5: Commit**

```bash
git add daemons/maintenance_daemon.py
git commit -m "feat: register scan_scheduler_health in daemon with AdaptiveThrottle(1200s)"
```

---

### Task 3: Add reset_throttle Action to Domain MCP Tool

**Files:**
- Modify: `plastic_promise/mcp/tools/domain.py:103-131` (handle_domain dispatch)

**Interfaces:**
- Consumes: `daemon._scanner_throttles` dict (accessed via engine or direct import)
- Produces: `domain(action="reset_throttle", scanner="<name>")` — resets throttle.current to throttle.base

- [ ] **Step 1: Update docstring and add elif branch**

Update the docstring on line 104:
```python
    """域联邦统一入口。action: stats|merge|unmerge|rename|rebuild|reset_throttle"""
```

Add after line 126 (`elif action == "rebuild":` block), before the `else`:

```python
        elif action == "reset_throttle":
            scanner = args.get("scanner", "")
            if not scanner:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "scanner parameter required for reset_throttle"},
                    ensure_ascii=False))]
            # Access daemon throttles — they live in maintenance_daemon module
            try:
                from daemons.maintenance_daemon import _scanner_throttles
                throttle = _scanner_throttles.get(scanner)
                if throttle is None:
                    return [TextContent(type="text", text=json.dumps(
                        {"error": f"unknown scanner: {scanner}. "
                                  f"Known: {list(_scanner_throttles.keys())}"},
                        ensure_ascii=False))]
                old_interval = throttle.current
                throttle.current = throttle.base
                throttle.empty_streak = 0
                return [TextContent(type="text", text=json.dumps({
                    "reset_throttle": scanner,
                    "old_interval": old_interval,
                    "new_interval": throttle.base,
                    "status": "ok",
                }, ensure_ascii=False))]
            except ImportError:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "daemon module not importable — is the daemon running?"},
                    ensure_ascii=False))]
```

- [ ] **Step 2: Commit**

```bash
git add plastic_promise/mcp/tools/domain.py
git commit -m "feat: add reset_throttle action to domain MCP tool"
```

---

### Task 4: Write Tests — scan_scheduler_health

**Files:**
- Create: `tests/test_scheduler_health.py`

**Interfaces:**
- Consumes: `scan_scheduler_health(engine)` from `plastic_promise.cron.scan_scheduler_health`
- Consumes: `create_test_db(db_path)` from `tests/test_scanners.py` (import or replicate)
- Consumes: `MockEngine` from `tests/test_scanners.py` (import or define inline)

- [ ] **Step 1: Write test file with helper, SNR test, empty-DB test, first-audit test, and auto-throttle test**

```python
"""Tests for scan_scheduler_health — 6-dimension dispatch meta-audit scanner."""

import pytest
import sqlite3
import os
import json
import tempfile
from datetime import datetime, timedelta


class MockEngine:
    """Minimal mock engine for scanner tests."""
    pass


def create_test_db(db_path: str):
    """Create test database with required tables."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_queue ("
        "  id TEXT PRIMARY KEY,"
        "  task_type TEXT NOT NULL,"
        "  title TEXT NOT NULL,"
        "  to_agent TEXT,"
        "  priority INTEGER DEFAULT 3,"
        "  from_agent TEXT,"
        "  status TEXT DEFAULT 'pending',"
        "  description TEXT,"
        "  source_scan TEXT,"
        "  claimed_by TEXT,"
        "  claimed_at TEXT,"
        "  done_at TEXT,"
        "  result TEXT,"
        "  verified_at TEXT,"
        "  verified_by TEXT,"
        "  verify_verdict TEXT,"
        "  escalation_count INTEGER DEFAULT 0,"
        "  payload TEXT,"
        "  created_at TEXT DEFAULT (datetime('now')),"
        "  updated_at TEXT DEFAULT (datetime('now'))"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS hunter_failure_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  agent_name TEXT,"
        "  task_id TEXT NOT NULL,"
        "  task_type TEXT NOT NULL,"
        "  failure_type TEXT NOT NULL,"
        "  trust_before REAL,"
        "  trust_after REAL,"
        "  penalty_applied REAL,"
        "  occurred_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS metric_history ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  metric_name TEXT NOT NULL,"
        "  metric_value REAL NOT NULL,"
        "  window_start TEXT NOT NULL,"
        "  window_end TEXT NOT NULL,"
        "  computed_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "  id TEXT PRIMARY KEY,"
        "  content TEXT,"
        "  memory_type TEXT,"
        "  source TEXT,"
        "  owner TEXT,"
        "  tier TEXT,"
        "  domain TEXT NOT NULL DEFAULT 'uncategorized',"
        "  entity_ids TEXT,"
        "  created_at TEXT,"
        "  access_count INTEGER,"
        "  worth_success INTEGER,"
        "  worth_failure INTEGER,"
        "  activation_weight REAL,"
        "  last_accessed TEXT,"
        "  tags TEXT NOT NULL DEFAULT '[]',"
        "  decay_multiplier REAL NOT NULL DEFAULT 1.0,"
        "  effective_half_life REAL NOT NULL DEFAULT 3.0"
        ")"
    )

    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
# Test: scanner_snr — noisy scanner detection
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scanner_snr_detects_noisy_scanner(monkeypatch):
    """scan_scheduler_health should detect scanners with >50% reject rate and >=10 tasks."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Insert 12 verified/reassigned tasks from scan_architecture, 7 rejected (58%)
        for i in range(7):
            task_id = f"arch_{i}"
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, source_scan, "
                "status, verify_verdict, created_at, priority) "
                "VALUES (?, ?, ?, 'pi_builder', 'scan_architecture', 'verified', "
                "'rejected', ?, 3)",
                (task_id, f"build_{i}", f"Build {i}",
                 (now - timedelta(days=i)).isoformat())
            )
        for i in range(5):
            task_id = f"arch_ok_{i}"
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, source_scan, "
                "status, verify_verdict, created_at, priority) "
                "VALUES (?, ?, ?, 'pi_builder', 'scan_architecture', 'verified', "
                "'accepted', ?, 3)",
                (task_id, f"build_ok_{i}", f"Build OK {i}",
                 (now - timedelta(days=i)).isoformat())
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1  # SNR red finding
        assert len(result["auto_actions"]) >= 1
        # Verify the auto_action targets scan_architecture
        scanner_names = [a["scanner"] for a in result["auto_actions"]]
        assert "scan_architecture" in scanner_names
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: empty database (first audit)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scheduler_health_empty_db_first_audit(monkeypatch):
    """scan_scheduler_health should return 0 findings and is_first_audit=True on empty DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] == 0
        assert result["dispatched"] >= 1  # Still dispatches audit report (first audit)
        assert result["auto_actions"] == []
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: agent timeout detection
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scheduler_health_detects_agent_timeout(monkeypatch):
    """scan_scheduler_health should flag agents with >5 timeout tasks in 7 days."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Create task_queue entries + hunter_failure_log entries for timeouts
        for i in range(7):
            task_id = f"timeout_{i}"
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, to_agent, status, "
                "claimed_by, claimed_at, escalation_count, created_at, priority) "
                "VALUES (?, ?, ?, 'pi_fixer', 'claimed', 'pi_fixer', ?, ?, ?, 3)",
                (task_id, f"fix_{i}", f"Fix {i}",
                 (now - timedelta(days=1, hours=i)).isoformat(),
                 (now - timedelta(days=1, hours=i+1)).isoformat())
            )
            conn.execute(
                "INSERT INTO hunter_failure_log (agent_name, task_id, task_type, "
                "failure_type, occurred_at) "
                "VALUES ('pi_fixer', ?, ?, 'timeout', ?)",
                (task_id, f"fix_{i}", (now - timedelta(hours=i)).isoformat())
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1  # Should find the timeout issue
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: priority balance — S-rank inflation
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scheduler_health_detects_priority_inflation(monkeypatch):
    """scan_scheduler_health should flag when priority=1 tasks exceed 50%."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # 60% priority=1, 40% priority=3 → S-rank inflation
        for i in range(60):
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, priority, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (f"p1_{i}", f"urgent_{i}", f"Urgent {i}", now.isoformat())
            )
        for i in range(40):
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, priority, created_at) "
                "VALUES (?, ?, ?, 3, ?)",
                (f"p3_{i}", f"normal_{i}", f"Normal {i}", now.isoformat())
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["findings"] >= 1  # Should flag S-rank inflation
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: dispatch latency detection
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scheduler_health_detects_high_latency(monkeypatch):
    """scan_scheduler_health should flag task types with >1h avg wait time."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Tasks created 2 hours ago, claimed just now → ~7200s latency
        for i in range(5):
            task_id = f"slow_{i}"
            created = (now - timedelta(hours=2)).isoformat()
            claimed = now.isoformat()
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, status, "
                "claimed_at, created_at, priority) "
                "VALUES (?, 'audit_architecture', ?, 'claimed', ?, ?, 3)",
                (task_id, f"Audit {i}", claimed, created)
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["findings"] >= 1  # Should flag high latency
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: small sample threshold — no false positive
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_scheduler_health_small_sample_no_false_positive(monkeypatch):
    """scan_scheduler_health should NOT auto-throttle scanners with total < 10 (small sample)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Only 3 tasks, all rejected (100% reject rate but <10 total → no auto-throttle)
        for i in range(3):
            conn.execute(
                "INSERT INTO task_queue (id, task_type, title, source_scan, "
                "status, verify_verdict, created_at, priority) "
                "VALUES (?, ?, ?, 'scan_memory_decay', 'verified', 'rejected', ?, 3)",
                (f"small_{i}", f"test_{i}", f"Test {i}", now.isoformat())
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [type('obj', (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["auto_actions"] == []  # Small sample, no auto-throttle
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run all scheduler health tests, verify they pass**

```bash
pytest tests/test_scheduler_health.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scheduler_health.py
git commit -m "test: add scan_scheduler_health tests — SNR, timeout, latency, priority, first-audit, small-sample"
```

---

## Verification

After all tasks complete:

1. **Run full scanner test suite:**
   ```bash
   pytest tests/test_scanners.py tests/test_scheduler_health.py -v
   ```
   Expected: All existing + new tests PASS.

2. **Import check — scanner loads without errors:**
   ```bash
   python -c "from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health; print('OK')"
   ```

3. **Domain reset_throttle smoke check:**
   ```bash
   python -c "
   from daemons.maintenance_daemon import _scanner_throttles
   t = _scanner_throttles['scan_architecture']
   t.current = 1200
   print(f'Before: {t.current}')
   # Simulate reset
   t.current = t.base
   t.empty_streak = 0
   print(f'After: {t.current}')
   print('OK')
   "
   ```

4. **Daemon start check** (manual, requires MCP server running):
   ```bash
   python daemons/maintenance_daemon.py
   ```
   Expected: "6 scanners" in startup banner, scan_scheduler_health runs after 1200s throttle.
