"""Regression coverage for scanner and daemon synthesis read boundaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from plastic_promise.core.synthesis import ensure_synthesis_schema


class _Engine:
    pass


def _create_db(db_path: str, *, include_scheduler_tables: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT,
            memory_type TEXT,
            tier TEXT,
            domain TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            entity_ids TEXT,
            created_at TEXT,
            last_accessed TEXT,
            worth_success INTEGER DEFAULT 0,
            worth_failure INTEGER DEFAULT 0,
            activation_weight REAL DEFAULT 0,
            access_count INTEGER DEFAULT 0,
            category TEXT
        )
        """
    )
    ensure_synthesis_schema(conn)
    if include_scheduler_tables:
        conn.execute(
            """
            CREATE TABLE task_queue (
                id TEXT PRIMARY KEY,
                task_type TEXT,
                priority INTEGER,
                status TEXT,
                source_scan TEXT,
                verify_verdict TEXT,
                created_at TEXT,
                claimed_at TEXT,
                claimed_by TEXT,
                escalation_count INTEGER,
                verified_at TEXT,
                verified_by TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE hunter_failure_log (
                id INTEGER PRIMARY KEY,
                agent_name TEXT,
                task_id TEXT,
                failure_type TEXT,
                occurred_at TEXT
            )
            """
        )
    return conn


def _insert_reserved(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    reservation_kind: str,
    content: str = "reserved synthesis body",
    domain: str = "building",
    tags: str = "[]",
    created_at: str | None = None,
    worth_success: int = 0,
    worth_failure: int = 0,
    category: str = "",
) -> None:
    created_at = created_at or datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO memories (
            id, content, memory_type, tier, domain, tags, created_at,
            last_accessed, worth_success, worth_failure, category
        ) VALUES (?, ?, ?, 'L1', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            "synthesis" if reservation_kind == "type" else "experience",
            domain,
            tags,
            created_at,
            created_at,
            worth_success,
            worth_failure,
            category,
        ),
    )
    if reservation_kind == "control":
        conn.execute(
            """
            INSERT INTO synthesis_artifacts (
                memory_id, synthesis_key, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, 'draft', '{}', ?, ?)
            """,
            (memory_id, f"reservation:{memory_id}", created_at, created_at),
        )


async def _capture_enqueue(calls: list[dict], *args, **kwargs):
    calls.append(args[1] if len(args) > 1 else kwargs)
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_architecture_scanner_ignores_reserved_memories(
    monkeypatch, tmp_path, reservation_kind
):
    """Reserved-only domain counts cannot create architecture delegations."""
    db_path = str(tmp_path / "architecture.db")
    conn = _create_db(db_path)
    now = datetime.now().isoformat()
    for index in range(50):
        _insert_reserved(
            conn,
            f"a-{index}",
            reservation_kind=reservation_kind,
            domain="domain_a",
            created_at=now,
        )
    for index in range(2):
        _insert_reserved(
            conn,
            f"b-{index}",
            reservation_kind=reservation_kind,
            domain="domain_b",
            created_at=now,
        )
    _insert_reserved(
        conn,
        "c-0",
        reservation_kind=reservation_kind,
        domain="domain_c",
        created_at=now,
    )
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(*args, **kwargs):
        return await _capture_enqueue(calls, *args, **kwargs)

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    from plastic_promise.cron.scan_architecture import scan_architecture

    result = await scan_architecture(_Engine())

    assert result == {"scanner": "scan_architecture", "findings": 0, "dispatched": 0}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_coupling_scanner_ignores_reserved_tag_statistics(
    monkeypatch, tmp_path, reservation_kind
):
    """Reserved tag co-occurrence cannot create coupling delegations."""
    db_path = str(tmp_path / "coupling.db")
    conn = _create_db(db_path)
    now = datetime.now().isoformat()
    rows = []
    rows.extend((["x", "y"],) * 5)
    rows.extend((["a", "b"],) * 5)
    rows.extend((["a"],) * 6)
    rows.extend((["b"],) * 6)
    rows.extend((["c", "d"],) * 5)
    rows.extend((["c"],) * 6)
    rows.extend((["d"],) * 6)
    rows.extend([f"filler:{index}"] for index in range(61))
    for index, tags in enumerate(rows):
        _insert_reserved(
            conn,
            f"memory-{index}",
            reservation_kind=reservation_kind,
            tags=json.dumps(tags),
            created_at=now,
        )
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(*args, **kwargs):
        return await _capture_enqueue(calls, *args, **kwargs)

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    from plastic_promise.cron.scan_coupling import scan_coupling

    result = await scan_coupling(_Engine())

    assert result == {"scanner": "scan_coupling", "findings": 0, "dispatched": 0}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_scheduler_health_does_not_admit_reserved_previous_audit(
    monkeypatch, tmp_path, reservation_kind
):
    """A draft audit body cannot become the report's previous audit context."""
    db_path = str(tmp_path / "scheduler.db")
    conn = _create_db(db_path, include_scheduler_tables=True)
    _insert_reserved(
        conn,
        "reserved-audit",
        reservation_kind=reservation_kind,
        content=json.dumps(
            {
                "audit_id": "reserved-audit-id",
                "scanner": "scan_scheduler_health",
                "dimensions": {},
            }
        ),
        created_at=datetime.now().isoformat(),
    )
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(*args, **kwargs):
        return await _capture_enqueue(calls, *args, **kwargs)

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health

    result = await scan_scheduler_health(_Engine())

    assert result["findings"] == 0
    reports = [call["payload"] for call in calls if call["task_type"] == "audit_scheduler"]
    assert len(reports) == 1
    assert reports[0]["is_first_audit"] is True
    assert reports[0]["previous_audit_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_innovation_scanner_ignores_reserved_preview_and_statistics(
    monkeypatch, tmp_path, reservation_kind
):
    """Reserved previews and aggregate signals cannot trigger innovation dispatch."""
    db_path = str(tmp_path / "maintenance.db")
    conn = _create_db(db_path)
    old = (datetime.now() - timedelta(hours=3)).isoformat()
    tags = json.dumps(["type:correct_memory", "task:active"])
    for index in range(16):
        _insert_reserved(
            conn,
            f"reserved-{index}",
            reservation_kind=reservation_kind,
            content="reserved preview body",
            tags=tags,
            created_at=old,
            worth_failure=10,
            category="other",
        )
    conn.commit()
    conn.close()

    import daemons.maintenance_daemon as maintenance_daemon

    calls: list[dict] = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
    monkeypatch.setattr(maintenance_daemon, "dispatch_fix_task", capture)
    monkeypatch.setenv("INNOVATION_THRESHOLD", "3")

    await maintenance_daemon.scan_innovation_opportunities()

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation_kind", ["type", "control"])
async def test_maintenance_audit_ignores_reserved_statistics(
    monkeypatch, tmp_path, reservation_kind
):
    """Reserved task and worth records cannot lower the public audit score."""
    db_path = str(tmp_path / "audit.db")
    conn = _create_db(db_path)
    tags = json.dumps(["task:active"])
    for index in range(16):
        _insert_reserved(
            conn,
            f"reserved-{index}",
            reservation_kind=reservation_kind,
            content="reserved audit body",
            tags=tags,
            created_at=datetime.now().isoformat(),
            worth_failure=10,
        )
    conn.commit()
    conn.close()

    import daemons.maintenance_daemon as maintenance_daemon

    class _TrustManager:
        def get(self, _role):
            return 0.6

    class _ContextEngine:
        _dm = None

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class _AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def get(self, *_args, **_kwargs):
            return _Response()

        async def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
    monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        "plastic_promise.defense.soul_enforcer.TrustManager", _TrustManager
    )
    monkeypatch.setattr("plastic_promise.core.context_engine.ContextEngine", _ContextEngine)
    monkeypatch.setattr(maintenance_daemon, "_last_audit_report", "")

    result = await maintenance_daemon.run_audit()

    assert result["scores"]["pipeline"] == 1.0
    assert result["scores"]["memory_quality"] == 1.0
    assert result["notification"] == {"status": "committed", "reason": ""}


@pytest.mark.asyncio
async def test_maintenance_audit_retries_identical_report_until_notify_commits(
    monkeypatch,
    tmp_path,
):
    db_path = str(tmp_path / "audit-retry.db")
    conn = _create_db(db_path)
    conn.close()

    import daemons.maintenance_daemon as maintenance_daemon

    class _TrustManager:
        def get(self, _role):
            return 0.6

    class _ContextEngine:
        _dm = None

    post_outcomes = [
        {"ok": False, "audit_persistence": {"reason": "runtime denied"}},
        {"ok": True},
    ]
    posted = []

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def get(self, *_args, **_kwargs):
            return _Response({"ok": True})

        async def post(self, _url, *, json, timeout):
            posted.append((json, timeout))
            return _Response(post_outcomes.pop(0))

    monkeypatch.setattr(maintenance_daemon, "DB_PATH", db_path)
    monkeypatch.setattr(maintenance_daemon.httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        "plastic_promise.defense.soul_enforcer.TrustManager", _TrustManager
    )
    monkeypatch.setattr("plastic_promise.core.context_engine.ContextEngine", _ContextEngine)
    monkeypatch.setattr(maintenance_daemon, "_last_audit_report", "")

    first = await maintenance_daemon.run_audit()
    second = await maintenance_daemon.run_audit()
    third = await maintenance_daemon.run_audit()

    assert len(posted) == 2
    assert posted[0][0]["content"] == posted[1][0]["content"]
    assert first["notification"] == {"status": "failed", "reason": "runtime denied"}
    assert second["notification"] == {"status": "committed", "reason": ""}
    assert third["notification"] == {
        "status": "skipped",
        "reason": "identical_committed_report",
    }
