"""Service lifecycle manager for One-Click Launcher.

Orchestrates start/stop of MCP Server and Maintenance Daemon with
dependency ordering, health checks, and crash recovery support.
"""

import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from plastic_promise.launcher.service_definition import (
    ServiceDefinition,
    ServiceStatus,
)
from plastic_promise.launcher.subprocess_utils import hidden_subprocess_kwargs


class ServiceRuntime:
    """Runtime state for one service instance."""

    def __init__(self, definition: ServiceDefinition):
        self.definition = definition
        self.status = ServiceStatus.PENDING
        self.process: subprocess.Popen | None = None
        self.pid: int | None = None
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

    def get_status(self) -> dict[str, ServiceStatus]:
        return {name: rt.status for name, rt in self._runtimes.items()}

    def _topological_order(self) -> list[ServiceRuntime]:
        """Return runtimes in dependency order (dependencies first).
        Raises ValueError if a dependency cycle is detected.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self._runtimes, WHITE)
        order = []

        def visit(name: str, path: list[str]):
            color[name] = GRAY
            path.append(name)
            rt = self._runtimes[name]
            for dep in rt.definition.depends_on:
                if dep not in self._runtimes:
                    continue
                if color[dep] == GRAY:
                    cycle_start = path.index(dep)
                    cycle = " -> ".join(path[cycle_start:] + [dep])
                    raise ValueError(f"Circular dependency detected: {cycle}")
                if color[dep] == WHITE:
                    visit(dep, path)
            path.pop()
            color[name] = BLACK
            order.append(rt)

        for name in self._runtimes:
            if color[name] == WHITE:
                visit(name, [])

        return order  # post-order = dependencies before dependents

    async def start_all(self, log_file: str | None = None):
        """Start all services in dependency order."""
        for rt in self._topological_order():
            await self._start_service(rt, log_file)
            # Cascade failure
            if rt.status == ServiceStatus.FAILED:
                for name, other_rt in self._runtimes.items():
                    if rt.definition.name in other_rt.definition.depends_on:
                        if other_rt.status == ServiceStatus.PENDING:
                            other_rt.status = ServiceStatus.FAILED
                            self._log(
                                f"[START] {other_rt.definition.name} ..... FAILED"
                                f" (dependency {rt.definition.name} failed)",
                                log_file,
                            )

    async def _start_service(self, rt: ServiceRuntime, log_file: str | None = None):
        """Start a single service and wait for health check."""
        svc = rt.definition
        rt.status = ServiceStatus.STARTING
        self._log(f"[START] {svc.name} .................... starting...", log_file)

        # Run pre-start commands
        for cmd in svc.pre_start:
            try:
                subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self._project_root,
                    timeout=30,
                    check=True,
                    **hidden_subprocess_kwargs(),
                )
            except Exception as e:
                self._log(
                    f"[START] {svc.name} .................... FAILED (pre_start: {e})", log_file
                )
                rt.status = ServiceStatus.FAILED
                return

        # Build environment
        env = os.environ.copy()
        env.update(svc.env)

        # Launch process
        try:
            cwd = self._project_root
            kwargs: dict = {"env": env, "cwd": cwd}
            if sys.platform == "win32":
                kwargs.update(hidden_subprocess_kwargs(new_process_group=True))
            else:
                kwargs["preexec_fn"] = os.setsid
            rt.process = subprocess.Popen(svc.command, **kwargs)
            rt.pid = rt.process.pid
        except Exception as e:
            self._log(f"[START] {svc.name} .................... FAILED (spawn: {e})", log_file)
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
                self._log(
                    f"[START] {svc.name} .................... healthy (pid={rt.pid})", log_file
                )
                return

            # Check if process died during startup
            if rt.process and rt.process.poll() is not None:
                self._log(
                    f"[START] {svc.name} .................... FAILED"
                    f" (exit code={rt.process.returncode})",
                    log_file,
                )
                rt.status = ServiceStatus.FAILED
                return

        # Timeout
        self._log(
            f"[START] {svc.name} .................... FAILED (timeout {svc.startup_timeout}s)",
            log_file,
        )
        self.stop_service(rt.definition.name)
        rt.status = ServiceStatus.FAILED

    async def _health_check(self, rt: ServiceRuntime) -> bool:
        """Check if a service is healthy.

        Priority:
        1. HTTP health endpoint (200 OK only)
        2. Heartbeat file freshness (for daemon without HTTP endpoint)
        3. PID alive check (fallback)
        """
        svc = rt.definition

        # 1. HTTP health check
        if svc.health_url:
            try:
                req = urllib.request.Request(svc.health_url)
                response = urllib.request.urlopen(req, timeout=3)
                return response.status == 200
            except Exception:
                return False

        # 2. Heartbeat file check (for daemon)
        if svc.name == "maintenance-daemon":
            heartbeat_path = os.path.join(
                self._project_root, "var", "run", "maintenance_daemon.heartbeat"
            )
            if os.path.exists(heartbeat_path):
                try:
                    mtime = os.path.getmtime(heartbeat_path)
                    age = time.time() - mtime
                    if age < 120:  # fresh within 2 minutes
                        return True
                except OSError:
                    pass

        # 3. PID alive check
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

    def stop_service(self, name: str):
        """Public method: stop a single service by name."""
        rt = self._runtimes.get(name)
        if rt is not None:
            self._stop_service(rt)

    def _stop_service(self, rt: ServiceRuntime):
        """Stop a single service. Terminate (5s grace) -> kill."""
        if rt.process is None or rt.process.poll() is not None:
            rt.status = ServiceStatus.STOPPED
            rt.pid = None
            return

        try:
            if sys.platform == "win32":
                rt.process.terminate()
            else:
                rt.process.terminate()

            try:
                rt.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rt.process.kill()
                try:
                    rt.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            try:
                rt.process.kill()
            except Exception:
                pass

        rt.status = ServiceStatus.STOPPED
        rt.pid = None

    async def stop_all(self, log_file: str | None = None):
        """Stop all services in reverse dependency order."""
        for rt in reversed(self._topological_order()):
            if rt.status in (ServiceStatus.HEALTHY, ServiceStatus.STARTING, ServiceStatus.FAILED):
                self._log(
                    f"[STOP]  {rt.definition.name} .................... stopping...", log_file
                )
                self._stop_service(rt)
                self._log(f"[STOP]  {rt.definition.name} .................... stopped", log_file)

    def reset_service(self, name: str, log_file: str | None = None):
        """Reset a service from UNRECOVERABLE back to STOPPED, clearing restart history."""
        rt = self._runtimes.get(name)
        if rt is None:
            return
        rt.status = ServiceStatus.STOPPED
        rt.restart_timestamps.clear()
        rt.consecutive_failures = 0
        self._log(f"[RESET] {name} .................... reset (manual)", log_file)

        # Cascade: flag dependents as eligible for restart
        for other_name, other_rt in self._runtimes.items():
            if name in other_rt.definition.depends_on and other_rt.status == ServiceStatus.FAILED:
                self._log(
                    f"[RESET] {other_name} .................... eligible for restart", log_file
                )

    @staticmethod
    def _log(message: str, log_file: str | None = None):
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        full_msg = f"[{timestamp}] {message}"
        print(full_msg)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(full_msg + "\n")
            except Exception:
                pass
