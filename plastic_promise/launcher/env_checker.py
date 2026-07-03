"""Environment pre-check for One-Click Launcher.

Checks:
  1. Python >= 3.10
  2. Ollama available (optional, skip with --skip-ollama-check)
  3. LanceDB importable
  4. Port 9020 free (or occupied by our own MCP server)
  5. plastic_memory.db exists (warn only)
"""

import json
import socket
import sys
import os
import urllib.request
import urllib.error


def run_env_checks(skip_ollama: bool = False) -> tuple[bool, list[str], bool]:
    """Run all environment checks. Returns (all_ok, messages, mcp_already_running).

    mcp_already_running is True when port 9020 is occupied by our own
    MCP server (verified via /health endpoint). The launcher can then
    skip starting the MCP service and only launch the daemon.
    """
    messages = []
    all_ok = True
    mcp_already_running = False

    # 1. Python >= 3.10
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
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
        occupant = _identify_port_9020_occupant()
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
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if os.path.exists(db_path):
        messages.append(f"[ENV]   {db_path} ................. [OK] found")
    else:
        messages.append(f"[ENV]   {db_path} ................. [WARN] not found (first run)")

    return all_ok, messages, mcp_already_running


def _identify_port_9020_occupant() -> dict | None:
    """Check if port 9020 is occupied by our own MCP server.

    Hits the /health endpoint. Returns a dict with 'pid' and 'uptime'
    if the response matches Plastic Promise MCP format, None otherwise.
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:9020/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        # Verify it's our MCP: must have status=ok, version, and pid
        if (
            isinstance(data, dict)
            and data.get("status") == "ok"
            and "version" in data
            and "pid" in data
        ):
            return {"pid": data["pid"], "uptime": float(data.get("uptime", 0))}
    except Exception:
        pass
    return None
