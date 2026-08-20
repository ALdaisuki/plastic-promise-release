from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_UNIT_ROOT = _ROOT / "deploy" / "systemd"
_MCP_DROPIN = _UNIT_ROOT / "plastic-promise-mcp.service.d" / "10-managed-env.conf"
_MAINTENANCE_UNIT = _UNIT_ROOT / "plastic-promise-maintenance.service"
_BASE_ENV = "EnvironmentFile=/srv/plastic-promise/state/plastic-promise.env"
_OPTIONAL_MANAGED_ENV = "EnvironmentFile=-/srv/plastic-promise/state/control/managed.env"
_REQUIRED_MANAGED_ENV = "EnvironmentFile=/srv/plastic-promise/state/control/managed.env"
_CREDENTIALS_ENV = "EnvironmentFile=/srv/plastic-promise/state/secrets/control-credentials.env"
_UV_PYTHON = "BindReadOnlyPaths=/home/plastic/.local/share/uv/python"
_UTC_ENV = "Environment=TZ=UTC"
_UNITS = {
    "plastic-promise-inference-gateway.service": {
        "exec_start": (
            "ExecStart=/srv/plastic-promise/runtime/.venv/bin/"
            "plastic-promise-inference-gateway --port 9030"
        ),
        "writable": "ReadWritePaths=/srv/plastic-promise/state/inference",
        "timeout_stop": "TimeoutStopSec=45s",
        "credentials": False,
        "managed_env": _REQUIRED_MANAGED_ENV,
    },
    "plastic-promise-control-plane.service": {
        "exec_start": (
            "ExecStart=/srv/plastic-promise/runtime/.venv/bin/"
            "plastic-promise-control-plane --port 9040"
        ),
        "writable": "ReadWritePaths=/srv/plastic-promise/state/control",
        "timeout_stop": "TimeoutStopSec=30s",
        "credentials": True,
        "managed_env": _OPTIONAL_MANAGED_ENV,
    },
}

pytestmark = pytest.mark.skipif(
    not (_ROOT / "MANIFEST.in").exists() or not _UNIT_ROOT.exists(),
    reason="deployment assets are excluded from the standard release variant",
)


def _directives(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="ascii").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_source_distribution_includes_systemd_units():
    manifest = (_ROOT / "MANIFEST.in").read_text(encoding="ascii").splitlines()

    assert "recursive-include deploy/systemd *.service" in manifest
    assert "recursive-include deploy/systemd *.conf" in manifest


def test_mcp_dropin_loads_activated_environment_after_bootstrap():
    directives = _directives(_MCP_DROPIN)

    assert directives == [
        "[Service]",
        _UTC_ENV,
        "EnvironmentFile=-/srv/plastic-promise/state/control/managed.env",
    ]
    assert _BASE_ENV not in directives
    assert all("0.0.0.0" not in directive for directive in directives)
    assert all("[::]" not in directive for directive in directives)


def test_maintenance_unit_is_fail_closed_and_loads_managed_environment_last():
    directives = _directives(_MAINTENANCE_UNIT)
    exec_start = (
        "ExecStart=/srv/plastic-promise/runtime/.venv/bin/python "
        "/srv/plastic-promise/runtime/daemons/maintenance_daemon.py "
        "--mcp-url http://127.0.0.1:9020/mcp"
    )

    for required in (
        "[Unit]",
        "[Service]",
        "[Install]",
        "Type=simple",
        "User=plastic",
        "Group=plastic",
        "WorkingDirectory=/srv/plastic-promise/runtime",
        "UMask=0077",
        _UTC_ENV,
        _BASE_ENV,
        _OPTIONAL_MANAGED_ENV,
        "Environment=PP_MAINTENANCE_RUN_DIR=/srv/plastic-promise/state/run",
        exec_start,
        "Restart=on-failure",
        "RestartPreventExitStatus=3",
        "RestartSec=5s",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        _UV_PYTHON,
        "ReadWritePaths=/srv/plastic-promise/state/db",
        "ReadWritePaths=/srv/plastic-promise/state/lancedb",
        "ReadWritePaths=/srv/plastic-promise/state/audit",
        "ReadWritePaths=/srv/plastic-promise/state/run",
        "WantedBy=multi-user.target",
    ):
        assert required in directives

    assert directives.index(_BASE_ENV) < directives.index(_OPTIONAL_MANAGED_ENV)
    assert all("0.0.0.0" not in directive for directive in directives)
    assert all("[::]" not in directive for directive in directives)
    assert all("Environment=PP_MAINTENANCE_ENABLED=1" not in directive for directive in directives)
    assert "ReadWritePaths=/srv/plastic-promise/state" not in directives


@pytest.mark.parametrize(("unit_name", "contract"), _UNITS.items())
def test_systemd_unit_contract(unit_name, contract):
    directives = _directives(_UNIT_ROOT / unit_name)

    for required in (
        "[Unit]",
        "[Service]",
        "[Install]",
        "Type=simple",
        "User=plastic",
        "Group=plastic",
        "WorkingDirectory=/srv/plastic-promise/runtime",
        "UMask=0077",
        _UTC_ENV,
        contract["exec_start"],
        "Restart=on-failure",
        "RestartSec=5s",
        "TimeoutStartSec=120s",
        contract["timeout_stop"],
        "KillSignal=SIGTERM",
        "StandardOutput=journal",
        "StandardError=journal",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        _UV_PYTHON,
        contract["writable"],
        "WantedBy=multi-user.target",
    ):
        assert required in directives

    assert directives.index(_BASE_ENV) < directives.index(contract["managed_env"])
    assert all("0.0.0.0" not in directive for directive in directives)
    assert all("[::]" not in directive for directive in directives)
    assert all("--host" not in directive for directive in directives)
    assert all("maintenance" not in directive.casefold() for directive in directives)

    if contract["credentials"]:
        assert directives.index(_BASE_ENV) < directives.index(_CREDENTIALS_ENV)
        assert directives.index(_CREDENTIALS_ENV) < directives.index(contract["managed_env"])
    else:
        assert _CREDENTIALS_ENV not in directives


@pytest.mark.parametrize("unit_name", _UNITS)
def test_uv_managed_python_below_home_remains_readable(unit_name):
    directives = _directives(_UNIT_ROOT / unit_name)

    assert "ProtectHome=tmpfs" in directives
    assert _UV_PYTHON in directives
    assert "ProtectHome=read-only" not in directives
    assert "ProtectHome=true" not in directives
    assert all("/.ssh" not in directive for directive in directives)
    assert all("/.config/gh" not in directive for directive in directives)


def test_gateway_mount_namespace_isolates_job_database_from_canonical_sqlite():
    directives = _directives(_UNIT_ROOT / "plastic-promise-inference-gateway.service")

    assert "ReadWritePaths=/srv/plastic-promise/state/inference" in directives
    assert "ReadOnlyPaths=/srv/plastic-promise/state/db" in directives
    assert "ReadWritePaths=/srv/plastic-promise/state/db" not in directives
    assert _REQUIRED_MANAGED_ENV in directives
    assert _OPTIONAL_MANAGED_ENV not in directives


def test_knowledge_ingest_unit_uses_canonical_utc_runtime():
    directives = _directives(_UNIT_ROOT / "plastic-promise-knowledge-ingest.service")

    assert _UTC_ENV in directives


def test_control_credentials_are_outside_the_control_plane_write_root():
    directives = _directives(_UNIT_ROOT / "plastic-promise-control-plane.service")

    assert _CREDENTIALS_ENV in directives
    assert "ReadWritePaths=/srv/plastic-promise/state/control" in directives
    assert "ReadOnlyPaths=/srv/plastic-promise/state/secrets" in directives
    assert "/state/control/credentials.env" not in "\n".join(directives)


@pytest.mark.parametrize(
    "module_path",
    (
        _ROOT / "plastic_promise" / "mcp" / "inference_gateway_server.py",
        _ROOT / "plastic_promise" / "mcp" / "control_plane_server.py",
    ),
)
def test_systemd_entry_points_have_no_public_bind_option(module_path):
    source = module_path.read_text(encoding="utf-8")

    assert 'bind_host="127.0.0.1"' in source
    assert 'host="127.0.0.1"' in source
    assert 'parser.add_argument("--port"' in source
    assert 'parser.add_argument("--host"' not in source


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is available only on the Linux deployment/CI host",
)
def test_systemd_analyze_accepts_unit_syntax(tmp_path):
    staged_units = []
    for unit_name, contract in _UNITS.items():
        content = (_UNIT_ROOT / unit_name).read_text(encoding="ascii")
        content = content.replace(contract["exec_start"], "ExecStart=/bin/true")
        content = content.replace(
            "WorkingDirectory=/srv/plastic-promise/runtime",
            "WorkingDirectory=/",
        )
        content = content.replace("EnvironmentFile=/", "EnvironmentFile=-/")
        content = content.replace("ReadWritePaths=/", "ReadWritePaths=-/")
        content = content.replace("ReadOnlyPaths=/", "ReadOnlyPaths=-/")
        staged = tmp_path / unit_name
        staged.write_text(content, encoding="ascii")
        staged_units.append(str(staged))

    maintenance_content = _MAINTENANCE_UNIT.read_text(encoding="ascii")
    maintenance_exec = next(
        directive
        for directive in _directives(_MAINTENANCE_UNIT)
        if directive.startswith("ExecStart=")
    )
    maintenance_content = maintenance_content.replace(maintenance_exec, "ExecStart=/bin/true")
    maintenance_content = maintenance_content.replace(
        "WorkingDirectory=/srv/plastic-promise/runtime",
        "WorkingDirectory=/",
    )
    maintenance_content = maintenance_content.replace("EnvironmentFile=/", "EnvironmentFile=-/")
    maintenance_content = maintenance_content.replace("ReadWritePaths=/", "ReadWritePaths=-/")
    staged_maintenance = tmp_path / _MAINTENANCE_UNIT.name
    staged_maintenance.write_text(maintenance_content, encoding="ascii")
    staged_units.append(str(staged_maintenance))

    completed = subprocess.run(
        ["systemd-analyze", "verify", *staged_units],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
