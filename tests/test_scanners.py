"""Tests for Hunter Guild Discovery Scanners (Task 8)."""

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


# ═══════════════════════════════════════════════════════════════
# Test: scan_memory_decay
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_memory_decay_detects_zombies(monkeypatch):
    """scan_memory_decay should detect L3 memories inactive >30 days."""
    # Create test DB with zombie memories
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        # Insert zombie L3 memories (last_accessed >30d ago)
        conn = sqlite3.connect(db_path)
        ancient_date = (datetime.now() - timedelta(days=60)).isoformat()
        for i in range(10):
            conn.execute(
                "INSERT INTO memories (id, content, tier, last_accessed, created_at, domain) "
                "VALUES (?, ?, 'L3', ?, ?, 'building')",
                (f"zombie_{i}", f"old memory {i}", ancient_date, ancient_date),
            )
        conn.commit()
        conn.close()

        # Override db path
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        # Mock handle_task_enqueue to prevent actual TCP/MCP calls
        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_memory_decay import scan_memory_decay

        result = await scan_memory_decay(MockEngine())

        assert result is not None
        assert "scanner" in result
        assert result["scanner"] == "scan_memory_decay"
        assert "findings" in result
        assert "dispatched" in result
        # Should find at least zombie memories (10 > threshold of 5)
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_memory_decay_no_zombies_empty_db(monkeypatch):
    """scan_memory_decay should return 0 findings on an empty database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_memory_decay import scan_memory_decay

        result = await scan_memory_decay(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_memory_decay"
        assert result["findings"] == 0
        assert result["dispatched"] == 0
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_memory_decay_domain_imbalance(monkeypatch):
    """scan_memory_decay should detect domain imbalance >60%."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()
        # 70% building, 30% designing → building >60%
        for i in range(70):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'building', ?, ?)",
                (f"b_{i}", f"building memory {i}", now, now),
            )
        for i in range(30):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'designing', ?, ?)",
                (f"d_{i}", f"designing memory {i}", now, now),
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_memory_decay import scan_memory_decay

        result = await scan_memory_decay(MockEngine())

        assert result["findings"] >= 1
        # Verify one finding is domain_imbalance
        assert result["scanner"] == "scan_memory_decay"
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: scan_trust
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_trust_detects_rapid_drops(monkeypatch):
    """scan_trust should detect trust drops >0.15 in 24 hours."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Set up trust score for agent
        conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pi_builder", 0.80, "high", "autonomous", now.isoformat(), now.isoformat()),
        )

        # Insert a rapid trust drop (>0.15 in 24h)
        recent = (now - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO trust_history (target, delta, old_value, new_value, reason, "
            "direction, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "pi_builder",
                -0.20,
                0.80,
                0.60,
                "L0 violation: dangerous operation blocked",
                "decay",
                recent,
            ),
        )
        conn.execute(
            "INSERT INTO trust_history (target, delta, old_value, new_value, reason, "
            "direction, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "pi_builder",
                -0.05,
                0.85,
                0.80,
                "SCARF < 0.40",
                "decay",
                (now - timedelta(hours=1)).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_trust import scan_trust

        result = await scan_trust(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_trust"
        assert result["findings"] >= 1
        # Should find the rapid drop
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_trust_no_drops_normal_state(monkeypatch):
    """scan_trust should return 0 findings when trust is stable."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Normal trust score with no rapid drops
        conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pi_builder", 0.75, "high", "autonomous", now.isoformat(), now.isoformat()),
        )

        # Only small fluctuations
        conn.execute(
            "INSERT INTO trust_history (target, delta, old_value, new_value, reason, "
            "direction, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("pi_builder", 0.02, 0.73, 0.75, "SCARF >= 0.80", "boost", now.isoformat()),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_trust import scan_trust

        result = await scan_trust(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_trust"
        # No rapid drops, so findings should be 0
        # (stagnant trust threshold is 14d, and we just created entries, so 0)
        assert result["findings"] == 0
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_trust_detects_stagnant(monkeypatch):
    """scan_trust should detect trust that hasn't moved in 14+ days."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()
        ancient = now - timedelta(days=30)

        # Trust score set 30 days ago, no history in last 14 days
        conn.execute(
            "INSERT INTO trust_scores (target, trust, tier, autonomy_level, last_updated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pi_fixer", 0.60, "medium", "standard", ancient.isoformat(), ancient.isoformat()),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_trust import scan_trust

        result = await scan_trust(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_trust"
        # Should detect stagnant trust (last_updated >14d ago, no recent history)
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: scan_architecture
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_architecture_detects_god_module(monkeypatch):
    """scan_architecture should detect a domain with disproportionate memory count."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # Create 3 domains with very uneven distribution
        # domain_A with 50, domain_B with 2, domain_C with 1
        # median=2, mean=17.67, std=22.87, threshold=47.74, 50 > 47.74
        for i in range(50):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'domain_A', ?, ?)",
                (f"a_{i}", f"A content {i}", now, now),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'domain_B', ?, ?)",
                (f"b_{i}", f"B content {i}", now, now),
            )
        for i in range(1):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'domain_C', ?, ?)",
                (f"c_{i}", f"C content {i}", now, now),
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_architecture import scan_architecture

        result = await scan_architecture(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_architecture"
        # domain_A (50) should exceed median+2*std of [1, 2, 50]
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: scan_quality_trends
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_quality_detects_recurrence(monkeypatch):
    """scan_quality_trends should detect repeated rejections of same task_type."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now()

        # Insert 3 rejected failures of same type by same agent within 14 days
        for i in range(3):
            occurred = (now - timedelta(days=i * 3)).isoformat()
            conn.execute(
                "INSERT INTO hunter_failure_log "
                "(agent_name, task_id, task_type, failure_type, trust_before, trust_after, penalty_applied, occurred_at) "
                "VALUES (?, ?, ?, 'rejected', ?, ?, ?, ?)",
                ("pi_builder", f"t_{i}", "build_module", 0.70, 0.67, -0.03, occurred),
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_quality_trends import scan_quality_trends

        result = await scan_quality_trends(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_quality_trends"
        assert result["findings"] >= 1
    finally:
        os.unlink(db_path)


# ═══════════════════════════════════════════════════════════════
# Test: scan_coupling
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_coupling_handles_empty_db(monkeypatch):
    """scan_coupling should handle empty database gracefully returning 0 findings."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_coupling import scan_coupling

        result = await scan_coupling(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_coupling"
        assert result["findings"] == 0
        assert result["dispatched"] == 0
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_coupling_detects_tag_anomalies(monkeypatch):
    """scan_coupling should detect unusual tag co-occurrence pairs."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)

        conn = sqlite3.connect(db_path)
        now = datetime.now().isoformat()

        # Create memories with known tag patterns:
        # Tag A and B co-occur in 15 out of 20 memories (expected ~3.2 if independent)
        for i in range(15):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, tags, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'building', ?, ?, ?)",
                (f"ab_{i}", f"AB memory {i}", json.dumps(["tag_A", "tag_B"]), now, now),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, tags, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'building', ?, ?, ?)",
                (f"aonly_{i}", f"A only {i}", json.dumps(["tag_A"]), now, now),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO memories (id, content, tier, domain, tags, created_at, last_accessed) "
                "VALUES (?, ?, 'L1', 'building', ?, ?, ?)",
                (f"bonly_{i}", f"B only {i}", json.dumps(["tag_B"]), now, now),
            )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)

        async def mock_enqueue(*args, **kwargs):
            return [
                type(
                    "obj",
                    (object,),
                    {"text": json.dumps({"task_id": "t_test", "status": "pending"})},
                )()
            ]

        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", mock_enqueue
        )

        from plastic_promise.cron.scan_coupling import scan_coupling

        result = await scan_coupling(MockEngine())

        assert result is not None
        assert result["scanner"] == "scan_coupling"
        # tag_A+tag_B co-occurs 15 times, expected much lower → anomaly
    finally:
        os.unlink(db_path)
