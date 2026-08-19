import json
from pathlib import Path

import pytest

from plastic_promise.mcp.tools.request_scope import build_request_scope
from plastic_promise.passive_memory.codex_hook import HookConfig, process_hook


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


def test_process_wide_project_variables_cannot_override_workspace_identity(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "clone", "git@github.com:owner/repo.git")

    config = HookConfig.from_environ(
        {
            "PP_PROJECT_ID": "project:stale-global",
            "PLASTIC_PROJECT_ID": "project:another-stale-global",
        },
        _payload(repo),
    )

    assert config.project_id == "project:repo:github.com/owner/repo"


def test_hook_without_workspace_identity_fails_closed_to_unknown_project():
    config = HookConfig.from_environ(
        {},
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session:missing-cwd",
            "turn_id": "turn:missing-cwd",
            "prompt": "continue",
        },
    )

    assert config.project_id == "project:unknown"


@pytest.mark.parametrize(
    "workspace_key",
    ["working_directory", "workdir", "workspace_root", "project_root"],
)
def test_hook_accepts_workspace_aliases(tmp_path, workspace_key):
    repository = _git_repo(tmp_path / workspace_key, "git@github.com:example/aliased.git")
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session:alias",
        "turn_id": "turn:alias",
        workspace_key: str(repository),
    }

    config = HookConfig.from_environ({}, payload)

    assert config.project_id == "project:repo:github.com/example/aliased"


@pytest.mark.asyncio
async def test_hook_turn_state_records_project_identity(tmp_path):
    repository = _git_repo(tmp_path / "project", "git@github.com:example/project.git")
    state_dir = tmp_path / "hook-state"
    environment = {
        "PP_CODEX_HOOK_STATE_DIR": str(state_dir),
        "PP_PASSIVE_CONTEXT": "on",
        "PP_PASSIVE_MEMORY": "on",
        "PP_MEMORY_PROPOSALS": "on",
    }

    async def call_tool(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    await process_hook(_payload(repository), call_tool=call_tool, environ=environment)

    state = next(state_dir.glob("turn-*.json"))
    assert json.loads(state.read_text(encoding="utf-8"))["project_id"] == (
        "project:repo:github.com/example/project"
    )


@pytest.mark.asyncio
async def test_hook_turn_state_is_project_scoped(tmp_path):
    first = _git_repo(tmp_path / "project-a", "git@github.com:example/project-a.git")
    second = _git_repo(tmp_path / "project-b", "git@github.com:example/project-b.git")
    environment = {
        "PP_CODEX_HOOK_STATE_DIR": str(tmp_path / "hook-state"),
        "PP_PASSIVE_CONTEXT": "on",
        "PP_PASSIVE_MEMORY": "on",
        "PP_MEMORY_PROPOSALS": "on",
    }

    async def call_tool(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    for repository in (first, second):
        await process_hook(
            _payload(repository),
            call_tool=call_tool,
            environ=environment,
        )

    state_files = list((tmp_path / "hook-state").glob("turn-*.json"))
    assert len(state_files) == 2


def test_request_scope_is_project_scoped_when_project_id_is_explicit():
    first = build_request_scope(
        {
            "project_id": "project:a",
            "stage_session_id": "session:same",
            "flow_line_id": "flow:same",
            "request_id": "req:same",
        },
        "context_supply",
    )
    second = build_request_scope(
        {
            "project_id": "project:b",
            "stage_session_id": "session:same",
            "flow_line_id": "flow:same",
            "request_id": "req:same",
        },
        "context_supply",
    )

    assert first["request_scope_id"] != second["request_scope_id"]
