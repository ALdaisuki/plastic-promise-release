from pathlib import Path

from plastic_promise.passive_memory.codex_hook import HookConfig


def _payload(cwd: Path) -> dict[str, str]:
    return {
        "cwd": str(cwd),
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session:test",
        "turn_id": "turn:test",
        "prompt": "continue",
    }


def _git_repo(path: Path, remote: str) -> Path:
    path.mkdir()
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        f'[remote "origin"]\n\turl = {remote}\n',
        encoding="utf-8",
    )
    return path


def test_hook_derives_same_persistent_project_id_for_clones(tmp_path):
    first = _git_repo(tmp_path / "clone-a", "git@github.com:ALdaisuki/plastic-promise.git")
    second = _git_repo(
        tmp_path / "clone-b",
        "https://github.com/ALdaisuki/plastic-promise.git",
    )

    first_config = HookConfig.from_environ({}, _payload(first))
    second_config = HookConfig.from_environ({}, _payload(second))

    assert first_config.project_id == "project:repo:github.com/aldaisuki/plastic-promise"
    assert second_config.project_id == first_config.project_id


def test_hook_keeps_unrelated_local_repositories_isolated(tmp_path):
    first = _git_repo(tmp_path / "local-a", "")
    second = _git_repo(tmp_path / "local-b", "")

    first_id = HookConfig.from_environ({}, _payload(first)).project_id
    second_id = HookConfig.from_environ({}, _payload(second)).project_id

    assert first_id.startswith("project:local:")
    assert second_id.startswith("project:local:")
    assert first_id != second_id


def test_explicit_project_id_still_overrides_repository_identity(tmp_path):
    repo = _git_repo(tmp_path / "clone", "git@github.com:owner/repo.git")

    config = HookConfig.from_environ(
        {"PP_CODEX_HOOK_PROJECT_ID": "project:explicit"},
        _payload(repo),
    )

    assert config.project_id == "project:explicit"
