"""Environment pre-check for One-Click Launcher.

Checks:
  1. Python >= 3.10
  2. Ollama available (optional, skip with --skip-ollama-check)
  3. LanceDB importable
  4. Port 9020 free
  5. plastic_memory.db exists (warn only)
"""

import socket
import sys
import os
import urllib.request
import urllib.error


def run_env_checks(skip_ollama: bool = False) -> tuple[bool, list[str]]:
    """Run all environment checks. Returns (all_ok, messages)."""
    messages = []
    all_ok = True

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

    # 4. Port 9020
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 9020))
        sock.close()
        messages.append("[ENV]   Port 9020 ..................... [OK] free")
    except OSError:
        messages.append("[ENV]   Port 9020 ..................... [FAIL] in use (another instance?)")
        all_ok = False

    # 5. plastic_memory.db
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if os.path.exists(db_path):
        messages.append(f"[ENV]   {db_path} ................. [OK] found")
    else:
        messages.append(f"[ENV]   {db_path} ................. [WARN] not found (first run)")

    return all_ok, messages
