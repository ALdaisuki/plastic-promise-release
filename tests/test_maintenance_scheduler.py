"""Durable maintenance scheduling and cycle trace contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_independent_deadlines_cannot_reset_or_starve_one_another():
    from plastic_promise.core.maintenance_scheduler import (
        AdaptiveThrottle,
        MaintenanceDeadline,
        MaintenanceRegistry,
    )

    calls: list[str] = []

    def job(name: str, interval: int) -> MaintenanceDeadline:
        async def run():
            calls.append(name)
            return {"name": name}

        return MaintenanceDeadline(name, AdaptiveThrottle(interval), 0.0, run)

    required = {
        "audit": 300,
        "governed_maintenance": 300,
        "safety_net": 600,
        "heartbeat": 10,
        "scheduler_health": 3600,
        "scan_data_quality": 600,
    }
    registry = MaintenanceRegistry([job(name, interval) for name, interval in required.items()])

    await registry.run_due(300.0)
    await registry.run_due(600.0)
    await registry.run_due(3600.0)

    assert set(calls) == set(required)
    assert all(deadline.next_deadline > 3600.0 for deadline in registry.jobs)


@pytest.mark.asyncio
async def test_large_clock_jump_runs_each_due_job_at_most_once_and_advances_future():
    from plastic_promise.core.maintenance_scheduler import (
        AdaptiveThrottle,
        MaintenanceDeadline,
        MaintenanceRegistry,
    )

    calls: list[str] = []

    async def run():
        calls.append("job")
        return {"ok": True}

    registry = MaintenanceRegistry([MaintenanceDeadline("job", AdaptiveThrottle(60), 0.0, run)])

    outcomes = await registry.run_due(86_400.0)

    assert calls == ["job"]
    assert len(outcomes) == 1
    assert registry.jobs[0].next_deadline > 86_400.0


def test_daemon_registry_eagerly_constructs_required_throttles(monkeypatch, tmp_path):
    from daemons import maintenance_daemon

    calls: list[int] = []
    real_throttle = maintenance_daemon.AdaptiveThrottle

    def recording_throttle(base_seconds: int):
        calls.append(base_seconds)
        return real_throttle(base_seconds)

    monkeypatch.setattr(maintenance_daemon, "AdaptiveThrottle", recording_throttle)
    registry = maintenance_daemon.build_maintenance_registry(
        now=100.0,
        heartbeat_path=tmp_path / "maintenance.heartbeat",
        startup_replay_cycle_id="startup-cycle",
    )

    assert {job.name for job in registry.jobs} == {
        "audit",
        "governed_maintenance",
        "safety_net",
        "heartbeat",
        "scheduler_health",
        "scan_data_quality",
    }
    assert len(calls) == 6
    assert all(type(value) is int and value > 0 for value in calls)


def _trace_engine(db_path):
    conn = sqlite3.connect(db_path)
    return SimpleNamespace(_sqlite=SimpleNamespace(_conn=conn), trace_db=db_path)


def _server_engine(db_path):
    from plastic_promise.core.context_engine import _SQLiteStorage

    storage = _SQLiteStorage(str(db_path))
    return SimpleNamespace(
        _sqlite=storage,
        _write_lock=threading.RLock(),
        trace_db=db_path,
    )


@contextmanager
def _storage_transaction(storage):
    with storage.batch():
        yield


def _configure_project_scoped_cycle(
    monkeypatch,
    maintenance_daemon,
    *,
    collaboration_maintenance=True,
):
    def periodic_maintenance(_engine, *, system_authority):
        assert system_authority is True
        return {"decay_updated": 0}

    monkeypatch.setattr(
        maintenance_daemon,
        "_maintenance_project_ids",
        lambda _engine=None: ("project:test",),
    )
    monkeypatch.setattr(
        maintenance_daemon,
        "run_periodic_memory_maintenance",
        periodic_maintenance,
    )
    if collaboration_maintenance:
        monkeypatch.setattr(
            maintenance_daemon,
            "run_collaboration_maintenance",
            lambda _engine: {
                "schema_version": "collaboration-maintenance/v1",
                "status": "success",
                "reconcile": {},
                "promotion": [],
                "canonical_memory_mutation": False,
                "event_delete": False,
            },
        )


@pytest.mark.asyncio
async def test_governed_cycle_persists_parent_and_ordered_children_after_reopen(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon
    from plastic_promise.core.traceability import TraceabilityStore

    engine = _trace_engine(tmp_path / "traceability.sqlite")
    _configure_project_scoped_cycle(monkeypatch, maintenance_daemon)

    async def lifecycle(_engine, *, project_id):
        assert project_id == "project:test"
        return {"processed": 2}

    async def audit():
        return {"score": 1.0}

    monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", lifecycle)
    monkeypatch.setattr(
        maintenance_daemon, "expire_pending_memory_proposals", lambda _engine: {"expired": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "scan_synthesis_integrity", lambda _engine: {"stale": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_memory_index_jobs", lambda _engine: {"succeeded": 1}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_synthesis_index_jobs", lambda _engine: {"succeeded": 1}
    )
    monkeypatch.setattr(maintenance_daemon, "run_audit", audit)

    result = await maintenance_daemon.run_governed_maintenance_cycle(
        engine, outer_parent_call_id="daemon-run-42"
    )
    engine._sqlite._conn.close()

    reopened = TraceabilityStore(engine.trace_db)
    root, children = reopened.get_cycle_span_tree(result["cycle_call_id"])
    reopened.close()

    assert result["status"] == "success"
    assert root["call_id"] == result["cycle_call_id"]
    assert root["stage"] == "maintenance_cycle"
    assert root["parent_call_id"] == "daemon-run-42"
    assert [span["stage"] for span in children] == [
        "memory_lifecycle",
        "collaboration_maintenance",
        "proposal_expiry",
        "synthesis_integrity",
        "memory_index_replay",
        "synthesis_index_replay",
        "audit",
    ]
    assert root["status"] == "success"
    assert [span["metadata"]["order"] for span in children] == list(range(1, 8))
    assert all(span["parent_call_id"] == result["cycle_call_id"] for span in children)


@pytest.mark.asyncio
async def test_governed_cycle_durably_marks_worker_runtime_failures_as_degraded(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon
    from plastic_promise.core.traceability import TraceabilityStore

    engine = _trace_engine(tmp_path / "traceability.sqlite")
    _configure_project_scoped_cycle(monkeypatch, maintenance_daemon)
    monkeypatch.setattr(
        maintenance_daemon,
        "scan_memory_decay",
        lambda _engine, **_kwargs: {"processed": 1},
    )
    monkeypatch.setattr(
        maintenance_daemon,
        "expire_pending_memory_proposals",
        lambda _engine: {
            "expired": 0,
            "semantic_jobs": {
                "skipped": "semantic_memory_runtime_unavailable",
                "failure_code": "semantic_memory_runtime_init_failed",
                "processed_batches": 0,
            },
            "promotion_jobs": {
                "skipped": "proposal_promotion_runtime_unavailable",
                "failure_code": "proposal_promotion_runtime_init_failed",
                "processed_batches": 0,
            },
        },
    )
    monkeypatch.setattr(
        maintenance_daemon, "scan_synthesis_integrity", lambda _engine: {"stale": 0}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_memory_index_jobs", lambda _engine: {"succeeded": 1}
    )
    monkeypatch.setattr(
        maintenance_daemon, "replay_synthesis_index_jobs", lambda _engine: {"succeeded": 1}
    )
    monkeypatch.setattr(maintenance_daemon, "run_audit", lambda: {"score": 1.0})

    result = await maintenance_daemon.run_governed_maintenance_cycle(engine)
    engine._sqlite._conn.close()
    store = TraceabilityStore(engine.trace_db)
    root, children = store.get_cycle_span_tree(result["cycle_call_id"])
    store.close()

    by_stage = {span["stage"]: span for span in children}
    expected_codes = [
        "proposal_promotion_runtime_init_failed",
        "semantic_memory_runtime_init_failed",
    ]
    assert result["status"] == "partial"
    assert result["degradations"] == {"proposal_expiry": expected_codes}
    assert root["status"] == "partial"
    assert root["degraded"] is True
    assert root["metadata"]["degraded_count"] == 1
    assert root["metadata"]["degradation_codes"] == {"proposal_expiry": expected_codes}
    assert by_stage["proposal_expiry"]["status"] == "degraded"
    assert by_stage["proposal_expiry"]["degraded"] is True
    assert by_stage["proposal_expiry"]["metadata"]["failure_codes"] == expected_codes


@pytest.mark.asyncio
async def test_governed_cycle_marks_parent_partial_and_continues_after_middle_failure(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon
    from plastic_promise.core.traceability import TraceabilityStore

    engine = _trace_engine(tmp_path / "traceability.sqlite")
    _configure_project_scoped_cycle(monkeypatch, maintenance_daemon)
    calls: list[str] = []

    async def lifecycle(_engine, *, project_id):
        assert project_id == "project:test"
        calls.append("memory_lifecycle")
        return {"processed": 1}

    def fail_synthesis(_engine):
        calls.append("synthesis_integrity")
        raise RuntimeError("middle failure")

    def replay_memory(_engine):
        calls.append("memory_index_replay")
        return {"succeeded": 1}

    def replay_synthesis(_engine):
        calls.append("synthesis_index_replay")
        return {"succeeded": 1}

    async def audit():
        calls.append("audit")
        return {"score": 1.0}

    monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", lifecycle)
    monkeypatch.setattr(
        maintenance_daemon, "expire_pending_memory_proposals", lambda _engine: {"expired": 0}
    )
    monkeypatch.setattr(maintenance_daemon, "scan_synthesis_integrity", fail_synthesis)
    monkeypatch.setattr(maintenance_daemon, "replay_memory_index_jobs", replay_memory)
    monkeypatch.setattr(maintenance_daemon, "replay_synthesis_index_jobs", replay_synthesis)
    monkeypatch.setattr(maintenance_daemon, "run_audit", audit)

    result = await maintenance_daemon.run_governed_maintenance_cycle(
        engine, outer_parent_call_id="daemon-run-43"
    )
    engine._sqlite._conn.close()
    root, children = TraceabilityStore(engine.trace_db).get_cycle_span_tree(result["cycle_call_id"])

    by_stage = {span["stage"]: span for span in children}
    assert result["status"] == "partial"
    assert root["status"] == "partial"
    assert by_stage["synthesis_integrity"]["status"] == "error"
    assert [
        by_stage[stage]["status"]
        for stage in (
            "memory_index_replay",
            "synthesis_index_replay",
            "audit",
        )
    ] == ["success", "success", "success"]
    assert calls[-3:] == ["memory_index_replay", "synthesis_index_replay", "audit"]


@pytest.mark.asyncio
async def test_collaboration_maintenance_missing_schema_degrades_and_later_stages_continue(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon

    engine = _server_engine(tmp_path / "missing-collaboration-schema.sqlite")
    _configure_project_scoped_cycle(
        monkeypatch,
        maintenance_daemon,
        collaboration_maintenance=False,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        maintenance_daemon,
        "scan_memory_decay",
        lambda _engine, **_kwargs: {"processed": 0},
    )
    monkeypatch.setattr(
        maintenance_daemon,
        "expire_pending_memory_proposals",
        lambda _engine: calls.append("proposal_expiry") or {"expired": 0},
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
    monkeypatch.setattr(
        maintenance_daemon,
        "run_audit",
        lambda: calls.append("audit") or {"score": 1.0},
    )

    result = await maintenance_daemon.run_governed_maintenance_cycle(engine)

    assert result["status"] == "partial"
    assert result["results"]["collaboration_maintenance"] == {
        "schema_version": "collaboration-maintenance-stage/v1",
        "status": "degraded",
        "failure_code": "durable_collaboration_schema_missing",
        "canonical_memory_mutation": False,
        "event_delete": False,
    }
    assert result["degradations"]["collaboration_maintenance"] == [
        "durable_collaboration_schema_missing"
    ]
    assert calls == ["proposal_expiry", "audit"]
    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='collaboration_runtime_schema'"
    ).fetchone() == (0,)
    engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_collaboration_maintenance_open_transaction_rolls_back_and_reports_stable_code(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon

    engine = _server_engine(tmp_path / "collaboration-open-transaction.sqlite")
    _configure_project_scoped_cycle(
        monkeypatch,
        maintenance_daemon,
        collaboration_maintenance=False,
    )
    engine._sqlite._conn.execute("CREATE TABLE collaboration_stage_writes (value TEXT)")
    engine._sqlite._conn.commit()
    later: list[str] = []

    monkeypatch.setattr(
        maintenance_daemon,
        "scan_memory_decay",
        lambda _engine, **_kwargs: {"processed": 0},
    )

    def leave_transaction_open(_engine):
        engine._sqlite._conn.execute(
            "INSERT INTO collaboration_stage_writes VALUES ('partial')"
        )
        return {"schema_version": "collaboration-maintenance/v1"}

    monkeypatch.setattr(
        maintenance_daemon,
        "run_collaboration_maintenance",
        leave_transaction_open,
    )
    monkeypatch.setattr(
        maintenance_daemon,
        "expire_pending_memory_proposals",
        lambda _engine: later.append("proposal_expiry") or {"expired": 0},
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

    result = await maintenance_daemon.run_governed_maintenance_cycle(engine)

    assert engine._sqlite._conn.execute(
        "SELECT COUNT(*) FROM collaboration_stage_writes"
    ).fetchone() == (0,)
    assert result["errors"]["collaboration_maintenance"] == "RuntimeError"
    assert result["failure_codes"]["collaboration_maintenance"] == (
        "maintenance_stage_left_open_transaction"
    )
    assert later == ["proposal_expiry"]
    engine._sqlite._conn.close()


def test_collaboration_maintenance_reuses_canonical_server_storage(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from tests.pr5_schema_fixture import install_pr5_collaboration_schema

    engine = _server_engine(tmp_path / "collaboration-maintenance.sqlite")
    install_pr5_collaboration_schema(
        engine._sqlite._conn,
        transaction_factory=lambda: _storage_transaction(engine._sqlite),
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
        suffix="maintenance-composition",
    )
    observed_connections: list[sqlite3.Connection] = []

    from plastic_promise.collaboration import durable_runtime

    real_maintenance = durable_runtime.DurableCollaborationRuntime.maintenance

    def observe_connection(runtime):
        observed_connections.append(runtime._connection)
        return real_maintenance(runtime)

    monkeypatch.setattr(
        durable_runtime.DurableCollaborationRuntime,
        "maintenance",
        observe_connection,
    )

    result = maintenance_daemon.run_collaboration_maintenance(engine)

    assert result["status"] == "success"
    assert result["schema_version"] == "collaboration-maintenance/v1"
    assert result["canonical_memory_mutation"] is False
    assert result["event_delete"] is False
    assert observed_connections == [engine._sqlite._conn]
    assert engine._sqlite._conn.in_transaction is False
    engine._sqlite._conn.close()


def test_collaboration_maintenance_authority_unavailable_fails_closed(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.collaboration import runtime_binding
    from plastic_promise.collaboration.durable_runtime import DurableCollaborationError
    from tests.pr5_schema_fixture import install_pr5_collaboration_schema

    engine = _server_engine(tmp_path / "collaboration-authority.sqlite")
    install_pr5_collaboration_schema(
        engine._sqlite._conn,
        transaction_factory=lambda: _storage_transaction(engine._sqlite),
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
        suffix="maintenance-authority",
    )

    def unavailable_authority(*_args, **_kwargs):
        raise DurableCollaborationError(
            "durable_collaboration_authority_cache_unavailable"
        )

    monkeypatch.setattr(runtime_binding, "_authority_bundle", unavailable_authority)

    result = maintenance_daemon.run_collaboration_maintenance(engine)

    assert result == {
        "schema_version": "collaboration-maintenance-stage/v1",
        "status": "degraded",
        "failure_code": "durable_collaboration_authority_cache_unavailable",
        "canonical_memory_mutation": False,
        "event_delete": False,
    }
    assert engine._sqlite._conn.in_transaction is False
    engine._sqlite._conn.close()


@pytest.mark.asyncio
async def test_failed_stage_rolls_back_before_independent_error_trace(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.core.traceability import TraceabilityStore

    engine = _trace_engine(tmp_path / "traceability.sqlite")
    _configure_project_scoped_cycle(monkeypatch, maintenance_daemon)
    engine._sqlite._conn.execute("CREATE TABLE stage_writes (value TEXT)")
    engine._sqlite._conn.commit()

    def fail_with_open_transaction(_engine, *, project_id):
        assert project_id == "project:test"
        engine._sqlite._conn.execute("INSERT INTO stage_writes VALUES ('partial')")
        raise RuntimeError("stage failed after write")

    monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", fail_with_open_transaction)
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

    result = await maintenance_daemon.run_governed_maintenance_cycle(engine)

    assert engine._sqlite._conn.execute("SELECT COUNT(*) FROM stage_writes").fetchone()[0] == 0
    root, children = TraceabilityStore(engine.trace_db).get_cycle_span_tree(result["cycle_call_id"])
    assert root["status"] == "partial"
    assert children[0]["stage"] == "memory_lifecycle"
    assert children[0]["status"] == "error"
    engine._sqlite._conn.close()


def test_tag_patch_uses_expected_tags_cas(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage

    db_path = tmp_path / "tag-cas.sqlite"
    storage = _SQLiteStorage(str(db_path))
    storage._conn.execute(
        "INSERT INTO memories (id, content, memory_type, tags) VALUES (?, ?, ?, ?)",
        ("task-1", "task", "experience", '["task:active"]'),
    )
    storage._conn.commit()
    storage._conn.close()
    monkeypatch.setattr(maintenance_daemon, "DB_PATH", str(db_path))

    assert not maintenance_daemon._patch_ordinary_tags(
        "task-1",
        expected_tags=["task:pending"],
        replacement_tags=["task:reviewed"],
    )
    assert maintenance_daemon._patch_ordinary_tags(
        "task-1",
        expected_tags=["task:active"],
        replacement_tags=["task:pending"],
    )

    reopened = _SQLiteStorage(str(db_path))
    assert reopened.get("task-1")["tags"] == ["task:pending"]
    reopened._conn.close()


def _seed_daemon_patch_memory(db_path, *, memory_id: str, tier: str, last_accessed: str):
    from plastic_promise.core.context_engine import _SQLiteStorage

    storage = _SQLiteStorage(str(db_path))
    storage.create_ordinary_if_absent(
        memory_id,
        {
            "id": memory_id,
            "content": f"canonical {memory_id}",
            "memory_type": "experience",
            "project_id": "project:daemon-patch",
            "tier": tier,
            "tags": ["status:current"],
            "category": "other",
            "worth_success": 0,
            "worth_failure": 0,
            "access_count": 0,
            "created_at": last_accessed,
            "last_accessed": last_accessed,
            "embedding_hash": f"sha256:{memory_id}",
            "embedding_text": f"canonical {memory_id}",
            "search_text": f"canonical {memory_id}",
        },
    )
    storage._conn.close()


def test_daemon_field_patch_rejects_stale_scalar_snapshot(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage

    db_path = tmp_path / "field-cas.sqlite"
    observed_at = datetime.now().isoformat()
    _seed_daemon_patch_memory(
        db_path,
        memory_id="worth-cas",
        tier="L1",
        last_accessed=observed_at,
    )
    monkeypatch.setattr(maintenance_daemon, "DB_PATH", str(db_path))

    assert not maintenance_daemon._patch_ordinary_fields(
        "worth-cas",
        replacements={"worth_success": 1, "worth_failure": 0},
        expected_snapshot={
            "worth_success": 0,
            "worth_failure": 0,
            "last_accessed": "concurrently-changed",
        },
    )

    reopened = _SQLiteStorage(str(db_path))
    assert reopened.get("worth-cas")["worth_success"] == 0
    reopened._conn.close()


@pytest.mark.asyncio
async def test_scan_stale_worth_routes_each_write_through_snapshot_patch(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import synthesis_content_hash

    db_path = tmp_path / "stale-worth.sqlite"
    observed_at = datetime.now().isoformat()
    _seed_daemon_patch_memory(
        db_path,
        memory_id="worth-revive",
        tier="L1",
        last_accessed=observed_at,
    )
    monkeypatch.setattr(maintenance_daemon, "DB_PATH", str(db_path))
    real_patch = maintenance_daemon._patch_ordinary_fields
    calls = []

    def recording_patch(memory_id, **kwargs):
        calls.append((memory_id, kwargs))
        return real_patch(memory_id, **kwargs)

    monkeypatch.setattr(maintenance_daemon, "_patch_ordinary_fields", recording_patch)

    await maintenance_daemon.scan_stale_worth()

    assert calls == [
        (
            "worth-revive",
            {
                "replacements": {"worth_success": 1, "worth_failure": 0},
                "expected_snapshot": {
                    "worth_success": 0,
                    "worth_failure": 0,
                    "last_accessed": observed_at,
                    "created_at": observed_at,
                },
                "expected_project_id": "project:daemon-patch",
                "expected_content_hash": synthesis_content_hash("canonical worth-revive"),
                "expected_embedding_hash": "sha256:worth-revive",
            },
        )
    ]
    reopened = _SQLiteStorage(str(db_path))
    assert reopened.get("worth-revive")["worth_success"] == 1
    reopened._conn.close()


@pytest.mark.asyncio
async def test_scan_tier_migration_queues_checked_index_job_in_patch_transaction(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage

    db_path = tmp_path / "tier-index.sqlite"
    observed_at = (datetime.now() - timedelta(hours=1)).isoformat()
    _seed_daemon_patch_memory(
        db_path,
        memory_id="tier-promote",
        tier="L1",
        last_accessed=observed_at,
    )
    monkeypatch.setattr(maintenance_daemon, "DB_PATH", str(db_path))

    await maintenance_daemon.scan_tier_migration()

    reopened = _SQLiteStorage(str(db_path))
    assert reopened.get("tier-promote")["tier"] == "L2"
    rows = reopened._conn.execute(
        "SELECT status, project_id, payload_json, metadata_json FROM store_outbox "
        "WHERE tool_name = 'memory_index' ORDER BY created_at, outbox_id"
    ).fetchall()
    assert len(rows) == 1
    status, project_id, payload_json, metadata_json = rows[0]
    payload = json.loads(payload_json)
    assert status == "pending"
    assert project_id == "project:daemon-patch"
    assert payload == {
        "action": "upsert",
        "expected_embedding_hash": "sha256:tier-promote",
        "material_revision": "sha256:tier-promote",
        "memory_id": "tier-promote",
        "memory_version": payload["memory_version"],
        "project_id": "project:daemon-patch",
    }
    assert type(payload["memory_version"]) is int
    assert json.loads(metadata_json) == {"job_schema": "memory-index/v3"}
    reopened._conn.close()


def test_daemon_index_patch_rolls_back_tier_when_checked_job_cannot_bind(monkeypatch, tmp_path):
    from daemons import maintenance_daemon
    from plastic_promise.core.context_engine import _SQLiteStorage
    from plastic_promise.core.synthesis import synthesis_content_hash

    db_path = tmp_path / "tier-index-rollback.sqlite"
    observed_at = datetime.now().isoformat()
    _seed_daemon_patch_memory(
        db_path,
        memory_id="tier-without-material",
        tier="L1",
        last_accessed=observed_at,
    )
    storage = _SQLiteStorage(str(db_path))
    storage.patch_ordinary(
        "tier-without-material",
        replacements={"embedding_hash": ""},
    )
    storage._conn.close()
    monkeypatch.setattr(maintenance_daemon, "DB_PATH", str(db_path))

    assert not maintenance_daemon._patch_ordinary_fields(
        "tier-without-material",
        replacements={"tier": "L2"},
        expected_snapshot={
            "tier": "L1",
            "last_accessed": observed_at,
            "access_count": 0,
        },
        expected_project_id="project:daemon-patch",
        expected_content_hash=synthesis_content_hash("canonical tier-without-material"),
        expected_embedding_hash="",
        publish_index=True,
    )

    reopened = _SQLiteStorage(str(db_path))
    assert reopened.get("tier-without-material")["tier"] == "L1"
    assert reopened._conn.execute("SELECT COUNT(*) FROM store_outbox").fetchone()[0] == 0
    reopened._conn.close()


def test_cycle_span_tree_rejects_self_parent_or_wrong_child_linkage(tmp_path):
    from plastic_promise.core.traceability import TraceabilityStore, record_call_span

    db_path = tmp_path / "traceability.sqlite"
    store = TraceabilityStore(db_path)
    conn = store.connection
    record_call_span(
        conn,
        call_id="self-root",
        parent_call_id="self-root",
        tool_name="maintenance_daemon",
        stage_name="maintenance_cycle",
    )
    with pytest.raises(ValueError, match="invalid_maintenance_cycle_span_tree"):
        store.get_cycle_span_tree("self-root")

    record_call_span(
        conn,
        call_id="valid-root",
        tool_name="maintenance_daemon",
        stage_name="maintenance_cycle",
    )
    stages = [
        "memory_lifecycle",
        "collaboration_maintenance",
        "proposal_expiry",
        "synthesis_integrity",
        "memory_index_replay",
        "synthesis_index_replay",
        "audit",
    ]
    for order, stage in enumerate(stages, 1):
        record_call_span(
            conn,
            call_id=f"child-{order}",
            parent_call_id="other-root" if order == 3 else "valid-root",
            tool_name="maintenance_daemon",
            stage_name=stage,
            metadata={"order": order},
        )
    with pytest.raises(ValueError, match="invalid_maintenance_cycle_span_tree"):
        store.get_cycle_span_tree("valid-root")
    store.close()


@pytest.mark.asyncio
async def test_governed_cycle_bootstraps_the_same_node_runtime_before_index_replay(
    monkeypatch, tmp_path
):
    import plastic_promise.core.node_runtime_bootstrap as node_bootstrap
    from daemons import maintenance_daemon

    engine = _trace_engine(tmp_path / "traceability.sqlite")
    _configure_project_scoped_cycle(monkeypatch, maintenance_daemon)
    calls: list[str] = []

    def bootstrap(target):
        assert target is engine
        calls.append("bootstrap")
        target._memory_index_node_runtime = object()

    monkeypatch.setattr(node_bootstrap, "bootstrap_memory_index_node_runtime", bootstrap)
    monkeypatch.setattr(maintenance_daemon, "scan_memory_decay", lambda _engine, **_kwargs: {})
    monkeypatch.setattr(maintenance_daemon, "expire_pending_memory_proposals", lambda _engine: {})
    monkeypatch.setattr(maintenance_daemon, "scan_synthesis_integrity", lambda _engine: {})

    def replay(target):
        assert target._memory_index_node_runtime is not None
        calls.append("memory_index_replay")
        return {"succeeded": 0}

    monkeypatch.setattr(maintenance_daemon, "replay_memory_index_jobs", replay)
    monkeypatch.setattr(maintenance_daemon, "replay_synthesis_index_jobs", lambda _engine: {})
    monkeypatch.setattr(maintenance_daemon, "run_audit", lambda: {})

    report = await maintenance_daemon.run_governed_maintenance_cycle(engine)
    engine._sqlite._conn.close()

    assert report["status"] == "success"
    assert calls == ["bootstrap", "memory_index_replay"]
