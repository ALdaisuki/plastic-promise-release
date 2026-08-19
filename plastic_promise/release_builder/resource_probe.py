"""Read-only, fail-closed resource observation for Release Builder entry points.

The probe deliberately performs no Docker mutation.  It samples for a full
window before a caller creates Buildx, cleans cache, or starts an image build.
Its JSON result contains aggregate capacity signals only—never process command
lines, credentials, endpoints, model paths, or database content.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING

from .contracts import ReleaseBuilderError, validate_windows_source_root
from .resource_gate import MIN_SAMPLE_SECONDS, ResourceSnapshot, evaluate_resource_gate

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class ResourceProbeError(RuntimeError):
    """Stable failure where an admission decision cannot be measured safely."""


@dataclass(frozen=True)
class ResourceSample:
    """One redacted instantaneous capacity sample."""

    cpu_percent: float
    available_ram_bytes: int
    gpu_utilization_percent: float
    gpu_vram_used_bytes: int
    gpu_temperature_celsius: float
    active_buildkit_build: bool
    inference_or_model_lock: bool
    disk_free_bytes: int
    disk_total_bytes: int


def aggregate_resource_samples(
    samples: Sequence[ResourceSample],
    *,
    observed_at: datetime,
    sample_seconds: int,
) -> ResourceSnapshot:
    """Make a conservative window snapshot from redacted instantaneous samples."""

    if not samples:
        raise ResourceProbeError("resource_probe_no_samples")
    return ResourceSnapshot(
        observed_at=observed_at.astimezone(UTC),
        sample_seconds=sample_seconds,
        cpu_average_percent=fmean(sample.cpu_percent for sample in samples),
        available_ram_bytes=min(sample.available_ram_bytes for sample in samples),
        gpu_average_utilization_percent=fmean(sample.gpu_utilization_percent for sample in samples),
        gpu_vram_used_bytes=max(sample.gpu_vram_used_bytes for sample in samples),
        gpu_temperature_celsius=max(sample.gpu_temperature_celsius for sample in samples),
        active_buildkit_build=any(sample.active_buildkit_build for sample in samples),
        inference_or_model_lock=any(sample.inference_or_model_lock for sample in samples),
        d_drive_free_bytes=min(sample.disk_free_bytes for sample in samples),
        d_drive_total_bytes=min(sample.disk_total_bytes for sample in samples),
    )


def observe_resource_window(
    disk_path: Path,
    *,
    window_seconds: int = MIN_SAMPLE_SECONDS,
    interval_seconds: float = 1.0,
    sampler: Callable[[Path], ResourceSample] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ResourceSnapshot:
    """Observe continuously for the required window without changing Docker state."""

    if window_seconds < MIN_SAMPLE_SECONDS:
        raise ResourceProbeError("resource_sample_window_too_short")
    if interval_seconds <= 0:
        raise ResourceProbeError("resource_sample_interval_invalid")
    collect = sampler or sample_system_resources
    started_at = monotonic()
    samples = [collect(disk_path)]
    while monotonic() - started_at < window_seconds:
        remaining = window_seconds - (monotonic() - started_at)
        sleeper(min(interval_seconds, remaining))
        samples.append(collect(disk_path))
    elapsed = max(window_seconds, int(monotonic() - started_at))
    return aggregate_resource_samples(samples, observed_at=now(), sample_seconds=elapsed)


def sample_system_resources(disk_path: Path) -> ResourceSample:
    """Collect one portable, read-only capacity sample or fail closed."""

    disk = shutil.disk_usage(disk_path)
    cpu_percent, available_ram_bytes = _sample_cpu_and_memory()
    gpu_utilization, gpu_vram_bytes, gpu_temperature = _sample_gpu()
    return ResourceSample(
        cpu_percent=cpu_percent,
        available_ram_bytes=available_ram_bytes,
        gpu_utilization_percent=gpu_utilization,
        gpu_vram_used_bytes=gpu_vram_bytes,
        gpu_temperature_celsius=gpu_temperature,
        active_buildkit_build=_buildkit_is_active(),
        inference_or_model_lock=_inference_or_model_lock_present(),
        disk_free_bytes=disk.free,
        disk_total_bytes=disk.total,
    )


def _sample_cpu_and_memory() -> tuple[float, int]:
    if os.name == "nt":
        payload = _powershell_json(
            "[pscustomobject]@{"
            "cpu=(Get-Counter '\\Processor(_Total)\\% Processor Time').CounterSamples[0].CookedValue;"
            "os=(Get-CimInstance Win32_OperatingSystem)"
            "} | Select-Object cpu,@{n='free_kib';e={$_.os.FreePhysicalMemory}}"
        )
        return float(payload["cpu"]), int(payload["free_kib"]) * 1024
    try:
        memory = Path("/proc/meminfo").read_text(encoding="utf-8")
        available_line = next(
            line for line in memory.splitlines() if line.startswith("MemAvailable:")
        )
        available_kib = int(available_line.split()[1])
        load_one = float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except (OSError, StopIteration, ValueError) as exc:
        raise ResourceProbeError("resource_probe_cpu_memory_unavailable") from exc
    return min(100.0, 100.0 * load_one / max(1, os.cpu_count() or 1)), available_kib * 1024


def _sample_gpu() -> tuple[float, int, float]:
    completed = _run(
        (
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
            "--format=csv,noheader,nounits",
        )
    )
    if completed is None or completed.returncode != 0 or not completed.stdout.strip():
        raise ResourceProbeError("resource_probe_gpu_unavailable")
    try:
        rows = [row.split(",") for row in completed.stdout.splitlines() if row.strip()]
        utilization = max(float(row[0].strip()) for row in rows)
        vram_bytes = max(int(float(row[1].strip()) * 1024 * 1024) for row in rows)
        temperature = max(float(row[2].strip()) for row in rows)
    except (IndexError, ValueError) as exc:
        raise ResourceProbeError("resource_probe_gpu_invalid") from exc
    return utilization, vram_bytes, temperature


def _buildkit_is_active() -> bool:
    completed = _run(("docker", "buildx", "history", "ls", "--format", "{{.Status}}"))
    if completed is None or completed.returncode != 0:
        return False
    return any("running" in status.casefold() for status in completed.stdout.splitlines())


def _inference_or_model_lock_present() -> bool:
    if os.name == "nt":
        payload = _powershell_json(
            "Get-CimInstance Win32_Process | Select-Object -ExpandProperty Name"
        )
        names = payload if isinstance(payload, list) else [payload]
    else:
        completed = _run(("ps", "-A", "-o", "comm="))
        if completed is None or completed.returncode != 0:
            raise ResourceProbeError("resource_probe_process_list_unavailable")
        names = completed.stdout.splitlines()
    markers = (
        "plastic-promise-local-inference",
        "llama-server",
        "ollama",
        "vllm",
        "text-generation",
    )
    return any(marker in str(name).casefold() for name in names for marker in markers)


def _powershell_json(script: str) -> object:
    completed = _run(
        ("powershell.exe", "-NoProfile", "-Command", f"{script} | ConvertTo-Json -Compress")
    )
    if completed is None or completed.returncode != 0 or not completed.stdout.strip():
        raise ResourceProbeError("resource_probe_powershell_unavailable")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ResourceProbeError("resource_probe_powershell_invalid") from exc


def _run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(arguments, capture_output=True, check=False, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plastic-promise-release-resource-gate")
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("resource-gate", help="Observe 10 seconds before a local build")
    gate.add_argument("--disk-path", type=Path, required=True)
    workspace = commands.add_parser(
        "validate-windows-source", help="Validate immutable Builder path"
    )
    workspace.add_argument("--path", required=True)
    workspace.add_argument("--source-revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-windows-source":
            source_root = validate_windows_source_root(args.path, args.source_revision)
            print(json.dumps({"status": "valid", "source_root": source_root}, sort_keys=True))
            return 0
        snapshot = observe_resource_window(args.disk_path)
        decision = evaluate_resource_gate(snapshot)
        payload = {
            "status": decision.status,
            "reason": decision.reason,
            "retry_queued": decision.retry_queued,
            "sample_seconds": snapshot.sample_seconds,
            "snapshot": {
                "cpu_average_percent": snapshot.cpu_average_percent,
                "available_ram_bytes": snapshot.available_ram_bytes,
                "gpu_average_utilization_percent": snapshot.gpu_average_utilization_percent,
                "gpu_vram_used_bytes": snapshot.gpu_vram_used_bytes,
                "gpu_temperature_celsius": snapshot.gpu_temperature_celsius,
                "active_buildkit_build": snapshot.active_buildkit_build,
                "inference_or_model_lock": snapshot.inference_or_model_lock,
                "disk_free_bytes": snapshot.d_drive_free_bytes,
                "disk_total_bytes": snapshot.d_drive_total_bytes,
            },
        }
        print(json.dumps(payload, sort_keys=True))
        return 0 if decision.status == "ready" else 75
    except (ReleaseBuilderError, ResourceProbeError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "deferred_resource_busy",
                    "reason": str(exc),
                    "retry_queued": False,
                },
                sort_keys=True,
            )
        )
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
