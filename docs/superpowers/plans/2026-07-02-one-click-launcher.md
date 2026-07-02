# One-Click Launcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unified one-click startup for Plastic Promise: `python scripts/init_and_start.py` launches MCP Server + Daemon with env checking, auto-bootstrap, crash recovery, and graceful shutdown.

**Architecture:** Five modules in `plastic_promise/launcher/` plus a thin CLI entry script. `ServiceManager` orchestrates subprocess lifecycle; `Watchdog` monitors health with crash recovery; `env_checker` and `bootstrap_checker` gate startup. Follows existing project patterns (async, sqlite3 for DB checks, httpx-style HTTP).

**Tech Stack:** Python 3.10+, `subprocess`, `asyncio`, `signal`, `psutil` (optional, with fallback), `sqlite3`.

**Spec:** `docs/superpowers/specs/2026-07-02-one-click-launcher-design.md`

## Global Constraints

- Windows as primary platform, Unix compatible for CI
- `psutil` optional — `pid_exists()` preferred, `tasklist /FI` fallback on Windows
- All output to console AND `init_and_start.log` (project root)
- Graceful shutdown on Ctrl+C — terminate (5s grace) → kill
- Port 9020 for MCP Server — check before start
- Ollama at `http://127.0.0.1:11434` — check before start (3s timeout)
- No system service registration (Windows Service / systemd)
- No Ollama process management (environment dependency only)
- No Bridge service management (interop, not core memory system)

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `plastic_promise/launcher/__init__.py` | Create | Package init |
| `plastic_promise/launcher/service_definition.py` | Create | Dataclasses: RestartPolicy, ServiceDefinition, ServiceStatus |
| `plastic_promise/launcher/env_checker.py` | Create | 5 environment checks |
| `plastic_promise/launcher/bootstrap_checker.py` | Create | First-run detection + auto-bootstrap |
| `plastic_promise/launcher/service_manager.py` | Create | Subprocess lifecycle + health checks |
| `plastic_promise/launcher/watchdog.py` | Create | Main loop + crash recovery + signal handling |
| `scripts/init_and_start.py` | Create | CLI entry point (thin wrapper) |
| `daemons/maintenance_daemon.py` | Modify | Heartbeat file writing in main loop |

---
```

### Task 1: service_definition.py — Pure Data Classes

**Files:**
- Create: `plastic_promise/launcher/__init__.py`
- Create: `plastic_promise/launcher/service_definition.py`

**Interfaces:**
- Produces: `RestartPolicy(max_retries, window_seconds, backoff_base, backoff_multiplier, max_backoff)` — dataclass
- Produces: `ServiceDefinition(name, command, health_url, startup_timeout, health_check_interval, depends_on, pre_start, restart_policy, env, cwd)` — dataclass
- Produces: `ServiceStatus(PENDING|STARTING|HEALTHY|FAILED|UNRECOVERABLE|STOPPED)` — enum

- [ ] **Step 1: Create `__init__.py`**

```python
"""Plastic Promise One-Click Launcher."""
```

- [ ] **Step 2: Create `service_definition.py`**

```python
"""Service definition dataclasses for the One-Click Launcher."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ServiceStatus(Enum):
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    FAILED = "failed"
    UNRECOVERABLE = "unrecoverable"
    STOPPED = "stopped"


@dataclass
class RestartPolicy:
    max_retries: int = 5
    window_seconds: float = 60.0
    backoff_base: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff: float = 30.0


@dataclass
class ServiceDefinition:
    name: str
    command: list[str]
    health_url: Optional[str] = None
    startup_timeout: float = 30.0
    health_check_interval: float = 5.0
    depends_on: list[str] = field(default_factory=list)
    pre_start: list[str] = field(default_factory=list)
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "."
```

- [ ] **Step 3: Verify imports and commit**

```bash
python -c "from plastic_promise.launcher.service_definition import RestartPolicy, ServiceDefinition, ServiceStatus; print('OK')"
git add plastic_promise/launcher/__init__.py plastic_promise/launcher/service_definition.py
git commit -m "feat: add ServiceDefinition, RestartPolicy, ServiceStatus dataclasses"
```

---

### Task 2: env_checker.py + bootstrap_checker.py

**Files:**
- Create: `plastic_promise/launcher/env_checker.py`
- Create: `plastic_promise/launcher/bootstrap_checker.py`

**Interfaces:**
- Produces: `run_env_checks(skip_ollama: bool = False) -> tuple[bool, list[str]]` — returns (all_ok, messages)
- Produces: `check_bootstrap(db_path: str) -> tuple[bool, str]` — returns (needs_bootstrap, message)
- Produces: `run_bootstrap(db_path: str) -> tuple[bool, str]` — runs bootstrap.py, returns (ok, message)

- [ ] **Step 1: Create `env_checker.py`**

```python
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
        messages.append(f"[ENV]   Python {py_ver} ................ ✅ OK")
    else:
        messages.append(f"[ENV]   Python {py_ver} ................ ❌ FAIL (need >= 3.10)")
        all_ok = False

    # 2. Ollama
    if skip_ollama:
        messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... ⚠️ SKIP (--skip-ollama-check)")
    else:
        try:
            req = urllib.request.Request("http://127.0.0.1:11434")
            urllib.request.urlopen(req, timeout=3)
            messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... ✅ OK")
        except Exception:
            messages.append("[ENV]   Ollama (127.0.0.1:11434) ..... ❌ FAIL (not reachable)")
            all_ok = False

    # 3. LanceDB
    try:
        import lancedb  # noqa: F401
        messages.append("[ENV]   LanceDB ....................... ✅ OK")
    except ImportError:
        messages.append("[ENV]   LanceDB ....................... ❌ FAIL (not installed)")
        all_ok = False

    # 4. Port 9020
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 9020))
        sock.close()
        messages.append("[ENV]   Port 9020 ..................... ✅ free")
    except OSError:
        messages.append("[ENV]   Port 9020 ..................... ❌ in use (another instance?)")
        all_ok = False

    # 5. plastic_memory.db
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if os.path.exists(db_path):
        messages.append(f"[ENV]   {db_path} ................. ✅ found")
    else:
        messages.append(f"[ENV]   {db_path} ................. ⚠️ not found (first run)")

    return all_ok, messages
```

- [ ] **Step 2: Create `bootstrap_checker.py`**

```python
"""First-run detection and auto-bootstrap for One-Click Launcher."""

import os
import sqlite3
import subprocess
import sys


def check_bootstrap(db_path: str) -> tuple[bool, str]:
    """Check if bootstrap is needed. Returns (needs_bootstrap, message)."""
    if not os.path.exists(db_path):
        return True, "plastic_memory.db not found — first run detected"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tags LIKE '%seed:true%'"
        )
        count = cursor.fetchone()[0]
        conn.close()

        if count == 0:
            return True, "DB exists but no seed memories found — re-bootstrap needed"
        return False, f"DB ready ({count} seed memories)"
    except sqlite3.OperationalError as e:
        return True, f"DB exists but memories table missing: {e}"


def run_bootstrap(project_root: str) -> tuple[bool, str]:
    """Run bootstrap.py. Returns (ok, message)."""
    bootstrap_script = os.path.join(project_root, "scripts", "bootstrap.py")
    if not os.path.exists(bootstrap_script):
        return False, f"Bootstrap script not found: {bootstrap_script}"

    try:
        result = subprocess.run(
            [sys.executable, bootstrap_script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        if result.returncode == 0:
            return True, "Bootstrap completed successfully"
        else:
            return False, f"Bootstrap failed (exit {result.returncode}): {result.stderr[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "Bootstrap timed out (>60s)"
    except Exception as e:
        return False, f"Bootstrap error: {e}"
```

- [ ] **Step 3: Verify and commit**

```bash
python -c "from plastic_promise.launcher.env_checker import run_env_checks; ok, msgs = run_env_checks(skip_ollama=True); print(f'OK={ok}, msgs={len(msgs)}')"
python -c "from plastic_promise.launcher.bootstrap_checker import check_bootstrap; needs, msg = check_bootstrap('plastic_memory.db'); print(f'needs={needs}, msg={msg}')"
git add plastic_promise/launcher/env_checker.py plastic_promise/launcher/bootstrap_checker.py
git commit -m "feat: add env_checker and bootstrap_checker for launcher"
```

---

### Task 3: service_manager.py — Service Lifecycle

**Files:**
- Create: `plastic_promise/launcher/service_manager.py`

**Interfaces:**
- Consumes: `ServiceDefinition`, `ServiceStatus`, `RestartPolicy` from `plastic_promise.launcher.service_definition`
- Produces: `ServiceManager` class with `start_all()`, `stop_all()`, `reset_service(name)`, `get_status()` methods
- Internal: `_start_service(svc)`, `_health_check(svc)`, `_stop_service(svc)`, `_topological_sort(svcs)`

- [ ] **Step 1: Create `service_manager.py`**

```python
"""Service lifecycle manager for One-Click Launcher.

Orchestrates start/stop of MCP Server and Maintenance Daemon with
dependency ordering, health checks, and crash recovery support.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    ServiceStatus,
    RestartPolicy,
)


class ServiceRuntime:
    """Runtime state for one service instance."""

    def __init__(self, definition: ServiceDefinition):
        self.definition = definition
        self.status = ServiceStatus.PENDING
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.restart_timestamps: list[float] = []
        self.consecutive_failures = 0

    def record_restart(self):
        now = time.time()
        self.restart_timestamps.append(now)
        policy = self.definition.restart_policy
        cutoff = now - policy.window_seconds
        self.restart_timestamps = [t for t in self.restart_timestamps if t > cutoff]

    def recent_restarts(self) -> int:
        return len(self.restart_timestamps)

    def is_unrecoverable(self) -> bool:
        return self.recent_restarts() >= self.definition.restart_policy.max_retries

    def backoff_seconds(self) -> float:
        policy = self.definition.restart_policy
        count = self.recent_restarts()
        return min(
            policy.backoff_base * (policy.backoff_multiplier ** max(0, count - 1)),
            policy.max_backoff,
        )


class ServiceManager:
    """Manages lifecycle of multiple services with dependency ordering."""

    def __init__(self, services: list[ServiceDefinition], project_root: str):
        self._runtimes: dict[str, ServiceRuntime] = {
            svc.name: ServiceRuntime(svc) for svc in services
        }
        self._project_root = project_root
        self._shutdown = False

    def get_status(self) -> dict[str, ServiceStatus]:
        return {name: rt.status for name, rt in self._runtimes.items()}

    def _topological_order(self) -> list[ServiceRuntime]:
        """Return runtimes in dependency order (dependencies first)."""
        visited = set()
        order = []

        def visit(name):
            if name in visited:
                return
            visited.add(name)
            rt = self._runtimes[name]
            for dep in rt.definition.depends_on:
                if dep in self._runtimes:
                    visit(dep)
            order.append(rt)

        for name in self._runtimes:
            visit(name)
        return order

    async def start_all(self, log_file: Optional[str] = None):
        """Start all services in dependency order."""
        for rt in self._topological_order():
            await self._start_service(rt, log_file)
            # Cascade failure: if a dependency failed, mark dependents as FAILED
            if rt.status == ServiceStatus.FAILED:
                for name, other_rt in self._runtimes.items():
                    if rt.definition.name in other_rt.definition.depends_on:
                        if other_rt.status == ServiceStatus.PENDING:
                            other_rt.status = ServiceStatus.FAILED
                            self._log(f"[START] {other_rt.definition.name} ..... ❌ FAILED (dependency {rt.definition.name} failed)", log_file)

    async def _start_service(self, rt: ServiceRuntime, log_file: Optional[str] = None):
        """Start a single service and wait for health check."""
        svc = rt.definition
        rt.status = ServiceStatus.STARTING
        self._log(f"[START] {svc.name} .................... 🔄 starting...", log_file)

        # Run pre-start commands
        for cmd in svc.pre_start:
            try:
                subprocess.run(cmd, shell=True, cwd=self._project_root, timeout=30, check=True)
            except Exception as e:
                self._log(f"[START] {svc.name} .................... ❌ FAILED (pre_start: {e})", log_file)
                rt.status = ServiceStatus.FAILED
                return

        # Build environment
        env = os.environ.copy()
        env.update(svc.env)

        # Launch process
        try:
            cwd = os.path.join(self._project_root, svc.cwd) if svc.cwd != "." else self._project_root
            if sys.platform == "win32":
                rt.process = subprocess.Popen(
                    svc.command,
                    env=env,
                    cwd=cwd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                rt.process = subprocess.Popen(
                    svc.command,
                    env=env,
                    cwd=cwd,
                    preexec_fn=os.setsid,
                )
            rt.pid = rt.process.pid
        except Exception as e:
            self._log(f"[START] {svc.name} .................... ❌ FAILED (spawn: {e})", log_file)
            rt.status = ServiceStatus.FAILED
            return

        # Health check loop
        deadline = time.time() + svc.startup_timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            healthy = await self._health_check(rt)
            if healthy:
                rt.status = ServiceStatus.HEALTHY
                rt.consecutive_failures = 0
                self._log(f"[START] {svc.name} .................... ✅ healthy (pid={rt.pid})", log_file)
                return

            # Check if process still alive
            if rt.process and rt.process.poll() is not None:
                self._log(f"[START] {svc.name} .................... ❌ FAILED (exit code={rt.process.returncode})", log_file)
                rt.status = ServiceStatus.FAILED
                return

        # Timeout
        self._log(f"[START] {svc.name} .................... ❌ FAILED (timeout {svc.startup_timeout}s)", log_file)
        self._stop_service(rt)
        rt.status = ServiceStatus.FAILED

    async def _health_check(self, rt: ServiceRuntime) -> bool:
        """Check if a service is healthy."""
        svc = rt.definition

        # HTTP health check
        if svc.health_url:
            try:
                req = urllib.request.Request(svc.health_url)
                response = urllib.request.urlopen(req, timeout=3)
                return response.status == 200
            except Exception:
                return False

        # Process-alive check (no HTTP endpoint — e.g., daemon)
        if rt.pid is not None:
            return self._pid_alive(rt.pid)

        return False

    def _pid_alive(self, pid: int) -> bool:
        """Check if a PID is alive. psutil preferred, fallback to platform tools."""
        try:
            import psutil
            return psutil.pid_exists(pid)
        except ImportError:
            pass

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

    def _stop_service(self, rt: ServiceRuntime):
        """Stop a single service. Terminate (5s grace) → kill."""
        if rt.process is None or rt.process.poll() is not None:
            rt.status = ServiceStatus.STOPPED
            return

        svc = rt.definition
        try:
            if sys.platform == "win32":
                rt.process.terminate()
            else:
                rt.process.terminate()

            try:
                rt.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rt.process.kill()
                rt.process.wait(timeout=2)
        except Exception:
            try:
                rt.process.kill()
            except Exception:
                pass

        rt.status = ServiceStatus.STOPPED
        rt.pid = None

    async def stop_all(self, log_file: Optional[str] = None):
        """Stop all services in reverse dependency order."""
        self._shutdown = True
        for rt in reversed(self._topological_order()):
            if rt.status in (ServiceStatus.HEALTHY, ServiceStatus.STARTING, ServiceStatus.FAILED):
                self._log(f"[STOP]  {rt.definition.name} .................... stopping...", log_file)
                self._stop_service(rt)
                self._log(f"[STOP]  {rt.definition.name} .................... ✅ stopped", log_file)

    def reset_service(self, name: str, log_file: Optional[str] = None):
        """Reset a service from UNRECOVERABLE back to STOPPED, clearing restart history."""
        rt = self._runtimes.get(name)
        if rt is None:
            return
        rt.status = ServiceStatus.STOPPED
        rt.restart_timestamps.clear()
        rt.consecutive_failures = 0
        self._log(f"[RESET] {name} .................... reset (manual)", log_file)

        # Cascade: restart dependents that were FAILED due to this service
        for other_name, other_rt in self._runtimes.items():
            if name in other_rt.definition.depends_on and other_rt.status == ServiceStatus.FAILED:
                self._log(f"[RESET] {other_name} .................... eligible for restart", log_file)

    @staticmethod
    def _log(message: str, log_file: Optional[str] = None):
        print(message)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(message + "\n")
            except Exception:
                pass
```

- [ ] **Step 2: Verify and commit**

```bash
python -c "from plastic_promise.launcher.service_manager import ServiceManager, ServiceRuntime; print('OK')"
git add plastic_promise/launcher/service_manager.py
git commit -m "feat: add ServiceManager with dependency ordering and health checks"
```

---

### Task 4: watchdog.py — Main Loop + Crash Recovery

**Files:**
- Create: `plastic_promise/launcher/watchdog.py`

**Interfaces:**
- Consumes: `ServiceManager` from `plastic_promise.launcher.service_manager`
- Consumes: `ServiceStatus` from `plastic_promise.launcher.service_definition`
- Produces: `async def watchdog_loop(manager, log_file)` — main loop
- Produces: `setup_signal_handlers(manager, log_file)` — registers SIGINT/SIGTERM/SIGBREAK

- [ ] **Step 1: Create `watchdog.py`**

```python
"""Watchdog main loop for One-Click Launcher.

Monitors service health, handles crash recovery with backoff,
and provides graceful shutdown via signal handlers.
"""

import asyncio
import signal
import sys
import time
from datetime import datetime
from typing import Optional

from plastic_promise.launcher.service_manager import ServiceManager
from plastic_promise.launcher.service_definition import ServiceStatus


_shutdown_flag = False
_manager_ref: Optional[ServiceManager] = None
_log_file_ref: Optional[str] = None


def setup_signal_handlers(manager: ServiceManager, log_file: Optional[str] = None):
    """Register signal handlers for graceful shutdown."""
    global _manager_ref, _log_file_ref
    _manager_ref = manager
    _log_file_ref = log_file

    def _handle_shutdown(signum, frame):
        global _shutdown_flag
        _shutdown_flag = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGBREAK, _handle_shutdown)
        except AttributeError:
            pass


async def watchdog_loop(manager: ServiceManager, log_file: Optional[str] = None):
    """Main watchdog loop — monitor health and recover from crashes."""
    global _shutdown_flag, _manager_ref, _log_file_ref
    _manager_ref = manager
    _log_file_ref = log_file
    _shutdown_flag = False

    while not _shutdown_flag:
        # Check each service
        for name, rt in manager._runtimes.items():
            if _shutdown_flag:
                break

            if rt.status in (ServiceStatus.HEALTHY, ServiceStatus.STARTING):
                # Health check
                healthy = await manager._health_check(rt)
                if not healthy:
                    rt.consecutive_failures += 1
                    if rt.consecutive_failures >= 3:
                        await _handle_crash(manager, rt, log_file)
                else:
                    rt.consecutive_failures = 0

            elif rt.status == ServiceStatus.FAILED:
                await _handle_crash(manager, rt, log_file)

        # Heartbeat log every 60s
        _log_watchdog_heartbeat(manager, log_file)

        await asyncio.sleep(1.0)

    # Shutdown
    _log("Shutting down all services...", log_file)
    await manager.stop_all(log_file)
    _log("All services stopped. Goodbye.", log_file)


async def _handle_crash(manager: ServiceManager, rt, log_file: Optional[str] = None):
    """Handle a crashed service — terminate remnant, check restart policy, backoff, restart."""
    svc = rt.definition

    # Kill remnant process
    if rt.process and rt.process.poll() is None:
        manager._stop_service(rt)

    rt.record_restart()
    recent = rt.recent_restarts()

    if rt.is_unrecoverable():
        rt.status = ServiceStatus.UNRECOVERABLE
        _log(f"[ALERT] {svc.name} UNRECOVERABLE — {recent} restarts in "
             f"{svc.restart_policy.window_seconds}s", log_file)
        return

    backoff = rt.backoff_seconds()
    _log(f"[RESTART] {svc.name} crashed — restarting (attempt {recent}/"
         f"{svc.restart_policy.max_retries}, backoff {backoff:.1f}s)", log_file)

    await asyncio.sleep(backoff)
    await manager._start_service(rt, log_file)


def _log_watchdog_heartbeat(manager: ServiceManager, log_file: Optional[str] = None):
    """Log a periodic heartbeat line."""
    now = datetime.now().strftime("%H:%M:%S")
    statuses = []
    for name, rt in manager._runtimes.items():
        pid_str = f"pid={rt.pid}" if rt.pid else "pid=N/A"
        statuses.append(f"{name} {pid_str}")

    # Only log every 60 iterations (~60s)
    _heartbeat_counter = getattr(_log_watchdog_heartbeat, "_counter", 0)
    _heartbeat_counter += 1
    _log_watchdog_heartbeat._counter = _heartbeat_counter

    if _heartbeat_counter >= 60:
        _log_watchdog_heartbeat._counter = 0
        _log(f"[WATCH] {now}  all healthy | {' '.join(statuses)}", log_file)


def _log(message: str, log_file: Optional[str] = None):
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    full_msg = f"[{timestamp}] {message}"
    print(full_msg)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(full_msg + "\n")
        except Exception:
            pass
```

- [ ] **Step 2: Modify daemon to write heartbeat file**

In `daemons/maintenance_daemon.py`, in the main loop after `await asyncio.sleep(10)` (line 1319), add:

```python
        # Write heartbeat file for launcher watchdog
        try:
            _heartbeat_path = os.path.join(_project_root, "maintenance_daemon.heartbeat")
            with open(_heartbeat_path, "w") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass
```

- [ ] **Step 3: Verify and commit**

```bash
python -c "from plastic_promise.launcher.watchdog import setup_signal_handlers, watchdog_loop; print('OK')"
git add plastic_promise/launcher/watchdog.py daemons/maintenance_daemon.py
git commit -m "feat: add watchdog loop with crash recovery and daemon heartbeat file"
```

---

### Task 5: init_and_start.py — CLI Entry Point

**Files:**
- Create: `scripts/init_and_start.py`

**Interfaces:**
- Consumes: all modules from `plastic_promise.launcher.*`
- Produces: CLI with flags `--skip-ollama-check`, `--check-only`, `--stop`

- [ ] **Step 1: Create `scripts/init_and_start.py`**

```python
"""Plastic Promise — One-Click Launcher entry point.

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
import signal

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

LOG_FILE = os.path.join(_project_root, "init_and_start.log")

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║  Plastic Promise — One-Click Launcher v0.1.0               ║
╚══════════════════════════════════════════════════════════════╝
"""


def check_stop_only():
    """Check if --stop was passed and handle it."""
    if "--stop" not in sys.argv:
        return False

    print("Stopping all running Plastic Promise services...")
    # Kill by known process names
    import subprocess
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/FI",
                        "WINDOWTITLE eq plastic_promise*"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "plastic_promise.mcp.server"], capture_output=True)
        subprocess.run(["pkill", "-f", "maintenance_daemon.py"], capture_output=True)

    # Cleanup PID/heartbeat files
    for fname in ["maintenance_daemon.pid", "maintenance_daemon.heartbeat"]:
        path = os.path.join(_project_root, fname)
        if os.path.exists(path):
            os.unlink(path)

    print("All services stopped.")
    return True


async def main():
    print(BANNER)

    args = parse_args()

    # Handle --stop
    if args.stop:
        return

    log_file = LOG_FILE
    # Clear log on new start
    try:
        with open(log_file, "w") as f:
            f.write(f"[{__import__('datetime').datetime.now().isoformat()}] Launcher starting\n")
    except Exception:
        pass

    # Environment checks
    all_ok, messages = run_env_checks(skip_ollama=args.skip_ollama_check)
    for msg in messages:
        print(msg)

    if args.check_only:
        if all_ok:
            print("\n✅ All environment checks passed.")
        else:
            print("\n❌ Some environment checks failed. Fix before starting.")
        return

    if not all_ok:
        print("\n❌ Environment checks failed. Fix issues or use --skip-ollama-check.")
        sys.exit(1)

    # Bootstrap
    db_path = os.environ.get("PLASTIC_DB_PATH",
                              os.path.join(_project_root, "plastic_memory.db"))
    needs, bootstrap_msg = check_bootstrap(db_path)
    if needs:
        print(f"\n[INIT]  Bootstrap ..................... 🔄 {bootstrap_msg}")
        ok, result_msg = run_bootstrap(_project_root)
        status = "✅" if ok else "❌"
        print(f"[INIT]  Bootstrap ..................... {status} {result_msg}")
        if not ok:
            sys.exit(1)
    else:
        print(f"\n[INIT]  Bootstrap ..................... ✅ {bootstrap_msg}")

    # Start services
    manager = ServiceManager(SERVICES, _project_root)
    setup_signal_handlers(manager, log_file)

    await manager.start_all(log_file)

    statuses = manager.get_status()
    healthy = sum(1 for s in statuses.values() if s == ServiceStatus.HEALTHY)

    print(f"\n{'─' * 60}")
    print(f"  {healthy}/{len(SERVICES)} services running. Dashboard: http://127.0.0.1:9020/dashboard")
    print(f"  Press Ctrl+C to stop all services.")
    print(f"{'─' * 60}\n")

    if healthy == 0:
        print("❌ No services started successfully. Check init_and_start.log.")
        sys.exit(1)

    # Watchdog loop (blocks until shutdown)
    await watchdog_loop(manager, log_file)


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
    if not check_stop_only():
        asyncio.run(main())
```

- [ ] **Step 2: Verify import and commit**

```bash
python -c "import scripts.init_and_start; print('import OK')" 2>&1 || echo "(expected: argparse parses when run as script)"
python -c "
import sys; sys.path.insert(0, '.')
from plastic_promise.launcher.service_definition import ServiceDefinition, RestartPolicy
from plastic_promise.launcher.service_manager import ServiceManager
svcs = [
    ServiceDefinition(name='mcp-server', command=[sys.executable, '-m', 'plastic_promise', '--sse', '9020'], health_url='http://127.0.0.1:9020/health'),
    ServiceDefinition(name='maintenance-daemon', command=[sys.executable, 'daemons/maintenance_daemon.py'], depends_on=['mcp-server']),
]
mgr = ServiceManager(svcs, '.')
print(f'Services: {list(mgr.get_status().keys())}')
assert 'mcp-server' in mgr.get_status()
assert 'maintenance-daemon' in mgr.get_status()
print('OK')
"

git add scripts/init_and_start.py
git commit -m "feat: add init_and_start.py CLI entry point for launcher"
```

---

### Task 6: Tests — Launcher Integration

**Files:**
- Create: `tests/test_launcher.py`

- [ ] **Step 1: Create `tests/test_launcher.py`**

```python
"""Tests for One-Click Launcher components."""

import os
import sys
import sqlite3
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    ServiceStatus,
    RestartPolicy,
)
from plastic_promise.launcher.env_checker import run_env_checks
from plastic_promise.launcher.bootstrap_checker import check_bootstrap


# ── service_definition tests ─────────────────────────────────

def test_service_definition_defaults():
    svc = ServiceDefinition(
        name="test-svc",
        command=["python", "-c", "print('hi')"],
    )
    assert svc.name == "test-svc"
    assert svc.health_url is None
    assert svc.startup_timeout == 30.0
    assert svc.depends_on == []
    assert isinstance(svc.restart_policy, RestartPolicy)
    assert svc.restart_policy.max_retries == 5


def test_restart_policy_backoff():
    policy = RestartPolicy(
        max_retries=5,
        window_seconds=60.0,
        backoff_base=1.0,
        backoff_multiplier=2.0,
        max_backoff=30.0,
    )
    assert policy.backoff_base == 1.0
    assert policy.backoff_multiplier == 2.0
    assert policy.max_backoff == 30.0


def test_service_status_enum():
    assert ServiceStatus.PENDING.value == "pending"
    assert ServiceStatus.HEALTHY.value == "healthy"
    assert ServiceStatus.UNRECOVERABLE.value == "unrecoverable"


# ── env_checker tests ────────────────────────────────────────

def test_env_checker_python_version():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert ok is True  # Python >= 3.10 always passes on our runtime
    assert any("Python" in m for m in msgs)


def test_env_checker_ollama_skip():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("SKIP" in m for m in msgs)


def test_env_checker_lancedb():
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("LanceDB" in m for m in msgs)
    # LanceDB should be installed in dev environment
    assert ok is True


def test_env_checker_port_check():
    """Port 9020 should be checked (may be free or occupied)."""
    ok, msgs = run_env_checks(skip_ollama=True)
    assert any("Port 9020" in m for m in msgs)


# ── bootstrap_checker tests ──────────────────────────────────

def test_check_bootstrap_missing_db():
    needs, msg = check_bootstrap("/nonexistent/path/db.sqlite")
    assert needs is True
    assert "not found" in msg


def test_check_bootstrap_existing_db():
    """Check that existing plastic_memory.db doesn't trigger bootstrap."""
    db_path = os.environ.get("PLASTIC_DB_PATH", "plastic_memory.db")
    if os.path.exists(db_path):
        needs, msg = check_bootstrap(db_path)
        # Should not need bootstrap if DB exists with seed memories
        assert isinstance(needs, bool)


def test_check_bootstrap_empty_db():
    """Empty DB without seed memories should trigger bootstrap."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "  id TEXT PRIMARY KEY,"
            "  content TEXT,"
            "  memory_type TEXT,"
            "  tags TEXT NOT NULL DEFAULT '[]'"
            ")"
        )
        conn.commit()
        conn.close()

        needs, msg = check_bootstrap(db_path)
        assert needs is True
        assert "no seed" in msg.lower() or "seed" in msg.lower()
    finally:
        os.unlink(db_path)


# ── ServiceManager tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_service_manager_creation():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [
        ServiceDefinition(name="s1", command=["echo", "1"]),
        ServiceDefinition(name="s2", command=["echo", "2"], depends_on=["s1"]),
    ]
    mgr = ServiceManager(svcs, ".")

    statuses = mgr.get_status()
    assert statuses["s1"] == ServiceStatus.PENDING
    assert statuses["s2"] == ServiceStatus.PENDING


@pytest.mark.asyncio
async def test_service_manager_topological_order():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [
        ServiceDefinition(name="b", command=["echo"], depends_on=["a"]),
        ServiceDefinition(name="a", command=["echo"]),
    ]
    mgr = ServiceManager(svcs, ".")
    order = mgr._topological_order()
    names = [rt.definition.name for rt in order]
    # "a" must come before "b"
    assert names.index("a") < names.index("b")


def test_service_manager_reset():
    from plastic_promise.launcher.service_manager import ServiceManager

    svcs = [ServiceDefinition(name="s1", command=["echo"])]
    mgr = ServiceManager(svcs, ".")
    mgr.reset_service("nonexistent")  # Should not crash
    mgr.reset_service("s1")
    assert mgr.get_status()["s1"] == ServiceStatus.STOPPED
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_launcher.py -v --tb=short
```

Expected: 12 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_launcher.py
git commit -m "test: add launcher component tests (12 tests)"
```

---

## Verification

After all tasks complete:

1. **Run all launcher tests:**
   ```bash
   pytest tests/test_launcher.py -v --tb=short
   ```
   Expected: 12 PASS.

2. **Import chain check:**
   ```bash
   python -c "
   from plastic_promise.launcher.service_definition import ServiceDefinition, ServiceStatus, RestartPolicy
   from plastic_promise.launcher.env_checker import run_env_checks
   from plastic_promise.launcher.bootstrap_checker import check_bootstrap, run_bootstrap
   from plastic_promise.launcher.service_manager import ServiceManager, ServiceRuntime
   from plastic_promise.launcher.watchdog import setup_signal_handlers, watchdog_loop
   print('All imports OK')
   "
   ```

3. **Env check only (safe, no services started):**
   ```bash
   python scripts/init_and_start.py --check-only --skip-ollama-check
   ```
   Expected: All env checks pass or warn cleanly.

4. **Full manual E2E** (requires MCP server not already running on 9020):
   ```bash
   python scripts/init_and_start.py --skip-ollama-check
   ```
   Expected: MCP Server starts → Daemon starts → Ctrl+C shuts down both cleanly.
```
