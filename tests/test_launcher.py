"""Tests for One-Click Launcher components."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import pytest
import tomllib

from plastic_promise.launcher.bootstrap_checker import check_bootstrap
from plastic_promise.launcher.env_checker import run_env_checks
from plastic_promise.launcher.service_definition import (
    RestartPolicy,
    ServiceDefinition,
    ServiceStatus,
)


def test_health_rejects_fresh_heartbeat_when_reported_pid_is_dead(monkeypatch, tmp_path):
    from plastic_promise.launcher import service_manager

    heartbeat = tmp_path / "maintenance.heartbeat"
    service_manager.write_maintenance_heartbeat(
        heartbeat,
        pid=424242,
        updated_at=datetime.now(timezone.utc),
        startup_replay_cycle_id="startup-cycle",
        process_generation="a" * 32,
    )
    monkeypatch.setattr(service_manager, "pid_is_alive", lambda pid: False)

    health = service_manager.read_maintenance_health(heartbeat)

    assert health["healthy"] is False
    assert health["reason"] == "maintenance_pid_not_alive"


def test_daemon_once_parser_requires_supported_mcp_url_and_json_contract():
    from daemons.maintenance_daemon import (
        parse_daemon_args,
        validate_daemon_once_arguments,
    )

    with pytest.raises(SystemExit):
        parse_daemon_args(["--once", "--mcp-url", "not-a-url", "--json"])
    result = validate_daemon_once_arguments(
        {
            "once": True,
            "mcp_url": "http://127.0.0.1:9020/mcp",
            "json": True,
        }
    )
    assert result == {"ok": True}
    assert (
        validate_daemon_once_arguments({"once": True, "json": False})["error"]
        == "daemon_once_arguments_invalid"
    )


def test_daemon_parser_rejects_foreign_source_identity():
    from daemons.maintenance_daemon import parse_daemon_args

    with pytest.raises(SystemExit):
        parse_daemon_args(["--source-root", "foreign-root"])
    with pytest.raises(SystemExit):
        parse_daemon_args(["--source-revision", "f" * 40])


@pytest.mark.asyncio
async def test_daemon_once_reuses_registry_once_and_skips_warmup_and_forever_loop(
    monkeypatch, tmp_path
):
    from daemons import maintenance_daemon

    calls = []

    class Registry:
        def __init__(self):
            self.jobs = [
                type(
                    "Job",
                    (),
                    {"name": "governed_maintenance", "next_deadline": 1.0},
                )()
            ]
            self.run_due_count = 0

        async def run_due(self, _now):
            self.run_due_count += 1
            calls.append("registry.run_due")
            return (
                {
                    "name": "governed_maintenance",
                    "status": "success",
                    "result": {
                        "status": "success",
                        "cycle_call_id": "cycle-once",
                        "errors": {},
                        "results": {
                            "memory_index_replay": {"failed": 0},
                            "synthesis_index_replay": {"failed": 0},
                        },
                    },
                },
            )

    registry = Registry()
    monkeypatch.setattr(
        maintenance_daemon, "build_maintenance_registry", lambda **_kwargs: registry
    )
    monkeypatch.setattr(
        maintenance_daemon, "run_warmup", lambda *_args, **_kwargs: calls.append("warmup")
    )
    monkeypatch.setattr(
        maintenance_daemon, "run_forever", lambda *_args, **_kwargs: calls.append("forever")
    )
    monkeypatch.setattr(maintenance_daemon, "_maintenance_engine", lambda: type("Engine", (), {})())
    monkeypatch.setattr(maintenance_daemon, "_close_maintenance_engine", lambda _engine: None)
    monkeypatch.setattr(maintenance_daemon, "_run_dir", str(tmp_path))
    monkeypatch.setattr(maintenance_daemon, "_pid_path", str(tmp_path / "daemon.pid"))
    monkeypatch.setattr(
        maintenance_daemon,
        "_heartbeat_path",
        str(tmp_path / "daemon.heartbeat"),
    )
    daemon_pid = tmp_path / "daemon.pid"
    daemon_pid.write_text("12345", encoding="utf-8")

    exit_code = await maintenance_daemon.daemon_main(
        ["--once", "--mcp-url", "http://127.0.0.1:9020/mcp", "--json"]
    )

    assert exit_code == 0
    assert calls == ["registry.run_due"]
    assert registry.run_due_count == 1
    assert daemon_pid.read_text(encoding="utf-8") == "12345"
    assert not (tmp_path / "daemon.heartbeat").exists()


@pytest.fixture(autouse=True)
def _restore_plastic_environment():
    keys = (
        "PLASTIC_DB_PATH",
        "PLASTIC_LANCEDB_PATH",
        "PLASTIC_PROJECT_ID",
        "PLASTIC_MCP_TRANSPORT",
        "PLASTIC_MCP_LEGACY_TRANSPORT_ALIAS",
        "PLASTIC_PROCESS_GENERATION",
        "EMBEDDER_TIMEOUT",
        "PP_PROJECT_ID",
    )
    original = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _load_init_and_start():
    path = os.path.join(os.getcwd(), "scripts", "init_and_start.py")
    spec = importlib.util.spec_from_file_location("init_and_start_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- service_definition tests -----------------------------------------


def test_service_definition_defaults():
    svc = ServiceDefinition(
        name="test-svc",
        command=["python", "-c", "print('hi')"],
    )
    assert svc.name == "test-svc"
    assert svc.health_url is None
    assert svc.startup_timeout == 30.0
    assert svc.depends_on == []
    assert isinstance(svc.restart_policy, RestartPolicy)
    assert svc.restart_policy.max_retries == 5


def test_restart_policy_backoff():
    policy = RestartPolicy(
        max_retries=5,
        window_seconds=60.0,
        backoff_base=1.0,
        backoff_multiplier=2.0,
        max_backoff=30.0,
    )
    assert policy.backoff_base == 1.0
    assert policy.backoff_multiplier == 2.0
    assert policy.max_backoff == 30.0


def test_service_status_enum():
    assert ServiceStatus.PENDING.value == "pending"
    assert ServiceStatus.HEALTHY.value == "healthy"
    assert ServiceStatus.UNRECOVERABLE.value == "unrecoverable"


def test_hidden_subprocess_kwargs():
    from plastic_promise.launcher.subprocess_utils import hidden_subprocess_kwargs

    kwargs = hidden_subprocess_kwargs(new_process_group=True)
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert "startupinfo" in kwargs
    else:
        assert kwargs == {}


# -- env_checker tests -----------------------------------------------


def test_env_checker_python_version():
    _, msgs = run_env_checks(skip_ollama=True)
    assert any("Python" in m for m in msgs)


def test_env_checker_ollama_skip():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("SKIP" in m for m in msgs)


def test_env_checker_lancedb():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("LanceDB" in m for m in msgs)


def test_env_checker_port():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("Port 9020" in m for m in msgs)


def test_env_checker_includes_codex_mcp_config_status():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("Codex MCP config" in m for m in msgs)


# -- bootstrap_checker tests -----------------------------------------


def test_check_bootstrap_missing_db():
    needs, msg = check_bootstrap("/nonexistent/path/db.sqlite")
    assert needs is True
    assert "not found" in msg


def test_check_bootstrap_existing_db():
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if os.path.exists(db_path):
        needs, msg = check_bootstrap(db_path)
        assert isinstance(needs, bool)


def test_check_bootstrap_empty_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "  id TEXT PRIMARY KEY,"
            "  content TEXT,"
            "  memory_type TEXT,"
            "  tags TEXT NOT NULL DEFAULT '[]'"
            ")"
        )
        conn.commit()
        conn.close()

        needs, msg = check_bootstrap(db_path)
        assert needs is True
        assert "seed" in msg.lower()
    finally:
        os.unlink(db_path)


# -- ServiceManager tests --------------------------------------------


@pytest.mark.asyncio
async def test_service_manager_creation():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [
        ServiceDefinition(name="s1", command=["echo", "1"]),
        ServiceDefinition(name="s2", command=["echo", "2"], depends_on=["s1"]),
    ]
    mgr = ServiceManager(svcs, ".")

    statuses = mgr.get_status()
    assert statuses["s1"] == ServiceStatus.PENDING
    assert statuses["s2"] == ServiceStatus.PENDING


@pytest.mark.asyncio
async def test_service_manager_topological_order():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [
        ServiceDefinition(name="b", command=["echo"], depends_on=["a"]),
        ServiceDefinition(name="a", command=["echo"]),
    ]
    mgr = ServiceManager(svcs, ".")
    order = mgr._topological_order()
    names = [rt.definition.name for rt in order]
    assert names.index("a") < names.index("b"), f"a before b, got {names}"


def test_service_manager_cycle_detection():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [
        ServiceDefinition(name="a", command=["echo"], depends_on=["b"]),
        ServiceDefinition(name="b", command=["echo"], depends_on=["a"]),
    ]
    mgr = ServiceManager(svcs, ".")
    try:
        mgr._topological_order()
        pytest.fail("Should have raised ValueError for circular dependency")
    except ValueError as e:
        assert "Circular dependency" in str(e)


def test_service_manager_reset():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [ServiceDefinition(name="s1", command=["echo"])]
    mgr = ServiceManager(svcs, ".")
    mgr.reset_service("nonexistent")  # should not crash
    mgr.reset_service("s1")
    assert mgr.get_status()["s1"] == ServiceStatus.STOPPED


@pytest.mark.asyncio
async def test_service_manager_adds_project_root_to_child_pythonpath(monkeypatch):
    from plastic_promise.launcher.service_manager import ServiceManager

    captured = {}

    class FakeProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    async def fake_health_check(_self, _runtime):
        return True

    project_root = os.getcwd()
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ServiceManager, "_health_check", fake_health_check)

    svc = ServiceDefinition(
        name="maintenance-daemon",
        command=[sys.executable, "daemons/maintenance_daemon.py"],
        startup_timeout=1.0,
    )
    mgr = ServiceManager([svc], project_root)

    await mgr._start_service(mgr._runtimes["maintenance-daemon"])

    assert captured["cwd"] == project_root
    assert captured["env"]["PYTHONPATH"].split(os.pathsep)[0] == project_root


def test_maintenance_daemon_script_bootstraps_project_root_without_pythonpath(tmp_path):
    script_path = os.path.join(os.getcwd(), "daemons", "maintenance_daemon.py")
    code = (
        "import runpy; "
        f"runpy.run_path({script_path!r}, run_name='not_main'); "
        "print('daemon imports ok')"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PLASTIC_DB_PATH"] = str(tmp_path / "plastic_memory.db")

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "daemon imports ok" in result.stdout


def test_launcher_configures_default_project_identity(monkeypatch, tmp_path):
    module = _load_init_and_start()
    monkeypatch.delenv("PLASTIC_DB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_LANCEDB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.delenv("PP_PROJECT_ID", raising=False)

    module.configure_default_environment(str(tmp_path))

    assert os.environ["PLASTIC_DB_PATH"] == os.path.join(
        str(tmp_path), "data", "db", "plastic_memory.db"
    )
    assert os.environ["PLASTIC_LANCEDB_PATH"] == os.path.join(str(tmp_path), "data", "lancedb")
    assert os.environ["PLASTIC_PROJECT_ID"] == "project:plastic-promise"


def test_launcher_preserves_pp_project_id_fallback(monkeypatch, tmp_path):
    module = _load_init_and_start()
    monkeypatch.delenv("PLASTIC_DB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_LANCEDB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.setenv("PP_PROJECT_ID", "project:custom")

    module.configure_default_environment(str(tmp_path))

    assert "PLASTIC_PROJECT_ID" not in os.environ
    assert os.environ["PP_PROJECT_ID"] == "project:custom"


def test_launcher_configures_stable_embedder_timeout(monkeypatch, tmp_path):
    module = _load_init_and_start()
    monkeypatch.delenv("EMBEDDER_TIMEOUT", raising=False)

    module.configure_default_environment(str(tmp_path))

    assert os.environ["EMBEDDER_TIMEOUT"] == "30"


def test_launcher_preserves_embedder_timeout_override(monkeypatch, tmp_path):
    module = _load_init_and_start()
    monkeypatch.setenv("EMBEDDER_TIMEOUT", "45")

    module.configure_default_environment(str(tmp_path))

    assert os.environ["EMBEDDER_TIMEOUT"] == "45"


def test_stop_service_command_matcher_is_scoped():
    module = _load_init_and_start()

    assert module._is_managed_service_command_line(
        r"C:\Python\python.exe -m plastic_promise --streamable-http 9020"
    )
    assert module._is_managed_service_command_line(
        r"C:\Python\python.exe -m plastic_promise.mcp.server --streamable-http 9020"
    )
    assert module._is_managed_service_command_line(
        r'C:\Python\python.exe "F:\Agent\Memory system\daemons\maintenance_daemon.py"'
    )
    assert not module._is_managed_service_command_line(r"C:\Python\python.exe unrelated_script.py")
    assert not module._is_managed_service_command_line(
        r"C:\Python\python.exe -m other_package --streamable-http 9020"
    )


def test_stop_on_windows_without_owned_pid_files_does_not_enumerate_or_kill(monkeypatch):
    module = _load_init_and_start()
    calls = []

    class Result:
        stdout = ""
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.sys, "argv", ["init_and_start.py", "--stop"])
    monkeypatch.setattr(module.os.path, "exists", lambda _path: False)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.do_stop() is True

    assert calls == []


def test_stop_on_windows_skips_stale_pid_file_for_unrelated_process(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "maintenance_daemon.pid"
    pid_file.write_text("222", encoding="utf-8")
    calls = []

    class Result:
        stdout = '{"ProcessId":222,"CommandLine":"python unrelated_script.py"}'

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.sys, "argv", ["init_and_start.py", "--stop"])
    monkeypatch.setattr(module, "PID_FILE", str(pid_file))
    monkeypatch.setattr(module, "MCP_PID_FILE", str(tmp_path / "mcp_server.pid"))
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.do_stop() is True

    assert ["taskkill", "/F", "/PID", "222"] not in calls
    assert pid_file.exists()


def test_daemon_command_matcher_accepts_supported_manual_mcp_url(tmp_path):
    module = _load_init_and_start()
    script = tmp_path / "daemons" / "maintenance_daemon.py"

    assert module._argv_matches_owned_service(
        [sys.executable, str(script), "--mcp-url", "http://127.0.0.1:9020/mcp"],
        source_root=str(tmp_path),
        service_name="maintenance-daemon",
    )
    assert not module._argv_matches_owned_service(
        [sys.executable, str(script), "--unknown", "value"],
        source_root=str(tmp_path),
        service_name="maintenance-daemon",
    )


def test_stop_on_windows_rejects_managed_command_from_foreign_worktree(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "mcp_server.pid"
    pid_file.write_text("444", encoding="utf-8")
    calls = []

    class Result:
        stdout = (
            '{"ProcessId":444,"CommandLine":"python -m plastic_promise '
            '--streamable-http 9020 --source-root F:\\\\Agent\\\\other-worktree"}'
        )
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.sys, "argv", ["init_and_start.py", "--stop"])
    monkeypatch.setattr(module, "MCP_PID_FILE", str(pid_file))
    monkeypatch.setattr(module, "PID_FILE", str(tmp_path / "maintenance_daemon.pid"))
    monkeypatch.setattr(module, "_project_root", r"F:\Agent\owned-worktree")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.do_stop() is True

    assert ["taskkill", "/F", "/PID", "444"] not in calls


def test_stop_on_windows_kills_only_owned_pid_with_source_root_marker(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "mcp_server.pid"
    pid_file.write_text("555", encoding="utf-8")
    calls = []

    class Result:
        stdout = (
            '{"ProcessId":555,"CommandLine":"python -m plastic_promise '
            '--streamable-http 9020 --source-root F:\\\\Agent\\\\owned-worktree"}'
        )
        returncode = 0

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.sys, "argv", ["init_and_start.py", "--stop"])
    monkeypatch.setattr(module, "MCP_PID_FILE", str(pid_file))
    monkeypatch.setattr(module, "PID_FILE", str(tmp_path / "maintenance_daemon.pid"))
    monkeypatch.setattr(module, "_project_root", r"F:\Agent\owned-worktree")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.do_stop() is True

    assert ["taskkill", "/F", "/PID", "555"] in calls


def test_launcher_reuses_healthy_owned_daemon_without_scheduling_duplicate(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "maintenance_daemon.pid"
    heartbeat = tmp_path / "maintenance_daemon.heartbeat"
    pid_file.write_text("777", encoding="utf-8")
    heartbeat.write_text(
        json.dumps(
            {
                "schema": "maintenance-heartbeat/v1",
                "pid": 777,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "startup_replay_cycle_id": "startup-cycle",
                "startup_replay_owner_pid": 777,
                "process_generation": "a" * 32,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PID_FILE", str(pid_file))
    monkeypatch.setattr(
        module,
        "_maintenance_heartbeat_path",
        lambda: str(heartbeat),
    )
    monkeypatch.setattr(module, "_pid_alive", lambda pid: pid == 777)
    monkeypatch.setattr(module, "_process_create_time", lambda pid: 0.0 if pid == 777 else None)
    monkeypatch.setattr(
        module,
        "_pid_matches_owned_service",
        lambda pid, **_kwargs: pid == 777,
    )
    monkeypatch.setattr(
        "plastic_promise.launcher.service_manager.pid_is_alive",
        lambda pid: pid == 777,
    )

    status = module._inspect_existing_daemon()
    selected = module._select_services_to_start(
        mcp_already_running=True,
        daemon_already_running=status["status"] == "reuse",
    )

    assert status == {"status": "reuse", "pid": 777, "reason": "ok"}
    assert selected == []


def test_launcher_rejects_heartbeat_from_previous_pid_incarnation(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "maintenance_daemon.pid"
    heartbeat = tmp_path / "maintenance_daemon.heartbeat"
    pid_file.write_text("778", encoding="utf-8")
    heartbeat.write_text(
        json.dumps(
            {
                "schema": "maintenance-heartbeat/v1",
                "pid": 778,
                "updated_at": "2026-07-13T00:00:00Z",
                "startup_replay_cycle_id": "old-cycle",
                "startup_replay_owner_pid": 778,
                "process_generation": "b" * 32,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "PID_FILE", str(pid_file))
    monkeypatch.setattr(module, "_maintenance_heartbeat_path", lambda: str(heartbeat))
    monkeypatch.setattr(module, "_pid_alive", lambda pid: pid == 778)
    monkeypatch.setattr(module, "_pid_matches_owned_service", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        module,
        "_process_create_time",
        lambda pid: datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc).timestamp(),
    )

    assert module._inspect_existing_daemon() == {
        "status": "conflict",
        "pid": 778,
        "reason": "maintenance_pid_incarnation_mismatch",
    }


def test_process_create_time_uses_windows_os_probe_without_psutil_process(monkeypatch):
    module = _load_init_and_start()

    class Result:
        returncode = 0
        stdout = "2026-07-13T01:02:03.0000000Z\n"

    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: Result())

    expected = datetime(2026, 7, 13, 1, 2, 3, tzinfo=timezone.utc).timestamp()
    assert module._process_create_time(999999) == expected


def test_launcher_requires_daemon_argv_to_attest_current_source_revision(tmp_path):
    module = _load_init_and_start()
    script = tmp_path / "daemons" / "maintenance_daemon.py"
    argv = [
        sys.executable,
        str(script),
        "--source-root",
        str(tmp_path),
        "--source-revision",
        "a" * 40,
    ]

    assert module._argv_matches_owned_service(
        argv,
        source_root=str(tmp_path),
        service_name="maintenance-daemon",
        expected_source_revision="a" * 40,
    )
    assert not module._argv_matches_owned_service(
        argv,
        source_root=str(tmp_path),
        service_name="maintenance-daemon",
        expected_source_revision="b" * 40,
    )


def test_launcher_start_lock_prevents_concurrent_inspect_and_spawn(monkeypatch, tmp_path):
    module = _load_init_and_start()
    lock_path = tmp_path / "launcher-start.lock"
    monkeypatch.setattr(module, "_launcher_start_lock_path", lambda: str(lock_path))

    first = module._acquire_launcher_start_lock()
    try:
        assert first is not None
        assert module._acquire_launcher_start_lock() is None
    finally:
        module._release_launcher_start_lock(first)

    second = module._acquire_launcher_start_lock()
    assert second is not None
    module._release_launcher_start_lock(second)


def test_launcher_rejects_live_daemon_pid_owned_by_foreign_checkout(monkeypatch, tmp_path):
    module = _load_init_and_start()
    pid_file = tmp_path / "maintenance_daemon.pid"
    pid_file.write_text("888", encoding="utf-8")
    monkeypatch.setattr(module, "PID_FILE", str(pid_file))
    monkeypatch.setattr(module, "_pid_alive", lambda pid: pid == 888)
    monkeypatch.setattr(module, "_pid_matches_owned_service", lambda *_args, **_kwargs: False)

    assert module._inspect_existing_daemon() == {
        "status": "conflict",
        "pid": 888,
        "reason": "maintenance_pid_not_owned",
    }


@pytest.mark.asyncio
async def test_launcher_check_only_never_inspects_or_mutates_daemon_state(monkeypatch, tmp_path):
    module = _load_init_and_start()
    args = type(
        "Args",
        (),
        {
            "stop": False,
            "check_only": True,
            "mode": None,
            "skip_ollama_check": True,
            "skip_lancedb_warmup": True,
        },
    )()
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module, "configure_default_environment", lambda _root: None)
    monkeypatch.setattr(module, "resolve_source_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(module, "run_env_checks", lambda **_kwargs: (True, [], False))
    log_path = tmp_path / "launcher.log"
    monkeypatch.setattr(module, "LOG_FILE", str(log_path))
    monkeypatch.setattr(
        module,
        "_inspect_existing_daemon",
        lambda: pytest.fail("check-only inspected daemon state"),
    )

    await module.main()
    assert not log_path.exists()


def _mcp_health_payload(tmp_path, *, pid=321, revision="a" * 40):
    from plastic_promise.launcher.service_manager import MCP_FUSION_IDENTITY_SCHEMA

    return {
        "status": "ok",
        "version": "0.1.15",
        "pid": pid,
        "source_root": str(tmp_path),
        "source_revision": revision,
        "fusion_policy": "max-v1",
        "fusion_attestation": {
            "schema": MCP_FUSION_IDENTITY_SCHEMA,
            "requested_policy": "max-v1",
            "effective_policy": "max-v1",
            "requested_runtime": "rust",
            "effective_runtime": "python",
            "capability_reason": "policy_requires_python:max-v1",
            "candidate_id": "",
            "config_hash": "",
            "config": None,
        },
    }


class _HealthyRuntimeEngine:
    class Embedder:
        def embed(self, _text):
            return [0.5, 0.5]

    def __init__(self, *, vector=None, ldb=True):
        self._embedder = self.Embedder()
        if vector is not None:
            self._embedder.embed = lambda _text: vector
        self._ldb = object() if ldb else None
        self._graph_edges = {"edge": []}

    def _ensure_heavy_init(self):
        return None

    def _check_rust_health(self):
        return True


def test_mcp_health_identity_rejects_foreign_200_and_pid(tmp_path):
    from plastic_promise.launcher.service_manager import validate_mcp_health_identity

    payload = _mcp_health_payload(tmp_path, pid=321)
    valid, reason = validate_mcp_health_identity(
        payload,
        expected_pid=999,
        expected_source_root=tmp_path,
        expected_source_revision="a" * 40,
    )
    assert (valid, reason) == (False, "health_pid_mismatch")

    payload["pid"] = 999
    payload["source_root"] = str(tmp_path / "foreign")
    valid, reason = validate_mcp_health_identity(
        payload,
        expected_pid=999,
        expected_source_root=tmp_path,
        expected_source_revision="a" * 40,
    )
    assert (valid, reason) == (False, "health_source_root_mismatch")

    payload["source_root"] = str(tmp_path)
    payload["source_revision"] = "b" * 40
    valid, reason = validate_mcp_health_identity(
        payload,
        expected_pid=999,
        expected_source_root=tmp_path,
        expected_source_revision="a" * 40,
    )
    assert (valid, reason) == (False, "health_source_revision_mismatch")


@pytest.mark.parametrize(
    ("requested_runtime", "effective_runtime", "capability_reason", "expected_reason"),
    [
        ("rust", "rust", "rust_capability_satisfied", "health_fusion_capability_mismatch"),
        ("rust", "python", "arbitrary_reason", "health_fusion_capability_mismatch"),
        ("rust", "python", None, "health_fusion_runtime_invalid"),
    ],
)
def test_mcp_health_identity_rejects_impossible_or_unbound_capability(
    tmp_path, requested_runtime, effective_runtime, capability_reason, expected_reason
):
    from plastic_promise.launcher.service_manager import validate_mcp_health_identity

    payload = _mcp_health_payload(tmp_path)
    payload["fusion_attestation"].update(
        {
            "requested_runtime": requested_runtime,
            "effective_runtime": effective_runtime,
            "capability_reason": capability_reason,
        }
    )

    valid, reason = validate_mcp_health_identity(
        payload,
        expected_pid=321,
        expected_source_root=tmp_path,
        expected_source_revision="a" * 40,
    )

    assert (valid, reason) == (False, expected_reason)


def test_port_reuse_rejects_foreign_checkout_even_with_valid_http_200(monkeypatch, tmp_path):
    from plastic_promise.launcher import env_checker

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(_mcp_health_payload(tmp_path / "foreign", pid=701)).encode("utf-8")

    monkeypatch.setattr(env_checker.urllib.request, "urlopen", lambda *_a, **_k: Response())

    occupant = env_checker._identify_port_9020_occupant(
        expected_source_root=str(tmp_path),
        expected_source_revision="a" * 40,
    )

    assert occupant is None


@pytest.mark.asyncio
async def test_service_manager_rejects_foreign_mcp_http_200(monkeypatch, tmp_path):
    from plastic_promise.launcher import service_manager

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            payload = _mcp_health_payload(tmp_path / "foreign", pid=700)
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(service_manager.urllib.request, "urlopen", lambda *_a, **_k: Response())
    svc = ServiceDefinition(name="mcp-server", command=["python"], health_url="http://health")
    manager = service_manager.ServiceManager(
        [svc], str(tmp_path), expected_source_revision="a" * 40
    )
    runtime = manager._runtimes["mcp-server"]
    runtime.pid = 700

    assert await manager._health_check(runtime) is False


def test_server_health_identity_exposes_checkout_and_fusion(monkeypatch):
    from plastic_promise.core.fusion_policy import canonical_fusion_config_hash
    from plastic_promise.mcp import server

    config = {
        "k": 2,
        "channels": ["vector", "bm25", "fts"],
        "weights": {"vector": 0.6, "bm25": 0.25, "fts": 0.15},
        "windows": {"vector": 20, "bm25": 20, "fts": 20},
    }
    config_hash = canonical_fusion_config_hash(config)
    candidate = "wrrf-v1:" + config_hash
    monkeypatch.setenv("PP_RETRIEVAL_FUSION_POLICY", candidate)
    monkeypatch.setenv("PP_RETRIEVAL_RRF_K", "2")
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WEIGHTS_JSON", json.dumps(config["weights"]))
    monkeypatch.setenv("PP_RETRIEVAL_RRF_WINDOWS_JSON", json.dumps(config["windows"]))

    identity = server._server_process_identity(engine=_HealthyRuntimeEngine())

    assert identity["pid"] == os.getpid()
    assert identity["source_root"] == server._SOURCE_ROOT
    assert identity["source_revision"] == server._SOURCE_REVISION
    assert identity["fusion_policy"] == candidate
    assert identity["fusion_attestation"]["candidate_id"] == candidate
    assert identity["fusion_attestation"]["config_hash"] == config_hash
    assert identity["fusion_attestation"]["config"]["config_hash"] == config_hash
    assert identity["fusion_attestation"]["effective_policy"] == candidate
    assert identity["fusion_attestation"]["effective_runtime"] == "python"


def test_server_health_identity_rejects_missing_wrrf_config_and_revision(monkeypatch):
    from plastic_promise.core.fusion_policy import FusionConfigurationError
    from plastic_promise.mcp import server

    monkeypatch.setenv("PP_RETRIEVAL_FUSION_POLICY", "wrrf-v1:" + "b" * 64)
    monkeypatch.delenv("PP_RETRIEVAL_RRF_K", raising=False)
    with pytest.raises(FusionConfigurationError):
        server._server_process_identity(engine=_HealthyRuntimeEngine())

    monkeypatch.setattr(server, "_SOURCE_REVISION", None)
    with pytest.raises(RuntimeError, match="source_revision_unavailable"):
        server._server_process_identity()


@pytest.mark.parametrize(
    ("engine", "reason"),
    [
        (_HealthyRuntimeEngine(vector=[0.0, 0.0]), "retrieval_embedding_zero_or_invalid"),
        (_HealthyRuntimeEngine(ldb=False), "retrieval_lancedb_unavailable"),
    ],
)
def test_server_health_identity_rejects_unrunnable_retrieval_capability(engine, reason):
    from plastic_promise.mcp import server

    with pytest.raises(RuntimeError, match=reason):
        server._server_process_identity(
            engine=engine,
            environ={
                "PP_RETRIEVAL_FUSION_POLICY": "legacy-auto",
                "LDB_INIT_ON_HEAVY_INIT": "1",
            },
        )


@pytest.mark.parametrize(
    ("policy", "effective_runtime"),
    [("legacy-auto", "rust"), ("max-v1", "python")],
)
def test_server_health_identity_keeps_builtin_policies_healthy(policy, effective_runtime):
    from plastic_promise.mcp import server

    identity = server._server_process_identity(
        engine=_HealthyRuntimeEngine(),
        environ={
            "PP_RETRIEVAL_FUSION_POLICY": policy,
            "PP_PREFER_RUST_SUPPLY": "1",
            "PP_FORCE_PYTHON_SUPPLY": "0",
        },
    )

    assert identity["fusion_policy"] == policy
    assert identity["fusion_attestation"]["effective_policy"] == policy
    assert identity["fusion_attestation"]["effective_runtime"] == effective_runtime
    assert identity["fusion_attestation"]["config"] is None


def test_maintenance_health_binds_runtime_pid_and_replay_owner(monkeypatch, tmp_path):
    from plastic_promise.launcher import service_manager

    heartbeat = tmp_path / "maintenance.heartbeat"
    service_manager.write_maintenance_heartbeat(
        heartbeat,
        pid=101,
        startup_replay_cycle_id="cycle-101",
        process_generation="a" * 32,
    )
    monkeypatch.setattr(service_manager, "pid_is_alive", lambda _pid: True)

    health = service_manager.read_maintenance_health(
        heartbeat,
        expected_pid=202,
        expected_process_generation="a" * 32,
    )
    assert health["reason"] == "maintenance_pid_mismatch"

    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    payload["startup_replay_owner_pid"] = 202
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")
    health = service_manager.read_maintenance_health(
        heartbeat,
        expected_pid=101,
        expected_process_generation="a" * 32,
    )
    assert health["reason"] == "maintenance_startup_replay_owner_mismatch"

    payload["startup_replay_owner_pid"] = 101
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")
    health = service_manager.read_maintenance_health(
        heartbeat,
        expected_pid=101,
        expected_process_generation="b" * 32,
    )
    assert health["reason"] == "maintenance_process_generation_mismatch"


def test_owned_service_argv_requires_exact_root_and_daemon_path(tmp_path):
    module = _load_init_and_start()
    root = str(tmp_path / "owned")
    foreign = root + "-foreign"

    assert module._argv_matches_owned_service(
        ["python", "-m", "plastic_promise", "--streamable-http", "9020", "--source-root", root],
        source_root=root,
        service_name="mcp-server",
    )
    assert not module._argv_matches_owned_service(
        [
            "python",
            "-m",
            "plastic_promise",
            "--streamable-http",
            "9020",
            "--source-root",
            foreign,
        ],
        source_root=root,
        service_name="mcp-server",
    )
    assert not module._argv_matches_owned_service(
        ["python", os.path.join(foreign, "daemons", "maintenance_daemon.py")],
        source_root=root,
        service_name="maintenance-daemon",
    )
    daemon = os.path.join(root, "daemons", "maintenance_daemon.py")
    assert not module._argv_matches_owned_service(
        ["not-python", "-m", "plastic_promise", "--streamable-http", "9020", "--source-root", root],
        source_root=root,
        service_name="mcp-server",
    )
    assert not module._argv_matches_owned_service(
        [
            "python",
            "unrelated.py",
            "-m",
            "plastic_promise",
            "--streamable-http",
            "9020",
            "--source-root",
            root,
        ],
        source_root=root,
        service_name="mcp-server",
    )
    assert not module._argv_matches_owned_service(
        ["python", "wrapper.py", daemon],
        source_root=root,
        service_name="maintenance-daemon",
    )
    for argv in (
        [
            "python",
            "-m",
            "plastic_promise.mcp.server",
            "--streamable-http",
            "9020",
            "--source-root",
            root,
        ],
        ["python", "-m", "plastic_promise", "--http", "9020", "--source-root", root],
        ["python", "-m", "plastic_promise", "--streamable-http", "9021", "--source-root", root],
        ["python", daemon, "--once", "--json"],
    ):
        service_name = "maintenance-daemon" if daemon in argv else "mcp-server"
        assert not module._argv_matches_owned_service(
            argv,
            source_root=root,
            service_name=service_name,
        )


@pytest.mark.asyncio
async def test_launcher_refuses_start_when_source_revision_is_unavailable(monkeypatch):
    module = _load_init_and_start()
    monkeypatch.setattr(module.sys, "argv", ["init_and_start.py", "--check-only"])
    monkeypatch.setattr(module, "resolve_source_revision", lambda _root: None)

    with pytest.raises(SystemExit) as exc_info:
        await module.main()

    assert exc_info.value.code == 1


def test_direct_mcp_server_streamable_http_configures_default_project_identity(monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    captured = {}

    async def fake_run_streamable_http(port):
        captured["port"] = port

    monkeypatch.delenv("PLASTIC_DB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_LANCEDB_PATH", raising=False)
    monkeypatch.delenv("PLASTIC_PROJECT_ID", raising=False)
    monkeypatch.delenv("PP_PROJECT_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["server.py", "--streamable-http", "9020"])
    monkeypatch.setattr(mcp_server, "run_streamable_http", fake_run_streamable_http)

    asyncio.run(mcp_server.main())

    assert captured["port"] == 9020
    assert os.environ["PLASTIC_MCP_TRANSPORT"] == "streamable_http"
    assert os.environ["PLASTIC_PROJECT_ID"] == "project:plastic-promise"
    assert os.environ["PLASTIC_DB_PATH"].endswith(os.path.join("data", "db", "plastic_memory.db"))
    assert os.environ["PLASTIC_LANCEDB_PATH"].endswith(os.path.join("data", "lancedb"))


def test_direct_mcp_server_applies_explicit_full_runtime_mode(monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    class FakeEngine:
        def refresh_runtime_mode(self, initialize_heavy=False, *, synchronize_index=False):
            assert initialize_heavy is True
            assert synchronize_index is True
            return {"index_sync": {"requested": True, "ready": True, "status": "ready"}}

    async def fake_run_streamable_http(_port):
        return None

    monkeypatch.setenv("PLASTIC_RUNTIME_MODE", "rust-full")
    # setenv records an absent original value so additions made by main() are
    # removed during teardown; delenv(..., raising=False) cannot track an
    # already-absent key and leaks full-mode state into later tests.
    monkeypatch.setenv("PP_MEMORY_CHUNKING", "off")
    monkeypatch.setenv("PP_MEMORY_CHUNK_ENGINE", "python")
    monkeypatch.setattr(sys, "argv", ["server.py", "--streamable-http", "9020"])
    monkeypatch.setattr(mcp_server, "run_streamable_http", fake_run_streamable_http)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: FakeEngine())

    asyncio.run(mcp_server.main())

    assert os.environ["PP_MEMORY_CHUNKING"] == "structure-v1"
    assert os.environ["PP_MEMORY_CHUNK_ENGINE"] == "rust"


def test_direct_mcp_server_legacy_sse_alias_still_routes_to_streamable_http(monkeypatch):
    from plastic_promise.mcp import server as mcp_server

    captured = {}

    async def fake_run_streamable_http(port):
        captured["port"] = port

    monkeypatch.delenv("PLASTIC_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("PLASTIC_MCP_LEGACY_TRANSPORT_ALIAS", raising=False)
    monkeypatch.setattr(sys, "argv", ["server.py", "--sse", "9020"])
    monkeypatch.setattr(mcp_server, "run_streamable_http", fake_run_streamable_http)

    asyncio.run(mcp_server.main())

    assert captured["port"] == 9020
    assert os.environ["PLASTIC_MCP_TRANSPORT"] == "streamable_http"
    assert os.environ["PLASTIC_MCP_LEGACY_TRANSPORT_ALIAS"] == "sse"


def test_packaged_streamable_http_entrypoints_resolve():
    with open("pyproject.toml", encoding="utf-8") as project_file:
        data = tomllib.loads(project_file.read())
    scripts = data["project"]["scripts"]

    assert scripts["plastic-promise-streamable-http"] == "plastic_promise:main_streamable_http"
    assert scripts["plastic-promise-http"] == "plastic_promise:main_http"
    assert scripts["plastic-promise-sse"] == "plastic_promise:main_sse"

    import plastic_promise

    assert callable(plastic_promise.main_streamable_http)
    assert callable(plastic_promise.main_http)
    assert callable(plastic_promise.main_sse)


def test_packaged_lancedb_dependency_excludes_known_native_fts_bug():
    with open("pyproject.toml", encoding="utf-8") as project_file:
        data = tomllib.loads(project_file.read())
    dependencies = set(data["project"]["dependencies"])
    with open("requirements.txt", encoding="utf-8") as requirements_file:
        requirements = {
            line.strip()
            for line in requirements_file
            if line.strip() and not line.lstrip().startswith("#")
        }

    assert "lancedb>=0.34.0" in dependencies
    assert "lancedb>=0.34.0" in requirements


def test_top_level_module_accepts_streamable_http_and_legacy_sse_flags():
    import plastic_promise.__main__ as top_level

    assert top_level._extract_streamable_http_port(
        ["plastic_promise", "--streamable-http", "9021"]
    ) == (
        True,
        9021,
    )
    assert top_level._extract_streamable_http_port(["plastic_promise", "--http", "9022"]) == (
        True,
        9022,
    )
    assert top_level._extract_streamable_http_port(["plastic_promise", "--sse", "9023"]) == (
        True,
        9023,
    )
    assert top_level._extract_streamable_http_port(["plastic_promise"]) == (False, 9020)


def test_pid_alive_nonexistent():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [ServiceDefinition(name="s1", command=["echo"])]
    mgr = ServiceManager(svcs, ".")
    assert mgr._pid_alive(99999999) is False


def test_lancedb_warmup_sets_maintenance_env_only_during_pass(monkeypatch):
    module = _load_init_and_start()
    calls = []

    class FakeContextEngine:
        def _ensure_heavy_init(self):
            calls.append(
                {
                    "transport": os.environ.get("PLASTIC_MCP_TRANSPORT"),
                    "init": os.environ.get("LDB_INIT_ON_HEAVY_INIT"),
                    "backfill": os.environ.get("LDB_BACKFILL_ON_INIT"),
                    "rebuild": os.environ.get("LDB_REBUILD_ON_INIT"),
                }
            )

    monkeypatch.setattr(module, "ContextEngine", FakeContextEngine)
    monkeypatch.setenv("PLASTIC_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("LDB_INIT_ON_HEAVY_INIT", raising=False)
    monkeypatch.delenv("LDB_BACKFILL_ON_INIT", raising=False)
    monkeypatch.delenv("LDB_REBUILD_ON_INIT", raising=False)

    ok, msg = module.run_lancedb_warmup_maintenance()

    assert ok is True
    assert "ready" in msg
    assert calls == [
        {
            "transport": "streamable_http",
            "init": "1",
            "backfill": "1",
            "rebuild": "1",
        }
    ]
    assert os.environ.get("PLASTIC_MCP_TRANSPORT") == "stdio"
    assert "LDB_INIT_ON_HEAVY_INIT" not in os.environ
    assert "LDB_BACKFILL_ON_INIT" not in os.environ
    assert "LDB_REBUILD_ON_INIT" not in os.environ


def test_lancedb_warmup_reports_sync_repair_counts(monkeypatch):
    module = _load_init_and_start()

    class FakeLdb:
        def count_rows(self):
            return 4

    class FakeContextEngine:
        def __init__(self):
            self._ldb = FakeLdb()
            self._lancedb_sync_status = {
                "success": True,
                "orphan_deleted": 2,
                "missing_backfilled": 1,
                "missing_skipped": 0,
            }

        def _ensure_heavy_init(self):
            pass

    monkeypatch.setattr(module, "ContextEngine", FakeContextEngine)

    ok, msg = module.run_lancedb_warmup_maintenance()

    assert ok is True
    assert msg == "ready (4 rows, sync=orphans:2 missing:1 skipped:0)"


def test_lancedb_warmup_reports_sync_degraded(monkeypatch):
    module = _load_init_and_start()

    class FakeLdb:
        def count_rows(self):
            return 4

    class FakeContextEngine:
        def __init__(self):
            self._ldb = FakeLdb()
            self._lancedb_sync_status = {"success": False, "error": "lancedb locked"}

        def _ensure_heavy_init(self):
            pass

    monkeypatch.setattr(module, "ContextEngine", FakeContextEngine)

    ok, msg = module.run_lancedb_warmup_maintenance()

    assert ok is True
    assert msg == "ready (4 rows, sync=degraded:lancedb locked)"


def test_startup_recovery_reports_released_stale_claims(monkeypatch):
    module = _load_init_and_start()

    monkeypatch.setattr(
        module,
        "release_stale_claims",
        lambda: {"released_count": 2, "escalated_count": 1},
    )

    ok, msg = module.run_startup_recovery()

    assert ok is True
    assert msg == "stale_claims_released=2, escalated=1"


def test_startup_recovery_degrades_without_blocking(monkeypatch):
    module = _load_init_and_start()

    def fail_recovery():
        raise RuntimeError("database locked")

    monkeypatch.setattr(module, "release_stale_claims", fail_recovery)

    ok, msg = module.run_startup_recovery()

    assert ok is False
    assert "database locked" in msg


@pytest.mark.asyncio
async def test_main_stop_returns_before_runtime_mode_prompt(monkeypatch):
    module = _load_init_and_start()
    stopped = []

    monkeypatch.setattr(sys, "argv", ["init_and_start.py", "--stop"])
    monkeypatch.setattr(module, "do_stop", lambda: stopped.append(True) or True)

    def fail_select_runtime_mode(*args, **kwargs):
        raise AssertionError("stop should not select a startup mode")

    monkeypatch.setattr(module, "select_runtime_mode", fail_select_runtime_mode)

    await module.main()

    assert stopped == [True]


# -- ServiceRuntime tests --------------------------------------------


def test_service_runtime_backoff():
    from plastic_promise.launcher.service_manager import ServiceRuntime

    svc = ServiceDefinition(
        name="test",
        command=["echo"],
        restart_policy=RestartPolicy(
            max_retries=5,
            window_seconds=60.0,
            backoff_base=1.0,
            backoff_multiplier=2.0,
            max_backoff=30.0,
        ),
    )
    rt = ServiceRuntime(svc)

    # First restart: backoff = 1.0 * 2^0 = 1.0
    rt.record_restart()
    assert rt.backoff_seconds() == 1.0

    # Second restart: backoff = 1.0 * 2^1 = 2.0
    rt.record_restart()
    assert rt.backoff_seconds() == 2.0

    # Not unrecoverable yet
    assert not rt.is_unrecoverable()


def test_service_runtime_unrecoverable():
    from plastic_promise.launcher.service_manager import ServiceRuntime

    svc = ServiceDefinition(
        name="test",
        command=["echo"],
        restart_policy=RestartPolicy(max_retries=3, window_seconds=60.0),
    )
    rt = ServiceRuntime(svc)

    for _ in range(3):
        rt.record_restart()

    assert rt.is_unrecoverable()


# -- Watchdog long-call health tolerance tests ------------------------


def test_watchdog_does_not_restart_alive_process_during_health_grace():
    from plastic_promise.launcher.service_manager import ServiceRuntime
    from plastic_promise.launcher.watchdog import _should_restart_unhealthy_service

    class AliveProcess:
        def poll(self):
            return None

    svc = ServiceDefinition(name="mcp-server", command=["python"], health_url="http://health")
    rt = ServiceRuntime(svc)
    rt.process = AliveProcess()
    rt.consecutive_failures = 3
    rt.first_unhealthy_at = 100.0

    assert _should_restart_unhealthy_service(rt, now=120.0) is False
    assert _should_restart_unhealthy_service(rt, now=281.0) is True


def test_watchdog_restarts_dead_process_without_health_grace():
    from plastic_promise.launcher.service_manager import ServiceRuntime
    from plastic_promise.launcher.watchdog import _should_restart_unhealthy_service

    class DeadProcess:
        def poll(self):
            return 1

    svc = ServiceDefinition(name="mcp-server", command=["python"], health_url="http://health")
    rt = ServiceRuntime(svc)
    rt.process = DeadProcess()
    rt.consecutive_failures = 1
    rt.first_unhealthy_at = 100.0

    assert _should_restart_unhealthy_service(rt, now=101.0) is True
