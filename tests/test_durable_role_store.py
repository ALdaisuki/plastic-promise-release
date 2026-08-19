"""Focused interface tests for the durable role-assignment store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.collaboration.contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from plastic_promise.collaboration.durable_role_store import (
    _MIGRATION_SCHEMA_AUTHORITY,
    DurableRoleAssignmentRepository,
    install_schema,
)
from plastic_promise.collaboration.lease_contract import (
    AGENT_OWNER_KIND,
    AGENT_WORK_POLICY,
    WorkItem,
    WorkLease,
)
from plastic_promise.collaboration.role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    RoleAssignmentBasis,
    RoleAssignmentError,
    RoleAssignmentRepository,
    open_server_role_assignment_authority,
)

NOW = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


class ReentrantWriter:
    """Small stand-in for the server's canonical lock + SQLite batch."""

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


def _text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, role="participant")


def _session(
    *,
    project: ProjectScope,
    coordination_session_id: str,
    agent_id: str,
    suffix: str,
) -> AgentSession:
    return AgentSession(
        session_id=f"agent-session:{agent_id.removeprefix('agent:')}:{suffix}",
        identity=_identity(agent_id),
        project=project,
        coordination_session_id=coordination_session_id,
        state="active",
        started_at=_text(NOW - timedelta(minutes=5)),
        last_heartbeat_at=_text(NOW - timedelta(seconds=5)),
        expires_at=_text(NOW + timedelta(minutes=30)),
    )


def _work(
    *,
    project: ProjectScope,
    coordination_session_id: str,
    work_item_id: str,
    assigned: AgentIdentity,
) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=f"work-receipt:{work_item_id.removeprefix('work:')}",
        work_item_id=work_item_id,
        project=project,
        coordination_session_id=coordination_session_id,
        assigned_agent=assigned,
        objective="Exercise one exact durable work-role scope",
        fencing_generation=1,
        issued_at=_text(NOW - timedelta(minutes=2)),
        expires_at=_text(NOW + timedelta(minutes=20)),
    )


def _lease(work: WorkReceipt, *, owner: AgentIdentity) -> WorkLease:
    item = WorkItem(
        work_item_id=work.work_item_id,
        project=work.project,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        operation_kind="implement",
        input_sha256="sha256:" + "1" * 64,
        result_schema="result-schema:durable-role-assignment",
        created_at=_text(NOW - timedelta(minutes=3)),
        max_attempts=2,
        coordination_session_id=work.coordination_session_id,
    )
    return WorkLease(
        lease_id=f"lease:{work.work_item_id.removeprefix('work:')}",
        work_item=item,
        owner_kind=AGENT_OWNER_KIND,
        policy_kind=AGENT_WORK_POLICY,
        owner_id=owner.agent_id,
        owner_identity=owner,
        fencing_generation=work.fencing_generation,
        attempt=1,
        issued_at=_text(NOW - timedelta(minutes=1)),
        expires_at=_text(NOW + timedelta(minutes=10)),
        result_binding_sha256=work.content_sha256,
        idempotency_key_sha256="sha256:" + "2" * 64,
    )


def _intent(
    *,
    session: AgentSession,
    work: WorkReceipt,
    use: str,
    role: str,
    stage: str,
) -> CollaborationEvent:
    return CollaborationEvent(
        event_id=(
            f"event:intent:{session.session_id.removeprefix('agent-session:')}:"
            f"{work.work_item_id.removeprefix('work:')}:{use}"
        ),
        project=work.project,
        coordination_session_id=work.coordination_session_id,
        actor=session.identity,
        event_type="agent.intent_declared",
        summary=f"Intent to enter {use}",
        created_at=_text(NOW - timedelta(seconds=30)),
        work_item_id=work.work_item_id,
        payload={
            "requested_use": use,
            "requested_role": role,
            "workflow_stage": stage,
            "authority_effect": "none",
        },
    )


def _submitter_basis(
    *,
    project_id: str = "project:durable-role",
    coordination_session_id: str = "coord:durable-role",
    agent_id: str = "agent:codex",
    session_suffix: str = "submit",
    work_item_id: str = "work:durable-submit",
) -> RoleAssignmentBasis:
    project = ProjectScope(project_id)
    session = _session(
        project=project,
        coordination_session_id=coordination_session_id,
        agent_id=agent_id,
        suffix=session_suffix,
    )
    work = _work(
        project=project,
        coordination_session_id=coordination_session_id,
        work_item_id=work_item_id,
        assigned=session.identity,
    )
    lease = _lease(work, owner=session.identity)
    return RoleAssignmentBasis(
        session=session,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=session,
            work=work,
            use=RESULT_SUBMISSION_USE,
            role=WORK_SUBMITTER_ROLE,
            stage="implement",
        ),
        workflow_stage="implement",
        work_state="in_progress",
        lease_state="active",
    )


def _reviewer_basis(
    *,
    project_id: str = "project:durable-role",
    coordination_session_id: str = "coord:durable-role",
    reviewer_agent_id: str = "agent:codex",
    submitter_agent_id: str = "agent:peer",
    work_item_id: str = "work:durable-review",
) -> RoleAssignmentBasis:
    project = ProjectScope(project_id)
    reviewer = _session(
        project=project,
        coordination_session_id=coordination_session_id,
        agent_id=reviewer_agent_id,
        suffix="review",
    )
    submitter = _session(
        project=project,
        coordination_session_id=coordination_session_id,
        agent_id=submitter_agent_id,
        suffix="submit",
    )
    work = _work(
        project=project,
        coordination_session_id=coordination_session_id,
        work_item_id=work_item_id,
        assigned=submitter.identity,
    )
    lease = _lease(work, owner=submitter.identity)
    result = ResultReceipt.for_work(
        work,
        receipt_id=f"result:{work_item_id.removeprefix('work:')}",
        submitted_by=submitter.identity,
        outcome="completed",
        summary="Completed by an independent submitter",
        submitted_at=_text(NOW - timedelta(seconds=20)),
        evidence_refs=("evidence:durable-role",),
    )
    return RoleAssignmentBasis(
        session=reviewer,
        work=work,
        lease=lease,
        intent_event=_intent(
            session=reviewer,
            work=work,
            use=ACCEPTANCE_REVIEW_USE,
            role=WORK_REVIEWER_ROLE,
            stage="code-review",
        ),
        workflow_stage="code-review",
        work_state="submitted",
        lease_state="completed",
        result=result,
        submitter_agent_session_id=submitter.session_id,
    )


def _open_repository(
    connection: sqlite3.Connection,
    clock: MutableClock,
) -> tuple[DurableRoleAssignmentRepository, ReentrantWriter]:
    writer = ReentrantWriter(connection)
    repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    return repository, writer


def _install(
    connection: sqlite3.Connection,
    clock: MutableClock,
) -> ReentrantWriter:
    writer = ReentrantWriter(connection)
    install_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        _migration_authority=_MIGRATION_SCHEMA_AUTHORITY,
    )
    return writer


def _issue(authority, use: str, basis: RoleAssignmentBasis):
    return authority.issue(
        use=use,
        agent_session_id=basis.session.session_id,
        work_item_id=basis.work.work_item_id,
        lease_id=basis.lease.lease_id,
        intent_event_id=basis.intent_event.event_id,
    )


def test_constructor_is_read_only_and_requires_explicit_schema_install() -> None:
    connection = sqlite3.connect(":memory:")
    clock = MutableClock()
    writer = ReentrantWriter(connection)

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_durable_schema_missing$",
    ):
        DurableRoleAssignmentRepository(
            connection,
            transaction_factory=writer.transaction,
            clock=clock,
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert not any(name.startswith("collaboration_role_assignment_") for name in tables)

    install_schema(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
        _migration_authority=_MIGRATION_SCHEMA_AUTHORITY,
    )
    repository = DurableRoleAssignmentRepository(
        connection,
        transaction_factory=writer.transaction,
        clock=clock,
    )
    assert isinstance(repository, RoleAssignmentRepository)
    connection.close()


def test_schema_install_requires_migration_authority() -> None:
    connection = sqlite3.connect(":memory:")
    clock = MutableClock()
    writer = ReentrantWriter(connection)

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_schema_install_authority_required$",
    ):
        install_schema(
            connection,
            transaction_factory=writer.transaction,
            clock=clock,
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert not any(name.startswith("collaboration_role_assignment_") for name in tables)
    assert writer.transactions == 0
    connection.close()


def test_exact_loads_and_basis_recover_after_connection_restart(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    clock = MutableClock()
    connection = sqlite3.connect(database)
    install_writer = _install(connection, clock)
    assert install_writer.transactions == 1
    repository, writer = _open_repository(connection, clock)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis()
    repository.register_basis(use=RESULT_SUBMISSION_USE, basis=basis)
    assert writer.transactions == 1

    receipt = _issue(authority, RESULT_SUBMISSION_USE, basis)
    assert writer.transactions == 2

    assert repository.load_by_digest(receipt.assignment_sha256) == receipt
    assert repository.load_by_idempotency(receipt.idempotency_sha256) == receipt
    assert repository.load_binding_state(receipt.assignment_sha256) is not None
    assert repository.load_by_digest("sha256:" + "0" * 64) is None
    assert repository.load_by_idempotency("sha256:" + "0" * 64) is None
    with pytest.raises(RoleAssignmentError, match="^role_assignment_digest_invalid$"):
        repository.load_by_digest(receipt.assignment_sha256[:-1])
    connection.close()

    clock.value = NOW + timedelta(seconds=1)
    restarted_connection = sqlite3.connect(database)
    restarted, _ = _open_repository(restarted_connection, clock)
    restarted_authority = open_server_role_assignment_authority(
        repository=restarted,
        clock=clock,
    )

    assert (
        restarted.resolve_issue_basis(
            use=RESULT_SUBMISSION_USE,
            agent_session_id=basis.session.session_id,
            work_item_id=basis.work.work_item_id,
            lease_id=basis.lease.lease_id,
            intent_event_id=basis.intent_event.event_id,
        )
        == basis
    )
    assert restarted.load_by_digest(receipt.assignment_sha256) == receipt
    verified = restarted_authority.verify_for_use(
        receipt.assignment_sha256,
        use=RESULT_SUBMISSION_USE,
        used_at=_text(clock.value),
    )
    assert verified.assignment_sha256 == receipt.assignment_sha256
    restarted_connection.close()


def test_replay_and_revocation_are_exact_generation_bound_and_restart_safe(tmp_path) -> None:
    database = tmp_path / "canonical.sqlite3"
    clock = MutableClock()
    connection = sqlite3.connect(database)
    _install(connection, clock)
    repository, _ = _open_repository(connection, clock)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    basis = _submitter_basis(work_item_id="work:revoke")
    repository.register_basis(use=RESULT_SUBMISSION_USE, basis=basis)
    receipt = _issue(authority, RESULT_SUBMISSION_USE, basis)

    clock.value = NOW + timedelta(seconds=1)
    replay = _issue(authority, RESULT_SUBMISSION_USE, basis)
    assert replay == receipt
    assert replay.issued_at_utc == _text(NOW)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM collaboration_role_assignment_receipts"
        ).fetchone()[0]
        == 1
    )
    assert (
        len(
            repository.list_active_assignments(
                project_id=basis.work.project.project_id,
                active_at_utc=_text(NOW),
            )
        )
        == 1
    )

    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_binding_generation_mismatch$",
    ):
        authority.revoke(
            receipt.assignment_sha256,
            expected_generation=receipt.binding_generation + 1,
        )
    with pytest.raises(
        RoleAssignmentError,
        match="^role_assignment_repository_write_authority_required$",
    ):
        repository.revoke_exact(
            receipt.assignment_sha256,
            expected_generation=receipt.binding_generation,
            revoked_at_utc=_text(clock.value),
        )

    clock.value = NOW - timedelta(seconds=1)
    with pytest.raises(RoleAssignmentError, match="^role_assignment_revocation_before_issue$"):
        authority.revoke(
            receipt.assignment_sha256,
            expected_generation=receipt.binding_generation,
        )
    clock.value = NOW + timedelta(seconds=1)
    revoked = authority.revoke(
        receipt.assignment_sha256,
        expected_generation=receipt.binding_generation,
    )
    assert revoked.revoked is True
    assert (
        authority.revoke(
            receipt.assignment_sha256,
            expected_generation=receipt.binding_generation,
        )
        == revoked
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM collaboration_role_assignment_revocations"
        ).fetchone()[0]
        == 1
    )
    assert (
        repository.list_active_assignments(
            project_id=basis.work.project.project_id,
            active_at_utc=_text(clock.value),
        )
        == ()
    )
    connection.close()

    restarted_connection = sqlite3.connect(database)
    restarted, _ = _open_repository(restarted_connection, clock)
    restarted_authority = open_server_role_assignment_authority(
        repository=restarted,
        clock=clock,
    )
    assert restarted.load_binding_state(receipt.assignment_sha256) == revoked
    with pytest.raises(RoleAssignmentError, match="^role_assignment_binding_revoked$"):
        restarted_authority.verify_for_use(
            receipt.assignment_sha256,
            use=RESULT_SUBMISSION_USE,
            used_at=_text(clock.value),
        )
    with pytest.raises(RoleAssignmentError, match="^role_assignment_binding_revoked$"):
        _issue(restarted_authority, RESULT_SUBMISSION_USE, basis)
    restarted_connection.close()


def test_active_projection_filters_exact_project_coordination_work_session_and_use() -> None:
    connection = sqlite3.connect(":memory:")
    clock = MutableClock()
    _install(connection, clock)
    repository, _ = _open_repository(connection, clock)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    submitter = _submitter_basis(work_item_id="work:scope-submit")
    reviewer = _reviewer_basis(work_item_id="work:scope-review")
    foreign = _submitter_basis(
        project_id="project:foreign-role",
        coordination_session_id="coord:foreign-role",
        agent_id="agent:foreign",
        work_item_id="work:foreign-role",
    )
    for use, basis in (
        (RESULT_SUBMISSION_USE, submitter),
        (ACCEPTANCE_REVIEW_USE, reviewer),
        (RESULT_SUBMISSION_USE, foreign),
    ):
        repository.register_basis(use=use, basis=basis)
        _issue(authority, use, basis)

    active = repository.list_active_assignments(
        project_id=submitter.work.project.project_id,
        active_at_utc=_text(NOW),
    )
    assert {item.work_item_id for item in active} == {
        submitter.work.work_item_id,
        reviewer.work.work_item_id,
    }
    assert {
        item.work_item_id
        for item in repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            coordination_session_id=submitter.work.coordination_session_id,
            active_at_utc=_text(NOW),
        )
    } == {submitter.work.work_item_id, reviewer.work.work_item_id}
    assert [
        item.work_item_id
        for item in repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            work_item_id=submitter.work.work_item_id,
            active_at_utc=_text(NOW),
        )
    ] == [submitter.work.work_item_id]
    assert [
        item.work_item_id
        for item in repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            agent_session_id=reviewer.session.session_id,
            active_at_utc=_text(NOW),
        )
    ] == [reviewer.work.work_item_id]
    assert [
        item.use
        for item in repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            use=ACCEPTANCE_REVIEW_USE,
            active_at_utc=_text(NOW),
        )
    ] == [ACCEPTANCE_REVIEW_USE]
    assert (
        repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            coordination_session_id="coord:not-this-one",
            active_at_utc=_text(NOW),
        )
        == ()
    )
    assert (
        repository.list_active_assignments(
            project_id=submitter.work.project.project_id,
            active_at_utc=_text(NOW + timedelta(minutes=6)),
        )
        == ()
    )
    projection = active[0].to_dict()
    assert projection["authority_effect"] == "none"
    assert projection["canonical_memory_effect"] == "none"
    assert "receipt_json" not in projection
    connection.close()


def test_snapshots_and_receipts_are_secret_free_canonical_json_and_append_only() -> None:
    connection = sqlite3.connect(":memory:")
    clock = MutableClock()
    _install(connection, clock)
    repository, _ = _open_repository(connection, clock)
    authority = open_server_role_assignment_authority(repository=repository, clock=clock)
    poisoned = replace(
        _reviewer_basis(work_item_id="work:secret-rejection"),
        submitter_agent_session_id="sk-proj-" + "x" * 24,
    )
    with pytest.raises(RoleAssignmentError, match="^role_assignment_secret_value_forbidden$"):
        repository.register_basis(use=ACCEPTANCE_REVIEW_USE, basis=poisoned)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM collaboration_role_assignment_basis_snapshots"
        ).fetchone()[0]
        == 0
    )

    basis = _submitter_basis(work_item_id="work:canonical-json")
    repository.register_basis(use=RESULT_SUBMISSION_USE, basis=basis)
    repository.register_basis(
        use=RESULT_SUBMISSION_USE,
        basis=replace(basis, work_state="leased"),
    )
    receipt = _issue(authority, RESULT_SUBMISSION_USE, replace(basis, work_state="leased"))
    clock.value = NOW + timedelta(seconds=1)
    authority.revoke(
        receipt.assignment_sha256,
        expected_generation=receipt.binding_generation,
    )

    assert (
        connection.execute(
            "SELECT COUNT(*) FROM collaboration_role_assignment_basis_snapshots"
        ).fetchone()[0]
        == 2
    )
    canonical_values = [
        str(value)
        for row in connection.execute(
            """
            SELECT intent_event_json, basis_json
              FROM collaboration_role_assignment_basis_snapshots
            """
        ).fetchall()
        for value in row
    ]
    canonical_values.extend(
        str(row[0])
        for row in connection.execute(
            "SELECT receipt_json FROM collaboration_role_assignment_receipts"
        ).fetchall()
    )
    canonical_values.extend(
        str(row[0])
        for row in connection.execute(
            "SELECT binding_json FROM collaboration_role_assignment_bindings"
        ).fetchall()
    )
    canonical_values.extend(
        str(row[0])
        for row in connection.execute(
            "SELECT revocation_json FROM collaboration_role_assignment_revocations"
        ).fetchall()
    )
    for raw in canonical_values:
        assert raw == json.dumps(
            json.loads(raw),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    serialized = "\n".join(canonical_values).casefold()
    for forbidden in ("api_key", "client_secret", "private_reasoning", "raw_prompt"):
        assert forbidden not in serialized

    with pytest.raises(sqlite3.IntegrityError, match="role_assignment_receipt_append_only"):
        connection.execute(
            """
            UPDATE collaboration_role_assignment_receipts
               SET workflow_stage='review'
             WHERE assignment_sha256=?
            """,
            (receipt.assignment_sha256,),
        )
    connection.rollback()
    connection.close()
