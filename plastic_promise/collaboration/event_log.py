"""Append-only SQLite adapter foundation for project-scoped collaboration events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from .canonical_time import canonical_text, server_expiry_text, server_now_text
from .contracts import (
    AgentIdentity,
    CollaborationContractError,
    CollaborationEvent,
    EventCursor,
    EventPage,
    ProjectScope,
)

_MAX_PAGE_SIZE = 200
_MAX_ISSUED_READ_RECEIPTS = 1024
COLLABORATION_EVENT_LOG_REVISION = "collaboration-event-log/sqlite-v1"
COLLABORATION_EVENT_READ_RECEIPT_SCHEMA = "collaboration-event-read-receipt/v1"
_EVENT_READ_RECEIPT_TOKEN = object()
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SOURCE_TIMESTAMP_PAYLOAD_KEY = "_server_time_diagnostics"
_EVENT_SELECT_COLUMNS = """
    sequence,
    event_id,
    project_id,
    coordination_session_id,
    actor_agent_id,
    actor_role,
    event_type,
    causal_parent_event_id,
    audience_roles_json,
    audience_agent_ids_json,
    event_json,
    event_sha256,
    created_at,
    expires_at
"""


@dataclass(frozen=True, slots=True, init=False)
class CollaborationEventReadReceipt:
    """Instance-bound evidence for one audience-filtered event-log read."""

    receipt_id: str
    source_anchor_sha256: str
    event_log_revision: str
    project: ProjectScope
    coordination_session_id: str
    audience: AgentIdentity
    after_cursor: EventCursor
    next_cursor: EventCursor
    source_head_cursor: EventCursor
    event_page_sha256: str
    visible_event_count: int
    generated_at_utc: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CollaborationContractError("collaboration_event_read_receipt_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        receipt_id: str,
        source_anchor_sha256: str,
        project: ProjectScope,
        coordination_session_id: str,
        audience: AgentIdentity,
        page: EventPage,
        source_head_cursor: EventCursor,
        generated_at_utc: str,
        _token: object,
    ) -> CollaborationEventReadReceipt:
        if _token is not _EVENT_READ_RECEIPT_TOKEN:
            raise CollaborationContractError("collaboration_event_read_receipt_server_required")
        if page.after_cursor is None:
            raise CollaborationContractError("collaboration_event_read_page_unbound")
        instance = object.__new__(cls)
        for field_name, value in {
            "receipt_id": receipt_id,
            "source_anchor_sha256": source_anchor_sha256,
            "event_log_revision": COLLABORATION_EVENT_LOG_REVISION,
            "project": project,
            "coordination_session_id": coordination_session_id,
            "audience": audience,
            "after_cursor": page.after_cursor,
            "next_cursor": page.next_cursor,
            "source_head_cursor": source_head_cursor,
            "event_page_sha256": page.content_sha256,
            "visible_event_count": len(page.events),
            "generated_at_utc": generated_at_utc,
        }.items():
            object.__setattr__(instance, field_name, value)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if not self.receipt_id.startswith("event-read:"):
            raise CollaborationContractError("collaboration_event_read_receipt_id_invalid")
        if not _SHA256_RE.fullmatch(self.source_anchor_sha256) or not _SHA256_RE.fullmatch(
            self.event_page_sha256
        ):
            raise CollaborationContractError("collaboration_event_read_receipt_digest_invalid")
        if self.event_log_revision != COLLABORATION_EVENT_LOG_REVISION:
            raise CollaborationContractError("collaboration_event_log_revision_invalid")
        if not isinstance(self.project, ProjectScope) or not isinstance(
            self.audience,
            AgentIdentity,
        ):
            raise CollaborationContractError("collaboration_event_read_receipt_scope_invalid")
        for cursor in (self.after_cursor, self.next_cursor, self.source_head_cursor):
            if (
                not isinstance(cursor, EventCursor)
                or cursor.project != self.project
                or cursor.coordination_session_id != self.coordination_session_id
            ):
                raise CollaborationContractError("collaboration_event_read_receipt_cursor_invalid")
        span = self.next_cursor.sequence - self.after_cursor.sequence
        if (
            self.next_cursor.sequence > self.source_head_cursor.sequence
            or self.visible_event_count < 0
            or self.visible_event_count > span
        ):
            raise CollaborationContractError("collaboration_event_read_receipt_span_invalid")
        try:
            canonical = canonical_text(self.generated_at_utc)
        except (TypeError, ValueError) as exc:
            raise CollaborationContractError(
                "collaboration_event_read_receipt_time_invalid"
            ) from exc
        if self.generated_at_utc != canonical:
            raise CollaborationContractError(
                "collaboration_event_read_receipt_time_not_canonical"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COLLABORATION_EVENT_READ_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "source_anchor_sha256": self.source_anchor_sha256,
            "event_log_revision": self.event_log_revision,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "audience": {
                "agent_id": self.audience.agent_id,
                "role": self.audience.role,
            },
            "after_cursor": self.after_cursor.to_dict(),
            "next_cursor": self.next_cursor.to_dict(),
            "source_head_cursor": self.source_head_cursor.to_dict(),
            "event_page_sha256": self.event_page_sha256,
            "visible_event_count": self.visible_event_count,
            "generated_at_utc": self.generated_at_utc,
            "authority": "process-local-server-event-log",
            "persistent_head_authority": "deferred-to-pr5",
        }

    @property
    def content_sha256(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _restamp_event(
    event: CollaborationEvent,
    observed_at: str,
    *,
    retention_seconds: int | None,
) -> CollaborationEvent:
    """Return a server-stamped event while preserving source clock evidence."""

    diagnostics = {
        # The collaboration bridge may compile a promotion candidate before
        # the server stamps the event.  Keep that immutable source digest in
        # the diagnostic envelope so the durable PR5 verifier can bind the
        # candidate to the exact pre-restamp event without treating an edge
        # timestamp as canonical ordering or expiry authority.
        "source_event_sha256": event.content_sha256,
        "created_at": event.created_at,
        "expires_at": event.expires_at,
    }
    payload = dict(event.payload)
    payload[_SOURCE_TIMESTAMP_PAYLOAD_KEY] = diagnostics
    try:
        expires_at = server_expiry_text(
            observed_at=observed_at,
            retention_seconds=retention_seconds,
        )
    except (TypeError, ValueError) as exc:
        raise CollaborationContractError("collaboration_event_expiry_invalid") from exc
    return replace(
        event,
        created_at=observed_at,
        expires_at=expires_at,
        payload=payload,
    )


def _event_request_fingerprint(event: CollaborationEvent) -> str:
    """Hash caller intent without server-owned clock fields or diagnostics."""

    projection = event.to_dict()
    projection["created_at"] = None
    projection["expires_at"] = None
    payload = projection.get("payload")
    if isinstance(payload, dict):
        payload.pop(_SOURCE_TIMESTAMP_PAYLOAD_KEY, None)
    encoded = json.dumps(
        projection,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CollaborationEventLog:
    """A small interface over an append-only SQLite collaboration projection.

    The adapter accepts an existing connection or an explicit database path and
    never discovers storage from environment variables.  Production wiring is
    responsible for supplying the server's canonical single-writer SQLite
    connection/path.  An injected connection remains owned by its caller; an
    explicit path creates an adapter-owned connection.  Audience filtering is a
    projection rule, not authentication: a future transport adapter must bind
    the supplied Agent identity to server-owned authority.
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        db_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        ensure_schema: bool = True,
    ) -> None:
        if (connection is None) == (db_path is None):
            raise CollaborationContractError("collaboration_database_source_invalid")
        if connection is not None and not isinstance(connection, sqlite3.Connection):
            raise CollaborationContractError("collaboration_connection_invalid")
        if db_path is not None:
            path_text = str(db_path)
            if not path_text.strip():
                raise CollaborationContractError("collaboration_db_path_invalid")
            self._connection = sqlite3.connect(path_text)
            self._owns_connection = True
        else:
            self._connection = connection
            self._owns_connection = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._source_anchor_sha256 = "sha256:" + hashlib.sha256(
            f"{COLLABORATION_EVENT_LOG_REVISION}:{uuid.uuid4().hex}".encode()
        ).hexdigest()
        self._issued_read_receipts: dict[str, str] = {}
        try:
            if ensure_schema:
                self._ensure_schema()
            else:
                self._verify_schema()
        except Exception:
            if self._owns_connection:
                self._connection.rollback()
                self._connection.close()
            raise

    def _verify_schema(self) -> None:
        """Verify an already-installed append-only schema without mutating it."""

        required = {
            ("table", "collaboration_events"),
            ("index", "idx_collaboration_events_scope_cursor"),
            ("index", "idx_collaboration_events_parent"),
            ("trigger", "collaboration_events_no_update"),
            ("trigger", "collaboration_events_no_delete"),
            ("trigger", "collaboration_events_no_replace"),
        }
        names = tuple(name for _kind, name in required)
        placeholders = ",".join("?" for _ in names)
        existing = {
            (str(row[0]), str(row[1]))
            for row in self._connection.execute(
                f"SELECT type, name FROM sqlite_master WHERE name IN ({placeholders})",
                names,
            ).fetchall()
        }
        if not required.issubset(existing):
            raise CollaborationContractError("collaboration_event_schema_missing")

    def append(
        self,
        event: CollaborationEvent,
        *,
        retention_seconds: int | None = None,
    ) -> EventCursor:
        """Append one event using one server-issued observation timestamp.

        The event's source timestamps are retained under a diagnostic payload
        key, but the stored ``created_at``/``expires_at`` are re-stamped from
        the server clock.  ``retention_seconds`` is a server policy input;
        the caller's absolute ``expires_at`` is never copied or translated.
        This prevents a client or compute node with a skewed clock from
        changing event ordering, expiry visibility, or cursor progression.
        """

        if not isinstance(event, CollaborationEvent):
            raise CollaborationContractError("collaboration_event_invalid")
        existing = self._event_row(event.event_id)
        if existing is not None:
            sequence, stored_event = self._decode_event_row(existing)
            if _event_request_fingerprint(stored_event) != _event_request_fingerprint(event):
                raise CollaborationContractError("collaboration_event_id_conflict")
            return EventCursor(
                stored_event.project,
                stored_event.coordination_session_id,
                sequence,
            )

        observed_at = self._now_text()
        canonical_event = _restamp_event(
            event,
            observed_at,
            retention_seconds=retention_seconds,
        )

        if canonical_event.causal_parent_event_id is not None:
            parent = self._event_row(canonical_event.causal_parent_event_id)
            if parent is None:
                raise CollaborationContractError("collaboration_parent_not_found")
            _, parent_event = self._decode_event_row(parent)
            if (
                parent_event.project != canonical_event.project
                or parent_event.coordination_session_id
                != canonical_event.coordination_session_id
            ):
                raise CollaborationContractError("collaboration_parent_scope_mismatch")

        event_json = canonical_event.canonical_json()
        try:
            cursor = self._connection.execute(
                """
                INSERT INTO collaboration_events (
                    event_id,
                    project_id,
                    coordination_session_id,
                    actor_agent_id,
                    actor_role,
                    event_type,
                    causal_parent_event_id,
                    audience_roles_json,
                    audience_agent_ids_json,
                    event_json,
                    event_sha256,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_event.event_id,
                    canonical_event.project.project_id,
                    canonical_event.coordination_session_id,
                    canonical_event.actor.agent_id,
                    canonical_event.actor.role,
                    canonical_event.event_type,
                    canonical_event.causal_parent_event_id,
                    json.dumps(list(canonical_event.audience_roles), separators=(",", ":")),
                    json.dumps(list(canonical_event.audience_agent_ids), separators=(",", ":")),
                    event_json,
                    canonical_event.content_sha256,
                    canonical_event.created_at,
                    canonical_event.expires_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if self._owns_connection:
                self._connection.rollback()
            replay = self._event_row(event.event_id)
            if replay is not None:
                sequence, replay_event = self._decode_event_row(replay)
                if _event_request_fingerprint(replay_event) == _event_request_fingerprint(event):
                    return EventCursor(
                        replay_event.project,
                        replay_event.coordination_session_id,
                        sequence,
                    )
            raise CollaborationContractError("collaboration_event_append_conflict") from exc
        try:
            if self._owns_connection:
                self._connection.commit()
        except Exception:
            if self._owns_connection:
                self._connection.rollback()
            raise
        return EventCursor(
            project=canonical_event.project,
            coordination_session_id=canonical_event.coordination_session_id,
            sequence=int(cursor.lastrowid),
        )

    def read(
        self,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        audience: AgentIdentity,
        after: EventCursor | None = None,
        limit: int = 100,
        include_expired: bool = False,
    ) -> EventPage:
        """Read a project/session page visible to one declared Agent audience."""

        if not isinstance(project, ProjectScope):
            raise CollaborationContractError("collaboration_read_project_invalid")
        if not isinstance(audience, AgentIdentity):
            raise CollaborationContractError("collaboration_read_audience_invalid")
        start = after or EventCursor.start(project, coordination_session_id)
        if start.project != project or start.coordination_session_id != coordination_session_id:
            raise CollaborationContractError("collaboration_cursor_scope_mismatch")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise CollaborationContractError("collaboration_page_limit_invalid")
        if not isinstance(include_expired, bool):
            raise CollaborationContractError("collaboration_include_expired_invalid")

        expiry_clause = ""
        parameters: list[object] = [
            project.project_id,
            coordination_session_id,
            start.sequence,
            audience.agent_id,
            audience.role,
            audience.agent_id,
        ]
        if not include_expired:
            expiry_clause = "AND (expires_at IS NULL OR expires_at > ?)"
            parameters.append(self._now_text())
        parameters.append(limit + 1)
        try:
            rows = self._connection.execute(
                f"""
                SELECT {_EVENT_SELECT_COLUMNS}
                  FROM collaboration_events
                 WHERE project_id = ?
                   AND coordination_session_id = ?
                   AND sequence > ?
                   AND (
                        (audience_roles_json = '[]' AND audience_agent_ids_json = '[]')
                        OR actor_agent_id = ?
                        OR EXISTS (
                            SELECT 1 FROM json_each(collaboration_events.audience_roles_json)
                             WHERE value = ?
                        )
                        OR EXISTS (
                            SELECT 1 FROM json_each(collaboration_events.audience_agent_ids_json)
                             WHERE value = ?
                        )
                   )
                   {expiry_clause}
                 ORDER BY sequence ASC
                 LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "malformed json" not in str(exc).casefold():
                raise
            raise CollaborationContractError("collaboration_event_projection_mismatch") from exc
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        events: list[CollaborationEvent] = []
        sequences: list[int] = []
        for row in visible_rows:
            sequence, event = self._decode_event_row(row)
            if event.project != project or event.coordination_session_id != coordination_session_id:
                raise CollaborationContractError("collaboration_event_scope_corrupt")
            if not self._visible_to(event, audience):
                raise CollaborationContractError("collaboration_event_audience_corrupt")
            events.append(event)
            sequences.append(sequence)
        next_sequence = sequences[-1] if sequences else start.sequence
        next_cursor = EventCursor(project, coordination_session_id, next_sequence)
        return EventPage(
            project=project,
            coordination_session_id=coordination_session_id,
            events=tuple(events),
            after_cursor=start,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def read_with_receipt(
        self,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        audience: AgentIdentity,
        after: EventCursor | None = None,
        limit: int = 100,
        include_expired: bool = False,
    ) -> tuple[EventPage, CollaborationEventReadReceipt]:
        """Read one page and issue instance-bound source-lineage evidence."""

        page = self.read(
            project=project,
            coordination_session_id=coordination_session_id,
            audience=audience,
            after=after,
            limit=limit,
            include_expired=include_expired,
        )
        source_head = self._source_head(project, coordination_session_id)
        receipt = CollaborationEventReadReceipt._issue(
            receipt_id=f"event-read:{uuid.uuid4().hex}",
            source_anchor_sha256=self._source_anchor_sha256,
            project=project,
            coordination_session_id=coordination_session_id,
            audience=audience,
            page=page,
            source_head_cursor=source_head,
            generated_at_utc=self._now_text(),
            _token=_EVENT_READ_RECEIPT_TOKEN,
        )
        self._issued_read_receipts[receipt.receipt_id] = receipt.content_sha256
        while len(self._issued_read_receipts) > _MAX_ISSUED_READ_RECEIPTS:
            self._issued_read_receipts.pop(next(iter(self._issued_read_receipts)))
        return page, receipt

    def verify_read_receipt(self, receipt: CollaborationEventReadReceipt) -> bool:
        """Verify one read receipt against this exact event-log instance."""

        if not isinstance(receipt, CollaborationEventReadReceipt):
            return False
        if (
            receipt.source_anchor_sha256 != self._source_anchor_sha256
            or receipt.event_log_revision != COLLABORATION_EVENT_LOG_REVISION
            or self._issued_read_receipts.get(receipt.receipt_id) != receipt.content_sha256
        ):
            return False
        current_head = self._source_head(receipt.project, receipt.coordination_session_id)
        return current_head.sequence >= receipt.source_head_cursor.sequence

    def close(self) -> None:
        """Close only a connection created from the explicit ``db_path``."""

        if self._owns_connection:
            self._connection.close()

    def _now_text(self) -> str:
        try:
            return server_now_text(self._clock)
        except (TypeError, ValueError) as exc:
            raise CollaborationContractError("collaboration_clock_invalid") from exc

    def _event_row(self, event_id: str):
        return self._connection.execute(
            f"SELECT {_EVENT_SELECT_COLUMNS} FROM collaboration_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()

    def _source_head(
        self,
        project: ProjectScope,
        coordination_session_id: str,
    ) -> EventCursor:
        row = self._connection.execute(
            """
            SELECT MAX(sequence)
              FROM collaboration_events
             WHERE project_id = ? AND coordination_session_id = ?
            """,
            (project.project_id, coordination_session_id),
        ).fetchone()
        sequence = 0 if row is None or row[0] is None else row[0]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CollaborationContractError("collaboration_event_head_corrupt")
        return EventCursor(project, coordination_session_id, sequence)

    @staticmethod
    def _decode_event_row(row) -> tuple[int, CollaborationEvent]:
        values = tuple(row)
        if len(values) != 14:
            raise CollaborationContractError("collaboration_event_storage_corrupt")
        (
            sequence,
            stored_event_id,
            stored_project_id,
            stored_session_id,
            stored_actor_id,
            stored_actor_role,
            stored_event_type,
            stored_parent_id,
            stored_audience_roles,
            stored_audience_agent_ids,
            event_json,
            stored_hash,
            stored_created_at,
            stored_expires_at,
        ) = values
        try:
            projection = json.loads(str(event_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollaborationContractError("collaboration_event_json_corrupt") from exc
        if not isinstance(projection, dict):
            raise CollaborationContractError("collaboration_event_json_corrupt")
        try:
            event = CollaborationEvent.from_dict(projection)
        except CollaborationContractError as exc:
            raise CollaborationContractError("collaboration_event_json_corrupt") from exc
        if event.content_sha256 != str(stored_hash):
            raise CollaborationContractError("collaboration_event_hash_mismatch")
        audience_roles = CollaborationEventLog._decode_string_list(stored_audience_roles)
        audience_agent_ids = CollaborationEventLog._decode_string_list(stored_audience_agent_ids)
        if (
            str(stored_event_id) != event.event_id
            or str(stored_project_id) != event.project.project_id
            or str(stored_session_id) != event.coordination_session_id
            or str(stored_actor_id) != event.actor.agent_id
            or str(stored_actor_role) != event.actor.role
            or str(stored_event_type) != event.event_type
            or stored_parent_id != event.causal_parent_event_id
            or audience_roles != list(event.audience_roles)
            or audience_agent_ids != list(event.audience_agent_ids)
            or str(stored_created_at) != event.created_at
            or stored_expires_at != event.expires_at
        ):
            raise CollaborationContractError("collaboration_event_projection_mismatch")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise CollaborationContractError("collaboration_event_sequence_corrupt")
        return sequence, event

    @staticmethod
    def _decode_string_list(value: object) -> list[str]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollaborationContractError("collaboration_event_projection_mismatch") from exc
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise CollaborationContractError("collaboration_event_projection_mismatch")
        return decoded

    @staticmethod
    def _visible_to(event: CollaborationEvent, audience: AgentIdentity) -> bool:
        if not event.audience_roles and not event.audience_agent_ids:
            return True
        return (
            event.actor.agent_id == audience.agent_id
            or audience.role in event.audience_roles
            or audience.agent_id in event.audience_agent_ids
        )
    def _ensure_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collaboration_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                actor_agent_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                causal_parent_event_id TEXT,
                audience_roles_json TEXT NOT NULL DEFAULT '[]',
                audience_agent_ids_json TEXT NOT NULL DEFAULT '[]',
                event_json TEXT NOT NULL,
                event_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_events_scope_cursor
                ON collaboration_events(project_id, coordination_session_id, sequence)
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_collaboration_events_parent
                ON collaboration_events(causal_parent_event_id)
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_update
            BEFORE UPDATE ON collaboration_events
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_delete
            BEFORE DELETE ON collaboration_events
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS collaboration_events_no_replace
            BEFORE INSERT ON collaboration_events
            WHEN EXISTS (
                SELECT 1
                  FROM collaboration_events
                 WHERE event_id = NEW.event_id
                    OR (NEW.sequence > 0 AND sequence = NEW.sequence)
            )
            BEGIN
                SELECT RAISE(ABORT, 'collaboration_event_append_only');
            END
            """
        )
        if self._owns_connection:
            self._connection.commit()
