"""Tests for Task Queue MCP tools — task_enqueue."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from plastic_promise.core.task_queue_schema import (
    LEGACY_TASK_PROJECT_ID,
    TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID,
    ensure_task_tables,
)
from plastic_promise.mcp.tools.task_queue import (
    _generate_task_id,
    handle_task_abandon,
    handle_task_claim,
    handle_task_complete,
    handle_task_enqueue,
    handle_task_heartbeat,
    handle_task_inbox,
    handle_task_verify,
)

PROJECT_ID = "project:test-task-queue"
OTHER_PROJECT_ID = "project:test-task-queue-other"


def _runtime_context(
    *,
    actor="pi_fixer",
    project_id=PROJECT_ID,
    trust_score=0.60,
    defense_decision="allow",
    tool_name="task_claim",
):
    return {
        "actor": actor,
        "call_id": "call:test-task-authority",
        "project_id": project_id,
        "trust_score": trust_score,
        "trust_tier": "standard",
        "defense_decision": defense_decision,
        "authority_source": "server_runtime_session",
        "tool_name": tool_name,
    }


@pytest.fixture
def test_db_path(tmp_path):
    """Create a temp database with task queue tables for isolated testing."""
    db_path = str(tmp_path / "test_plastic.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_task_tables(conn)
    conn.close()
    return db_path


def test_generate_task_id():
    tid = _generate_task_id()
    assert tid.startswith("t_")
    assert len(tid) > 4


@pytest.mark.parametrize(
    "handler",
    [
        handle_task_enqueue,
        handle_task_claim,
        handle_task_complete,
        handle_task_verify,
        handle_task_inbox,
        handle_task_heartbeat,
        handle_task_abandon,
    ],
)
@pytest.mark.parametrize(
    "project_id",
    [
        None,
        "",
        "unknown",
        "project:unknown",
        "project:legacy-quarantine",
        "not-a-project-id",
        "project:has space",
        "project:control\ncharacter",
        "project:非canonical",
        f"project:{'a' * 249}",
        " project:test-task-queue ",
    ],
)
def test_task_tools_fail_closed_without_canonical_project(
    test_db_path, monkeypatch, handler, project_id
):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    result = json.loads(asyncio.run(handler(None, {"project_id": project_id}))[0].text)

    assert result == {
        "success": False,
        "status": "rejected",
        "project_id": str(project_id or "").strip(),
        "reason": "canonical project_id is required",
    }
    conn = sqlite3.connect(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 0
    conn.close()


def test_task_enqueue_basic(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    result = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "测试委托: 修复重复记忆",
                "to_agent": "pi_fixer",
                "priority": 3,
                "from_agent": "daemon",
                "description": "单元测试创建的委托",
                "source_scan": "test",
            },
        )
    )

    text = json.loads(result[0].text)
    assert text["status"] == "pending"
    assert text["task_id"].startswith("t_")
    assert text["sse_broadcast"] is False  # No SSE in Phase 1
    assert text["review_required"] is False
    assert text["project_id"] == PROJECT_ID


def test_task_enqueue_rejects_cross_project_or_missing_parent(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    parent = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "build_parent",
                    "title": "parent",
                    "to_agent": "pi_builder",
                },
            )
        )[0].text
    )

    denied_cross_project = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": OTHER_PROJECT_ID,
                    "task_type": "build_child",
                    "title": "cross-project child",
                    "to_agent": "pi_builder",
                    "parent_task_id": parent["task_id"],
                },
            )
        )[0].text
    )
    denied_missing = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "build_child",
                    "title": "missing-parent child",
                    "to_agent": "pi_builder",
                    "parent_task_id": "t_missing",
                },
            )
        )[0].text
    )
    accepted = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "build_child",
                    "title": "same-project child",
                    "to_agent": "pi_builder",
                    "parent_task_id": parent["task_id"],
                },
            )
        )[0].text
    )

    assert denied_cross_project["status"] == "rejected"
    assert denied_missing["status"] == "rejected"
    assert accepted["status"] == "pending"
    conn = sqlite3.connect(test_db_path)
    row = conn.execute(
        "SELECT project_id, parent_task_id FROM task_queue WHERE id = ?",
        (accepted["task_id"],),
    ).fetchone()
    conn.close()
    assert row == (PROJECT_ID, parent["task_id"])


def test_task_schema_additively_quarantines_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy-task-queue.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE task_queue ("
        "id TEXT PRIMARY KEY, task_type TEXT NOT NULL, priority INTEGER DEFAULT 3, "
        "status TEXT DEFAULT 'pending', title TEXT NOT NULL, description TEXT, payload TEXT, "
        "from_agent TEXT DEFAULT 'daemon', to_agent TEXT NOT NULL, domain TEXT, "
        "claimed_by TEXT, claimed_at TEXT, heartbeat_at TEXT, done_at TEXT, verified_at TEXT, "
        "verified_by TEXT, verify_verdict TEXT, result TEXT, escalation_count INTEGER DEFAULT 0, "
        "max_escalations INTEGER DEFAULT 3, last_escalation_at TEXT, "
        "timeout_seconds INTEGER DEFAULT 300, memory_id TEXT, principle_id TEXT, "
        "source_scan TEXT, parent_task_id TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO task_queue (id, task_type, title, to_agent) "
        "VALUES ('legacy-task', 'fix_memory', 'legacy', 'pi_fixer')"
    )

    ensure_task_tables(conn)
    ensure_task_tables(conn)

    row = conn.execute(
        "SELECT project_id, status FROM task_queue WHERE id = 'legacy-task'"
    ).fetchone()
    migration = conn.execute(
        "SELECT quarantined_rows FROM task_queue_schema_migrations WHERE migration_id = ?",
        (TASK_QUEUE_PROJECT_SCOPE_MIGRATION_ID,),
    ).fetchone()
    conn.close()
    assert row == (LEGACY_TASK_PROJECT_ID, "pending")
    assert migration == (1,)


def test_task_queue_isolates_dedup_inbox_and_mutations_by_project(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    shared = {
        "task_type": "investigate_coupling",
        "title": "same finding",
        "to_agent": "pi_reviewer",
        "priority": 3,
        "source_scan": "scan_coupling",
        "payload": {"type": "same-finding", "subject": "shared"},
    }

    first = json.loads(
        asyncio.run(handle_task_enqueue(engine, {"project_id": PROJECT_ID, **shared}))[0].text
    )
    other = json.loads(
        asyncio.run(handle_task_enqueue(engine, {"project_id": OTHER_PROJECT_ID, **shared}))[0].text
    )
    duplicate = json.loads(
        asyncio.run(handle_task_enqueue(engine, {"project_id": PROJECT_ID, **shared}))[0].text
    )

    assert first["status"] == other["status"] == "pending"
    assert first["task_id"] != other["task_id"]
    assert duplicate["status"] == "duplicate"
    assert duplicate["existing_task_id"] == first["task_id"]

    inbox = json.loads(
        asyncio.run(
            handle_task_inbox(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_reviewer",
                    "trust_score": 0.60,
                    "filter_status": "all",
                },
            )
        )[0].text
    )
    assert [task["id"] for task in inbox["tasks"]] == [first["task_id"]]
    assert inbox["tasks"][0]["project_id"] == PROJECT_ID

    wrong_claim = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": OTHER_PROJECT_ID,
                    "agent_name": "pi_reviewer",
                    "task_id": first["task_id"],
                    "trust_score": 0.60,
                },
            )
        )[0].text
    )
    assert wrong_claim["success"] is False

    claimed = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_reviewer",
                    "task_id": first["task_id"],
                    "trust_score": 0.60,
                },
            )
        )[0].text
    )
    assert claimed["success"] is True

    for handler, handler_args in (
        (handle_task_heartbeat, {"agent_name": "pi_reviewer"}),
        (handle_task_abandon, {"agent_name": "pi_reviewer"}),
        (handle_task_complete, {"agent_name": "pi_reviewer", "result": "cross-project"}),
    ):
        denied = json.loads(
            asyncio.run(
                handler(
                    engine,
                    {
                        "project_id": OTHER_PROJECT_ID,
                        "task_id": first["task_id"],
                        **handler_args,
                    },
                )
            )[0].text
        )
        assert denied["success"] is False

    completed = json.loads(
        asyncio.run(
            handle_task_complete(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": first["task_id"],
                    "agent_name": "pi_reviewer",
                    "result": "done",
                },
            )
        )[0].text
    )
    assert completed["success"] is True

    denied_verify = json.loads(
        asyncio.run(
            handle_task_verify(
                engine,
                {
                    "project_id": OTHER_PROJECT_ID,
                    "task_id": first["task_id"],
                    "verdict": "accepted",
                },
            )
        )[0].text
    )
    assert denied_verify["success"] is False

    conn = sqlite3.connect(test_db_path)
    conn.row_factory = sqlite3.Row
    parent = conn.execute(
        "SELECT project_id, status FROM task_queue WHERE id=?", (first["task_id"],)
    ).fetchone()
    verifier = conn.execute(
        "SELECT project_id FROM task_queue WHERE id=?", (completed["verification_task_id"],)
    ).fetchone()
    conn.close()
    assert dict(parent) == {"project_id": PROJECT_ID, "status": "done"}
    assert verifier["project_id"] == PROJECT_ID


def test_scanner_enqueue_deduplicates_matching_pending_payload_without_time_window(
    test_db_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    args = {
        "project_id": PROJECT_ID,
        "task_type": "investigate_coupling",
        "title": "stable coupling finding",
        "to_agent": "pi_reviewer",
        "priority": 3,
        "source_scan": "scan_coupling",
        "payload": {"type": "tag_cooccurrence_anomaly", "tags": ["a", "b"]},
    }

    first = json.loads(asyncio.run(handle_task_enqueue(engine, args))[0].text)
    conn = sqlite3.connect(test_db_path)
    conn.execute(
        "UPDATE task_queue SET created_at = datetime('now', '-30 days') WHERE id = ?",
        (first["task_id"],),
    )
    conn.commit()
    conn.close()

    second = json.loads(asyncio.run(handle_task_enqueue(engine, args))[0].text)

    assert second == {
        "project_id": PROJECT_ID,
        "status": "duplicate",
        "existing_task_id": first["task_id"],
        "reason": (
            "Pending investigate_coupling from scan_coupling already exists for this payload"
        ),
    }
    conn = sqlite3.connect(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 1
    conn.close()


def test_scanner_enqueue_does_not_deduplicate_against_ordinary_pending_task(
    test_db_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    payload = {"type": "tag_cooccurrence_anomaly", "tags": ["a", "b"]}

    ordinary = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "investigate_coupling",
                    "title": "manual coupling investigation",
                    "to_agent": "pi_reviewer",
                    "priority": 3,
                    "payload": payload,
                },
            )
        )[0].text
    )
    scanner = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "investigate_coupling",
                    "title": "scanner coupling finding",
                    "to_agent": "pi_reviewer",
                    "priority": 3,
                    "source_scan": "scan_coupling",
                    "payload": payload,
                },
            )
        )[0].text
    )

    assert ordinary["status"] == "pending"
    assert scanner["status"] == "pending"
    assert scanner["task_id"] != ordinary["task_id"]
    conn = sqlite3.connect(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 2
    conn.close()


def test_scanner_enqueue_dedup_is_atomic_across_connections(test_db_path, monkeypatch):
    from plastic_promise.mcp.tools import task_queue

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    args = {
        "project_id": PROJECT_ID,
        "task_type": "investigate_coupling",
        "title": "concurrent coupling finding",
        "to_agent": "pi_reviewer",
        "priority": 3,
        "source_scan": "scan_coupling",
        "payload": {"type": "tag_cooccurrence_anomaly", "tags": ["a", "b"]},
    }
    original_get_conn = task_queue._get_conn
    ready = threading.Barrier(2)

    def synchronized_connection():
        conn = original_get_conn()
        ready.wait(timeout=5)
        return conn

    monkeypatch.setattr(task_queue, "_get_conn", synchronized_connection)

    def enqueue():
        return json.loads(asyncio.run(handle_task_enqueue(engine, args))[0].text)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: enqueue(), range(2)))

    assert sorted(result["status"] for result in results) == ["duplicate", "pending"]
    conn = sqlite3.connect(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 1
    conn.close()


def test_task_enqueue_d_rank_rejected(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    result = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "D级猎人尝试挂委托",
                "to_agent": "pi_fixer",
                "from_agent": "unknown_agent",
                "from_trust_score": 0.20,  # D级
                "priority": 3,
            },
        )
    )

    text = json.loads(result[0].text)
    assert text["status"] == "rejected"
    assert "降级猎人" in text["reason"]


def test_task_enqueue_review_subtask_inherits_project(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    result = json.loads(
        asyncio.run(
            handle_task_enqueue(
                None,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "audit_architecture",
                    "title": "C级审批委托",
                    "to_agent": "pi_reviewer",
                    "from_agent": "pi_builder",
                    "from_trust_score": 0.40,
                    "priority": 2,
                },
            )
        )[0].text
    )

    assert result["status"] == "pending_review"
    assert result["project_id"] == PROJECT_ID
    conn = sqlite3.connect(test_db_path)
    rows = conn.execute(
        "SELECT id, project_id FROM task_queue WHERE id IN (?, ?) ORDER BY id",
        (result["task_id"], result["review_task_id"]),
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {PROJECT_ID}


def test_task_enqueue_review_parent_and_child_roll_back_together(test_db_path, monkeypatch):
    from plastic_promise.mcp.tools import task_queue as task_queue_module

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    monkeypatch.setattr(task_queue_module, "_generate_task_id", lambda: "t_same")

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(
            handle_task_enqueue(
                None,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "audit_architecture",
                    "title": "atomic C review",
                    "to_agent": "pi_reviewer",
                    "from_agent": "pi_builder",
                    "from_trust_score": 0.40,
                    "priority": 2,
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0] == 0
    conn.close()


# ═══════════════════════════════════════════════════════════════
# task_claim tests
# ═══════════════════════════════════════════════════════════════


def test_task_claim_success(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    # First enqueue a task
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "待揭榜委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]

    # Now claim it
    r2 = asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert "[OK]" in data["match"]
    assert data["rank"]["rank"] == "B"


def test_task_claim_rank_mismatch(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "audit_architecture",
                "title": "A级委托",
                "to_agent": "claude",
                "priority": 2,  # priority=2 → rank A
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]

    r2 = asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.55,  # B级接A级 → 越级
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "!!!" in data["match"]


def test_task_claim_double_prevented(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()

    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "单次委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]

    # First claim succeeds
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    # Second claim must fail (already claimed)
    r2 = asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_reviewer",
                "task_id": task_id,
                "trust_score": 0.70,
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is False
    assert "已被揭榜" in data["reason"]


def test_public_force_claim_requires_high_trust_reviewer_authority(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                None,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "audit_architecture",
                    "title": "S task",
                    "to_agent": "claude",
                    "priority": 1,
                },
            )
        )[0].text
    )["task_id"]

    denied = json.loads(
        asyncio.run(
            handle_task_claim(
                None,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "task_id": task_id,
                    "trust_score": 0.60,
                    "force": True,
                },
                _runtime_context=_runtime_context(),
            )
        )[0].text
    )
    assert denied["reason"] == "task_force_claim_authority_required"


# ═══════════════════════════════════════════════════════════════
# task_complete tests
# ═══════════════════════════════════════════════════════════════


def test_task_complete_creates_verify_subtask(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "可完成委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    r2 = asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "修复完成：移除3条重复记忆",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["status"] == "done"
    assert data["verification_task_id"] is not None  # Auto-created verify task for Claude


def test_task_complete_wrong_agent(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "我的委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    r2 = asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_builder",  # Different agent!
                "result": "不是我揭的",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is False


def test_task_complete_is_single_transition_and_creates_one_verification_task(
    test_db_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "complete once",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    first = json.loads(
        asyncio.run(
            handle_task_complete(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                    "result": "first",
                },
            )
        )[0].text
    )
    second = json.loads(
        asyncio.run(
            handle_task_complete(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                    "result": "second",
                },
            )
        )[0].text
    )

    assert first["success"] is True
    assert second["success"] is False
    assert second["reason"] == "task_state_conflict"
    conn = sqlite3.connect(test_db_path)
    verification_count = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE project_id=? "
        "AND task_type='verify_task' AND parent_task_id=?",
        (PROJECT_ID, task_id),
    ).fetchone()[0]
    result_text = conn.execute(
        "SELECT result FROM task_queue WHERE project_id=? AND id=?",
        (PROJECT_ID, task_id),
    ).fetchone()[0]
    conn.close()
    assert verification_count == 1
    assert result_text == "first"


# ═══════════════════════════════════════════════════════════════
# task_verify tests
# ═══════════════════════════════════════════════════════════════


def test_task_verify_accepted_boosts_trust(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "验收测试委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "done",
            },
        )
    )

    r2 = asyncio.run(
        handle_task_verify(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "verdict": "accepted",
                "verified_by": "claude",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "verified"
    assert data["trust_adjustment"]["delta"] == 0.02


def test_task_verify_rejected_deducts(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "打回测试委托",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "done",
            },
        )
    )

    r2 = asyncio.run(
        handle_task_verify(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "verdict": "rejected",
                "verified_by": "claude",
                "comment": "修复不彻底",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["new_status"] == "reassigned"
    assert data["trust_adjustment"]["delta"] == -0.03
    conn = sqlite3.connect(test_db_path)
    project_id = conn.execute(
        "SELECT project_id FROM task_queue WHERE id=?", (data["new_task_id"],)
    ).fetchone()[0]
    conn.close()
    assert project_id == PROJECT_ID


def test_task_runtime_authority_rejects_forged_project_actor_trust_and_reviewer(
    test_db_path, monkeypatch
):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "server authority",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]

    forged_project = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": OTHER_PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "task_id": task_id,
                    "trust_score": 0.60,
                },
                _runtime_context=_runtime_context(),
            )
        )[0].text
    )
    forged_actor = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_builder",
                    "task_id": task_id,
                    "trust_score": 0.60,
                },
                _runtime_context=_runtime_context(),
            )
        )[0].text
    )
    forged_trust = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "task_id": task_id,
                    "trust_score": 1.0,
                },
                _runtime_context=_runtime_context(),
            )
        )[0].text
    )

    assert forged_project["reason"] == "task_project_scope_mismatch"
    assert forged_actor["reason"] == "task_actor_mismatch"
    assert forged_trust["reason"] == "task_trust_declaration_mismatch"

    claimed = json.loads(
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "task_id": task_id,
                    "trust_score": 0.60,
                },
                _runtime_context=_runtime_context(),
            )
        )[0].text
    )
    assert claimed["success"] is True
    completed = json.loads(
        asyncio.run(
            handle_task_complete(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                    "result": "done",
                },
                _runtime_context=_runtime_context(tool_name="task_complete"),
            )
        )[0].text
    )
    assert completed["success"] is True

    forged_reviewer = json.loads(
        asyncio.run(
            handle_task_verify(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "verdict": "accepted",
                    "verified_by": "claude",
                },
                _runtime_context=_runtime_context(actor="pi_fixer", tool_name="task_verify"),
            )
        )[0].text
    )
    assert forged_reviewer["reason"] == "task_actor_mismatch"

    ordinary_reviewer = json.loads(
        asyncio.run(
            handle_task_verify(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "verdict": "accepted",
                    "verified_by": "pi_fixer",
                },
                _runtime_context=_runtime_context(actor="pi_fixer", tool_name="task_verify"),
            )
        )[0].text
    )
    assert ordinary_reviewer["reason"] == "task_reviewer_authority_required"

    accepted = json.loads(
        asyncio.run(
            handle_task_verify(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "verdict": "accepted",
                    "verified_by": "codex",
                },
                _runtime_context=_runtime_context(
                    actor="codex",
                    trust_score=0.80,
                    tool_name="task_verify",
                ),
            )
        )[0].text
    )
    assert accepted["success"] is True


# ═══════════════════════════════════════════════════════════════
# task_inbox tests
# ═══════════════════════════════════════════════════════════════


def test_task_inbox_default_pending(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    # Enqueue 2 tasks for pi_fixer
    asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "任务A",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "gc_cleanup",
                "title": "任务B",
                "to_agent": "pi_fixer",
                "priority": 4,
            },
        )
    )

    r = asyncio.run(
        handle_task_inbox(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "trust_score": 0.60,
            },
        )
    )
    data = json.loads(r[0].text)
    assert data["agent_name"] == "pi_fixer"
    assert data["rank"]["rank"] == "B"
    assert data["stats"]["available"] >= 2


def test_task_inbox_rank_match_display(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "audit_architecture",
                "title": "A级任务",
                "to_agent": "claude",
                "priority": 2,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]

    r2 = asyncio.run(
        handle_task_inbox(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "trust_score": 0.55,
                "filter_status": "pending",
            },
        )
    )
    data = json.loads(r2[0].text)
    task = next(t for t in data["tasks"] if t["id"] == task_id)
    assert task["can_claim"] is False
    assert "!!!" in task["match"]


def test_task_inbox_clamps_limit_to_bounded_page(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    conn = sqlite3.connect(test_db_path)
    conn.executemany(
        "INSERT INTO task_queue "
        "(id, project_id, task_type, title, to_agent, status) "
        "VALUES (?, ?, 'fix_memory', ?, 'pi_fixer', 'pending')",
        [(f"task-{index:03d}", PROJECT_ID, f"task {index}") for index in range(120)],
    )
    conn.commit()
    conn.close()

    inbox = json.loads(
        asyncio.run(
            handle_task_inbox(
                None,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "trust_score": 0.60,
                    "filter_status": "all",
                    "limit": 1000000,
                },
            )
        )[0].text
    )

    assert len(inbox["tasks"]) == 100


# ═══════════════════════════════════════════════════════════════
# task_heartbeat tests
# ═══════════════════════════════════════════════════════════════


def test_task_heartbeat(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "心跳测试",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    r2 = asyncio.run(
        handle_task_heartbeat(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["overdue"] is False


def test_task_heartbeat_fails_after_completion(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "no heartbeat after done",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "done",
            },
        )
    )

    heartbeat = json.loads(
        asyncio.run(
            handle_task_heartbeat(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                },
            )
        )[0].text
    )
    assert heartbeat["success"] is False
    assert heartbeat["reason"] == "task_state_conflict"


# ═══════════════════════════════════════════════════════════════
# task_abandon tests
# ═══════════════════════════════════════════════════════════════


def test_task_abandon(test_db_path, monkeypatch):
    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)

    class MockEngine:
        pass

    engine = MockEngine()
    r = asyncio.run(
        handle_task_enqueue(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_type": "fix_memory",
                "title": "弃单测试",
                "to_agent": "pi_fixer",
                "priority": 3,
            },
        )
    )
    task_id = json.loads(r[0].text)["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    r2 = asyncio.run(
        handle_task_abandon(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "reason": "太难了",
            },
        )
    )
    data = json.loads(r2[0].text)
    assert data["success"] is True
    assert data["penalty"]["type"] == "abandoned"


def test_task_claim_rolls_back_state_when_runtime_event_fails(test_db_path, monkeypatch):
    from plastic_promise.core import event_protocol

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "claim transaction",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]

    def fail_runtime_event(*_args, **_kwargs):
        raise RuntimeError("runtime event failed")

    monkeypatch.setattr(event_protocol, "record_runtime_event", fail_runtime_event)
    with pytest.raises(RuntimeError, match="runtime event failed"):
        asyncio.run(
            handle_task_claim(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "agent_name": "pi_fixer",
                    "task_id": task_id,
                    "trust_score": 0.60,
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    state = conn.execute(
        "SELECT status, claimed_by FROM task_queue WHERE id=?", (task_id,)
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE event_name='task_claim' AND json_extract(metadata_json, '$.task_id')=?",
        (task_id,),
    ).fetchone()[0]
    conn.close()
    assert state == ("pending", None)
    assert event_count == 0


def test_task_complete_rolls_back_state_and_verifier_when_runtime_event_fails(
    test_db_path, monkeypatch
):
    from plastic_promise.core import event_protocol

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "complete transaction",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    def fail_runtime_event(*_args, **_kwargs):
        raise RuntimeError("runtime event failed")

    monkeypatch.setattr(event_protocol, "record_runtime_event", fail_runtime_event)
    with pytest.raises(RuntimeError, match="runtime event failed"):
        asyncio.run(
            handle_task_complete(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                    "result": "should roll back",
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    state = conn.execute("SELECT status, result FROM task_queue WHERE id=?", (task_id,)).fetchone()
    verifier_count = conn.execute(
        "SELECT COUNT(*) FROM task_queue "
        "WHERE project_id=? AND parent_task_id=? AND task_type='verify_task'",
        (PROJECT_ID, task_id),
    ).fetchone()[0]
    event_count = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE event_name='task_complete' AND json_extract(metadata_json, '$.task_id')=?",
        (task_id,),
    ).fetchone()[0]
    conn.close()
    assert state == ("claimed", None)
    assert verifier_count == 0
    assert event_count == 0


def test_task_verify_rolls_back_state_when_runtime_event_fails(test_db_path, monkeypatch):
    from plastic_promise.core import event_protocol

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "verify transaction",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    asyncio.run(
        handle_task_complete(
            engine,
            {
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "agent_name": "pi_fixer",
                "result": "ready for review",
            },
        )
    )

    def fail_runtime_event(*_args, **_kwargs):
        raise RuntimeError("runtime event failed")

    monkeypatch.setattr(event_protocol, "record_runtime_event", fail_runtime_event)
    with pytest.raises(RuntimeError, match="runtime event failed"):
        asyncio.run(
            handle_task_verify(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "verdict": "accepted",
                    "verified_by": "claude",
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    state = conn.execute(
        "SELECT status, verified_by, verify_verdict FROM task_queue WHERE id=?",
        (task_id,),
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE event_name='task_verify' AND json_extract(metadata_json, '$.task_id')=?",
        (task_id,),
    ).fetchone()[0]
    conn.close()
    assert state == ("done", None, None)
    assert event_count == 0


def test_task_abandon_rolls_back_state_and_penalty_when_runtime_event_fails(
    test_db_path, monkeypatch
):
    from plastic_promise.core import event_protocol
    from plastic_promise.defense import soul_enforcer

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "abandon transaction",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )

    decays = []

    class FakeTrustManager:
        def get(self, _target):
            return 0.60

        def decay(self, *args, **kwargs):
            decays.append((args, kwargs))

    def fail_runtime_event(*_args, **_kwargs):
        raise RuntimeError("runtime event failed")

    monkeypatch.setattr(soul_enforcer, "TrustManager", FakeTrustManager)
    monkeypatch.setattr(event_protocol, "record_runtime_event", fail_runtime_event)
    with pytest.raises(RuntimeError, match="runtime event failed"):
        asyncio.run(
            handle_task_abandon(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                    "reason": "should roll back",
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    state = conn.execute(
        "SELECT status, claimed_by FROM task_queue WHERE id=?", (task_id,)
    ).fetchone()
    failure_count = conn.execute(
        "SELECT COUNT(*) FROM hunter_failure_log WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    event_count = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE event_name='task_abandon' AND json_extract(metadata_json, '$.task_id')=?",
        (task_id,),
    ).fetchone()[0]
    conn.close()
    assert state == ("claimed", "pi_fixer")
    assert failure_count == 0
    assert event_count == 0
    assert decays == []


def test_task_heartbeat_rolls_back_transition_when_runtime_event_fails(test_db_path, monkeypatch):
    from plastic_promise.core import event_protocol

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    engine = type("MockEngine", (), {})()
    task_id = json.loads(
        asyncio.run(
            handle_task_enqueue(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_type": "fix_memory",
                    "title": "heartbeat transaction",
                    "to_agent": "pi_fixer",
                    "priority": 3,
                },
            )
        )[0].text
    )["task_id"]
    asyncio.run(
        handle_task_claim(
            engine,
            {
                "project_id": PROJECT_ID,
                "agent_name": "pi_fixer",
                "task_id": task_id,
                "trust_score": 0.60,
            },
        )
    )
    conn = sqlite3.connect(test_db_path)
    original = conn.execute(
        "SELECT status, heartbeat_at FROM task_queue WHERE id=?", (task_id,)
    ).fetchone()
    conn.close()

    def fail_runtime_event(*_args, **_kwargs):
        raise RuntimeError("runtime event failed")

    monkeypatch.setattr(event_protocol, "record_runtime_event", fail_runtime_event)
    with pytest.raises(RuntimeError, match="runtime event failed"):
        asyncio.run(
            handle_task_heartbeat(
                engine,
                {
                    "project_id": PROJECT_ID,
                    "task_id": task_id,
                    "agent_name": "pi_fixer",
                },
            )
        )

    conn = sqlite3.connect(test_db_path)
    state = conn.execute(
        "SELECT status, heartbeat_at FROM task_queue WHERE id=?", (task_id,)
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE event_name='task_heartbeat' AND json_extract(metadata_json, '$.task_id')=?",
        (task_id,),
    ).fetchone()[0]
    conn.close()
    assert state == original
    assert event_count == 0


def test_task_board_tools_are_exposed_by_mcp_server():
    from plastic_promise.mcp.server import list_tools

    tools = asyncio.run(list_tools())
    names = {t.name for t in tools}
    assert {
        "task_enqueue",
        "task_claim",
        "task_complete",
        "task_verify",
        "task_inbox",
        "task_heartbeat",
        "task_abandon",
    }.issubset(names)


@pytest.mark.asyncio
async def test_public_task_dispatch_fails_closed_without_session_init(test_db_path, monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    class Session:
        pass

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: Session(), raising=False)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *args, **kwargs: None)

    response = await mcp_server.call_tool(
        "task_inbox",
        {
            "project_id": PROJECT_ID,
            "agent_name": "codex",
            "trust_score": 0.60,
        },
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is False
    assert payload["reason"] == "task_runtime_authorization_required"


@pytest.mark.asyncio
async def test_successful_session_init_binds_project_and_dispatches_private_task_authority(
    test_db_path, monkeypatch
):
    from plastic_promise.mcp import server as mcp_server
    from plastic_promise.mcp.tools import task_queue as task_queue_module

    class Session:
        pass

    class SkillEngine:
        async def exec(self, name, arguments, caller):
            assert name == "session-init"
            return SimpleNamespace(
                data={"stage_session_id": "stage:test", "project_id": PROJECT_ID},
                skill_name="session-init",
                success=True,
                degrade_log=[],
                errors=[],
                audit_trail={},
            )

    session = Session()
    captured = []

    async def capture(_engine, _arguments, *, _runtime_context=None):
        captured.append(_runtime_context)
        return [
            type(
                "Response",
                (),
                {"text": json.dumps({"success": True, "project_id": PROJECT_ID})},
            )()
        ]

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    monkeypatch.setenv("PP_MCP_RUNTIME_ACTOR", "codex-desktop")
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: session, raising=False)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: SkillEngine())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mcp_server,
        "_mutation_runtime_context",
        lambda tool_name, arguments=None: {
            "actor": "codex",
            "call_id": "call:dispatcher",
            "project_id": "project:process-default",
            "project_policy": "balanced",
            "trust_score": 0.80,
            "trust_tier": "autonomous",
            "defense_decision": "allow",
            "tool_name": tool_name,
        },
    )
    monkeypatch.setattr(task_queue_module, "handle_task_inbox", capture)

    init_response = await mcp_server.call_tool(
        "session-init",
        {"task_description": "bind task project"},
    )
    init_payload = json.loads(init_response[0].text)
    assert init_payload["success"] is True
    assert init_payload["project_id"] == PROJECT_ID
    assert init_payload["diagnostics"]["task_session_binding"]["success"] is True

    await mcp_server.call_tool(
        "task_inbox",
        {
            "project_id": PROJECT_ID,
            "agent_name": "codex",
            "trust_score": 0.80,
        },
    )

    assert captured == [
        {
            "actor": "codex",
            "call_id": "call:dispatcher",
            "project_id": PROJECT_ID,
            "project_policy": "balanced",
            "trust_score": 0.80,
            "trust_tier": "autonomous",
            "defense_decision": "allow",
            "tool_name": "task_inbox",
            "authority_source": "server_runtime_session",
        }
    ]


@pytest.mark.asyncio
async def test_session_init_reports_task_binding_degradation_when_actor_is_unconfigured(
    test_db_path, monkeypatch
):
    from plastic_promise.mcp import server as mcp_server

    class Session:
        pass

    class SkillEngine:
        async def exec(self, name, arguments, caller):
            assert name == "session-init"
            return SimpleNamespace(
                data={"stage_session_id": "stage:test", "project_id": PROJECT_ID},
                skill_name="session-init",
                success=True,
                degrade_log=[],
                errors=[],
                audit_trail={},
            )

    monkeypatch.setenv("PLASTIC_DB_PATH", test_db_path)
    monkeypatch.delenv("PP_MCP_RUNTIME_ACTOR", raising=False)
    monkeypatch.setattr(mcp_server, "_current_mcp_session", lambda: Session(), raising=False)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_skill_engine", lambda: SkillEngine())
    monkeypatch.setattr(mcp_server, "_record_tool_runtime_event", lambda *args, **kwargs: None)

    response = await mcp_server.call_tool(
        "session-init",
        {"task_description": "report task binding degradation"},
    )
    payload = json.loads(response[0].text)

    assert payload["success"] is True
    assert payload["project_id"] == PROJECT_ID
    assert payload["degraded"] is True
    assert "task_session_binding:task_runtime_actor_unconfigured" in payload["warnings"]
    assert payload["diagnostics"]["task_session_binding"] == {
        "success": False,
        "project_id": PROJECT_ID,
        "reason": "task_runtime_actor_unconfigured",
    }
