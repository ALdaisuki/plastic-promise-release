"""Durable state for pinned official engineering workflows."""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

_PROJECT_SCOPE_MARKER = "::project:"
_FLOW_SCOPE_MARKER = "::flow:"


def _encode_scope_component(value: str) -> str:
    """Escape reserved separators without changing ordinary scope ids."""
    return value.replace("%", "%25").replace("::", "%3A%3A")


def _decode_scope_component(value: str) -> str:
    return value.replace("%3A%3A", "::").replace("%25", "%")


@dataclass(frozen=True)
class WorkflowState:
    scope_id: str
    stage_session_id: str
    flow_line_id: str
    route_id: str
    current_stage: str | None
    current_step_index: int
    parent_entity_id: str | None
    current_entity_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowInstance:
    """One immutable official workflow run within a client conversation."""

    instance_id: str
    project_id: str
    workflow_session_id: str
    client_flow_line_id: str
    flow_line_id: str
    route_id: str
    generation: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_flow_scope(scope_id: str) -> tuple[str, str]:
    """Split the public session id and flow line from a canonical scope id."""
    public_scope = str(scope_id or "").partition(_PROJECT_SCOPE_MARKER)[0]
    stage_session_id, separator, flow_line_id = public_scope.partition(_FLOW_SCOPE_MARKER)
    if not separator:
        return _decode_scope_component(stage_session_id) or "default", ""
    return (
        _decode_scope_component(stage_session_id) or "default",
        _decode_scope_component(flow_line_id),
    )


def compose_flow_scope(
    stage_session_id: str | None,
    flow_line_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Build the canonical persisted scope for one workflow lane."""
    raw_session = str(stage_session_id or "").strip() or "default"
    raw_flow = str(flow_line_id or "").strip()
    project = str(project_id or "").strip()

    # Every public component is escaped, including a session-only scope. A
    # caller-supplied value that merely looks canonical must remain data rather
    # than gaining authority over the persisted flow/project separators.
    session = _encode_scope_component(raw_session)
    flow = _encode_scope_component(raw_flow)
    scope = f"{session}{_FLOW_SCOPE_MARKER}{flow}" if flow else session
    if not project:
        return scope
    project_digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    return f"{scope}{_PROJECT_SCOPE_MARKER}{project_digest}"


def engine_connection(engine: Any):
    """Return the canonical SQLite connection when the engine owns one."""
    sqlite = getattr(engine, "_sqlite", None)
    connection = getattr(sqlite, "_conn", None)
    return connection if isinstance(connection, sqlite3.Connection) else None


def ensure_workflow_state_schema(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS official_workflow_state (
            scope_id TEXT PRIMARY KEY,
            stage_session_id TEXT NOT NULL,
            flow_line_id TEXT NOT NULL DEFAULT '',
            route_id TEXT NOT NULL DEFAULT '',
            current_stage TEXT NOT NULL DEFAULT '',
            current_step_index INTEGER NOT NULL DEFAULT -1,
            parent_entity_id TEXT NOT NULL DEFAULT '',
            current_entity_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS official_workflow_instances (
            instance_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workflow_session_id TEXT NOT NULL,
            client_flow_line_id TEXT NOT NULL,
            instance_flow_line_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, workflow_session_id, client_flow_line_id, generation),
            UNIQUE(project_id, workflow_session_id, client_flow_line_id, instance_flow_line_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_official_workflow_instance_history "
        "ON official_workflow_instances("
        "project_id, workflow_session_id, client_flow_line_id, generation DESC)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_official_workflow_instance_active "
        "ON official_workflow_instances(project_id, workflow_session_id, client_flow_line_id) "
        "WHERE status = 'active'"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_official_workflow_route "
        "ON official_workflow_state(route_id, updated_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS official_workflow_receipts (
            receipt_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            stage TEXT NOT NULL,
            upstream_revision TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(scope_id, route_id, step_index)
        )
        """
    )


def _workflow_instance_from_row(row: Any) -> WorkflowInstance:
    return WorkflowInstance(
        instance_id=str(row[0]),
        project_id=str(row[1]),
        workflow_session_id=str(row[2]),
        client_flow_line_id=str(row[3]),
        flow_line_id=str(row[4]),
        route_id=str(row[5]),
        generation=int(row[6]),
        status=str(row[7]),
    )


def _workflow_instance_is_complete(
    connection: Any,
    instance: WorkflowInstance,
) -> bool:
    from plastic_promise.core.official_workflow import OFFICIAL_ROUTES

    route = OFFICIAL_ROUTES.get(instance.route_id)
    if route is None or not route.stages:
        return False
    scope_id = compose_flow_scope(
        instance.workflow_session_id,
        instance.flow_line_id,
        instance.project_id,
    )
    state = load_workflow_state(connection, scope_id)
    return bool(state is not None and state.current_step_index >= len(route.stages) - 1)


def resolve_workflow_instance(
    connection: Any,
    *,
    project_id: str,
    workflow_session_id: str,
    client_flow_line_id: str,
    requested_route: str,
    new_root_selected: bool,
    starts_workflow: bool,
) -> WorkflowInstance | None:
    """Resolve the active workflow run without reusing completed cursors."""

    project = str(project_id or "").strip()
    session = str(workflow_session_id or "").strip() or "default"
    client_flow = str(client_flow_line_id or "").strip() or "default"
    route_id = str(requested_route or "").strip()
    had_transaction = bool(connection.in_transaction)
    ensure_workflow_state_schema(connection)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def active_instance() -> WorkflowInstance | None:
        row = connection.execute(
            """
            SELECT instance_id, project_id, workflow_session_id, client_flow_line_id,
                   instance_flow_line_id, route_id, generation, status
              FROM official_workflow_instances
             WHERE project_id = ? AND workflow_session_id = ?
               AND client_flow_line_id = ? AND status = 'active'
             ORDER BY generation DESC LIMIT 1
            """,
            (project, session, client_flow),
        ).fetchone()
        return _workflow_instance_from_row(row) if row is not None else None

    try:
        active = active_instance()
        if active is None:
            legacy_scope = compose_flow_scope(session, client_flow, project)
            legacy_state = load_workflow_state(connection, legacy_scope)
            if legacy_state is not None and legacy_state.route_id:
                legacy = WorkflowInstance(
                    instance_id="workflow-instance:"
                    + hashlib.sha256(
                        f"{project}\x1f{session}\x1f{client_flow}\x1flegacy".encode()
                    ).hexdigest(),
                    project_id=project,
                    workflow_session_id=session,
                    client_flow_line_id=client_flow,
                    flow_line_id=client_flow,
                    route_id=legacy_state.route_id,
                    generation=0,
                    status="active",
                )
                legacy_status = (
                    "completed" if _workflow_instance_is_complete(connection, legacy) else "active"
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO official_workflow_instances (
                        instance_id, project_id, workflow_session_id, client_flow_line_id,
                        instance_flow_line_id, route_id, generation, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy.instance_id,
                        project,
                        session,
                        client_flow,
                        client_flow,
                        legacy.route_id,
                        0,
                        legacy_status,
                        now,
                        now,
                    ),
                )
                active = active_instance()

        if active is not None and _workflow_instance_is_complete(connection, active):
            connection.execute(
                "UPDATE official_workflow_instances SET status = 'completed', updated_at = ? "
                "WHERE instance_id = ? AND status = 'active'",
                (now, active.instance_id),
            )
            active = None

        if active is not None and not new_root_selected:
            if not had_transaction:
                connection.commit()
            return active

        if active is not None:
            connection.execute(
                "UPDATE official_workflow_instances SET status = 'superseded', updated_at = ? "
                "WHERE instance_id = ? AND status = 'active'",
                (now, active.instance_id),
            )

        if not starts_workflow:
            if not had_transaction:
                connection.commit()
            return None

        row = connection.execute(
            """
            SELECT COALESCE(MAX(generation), 0)
              FROM official_workflow_instances
             WHERE project_id = ? AND workflow_session_id = ? AND client_flow_line_id = ?
            """,
            (project, session, client_flow),
        ).fetchone()
        generation = int(row[0] or 0) + 1
        flow_line_id = f"{client_flow}:workflow:{generation}"
        identity = f"{project}\x1f{session}\x1f{client_flow}\x1f{generation}\x1f{route_id}"
        instance = WorkflowInstance(
            instance_id="workflow-instance:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            project_id=project,
            workflow_session_id=session,
            client_flow_line_id=client_flow,
            flow_line_id=flow_line_id,
            route_id=route_id,
            generation=generation,
            status="active",
        )
        connection.execute(
            """
            INSERT INTO official_workflow_instances (
                instance_id, project_id, workflow_session_id, client_flow_line_id,
                instance_flow_line_id, route_id, generation, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                instance.instance_id,
                project,
                session,
                client_flow,
                instance.flow_line_id,
                instance.route_id,
                instance.generation,
                now,
                now,
            ),
        )
        if not had_transaction:
            connection.commit()
        return instance
    except BaseException:
        if not had_transaction:
            connection.rollback()
        raise


def load_workflow_state(connection, scope_id: str) -> WorkflowState | None:
    row = connection.execute(
        """
        SELECT scope_id, stage_session_id, flow_line_id, route_id,
               current_stage, current_step_index, parent_entity_id,
               current_entity_id
          FROM official_workflow_state
         WHERE scope_id = ?
        """,
        (scope_id,),
    ).fetchone()
    if row is None:
        return None
    return WorkflowState(
        scope_id=str(row[0]),
        stage_session_id=str(row[1]),
        flow_line_id=str(row[2]),
        route_id=str(row[3]),
        current_stage=str(row[4]) or None,
        current_step_index=int(row[5]),
        parent_entity_id=str(row[6]) or None,
        current_entity_id=str(row[7]) or None,
    )


def save_workflow_state(connection, state: WorkflowState) -> None:
    owns_transaction = not connection.in_transaction
    ensure_workflow_state_schema(connection)
    _upsert_workflow_state(connection, state)
    if owns_transaction:
        connection.commit()


def _upsert_workflow_state(connection, state: WorkflowState) -> None:
    connection.execute(
        """
        INSERT INTO official_workflow_state (
            scope_id, stage_session_id, flow_line_id, route_id,
            current_stage, current_step_index, parent_entity_id,
            current_entity_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope_id) DO UPDATE SET
            stage_session_id = excluded.stage_session_id,
            flow_line_id = excluded.flow_line_id,
            route_id = excluded.route_id,
            current_stage = excluded.current_stage,
            current_step_index = excluded.current_step_index,
            parent_entity_id = excluded.parent_entity_id,
            current_entity_id = excluded.current_entity_id,
            updated_at = excluded.updated_at
        """,
        (
            state.scope_id,
            state.stage_session_id,
            state.flow_line_id,
            state.route_id,
            state.current_stage or "",
            state.current_step_index,
            state.parent_entity_id or "",
            state.current_entity_id or "",
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )


def commit_workflow_transition(
    connection,
    *,
    scope_id: str,
    route_id: str,
    step_index: int,
    receipt: dict[str, Any],
    current_stage: str,
    parent_entity_id: str | None = None,
) -> str:
    """Atomically persist a validated receipt and its resulting workflow cursor."""
    ensure_workflow_state_schema(connection)
    if connection.in_transaction:
        raise RuntimeError("workflow_transition_requires_clean_connection")
    receipt_id, evidence_json, expected = _execution_receipt_identity(
        scope_id=scope_id,
        route_id=route_id,
        step_index=step_index,
        receipt=receipt,
    )
    previous = load_workflow_state(connection, scope_id)
    public_session_id, flow_line_id = split_flow_scope(scope_id)
    state = WorkflowState(
        scope_id=scope_id,
        stage_session_id=public_session_id,
        flow_line_id=flow_line_id,
        route_id=route_id,
        current_stage=current_stage,
        current_step_index=step_index,
        parent_entity_id=parent_entity_id
        if parent_entity_id is not None
        else previous.parent_entity_id
        if previous is not None
        else None,
        current_entity_id=previous.current_entity_id if previous is not None else None,
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO official_workflow_receipts (
                receipt_id, scope_id, route_id, step_index, stage,
                upstream_revision, content_sha256, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_id, route_id, step_index) DO NOTHING
            """,
            (
                receipt_id,
                scope_id,
                route_id,
                step_index,
                receipt["skill"],
                receipt["upstream_revision"],
                receipt["content_sha256"],
                evidence_json,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        row = connection.execute(
            """
            SELECT receipt_id, stage, upstream_revision, content_sha256, evidence_json
              FROM official_workflow_receipts
             WHERE scope_id = ? AND route_id = ? AND step_index = ?
            """,
            (scope_id, route_id, step_index),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise ValueError("workflow_receipt_conflict")
        _upsert_workflow_state(connection, state)
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    return receipt_id


def inspect_execution_receipt(
    connection,
    *,
    scope_id: str,
    route_id: str,
    step_index: int,
    receipt: dict[str, Any],
) -> tuple[str, str]:
    """Return ``missing``, ``match``, or ``conflict`` before adapter execution."""
    ensure_workflow_state_schema(connection)
    receipt_id, _evidence_json, expected = _execution_receipt_identity(
        scope_id=scope_id,
        route_id=route_id,
        step_index=step_index,
        receipt=receipt,
    )
    row = connection.execute(
        """
        SELECT receipt_id, stage, upstream_revision, content_sha256, evidence_json
          FROM official_workflow_receipts
         WHERE scope_id = ? AND route_id = ? AND step_index = ?
        """,
        (scope_id, route_id, step_index),
    ).fetchone()
    if row is None:
        return "missing", receipt_id
    if tuple(row) == expected:
        return "match", receipt_id
    return "conflict", str(row[0])


def _execution_receipt_identity(
    *,
    scope_id: str,
    route_id: str,
    step_index: int,
    receipt: dict[str, Any],
) -> tuple[str, str, tuple[str, str, str, str, str]]:
    evidence_json = json.dumps(
        receipt["evidence"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    identity = "\x1f".join(
        (
            scope_id,
            route_id,
            str(step_index),
            str(receipt["skill"]),
            str(receipt["upstream_revision"]),
            str(receipt["content_sha256"]),
            evidence_json,
        )
    )
    receipt_id = "workflow-receipt:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    expected = (
        receipt_id,
        receipt["skill"],
        receipt["upstream_revision"],
        receipt["content_sha256"],
        evidence_json,
    )
    return receipt_id, evidence_json, expected
