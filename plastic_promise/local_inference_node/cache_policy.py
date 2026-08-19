"""Safe, side-effect-free cache-retention planning for local node models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

CACHE_MANIFEST_SCHEMA_VERSION = "plastic-promise-local-node-cache/v1"
DEFAULT_CLEANUP_HOUR = 4
DEFAULT_CLEANUP_MINUTE = 30
DEFAULT_IDLE_RETENTION = timedelta(hours=24)


@dataclass(frozen=True)
class ModelCacheEntry:
    """One Plastic Promise-managed, non-canonical model-cache artifact."""

    revision: str
    relative_path: str
    last_used_at: datetime

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        if not self.revision.strip():
            raise ValueError("node_cache_revision_invalid")
        if self.last_used_at.tzinfo is None:
            raise ValueError("node_cache_last_used_at_timezone_required")


@dataclass(frozen=True)
class ModelCacheManifest:
    """Secret-free manifest for Plastic Promise-managed model-cache artifacts."""

    active_revision: str
    fallback_revision: str
    entries: tuple[ModelCacheEntry, ...]
    schema_version: str = CACHE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CACHE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("node_cache_manifest_schema_unsupported")
        if not self.active_revision.strip() or not self.fallback_revision.strip():
            raise ValueError("node_cache_manifest_revision_invalid")
        if self.active_revision == self.fallback_revision:
            raise ValueError("node_cache_manifest_rollback_required")
        paths = [entry.relative_path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("node_cache_manifest_path_duplicate")

    @classmethod
    def from_json(cls, payload: object) -> ModelCacheManifest:
        if not isinstance(payload, dict):
            raise ValueError("node_cache_manifest_invalid")
        expected = {"schema_version", "active_revision", "fallback_revision", "entries"}
        if set(payload) != expected:
            raise ValueError("node_cache_manifest_fields_invalid")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("node_cache_manifest_entries_invalid")
        entries: list[ModelCacheEntry] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "revision",
                "relative_path",
                "last_used_at",
            }:
                raise ValueError("node_cache_manifest_entry_invalid")
            raw_last_used_at = raw_entry["last_used_at"]
            if not isinstance(raw_last_used_at, str):
                raise ValueError("node_cache_manifest_entry_invalid")
            try:
                last_used_at = datetime.fromisoformat(raw_last_used_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("node_cache_manifest_entry_invalid") from exc
            entries.append(
                ModelCacheEntry(
                    revision=_manifest_string(raw_entry["revision"]),
                    relative_path=_manifest_string(raw_entry["relative_path"]),
                    last_used_at=last_used_at,
                )
            )
        return cls(
            schema_version=_manifest_string(payload["schema_version"]),
            active_revision=_manifest_string(payload["active_revision"]),
            fallback_revision=_manifest_string(payload["fallback_revision"]),
            entries=tuple(entries),
        )

    def public_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "active_revision": self.active_revision,
            "fallback_revision": self.fallback_revision,
            "entries": [
                {
                    "revision": entry.revision,
                    "relative_path": entry.relative_path,
                    "last_used_at": entry.last_used_at.astimezone(timezone.utc).isoformat(),
                }
                for entry in self.entries
            ],
        }


@dataclass(frozen=True)
class CacheCleanupConditions:
    """State supplied by the node supervisor before a scheduled cleanup."""

    node_healthy: bool
    model_download_active: bool
    index_rebuild_active: bool
    now: datetime

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("node_cache_cleanup_now_timezone_required")

    @classmethod
    def from_json(cls, payload: object, *, now: datetime) -> CacheCleanupConditions:
        if not isinstance(payload, dict) or set(payload) != {
            "node_healthy",
            "model_download_active",
            "index_rebuild_active",
        }:
            raise ValueError("node_cache_status_invalid")
        values = tuple(payload.values())
        if not all(isinstance(value, bool) for value in values):
            raise ValueError("node_cache_status_invalid")
        return cls(
            node_healthy=payload["node_healthy"],
            model_download_active=payload["model_download_active"],
            index_rebuild_active=payload["index_rebuild_active"],
            now=now,
        )


@dataclass(frozen=True)
class CacheCleanupPlan:
    """A reviewable deletion plan; this module never mutates any path."""

    eligible_paths: tuple[str, ...]
    skipped_reason: str | None
    run_at_local_time: str = "04:30"


def plan_cache_cleanup(
    entries: Iterable[ModelCacheEntry],
    *,
    active_revision: str,
    fallback_revision: str,
    conditions: CacheCleanupConditions,
    idle_retention: timedelta = DEFAULT_IDLE_RETENTION,
) -> CacheCleanupPlan:
    """Return only safe stale-cache candidates under the fixed daily policy."""

    if idle_retention < timedelta(0):
        raise ValueError("node_cache_idle_retention_invalid")
    if not conditions.node_healthy:
        return CacheCleanupPlan((), "node_unhealthy")
    if conditions.model_download_active:
        return CacheCleanupPlan((), "model_download_active")
    if conditions.index_rebuild_active:
        return CacheCleanupPlan((), "index_rebuild_active")

    protected_revisions = {active_revision, fallback_revision}
    eligible = sorted(
        entry.relative_path
        for entry in entries
        if entry.revision not in protected_revisions
        and conditions.now.astimezone(timezone.utc) - entry.last_used_at.astimezone(timezone.utc)
        >= idle_retention
    )
    return CacheCleanupPlan(tuple(eligible), None)


def load_cache_manifest(path: Path) -> ModelCacheManifest:
    """Load a node-local manifest without touching model directories or caches."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("node_cache_manifest_unreadable") from exc
    return ModelCacheManifest.from_json(payload)


def load_cleanup_conditions(path: Path, *, now: datetime) -> CacheCleanupConditions:
    """Load supervisor status for the planner; status carries no user payload."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("node_cache_status_unreadable") from exc
    return CacheCleanupConditions.from_json(payload, now=now)


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("node_cache_relative_path_invalid")


def _manifest_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("node_cache_manifest_entry_invalid")
    return value
