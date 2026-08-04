from __future__ import annotations

import os
import plistlib
import stat

import pytest

from scripts import manage_codex_hook_cleanup_launchd as launchd


def _fake_python(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\ncase \"$2\" in\n  *'sys.version_info >= (3, 10)'*) exit 0 ;;\nesac\nexit 1\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    return python


def test_launch_agent_runs_bounded_cleanup_from_project_venv(tmp_path):
    python = _fake_python(tmp_path)
    state_dir = tmp_path / "var" / "codex-hooks"

    payload = launchd.build_launch_agent(
        project_root=tmp_path,
        python_executable=python,
        state_dir=state_dir,
    )

    assert payload["Label"] == launchd.LABEL
    assert payload["ProgramArguments"] == [
        str(python.resolve()),
        "-m",
        "plastic_promise.passive_memory.codex_hook",
        "--cleanup-states",
    ]
    assert payload["StartInterval"] == 900
    assert payload["RunAtLoad"] is True
    assert payload["EnvironmentVariables"]["PP_CODEX_HOOK_STATE_DIR"] == str(state_dir.resolve())
    assert "secret" not in repr(payload).casefold()


def test_launch_agent_interval_is_bounded(tmp_path):
    python = _fake_python(tmp_path)

    with pytest.raises(ValueError, match="interval_seconds"):
        launchd.build_launch_agent(
            project_root=tmp_path,
            python_executable=python,
            state_dir=tmp_path / "states",
            interval_seconds=59,
        )


def test_plist_write_is_atomic_owner_only_and_parseable(tmp_path):
    python = _fake_python(tmp_path)
    payload = launchd.build_launch_agent(
        project_root=tmp_path,
        python_executable=python,
        state_dir=tmp_path / "states",
    )
    path = tmp_path / "LaunchAgents" / f"{launchd.LABEL}.plist"

    launchd._write_plist(path, payload)

    assert plistlib.loads(path.read_bytes()) == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_project_python_requires_executable_venv(tmp_path):
    with pytest.raises(RuntimeError, match="project Python not found"):
        launchd._project_python(tmp_path)

    python = _fake_python(tmp_path)
    assert os.path.samefile(launchd._project_python(tmp_path), python)


def test_project_python_rejects_executable_that_fails_version_probe(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python.chmod(0o700)

    with pytest.raises(RuntimeError, match="Python 3.10"):
        launchd._project_python(tmp_path)
