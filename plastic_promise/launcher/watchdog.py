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
_heartbeat_counter = 0


def setup_signal_handlers(manager: ServiceManager, log_file: Optional[str] = None):
    """Register signal handlers for graceful shutdown."""

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
    """Main watchdog loop -- monitor health and recover from crashes."""
    global _shutdown_flag, _heartbeat_counter

    while not _shutdown_flag:
        # Check each service
        for name, rt in list(manager._runtimes.items()):
            if _shutdown_flag:
                break

            if rt.status == ServiceStatus.HEALTHY:
                healthy = await manager._health_check(rt)
                if not healthy:
                    rt.consecutive_failures += 1
                    if rt.consecutive_failures >= 3:
                        await _handle_crash(manager, rt, log_file)
                else:
                    rt.consecutive_failures = 0

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


async def _handle_crash(manager: ServiceManager, rt, log_file: Optional[str] = None):
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


def _log_watchdog_heartbeat(manager: ServiceManager, log_file: Optional[str] = None):
    """Log a periodic heartbeat line."""
    now = datetime.now().strftime("%H:%M:%S")
    statuses = []
    for name, rt in manager._runtimes.items():
        pid_str = f"pid={rt.pid}" if rt.pid else "pid=N/A"
        statuses.append(f"{name} {pid_str}")
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
