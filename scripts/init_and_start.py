"""Plastic Promise -- One-Click Launcher entry point.

Usage:
  python scripts/init_and_start.py                     # Start all services
  python scripts/init_and_start.py --skip-ollama-check  # Skip Ollama check
  python scripts/init_and_start.py --check-only         # Only check environment
  python scripts/init_and_start.py --stop               # Stop all running services
"""

import argparse
import asyncio
import ctypes
import json
import os
import re
import shlex
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

# Path setup
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

# Imports intentionally follow project-root injection for direct script execution.
# ruff: noqa: E402
from plastic_promise import __version__
from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.core.task_recovery import release_stale_claims
from plastic_promise.launcher.bootstrap_checker import check_bootstrap, run_bootstrap
from plastic_promise.launcher.default_environment import configure_default_environment
from plastic_promise.launcher.env_checker import run_env_checks
from plastic_promise.launcher.runtime_mode import (
    RUNTIME_MODE_KEYS,
    apply_runtime_mode,
    select_runtime_mode,
)
from plastic_promise.launcher.service_definition import (
    RestartPolicy,
    ServiceDefinition,
    ServiceStatus,
)
from plastic_promise.launcher.service_manager import (
    ServiceManager,
    read_maintenance_health,
    resolve_source_revision,
)
from plastic_promise.launcher.subprocess_utils import hidden_subprocess_kwargs
from plastic_promise.launcher.watchdog import (
    setup_signal_handlers,
    watchdog_loop,
)

# Service definitions
SERVICES = [
    ServiceDefinition(
        name="mcp-server",
        command=[
            sys.executable,
            "-m",
            "plastic_promise",
            "--streamable-http",
            "9020",
            "--source-root",
            _project_root,
        ],
        health_url="http://127.0.0.1:9020/health",
        startup_timeout=15.0,
        health_check_interval=5.0,
        restart_policy=RestartPolicy(max_retries=5, window_seconds=60.0),
    ),
    ServiceDefinition(
        name="maintenance-daemon",
        command=[sys.executable, os.path.join(_project_root, "daemons", "maintenance_daemon.py")],
        health_url=None,
        startup_timeout=180.0,
        health_check_interval=10.0,
        depends_on=["mcp-server"],
        restart_policy=RestartPolicy(max_retries=5, window_seconds=120.0),
    ),
]

LOG_FILE = os.path.join(_project_root, "var", "log", "init_and_start.log")
PID_FILE = os.path.join(_project_root, "var", "run", "maintenance_daemon.pid")
MCP_PID_FILE = os.path.join(_project_root, "var", "run", "mcp_server.pid")
LANCEDB_WARMUP_ENV = {
    "PLASTIC_MCP_TRANSPORT": "streamable_http",
    "LDB_INIT_ON_HEAVY_INIT": "1",
    "LDB_BACKFILL_ON_INIT": "1",
    "LDB_REBUILD_ON_INIT": "1",
}

BANNER = f"""\
==============================================================
  Plastic Promise -- One-Click Launcher v{__version__}
=============================================================="""


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive (cross-platform)."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
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


def _is_managed_service_command_line(command_line: str | None) -> bool:
    """Return True for Plastic Promise service processes managed by this launcher."""
    normalized = (command_line or "").replace("\\", "/").lower()
    if not normalized:
        return False
    normalized_spaced = f" {normalized} "

    starts_streamable_mcp = (
        " -m plastic_promise " in normalized_spaced
        or " -m plastic_promise." in normalized_spaced
        or "plastic_promise.mcp.server" in normalized
    ) and any(flag in normalized for flag in ("--streamable-http", "--http", "--sse"))
    starts_maintenance_daemon = "maintenance_daemon.py" in normalized
    return starts_streamable_mcp or starts_maintenance_daemon


def _windows_python_processes() -> list[dict[str, object]]:
    """Return Windows python/pythonw processes with command lines, best effort."""
    command = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return []

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _windows_process_by_pid(pid: int) -> dict[str, object] | None:
    command = (
        "Get-CimInstance Win32_Process "
        f'-Filter "ProcessId = {pid}" | '
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return None

    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _windows_command_line_to_argv(command_line: str) -> list[str]:
    """Parse a Windows command line using the operating system's argv rules."""
    if not command_line:
        return []
    if not hasattr(ctypes, "windll"):
        return [value.strip('"') for value in shlex.split(command_line, posix=False)]
    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command_line, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(argv)


def _process_argv_by_pid(pid: int) -> list[str]:
    """Read an existing process argv without trusting a recycled PID."""
    try:
        import psutil

        argv = psutil.Process(pid).cmdline()
        if argv:
            return [str(value) for value in argv]
    except (ImportError, OSError):
        pass
    except Exception:
        pass

    if sys.platform == "win32":
        process = _windows_process_by_pid(pid)
        return _windows_command_line_to_argv(str((process or {}).get("CommandLine") or ""))

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="surrogateescape") for part in raw.split(b"\0") if part]


def _daemon_argv_options(argv: list[str]) -> dict[str, str | bool] | None:
    """Parse only the maintenance daemon's declared CLI surface."""
    value_options = {"--mcp-url", "--source-root", "--source-revision"}
    flag_options = {"--once", "--json"}
    parsed: dict[str, str | bool] = {}
    index = 2
    while index < len(argv):
        token = argv[index]
        name, separator, inline_value = token.partition("=")
        if name in flag_options and not separator:
            if name in parsed:
                return None
            parsed[name] = True
            index += 1
            continue
        if name not in value_options or name in parsed:
            return None
        if separator:
            value = inline_value
            index += 1
        elif index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
        else:
            return None
        if not value:
            return None
        parsed[name] = value
    return parsed


def _argv_matches_owned_service(
    argv: list[str],
    *,
    source_root: str,
    service_name: str,
    expected_source_revision: str | None = None,
) -> bool:
    """Match launcher-owned MCP/daemon argv using exact canonical path values."""
    if not argv:
        return False
    executable_name = os.path.basename(argv[0]).lower()
    if re.fullmatch(r"python(?:w|\d+(?:\.\d+)?)?(?:\.exe)?", executable_name) is None:
        return False
    canonical_root = os.path.normcase(os.path.realpath(os.path.abspath(source_root)))
    if service_name == "mcp-server":
        if (
            len(argv) != 7
            or argv[1] != "-m"
            or argv[2] != "plastic_promise"
            or argv[3] != "--streamable-http"
            or argv[4] != "9020"
            or argv[5] != "--source-root"
        ):
            return False
        declared_root = argv[6]
        return bool(
            declared_root
            and os.path.normcase(os.path.realpath(os.path.abspath(declared_root))) == canonical_root
        )
    if service_name == "maintenance-daemon":
        expected_script = os.path.normcase(
            os.path.realpath(os.path.join(canonical_root, "daemons", "maintenance_daemon.py"))
        )
        script_matches = bool(
            len(argv) >= 2
            and argv[1].lower().endswith(".py")
            and os.path.normcase(os.path.realpath(os.path.abspath(argv[1]))) == expected_script
        )
        if not script_matches:
            return False
        options = _daemon_argv_options(argv)
        if options is None:
            return False
        declared_root = options.get("--source-root")
        declared_revision = options.get("--source-revision")
        if bool(declared_root) != bool(declared_revision):
            return False
        if declared_root and (
            os.path.normcase(os.path.realpath(os.path.abspath(str(declared_root))))
            != canonical_root
        ):
            return False
        if expected_source_revision is None:
            return not any(options.get(flag) for flag in ("--once", "--json"))
        return bool(declared_root and declared_revision == expected_source_revision)
    return False


def _pid_matches_owned_service(
    pid: int,
    *,
    source_root: str,
    service_name: str,
    expected_source_revision: str | None = None,
) -> bool:
    return _argv_matches_owned_service(
        _process_argv_by_pid(pid),
        source_root=source_root,
        service_name=service_name,
        expected_source_revision=expected_source_revision,
    )


def _maintenance_heartbeat_path() -> str:
    return os.path.join(_project_root, "var", "run", "maintenance_daemon.heartbeat")


def _launcher_start_lock_path() -> str:
    return os.path.join(_project_root, "var", "run", "launcher-start.lock")


def _acquire_launcher_start_lock():
    """Acquire a cross-process lock spanning daemon inspection and spawn."""
    path = Path(_launcher_start_lock_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError):
        handle.close()
        return None
    return handle


def _release_launcher_start_lock(handle) -> None:
    if handle is None:
        return
    with suppress(OSError, ValueError):
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _process_create_time(pid: int) -> float | None:
    """Return the OS creation time for the current PID incarnation."""
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except (ImportError, OSError, ValueError):
        pass
    except Exception:
        pass

    if sys.platform == "win32":
        command = (
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($p) { $p.CreationDate.ToUniversalTime().ToString('o') }"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            value = completed.stdout.strip()
            if completed.returncode == 0 and value:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return None

    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        clock_ticks = os.sysconf("SC_CLK_TCK")
        boot_line = next(
            line
            for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        boot_time = float(boot_line.split()[1])
        return boot_time + (float(stat_fields[21]) / float(clock_ticks))
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def _inspect_existing_daemon(source_revision: str | None = None) -> dict[str, object]:
    """Classify an existing daemon PID before the launcher may spawn another."""
    if not os.path.exists(PID_FILE):
        return {"status": "absent", "pid": None, "reason": "pid_file_missing"}
    try:
        pid = int(Path(PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return {"status": "stale", "pid": None, "reason": "pid_file_invalid"}
    if not _pid_alive(pid):
        return {"status": "stale", "pid": pid, "reason": "maintenance_pid_not_alive"}
    if not _pid_matches_owned_service(
        pid,
        source_root=_project_root,
        service_name="maintenance-daemon",
        expected_source_revision=source_revision or resolve_source_revision(_project_root),
    ):
        return {"status": "conflict", "pid": pid, "reason": "maintenance_pid_not_owned"}
    heartbeat_path = Path(_maintenance_heartbeat_path())
    try:
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        heartbeat = {}
    generation = heartbeat.get("process_generation") if isinstance(heartbeat, dict) else None
    try:
        heartbeat_time = datetime.fromisoformat(str(heartbeat["updated_at"]).replace("Z", "+00:00"))
        if heartbeat_time.tzinfo is None:
            heartbeat_time = heartbeat_time.replace(tzinfo=timezone.utc)
        heartbeat_timestamp = heartbeat_time.timestamp()
    except (KeyError, TypeError, ValueError):
        heartbeat_timestamp = None
    process_create_time = _process_create_time(pid)
    if (
        process_create_time is None
        or heartbeat_timestamp is None
        or heartbeat_timestamp < process_create_time
    ):
        return {
            "status": "conflict",
            "pid": pid,
            "reason": "maintenance_pid_incarnation_mismatch",
        }
    health = read_maintenance_health(
        heartbeat_path,
        expected_pid=pid,
        expected_process_generation=str(generation or ""),
    )
    if health.get("healthy") is True:
        return {"status": "reuse", "pid": pid, "reason": "ok"}
    return {
        "status": "conflict",
        "pid": pid,
        "reason": str(health.get("reason") or "maintenance_health_invalid"),
    }


def _select_services_to_start(
    *,
    mcp_already_running: bool,
    daemon_already_running: bool,
    source_revision: str | None = None,
) -> list[ServiceDefinition]:
    """Return fresh definitions for only the services this launcher must own."""
    selected: list[ServiceDefinition] = []
    for service in SERVICES:
        if service.name == "mcp-server" and mcp_already_running:
            continue
        if service.name == "maintenance-daemon" and daemon_already_running:
            continue
        dependencies = [
            dependency
            for dependency in service.depends_on
            if not (dependency == "mcp-server" and mcp_already_running)
        ]
        command = list(service.command)
        if service.name == "maintenance-daemon":
            revision = source_revision or resolve_source_revision(_project_root)
            if revision is None:
                raise ValueError("maintenance_source_revision_unavailable")
            command.extend(["--source-root", _project_root, "--source-revision", revision])
        selected.append(
            replace(
                service,
                command=command,
                depends_on=dependencies,
                pre_start=list(service.pre_start),
                env=dict(service.env),
            )
        )
    return selected


def _stop_windows_pid(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            **hidden_subprocess_kwargs(),
        )
        return completed.returncode == 0
    except Exception:
        return False


def run_lancedb_warmup_maintenance():
    """Warm LanceDB and run its existing backfill/rebuild maintenance once."""
    previous = {key: os.environ.get(key) for key in LANCEDB_WARMUP_ENV}
    try:
        os.environ.update(LANCEDB_WARMUP_ENV)
        engine = ContextEngine()
        engine._ensure_heavy_init()
        row_count = engine._ldb.count_rows() if getattr(engine, "_ldb", None) is not None else 0
        sync_status = getattr(engine, "_lancedb_sync_status", None)
        sync_msg = ""
        if isinstance(sync_status, dict):
            if sync_status.get("success") is False:
                sync_msg = f", sync=degraded:{sync_status.get('error', 'unknown')}"
            else:
                sync_msg = (
                    f", sync=orphans:{sync_status.get('orphan_deleted', 0)}"
                    f" missing:{sync_status.get('missing_backfilled', 0)}"
                    f" skipped:{sync_status.get('missing_skipped', 0)}"
                )
        return True, f"ready ({row_count} rows{sync_msg})"
    except Exception as exc:
        return False, str(exc)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_startup_recovery():
    """Run best-effort storage/task recovery before services start."""
    try:
        result = release_stale_claims()
        released = result.get("released_count", 0)
        escalated = result.get("escalated_count", 0)
        return True, f"stale_claims_released={released}, escalated={escalated}"
    except Exception as exc:
        return False, str(exc)


def do_stop():
    """Stop all running services. Returns True if --stop was handled."""
    if "--stop" not in sys.argv:
        return False

    print("Stopping all running Plastic Promise services...")
    killed = 0
    preserve_runtime_files: set[str] = set()

    # Kill by PID file first (most reliable)
    for pid_path, service_name in (
        (MCP_PID_FILE, "mcp-server"),
        (PID_FILE, "maintenance-daemon"),
    ):
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                if not _pid_matches_owned_service(
                    pid,
                    source_root=_project_root,
                    service_name=service_name,
                ):
                    print(
                        f"  Skipped PID {pid} (from {os.path.basename(pid_path)}; "
                        "not owned by this checkout)"
                    )
                    preserve_runtime_files.add(os.path.abspath(pid_path))
                    if service_name == "maintenance-daemon":
                        preserve_runtime_files.add(os.path.abspath(_maintenance_heartbeat_path()))
                    continue
                if sys.platform == "win32":
                    if not _stop_windows_pid(pid):
                        preserve_runtime_files.add(os.path.abspath(pid_path))
                        if service_name == "maintenance-daemon":
                            preserve_runtime_files.add(
                                os.path.abspath(_maintenance_heartbeat_path())
                            )
                        continue
                else:
                    os.kill(pid, 15)
                killed += 1
                print(f"  Stopped PID {pid} (from {os.path.basename(pid_path)})")
            except Exception:
                preserve_runtime_files.add(os.path.abspath(pid_path))
                if service_name == "maintenance-daemon":
                    preserve_runtime_files.add(os.path.abspath(_maintenance_heartbeat_path()))

    # Cleanup files
    for fname in ["mcp_server.pid", "maintenance_daemon.pid", "maintenance_daemon.heartbeat"]:
        path = os.path.join(_project_root, "var", "run", fname)
        if os.path.abspath(path) in preserve_runtime_files:
            continue
        if os.path.exists(path):
            with suppress(OSError):
                os.unlink(path)

    print(f"All services stopped ({killed} matching process(es)).")
    return True


async def main():
    print(BANNER)

    # Set default paths for subprocess inheritance (overridable by env vars)
    configure_default_environment(_project_root)

    args = parse_args()

    if args.stop:
        do_stop()
        return

    source_revision = resolve_source_revision(_project_root)
    if source_revision is None:
        print("[ERROR] Cannot resolve the current Git source revision; refusing to start.")
        sys.exit(1)

    runtime_mode = None
    if not args.check_only:
        try:
            runtime_mode = select_runtime_mode(args.mode)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(2)
        apply_runtime_mode(runtime_mode)
        print(f"[INIT]  Runtime mode ................. {runtime_mode.label} ({runtime_mode.key})")
    elif args.mode:
        try:
            runtime_mode = apply_runtime_mode(args.mode)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(2)
        print(
            f"[INIT]  Runtime mode ................. {runtime_mode.label} "
            f"({runtime_mode.key}; check-only)"
        )

    # Verify daemon script exists before attempting to start it
    daemon_path = os.path.join(_project_root, "daemons", "maintenance_daemon.py")
    if not os.path.exists(daemon_path):
        print(f"[WARN]  Daemon script not found: {daemon_path}")

    # Environment checks
    all_ok, messages, mcp_already_running = run_env_checks(
        skip_ollama=args.skip_ollama_check,
        include_mcp_status=True,
        expected_source_root=_project_root,
        expected_source_revision=source_revision,
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

    # Clear log only for a real launch; --check-only remains filesystem read-only.
    try:
        with open(LOG_FILE, "w") as f:
            f.write(f"[{datetime.now().isoformat()}] Launcher starting\n")
    except Exception:
        pass

    launcher_start_lock = _acquire_launcher_start_lock()
    if launcher_start_lock is None:
        print(
            "[ERROR] Another launcher is inspecting or starting services; "
            "refusing a concurrent daemon spawn."
        )
        sys.exit(1)

    daemon_state = _inspect_existing_daemon(source_revision)
    if daemon_state["status"] == "stale":
        print(
            "[INIT]  Stale daemon state cleaned "
            f"({daemon_state['reason']}; PID={daemon_state['pid']})"
        )
        for path in (PID_FILE, _maintenance_heartbeat_path()):
            with suppress(OSError):
                os.unlink(path)
    elif daemon_state["status"] == "conflict":
        _release_launcher_start_lock(launcher_start_lock)
        print(
            "[ERROR] Existing maintenance daemon state is not reusable: "
            f"{daemon_state['reason']} (PID={daemon_state['pid']}). "
            "Refusing to start a second daemon."
        )
        sys.exit(1)
    daemon_already_running = daemon_state["status"] == "reuse"
    if daemon_already_running:
        print(
            "[INIT]  Maintenance Daemon ........... already running "
            f"(owned PID {daemon_state['pid']})"
        )

    # --- Adjust services that this launcher instance must own ---
    if daemon_already_running and not mcp_already_running:
        _release_launcher_start_lock(launcher_start_lock)
        print(
            "[ERROR] An owned Maintenance Daemon is running without the expected MCP identity; "
            "refusing a mixed-generation launch."
        )
        sys.exit(1)
    services_to_start = _select_services_to_start(
        mcp_already_running=mcp_already_running,
        daemon_already_running=daemon_already_running,
        source_revision=source_revision,
    )
    if mcp_already_running:
        action = (
            "reusing owned Maintenance Daemon"
            if daemon_already_running
            else "starting Maintenance Daemon"
        )
        print(f"\n[INFO]  MCP Server already running on port 9020; {action}.")
    # ----------------------------------------------------

    # Bootstrap
    db_path = os.environ.get(
        "PLASTIC_DB_PATH", os.path.join(_project_root, "data", "db", "plastic_memory.db")
    )
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

    recovery_ok, recovery_msg = run_startup_recovery()
    recovery_status = "ready" if recovery_ok else "degraded"
    print(f"[INIT]  Startup recovery .............. {recovery_status} ({recovery_msg})")

    # LanceDB warmup / maintenance
    if args.skip_lancedb_warmup or os.environ.get("PLASTIC_SKIP_LANCEDB_WARMUP") == "1":
        reason = "requested"
        if runtime_mode is not None and not runtime_mode.runs_lancedb_warmup:
            reason = f"mode {runtime_mode.label}"
        print(f"[INIT]  LanceDB warmup/maintenance ... skipped ({reason})")
    else:
        ok, warmup_msg = run_lancedb_warmup_maintenance()
        status = "ready" if ok else "degraded"
        print(f"[INIT]  LanceDB warmup/maintenance ... {status} ({warmup_msg})")

    # Start services
    if not services_to_start:
        _release_launcher_start_lock(launcher_start_lock)
        print("\nAll requested services are already running with matching ownership evidence.")
        return
    manager = ServiceManager(
        services_to_start,
        _project_root,
        expected_source_revision=source_revision,
    )
    setup_signal_handlers(manager, LOG_FILE)

    await manager.start_all(LOG_FILE)
    _release_launcher_start_lock(launcher_start_lock)

    statuses = manager.get_status()
    healthy = sum(1 for s in statuses.values() if s == ServiceStatus.HEALTHY)
    total = len(services_to_start)

    print(f"\n{'=' * 60}")
    if mcp_already_running:
        print(f"  MCP Server (already running) + {healthy}/{total} daemon running.")
    else:
        print(f"  {healthy}/{total} services running.")
    print("  Dashboard: http://127.0.0.1:9020/dashboard")
    print("  Press Ctrl+C to stop all services.")
    print(f"{'=' * 60}\n")

    if healthy == 0:
        print("No services started successfully. Check var/log/init_and_start.log.")
        sys.exit(1)

    # Watchdog loop (blocks until shutdown)
    await watchdog_loop(manager, LOG_FILE)


def parse_args():
    parser = argparse.ArgumentParser(description="Plastic Promise One-Click Launcher")
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
    parser.add_argument(
        "--skip-lancedb-warmup",
        action="store_true",
        help="Skip LanceDB warmup/maintenance before services start",
    )
    parser.add_argument(
        "--mode",
        help=(
            "Startup mode. Valid modes: "
            + ", ".join(RUNTIME_MODE_KEYS)
            + ". If omitted in an interactive terminal, the launcher asks before starting."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    if not do_stop():
        asyncio.run(main())
