from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_SCRIPT = _ROOT / "scripts" / "deploy_server_revision.py"
_SPEC = importlib.util.spec_from_file_location("deploy_server_revision", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(module)


def _parse(*extra: str):
    revision = "a" * 40
    current = "b" * 40
    return module.create_parser().parse_args(
        [
            "--ssh-target",
            "root@example.internal",
            "--revision",
            revision,
            "--expected-current-revision",
            current,
            *extra,
        ]
    )


def test_default_mode_is_a_no_secret_plan():
    plan = module.build_plan(_parse())

    assert plan["apply"] is False
    assert plan["health_timeout_seconds"] == 10
    assert plan["transport"] == "prerequisite-bound-offline-git-bundle"
    assert plan["database_backup"] == "sqlite-online-backup+integrity-check+sha256"
    assert plan["host_timezone_mutation"] is False


@pytest.mark.parametrize("timeout", ("0", "11", "120"))
def test_health_timeout_cannot_exceed_ten_seconds(timeout: str):
    with pytest.raises(SystemExit):
        _parse("--health-timeout-seconds", timeout)


@pytest.mark.parametrize(
    "option,value",
    (
        ("--remote-runtime", "/"),
        ("--remote-state", "relative/path"),
        ("--canonical-db", "/srv/state/db;touch-bad"),
        ("--health-url", "https://example.com/health"),
        ("--ssh-target", "root@example;bad"),
    ),
)
def test_operator_inputs_fail_closed(option: str, value: str):
    with pytest.raises(SystemExit):
        _parse(option, value)


def test_remote_contract_covers_backup_rollback_revision_health_and_utc():
    remote = module._REMOTE_SCRIPT

    for required in (
        "source.backup(destination)",
        "PRAGMA integrity_check",
        "sha256",
        "git bundle verify",
        "server_runtime_worktree_dirty",
        "server_current_revision_mismatch",
        "canonical_mcp_service_not_active",
        "server_cutover_rolled_back",
        "server_health_revision_mismatch",
        "TZ=UTC",
        "SECONDS + health_timeout",
    ):
        assert required in remote


def test_remote_contract_never_mutates_host_timezone():
    remote = module._REMOTE_SCRIPT

    assert "timedatectl set-timezone" not in remote
    assert "Set-TimeZone" not in remote
    assert "/etc/localtime" not in remote
