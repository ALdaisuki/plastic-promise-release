"""Bounded execution for step-closure post_task calls."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("plastic-promise.closure")

_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


@dataclass
class ClosureRun:
    completed: bool
    result: dict[str, Any] | None = None
    timed_out: bool = False
    skipped: bool = False
    timeout_s: float = 0.0
    reason: str = ""


def _bounded_float_env(name: str, default: float, low: float = 0.05, high: float = 30.0) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(low, min(value, high))


def _max_background() -> int:
    raw = os.environ.get("PP_STEP_CLOSURE_MAX_BACKGROUND", "1")
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(0, min(value, 8))


def _reserve_slot() -> bool:
    global _IN_FLIGHT
    limit = _max_background()
    if limit <= 0:
        return False
    with _IN_FLIGHT_LOCK:
        if limit <= _IN_FLIGHT:
            return False
        _IN_FLIGHT += 1
        return True


def _release_slot() -> None:
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT = max(0, _IN_FLIGHT - 1)


def _post_task_call(kwargs: dict[str, Any]) -> dict[str, Any]:
    from plastic_promise.loop.soul_loop import post_task

    return post_task(**kwargs)


def _run_in_background(kwargs: dict[str, Any], results: queue.Queue[tuple[str, Any]]) -> None:
    try:
        results.put(("result", _post_task_call(kwargs)))
    except Exception as exc:
        results.put(("error", exc))
        logger.warning("background post_task failed: %s", exc)
    finally:
        _release_slot()


async def run_post_task_best_effort(**kwargs: Any) -> ClosureRun:
    """Run post_task with bounded caller latency.

    Coroutine timeouts cannot stop blocking DB/vector work once it is running in
    a thread. Use a bounded daemon worker so the caller returns on time and the
    closure finishes or fails independently.
    """

    timeout_s = _bounded_float_env("PP_STEP_CLOSURE_TIMEOUT_SEC", default=3.0)
    if not _reserve_slot():
        return ClosureRun(
            completed=False,
            skipped=True,
            timeout_s=timeout_s,
            reason="step closure already in progress",
        )

    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_run_in_background,
        args=(dict(kwargs), results),
        name="plastic-promise-step-closure",
        daemon=True,
    )
    worker.start()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            kind, value = results.get_nowait()
        except queue.Empty:
            await _sleep_briefly()
            continue
        if kind == "result":
            return ClosureRun(completed=True, result=value, timeout_s=timeout_s)
        return ClosureRun(completed=False, timeout_s=timeout_s, reason=str(value))

    try:
        kind, value = results.get_nowait()
    except queue.Empty:
        return ClosureRun(
            completed=False,
            timed_out=True,
            timeout_s=timeout_s,
            reason=f"post_task exceeded {timeout_s:.2f}s",
        )
    if kind == "result":
        return ClosureRun(completed=True, result=value, timeout_s=timeout_s)
    return ClosureRun(completed=False, timeout_s=timeout_s, reason=str(value))


async def _sleep_briefly() -> None:
    import asyncio

    await asyncio.sleep(0.01)
