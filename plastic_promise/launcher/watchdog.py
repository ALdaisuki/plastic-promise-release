"""Watchdog main loop for One-Click Launcher.

Monitors service health, handles crash recovery with backoff,
and provides graceful shutdown via signal handlers.
"""

import asyncio
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime

from plastic_promise.launcher.service_definition import ServiceStatus
from plastic_promise.launcher.service_manager import ServiceManager

_shutdown_flag = False
_heartbeat_counter = 0


def setup_signal_handlers(manager: ServiceManager, log_file: str | None = None):
    """Register signal handlers for graceful shutdown."""

    def _handle_shutdown(signum, frame):
        global _shutdown_flag
        _shutdown_flag = True

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    if sys.platform == "win32":
        with suppress(AttributeError):
            signal.signal(signal.SIGBREAK, _handle_shutdown)


async def watchdog_loop(manager: ServiceManager, log_file: str | None = None):
    """Main watchdog loop -- monitor health and recover from crashes."""
    global _shutdown_flag, _heartbeat_counter

    while not _shutdown_flag:
        # Check each service
        for rt in list(manager._runtimes.values()):
            if _shutdown_flag:
                break

            if rt.status == ServiceStatus.HEALTHY:
                healthy = await manager._health_check(rt)
                if not healthy:
                    now = time.monotonic()
                    if rt.first_unhealthy_at is None:
                        rt.first_unhealthy_at = now
                    rt.consecutive_failures += 1
                    if _should_restart_unhealthy_service(rt, now=now):
                        await _handle_crash(manager, rt, log_file)
                else:
                    rt.consecutive_failures = 0
                    rt.first_unhealthy_at = None

            elif rt.status == ServiceStatus.FAILED:
                await _handle_crash(manager, rt, log_file)

        # Heartbeat log every 60 iterations (~60s)
        _heartbeat_counter += 1
        if _heartbeat_counter >= 60:
            _heartbeat_counter = 0
            _log_watchdog_heartbeat(manager, log_file)

        await asyncio.sleep(1.0)

    # Shutdown
    _log("Shutting down all services...", log_file)
    await manager.stop_all(log_file)
    _log("All services stopped. Goodbye.", log_file)


async def _handle_crash(manager: ServiceManager, rt, log_file: str | None = None):
    """Handle a crashed service -- terminate remnant, backoff, restart."""
    svc = rt.definition

    # Kill remnant process via public stop_service
    if rt.process and rt.process.poll() is None:
        manager.stop_service(svc.name)

    rt.record_restart()
    recent = rt.recent_restarts()

    if rt.is_unrecoverable():
        rt.status = ServiceStatus.UNRECOVERABLE
        _log(
            f"[ALERT] {svc.name} UNRECOVERABLE -- {recent} restarts in "
            f"{svc.restart_policy.window_seconds}s, manual intervention needed",
            log_file,
        )
        return

    backoff = rt.backoff_seconds()
    _log(
        f"[RESTART] {svc.name} crashed -- restarting (attempt {recent}/"
        f"{svc.restart_policy.max_retries}, backoff {backoff:.1f}s)",
        log_file,
    )

    await asyncio.sleep(backoff)
    await manager._start_service(rt, log_file)


def _should_restart_unhealthy_service(rt, now: float | None = None) -> bool:
    """Return true when failed health checks justify a restart.

    A long-running MCP tool call can temporarily block the HTTP health endpoint
    while the child process is still alive. Treat that as unhealthy, not crashed,
    until the grace window expires.
    """
    if rt.process is not None and rt.process.poll() is not None:
        return True

    if rt.consecutive_failures < 3:
        return False

    first_unhealthy_at = getattr(rt, "first_unhealthy_at", None)
    if first_unhealthy_at is None:
        return False

    grace = getattr(rt.definition, "unhealthy_restart_grace_seconds", 180.0)
    return (time.monotonic() if now is None else now) - first_unhealthy_at >= grace


def _log_watchdog_heartbeat(manager: ServiceManager, log_file: str | None = None):
    """Log a periodic heartbeat line."""
    now = datetime.now().strftime("%H:%M:%S")
    statuses = []
    for name, rt in manager._runtimes.items():
        pid_str = f"pid={rt.pid}" if rt.pid else "pid=N/A"
        statuses.append(f"{name} {pid_str}")
    _log(f"[WATCH] {now}  all healthy | {' '.join(statuses)}", log_file)


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
