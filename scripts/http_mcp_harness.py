"""Owned subprocess and HTTP readiness helpers for MCP acceptance tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plastic_promise.launcher.service_manager import validate_mcp_health_identity
from plastic_promise.launcher.subprocess_utils import hidden_subprocess_kwargs


@dataclass
class ManagedProcess:
    process: subprocess.Popen[str]
    command: tuple[str, ...]
    stdout_path: Path
    stderr_path: Path

    @classmethod
    def start(
        cls,
        command: tuple[str, ...] | list[str],
        *,
        cwd: str | Path,
        env: dict[str, str],
        stdout_path: str | Path,
        stderr_path: str | Path,
    ) -> ManagedProcess:
        stdout = Path(stdout_path).resolve()
        stderr = Path(stderr_path).resolve()
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stderr.parent.mkdir(parents=True, exist_ok=True)
        normalized = tuple(str(part) for part in command)
        stdout_handle = stdout.open("w", encoding="utf-8")
        stderr_handle = stderr.open("w", encoding="utf-8")
        try:
            kwargs: dict[str, Any] = {
                "cwd": str(Path(cwd).resolve()),
                "env": dict(env),
                "stdout": stdout_handle,
                "stderr": stderr_handle,
                "text": True,
            }
            if sys.platform == "win32":
                kwargs.update(hidden_subprocess_kwargs(new_process_group=True))
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(normalized, **kwargs)
        except BaseException:
            stdout_handle.close()
            stderr_handle.close()
            raise
        stdout_handle.close()
        stderr_handle.close()
        return cls(process, normalized, stdout, stderr)

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    @property
    def dead(self) -> bool:
        return self.process.poll() is not None

    def wait(self, timeout: float = 30.0) -> int:
        return int(self.process.wait(timeout=timeout))

    def terminate(self, timeout: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=max(1.0, timeout / 2))


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def process_environment(overrides: dict[str, str], *, project_root: str | Path) -> dict[str, str]:
    allowlist = {
        "APPDATA",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowlist}
    python_paths = [str(Path(project_root).resolve())]
    for path in sys.path:
        if path and "site-packages" in path:
            python_paths.append(str(Path(path).resolve()))
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
    env.update({str(key): str(value) for key, value in overrides.items()})
    return env


def runtime_python() -> str:
    return str(getattr(sys, "_base_executable", None) or sys.executable)


def require_owned_health(
    payload: dict[str, Any],
    managed: ManagedProcess,
    *,
    expected_source_root: str | Path | None = None,
    expected_source_revision: str | None = None,
    expected_fusion_policy: str | None = None,
) -> dict[str, Any]:
    """Reject a healthy endpoint unless it belongs to the expected process/source."""

    if payload.get("status") != "ok":
        raise RuntimeError("health_not_ok")
    try:
        observed_pid = int(payload.get("pid"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("health_pid_missing") from exc
    if observed_pid != managed.pid:
        raise RuntimeError(f"health_pid_mismatch:expected={managed.pid}:observed={observed_pid}")
    identity_requested = any(
        value is not None
        for value in (
            expected_source_root,
            expected_source_revision,
            expected_fusion_policy,
        )
    )
    if identity_requested:
        if (
            expected_source_root is None
            or expected_source_revision is None
            or expected_fusion_policy is None
        ):
            raise RuntimeError("expected_health_identity_incomplete")
        valid, reason = validate_mcp_health_identity(
            payload,
            expected_pid=managed.pid,
            expected_source_root=expected_source_root,
            expected_source_revision=expected_source_revision,
        )
        if not valid:
            raise RuntimeError(reason)
        if payload.get("fusion_policy") != expected_fusion_policy:
            raise RuntimeError("health_fusion_policy_mismatch")
    return dict(payload)


async def wait_for_health(
    url: str,
    managed: ManagedProcess,
    *,
    timeout: float = 120.0,
    expected_source_root: str | Path | None = None,
    expected_source_revision: str | None = None,
    expected_fusion_policy: str | None = None,
) -> dict[str, Any]:
    import httpx

    deadline = time.monotonic() + timeout
    last_error = "health_not_ready"
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.monotonic() < deadline:
            if managed.process.poll() is not None:
                raise RuntimeError(
                    f"managed_process_exited_before_health:{managed.process.returncode}"
                )
            try:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "ok":
                    return require_owned_health(
                        dict(payload),
                        managed,
                        expected_source_root=expected_source_root,
                        expected_source_revision=expected_source_revision,
                        expected_fusion_policy=expected_fusion_policy,
                    )
                last_error = "health_not_ok"
            except Exception as exc:
                last_error = exc.__class__.__name__
            await asyncio.sleep(0.2)
    raise RuntimeError(f"managed_health_timeout:{last_error}")


def _parse_tool_payload(content: list[Any], tool_name: str) -> dict[str, Any]:
    texts = [
        str(getattr(item, "text", "")) for item in content if getattr(item, "type", "") == "text"
    ]
    if not texts:
        raise RuntimeError(f"public_tool_empty_response:{tool_name}")
    try:
        payload = json.loads(texts[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"public_tool_non_json:{tool_name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"public_tool_non_object:{tool_name}")
    if payload.get("error"):
        raise RuntimeError(f"public_tool_error:{tool_name}:{payload['error']}")
    return payload


async def call_tools_json(
    url: str,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    timeout: float = 120.0,
) -> list[dict[str, Any]]:
    """Call public MCP tools through one Streamable HTTP session."""

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    request_timeout = httpx.Timeout(timeout, read=timeout)
    async with (
        httpx.AsyncClient(timeout=request_timeout) as client,
        streamable_http_client(url, http_client=client) as streams,
    ):
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            payloads: list[dict[str, Any]] = []
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                payloads.append(_parse_tool_payload(list(result.content), name))
            return payloads


async def call_tool_json(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    return (await call_tools_json(url, [(tool_name, arguments)], timeout=timeout))[0]


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
)


def sanitized_log_tail(
    path: str | Path,
    *,
    private_roots: tuple[str | Path, ...] = (),
    max_lines: int = 80,
    max_chars: int = 500,
) -> list[str]:
    """Return a bounded log tail with paths and common secrets redacted."""

    target = Path(path)
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    roots = sorted(
        {str(Path(root).resolve()) for root in private_roots},
        key=len,
        reverse=True,
    )
    sanitized: list[str] = []
    for raw_line in lines:
        line = raw_line
        for root in roots:
            line = line.replace(root, "<isolated-root>")
            line = line.replace(root.replace("\\", "/"), "<isolated-root>")
        for pattern in _SECRET_PATTERNS:
            line = pattern.sub(r"\1<redacted>", line)
        sanitized.append(line[:max_chars])
    return sanitized


async def wait_for_port_closed(port: int, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
        except (OSError, asyncio.TimeoutError):
            return
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)
    raise RuntimeError("managed_port_still_open")
