import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plastic_promise.core import server_status
from plastic_promise.core.server_status import ServerStatusSettings, collect_server_status


def _settings(tmp_path: Path, **overrides) -> ServerStatusSettings:
    values = {
        "sqlite_path": tmp_path / "plastic_memory.db",
        "inference_job_db_path": tmp_path / "inference_jobs.db",
        "lancedb_root": tmp_path / "lancedb",
        "maintenance_heartbeat_path": tmp_path / "maintenance.heartbeat",
        "listener_ports": (),
    }
    values.update(overrides)
    return ServerStatusSettings(**values)


def _create_primary_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE store_outbox (outbox_id TEXT PRIMARY KEY, status TEXT);
            INSERT INTO store_outbox VALUES ('1', 'done');
            INSERT INTO store_outbox VALUES ('2', 'pending');
            INSERT INTO store_outbox VALUES ('3', 'unexpected');

            CREATE TABLE task_queue (id TEXT PRIMARY KEY, status TEXT);
            INSERT INTO task_queue VALUES ('1', 'pending');
            INSERT INTO task_queue VALUES ('2', 'pending');
            INSERT INTO task_queue VALUES ('3', 'claimed');
            """
        )


def _create_inference_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE inference_rerank_jobs (job_id TEXT PRIMARY KEY, status TEXT);
            INSERT INTO inference_rerank_jobs VALUES ('1', 'pending');
            INSERT INTO inference_rerank_jobs VALUES ('2', 'leased');
            INSERT INTO inference_rerank_jobs VALUES ('3', 'completed');

            CREATE TABLE inference_rerank_reservations (
                reservation_id TEXT PRIMARY KEY,
                status TEXT
            );
            INSERT INTO inference_rerank_reservations VALUES ('1', 'preparing');
            INSERT INTO inference_rerank_reservations VALUES ('2', 'finalized');
            """
        )


def _write_heartbeat(
    path: Path,
    *,
    now: datetime,
    pid: int = 321,
    process_generation: str = "a" * 32,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "maintenance-heartbeat/v1",
                "pid": pid,
                "updated_at": now.isoformat().replace("+00:00", "Z"),
                "startup_replay_cycle_id": "startup-cycle",
                "startup_replay_owner_pid": pid,
                "process_generation": process_generation,
            }
        ),
        encoding="utf-8",
    )


def test_collects_canonical_and_inference_counts_with_read_only_connections(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    _create_primary_database(settings.sqlite_path)
    _create_inference_database(settings.inference_job_db_path)
    primary_before = settings.sqlite_path.read_bytes()
    jobs_before = settings.inference_job_db_path.read_bytes()

    real_connect = sqlite3.connect
    observed_connections = []

    def recording_connect(database, *args, **kwargs):
        observed_connections.append((database, kwargs.copy()))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(server_status.sqlite3, "connect", recording_connect)

    snapshot = collect_server_status(
        settings,
        now=datetime(2026, 7, 24, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert snapshot["schema"] == "plastic-promise/server-status/v1"
    assert snapshot["collected_at"] == "2026-07-24T01:02:03Z"
    assert snapshot["sqlite"]["state"] == "ready"
    assert snapshot["sqlite"]["access"] == {"mode": "ro", "query_only": True}
    assert snapshot["sqlite"]["tables"]["store_outbox"] == {
        "state": "ready",
        "total": 3,
        "by_status": {
            "pending": 1,
            "processing": 0,
            "blocked": 0,
            "failed": 0,
            "done": 1,
        },
        "other": 1,
    }
    assert snapshot["sqlite"]["tables"]["task_queue"]["by_status"]["pending"] == 2
    assert snapshot["inference_jobs"]["state"] == "ready"
    assert snapshot["inference_jobs"]["jobs"]["by_status"] == {
        "pending": 1,
        "leased": 1,
        "completed": 1,
        "expired": 0,
    }
    assert snapshot["inference_jobs"]["reservations"]["by_status"]["preparing"] == 1
    assert len(observed_connections) == 2
    assert all("mode=ro" in str(database) for database, _kwargs in observed_connections)
    assert all(kwargs["uri"] is True for _database, kwargs in observed_connections)
    assert settings.sqlite_path.read_bytes() == primary_before
    assert settings.inference_job_db_path.read_bytes() == jobs_before


def test_missing_files_and_tables_are_reported_without_creation(tmp_path):
    settings = _settings(tmp_path)
    _create_primary_database(settings.sqlite_path)
    with sqlite3.connect(settings.sqlite_path) as connection:
        connection.execute("DROP TABLE task_queue")

    snapshot = collect_server_status(settings)

    assert snapshot["sqlite"]["state"] == "ready"
    assert snapshot["sqlite"]["tables"]["task_queue"]["reason"] == "table_missing"
    assert snapshot["inference_jobs"]["state"] == "missing"
    assert snapshot["lancedb"]["state"] == "missing"
    assert snapshot["maintenance"]["state"] == "disabled"
    assert not settings.inference_job_db_path.exists()
    assert not settings.lancedb_root.exists()
    assert not settings.maintenance_heartbeat_path.exists()


def test_loopback_probes_report_reachability_without_health_claims(tmp_path, monkeypatch):
    calls = []

    class Connection:
        def close(self):
            calls.append("closed")

    def fake_create_connection(address, *, timeout):
        calls.append((address, timeout))
        if address[1] == 9020:
            return Connection()
        raise ConnectionRefusedError

    monkeypatch.setattr(server_status.socket, "create_connection", fake_create_connection)
    settings = _settings(
        tmp_path,
        listener_ports=(("mcp", 9020), ("inference_gateway", 9030)),
        socket_timeout_seconds=0.05,
    )

    snapshot = collect_server_status(settings)

    assert snapshot["listeners"] == {
        "mcp": {
            "host": "127.0.0.1",
            "port": 9020,
            "state": "reachable",
            "listening": True,
        },
        "inference_gateway": {
            "host": "127.0.0.1",
            "port": 9030,
            "state": "unreachable",
            "listening": False,
        },
    }
    assert "healthy" not in json.dumps(snapshot)
    assert calls == [(("127.0.0.1", 9020), 0.05), "closed", (("127.0.0.1", 9030), 0.05)]


def test_lancedb_reads_only_current_generation_manifest_and_redacts_report(tmp_path, monkeypatch):
    root = tmp_path / "lancedb"
    root.mkdir(mode=0o700)
    secret = "sk-sensitive-marker"
    observed = []
    manifest = SimpleNamespace(
        manifest_schema="plastic-promise/lancedb-generation/v2",
        generation_id="generation-1",
        index_schema="memory-index/v3",
        embedding_model="text-embedding-v4",
        model_revision="2026-07-23",
        embedding_dimension=1024,
        source_db_sha256="1" * 64,
        source_row_count=3133,
        build_status="complete",
        built_row_count=3133,
        verification_status="verified",
        quality_report={"gate": {"status": "pass"}, "api_key": secret},
        index_text_policy="compact-v2",
        index_material_sha256="2" * 64,
        identity_sha256="3" * 64,
        manifest_sha256="4" * 64,
        created_at="2026-07-24T00:00:00Z",
        completed_at="2026-07-24T00:01:00Z",
        verified_at="2026-07-24T00:02:00Z",
        index_outbox={
            "watermark": 17,
            "immutable_digest": "5" * 64,
            "job_count": 17,
            "source_fingerprint": "6" * 64,
            "reconciled": True,
            "embedding_index_identity": secret,
        },
    )

    class FakeGenerationManager:
        def __init__(self, selected_root, *, create):
            observed.append((selected_root, create))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def current_manifest_metadata(self):
            observed.append("current_manifest_metadata")
            return manifest

    monkeypatch.setattr(server_status, "GenerationManager", FakeGenerationManager)

    snapshot = collect_server_status(_settings(tmp_path, lancedb_root=root))

    assert observed == [(root, False), "current_manifest_metadata"]
    assert snapshot["lancedb"]["state"] == "current-generation"
    assert snapshot["lancedb"]["tables_opened"] is False
    assert snapshot["lancedb"]["verification_scope"] == "manifest-metadata-only"
    assert snapshot["lancedb"]["index_tree_verified"] is False
    assert snapshot["lancedb"]["manifest"]["generation_id"] == "generation-1"
    assert snapshot["lancedb"]["manifest"]["quality_gate_status"] == "pass"
    assert secret not in json.dumps(snapshot)


def test_lancedb_status_reports_generation_bound_live_lag(tmp_path, monkeypatch):
    from plastic_promise.core.generation_live_index import bootstrap_generation_live_index

    root = tmp_path / "lancedb"
    root.mkdir(mode=0o700)
    base_index = tmp_path / "generation-1" / "index"
    base_index.mkdir(parents=True)
    (base_index / "artifact.bin").write_bytes(b"base")
    manifest = SimpleNamespace(
        manifest_schema="plastic-promise/lancedb-generation/v2",
        generation_id="generation-1",
        index_schema="memory-index/v3",
        embedding_model="text-embedding-v4",
        model_revision="2026-07-23",
        embedding_dimension=1024,
        source_db_sha256="1" * 64,
        source_row_count=1,
        build_status="complete",
        built_row_count=1,
        verification_status="verified",
        quality_report={"gate": {"status": "pass"}},
        index_text_policy="compact-v2",
        index_material_sha256="2" * 64,
        identity_sha256="3" * 64,
        manifest_sha256="4" * 64,
        created_at="2026-07-24T00:00:00Z",
        completed_at="2026-07-24T00:01:00Z",
        verified_at="2026-07-24T00:02:00Z",
        index_outbox={
            "watermark": 1,
            "immutable_digest": "5" * 64,
            "job_count": 1,
            "reconciled": True,
            "embedding_index_identity": "embedding-a",
            "receipt": {
                "generation_id": "generation-1",
                "manifest_hash": "6" * 64,
                "watermark": 1,
                "immutable_digest": "5" * 64,
                "job_count": 1,
                "marked_done_count": 0,
                "reconciled_at": "2026-07-24T00:02:00Z",
            },
        },
    )
    live_root = tmp_path / "live"
    bootstrap_generation_live_index(
        base_index_path=base_index,
        base_manifest=manifest,
        base_selection_identity="d" * 64,
        live_root=live_root,
    )
    sqlite_path = tmp_path / "plastic_memory.db"
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute(
            "CREATE TABLE store_outbox (tool_name TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO store_outbox (tool_name, status) VALUES (?, ?)",
            [("memory_index", "done"), ("memory_index", "pending")],
        )
        connection.commit()
    finally:
        connection.close()

    class FakeGenerationManager:
        def __init__(self, _root, *, create):
            assert create is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def current_manifest_metadata(self):
            return manifest

        def current_selection_identity(self):
            return "d" * 64

    monkeypatch.setattr(server_status, "GenerationManager", FakeGenerationManager)
    snapshot = collect_server_status(
        _settings(
            tmp_path,
            sqlite_path=sqlite_path,
            lancedb_root=root,
            lancedb_live_root=live_root,
        )
    )

    live = snapshot["lancedb"]["live_index"]
    assert live["state"] == "ready"
    assert live["lag"]["state"] == "lagged"
    assert live["lag"]["active_job_count"] == 1
    assert live["lag"]["completed_job_count"] == 0


def test_lancedb_does_not_call_an_unverified_current_manifest_ready(tmp_path, monkeypatch):
    root = tmp_path / "lancedb"
    root.mkdir(mode=0o700)
    manifest = SimpleNamespace(
        manifest_schema="plastic-promise/lancedb-generation/v2",
        generation_id="incomplete-generation",
        index_schema="memory-index/v3",
        embedding_model="text-embedding-v4",
        model_revision="2026-07-23",
        embedding_dimension=1024,
        source_db_sha256="1" * 64,
        source_row_count=3133,
        build_status="building",
        built_row_count=None,
        verification_status="unverified",
        quality_report=None,
        index_text_policy="compact-v2",
        index_material_sha256="2" * 64,
        identity_sha256="3" * 64,
        manifest_sha256="4" * 64,
        created_at="2026-07-24T00:00:00Z",
        completed_at=None,
        verified_at=None,
        index_outbox=None,
    )

    class FakeGenerationManager:
        def __init__(self, _root, *, create):
            assert create is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def current_manifest_metadata(self):
            return manifest

    monkeypatch.setattr(server_status, "GenerationManager", FakeGenerationManager)

    status = collect_server_status(_settings(tmp_path, lancedb_root=root))["lancedb"]

    assert status["state"] == "current-generation"
    assert status["manifest"]["build_status"] == "building"
    assert status["manifest"]["verification_status"] == "unverified"


def test_maintenance_distinguishes_disabled_not_running_unverified_fresh_and_stale(
    tmp_path,
    monkeypatch,
):
    now = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    heartbeat = tmp_path / "maintenance.heartbeat"
    monkeypatch.setattr(server_status, "_pid_is_alive", lambda _pid: True)

    disabled = collect_server_status(_settings(tmp_path), now=now)
    assert disabled["maintenance"]["state"] == "disabled"

    enabled_settings = _settings(tmp_path, maintenance_enabled=True)
    not_running = collect_server_status(enabled_settings, now=now)
    assert not_running["maintenance"]["state"] == "not-running"

    _write_heartbeat(heartbeat, now=now)
    unverified = collect_server_status(enabled_settings, now=now)
    assert unverified["maintenance"]["state"] == "identity-unverified"
    assert unverified["maintenance"]["reason"] == "maintenance_expected_identity_missing"

    verified_settings = _settings(
        tmp_path,
        maintenance_enabled=True,
        maintenance_expected_pid=321,
        maintenance_expected_process_generation="a" * 32,
    )
    fresh = collect_server_status(verified_settings, now=now)
    assert fresh["maintenance"]["state"] == "fresh"
    assert fresh["maintenance"]["reason"] == "ok"

    _write_heartbeat(heartbeat, now=now - timedelta(seconds=120))
    stale = collect_server_status(verified_settings, now=now)
    assert stale["maintenance"]["state"] == "stale"

    monkeypatch.setattr(server_status, "_pid_is_alive", lambda _pid: False)
    stopped = collect_server_status(verified_settings, now=now)
    assert stopped["maintenance"]["state"] == "not-running"
    assert stopped["maintenance"]["reason"] == "maintenance_pid_not_alive"


def test_maintenance_rejects_non_integral_or_oversized_pid_evidence(tmp_path):
    settings = _settings(tmp_path, maintenance_enabled=True)
    heartbeat = settings.maintenance_heartbeat_path
    for pid in (321.0, 10**100):
        heartbeat.write_text(
            json.dumps(
                {
                    "schema": "maintenance-heartbeat/v1",
                    "pid": pid,
                    "updated_at": "2026-07-24T02:00:00Z",
                    "startup_replay_cycle_id": "startup-cycle",
                    "startup_replay_owner_pid": pid,
                    "process_generation": "a" * 32,
                }
            ),
            encoding="utf-8",
        )

        status = collect_server_status(
            settings,
            now=datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc),
        )["maintenance"]

        assert status["state"] == "identity-unverified"
        assert status["reason"] == "maintenance_heartbeat_identity_invalid"


def test_maintenance_checks_identity_before_liveness_or_freshness(tmp_path, monkeypatch):
    now = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    settings = _settings(
        tmp_path,
        maintenance_enabled=True,
        maintenance_expected_pid=999,
        maintenance_expected_process_generation="b" * 32,
    )
    _write_heartbeat(
        settings.maintenance_heartbeat_path,
        now=now - timedelta(days=1),
        pid=321,
        process_generation="a" * 32,
    )
    monkeypatch.setattr(server_status, "_pid_is_alive", lambda _pid: False)

    status = collect_server_status(settings, now=now)["maintenance"]

    assert status["state"] == "identity-unverified"
    assert status["reason"] == "maintenance_expected_identity_mismatch"


def test_heartbeat_reader_rejects_fifo_without_blocking(tmp_path):
    heartbeat = tmp_path / "maintenance.heartbeat"
    os.mkfifo(heartbeat)

    assert server_status._read_heartbeat(heartbeat) is None


@pytest.mark.parametrize(
    "payload",
    (
        ("[" * 5_000) + ("]" * 5_000),
        '{"pid":' + ("9" * 5_000) + "}",
    ),
)
def test_malformed_heartbeat_json_cannot_crash_the_snapshot(tmp_path, payload):
    settings = _settings(tmp_path, maintenance_enabled=True)
    settings.maintenance_heartbeat_path.write_text(payload, encoding="utf-8")

    status = collect_server_status(settings)["maintenance"]

    assert status["state"] == "identity-unverified"


def test_sqlite_aggregation_has_a_vm_execution_deadline(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _create_primary_database(settings.sqlite_path)
    with sqlite3.connect(settings.sqlite_path) as connection:
        connection.executemany(
            "INSERT INTO store_outbox VALUES (?, 'pending')",
            ((str(index),) for index in range(4, 10_004)),
        )

    first_read = True

    def expired_clock():
        nonlocal first_read
        if first_read:
            first_read = False
            return 0.0
        return 1.0

    monkeypatch.setattr(server_status.time, "monotonic", expired_clock)

    snapshot = collect_server_status(settings)

    assert snapshot["sqlite"]["state"] == "unavailable"
    assert snapshot["sqlite"]["reason"] == "database_read_unavailable"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"listener_ports": (("MCP", 9020),)}, "listener_name_invalid"),
        ({"listener_ports": (("mcp", 0),)}, "listener_port_invalid"),
        ({"sqlite_path": "invalid\x00path"}, "sqlite_path_invalid"),
        ({"maintenance_expected_pid": 1 << 40}, "maintenance_expected_pid_invalid"),
        ({"socket_timeout_seconds": 60}, "socket_timeout_seconds_invalid"),
        (
            {"maintenance_expected_process_generation": "not-an-identity"},
            "maintenance_expected_process_generation_invalid",
        ),
    ],
)
def test_settings_reject_unbounded_or_ambiguous_inputs(tmp_path, overrides, reason):
    with pytest.raises(ValueError, match=f"^{reason}$"):
        _settings(tmp_path, **overrides)


def test_snapshot_clock_requires_an_explicit_timezone(tmp_path):
    with pytest.raises(ValueError, match="^now_must_be_timezone_aware$"):
        collect_server_status(_settings(tmp_path), now=datetime(2026, 7, 24))
