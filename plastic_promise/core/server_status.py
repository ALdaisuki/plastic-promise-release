"""Bounded, read-only server status collection for the operator control plane.

The collector deliberately avoids service managers, provider clients, and the
application stores.  SQLite is opened with ``mode=ro`` plus ``query_only``;
LanceDB status comes only from the immutable generation manifest; and TCP
checks prove only that a loopback listener accepted a connection.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import sqlite3
import stat
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plastic_promise.core.lancedb_generation import GenerationManager

SERVER_STATUS_SCHEMA = "plastic-promise/server-status/v1"
MAINTENANCE_HEARTBEAT_SCHEMA = "maintenance-heartbeat/v1"

DEFAULT_LISTENER_PORTS = (
    ("mcp", 9020),
    ("inference_gateway", 9030),
    ("config_control", 9040),
)

_MAX_LISTENERS = 16
_MAX_HEARTBEAT_BYTES = 64 * 1024
_MAX_PID = (1 << 31) - 1
_SQLITE_QUERY_BUDGET_SECONDS = 0.5
_SQLITE_PROGRESS_INSTRUCTIONS = 1_000
_LISTENER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PROCESS_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_HEARTBEAT_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_OUTBOX_STATUSES = ("pending", "processing", "blocked", "failed", "done")
_TASK_STATUSES = (
    "pending",
    "claimed",
    "executing",
    "done",
    "pending_review",
    "verified",
    "reassigned",
)
_JOB_STATUSES = ("pending", "leased", "completed", "expired")
_RESERVATION_STATUSES = ("reserved", "preparing", "finalized", "released", "expired")


@dataclass(frozen=True)
class ServerStatusSettings:
    """Filesystem and loopback inputs for one server status snapshot."""

    sqlite_path: Path
    inference_job_db_path: Path
    lancedb_root: Path
    maintenance_heartbeat_path: Path
    lancedb_live_root: Path | None = None
    maintenance_enabled: bool = False
    maintenance_expected_pid: int | None = None
    maintenance_expected_process_generation: str | None = None
    listener_ports: tuple[tuple[str, int], ...] = DEFAULT_LISTENER_PORTS
    socket_timeout_seconds: float = 0.2
    maintenance_max_age_seconds: float = 120.0

    def __post_init__(self) -> None:
        for field_name in (
            "sqlite_path",
            "inference_job_db_path",
            "lancedb_root",
            "maintenance_heartbeat_path",
        ):
            value = getattr(self, field_name)
            try:
                raw_path = os.fspath(value)
            except TypeError as exc:
                raise ValueError(f"{field_name}_invalid") from exc
            if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
                raise ValueError(f"{field_name}_invalid")
            object.__setattr__(self, field_name, Path(raw_path))
        if self.lancedb_live_root is not None:
            try:
                raw_live_root = os.fspath(self.lancedb_live_root)
            except TypeError as exc:
                raise ValueError("lancedb_live_root_invalid") from exc
            if not isinstance(raw_live_root, str) or not raw_live_root or "\x00" in raw_live_root:
                raise ValueError("lancedb_live_root_invalid")
            object.__setattr__(self, "lancedb_live_root", Path(raw_live_root))

        if type(self.maintenance_enabled) is not bool:
            raise ValueError("maintenance_enabled_invalid")
        if self.maintenance_expected_pid is not None and (
            type(self.maintenance_expected_pid) is not int
            or self.maintenance_expected_pid <= 0
            or self.maintenance_expected_pid > _MAX_PID
        ):
            raise ValueError("maintenance_expected_pid_invalid")
        generation = self.maintenance_expected_process_generation
        if generation is not None and (
            not isinstance(generation, str) or _PROCESS_GENERATION.fullmatch(generation) is None
        ):
            raise ValueError("maintenance_expected_process_generation_invalid")

        try:
            normalized_listeners = tuple(self.listener_ports)
        except TypeError as exc:
            raise ValueError("listener_ports_invalid") from exc
        if len(normalized_listeners) > _MAX_LISTENERS:
            raise ValueError("listener_ports_too_many")
        seen_names: set[str] = set()
        checked_listeners: list[tuple[str, int]] = []
        for item in normalized_listeners:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("listener_ports_invalid")
            name, port = item
            if not isinstance(name, str) or _LISTENER_NAME.fullmatch(name) is None:
                raise ValueError("listener_name_invalid")
            if name in seen_names:
                raise ValueError("listener_name_duplicate")
            if type(port) is not int or not 1 <= port <= 65_535:
                raise ValueError("listener_port_invalid")
            seen_names.add(name)
            checked_listeners.append((name, port))
        object.__setattr__(self, "listener_ports", tuple(checked_listeners))

        _validate_bounded_seconds(
            self.socket_timeout_seconds,
            minimum=0.01,
            maximum=2.0,
            reason="socket_timeout_seconds_invalid",
        )
        _validate_bounded_seconds(
            self.maintenance_max_age_seconds,
            minimum=1.0,
            maximum=86_400.0,
            reason="maintenance_max_age_seconds_invalid",
        )


def collect_server_status(
    settings: ServerStatusSettings,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Collect a bounded snapshot without providers, mutations, or health claims."""

    if not isinstance(settings, ServerStatusSettings):
        raise TypeError("settings_must_be_server_status_settings")
    observed_now = _aware_utc(now)
    return {
        "schema": SERVER_STATUS_SCHEMA,
        "collected_at": _utc_text(observed_now),
        "listeners": _listener_status(settings),
        "sqlite": _canonical_sqlite_status(settings.sqlite_path),
        "inference_jobs": _inference_job_status(settings.inference_job_db_path),
        "lancedb": _lancedb_status(
            settings.lancedb_root,
            live_root=settings.lancedb_live_root,
            sqlite_path=settings.sqlite_path,
        ),
        "maintenance": _maintenance_status(settings, now=observed_now),
    }


def _listener_status(settings: ServerStatusSettings) -> dict[str, object]:
    listeners: dict[str, object] = {}
    for name, port in settings.listener_ports:
        reachable = False
        try:
            connection = socket.create_connection(
                ("127.0.0.1", port),
                timeout=float(settings.socket_timeout_seconds),
            )
            with closing(connection):
                reachable = True
        except (OSError, TimeoutError):
            pass
        listeners[name] = {
            "host": "127.0.0.1",
            "port": port,
            "state": "reachable" if reachable else "unreachable",
            "listening": reachable,
        }
    return listeners


def _canonical_sqlite_status(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "state": "missing",
        "file": _file_metadata(path),
        "wal_file": _file_metadata(Path(f"{path}-wal")),
        "access": {"mode": "ro", "query_only": None},
        "tables": {
            "store_outbox": _unavailable_aggregate("database_missing"),
            "task_queue": _unavailable_aggregate("database_missing"),
        },
    }
    if result["file"]["kind"] != "file":  # type: ignore[index]
        if result["file"]["exists"]:  # type: ignore[index]
            result["state"] = "unavailable"
            result["reason"] = "database_not_regular_file"
        return result

    try:
        with closing(_connect_read_only(path)) as connection:
            result["access"] = {"mode": "ro", "query_only": True}
            result["tables"] = {
                "store_outbox": _aggregate_statuses(
                    connection,
                    table="store_outbox",
                    statuses=_OUTBOX_STATUSES,
                ),
                "task_queue": _aggregate_statuses(
                    connection,
                    table="task_queue",
                    statuses=_TASK_STATUSES,
                ),
            }
    except (OSError, sqlite3.Error, ValueError):
        result["state"] = "unavailable"
        result["reason"] = "database_read_unavailable"
        return result
    result["state"] = "ready"
    return result


def _inference_job_status(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "state": "missing",
        "file": _file_metadata(path),
        "wal_file": _file_metadata(Path(f"{path}-wal")),
        "access": {"mode": "ro", "query_only": None},
        "jobs": _unavailable_aggregate("database_missing"),
        "reservations": _unavailable_aggregate("database_missing"),
    }
    if result["file"]["kind"] != "file":  # type: ignore[index]
        if result["file"]["exists"]:  # type: ignore[index]
            result["state"] = "unavailable"
            result["reason"] = "database_not_regular_file"
        return result

    try:
        with closing(_connect_read_only(path)) as connection:
            jobs = _aggregate_statuses(
                connection,
                table="inference_rerank_jobs",
                statuses=_JOB_STATUSES,
            )
            reservations = _aggregate_statuses(
                connection,
                table="inference_rerank_reservations",
                statuses=_RESERVATION_STATUSES,
            )
            result["access"] = {"mode": "ro", "query_only": True}
            result["jobs"] = jobs
            result["reservations"] = reservations
    except (OSError, sqlite3.Error, ValueError):
        result["state"] = "unavailable"
        result["reason"] = "database_read_unavailable"
        return result
    result["state"] = "ready"
    return result


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=0.2,
    )
    try:
        query_deadline = time.monotonic() + _SQLITE_QUERY_BUDGET_SECONDS
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= query_deadline),
            _SQLITE_PROGRESS_INSTRUCTIONS,
        )
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or query_only[0] != 1:
            raise sqlite3.OperationalError("query_only_unavailable")
        connection.execute("PRAGMA temp_store = MEMORY")
        return connection
    except BaseException:
        connection.close()
        raise


def _aggregate_statuses(
    connection: sqlite3.Connection,
    *,
    table: str,
    statuses: tuple[str, ...],
) -> dict[str, object]:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if table_exists is None:
        return _unavailable_aggregate("table_missing")
    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if "status" not in columns:
        return _unavailable_aggregate("status_column_missing")

    placeholders = ",".join("?" for _ in statuses)
    total_row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    rows = connection.execute(
        f'SELECT status, COUNT(*) FROM "{table}" WHERE status IN ({placeholders}) GROUP BY status',
        statuses,
    ).fetchall()
    total = int(total_row[0]) if total_row is not None else 0
    by_status = dict.fromkeys(statuses, 0)
    for status, count in rows:
        if status in by_status:
            by_status[str(status)] = int(count)
    known_total = sum(by_status.values())
    return {
        "state": "ready",
        "total": total,
        "by_status": by_status,
        "other": max(0, total - known_total),
    }


def _unavailable_aggregate(reason: str) -> dict[str, object]:
    return {
        "state": "unavailable",
        "reason": reason,
        "total": None,
        "by_status": {},
        "other": None,
    }


def _lancedb_status(
    root: Path,
    *,
    live_root: Path | None = None,
    sqlite_path: Path | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "root": str(root),
        "state": "missing",
        "root_metadata": _file_metadata(root),
        "manifest": None,
        "tables_opened": False,
        "verification_scope": "manifest-metadata-only",
        "index_tree_verified": False,
        "live_index": None,
    }
    if result["root_metadata"]["kind"] != "directory":  # type: ignore[index]
        if result["root_metadata"]["exists"]:  # type: ignore[index]
            result["state"] = "unavailable"
            result["reason"] = "lancedb_root_not_directory"
        return result

    try:
        with GenerationManager(root, create=False) as manager:
            manifest = manager.current_manifest_metadata()
            selection_identity = (
                manager.current_selection_identity()
                if manifest is not None and live_root is not None
                else ""
            )
    except Exception:
        result["state"] = "unavailable"
        result["reason"] = "lancedb_manifest_unavailable"
        return result
    if manifest is None:
        result["state"] = "no-current-generation"
        return result

    quality_report = manifest.quality_report
    gate = quality_report.get("gate") if isinstance(quality_report, dict) else None
    result["state"] = "current-generation"
    result["manifest"] = {
        "manifest_schema": manifest.manifest_schema,
        "generation_id": manifest.generation_id,
        "index_schema": manifest.index_schema,
        "embedding_model": manifest.embedding_model,
        "model_revision": manifest.model_revision,
        "embedding_dimension": manifest.embedding_dimension,
        "source_db_sha256": manifest.source_db_sha256,
        "source_row_count": manifest.source_row_count,
        "build_status": manifest.build_status,
        "built_row_count": manifest.built_row_count,
        "verification_status": manifest.verification_status,
        "quality_gate_status": gate.get("status") if isinstance(gate, dict) else None,
        "index_text_policy": manifest.index_text_policy,
        "index_material_sha256": manifest.index_material_sha256,
        "identity_sha256": manifest.identity_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "created_at": manifest.created_at,
        "completed_at": manifest.completed_at,
        "verified_at": manifest.verified_at,
        "outbox": _manifest_outbox_summary(manifest.index_outbox),
    }
    if live_root is not None:
        result["live_index"] = _generation_live_status(
            live_root,
            sqlite_path=sqlite_path,
            manifest=manifest,
            base_selection_identity=selection_identity,
        )
    return result


def _generation_live_status(
    live_root: Path,
    *,
    sqlite_path: Path | None,
    manifest: object,
    base_selection_identity: str,
) -> dict[str, object]:
    from plastic_promise.core.generation_live_index import (
        resolve_generation_live_index,
        summarize_generation_live_index_lag,
    )

    result: dict[str, object] = {
        "root": str(live_root),
        "root_metadata": _file_metadata(live_root),
        "state": "unavailable",
        "base_generation_id": getattr(manifest, "generation_id", None),
        "base_manifest_sha256": getattr(manifest, "manifest_sha256", None),
        "lag": None,
    }
    try:
        resolve_generation_live_index(
            live_root,
            manifest,
            base_selection_identity=base_selection_identity,
        )
        if sqlite_path is None:
            raise ValueError("live_index_sqlite_path_unavailable")
        with closing(_connect_read_only(sqlite_path)) as connection:
            result["lag"] = summarize_generation_live_index_lag(connection, manifest)
    except Exception as exc:
        result["reason"] = exc.__class__.__name__
        return result
    lag = result["lag"]
    lag_state = lag.get("state") if isinstance(lag, dict) else "unavailable"
    result["state"] = "ready" if lag_state in {"ready", "lagged"} else str(lag_state)
    return result


def _manifest_outbox_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "watermark": value.get("watermark"),
        "immutable_digest": value.get("immutable_digest"),
        "job_count": value.get("job_count"),
        "source_fingerprint": value.get("source_fingerprint"),
        "reconciled": value.get("reconciled"),
    }


def _maintenance_status(
    settings: ServerStatusSettings,
    *,
    now: datetime,
) -> dict[str, object]:
    path = settings.maintenance_heartbeat_path
    result: dict[str, object] = {
        "enabled": settings.maintenance_enabled,
        "state": "disabled" if not settings.maintenance_enabled else "not-running",
        "heartbeat_file": _file_metadata(path),
    }
    if not settings.maintenance_enabled:
        return result
    if result["heartbeat_file"]["kind"] != "file":  # type: ignore[index]
        if result["heartbeat_file"]["exists"]:  # type: ignore[index]
            result["state"] = "identity-unverified"
            result["reason"] = "maintenance_heartbeat_not_regular_file"
        else:
            result["reason"] = "maintenance_heartbeat_missing"
        return result

    payload = _read_heartbeat(path)
    if payload is None:
        result["state"] = "identity-unverified"
        result["reason"] = "maintenance_heartbeat_invalid"
        return result
    pid = payload.get("pid")
    process_generation = payload.get("process_generation")
    startup_cycle = payload.get("startup_replay_cycle_id")
    startup_owner = payload.get("startup_replay_owner_pid")
    if (
        payload.get("schema") != MAINTENANCE_HEARTBEAT_SCHEMA
        or type(pid) is not int
        or pid <= 0
        or pid > _MAX_PID
        or not isinstance(process_generation, str)
        or _PROCESS_GENERATION.fullmatch(process_generation) is None
        or not isinstance(startup_cycle, str)
        or not startup_cycle
        or type(startup_owner) is not int
        or startup_owner != pid
    ):
        result["state"] = "identity-unverified"
        result["reason"] = "maintenance_heartbeat_identity_invalid"
        return result
    result["pid"] = pid
    expected_pid = settings.maintenance_expected_pid
    expected_generation = settings.maintenance_expected_process_generation
    if expected_pid is None or expected_generation is None:
        result["state"] = "identity-unverified"
        result["reason"] = "maintenance_expected_identity_missing"
        return result
    if pid != expected_pid or process_generation != expected_generation:
        result["state"] = "identity-unverified"
        result["reason"] = "maintenance_expected_identity_mismatch"
        return result
    if not _pid_is_alive(pid):
        result["state"] = "not-running"
        result["reason"] = "maintenance_pid_not_alive"
        return result

    try:
        updated_at = _parse_utc(payload["updated_at"])
    except (KeyError, TypeError, ValueError):
        result["state"] = "identity-unverified"
        result["reason"] = "maintenance_heartbeat_timestamp_invalid"
        return result
    age_seconds = (now - updated_at).total_seconds()
    result["updated_at"] = _utc_text(updated_at)
    result["age_seconds"] = age_seconds
    if age_seconds < 0 or age_seconds >= settings.maintenance_max_age_seconds:
        result["state"] = "stale"
        result["reason"] = "maintenance_heartbeat_stale"
        return result

    result["state"] = "fresh"
    result["reason"] = "ok"
    return result


def _read_heartbeat(path: Path) -> dict[str, Any] | None:
    heartbeat_fd = -1
    try:
        heartbeat_fd = os.open(path, _HEARTBEAT_OPEN_FLAGS)
        before = os.fstat(heartbeat_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_HEARTBEAT_BYTES
        ):
            return None
        raw = bytearray()
        while len(raw) <= _MAX_HEARTBEAT_BYTES:
            chunk = os.read(
                heartbeat_fd,
                min(8_192, _MAX_HEARTBEAT_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(heartbeat_fd)
        if len(raw) > _MAX_HEARTBEAT_BYTES or _file_version(before) != _file_version(after):
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    finally:
        if heartbeat_fd >= 0:
            with suppress(OSError):
                os.close(heartbeat_fd)
    return payload if isinstance(payload, dict) else None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _file_version(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
    )


def _file_metadata(path: Path) -> dict[str, object]:
    try:
        details = path.stat()
    except FileNotFoundError:
        return {
            "exists": False,
            "kind": "missing",
            "size_bytes": None,
            "modified_at": None,
            "mode": None,
        }
    except OSError:
        return {
            "exists": True,
            "kind": "unavailable",
            "size_bytes": None,
            "modified_at": None,
            "mode": None,
        }
    if stat.S_ISREG(details.st_mode):
        kind = "file"
    elif stat.S_ISDIR(details.st_mode):
        kind = "directory"
    else:
        kind = "other"
    try:
        modified_at = _utc_text(datetime.fromtimestamp(details.st_mtime, tz=timezone.utc))
    except (OSError, OverflowError, ValueError):
        modified_at = None
    return {
        "exists": True,
        "kind": kind,
        "size_bytes": details.st_size,
        "modified_at": modified_at,
        "mode": f"{stat.S_IMODE(details.st_mode):04o}",
    }


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() is None
    ):
        raise ValueError("now_must_be_timezone_aware")
    return observed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_bounded_seconds(
    value: object,
    *,
    minimum: float,
    maximum: float,
    reason: str,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(reason)
