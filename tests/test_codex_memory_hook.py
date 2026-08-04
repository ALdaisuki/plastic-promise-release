from __future__ import annotations

import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from plastic_promise.passive_memory import codex_hook
from plastic_promise.passive_memory.codex_hook import process_hook


def _environment(state_dir: Path) -> dict[str, str]:
    return {
        "PP_CODEX_HOOK_STATE_DIR": str(state_dir),
        "PP_CODEX_HOOK_MCP_URL": "http://127.0.0.1:9020/mcp",
        "PP_CODEX_HOOK_TIMEOUT_SEC": "1",
        "PP_PROJECT_ID": "project:test",
        "PP_PASSIVE_CONTEXT": "on",
        "PP_PASSIVE_MEMORY": "on",
        "PP_MEMORY_PROPOSALS": "on",
    }


def _prompt_payload(*, session_id: str = "session-1", turn_id: str = "turn-1") -> dict:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": "F:/Agent/Memory system",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Remember TypeScript and continue the task.",
        "model": "codex",
        "permission_mode": "default",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9020/mcp",
        "https://127.0.0.2:9443/custom/mcp?transport=streamable",
        "http://localhost:19020/mcp/v1",
        "HTTPS://LOCALHOST:9443/custom/mcp",
        "http://[::1]:9020/mcp",
    ],
)
def test_hook_mcp_url_accepts_only_supported_loopback_forms(tmp_path, url):
    environment = {**_environment(tmp_path), "PP_CODEX_HOOK_MCP_URL": url}

    config = codex_hook.HookConfig.from_environ(environment, _prompt_payload())

    assert config.mcp_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.test/mcp",
        "http://localhost.example.test/mcp",
        "http://0.0.0.0:9020/mcp",
        "http://2130706433:9020/mcp",
        "ftp://127.0.0.1:9020/mcp",
        "http://user@localhost:9020/mcp",
        "http://localhost:not-a-port/mcp",
        "http://[::1%25lo0]:9020/mcp",
        "//127.0.0.1:9020/mcp",
        "http://127.0.0.1:9020/mcp\nhttps://mcp.example.test",
    ],
)
def test_hook_mcp_url_rejects_non_loopback_or_ambiguous_targets(tmp_path, url):
    environment = {**_environment(tmp_path), "PP_CODEX_HOOK_MCP_URL": url}

    config = codex_hook.HookConfig.from_environ(environment, _prompt_payload())

    assert config.mcp_url == codex_hook._DEFAULT_MCP_URL


@pytest.mark.asyncio
async def test_hook_http_client_ignores_proxy_environment(tmp_path, monkeypatch):
    import httpx
    import mcp
    from mcp.client import streamable_http

    observed: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            observed["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client):
        observed["url"] = url
        observed["http_client"] = http_client
        yield ("reader", "writer")

    class FakeClientSession:
        def __init__(self, reader, writer):
            observed["streams"] = (reader, writer)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def initialize(self):
            observed["initialized"] = True

        async def call_tool(self, tool_name, arguments):
            observed["tool_call"] = (tool_name, arguments)
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text='{"status":"ok"}')],
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())

    result = await codex_hook._call_mcp_tool("tool", {"value": 1}, config)

    assert result == {"status": "ok"}
    assert observed["url"] == "http://127.0.0.1:9020/mcp"
    assert observed["client_options"]["trust_env"] is False


@pytest.mark.asyncio
async def test_user_prompt_submit_injects_context_and_persists_turn(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {
            "status": "injected",
            "injection": "<relevant-memories>TypeScript</relevant-memories>",
        }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "<relevant-memories>TypeScript</relevant-memories>",
        },
    }
    assert calls[0][0] == "auto_context_inject"
    assert calls[0][1]["event"] == "before_invoke"
    assert calls[0][1]["user_text"] == _prompt_payload()["prompt"]
    assert calls[0][1]["stage_session_id"] == "session-1"
    assert calls[0][1]["request_id"] == "turn-1"
    assert calls[0][1]["project_id"] == "project:test"
    state_files = list(tmp_path.glob("*.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["prompt"] == _prompt_payload()["prompt"]


@pytest.mark.asyncio
async def test_value_before_password_label_is_redacted_before_state_and_mcp(tmp_path):
    calls: list[tuple[str, dict]] = []
    synthetic_secret = "Qzrm&)4816kappa"
    payload = _prompt_payload()
    payload["prompt"] = f"{synthetic_secret}这个密码试试"

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    await process_hook(payload, call_tool=call_tool, environ=_environment(tmp_path))

    assert calls[0][1]["user_text"] == "[REDACTED]"
    assert synthetic_secret not in json.dumps(calls[0][1])
    state_files = list(tmp_path.glob("*.json"))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text(encoding="utf-8"))["prompt"] == "[REDACTED]"
    assert synthetic_secret.encode() not in state_files[0].read_bytes()


@pytest.mark.asyncio
async def test_unlabelled_multiline_password_is_redacted_before_state_and_mcp(tmp_path):
    calls: list[tuple[str, dict]] = []
    synthetic_secret = "Qzrm&)4816kappa"
    payload = _prompt_payload()
    payload["prompt"] = (
        f"i-example-instance\n203.0.113.42\n{synthetic_secret}\nPlease continue the deployment."
    )

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    await process_hook(payload, call_tool=call_tool, environ=_environment(tmp_path))

    assert calls[0][1]["user_text"] == "[REDACTED]"
    assert synthetic_secret not in json.dumps(calls[0][1])
    state_files = list(tmp_path.glob("turn-*.json"))
    assert len(state_files) == 1
    assert json.loads(state_files[0].read_text(encoding="utf-8"))["prompt"] == "[REDACTED]"
    assert synthetic_secret.encode() not in state_files[0].read_bytes()


@pytest.mark.asyncio
async def test_stop_reuses_original_prompt_and_deletes_completed_turn(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        if arguments["event"] == "before_invoke":
            return {"status": "empty", "injection": ""}
        return {"status": "queued", "queued": True, "outbox_id": "outbox-1"}

    await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )
    output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "TypeScript configuration is complete.",
            "stop_hook_active": False,
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    assert calls[-1][1]["event"] == "after_invoke"
    assert calls[-1][1]["user_text"] == _prompt_payload()["prompt"]
    assert calls[-1][1]["assistant_text"] == "TypeScript configuration is complete."
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_hook_round_trip_preserves_user_prompt_whitespace(tmp_path):
    calls: list[tuple[str, dict]] = []
    prompt = "  Preserve  user-authored\nspacing exactly.  \n"
    payload = _prompt_payload()
    payload["prompt"] = prompt

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    await process_hook(payload, call_tool=call_tool, environ=_environment(tmp_path))
    await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert calls[0][1]["user_text"] == prompt
    assert calls[1][1]["user_text"] == prompt


@pytest.mark.asyncio
async def test_stop_reports_only_temporary_proposal_ids_saved_by_preload(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        if arguments["event"] == "before_invoke":
            return {
                "status": "injected",
                "injection": "<temporary-memory-proposals />",
                "temporary_proposal_ids": ["proposal-a", "proposal-a", "proposal-b"],
            }
        return {"status": "queued", "queued": True, "outbox_id": "outbox-1"}

    await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )
    state_path = next(tmp_path.glob("turn-*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["temporary_proposal_ids"] == ["proposal-a", "proposal-b"]
    assert "temporary-memory-proposals" not in state_path.read_text(encoding="utf-8")

    await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert calls[-1][1]["metadata"]["exposed_temporary_proposal_ids"] == [
        "proposal-a",
        "proposal-b",
    ]


@pytest.mark.asyncio
async def test_stop_redacts_secret_from_assistant_text_before_mcp_call(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        if arguments["event"] == "before_invoke":
            return {"status": "empty", "injection": ""}
        return {"status": "queued", "queued": True, "outbox_id": "outbox-1"}

    await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )
    synthetic_secret = "sk-" + "synthetic-assistant-credential-123456"
    await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": f"Provider accepted {synthetic_secret}",
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert calls[-1][1]["assistant_text"] == "[REDACTED]"
    assert synthetic_secret not in json.dumps(calls[-1][1])


@pytest.mark.asyncio
async def test_stop_failure_is_fail_open_and_retains_turn_for_retry(tmp_path):
    async def preload(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    await process_hook(
        _prompt_payload(),
        call_tool=preload,
        environ=_environment(tmp_path),
    )

    async def unavailable(_tool_name, _arguments, _config):
        raise RuntimeError("server unavailable")

    output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Result",
        },
        call_tool=unavailable,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_session_end_removes_only_matching_session_state(tmp_path):
    async def preload(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    await process_hook(
        _prompt_payload(session_id="session-1", turn_id="turn-1"),
        call_tool=preload,
        environ=_environment(tmp_path),
    )
    await process_hook(
        _prompt_payload(session_id="session-2", turn_id="turn-2"),
        call_tool=preload,
        environ=_environment(tmp_path),
    )
    output = await process_hook(
        {
            "session_id": "session-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "SessionEnd",
            "reason": "exit",
        },
        call_tool=preload,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    remaining = [
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in tmp_path.glob("*.json")
    ]
    assert remaining == ["session-2"]


@pytest.mark.asyncio
async def test_session_end_scans_past_periodic_cleanup_limit(tmp_path):
    created_at = codex_hook.time.time()
    for index in range(codex_hook._MAX_STATE_FILES + 1):
        path = tmp_path / f"turn-unrelated-{index:04d}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": f"other-{index}",
                    "turn_id": f"turn-{index}",
                    "created_at": created_at,
                }
            ),
            encoding="utf-8",
        )
    matching = tmp_path / "turn-matching-session.json"
    matching.write_text(
        json.dumps(
            {
                "session_id": "session-target",
                "turn_id": "turn-target",
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )

    output = await process_hook(
        {
            "session_id": "session-target",
            "hook_event_name": "SessionEnd",
        },
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    assert not matching.exists()
    assert len(list(tmp_path.glob("turn-*.json"))) == codex_hook._MAX_STATE_FILES + 1


def test_standalone_cleanup_command_is_bounded_and_reports_remaining_work(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(codex_hook, "_MAX_STATE_FILES", 3)
    monkeypatch.setenv("PP_CODEX_HOOK_STATE_DIR", str(tmp_path))
    for index in range(4):
        (tmp_path / f"turn-expired-{index}.json").write_text("{}", encoding="utf-8")

    assert codex_hook.main(["--cleanup-states"]) == 0
    first = json.loads(capsys.readouterr().out)

    assert first == {
        "status": "ok",
        "operation": "cleanup_states",
        "scanned": 3,
        "removed": 3,
        "has_more": True,
    }
    assert len(list(tmp_path.glob("turn-*.json"))) == 1

    assert codex_hook.main(["--cleanup-states"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert second == {
        "status": "ok",
        "operation": "cleanup_states",
        "scanned": 1,
        "removed": 1,
        "has_more": False,
    }
    assert not list(tmp_path.glob("turn-*.json"))


def test_bounded_cleanup_cursor_prevents_expired_file_starvation(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    environment = {
        **_environment(tmp_path),
        "PP_CODEX_HOOK_STATE_TTL_SEC": "60",
    }
    config = codex_hook.HookConfig.from_environ(environment, {})
    monkeypatch.setattr(codex_hook.time, "time", lambda: now)
    for name in ("turn-a-fresh.json", "turn-b-fresh.json"):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "session_id": name,
                    "turn_id": name,
                    "created_at": now,
                }
            ),
            encoding="utf-8",
        )
    expired = tmp_path / "turn-z-expired.json"
    expired.write_text(
        json.dumps(
            {
                "session_id": "expired",
                "turn_id": "expired",
                "created_at": now - 120,
            }
        ),
        encoding="utf-8",
    )

    first = codex_hook._cleanup_states(config, max_files=2)
    second = codex_hook._cleanup_states(config, max_files=2)

    assert first == {"scanned": 2, "removed": 0, "has_more": True}
    assert second["removed"] == 1
    assert not expired.exists()


def test_cleanup_cli_relative_state_dir_is_anchored_to_project(tmp_path, monkeypatch):
    scheduler_cwd = tmp_path / "scheduler"
    scheduler_cwd.mkdir()
    monkeypatch.chdir(scheduler_cwd)

    config = codex_hook.HookConfig.from_environ(
        {"PP_CODEX_HOOK_STATE_DIR": "var/codex-hooks"},
        {},
    )

    assert config.state_dir == codex_hook._project_root() / "var" / "codex-hooks"


@pytest.mark.asyncio
async def test_unknown_hook_event_never_calls_mcp(tmp_path):
    async def unexpected(_tool_name, _arguments, _config):
        raise AssertionError("MCP must not be called")

    output = await process_hook(
        {"hook_event_name": "Notification", "cwd": "F:/Agent/Memory system"},
        call_tool=unexpected,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}


@pytest.mark.asyncio
async def test_all_passive_modes_off_make_no_mcp_call_or_turn_state(tmp_path):
    calls = []

    async def unexpected(*args):
        calls.append(args)
        raise AssertionError("MCP must not be called")

    environment = {
        **_environment(tmp_path),
        "PP_PASSIVE_CONTEXT": "off",
        "PP_PASSIVE_MEMORY": "off",
        "PP_MEMORY_PROPOSALS": "off",
    }

    output = await process_hook(
        _prompt_payload(),
        call_tool=unexpected,
        environ=environment,
    )

    assert output == {"continue": True}
    assert calls == []
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_invalid_passive_modes_fail_closed_without_mcp_or_turn_state(tmp_path):
    calls = []

    async def unexpected(*args):
        calls.append(args)
        raise AssertionError("MCP must not be called")

    environment = {
        **_environment(tmp_path),
        "PP_PASSIVE_CONTEXT": "invalid",
        "PP_PASSIVE_MEMORY": "invalid",
        "PP_MEMORY_PROPOSALS": "invalid",
    }

    output = await process_hook(
        _prompt_payload(),
        call_tool=unexpected,
        environ=environment,
    )

    assert output == {"continue": True}
    assert calls == []
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_context_off_capture_on_keeps_turn_without_preload_call(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "queued", "queued": True, "outbox_id": "outbox-1"}

    environment = {
        **_environment(tmp_path),
        "PP_PASSIVE_CONTEXT": "off",
    }

    prompt_output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=environment,
    )
    assert prompt_output == {"continue": True}
    assert calls == []
    assert len(list(tmp_path.glob("*.json"))) == 1

    stop_output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=call_tool,
        environ=environment,
    )

    assert stop_output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["after_invoke"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disabled_gate",
    ["PP_PASSIVE_MEMORY", "PP_MEMORY_PROPOSALS"],
)
async def test_capture_gate_off_makes_stop_noop_without_turn_state(tmp_path, disabled_gate):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    environment = {
        **_environment(tmp_path),
        disabled_gate: "off",
    }
    await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=environment,
    )
    stop_output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=call_tool,
        environ=environment,
    )

    assert stop_output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["before_invoke"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_gate", ["PP_PASSIVE_MEMORY", "PP_MEMORY_PROPOSALS"])
async def test_disabling_capture_before_stop_discards_existing_turn_state(tmp_path, disabled_gate):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )
    assert len(list(tmp_path.glob("*.json"))) == 1

    environment = {**_environment(tmp_path), disabled_gate: "off"}
    output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=call_tool,
        environ=environment,
    )

    assert output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["before_invoke"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_session_end_cleans_existing_state_after_capture_is_disabled(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "empty", "injection": ""}

    for session_id, turn_id in (("session-1", "turn-1"), ("session-2", "turn-2")):
        await process_hook(
            _prompt_payload(session_id=session_id, turn_id=turn_id),
            call_tool=call_tool,
            environ=_environment(tmp_path),
        )
    assert len(list(tmp_path.glob("*.json"))) == 2

    environment = {**_environment(tmp_path), "PP_PASSIVE_MEMORY": "off"}
    output = await process_hook(
        {
            "session_id": "session-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "SessionEnd",
        },
        call_tool=call_tool,
        environ=environment,
    )

    assert output == {"continue": True}
    assert len(calls) == 2
    remaining = [
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in tmp_path.glob("*.json")
    ]
    assert remaining == ["session-2"]


def test_failed_atomic_state_replace_removes_temporary_prompt(tmp_path, monkeypatch):
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())

    def fail_replace(_path, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        codex_hook._write_turn_state(config, _prompt_payload(), _prompt_payload()["prompt"])

    assert not list(tmp_path.glob("*.tmp"))


def test_secret_prompt_is_redacted_before_temporary_state_write(tmp_path, monkeypatch):
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())
    secret = "sk-" + "synthetic-credential-material-123456"
    observed_temporary: list[bytes] = []
    original_replace = Path.replace

    def inspect_replace(source, target):
        observed_temporary.append(source.read_bytes())
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", inspect_replace)

    path = codex_hook._write_turn_state(
        config,
        _prompt_payload(),
        f"Configure the provider with api_key={secret}",
    )

    assert path is not None
    assert observed_temporary
    secret_bytes = secret.encode("utf-8")
    assert all(secret_bytes not in content for content in observed_temporary)
    assert secret_bytes not in path.read_bytes()
    assert json.loads(path.read_text(encoding="utf-8"))["prompt"] == "[REDACTED]"


def test_atomic_turn_state_has_owner_only_permissions(tmp_path, monkeypatch):
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())
    creation_modes: list[int] = []
    original_mkstemp = codex_hook.tempfile.mkstemp

    def inspect_mkstemp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        creation_modes.append(stat.S_IMODE(Path(name).stat().st_mode))
        return descriptor, name

    monkeypatch.setattr(codex_hook.tempfile, "mkstemp", inspect_mkstemp)

    path = codex_hook._write_turn_state(
        config,
        _prompt_payload(),
        _prompt_payload()["prompt"],
    )

    assert path is not None
    assert creation_modes == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_turn_state_directory_is_owner_only(tmp_path):
    state_dir = tmp_path / "shared-state"
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)
    config = codex_hook.HookConfig.from_environ(
        _environment(state_dir),
        _prompt_payload(),
    )

    path = codex_hook._write_turn_state(
        config,
        _prompt_payload(),
        _prompt_payload()["prompt"],
    )

    assert path is not None
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_project_hooks_register_passive_memory_lifecycle():
    hook_path = Path(__file__).parents[1] / ".codex" / "hooks.json"
    if not hook_path.exists():
        pytest.skip("project hook registration is excluded from the standard release variant")
    payload = json.loads(hook_path.read_text(encoding="utf-8"))

    assert set(payload["hooks"]) == {"UserPromptSubmit", "Stop", "SessionEnd"}
    for event in payload["hooks"].values():
        hook = event[0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["command"].startswith(".venv/bin/python ")
        assert hook["commandWindows"].startswith(".venv\\Scripts\\python.exe ")
        assert "plastic_promise.passive_memory.codex_hook" in hook["command"]
        assert "plastic_promise.passive_memory.codex_hook" in hook["commandWindows"]
