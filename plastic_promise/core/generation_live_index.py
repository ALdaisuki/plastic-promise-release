"""Generation-bound mutable LanceDB view for durable outbox replay.

The verified generation remains immutable.  Operators explicitly bootstrap a
private live root from its index, then the runtime may apply checked outbox
upserts and deletes to that copy.  The small binding manifest prevents a live
view from being reused after generation promotion or rollback.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LIVE_INDEX_SCHEMA = "plastic-promise/generation-live-index/v1"
LIVE_INDEX_LAG_SCHEMA = "plastic-promise/generation-live-index-lag/v1"
LIVE_INDEX_MANIFEST = "manifest.json"
LIVE_INDEX_DIRECTORY = "index"

_MAX_MANIFEST_BYTES = 64 * 1024
_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "base_generation_id",
        "base_manifest_sha256",
        "base_outbox_watermark",
        "base_selection_identity",
        "embedding_index_identity",
    }
)
_INDEX_OUTBOX_TOOLS = ("memory_index", "synthesis_index")
_ACTIVE_STATUSES = frozenset({"pending", "processing"})
_BLOCKED_STATUSES = frozenset({"blocked", "failed"})
_KNOWN_STATUSES = _ACTIVE_STATUSES | _BLOCKED_STATUSES | {"done"}


class GenerationLiveIndexError(RuntimeError):
    """The mutable live view is missing, unsafe, or bound to another base."""


def _manifest_value(manifest: Mapping[str, Any] | object, name: str) -> Any:
    if isinstance(manifest, Mapping):
        return manifest.get(name)
    return getattr(manifest, name, None)


def _binding_from_generation(manifest: Mapping[str, Any] | object) -> dict[str, Any]:
    generation_id = _manifest_value(manifest, "generation_id")
    manifest_sha256 = _manifest_value(manifest, "manifest_sha256")
    if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
        raise GenerationLiveIndexError("live_index_base_generation_invalid")
    if not isinstance(manifest_sha256, str) or _SHA256.fullmatch(manifest_sha256) is None:
        raise GenerationLiveIndexError("live_index_base_manifest_invalid")

    outbox = _manifest_value(manifest, "index_outbox")
    watermark: int | None = None
    embedding_identity = ""
    if outbox is not None:
        if not isinstance(outbox, Mapping):
            raise GenerationLiveIndexError("live_index_base_outbox_invalid")
        watermark_value = outbox.get("watermark")
        if isinstance(watermark_value, bool) or not isinstance(watermark_value, int):
            raise GenerationLiveIndexError("live_index_base_outbox_invalid")
        if watermark_value < 0:
            raise GenerationLiveIndexError("live_index_base_outbox_invalid")
        watermark = watermark_value
        identity_value = outbox.get("embedding_index_identity")
        if identity_value is not None:
            if not isinstance(identity_value, str) or not identity_value.strip():
                raise GenerationLiveIndexError("live_index_base_outbox_invalid")
            embedding_identity = identity_value.strip()

    return {
        "schema": LIVE_INDEX_SCHEMA,
        "base_generation_id": generation_id,
        "base_manifest_sha256": manifest_sha256,
        "base_outbox_watermark": watermark,
        "embedding_index_identity": embedding_identity,
    }


def _replayable_binding_from_generation(
    manifest: Mapping[str, Any] | object,
    *,
    selection_identity: str,
) -> dict[str, Any]:
    outbox = _manifest_value(manifest, "index_outbox")
    if (
        not isinstance(outbox, Mapping)
        or outbox.get("reconciled") is not True
        or not isinstance(outbox.get("receipt"), Mapping)
    ):
        raise GenerationLiveIndexError("live_index_base_outbox_unavailable")
    binding = _binding_from_generation(manifest)
    if binding["base_outbox_watermark"] is None:
        raise GenerationLiveIndexError("live_index_base_outbox_unavailable")
    if not isinstance(selection_identity, str) or _SHA256.fullmatch(selection_identity) is None:
        raise GenerationLiveIndexError("live_index_base_selection_invalid")
    binding["base_selection_identity"] = selection_identity
    return binding


def _require_directory(path: Path, reason: str) -> None:
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise GenerationLiveIndexError(reason) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise GenerationLiveIndexError(reason)


def _require_regular_file(path: Path, reason: str) -> None:
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise GenerationLiveIndexError(reason) from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise GenerationLiveIndexError(reason)


def _validate_source_tree(root: Path) -> None:
    _require_directory(root, "live_index_base_path_invalid")
    for directory, directories, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*directories, *files]:
            path = directory_path / name
            try:
                entry = os.lstat(path)
            except OSError as exc:
                raise GenerationLiveIndexError("live_index_base_tree_unreadable") from exc
            if stat.S_ISLNK(entry.st_mode):
                raise GenerationLiveIndexError("live_index_base_tree_unsafe")
            if name in directories and not stat.S_ISDIR(entry.st_mode):
                raise GenerationLiveIndexError("live_index_base_tree_unsafe")
            if name in files and not stat.S_ISREG(entry.st_mode):
                raise GenerationLiveIndexError("live_index_base_tree_unsafe")


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _make_private(root: Path) -> None:
    for directory, _directories, files in os.walk(root):
        os.chmod(directory, 0o700)
        for name in files:
            os.chmod(Path(directory) / name, 0o600)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Persist every copied file before publishing its containing directories."""

    _require_directory(root, "live_index_material_unavailable")
    for directory, _directories, files in os.walk(root, topdown=False, followlinks=False):
        directory_path = Path(directory)
        for name in files:
            path = directory_path / name
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise GenerationLiveIndexError("live_index_base_tree_unsafe")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _fsync_directory(directory_path)


def _publish_staged_root(temporary: Path, target: Path, parent: Path) -> None:
    """Publish a complete staging tree without ever replacing a target root."""

    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise GenerationLiveIndexError("live_index_root_exists") from exc

    try:
        (temporary / LIVE_INDEX_DIRECTORY).rename(target / LIVE_INDEX_DIRECTORY)
        _fsync_directory(target)
        (temporary / LIVE_INDEX_MANIFEST).rename(target / LIVE_INDEX_MANIFEST)
        _fsync_directory(target)
        temporary.rmdir()
        _fsync_directory(parent)
    except OSError as exc:
        # A partially published root has no manifest until the copied index is
        # durable, so readers fail closed.  Leave it for explicit inspection;
        # bootstrap must never guess that it is safe to overwrite or delete.
        raise GenerationLiveIndexError("live_index_bootstrap_failed") from exc


def _read_manifest(live_root: Path) -> dict[str, Any]:
    _require_directory(live_root, "live_index_root_unsafe")
    manifest_path = live_root / LIVE_INDEX_MANIFEST
    _require_regular_file(manifest_path, "live_index_manifest_unsafe")
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise GenerationLiveIndexError("live_index_manifest_invalid")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except GenerationLiveIndexError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationLiveIndexError("live_index_manifest_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise GenerationLiveIndexError("live_index_manifest_invalid")
    try:
        normalized = _binding_from_generation(
            {
                "generation_id": payload["base_generation_id"],
                "manifest_sha256": payload["base_manifest_sha256"],
                "index_outbox": (
                    None
                    if payload["base_outbox_watermark"] is None
                    else {
                        "watermark": payload["base_outbox_watermark"],
                        **(
                            {"embedding_index_identity": payload["embedding_index_identity"]}
                            if payload["embedding_index_identity"]
                            else {}
                        ),
                    }
                ),
            }
        )
        selection_identity = payload["base_selection_identity"]
        if not isinstance(selection_identity, str) or _SHA256.fullmatch(selection_identity) is None:
            raise GenerationLiveIndexError("live_index_manifest_invalid")
        normalized["base_selection_identity"] = selection_identity
    except GenerationLiveIndexError as exc:
        raise GenerationLiveIndexError("live_index_manifest_invalid") from exc
    if normalized != payload:
        raise GenerationLiveIndexError("live_index_manifest_invalid")
    return normalized


def inspect_generation_live_index(live_root: str | Path) -> dict[str, Any]:
    """Return bounded non-secret binding metadata without opening LanceDB."""

    return _read_manifest(Path(live_root).expanduser())


def resolve_generation_live_index(
    live_root: str | Path,
    base_manifest: Mapping[str, Any] | object,
    *,
    base_selection_identity: str,
) -> Path:
    """Resolve a live index only when it is bound to the selected generation."""

    root = Path(live_root).expanduser()
    observed = _read_manifest(root)
    expected = _replayable_binding_from_generation(
        base_manifest,
        selection_identity=base_selection_identity,
    )
    if observed["base_generation_id"] != expected["base_generation_id"]:
        raise GenerationLiveIndexError("live_index_base_generation_mismatch")
    if observed["base_manifest_sha256"] != expected["base_manifest_sha256"]:
        raise GenerationLiveIndexError("live_index_base_manifest_mismatch")
    if observed["base_outbox_watermark"] != expected["base_outbox_watermark"]:
        raise GenerationLiveIndexError("live_index_base_outbox_mismatch")
    if observed["base_selection_identity"] != expected["base_selection_identity"]:
        raise GenerationLiveIndexError("live_index_base_selection_mismatch")
    if observed["embedding_index_identity"] != expected["embedding_index_identity"]:
        raise GenerationLiveIndexError("live_index_embedding_identity_mismatch")
    index = root / LIVE_INDEX_DIRECTORY
    _require_directory(index, "live_index_material_unavailable")
    return index


def summarize_generation_live_index_lag(
    connection: sqlite3.Connection,
    base_manifest: Mapping[str, Any] | object,
) -> dict[str, Any]:
    """Summarize post-base outbox delivery without exposing job material."""

    if not isinstance(connection, sqlite3.Connection):
        raise GenerationLiveIndexError("live_index_outbox_database_required")
    binding = _binding_from_generation(base_manifest)
    watermark = binding["base_outbox_watermark"]
    if watermark is None:
        return {
            "schema": LIVE_INDEX_LAG_SCHEMA,
            "state": "unavailable",
            "reason": "base_outbox_watermark_unavailable",
            "base_generation_id": binding["base_generation_id"],
            "base_outbox_watermark": None,
            "newer_job_count": None,
            "active_job_count": None,
            "completed_job_count": None,
            "blocked_job_count": None,
            "status_counts": {},
            "newest_rowid": None,
        }
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(store_outbox)")}
        if not {"tool_name", "status"}.issubset(columns):
            raise GenerationLiveIndexError("live_index_outbox_schema_unavailable")
        rows = connection.execute(
            "SELECT status, COUNT(*), MAX(rowid) FROM store_outbox "
            "WHERE tool_name IN (?, ?) AND rowid > ? GROUP BY status",
            (*_INDEX_OUTBOX_TOOLS, watermark),
        ).fetchall()
    except GenerationLiveIndexError:
        raise
    except sqlite3.Error as exc:
        raise GenerationLiveIndexError("live_index_outbox_unreadable") from exc

    status_counts = {str(status): int(count) for status, count, _rowid in rows}
    active = sum(status_counts.get(status, 0) for status in _ACTIVE_STATUSES)
    blocked = sum(status_counts.get(status, 0) for status in _BLOCKED_STATUSES)
    completed = status_counts.get("done", 0)
    unknown = sum(count for status, count in status_counts.items() if status not in _KNOWN_STATUSES)
    if unknown:
        state = "unknown"
    elif blocked:
        state = "blocked"
    elif active:
        state = "lagged"
    else:
        state = "ready"
    return {
        "schema": LIVE_INDEX_LAG_SCHEMA,
        "state": state,
        "base_generation_id": binding["base_generation_id"],
        "base_outbox_watermark": watermark,
        "newer_job_count": sum(status_counts.values()),
        "active_job_count": active,
        "completed_job_count": completed,
        "blocked_job_count": blocked,
        "status_counts": status_counts,
        "newest_rowid": max((int(rowid) for _status, _count, rowid in rows), default=watermark),
    }


def bootstrap_generation_live_index(
    *,
    base_index_path: str | Path,
    base_manifest: Mapping[str, Any] | object,
    base_selection_identity: str,
    live_root: str | Path,
) -> Path:
    """Durably copy a verified immutable index into a new private live root."""

    base = Path(base_index_path).expanduser()
    target = Path(live_root).expanduser()
    _validate_source_tree(base)
    binding = _replayable_binding_from_generation(
        base_manifest,
        selection_identity=base_selection_identity,
    )
    if target.exists() or target.is_symlink():
        raise GenerationLiveIndexError("live_index_root_exists")
    parent = target.parent
    _require_directory(parent, "live_index_parent_invalid")

    temporary = parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(mode=0o700)
        shutil.copytree(base, temporary / LIVE_INDEX_DIRECTORY, copy_function=shutil.copy2)
        _make_private(temporary)
        _write_manifest(temporary / LIVE_INDEX_MANIFEST, binding)
        _fsync_tree(temporary / LIVE_INDEX_DIRECTORY)
        _fsync_directory(temporary)
        _publish_staged_root(temporary, target, parent)
    except GenerationLiveIndexError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise GenerationLiveIndexError("live_index_bootstrap_failed") from exc
    return resolve_generation_live_index(
        target,
        base_manifest,
        base_selection_identity=base_selection_identity,
    )


__all__ = [
    "LIVE_INDEX_DIRECTORY",
    "LIVE_INDEX_LAG_SCHEMA",
    "LIVE_INDEX_MANIFEST",
    "LIVE_INDEX_SCHEMA",
    "GenerationLiveIndexError",
    "bootstrap_generation_live_index",
    "inspect_generation_live_index",
    "resolve_generation_live_index",
    "summarize_generation_live_index_lag",
]
