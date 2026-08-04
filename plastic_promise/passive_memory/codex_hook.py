"""Codex hook bridge for passive context preload and governed memory capture."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from plastic_promise.core.memory_proposals import contains_secret
from plastic_promise.core.project_context import infer_repository_project_id

ToolCaller = Callable[[str, dict[str, Any], "HookConfig"], Awaitable[dict[str, Any]]]

_HOOK_EVENTS = {
    "userpromptsubmit": "UserPromptSubmit",
    "stop": "Stop",
    "sessionend": "SessionEnd",
}
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_STATE_FILES = 1000
_DEFAULT_MCP_URL = "http://127.0.0.1:9020/mcp"
_CLEANUP_STATES_ARGUMENT = "--cleanup-states"
_CLEANUP_CURSOR_NAME = ".cleanup-cursor"
_REDACTED_PROMPT = "[REDACTED]"
_MAX_TEMPORARY_PROPOSAL_IDS = 8


def _text(value: object) -> str:
    return str(value or "").strip()


def _raw_text(value: object) -> str:
    return "" if value is None else str(value)


def _truthy(value: object) -> bool:
    return _text(value).casefold() in {"1", "true", "yes", "on"}


def _bounded_int(
    value: object,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(
    value: object,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _first_text(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = _text(environ.get(name))
        if value:
            return value
    return ""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _loopback_mcp_url(value: object) -> str:
    candidate = _text(value) or _DEFAULT_MCP_URL
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        return _DEFAULT_MCP_URL
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return _DEFAULT_MCP_URL
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port < 1)
        or "%" in hostname
    ):
        return _DEFAULT_MCP_URL
    if hostname.casefold() == "localhost":
        return candidate
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return _DEFAULT_MCP_URL
    return candidate if address.is_loopback else _DEFAULT_MCP_URL


def _passive_mode(environ: Mapping[str, str], name: str) -> str:
    mode = _text(environ.get(name)).casefold() or "off"
    return mode if mode in {"off", "shadow", "on"} else "off"


def _hook_modes(environ: Mapping[str, str]) -> tuple[str, str, str]:
    return (
        _passive_mode(environ, "PP_PASSIVE_CONTEXT"),
        _passive_mode(environ, "PP_PASSIVE_MEMORY"),
        _passive_mode(environ, "PP_MEMORY_PROPOSALS"),
    )


@dataclass(frozen=True)
class HookConfig:
    mcp_url: str
    timeout_seconds: float
    state_dir: Path
    state_ttl_seconds: int
    max_text_chars: int
    project_id: str
    project_policy: str
    task_type: str
    bearer_token: str
    verbose: bool

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> HookConfig:
        payload_cwd = _text(payload.get("cwd"))
        cwd = Path(payload_cwd).expanduser() if payload_cwd else _project_root()
        configured_state_dir = _first_text(environ, "PP_CODEX_HOOK_STATE_DIR")
        state_dir = (
            Path(configured_state_dir).expanduser()
            if configured_state_dir
            else cwd / "var" / "codex-hooks"
        )
        if not state_dir.is_absolute():
            state_dir = cwd / state_dir
        configured_mcp_url = _first_text(
            environ,
            "PP_CODEX_HOOK_MCP_URL",
            "PP_MCP_URL",
        )
        return cls(
            mcp_url=_loopback_mcp_url(configured_mcp_url),
            timeout_seconds=_bounded_float(
                environ.get("PP_CODEX_HOOK_TIMEOUT_SEC"),
                4.0,
                minimum=0.5,
                maximum=30.0,
            ),
            state_dir=state_dir,
            state_ttl_seconds=_bounded_int(
                environ.get("PP_CODEX_HOOK_STATE_TTL_SEC"),
                21600,
                minimum=60,
                maximum=604800,
            ),
            max_text_chars=_bounded_int(
                environ.get("PP_CODEX_HOOK_MAX_TEXT_CHARS"),
                65536,
                minimum=1000,
                maximum=262144,
            ),
            project_id=(
                _first_text(
                    environ,
                    "PP_CODEX_HOOK_PROJECT_ID",
                    "PP_PROJECT_ID",
                    "PLASTIC_PROJECT_ID",
                )
                or infer_repository_project_id(cwd)
            ),
            project_policy=_first_text(environ, "PP_CODEX_HOOK_PROJECT_POLICY") or "balanced",
            task_type=_first_text(environ, "PP_CODEX_HOOK_TASK_TYPE") or "general",
            bearer_token=_first_text(
                environ,
                "PP_CODEX_HOOK_BEARER_TOKEN",
                "PP_MCP_BEARER_TOKEN",
            ),
            verbose=_truthy(environ.get("PP_CODEX_HOOK_VERBOSE")),
        )


def _event_name(payload: Mapping[str, Any]) -> str:
    return _HOOK_EVENTS.get(_text(payload.get("hook_event_name")).casefold(), "")


def _identity(payload: Mapping[str, Any], event_name: str) -> tuple[str, str, str]:
    session_id = _text(payload.get("session_id"))
    turn_id = _text(payload.get("turn_id"))
    stable = "\x1f".join((session_id, turn_id, event_name))
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return session_id, turn_id, digest


def _state_path(config: HookConfig, payload: Mapping[str, Any]) -> Path | None:
    session_id, turn_id, digest = _identity(payload, "turn")
    if not session_id or not turn_id:
        return None
    return config.state_dir / f"turn-{digest}.json"


def _ensure_state_dir(config: HookConfig, *, create: bool) -> bool:
    path = config.state_dir
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_dir():
        raise OSError("hook_state_directory_unsafe")
    metadata = path.stat()
    if hasattr(os, "getuid"):
        if metadata.st_uid != os.getuid():
            raise OSError("hook_state_directory_owner_invalid")
        if metadata.st_mode & 0o077:
            os.chmod(path, 0o700)
    return True


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _safe_prompt(prompt: str) -> str:
    return _REDACTED_PROMPT if contains_secret(prompt) else prompt


def _temporary_proposal_ids(value: Mapping[str, Any]) -> list[str]:
    raw = value.get("temporary_proposal_ids")
    if not isinstance(raw, (list, tuple)):
        return []
    proposal_ids: list[str] = []
    for item in raw:
        proposal_id = _text(item)
        if not proposal_id or len(proposal_id) > 256 or proposal_id in proposal_ids:
            continue
        proposal_ids.append(proposal_id)
        if len(proposal_ids) >= _MAX_TEMPORARY_PROPOSAL_IDS:
            break
    return proposal_ids


def _write_turn_state(
    config: HookConfig,
    payload: Mapping[str, Any],
    prompt: str,
    *,
    temporary_proposal_ids: Sequence[str] = (),
) -> Path | None:
    path = _state_path(config, payload)
    if path is None:
        return None
    session_id, turn_id, _digest = _identity(payload, "turn")
    _ensure_state_dir(config, create=True)
    state = {
        "schema_version": "codex-hook-turn-v2",
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": _text(payload.get("cwd")),
        "prompt": _safe_prompt(prompt)[: config.max_text_chars],
        "temporary_proposal_ids": _temporary_proposal_ids(
            {"temporary_proposal_ids": temporary_proposal_ids}
        ),
        "created_at": time.time(),
    }
    content = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=config.state_dir,
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if temporary is not None:
            _unlink(temporary)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _read_turn_state(
    config: HookConfig,
    payload: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None]:
    path = _state_path(config, payload)
    if path is None or not path.is_file():
        return path, None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("turn_state_not_object")
        created_at = float(state.get("created_at") or 0.0)
        if not math.isfinite(created_at) or created_at <= 0:
            raise ValueError("turn_state_invalid_created_at")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _unlink(path)
        return path, None
    session_id, turn_id, _digest = _identity(payload, "turn")
    if state.get("session_id") != session_id or state.get("turn_id") != turn_id:
        _unlink(path)
        return path, None
    now = time.time()
    if created_at > now + config.state_ttl_seconds or now - created_at > config.state_ttl_seconds:
        _unlink(path)
        return path, None
    return path, state


def _cleanup_states(
    config: HookConfig,
    *,
    session_id: str = "",
    max_files: int | None = _MAX_STATE_FILES,
) -> dict[str, int | bool]:
    stats: dict[str, int | bool] = {
        "scanned": 0,
        "removed": 0,
        "has_more": False,
    }
    if not _ensure_state_dir(config, create=False):
        return stats
    now = time.time()
    try:
        paths = sorted(config.state_dir.glob("turn-*.json"), key=lambda path: path.name)
        cursor = _read_cleanup_cursor(config)
        if cursor and paths:
            split_at = next(
                (index for index, path in enumerate(paths) if path.name > cursor),
                0,
            )
            paths = paths[split_at:] + paths[:split_at]
        selected = paths if max_files is None else paths[:max_files]
        for path in selected:
            stats["scanned"] += 1
            remove = False
            observed = _path_identity(path)
            try:
                if path.is_symlink():
                    raise ValueError("turn_state_symlink")
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("turn_state_not_object")
                created_at = float(state.get("created_at") or 0.0)
                if not math.isfinite(created_at) or created_at <= 0:
                    raise ValueError("turn_state_invalid_created_at")
                matches_session = bool(session_id and state.get("session_id") == session_id)
                expired = (
                    created_at > now + config.state_ttl_seconds
                    or now - created_at > config.state_ttl_seconds
                )
                remove = matches_session or expired
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                remove = True
            if remove and _unlink_if_unchanged(path, observed):
                stats["removed"] += 1
        if max_files is not None and len(paths) > len(selected):
            stats["has_more"] = True
            if selected:
                try:
                    _write_cleanup_cursor(config, selected[-1].name)
                except OSError as exc:
                    raise RuntimeError("hook_cleanup_cursor_write_failed") from exc
        else:
            _unlink(config.state_dir / _CLEANUP_CURSOR_NAME)
    except OSError:
        pass
    return stats


def _path_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _unlink_if_unchanged(
    path: Path,
    observed: tuple[int, int, int, int] | None,
) -> bool:
    if observed is None or _path_identity(path) != observed:
        return False
    return _unlink(path)


def _read_cleanup_cursor(config: HookConfig) -> str:
    path = config.state_dir / _CLEANUP_CURSOR_NAME
    if not path.exists() and not path.is_symlink():
        return ""
    if path.is_symlink() or not path.is_file():
        _unlink(path)
        return ""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if (
        not value.startswith("turn-")
        or not value.endswith(".json")
        or len(value) > 255
        or Path(value).name != value
    ):
        _unlink(path)
        return ""
    return value


def _write_cleanup_cursor(config: HookConfig, value: str) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{_CLEANUP_CURSOR_NAME}.",
            suffix=".tmp",
            dir=config.state_dir,
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(config.state_dir / _CLEANUP_CURSOR_NAME)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            _unlink(temporary)


def _call_arguments(
    payload: Mapping[str, Any],
    config: HookConfig,
    *,
    event: str,
    user_text: str,
    assistant_text: str = "",
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session_id, turn_id, digest = _identity(payload, event)
    metadata = {
        key: value
        for key, value in {
            "hook_event_name": _event_name(payload),
            "cwd": _text(payload.get("cwd")),
            "model": _text(payload.get("model")),
            "permission_mode": _text(payload.get("permission_mode")),
        }.items()
        if value
    }
    if extra_metadata:
        metadata.update(dict(extra_metadata))
    arguments: dict[str, Any] = {
        "event": event,
        "task_description": user_text or f"Codex {event}",
        "task_type": config.task_type,
        "source": "codex_hook",
        "user_text": user_text,
        "assistant_text": assistant_text,
        "call_id": f"codex-hook:{digest[:24]}",
        "request_id": turn_id or digest[:24],
        "stage_session_id": session_id or f"codex:{digest[:16]}",
        "flow_line_id": "codex",
        "project_policy": config.project_policy,
        "visibility": "project",
        "metadata": metadata,
    }
    if config.project_id:
        arguments["project_id"] = config.project_id
    return arguments


def _parse_tool_result(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError("mcp_tool_error")
    for item in list(getattr(result, "content", None) or []):
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("mcp_tool_non_json")


async def _call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    config: HookConfig,
) -> dict[str, Any]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {}
    if config.bearer_token:
        headers["Authorization"] = f"Bearer {config.bearer_token}"
    timeout = httpx.Timeout(config.timeout_seconds, read=config.timeout_seconds)
    async with (
        httpx.AsyncClient(timeout=timeout, headers=headers, trust_env=False) as client,
        streamable_http_client(config.mcp_url, http_client=client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool_name, arguments)
        return _parse_tool_result(result)


def _continue_output(config: HookConfig, operation: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {"continue": True}
    if config.verbose and operation:
        output["systemMessage"] = (
            f"Plastic Promise {operation} unavailable; Codex continued without blocking."
        )
    return output


def _injection_text(payload: Mapping[str, Any]) -> str:
    direct = _text(payload.get("injection"))
    if direct:
        return direct
    data = payload.get("data")
    return _text(data.get("injection")) if isinstance(data, dict) else ""


def _capture_completed(payload: Mapping[str, Any]) -> bool:
    status = _text(payload.get("status")).casefold()
    return not payload.get("error") and status not in {"degraded", "rejected", "error"}


async def process_hook(
    payload: Mapping[str, Any],
    *,
    call_tool: ToolCaller = _call_mcp_tool,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = dict(payload or {})
    environment = os.environ if environ is None else environ
    config = HookConfig.from_environ(environment, values)
    event_name = _event_name(values)
    context_mode, memory_mode, proposal_mode = _hook_modes(environment)
    context_enabled = context_mode != "off"
    capture_enabled = memory_mode != "off" and proposal_mode != "off"

    if event_name == "UserPromptSubmit":
        prompt = _safe_prompt(_raw_text(values.get("prompt")))[: config.max_text_chars]
        if not prompt.strip():
            return _continue_output(config)
        if capture_enabled:
            _cleanup_states(config)
            with suppress(OSError):
                _write_turn_state(config, values, prompt)
        if not context_enabled:
            return _continue_output(config)
        arguments = _call_arguments(
            values,
            config,
            event="before_invoke",
            user_text=prompt,
        )
        try:
            result = await asyncio.wait_for(
                call_tool("auto_context_inject", arguments, config),
                timeout=config.timeout_seconds + 0.5,
            )
        except Exception:
            return _continue_output(config, "preload")
        temporary_proposal_ids = _temporary_proposal_ids(result)
        if capture_enabled and temporary_proposal_ids:
            with suppress(OSError):
                _write_turn_state(
                    config,
                    values,
                    prompt,
                    temporary_proposal_ids=temporary_proposal_ids,
                )
        injection = _injection_text(result)
        if not injection:
            return _continue_output(config)
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": injection,
            },
        }

    if event_name == "Stop":
        if not capture_enabled:
            path = _state_path(config, values)
            if path is not None:
                _unlink(path)
            return _continue_output(config)
        _cleanup_states(config)
        path, state = _read_turn_state(config, values)
        if state is None:
            return _continue_output(config)
        user_text = _raw_text(state.get("prompt"))[: config.max_text_chars]
        assistant_text = _safe_prompt(_text(values.get("last_assistant_message")))[
            : config.max_text_chars
        ]
        arguments = _call_arguments(
            values,
            config,
            event="after_invoke",
            user_text=user_text,
            assistant_text=assistant_text,
            extra_metadata=(
                {"exposed_temporary_proposal_ids": _temporary_proposal_ids(state)}
                if _temporary_proposal_ids(state)
                else None
            ),
        )
        try:
            result = await asyncio.wait_for(
                call_tool("auto_context_inject", arguments, config),
                timeout=config.timeout_seconds + 0.5,
            )
        except Exception:
            return _continue_output(config, "capture")
        if _capture_completed(result) and path is not None:
            _unlink(path)
        return _continue_output(config)

    if event_name == "SessionEnd":
        session_id = _text(values.get("session_id"))
        _cleanup_states(
            config,
            session_id=session_id,
            max_files=None if session_id else _MAX_STATE_FILES,
        )
        return _continue_output(config)

    return _continue_output(config)


def _read_payload(stream: TextIO) -> dict[str, Any]:
    raw = stream.read(_MAX_INPUT_BYTES + 1)
    if len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("hook_input_too_large")
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("hook_input_not_object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        if arguments != [_CLEANUP_STATES_ARGUMENT]:
            sys.stderr.write(f"usage: {Path(sys.argv[0]).name} [{_CLEANUP_STATES_ARGUMENT}]\n")
            return 2
        try:
            config = HookConfig.from_environ(os.environ, {})
            stats = _cleanup_states(config, max_files=_MAX_STATE_FILES)
            output: dict[str, Any] = {
                "status": "ok",
                "operation": "cleanup_states",
                **stats,
            }
        except Exception:
            output = {"status": "error", "operation": "cleanup_states"}
            sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
            sys.stdout.write("\n")
            return 1
        sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        return 0
    try:
        payload = _read_payload(sys.stdin)
        output = asyncio.run(process_hook(payload))
    except Exception:
        output = {"continue": True}
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
