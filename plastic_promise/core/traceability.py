"""Local traceability helpers for call spans and degradation events."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

MEMORY_INDEX_JOB_SCHEMA = "memory-index/v3"
_MEMORY_INDEX_ACTIONS = frozenset({"upsert", "delete"})
_MAINTENANCE_CYCLE_STAGES = (
    "memory_lifecycle",
    "proposal_expiry",
    "synthesis_integrity",
    "memory_index_replay",
    "synthesis_index_replay",
    "audit",
)


class TraceabilityStore:
    """Durable reader for strict maintenance cycle span trees."""

    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        ensure_traceability_schema(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def get_cycle_span_tree(
        self, cycle_call_id: str
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        root_row = self._conn.execute(
            "SELECT * FROM call_spans WHERE call_id = ?",
            (cycle_call_id,),
        ).fetchone()
        child_rows = self._conn.execute(
            "SELECT * FROM call_spans WHERE parent_call_id = ? AND call_id <> ?",
            (cycle_call_id, cycle_call_id),
        ).fetchall()
        root = self._span_dict(root_row) if root_row is not None else None
        children = [self._span_dict(row) for row in child_rows]
        children.sort(key=self._span_order)
        if not self._valid_cycle_tree(cycle_call_id, root, children):
            raise ValueError("invalid_maintenance_cycle_span_tree")
        return root, tuple(children)

    @staticmethod
    def _span_order(span: dict[str, Any]) -> int:
        metadata = span.get("metadata")
        order = metadata.get("order") if isinstance(metadata, dict) else None
        return order if type(order) is int else -1

    @staticmethod
    def _span_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = None
        return {
            "call_id": str(row["call_id"] or ""),
            "parent_call_id": str(row["parent_call_id"] or ""),
            "tool_name": str(row["tool_name"] or ""),
            "stage": str(row["stage_name"] or ""),
            "status": str(row["status"] or ""),
            "degraded": bool(row["degraded"]),
            "metadata": metadata,
            "started_at": str(row["started_at"] or ""),
            "ended_at": str(row["ended_at"] or ""),
        }

    @staticmethod
    def _valid_cycle_tree(
        cycle_call_id: str,
        root: dict[str, Any] | None,
        children: list[dict[str, Any]],
    ) -> bool:
        if root is None or root["call_id"] != cycle_call_id:
            return False
        if root["parent_call_id"] == cycle_call_id or root["stage"] != "maintenance_cycle":
            return False
        if not isinstance(root["metadata"], dict) or len(children) != 6:
            return False
        if [child["stage"] for child in children] != list(_MAINTENANCE_CYCLE_STAGES):
            return False
        orders = [
            child["metadata"].get("order") if isinstance(child["metadata"], dict) else None
            for child in children
        ]
        if orders != list(range(1, 7)):
            return False
        return all(
            child["call_id"] != cycle_call_id and child["parent_call_id"] == cycle_call_id
            for child in children
        )


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_call_id() -> str:
    return f"call_{secrets.token_hex(8)}"


def _metadata_json(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, ensure_ascii=False)


def _table_columns(conn, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_traceability_schema(conn) -> None:
    caller_owned_transaction = bool(conn.in_transaction)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_spans (
            call_id TEXT PRIMARY KEY,
            parent_call_id TEXT NOT NULL DEFAULT '',
            request_scope_id TEXT NOT NULL DEFAULT '',
            stage_session_id TEXT NOT NULL DEFAULT '',
            flow_line_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL,
            stage_name TEXT NOT NULL DEFAULT '',
            caller TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'success',
            degraded INTEGER NOT NULL DEFAULT 0,
            input_hash TEXT NOT NULL DEFAULT '',
            output_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_lineage (
            lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL DEFAULT '',
            parent_memory_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            relation TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS degradation_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            request_scope_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            link_name TEXT NOT NULL,
            policy TEXT NOT NULL,
            level TEXT NOT NULL,
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            fallback_used TEXT NOT NULL DEFAULT '',
            minimum_result TEXT NOT NULL DEFAULT '',
            user_visible INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS store_outbox (
            outbox_id TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            error_class TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_events (
            event_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_kind TEXT NOT NULL,
            event_name TEXT NOT NULL,
            status TEXT NOT NULL,
            request_scope_id TEXT NOT NULL DEFAULT '',
            stage_session_id TEXT NOT NULL DEFAULT '',
            flow_line_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            trust_tier TEXT NOT NULL DEFAULT '',
            defense_decision TEXT NOT NULL DEFAULT '',
            audit_trace_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    call_span_columns = _table_columns(conn, "call_spans")
    if "started_at" not in call_span_columns:
        conn.execute("ALTER TABLE call_spans ADD COLUMN started_at TEXT NOT NULL DEFAULT ''")
        call_span_columns.add("started_at")
    if "ended_at" not in call_span_columns:
        conn.execute("ALTER TABLE call_spans ADD COLUMN ended_at TEXT")
        call_span_columns.add("ended_at")
    if "created_at" in call_span_columns:
        conn.execute(
            """
            UPDATE call_spans
            SET started_at = created_at
            WHERE started_at = '' AND created_at IS NOT NULL
            """
        )
    if "updated_at" in call_span_columns:
        conn.execute(
            """
            UPDATE call_spans
            SET ended_at = updated_at
            WHERE (ended_at IS NULL OR ended_at = '') AND updated_at IS NOT NULL
            """
        )
    lineage_columns = _table_columns(conn, "memory_lineage")
    if "parent_memory_id" not in lineage_columns:
        conn.execute(
            "ALTER TABLE memory_lineage ADD COLUMN parent_memory_id TEXT NOT NULL DEFAULT ''"
        )
    outbox_columns = _table_columns(conn, "store_outbox")
    if "dedupe_key" not in outbox_columns:
        conn.execute("ALTER TABLE store_outbox ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
    if "attempt_count" not in outbox_columns:
        conn.execute("ALTER TABLE store_outbox ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if "updated_at" not in outbox_columns:
        conn.execute("ALTER TABLE store_outbox ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    if "next_attempt_at" not in outbox_columns:
        conn.execute("ALTER TABLE store_outbox ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE store_outbox SET updated_at = created_at WHERE updated_at = ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_store_outbox_active_dedupe "
        "ON store_outbox(dedupe_key) "
        "WHERE dedupe_key <> '' AND status IN ('pending', 'processing')"
    )
    if not caller_owned_transaction and conn.in_transaction:
        conn.commit()


def record_memory_lineage(
    conn,
    *,
    memory_id: str,
    parent_memory_id: str,
    relation: str,
    call_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert one lineage edge without taking ownership of the caller transaction."""
    project_row = conn.execute(
        "SELECT project_id FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    project_id = str(project_row[0] or "") if project_row else ""
    span_row = conn.execute(
        "SELECT request_scope_id FROM call_spans WHERE call_id = ?",
        (call_id,),
    ).fetchone()
    request_scope_id = str(span_row[0] or "") if span_row else ""
    cursor = conn.execute(
        """
        INSERT INTO memory_lineage (
            memory_id,
            parent_memory_id,
            call_id,
            request_scope_id,
            project_id,
            relation,
            metadata_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            parent_memory_id,
            call_id,
            request_scope_id,
            project_id,
            relation,
            _metadata_json(metadata),
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def _default_outbox_path() -> Path:
    try:
        from plastic_promise.core.paths import get_db_path

        return Path(get_db_path()).with_name("store_outbox.jsonl")
    except Exception:
        return Path("store_outbox.jsonl")


def record_outbox_event(
    conn,
    *,
    tool_name: str,
    project_id: str,
    call_id: str,
    status: str,
    payload: dict[str, Any],
    error_class: str = "",
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
    fallback_path: str | Path | None = None,
) -> str:
    outbox_id = f"outbox_{secrets.token_hex(8)}"
    created_at = utc_now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    metadata_json = _metadata_json(metadata)

    try:
        ensure_traceability_schema(conn)
        conn.execute(
            """
            INSERT INTO store_outbox (
                outbox_id,
                tool_name,
                project_id,
                call_id,
                status,
                payload_json,
                error_class,
                error_message,
                metadata_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                tool_name,
                project_id,
                call_id,
                status,
                payload_json,
                error_class,
                error_message,
                metadata_json,
                created_at,
            ),
        )
        conn.commit()
        return outbox_id
    except Exception:
        path = Path(fallback_path) if fallback_path is not None else _default_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "outbox_id": outbox_id,
            "tool_name": tool_name,
            "project_id": project_id,
            "call_id": call_id,
            "status": status,
            "payload": payload or {},
            "error_class": error_class,
            "error_message": error_message,
            "metadata": metadata or {},
            "created_at": created_at,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return outbox_id


def enqueue_memory_index_job(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    project_id: str,
    action: Literal["upsert", "delete"],
    expected_embedding_hash: str,
    call_id: str,
) -> str:
    """Publish one canonical-state-bound ordinary-memory index job.

    The caller may already own the SQLite transaction that changes the memory.
    In that case the outbox publication participates in the same transaction;
    derived LanceDB work remains a post-commit consumer concern.
    """
    ensure_traceability_schema(conn)
    memory_id = str(memory_id or "").strip()
    project_id = str(project_id or "").strip()
    action = str(action or "").strip()
    expected_embedding_hash = str(expected_embedding_hash or "").strip()
    if action not in _MEMORY_INDEX_ACTIONS:
        raise ValueError("invalid_memory_index_action")
    if not memory_id or not project_id or not expected_embedding_hash:
        raise ValueError("invalid_memory_index_job")

    owns_transaction = not bool(conn.in_transaction)
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        canonical = conn.execute(
            "SELECT project_id, embedding_hash FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if canonical is None:
            raise ValueError("memory_index_canonical_missing")
        if str(canonical[0] or "") != project_id:
            raise ValueError("memory_index_project_mismatch")
        if str(canonical[1] or "") != expected_embedding_hash:
            raise ValueError("memory_index_material_mismatch")
        version_rows = conn.execute(
            "SELECT version FROM memory_version WHERE singleton = 1"
        ).fetchall()
        if len(version_rows) != 1 or type(version_rows[0][0]) is not int or version_rows[0][0] < 0:
            raise ValueError("memory_version_unavailable")
        memory_version = int(version_rows[0][0])
        dedupe_key = "memory-index:" + json.dumps(
            {
                "action": action,
                "expected_embedding_hash": expected_embedding_hash,
                "memory_id": memory_id,
                "memory_version": memory_version,
                "project_id": project_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        existing = conn.execute(
            "SELECT outbox_id FROM store_outbox "
            "WHERE dedupe_key = ? AND status IN ('pending', 'processing') LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            if owns_transaction:
                conn.commit()
            return str(existing[0])

        outbox_id = f"outbox_{secrets.token_hex(8)}"
        now = utc_now()
        payload_json = json.dumps(
            {
                "action": action,
                "expected_embedding_hash": expected_embedding_hash,
                "material_revision": expected_embedding_hash,
                "memory_id": memory_id,
                "memory_version": memory_version,
                "project_id": project_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO store_outbox (
                outbox_id, tool_name, project_id, call_id, status, payload_json,
                error_class, error_message, metadata_json, created_at,
                dedupe_key, attempt_count, updated_at, next_attempt_at
            ) VALUES (?, 'memory_index', ?, ?, 'pending', ?, '', '', ?, ?, ?, 0, ?, '')
            """,
            (
                outbox_id,
                project_id,
                call_id,
                payload_json,
                json.dumps(
                    {"job_schema": MEMORY_INDEX_JOB_SCHEMA},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
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
        concurrent = conn.execute(
            "SELECT outbox_id FROM store_outbox "
            "WHERE dedupe_key = ? AND status IN ('pending', 'processing') LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if concurrent is not None:
            return str(concurrent[0])
        raise
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.rollback()
        raise


def _resolve_expected_embedding_hash(
    *,
    expected_embedding_hash: str | None,
    embedding_hash: str | None,
) -> str:
    """Accept the former keyword while rejecting ambiguous producer input."""
    expected = str(expected_embedding_hash or "").strip()
    legacy = str(embedding_hash or "").strip()
    if expected and legacy and expected != legacy:
        raise ValueError("memory_index_material_mismatch")
    return expected or legacy


def enqueue_memory_index_upsert(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    project_id: str,
    call_id: str,
    expected_embedding_hash: str | None = None,
    embedding_hash: str | None = None,
) -> str:
    """Enqueue an ordinary-memory index upsert using the V3 contract.

    ``embedding_hash`` remains a compatibility alias for existing producer
    call sites. New callers should provide ``expected_embedding_hash``.
    """
    return enqueue_memory_index_job(
        conn,
        memory_id=memory_id,
        project_id=project_id,
        action="upsert",
        expected_embedding_hash=_resolve_expected_embedding_hash(
            expected_embedding_hash=expected_embedding_hash,
            embedding_hash=embedding_hash,
        ),
        call_id=call_id,
    )


def enqueue_memory_index_delete(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    project_id: str,
    call_id: str,
    expected_embedding_hash: str | None = None,
    embedding_hash: str | None = None,
) -> str:
    """Enqueue an ordinary-memory index delete using the V3 contract."""
    return enqueue_memory_index_job(
        conn,
        memory_id=memory_id,
        project_id=project_id,
        action="delete",
        expected_embedding_hash=_resolve_expected_embedding_hash(
            expected_embedding_hash=expected_embedding_hash,
            embedding_hash=embedding_hash,
        ),
        call_id=call_id,
    )


def record_call_span(
    conn,
    *,
    call_id: str,
    parent_call_id: str = "",
    request_scope_id: str = "",
    stage_session_id: str = "",
    flow_line_id: str = "",
    project_id: str = "",
    tool_name: str,
    stage_name: str = "",
    caller: str = "",
    status: str = "success",
    degraded: bool = False,
    input_hash: str = "",
    output_hash: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    now = utc_now()
    columns = [
        "call_id",
        "parent_call_id",
        "request_scope_id",
        "stage_session_id",
        "flow_line_id",
        "project_id",
        "tool_name",
        "stage_name",
        "caller",
        "status",
        "degraded",
        "input_hash",
        "output_hash",
        "metadata_json",
        "started_at",
        "ended_at",
    ]
    values = [
        call_id,
        parent_call_id,
        request_scope_id,
        stage_session_id,
        flow_line_id,
        project_id,
        tool_name,
        stage_name,
        caller,
        status,
        int(degraded),
        input_hash,
        output_hash,
        _metadata_json(metadata),
        now,
        now,
    ]
    call_span_columns = _table_columns(conn, "call_spans")
    update_assignments = [
        "parent_call_id = excluded.parent_call_id",
        "request_scope_id = excluded.request_scope_id",
        "stage_session_id = excluded.stage_session_id",
        "flow_line_id = excluded.flow_line_id",
        "project_id = excluded.project_id",
        "tool_name = excluded.tool_name",
        "stage_name = excluded.stage_name",
        "caller = excluded.caller",
        "ended_at = excluded.ended_at",
        "status = excluded.status",
        "degraded = excluded.degraded",
        "input_hash = excluded.input_hash",
        "output_hash = excluded.output_hash",
        "metadata_json = excluded.metadata_json",
    ]
    if "created_at" in call_span_columns:
        columns.append("created_at")
        values.append(now)
    if "updated_at" in call_span_columns:
        columns.append("updated_at")
        values.append(now)
        update_assignments.append("updated_at = excluded.updated_at")

    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""
        INSERT INTO call_spans ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(call_id) DO UPDATE SET
            {", ".join(update_assignments)}
        """,
        values,
    )
    conn.commit()


def record_degradation_event(
    conn,
    *,
    call_id: str,
    request_scope_id: str,
    project_id: str,
    tool_name: str,
    link_name: str,
    policy: str,
    level: str,
    error_class: str = "",
    error_message: str = "",
    fallback_used: str = "",
    minimum_result: str = "",
    user_visible: bool = True,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO degradation_events (
            call_id,
            request_scope_id,
            project_id,
            tool_name,
            link_name,
            policy,
            level,
            error_class,
            error_message,
            fallback_used,
            minimum_result,
            user_visible,
            metadata_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            request_scope_id,
            project_id,
            tool_name,
            link_name,
            policy,
            level,
            error_class,
            error_message,
            fallback_used,
            minimum_result,
            int(user_visible),
            _metadata_json(metadata),
            utc_now(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _engine_conn(engine: Any):
    sqlite = getattr(engine, "_sqlite", None)
    return getattr(sqlite, "_conn", None)


def safe_record_call_span(engine: Any, **kwargs: Any) -> bool:
    """Best-effort handler trace write; never block the user path."""
    conn = _engine_conn(engine)
    if conn is None:
        return False
    try:
        ensure_traceability_schema(conn)
        record_call_span(conn, **kwargs)
        return True
    except Exception:
        return False


def safe_record_degradation_event(engine: Any, **kwargs: Any) -> bool:
    """Best-effort degradation event write; never block the user path."""
    conn = _engine_conn(engine)
    if conn is None:
        return False
    try:
        ensure_traceability_schema(conn)
        record_degradation_event(conn, **kwargs)
        return True
    except Exception:
        return False


def build_envelope(
    *,
    data: Any,
    trace: dict[str, Any],
    success: bool = True,
    warnings: list[str] | None = None,
    fallback_used: list[str] | None = None,
    minimum_result: str = "",
    degrade_level: str = "",
) -> dict[str, Any]:
    warnings = warnings or []
    fallback_used = fallback_used or []
    degraded = bool(warnings or fallback_used or minimum_result)
    if degraded and not degrade_level:
        degrade_level = "warning"
    if not degraded and not degrade_level:
        degrade_level = "none"

    return {
        "success": success,
        "degraded": degraded,
        "degrade_level": degrade_level,
        "warnings": warnings,
        "fallback_used": fallback_used,
        "minimum_result": minimum_result,
        "trace": trace,
        "data": data,
    }
