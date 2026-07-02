"""Tests for scan_scheduler_health — the 6-dimension meta-audit scanner."""

import pytest
import sqlite3
import os
import json
import tempfile
from datetime import datetime, timedelta


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


class MockEngine:
    """Minimal mock engine for scanner tests."""

    pass


def create_test_db(db_path: str):
    """Create test database with required tables and sample data."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Memories table (matching actual schema)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "  id TEXT PRIMARY KEY,"
        "  content TEXT,"
        "  memory_type TEXT,"
        "  source TEXT,"
        "  owner TEXT,"
        "  tier TEXT,"
        "  scope TEXT,"
        "  category TEXT,"
        "  importance REAL,"
        "  entity_ids TEXT,"
        "  created_at TEXT,"
        "  access_count INTEGER,"
        "  worth_success INTEGER,"
        "  worth_failure INTEGER,"
        "  activation_weight REAL,"
        "  last_accessed TEXT,"
        "  tags TEXT NOT NULL DEFAULT '[]',"
        "  domain TEXT NOT NULL DEFAULT 'uncategorized',"
        "  decay_multiplier REAL NOT NULL DEFAULT 1.0,"
        "  effective_half_life REAL NOT NULL DEFAULT 3.0"
        ")"
    )

    # Trust scores table
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trust_scores ("
        "  target TEXT PRIMARY KEY,"
        "  trust REAL NOT NULL DEFAULT 0.6,"
        "  tier TEXT NOT NULL DEFAULT 'medium',"
        "  autonomy_level TEXT NOT NULL DEFAULT 'standard',"
        "  last_updated TEXT NOT NULL,"
        "  created_at TEXT NOT NULL"
        ")"
    )

    # Trust history table
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trust_history ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  target TEXT NOT NULL,"
        "  delta REAL NOT NULL,"
        "  reason TEXT NOT NULL DEFAULT '',"
        "  old_value REAL NOT NULL,"
        "  new_value REAL NOT NULL,"
        "  direction TEXT NOT NULL,"
        "  timestamp TEXT NOT NULL"
        ")"
    )

    # Task queue table (for enqueue dispatch)
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
        "  domain TEXT,"
        "  memory_id TEXT,"
        "  principle_id TEXT,"
        "  source_scan TEXT,"
        "  parent_task_id TEXT,"
        "  claimed_by TEXT,"
        "  claimed_at TEXT,"
        "  heartbeat_at TEXT,"
        "  timeout_seconds INTEGER DEFAULT 300,"
        "  max_escalations INTEGER DEFAULT 3,"
        "  escalation_count INTEGER DEFAULT 0,"
        "  last_escalation_at TEXT,"
        "  done_at TEXT,"
        "  result TEXT,"
        "  verified_at TEXT,"
        "  verified_by TEXT,"
        "  verify_verdict TEXT,"
        "  payload TEXT,"
        "  created_at TEXT DEFAULT (datetime('now')),"
        "  updated_at TEXT DEFAULT (datetime('now'))"
        ")"
    )

    # Metric history table
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

    # Hunter failure log table
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hunter_failure_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  agent_name TEXT NOT NULL,"
        "  task_id TEXT NOT NULL,"
        "  task_type TEXT NOT NULL,"
        "  failure_type TEXT NOT NULL,"
        "  trust_before REAL,"
        "  trust_after REAL,"
        "  penalty_applied REAL,"
        "  occurred_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )

    conn.commit()
    conn.close()


async def _mock_enqueue(*args, **kwargs):
    """Shared mock for handle_task_enqueue to avoid actual MCP calls."""
    return [
        type("obj", (object,), {"text": json.dumps({"task_id": "t_test", "status": "pending"})})()
    ]


# ═══════════════════════════════════════════════════════════════
# Test 1: Scanner SNR detects noisy scanner
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scanner_snr_detects_noisy_scanner(monkeypatch):
    """12 tasks from scan_architecture, 7 rejected (58%) — should trigger auto-throttle."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # 7 rejected tasks from scan_architecture
        for i in range(7):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, source_scan, "
                "verify_verdict, verified_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"rej_{i}",
                    "fix_memory",
                    f"Fix {i}",
                    "pi_fixer",
                    3,
                    "verified",
                    "scan_architecture",
                    "rejected",
                    now,
                    now,
                ),
            )
        # 5 accepted tasks from scan_architecture
        for i in range(5):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, source_scan, "
                "verify_verdict, verified_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"acc_{i}",
                    "fix_memory",
                    f"Fix {i}",
                    "pi_fixer",
                    3,
                    "verified",
                    "scan_architecture",
                    "accepted",
                    now,
                    now,
                ),
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1
        # scan_architecture should be in auto_actions
        throttled = [a["scanner"] for a in result["auto_actions"]]
        assert "scan_architecture" in throttled
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test 2: Empty DB first audit
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_health_empty_db_first_audit(monkeypatch):
    """Empty DB returns 0 findings but still dispatches the audit report."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] == 0
        assert result["dispatched"] >= 1
        assert result["auto_actions"] == []
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test 3: Agent timeout detection
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_health_detects_agent_timeout(monkeypatch):
    """7 timeout failures from pi_fixer — should produce findings >= 1."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # Create matching task_queue entries for each failure
        for i in range(7):
            task_id = f"timeout_task_{i}"
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, escalation_count, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, "fix_memory", f"Fix {i}", "pi_fixer", 3, "pending", 2, now),
            )
            conn.execute(
                "INSERT INTO hunter_failure_log "
                "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, "
                "penalty_applied, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("pi_fixer", task_id, "fix_memory", "timeout", 0.60, 0.59, -0.01, now),
            )

        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test 4: Priority inflation detection
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_health_detects_priority_inflation(monkeypatch):
    """60 P1 + 40 P3 = 60% S-rank priority — should produce findings >= 1."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # 60 Priority 1 tasks
        for i in range(60):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"p1_{i}", "fix_memory", f"P1 Fix {i}", "pi_builder", 1, "pending", now),
            )
        # 40 Priority 3 tasks
        for i in range(40):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"p3_{i}", "fix_memory", f"P3 Fix {i}", "pi_fixer", 3, "pending", now),
            )

        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test 5: High dispatch latency detection
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_health_detects_high_latency(monkeypatch):
    """Tasks created ~2h before being claimed — should detect high latency."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)

        # 5 tasks created ~2h ago, claimed just now — ~7200s avg wait
        for i in range(5):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, "
                "claimed_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, "
                "datetime('now'), datetime('now', '-2 hours'))",
                (f"slow_{i}", "build_module", f"Build {i}", "pi_builder", 3, "claimed"),
            )

        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test 6: Small sample — no false positive auto-throttle
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_health_small_sample_no_false_positive(monkeypatch):
    """3 tasks all rejected (<10 total) — auto_actions must be empty."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # 3 tasks from scan_test, all rejected, total=3 < 10
        for i in range(3):
            conn.execute(
                "INSERT INTO task_queue "
                "(id, task_type, title, to_agent, priority, status, source_scan, "
                "verify_verdict, verified_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"small_{i}",
                    "fix_memory",
                    f"Fix {i}",
                    "pi_fixer",
                    3,
                    "verified",
                    "scan_test",
                    "rejected",
                    now,
                    now,
                ),
            )

        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", _mock_enqueue
        )

        from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

        result = await scan_scheduler_health(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_scheduler_health"
        # auto_actions only trigger when reject_rate > 0.50 AND total >= 10
        assert result["auto_actions"] == []
    finally:
        os.unlink(db_path)
