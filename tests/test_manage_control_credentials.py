from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "manage_control_credentials.py"


def _module():
    spec = importlib.util.spec_from_file_location("manage_control_credentials", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_credentials_file_contains_only_digests_and_uses_private_permissions(tmp_path):
    output = tmp_path / "control" / "credentials.env"
    module = _module()

    tokens = module.create_credentials(output)
    content = output.read_text(encoding="ascii")
    expected = "".join(
        f"{env_name}={hashlib.sha256(tokens[role].encode('ascii')).hexdigest()}\n"
        for role, env_name in module._ROLES
    )

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert content == expected
    assert all(token not in content for token in tokens.values())
    assert content.count("_TOKEN_SHA256=") == 3


def test_credentials_creation_refuses_to_overwrite(tmp_path):
    output = tmp_path / "credentials.env"
    output.write_text("existing", encoding="ascii")

    with pytest.raises(FileExistsError):
        _module().create_credentials(output)

    assert output.read_text(encoding="ascii") == "existing"


def test_existing_private_parent_is_not_chmodded(tmp_path, monkeypatch):
    parent = tmp_path / "control"
    parent.mkdir(mode=0o700)
    output = parent / "credentials.env"
    original_chmod = Path.chmod

    def refuse_parent_chmod(path, mode, *args, **kwargs):
        if path == parent:
            raise AssertionError("existing parent must not be chmodded")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", refuse_parent_chmod)

    _module().create_credentials(output)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o701, 0o710, 0o750])
def test_existing_parent_with_group_or_other_access_fails_closed(tmp_path, mode):
    parent = tmp_path / "control"
    parent.mkdir(mode=0o700)
    parent.chmod(mode)
    output = parent / "credentials.env"

    with pytest.raises(PermissionError, match="group or other access"):
        _module().create_credentials(output)

    assert stat.S_IMODE(parent.stat().st_mode) == mode
    assert not output.exists()


def test_symlink_parent_fails_closed(tmp_path):
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(private_parent, target_is_directory=True)

    with pytest.raises(PermissionError, match="must not be a symlink"):
        _module().create_credentials(symlink_parent / "credentials.env")

    assert not (private_parent / "credentials.env").exists()


def test_non_directory_parent_fails_closed(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("existing", encoding="ascii")

    with pytest.raises(NotADirectoryError, match="not a directory"):
        _module().create_credentials(parent / "credentials.env")

    assert parent.read_text(encoding="ascii") == "existing"


def test_parent_owned_by_another_user_fails_closed(tmp_path, monkeypatch):
    parent = tmp_path / "control"
    parent.mkdir(mode=0o700)
    module = _module()
    monkeypatch.setattr(module, "_effective_uid", lambda: os.geteuid() + 1)

    with pytest.raises(PermissionError, match="not owned by the current user"):
        module.create_credentials(parent / "credentials.env")


def test_existing_target_symlink_is_not_followed_or_overwritten(tmp_path):
    parent = tmp_path / "control"
    parent.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="ascii")
    output = parent / "credentials.env"
    output.symlink_to(victim)

    with pytest.raises(FileExistsError):
        _module().create_credentials(output)

    assert output.is_symlink()
    assert victim.read_text(encoding="ascii") == "unchanged"
    assert not list(parent.glob(".*.tmp"))
