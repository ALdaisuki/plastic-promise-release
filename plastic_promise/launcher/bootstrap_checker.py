"""First-run detection and auto-bootstrap for One-Click Launcher."""

import asyncio
import os
import sqlite3
import subprocess
import sys


def check_bootstrap(db_path: str) -> tuple[bool, str]:
    """Check if bootstrap is needed. Returns (needs_bootstrap, message)."""
    if not os.path.exists(db_path):
        return True, "plastic_memory.db not found -- first run detected"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%seed:true%'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        if count == 0:
            return True, "DB exists but no seed memories found -- re-bootstrap needed"
        return False, f"DB ready ({count} seed memories)"
    except sqlite3.OperationalError as e:
        return True, f"DB exists but memories table missing: {e}"


async def run_bootstrap(project_root: str) -> tuple[bool, str]:
    """Run bootstrap.py. Returns (ok, message). Non-blocking via asyncio.to_thread."""
    bootstrap_script = os.path.join(project_root, "scripts", "bootstrap.py")
    if not os.path.exists(bootstrap_script):
        return False, f"Bootstrap script not found: {bootstrap_script}"

    def _run():
        result = subprocess.run(
            [sys.executable, bootstrap_script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        return result

    try:
        result = await asyncio.to_thread(_run)
        if result.returncode == 0:
            return True, "Bootstrap completed successfully"
        else:
            return False, f"Bootstrap failed (exit {result.returncode}): {result.stderr[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "Bootstrap timed out (>60s)"
    except Exception as e:
        return False, f"Bootstrap error: {e}"
