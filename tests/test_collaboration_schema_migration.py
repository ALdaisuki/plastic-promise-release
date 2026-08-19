"""Focused evidence for the deployment-owned PR5 schema installation seam."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import AgentIdentity, AgentSession, ProjectScope
from plastic_promise.collaboration.coordination_plan import (
    TopLevelAgentBinding,
    UserMandate,
)
from plastic_promise.collaboration.durable_acceptance_store import (
    DURABLE_ACCEPTANCE_SCHEMA_REVISION,
    DurableAcceptanceAuthorityRepository,
)
from plastic_promise.collaboration.durable_activity_store import DurableActivityRepository
from plastic_promise.collaboration.durable_coordination_plan_store import (
    DURABLE_COORDINATION_PLAN_INDEX_DDL,
    DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS,
    DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,
    DURABLE_COORDINATION_PLAN_TRIGGER_DDL,
    DurableCoordinationPlanRepository,
)
from plastic_promise.collaboration.durable_coordinator_store import (
    DURABLE_COORDINATOR_SCHEMA_REVISION,
    DurableCoordinatorRepository,
)
from plastic_promise.collaboration.durable_runtime import (
    DURABLE_COLLABORATION_REVISION,
    DurableCollaborationRuntime,
)
from plastic_promise.deployment.collaboration_schema_migration import (
    COLLABORATION_SCHEMA_MANIFEST,
    COLLABORATION_SCHEMA_MANIFEST_SHA256,
    CollaborationSchemaMigration,
    CollaborationSchemaMigrationError,
    bind_canonical_backup_receipt,
    collaboration_schema_present,
)
from plastic_promise.deployment.migration_operations import OPERATION_PHASE_MANIFEST

NOW = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _manifest_digest(manifest: tuple[str, ...]) -> str:
    payload = json.dumps(
        list(manifest),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class _Plan:
    installation_ref: str = "installation-one"
    operation_ref: str = "migration-one"
    plan_hash: str = _digest("plan-one")
    phase_manifest: tuple[str, ...] = OPERATION_PHASE_MANIFEST
    phase_manifest_sha256: str = _manifest_digest(OPERATION_PHASE_MANIFEST)
    schema_manifest: tuple[str, ...] = COLLABORATION_SCHEMA_MANIFEST
    schema_manifest_sha256: str = COLLABORATION_SCHEMA_MANIFEST_SHA256


@dataclass(frozen=True)
class _Lease:
    operation_id: str = "migration-operation:one"
    plan_hash: str = _digest("plan-one")
    grant_id: str = "grant-one"
    fencing_generation: int = 7
    phase_manifest_sha256: str = _manifest_digest(OPERATION_PHASE_MANIFEST)
    schema_manifest_sha256: str = COLLABORATION_SCHEMA_MANIFEST_SHA256


@dataclass(frozen=True)
class _Context:
    plan: _Plan = _Plan()
    lease: _Lease = _Lease()


class _TransactionFactory:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.calls = 0

    @contextmanager
    def __call__(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()


def _migration(
    connection: sqlite3.Connection,
) -> tuple[CollaborationSchemaMigration, _TransactionFactory]:
    connection.execute("PRAGMA foreign_keys = ON")
    transactions = _TransactionFactory(connection)
    return (
        CollaborationSchemaMigration(
            connection,
            transaction_factory=transactions,
            clock=lambda: NOW,
        ),
        transactions,
    )


def _backup(context: _Context):  # type: ignore[no-untyped-def]
    return bind_canonical_backup_receipt(
        context,
        backup_receipt_sha256=_digest("canonical-backup"),
        completed_at=NOW,
    )


def _successor_context(context: _Context) -> _Context:
    """Return a distinct migration lease for an upgrade after initial install."""

    plan_hash = _digest("plan-two")
    return replace(
        context,
        plan=replace(
            context.plan,
            installation_ref="installation-two",
            operation_ref="migration-two",
            plan_hash=plan_hash,
        ),
        lease=replace(
            context.lease,
            operation_id="migration-operation:two",
            plan_hash=plan_hash,
            grant_id="grant-two",
            fencing_generation=8,
        ),
    )


def _insert_top_level_binding_fixture(
    connection: sqlite3.Connection,
) -> tuple[AgentSession, TopLevelAgentBinding]:
    """Create one canonical v2 binding before downgrading its table to v1."""

    project = ProjectScope("project:coordination-migration")
    identity = AgentIdentity(agent_id="agent:root", role="participant")
    session = AgentSession(
        session_id="agent-session:root",
        identity=identity,
        project=project,
        coordination_session_id="coord:migration",
        state="active",
        started_at=(NOW.replace(microsecond=0) - timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        last_heartbeat_at=NOW.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        expires_at=(NOW.replace(microsecond=0) + timedelta(hours=2))
        .isoformat()
        .replace("+00:00", "Z"),
    )
    identity_json = identity.canonical_json()
    connection.execute(
        """
        INSERT INTO collaboration_agents (
            project_id, agent_id, role, parent_agent_id, capabilities_json,
            identity_json, state, first_seen_at, last_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            project.project_id,
            identity.agent_id,
            identity.role,
            identity.parent_agent_id,
            "[]",
            identity_json,
            session.started_at,
            session.last_heartbeat_at,
            session.last_heartbeat_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO collaboration_agent_sessions (
            session_id, project_id, agent_id, coordination_session_id,
            identity_json, session_json, session_sha256,
            started_at, last_heartbeat_at, expires_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session.session_id,
            project.project_id,
            identity.agent_id,
            session.coordination_session_id,
            identity_json,
            session.canonical_json(),
            session.content_sha256,
            session.started_at,
            session.last_heartbeat_at,
            session.expires_at,
            session.last_heartbeat_at,
        ),
    )
    mandate = UserMandate(
        mandate_id="mandate:migration",
        project=project,
        coordination_session_id=session.coordination_session_id,
        user_instruction_sha256=_digest("user-mandate-migration"),
        objective="Exercise a recoverable coordination-plan schema upgrade",
        constraints=("Top-Level Agent stays bound to the user mandate",),
        issued_at_utc=session.last_heartbeat_at,
        expires_at_utc=session.expires_at or "",
    )
    binding = TopLevelAgentBinding(
        project=project,
        coordination_session_id=session.coordination_session_id,
        top_level_agent_session_id=session.session_id,
        top_level_agent_id=identity.agent_id,
        agent_session_sha256=session.content_sha256,
        mandate_sha256=mandate.content_sha256,
        binding_generation=1,
        bound_at_utc=session.last_heartbeat_at,
    )
    connection.execute(
        """
        INSERT INTO collaboration_coordination_plan_top_level_bindings (
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
    connection.commit()
    return session, binding


def _downgrade_top_level_binding_table_to_v1(
    connection: sqlite3.Connection,
    *,
    corrupt_binding_sha256: bool = False,
) -> tuple[str, str]:
    """Replace the v2 binding table with the exact v1 projection for testing."""

    row = connection.execute(
        """
        SELECT project_id, coordination_session_id,
               top_level_agent_session_id, top_level_agent_id,
               agent_session_sha256, mandate_sha256,
               binding_generation, bound_at_utc,
               binding_json, binding_sha256
          FROM collaboration_coordination_plan_top_level_bindings
        """
    ).fetchone()
    assert row is not None
    binding_json = str(row[8])
    binding_sha256 = "sha256:" + "f" * 64 if corrupt_binding_sha256 else str(row[9])
    connection.execute("DROP TABLE collaboration_coordination_plan_top_level_bindings")
    connection.execute(
        """
        CREATE TABLE collaboration_coordination_plan_top_level_bindings (
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            top_level_agent_session_id TEXT NOT NULL,
            top_level_agent_id TEXT NOT NULL,
            agent_session_sha256 TEXT NOT NULL,
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
        """
    )
    connection.execute(DURABLE_COORDINATION_PLAN_INDEX_DDL[0])
    for statement in DURABLE_COORDINATION_PLAN_TRIGGER_DDL[:2]:
        connection.execute(statement)
    connection.execute(
        """
        INSERT INTO collaboration_coordination_plan_top_level_bindings (
            project_id, coordination_session_id,
            top_level_agent_session_id, top_level_agent_id,
            agent_session_sha256, binding_generation, bound_at_utc,
            binding_json, binding_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[6],
            row[7],
            binding_json,
            binding_sha256,
        ),
    )
    connection.execute(
        """
        UPDATE collaboration_coordination_plan_schema
           SET schema_revision=?
         WHERE singleton=1
        """,
        ("collaboration-coordination-plan/sqlite-v1",),
    )
    connection.commit()
    return binding_json, binding_sha256


def _insert_runtime_session(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    project_id: str = "project:lease-migration",
    coordination_session_id: str = "coord:lease-migration",
    agent_id: str = "agent:worker",
) -> None:
    """Insert the canonical session row used by the lease backfill query."""

    connection.execute(
        """
        INSERT OR IGNORE INTO collaboration_agents (
            project_id, agent_id, role, identity_json,
            first_seen_at, last_seen_at, updated_at
        ) VALUES (?, ?, 'participant', '{}', ?, ?, ?)
        """,
        (
            project_id,
            agent_id,
            NOW.isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
        ),
    )
    connection.execute(
        """
        INSERT INTO collaboration_agent_sessions (
            session_id, project_id, agent_id, coordination_session_id,
            identity_json, session_json, session_sha256,
            started_at, last_heartbeat_at, expires_at, updated_at
        ) VALUES (?, ?, ?, ?, '{}', '{}', ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            project_id,
            agent_id,
            coordination_session_id,
            _digest(session_id),
            NOW.isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
            (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
        ),
    )


def _downgrade_runtime_table_to_v1(
    connection: sqlite3.Connection,
    *,
    project_id: str = "project:lease-migration",
    coordination_session_id: str = "coord:lease-migration",
    owner_id: str = "agent:worker",
) -> None:
    """Project the installed runtime lease table back to its exact v1 shape."""

    connection.execute("DROP INDEX IF EXISTS idx_collaboration_leases_owner_session")
    connection.execute("DROP TABLE collaboration_work_leases")
    connection.execute(
        """
        CREATE TABLE collaboration_work_leases (
            lease_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            owner_kind TEXT NOT NULL,
            policy_kind TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            lease_json TEXT NOT NULL,
            lease_sha256 TEXT NOT NULL,
            fencing_generation INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            heartbeat_sequence INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            released_at TEXT NOT NULL DEFAULT '',
            release_reason TEXT NOT NULL DEFAULT '',
            UNIQUE(work_item_id, fencing_generation)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_collaboration_leases_active
            ON collaboration_work_leases(project_id, state, expires_at)
        """
    )
    connection.execute(
        """
        INSERT INTO collaboration_work_leases (
            lease_id, work_item_id, project_id, coordination_session_id,
            owner_kind, policy_kind, owner_id, lease_json, lease_sha256,
            fencing_generation, attempt, issued_at, expires_at,
            heartbeat_sequence, last_heartbeat_at, state, released_at,
            release_reason
        ) VALUES (?, ?, ?, ?, 'agent', 'default', ?, '{}', ?, 1, 1, ?, ?, 0, ?, 'active', '', '')
        """,
        (
            "lease:migration",
            "work:migration",
            project_id,
            coordination_session_id,
            owner_id,
            _digest("lease:migration"),
            NOW.isoformat().replace("+00:00", "Z"),
            (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            NOW.isoformat().replace("+00:00", "Z"),
        ),
    )
    connection.execute(
        "UPDATE collaboration_runtime_schema SET schema_revision=? WHERE singleton=1",
        ("pr5-durable-lifecycle-v1",),
    )
    connection.commit()


def _assert_runtime_v1_untouched(connection: sqlite3.Connection) -> None:
    assert tuple(
        str(row[1]) for row in connection.execute("PRAGMA table_info(collaboration_work_leases)")
    ) == (
        "lease_id",
        "work_item_id",
        "project_id",
        "coordination_session_id",
        "owner_kind",
        "policy_kind",
        "owner_id",
        "lease_json",
        "lease_sha256",
        "fencing_generation",
        "attempt",
        "issued_at",
        "expires_at",
        "heartbeat_sequence",
        "last_heartbeat_at",
        "state",
        "released_at",
        "release_reason",
    )
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_runtime_schema WHERE singleton=1"
    ).fetchone() == ("pr5-durable-lifecycle-v1",)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_collaboration_leases_owner_session'"
        ).fetchone()
        is None
    )


def test_install_upgrades_v1_lease_owner_to_unique_session_and_runtime_is_constructible(
    tmp_path,
):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    initial_context = _Context()
    migration.install(initial_context, _backup(initial_context))
    _insert_runtime_session(connection, session_id="agent-session:unique-owner")
    _downgrade_runtime_table_to_v1(connection)
    upgrade_context = _successor_context(initial_context)

    upgrade_receipt = migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    assert upgrade_receipt.operation_id == upgrade_context.lease.operation_id
    assert connection.execute(
        "SELECT owner_session_id FROM collaboration_work_leases WHERE lease_id=?",
        ("lease:migration",),
    ).fetchone() == ("agent-session:unique-owner",)
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_runtime_schema WHERE singleton=1"
    ).fetchone() == (DURABLE_COLLABORATION_REVISION,)
    assert tuple(
        str(row[2])
        for row in connection.execute("PRAGMA index_info(idx_collaboration_leases_owner_session)")
    ) == (
        "project_id",
        "coordination_session_id",
        "owner_id",
        "owner_session_id",
        "state",
    )
    assert migration.verify(operation_id=upgrade_context.lease.operation_id) == upgrade_receipt
    assert isinstance(
        DurableCollaborationRuntime(
            connection,
            transaction_factory=transactions,
            clock=lambda: NOW,
        ),
        DurableCollaborationRuntime,
    )
    connection.close()


@pytest.mark.parametrize("candidate_count", [0, 2], ids=["zero-candidates", "multiple-candidates"])
def test_v1_lease_owner_upgrade_fails_closed_without_exactly_one_session(
    tmp_path,
    candidate_count: int,
):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    initial_context = _Context()
    initial_receipt = migration.install(initial_context, _backup(initial_context))
    for candidate_number in range(candidate_count):
        _insert_runtime_session(
            connection,
            session_id=f"agent-session:candidate-{candidate_number}",
        )
    _downgrade_runtime_table_to_v1(connection)
    upgrade_context = _successor_context(initial_context)

    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="collaboration_runtime_upgrade_lease_owner_ambiguous",
    ):
        migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    _assert_runtime_v1_untouched(connection)
    assert connection.execute(
        "SELECT lease_id, owner_id FROM collaboration_work_leases"
    ).fetchone() == ("lease:migration", "agent:worker")
    assert connection.execute(
        "SELECT operation_id FROM collaboration_schema_install_receipts ORDER BY operation_id"
    ).fetchall() == [(initial_receipt.operation_id,)]
    assert (
        connection.execute(
            "SELECT 1 FROM collaboration_schema_install_receipts WHERE operation_id=?",
            (upgrade_context.lease.operation_id,),
        ).fetchone()
        is None
    )
    connection.close()


def test_v1_lease_owner_upgrade_rolls_back_after_later_schema_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    initial_context = _Context()
    initial_receipt = migration.install(initial_context, _backup(initial_context))
    _insert_runtime_session(connection, session_id="agent-session:rollback-owner")
    _downgrade_runtime_table_to_v1(connection)
    upgrade_context = _successor_context(initial_context)

    import plastic_promise.deployment.collaboration_schema_migration as migration_module

    monkeypatch.setattr(
        migration_module,
        "_OWN_DDL",
        ("CREATE TABLE this is deliberately invalid SQL",),
    )
    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="collaboration_schema_install_failed",
    ):
        migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    _assert_runtime_v1_untouched(connection)
    assert connection.execute(
        "SELECT lease_id, owner_id FROM collaboration_work_leases"
    ).fetchone() == ("lease:migration", "agent:worker")
    assert connection.execute(
        "SELECT operation_id FROM collaboration_schema_install_receipts"
    ).fetchall() == [(initial_receipt.operation_id,)]
    connection.close()


def test_install_is_one_transaction_and_restart_verifies_exact_receipt(tmp_path):
    database_path = tmp_path / "canonical.db"
    connection = sqlite3.connect(database_path)
    migration, transactions = _migration(connection)
    context = _Context()

    receipt = migration.install(context, _backup(context))
    replay = migration.install(context, _backup(context))

    assert transactions.calls == 1
    assert replay == receipt
    assert receipt.operation_id == context.lease.operation_id
    assert receipt.schema_manifest_sha256 == COLLABORATION_SCHEMA_MANIFEST_SHA256
    assert collaboration_schema_present(connection)
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_role_assignment_schema WHERE singleton=1"
    ).fetchone() == ("collaboration-role-assignment/sqlite-v1",)
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_acceptance_schema WHERE singleton=1"
    ).fetchone() == (DURABLE_ACCEPTANCE_SCHEMA_REVISION,)
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_coordinator_audit_schema WHERE singleton=1"
    ).fetchone() == (DURABLE_COORDINATOR_SCHEMA_REVISION,)
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_coordination_plan_schema WHERE singleton=1"
    ).fetchone() == (DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,)
    activity_repository = DurableActivityRepository(
        connection,
        transaction_factory=transactions,
    )
    assert isinstance(
        DurableAcceptanceAuthorityRepository(
            connection,
            transaction_factory=transactions,
        ),
        DurableAcceptanceAuthorityRepository,
    )
    assert isinstance(
        DurableCoordinatorRepository(
            connection,
            activity_repository=activity_repository,
            transaction_factory=transactions,
        ),
        DurableCoordinatorRepository,
    )
    assert isinstance(
        DurableCoordinationPlanRepository(
            connection,
            transaction_factory=transactions,
            clock=lambda: NOW,
        ),
        DurableCoordinationPlanRepository,
    )
    connection.close()

    restarted = sqlite3.connect(database_path)
    verifier, restarted_transactions = _migration(restarted)
    assert verifier.verify(operation_id=context.lease.operation_id) == receipt
    assert restarted_transactions.calls == 0


def test_install_upgrades_v1_top_level_binding_without_reissuing_it(tmp_path):
    database_path = tmp_path / "canonical.db"
    connection = sqlite3.connect(database_path)
    migration, transactions = _migration(connection)
    initial_context = _Context()
    migration.install(initial_context, _backup(initial_context))
    session, binding = _insert_top_level_binding_fixture(connection)
    binding_json, binding_sha256 = _downgrade_top_level_binding_table_to_v1(connection)
    v1_columns = tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(collaboration_coordination_plan_top_level_bindings)"
        )
    )
    assert v1_columns == tuple(
        column
        for column in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS[
            "collaboration_coordination_plan_top_level_bindings"
        ]
        if column != "mandate_sha256"
    )

    upgrade_context = _successor_context(initial_context)
    upgrade_receipt = migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    assert upgrade_receipt.operation_id == upgrade_context.lease.operation_id
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_coordination_plan_schema WHERE singleton=1"
    ).fetchone() == (DURABLE_COORDINATION_PLAN_SCHEMA_REVISION,)
    v2_columns = tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(collaboration_coordination_plan_top_level_bindings)"
        )
    )
    assert (
        v2_columns
        == DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS[
            "collaboration_coordination_plan_top_level_bindings"
        ]
    )
    row = connection.execute(
        """
        SELECT mandate_sha256, binding_json, binding_sha256
          FROM collaboration_coordination_plan_top_level_bindings
        """
    ).fetchone()
    assert row == (binding.mandate_sha256, binding_json, binding_sha256)
    repository = DurableCoordinationPlanRepository(
        connection,
        transaction_factory=transactions,
        clock=lambda: NOW,
    )
    assert (
        repository.load_top_level_binding(
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
        )
        == binding
    )
    connection.close()

    restarted = sqlite3.connect(database_path)
    verifier, restarted_transactions = _migration(restarted)
    assert verifier.verify(operation_id=upgrade_context.lease.operation_id) == upgrade_receipt
    restarted_repository = DurableCoordinationPlanRepository(
        restarted,
        transaction_factory=restarted_transactions,
        clock=lambda: NOW,
    )
    assert (
        restarted_repository.load_top_level_binding(
            project_id=session.project.project_id,
            coordination_session_id=session.coordination_session_id,
        )
        == binding
    )


def test_v1_to_v2_upgrade_rolls_back_after_binding_table_rebuild_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """SQLite must restore the exact v1 table if rebuild fails after rename."""

    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    initial_context = _Context()
    migration.install(initial_context, _backup(initial_context))
    _insert_top_level_binding_fixture(connection)
    binding_json, binding_sha256 = _downgrade_top_level_binding_table_to_v1(connection)
    upgrade_context = _successor_context(initial_context)

    import plastic_promise.deployment.collaboration_schema_migration as migration_module

    monkeypatch.setattr(
        migration_module,
        "DURABLE_COORDINATION_PLAN_TOP_LEVEL_BINDING_DDL",
        "CREATE TABLE this is deliberately invalid SQL",
    )
    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="collaboration_schema_install_failed",
    ):
        migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_coordination_plan_schema WHERE singleton=1"
    ).fetchone() == ("collaboration-coordination-plan/sqlite-v1",)
    assert tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(collaboration_coordination_plan_top_level_bindings)"
        )
    ) == tuple(
        column
        for column in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS[
            "collaboration_coordination_plan_top_level_bindings"
        ]
        if column != "mandate_sha256"
    )
    assert connection.execute(
        "SELECT binding_json, binding_sha256 "
        "FROM collaboration_coordination_plan_top_level_bindings"
    ).fetchone() == (binding_json, binding_sha256)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "collaboration_coordination_plan_top_level_bindings_v1" not in tables
    assert (
        connection.execute(
            "SELECT 1 FROM collaboration_schema_install_receipts WHERE operation_id=?",
            (upgrade_context.lease.operation_id,),
        ).fetchone()
        is None
    )


def test_v1_to_v2_upgrade_rejects_tampered_binding_without_advancing_marker(tmp_path):
    """A copied v1 row cannot acquire missing mandate lineage during upgrade."""

    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    initial_context = _Context()
    migration.install(initial_context, _backup(initial_context))
    _insert_top_level_binding_fixture(connection)
    binding_json, binding_sha256 = _downgrade_top_level_binding_table_to_v1(
        connection,
        corrupt_binding_sha256=True,
    )
    upgrade_context = _successor_context(initial_context)

    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="collaboration_coordination_plan_upgrade_binding_invalid",
    ):
        migration.install(upgrade_context, _backup(upgrade_context))

    assert transactions.calls == 2
    assert connection.execute(
        "SELECT schema_revision FROM collaboration_coordination_plan_schema WHERE singleton=1"
    ).fetchone() == ("collaboration-coordination-plan/sqlite-v1",)
    assert tuple(
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(collaboration_coordination_plan_top_level_bindings)"
        )
    ) == tuple(
        column
        for column in DURABLE_COORDINATION_PLAN_REQUIRED_COLUMNS[
            "collaboration_coordination_plan_top_level_bindings"
        ]
        if column != "mandate_sha256"
    )
    assert connection.execute(
        "SELECT binding_json, binding_sha256 "
        "FROM collaboration_coordination_plan_top_level_bindings"
    ).fetchone() == (binding_json, binding_sha256)
    assert (
        connection.execute(
            "SELECT 1 FROM collaboration_schema_install_receipts WHERE operation_id=?",
            (upgrade_context.lease.operation_id,),
        ).fetchone()
        is None
    )


def test_install_rejects_missing_or_foreign_backup_receipt(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    context = _Context()

    with pytest.raises(
        CollaborationSchemaMigrationError, match="canonical_backup_receipt_required"
    ):
        migration.install(context, None)  # type: ignore[arg-type]
    foreign_context = replace(
        context,
        lease=replace(context.lease, grant_id="grant-foreign"),
    )
    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="canonical_backup_receipt_binding_mismatch",
    ):
        migration.install(context, _backup(foreign_context))
    assert transactions.calls == 0


@pytest.mark.parametrize(
    "foreign_context",
    [
        replace(_Context(), lease=replace(_Lease(), grant_id="grant-foreign")),
        replace(_Context(), lease=replace(_Lease(), operation_id="migration-operation:foreign")),
        replace(_Context(), lease=replace(_Lease(), fencing_generation=8)),
        replace(
            _Context(),
            plan=replace(_Plan(), plan_hash=_digest("foreign-plan")),
            lease=replace(_Lease(), plan_hash=_digest("foreign-plan")),
        ),
    ],
)
def test_backup_receipt_is_bound_to_grant_lease_fence_and_plan(
    tmp_path,
    foreign_context: _Context,
):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    context = _Context()

    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="canonical_backup_receipt_binding_mismatch",
    ):
        migration.install(foreign_context, _backup(context))
    assert transactions.calls == 0


def test_phase_or_schema_manifest_mismatch_fails_before_transaction(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, transactions = _migration(connection)
    context = _Context()
    backup = _backup(context)

    foreign_phases = ("stage-verify-edge-compute",)
    wrong_phase = replace(
        context,
        plan=replace(
            context.plan,
            phase_manifest=foreign_phases,
            phase_manifest_sha256=_manifest_digest(foreign_phases),
        ),
        lease=replace(
            context.lease,
            phase_manifest_sha256=_manifest_digest(foreign_phases),
        ),
    )
    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="canonical_backup_receipt_binding_mismatch",
    ):
        migration.install(wrong_phase, backup)

    wrong_schema = replace(
        context,
        plan=replace(
            context.plan,
            schema_manifest=("schema:foreign",),
            schema_manifest_sha256=_manifest_digest(("schema:foreign",)),
        ),
        lease=replace(
            context.lease,
            schema_manifest_sha256=_manifest_digest(("schema:foreign",)),
        ),
    )
    with pytest.raises(CollaborationSchemaMigrationError, match="schema_manifest_mismatch"):
        migration.install(wrong_schema, backup)
    assert transactions.calls == 0


@pytest.mark.parametrize(
    ("kind", "name", "code"),
    [
        (
            "index",
            "idx_collaboration_agent_activity_scope_cursor",
            "collaboration_schema_index_missing",
        ),
        (
            "trigger",
            "collaboration_acceptance_receipts_no_delete",
            "collaboration_schema_trigger_missing",
        ),
        (
            "trigger",
            "collaboration_acceptance_review_bindings_no_delete",
            "collaboration_schema_trigger_missing",
        ),
        (
            "index",
            "idx_collaboration_coordinator_audits_scope_generation",
            "collaboration_schema_index_missing",
        ),
        (
            "trigger",
            "collaboration_coordinator_audit_heads_generation_step",
            "collaboration_schema_trigger_missing",
        ),
        (
            "index",
            "idx_collaboration_coordination_plans_scope_revision",
            "collaboration_schema_index_missing",
        ),
        (
            "trigger",
            "collaboration_coordination_plans_no_update",
            "collaboration_schema_trigger_missing",
        ),
    ],
)
def test_verify_fails_closed_when_required_index_or_trigger_is_missing(
    tmp_path,
    kind: str,
    name: str,
    code: str,
):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, _transactions = _migration(connection)
    context = _Context()
    migration.install(context, _backup(context))
    connection.execute(f"DROP {kind.upper()} {name}")

    with pytest.raises(CollaborationSchemaMigrationError, match=code):
        migration.verify(operation_id=context.lease.operation_id)


def test_verify_fails_closed_when_coordinator_marker_is_stale(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    migration, _transactions = _migration(connection)
    context = _Context()
    migration.install(context, _backup(context))
    connection.execute(
        "UPDATE collaboration_coordinator_audit_schema SET schema_revision=? WHERE singleton=1",
        ("collaboration-coordinator-audit/sqlite-stale",),
    )
    connection.commit()

    with pytest.raises(
        CollaborationSchemaMigrationError,
        match="collaboration_schema_marker_stale",
    ):
        migration.verify(operation_id=context.lease.operation_id)


def test_failed_full_verification_rolls_back_every_new_schema_object(tmp_path):
    connection = sqlite3.connect(tmp_path / "canonical.db")
    connection.execute(
        "CREATE TABLE collaboration_agent_activity(activity_update_sha256 TEXT PRIMARY KEY)"
    )
    connection.commit()
    migration, transactions = _migration(connection)
    context = _Context()

    with pytest.raises(
        CollaborationSchemaMigrationError, match="collaboration_schema_install_failed"
    ):
        migration.install(context, _backup(context))

    assert transactions.calls == 1
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "collaboration_agent_activity" in tables
    assert "collaboration_runtime_schema" not in tables
    assert "collaboration_schema_install_receipts" not in tables
