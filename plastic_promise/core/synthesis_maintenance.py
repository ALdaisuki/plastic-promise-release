"""Canonical invalidation and derived-index repair for governed synthesis."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plastic_promise.core.memory_index import read_persisted_index_material
from plastic_promise.core.synthesis import SynthesisStore
from plastic_promise.core.synthesis_retrieval import (
    read_memory_version,
    synthesis_index_eligible,
)
from plastic_promise.core.traceability import (
    ensure_traceability_schema,
    record_memory_lineage,
    utc_now,
)

_INDEX_ACTIONS = frozenset({"upsert", "delete"})
_CONTRADICTION_CODES = frozenset({"SYNTHESIS_CONTRADICTION_OPEN"})
_DEFAULT_INDEX_JOB_LEASE_SECONDS = 300
_MAX_INDEX_JOB_LEASE_SECONDS = 86400
_LEGACY_MEMORY_INDEX_JOB_SCHEMA = "memory-index/v2"
_MEMORY_INDEX_JOB_SCHEMA = "memory-index/v3"
_SYNTHESIS_INDEX_JOB_SCHEMA = "synthesis-index/v1"
_TEST_INDEX_FAILURE_SCHEMA = "test-index-failure/v1"
_TEST_MODE_VALUES = frozenset({"1", "true", "yes", "on"})
_TEST_INDEX_FAILURE_LOCK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ScanReport:
    scanned: int
    stale_ids: tuple[str, ...]
    contested_ids: tuple[str, ...]
    queued_job_ids: tuple[str, ...]
    retryable_findings: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class ReplayReport:
    selected: int
    claimed: int
    succeeded: int
    failed: int
    skipped: int
    done_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]


@dataclass(frozen=True)
class _IndexControlState:
    status: str
    revision: int | None
    eligible: bool


@dataclass(frozen=True)
class _MemoryIndexJob:
    schema: str
    action: str
    memory_id: str
    expected_embedding_hash: str
    material_revision: str
    memory_version: int
    project_id: str


class _LeaseOwnershipLost(RuntimeError):
    """The durable outbox lease moved to another worker before a side effect."""


class InjectedIndexFailure(RuntimeError):
    """Test-only failure injected exactly before a checked index side effect."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _test_index_failure_marker_path() -> Path | None:
    configured = str(os.environ.get("PP_TEST_INDEX_FAIL_MARKER") or "").strip()
    return Path(configured) if configured else None


def _test_mode_enabled() -> bool:
    return str(os.environ.get("PP_TEST_MODE") or "").strip().casefold() in _TEST_MODE_VALUES


def _read_test_index_failure_marker(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("index_failure_marker_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _TEST_INDEX_FAILURE_SCHEMA:
        raise RuntimeError("index_failure_marker_invalid")

    def normalize(entry: Any) -> dict[str, Any]:
        if not isinstance(entry, dict) or set(entry) != {
            "action",
            "memory_id",
            "remaining",
        }:
            raise RuntimeError("index_failure_marker_invalid")
        action = entry.get("action")
        memory_id = entry.get("memory_id")
        remaining = entry.get("remaining")
        if (
            not isinstance(action, str)
            or action.strip().casefold() not in _INDEX_ACTIONS
            or not isinstance(memory_id, str)
            or not memory_id.strip()
            or type(remaining) is not int
            or remaining <= 0
        ):
            raise RuntimeError("index_failure_marker_invalid")
        return {
            "action": action.strip().casefold(),
            "memory_id": memory_id.strip(),
            "remaining": remaining,
        }

    if set(payload) == {"schema", "action", "memory_id", "remaining"}:
        return {
            "schema": _TEST_INDEX_FAILURE_SCHEMA,
            **normalize(
                {
                    "action": payload["action"],
                    "memory_id": payload["memory_id"],
                    "remaining": payload["remaining"],
                }
            ),
        }
    if set(payload) != {"schema", "failures"} or not isinstance(payload["failures"], list):
        raise RuntimeError("index_failure_marker_invalid")
    failures = [normalize(entry) for entry in payload["failures"]]
    if not failures or len({(item["action"], item["memory_id"]) for item in failures}) != len(
        failures
    ):
        raise RuntimeError("index_failure_marker_invalid")
    return {"schema": _TEST_INDEX_FAILURE_SCHEMA, "failures": failures}


def validate_test_index_failure_configuration() -> Path | None:
    """Validate the environment-only failure seam without enabling it publicly."""
    path = _test_index_failure_marker_path()
    if path is None:
        return None
    if not _test_mode_enabled():
        raise RuntimeError("index_failure_marker_requires_test_mode")
    if path.exists():
        _read_test_index_failure_marker(path)
    return path


def _acquire_test_index_failure_lock(lock_path: Path) -> int:
    deadline = time.monotonic() + _TEST_INDEX_FAILURE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("index_failure_marker_lock_timeout") from None
            time.sleep(0.005)


def _write_test_index_failure_marker(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def consume_test_index_failure(*, action: str, memory_id: str) -> None:
    """Atomically consume one matching test-only checked-index failure marker."""
    path = validate_test_index_failure_configuration()
    if path is None or not path.exists():
        return
    normalized_action = str(action or "").strip().casefold()
    normalized_memory_id = str(memory_id or "").strip()
    if normalized_action not in _INDEX_ACTIONS or not normalized_memory_id:
        return
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = _acquire_test_index_failure_lock(lock_path)
    try:
        if not path.exists():
            return
        marker = _read_test_index_failure_marker(path)
        failures = marker.get("failures")
        if isinstance(failures, list):
            match = next(
                (
                    item
                    for item in failures
                    if item["action"] == normalized_action
                    and item["memory_id"] == normalized_memory_id
                ),
                None,
            )
            if match is None:
                return
            if int(match["remaining"]) == 1:
                failures.remove(match)
            else:
                match["remaining"] = int(match["remaining"]) - 1
            if failures:
                _write_test_index_failure_marker(path, marker)
            else:
                path.unlink()
        else:
            if marker["action"] != normalized_action or marker["memory_id"] != normalized_memory_id:
                return
            remaining = int(marker["remaining"])
            if remaining == 1:
                path.unlink()
            else:
                _write_test_index_failure_marker(
                    path,
                    {**marker, "remaining": remaining - 1},
                )
        raise InjectedIndexFailure(
            f"injected_index_failure:{normalized_action}:{normalized_memory_id}"
        )
    finally:
        os.close(lock_fd)
        with suppress(FileNotFoundError):
            lock_path.unlink()


def _canonical_connection(engine: Any):
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    return getattr(sqlite, "_conn", None)


def _canonical_memory(engine: Any, memory_id: str) -> dict[str, Any] | None:
    state = getattr(engine, "__dict__", {})
    sqlite = state.get("_sqlite") if isinstance(state, dict) else None
    get_memory = getattr(sqlite, "get", None)
    if not callable(get_memory):
        return None
    value = get_memory(memory_id)
    return dict(value) if isinstance(value, dict) else None


def enqueue_synthesis_index_job(
    conn,
    *,
    memory_id: str,
    revision: int,
    action: str,
    call_id: str,
) -> str:
    """Enqueue one active `(memory_id, revision, action)` derived-index job."""
    memory_id = str(memory_id or "").strip()
    action = str(action or "").strip().casefold()
    if not memory_id:
        raise ValueError("missing_synthesis_memory_id")
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid_synthesis_revision")
    if action not in _INDEX_ACTIONS:
        raise ValueError("invalid_synthesis_index_action")

    ensure_traceability_schema(conn)
    dedupe_key = f"synthesis-index:{memory_id}:{revision}:{action}"
    owns_transaction = not bool(conn.in_transaction)
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT outbox_id FROM store_outbox "
            "WHERE dedupe_key = ? AND status IN ('pending', 'processing') "
            "ORDER BY created_at, outbox_id LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if active is not None:
            if owns_transaction:
                conn.commit()
            return str(active[0])

        project_row = conn.execute(
            "SELECT project_id FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        project_id = str(project_row[0] or "") if project_row else ""
        outbox_id = f"outbox_{secrets.token_hex(8)}"
        now = utc_now()
        conn.execute(
            "INSERT INTO store_outbox "
            "(outbox_id, tool_name, project_id, call_id, status, payload_json, "
            "error_class, error_message, metadata_json, created_at, dedupe_key, "
            "attempt_count, updated_at, next_attempt_at) "
            "VALUES (?, 'synthesis_index', ?, ?, 'pending', ?, '', '', ?, ?, ?, 0, ?, '')",
            (
                outbox_id,
                project_id,
                str(call_id or ""),
                _json(
                    {
                        "action": action,
                        "memory_id": memory_id,
                        "revision": revision,
                    }
                ),
                _json({"job_schema": "synthesis-index/v1"}),
                now,
                dedupe_key,
                now,
            ),
        )
        if owns_transaction:
            conn.commit()
        return outbox_id
    except sqlite3.IntegrityError:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        active = conn.execute(
            "SELECT outbox_id FROM store_outbox "
            "WHERE dedupe_key = ? AND status IN ('pending', 'processing') LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if active is None:
            raise
        return str(active[0])
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _lease_seconds(value: int | None) -> int:
    configured: object = (
        os.environ.get("PP_INDEX_JOB_LEASE_SECONDS", _DEFAULT_INDEX_JOB_LEASE_SECONDS)
        if value is None
        else value
    )
    try:
        seconds = int(configured)
    except (TypeError, ValueError):
        seconds = _DEFAULT_INDEX_JOB_LEASE_SECONDS
    return max(1, min(seconds, _MAX_INDEX_JOB_LEASE_SECONDS))


def _replay_window(lease_seconds: int | None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_lease_seconds(lease_seconds))
    return (
        now.isoformat().replace("+00:00", "Z"),
        cutoff.isoformat().replace("+00:00", "Z"),
    )


def _claim_job(
    conn,
    outbox_id: str,
    now: str,
    *,
    lease_cutoff: str,
) -> bool:
    cursor = conn.execute(
        "UPDATE store_outbox SET status = 'processing', updated_at = ?, "
        "next_attempt_at = '' WHERE outbox_id = ? AND ("
        "(status = 'pending' AND (next_attempt_at = '' OR next_attempt_at <= ?)) OR "
        "(status = 'processing' AND updated_at <= ?))",
        (now, outbox_id, now, lease_cutoff),
    )
    conn.commit()
    return cursor.rowcount == 1


def _checked_lancedb(engine: Any):
    ensure_heavy = getattr(engine, "ensure_heavy_init", None)
    if callable(ensure_heavy):
        ensure_heavy()
    lancedb = getattr(engine, "lancedb_store", None)
    if lancedb is None:
        raise RuntimeError("lancedb_store_unavailable")
    return lancedb


def _replace_checked_index_row(lancedb: Any, *, memory_id: str, **kwargs: Any) -> None:
    """Run the test seam immediately before the real checked replacement."""
    consume_test_index_failure(action="upsert", memory_id=memory_id)
    lancedb.replace_checked(memory_id=memory_id, **kwargs)


def _delete_checked_index_row(lancedb: Any, memory_id: str) -> None:
    """Run the test seam immediately before the real checked deletion."""
    consume_test_index_failure(action="delete", memory_id=memory_id)
    lancedb.delete_checked(memory_id)


def _transaction_index_eligible(conn: Any, memory_id: str) -> bool:
    if os.environ.get("PP_SYNTHESIS_RETRIEVAL") != "1":
        return False
    try:
        from plastic_promise.core.synthesis_retrieval import _validate_synthesis

        _validate_synthesis(conn, memory_id, allow_review=False)
    except Exception:
        return False
    return True


def _index_control_state(
    conn: Any,
    memory_id: str,
    *,
    transaction_safe: bool = False,
) -> _IndexControlState:
    row = conn.execute(
        "SELECT status, revision FROM synthesis_artifacts WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return _IndexControlState("", None, False)
    status = str(row[0] or "")
    revision = row[1] if type(row[1]) is int and row[1] >= 1 else None
    eligible = False
    if status == "verified" and revision is not None:
        eligible = (
            _transaction_index_eligible(conn, memory_id)
            if transaction_safe
            else synthesis_index_eligible(conn, memory_id)
        )
    return _IndexControlState(status, revision, eligible)


def _matches_current_upsert(state: _IndexControlState, revision: int) -> bool:
    return state.eligible and state.revision == revision


def _require_lease_owner(
    conn: Any,
    lease_owner: tuple[str, str] | None,
) -> None:
    if lease_owner is None:
        return
    outbox_id, claimed_at = lease_owner
    row = conn.execute(
        "SELECT 1 FROM store_outbox "
        "WHERE outbox_id = ? AND status = 'processing' AND updated_at = ?",
        (outbox_id, claimed_at),
    ).fetchone()
    if row is None:
        raise _LeaseOwnershipLost("index_job_lease_lost")


@contextmanager
def _index_state_lock(
    conn: Any,
    *,
    lease_owner: tuple[str, str] | None = None,
):
    if bool(conn.in_transaction):
        raise RuntimeError("canonical_transaction_open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_lease_owner(conn, lease_owner)
        yield
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    else:
        conn.commit()


def _delete_if_currently_ineligible(
    conn: Any,
    lancedb: Any,
    memory_id: str,
    *,
    lease_owner: tuple[str, str] | None = None,
) -> None:
    with _index_state_lock(conn, lease_owner=lease_owner):
        if _index_control_state(
            conn,
            memory_id,
            transaction_safe=True,
        ).eligible:
            return
        _delete_checked_index_row(lancedb, memory_id)


def _runtime_embedder(engine: Any, *, unavailable_reason: str):
    state = getattr(engine, "__dict__", {})
    embedder = state.get("_embedder") if isinstance(state, dict) else None
    if embedder is None or not callable(getattr(embedder, "embed", None)):
        raise RuntimeError(unavailable_reason)
    model_name = str(
        getattr(embedder, "model_name", None)
        or getattr(embedder, "model", None)
        or embedder.__class__.__name__
    )
    return embedder, model_name


def _ordinary_index_state(
    engine: Any,
    conn: Any,
    memory_id: str,
    *,
    model_name: str | None,
) -> tuple[str, dict[str, Any] | None, Any | None]:
    row = conn.execute(
        "SELECT memories.memory_type, EXISTS("
        "SELECT 1 FROM synthesis_artifacts "
        "WHERE synthesis_artifacts.memory_id = memories.id) "
        "FROM memories WHERE memories.id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        return "missing", None, None
    if str(row[0] or "").strip().casefold() == "synthesis" or bool(row[1]):
        return "reserved", None, None
    memory = _canonical_memory(engine, memory_id)
    if memory is None:
        return "missing", None, None
    try:
        from plastic_promise.core.synthesis_retrieval import _source_is_available

        available = bool(_source_is_available(memory))
    except Exception:
        return "invalid", memory, None
    material = read_persisted_index_material(memory, model_name=model_name)
    if not available:
        return "blocked", memory, material
    if material is None:
        return "invalid", memory, None
    return "valid", memory, material


def _parse_memory_index_job(
    payload: dict[str, Any],
    *,
    job_schema: str,
) -> _MemoryIndexJob:
    """Validate a durable V2/V3 ordinary-index payload without guessing fields."""
    if job_schema == _LEGACY_MEMORY_INDEX_JOB_SCHEMA:
        expected_keys = {
            "action",
            "embedding_hash",
            "material_revision",
            "memory_id",
            "memory_version",
        }
        expected_hash_key = "embedding_hash"
        project_id = ""
    elif job_schema == _MEMORY_INDEX_JOB_SCHEMA:
        expected_keys = {
            "action",
            "expected_embedding_hash",
            "material_revision",
            "memory_id",
            "memory_version",
            "project_id",
        }
        expected_hash_key = "expected_embedding_hash"
        project_id = ""
    else:
        raise ValueError("invalid_index_job_schema")

    string_fields = {
        "action",
        expected_hash_key,
        "material_revision",
        "memory_id",
    }
    if job_schema == _MEMORY_INDEX_JOB_SCHEMA:
        string_fields.add("project_id")
    if set(payload) != expected_keys or any(
        type(payload.get(field)) is not str for field in string_fields
    ):
        raise ValueError("invalid_memory_index_payload")

    memory_id = payload["memory_id"].strip()
    action = payload["action"].strip().casefold()
    expected_hash = payload[expected_hash_key].strip()
    material_revision = payload["material_revision"].strip()
    if job_schema == _MEMORY_INDEX_JOB_SCHEMA:
        project_id = payload["project_id"].strip()
    memory_version = payload.get("memory_version")
    if (
        not memory_id
        or action not in _INDEX_ACTIONS
        or (job_schema == _LEGACY_MEMORY_INDEX_JOB_SCHEMA and action != "upsert")
        or not expected_hash
        or material_revision != expected_hash
        or type(memory_version) is not int
        or memory_version < 0
        or (job_schema == _MEMORY_INDEX_JOB_SCHEMA and not project_id)
    ):
        raise ValueError("invalid_memory_index_payload")
    return _MemoryIndexJob(
        schema=job_schema,
        action=action,
        memory_id=memory_id,
        expected_embedding_hash=expected_hash,
        material_revision=material_revision,
        memory_version=memory_version,
        project_id=project_id,
    )


def _memory_index_job_matches_current(
    conn: Any,
    job: _MemoryIndexJob,
    memory: dict[str, Any] | None,
    material: Any | None,
) -> bool:
    current_version = read_memory_version(conn)
    if current_version < job.memory_version:
        raise RuntimeError("memory_index_future_revision")
    if job.schema == _MEMORY_INDEX_JOB_SCHEMA and (
        memory is None or str(memory.get("project_id") or "") != job.project_id
    ):
        return False
    return (
        material is not None
        and str(getattr(material, "embedding_hash", "") or "") == job.expected_embedding_hash
    )


def _delete_blocked_ordinary_vector(
    engine: Any,
    conn: Any,
    lancedb: Any,
    job: _MemoryIndexJob,
    *,
    lease_owner: tuple[str, str] | None = None,
) -> None:
    """Remove a vector only after the final lock still observes a tombstone.

    Project and material revisions are upsert identities, not delete guards.
    Once the canonical source is still blocked under the final lock, no vector
    for that memory id is eligible, even if the tombstone drifted after the job
    was queued. A revived source remains protected by the state check.
    """
    with _index_state_lock(conn, lease_owner=lease_owner):
        state, memory, material = _ordinary_index_state(
            engine,
            conn,
            job.memory_id,
            model_name=None,
        )
        if state != "blocked":
            return
        _delete_checked_index_row(lancedb, job.memory_id)


def _delete_unindexable_ordinary_vector(
    engine: Any,
    conn: Any,
    lancedb: Any,
    memory_id: str,
    *,
    lease_owner: tuple[str, str] | None = None,
) -> None:
    """Remove an orphan only when the final canonical state has no index material."""
    with _index_state_lock(conn, lease_owner=lease_owner):
        state, _memory, _material = _ordinary_index_state(
            engine,
            conn,
            memory_id,
            model_name=None,
        )
        if state in {"missing", "invalid"}:
            _delete_checked_index_row(lancedb, memory_id)


def _replay_memory_index_job(
    engine: Any,
    payload: dict[str, Any],
    *,
    job_schema: str = _MEMORY_INDEX_JOB_SCHEMA,
    lease_owner: tuple[str, str] | None = None,
) -> None:
    conn = _canonical_connection(engine)
    if conn is None:
        raise RuntimeError("canonical_store_unavailable")
    job = _parse_memory_index_job(payload, job_schema=job_schema)
    if read_memory_version(conn) < job.memory_version:
        raise RuntimeError("memory_index_future_revision")

    lancedb = _checked_lancedb(engine)
    if job.action == "delete":
        _delete_blocked_ordinary_vector(
            engine,
            conn,
            lancedb,
            job,
            lease_owner=lease_owner,
        )
        return

    initial_state, initial_memory, initial_material = _ordinary_index_state(
        engine,
        conn,
        job.memory_id,
        model_name=None,
    )
    if initial_state == "blocked":
        # A current tombstone must never be resurrected by a delayed upsert.
        _delete_blocked_ordinary_vector(
            engine,
            conn,
            lancedb,
            job,
            lease_owner=lease_owner,
        )
        return
    if initial_state in {"missing", "invalid"}:
        _delete_unindexable_ordinary_vector(
            engine,
            conn,
            lancedb,
            job.memory_id,
            lease_owner=lease_owner,
        )
        return
    if initial_state != "valid" or not _memory_index_job_matches_current(
        conn,
        job,
        initial_memory,
        initial_material,
    ):
        return

    embedder, model_name = _runtime_embedder(
        engine,
        unavailable_reason="memory_index_embedder_unavailable",
    )
    state, memory, material = _ordinary_index_state(
        engine,
        conn,
        job.memory_id,
        model_name=model_name,
    )
    if state == "blocked":
        _delete_blocked_ordinary_vector(
            engine,
            conn,
            lancedb,
            job,
            lease_owner=lease_owner,
        )
        return
    if state != "valid" or not _memory_index_job_matches_current(
        conn,
        job,
        memory,
        material,
    ):
        return

    vector = embedder.embed(material.vector_text)
    if not isinstance(vector, list) or len(vector) != 1024 or not any(vector):
        raise RuntimeError("memory_index_embedding_invalid")

    with _index_state_lock(conn, lease_owner=lease_owner):
        locked_state, locked_memory, locked_material = _ordinary_index_state(
            engine,
            conn,
            job.memory_id,
            model_name=model_name,
        )
        if locked_state == "blocked":
            _delete_checked_index_row(lancedb, job.memory_id)
            return
        if locked_state in {"missing", "invalid"}:
            _delete_checked_index_row(lancedb, job.memory_id)
            return
        if (
            locked_state != "valid"
            or locked_material != material
            or not _memory_index_job_matches_current(
                conn,
                job,
                locked_memory,
                locked_material,
            )
        ):
            return
        _replace_checked_index_row(
            lancedb,
            memory_id=job.memory_id,
            vector=vector,
            text=locked_material.search_text,
            tier=str(locked_memory.get("tier") or "L1"),
            category=str(locked_memory.get("category") or "other"),
            scope=str(locked_memory.get("scope") or "global"),
        )


def _replay_index_job(
    engine: Any,
    payload: dict[str, Any],
    *,
    job_schema: str = _SYNTHESIS_INDEX_JOB_SCHEMA,
    lease_owner: tuple[str, str] | None = None,
) -> None:
    conn = _canonical_connection(engine)
    if conn is None:
        raise RuntimeError("canonical_store_unavailable")
    if job_schema != _SYNTHESIS_INDEX_JOB_SCHEMA:
        raise ValueError("invalid_index_job_schema")
    memory_id = str(payload.get("memory_id") or "")
    revision = payload.get("revision")
    action = str(payload.get("action") or "")
    if (
        set(payload) != {"action", "memory_id", "revision"}
        or not memory_id
        or type(revision) is not int
        or revision < 1
        or action not in _INDEX_ACTIONS
    ):
        raise ValueError("invalid_synthesis_index_payload")

    control = _index_control_state(conn, memory_id)
    if control.eligible and (action != "upsert" or control.revision != revision):
        return
    lancedb = _checked_lancedb(engine)
    if not control.eligible:
        _delete_if_currently_ineligible(
            conn,
            lancedb,
            memory_id,
            lease_owner=lease_owner,
        )
        return

    memory = _canonical_memory(engine, memory_id)
    if memory is None:
        raise RuntimeError("canonical_memory_unavailable")
    embedder, model_name = _runtime_embedder(
        engine,
        unavailable_reason="synthesis_embedder_unavailable",
    )
    material = read_persisted_index_material(memory, model_name=model_name)
    if material is None:
        raise RuntimeError("synthesis_index_material_unavailable")
    vector = embedder.embed(material.vector_text)
    if not isinstance(vector, list) or len(vector) != 1024 or not any(vector):
        raise RuntimeError("synthesis_embedding_invalid")

    after_embedding = _index_control_state(conn, memory_id)
    if not _matches_current_upsert(after_embedding, revision):
        if not after_embedding.eligible:
            _delete_if_currently_ineligible(
                conn,
                lancedb,
                memory_id,
                lease_owner=lease_owner,
            )
        return

    with _index_state_lock(conn, lease_owner=lease_owner):
        locked_state = _index_control_state(
            conn,
            memory_id,
            transaction_safe=True,
        )
        if not _matches_current_upsert(locked_state, revision):
            if not locked_state.eligible:
                _delete_checked_index_row(lancedb, memory_id)
            return
        locked_memory = _canonical_memory(engine, memory_id)
        locked_material = (
            read_persisted_index_material(locked_memory, model_name=model_name)
            if locked_memory is not None
            else None
        )
        if locked_material != material:
            raise RuntimeError("synthesis_index_material_changed")
        _replace_checked_index_row(
            lancedb,
            memory_id=memory_id,
            vector=vector,
            text=material.search_text,
            tier=str(locked_memory.get("tier") or "L1"),
            category=str(locked_memory.get("category") or "other"),
            scope=str(locked_memory.get("scope") or "global"),
        )
        final_state = _index_control_state(
            conn,
            memory_id,
            transaction_safe=True,
        )
        final_memory = _canonical_memory(engine, memory_id)
        final_material = (
            read_persisted_index_material(final_memory, model_name=model_name)
            if final_memory is not None
            else None
        )
        if not _matches_current_upsert(final_state, revision) or final_material != material:
            _delete_checked_index_row(lancedb, memory_id)


def _replay_outbox_jobs(
    engine: Any,
    *,
    tool_name: str,
    replay_job: Any,
    job_schema: str | frozenset[str],
    invalid_payload_reason: str,
    limit: int,
    lease_seconds: int | None,
) -> ReplayReport:
    conn = _canonical_connection(engine)
    if conn is None:
        return ReplayReport(0, 0, 0, 1, 0, (), ("canonical_store_unavailable",))
    if conn.in_transaction:
        return ReplayReport(0, 0, 0, 1, 0, (), ("canonical_transaction_open",))
    ensure_traceability_schema(conn)
    accepted_schemas = (
        frozenset({job_schema}) if isinstance(job_schema, str) else frozenset(job_schema)
    )
    limit = max(0, min(int(limit), 1000))
    now, lease_cutoff = _replay_window(lease_seconds)
    rows = conn.execute(
        "SELECT outbox_id, payload_json, metadata_json, attempt_count FROM store_outbox "
        "WHERE tool_name = ? AND ("
        "(status = 'pending' AND (next_attempt_at = '' OR next_attempt_at <= ?)) OR "
        "(status = 'processing' AND updated_at <= ?)) "
        "ORDER BY created_at, outbox_id LIMIT ?",
        (tool_name, now, lease_cutoff, limit),
    ).fetchall()
    claimed = succeeded = failed = skipped = 0
    done_ids: list[str] = []
    failed_ids: list[str] = []
    for outbox_id_raw, payload_json, metadata_json, attempt_count_raw in rows:
        outbox_id = str(outbox_id_raw)
        claimed_at = utc_now()
        if not _claim_job(
            conn,
            outbox_id,
            claimed_at,
            lease_cutoff=lease_cutoff,
        ):
            skipped += 1
            continue
        claimed += 1
        try:
            payload = json.loads(payload_json)
            metadata = json.loads(metadata_json)
            schema = metadata.get("job_schema") if isinstance(metadata, dict) else None
            if schema not in accepted_schemas:
                raise ValueError("invalid_index_job_schema")
            if not isinstance(payload, dict):
                raise ValueError(invalid_payload_reason)
            replay_job(
                engine,
                payload,
                job_schema=str(schema),
                lease_owner=(outbox_id, claimed_at),
            )
        except Exception as exc:
            attempts = max(0, int(attempt_count_raw or 0)) + 1
            delay_seconds = min(300, 2 ** min(attempts, 8))
            next_attempt = (
                (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
                .isoformat()
                .replace("+00:00", "Z")
            )
            cursor = conn.execute(
                "UPDATE store_outbox SET status = 'pending', attempt_count = ?, "
                "updated_at = ?, next_attempt_at = ?, error_class = ?, error_message = ? "
                "WHERE outbox_id = ? AND status = 'processing' AND updated_at = ?",
                (
                    attempts,
                    utc_now(),
                    next_attempt,
                    exc.__class__.__name__[:128],
                    str(exc)[:500],
                    outbox_id,
                    claimed_at,
                ),
            )
            conn.commit()
            if cursor.rowcount == 1:
                failed += 1
                failed_ids.append(outbox_id)
            else:
                skipped += 1
        else:
            cursor = conn.execute(
                "UPDATE store_outbox SET status = 'done', updated_at = ?, "
                "next_attempt_at = '', error_class = '', error_message = '' "
                "WHERE outbox_id = ? AND status = 'processing' AND updated_at = ?",
                (utc_now(), outbox_id, claimed_at),
            )
            conn.commit()
            if cursor.rowcount == 1:
                succeeded += 1
                done_ids.append(outbox_id)
            else:
                skipped += 1
    return ReplayReport(
        selected=len(rows),
        claimed=claimed,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        done_ids=tuple(done_ids),
        failed_ids=tuple(failed_ids),
    )


def replay_memory_index_jobs(
    engine: Any,
    *,
    limit: int = 100,
    lease_seconds: int | None = None,
) -> ReplayReport:
    """Replay durable ordinary-memory index jobs against canonical material."""
    validate_test_index_failure_configuration()
    return _replay_outbox_jobs(
        engine,
        tool_name="memory_index",
        replay_job=_replay_memory_index_job,
        job_schema=frozenset({_LEGACY_MEMORY_INDEX_JOB_SCHEMA, _MEMORY_INDEX_JOB_SCHEMA}),
        invalid_payload_reason="invalid_memory_index_payload",
        limit=limit,
        lease_seconds=lease_seconds,
    )


def replay_synthesis_index_jobs(
    engine: Any,
    *,
    limit: int = 100,
    lease_seconds: int | None = None,
) -> ReplayReport:
    """Replay synthesis jobs and reclaim only expired processing leases."""
    validate_test_index_failure_configuration()
    return _replay_outbox_jobs(
        engine,
        tool_name="synthesis_index",
        replay_job=_replay_index_job,
        job_schema=_SYNTHESIS_INDEX_JOB_SCHEMA,
        invalid_payload_reason="invalid_synthesis_index_payload",
        limit=limit,
        lease_seconds=lease_seconds,
    )


def _refresh_engine(engine: Any) -> None:
    refresh = getattr(engine, "_refresh_canonical_cache_if_changed", None)
    if callable(refresh):
        refresh(force=True)


def scan_synthesis_integrity(
    engine: Any,
    *,
    repair_metadata: bool = True,
) -> ScanReport:
    """Transition invalid verified synthesis and atomically queue index deletion."""
    conn = _canonical_connection(engine)
    if conn is None:
        return ScanReport(
            0,
            (),
            (),
            (),
            ({"memory_id": "", "reason": "canonical_store_unavailable"},),
        )
    if conn.in_transaction:
        return ScanReport(
            0,
            (),
            (),
            (),
            ({"memory_id": "", "reason": "canonical_transaction_open"},),
        )

    store = SynthesisStore(conn, engine=engine)
    artifacts = store.list(status="verified")
    stale_ids: list[str] = []
    contested_ids: list[str] = []
    queued_job_ids: list[str] = []
    retryable: list[dict[str, str]] = []
    for artifact in artifacts:
        try:
            findings = store.lint(memory_id=artifact.memory_id)
            if not findings:
                if repair_metadata:
                    conn.execute(
                        "UPDATE synthesis_artifacts SET last_linted_at = ? "
                        "WHERE memory_id = ? AND status = 'verified' AND revision = ?",
                        (utc_now(), artifact.memory_id, artifact.revision),
                    )
                    conn.commit()
                continue

            codes = sorted({str(finding["code"]) for finding in findings})
            is_contested = any(code in _CONTRADICTION_CODES for code in codes)
            call_id = f"synthesis-scan:{secrets.token_hex(8)}"
            conn.execute("BEGIN IMMEDIATE")
            if is_contested:
                store.mark_contested(
                    artifact.memory_id,
                    ",".join(codes),
                    artifact.revision,
                    actor="maintenance_daemon",
                    call_id=call_id,
                )
            else:
                store.mark_stale(
                    artifact.memory_id,
                    ",".join(codes),
                    artifact.revision,
                    actor="maintenance_daemon",
                    call_id=call_id,
                )
            job_id = enqueue_synthesis_index_job(
                conn,
                memory_id=artifact.memory_id,
                revision=artifact.revision,
                action="delete",
                call_id=call_id,
            )
            for finding in findings:
                record_memory_lineage(
                    conn,
                    memory_id=artifact.memory_id,
                    parent_memory_id=str(finding.get("source_id") or artifact.memory_id),
                    relation="synthesis_invalidated",
                    call_id=call_id,
                    metadata={"code": finding["code"]},
                )
            conn.commit()
            queued_job_ids.append(job_id)
            if is_contested:
                contested_ids.append(artifact.memory_id)
            else:
                stale_ids.append(artifact.memory_id)
            _refresh_engine(engine)
        except Exception as exc:
            if conn.in_transaction:
                conn.rollback()
            retryable.append(
                {
                    "memory_id": artifact.memory_id,
                    "reason": "synthesis_scan_retryable",
                    "error_class": exc.__class__.__name__,
                }
            )
    return ScanReport(
        scanned=len(artifacts),
        stale_ids=tuple(stale_ids),
        contested_ids=tuple(contested_ids),
        queued_job_ids=tuple(queued_job_ids),
        retryable_findings=tuple(retryable),
    )
