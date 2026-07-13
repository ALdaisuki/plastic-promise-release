"""Environment pre-check for One-Click Launcher.

Checks:
  1. Python >= 3.10
  2. Ollama available (optional, skip with --skip-ollama-check)
  3. LanceDB importable
  4. Port 9020 free (or occupied by our own MCP server)
  5. plastic_memory.db exists (warn only)
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import overload

from plastic_promise.core.paths import get_db_path
from plastic_promise.launcher.codex_config import check_project_codex_mcp_config
from plastic_promise.launcher.service_manager import (
    canonical_source_root,
    resolve_source_revision,
    validate_mcp_health_identity,
)

_PROJECT_ROOT = canonical_source_root(os.path.join(os.path.dirname(__file__), "..", ".."))


@overload
def run_env_checks(
    skip_ollama: bool = False,
    *,
    expected_source_root: str | None = None,
    expected_source_revision: str | None = None,
) -> tuple[bool, list[str]]: ...


@overload
def run_env_checks(
    skip_ollama: bool = False,
    *,
    include_mcp_status: bool,
    expected_source_root: str | None = None,
    expected_source_revision: str | None = None,
) -> tuple[bool, list[str], bool]: ...


def run_env_checks(
    skip_ollama: bool = False,
    *,
    include_mcp_status: bool = False,
    expected_source_root: str | None = None,
    expected_source_revision: str | None = None,
) -> tuple[bool, list[str]] | tuple[bool, list[str], bool]:
    """Run all environment checks.

    Returns the historical ``(all_ok, messages)`` tuple by default.
    Callers that need launcher-specific MCP reuse state can request
    ``(all_ok, messages, mcp_already_running)`` explicitly.
    """
    messages = []
    all_ok = True
    mcp_already_running = False

    # 1. Python >= 3.10
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):  # noqa: UP036 - package still supports Python 3.10
        messages.append(f"[ENV]   Python {py_ver} ................ [OK]")
    else:
        messages.append(f"[ENV]   Python {py_ver} ................ [FAIL] (need >= 3.10)")
        all_ok = False

    # 2. Ollama
    if skip_ollama:
        messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... [WARN] SKIP (--skip-ollama-check)")
    else:
        try:
            req = urllib.request.Request("http://127.0.0.1:11434")
            urllib.request.urlopen(req, timeout=3)
            messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... [OK]")
        except Exception:
            messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... [FAIL] (not reachable)")
            all_ok = False

    # 3. LanceDB
    try:
        import lancedb  # noqa: F401

        messages.append("[ENV]   LanceDB ....................... [OK]")
    except ImportError:
        messages.append("[ENV]   LanceDB ....................... [FAIL] (not installed)")
        all_ok = False

    # 4. Port 9020 — check if free, or occupied by our own MCP
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 9020))
        sock.close()
        messages.append("[ENV]   Port 9020 ..................... [OK] free")
    except OSError:
        # Port is occupied — check if it's our own MCP server
        source_root = expected_source_root or _PROJECT_ROOT
        revision = (
            expected_source_revision
            if expected_source_revision is not None
            else resolve_source_revision(source_root)
        )
        occupant = _identify_port_9020_occupant(
            expected_source_root=source_root,
            expected_source_revision=revision,
        )
        if occupant:
            mcp_already_running = True
            messages.append(
                f"[ENV]   Port 9020 ..................... [OK] in use by Plastic Promise MCP"
                f" (pid={occupant['pid']}, uptime={occupant['uptime']:.0f}s)"
            )
        else:
            messages.append(
                "[ENV]   Port 9020 ..................... [FAIL] in use by unknown process"
                " (not Plastic Promise MCP)"
            )
            all_ok = False

    # 5. plastic_memory.db
    db_path = get_db_path()
    if os.path.exists(db_path):
        messages.append(f"[ENV]   {db_path} ................. [OK] found")
    else:
        messages.append(f"[ENV]   {db_path} ................. [WARN] not found (first run)")

    codex_ok, codex_messages = check_project_codex_mcp_config(os.getcwd())
    messages.extend(codex_messages)
    all_ok = all_ok and codex_ok

    if include_mcp_status:
        return all_ok, messages, mcp_already_running
    return all_ok, messages


def _identify_port_9020_occupant(
    *,
    expected_source_root: str = _PROJECT_ROOT,
    expected_source_revision: str | None = None,
) -> dict | None:
    """Return an occupant only when health proves the expected checkout identity."""
    try:
        req = urllib.request.Request("http://127.0.0.1:9020/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        valid, _reason = validate_mcp_health_identity(
            data,
            expected_source_root=expected_source_root,
            expected_source_revision=expected_source_revision,
        )
        if valid and "version" in data:
            return {"pid": data["pid"], "uptime": float(data.get("uptime", 0))}
    except Exception:
        pass
    return None
