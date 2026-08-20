"""Pure resource admission policy for a Windows desktop Release Builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

GIB = 1024**3
MIN_SAMPLE_SECONDS = 10
MIN_FREE_BYTES = 80 * GIB
MIN_FREE_FRACTION = 0.20


@dataclass(frozen=True)
class ResourceSnapshot:
    observed_at: datetime
    sample_seconds: int
    cpu_average_percent: float
    available_ram_bytes: int
    gpu_average_utilization_percent: float
    gpu_vram_used_bytes: int
    gpu_temperature_celsius: float
    active_buildkit_build: bool
    inference_or_model_lock: bool
    d_drive_free_bytes: int
    d_drive_total_bytes: int


@dataclass(frozen=True)
class ResourceGateDecision:
    status: str
    reason: str | None
    retry_queued: bool
    suggested_recheck_at: datetime | None


def evaluate_resource_gate(snapshot: ResourceSnapshot) -> ResourceGateDecision:
    """Return a deterministic, non-queuing admit/defer decision."""

    reason = _busy_reason(snapshot)
    if reason is None:
        return ResourceGateDecision("ready", None, False, None)
    observed_at = snapshot.observed_at.astimezone(UTC)
    return ResourceGateDecision(
        "deferred_resource_busy",
        reason,
        False,
        observed_at.replace(microsecond=0),
    )


def _busy_reason(snapshot: ResourceSnapshot) -> str | None:
    if snapshot.sample_seconds < MIN_SAMPLE_SECONDS:
        return "resource_sample_incomplete"
    if snapshot.cpu_average_percent >= 75.0:
        return "cpu_busy"
    if snapshot.available_ram_bytes < 8 * GIB:
        return "memory_low"
    if snapshot.gpu_average_utilization_percent >= 20.0:
        return "gpu_busy"
    if snapshot.gpu_vram_used_bytes >= 4 * GIB:
        return "gpu_vram_busy"
    if snapshot.gpu_temperature_celsius >= 75.0:
        return "gpu_temperature_high"
    if snapshot.active_buildkit_build:
        return "buildkit_active"
    if snapshot.inference_or_model_lock:
        return "plastic_promise_accelerator_busy"
    required_free = max(MIN_FREE_BYTES, int(snapshot.d_drive_total_bytes * MIN_FREE_FRACTION))
    if snapshot.d_drive_free_bytes < required_free:
        return "d_drive_space_low"
    return None
