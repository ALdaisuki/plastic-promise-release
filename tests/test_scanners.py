"""Tests for Hunter Guild Discovery Scanners (Task 8)."""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


class MockEngine:
    """Minimal mock engine for scanner tests."""

    pass


class MutationSpyEngine:
    """Record lifecycle calls without performing canonical writes."""

    def __init__(self):
        self.mutations = []

    def mutate_ordinary_source(self, memory_id, **mutation):
        self.mutations.append({"memory_id": memory_id, **mutation})
        return {"memory_id": memory_id}


class _PeriodicMaintenanceProbe:
    received_engine = None

    def __init__(self, engine):
        type(self).received_engine = engine
        self._engine = engine

    def update_all_decay(self):
        return 0


class _PeriodicEvolveProbe:
    def __init__(self, rec_mem):
        self.rec_mem = rec_mem

    def evolve_cycle(self):
        return {"promoted": 0, "demoted": 0, "decayed": 0}


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
        "  project_id TEXT NOT NULL DEFAULT 'project:test',"
        "  metadata_json TEXT NOT NULL DEFAULT '{}',"
        "  embedding_hash TEXT NOT NULL DEFAULT 'sha256:test-index',"
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


@pytest.mark.asyncio
async def test_scan_memory_decay_marks_stale_low_worth_without_hard_delete(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        stale_date = (datetime.now() - timedelta(days=90)).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
            "access_count, worth_success, worth_failure, tags, decay_multiplier) "
            "VALUES ('stale_low', 'stale low worth memory', 'experience', 'L1', ?, ?, "
            "0, 0, 5, '[]', 0.1)",
            (stale_date, stale_date),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setenv("PP_PERIODIC_MAINTENANCE", "0")
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            lambda *args, **kwargs: [],
        )

        from plastic_promise.cron.scan_memory_decay import scan_memory_decay

        engine = MutationSpyEngine()
        result = await scan_memory_decay(engine)

        conn = sqlite3.connect(db_path)
        tags_raw = conn.execute("SELECT tags FROM memories WHERE id='stale_low'").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM memories WHERE id='stale_low'").fetchone()[0]
        conn.close()
        assert result["lifecycle"]["stale_marked"] == 1
        assert count == 1
        assert json.loads(tags_raw) == []
        assert len(engine.mutations) == 1
        mutation = engine.mutations[0]
        assert mutation["memory_id"] == "stale_low"
        assert mutation["operation"] == "forgotten"
        assert mutation["reason"] == "lifecycle:stale"
        assert mutation["actor"] == "scan_memory_decay"
        assert mutation["call_id"].startswith(
            "internal:scan_memory_decay:lifecycle:stale:"
        )
        assert mutation["expected_project_id"] == "project:test"
        assert mutation["expected_content_hash"]
        assert mutation["expected_source_snapshot"]["decay_multiplier"] == 0.1
        assert mutation["expected_source_snapshot"]["embedding_hash"]
        assert mutation["expected_source_snapshot"]["worth_failure"] == 5
        assert mutation["expected_peer_snapshots"] == {}
        assert mutation["require_source_available"] is True

    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_scan_memory_decay_marks_duplicate_conflict_replacement(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        create_test_db(db_path)
        now = datetime.now().isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
            "access_count, worth_success, worth_failure, tags) "
            "VALUES ('dup_winner', 'same durable content', 'experience', 'L3', ?, ?, "
            "3, 5, 0, '[]')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
            "access_count, worth_success, worth_failure, tags) "
            "VALUES ('dup_loser', 'same durable content', 'experience', 'L1', ?, ?, "
            "0, 0, 4, '[]')",
            (now, now),
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setenv("PP_PERIODIC_MAINTENANCE", "0")
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue",
            lambda *args, **kwargs: [],
        )

        from plastic_promise.cron.scan_memory_decay import scan_memory_decay

        engine = MutationSpyEngine()
        result = await scan_memory_decay(engine)

        conn = sqlite3.connect(db_path)
        tags_raw = conn.execute("SELECT tags FROM memories WHERE id='dup_loser'").fetchone()[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content='same durable content'"
        ).fetchone()[0]
        conn.close()
        assert result["lifecycle"]["conflicts_marked"] == 1
        assert count == 2
        assert json.loads(tags_raw) == []
        assert len(engine.mutations) == 1
        mutation = engine.mutations[0]
        assert mutation["memory_id"] == "dup_loser"
        assert mutation["operation"] == "forgotten"
        assert mutation["reason"] == "lifecycle:duplicate_replacement:dup_winner"
        assert mutation["actor"] == "scan_memory_decay"
        assert mutation["call_id"].startswith(
            "internal:scan_memory_decay:lifecycle:duplicate:"
        )
        assert mutation["expected_project_id"] == "project:test"
        assert mutation["expected_content_hash"]
        assert mutation["expected_source_snapshot"]["worth_failure"] == 4
        survivor = mutation["expected_peer_snapshots"]["dup_winner"]
        assert survivor["project_id"] == "project:test"
        assert survivor["embedding_hash"]
        assert survivor["worth_success"] == 5
        assert survivor["content_hash"]
        assert mutation["require_source_available"] is True

    finally:
        os.unlink(db_path)


def test_lifecycle_scan_discovers_candidates_without_raw_ordinary_writes(tmp_path):
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / "lifecycle.sqlite"
    create_test_db(str(db_path))
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
        "access_count, worth_success, worth_failure, tags, decay_multiplier) "
        "VALUES (?, ?, 'experience', 'L1', ?, ?, ?, ?, ?, '[]', ?)",
        [
            ("stale", "stale body", now, now, 0, 0, 5, 0.1),
            ("winner", "duplicate body", now, now, 4, 8, 0, 1.0),
            ("loser", "duplicate body", now, now, 0, 0, 4, 1.0),
        ],
    )
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    engine = MutationSpyEngine()

    lifecycle = _run_lifecycle_maintenance(conn, engine)

    assert lifecycle["stale_marked"] == 1
    assert lifecycle["conflicts_marked"] == 1
    assert {mutation["memory_id"] for mutation in engine.mutations} == {
        "stale",
        "loser",
    }
    normalized = [" ".join(statement.casefold().split()) for statement in statements]
    assert not any("update memories set" in statement for statement in normalized)
    assert not any("delete from memories" in statement for statement in normalized)
    assert len({mutation["call_id"] for mutation in engine.mutations}) == 2
    assert all(mutation["actor"] == "scan_memory_decay" for mutation in engine.mutations)
    conn.close()


def test_lifecycle_scan_unindexable_rows_do_not_starve_valid_candidate(tmp_path):
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / "unindexable-lifecycle.sqlite"
    create_test_db(str(db_path))
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [
        (
            f"legacy-{index}",
            f"legacy unindexable {index}",
            now,
            now,
            "",
        )
        for index in range(55)
    ]
    rows.append(("valid-stale", "valid stale candidate", now, now, "sha256:valid"))
    conn.executemany(
        "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
        "access_count, worth_success, worth_failure, tags, decay_multiplier, "
        "embedding_hash) VALUES (?, ?, 'experience', 'L1', ?, ?, 0, 0, 9, '[]', "
        "0.1, ?)",
        rows,
    )
    conn.commit()
    engine = MutationSpyEngine()

    lifecycle = _run_lifecycle_maintenance(conn, engine)

    assert lifecycle["stale_marked"] == 1
    assert [mutation["memory_id"] for mutation in engine.mutations] == ["valid-stale"]
    conn.close()


def test_lifecycle_scan_projectless_rows_do_not_starve_valid_candidate(tmp_path):
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / "projectless-lifecycle.sqlite"
    create_test_db(str(db_path))
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [
        (
            f"projectless-{index}",
            f"projectless stale {index}",
            now,
            now,
            "",
            f"sha256:projectless-{index}",
        )
        for index in range(55)
    ]
    rows.append(
        (
            "valid-project-stale",
            "valid project stale candidate",
            now,
            now,
            "project:test",
            "sha256:valid-project",
        )
    )
    conn.executemany(
        "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
        "access_count, worth_success, worth_failure, tags, decay_multiplier, "
        "project_id, embedding_hash) VALUES (?, ?, 'experience', 'L1', ?, ?, "
        "0, 0, 9, '[]', 0.1, ?, ?)",
        rows,
    )
    conn.commit()
    engine = MutationSpyEngine()

    lifecycle = _run_lifecycle_maintenance(conn, engine)

    assert lifecycle["stale_marked"] == 1
    assert [mutation["memory_id"] for mutation in engine.mutations] == [
        "valid-project-stale"
    ]
    conn.close()


def test_lifecycle_malformed_duplicate_clusters_do_not_occupy_fixed_limit(tmp_path):
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / "malformed-duplicate-lifecycle.sqlite"
    create_test_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = "2026-07-12T00:00:00Z"
    columns = (
        "id",
        "content",
        "memory_type",
        "tier",
        "created_at",
        "last_accessed",
        "access_count",
        "worth_success",
        "worth_failure",
        "tags",
        "project_id",
        "metadata_json",
        "embedding_hash",
        "decay_multiplier",
        "effective_half_life",
    )
    malformed_kinds = (
        "worth_text",
        "worth_negative",
        "worth_infinite",
        "access_text",
        "access_negative",
        "access_fractional",
        "created_blob",
        "created_empty",
        "embedding_blob",
        "embedding_empty",
        "project_blob",
        "project_empty",
        "content_blob",
        "content_empty",
        "id_blob",
        "id_empty",
        "tags_invalid",
        "metadata_invalid",
    )
    rows = []
    for cluster_index in range(24):
        kind = malformed_kinds[cluster_index % len(malformed_kinds)]
        cluster = f"bad-{cluster_index:02d}"
        for member in range(3):
            row = {
                "id": f"{cluster}-{member}",
                "content": f"{cluster}-content",
                "memory_type": "experience",
                "tier": "L1",
                "created_at": now,
                "last_accessed": now,
                "access_count": 0,
                "worth_success": member + 1,
                "worth_failure": 1,
                "tags": '["status:current"]',
                "project_id": f"project:{cluster}",
                "metadata_json": '{"quality":{"status":"current"}}',
                "embedding_hash": f"sha256:{cluster}-{member}",
                "decay_multiplier": 1.0,
                "effective_half_life": 3.0,
            }
            if kind == "worth_text":
                row["worth_success"] = "not-numeric"
            elif kind == "worth_negative":
                row["worth_failure"] = -1
            elif kind == "worth_infinite":
                row["worth_success"] = float("inf")
            elif kind == "access_text":
                row["access_count"] = "not-numeric"
            elif kind == "access_negative":
                row["access_count"] = -1
            elif kind == "access_fractional":
                row["access_count"] = 0.5
            elif kind == "created_blob":
                row["created_at"] = sqlite3.Binary(now.encode())
            elif kind == "created_empty":
                row["created_at"] = " "
            elif kind == "embedding_blob":
                row["embedding_hash"] = sqlite3.Binary(b"sha256:blob")
            elif kind == "embedding_empty":
                row["embedding_hash"] = " "
            elif kind == "project_blob":
                row["project_id"] = sqlite3.Binary(cluster.encode())
            elif kind == "project_empty":
                row["project_id"] = " "
            elif kind == "content_blob":
                row["content"] = sqlite3.Binary(cluster.encode())
            elif kind == "content_empty":
                row["content"] = ""
            elif kind == "id_blob":
                row["id"] = sqlite3.Binary(f"{cluster}-{member}".encode())
            elif kind == "id_empty":
                row["id"] = " " * (member + 1)
            elif kind == "tags_invalid":
                row["tags"] = "not-json"
            elif kind == "metadata_invalid":
                row["metadata_json"] = "not-json"
            rows.append(tuple(row[column] for column in columns))

    def valid_row(memory_id, success):
        return (
            memory_id,
            "zz-valid-duplicate-content",
            "experience",
            "L1",
            now,
            now,
            0,
            success,
            1,
            '["status:current"]',
            "project:zz-valid",
            '{"quality":{"status":"current"}}',
            f"sha256:{memory_id}",
            1.0,
            3.0,
        )

    rows.extend((valid_row("zz-valid-winner", 9), valid_row("zz-valid-loser", 1)))
    conn.executemany(
        f"INSERT INTO memories ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _column in columns)})",
        rows,
    )
    conn.commit()
    engine = MutationSpyEngine()

    lifecycle = _run_lifecycle_maintenance(conn, engine)

    assert lifecycle["conflicts_marked"] == 1
    assert [mutation["memory_id"] for mutation in engine.mutations] == [
        "zz-valid-loser"
    ]
    conn.close()


def test_lifecycle_fractional_worth_is_not_truncated_during_ranking():
    from plastic_promise.cron.scan_memory_decay import _worth

    assert _worth(0, 0.5) < _worth(2, 3)


def test_lifecycle_scan_repeatedly_leaves_unavailable_sources_byte_stable(tmp_path):
    from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
    from plastic_promise.core.synthesis_retrieval import read_memory_version
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / "unavailable-lifecycle.sqlite"
    storage = _SQLiteStorage(str(db_path))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    body = "Unavailable duplicate content must never be lifecycle-mutated again."

    def unavailable(memory_id, state):
        return {
            "id": memory_id,
            "content": body,
            "memory_type": "experience",
            "project_id": "project:unavailable",
            "visibility": "project",
            "tags": [f"status:{state}"],
            "metadata_json": {"quality": {"status": state}},
            "worth_success": 0,
            "worth_failure": 9,
            "decay_multiplier": 0.01,
            "embedding_text": body,
            "embedding_hash": f"sha256:{memory_id}",
            "search_text": body,
        }

    try:
        assert storage.upsert_ordinary("wrong-source", unavailable("wrong-source", "wrong"))
        assert storage.upsert_ordinary(
            "deprecated-source",
            unavailable("deprecated-source", "deprecated"),
        )
        malformed = unavailable("malformed-source", "current")
        assert storage.upsert_ordinary("malformed-source", malformed)
        storage._conn.execute(
            "UPDATE memories SET tags = '{' WHERE id = 'malformed-source'"
        )
        storage._conn.commit()
        discovery = sqlite3.connect(db_path)
        discovery.row_factory = sqlite3.Row
        before_rows = discovery.execute(
            "SELECT * FROM memories ORDER BY id"
        ).fetchall()
        before_version = read_memory_version(discovery)
        before_jobs = discovery.execute(
            "SELECT * FROM store_outbox ORDER BY outbox_id"
        ).fetchall()
        before_lineage = discovery.execute(
            "SELECT * FROM memory_lineage ORDER BY lineage_id"
        ).fetchall()

        first = _run_lifecycle_maintenance(discovery, engine)
        second = _run_lifecycle_maintenance(discovery, engine)

        assert first["stale_marked"] == first["conflicts_marked"] == 0
        assert second["stale_marked"] == second["conflicts_marked"] == 0
        assert discovery.execute("SELECT * FROM memories ORDER BY id").fetchall() == before_rows
        assert read_memory_version(discovery) == before_version
        assert discovery.execute(
            "SELECT * FROM store_outbox ORDER BY outbox_id"
        ).fetchall() == before_jobs
        assert discovery.execute(
            "SELECT * FROM memory_lineage ORDER BY lineage_id"
        ).fetchall() == before_lineage
    finally:
        if "discovery" in locals():
            discovery.close()
        storage._conn.close()


@pytest.mark.parametrize("survivor_change", ["tombstone", "worth"])
def test_lifecycle_duplicate_rejects_changed_survivor(tmp_path, survivor_change):
    from plastic_promise.core.context_engine import ContextEngine, _SQLiteStorage
    from plastic_promise.core.synthesis_retrieval import read_memory_version
    from plastic_promise.cron.scan_memory_decay import _run_lifecycle_maintenance

    db_path = tmp_path / f"lifecycle-survivor-{survivor_change}.sqlite"
    storage = _SQLiteStorage(str(db_path))
    engine = ContextEngine(use_sqlite=False)
    engine._sqlite = storage
    body = "The survivor and loser initially form one live duplicate cluster."

    def source(memory_id, success, failure):
        return {
            "id": memory_id,
            "content": body,
            "memory_type": "experience",
            "project_id": "project:survivor-race",
            "visibility": "project",
            "tags": ["status:current"],
            "metadata_json": {"quality": {"status": "current"}},
            "worth_success": success,
            "worth_failure": failure,
            "decay_multiplier": 1.0,
            "embedding_text": body,
            "embedding_hash": f"sha256:{memory_id}",
            "search_text": body,
        }

    survivor_id = "lifecycle-survivor"
    loser_id = "lifecycle-loser"
    try:
        assert storage.upsert_ordinary(survivor_id, source(survivor_id, 9, 0))
        assert storage.upsert_ordinary(loser_id, source(loser_id, 0, 9))
        discovery = sqlite3.connect(db_path)
        discovery.row_factory = sqlite3.Row
        before_loser = storage._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (loser_id,),
        ).fetchone()
        before_version = read_memory_version(storage._conn)
        before_lineage = storage._conn.execute(
            "SELECT COUNT(*) FROM memory_lineage"
        ).fetchone()[0]
        before_jobs = storage._conn.execute(
            "SELECT COUNT(*) FROM store_outbox"
        ).fetchone()[0]

        class RacingEngine:
            def mutate_ordinary_source(self, memory_id, **mutation):
                if survivor_change == "tombstone":
                    storage._conn.execute(
                        "UPDATE memories SET tags = ?, metadata_json = ? WHERE id = ?",
                        (
                            json.dumps(["status:wrong"]),
                            json.dumps({"quality": {"status": "wrong"}}),
                            survivor_id,
                        ),
                    )
                else:
                    storage._conn.execute(
                        "UPDATE memories SET worth_success = 0, worth_failure = 100 "
                        "WHERE id = ?",
                        (survivor_id,),
                    )
                storage._conn.commit()
                return engine.mutate_ordinary_source(memory_id, **mutation)

        lifecycle = _run_lifecycle_maintenance(discovery, RacingEngine())

        assert lifecycle["conflicts_marked"] == 0
        assert storage._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (loser_id,),
        ).fetchone() == before_loser
        assert read_memory_version(storage._conn) == before_version
        assert storage._conn.execute(
            "SELECT COUNT(*) FROM memory_lineage"
        ).fetchone()[0] == before_lineage
        assert storage._conn.execute(
            "SELECT COUNT(*) FROM store_outbox"
        ).fetchone()[0] == before_jobs
    finally:
        if "discovery" in locals():
            discovery.close()
        storage._conn.close()


def test_recmem_content_update_and_forget_use_coordinator_evidence():
    from plastic_promise.memory.soul_memory import MemoryRecord, RecMem

    class Engine:
        def __init__(self):
            self.mutations = []
            self.patch_calls = []
            self.delete_memory_called = False

        def mutate_ordinary_source(self, memory_id, **mutation):
            self.mutations.append({"memory_id": memory_id, **mutation})
            return {"memory_id": memory_id}

        def get_memory_dict_for_review(self, memory_id):
            record = rec_mem._records.get(memory_id)
            if record is None:
                return None
            return {
                "id": memory_id,
                "content": record.content,
                "category": record.category,
                "metadata_json": {"quality": {"status": "current"}},
                "project_id": "project:test",
                "tags": list(record.tags),
                "worth_failure": record.worth_failure,
                "worth_success": record.worth_success,
            }

        def patch_ordinary_memory(self, memory_id, **patch):
            self.patch_calls.append({"memory_id": memory_id, **patch})
            return {
                "importance": 0.9,
                "worth_success": 0,
                "worth_failure": 0,
            }

        def delete_memory(self, memory_id):
            self.delete_memory_called = True
            return True

    engine = Engine()
    rec_mem = RecMem.__new__(RecMem)
    rec_mem._engine = engine
    rec_mem._records = {
        "source": MemoryRecord(
            "old body",
            memory_id="source",
            activation_weight=0.4,
            worth_success=2,
            worth_failure=3,
        )
    }

    updated = rec_mem.update(
        "source",
        content="new body",
        importance=0.9,
        reset_worth=True,
    )

    assert updated is rec_mem._records["source"]
    assert updated.content == "new body"
    assert updated.activation_weight == 0.9
    assert (updated.worth_success, updated.worth_failure) == (0, 0)
    update_mutation = engine.mutations[0]
    assert update_mutation["operation"] == "replace_content"
    assert update_mutation["reason"] == "recmem:update"
    assert update_mutation["actor"] == "recmem"
    assert update_mutation["call_id"].startswith("internal:recmem:update:")

    assert rec_mem.forget("source", reason="operator request") is True
    forget_mutation = engine.mutations[1]
    assert forget_mutation["operation"] == "forgotten"
    assert forget_mutation["reason"] == "recmem:forget:operator request"
    assert forget_mutation["actor"] == "recmem"
    assert forget_mutation["call_id"].startswith("internal:recmem:forget:")
    assert "source" not in rec_mem._records
    assert engine.delete_memory_called is False


@pytest.mark.asyncio
async def test_scan_memory_decay_binds_periodic_maintenance_to_supplied_engine(monkeypatch):
    from plastic_promise.cron.scan_memory_decay import scan_memory_decay

    engine = MockEngine()
    _PeriodicMaintenanceProbe.received_engine = None
    monkeypatch.setattr(
        "plastic_promise.memory.soul_memory.RecMem", _PeriodicMaintenanceProbe
    )
    monkeypatch.setattr("plastic_promise.memory.soul_memory.EvolveR", _PeriodicEvolveProbe)
    monkeypatch.setattr(
        "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", lambda *args, **kwargs: []
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        create_test_db(db_path)
        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        result = await scan_memory_decay(engine)

        assert _PeriodicMaintenanceProbe.received_engine is engine
        assert result["scanner"] == "scan_memory_decay"
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_scan_memory_decay_never_changes_reserved_synthesis(monkeypatch, reservation_kind):
    from plastic_promise.core.synthesis import ensure_synthesis_schema
    from plastic_promise.cron.scan_memory_decay import scan_memory_decay

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        create_test_db(db_path)
        stale = (datetime.now() - timedelta(days=90)).isoformat()
        memory_id = f"reserved-{reservation_kind}"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO memories (id, content, memory_type, tier, created_at, last_accessed, "
            "access_count, worth_success, worth_failure, tags, decay_multiplier) "
            "VALUES (?, 'governed body', ?, 'L1', ?, ?, 0, 0, 10, '[]', 0.0)",
            (memory_id, "synthesis" if reservation_kind == "type" else "experience", stale, stale),
        )
        ensure_synthesis_schema(conn)
        if reservation_kind == "control":
            conn.execute(
                "INSERT INTO synthesis_artifacts ("
                "memory_id, synthesis_key, status, revision, support_count, validity_scope, "
                "source_fingerprint, created_by_call_id, metadata_json, created_at, updated_at"
                ") VALUES (?, ?, 'draft', 1, 0, 'global', 'fingerprint', 'call', '{}', ?, ?)",
                (memory_id, f"key:{memory_id}", stale, stale),
            )
        conn.commit()
        before_memory = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        before_control = conn.execute(
            "SELECT * FROM synthesis_artifacts WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        conn.close()

        monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
        monkeypatch.setenv("PP_PERIODIC_MAINTENANCE", "0")
        monkeypatch.setattr(
            "plastic_promise.mcp.tools.task_queue.handle_task_enqueue", lambda *args, **kwargs: []
        )
        result = await scan_memory_decay(MockEngine())

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone() == before_memory
        assert conn.execute(
            "SELECT * FROM synthesis_artifacts WHERE memory_id = ?", (memory_id,)
        ).fetchone() == before_control
        conn.close()
        assert result["lifecycle"] == {
            "stale_marked": 0,
            "conflicts_marked": 0,
            "forgotten_candidates": 0,
        }
        assert result["findings"] == 0
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
