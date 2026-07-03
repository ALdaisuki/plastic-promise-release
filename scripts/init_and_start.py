"""Plastic Promise -- One-Click Launcher entry point.

Usage:
  python scripts/init_and_start.py                     # Start all services
  python scripts/init_and_start.py --skip-ollama-check  # Skip Ollama check
  python scripts/init_and_start.py --check-only         # Only check environment
  python scripts/init_and_start.py --stop               # Stop all running services
"""

import argparse
import asyncio
import os
import sys
import subprocess

# Path setup
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    RestartPolicy,
    ServiceStatus,
)
from plastic_promise.launcher.env_checker import run_env_checks
from plastic_promise.launcher.bootstrap_checker import check_bootstrap, run_bootstrap
from plastic_promise.launcher.service_manager import ServiceManager
from plastic_promise.launcher.watchdog import (
    setup_signal_handlers,
    watchdog_loop,
)

# Service definitions
SERVICES = [
    ServiceDefinition(
        name="mcp-server",
        command=[sys.executable, "-m", "plastic_promise", "--sse", "9020"],
        health_url="http://127.0.0.1:9020/health",
        startup_timeout=15.0,
        health_check_interval=5.0,
        restart_policy=RestartPolicy(max_retries=5, window_seconds=60.0),
    ),
    ServiceDefinition(
        name="maintenance-daemon",
        command=[sys.executable, "daemons/maintenance_daemon.py"],
        health_url=None,
        startup_timeout=10.0,
        health_check_interval=10.0,
        depends_on=["mcp-server"],
        restart_policy=RestartPolicy(max_retries=5, window_seconds=120.0),
    ),
]

LOG_FILE = os.path.join(_project_root, "var", "log", "init_and_start.log")
PID_FILE = os.path.join(_project_root, "var", "run", "maintenance_daemon.pid")

BANNER = """\
==============================================================
  Plastic Promise -- One-Click Launcher v0.1.0
=============================================================="""


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive (cross-platform)."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def do_stop():
    """Stop all running services. Returns True if --stop was handled."""
    if "--stop" not in sys.argv:
        return False

    print("Stopping all running Plastic Promise services...")
    killed = 0

    # Kill by PID file first (most reliable)
    for pid_path in [PID_FILE]:
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
                else:
                    os.kill(pid, 15)
                killed += 1
                print(f"  Stopped PID {pid} (from {os.path.basename(pid_path)})")
            except Exception:
                pass

    # Fallback: kill any remaining Python processes matching service names
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/FI", "IMAGENAME eq python.exe"],
            capture_output=True,
        )
    else:
        subprocess.run(["pkill", "-f", "plastic_promise.mcp.server"], capture_output=True)
        subprocess.run(["pkill", "-f", "maintenance_daemon.py"], capture_output=True)

    # Cleanup files
    for fname in ["maintenance_daemon.pid", "maintenance_daemon.heartbeat"]:
        path = os.path.join(_project_root, "var", "run", fname)
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass

    print(f"All services stopped ({killed} via PID file).")
    return True


async def main():
    print(BANNER)

    # Set default paths for subprocess inheritance (overridable by env vars)
    if "PLASTIC_DB_PATH" not in os.environ:
        os.environ["PLASTIC_DB_PATH"] = os.path.join(_project_root, "data", "db", "plastic_memory.db")
    if "PLASTIC_LANCEDB_PATH" not in os.environ:
        os.environ["PLASTIC_LANCEDB_PATH"] = os.path.join(_project_root, "data", "lancedb")

    args = parse_args()

    # Clean up stale PID files from previous crashed sessions
    for pid_path in [PID_FILE]:
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                if not _pid_alive(pid):
                    print(f"[INIT]  Stale PID file cleaned (PID {pid} not alive)")
                    os.unlink(pid_path)
            except Exception:
                try:
                    os.unlink(pid_path)
                except OSError:
                    pass

    # Verify daemon script exists before attempting to start it
    daemon_path = os.path.join(_project_root, "daemons", "maintenance_daemon.py")
    if not os.path.exists(daemon_path):
        print(f"[WARN]  Daemon script not found: {daemon_path}")

    # Clear log on new start
    try:
        with open(LOG_FILE, "w") as f:
            f.write(f"[{__import__('datetime').datetime.now().isoformat()}] Launcher starting\n")
    except Exception:
        pass

    # Environment checks
    all_ok, messages, mcp_already_running = run_env_checks(
        skip_ollama=args.skip_ollama_check
    )
    for msg in messages:
        print(msg)

    if args.check_only:
        if all_ok:
            print("\nAll environment checks passed.")
        else:
            print("\nSome environment checks failed. Fix before starting.")
        return

    if not all_ok:
        print("\nEnvironment checks failed. Fix issues or use --skip-ollama-check.")
        sys.exit(1)

    # --- Adjust services if MCP is already running ---
    services_to_start = list(SERVICES)
    if mcp_already_running:
        print("\n[INFO]  MCP Server already running on port 9020 —"
              " starting only Maintenance Daemon.")
        # Keep only non-mcp-server services, strip mcp-server dependency
        services_to_start = []
        for svc in SERVICES:
            if svc.name == "mcp-server":
                continue
            # Remove dependency on the already-running MCP
            new_deps = [d for d in svc.depends_on if d != "mcp-server"]
            if new_deps != svc.depends_on:
                svc.depends_on = new_deps
            services_to_start.append(svc)
    # ----------------------------------------------------

    # Bootstrap
    db_path = os.environ.get("PLASTIC_DB_PATH",
                              os.path.join(_project_root, "data", "db", "plastic_memory.db"))
    needs, bootstrap_msg = check_bootstrap(db_path)
    if needs:
        print(f"\n[INIT]  Bootstrap ..................... {bootstrap_msg}")
        ok, result_msg = await run_bootstrap(_project_root)
        status = "done" if ok else "FAILED"
        print(f"[INIT]  Bootstrap ..................... {status} ({result_msg})")
        if not ok:
            sys.exit(1)
    else:
        print(f"\n[INIT]  Bootstrap ..................... {bootstrap_msg}")

    # Start services
    manager = ServiceManager(services_to_start, _project_root)
    setup_signal_handlers(manager, LOG_FILE)

    await manager.start_all(LOG_FILE)

    statuses = manager.get_status()
    healthy = sum(1 for s in statuses.values() if s == ServiceStatus.HEALTHY)
    total = len(services_to_start)

    print(f"\n{'=' * 60}")
    if mcp_already_running:
        print(f"  MCP Server (already running) + {healthy}/{total} daemon running.")
    else:
        print(f"  {healthy}/{total} services running.")
    print(f"  Dashboard: http://127.0.0.1:9020/dashboard")
    print(f"  Press Ctrl+C to stop all services.")
    print(f"{'=' * 60}\n")

    if healthy == 0:
        print("No services started successfully. Check var/log/init_and_start.log.")
        sys.exit(1)

    # Watchdog loop (blocks until shutdown)
    await watchdog_loop(manager, LOG_FILE)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Plastic Promise One-Click Launcher"
    )
    parser.add_argument(
        "--skip-ollama-check",
        action="store_true",
        help="Skip Ollama availability check (degraded mode: no LLM classification)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only run environment checks, do not start services",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop all running services and clean up",
    )
    return parser.parse_args()


if __name__ == "__main__":
    if not do_stop():
        asyncio.run(main())
