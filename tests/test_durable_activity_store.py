"""Focused persistence tests for the durable Agent activity ledger."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from plastic_promise.collaboration.activity_update import (
    ActivityAuditRepository,
    ActivityContractError,
    ActivityScope,
    ActivitySlice,
    AgentActivityUpdate,
    open_server_activity_audit_authority,
)
from plastic_promise.collaboration.contracts import ProjectScope
from plastic_promise.collaboration.durable_activity_store import (
    DURABLE_ACTIVITY_REQUIRED_INDEXES,
    DURABLE_ACTIVITY_REQUIRED_TABLES,
    DURABLE_ACTIVITY_REQUIRED_TRIGGERS,
    DURABLE_ACTIVITY_SCHEMA_REVISION,
    DurableActivityRepository,
)

NOW = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
PROJECT = ProjectScope("project:durable-activity")
ROLE_ASSIGNMENT_SHA256 = "sha256:" + "a" * 64

_TEST_SCHEMA = (
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


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


class ReentrantWriter:
    """Small stand-in for the canonical SQLite single-writer lock."""

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


def _install_test_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in _TEST_SCHEMA:
        connection.execute(statement)
    connection.execute(
        """
        INSERT INTO collaboration_activity_schema(
            singleton, schema_revision, installed_at_utc
        ) VALUES (1, ?, ?)
        """,
        (DURABLE_ACTIVITY_SCHEMA_REVISION, "2026-08-12T00:59:00.000000Z"),
    )
    connection.commit()


def _scope() -> ActivityScope:
    return ActivityScope(
        project=PROJECT,
        coordination_session_id="coord:durable-activity",
        agent_session_id="agent-session:durable-activity",
        agent_id="agent:durable-activity",
    )


def _update(
    *,
    cursor: int = 1,
    summary: str = "Persist one exact Agent activity update",
    current_summary: str = "Implementing the durable activity repository",
) -> AgentActivityUpdate:
    return AgentActivityUpdate(
        scope=_scope(),
        role="activity_store",
        summary=summary,
        previous=ActivitySlice(
            scope="contract audit",
            paths=("plastic_promise/collaboration/activity_update.py",),
            summary="Audited the typed public activity contract",
        ),
        current=ActivitySlice(
            scope="durable activity implementation",
            paths=("plastic_promise/collaboration/durable_activity_store.py",),
            summary=current_summary,
        ),
        next=ActivitySlice(
            scope="focused verification",
            paths=("tests/test_durable_activity_store.py",),
            summary="Verify restart-safe receipt lineage",
        ),
        blockers=(),
        work_item_id="work:durable-activity",
        role_assignment_sha256=ROLE_ASSIGNMENT_SHA256,
        cursor=cursor,
    )


def _open(
    connection: sqlite3.Connection,
) -> tuple[DurableActivityRepository, ReentrantWriter]:
    writer = ReentrantWriter(connection)
    repository = DurableActivityRepository(
        connection,
        transaction_factory=writer.transaction,
    )
    return repository, writer


def test_constructor_is_read_only_and_requires_the_migration_owned_schema() -> None:
    connection = sqlite3.connect(":memory:")
    writer = ReentrantWriter(connection)

    with pytest.raises(ActivityContractError, match="^activity_durable_schema_missing$"):
        DurableActivityRepository(
            connection,
            transaction_factory=writer.transaction,
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert not tables.intersection(DURABLE_ACTIVITY_REQUIRED_TABLES)
    assert not hasattr(DurableActivityRepository, "install_schema")
    connection.close()


def test_frozen_table_index_and_trigger_manifest_is_verified() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, _ = _open(connection)

    assert isinstance(repository, ActivityAuditRepository)
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
    }
    assert set(DURABLE_ACTIVITY_REQUIRED_TABLES).issubset(names)
    assert set(DURABLE_ACTIVITY_REQUIRED_INDEXES).issubset(names)
    assert set(DURABLE_ACTIVITY_REQUIRED_TRIGGERS).issubset(names)
    connection.close()

    stale = sqlite3.connect(":memory:")
    _install_test_schema(stale)
    stale.execute("DROP INDEX idx_collaboration_activity_audits_scope_cursor")
    with pytest.raises(ActivityContractError, match="^activity_durable_schema_stale$"):
        DurableActivityRepository(
            stale,
            transaction_factory=ReentrantWriter(stale).transaction,
        )
    stale.close()


def test_receipt_issue_replay_and_verification_survive_connection_restart(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    first_connection = sqlite3.connect(database)
    _install_test_schema(first_connection)
    first_repository, first_writer = _open(first_connection)
    first_authority = open_server_activity_audit_authority(
        repository=first_repository,
        clock=MutableClock(),
    )
    update = _update(cursor=3)

    receipt = first_authority.issue(update)

    assert first_writer.transactions == 1
    assert first_authority.verify_issued(receipt, update=update) == receipt
    first_connection.close()

    second_connection = sqlite3.connect(database)
    second_connection.execute("PRAGMA foreign_keys=ON")
    second_repository, second_writer = _open(second_connection)
    second_authority = open_server_activity_audit_authority(
        repository=second_repository,
        clock=MutableClock(),
    )

    recovered = second_authority.verify_issued(receipt, update=update)
    replay = second_authority.issue(update)
    stored = second_repository.load_by_update_digest(update.update_sha256)

    assert recovered == receipt
    assert recovered is not receipt
    assert replay == receipt
    assert second_writer.transactions == 0
    assert stored is not None
    assert stored.update == update
    assert stored.receipt == receipt
    assert stored.recorded_at_utc == "2026-08-12T01:00:00.000000Z"
    second_connection.close()


def test_cursor_conflict_and_regression_remain_fenced_after_restart(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    connection = sqlite3.connect(database)
    _install_test_schema(connection)
    repository, _ = _open(connection)
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )
    authority.issue(_update(cursor=3))
    connection.close()

    restarted = sqlite3.connect(database)
    restarted.execute("PRAGMA foreign_keys=ON")
    restarted_repository, _ = _open(restarted)
    restarted_authority = open_server_activity_audit_authority(
        repository=restarted_repository,
        clock=MutableClock(),
    )

    with pytest.raises(ActivityContractError, match="^activity_audit_cursor_conflict$"):
        restarted_authority.issue(
            _update(cursor=3, current_summary="A conflicting update at the same cursor")
        )
    restarted_authority.issue(_update(cursor=5, current_summary="Later verified progress"))
    with pytest.raises(ActivityContractError, match="^activity_audit_cursor_regression$"):
        restarted_authority.issue(_update(cursor=4, current_summary="Regressed progress"))
    restarted.close()


def test_repository_write_requires_the_server_authority_token() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, _ = _open(connection)
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )
    update = _update()
    receipt = authority.issue(update)

    with pytest.raises(
        ActivityContractError,
        match="^activity_audit_repository_write_authority_required$",
    ):
        repository.append_exact(
            update,
            receipt,
            recorded_at_utc="2026-08-12T01:00:00.000000Z",
        )
    connection.close()


def test_denormalized_or_json_tampering_fails_closed_on_read() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, _ = _open(connection)
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )
    update = _update()
    authority.issue(update)

    connection.execute("DROP TRIGGER collaboration_agent_activity_no_update")
    connection.execute(
        "UPDATE collaboration_agent_activity SET agent_id='agent:tampered' "
        "WHERE activity_update_sha256=?",
        (update.update_sha256,),
    )
    connection.commit()

    with pytest.raises(ActivityContractError, match="^activity_durable_record_corrupt$"):
        repository.load_by_update_digest(update.update_sha256)
    connection.close()


def test_failed_audit_insert_rolls_back_the_activity_row_atomically() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, writer = _open(connection)
    connection.execute(
        """
        CREATE TRIGGER test_reject_activity_audit
        BEFORE INSERT ON collaboration_activity_audits
        BEGIN
            SELECT RAISE(ABORT, 'test_reject_activity_audit');
        END
        """
    )
    connection.commit()
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )

    with pytest.raises(ActivityContractError, match="^activity_durable_append_conflict$"):
        authority.issue(_update())

    assert writer.transactions == 1
    assert connection.execute("SELECT COUNT(*) FROM collaboration_agent_activity").fetchone() == (
        0,
    )
    assert connection.execute("SELECT COUNT(*) FROM collaboration_activity_audits").fetchone() == (
        0,
    )
    connection.close()


def test_exact_replay_cannot_change_the_receipt_id() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, _ = _open(connection)
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )
    update = _update()
    authority.issue(update)

    with pytest.raises(ActivityContractError, match="^activity_audit_replay_ambiguous$"):
        authority.issue(update, receipt_id="activity-audit:alternate")
    connection.close()


def test_rehydrated_receipt_is_bound_to_exact_canonical_json() -> None:
    connection = sqlite3.connect(":memory:")
    _install_test_schema(connection)
    repository, _ = _open(connection)
    authority = open_server_activity_audit_authority(
        repository=repository,
        clock=MutableClock(),
    )
    update = _update()
    receipt = authority.issue(update)

    connection.execute("DROP TRIGGER collaboration_activity_audits_no_update")
    changed = receipt.to_dict()
    changed["validated_at_utc"] = "2026-08-12T01:00:01.000000Z"
    changed_json = json.dumps(
        changed,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "UPDATE collaboration_activity_audits SET receipt_json=? WHERE receipt_id=?",
        (changed_json, receipt.receipt_id),
    )
    connection.commit()

    with pytest.raises(ActivityContractError, match="^activity_durable_record_corrupt$"):
        repository.load_by_receipt_id(receipt.receipt_id)
    connection.close()
