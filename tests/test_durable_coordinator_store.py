"""Focused persistence tests for the durable coordinator audit ledger."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from plastic_promise.collaboration.activity_update import (
    ActivityScope,
    ActivitySlice,
    AgentActivityUpdate,
    InMemoryActivityAuditRepository,
    open_server_activity_audit_authority,
)
from plastic_promise.collaboration.contracts import ProjectScope
from plastic_promise.collaboration.coordinator_supervisor import (
    _RECEIPT_ISSUE_TOKEN,
    _SERVER_COORDINATOR_AUTHORITY_TOKEN,
    CoordinatorActivityAuditReceipt,
    CoordinatorAuditError,
    CoordinatorAuditRecord,
    CoordinatorDispatchError,
    CoordinatorSupervisor,
    EvidenceObservation,
    _coordinator_receipt_id_for,
    open_server_coordinator_audit_authority,
)
from plastic_promise.collaboration.durable_activity_store import (
    DURABLE_ACTIVITY_SCHEMA_REVISION,
    DurableActivityRepository,
)
from plastic_promise.collaboration.durable_coordinator_store import (
    DURABLE_COORDINATOR_REQUIRED_INDEXES,
    DURABLE_COORDINATOR_REQUIRED_TABLES,
    DURABLE_COORDINATOR_REQUIRED_TRIGGERS,
    DURABLE_COORDINATOR_SCHEMA_REVISION,
    DurableCoordinatorRepository,
)

NOW = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:durable-coordinator")
COORDINATION_SESSION_ID = "coord:durable-coordinator"
ROLE_ASSIGNMENT_SHA256 = "sha256:" + "a" * 64

_ACTIVITY_SCHEMA = (
    """
    CREATE TABLE collaboration_activity_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE collaboration_agent_activity (
        activity_update_sha256 TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        agent_session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL DEFAULT '',
        role_assignment_sha256 TEXT NOT NULL DEFAULT '',
        cursor INTEGER NOT NULL,
        update_json TEXT NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        UNIQUE(project_id, coordination_session_id, agent_session_id, work_item_id, cursor),
        CHECK(cursor >= 0)
    )
    """,
    """
    CREATE INDEX idx_collaboration_agent_activity_scope_cursor
        ON collaboration_agent_activity(
            project_id, coordination_session_id, agent_session_id, work_item_id, cursor
        )
    """,
    """
    CREATE TABLE collaboration_activity_audits (
        audit_receipt_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        activity_update_sha256 TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        agent_session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL DEFAULT '',
        cursor INTEGER NOT NULL,
        validated_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        FOREIGN KEY(activity_update_sha256)
            REFERENCES collaboration_agent_activity(activity_update_sha256) ON DELETE RESTRICT,
        CHECK(cursor >= 0)
    )
    """,
    """
    CREATE INDEX idx_collaboration_activity_audits_scope_cursor
        ON collaboration_activity_audits(
            project_id, coordination_session_id, agent_session_id, work_item_id, cursor
        )
    """,
    """
    CREATE TRIGGER collaboration_agent_activity_no_update
    BEFORE UPDATE ON collaboration_agent_activity
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_agent_activity_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_agent_activity_no_delete
    BEFORE DELETE ON collaboration_agent_activity
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_agent_activity_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_activity_audits_no_update
    BEFORE UPDATE ON collaboration_activity_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_activity_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_activity_audits_no_delete
    BEFORE DELETE ON collaboration_activity_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_activity_audit_append_only');
    END
    """,
)

_COORDINATOR_SCHEMA = (
    """
    CREATE TABLE collaboration_coordinator_audit_schema (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_revision TEXT NOT NULL,
        installed_at_utc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE collaboration_coordinator_audits (
        coordinator_audit_receipt_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        authority_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        activity_update_sha256 TEXT NOT NULL,
        activity_receipt_sha256 TEXT NOT NULL,
        audit_generation INTEGER NOT NULL,
        status TEXT NOT NULL,
        completion_verified INTEGER NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        FOREIGN KEY(activity_receipt_sha256)
            REFERENCES collaboration_activity_audits(audit_receipt_sha256) ON DELETE RESTRICT,
        UNIQUE(project_id, coordination_session_id, activity_update_sha256, audit_generation),
        CHECK(audit_generation >= 1),
        CHECK(status IN ('verified', 'mismatch', 'overlap', 'stale', 'blocked')),
        CHECK(completion_verified IN (0, 1))
    )
    """,
    """
    CREATE INDEX idx_collaboration_coordinator_audits_scope_generation
        ON collaboration_coordinator_audits(
            project_id, coordination_session_id, activity_update_sha256, audit_generation
        )
    """,
    """
    CREATE TABLE collaboration_coordinator_audit_heads (
        project_id TEXT NOT NULL,
        coordination_session_id TEXT NOT NULL,
        activity_update_sha256 TEXT NOT NULL,
        current_generation INTEGER NOT NULL,
        current_receipt_id TEXT NOT NULL UNIQUE,
        current_receipt_sha256 TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY(project_id, coordination_session_id, activity_update_sha256),
        FOREIGN KEY(current_receipt_id)
            REFERENCES collaboration_coordinator_audits(receipt_id) ON DELETE RESTRICT,
        CHECK(current_generation >= 1)
    )
    """,
    """
    CREATE TABLE collaboration_coordinator_audit_consumptions (
        activity_update_sha256 TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE,
        receipt_sha256 TEXT NOT NULL,
        audit_generation INTEGER NOT NULL,
        consumed_at_utc TEXT NOT NULL,
        FOREIGN KEY(receipt_id)
            REFERENCES collaboration_coordinator_audits(receipt_id) ON DELETE RESTRICT,
        CHECK(audit_generation >= 1)
    )
    """,
    """
    CREATE INDEX idx_collaboration_coordinator_consumptions_receipt
        ON collaboration_coordinator_audit_consumptions(receipt_id)
    """,
    """
    CREATE TRIGGER collaboration_coordinator_audits_no_update
    BEFORE UPDATE ON collaboration_coordinator_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_audits_no_delete
    BEFORE DELETE ON collaboration_coordinator_audits
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_audit_heads_no_delete
    BEFORE DELETE ON collaboration_coordinator_audit_heads
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_no_delete');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_audit_heads_identity_immutable
    BEFORE UPDATE ON collaboration_coordinator_audit_heads
    WHEN OLD.project_id IS NOT NEW.project_id
      OR OLD.coordination_session_id IS NOT NEW.coordination_session_id
      OR OLD.activity_update_sha256 IS NOT NEW.activity_update_sha256
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_identity_immutable');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_audit_heads_generation_step
    BEFORE UPDATE ON collaboration_coordinator_audit_heads
    WHEN NEW.current_generation != OLD.current_generation + 1
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_audit_head_generation_invalid');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_consumptions_no_update
    BEFORE UPDATE ON collaboration_coordinator_audit_consumptions
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_consumption_append_only');
    END
    """,
    """
    CREATE TRIGGER collaboration_coordinator_consumptions_no_delete
    BEFORE DELETE ON collaboration_coordinator_audit_consumptions
    BEGIN
        SELECT RAISE(ABORT, 'collaboration_coordinator_consumption_append_only');
    END
    """,
)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


class ReentrantWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.depth = 0
        self.transactions = 0

    @contextmanager
    def transaction(self):
        outer = self.depth == 0 and not self.connection.in_transaction
        self.depth += 1
        if outer:
            self.transactions += 1
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if outer:
                self.connection.rollback()
            raise
        else:
            if outer:
                self.connection.commit()
        finally:
            self.depth -= 1


class BoundAdapter:
    def __init__(self, kind: str, *, proves_completion: bool = False) -> None:
        self.kind = kind
        self.proves_completion = proves_completion

    def inspect(self, update, receipt) -> EvidenceObservation:
        characters = {"lease": "1", "event": "2", "git_diff": "3", "result_receipt": "4"}
        return EvidenceObservation(
            status="verified",
            activity_update_sha256=receipt.activity_update_sha256,
            activity_scope_sha256=receipt.activity_scope_sha256,
            evidence_sha256="sha256:" + characters[self.kind] * 64,
            work_item_id=receipt.work_item_id,
            role_assignment_sha256=receipt.role_assignment_sha256,
            cursor=receipt.cursor,
            observed_paths=update.evidence_paths if self.kind == "git_diff" else (),
            proves_completion=self.proves_completion,
        )


def _install_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _ACTIVITY_SCHEMA + _COORDINATOR_SCHEMA:
        connection.execute(statement)
    installed = "2026-08-12T05:59:00.000000Z"
    connection.execute(
        "INSERT INTO collaboration_activity_schema VALUES (1, ?, ?)",
        (DURABLE_ACTIVITY_SCHEMA_REVISION, installed),
    )
    connection.execute(
        "INSERT INTO collaboration_coordinator_audit_schema VALUES (1, ?, ?)",
        (DURABLE_COORDINATOR_SCHEMA_REVISION, installed),
    )
    connection.commit()


def _update() -> AgentActivityUpdate:
    return AgentActivityUpdate(
        scope=ActivityScope(
            project=PROJECT,
            coordination_session_id=COORDINATION_SESSION_ID,
            agent_session_id="agent-session:durable-coordinator",
            agent_id="agent:durable-coordinator",
        ),
        role="coordinator_store",
        summary="Completed the durable coordinator store implementation",
        previous=ActivitySlice(
            scope="contract audit",
            paths=("plastic_promise/collaboration/coordinator_supervisor.py",),
            summary="Audited the coordinator proof contract",
        ),
        current=ActivitySlice(
            scope="durable coordinator implementation",
            paths=("plastic_promise/collaboration/durable_coordinator_store.py",),
            summary="Completed the repository-backed coordinator authority",
        ),
        next=ActivitySlice(
            scope="focused verification",
            paths=("tests/test_durable_coordinator_store.py",),
            summary="Verify restart-safe generation and consumption",
        ),
        blockers=(),
        work_item_id="work:durable-coordinator",
        role_assignment_sha256=ROLE_ASSIGNMENT_SHA256,
        cursor=1,
    )


def _open(connection: sqlite3.Connection):
    writer = ReentrantWriter(connection)
    activity_repository = DurableActivityRepository(
        connection,
        transaction_factory=writer.transaction,
    )
    activity_authority = open_server_activity_audit_authority(
        repository=activity_repository,
        clock=MutableClock(),
    )
    coordinator_repository = DurableCoordinatorRepository(
        connection,
        activity_repository=activity_repository,
        transaction_factory=writer.transaction,
    )
    coordinator_authority = open_server_coordinator_audit_authority(
        project=PROJECT,
        coordination_session_id=COORDINATION_SESSION_ID,
        repository=coordinator_repository,
        clock=MutableClock(),
    )
    supervisor = CoordinatorSupervisor(
        project=PROJECT,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_authority=activity_authority,
        coordinator_authority=coordinator_authority,
        lease_adapter=BoundAdapter("lease"),
        event_adapter=BoundAdapter("event"),
        git_diff_adapter=BoundAdapter("git_diff"),
        result_receipt_adapter=BoundAdapter("result_receipt", proves_completion=True),
    )
    return supervisor, coordinator_repository, writer


def test_constructor_is_read_only_and_requires_migration_owned_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    writer = ReentrantWriter(connection)

    with pytest.raises(CoordinatorAuditError, match="^coordinator_durable_schema_missing$"):
        DurableCoordinatorRepository(
            connection,
            activity_repository=InMemoryActivityAuditRepository(),
            transaction_factory=writer.transaction,
        )

    assert not hasattr(DurableCoordinatorRepository, "install_schema")
    connection.close()


def test_frozen_schema_manifest_is_verified() -> None:
    connection = sqlite3.connect(":memory:")
    _install_schema(connection)
    _supervisor, repository, _writer = _open(connection)

    assert isinstance(repository, object)
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
    }
    assert set(DURABLE_COORDINATOR_REQUIRED_TABLES).issubset(names)
    assert set(DURABLE_COORDINATOR_REQUIRED_INDEXES).issubset(names)
    assert set(DURABLE_COORDINATOR_REQUIRED_TRIGGERS).issubset(names)
    connection.close()


def test_generation_head_and_consumption_survive_restart(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    first = sqlite3.connect(database)
    _install_schema(first)
    supervisor, _repository, writer = _open(first)
    update = _update()
    receipt = supervisor.audit_activity(update)
    assert receipt.audit_generation == 1
    assert writer.transactions == 2  # activity append + coordinator generation
    first.close()

    second = sqlite3.connect(database)
    second.execute("PRAGMA foreign_keys=ON")
    restarted, repository, restarted_writer = _open(second)
    current = repository.load_current(
        project_id=PROJECT.project_id,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_update_sha256=update.update_sha256,
    )
    assert current is not None
    assert current.receipt == receipt
    assert current.receipt is not receipt

    assert (
        restarted.dispatch_eligible(
            audit_receipt=receipt,
            source_update=update,
            work_items=(),
            dependency_callback=lambda _item: True,
            dispatch_callback=lambda _item: None,
        )
        == ()
    )
    assert restarted_writer.transactions == 1
    second.close()

    third = sqlite3.connect(database)
    third.execute("PRAGMA foreign_keys=ON")
    replayed, _repository, _writer = _open(third)
    with pytest.raises(CoordinatorDispatchError, match="coordinator_audit_receipt_replayed"):
        replayed.dispatch_eligible(
            audit_receipt=receipt,
            source_update=update,
            work_items=(),
            dependency_callback=lambda _item: True,
            dispatch_callback=lambda _item: None,
        )
    third.close()


def test_new_generation_supersedes_prior_receipt_after_restart(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    first = sqlite3.connect(database)
    _install_schema(first)
    supervisor, _repository, _writer = _open(first)
    update = _update()
    first_receipt = supervisor.audit_activity(update)
    first.close()

    second = sqlite3.connect(database)
    second.execute("PRAGMA foreign_keys=ON")
    restarted, repository, _writer = _open(second)
    second_receipt = restarted.audit_activity(update)
    assert second_receipt.audit_generation == 2
    current = repository.load_current(
        project_id=PROJECT.project_id,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_update_sha256=update.update_sha256,
    )
    assert current is not None and current.receipt == second_receipt
    with pytest.raises(CoordinatorDispatchError, match="coordinator_audit_receipt_superseded"):
        restarted.dispatch_eligible(
            audit_receipt=first_receipt,
            source_update=update,
            work_items=(),
            dependency_callback=lambda _item: True,
            dispatch_callback=lambda _item: None,
        )
    second.close()


def test_stale_concurrent_generation_loses_cas_and_rolls_back() -> None:
    connection = sqlite3.connect(":memory:")
    _install_schema(connection)
    supervisor, repository, _writer = _open(connection)
    update = _update()
    receipt = supervisor.audit_activity(update)
    current = repository.load_current(
        project_id=PROJECT.project_id,
        coordination_session_id=COORDINATION_SESSION_ID,
        activity_update_sha256=update.update_sha256,
    )
    assert current is not None

    competing_receipt = CoordinatorActivityAuditReceipt._issue(
        receipt_id=_coordinator_receipt_id_for(
            authority_id=receipt.authority_id,
            activity_update_sha256=receipt.activity_update_sha256,
            generation=3,
        ),
        authority_id=receipt.authority_id,
        audit_generation=3,
        activity_receipt=receipt.activity_receipt,
        status=receipt.status,
        evidence_lineage=receipt.evidence_lineage,
        reason_codes=receipt.reason_codes,
        completion_verified=receipt.completion_verified,
        _token=_RECEIPT_ISSUE_TOKEN,
    )
    competing = CoordinatorAuditRecord(
        project=current.project,
        coordination_session_id=current.coordination_session_id,
        receipt=competing_receipt,
        recorded_at_utc="2026-08-12T06:00:01.000000Z",
    )

    with pytest.raises(CoordinatorAuditError, match="coordinator_audit_generation_conflict"):
        repository.append_generation(
            competing,
            expected_generation=2,
            _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM collaboration_coordinator_audits"
    ).fetchone() == (1,)
    connection.close()


def test_exact_generation_replay_preserves_original_record_time() -> None:
    connection = sqlite3.connect(":memory:")
    _install_schema(connection)
    supervisor, repository, writer = _open(connection)
    receipt = supervisor.audit_activity(_update())
    stored = repository.load_by_receipt_id(receipt.receipt_id)
    assert stored is not None
    replay = CoordinatorAuditRecord(
        project=stored.project,
        coordination_session_id=stored.coordination_session_id,
        receipt=stored.receipt,
        recorded_at_utc="2026-08-12T06:01:00.000000Z",
    )

    returned = repository.append_generation(
        replay,
        expected_generation=0,
        _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
    )

    assert returned == stored
    assert returned.recorded_at_utc == "2026-08-12T06:00:00.000000Z"
    assert writer.transactions == 2
    connection.close()


def test_consumption_row_binding_corruption_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    _install_schema(connection)
    supervisor, repository, _writer = _open(connection)
    update = _update()
    receipt = supervisor.audit_activity(update)
    assert (
        supervisor.dispatch_eligible(
            audit_receipt=receipt,
            source_update=update,
            work_items=(),
            dependency_callback=lambda _item: True,
            dispatch_callback=lambda _item: None,
        )
        == ()
    )
    connection.execute("DROP TRIGGER collaboration_coordinator_consumptions_no_update")
    connection.execute(
        "UPDATE collaboration_coordinator_audit_consumptions "
        "SET receipt_sha256=? WHERE activity_update_sha256=?",
        ("sha256:" + "f" * 64, update.update_sha256),
    )
    connection.commit()

    with pytest.raises(
        CoordinatorDispatchError,
        match="coordinator_durable_record_corrupt",
    ):
        supervisor.dispatch_eligible(
            audit_receipt=receipt,
            source_update=update,
            work_items=(),
            dependency_callback=lambda _item: True,
            dispatch_callback=lambda _item: None,
        )
    with pytest.raises(CoordinatorAuditError, match="coordinator_durable_record_corrupt"):
        repository.load_consumption(update.update_sha256)
    connection.close()


def test_json_or_denormalized_tampering_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    _install_schema(connection)
    supervisor, repository, _writer = _open(connection)
    receipt = supervisor.audit_activity(_update())
    connection.execute("DROP TRIGGER collaboration_coordinator_audits_no_update")
    payload = receipt.to_dict()
    payload["status"] = "blocked"
    connection.execute(
        "UPDATE collaboration_coordinator_audits SET receipt_json=? WHERE receipt_id=?",
        (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            receipt.receipt_id,
        ),
    )
    connection.commit()

    with pytest.raises(CoordinatorAuditError, match="coordinator_durable_record_corrupt"):
        repository.load_by_receipt_id(receipt.receipt_id)
    connection.close()
