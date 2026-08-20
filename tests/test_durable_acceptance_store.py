"""Focused restart, rollback, and lineage tests for durable acceptance."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from plastic_promise.collaboration.acceptance_receipt import (
    REVIEW_CHANNELS,
    AcceptanceReceipt,
    AcceptanceReceiptAuthority,
    AcceptanceReceiptError,
    ReviewReceipt,
    open_server_acceptance_receipt_authority,
)
from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.durable_acceptance_store import (
    DURABLE_ACCEPTANCE_SCHEMA_REVISION,
    DurableAcceptanceAuthorityRepository,
)
from plastic_promise.collaboration.durable_role_store import (
    _MIGRATION_SCHEMA_AUTHORITY,
    DurableRoleAssignmentRepository,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.policy_binding import (
    AgentPolicyBinding,
    AgentPolicyBindingAuthority,
    open_server_agent_policy_binding_authority,
)
from plastic_promise.collaboration.role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    RoleAssignmentAuthority,
    RoleAssignmentBasis,
    open_server_role_assignment_authority,
)

if TYPE_CHECKING:
    from pathlib import Path

BASE = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)
POLICY_REVISION = "acceptance-review-policy/v1"
SOURCE_REVISION = "c" * 40
DIFF_DIGEST = "sha256:" + "a" * 64
REQUIREMENT_SET_DIGEST = "sha256:" + "b" * 64
UNION_CONTRACT_REVISION = "2026-08-11.3"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class ReentrantWriter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.depth = 0

    @contextmanager
    def transaction(self):
        outer = self.depth == 0 and not self.connection.in_transaction
        self.depth += 1
        if outer:
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


@dataclass
class DurableHarness:
    path: Path
    connection: sqlite3.Connection
    writer: ReentrantWriter
    clock: MutableClock
    repository: DurableAcceptanceAuthorityRepository
    role_repository: DurableRoleAssignmentRepository
    role_authority: RoleAssignmentAuthority
    policy_authority: AgentPolicyBindingAuthority
    reviewer_binding: AgentPolicyBinding
    authority: AcceptanceReceiptAuthority
    project: ProjectScope
    work: WorkReceipt
    lease: WorkLease
    result: ResultReceipt
    submitter_session: AgentSession
    reviewer_session: AgentSession
    reviews: tuple[ReviewReceipt, ...]

    @property
    def review(self) -> ReviewReceipt:
        return self.reviews[0]

    def issue(self, *, acceptance_receipt_id: str = "acceptance:durable") -> AcceptanceReceipt:
        return self.authority.issue(
            self.work,
            self.result,
            self.reviews,
            submitter_session=self.submitter_session,
            reviewer_session=self.reviewer_session,
            reviewer_policy_binding=self.reviewer_binding,
            acceptance_receipt_id=acceptance_receipt_id,
        )


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _install_test_schema(
    connection: sqlite3.Connection,
    writer: ReentrantWriter,
    clock: MutableClock,
) -> None:
    """Test-only DDL mirror; deployment owns the canonical installer."""

    connection.executescript(
        """
        CREATE TABLE collaboration_agent_sessions (
            session_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            session_json TEXT NOT NULL,
            session_sha256 TEXT NOT NULL
        );
        CREATE TABLE collaboration_work_items (
            work_item_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            work_receipt_json TEXT NOT NULL,
            work_receipt_sha256 TEXT NOT NULL,
            assigned_agent_id TEXT NOT NULL
        );
        CREATE TABLE collaboration_work_leases (
            lease_id TEXT PRIMARY KEY,
            work_item_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            owner_session_id TEXT NOT NULL,
            lease_json TEXT NOT NULL,
            lease_sha256 TEXT NOT NULL,
            fencing_generation INTEGER NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE collaboration_results (
            receipt_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            result_json TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            outcome TEXT NOT NULL
        );
        """
    )
    connection.commit()
    DurableRoleAssignmentRepository.install_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        _migration_authority=_MIGRATION_SCHEMA_AUTHORITY,
    )
    connection.executescript(
        """
        CREATE TABLE collaboration_acceptance_schema (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_revision TEXT NOT NULL,
            installed_at_utc TEXT NOT NULL
        );
        CREATE TABLE collaboration_review_receipts (
            review_receipt_sha256 TEXT PRIMARY KEY,
            review_receipt_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            work_receipt_sha256 TEXT NOT NULL,
            result_receipt_sha256 TEXT NOT NULL,
            reviewer_assignment_sha256 TEXT NOT NULL,
            reviewer_agent_session_id TEXT NOT NULL,
            review_policy_revision TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            review_channel TEXT NOT NULL,
            diff_digest TEXT NOT NULL,
            requirement_set_digest TEXT NOT NULL,
            union_contract_revision TEXT NOT NULL,
            decision TEXT NOT NULL,
            conflict_state TEXT NOT NULL,
            reviewed_at_utc TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            UNIQUE(
                project_id, coordination_session_id, work_item_id,
                result_receipt_sha256, reviewer_assignment_sha256, review_channel
            )
        );
        CREATE INDEX idx_collaboration_review_receipts_scope
            ON collaboration_review_receipts(
                project_id, coordination_session_id, work_item_id, reviewed_at_utc
            );
        CREATE TABLE collaboration_acceptance_receipts (
            acceptance_receipt_sha256 TEXT PRIMARY KEY,
            acceptance_receipt_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL,
            coordination_session_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            work_receipt_id TEXT NOT NULL,
            work_receipt_sha256 TEXT NOT NULL,
            result_receipt_id TEXT NOT NULL,
            result_receipt_sha256 TEXT NOT NULL,
            review_receipt_id TEXT NOT NULL,
            review_receipt_sha256 TEXT NOT NULL,
            submitter_agent_session_id TEXT NOT NULL,
            submitter_agent_session_sha256 TEXT NOT NULL,
            reviewer_agent_session_id TEXT NOT NULL,
            reviewer_agent_session_sha256 TEXT NOT NULL,
            submitter_assignment_sha256 TEXT NOT NULL,
            reviewer_assignment_sha256 TEXT NOT NULL,
            assignment_policy_revision TEXT NOT NULL,
            review_policy_revision TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            diff_digest TEXT NOT NULL,
            requirement_set_digest TEXT NOT NULL,
            union_contract_revision TEXT NOT NULL,
            decision TEXT NOT NULL,
            conflict_state TEXT NOT NULL,
            issued_at_utc TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            UNIQUE(project_id, coordination_session_id, work_item_id, result_receipt_sha256)
        );
        CREATE INDEX idx_collaboration_acceptance_receipts_scope
            ON collaboration_acceptance_receipts(
                project_id, coordination_session_id, work_item_id, issued_at_utc
            );
        CREATE TRIGGER collaboration_review_receipts_no_update
        BEFORE UPDATE ON collaboration_review_receipts
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_review_receipt_append_only');
        END;
        CREATE TRIGGER collaboration_review_receipts_no_delete
        BEFORE DELETE ON collaboration_review_receipts
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_review_receipt_append_only');
        END;
        CREATE TRIGGER collaboration_acceptance_receipts_no_update
        BEFORE UPDATE ON collaboration_acceptance_receipts
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_acceptance_receipt_append_only');
        END;
        CREATE TRIGGER collaboration_acceptance_receipts_no_delete
        BEFORE DELETE ON collaboration_acceptance_receipts
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_acceptance_receipt_append_only');
        END;
        CREATE TABLE collaboration_acceptance_review_bindings (
            acceptance_receipt_sha256 TEXT NOT NULL,
            review_channel TEXT NOT NULL,
            review_receipt_sha256 TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            diff_digest TEXT NOT NULL,
            requirement_set_digest TEXT NOT NULL,
            union_contract_revision TEXT NOT NULL,
            PRIMARY KEY (acceptance_receipt_sha256, review_channel),
            UNIQUE (acceptance_receipt_sha256, review_receipt_sha256)
        );
        CREATE TRIGGER collaboration_acceptance_review_bindings_no_update
        BEFORE UPDATE ON collaboration_acceptance_review_bindings
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_acceptance_review_binding_append_only');
        END;
        CREATE TRIGGER collaboration_acceptance_review_bindings_no_delete
        BEFORE DELETE ON collaboration_acceptance_review_bindings
        BEGIN
            SELECT RAISE(ABORT, 'collaboration_acceptance_review_binding_append_only');
        END;
        """
    )
    connection.execute(
        """
        INSERT INTO collaboration_acceptance_schema (
            singleton, schema_revision, installed_at_utc
        ) VALUES (1, ?, ?)
        """,
        (DURABLE_ACCEPTANCE_SCHEMA_REVISION, _text(clock.value)),
    )
    connection.commit()


def _session(
    identity: AgentIdentity,
    project: ProjectScope,
    *,
    session_id: str,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        identity=identity,
        project=project,
        coordination_session_id="coord:durable-acceptance",
        state="active",
        started_at=_text(BASE - timedelta(minutes=10)),
        last_heartbeat_at=_text(BASE + timedelta(minutes=20)),
        expires_at=_text(BASE + timedelta(hours=2)),
    )


def _intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
    use: str,
    role: str,
    stage: str,
    created_at: datetime,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=f"event:intent:{session.session_id}:{use}",
        project=work.project,
        coordination_session_id=work.coordination_session_id,
        actor=session.identity,
        event_type="agent.intent_declared",
        summary=f"Intent for {use}",
        created_at=_text(created_at),
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _persist_initial_sources(
    connection: sqlite3.Connection,
    writer: ReentrantWriter,
    *,
    work: WorkReceipt,
    lease: WorkLease,
    submitter_session: AgentSession,
    reviewer_session: AgentSession,
) -> None:
    with writer.transaction():
        for session in (submitter_session, reviewer_session):
            connection.execute(
                """
                INSERT INTO collaboration_agent_sessions (
                    session_id, project_id, agent_id, coordination_session_id,
                    session_json, session_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.project.project_id,
                    session.identity.agent_id,
                    session.coordination_session_id,
                    _canonical_json(session.to_dict()),
                    session.content_sha256,
                ),
            )
        connection.execute(
            """
            INSERT INTO collaboration_work_items (
                work_item_id, project_id, coordination_session_id,
                work_receipt_json, work_receipt_sha256, assigned_agent_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                work.work_item_id,
                work.project.project_id,
                work.coordination_session_id,
                _canonical_json(work.to_dict()),
                work.content_sha256,
                work.assigned_agent.agent_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO collaboration_work_leases (
                lease_id, work_item_id, project_id, coordination_session_id,
                owner_id, owner_session_id, lease_json, lease_sha256, fencing_generation, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                lease.lease_id,
                work.work_item_id,
                work.project.project_id,
                work.coordination_session_id,
                lease.owner_id,
                submitter_session.session_id,
                _canonical_json(lease.to_dict()),
                lease.content_sha256,
                lease.fencing_generation,
            ),
        )


def _persist_result(
    connection: sqlite3.Connection,
    writer: ReentrantWriter,
    *,
    result: ResultReceipt,
    lease: WorkLease,
) -> None:
    projection = result.to_dict()
    projection["lease_binding"] = {
        "lease_id": lease.lease_id,
        "lease_sha256": lease.content_sha256,
        "fencing_generation": lease.fencing_generation,
        "result_binding_sha256": result.work_receipt_sha256,
    }
    with writer.transaction():
        connection.execute(
            """
            INSERT INTO collaboration_results (
                receipt_id, project_id, coordination_session_id, work_item_id,
                result_json, result_sha256, submitted_at, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.receipt_id,
                result.project.project_id,
                result.coordination_session_id,
                result.work_item_id,
                _canonical_json(projection),
                result.content_sha256,
                result.submitted_at,
                result.outcome,
            ),
        )


def _harness(tmp_path: Path) -> DurableHarness:
    path = tmp_path / "durable-acceptance.sqlite3"
    connection = sqlite3.connect(path)
    writer = ReentrantWriter(connection)
    clock = MutableClock(BASE + timedelta(minutes=5))
    _install_test_schema(connection, writer, clock)

    project = ProjectScope("project:durable-acceptance")
    submitter = AgentIdentity("agent:durable-submitter", "participant")
    reviewer = AgentIdentity("agent:durable-reviewer", "deepsec_reviewer")
    work = WorkReceipt(
        receipt_id="work-receipt:durable-acceptance",
        work_item_id="work:durable-acceptance",
        project=project,
        coordination_session_id="coord:durable-acceptance",
        assigned_agent=submitter,
        objective="Persist exact acceptance authority across restart",
        fencing_generation=3,
        issued_at=_text(BASE),
        expires_at=_text(BASE + timedelta(hours=1)),
    )
    item = WorkItem(
        work_item_id=work.work_item_id,
        project=project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:durable-acceptance",
        created_at=_text(BASE),
        max_attempts=3,
        coordination_session_id=work.coordination_session_id,
    )
    lease = WorkLease(
        lease_id="lease:durable-acceptance",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=submitter.agent_id,
        owner_identity=submitter,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at=_text(BASE + timedelta(minutes=1)),
        expires_at=_text(BASE + timedelta(minutes=45)),
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )
    submitter_session = _session(
        submitter,
        project,
        session_id="agent-session:durable-submitter",
    )
    reviewer_session = _session(
        reviewer,
        project,
        session_id="agent-session:durable-reviewer",
    )
    _persist_initial_sources(
        connection,
        writer,
        work=work,
        lease=lease,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
    )

    role_repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    role_authority = open_server_role_assignment_authority(
        repository=role_repository,
        clock=clock,
    )
    submitter_basis = RoleAssignmentBasis(
        session=submitter_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=submitter_session,
            work=work,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
            created_at=BASE + timedelta(minutes=4),
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )
    role_repository.register_basis(use=RESULT_SUBMISSION_USE, basis=submitter_basis)
    submitter_assignment = role_authority.issue(
        use=RESULT_SUBMISSION_USE,
        agent_session_id=submitter_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=submitter_basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    result = ResultReceipt.for_work(
        work,
        receipt_id="result:durable-acceptance",
        submitted_by=submitter,
        outcome="completed",
        summary="Durable acceptance source result",
        submitted_at=_text(BASE + timedelta(minutes=10)),
        role_assignment_sha256=submitter_assignment.assignment_sha256,
        artifact_refs=("artifact:durable-acceptance",),
        evidence_refs=("test:durable-acceptance",),
        result={"status": "focused-pass"},
    )
    _persist_result(connection, writer, result=result, lease=lease)

    clock.value = BASE + timedelta(minutes=13)
    reviewer_basis = RoleAssignmentBasis(
        session=reviewer_session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=reviewer_session,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
            created_at=BASE + timedelta(minutes=12),
        ),
        workflow_stage="code-review",
        work_state="reviewing",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter_session.session_id,
    )
    role_repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=reviewer_basis)
    reviewer_assignment = role_authority.issue(
        use=ACCEPTANCE_REVIEW_USE,
        agent_session_id=reviewer_session.session_id,
        work_item_id=work.work_item_id,
        lease_id=lease.lease_id,
        intent_event_id=reviewer_basis.intent_event.event_id,
        ttl_seconds=1800,
    )
    reviews = tuple(
        ReviewReceipt.for_result(
            work,
            result,
            review_receipt_id=f"review:durable-acceptance:{channel}",
            reviewer_assignment_sha256=reviewer_assignment.assignment_sha256,
            reviewer_agent_session_id=reviewer_session.session_id,
            review_policy_revision=POLICY_REVISION,
            source_revision=SOURCE_REVISION,
            decision="accepted",
            conflict_state="none",
            reviewed_at_utc=_text(BASE + timedelta(minutes=15)),
            evidence_refs=(f"review:durable-acceptance:{channel}",),
            review_channel=channel,
            diff_digest=DIFF_DIGEST,
            requirement_set_digest=REQUIREMENT_SET_DIGEST,
            union_contract_revision=UNION_CONTRACT_REVISION,
        )
        for channel in REVIEW_CHANNELS
    )

    clock.value = BASE + timedelta(minutes=20)
    policy_authority = open_server_agent_policy_binding_authority(clock=clock)
    reviewer_binding = policy_authority.issue(
        reviewer_session,
        binding_id="binding:durable-acceptance-reviewer",
        policy_revision=POLICY_REVISION,
        ttl_seconds=1800,
    )
    repository = DurableAcceptanceAuthorityRepository(
        connection,
        transaction_factory=writer.transaction,
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=policy_authority,
        role_assignment_authority=role_authority,
        repository=repository,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        clock=clock,
    )
    return DurableHarness(
        path=path,
        connection=connection,
        writer=writer,
        clock=clock,
        repository=repository,
        role_repository=role_repository,
        role_authority=role_authority,
        policy_authority=policy_authority,
        reviewer_binding=reviewer_binding,
        authority=authority,
        project=project,
        work=work,
        lease=lease,
        result=result,
        submitter_session=submitter_session,
        reviewer_session=reviewer_session,
        reviews=reviews,
    )


def test_constructor_fails_closed_without_installing_or_repairing_schema() -> None:
    connection = sqlite3.connect(":memory:")
    writer = ReentrantWriter(connection)

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_durable_schema_missing$",
    ):
        DurableAcceptanceAuthorityRepository(
            connection,
            transaction_factory=writer.transaction,
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert not any(name.startswith("collaboration_acceptance") for name in tables)
    assert "collaboration_review_receipts" not in tables


def test_constructor_rejects_named_but_semantically_wrong_schema(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.connection.execute("DROP INDEX idx_collaboration_acceptance_receipts_scope")
    harness.connection.execute(
        """
        CREATE INDEX idx_collaboration_acceptance_receipts_scope
            ON collaboration_acceptance_receipts(project_id, work_item_id)
        """
    )
    harness.connection.commit()

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_durable_schema_stale$",
    ):
        DurableAcceptanceAuthorityRepository(
            harness.connection,
            transaction_factory=harness.writer.transaction,
        )


def test_disk_restart_preserves_verify_replay_and_revision_history(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    receipt = harness.issue()
    assert (
        tuple(
            harness.repository.load_review_by_digest(review.content_sha256)
            for review in harness.reviews
        )
        == harness.reviews
    )
    assert harness.authority.verify_issued(receipt) == receipt
    assert harness.authority.verify_issued(receipt) == receipt
    assert harness.connection.execute(
        "SELECT COUNT(*) FROM collaboration_review_receipts"
    ).fetchone()[0] == len(REVIEW_CHANNELS)
    assert (
        harness.connection.execute(
            "SELECT COUNT(*) FROM collaboration_acceptance_receipts"
        ).fetchone()[0]
        == 1
    )
    columns = {
        str(row[1])
        for row in harness.connection.execute(
            "PRAGMA table_info(collaboration_acceptance_receipts)"
        ).fetchall()
    }
    assert "consumed" not in columns
    harness.role_authority.revoke(
        harness.review.reviewer_assignment_sha256,
        expected_generation=1,
    )
    assert harness.authority.verify_issued(receipt) == receipt
    harness.connection.close()

    restarted_connection = sqlite3.connect(harness.path)
    restarted_writer = ReentrantWriter(restarted_connection)
    restarted_clock = MutableClock(BASE + timedelta(minutes=21))
    restarted_repository = DurableAcceptanceAuthorityRepository(
        restarted_connection,
        transaction_factory=restarted_writer.transaction,
    )
    restarted_role_repository = DurableRoleAssignmentRepository(
        restarted_connection,
        transaction_factory=restarted_writer.transaction,
        clock=restarted_clock,
    )
    restarted_role_authority = open_server_role_assignment_authority(
        repository=restarted_role_repository,
        clock=restarted_clock,
    )
    restarted_policy = open_server_agent_policy_binding_authority(clock=restarted_clock)
    restarted_authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=restarted_policy,
        role_assignment_authority=restarted_role_authority,
        repository=restarted_repository,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision="d" * 40,
        clock=restarted_clock,
    )

    assert restarted_authority.verify_issued(receipt) == receipt
    assert restarted_authority.verify_issued(receipt) == receipt
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_source_revision_stale$",
    ):
        restarted_authority.issue(
            harness.work,
            harness.result,
            harness.reviews,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            reviewer_policy_binding=None,
            acceptance_receipt_id=receipt.acceptance_receipt_id,
        )
    current_revision_authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=restarted_policy,
        role_assignment_authority=restarted_role_authority,
        repository=restarted_repository,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        clock=restarted_clock,
    )
    assert (
        current_revision_authority.issue(
            harness.work,
            harness.result,
            harness.reviews,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            reviewer_policy_binding=None,
            acceptance_receipt_id=receipt.acceptance_receipt_id,
        )
        == receipt
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_evidence_digest_mismatch$",
    ):
        restarted_authority.verify_issued(replace(receipt, evidence_sha256="sha256:" + "0" * 64))
    with pytest.raises(
        sqlite3.IntegrityError, match="collaboration_acceptance_receipt_append_only"
    ):
        restarted_connection.execute(
            """
            UPDATE collaboration_acceptance_receipts
               SET source_revision=? WHERE acceptance_receipt_id=?
            """,
            ("e" * 40, receipt.acceptance_receipt_id),
        )
    restarted_connection.close()


def test_assignment_revocation_between_precheck_and_append_fails_closed(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    revoked = False

    @contextmanager
    def revoking_transaction():
        nonlocal revoked
        if not revoked:
            revoked = True
            harness.role_authority.revoke(
                harness.review.reviewer_assignment_sha256,
                expected_generation=1,
            )
        with harness.writer.transaction():
            yield

    repository = DurableAcceptanceAuthorityRepository(
        harness.connection,
        transaction_factory=revoking_transaction,
    )
    authority = open_server_acceptance_receipt_authority(
        reviewer_policy_authority=harness.policy_authority,
        role_assignment_authority=harness.role_authority,
        repository=repository,
        current_review_policy_revision=POLICY_REVISION,
        current_source_revision=SOURCE_REVISION,
        clock=harness.clock,
    )

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_role_assignment_lineage_mismatch$",
    ):
        authority.issue(
            harness.work,
            harness.result,
            harness.reviews,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            reviewer_policy_binding=harness.reviewer_binding,
            acceptance_receipt_id="acceptance:revocation-race",
        )
    assert revoked is True
    assert (
        harness.connection.execute("SELECT COUNT(*) FROM collaboration_review_receipts").fetchone()[
            0
        ]
        == 0
    )
    assert (
        harness.connection.execute(
            "SELECT COUNT(*) FROM collaboration_acceptance_receipts"
        ).fetchone()[0]
        == 0
    )


def test_result_lease_binding_is_verified_beyond_result_digest(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    row = harness.connection.execute(
        "SELECT result_json FROM collaboration_results WHERE receipt_id=?",
        (harness.result.receipt_id,),
    ).fetchone()
    payload = json.loads(str(row[0]))
    payload["lease_binding"]["lease_sha256"] = "sha256:" + "0" * 64
    harness.connection.execute(
        "UPDATE collaboration_results SET result_json=? WHERE receipt_id=?",
        (_canonical_json(payload), harness.result.receipt_id),
    )
    harness.connection.commit()

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_result_lease_binding_mismatch$",
    ):
        harness.issue()


def test_rolled_back_canonical_sources_never_become_acceptance_authority(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    orphan_work = replace(
        harness.work,
        receipt_id="work-receipt:rollback-orphan",
        work_item_id="work:rollback-orphan",
    )
    orphan_result = ResultReceipt.for_work(
        orphan_work,
        receipt_id="result:rollback-orphan",
        submitted_by=orphan_work.assigned_agent,
        outcome="completed",
        summary="This transaction must roll back",
        submitted_at=harness.result.submitted_at,
        role_assignment_sha256="sha256:" + "1" * 64,
    )

    with pytest.raises(RuntimeError, match="rollback-fixture"), harness.writer.transaction():
        harness.connection.execute(
            """
                INSERT INTO collaboration_work_items (
                    work_item_id, project_id, coordination_session_id,
                    work_receipt_json, work_receipt_sha256, assigned_agent_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
            (
                orphan_work.work_item_id,
                orphan_work.project.project_id,
                orphan_work.coordination_session_id,
                _canonical_json(orphan_work.to_dict()),
                orphan_work.content_sha256,
                orphan_work.assigned_agent.agent_id,
            ),
        )
        raise RuntimeError("rollback-fixture")

    assert (
        harness.connection.execute(
            "SELECT 1 FROM collaboration_work_items WHERE work_item_id=?",
            (orphan_work.work_item_id,),
        ).fetchone()
        is None
    )
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_work_source_unverified$",
    ):
        harness.repository.require_canonical_sources(
            orphan_work,
            orphan_result,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            submitter_assignment_sha256=orphan_result.role_assignment_sha256,
            reviewer_assignment_sha256=harness.review.reviewer_assignment_sha256,
        )


def test_repository_write_requires_server_authority_and_natural_binding_is_unique(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    receipt = harness.issue()

    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_repository_write_authority_required$",
    ):
        harness.repository.append_exact(
            harness.reviews,
            receipt,
            work=harness.work,
            result=harness.result,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
        )
    conflicting_reviews = tuple(replace(review, decision="rejected") for review in harness.reviews)
    with pytest.raises(
        AcceptanceReceiptError,
        match="^acceptance_receipt_decision_conflict$",
    ):
        harness.authority.issue(
            harness.work,
            harness.result,
            conflicting_reviews,
            submitter_session=harness.submitter_session,
            reviewer_session=harness.reviewer_session,
            reviewer_policy_binding=harness.reviewer_binding,
            acceptance_receipt_id=receipt.acceptance_receipt_id,
        )
