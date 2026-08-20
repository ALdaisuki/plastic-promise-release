from __future__ import annotations

import json
import stat
import time
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
        "PP_CODEX_HOOK_PROJECT_ID": "project:test",
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


def _continuation_for(session_id: str) -> str:
    return f"continuation-{session_id}-" + ("a" * 32)


def _continuation_expiry() -> int:
    return int(time.time()) + 900


def _continuation_result(session_id: str) -> dict:
    return {
        codex_hook._CONTINUATION_RESULT_KEY: _continuation_for(session_id),
        codex_hook._CONTINUATION_EXPIRES_RESULT_KEY: _continuation_expiry(),
    }


def _completed_session_end() -> dict:
    return {
        "event": "session_end",
        "status": "completed",
        "persistent": True,
        "durable_collaboration_lifecycle": {
            "state": "durable",
            "action": "session_end",
            "persistent": True,
            "receipt": {
                "schema_version": "durable-collaboration-session-end/v1",
                "state": "closed",
                "persistent": True,
            },
        },
    }


def _deferred_stop_activity() -> dict:
    return {
        "state": "deferred",
        "reason": "stop_activity_unavailable",
    }


def _completed_stop_activity() -> dict:
    return {
        "state": "durable",
        "action": "heartbeat",
        "persistent": True,
        "receipt": {
            "schema_version": "durable-collaboration-heartbeat/v1",
            "state": "active",
            "persistent": True,
            "stop_activity": {
                "schema_version": "durable-collaboration-stop-activity/v1",
                "state": "durable",
                "persistent": True,
                "events": [],
            },
        },
    }


@pytest.mark.parametrize(
    "value",
    [None, 123, "", "contains space", "contains\nnewline", "a" * 4097],
)
def test_continuation_token_rejects_non_string_whitespace_and_oversized_values(value):
    assert codex_hook._continuation_token(value) == ""


def test_registration_continuation_requires_bounded_client_secret_projection():
    hook_session_id = "hook-session:codex:" + ("a" * 40)
    token = _continuation_for("session-1")
    expiry = _continuation_expiry()
    payload = {
        "collaboration_continuation": {
            "schema_version": "durable-collaboration-continuation/v1",
            "token": token,
            "expires_at_epoch": expiry,
            "hook_session_id": hook_session_id,
            "storage": "client-secret-only",
        }
    }

    assert codex_hook._registration_continuation(
        payload,
        expected_hook_session_id=hook_session_id,
    ) == (token, expiry)

    for field, invalid in (
        ("schema_version", "unknown/v1"),
        ("token", "contains space"),
        ("expires_at_epoch", int(time.time()) - 1),
        ("expires_at_epoch", True),
        ("hook_session_id", "hook-session:codex:other"),
        ("storage", "server-visible"),
    ):
        invalid_payload = json.loads(json.dumps(payload))
        invalid_payload["collaboration_continuation"][field] = invalid
        assert codex_hook._registration_continuation(
            invalid_payload,
            expected_hook_session_id=hook_session_id,
        ) == ("", 0)


def test_session_state_is_scoped_to_one_hook_session_and_rejects_expiry(tmp_path):
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())
    token = _continuation_for("session-1")
    session_1_path = codex_hook._write_session_state(
        config,
        "session-1",
        token,
        _continuation_expiry(),
    )
    session_2_path = codex_hook._session_state_path(config, "session-2")
    session_2_path.write_bytes(session_1_path.read_bytes())

    _path, cross_session = codex_hook._read_session_state(config, "session-2")

    assert cross_session is None
    assert not session_2_path.exists()
    assert session_1_path.exists()

    state = json.loads(session_1_path.read_text(encoding="utf-8"))
    state["expires_at_epoch"] = int(time.time()) - 1
    session_1_path.write_text(json.dumps(state), encoding="utf-8")

    _path, expired = codex_hook._read_session_state(config, "session-1")

    assert expired is None
    assert not session_1_path.exists()


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
    continuation = _continuation_for("session-1")

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            observed["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client, terminate_on_close):
        observed["url"] = url
        observed["http_client"] = http_client
        observed["terminate_on_close"] = terminate_on_close
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
            observed.setdefault("tool_calls", []).append((tool_name, arguments))
            if tool_name == "session-init":
                payload = {
                    "success": True,
                    "project_id": "project:test",
                    "collaboration_continuation": {
                        "schema_version": "durable-collaboration-continuation/v1",
                        "token": continuation,
                        "expires_at_epoch": _continuation_expiry(),
                        "hook_session_id": arguments["hook_session_id"],
                        "storage": "client-secret-only",
                    },
                    "diagnostics": {
                        "task_session_binding": {"success": True},
                        "durable_collaboration_binding": {
                            "success": True,
                            "persistent": True,
                        },
                    },
                    "durable_collaboration": {
                        "schema_version": "durable-collaboration-session-init/v1",
                        "project_id": "project:test",
                        "persistent": True,
                    },
                }
            else:
                payload = {"status": "ok"}
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps(payload))],
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())

    hook_arguments = codex_hook._call_arguments(
        _prompt_payload(),
        config,
        event="before_invoke",
        user_text="task",
    )
    result = await codex_hook._call_mcp_tool("auto_context_inject", hook_arguments, config)

    assert result["status"] == "ok"
    assert result[codex_hook._REGISTERED_CALL_RESULT_KEY]["success"] is True
    assert observed["url"] == "http://127.0.0.1:9020/mcp"
    assert observed["client_options"]["trust_env"] is False
    assert observed["terminate_on_close"] is True
    assert [name for name, _arguments in observed["tool_calls"]] == [
        "session-init",
        "auto_context_inject",
    ]
    registration = observed["tool_calls"][0][1]
    assert registration["context_mode"] == "none"
    assert registration["project_id"] == "project:test"
    assert registration["hook_session_id"].startswith("hook-session:codex:")
    assert "collaboration_continuation_token" not in registration
    target_arguments = observed["tool_calls"][1][1]
    assert target_arguments["hook_session_id"] == registration["hook_session_id"]
    assert target_arguments["collaboration_continuation_token"] == continuation
    public_target_arguments = dict(target_arguments)
    public_target_arguments.pop("collaboration_continuation_token")
    assert continuation not in json.dumps(public_target_arguments)
    assert continuation not in target_arguments["task_description"]
    assert continuation not in target_arguments["user_text"]
    assert continuation not in target_arguments["assistant_text"]
    assert continuation not in json.dumps(target_arguments["metadata"])
    session_state = next(tmp_path.glob("session-*.json"))
    stored = json.loads(session_state.read_text(encoding="utf-8"))
    assert stored["collaboration_continuation"] == continuation
    assert stored["expires_at_epoch"] > int(time.time())
    assert stat.S_IMODE(session_state.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_hook_http_client_resumes_stored_continuation_on_fresh_transport(
    tmp_path,
    monkeypatch,
):
    import httpx
    import mcp
    from mcp.client import streamable_http

    calls: list[tuple[str, dict]] = []
    continuation = _continuation_for("session-1")
    expiry = _continuation_expiry()
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())
    codex_hook._write_session_state(config, "session-1", continuation, expiry)

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(_url, *, http_client, terminate_on_close):
        assert http_client is not None
        assert terminate_on_close is True
        yield ("reader", "writer")

    class FakeClientSession:
        def __init__(self, _reader, _writer):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool_name, arguments):
            calls.append((tool_name, arguments))
            payload = (
                {
                    "success": True,
                    "project_id": "project:test",
                    "diagnostics": {
                        "task_session_binding": {"success": True},
                        "durable_collaboration_binding": {
                            "success": True,
                            "persistent": True,
                        },
                    },
                    "durable_collaboration": {
                        "schema_version": "durable-collaboration-session-init/v1",
                        "project_id": "project:test",
                        "persistent": True,
                    },
                }
                if tool_name == "session-init"
                else {"status": "queued", "queued": True, "outbox_id": "outbox-1"}
            )
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps(payload))],
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    arguments = codex_hook._call_arguments(
        {
            **_prompt_payload(),
            "hook_event_name": "Stop",
        },
        config,
        event="after_invoke",
        user_text="task",
        assistant_text="done",
    )

    result = await codex_hook._call_mcp_tool("auto_context_inject", arguments, config)

    assert [name for name, _arguments in calls] == [
        "session-init",
        "auto_context_inject",
    ]
    registration = calls[0][1]
    target = calls[1][1]
    expected_hook_session_id = codex_hook._hook_session_id("project:test", "session-1")
    assert registration["hook_session_id"] == expected_hook_session_id
    assert registration["collaboration_continuation_token"] == continuation
    assert target["hook_session_id"] == expected_hook_session_id
    assert target["collaboration_continuation_token"] == continuation
    public_target = dict(target)
    public_target.pop("collaboration_continuation_token")
    assert continuation not in json.dumps(public_target)
    assert result[codex_hook._CONTINUATION_RESULT_KEY] == continuation
    assert result[codex_hook._CONTINUATION_EXPIRES_RESULT_KEY] == expiry
    assert "collaboration_continuation" not in result[codex_hook._REGISTERED_CALL_RESULT_KEY]
    stored_path = codex_hook._session_state_path(config, "session-1")
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    assert stored["collaboration_continuation"] == continuation
    assert stored["expires_at_epoch"] == expiry


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["after_invoke", "session_end"])
async def test_hook_http_client_defers_without_continuation_before_opening_transport(
    tmp_path,
    event,
):
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())
    arguments = codex_hook._call_arguments(
        _prompt_payload(),
        config,
        event=event,
        user_text="",
        assistant_text="",
    )

    result = await codex_hook._call_mcp_tool("auto_context_inject", arguments, config)

    assert result == {
        "status": "deferred",
        "reason": "collaboration_continuation_required",
        "persistent": False,
    }


@pytest.mark.asyncio
async def test_hook_http_client_fails_closed_when_session_registration_degrades(
    tmp_path,
    monkeypatch,
):
    import httpx
    import mcp
    from mcp.client import streamable_http

    calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    @asynccontextmanager
    async def fake_streamable_http_client(_url, *, http_client, terminate_on_close):
        assert http_client is not None
        assert terminate_on_close is True
        yield ("reader", "writer")

    class FakeClientSession:
        def __init__(self, _reader, _writer):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool_name, _arguments):
            calls.append(tool_name)
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        text=json.dumps(
                            {
                                "success": True,
                                "project_id": "project:test",
                                "degraded": True,
                                "diagnostics": {
                                    "task_session_binding": {"success": True},
                                    "durable_collaboration_binding": {
                                        "success": False,
                                        "persistent": False,
                                        "reason": "durable_collaboration_schema_missing",
                                    },
                                },
                            }
                        )
                    )
                ],
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeClientSession)
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), _prompt_payload())

    result = await codex_hook._call_mcp_tool(
        "auto_context_inject",
        codex_hook._call_arguments(
            _prompt_payload(),
            config,
            event="before_invoke",
            user_text="task",
        ),
        config,
    )

    assert calls == ["session-init"]
    assert result["status"] == "degraded"
    assert result["persistent"] is False
    assert result["reason"] == "durable_collaboration_session_init_failed"


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
async def test_user_prompt_submit_never_renders_internal_continuation(tmp_path):
    continuation = _continuation_for("session-1")

    async def call_tool(_tool_name, _arguments, _config):
        return {
            "status": "injected",
            "injection": "<relevant-memories>safe context</relevant-memories>",
            **_continuation_result("session-1"),
        }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert continuation not in json.dumps(output)
    assert output["hookSpecificOutput"]["additionalContext"] == (
        "<relevant-memories>safe context</relevant-memories>"
    )
    session_path = next(tmp_path.glob("session-*.json"))
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_user_prompt_submit_projects_bounded_durable_collaboration(tmp_path):
    registration = {
        "success": True,
        "project_id": "project:test",
        "diagnostics": {
            "task_session_binding": {"success": True},
            "durable_collaboration_binding": {"success": True, "persistent": True},
        },
        "durable_collaboration": {
            "schema_version": "durable-collaboration-session-init/v1",
            "project_id": "project:test",
            "persistent": True,
            "agent": {
                "agent_id": "agent:codex",
                "role": "participant",
                "transport_session_id": "must-not-project",
            },
            "working_set_summary": {"agents": {"active": 1}},
            "assigned_work": [
                {"work_item_id": f"work:{index}", "title": "bounded work"}
                for index in range(codex_hook._MAX_COLLABORATION_ITEMS + 4)
            ],
            "peer_delta": {"items": []},
            "cursor": {"stored_sequence": 0, "next_sequence": 0},
            "canonical_memory_effect": "none",
        },
    }

    async def call_tool(_tool_name, _arguments, _config):
        return {
            "status": "empty",
            "injection": "",
            codex_hook._REGISTERED_CALL_RESULT_KEY: registration,
            "durable_collaboration_lifecycle": {
                "state": "durable",
                "action": "heartbeat",
                "persistent": True,
                "receipt": {
                    "schema_version": "durable-collaboration-heartbeat/v1",
                    "persistent": True,
                    "assigned_work": registration["durable_collaboration"]["assigned_work"],
                    "working_set_summary": {"agents": {"active": 1}},
                    "peer_delta": {"items": []},
                    "cursor": {"stored_sequence": 0, "next_sequence": 0},
                    "canonical_memory_effect": "none",
                },
            },
        }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    injection = output["hookSpecificOutput"]["additionalContext"]
    assert injection.startswith("<plastic-promise-collaboration")
    assert "durable-collaboration-heartbeat/v1" in injection
    assert "authenticated-hook-session" in injection
    assert "canonical_memory_effect" in injection
    assert "transport_session_id" not in injection
    assert "work:19" in injection
    assert "work:20" not in injection
    assert len(injection) <= codex_hook._MAX_COLLABORATION_TEXT_CHARS + 128


@pytest.mark.asyncio
async def test_user_prompt_submit_drops_oversized_collaboration_projection(tmp_path):
    registration = {
        "success": True,
        "project_id": "project:test",
        "diagnostics": {
            "task_session_binding": {"success": True},
            "durable_collaboration_binding": {"success": True, "persistent": True},
        },
        "durable_collaboration": {
            "project_id": "project:test",
            "persistent": True,
            "assigned_work": [
                {"work_item_id": f"work:{index}", "title": "x" * 512}
                for index in range(codex_hook._MAX_COLLABORATION_ITEMS)
            ],
        },
    }

    async def call_tool(_tool_name, _arguments, _config):
        return {
            "status": "empty",
            "injection": "",
            codex_hook._REGISTERED_CALL_RESULT_KEY: registration,
        }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}


@pytest.mark.asyncio
async def test_user_prompt_submit_suppresses_unverified_or_cross_project_projection(tmp_path):
    async def call_tool(_tool_name, _arguments, _config):
        return {
            "status": "empty",
            "injection": "",
            codex_hook._REGISTERED_CALL_RESULT_KEY: {
                "success": True,
                "project_id": "project:other",
                "diagnostics": {
                    "task_session_binding": {"success": True},
                    "durable_collaboration_binding": {"success": True, "persistent": True},
                },
                "durable_collaboration": {
                    "project_id": "project:other",
                    "peer_delta": {"items": [{"summary": "foreign"}]},
                },
            },
        }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}


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
        return {
            "status": "queued",
            "queued": True,
            "outbox_id": "outbox-1",
            "durable_collaboration_lifecycle": _completed_stop_activity(),
        }

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
async def test_stop_deferred_typed_activity_replaces_prompt_with_bounded_retry_marker(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(_tool_name, arguments, _config):
        calls.append(("auto_context_inject", arguments))
        if arguments["event"] == "before_invoke":
            return {"status": "empty", "injection": ""}
        return {
            "status": "queued",
            "queued": True,
            "outbox_id": "outbox-stop-1",
            "durable_collaboration_lifecycle": _deferred_stop_activity(),
        }

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
            "last_assistant_message": "private assistant output",
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    state_path = next(tmp_path.glob("turn-*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "codex-hook-stop-retry-v1"
    assert state["stop_request_id"] == "turn-1"
    assert "prompt" not in state
    assert "assistant_text" not in state
    assert "private assistant output" not in state_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_capture_disabled_stop_retries_typed_activity_with_same_request_id(tmp_path):
    calls: list[dict] = []
    attempts = 0

    async def call_tool(_tool_name, arguments, _config):
        nonlocal attempts
        if arguments["event"] == "after_invoke":
            calls.append(arguments)
            attempts += 1
            lifecycle = _deferred_stop_activity() if attempts == 1 else _completed_stop_activity()
            return {
                "status": "empty",
                "injection": "",
                "durable_collaboration_lifecycle": lifecycle,
            }
        return {"status": "empty", "injection": ""}

    environment = {**_environment(tmp_path), "PP_PASSIVE_MEMORY": "off"}
    first = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "private assistant output",
        },
        call_tool=call_tool,
        environ=environment,
    )
    assert first == {"continue": True}
    retry_path = next(tmp_path.glob("turn-*.json"))
    retry_state = json.loads(retry_path.read_text(encoding="utf-8"))
    assert retry_state["schema_version"] == "codex-hook-stop-retry-v1"
    assert retry_state["stop_request_id"] == "turn-1"
    assert "prompt" not in retry_state
    assert "assistant_text" not in retry_state

    second = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "another private assistant output",
        },
        call_tool=call_tool,
        environ=environment,
    )
    assert second == {"continue": True}
    assert calls[0]["request_id"] == calls[1]["request_id"] == "turn-1"
    assert not list(tmp_path.glob("turn-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "queued", "queued": True, "outbox_id": None},
        {
            "status": "queued",
            "queued": True,
            "outbox_id": "outbox-1",
            "canonical_memory_effect": "adopted",
        },
        {"status": "semantic_queued", "semantic_job_id": None},
        {"status": "completed", "persistent": True},
    ],
)
async def test_stop_retains_turn_unless_capture_is_pending_only(tmp_path, result):
    async def preload(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    await process_hook(
        _prompt_payload(),
        call_tool=preload,
        environ=_environment(tmp_path),
    )

    async def capture(_tool_name, _arguments, _config):
        return result

    output = await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=capture,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    assert len(list(tmp_path.glob("turn-*.json"))) == 1


@pytest.mark.asyncio
async def test_stop_deletes_turn_for_pending_semantic_capture(tmp_path):
    async def preload(_tool_name, _arguments, _config):
        return {"status": "empty", "injection": ""}

    await process_hook(
        _prompt_payload(),
        call_tool=preload,
        environ=_environment(tmp_path),
    )

    async def semantic_capture(_tool_name, _arguments, _config):
        return {
            "status": "semantic_queued",
            "semantic_job_id": "derived-work-1",
            "canonical_memory_effect": "none",
            "durable_collaboration_lifecycle": _completed_stop_activity(),
        }

    await process_hook(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": "F:/Agent/Memory system",
            "hook_event_name": "Stop",
            "last_assistant_message": "Done",
        },
        call_tool=semantic_capture,
        environ=_environment(tmp_path),
    )

    assert not list(tmp_path.glob("turn-*.json"))


@pytest.mark.asyncio
async def test_session_end_removes_only_matching_session_state(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def preload(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        session_id = arguments["stage_session_id"]
        if arguments["event"] == "session_end":
            return {
                **_completed_session_end(),
                **_continuation_result(session_id),
            }
        return {
            "status": "empty",
            "injection": "",
            **_continuation_result(session_id),
        }

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
    assert calls[-1][0] == "auto_context_inject"
    assert calls[-1][1]["event"] == "session_end"
    assert calls[-1][1]["metadata"]["session_end_reason"] == "exit"
    remaining = [
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in tmp_path.glob("turn-*.json")
    ]
    assert remaining == ["session-2"]
    assert (
        codex_hook._session_state_path(
            codex_hook.HookConfig.from_environ(_environment(tmp_path), {}),
            "session-1",
        ).exists()
        is False
    )
    assert (
        codex_hook._session_state_path(
            codex_hook.HookConfig.from_environ(_environment(tmp_path), {}),
            "session-2",
        ).exists()
        is True
    )


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
    config = codex_hook.HookConfig.from_environ(_environment(tmp_path), {})
    codex_hook._write_session_state(
        config,
        "session-target",
        _continuation_for("session-target"),
        _continuation_expiry(),
    )

    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {
            **_completed_session_end(),
            **_continuation_result("session-target"),
        }

    output = await process_hook(
        {
            "session_id": "session-target",
            "hook_event_name": "SessionEnd",
        },
        call_tool=call_tool,
        environ=_environment(tmp_path),
    )

    assert output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["session_end"]
    assert not matching.exists()
    assert len(list(tmp_path.glob("turn-*.json"))) == codex_hook._MAX_STATE_FILES + 1
    assert not codex_hook._session_state_path(config, "session-target").exists()


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
async def test_all_passive_modes_off_still_heartbeats_without_turn_state(tmp_path):
    calls = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "skipped", "reason": "passive_context_disabled"}

    environment = {
        **_environment(tmp_path),
        "PP_PASSIVE_CONTEXT": "off",
        "PP_PASSIVE_MEMORY": "off",
        "PP_MEMORY_PROPOSALS": "off",
    }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=environment,
    )

    assert output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["before_invoke"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_invalid_passive_modes_keep_lifecycle_but_fail_closed_for_memory(tmp_path):
    calls = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {"status": "skipped", "reason": "passive_context_disabled"}

    environment = {
        **_environment(tmp_path),
        "PP_PASSIVE_CONTEXT": "invalid",
        "PP_PASSIVE_MEMORY": "invalid",
        "PP_MEMORY_PROPOSALS": "invalid",
    }

    output = await process_hook(
        _prompt_payload(),
        call_tool=call_tool,
        environ=environment,
    )

    assert output == {"continue": True}
    assert [arguments["event"] for _name, arguments in calls] == ["before_invoke"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_context_off_capture_on_keeps_turn_and_heartbeat_without_memory_injection(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {
            "status": "queued",
            "queued": True,
            "outbox_id": "outbox-1",
            "durable_collaboration_lifecycle": _completed_stop_activity(),
        }

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
    assert [arguments["event"] for _name, arguments in calls] == ["before_invoke"]
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
    assert [arguments["event"] for _name, arguments in calls] == [
        "before_invoke",
        "after_invoke",
    ]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disabled_gate",
    ["PP_PASSIVE_MEMORY", "PP_MEMORY_PROPOSALS"],
)
async def test_capture_gate_off_keeps_bounded_hook_heartbeats_without_turn_state(
    tmp_path,
    disabled_gate,
):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {
            "status": "empty",
            "injection": "",
            "durable_collaboration_lifecycle": _completed_stop_activity(),
        }

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
    assert [arguments["event"] for _name, arguments in calls] == [
        "before_invoke",
        "after_invoke",
    ]
    assert calls[-1][1]["user_text"] == ""
    assert calls[-1][1]["assistant_text"] == ""
    assert calls[-1][1]["metadata"]["passive_capture_enabled"] is False
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_gate", ["PP_PASSIVE_MEMORY", "PP_MEMORY_PROPOSALS"])
async def test_disabling_capture_before_stop_discards_existing_turn_state(tmp_path, disabled_gate):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        return {
            "status": "empty",
            "injection": "",
            "durable_collaboration_lifecycle": _completed_stop_activity(),
        }

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
    assert [arguments["event"] for _name, arguments in calls] == [
        "before_invoke",
        "after_invoke",
    ]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_session_end_cleans_existing_state_after_capture_is_disabled(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def call_tool(tool_name, arguments, _config):
        calls.append((tool_name, arguments))
        session_id = arguments["stage_session_id"]
        if arguments["event"] == "session_end":
            return {
                **_completed_session_end(),
                **_continuation_result(session_id),
            }
        return {
            "status": "empty",
            "injection": "",
            **_continuation_result(session_id),
        }

    for session_id, turn_id in (("session-1", "turn-1"), ("session-2", "turn-2")):
        await process_hook(
            _prompt_payload(session_id=session_id, turn_id=turn_id),
            call_tool=call_tool,
            environ=_environment(tmp_path),
        )
    assert len(list(tmp_path.glob("turn-*.json"))) == 2
    assert len(list(tmp_path.glob("session-*.json"))) == 2

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
    assert len(calls) == 3
    assert calls[-1][1]["event"] == "session_end"
    remaining = [
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in tmp_path.glob("turn-*.json")
    ]
    assert remaining == ["session-2"]
    assert len(list(tmp_path.glob("session-*.json"))) == 1


@pytest.mark.asyncio
async def test_session_end_failure_is_explicitly_deferred_and_retains_retry_state(tmp_path):
    async def preload(_tool_name, arguments, _config):
        return {
            "status": "empty",
            "injection": "",
            **_continuation_result(arguments["stage_session_id"]),
        }

    await process_hook(
        _prompt_payload(session_id="session-1", turn_id="turn-1"),
        call_tool=preload,
        environ=_environment(tmp_path),
    )

    async def unavailable(_tool_name, _arguments, _config):
        raise RuntimeError("server unavailable")

    output = await process_hook(
        {
            "session_id": "session-1",
            "hook_event_name": "SessionEnd",
        },
        call_tool=unavailable,
        environ=_environment(tmp_path),
    )

    assert output == {
        "continue": True,
        "systemMessage": "Plastic Promise session_end deferred; retry state retained.",
    }
    assert len(list(tmp_path.glob("turn-*.json"))) == 1
    assert len(list(tmp_path.glob("session-*.json"))) == 1


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
