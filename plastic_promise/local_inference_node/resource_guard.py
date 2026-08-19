"""Runtime resource admission for the isolated compute node.

The compute node must not compete with an operator's game, renderer, or
another accelerator workload.  This guard is deliberately read-only: it
observes aggregate GPU utilization when ``nvidia-smi`` is available and
returns a bounded defer decision.  It never kills another process and never
changes host scheduling state.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceDecision:
    """One redacted admission decision."""

    allowed: bool
    state: str
    reason: str | None
    gpu_utilization_percent: float | None
    retry_after_seconds: int

    def public_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "gpu_utilization_percent": self.gpu_utilization_percent,
            "retry_after_seconds": self.retry_after_seconds,
        }


class NodeResourceGuard:
    """Bounded, cached GPU admission for inference requests."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        gpu_utilization_limit: float = 70.0,
        sample_ttl_seconds: float = 2.0,
        retry_after_seconds: int = 5,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("node_resource_guard_enabled_invalid")
        if (
            isinstance(gpu_utilization_limit, bool)
            or not isinstance(gpu_utilization_limit, (int, float))
            or not math.isfinite(float(gpu_utilization_limit))
            or not 1.0 <= float(gpu_utilization_limit) <= 100.0
        ):
            raise ValueError("node_resource_gpu_utilization_limit_invalid")
        if (
            isinstance(sample_ttl_seconds, bool)
            or not isinstance(sample_ttl_seconds, (int, float))
            or not math.isfinite(float(sample_ttl_seconds))
            or not 0.1 <= float(sample_ttl_seconds) <= 60.0
        ):
            raise ValueError("node_resource_sample_ttl_invalid")
        if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int):
            raise ValueError("node_resource_retry_after_invalid")
        if not 1 <= retry_after_seconds <= 300:
            raise ValueError("node_resource_retry_after_invalid")
        self.enabled = enabled
        self.gpu_utilization_limit = float(gpu_utilization_limit)
        self.sample_ttl_seconds = float(sample_ttl_seconds)
        self.retry_after_seconds = retry_after_seconds
        self._cached_at = 0.0
        self._cached_decision = ResourceDecision(
            allowed=True,
            state="unknown",
            reason="resource_probe_not_run",
            gpu_utilization_percent=None,
            retry_after_seconds=retry_after_seconds,
        )
        self._sample_lock = asyncio.Lock()

    async def admit(self, *, active: int) -> ResourceDecision:
        """Return a bounded decision for the current node state.

        ``active`` describes work already owned by this node.  Existing work
        is never interrupted by a later resource sample; callers admitting a
        *new* request must use :meth:`admit_new_request` instead.
        """

        if not self.enabled:
            return ResourceDecision(
                True, "disabled", "operator_override", None, self.retry_after_seconds
            )
        if isinstance(active, bool) or not isinstance(active, int) or active < 0:
            raise ValueError("node_resource_active_invalid")
        # Once this node owns a slot, do not reject the in-flight operation
        # because its own CUDA kernels raised aggregate utilization.
        if active > 0:
            return ResourceDecision(
                True, "owned", "active_inference", None, self.retry_after_seconds
            )
        return await self.admit_new_request()

    async def admit_new_request(self) -> ResourceDecision:
        """Check external contention for one request that has not started."""

        if not self.enabled:
            return ResourceDecision(
                True, "disabled", "operator_override", None, self.retry_after_seconds
            )
        now = time.monotonic()
        if now - self._cached_at <= self.sample_ttl_seconds:
            return self._cached_decision
        async with self._sample_lock:
            now = time.monotonic()
            if now - self._cached_at > self.sample_ttl_seconds:
                self._cached_decision = await asyncio.to_thread(self._sample)
                self._cached_at = now
        return self._cached_decision

    async def snapshot(self, *, active: int) -> ResourceDecision:
        """Health projection using the same admission policy as requests."""

        return await self.admit(active=active)

    def _sample(self) -> ResourceDecision:
        if not self.enabled:
            return ResourceDecision(
                True, "disabled", "operator_override", None, self.retry_after_seconds
            )
        try:
            completed = subprocess.run(
                (
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits",
                ),
                capture_output=True,
                check=False,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            # CPU variants and hosts without NVIDIA tooling remain usable.
            return ResourceDecision(
                True, "unknown", "gpu_probe_unavailable", None, self.retry_after_seconds
            )
        if completed.returncode != 0:
            return ResourceDecision(
                True, "unknown", "gpu_probe_unavailable", None, self.retry_after_seconds
            )
        try:
            values = [float(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
            utilization = max(values)
        except (TypeError, ValueError):
            return ResourceDecision(
                True, "unknown", "gpu_probe_invalid", None, self.retry_after_seconds
            )
        if not math.isfinite(utilization) or not 0.0 <= utilization <= 100.0:
            return ResourceDecision(
                True, "unknown", "gpu_probe_invalid", None, self.retry_after_seconds
            )
        if utilization >= self.gpu_utilization_limit:
            return ResourceDecision(
                False,
                "degraded",
                "external_gpu_overloaded",
                utilization,
                self.retry_after_seconds,
            )
        return ResourceDecision(True, "ready", None, utilization, self.retry_after_seconds)


def resource_guard_from_environment(values: object) -> NodeResourceGuard:
    """Build a guard from a string mapping without exposing secrets."""

    def get(name: str, default: str) -> str:
        value = values.get(name, default) if hasattr(values, "get") else default
        return str(value).strip() if value is not None else default

    raw_enabled = get("PP_LOCAL_NODE_RESOURCE_GUARD", "on").casefold()
    if raw_enabled in {"1", "true", "yes", "on"}:
        enabled = True
    elif raw_enabled in {"0", "false", "no", "off"}:
        enabled = False
    else:
        raise ValueError("pp_local_node_resource_guard_invalid")
    try:
        limit = float(get("PP_LOCAL_NODE_RESOURCE_GPU_UTILIZATION_LIMIT", "70"))
        ttl = float(get("PP_LOCAL_NODE_RESOURCE_SAMPLE_TTL_SECONDS", "2"))
        retry = int(get("PP_LOCAL_NODE_RESOURCE_RETRY_AFTER_SECONDS", "5"))
    except (TypeError, ValueError) as exc:
        raise ValueError("pp_local_node_resource_config_invalid") from exc
    return NodeResourceGuard(
        enabled=enabled,
        gpu_utilization_limit=limit,
        sample_ttl_seconds=ttl,
        retry_after_seconds=retry,
    )
