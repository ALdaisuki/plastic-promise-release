"""Codex MCP configuration checks used by the launcher preflight."""

from __future__ import annotations

import shutil
from pathlib import Path

import tomllib

EXPECTED_STREAMABLE_HTTP_URL = "http://127.0.0.1:9020/mcp"


def check_project_codex_mcp_config(project_root: str | Path = ".") -> tuple[bool, list[str]]:
    """Validate the project-local Codex MCP config when it exists."""
    root = Path(project_root)
    config_path = root / ".codex" / "config.toml"
    if not config_path.exists():
        return True, ["[ENV]   Codex MCP config .............. [WARN] not found"]

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"[ENV]   Codex MCP config .............. [FAIL] unreadable ({exc})"]

    messages: list[str] = []
    ok = True

    plastic = data.get("mcp_servers", {}).get("plastic_promise")
    if not isinstance(plastic, dict):
        messages.append("[ENV]   Codex MCP config .............. [FAIL] missing plastic_promise server")
        ok = False
    elif plastic.get("url") != EXPECTED_STREAMABLE_HTTP_URL:
        messages.append(
            "[ENV]   Codex MCP config .............. [FAIL] plastic_promise url must be "
            f"{EXPECTED_STREAMABLE_HTTP_URL}"
        )
        ok = False

    fallback = (
        data.get("profiles", {})
        .get("stdio-fallback", {})
        .get("mcp_servers", {})
        .get("plastic_promise")
    )
    if isinstance(fallback, dict):
        command = str(fallback.get("command", "")).strip()
        if not command or _resolve_command(command, root) is None:
            messages.append(
                "[ENV]   Codex MCP config .............. [FAIL] stdio fallback command not found"
            )
            ok = False
        if fallback.get("args") != ["-m", "plastic_promise"]:
            messages.append(
                "[ENV]   Codex MCP config .............. [FAIL] stdio fallback args must run "
                "-m plastic_promise"
            )
            ok = False

    if ok:
        messages.append("[ENV]   Codex MCP config .............. [OK] streamable HTTP + fallback")
    return ok, messages


def _resolve_command(command: str, project_root: Path) -> str | None:
    command_path = Path(command)
    if command_path.is_absolute():
        return str(command_path) if command_path.exists() else None

    project_command = project_root / command_path
    if project_command.exists():
        return str(project_command)

    if len(command_path.parts) > 1:
        return None

    return shutil.which(command)
