"""Side-effect-free storage preflight for a local heterogeneous node."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


GIBIBYTE = 1024**3
MINIMUM_POST_APPLY_FREE_BYTES = 10 * GIBIBYTE
MINIMUM_POST_APPLY_FREE_FRACTION = 0.20


@dataclass(frozen=True)
class NodeResourceEstimate:
    """Every byte class that must be reserved before a node apply may start."""

    image_download_bytes: int
    image_unpack_bytes: int
    model_artifact_bytes: int
    model_cache_bytes: int
    rollback_bytes: int
    existing_runtime_bytes: int

    def __post_init__(self) -> None:
        if any(value < 0 for value in self._values()):
            raise ValueError("node_resource_estimate_negative")

    @property
    def required_new_bytes(self) -> int:
        return sum(self._values())

    def _values(self) -> tuple[int, ...]:
        return (
            self.image_download_bytes,
            self.image_unpack_bytes,
            self.model_artifact_bytes,
            self.model_cache_bytes,
            self.rollback_bytes,
            self.existing_runtime_bytes,
        )


@dataclass(frozen=True)
class NodeResourcePreflight:
    """Preflight outcome that callers can serialize without executing an apply."""

    total_bytes: int
    free_bytes: int
    required_new_bytes: int
    required_post_apply_free_bytes: int
    projected_free_bytes: int
    allowed: bool


def assess_node_resources(path: Path, estimate: NodeResourceEstimate) -> NodeResourcePreflight:
    """Inspect a target filesystem and enforce ``max(20%, 10 GiB)`` free space."""

    usage = shutil.disk_usage(path)
    return evaluate_node_capacity(
        total_bytes=usage.total,
        free_bytes=usage.free,
        estimate=estimate,
    )


def evaluate_node_capacity(
    *, total_bytes: int, free_bytes: int, estimate: NodeResourceEstimate
) -> NodeResourcePreflight:
    """Pure variant used by installers and tests before touching a filesystem."""

    if total_bytes < 0 or free_bytes < 0 or free_bytes > total_bytes:
        raise ValueError("node_resource_capacity_invalid")
    required_free = max(
        MINIMUM_POST_APPLY_FREE_BYTES,
        int(total_bytes * MINIMUM_POST_APPLY_FREE_FRACTION),
    )
    projected_free = free_bytes - estimate.required_new_bytes
    return NodeResourcePreflight(
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        required_new_bytes=estimate.required_new_bytes,
        required_post_apply_free_bytes=required_free,
        projected_free_bytes=projected_free,
        allowed=projected_free >= required_free,
    )
