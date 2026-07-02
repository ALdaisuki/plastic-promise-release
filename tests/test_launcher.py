"""Tests for One-Click Launcher components."""

import os
import sqlite3
import tempfile
import pytest

from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    ServiceStatus,
    RestartPolicy,
)
from plastic_promise.launcher.env_checker import run_env_checks
from plastic_promise.launcher.bootstrap_checker import check_bootstrap


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


def test_pid_alive_nonexistent():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [ServiceDefinition(name="s1", command=["echo"])]
    mgr = ServiceManager(svcs, ".")
    assert mgr._pid_alive(99999999) is False


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
