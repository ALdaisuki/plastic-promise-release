"""Focused project-scope contracts for internal scanners and Maintenance."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from plastic_promise.core.synthesis import ensure_synthesis_schema


class _Engine:
    pass


def _create_scanner_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'experience',
            source TEXT NOT NULL DEFAULT 'test',
            owner TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'L1',
            scope TEXT NOT NULL DEFAULT 'project',
            category TEXT NOT NULL DEFAULT 'other',
            importance REAL NOT NULL DEFAULT 0.5,
            entity_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            worth_success REAL NOT NULL DEFAULT 0,
            worth_failure REAL NOT NULL DEFAULT 0,
            activation_weight REAL NOT NULL DEFAULT 0,
            last_accessed TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            domain TEXT NOT NULL DEFAULT 'uncategorized',
            project_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding_hash TEXT NOT NULL DEFAULT 'sha256:test-index',
            decay_multiplier REAL NOT NULL DEFAULT 1.0,
            effective_half_life REAL NOT NULL DEFAULT 3.0
        )
        """
    )
    ensure_synthesis_schema(conn)
    conn.commit()
    return conn


def _insert_memory(
    conn: sqlite3.Connection,
    memory_id: str,
    project_id: str,
    *,
    domain: str = "building",
    tags: list[str] | None = None,
    tier: str = "L1",
    created_at: str | None = None,
    last_accessed: str | None = None,
) -> None:
    created_at = created_at or datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO memories (
            id, content, tier, domain, tags, created_at, last_accessed, project_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            f"memory {memory_id}",
            tier,
            domain,
            json.dumps(tags or []),
            created_at,
            last_accessed or created_at,
            project_id,
        ),
    )


def test_project_scope_resolution_is_unique_only_and_explicit_invalid_fails_closed():
    from plastic_promise.cron.project_scope import (
        ProjectScopeResolutionError,
        list_memory_project_ids,
        resolve_memory_project_id,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memories (project_id TEXT)")
    conn.executemany(
        "INSERT INTO memories(project_id) VALUES (?)",
        [
            ("project:alpha",),
            ("project:system-governance",),
            ("project:legacy-global",),
            ("project:legacy-quarantine",),
            (" project:dirty ",),
            ("project:has space",),
            ("project:control\ncharacter",),
            ("project:非canonical",),
            ("project:unknown",),
        ],
    )

    assert list_memory_project_ids(conn) == ("project:alpha",)
    assert resolve_memory_project_id(conn) == "project:alpha"

    for invalid in (
        "",
        "unknown",
        "project:unknown",
        "project:",
        "project:legacy-global",
        "project:legacy-quarantine",
        "alpha",
        "project:has space",
        "project:control\ncharacter",
        "project:非canonical",
        f"project:{'a' * 249}",
        " project:alpha ",
    ):
        with pytest.raises(ProjectScopeResolutionError, match="project_scope_invalid"):
            resolve_memory_project_id(conn, invalid)

    conn.execute("INSERT INTO memories(project_id) VALUES ('project:beta')")
    with pytest.raises(ProjectScopeResolutionError, match="project_scope_ambiguous"):
        resolve_memory_project_id(conn)
    conn.close()


@pytest.mark.asyncio
async def test_architecture_scanner_filters_before_aggregation(monkeypatch, tmp_path):
    from plastic_promise.cron.scan_architecture import scan_architecture

    db_path = str(tmp_path / "architecture.sqlite")
    conn = _create_scanner_db(db_path)
    for domain in ("alpha", "beta", "gamma"):
        for index in range(4):
            _insert_memory(conn, f"a-{domain}-{index}", "project:alpha", domain=domain)
    for index in range(50):
        _insert_memory(conn, f"b-hot-{index}", "project:beta", domain="hot")
    for index in range(2):
        _insert_memory(conn, f"b-warm-{index}", "project:beta", domain="warm")
    _insert_memory(conn, "b-cold", "project:beta", domain="cold")
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(_engine, arguments):
        calls.append(arguments)
        return []

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    alpha = await scan_architecture(_Engine(), project_id="project:alpha")
    assert alpha == {"scanner": "scan_architecture", "findings": 0, "dispatched": 0}
    assert calls == []

    beta = await scan_architecture(_Engine(), project_id="project:beta")
    assert beta["findings"] >= 1
    assert calls
    assert all(call["project_id"] == "project:beta" for call in calls)

    ambiguous = await scan_architecture(_Engine())
    assert ambiguous["failure_code"] == "project_scope_ambiguous"


@pytest.mark.asyncio
async def test_coupling_scanner_filters_before_aggregation(monkeypatch, tmp_path):
    from plastic_promise.cron.scan_coupling import scan_coupling

    db_path = str(tmp_path / "coupling.sqlite")
    conn = _create_scanner_db(db_path)
    for index in range(12):
        _insert_memory(conn, f"a-{index}", "project:alpha", tags=[f"alpha:{index}"])

    beta_rows: list[list[str]] = []
    beta_rows.extend((["x", "y"],) * 5)
    beta_rows.extend((["a", "b"],) * 5)
    beta_rows.extend((["a"],) * 6)
    beta_rows.extend((["b"],) * 6)
    beta_rows.extend((["c", "d"],) * 5)
    beta_rows.extend((["c"],) * 6)
    beta_rows.extend((["d"],) * 6)
    beta_rows.extend([[f"filler:{index}"] for index in range(61)])
    for index, tags in enumerate(beta_rows):
        _insert_memory(conn, f"b-{index}", "project:beta", tags=tags)
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(_engine, arguments):
        calls.append(arguments)
        return []

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    alpha = await scan_coupling(_Engine(), project_id="project:alpha")
    assert alpha == {"scanner": "scan_coupling", "findings": 0, "dispatched": 0}
    assert calls == []

    beta = await scan_coupling(_Engine(), project_id="project:beta")
    assert beta["findings"] >= 1
    assert calls
    assert all(call["project_id"] == "project:beta" for call in calls)


@pytest.mark.asyncio
async def test_memory_decay_scanner_isolates_counts_and_dispatch(monkeypatch, tmp_path):
    from plastic_promise.cron.scan_memory_decay import scan_memory_decay

    db_path = str(tmp_path / "decay.sqlite")
    conn = _create_scanner_db(db_path)
    stale = (datetime.now() - timedelta(days=60)).isoformat()
    for index in range(6):
        _insert_memory(
            conn,
            f"a-zombie-{index}",
            "project:alpha",
            domain="alpha" if index % 2 else "beta",
            tier="L3",
            created_at=stale,
            last_accessed=stale,
        )
    for index in range(20):
        _insert_memory(
            conn,
            f"b-zombie-{index}",
            "project:beta",
            domain="other",
            tier="L3",
            created_at=stale,
            last_accessed=stale,
        )
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(_engine, arguments):
        calls.append(arguments)
        return []

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    result = await scan_memory_decay(_Engine(), project_id="project:alpha")

    zombie = next(call for call in calls if call["payload"]["type"] == "zombie_memories")
    assert result["dispatched"] == len(calls)
    assert zombie["payload"]["count"] == 6
    assert all(call["project_id"] == "project:alpha" for call in calls)


@pytest.mark.asyncio
async def test_system_scanners_dispatch_only_to_system_governance(monkeypatch, tmp_path):
    from plastic_promise.cron.project_scope import SYSTEM_GOVERNANCE_PROJECT_ID
    from plastic_promise.cron.scan_quality_trends import scan_quality_trends
    from plastic_promise.cron.scan_scheduler_health import scan_scheduler_health
    from plastic_promise.cron.scan_trust import scan_trust

    db_path = str(tmp_path / "system-scanners.sqlite")
    conn = _create_scanner_db(db_path)
    conn.executescript(
        """
        CREATE TABLE trust_scores (
            target TEXT PRIMARY KEY,
            trust REAL,
            tier TEXT,
            autonomy_level TEXT,
            last_updated TEXT,
            created_at TEXT
        );
        CREATE TABLE trust_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT,
            delta REAL,
            reason TEXT,
            old_value REAL,
            new_value REAL,
            direction TEXT,
            timestamp TEXT
        );
        CREATE TABLE task_queue (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            task_type TEXT,
            title TEXT,
            to_agent TEXT,
            priority INTEGER,
            status TEXT,
            source_scan TEXT,
            verify_verdict TEXT,
            created_at TEXT,
            claimed_at TEXT,
            escalation_count INTEGER,
            verified_at TEXT,
            verified_by TEXT
        );
        CREATE TABLE hunter_failure_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT,
            task_id TEXT,
            task_type TEXT,
            failure_type TEXT,
            trust_before REAL,
            trust_after REAL,
            penalty_applied REAL,
            occurred_at TEXT
        );
        CREATE TABLE metric_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT,
            metric_value REAL,
            window_start TEXT,
            window_end TEXT,
            computed_at TEXT
        );
        """
    )
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO trust_scores VALUES (?, ?, ?, ?, ?, ?)",
        ("pi_builder", 0.6, "medium", "standard", now, now),
    )
    conn.executemany(
        """
        INSERT INTO trust_history (
            target, delta, reason, old_value, new_value, direction, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pi_builder", -0.2, "drop", 0.8, 0.6, "decay", now),
            ("pi_builder", -0.05, "drop", 0.85, 0.8, "decay", now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO hunter_failure_log (
            agent_name, task_id, task_type, failure_type, occurred_at
        ) VALUES (?, ?, ?, 'rejected', ?)
        """,
        [
            ("pi_builder", "failure-1", "build_module", now),
            ("pi_builder", "failure-2", "build_module", now),
        ],
    )
    conn.commit()
    conn.close()

    calls: list[dict] = []

    async def capture(_engine, arguments):
        calls.append(arguments)
        return []

    monkeypatch.setenv("PLASTIC_DB_PATH", db_path)
    monkeypatch.setattr("plastic_promise.mcp.tools.task_queue.handle_task_enqueue", capture)

    await scan_trust(_Engine())
    await scan_quality_trends(_Engine())
    await scan_scheduler_health(_Engine())

    sources = {call["source_scan"] for call in calls}
    assert {"scan_trust", "scan_quality_trends", "scan_scheduler_health"} <= sources
    assert all(call["project_id"] == SYSTEM_GOVERNANCE_PROJECT_ID for call in calls)


@pytest.mark.asyncio
async def test_governed_maintenance_enumerates_and_aggregates_projects(monkeypatch, tmp_path):
    from daemons import maintenance_daemon

    db_path = tmp_path / "maintenance.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memories (project_id TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO memories(project_id) VALUES (?)",
        [("project:beta",), ("project:alpha",), ("project:system-governance",)],
    )
    conn.commit()
    engine = SimpleNamespace(_sqlite=SimpleNamespace(_conn=conn))
    calls: list[str] = []

    async def scanner(_engine, *, project_id):
        calls.append(project_id)
        count = 1 if project_id == "project:alpha" else 2
        return {
            "scanner": "scan_memory_decay",
            "findings": count,
            "dispatched": count - 1,
            "lifecycle": {"stale_marked": count},
        }

    def periodic_maintenance(_engine, *, system_authority):
        assert system_authority is True
        return {"decay_updated": 3}

    monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", scanner)
    # This test isolates project-scoped memory lifecycle aggregation.  The
    # production cycle also includes the durable collaboration maintenance
    # stage, which requires a fully bound server writer and is covered by its
    # dedicated contract tests.
    monkeypatch.setattr(
        maintenance_daemon,
        "run_collaboration_maintenance",
        lambda _engine: {"status": "success"},
    )
    monkeypatch.setattr(
        maintenance_daemon,
        "run_periodic_memory_maintenance",
        periodic_maintenance,
    )
    monkeypatch.setattr(
        maintenance_daemon, "expire_pending_memory_proposals", lambda _engine: {"expired": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "scan_synthesis_integrity", lambda _engine: {"stale": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_memory_index_jobs", lambda _engine: {"succeeded": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_synthesis_index_jobs", lambda _engine: {"succeeded": 0}
    )
    monkeypatch.setattr(maintenance_daemon, "run_audit", lambda: {"score": 1.0})

    report = await maintenance_daemon.run_governed_maintenance_cycle(engine)

    lifecycle = report["results"]["memory_lifecycle"]
    assert report["status"] == "success"
    assert calls == ["project:alpha", "project:beta"]
    assert lifecycle["project_count"] == 2
    assert lifecycle["findings"] == 3
    assert lifecycle["dispatched"] == 1
    assert lifecycle["lifecycle"] == {"stale_marked": 3}
    assert lifecycle["routine"] == {"decay_updated": 3}
    conn.close()


@pytest.mark.asyncio
async def test_preflight_pending_dedupe_is_project_scoped(monkeypatch, tmp_path):
    from plastic_promise.core import maintenance_preflight
    from plastic_promise.mcp.tools.task_queue import _compute_payload_hash

    db_path = tmp_path / "preflight.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE memories (project_id TEXT NOT NULL);
        CREATE TABLE behavior_graph_edges (id INTEGER);
        CREATE TABLE memory_proposals (status TEXT, expires_at TEXT);
        CREATE TABLE store_outbox (tool_name TEXT, status TEXT);
        CREATE TABLE synthesis_artifacts (memory_id TEXT, status TEXT);
        CREATE TABLE task_queue (
            project_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            title TEXT NOT NULL,
            to_agent TEXT,
            source_scan TEXT,
            payload TEXT,
            created_at TEXT,
            status TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO memories(project_id) VALUES (?)",
        [("project:alpha",), ("project:beta",)],
    )
    finding = {
        "type": "tag_cooccurrence_anomaly",
        "task_type_field": "investigate_coupling",
        "title": "same finding",
    }
    payload_hash = _compute_payload_hash(finding)
    conn.execute(
        """
        INSERT INTO task_queue (
            project_id, task_type, title, to_agent, source_scan,
            payload, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            "project:alpha",
            "investigate_coupling",
            "same finding",
            "pi_reviewer",
            "scan_coupling",
            json.dumps({"payload_hash": payload_hash}),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    async def projected(_engine, **_kwargs):
        return {
            "scanner": "scan_coupling",
            "findings": 1,
            "dispatched": 0,
            "projected_findings": [dict(finding)],
        }

    monkeypatch.setattr("plastic_promise.cron.scan_coupling.scan_coupling", projected)
    monkeypatch.setattr(
        maintenance_preflight,
        "_lifecycle_report",
        lambda _conn, _environ: {"decay_recalculation_candidates": 0},
    )
    monkeypatch.setattr(
        maintenance_preflight,
        "_synthesis_report",
        lambda _conn: {"affected_candidates": 0},
    )

    report = await maintenance_preflight.build_maintenance_preflight(
        db_path=str(db_path),
        managed_env_path=str(tmp_path / "missing-managed.env"),
        environ={},
    )

    projects = report["scanners"]["coupling"]["projects"]
    assert projects["project:alpha"] == {
        "projected_findings": 1,
        "already_pending": 1,
        "projected_new_tasks": 0,
    }
    assert projects["project:beta"] == {
        "projected_findings": 1,
        "already_pending": 0,
        "projected_new_tasks": 1,
    }
