"""Independent monotonic deadlines for maintenance jobs."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence


class AdaptiveThrottle:
    """Mutable interval that backs off after repeated empty scans."""

    def __init__(self, base_seconds: int):
        if type(base_seconds) is not int or base_seconds <= 0:
            raise ValueError("base_seconds must be a positive integer")
        self.base = base_seconds
        self.current = base_seconds
        self.empty_streak = 0

    def on_empty(self) -> None:
        self.empty_streak += 1
        if self.empty_streak >= 3:
            self.current = min(self.current * 2, self.base * 8)

    def on_hit(self) -> None:
        self.empty_streak = 0
        self.current = self.base

    @property
    def should_run(self) -> bool:
        return True


@dataclass
class MaintenanceDeadline:
    name: str
    interval: AdaptiveThrottle
    next_deadline: float
    runner: Callable[[], Awaitable[Any] | Any]
    last_outcome: Mapping[str, Any] | None = None


class MaintenanceRegistry:
    """Run each due job once and advance its own absolute deadline."""

    def __init__(self, jobs: Sequence[MaintenanceDeadline]):
        names = [job.name for job in jobs]
        if len(names) != len(set(names)):
            raise ValueError("duplicate maintenance deadline")
        self.jobs = list(jobs)

    async def run_due(self, now: float) -> tuple[Mapping[str, Any], ...]:
        if not math.isfinite(now):
            raise ValueError("maintenance clock must be finite")
        outcomes: list[Mapping[str, Any]] = []
        for job in self.jobs:
            if now < job.next_deadline:
                continue
            try:
                value = job.runner()
                if inspect.isawaitable(value):
                    value = await value
                outcome: Mapping[str, Any] = {
                    "name": job.name,
                    "status": "success",
                    "result": value,
                }
            except Exception as exc:
                outcome = {
                    "name": job.name,
                    "status": "error",
                    "error_class": exc.__class__.__name__,
                }
            job.last_outcome = outcome
            outcomes.append(outcome)
            self._advance_past(job, now)
        return tuple(outcomes)

    def next_delay(self, now: float, *, maximum: float = 10.0) -> float:
        if maximum < 0:
            raise ValueError("maximum delay must be non-negative")
        if not self.jobs:
            return maximum
        due_in = min(job.next_deadline - now for job in self.jobs)
        return min(maximum, max(0.0, due_in))

    @staticmethod
    def _advance_past(job: MaintenanceDeadline, now: float) -> None:
        interval = float(job.interval.current)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("maintenance interval must be positive and finite")
        elapsed = max(0.0, now - job.next_deadline)
        periods = math.floor(elapsed / interval) + 1
        job.next_deadline += periods * interval
