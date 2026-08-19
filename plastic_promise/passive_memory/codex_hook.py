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
# Codex runs on the Mac and reaches the server-side MCP listener through the
# SSH local forward.  The server itself listens on 9020; 19020 is the Mac
# client endpoint.  Keep the endpoint override for explicitly provisioned
# local-development or alternate-forward setups.
_DEFAULT_MCP_URL = "http://127.0.0.1:19020/mcp"
_CLEANUP_STATES_ARGUMENT = "--cleanup-states"
_CLEANUP_CURSOR_NAME = ".cleanup-cursor"
_REDACTED_PROMPT = "[REDACTED]"
_MAX_TEMPORARY_PROPOSAL_IDS = 8
_MAX_COLLABORATION_ITEMS = 20
_MAX_COLLABORATION_TEXT_CHARS = 8192
_MAX_CONTINUATION_CHARS = 4096
_MAX_CONTINUATION_TTL_SECONDS = 60 * 60
_REGISTERED_CALL_RESULT_KEY = "_codex_hook_session_init"
_CONTINUATION_RESULT_KEY = "_codex_hook_collaboration_continuation"
_CONTINUATION_EXPIRES_RESULT_KEY = "_codex_hook_collaboration_continuation_expires_at"
_SESSION_STATE_SCHEMA = "codex-hook-session-v1"
_STOP_RETRY_STATE_SCHEMA = "codex-hook-stop-retry-v1"
_CAPTURE_TERMINAL_STATUSES = frozenset(
    {
        "duplicate",
        "semantic_duplicate",
        "semantic_queued",
        "shadow",
        "skipped",
        "queued",
    }
)
_COLLABORATION_FORBIDDEN_KEYS = frozenset(
    {
        "agent_session_id",
        "binding_id",
        "binding_digest",
        "collaboration_continuation",
        "collaboration_continuation_token",
        "lease_json",
        "owner_session_id",
        "transport_session_id",
        "work_receipt_json",
    }
)


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


def _payload_workspace(payload: Mapping[str, Any]) -> str:
    """Return the first explicit workspace identity supplied by the hook host."""

    for name in (
        "cwd",
        "working_directory",
        "workdir",
        "workspace_root",
        "project_root",
    ):
        value = _text(payload.get(name))
        if value:
            return value
    workspace = payload.get("workspace")
    if isinstance(workspace, Mapping):
        for name in ("cwd", "root", "path"):
            value = _text(workspace.get(name))
            if value:
                return value
    return ""


def _normalize_project_id(value: object) -> str:
    project_id = _text(value)
    if not project_id:
        return ""
    return project_id if project_id.startswith("project:") else f"project:{project_id}"


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
        payload_workspace = _payload_workspace(payload)
        cwd = Path(payload_workspace).expanduser() if payload_workspace else _project_root()
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
        explicit_project_id = _normalize_project_id(
            _first_text(environ, "PP_CODEX_HOOK_PROJECT_ID") or payload.get("project_id")
        )
        if explicit_project_id:
            project_id = explicit_project_id
        elif payload_workspace:
            # A workspace-bearing hook event is the authoritative project
            # identity.  Generic process-wide project variables are ignored
            # here so a stale global environment cannot bleed one repository
            # into another.  If the workspace cannot be resolved, the
            # repository helper returns a stable local id or project:unknown.
            project_id = infer_repository_project_id(cwd)
        else:
            # Process-wide variables remain a compatibility fallback only for
            # callers that provide no workspace identity at all.
            project_id = (
                _normalize_project_id(_first_text(environ, "PP_PROJECT_ID", "PLASTIC_PROJECT_ID"))
                or "project:unknown"
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
            project_id=project_id,
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


def _identity(
    payload: Mapping[str, Any],
    event_name: str,
    project_id: str = "",
) -> tuple[str, str, str]:
    session_id = _text(payload.get("session_id"))
    turn_id = _text(payload.get("turn_id"))
    stable = "\x1f".join((project_id, session_id, turn_id, event_name))
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    return session_id, turn_id, digest


def _state_path(config: HookConfig, payload: Mapping[str, Any]) -> Path | None:
    session_id, turn_id, digest = _identity(payload, "turn", config.project_id)
    if not session_id or not turn_id:
        return None
    return config.state_dir / f"turn-{digest}.json"


def _hook_session_id(project_id: str, stage_session_id: object) -> str:
    session_id = _text(stage_session_id)
    if not project_id or not session_id:
        return ""
    digest = hashlib.sha256(f"{project_id}\x1f{session_id}".encode()).hexdigest()
    return f"hook-session:codex:{digest[:40]}"


def _session_state_path(config: HookConfig, stage_session_id: object) -> Path | None:
    hook_session_id = _hook_session_id(config.project_id, stage_session_id)
    if not hook_session_id:
        return None
    digest = hashlib.sha256(hook_session_id.encode("utf-8")).hexdigest()
    return config.state_dir / f"session-{digest}.json"


def _continuation_token(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_CONTINUATION_CHARS:
        return ""
    if any(
        character.isspace() or ord(character) < 33 or ord(character) == 127 for character in value
    ):
        return ""
    return value


def _continuation_expiry(value: object, *, now: float | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    current = int(time.time() if now is None else now)
    if value <= current or value > current + _MAX_CONTINUATION_TTL_SECONDS:
        return 0
    return value


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
    session_id, turn_id, _digest = _identity(payload, "turn", config.project_id)
    _ensure_state_dir(config, create=True)
    state = {
        "schema_version": "codex-hook-turn-v3",
        "session_id": session_id,
        "turn_id": turn_id,
        "project_id": config.project_id,
        "cwd": _payload_workspace(payload),
        "prompt": _safe_prompt(prompt)[: config.max_text_chars],
        "temporary_proposal_ids": _temporary_proposal_ids(
            {"temporary_proposal_ids": temporary_proposal_ids}
        ),
        "created_at": time.time(),
    }
    return _write_state_file(config, path, state)


def _write_stop_retry_state(
    config: HookConfig,
    payload: Mapping[str, Any],
) -> Path | None:
    path = _state_path(config, payload)
    if path is None:
        return None
    session_id, turn_id, digest = _identity(payload, "after_invoke", config.project_id)
    state = {
        "schema_version": _STOP_RETRY_STATE_SCHEMA,
        "session_id": session_id,
        "turn_id": turn_id,
        "project_id": config.project_id,
        "stop_request_id": turn_id or digest[:24],
        "created_at": time.time(),
    }
    return _write_state_file(config, path, state)


def _write_state_file(
    config: HookConfig,
    path: Path,
    state: Mapping[str, Any],
) -> Path:
    _ensure_state_dir(config, create=True)
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


def _write_session_state(
    config: HookConfig,
    stage_session_id: object,
    continuation: object,
    expires_at_epoch: object,
) -> Path:
    path = _session_state_path(config, stage_session_id)
    hook_session_id = _hook_session_id(config.project_id, stage_session_id)
    token = _continuation_token(continuation)
    expiry = _continuation_expiry(expires_at_epoch)
    if path is None or not hook_session_id or not token or not expiry:
        raise OSError("hook_session_state_invalid")
    _ensure_state_dir(config, create=True)
    state = {
        "schema_version": _SESSION_STATE_SCHEMA,
        "hook_session_id": hook_session_id,
        "project_id": config.project_id,
        "collaboration_continuation": token,
        "expires_at_epoch": expiry,
        "updated_at": time.time(),
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


def _read_session_state(
    config: HookConfig,
    stage_session_id: object,
) -> tuple[Path | None, dict[str, Any] | None]:
    path = _session_state_path(config, stage_session_id)
    if path is None or not path.is_file():
        return path, None
    try:
        if path.is_symlink():
            raise ValueError("hook_session_state_symlink")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema_version") != _SESSION_STATE_SCHEMA:
            raise ValueError("hook_session_state_invalid")
        updated_at = float(state.get("updated_at") or 0.0)
        if not math.isfinite(updated_at) or updated_at <= 0:
            raise ValueError("hook_session_state_invalid_updated_at")
        if state.get("project_id") != config.project_id:
            raise ValueError("hook_session_state_project_mismatch")
        expected_hook_session_id = _hook_session_id(config.project_id, stage_session_id)
        if state.get("hook_session_id") != expected_hook_session_id:
            raise ValueError("hook_session_state_scope_mismatch")
        token = _continuation_token(state.get("collaboration_continuation"))
        if not token:
            raise ValueError("hook_session_state_continuation_invalid")
        now = time.time()
        expiry = _continuation_expiry(state.get("expires_at_epoch"), now=now)
        if not expiry:
            raise ValueError("hook_session_state_continuation_expired")
        if (
            updated_at > now + config.state_ttl_seconds
            or now - updated_at > config.state_ttl_seconds
        ):
            raise ValueError("hook_session_state_expired")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _unlink(path)
        return path, None
    state["collaboration_continuation"] = token
    state["expires_at_epoch"] = expiry
    return path, state


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
    session_id, turn_id, _digest = _identity(payload, "turn", config.project_id)
    if (
        state.get("session_id") != session_id
        or state.get("turn_id") != turn_id
        or state.get("project_id", config.project_id) != config.project_id
    ):
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
                matches_project = state.get("project_id", config.project_id) == config.project_id
                matches_session = bool(
                    session_id and matches_project and state.get("session_id") == session_id
                )
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
    session_id, turn_id, digest = _identity(payload, event, config.project_id)
    metadata = {
        key: value
        for key, value in {
            "hook_event_name": _event_name(payload),
            "cwd": _payload_workspace(payload),
            "project_id": config.project_id,
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


def _session_init_arguments(
    payload: Mapping[str, Any],
    config: HookConfig,
    *,
    event: str,
    task_description: str,
    continuation: str = "",
) -> dict[str, Any]:
    """Build the non-authoritative registration request for one fresh Hook client.

    The Hook supplies only project/workflow consistency inputs.  Durable actor,
    transport, policy, role, AgentSession identity, and continuation authority
    remain server-owned and cannot be selected through this payload.
    """

    session_id, _turn_id, digest = _identity(payload, event, config.project_id)
    session_id = session_id or _text(payload.get("stage_session_id"))
    hook_session_id = _hook_session_id(config.project_id, session_id)
    arguments: dict[str, Any] = {
        "task_description": task_description or f"Codex {event}",
        "task_type": config.task_type,
        "context_mode": "none",
        "response_mode": "compact",
        "route": "idea-to-ship",
        "stage_session_id": session_id or f"codex:{digest[:16]}",
        "flow_line_id": "codex",
        "project_policy": config.project_policy,
        "hook_session_id": hook_session_id,
    }
    if continuation:
        arguments["collaboration_continuation_token"] = continuation
    if config.project_id:
        arguments["project_id"] = config.project_id
    return arguments


def _registration_continuation(
    payload: Mapping[str, Any],
    *,
    expected_hook_session_id: str,
) -> tuple[str, int]:
    collaboration = payload.get("collaboration_continuation")
    if not isinstance(collaboration, Mapping):
        return "", 0
    if collaboration.get("schema_version") != "durable-collaboration-continuation/v1":
        return "", 0
    if collaboration.get("storage") != "client-secret-only":
        return "", 0
    if collaboration.get("hook_session_id") != expected_hook_session_id:
        return "", 0
    token = _continuation_token(collaboration.get("token"))
    expiry = _continuation_expiry(collaboration.get("expires_at_epoch"))
    if not token or not expiry:
        return "", 0
    return token, expiry


def _without_continuation_secrets(payload: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(payload)
    projected.pop("collaboration_continuation", None)
    projected.pop("collaboration_continuation_token", None)
    return projected


def _contains_continuation(value: object, token: str) -> bool:
    if not token:
        return False
    if isinstance(value, str):
        return token in value
    if isinstance(value, Mapping):
        return any(
            _contains_continuation(key, token) or _contains_continuation(item, token)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_continuation(item, token) for item in value)
    return False


def _session_init_succeeded(payload: Mapping[str, Any], config: HookConfig) -> bool:
    if payload.get("success") is not True:
        return False
    projected_project = _normalize_project_id(payload.get("project_id"))
    if projected_project and projected_project != config.project_id:
        return False
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return False
    task_binding = diagnostics.get("task_session_binding")
    durable_binding = diagnostics.get("durable_collaboration_binding")
    return bool(
        isinstance(task_binding, Mapping)
        and task_binding.get("success") is True
        and isinstance(durable_binding, Mapping)
        and durable_binding.get("success") is True
        and durable_binding.get("persistent") is True
    )


def _bounded_collaboration_value(value: object, *, depth: int = 0) -> Any:
    """Project server collaboration data through a small defensive JSON bound."""

    if depth >= 5:
        return None
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:64]:
            key = _text(raw_key)
            if not key or key in _COLLABORATION_FORBIDDEN_KEYS:
                continue
            projected[key[:96]] = _bounded_collaboration_value(item, depth=depth + 1)
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_collaboration_value(item, depth=depth + 1)
            for item in list(value)[:_MAX_COLLABORATION_ITEMS]
        ]
    return _text(value)[:512]


def _bounded_collaboration_projection(
    registration: Mapping[str, Any],
    result: Mapping[str, Any],
    config: HookConfig,
) -> dict[str, Any] | None:
    """Select a verified heartbeat/session-init projection without authority handles."""

    if not _session_init_succeeded(registration, config):
        return None
    lifecycle = result.get("durable_collaboration_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    if lifecycle.get("state") == "durable" and lifecycle.get("persistent") is True:
        receipt = lifecycle.get("receipt")
        if isinstance(receipt, Mapping):
            source: Mapping[str, Any] = receipt
            action = _text(lifecycle.get("action")) or "heartbeat"
        else:
            return None
    else:
        source = registration.get("durable_collaboration")
        if not isinstance(source, Mapping):
            return None
        action = "session_init"
    source_project = _normalize_project_id(source.get("project_id"))
    if source_project and source_project != config.project_id:
        return None
    allowlisted = {
        key: source[key]
        for key in (
            "schema_version",
            "state",
            "persistent",
            "project_id",
            "agent",
            "working_set_summary",
            "assigned_work",
            "peer_delta",
            "cursor",
            "visibility",
            "canonical_memory_effect",
            "observed_at",
            "reconcile",
            "released_lease_count",
        )
        if key in source
    }
    projected = _bounded_collaboration_value(allowlisted)
    if not isinstance(projected, dict):
        return None
    return {
        "schema_version": "codex-hook-collaboration/v1",
        "action": action,
        "project_id": config.project_id,
        "session_scope": "authenticated-hook-session",
        "projection": projected,
        "canonical_memory_effect": "none",
    }


def _collaboration_injection(
    registration: Mapping[str, Any],
    result: Mapping[str, Any],
    config: HookConfig,
) -> str:
    projection = _bounded_collaboration_projection(registration, result, config)
    if projection is None:
        return ""
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
    if len(encoded) > _MAX_COLLABORATION_TEXT_CHARS:
        return ""
    return (
        '<plastic-promise-collaboration trust="server-projection">'
        f"{encoded}</plastic-promise-collaboration>"
    )


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
    event = _text(arguments.get("event")).casefold() or tool_name
    stage_session_id = _text(arguments.get("stage_session_id"))
    hook_session_id = _hook_session_id(config.project_id, stage_session_id)
    _session_path, session_state = _read_session_state(config, stage_session_id)
    continuation = (
        _continuation_token(session_state.get("collaboration_continuation"))
        if isinstance(session_state, Mapping)
        else ""
    )
    continuation_expiry = (
        _continuation_expiry(session_state.get("expires_at_epoch"))
        if isinstance(session_state, Mapping)
        else 0
    )
    if event in {"after_invoke", "session_end"} and not continuation:
        return {
            "status": "deferred",
            "reason": "collaboration_continuation_required",
            "persistent": False,
        }
    timeout = httpx.Timeout(config.timeout_seconds, read=config.timeout_seconds)
    async with (
        httpx.AsyncClient(timeout=timeout, headers=headers, trust_env=False) as client,
        streamable_http_client(
            config.mcp_url,
            http_client=client,
            terminate_on_close=True,
        ) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        registration = await session.call_tool(
            "session-init",
            _session_init_arguments(
                arguments,
                config,
                event=event,
                task_description=_text(arguments.get("task_description")),
                continuation=continuation,
            ),
        )
        registration_payload = _parse_tool_result(registration)
        registration_projection = _without_continuation_secrets(registration_payload)
        if not _session_init_succeeded(registration_payload, config):
            return {
                "status": "degraded",
                "reason": "durable_collaboration_session_init_failed",
                "persistent": False,
                _REGISTERED_CALL_RESULT_KEY: registration_projection,
            }
        issued_continuation, issued_expiry = _registration_continuation(
            registration_payload,
            expected_hook_session_id=hook_session_id,
        )
        if "collaboration_continuation" in registration_payload and not issued_continuation:
            return {
                "status": "deferred",
                "reason": "collaboration_continuation_missing_or_invalid",
                "persistent": False,
                _REGISTERED_CALL_RESULT_KEY: registration_projection,
            }
        refreshed_continuation = issued_continuation or continuation
        refreshed_expiry = issued_expiry or continuation_expiry
        if not refreshed_continuation:
            return {
                "status": "deferred",
                "reason": "collaboration_continuation_missing_or_invalid",
                "persistent": False,
                _REGISTERED_CALL_RESULT_KEY: registration_projection,
            }
        if not refreshed_expiry:
            return {
                "status": "deferred",
                "reason": "collaboration_continuation_expiry_missing_or_invalid",
                "persistent": False,
                _REGISTERED_CALL_RESULT_KEY: registration_projection,
            }
        if _contains_continuation(registration_projection, refreshed_continuation):
            return {
                "status": "deferred",
                "reason": "collaboration_continuation_exposure_blocked",
                "persistent": False,
            }
        if issued_continuation:
            try:
                _write_session_state(
                    config,
                    stage_session_id,
                    refreshed_continuation,
                    refreshed_expiry,
                )
            except OSError:
                # A newly opened session without a client-side continuation
                # would be orphaned. Close only that initial binding; an
                # existing resumable session keeps its prior retry state.
                if not continuation:
                    with suppress(Exception):
                        await session.call_tool(
                            "auto_context_inject",
                            {
                                **arguments,
                                "event": "session_end",
                                "task_description": "Codex continuation persistence failed",
                                "user_text": "",
                                "assistant_text": "",
                                "hook_session_id": hook_session_id,
                                "collaboration_continuation_token": refreshed_continuation,
                                "metadata": {
                                    "project_id": config.project_id,
                                    "session_end_reason": "continuation_persistence_failed",
                                },
                            },
                        )
                return {
                    "status": "deferred",
                    "reason": "collaboration_continuation_persistence_failed",
                    "persistent": False,
                    _REGISTERED_CALL_RESULT_KEY: registration_projection,
                }
        target_arguments = {
            **arguments,
            "hook_session_id": hook_session_id,
            "collaboration_continuation_token": refreshed_continuation,
        }
        result = await session.call_tool(tool_name, target_arguments)
        payload = _without_continuation_secrets(_parse_tool_result(result))
        if _contains_continuation(payload, refreshed_continuation):
            payload = {
                "status": "degraded",
                "reason": "collaboration_continuation_exposure_blocked",
                "persistent": False,
            }
        payload[_REGISTERED_CALL_RESULT_KEY] = registration_projection
        payload[_CONTINUATION_RESULT_KEY] = refreshed_continuation
        payload[_CONTINUATION_EXPIRES_RESULT_KEY] = refreshed_expiry
        return payload


def _continue_output(config: HookConfig, operation: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {"continue": True}
    if config.verbose and operation:
        output["systemMessage"] = (
            f"Plastic Promise {operation} unavailable; Codex continued without blocking."
        )
    return output


def _hook_operation_timeout(config: HookConfig) -> float:
    """Bound register + lifecycle operation + cleanup for a fresh Hook client."""

    return min(30.0, max(2.0, config.timeout_seconds * 3.0 + 0.5))


def _persist_result_continuation(
    payload: Mapping[str, Any],
    config: HookConfig,
    hook_payload: Mapping[str, Any],
) -> bool:
    token = _continuation_token(payload.get(_CONTINUATION_RESULT_KEY))
    if not token:
        return True
    expiry = _continuation_expiry(payload.get(_CONTINUATION_EXPIRES_RESULT_KEY))
    if not expiry:
        return False
    stage_session_id = _text(hook_payload.get("session_id"))
    if not stage_session_id:
        return False
    _path, current = _read_session_state(config, stage_session_id)
    if (
        isinstance(current, Mapping)
        and _continuation_token(current.get("collaboration_continuation")) == token
        and _continuation_expiry(current.get("expires_at_epoch")) == expiry
    ):
        return True
    try:
        _write_session_state(config, stage_session_id, token, expiry)
    except OSError:
        return False
    return True


def _session_end_completed(payload: Mapping[str, Any]) -> bool:
    lifecycle = payload.get("durable_collaboration_lifecycle")
    receipt = lifecycle.get("receipt") if isinstance(lifecycle, Mapping) else None
    return bool(
        _text(payload.get("status")).casefold() == "completed"
        and payload.get("persistent") is True
        and isinstance(lifecycle, Mapping)
        and lifecycle.get("state") == "durable"
        and lifecycle.get("action") == "session_end"
        and lifecycle.get("persistent") is True
        and isinstance(receipt, Mapping)
        and receipt.get("schema_version") == "durable-collaboration-session-end/v1"
        and receipt.get("state") == "closed"
        and receipt.get("persistent") is True
    )


def _session_end_deferred_output() -> dict[str, Any]:
    return {
        "continue": True,
        "systemMessage": "Plastic Promise session_end deferred; retry state retained.",
    }


def _injection_text(payload: Mapping[str, Any]) -> str:
    direct = _text(payload.get("injection"))
    if direct:
        return direct
    data = payload.get("data")
    return _text(data.get("injection")) if isinstance(data, dict) else ""


def _capture_completed(payload: Mapping[str, Any]) -> bool:
    status = _text(payload.get("status")).casefold()
    if payload.get("error") or status not in _CAPTURE_TERMINAL_STATUSES:
        return False
    if payload.get("inject_memory_id") not in (None, ""):
        return False
    if payload.get("canonical_memory_effect") not in (None, "", "none"):
        return False
    if status == "queued":
        return bool(payload.get("queued")) and bool(_text(payload.get("outbox_id")))
    if status == "semantic_queued":
        return bool(_text(payload.get("semantic_job_id")))
    return True


def _stop_activity_completed(payload: Mapping[str, Any]) -> bool:
    lifecycle = payload.get("durable_collaboration_lifecycle")
    receipt = lifecycle.get("receipt") if isinstance(lifecycle, Mapping) else None
    stop_activity = receipt.get("stop_activity") if isinstance(receipt, Mapping) else None
    return bool(
        isinstance(lifecycle, Mapping)
        and lifecycle.get("state") == "durable"
        and lifecycle.get("action") == "heartbeat"
        and lifecycle.get("persistent") is True
        and isinstance(receipt, Mapping)
        and receipt.get("persistent") is True
        and isinstance(stop_activity, Mapping)
        and stop_activity.get("schema_version") == "durable-collaboration-stop-activity/v1"
        and stop_activity.get("state") == "durable"
        and stop_activity.get("persistent") is True
        and isinstance(stop_activity.get("events"), list)
    )


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
        arguments = _call_arguments(
            values,
            config,
            event="before_invoke",
            user_text=prompt,
        )
        try:
            result = await asyncio.wait_for(
                call_tool("auto_context_inject", arguments, config),
                timeout=_hook_operation_timeout(config),
            )
        except Exception:
            return _continue_output(config, "preload")
        if not _persist_result_continuation(result, config, values):
            return _continue_output(config, "continuation")
        temporary_proposal_ids = _temporary_proposal_ids(result)
        if capture_enabled and temporary_proposal_ids:
            with suppress(OSError):
                _write_turn_state(
                    config,
                    values,
                    prompt,
                    temporary_proposal_ids=temporary_proposal_ids,
                )
        registration = result.get(_REGISTERED_CALL_RESULT_KEY)
        registration = registration if isinstance(registration, Mapping) else {}
        collaboration = _collaboration_injection(registration, result, config)
        injection = _injection_text(result) if context_enabled else ""
        injection = "\n\n".join(part for part in (collaboration, injection) if part)
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
            path, retry_state = _read_turn_state(config, values)
            arguments = _call_arguments(
                values,
                config,
                event="after_invoke",
                user_text="",
                assistant_text="",
                extra_metadata={"passive_capture_enabled": False},
            )
            retry_request_id = (
                _text(retry_state.get("stop_request_id"))
                if isinstance(retry_state, Mapping)
                else ""
            )
            if retry_request_id:
                arguments["request_id"] = retry_request_id
            try:
                result = await asyncio.wait_for(
                    call_tool("auto_context_inject", arguments, config),
                    timeout=_hook_operation_timeout(config),
                )
            except Exception:
                with suppress(OSError):
                    _write_stop_retry_state(config, values)
                return _continue_output(config, "heartbeat")
            if not _persist_result_continuation(result, config, values):
                with suppress(OSError):
                    _write_stop_retry_state(config, values)
                return _continue_output(config, "continuation")
            if _stop_activity_completed(result):
                if path is not None:
                    _unlink(path)
            else:
                with suppress(OSError):
                    _write_stop_retry_state(config, values)
            return _continue_output(config)
        _cleanup_states(config)
        path, state = _read_turn_state(config, values)
        user_text = (
            _raw_text(state.get("prompt"))[: config.max_text_chars]
            if isinstance(state, Mapping)
            else ""
        )
        assistant_text = (
            _safe_prompt(_text(values.get("last_assistant_message")))[: config.max_text_chars]
            if isinstance(state, Mapping)
            else ""
        )
        arguments = _call_arguments(
            values,
            config,
            event="after_invoke",
            user_text=user_text,
            assistant_text=assistant_text,
            extra_metadata=(
                {"exposed_temporary_proposal_ids": _temporary_proposal_ids(state)}
                if isinstance(state, Mapping) and _temporary_proposal_ids(state)
                else ({"passive_capture_available": False} if state is None else None)
            ),
        )
        retry_request_id = _text(state.get("stop_request_id")) if isinstance(state, Mapping) else ""
        if retry_request_id:
            arguments["request_id"] = retry_request_id
        try:
            result = await asyncio.wait_for(
                call_tool("auto_context_inject", arguments, config),
                timeout=_hook_operation_timeout(config),
            )
        except Exception:
            return _continue_output(config, "capture")
        if not _persist_result_continuation(result, config, values):
            return _continue_output(config, "continuation")
        capture_completed = _capture_completed(result)
        stop_activity_completed = _stop_activity_completed(result)
        if capture_completed and stop_activity_completed and path is not None:
            _unlink(path)
        elif capture_completed and not stop_activity_completed:
            with suppress(OSError):
                _write_stop_retry_state(config, values)
        return _continue_output(config)

    if event_name == "SessionEnd":
        session_id = _text(values.get("session_id"))
        arguments = _call_arguments(
            values,
            config,
            event="session_end",
            user_text="",
            extra_metadata={"session_end_reason": _text(values.get("reason"))}
            if _text(values.get("reason"))
            else None,
        )
        try:
            result = await asyncio.wait_for(
                call_tool("auto_context_inject", arguments, config),
                timeout=_hook_operation_timeout(config),
            )
        except Exception:
            return _session_end_deferred_output()
        if not _persist_result_continuation(result, config, values):
            return _session_end_deferred_output()
        if not _session_end_completed(result):
            return _session_end_deferred_output()
        _cleanup_states(
            config,
            session_id=session_id,
            max_files=None if session_id else _MAX_STATE_FILES,
        )
        session_path = _session_state_path(config, session_id)
        if session_path is not None:
            _unlink(session_path)
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
