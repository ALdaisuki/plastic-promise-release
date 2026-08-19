"""Verify-only SQLite adapter for durable coordination-plan authority.

The deployment migration owns this module's tables, indexes, triggers, and
schema marker.  Runtime construction is intentionally read-only: callers pass
the already-open canonical server SQLite connection and the pp-core
single-writer transaction factory.  The adapter never opens a second
connection and exposes no schema installer.

Plans, activations, Top-Level Agent bindings, and resource-usage receipts are
stored as canonical JSON plus denormalized lookup columns.  Every read
reconstructs the typed contract and compares both representations, so copied
portable JSON and partially tampered rows fail closed.  Only the current head
can accept new usage; superseded plans remain immutable historical evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .canonical_time import canonical_text, server_now
from .contracts import AgentIdentity, AgentSession, ProjectScope
from .coordination_plan import (
    _SERVER_AUTHORITY_TOKEN,
    COORDINATION_PLAN_ACTIVATION_SCHEMA,
    COORDINATION_PLAN_ISSUER,
    COORDINATION_PLAN_SCHEMA,
    DELEGATION_EDGE_SCHEMA,
    RESOURCE_ALLOCATION_SCHEMA,
    RESOURCE_USAGE_RECEIPT_SCHEMA,
    RESPONSIBILITY_NODE_SCHEMA,
    TOKEN_AUTHORITY_PROVIDER,
    TOP_LEVEL_AGENT_BINDING_SCHEMA,
    USER_MANDATE_SCHEMA,
    CoordinationPlan,
    CoordinationPlanActivation,
    CoordinationPlanError,
    DelegationEdge,
    ResourceAllocation,
    ResourceUsageReceipt,
    ResponsibilityNode,
    TopLevelAgentBinding,
    UserMandate,
    _digest,
    _identifier,
    _optional_digest,
    _require_activation_matches_plan,
    _require_binding_matches_session_and_mandate,
    _require_session_for_mandate,
    _require_session_for_plan,
    _require_session_scope,
    _require_usage_within_budget,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from datetime import datetime


DURABLE_COORDINATION_PLAN_SCHEMA_REVISION = "collaboration-coordination-plan/sqlite-v2"
DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_SCHEMA = TOP_LEVEL_AGENT_BINDING_SCHEMA

DURABLE_COORDINATION_PLAN_REQUIRED_TABLES = (
    "collaboration_coordination_plan_schema",
    "collaboration_coordination_plan_top_level_bindings",
    "collaboration_coordination_plans",
    "collaboration_coordination_plan_activations",
    "collaboration_coordination_plan_heads",
    "collaboration_resource_usage_receipts",
)

(
    _SCHEMA_TABLE,
    _TOP_LEVEL_BINDING_TABLE,
    _PLAN_TABLE,
    _ACTIVATION_TABLE,
    _HEAD_TABLE,
    _USAGE_TABLE,
) = DURABLE_COORDINATION_PLAN_REQUIRED_TABLES

DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES = (
    "idx_collaboration_coordination_plan_bindings_session",
    "idx_collaboration_coordination_plans_scope_revision",
    "idx_collaboration_coordination_plan_activations_scope_revision",
    "idx_collaboration_resource_usage_plan_node_measurement",
)

DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS = (
    "collaboration_coordination_plan_top_level_bindings_no_update",
    "collaboration_coordination_plan_top_level_bindings_no_delete",
    "collaboration_coordination_plans_no_update",
    "collaboration_coordination_plans_no_delete",
    "collaboration_coordination_plan_activations_no_update",
    "collaboration_coordination_plan_activations_no_delete",
    "collaboration_coordination_plan_heads_no_delete",
    "collaboration_resource_usage_receipts_no_update",
    "collaboration_resource_usage_receipts_no_delete",
)

DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS = {
    _SCHEMA_TABLE: (
        "singleton",
        "schema_revision",
        "installed_at_utc",
    ),
    _TOP_LEVEL_BINDING_TABLE: (
        "project_id",
        "coordination_session_id",
        "top_level_agent_session_id",
        "top_level_agent_id",
        "agent_session_sha256",
        "mandate_sha256",
        "binding_generation",
        "bound_at_utc",
        "binding_json",
        "binding_sha256",
    ),
    _PLAN_TABLE: (
        "plan_sha256",
        "plan_id",
        "plan_revision",
        "mandate_sha256",
        "project_id",
        "coordination_session_id",
        "top_level_agent_session_id",
        "root_node_id",
        "created_at_utc",
        "expires_at_utc",
        "supersedes_plan_sha256",
        "plan_json",
    ),
    _ACTIVATION_TABLE: (
        "activation_sha256",
        "activation_id",
        "plan_sha256",
        "plan_id",
        "plan_revision",
        "mandate_sha256",
        "project_id",
        "coordination_session_id",
        "top_level_agent_session_id",
        "issued_at_utc",
        "expires_at_utc",
        "supersedes_activation_sha256",
        "activation_json",
    ),
    _HEAD_TABLE: (
        "project_id",
        "coordination_session_id",
        "plan_sha256",
        "activation_sha256",
        "plan_id",
        "plan_revision",
        "head_generation",
        "updated_at_utc",
    ),
    _USAGE_TABLE: (
        "usage_receipt_sha256",
        "receipt_id",
        "plan_sha256",
        "responsibility_node_id",
        "agent_session_id",
        "token_usage",
        "token_measurement",
        "measurement_evidence_sha256",
        "recorded_at_utc",
        "receipt_json",
    ),
}

DURABLE_COORDINATION_PLAN_DEPENDENCY_COLUMNS = {
    "collaboration_agents": (
        "project_id",
        "agent_id",
        "role",
        "parent_agent_id",
        "capabilities_json",
        "identity_json",
        "state",
    ),
    "collaboration_agent_sessions": (
        "session_id",
        "project_id",
        "agent_id",
        "coordination_session_id",
        "identity_json",
        "session_json",
        "session_sha256",
        "state",
        "started_at",
        "last_heartbeat_at",
        "expires_at",
    ),
}

DURABLE_COORDINATION_PLAN_TABLE_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {_SCHEMA_TABLE} (
        singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_TOP_LEVEL_BINDING_TABLE} (
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        top_level_agent_session_id TEXT NOT NULL,
        top_level_agent_id TEXT NOT NULL,
        agent_session_sha256 TEXT NOT NULL,
        mandate_sha256 TEXT NOT NULL,
        binding_generation INTEGER NOT NULL CHECK(binding_generation > 0),
        bound_at_utc TEXT NOT NULL,
        binding_json TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        PRIMARY KEY(project_id, coordination_session_id),
        UNIQUE(top_level_agent_session_id),
        UNIQUE(binding_sha256),
        FOREIGN KEY(top_level_agent_session_id)
            REFERENCES collaboration_agent_sessions(session_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(project_id, top_level_agent_id)
            REFERENCES collaboration_agents(project_id, agent_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_PLAN_TABLE} (
        plan_sha256 TEXT NOT NULL PRIMARY KEY,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL CHECK(plan_revision > 0),
        mandate_sha256 TEXT NOT NULL,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        top_level_agent_session_id TEXT NOT NULL,
        root_node_id TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        supersedes_plan_sha256 TEXT,
        plan_json TEXT NOT NULL,
        UNIQUE(plan_id, plan_revision),
        FOREIGN KEY(top_level_agent_session_id)
            REFERENCES collaboration_agent_sessions(session_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(supersedes_plan_sha256)
            REFERENCES {_PLAN_TABLE}(plan_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        CHECK(
            (plan_revision = 1 AND supersedes_plan_sha256 IS NULL)
            OR (plan_revision > 1 AND supersedes_plan_sha256 IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_ACTIVATION_TABLE} (
        activation_sha256 TEXT NOT NULL PRIMARY KEY,
        activation_id TEXT NOT NULL UNIQUE,
        plan_sha256 TEXT NOT NULL UNIQUE,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL CHECK(plan_revision > 0),
        mandate_sha256 TEXT NOT NULL,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        top_level_agent_session_id TEXT NOT NULL,
        issued_at_utc TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,
        supersedes_activation_sha256 TEXT,
        activation_json TEXT NOT NULL,
        FOREIGN KEY(plan_sha256)
            REFERENCES {_PLAN_TABLE}(plan_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(top_level_agent_session_id)
            REFERENCES collaboration_agent_sessions(session_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(supersedes_activation_sha256)
            REFERENCES {_ACTIVATION_TABLE}(activation_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        CHECK(
            (plan_revision = 1 AND supersedes_activation_sha256 IS NULL)
            OR (plan_revision > 1 AND supersedes_activation_sha256 IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_HEAD_TABLE} (
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL,
        activation_sha256 TEXT NOT NULL UNIQUE,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL CHECK(plan_revision > 0),
        head_generation INTEGER NOT NULL CHECK(head_generation > 0),
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY(project_id, coordination_session_id),
        FOREIGN KEY(plan_sha256)
            REFERENCES {_PLAN_TABLE}(plan_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(activation_sha256)
            REFERENCES {_ACTIVATION_TABLE}(activation_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {_USAGE_TABLE} (
        usage_receipt_sha256 TEXT NOT NULL PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        plan_sha256 TEXT NOT NULL,
        responsibility_node_id TEXT NOT NULL,
        agent_session_id TEXT NOT NULL,
        token_usage INTEGER,
        token_measurement TEXT NOT NULL,
        measurement_evidence_sha256 TEXT NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        FOREIGN KEY(plan_sha256)
            REFERENCES {_PLAN_TABLE}(plan_sha256)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(agent_session_id)
            REFERENCES collaboration_agent_sessions(session_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        CHECK(
            (token_measurement = 'unavailable'
                AND token_usage IS NULL
                AND measurement_evidence_sha256 = '')
            OR (token_measurement = 'agent-estimate'
                AND token_usage IS NOT NULL
                AND token_usage >= 0)
            OR (token_measurement = 'provider-authoritative'
                AND token_usage IS NOT NULL
                AND token_usage >= 0
                AND measurement_evidence_sha256 <> '')
        )
    )
    """,
)

# Deployment migration needs to rebuild only this one v1 table.  Keep the
# statement named at the adapter seam instead of making deployment depend on
# the incidental position of a member in ``DURABLE_COORDINATION_PLAN_SCHEMA_DDL``.
DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_DDL = DURABLE_COORDINATION_PLAN_TABLE_DDL[1]

DURABLE_COORDINATION_PLAN_INDEX_DDL = (
    f"""
    CREATE INDEX IF NOT EXISTS {DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[0]}
        ON {_TOP_LEVEL_BINDING_TABLE}(top_level_agent_session_id)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS {DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[1]}
        ON {_PLAN_TABLE}(project_id, coordination_session_id, plan_revision)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS {DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[2]}
        ON {_ACTIVATION_TABLE}(project_id, coordination_session_id, plan_revision)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS {DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[3]}
        ON {_USAGE_TABLE}(plan_sha256, responsibility_node_id, token_measurement)
    """,
)


def _append_only_trigger_ddl(*, name: str, table: str, action: str, failure: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name}
    BEFORE {action} ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{failure}');
    END
    """


DURABLE_COORDINATION_PLAN_TRIGGER_DDL = (
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[0],
        table=_TOP_LEVEL_BINDING_TABLE,
        action="UPDATE",
        failure="coordination_plan_top_level_binding_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[1],
        table=_TOP_LEVEL_BINDING_TABLE,
        action="DELETE",
        failure="coordination_plan_top_level_binding_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[2],
        table=_PLAN_TABLE,
        action="UPDATE",
        failure="coordination_plan_plan_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[3],
        table=_PLAN_TABLE,
        action="DELETE",
        failure="coordination_plan_plan_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[4],
        table=_ACTIVATION_TABLE,
        action="UPDATE",
        failure="coordination_plan_activation_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[5],
        table=_ACTIVATION_TABLE,
        action="DELETE",
        failure="coordination_plan_activation_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[6],
        table=_HEAD_TABLE,
        action="DELETE",
        failure="coordination_plan_head_delete_forbidden",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[7],
        table=_USAGE_TABLE,
        action="UPDATE",
        failure="coordination_plan_usage_append_only",
    ),
    _append_only_trigger_ddl(
        name=DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[8],
        table=_USAGE_TABLE,
        action="DELETE",
        failure="coordination_plan_usage_append_only",
    ),
)

DURABLE_COORDINATION_PLAN_SCHEMA_DDL = (
    *DURABLE_COORDINATION_PLAN_TABLE_DDL,
    *DURABLE_COORDINATION_PLAN_INDEX_DDL,
    *DURABLE_COORDINATION_PLAN_TRIGGER_DDL,
)


def _schema_manifest_sha256() -> str:
    payload = json.dumps(
        {
            "schema_revision": DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,
            "ddl": [
                " ".join(statement.split()) for statement in DURABLE_COORDINATION_PLAN_SCHEMA_DDL
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


DURABLE_COORDINATION_PLAN_SCHEMA_MANIFEST_SHA256 = _schema_manifest_sha256()

_INDEX_SPECS = {
    DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[0]: (
        _TOP_LEVEL_BINDING_TABLE,
        ("top_level_agent_session_id",),
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[1]: (
        _PLAN_TABLE,
        ("project_id", "coordination_session_id", "plan_revision"),
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[2]: (
        _ACTIVATION_TABLE,
        ("project_id", "coordination_session_id", "plan_revision"),
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_INDEXES[3]: (
        _USAGE_TABLE,
        ("plan_sha256", "responsibility_node_id", "token_measurement"),
    ),
}

_UNIQUE_COLUMN_SETS = {
    _TOP_LEVEL_BINDING_TABLE: (
        ("project_id", "coordination_session_id"),
        ("top_level_agent_session_id",),
        ("binding_sha256",),
    ),
    _PLAN_TABLE: (("plan_sha256",), ("plan_id", "plan_revision")),
    _ACTIVATION_TABLE: (
        ("activation_sha256",),
        ("activation_id",),
        ("plan_sha256",),
    ),
    _HEAD_TABLE: (
        ("project_id", "coordination_session_id"),
        ("activation_sha256",),
    ),
    _USAGE_TABLE: (("usage_receipt_sha256",), ("receipt_id",)),
}

_FOREIGN_KEYS = {
    _TOP_LEVEL_BINDING_TABLE: {
        ("collaboration_agent_sessions", "top_level_agent_session_id", "session_id"),
        ("collaboration_agents", "project_id", "project_id"),
        ("collaboration_agents", "top_level_agent_id", "agent_id"),
    },
    _PLAN_TABLE: {
        ("collaboration_agent_sessions", "top_level_agent_session_id", "session_id"),
        (_PLAN_TABLE, "supersedes_plan_sha256", "plan_sha256"),
    },
    _ACTIVATION_TABLE: {
        (_PLAN_TABLE, "plan_sha256", "plan_sha256"),
        ("collaboration_agent_sessions", "top_level_agent_session_id", "session_id"),
        (_ACTIVATION_TABLE, "supersedes_activation_sha256", "activation_sha256"),
    },
    _HEAD_TABLE: {
        (_PLAN_TABLE, "plan_sha256", "plan_sha256"),
        (_ACTIVATION_TABLE, "activation_sha256", "activation_sha256"),
    },
    _USAGE_TABLE: {
        (_PLAN_TABLE, "plan_sha256", "plan_sha256"),
        ("collaboration_agent_sessions", "agent_session_id", "session_id"),
    },
}

_TRIGGER_SPECS = {
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[0]: (
        _TOP_LEVEL_BINDING_TABLE,
        "before update on",
        "coordination_plan_top_level_binding_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[1]: (
        _TOP_LEVEL_BINDING_TABLE,
        "before delete on",
        "coordination_plan_top_level_binding_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[2]: (
        _PLAN_TABLE,
        "before update on",
        "coordination_plan_plan_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[3]: (
        _PLAN_TABLE,
        "before delete on",
        "coordination_plan_plan_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[4]: (
        _ACTIVATION_TABLE,
        "before update on",
        "coordination_plan_activation_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[5]: (
        _ACTIVATION_TABLE,
        "before delete on",
        "coordination_plan_activation_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[6]: (
        _HEAD_TABLE,
        "before delete on",
        "coordination_plan_head_delete_forbidden",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[7]: (
        _USAGE_TABLE,
        "before update on",
        "coordination_plan_usage_append_only",
    ),
    DURABLE_COORDINATION_PLAN_REQUIRED_TRIGGERS[8]: (
        _USAGE_TABLE,
        "before delete on",
        "coordination_plan_usage_append_only",
    ),
}

_TABLE_SQL_FRAGMENTS = {
    _SCHEMA_TABLE: ("check(singleton = 1)",),
    _TOP_LEVEL_BINDING_TABLE: ("check(binding_generation > 0)",),
    _PLAN_TABLE: (
        "check(plan_revision > 0)",
        "plan_revision = 1 and supersedes_plan_sha256 is null",
        "plan_revision > 1 and supersedes_plan_sha256 is not null",
    ),
    _ACTIVATION_TABLE: (
        "check(plan_revision > 0)",
        "plan_revision = 1 and supersedes_activation_sha256 is null",
        "plan_revision > 1 and supersedes_activation_sha256 is not null",
    ),
    _HEAD_TABLE: (
        "check(plan_revision > 0)",
        "check(head_generation > 0)",
    ),
    _USAGE_TABLE: (
        "token_measurement = 'unavailable'",
        "token_measurement = 'agent-estimate'",
        "token_measurement = 'provider-authoritative'",
        "token_usage >= 0",
    ),
}

_MANDATE_FIELDS = frozenset(
    {
        "schema_version",
        "mandate_id",
        "project_id",
        "coordination_session_id",
        "user_instruction_sha256",
        "objective",
        "constraints",
        "issued_at_utc",
        "expires_at_utc",
        "raw_user_content",
        "authority_effect",
    }
)
_ALLOCATION_FIELDS = frozenset(
    {"schema_version", "agent_slots", "token_budget", "token_budget_authority"}
)
_NODE_FIELDS = frozenset(
    {
        "schema_version",
        "node_id",
        "work_item_id",
        "responsibility_fingerprint",
        "role_intent",
        "scope",
        "allowed_paths",
        "allowed_tools",
        "acceptance_conditions",
        "allocation",
        "can_delegate",
        "authority_effect",
    }
)
_EDGE_FIELDS = frozenset({"schema_version", "parent_node_id", "child_node_id"})
_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "plan_id",
        "plan_revision",
        "project_id",
        "coordination_session_id",
        "mandate",
        "mandate_sha256",
        "top_level_agent_session_id",
        "root_node_id",
        "nodes",
        "edges",
        "total_allocation",
        "created_at_utc",
        "expires_at_utc",
        "supersedes_plan_sha256",
        "frozen",
        "authority_effect",
    }
)
_ACTIVATION_FIELDS = frozenset(
    {
        "schema_version",
        "issuer",
        "activation_id",
        "plan_id",
        "plan_revision",
        "plan_sha256",
        "mandate_sha256",
        "project_id",
        "coordination_session_id",
        "top_level_agent_session_id",
        "issued_at_utc",
        "expires_at_utc",
        "supersedes_activation_sha256",
        "authority_effect",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "plan_sha256",
        "responsibility_node_id",
        "agent_session_id",
        "token_usage",
        "token_measurement",
        "measurement_evidence_sha256",
        "recorded_at_utc",
        "budget_authority_effect",
    }
)
_IDENTITY_FIELDS = frozenset({"agent_id", "role", "parent_agent_id", "capabilities"})
_SESSION_FIELDS = frozenset(
    {
        "session_id",
        "identity",
        "project_id",
        "coordination_session_id",
        "state",
        "started_at",
        "last_heartbeat_at",
        "expires_at",
    }
)
_TOP_LEVEL_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "coordination_session_id",
        "top_level_agent_session_id",
        "top_level_agent_id",
        "agent_session_sha256",
        "mandate_sha256",
        "binding_generation",
        "bound_at_utc",
        "authority_effect",
    }
)
_ACTIVE_STATES = frozenset({"registered", "active", "idle"})


class DurableCoordinationPlanRepository:
    """Canonical SQLite adapter satisfying ``CoordinationPlanRepository``."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise CoordinationPlanError("coordination_plan_durable_connection_invalid")
        if not callable(transaction_factory):
            raise CoordinationPlanError("coordination_plan_durable_writer_required")
        if clock is not None and not callable(clock):
            raise CoordinationPlanError("coordination_plan_clock_invalid")
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._clock = clock
        self._verify_schema()

    def load_plan_by_digest(self, plan_sha256: str) -> CoordinationPlan | None:
        digest = _digest(plan_sha256, "coordination_plan_digest_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_PLAN_TABLE} WHERE plan_sha256=?",
            (digest,),
        )
        return None if row is None else _plan_from_row(row)

    def load_activation_by_digest(
        self,
        activation_sha256: str,
    ) -> CoordinationPlanActivation | None:
        digest = _digest(
            activation_sha256,
            "coordination_plan_activation_digest_invalid",
        )
        row = self._fetchone(
            f"SELECT * FROM {_ACTIVATION_TABLE} WHERE activation_sha256=?",
            (digest,),
        )
        return None if row is None else _activation_from_row(row)

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation] | None:
        project = ProjectScope(project_id)
        coordination = _identifier(
            coordination_session_id,
            "coordination_plan_session_invalid",
        )
        head = self._fetchone(
            f"""
            SELECT * FROM {_HEAD_TABLE}
             WHERE project_id=? AND coordination_session_id=?
            """,
            (project.project_id, coordination),
        )
        if head is None:
            return None
        plan = self.load_plan_by_digest(str(head["plan_sha256"]))
        activation = self.load_activation_by_digest(str(head["activation_sha256"]))
        if plan is None or activation is None:
            raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
        _require_activation_matches_plan(plan, activation)
        expected = {
            "project_id": plan.project.project_id,
            "coordination_session_id": plan.coordination_session_id,
            "plan_sha256": plan.content_sha256,
            "activation_sha256": activation.activation_sha256,
            "plan_id": plan.plan_id,
            "plan_revision": plan.plan_revision,
        }
        if any(head[key] != value for key, value in expected.items()):
            raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
        _positive_sql_integer(
            head["head_generation"],
            "coordination_plan_durable_record_corrupt",
        )
        _canonical_timestamp(
            head["updated_at_utc"],
            "coordination_plan_durable_record_corrupt",
        )
        return plan, activation

    def resolve_registered_session(self, agent_session_id: str) -> AgentSession:
        session, _ = self._load_agent_session(
            agent_session_id,
            missing_code="coordination_plan_top_level_session_missing",
        )
        return session

    def append_top_level_binding(
        self,
        mandate: UserMandate,
        *,
        agent_session_id: str,
        expected_binding_generation: int,
        _authority_token: object | None = None,
    ) -> TopLevelAgentBinding:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError(
                "coordination_plan_top_level_binding_write_authority_required"
            )
        if not isinstance(mandate, UserMandate):
            raise CoordinationPlanError("user_mandate_invalid")
        expected = _non_negative_sql_integer(
            expected_binding_generation,
            "coordination_plan_top_level_binding_generation_invalid",
        )

        try:
            with self._transaction_factory():
                session, registration_sha256 = self._load_agent_session(
                    agent_session_id,
                    missing_code="coordination_plan_top_level_session_missing",
                )
                _require_session_for_mandate(
                    session,
                    mandate,
                    now=server_now(self._clock),
                )
                existing = self.load_top_level_binding(
                    project_id=mandate.project.project_id,
                    coordination_session_id=mandate.coordination_session_id,
                )
                if existing is not None:
                    if (
                        existing.top_level_agent_session_id == session.session_id
                        and hmac.compare_digest(
                            existing.agent_session_sha256,
                            registration_sha256,
                        )
                        and hmac.compare_digest(
                            existing.mandate_sha256,
                            mandate.content_sha256,
                        )
                    ):
                        if expected not in {0, existing.binding_generation}:
                            raise CoordinationPlanError(
                                "coordination_plan_top_level_binding_generation_conflict"
                            )
                        return existing
                    raise CoordinationPlanError("coordination_plan_top_level_binding_conflict")
                if expected != 0:
                    raise CoordinationPlanError(
                        "coordination_plan_top_level_binding_generation_conflict"
                    )
                binding = TopLevelAgentBinding(
                    project=mandate.project,
                    coordination_session_id=mandate.coordination_session_id,
                    top_level_agent_session_id=session.session_id,
                    top_level_agent_id=session.identity.agent_id,
                    agent_session_sha256=registration_sha256,
                    mandate_sha256=mandate.content_sha256,
                    binding_generation=1,
                    bound_at_utc=canonical_text(server_now(self._clock)),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO {_TOP_LEVEL_BINDING_TABLE} (
                        project_id, coordination_session_id,
                        top_level_agent_session_id, top_level_agent_id,
                        agent_session_sha256, mandate_sha256,
                        binding_generation, bound_at_utc,
                        binding_json, binding_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.project.project_id,
                        binding.coordination_session_id,
                        binding.top_level_agent_session_id,
                        binding.top_level_agent_id,
                        binding.agent_session_sha256,
                        binding.mandate_sha256,
                        binding.binding_generation,
                        binding.bound_at_utc,
                        binding.canonical_json(),
                        binding.binding_sha256,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            winner = self.load_top_level_binding(
                project_id=mandate.project.project_id,
                coordination_session_id=mandate.coordination_session_id,
            )
            if winner is not None:
                session, registration_sha256 = self._load_agent_session(
                    agent_session_id,
                    missing_code="coordination_plan_top_level_session_missing",
                )
                if (
                    winner.top_level_agent_session_id == session.session_id
                    and hmac.compare_digest(
                        winner.agent_session_sha256,
                        registration_sha256,
                    )
                    and hmac.compare_digest(
                        winner.mandate_sha256,
                        mandate.content_sha256,
                    )
                    and expected in {0, winner.binding_generation}
                ):
                    return winner
            raise CoordinationPlanError("coordination_plan_top_level_binding_conflict") from exc

        stored = self.load_top_level_binding(
            project_id=mandate.project.project_id,
            coordination_session_id=mandate.coordination_session_id,
        )
        if stored is None:
            raise CoordinationPlanError("coordination_plan_top_level_binding_append_missing")
        return stored

    def load_top_level_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
    ) -> TopLevelAgentBinding | None:
        project = ProjectScope(project_id)
        coordination = _identifier(
            coordination_session_id,
            "coordination_plan_session_invalid",
        )
        row = self._fetchone(
            f"""
            SELECT * FROM {_TOP_LEVEL_BINDING_TABLE}
             WHERE project_id=? AND coordination_session_id=?
            """,
            (project.project_id, coordination),
        )
        if row is None:
            return None
        session, registration_sha256 = self._load_agent_session(
            str(row["top_level_agent_session_id"]),
            missing_code="coordination_plan_top_level_session_missing",
        )
        return _top_level_binding_from_row(
            row,
            session=session,
            registration_sha256=registration_sha256,
        )

    def require_top_level_session(self, plan: CoordinationPlan) -> None:
        if not isinstance(plan, CoordinationPlan):
            raise CoordinationPlanError("coordination_plan_invalid")
        binding = self.load_top_level_binding(
            project_id=plan.project.project_id,
            coordination_session_id=plan.coordination_session_id,
        )
        if binding is None:
            raise CoordinationPlanError("coordination_plan_top_level_binding_missing")
        if binding.top_level_agent_session_id != plan.top_level_agent_session_id:
            raise CoordinationPlanError("coordination_plan_top_level_session_mismatch")
        session, registration_sha256 = self._load_agent_session(
            plan.top_level_agent_session_id,
            missing_code="coordination_plan_top_level_session_missing",
        )
        if not hmac.compare_digest(
            binding.agent_session_sha256,
            registration_sha256,
        ):
            raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
        _require_binding_matches_session_and_mandate(
            binding,
            session=session,
            mandate=plan.mandate,
        )
        _require_session_for_plan(session, plan, now=server_now(self._clock))

    def append_and_activate(
        self,
        plan: CoordinationPlan,
        activation: CoordinationPlanActivation,
        *,
        expected_current_activation_sha256: str,
        _authority_token: object | None = None,
    ) -> tuple[CoordinationPlan, CoordinationPlanActivation]:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError("coordination_plan_repository_write_authority_required")
        if not isinstance(plan, CoordinationPlan) or not isinstance(
            activation,
            CoordinationPlanActivation,
        ):
            raise CoordinationPlanError("coordination_plan_invalid")
        _require_activation_matches_plan(plan, activation)
        expected = _optional_digest(
            expected_current_activation_sha256,
            "coordination_plan_expected_activation_invalid",
        )

        try:
            with self._transaction_factory():
                self.require_top_level_session(plan)
                current = self.load_current(
                    project_id=plan.project.project_id,
                    coordination_session_id=plan.coordination_session_id,
                )
                if current is not None and hmac.compare_digest(
                    current[1].activation_sha256,
                    activation.activation_sha256,
                ):
                    if expected != activation.supersedes_activation_sha256:
                        raise CoordinationPlanError("coordination_plan_generation_conflict")
                    _require_exact_plan_activation(
                        current,
                        plan=plan,
                        activation=activation,
                    )
                    return current

                self._require_activation_generation(
                    plan=plan,
                    activation=activation,
                    current=current,
                    expected=expected,
                )
                self._insert_plan(plan)
                self._insert_activation(activation)
                now_text = canonical_text(server_now(self._clock))
                if current is None:
                    self._connection.execute(
                        f"""
                        INSERT INTO {_HEAD_TABLE} (
                            project_id, coordination_session_id, plan_sha256,
                            activation_sha256, plan_id, plan_revision,
                            head_generation, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            plan.project.project_id,
                            plan.coordination_session_id,
                            plan.content_sha256,
                            activation.activation_sha256,
                            plan.plan_id,
                            plan.plan_revision,
                            now_text,
                        ),
                    )
                else:
                    head = self._fetchone(
                        f"""
                        SELECT head_generation FROM {_HEAD_TABLE}
                         WHERE project_id=? AND coordination_session_id=?
                           AND activation_sha256=?
                        """,
                        (
                            plan.project.project_id,
                            plan.coordination_session_id,
                            expected,
                        ),
                    )
                    if head is None:
                        raise CoordinationPlanError("coordination_plan_generation_conflict")
                    generation = _positive_sql_integer(
                        head["head_generation"],
                        "coordination_plan_durable_record_corrupt",
                    )
                    updated = self._connection.execute(
                        f"""
                        UPDATE {_HEAD_TABLE}
                           SET plan_sha256=?, activation_sha256=?, plan_id=?,
                               plan_revision=?, head_generation=?, updated_at_utc=?
                         WHERE project_id=? AND coordination_session_id=?
                           AND activation_sha256=? AND head_generation=?
                        """,
                        (
                            plan.content_sha256,
                            activation.activation_sha256,
                            plan.plan_id,
                            plan.plan_revision,
                            generation + 1,
                            now_text,
                            plan.project.project_id,
                            plan.coordination_session_id,
                            expected,
                            generation,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise CoordinationPlanError("coordination_plan_generation_conflict")
        except sqlite3.IntegrityError as exc:
            winner = self.load_current(
                project_id=plan.project.project_id,
                coordination_session_id=plan.coordination_session_id,
            )
            if winner is not None and hmac.compare_digest(
                winner[1].activation_sha256,
                activation.activation_sha256,
            ):
                _require_exact_plan_activation(
                    winner,
                    plan=plan,
                    activation=activation,
                )
                return winner
            raise CoordinationPlanError("coordination_plan_generation_conflict") from exc

        stored = self.load_current(
            project_id=plan.project.project_id,
            coordination_session_id=plan.coordination_session_id,
        )
        if stored is None:
            raise CoordinationPlanError("coordination_plan_durable_append_missing")
        _require_exact_plan_activation(stored, plan=plan, activation=activation)
        return stored

    def append_usage(
        self,
        receipt: ResourceUsageReceipt,
        *,
        _authority_token: object | None = None,
    ) -> ResourceUsageReceipt:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise CoordinationPlanError("resource_usage_repository_write_authority_required")
        if not isinstance(receipt, ResourceUsageReceipt):
            raise CoordinationPlanError("resource_usage_receipt_invalid")

        try:
            with self._transaction_factory():
                plan = self.load_plan_by_digest(receipt.plan_sha256)
                if plan is None:
                    raise CoordinationPlanError("resource_usage_plan_not_found")
                current = self.load_current(
                    project_id=plan.project.project_id,
                    coordination_session_id=plan.coordination_session_id,
                )
                if current is None or not hmac.compare_digest(
                    current[0].content_sha256,
                    plan.content_sha256,
                ):
                    raise CoordinationPlanError("resource_usage_plan_not_current")
                try:
                    plan.node(receipt.responsibility_node_id)
                except CoordinationPlanError as exc:
                    raise CoordinationPlanError("resource_usage_node_not_found") from exc
                session, _ = self._load_agent_session(
                    receipt.agent_session_id,
                    missing_code="resource_usage_agent_session_missing",
                )
                _require_session_scope(session, plan, now=server_now(self._clock))

                existing = self.load_usage_by_id(receipt.receipt_id)
                if existing is not None:
                    if existing != receipt:
                        raise CoordinationPlanError("resource_usage_receipt_conflict")
                    return existing
                by_digest = self._load_usage_by_digest(receipt.content_sha256)
                if by_digest is not None:
                    if by_digest != receipt:
                        raise CoordinationPlanError("resource_usage_receipt_conflict")
                    return by_digest
                _require_usage_within_budget(self, plan=plan, receipt=receipt)
                self._connection.execute(
                    f"""
                    INSERT INTO {_USAGE_TABLE} (
                        usage_receipt_sha256, receipt_id, plan_sha256,
                        responsibility_node_id, agent_session_id, token_usage,
                        token_measurement, measurement_evidence_sha256,
                        recorded_at_utc, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.content_sha256,
                        receipt.receipt_id,
                        receipt.plan_sha256,
                        receipt.responsibility_node_id,
                        receipt.agent_session_id,
                        receipt.token_usage,
                        receipt.token_measurement,
                        receipt.measurement_evidence_sha256,
                        receipt.recorded_at_utc,
                        receipt.canonical_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            winner = self.load_usage_by_id(receipt.receipt_id)
            if winner is not None and winner == receipt:
                return winner
            raise CoordinationPlanError("resource_usage_receipt_conflict") from exc

        stored = self.load_usage_by_id(receipt.receipt_id)
        if stored is None or stored != receipt:
            raise CoordinationPlanError("resource_usage_durable_append_missing")
        return stored

    def load_usage_by_id(self, receipt_id: str) -> ResourceUsageReceipt | None:
        normalized = _identifier(receipt_id, "resource_usage_receipt_id_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_USAGE_TABLE} WHERE receipt_id=?",
            (normalized,),
        )
        return None if row is None else _usage_from_row(row)

    def total_provider_token_usage(
        self,
        *,
        plan_sha256: str,
        responsibility_node_id: str,
    ) -> int:
        digest = _digest(plan_sha256, "resource_usage_plan_digest_invalid")
        node_id = _identifier(
            responsibility_node_id,
            "resource_usage_node_id_invalid",
        )
        plan = self.load_plan_by_digest(digest)
        if plan is None:
            raise CoordinationPlanError("resource_usage_plan_not_found")
        try:
            plan.node(node_id)
        except CoordinationPlanError as exc:
            raise CoordinationPlanError("resource_usage_node_not_found") from exc
        rows = self._fetchall(
            f"""
            SELECT * FROM {_USAGE_TABLE}
             WHERE plan_sha256=? AND responsibility_node_id=?
               AND token_measurement=?
            ORDER BY receipt_id
            """,
            (digest, node_id, TOKEN_AUTHORITY_PROVIDER),
        )
        total = 0
        for row in rows:
            receipt = _usage_from_row(row)
            if receipt.token_measurement != TOKEN_AUTHORITY_PROVIDER:
                raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
            total += int(receipt.token_usage or 0)
        return total

    def _load_usage_by_digest(self, receipt_sha256: str) -> ResourceUsageReceipt | None:
        digest = _digest(receipt_sha256, "resource_usage_receipt_digest_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_USAGE_TABLE} WHERE usage_receipt_sha256=?",
            (digest,),
        )
        return None if row is None else _usage_from_row(row)

    def _load_agent_session(
        self,
        session_id: str,
        *,
        missing_code: str,
    ) -> tuple[AgentSession, str]:
        normalized = _identifier(session_id, "resource_usage_agent_session_invalid")
        session_row = self._fetchone(
            "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
            (normalized,),
        )
        if session_row is None:
            raise CoordinationPlanError(missing_code)
        agent_row = self._fetchone(
            """
            SELECT * FROM collaboration_agents
             WHERE project_id=? AND agent_id=?
            """,
            (session_row["project_id"], session_row["agent_id"]),
        )
        if agent_row is None:
            raise CoordinationPlanError(missing_code)
        session = _session_from_rows(session_row, agent_row)
        registration_sha256 = _digest(
            session_row["session_sha256"],
            "coordination_plan_agent_session_corrupt",
        )
        return session, registration_sha256

    def _require_activation_generation(
        self,
        *,
        plan: CoordinationPlan,
        activation: CoordinationPlanActivation,
        current: tuple[CoordinationPlan, CoordinationPlanActivation] | None,
        expected: str,
    ) -> None:
        if current is None:
            if expected:
                raise CoordinationPlanError("coordination_plan_generation_conflict")
            if plan.plan_revision != 1 or activation.supersedes_activation_sha256:
                raise CoordinationPlanError("coordination_plan_initial_revision_invalid")
            existing = self._fetchone(
                f"""
                SELECT 1 FROM {_PLAN_TABLE}
                 WHERE project_id=? AND coordination_session_id=?
                LIMIT 1
                """,
                (plan.project.project_id, plan.coordination_session_id),
            )
            if existing is not None:
                raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
            return
        previous, previous_activation = current
        if not hmac.compare_digest(expected, previous_activation.activation_sha256):
            raise CoordinationPlanError("coordination_plan_generation_conflict")
        plan.validate_successor(previous)
        if not hmac.compare_digest(
            activation.supersedes_activation_sha256,
            previous_activation.activation_sha256,
        ):
            raise CoordinationPlanError("coordination_plan_activation_successor_mismatch")

    def _insert_plan(self, plan: CoordinationPlan) -> None:
        existing = self.load_plan_by_digest(plan.content_sha256)
        if existing is not None:
            if existing != plan:
                raise CoordinationPlanError("coordination_plan_repository_corrupt")
            return
        revision = self._fetchone(
            f"SELECT plan_sha256 FROM {_PLAN_TABLE} WHERE plan_id=? AND plan_revision=?",
            (plan.plan_id, plan.plan_revision),
        )
        if revision is not None:
            raise CoordinationPlanError("coordination_plan_revision_conflict")
        self._connection.execute(
            f"""
            INSERT INTO {_PLAN_TABLE} (
                plan_sha256, plan_id, plan_revision, mandate_sha256,
                project_id, coordination_session_id, top_level_agent_session_id,
                root_node_id, created_at_utc, expires_at_utc,
                supersedes_plan_sha256, plan_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.content_sha256,
                plan.plan_id,
                plan.plan_revision,
                plan.mandate.content_sha256,
                plan.project.project_id,
                plan.coordination_session_id,
                plan.top_level_agent_session_id,
                plan.root_node_id,
                plan.created_at_utc,
                plan.expires_at_utc,
                plan.supersedes_plan_sha256 or None,
                plan.canonical_json(),
            ),
        )

    def _insert_activation(self, activation: CoordinationPlanActivation) -> None:
        existing = self.load_activation_by_digest(activation.activation_sha256)
        if existing is not None:
            if existing != activation:
                raise CoordinationPlanError("coordination_plan_repository_corrupt")
            return
        by_id = self._fetchone(
            f"SELECT activation_sha256 FROM {_ACTIVATION_TABLE} WHERE activation_id=?",
            (activation.activation_id,),
        )
        if by_id is not None:
            raise CoordinationPlanError("coordination_plan_activation_id_conflict")
        by_plan = self._fetchone(
            f"SELECT activation_sha256 FROM {_ACTIVATION_TABLE} WHERE plan_sha256=?",
            (activation.plan_sha256,),
        )
        if by_plan is not None:
            raise CoordinationPlanError("coordination_plan_activation_plan_conflict")
        self._connection.execute(
            f"""
            INSERT INTO {_ACTIVATION_TABLE} (
                activation_sha256, activation_id, plan_sha256, plan_id,
                plan_revision, mandate_sha256, project_id,
                coordination_session_id, top_level_agent_session_id,
                issued_at_utc, expires_at_utc,
                supersedes_activation_sha256, activation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activation.activation_sha256,
                activation.activation_id,
                activation.plan_sha256,
                activation.plan_id,
                activation.plan_revision,
                activation.mandate_sha256,
                activation.project.project_id,
                activation.coordination_session_id,
                activation.top_level_agent_session_id,
                activation.issued_at_utc,
                activation.expires_at_utc,
                activation.supersedes_activation_sha256 or None,
                activation.canonical_json(),
            ),
        )

    def _verify_schema(self) -> None:
        enabled = self._connection.execute("PRAGMA foreign_keys").fetchone()
        if enabled is None or int(enabled[0]) != 1:
            raise CoordinationPlanError("coordination_plan_durable_foreign_keys_required")
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = set(DURABLE_COORDINATION_PLAN_REQUIRED_TABLES) | set(
            DURABLE_COORDINATION_PLAN_DEPENDENCY_COLUMNS
        )
        if not required.issubset(tables):
            raise CoordinationPlanError("coordination_plan_durable_schema_missing")
        for table, columns in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS.items():
            actual = tuple(
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if actual != columns:
                raise CoordinationPlanError("coordination_plan_durable_schema_stale")
        for table, columns in DURABLE_COORDINATION_PLAN_DEPENDENCY_COLUMNS.items():
            actual = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not set(columns).issubset(actual):
                raise CoordinationPlanError("coordination_plan_durable_schema_stale")
        for table, column_sets in _UNIQUE_COLUMN_SETS.items():
            for columns in column_sets:
                _verify_unique_index(self._connection, table, columns)
        for name, (table, columns) in _INDEX_SPECS.items():
            _verify_named_index(self._connection, name, table, columns)
        for table, expected in _FOREIGN_KEYS.items():
            _verify_foreign_keys(self._connection, table, expected)
        for name, (table, action, failure) in _TRIGGER_SPECS.items():
            _verify_trigger(self._connection, name, table, action, failure)
        for table, fragments in _TABLE_SQL_FRAGMENTS.items():
            _verify_table_sql(self._connection, table, fragments)
        marker = self._fetchone(f"SELECT * FROM {_SCHEMA_TABLE} WHERE singleton=1")
        if marker is None:
            raise CoordinationPlanError("coordination_plan_durable_schema_missing")
        if marker["schema_revision"] != DURABLE_COORDINATION_PLAN_SCHEMA_REVISION:
            raise CoordinationPlanError("coordination_plan_durable_schema_stale")
        _canonical_timestamp(
            marker["installed_at_utc"],
            "coordination_plan_durable_schema_stale",
        )

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> dict[str, Any] | None:
        return _row(self._connection.execute(sql, parameters))

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> list[dict[str, Any]]:
        return _rows(self._connection.execute(sql, parameters))


def _plan_from_row(row: Mapping[str, object]) -> CoordinationPlan:
    try:
        raw = str(row["plan_json"])
        plan = _plan_from_json(raw)
        expected = {
            "plan_sha256": plan.content_sha256,
            "plan_id": plan.plan_id,
            "plan_revision": plan.plan_revision,
            "mandate_sha256": plan.mandate.content_sha256,
            "project_id": plan.project.project_id,
            "coordination_session_id": plan.coordination_session_id,
            "top_level_agent_session_id": plan.top_level_agent_session_id,
            "root_node_id": plan.root_node_id,
            "created_at_utc": plan.created_at_utc,
            "expires_at_utc": plan.expires_at_utc,
            "supersedes_plan_sha256": plan.supersedes_plan_sha256 or None,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
        return plan
    except CoordinationPlanError as exc:
        if exc.code == "coordination_plan_durable_record_corrupt":
            raise
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc


def _activation_from_row(row: Mapping[str, object]) -> CoordinationPlanActivation:
    try:
        raw = str(row["activation_json"])
        activation = _activation_from_json(raw)
        expected = {
            "activation_sha256": activation.activation_sha256,
            "activation_id": activation.activation_id,
            "plan_sha256": activation.plan_sha256,
            "plan_id": activation.plan_id,
            "plan_revision": activation.plan_revision,
            "mandate_sha256": activation.mandate_sha256,
            "project_id": activation.project.project_id,
            "coordination_session_id": activation.coordination_session_id,
            "top_level_agent_session_id": activation.top_level_agent_session_id,
            "issued_at_utc": activation.issued_at_utc,
            "expires_at_utc": activation.expires_at_utc,
            "supersedes_activation_sha256": (activation.supersedes_activation_sha256 or None),
        }
        if any(row[key] != value for key, value in expected.items()):
            raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
        return activation
    except CoordinationPlanError as exc:
        if exc.code == "coordination_plan_durable_record_corrupt":
            raise
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc


def _top_level_binding_from_row(
    row: Mapping[str, object],
    *,
    session: AgentSession,
    registration_sha256: str,
) -> TopLevelAgentBinding:
    """Rehydrate a binding only when JSON, row, and session agree exactly."""

    try:
        raw = str(row["binding_json"])
        payload = _canonical_mapping(raw)
        _require_exact_fields(payload, _TOP_LEVEL_BINDING_FIELDS)
        if payload.get("schema_version") != DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_SCHEMA:
            raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
        binding = TopLevelAgentBinding(
            project=ProjectScope(payload.get("project_id")),  # type: ignore[arg-type]
            coordination_session_id=payload.get("coordination_session_id"),  # type: ignore[arg-type]
            top_level_agent_session_id=payload.get("top_level_agent_session_id"),  # type: ignore[arg-type]
            top_level_agent_id=payload.get("top_level_agent_id"),  # type: ignore[arg-type]
            agent_session_sha256=payload.get("agent_session_sha256"),  # type: ignore[arg-type]
            mandate_sha256=payload.get("mandate_sha256"),  # type: ignore[arg-type]
            binding_generation=payload.get("binding_generation"),  # type: ignore[arg-type]
            bound_at_utc=payload.get("bound_at_utc"),  # type: ignore[arg-type]
        )
        if binding.canonical_json() != raw:
            raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
        expected = {
            "project_id": binding.project.project_id,
            "coordination_session_id": binding.coordination_session_id,
            "top_level_agent_session_id": binding.top_level_agent_session_id,
            "top_level_agent_id": binding.top_level_agent_id,
            "agent_session_sha256": binding.agent_session_sha256,
            "mandate_sha256": binding.mandate_sha256,
            "binding_generation": binding.binding_generation,
            "bound_at_utc": binding.bound_at_utc,
            "binding_json": raw,
            "binding_sha256": binding.binding_sha256,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
        if (
            binding.project != session.project
            or binding.coordination_session_id != session.coordination_session_id
            or binding.top_level_agent_session_id != session.session_id
            or binding.top_level_agent_id != session.identity.agent_id
            or not hmac.compare_digest(binding.agent_session_sha256, registration_sha256)
        ):
            raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt")
        return binding
    except CoordinationPlanError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_top_level_binding_corrupt") from exc


def _usage_from_row(row: Mapping[str, object]) -> ResourceUsageReceipt:
    try:
        raw = str(row["receipt_json"])
        receipt = _usage_from_json(raw)
        expected = {
            "usage_receipt_sha256": receipt.content_sha256,
            "receipt_id": receipt.receipt_id,
            "plan_sha256": receipt.plan_sha256,
            "responsibility_node_id": receipt.responsibility_node_id,
            "agent_session_id": receipt.agent_session_id,
            "token_usage": receipt.token_usage,
            "token_measurement": receipt.token_measurement,
            "measurement_evidence_sha256": receipt.measurement_evidence_sha256,
            "recorded_at_utc": receipt.recorded_at_utc,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
        return receipt
    except CoordinationPlanError as exc:
        if exc.code == "coordination_plan_durable_record_corrupt":
            raise
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc


def _plan_from_json(raw: str) -> CoordinationPlan:
    payload = _canonical_mapping(raw)
    _require_exact_fields(payload, _PLAN_FIELDS)
    if payload.get("schema_version") != COORDINATION_PLAN_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    mandate = _mandate_from_mapping(_mapping(payload.get("mandate")))
    plan = CoordinationPlan(
        plan_id=payload.get("plan_id"),  # type: ignore[arg-type]
        plan_revision=payload.get("plan_revision"),  # type: ignore[arg-type]
        mandate=mandate,
        top_level_agent_session_id=payload.get("top_level_agent_session_id"),  # type: ignore[arg-type]
        root_node_id=payload.get("root_node_id"),  # type: ignore[arg-type]
        nodes=tuple(_node_from_mapping(item) for item in _mapping_list(payload.get("nodes"))),
        edges=tuple(_edge_from_mapping(item) for item in _mapping_list(payload.get("edges"))),
        total_allocation=_allocation_from_mapping(_mapping(payload.get("total_allocation"))),
        created_at_utc=payload.get("created_at_utc"),  # type: ignore[arg-type]
        expires_at_utc=payload.get("expires_at_utc"),  # type: ignore[arg-type]
        supersedes_plan_sha256=payload.get("supersedes_plan_sha256"),  # type: ignore[arg-type]
    )
    if plan.canonical_json() != raw:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return plan


def _mandate_from_mapping(payload: Mapping[str, object]) -> UserMandate:
    _require_exact_fields(payload, _MANDATE_FIELDS)
    if payload.get("schema_version") != USER_MANDATE_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    mandate = UserMandate(
        mandate_id=payload.get("mandate_id"),  # type: ignore[arg-type]
        project=ProjectScope(payload.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=payload.get("coordination_session_id"),  # type: ignore[arg-type]
        user_instruction_sha256=payload.get("user_instruction_sha256"),  # type: ignore[arg-type]
        objective=payload.get("objective"),  # type: ignore[arg-type]
        constraints=_string_tuple(payload.get("constraints")),
        issued_at_utc=payload.get("issued_at_utc"),  # type: ignore[arg-type]
        expires_at_utc=payload.get("expires_at_utc"),  # type: ignore[arg-type]
    )
    if mandate.to_dict() != dict(payload):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return mandate


def _allocation_from_mapping(payload: Mapping[str, object]) -> ResourceAllocation:
    _require_exact_fields(payload, _ALLOCATION_FIELDS)
    if payload.get("schema_version") != RESOURCE_ALLOCATION_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    allocation = ResourceAllocation(
        agent_slots=payload.get("agent_slots"),  # type: ignore[arg-type]
        token_budget=payload.get("token_budget"),  # type: ignore[arg-type]
        token_budget_authority=payload.get("token_budget_authority"),  # type: ignore[arg-type]
    )
    if allocation.to_dict() != dict(payload):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return allocation


def _node_from_mapping(payload: Mapping[str, object]) -> ResponsibilityNode:
    _require_exact_fields(payload, _NODE_FIELDS)
    if payload.get("schema_version") != RESPONSIBILITY_NODE_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    node = ResponsibilityNode(
        node_id=payload.get("node_id"),  # type: ignore[arg-type]
        work_item_id=payload.get("work_item_id"),  # type: ignore[arg-type]
        role_intent=payload.get("role_intent"),  # type: ignore[arg-type]
        scope=payload.get("scope"),  # type: ignore[arg-type]
        allowed_paths=_string_tuple(payload.get("allowed_paths")),
        allowed_tools=_string_tuple(payload.get("allowed_tools")),
        acceptance_conditions=_string_tuple(payload.get("acceptance_conditions")),
        allocation=_allocation_from_mapping(_mapping(payload.get("allocation"))),
        can_delegate=payload.get("can_delegate"),  # type: ignore[arg-type]
    )
    if node.to_dict() != dict(payload):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return node


def _edge_from_mapping(payload: Mapping[str, object]) -> DelegationEdge:
    _require_exact_fields(payload, _EDGE_FIELDS)
    if payload.get("schema_version") != DELEGATION_EDGE_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    edge = DelegationEdge(
        parent_node_id=payload.get("parent_node_id"),  # type: ignore[arg-type]
        child_node_id=payload.get("child_node_id"),  # type: ignore[arg-type]
    )
    if edge.to_dict() != dict(payload):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return edge


def _activation_from_json(raw: str) -> CoordinationPlanActivation:
    payload = _canonical_mapping(raw)
    _require_exact_fields(payload, _ACTIVATION_FIELDS)
    if (
        payload.get("schema_version") != COORDINATION_PLAN_ACTIVATION_SCHEMA
        or payload.get("issuer") != COORDINATION_PLAN_ISSUER
    ):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    activation = CoordinationPlanActivation(
        activation_id=payload.get("activation_id"),  # type: ignore[arg-type]
        plan_id=payload.get("plan_id"),  # type: ignore[arg-type]
        plan_revision=payload.get("plan_revision"),  # type: ignore[arg-type]
        plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
        mandate_sha256=payload.get("mandate_sha256"),  # type: ignore[arg-type]
        project=ProjectScope(payload.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=payload.get("coordination_session_id"),  # type: ignore[arg-type]
        top_level_agent_session_id=payload.get("top_level_agent_session_id"),  # type: ignore[arg-type]
        issued_at_utc=payload.get("issued_at_utc"),  # type: ignore[arg-type]
        expires_at_utc=payload.get("expires_at_utc"),  # type: ignore[arg-type]
        supersedes_activation_sha256=payload.get(  # type: ignore[arg-type]
            "supersedes_activation_sha256"
        ),
    )
    if activation.canonical_json() != raw:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return activation


def _usage_from_json(raw: str) -> ResourceUsageReceipt:
    payload = _canonical_mapping(raw)
    _require_exact_fields(payload, _USAGE_FIELDS)
    if payload.get("schema_version") != RESOURCE_USAGE_RECEIPT_SCHEMA:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    receipt = ResourceUsageReceipt(
        receipt_id=payload.get("receipt_id"),  # type: ignore[arg-type]
        plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
        responsibility_node_id=payload.get("responsibility_node_id"),  # type: ignore[arg-type]
        agent_session_id=payload.get("agent_session_id"),  # type: ignore[arg-type]
        token_usage=payload.get("token_usage"),  # type: ignore[arg-type]
        token_measurement=payload.get("token_measurement"),  # type: ignore[arg-type]
        measurement_evidence_sha256=payload.get(  # type: ignore[arg-type]
            "measurement_evidence_sha256"
        ),
        recorded_at_utc=payload.get("recorded_at_utc"),  # type: ignore[arg-type]
    )
    if receipt.canonical_json() != raw:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return receipt


def _session_from_rows(
    session_row: Mapping[str, object],
    agent_row: Mapping[str, object],
) -> AgentSession:
    try:
        session_raw = str(session_row["session_json"])
        session_payload = _canonical_mapping(session_raw)
        _require_exact_fields(session_payload, _SESSION_FIELDS)
        identity = _identity_from_mapping(_mapping(session_payload.get("identity")))
        snapshot = AgentSession(
            session_id=session_payload.get("session_id"),  # type: ignore[arg-type]
            identity=identity,
            project=ProjectScope(session_payload.get("project_id")),  # type: ignore[arg-type]
            coordination_session_id=session_payload.get("coordination_session_id"),  # type: ignore[arg-type]
            state=session_payload.get("state"),  # type: ignore[arg-type]
            started_at=session_payload.get("started_at"),  # type: ignore[arg-type]
            last_heartbeat_at=session_payload.get("last_heartbeat_at"),  # type: ignore[arg-type]
            expires_at=session_payload.get("expires_at"),  # type: ignore[arg-type]
        )
        if snapshot.canonical_json() != session_raw:
            raise CoordinationPlanError("coordination_plan_agent_session_corrupt")
        session_identity_raw = str(session_row["identity_json"])
        agent_identity_raw = str(agent_row["identity_json"])
        identity_json = _canonical_json(identity.to_dict())
        capabilities_json = _canonical_json(list(identity.capabilities))
        static_checks = (
            (session_row["session_id"], snapshot.session_id),
            (session_row["project_id"], snapshot.project.project_id),
            (session_row["agent_id"], snapshot.identity.agent_id),
            (session_row["coordination_session_id"], snapshot.coordination_session_id),
            (session_row["session_sha256"], snapshot.content_sha256),
            (session_row["started_at"], snapshot.started_at),
            (session_row["expires_at"], snapshot.expires_at),
            (session_identity_raw, identity_json),
            (agent_row["project_id"], snapshot.project.project_id),
            (agent_row["agent_id"], identity.agent_id),
            (agent_row["role"], identity.role),
            (agent_row["parent_agent_id"], identity.parent_agent_id),
            (agent_row["capabilities_json"], capabilities_json),
            (agent_identity_raw, identity_json),
        )
        if any(left != right for left, right in static_checks):
            raise CoordinationPlanError("coordination_plan_agent_session_corrupt")
        state = str(session_row["state"])
        if str(agent_row["state"]) not in _ACTIVE_STATES and state in _ACTIVE_STATES:
            raise CoordinationPlanError("coordination_plan_agent_session_inactive")
        return AgentSession(
            session_id=snapshot.session_id,
            identity=identity,
            project=snapshot.project,
            coordination_session_id=snapshot.coordination_session_id,
            state=state,
            started_at=session_row["started_at"],  # type: ignore[arg-type]
            last_heartbeat_at=session_row["last_heartbeat_at"],  # type: ignore[arg-type]
            expires_at=session_row["expires_at"],  # type: ignore[arg-type]
        )
    except CoordinationPlanError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_agent_session_corrupt") from exc


def _identity_from_mapping(payload: Mapping[str, object]) -> AgentIdentity:
    _require_exact_fields(payload, _IDENTITY_FIELDS)
    return AgentIdentity(
        agent_id=payload.get("agent_id"),  # type: ignore[arg-type]
        role=payload.get("role"),  # type: ignore[arg-type]
        parent_agent_id=payload.get("parent_agent_id"),  # type: ignore[arg-type]
        capabilities=_string_tuple(payload.get("capabilities")),
    )


def _require_exact_plan_activation(
    stored: tuple[CoordinationPlan, CoordinationPlanActivation],
    *,
    plan: CoordinationPlan,
    activation: CoordinationPlanActivation,
) -> None:
    if stored[0] != plan or stored[1] != activation:
        raise CoordinationPlanError("coordination_plan_repository_corrupt")


def _canonical_mapping(raw: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != raw:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return payload


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt") from exc


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return value


def _mapping_list(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return tuple(value)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")
    return tuple(value)


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
) -> None:
    if set(payload) != expected:
        raise CoordinationPlanError("coordination_plan_durable_record_corrupt")


def _canonical_timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CoordinationPlanError(code)
    try:
        rendered = canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CoordinationPlanError(code) from exc
    if rendered != value:
        raise CoordinationPlanError(code)
    return rendered


def _positive_sql_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoordinationPlanError(code)
    return value


def _non_negative_sql_integer(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoordinationPlanError(code)
    return value


def _verify_unique_index(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> None:
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(row[2]):
            continue
        actual = tuple(
            str(item[2]) for item in connection.execute(f"PRAGMA index_info({row[1]})").fetchall()
        )
        if actual == columns:
            return
    raise CoordinationPlanError("coordination_plan_durable_schema_stale")


def _verify_named_index(
    connection: sqlite3.Connection,
    name: str,
    table: str,
    columns: tuple[str, ...],
) -> None:
    row = connection.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    if row is None or str(row[0]) != table:
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")
    listed = [
        item
        for item in connection.execute(f"PRAGMA index_list({table})").fetchall()
        if str(item[1]) == name
    ]
    if len(listed) != 1 or bool(listed[0][2]):
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")
    actual = tuple(
        str(item[2]) for item in connection.execute(f"PRAGMA index_info({name})").fetchall()
    )
    if actual != columns:
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")


def _verify_foreign_keys(
    connection: sqlite3.Connection,
    table: str,
    expected: set[tuple[str, str, str]],
) -> None:
    rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    actual = {(str(row[2]), str(row[3]), str(row[4])) for row in rows}
    actions_valid = all(
        str(row[5]).upper() == "RESTRICT" and str(row[6]).upper() == "RESTRICT" for row in rows
    )
    if actual != expected or not actions_valid:
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")


def _verify_trigger(
    connection: sqlite3.Connection,
    name: str,
    table: str,
    action: str,
    failure: str,
) -> None:
    row = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    if row is None or str(row[0]) != table or not isinstance(row[1], str):
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")
    normalized = " ".join(str(row[1]).casefold().split())
    required = (action, table.casefold(), f"raise(abort, '{failure.casefold()}')")
    if any(fragment not in normalized for fragment in required):
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")


def _verify_table_sql(
    connection: sqlite3.Connection,
    table: str,
    fragments: tuple[str, ...],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")
    normalized = _compact_sql(str(row[0]))
    if any(_compact_sql(fragment) not in normalized for fragment in fragments):
        raise CoordinationPlanError("coordination_plan_durable_schema_stale")


def _compact_sql(value: str) -> str:
    return re.sub(r"[\s\"`\[\]]+", "", value.casefold())


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    raw = cursor.fetchone()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    return [dict(zip(columns, tuple(raw), strict=True)) for raw in cursor.fetchall()]
