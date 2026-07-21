"""Scope-safe, read-only SQLite projections for Dashboard V2."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from plastic_promise.core.chunking import chunk_manifest_hash
from plastic_promise.core.memory_index import chunk_manifest_source_hash
from plastic_promise.core.retrieval_explain import (
    METADATA_KEY,
    sanitize_retrieval_explain_snapshot,
)

if TYPE_CHECKING:
    from plastic_promise.mcp.dashboard_v2.config import DashboardScope


class DashboardCursorError(ValueError):
    """An opaque dashboard cursor is malformed or bound to another query."""


_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "admin_api_key",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
)


def _sensitive_key(key: object) -> bool:
    normalized = str(key).strip().casefold().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("api_key", "credential", "password", "private_key", "secret", "token")
    )


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    return _ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


def redact_value(value: Any) -> Any:
    """Recursively redact credential-shaped keys and inline secret material."""
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _sensitive_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, set):
        return [redact_value(item) for item in sorted(value, key=str)]
    if isinstance(value, bytes):
        return _REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_object(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: object) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _metadata_object(raw: object) -> dict[str, Any]:
    metadata = _json_object(raw)
    return _project_metadata(metadata)


def _project_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(metadata)
    if METADATA_KEY in metadata:
        snapshot = sanitize_retrieval_explain_snapshot(metadata.get(METADATA_KEY))
        if snapshot is None:
            metadata.pop(METADATA_KEY, None)
        else:
            metadata[METADATA_KEY] = snapshot
    return redact_value(metadata)


_MEMORY_METADATA_OMITTED_FIELDS = frozenset(
    {
        "chunk_manifest",
        "embedding_text",
        "l2_content",
        "raw_content",
        "search_text",
        "vector_text",
    }
)


def _public_memory_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Remove full-text index material before exposing arbitrary memory metadata."""

    def project(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): project(item)
                for key, item in value.items()
                if str(key).strip().casefold() not in _MEMORY_METADATA_OMITTED_FIELDS
            }
        if isinstance(value, tuple):
            return tuple(project(item) for item in value)
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, set):
            return [project(item) for item in sorted(value, key=str)]
        return value

    projected = project(metadata)
    return _project_metadata(projected if isinstance(projected, Mapping) else {})


def _span_duration(started_at: object, ended_at: object) -> tuple[float | None, str]:
    """Return a measured duration without treating legacy equal timestamps as zero."""
    start_text = str(started_at or "").strip()
    end_text = str(ended_at or "").strip()
    if not start_text or not end_text or start_text == end_text:
        return None, "not_captured"
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        duration_ms = (end - start).total_seconds() * 1000
    except (TypeError, ValueError):
        return None, "invalid"
    if duration_ms < 0:
        return None, "invalid"
    return round(duration_ms, 3), "measured"


def _project_call_span(row: dict[str, Any]) -> dict[str, Any]:
    row["degraded"] = bool(row["degraded"])
    row["metadata"] = _metadata_object(row.pop("metadata_json"))
    row["duration_ms"], row["duration_status"] = _span_duration(
        row.get("started_at"), row.get("ended_at")
    )
    return row


def _row(cursor: sqlite3.Cursor, raw: Sequence[Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    columns = [str(description[0]) for description in cursor.description or ()]
    return dict(zip(columns, raw, strict=True))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [str(description[0]) for description in cursor.description or ()]
    return [dict(zip(columns, raw, strict=True)) for raw in cursor.fetchall()]


def _limit(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 25
    return max(1, min(parsed, 100))


def _fingerprint(value: object) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_MEMORY_COLUMNS = """
    id, content, memory_type, source, owner, tier, scope, category, tags,
    domain, importance, created_at, access_count, worth_success, worth_failure,
    activation_weight, last_accessed, project_id, visibility, source_class,
    created_by_call_id, origin_kind, origin_uri, origin_ref, metadata_json,
    embedding_text, l0_abstract, l1_summary
"""

# Chunk manifests are persisted inside the canonical memory's index metadata.
# Dashboard projections intentionally cap the material so one unusually large
# memory cannot turn a read-only detail request into an unbounded response.
_MAX_DASHBOARD_CHUNKS = 256
_MAX_CHUNK_TEXT = 12_000
_MAX_CHUNK_HEADER_PARTS = 32


def _bounded_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _chunk_projection(
    raw: object,
    *,
    parent_memory_id: str,
    include_text: bool = True,
) -> dict[str, Any] | None:
    """Return the stable, user-facing fields of one structured chunk."""
    if not isinstance(raw, Mapping):
        return None
    chunk_id = str(raw.get("chunk_id") or raw.get("id") or "").strip()
    if not chunk_id:
        return None
    ordinal = _bounded_int(raw.get("ordinal"))
    source_start = _bounded_int(raw.get("source_start"))
    source_end = _bounded_int(raw.get("source_end"))
    header = raw.get("header_path", raw.get("heading_path", []))
    if isinstance(header, str):
        header_path = [header[:512]] if header else []
    elif isinstance(header, (list, tuple)):
        header_path = [str(part)[:512] for part in header[:_MAX_CHUNK_HEADER_PARTS]]
    else:
        header_path = []
    projected: dict[str, Any] = {
        "chunk_id": chunk_id[:200],
        "parent_memory_id": parent_memory_id,
        "ordinal": ordinal if ordinal is not None else 0,
        "kind": str(raw.get("kind") or "unknown")[:64],
        "header_path": header_path,
        "source_start": source_start if source_start is not None else 0,
        "source_end": source_end if source_end is not None else 0,
        "source_hash": str(raw.get("source_hash") or "")[:128],
        "text_hash": str(raw.get("text_hash") or "")[:128],
        "context_truncated": bool(raw.get("context_truncated", False)),
    }
    if include_text:
        projected["text"] = _redact_text(str(raw.get("text") or "")[:_MAX_CHUNK_TEXT])
    return projected


def _chunk_manifest_projection(
    metadata: Mapping[str, Any],
    *,
    parent_memory_id: str,
    include_text: bool = True,
    max_chunks: int = _MAX_DASHBOARD_CHUNKS,
    source_text: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract a bounded manifest and its chunks from memory metadata."""
    index = metadata.get("memory_index")
    manifest = index.get("chunk_manifest") if isinstance(index, Mapping) else None
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != "structure-v1":
        return {
            "status": "not_recorded",
            "enabled": False,
            "chunk_count": 0,
            "returned_count": 0,
            "projection_limit": max(max_chunks, 0),
            "projection_truncated": False,
        }, []
    stored_hash = index.get("chunk_manifest_hash") if isinstance(index, Mapping) else None
    if not isinstance(stored_hash, str) or not stored_hash:
        return {
            "status": "invalid",
            "enabled": False,
            "chunk_count": 0,
            "returned_count": 0,
            "projection_limit": max(max_chunks, 0),
            "projection_truncated": False,
            "reason": "manifest_hash_missing",
        }, []
    try:
        calculated_hash = chunk_manifest_hash(dict(manifest))
    except (TypeError, ValueError):
        calculated_hash = ""
    if not hmac.compare_digest(stored_hash, calculated_hash):
        return {
            "status": "invalid",
            "enabled": False,
            "chunk_count": 0,
            "returned_count": 0,
            "projection_limit": max(max_chunks, 0),
            "projection_truncated": False,
            "reason": "manifest_hash_mismatch",
        }, []
    if source_text:
        expected_source_hash = chunk_manifest_source_hash(source_text)
        if not hmac.compare_digest(
            str(manifest.get("source_hash") or ""),
            expected_source_hash or "",
        ):
            return {
                "status": "invalid",
                "enabled": False,
                "chunk_count": 0,
                "returned_count": 0,
                "projection_limit": max(max_chunks, 0),
                "projection_truncated": False,
                "reason": "manifest_source_mismatch",
            }, []
    raw_chunks = manifest.get("chunks")
    if not isinstance(raw_chunks, (list, tuple)) or _bounded_int(
        manifest.get("chunk_count")
    ) != len(raw_chunks):
        return {
            "status": "invalid",
            "enabled": False,
            "chunk_count": 0,
            "returned_count": 0,
            "projection_limit": max(max_chunks, 0),
            "projection_truncated": False,
            "reason": "manifest_shape_invalid",
        }, []
    projection_limit = max(int(max_chunks), 0)
    chunks = [
        projected
        for raw in raw_chunks[:projection_limit]
        if (projected := _chunk_projection(
            raw,
            parent_memory_id=parent_memory_id,
            include_text=include_text,
        ))
        is not None
    ]
    summary = {
        "status": "available",
        "enabled": True,
        "schema_version": "structure-v1",
        "chunk_count": _bounded_int(manifest.get("chunk_count")) or len(chunks),
        "source_hash": str(manifest.get("source_hash") or "")[:128],
        "source_chars": _bounded_int(manifest.get("source_chars")) or 0,
        "covered_source_chars": _bounded_int(manifest.get("covered_source_chars")) or 0,
        "last_source_end": _bounded_int(manifest.get("last_source_end")) or 0,
        "truncated": bool(manifest.get("truncated", False)),
        "resource_limited": bool(manifest.get("resource_limited", False)),
        "returned_count": len(chunks),
        "projection_limit": projection_limit,
        "projection_truncated": len(raw_chunks) > len(chunks),
    }
    summary["manifest_hash"] = stored_hash[:128]
    return summary, chunks


def _matching_chunk_projection(
    metadata: Mapping[str, Any],
    *,
    parent_memory_id: str,
    chunk_id: str,
    source_text: str | None = None,
) -> dict[str, Any] | None:
    """Match an explicit chunk against the complete, verified manifest."""
    index = metadata.get("memory_index")
    manifest = index.get("chunk_manifest") if isinstance(index, Mapping) else None
    if not isinstance(manifest, Mapping):
        return None
    summary, _ = _chunk_manifest_projection(
        metadata,
        parent_memory_id=parent_memory_id,
        include_text=False,
        max_chunks=0,
        source_text=source_text,
    )
    if summary.get("status") != "available":
        return None
    raw_chunks = manifest.get("chunks")
    if not isinstance(raw_chunks, (list, tuple)):
        return None
    for raw in raw_chunks:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("chunk_id") or raw.get("id") or "").strip() != chunk_id:
            continue
        return _chunk_projection(
            raw,
            parent_memory_id=parent_memory_id,
            include_text=False,
        )
    return None


def _chunk_anchor_projection(chunk: Mapping[str, Any]) -> dict[str, Any]:
    """Drop chunk body text when a chunk is embedded in a graph edge."""
    return {
        key: chunk[key]
        for key in (
            "chunk_id",
            "parent_memory_id",
            "ordinal",
            "kind",
            "header_path",
            "source_start",
            "source_end",
            "source_hash",
            "text_hash",
            "context_truncated",
        )
        if key in chunk
    }


def _chunk_anchor_summary(
    chunking: object,
    anchors: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    summary = chunking if isinstance(chunking, Mapping) else {}
    total = _bounded_int(summary.get("chunk_count")) or 0
    returned = len(anchors)
    return {
        "total": total,
        "returned": returned,
        "truncated": total > returned,
        "limit": limit,
    }


class DashboardRepository:
    """Project-bound queries over existing canonical and operational tables."""

    def __init__(self, connection: sqlite3.Connection, scope: DashboardScope) -> None:
        self._conn = connection
        self.scope = scope

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _envelope(
        self,
        data: list[dict[str, Any]],
        *,
        total: int,
        limit: int,
        next_cursor: str | None,
    ) -> dict[str, Any]:
        return {
            "data": data,
            "scope": self.scope.to_dict(),
            "page": {
                "limit": limit,
                "total": total,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
            },
            "degraded": False,
            "warnings": [],
        }

    def _encode_cursor(
        self,
        collection: str,
        filters: Mapping[str, Any],
        created_at: object,
        record_id: object,
    ) -> str:
        payload = {
            "v": 1,
            "collection": collection,
            "scope": self.scope.fingerprint,
            "filters": _fingerprint(filters),
            "created_at": str(created_at or ""),
            "record_id": str(record_id or ""),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        wrapper = {
            "payload": payload,
            "fingerprint": hashlib.sha256(b"dashboard-v2-cursor\0" + encoded).hexdigest(),
        }
        raw = json.dumps(wrapper, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_cursor(
        self,
        cursor: str | None,
        collection: str,
        filters: Mapping[str, Any],
    ) -> tuple[str, str] | None:
        if cursor is None:
            return None
        try:
            padding = "=" * (-len(cursor) % 4)
            wrapper = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
            if set(wrapper) != {"payload", "fingerprint"}:
                raise ValueError
            payload = wrapper["payload"]
            if set(payload) != {
                "v",
                "collection",
                "scope",
                "filters",
                "created_at",
                "record_id",
            }:
                raise ValueError
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            expected = hashlib.sha256(b"dashboard-v2-cursor\0" + encoded).hexdigest()
            if not hmac.compare_digest(str(wrapper["fingerprint"]), expected):
                raise ValueError
            if payload["v"] != 1 or payload["collection"] != collection:
                raise DashboardCursorError("cursor_collection_mismatch")
            if payload["scope"] != self.scope.fingerprint:
                raise DashboardCursorError("cursor_scope_mismatch")
            if payload["filters"] != _fingerprint(filters):
                raise DashboardCursorError("cursor_filter_mismatch")
            created_at = payload["created_at"]
            record_id = payload["record_id"]
            if not isinstance(created_at, str) or not isinstance(record_id, str):
                raise ValueError
            return created_at, record_id
        except DashboardCursorError:
            raise
        except Exception as exc:
            raise DashboardCursorError("cursor_invalid") from exc

    def overview(self) -> dict[str, int]:
        project_id = self.scope.project_id
        memory_count = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE project_id = ? OR visibility = 'global'",
            (project_id,),
        ).fetchone()[0]
        request_count = self._conn.execute(
            "SELECT COUNT(*) FROM call_spans WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        synthesis_count = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM synthesis_artifacts AS sa
            JOIN memories AS m ON m.id = sa.memory_id
            WHERE m.project_id = ? OR m.visibility = 'global'
            """,
            (project_id,),
        ).fetchone()[0]
        runtime_count = self._conn.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        degradation_count = self._conn.execute(
            "SELECT COUNT(*) FROM degradation_events WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        outbox_count = self._conn.execute(
            "SELECT COUNT(*) FROM store_outbox WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        pending_outbox_count = self._conn.execute(
            "SELECT COUNT(*) FROM store_outbox WHERE project_id = ? AND status IN ('pending','processing')",
            (project_id,),
        ).fetchone()[0]
        return {
            "memory_count": int(memory_count),
            "request_count": int(request_count),
            "synthesis_count": int(synthesis_count),
            "operation_count": int(runtime_count + degradation_count + outbox_count),
            "runtime_event_count": int(runtime_count),
            "degradation_count": int(degradation_count),
            "outbox_count": int(outbox_count),
            "pending_outbox_count": int(pending_outbox_count),
        }

    def list_requests(
        self,
        *,
        limit: int = 25,
        cursor: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        degraded: bool | None = None,
    ) -> dict[str, Any]:
        bounded = _limit(limit)
        filters = {
            "status": str(status or ""),
            "tool_name": str(tool_name or ""),
            "degraded": degraded,
        }
        keyset = self._decode_cursor(cursor, "requests", filters)
        clauses = ["project_id = ?"]
        params: list[Any] = [self.scope.project_id]
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if tool_name:
            clauses.append("tool_name = ?")
            params.append(str(tool_name))
        if degraded is not None:
            clauses.append("degraded = ?")
            params.append(int(degraded))
        count = self._conn.execute(
            f"SELECT COUNT(*) FROM call_spans WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]
        if keyset:
            clauses.append("(started_at < ? OR (started_at = ? AND call_id < ?))")
            params.extend((keyset[0], keyset[0], keyset[1]))
        query = f"""
            SELECT call_id, parent_call_id, request_scope_id, stage_session_id,
                   flow_line_id, project_id, tool_name, stage_name, caller,
                   status, degraded, input_hash, output_hash, metadata_json,
                   started_at, ended_at
            FROM call_spans
            WHERE {' AND '.join(clauses)}
            ORDER BY started_at DESC, call_id DESC
            LIMIT ?
        """
        rows = _rows(self._conn.execute(query, (*params, bounded + 1)))
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        for row in rows:
            _project_call_span(row)
        next_cursor = (
            self._encode_cursor("requests", filters, rows[-1]["started_at"], rows[-1]["call_id"])
            if has_more and rows
            else None
        )
        return self._envelope(rows, total=int(count), limit=bounded, next_cursor=next_cursor)

    def get_request(self, call_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            """
            SELECT call_id, parent_call_id, request_scope_id, stage_session_id,
                   flow_line_id, project_id, tool_name, stage_name, caller,
                   status, degraded, input_hash, output_hash, metadata_json,
                   started_at, ended_at
            FROM call_spans
            WHERE call_id = ? AND project_id = ?
            """,
            (str(call_id), self.scope.project_id),
        )
        row = _row(cursor, cursor.fetchone())
        if row is None:
            return None
        return _project_call_span(row)

    def list_memories(
        self,
        *,
        limit: int = 25,
        cursor: str | None = None,
        memory_type: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        bounded = _limit(limit)
        filters = {"memory_type": str(memory_type or ""), "query": str(query or "")}
        keyset = self._decode_cursor(cursor, "memories", filters)
        clauses = ["(project_id = ? OR visibility = 'global')"]
        params: list[Any] = [self.scope.project_id]
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(str(memory_type))
        if query:
            clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(str(query))}%")
        count = self._conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]
        if keyset:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend((keyset[0], keyset[0], keyset[1]))
        rows = _rows(
            self._conn.execute(
                f"""
                SELECT {_MEMORY_COLUMNS}
                FROM memories
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, bounded + 1),
            )
        )
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        for row in rows:
            row["content_preview"] = str(row.pop("content") or "")[:300]
            row["tags"] = _json_list(row["tags"])
            source_text = str(row.pop("embedding_text") or "")
            metadata = _json_object(row.pop("metadata_json"))
            chunking, _chunks = _chunk_manifest_projection(
                metadata,
                parent_memory_id=str(row.get("id") or ""),
                include_text=False,
                max_chunks=0,
                source_text=source_text,
            )
            row["metadata"] = _public_memory_metadata(metadata)
            row["chunking"] = chunking
            row["chunk_count"] = chunking["chunk_count"]
        next_cursor = (
            self._encode_cursor("memories", filters, rows[-1]["created_at"], rows[-1]["id"])
            if has_more and rows
            else None
        )
        return self._envelope(rows, total=int(count), limit=bounded, next_cursor=next_cursor)

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            f"""
            SELECT {_MEMORY_COLUMNS}
            FROM memories
            WHERE id = ? AND (project_id = ? OR visibility = 'global')
            """,
            (str(memory_id), self.scope.project_id),
        )
        row = _row(cursor, cursor.fetchone())
        if row is None:
            return None
        row["tags"] = _json_list(row["tags"])
        source_text = str(row.pop("embedding_text") or "")
        metadata = _json_object(row.pop("metadata_json"))
        chunking, chunks = _chunk_manifest_projection(
            metadata,
            parent_memory_id=str(row.get("id") or ""),
            source_text=source_text,
        )
        row["metadata"] = _public_memory_metadata(metadata)
        row["chunking"] = chunking
        row["chunks"] = chunks
        row["chunk_count"] = chunking["chunk_count"]
        return row

    def get_lineage(self, memory_id: str, *, limit: int = 100) -> dict[str, Any] | None:
        anchor = self.get_memory(memory_id)
        if anchor is None:
            return None
        bounded = _limit(limit)
        project_id = self.scope.project_id
        rows = _rows(
            self._conn.execute(
                """
                WITH scoped_memories AS (
                    SELECT id, project_id, visibility
                    FROM memories
                    WHERE project_id = ? OR visibility = 'global'
                )
                SELECT ml.lineage_id, ml.memory_id, ml.parent_memory_id, ml.call_id,
                       ml.request_scope_id, ml.project_id, ml.relation,
                       ml.metadata_json, ml.created_at,
                       child.visibility AS child_visibility,
                       parent.visibility AS parent_visibility,
                       CASE
                         WHEN ml.project_id = ? THEN 'project'
                         ELSE 'legacy_global'
                       END AS evidence_scope
                FROM memory_lineage AS ml
                JOIN scoped_memories AS child ON child.id = ml.memory_id
                JOIN scoped_memories AS parent ON parent.id = ml.parent_memory_id
                WHERE (ml.memory_id = ? OR ml.parent_memory_id = ?)
                  AND (
                    ml.project_id = ?
                    OR (
                      ml.project_id = 'project:legacy-global'
                      AND child.visibility = 'global'
                      AND parent.visibility = 'global'
                    )
                  )
                ORDER BY ml.created_at DESC, ml.lineage_id DESC
                LIMIT ?
                """,
                (
                    project_id,
                    project_id,
                    str(memory_id),
                    str(memory_id),
                    project_id,
                    bounded + 1,
                ),
            )
        )
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        memory_cache: dict[str, dict[str, Any] | None] = {str(memory_id): anchor}
        call_cache: dict[str, dict[str, Any] | None] = {}

        def scoped_memory(value: object) -> dict[str, Any] | None:
            key = str(value or "")
            if key not in memory_cache:
                memory_cache[key] = self.get_memory(key)
            return memory_cache[key]

        def scoped_call(value: object) -> dict[str, Any] | None:
            key = str(value or "")
            if not key:
                return None
            if key not in call_cache:
                call_cache[key] = self.get_request(key)
            return call_cache[key]

        edges: list[dict[str, Any]] = []
        roles: dict[str, set[str]] = {str(memory_id): {"anchor"}}
        for row in rows:
            row["metadata"] = _metadata_object(row.pop("metadata_json"))
            parent_id = str(row.get("parent_memory_id") or "")
            child_id = str(row.get("memory_id") or "")
            parent = scoped_memory(parent_id)
            child = scoped_memory(child_id)
            roles.setdefault(parent_id, set()).add("parent")
            roles.setdefault(child_id, set()).add("child")
            parent_chunks = parent.get("chunks", []) if isinstance(parent, Mapping) else []
            child_chunks = child.get("chunks", []) if isinstance(child, Mapping) else []
            source_anchors = [
                _chunk_anchor_projection(chunk)
                for chunk in parent_chunks[:8]
                if isinstance(chunk, Mapping)
            ]
            target_anchors = [
                _chunk_anchor_projection(chunk)
                for chunk in child_chunks[:8]
                if isinstance(chunk, Mapping)
            ]
            call = scoped_call(row.get("call_id"))
            call_evidence = None
            if isinstance(call, Mapping):
                call_evidence = {
                    key: call.get(key)
                    for key in (
                        "call_id",
                        "tool_name",
                        "status",
                        "degraded",
                        "started_at",
                        "ended_at",
                        "duration_ms",
                        "duration_status",
                        "request_scope_id",
                    )
                }
            edge = {
                **row,
                "id": f"lineage:{row.get('lineage_id')}",
                "type": "memory_lineage",
                "source": parent_id,
                "target": child_id,
                "directed": True,
                "direction": "parent_to_child",
                "timestamp": row.get("created_at"),
                "call": call_evidence,
                "evidence": {
                    "scope": row.get("evidence_scope"),
                    "call_id": row.get("call_id"),
                    "request_scope_id": row.get("request_scope_id"),
                    "recorded_at": row.get("created_at"),
                    "source_visibility": row.get("parent_visibility"),
                    "target_visibility": row.get("child_visibility"),
                    "metadata": row.get("metadata", {}),
                },
                "chunk_anchors": {
                    "status": (
                        "manifest_available_not_lineage_specific"
                        if parent_chunks or child_chunks
                        else "not_recorded"
                    ),
                    "source": source_anchors,
                    "target": target_anchors,
                    "source_summary": _chunk_anchor_summary(
                        parent.get("chunking") if isinstance(parent, Mapping) else None,
                        source_anchors,
                        limit=8,
                    ),
                    "target_summary": _chunk_anchor_summary(
                        child.get("chunking") if isinstance(child, Mapping) else None,
                        target_anchors,
                        limit=8,
                    ),
                },
            }
            edges.append(edge)

        nodes: list[dict[str, Any]] = []
        ordered_ids = [str(memory_id)] + sorted(
            key for key in roles if key and key != str(memory_id)
        )
        for node_id in ordered_ids:
            memory = scoped_memory(node_id)
            if not isinstance(memory, Mapping):
                continue
            chunks = memory.get("chunks", [])
            chunk_anchors = [
                _chunk_anchor_projection(chunk)
                for chunk in chunks[:16]
                if isinstance(chunk, Mapping)
            ]
            nodes.append(
                {
                    "id": node_id,
                    "type": "memory",
                    "roles": sorted(roles.get(node_id, {"related"})),
                    "memory_type": memory.get("memory_type"),
                    "source_class": memory.get("source_class"),
                    "content_preview": str(memory.get("content") or "")[:300],
                    "project_id": memory.get("project_id"),
                    "visibility": memory.get("visibility"),
                    "created_at": memory.get("created_at"),
                    "created_by_call_id": memory.get("created_by_call_id"),
                    "chunking": memory.get("chunking"),
                    "chunk_anchors": chunk_anchors,
                    "chunk_anchor_summary": _chunk_anchor_summary(
                        memory.get("chunking"),
                        chunk_anchors,
                        limit=16,
                    ),
                }
            )
        relation_counts: dict[str, int] = {}
        for row in edges:
            relation = str(row.get("relation") or "unknown")
            relation_counts[relation] = relation_counts.get(relation, 0) + 1
        return {
            "memory_id": str(memory_id),
            "memory": anchor,
            "nodes": nodes,
            "edges": edges,
            "data": edges,
            "summary": {
                "returned": len(edges),
                "has_more": has_more,
                "relations": relation_counts,
                "legacy_global_edges": sum(
                    row.get("evidence_scope") == "legacy_global" for row in edges
                ),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "chunk_anchor_count": sum(
                    int(node.get("chunk_anchor_summary", {}).get("total", 0))
                    for node in nodes
                ),
                "chunk_anchor_returned": sum(
                    int(node.get("chunk_anchor_summary", {}).get("returned", 0))
                    for node in nodes
                ),
                "chunk_anchors_truncated": any(
                    node.get("chunk_anchor_summary", {}).get("truncated") is True
                    for node in nodes
                ),
            },
        }

    def enrich_retrieval_explain(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Attach canonical chunk anchors and per-channel scores to a snapshot.

        Retrieval snapshots deliberately do not persist memory bodies.  When a
        candidate points at a memory that has a validated structure-v1 manifest,
        this projection supplies structural evidence only.  It never claims a
        specific chunk was hit unless the snapshot itself recorded ``chunk_id``.
        """
        projected = sanitize_retrieval_explain_snapshot(snapshot)
        if projected is None:
            return {}
        chunk_cache: dict[
            str,
            tuple[dict[str, Any], str, dict[str, Any], list[dict[str, Any]]] | None,
        ] = {}

        def chunks_for(
            value: object,
        ) -> tuple[dict[str, Any], str, dict[str, Any], list[dict[str, Any]]] | None:
            key = str(value or "").strip()
            if not key:
                return None
            if key not in chunk_cache:
                row = self._conn.execute(
                    """
                    SELECT metadata_json, embedding_text
                    FROM memories
                    WHERE id = ? AND (project_id = ? OR visibility = 'global')
                    """,
                    (key, self.scope.project_id),
                ).fetchone()
                if row is None:
                    chunk_cache[key] = None
                else:
                    metadata = _json_object(row[0])
                    source_text = str(row[1] or "")
                    chunking, anchors = _chunk_manifest_projection(
                        metadata,
                        parent_memory_id=key,
                        include_text=False,
                        max_chunks=16,
                        source_text=source_text,
                    )
                    chunk_cache[key] = (metadata, source_text, chunking, anchors)
            return chunk_cache[key]

        def annotate(item: object, *, channel: str | None = None) -> dict[str, Any]:
            if not isinstance(item, Mapping):
                return {}
            result = dict(item)
            memory_id = str(
                result.get("parent_memory_id")
                or result.get("memory_id")
                or result.get("id")
                or ""
            ).strip()
            chunk_data = chunks_for(memory_id)
            metadata, source_text, chunking, anchors = chunk_data or ({}, "", {}, [])
            explicit_chunk_id = str(result.get("chunk_id") or "").strip()
            matched = (
                _matching_chunk_projection(
                    metadata,
                    parent_memory_id=memory_id,
                    chunk_id=explicit_chunk_id,
                    source_text=source_text,
                )
                if explicit_chunk_id
                else None
            )
            if matched is not None:
                result["chunk_evidence"] = {
                    "status": "matched",
                    **matched,
                }
            elif explicit_chunk_id and chunking.get("status") == "available":
                result["chunk_evidence"] = {
                    "status": "recorded_chunk_not_found",
                    "parent_memory_id": memory_id,
                    "chunk_id": explicit_chunk_id,
                    "available_count": int(chunking.get("chunk_count") or 0),
                    "returned_count": len(anchors),
                    "truncated": bool(chunking.get("chunk_count", 0) > len(anchors)),
                    "anchors": anchors,
                }
            elif anchors:
                result["chunk_evidence"] = {
                    "status": "available_not_recorded",
                    "parent_memory_id": memory_id,
                    "available_count": int(chunking.get("chunk_count") or 0),
                    "returned_count": len(anchors),
                    "truncated": bool(chunking.get("chunk_count", 0) > len(anchors)),
                    "anchors": anchors,
                }
            if channel:
                result["channel"] = channel
            return result

        channel_scores: dict[str, dict[str, float | int]] = {}
        channels = projected.get("channels")
        if isinstance(channels, list):
            for channel_index, channel_row in enumerate(channels):
                if not isinstance(channel_row, Mapping):
                    continue
                channel = str(channel_row.get("name") or "")
                items = channel_row.get("items")
                if not channel or not isinstance(items, list):
                    continue
                annotated_items = []
                for item in items:
                    annotated = annotate(item, channel=channel)
                    annotated_items.append(annotated)
                    key = str(
                        annotated.get("parent_memory_id")
                        or annotated.get("memory_id")
                        or annotated.get("id")
                        or ""
                    )
                    score = annotated.get("score")
                    if key and isinstance(score, (int, float)) and not isinstance(score, bool):
                        channel_scores.setdefault(key, {})[channel] = score
                channel_row = dict(channel_row)
                channel_row["items"] = annotated_items
                # Replace the matching row while retaining ordering.
                channels[channel_index] = channel_row

        raw_items = projected.get("items")
        if isinstance(raw_items, list):
            annotated_candidates = []
            for item in raw_items:
                annotated = annotate(item)
                key = str(
                    annotated.get("parent_memory_id")
                    or annotated.get("memory_id")
                    or annotated.get("id")
                    or ""
                )
                if key and key in channel_scores:
                    annotated["channel_scores"] = dict(channel_scores[key])
                annotated_candidates.append(annotated)
            projected["items"] = annotated_candidates
        return projected

    def list_synthesis(
        self,
        *,
        limit: int = 25,
        cursor: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        bounded = _limit(limit)
        filters = {"status": str(status or "")}
        keyset = self._decode_cursor(cursor, "synthesis", filters)
        clauses = ["(m.project_id = ? OR m.visibility = 'global')"]
        params: list[Any] = [self.scope.project_id]
        if status:
            clauses.append("sa.status = ?")
            params.append(str(status))
        from_sql = "FROM synthesis_artifacts AS sa JOIN memories AS m ON m.id = sa.memory_id"
        count = self._conn.execute(
            f"SELECT COUNT(*) {from_sql} WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]
        status_rows = self._conn.execute(
            f"""
            SELECT sa.status, COUNT(*)
            {from_sql}
            WHERE (m.project_id = ? OR m.visibility = 'global')
            GROUP BY sa.status
            """,
            (self.scope.project_id,),
        ).fetchall()
        if keyset:
            clauses.append("(sa.updated_at < ? OR (sa.updated_at = ? AND sa.memory_id < ?))")
            params.extend((keyset[0], keyset[0], keyset[1]))
        rows = _rows(
            self._conn.execute(
                f"""
                SELECT sa.memory_id, sa.synthesis_key, sa.status, sa.revision,
                       sa.support_count, sa.validity_scope, sa.source_fingerprint,
                       sa.last_verified_at, sa.last_linted_at, sa.stale_reason,
                       sa.created_by_call_id, sa.verified_by_actor,
                       sa.verified_by_call_id, sa.metadata_json, sa.created_at,
                       sa.updated_at, m.content, m.project_id, m.visibility
                {from_sql}
                WHERE {' AND '.join(clauses)}
                ORDER BY sa.updated_at DESC, sa.memory_id DESC
                LIMIT ?
                """,
                (*params, bounded + 1),
            )
        )
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        for row in rows:
            row["content_preview"] = str(row.pop("content") or "")[:300]
            row["metadata"] = _metadata_object(row.pop("metadata_json"))
        next_cursor = (
            self._encode_cursor(
                "synthesis", filters, rows[-1]["updated_at"], rows[-1]["memory_id"]
            )
            if has_more and rows
            else None
        )
        envelope = self._envelope(rows, total=int(count), limit=bounded, next_cursor=next_cursor)
        envelope["summary"] = {
            "artifact_count": sum(int(row[1]) for row in status_rows),
            "status_counts": {str(row[0] or "unknown"): int(row[1]) for row in status_rows},
        }
        return envelope

    @staticmethod
    def _operations_union() -> str:
        return """
            SELECT 'runtime_event' AS kind, event_id AS record_id, created_at,
                   status, event_name AS name, actor, request_scope_id, project_id,
                   metadata_json, audit_trace_json AS details_json, '' AS call_id,
                   '' AS error_class, '' AS error_message, 0 AS attempt_count,
                   '' AS next_attempt_at
            FROM runtime_events
            WHERE project_id = ?
            UNION ALL
            SELECT 'degradation' AS kind, CAST(event_id AS TEXT) AS record_id, created_at,
                   level AS status, tool_name || ':' || link_name AS name, '' AS actor,
                   request_scope_id, project_id, metadata_json, '{}' AS details_json,
                   call_id, error_class, error_message, 0 AS attempt_count,
                   '' AS next_attempt_at
            FROM degradation_events
            WHERE project_id = ?
            UNION ALL
            SELECT 'outbox' AS kind, outbox_id AS record_id, created_at,
                   status, tool_name AS name, '' AS actor, '' AS request_scope_id,
                   project_id, metadata_json, '{}' AS details_json, call_id,
                   error_class, error_message, attempt_count, next_attempt_at
            FROM store_outbox
            WHERE project_id = ?
        """

    def list_operations(
        self,
        *,
        limit: int = 25,
        cursor: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if kind is not None and kind not in {"runtime_event", "degradation", "outbox"}:
            raise ValueError("operation_kind_invalid")
        bounded = _limit(limit)
        filters = {"kind": str(kind or ""), "status": str(status or "")}
        keyset = self._decode_cursor(cursor, "operations", filters)
        outer_clauses: list[str] = []
        outer_params: list[Any] = []
        if kind:
            outer_clauses.append("kind = ?")
            outer_params.append(kind)
        if status:
            outer_clauses.append("status = ?")
            outer_params.append(str(status))
        base_params = [self.scope.project_id] * 3
        where = f"WHERE {' AND '.join(outer_clauses)}" if outer_clauses else ""
        count = self._conn.execute(
            f"SELECT COUNT(*) FROM ({self._operations_union()}) AS operations {where}",
            (*base_params, *outer_params),
        ).fetchone()[0]
        data_clauses = list(outer_clauses)
        data_params = [*base_params, *outer_params]
        if keyset:
            data_clauses.append("(created_at < ? OR (created_at = ? AND record_id < ?))")
            data_params.extend((keyset[0], keyset[0], keyset[1]))
        data_where = f"WHERE {' AND '.join(data_clauses)}" if data_clauses else ""
        rows = _rows(
            self._conn.execute(
                f"""
                SELECT * FROM ({self._operations_union()}) AS operations
                {data_where}
                ORDER BY created_at DESC, record_id DESC
                LIMIT ?
                """,
                (*data_params, bounded + 1),
            )
        )
        has_more = len(rows) > bounded
        rows = rows[:bounded]
        for row in rows:
            row["metadata"] = _metadata_object(row.pop("metadata_json"))
            row["details"] = redact_value(_json_object(row.pop("details_json")))
            row["error_message"] = redact_value(str(row["error_message"] or ""))
            if row["kind"] != "outbox":
                row.pop("attempt_count", None)
                row.pop("next_attempt_at", None)
        next_cursor = (
            self._encode_cursor(
                "operations", filters, rows[-1]["created_at"], rows[-1]["record_id"]
            )
            if has_more and rows
            else None
        )
        return self._envelope(rows, total=int(count), limit=bounded, next_cursor=next_cursor)

    def get_trust(self, target: str = "") -> dict[str, Any] | None:
        """Read trust without invoking lazy decay or creating a missing target."""
        try:
            cursor = self._conn.execute(
                """
                SELECT target, trust, tier, autonomy_level, last_updated
                FROM trust_scores
                WHERE target = ?
                """,
                (str(target),),
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return None
            raise
        return _row(cursor, cursor.fetchone())


__all__ = ["DashboardCursorError", "DashboardRepository", "redact_value"]
