"""Durable SQLite adapter for server-owned work-role assignments.

The adapter is deliberately a deep module behind
``RoleAssignmentRepository``.  Callers supply the canonical server SQLite
connection and its single-writer transaction factory; this module never opens
another database, never grants authority from stored public values, and never
performs an implicit migration during normal construction.

The explicit schema helper stores immutable, canonical JSON for role-intent
basis snapshots and assignment receipts.  Current binding state is kept
separately so one exact generation can be revoked without mutating its receipt,
while an append-only revocation record preserves the state transition.  A
bounded read projection is provided for Dashboard composition, but it is
display evidence only and never substitutes for ``verify_for_use``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .canonical_time import canonical_text, server_now
from .contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
    _secret_value,
)
from .lease_contract import WorkItem, WorkLease
from .role_assignment import (
    _SERVER_AUTHORITY_TOKEN,
    ROLE_ASSIGNMENT_ISSUER,
    RoleAssignmentBasis,
    RoleAssignmentBindingState,
    RoleAssignmentError,
    RoleAssignmentReceipt,
    _binding_generation,
    _digest,
    _identifier,
    _stage,
    _timestamp,
    _use,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION = "collaboration-role-assignment/sqlite-v1"
DURABLE_ROLE_ASSIGNMENT_BASIS_SCHEMA = "collaboration-role-assignment-basis/v1"
DURABLE_ROLE_ASSIGNMENT_REVOCATION_SCHEMA = "collaboration-role-assignment-revocation/v1"
ACTIVE_ROLE_ASSIGNMENT_PROJECTION_SCHEMA = "active-role-assignment-projection/v1"

# Schema installation is a deployment operation, not a repository-construction
# convenience.  The capability is intentionally private and is only held by
# ``deployment.collaboration_schema_migration``.  Runtime adapters remain
# verify-only and ordinary callers cannot create canonical role tables by
# invoking a public repository method.
_MIGRATION_SCHEMA_AUTHORITY = object()

DEFAULT_ACTIVE_ASSIGNMENT_LIMIT = 100
MAX_ACTIVE_ASSIGNMENT_LIMIT = 500

_SCHEMA_TABLE = "collaboration_role_assignment_schema"
_BASIS_SNAPSHOT_TABLE = "collaboration_role_assignment_basis_snapshots"
_BASIS_CURRENT_TABLE = "collaboration_role_assignment_basis_current"
_RECEIPT_TABLE = "collaboration_role_assignment_receipts"
_BINDING_TABLE = "collaboration_role_assignment_bindings"
_REVOCATION_TABLE = "collaboration_role_assignment_revocations"
_BASIS_WORK_STATES = frozenset({"leased", "in_progress", "reviewing", "submitted"})
_BASIS_LEASE_STATES = frozenset({"active", "completed"})


@dataclass(frozen=True, slots=True)
class ActiveRoleAssignmentProjection:
    """Bounded, non-authoritative assignment row for Dashboard readers."""

    assignment_sha256: str
    assignment_id: str
    project_id: str
    coordination_session_id: str
    work_item_id: str
    agent_session_id: str
    agent_id: str
    assignment_role: str
    use: str
    workflow_stage: str
    binding_generation: int
    issued_at_utc: str
    expires_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVE_ROLE_ASSIGNMENT_PROJECTION_SCHEMA,
            "assignment_sha256": self.assignment_sha256,
            "assignment_id": self.assignment_id,
            "project_id": self.project_id,
            "coordination_session_id": self.coordination_session_id,
            "work_item_id": self.work_item_id,
            "agent_session_id": self.agent_session_id,
            "agent_id": self.agent_id,
            "assignment_role": self.assignment_role,
            "use": self.use,
            "workflow_stage": self.workflow_stage,
            "binding_generation": self.binding_generation,
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "state": "active",
            "authority_effect": "none",
            "canonical_memory_effect": "none",
            "verification": "server-role-assignment-authority-required",
        }


class DurableRoleAssignmentRepository:
    """Canonical SQLite adapter satisfying ``RoleAssignmentRepository``.

    ``connection`` must be the already-open server-owned canonical connection.
    Every mutation runs through ``transaction_factory`` so the adapter shares
    the server's lock and transaction ownership.  Construction is read-only
    and fails closed until :meth:`install_schema` has run under the migration
    orchestrator.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_connection(connection)
        _require_transaction_factory(transaction_factory)
        if clock is not None and not callable(clock):
            raise RoleAssignmentError("role_assignment_clock_invalid")
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verify_schema()

    @classmethod
    def install_schema(
        cls,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] | None = None,
        _migration_authority: object | None = None,
    ) -> None:
        """Install the additive tables inside the migration transaction.

        ``_migration_authority`` is deliberately not a caller-selectable
        policy value.  It is an opaque capability held by the deployment
        schema orchestrator; without it this method is verify-only and fails
        closed.  Keeping the guard here prevents a direct repository caller
        from bypassing backup/grant/fence checks in the migration layer.
        """

        if _migration_authority is not _MIGRATION_SCHEMA_AUTHORITY:
            raise RoleAssignmentError("role_assignment_schema_install_authority_required")
        _require_connection(connection)
        _require_transaction_factory(transaction_factory)
        if clock is not None and not callable(clock):
            raise RoleAssignmentError("role_assignment_clock_invalid")
        installer = object.__new__(cls)
        installer._connection = connection
        installer._transaction_factory = transaction_factory
        installer._clock = clock or (lambda: datetime.now(timezone.utc))
        with transaction_factory():
            installer._ensure_schema()
            installer._verify_schema_structure(marker_required=False)
            installed_at = installer._now_text()
            connection.execute(
                f"""
                INSERT INTO {_SCHEMA_TABLE} (singleton, schema_revision, installed_at)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    schema_revision=excluded.schema_revision,
                    installed_at=excluded.installed_at
                """,
                (DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION, installed_at),
            )
            installer._verify_schema()

    def register_basis(self, *, use: str, basis: RoleAssignmentBasis) -> None:
        """Persist one current role-intent basis and retain immutable revisions."""

        if not isinstance(basis, RoleAssignmentBasis):
            raise RoleAssignmentError("role_assignment_basis_invalid")
        normalized_use = _use(use)
        basis_key = _basis_key(
            normalized_use,
            basis.session.session_id,
            basis.work.work_item_id,
            basis.lease.lease_id,
            basis.intent_event.event_id,
        )
        basis_payload = _basis_to_dict(normalized_use, basis)
        basis_json = _canonical_json(basis_payload)
        basis_sha256 = _sha256(basis_payload)
        intent_event_json = _canonical_json(basis.intent_event.to_dict())
        registered_at = self._now_text()
        natural_key = (
            normalized_use,
            basis.session.session_id,
            basis.work.work_item_id,
            basis.lease.lease_id,
            basis.intent_event.event_id,
        )
        with self._write():
            current = self._fetchone(
                f"SELECT * FROM {_BASIS_CURRENT_TABLE} WHERE basis_key_sha256=?",
                (basis_key,),
            )
            if current is not None and _basis_natural_key(current) != natural_key:
                raise RoleAssignmentError("role_assignment_replay_conflict")
            self._connection.execute(
                f"""
                INSERT OR IGNORE INTO {_BASIS_SNAPSHOT_TABLE} (
                    basis_key_sha256, basis_sha256, use, project_id,
                    coordination_session_id, agent_session_id, work_item_id,
                    lease_id, intent_event_id, intent_event_sha256,
                    intent_event_json, basis_json, registered_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    basis_key,
                    basis_sha256,
                    normalized_use,
                    basis.work.project.project_id,
                    basis.work.coordination_session_id,
                    basis.session.session_id,
                    basis.work.work_item_id,
                    basis.lease.lease_id,
                    basis.intent_event.event_id,
                    basis.intent_event.content_sha256,
                    intent_event_json,
                    basis_json,
                    registered_at,
                ),
            )
            snapshot = self._fetchone(
                f"""
                SELECT * FROM {_BASIS_SNAPSHOT_TABLE}
                 WHERE basis_key_sha256=? AND basis_sha256=?
                """,
                (basis_key, basis_sha256),
            )
            if snapshot is None:
                raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
            if (
                str(snapshot["basis_json"]) != basis_json
                or str(snapshot["intent_event_json"]) != intent_event_json
            ):
                raise RoleAssignmentError("role_assignment_replay_conflict")
            self._connection.execute(
                f"""
                INSERT INTO {_BASIS_CURRENT_TABLE} (
                    basis_key_sha256, use, project_id, coordination_session_id,
                    agent_session_id, work_item_id, lease_id, intent_event_id,
                    snapshot_id, basis_sha256, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(basis_key_sha256) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,
                    basis_sha256=excluded.basis_sha256,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    basis_key,
                    normalized_use,
                    basis.work.project.project_id,
                    basis.work.coordination_session_id,
                    basis.session.session_id,
                    basis.work.work_item_id,
                    basis.lease.lease_id,
                    basis.intent_event.event_id,
                    int(snapshot["snapshot_id"]),
                    basis_sha256,
                    registered_at,
                ),
            )

    def resolve_issue_basis(
        self,
        *,
        use: str,
        agent_session_id: str,
        work_item_id: str,
        lease_id: str,
        intent_event_id: str,
    ) -> RoleAssignmentBasis:
        normalized_use = _use(use)
        normalized_session = _identifier(
            agent_session_id,
            "role_assignment_agent_session_id_invalid",
        )
        normalized_work = _identifier(work_item_id, "role_assignment_work_item_invalid")
        normalized_lease = _identifier(lease_id, "role_assignment_lease_id_invalid")
        normalized_event = _identifier(
            intent_event_id,
            "role_assignment_intent_event_id_invalid",
        )
        row = self._fetchone(
            f"""
            SELECT snapshots.*
              FROM {_BASIS_CURRENT_TABLE} AS current
              JOIN {_BASIS_SNAPSHOT_TABLE} AS snapshots
                ON snapshots.snapshot_id = current.snapshot_id
             WHERE current.use=?
               AND current.agent_session_id=?
               AND current.work_item_id=?
               AND current.lease_id=?
               AND current.intent_event_id=?
            """,
            (
                normalized_use,
                normalized_session,
                normalized_work,
                normalized_lease,
                normalized_event,
            ),
        )
        if row is None:
            raise RoleAssignmentError("role_assignment_basis_not_found")
        return _basis_from_row(row)

    def append_exact(
        self,
        receipt: RoleAssignmentReceipt,
        *,
        server_basis_sha256: str | None = None,
        _authority_token: object | None = None,
    ) -> RoleAssignmentReceipt:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise RoleAssignmentError("role_assignment_repository_write_authority_required")
        if not isinstance(receipt, RoleAssignmentReceipt):
            raise RoleAssignmentError("role_assignment_receipt_invalid")
        basis_digest = _digest(
            server_basis_sha256,
            "role_assignment_server_basis_digest_invalid",
        )
        with self._write():
            existing = self._load_receipt_by(
                "idempotency_sha256",
                receipt.idempotency_sha256,
            )
            if existing is not None:
                self._require_existing_binding(existing, basis_digest)
                return existing
            existing = self._load_receipt_by("assignment_id", receipt.assignment_id)
            if existing is not None:
                if hmac.compare_digest(existing.assignment_sha256, receipt.assignment_sha256):
                    self._require_existing_binding(existing, basis_digest)
                    return existing
                raise RoleAssignmentError("role_assignment_replay_conflict")
            existing = self._load_receipt_by(
                "assignment_sha256",
                receipt.assignment_sha256,
            )
            if existing is not None:
                self._require_existing_binding(existing, basis_digest)
                return existing

            receipt_json = _canonical_json(receipt.to_dict())
            state = RoleAssignmentBindingState(
                assignment_sha256=receipt.assignment_sha256,
                server_basis_sha256=basis_digest,
                binding_generation=receipt.binding_generation,
            )
            binding_json = _canonical_json(state.to_dict())
            now_text = self._now_text()
            try:
                self._connection.execute(
                    f"""
                    INSERT INTO {_RECEIPT_TABLE} (
                        assignment_sha256, assignment_id, idempotency_sha256,
                        project_id, coordination_session_id, work_item_id,
                        agent_session_id, agent_id, assignment_role, use,
                        workflow_stage, binding_generation, issued_at_utc,
                        expires_at_utc, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.assignment_sha256,
                        receipt.assignment_id,
                        receipt.idempotency_sha256,
                        receipt.project.project_id,
                        receipt.coordination_session_id,
                        receipt.work_item_id,
                        receipt.agent_session_id,
                        receipt.agent_id,
                        receipt.assignment_role,
                        receipt.use,
                        receipt.workflow_stage,
                        receipt.binding_generation,
                        receipt.issued_at_utc,
                        receipt.expires_at_utc,
                        receipt_json,
                    ),
                )
                self._connection.execute(
                    f"""
                    INSERT INTO {_BINDING_TABLE} (
                        assignment_sha256, server_basis_sha256,
                        binding_generation, revoked_at_utc, binding_json,
                        binding_sha256, updated_at_utc
                    ) VALUES (?, ?, ?, '', ?, ?, ?)
                    """,
                    (
                        state.assignment_sha256,
                        state.server_basis_sha256,
                        state.binding_generation,
                        binding_json,
                        _sha256(state.to_dict()),
                        now_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                winner = self._load_receipt_by(
                    "idempotency_sha256",
                    receipt.idempotency_sha256,
                )
                if winner is None:
                    winner = self._load_receipt_by("assignment_id", receipt.assignment_id)
                if winner is None:
                    winner = self._load_receipt_by(
                        "assignment_sha256",
                        receipt.assignment_sha256,
                    )
                if winner is None:
                    raise RoleAssignmentError("role_assignment_replay_conflict") from exc
                self._require_existing_binding(winner, basis_digest)
                return winner
            return receipt

    def load_by_digest(self, assignment_sha256: str) -> RoleAssignmentReceipt | None:
        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        receipt = self._load_receipt_by("assignment_sha256", digest)
        if receipt is not None and not hmac.compare_digest(receipt.assignment_sha256, digest):
            raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
        return receipt

    def load_by_idempotency(
        self,
        idempotency_sha256: str,
    ) -> RoleAssignmentReceipt | None:
        digest = _digest(
            idempotency_sha256,
            "role_assignment_idempotency_digest_invalid",
        )
        receipt = self._load_receipt_by("idempotency_sha256", digest)
        if receipt is not None and not hmac.compare_digest(receipt.idempotency_sha256, digest):
            raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
        return receipt

    def load_binding_state(
        self,
        assignment_sha256: str,
    ) -> RoleAssignmentBindingState | None:
        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_BINDING_TABLE} WHERE assignment_sha256=?",
            (digest,),
        )
        if row is None:
            return None
        state = _binding_from_row(row)
        if not hmac.compare_digest(state.assignment_sha256, digest):
            raise RoleAssignmentError("role_assignment_binding_digest_mismatch")
        return state

    def revoke_exact(
        self,
        assignment_sha256: str,
        *,
        expected_generation: int,
        revoked_at_utc: str,
        _authority_token: object | None = None,
    ) -> RoleAssignmentBindingState:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise RoleAssignmentError("role_assignment_repository_write_authority_required")
        digest = _digest(assignment_sha256, "role_assignment_digest_invalid")
        generation = _binding_generation(expected_generation)
        revoked_at = _timestamp(revoked_at_utc, "role_assignment_revoked_at_invalid")
        with self._write():
            receipt = self._load_receipt_by("assignment_sha256", digest)
            if receipt is None:
                raise RoleAssignmentError("role_assignment_not_server_issued")
            state = self.load_binding_state(digest)
            if state is None:
                raise RoleAssignmentError("role_assignment_binding_state_missing")
            if state.binding_generation != generation:
                raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
            if state.revoked:
                return state
            if revoked_at < receipt.issued_at_utc:
                raise RoleAssignmentError("role_assignment_revocation_before_issue")
            revoked = RoleAssignmentBindingState(
                assignment_sha256=state.assignment_sha256,
                server_basis_sha256=state.server_basis_sha256,
                binding_generation=state.binding_generation,
                revoked_at_utc=revoked_at,
            )
            revocation_payload = {
                "schema_version": DURABLE_ROLE_ASSIGNMENT_REVOCATION_SCHEMA,
                "issuer": ROLE_ASSIGNMENT_ISSUER,
                "assignment_sha256": revoked.assignment_sha256,
                "binding_generation": revoked.binding_generation,
                "revoked_at_utc": revoked.revoked_at_utc,
                "authority_effect": "binding-revoked",
                "canonical_memory_effect": "none",
            }
            revocation_json = _canonical_json(revocation_payload)
            binding_json = _canonical_json(revoked.to_dict())
            cursor = self._connection.execute(
                f"""
                UPDATE {_BINDING_TABLE}
                   SET revoked_at_utc=?, binding_json=?, binding_sha256=?,
                       updated_at_utc=?
                 WHERE assignment_sha256=?
                   AND binding_generation=?
                   AND revoked_at_utc=''
                """,
                (
                    revoked.revoked_at_utc,
                    binding_json,
                    _sha256(revoked.to_dict()),
                    revoked.revoked_at_utc,
                    digest,
                    generation,
                ),
            )
            if cursor.rowcount != 1:
                current = self.load_binding_state(digest)
                if current is None:
                    raise RoleAssignmentError("role_assignment_binding_state_missing")
                if current.binding_generation != generation:
                    raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
                if current.revoked:
                    return current
                raise RoleAssignmentError("role_assignment_revocation_not_persisted")
            try:
                self._connection.execute(
                    f"""
                    INSERT INTO {_REVOCATION_TABLE} (
                        assignment_sha256, binding_generation, revoked_at_utc,
                        revocation_json, revocation_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        generation,
                        revoked.revoked_at_utc,
                        revocation_json,
                        _sha256(revocation_payload),
                        revoked.revoked_at_utc,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self._fetchone(
                    f"""
                    SELECT revocation_json FROM {_REVOCATION_TABLE}
                     WHERE assignment_sha256=? AND binding_generation=?
                    """,
                    (digest, generation),
                )
                if existing is None or str(existing["revocation_json"]) != revocation_json:
                    raise RoleAssignmentError("role_assignment_replay_conflict") from exc
            return revoked

    def list_active_assignments(
        self,
        *,
        project_id: str,
        coordination_session_id: str | None = None,
        work_item_id: str | None = None,
        agent_session_id: str | None = None,
        use: str | None = None,
        active_at_utc: str | None = None,
        limit: int = DEFAULT_ACTIVE_ASSIGNMENT_LIMIT,
    ) -> tuple[ActiveRoleAssignmentProjection, ...]:
        """Return an exact-scope, bounded display projection of active bindings."""

        try:
            project = ProjectScope(project_id).project_id
        except ValueError as exc:
            raise RoleAssignmentError("role_assignment_project_invalid") from exc
        normalized_coordination = _optional_identifier(
            coordination_session_id,
            "role_assignment_session_scope_invalid",
        )
        normalized_work = _optional_identifier(
            work_item_id,
            "role_assignment_work_item_invalid",
        )
        normalized_session = _optional_identifier(
            agent_session_id,
            "role_assignment_agent_session_id_invalid",
        )
        normalized_use = None if use is None else _use(use)
        at = (
            self._now_text()
            if active_at_utc is None
            else _timestamp(
                active_at_utc,
                "role_assignment_use_time_invalid",
            )
        )
        bounded_limit = _active_limit(limit)
        clauses = [
            "receipts.project_id=?",
            "bindings.revoked_at_utc=''",
            "receipts.issued_at_utc<=?",
            "receipts.expires_at_utc>?",
            "bindings.binding_generation=receipts.binding_generation",
        ]
        parameters: list[object] = [project, at, at]
        for column, value in (
            ("receipts.coordination_session_id", normalized_coordination),
            ("receipts.work_item_id", normalized_work),
            ("receipts.agent_session_id", normalized_session),
            ("receipts.use", normalized_use),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        parameters.append(bounded_limit)
        rows = self._fetchall(
            f"""
            SELECT receipts.*, bindings.server_basis_sha256,
                   bindings.revoked_at_utc, bindings.binding_json,
                   bindings.binding_sha256, bindings.updated_at_utc
              FROM {_RECEIPT_TABLE} AS receipts
              JOIN {_BINDING_TABLE} AS bindings
                ON bindings.assignment_sha256 = receipts.assignment_sha256
             WHERE {" AND ".join(clauses)}
             ORDER BY receipts.expires_at_utc ASC,
                      receipts.assignment_id ASC
             LIMIT ?
            """,
            tuple(parameters),
        )
        projections: list[ActiveRoleAssignmentProjection] = []
        for row in rows:
            receipt = _receipt_from_row(row)
            state = _binding_from_row(row)
            if state.revoked or state.binding_generation != receipt.binding_generation:
                raise RoleAssignmentError("role_assignment_binding_state_invalid")
            projections.append(
                ActiveRoleAssignmentProjection(
                    assignment_sha256=receipt.assignment_sha256,
                    assignment_id=receipt.assignment_id,
                    project_id=receipt.project.project_id,
                    coordination_session_id=receipt.coordination_session_id,
                    work_item_id=receipt.work_item_id,
                    agent_session_id=receipt.agent_session_id,
                    agent_id=receipt.agent_id,
                    assignment_role=receipt.assignment_role,
                    use=receipt.use,
                    workflow_stage=receipt.workflow_stage,
                    binding_generation=receipt.binding_generation,
                    issued_at_utc=receipt.issued_at_utc,
                    expires_at_utc=receipt.expires_at_utc,
                )
            )
        return tuple(projections)

    def _write(self) -> AbstractContextManager[None]:
        return self._transaction_factory()

    def _now_text(self) -> str:
        try:
            return canonical_text(server_now(self._clock))
        except (TypeError, ValueError) as exc:
            raise RoleAssignmentError("role_assignment_clock_invalid") from exc

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

    def _load_receipt_by(self, column: str, value: str) -> RoleAssignmentReceipt | None:
        if column not in {"assignment_sha256", "assignment_id", "idempotency_sha256"}:
            raise RoleAssignmentError("role_assignment_repository_lookup_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_RECEIPT_TABLE} WHERE {column}=?",
            (value,),
        )
        if row is None:
            return None
        receipt = _receipt_from_row(row)
        if not hmac.compare_digest(str(row[column]), value):
            raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
        return receipt

    def _require_existing_binding(
        self,
        receipt: RoleAssignmentReceipt,
        server_basis_sha256: str,
    ) -> None:
        state = self.load_binding_state(receipt.assignment_sha256)
        if state is None:
            raise RoleAssignmentError("role_assignment_binding_state_missing")
        if not hmac.compare_digest(state.server_basis_sha256, server_basis_sha256):
            raise RoleAssignmentError("role_assignment_replay_conflict")
        if state.binding_generation != receipt.binding_generation:
            raise RoleAssignmentError("role_assignment_binding_generation_mismatch")
        if state.revoked:
            raise RoleAssignmentError("role_assignment_binding_revoked")

    def _ensure_schema(self) -> None:
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA_TABLE} (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_revision TEXT NOT NULL,
                installed_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {_BASIS_SNAPSHOT_TABLE} (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                basis_key_sha256 TEXT NOT NULL,
                basis_sha256 TEXT NOT NULL,
                use TEXT NOT NULL,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                agent_session_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                intent_event_id TEXT NOT NULL,
                intent_event_sha256 TEXT NOT NULL,
                intent_event_json TEXT NOT NULL,
                basis_json TEXT NOT NULL,
                registered_at_utc TEXT NOT NULL,
                UNIQUE(basis_key_sha256, basis_sha256)
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_role_assignment_basis_scope
                ON {_BASIS_SNAPSHOT_TABLE}(
                    project_id, coordination_session_id, work_item_id,
                    agent_session_id, use
                )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {_BASIS_CURRENT_TABLE} (
                basis_key_sha256 TEXT PRIMARY KEY,
                use TEXT NOT NULL,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                agent_session_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                intent_event_id TEXT NOT NULL,
                snapshot_id INTEGER NOT NULL,
                basis_sha256 TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(use, agent_session_id, work_item_id, lease_id, intent_event_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {_RECEIPT_TABLE} (
                assignment_sha256 TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL UNIQUE,
                idempotency_sha256 TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                coordination_session_id TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                agent_session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                assignment_role TEXT NOT NULL,
                use TEXT NOT NULL,
                workflow_stage TEXT NOT NULL,
                binding_generation INTEGER NOT NULL,
                issued_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                receipt_json TEXT NOT NULL
            )
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_role_assignment_active_scope
                ON {_RECEIPT_TABLE}(
                    project_id, coordination_session_id, work_item_id,
                    agent_session_id, use, expires_at_utc
                )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {_BINDING_TABLE} (
                assignment_sha256 TEXT PRIMARY KEY,
                server_basis_sha256 TEXT NOT NULL,
                binding_generation INTEGER NOT NULL,
                revoked_at_utc TEXT NOT NULL DEFAULT '',
                binding_json TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {_REVOCATION_TABLE} (
                assignment_sha256 TEXT NOT NULL,
                binding_generation INTEGER NOT NULL,
                revoked_at_utc TEXT NOT NULL,
                revocation_json TEXT NOT NULL,
                revocation_sha256 TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(assignment_sha256, binding_generation)
            )
            """,
            _append_only_trigger(_BASIS_SNAPSHOT_TABLE, "basis_snapshot"),
            _no_delete_trigger(_BASIS_SNAPSHOT_TABLE, "basis_snapshot"),
            _append_only_trigger(_RECEIPT_TABLE, "receipt"),
            _no_delete_trigger(_RECEIPT_TABLE, "receipt"),
            _append_only_trigger(_REVOCATION_TABLE, "revocation"),
            _no_delete_trigger(_REVOCATION_TABLE, "revocation"),
            f"""
            CREATE TRIGGER IF NOT EXISTS role_assignment_bindings_immutable_identity
            BEFORE UPDATE ON {_BINDING_TABLE}
            WHEN NEW.assignment_sha256 != OLD.assignment_sha256
              OR NEW.server_basis_sha256 != OLD.server_basis_sha256
              OR NEW.binding_generation != OLD.binding_generation
              OR (OLD.revoked_at_utc != '' AND NEW.revoked_at_utc != OLD.revoked_at_utc)
            BEGIN
                SELECT RAISE(ABORT, 'role_assignment_binding_identity_immutable');
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS role_assignment_bindings_no_delete
            BEFORE DELETE ON {_BINDING_TABLE}
            BEGIN
                SELECT RAISE(ABORT, 'role_assignment_binding_delete_forbidden');
            END
            """,
        )
        for statement in statements:
            self._connection.execute(statement)

    def _verify_schema(self) -> None:
        self._verify_schema_structure(marker_required=True)
        marker = self._fetchone(f"SELECT schema_revision FROM {_SCHEMA_TABLE} WHERE singleton=1")
        if marker is None:
            raise RoleAssignmentError("role_assignment_durable_schema_missing")
        if str(marker["schema_revision"]) != DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION:
            raise RoleAssignmentError("role_assignment_durable_schema_stale")

    def _verify_schema_structure(self, *, marker_required: bool) -> None:
        required_columns = {
            _SCHEMA_TABLE: {"singleton", "schema_revision", "installed_at"},
            _BASIS_SNAPSHOT_TABLE: {
                "snapshot_id",
                "basis_key_sha256",
                "basis_sha256",
                "use",
                "project_id",
                "coordination_session_id",
                "agent_session_id",
                "work_item_id",
                "lease_id",
                "intent_event_id",
                "intent_event_sha256",
                "intent_event_json",
                "basis_json",
                "registered_at_utc",
            },
            _BASIS_CURRENT_TABLE: {
                "basis_key_sha256",
                "use",
                "project_id",
                "coordination_session_id",
                "agent_session_id",
                "work_item_id",
                "lease_id",
                "intent_event_id",
                "snapshot_id",
                "basis_sha256",
                "updated_at_utc",
            },
            _RECEIPT_TABLE: {
                "assignment_sha256",
                "assignment_id",
                "idempotency_sha256",
                "project_id",
                "coordination_session_id",
                "work_item_id",
                "agent_session_id",
                "agent_id",
                "assignment_role",
                "use",
                "workflow_stage",
                "binding_generation",
                "issued_at_utc",
                "expires_at_utc",
                "receipt_json",
            },
            _BINDING_TABLE: {
                "assignment_sha256",
                "server_basis_sha256",
                "binding_generation",
                "revoked_at_utc",
                "binding_json",
                "binding_sha256",
                "updated_at_utc",
            },
            _REVOCATION_TABLE: {
                "assignment_sha256",
                "binding_generation",
                "revoked_at_utc",
                "revocation_json",
                "revocation_sha256",
                "created_at_utc",
            },
        }
        existing = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not set(required_columns).issubset(existing):
            code = (
                "role_assignment_durable_schema_missing"
                if marker_required
                else "role_assignment_durable_schema_stale"
            )
            raise RoleAssignmentError(code)
        for table, required in required_columns.items():
            columns = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required.issubset(columns):
                raise RoleAssignmentError("role_assignment_durable_schema_stale")


def install_schema(
    connection: sqlite3.Connection,
    *,
    transaction_factory: Callable[[], AbstractContextManager[None]],
    clock: Callable[[], datetime] | None = None,
    _migration_authority: object | None = None,
) -> None:
    """Migration-orchestrator-only helper for the durable role schema."""

    DurableRoleAssignmentRepository.install_schema(
        connection,
        transaction_factory=transaction_factory,
        clock=clock,
        _migration_authority=_migration_authority,
    )


def _append_only_trigger(table: str, label: str) -> str:
    return f"""
        CREATE TRIGGER IF NOT EXISTS role_assignment_{label}_no_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, 'role_assignment_{label}_append_only');
        END
    """


def _no_delete_trigger(table: str, label: str) -> str:
    return f"""
        CREATE TRIGGER IF NOT EXISTS role_assignment_{label}_no_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, 'role_assignment_{label}_append_only');
        END
    """


def _require_connection(connection: object) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise RoleAssignmentError("role_assignment_durable_connection_invalid")


def _require_transaction_factory(value: object) -> None:
    if not callable(value):
        raise RoleAssignmentError("role_assignment_durable_writer_required")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _basis_key(
    use: str,
    agent_session_id: str,
    work_item_id: str,
    lease_id: str,
    intent_event_id: str,
) -> str:
    return _sha256(
        {
            "schema_version": "collaboration-role-assignment-basis-key/v1",
            "use": use,
            "agent_session_id": agent_session_id,
            "work_item_id": work_item_id,
            "lease_id": lease_id,
            "intent_event_id": intent_event_id,
        }
    )


def _basis_natural_key(row: Mapping[str, object]) -> tuple[str, str, str, str, str]:
    return (
        str(row["use"]),
        str(row["agent_session_id"]),
        str(row["work_item_id"]),
        str(row["lease_id"]),
        str(row["intent_event_id"]),
    )


def _basis_to_dict(use: str, basis: RoleAssignmentBasis) -> dict[str, object]:
    workflow_stage = _stage(basis.workflow_stage)
    work_state = _basis_state(basis.work_state, _BASIS_WORK_STATES)
    lease_state = _basis_state(basis.lease_state, _BASIS_LEASE_STATES)
    submitter_agent_session_id = str(basis.submitter_agent_session_id or "").strip()
    if submitter_agent_session_id:
        submitter_agent_session_id = _identifier(
            submitter_agent_session_id,
            "role_assignment_submitter_session_id_invalid",
        )
        if _secret_value(submitter_agent_session_id):
            raise RoleAssignmentError("role_assignment_secret_value_forbidden")
    return {
        "schema_version": DURABLE_ROLE_ASSIGNMENT_BASIS_SCHEMA,
        "use": use,
        "session": basis.session.to_dict(),
        "work": basis.work.to_dict(),
        "lease": basis.lease.to_dict(),
        "intent_event": basis.intent_event.to_dict(),
        "workflow_stage": workflow_stage,
        "work_state": work_state,
        "lease_state": lease_state,
        "result": basis.result.to_dict() if basis.result is not None else None,
        "submitter_agent_session_id": submitter_agent_session_id,
        "authority_effect": "none",
        "canonical_memory_effect": "none",
    }


def _basis_from_row(row: Mapping[str, object]) -> RoleAssignmentBasis:
    basis_json = str(row["basis_json"])
    payload = _canonical_mapping(basis_json, "role_assignment_basis_snapshot_invalid")
    try:
        basis = _basis_from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid") from exc
    normalized_use = _use(row["use"])
    if _canonical_json(_basis_to_dict(normalized_use, basis)) != basis_json:
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
    if not hmac.compare_digest(_sha256(payload), str(row["basis_sha256"])):
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
    if _basis_natural_key(row) != (
        normalized_use,
        basis.session.session_id,
        basis.work.work_item_id,
        basis.lease.lease_id,
        basis.intent_event.event_id,
    ):
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
    intent_json = str(row["intent_event_json"])
    if intent_json != _canonical_json(basis.intent_event.to_dict()):
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
    if not hmac.compare_digest(
        str(row["intent_event_sha256"]),
        basis.intent_event.content_sha256,
    ):
        raise RoleAssignmentError("role_assignment_basis_snapshot_invalid")
    return basis


def _basis_from_dict(value: Mapping[str, object]) -> RoleAssignmentBasis:
    session = _session_from_dict(_mapping(value.get("session")))
    work = _work_receipt_from_dict(_mapping(value.get("work")))
    lease = _lease_from_dict(_mapping(value.get("lease")))
    intent = CollaborationEvent.from_dict(_mapping(value.get("intent_event")))
    raw_result = value.get("result")
    result = None if raw_result is None else _result_from_dict(_mapping(raw_result))
    return RoleAssignmentBasis(
        session=session,
        work=work,
        lease=lease,
        intent_event=intent,
        workflow_stage=value.get("workflow_stage"),  # type: ignore[arg-type]
        work_state=value.get("work_state"),  # type: ignore[arg-type]
        lease_state=value.get("lease_state"),  # type: ignore[arg-type]
        result=result,
        submitter_agent_session_id=value.get("submitter_agent_session_id"),  # type: ignore[arg-type]
    )


def _receipt_from_row(row: Mapping[str, object]) -> RoleAssignmentReceipt:
    receipt_json = str(row["receipt_json"])
    payload = _canonical_mapping(receipt_json, "role_assignment_receipt_invalid")
    try:
        receipt = RoleAssignmentReceipt(
            assignment_id=payload.get("assignment_id"),  # type: ignore[arg-type]
            project=ProjectScope(payload.get("project_id")),  # type: ignore[arg-type]
            coordination_session_id=payload.get("coordination_session_id"),  # type: ignore[arg-type]
            work_item_id=payload.get("work_item_id"),  # type: ignore[arg-type]
            work_receipt_sha256=payload.get("work_receipt_sha256"),  # type: ignore[arg-type]
            lease_id=payload.get("lease_id"),  # type: ignore[arg-type]
            lease_sha256=payload.get("lease_sha256"),  # type: ignore[arg-type]
            fencing_generation=payload.get("fencing_generation"),  # type: ignore[arg-type]
            agent_session_id=payload.get("agent_session_id"),  # type: ignore[arg-type]
            agent_session_sha256=payload.get("agent_session_sha256"),  # type: ignore[arg-type]
            agent_id=payload.get("agent_id"),  # type: ignore[arg-type]
            assignment_role=payload.get("assignment_role"),  # type: ignore[arg-type]
            use=payload.get("use"),  # type: ignore[arg-type]
            workflow_stage=payload.get("workflow_stage"),  # type: ignore[arg-type]
            intent_event_id=payload.get("intent_event_id"),  # type: ignore[arg-type]
            intent_event_sha256=payload.get("intent_event_sha256"),  # type: ignore[arg-type]
            result_receipt_sha256=payload.get("result_receipt_sha256"),  # type: ignore[arg-type]
            assignment_policy_revision=payload.get("assignment_policy_revision"),  # type: ignore[arg-type]
            issued_at_utc=payload.get("issued_at_utc"),  # type: ignore[arg-type]
            expires_at_utc=payload.get("expires_at_utc"),  # type: ignore[arg-type]
            idempotency_sha256=payload.get("idempotency_sha256"),  # type: ignore[arg-type]
            binding_generation=payload.get("binding_generation"),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise RoleAssignmentError("role_assignment_receipt_invalid") from exc
    if _canonical_json(receipt.to_dict()) != receipt_json:
        raise RoleAssignmentError("role_assignment_receipt_invalid")
    column_checks = (
        (receipt.assignment_sha256, row["assignment_sha256"]),
        (receipt.assignment_id, row["assignment_id"]),
        (receipt.idempotency_sha256, row["idempotency_sha256"]),
        (receipt.project.project_id, row["project_id"]),
        (receipt.coordination_session_id, row["coordination_session_id"]),
        (receipt.work_item_id, row["work_item_id"]),
        (receipt.agent_session_id, row["agent_session_id"]),
        (receipt.agent_id, row["agent_id"]),
        (receipt.assignment_role, row["assignment_role"]),
        (receipt.use, row["use"]),
        (receipt.workflow_stage, row["workflow_stage"]),
        (receipt.binding_generation, row["binding_generation"]),
        (receipt.issued_at_utc, row["issued_at_utc"]),
        (receipt.expires_at_utc, row["expires_at_utc"]),
    )
    if any(str(expected) != str(observed) for expected, observed in column_checks):
        raise RoleAssignmentError("role_assignment_repository_lookup_mismatch")
    return receipt


def _binding_from_row(row: Mapping[str, object]) -> RoleAssignmentBindingState:
    try:
        state = RoleAssignmentBindingState(
            assignment_sha256=row["assignment_sha256"],  # type: ignore[arg-type]
            server_basis_sha256=row["server_basis_sha256"],  # type: ignore[arg-type]
            binding_generation=row["binding_generation"],  # type: ignore[arg-type]
            revoked_at_utc=row["revoked_at_utc"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise RoleAssignmentError("role_assignment_binding_state_invalid") from exc
    binding_json = str(row["binding_json"])
    payload = _canonical_mapping(binding_json, "role_assignment_binding_state_invalid")
    if _canonical_json(state.to_dict()) != binding_json:
        raise RoleAssignmentError("role_assignment_binding_state_invalid")
    if not hmac.compare_digest(_sha256(payload), str(row["binding_sha256"])):
        raise RoleAssignmentError("role_assignment_binding_state_invalid")
    return state


def _identity_from_dict(value: Mapping[str, object]) -> AgentIdentity:
    return AgentIdentity(
        agent_id=value.get("agent_id"),  # type: ignore[arg-type]
        role=value.get("role"),  # type: ignore[arg-type]
        parent_agent_id=value.get("parent_agent_id"),  # type: ignore[arg-type]
        capabilities=_string_tuple(value.get("capabilities")),
    )


def _session_from_dict(value: Mapping[str, object]) -> AgentSession:
    return AgentSession(
        session_id=value.get("session_id"),  # type: ignore[arg-type]
        identity=_identity_from_dict(_mapping(value.get("identity"))),
        project=ProjectScope(value.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=value.get("coordination_session_id"),  # type: ignore[arg-type]
        state=value.get("state"),  # type: ignore[arg-type]
        started_at=value.get("started_at"),  # type: ignore[arg-type]
        last_heartbeat_at=value.get("last_heartbeat_at"),  # type: ignore[arg-type]
        expires_at=value.get("expires_at"),  # type: ignore[arg-type]
    )


def _work_receipt_from_dict(value: Mapping[str, object]) -> WorkReceipt:
    return WorkReceipt(
        receipt_id=value.get("receipt_id"),  # type: ignore[arg-type]
        work_item_id=value.get("work_item_id"),  # type: ignore[arg-type]
        project=ProjectScope(value.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=value.get("coordination_session_id"),  # type: ignore[arg-type]
        assigned_agent=_identity_from_dict(_mapping(value.get("assigned_agent"))),
        objective=value.get("objective"),  # type: ignore[arg-type]
        fencing_generation=value.get("fencing_generation"),  # type: ignore[arg-type]
        issued_at=value.get("issued_at"),  # type: ignore[arg-type]
        expires_at=value.get("expires_at"),  # type: ignore[arg-type]
        dependency_work_ids=_string_tuple(value.get("dependency_work_ids")),
    )


def _work_item_from_dict(value: Mapping[str, object]) -> WorkItem:
    return WorkItem(
        work_item_id=value.get("work_item_id"),  # type: ignore[arg-type]
        project=ProjectScope(value.get("project_id")),  # type: ignore[arg-type]
        owner_kind=value.get("owner_kind"),  # type: ignore[arg-type]
        policy_kind=value.get("policy_kind"),  # type: ignore[arg-type]
        operation_kind=value.get("operation_kind"),  # type: ignore[arg-type]
        input_sha256=value.get("input_sha256"),  # type: ignore[arg-type]
        result_schema=value.get("result_schema"),  # type: ignore[arg-type]
        created_at=value.get("created_at"),  # type: ignore[arg-type]
        max_attempts=value.get("max_attempts"),  # type: ignore[arg-type]
        coordination_session_id=value.get("coordination_session_id"),  # type: ignore[arg-type]
    )


def _lease_from_dict(value: Mapping[str, object]) -> WorkLease:
    raw_identity = value.get("owner_identity")
    owner_identity = None if raw_identity is None else _identity_from_dict(_mapping(raw_identity))
    return WorkLease(
        lease_id=value.get("lease_id"),  # type: ignore[arg-type]
        work_item=_work_item_from_dict(_mapping(value.get("work_item"))),
        owner_kind=value.get("owner_kind"),  # type: ignore[arg-type]
        policy_kind=value.get("policy_kind"),  # type: ignore[arg-type]
        owner_id=value.get("owner_id"),  # type: ignore[arg-type]
        owner_identity=owner_identity,
        fencing_generation=value.get("fencing_generation"),  # type: ignore[arg-type]
        attempt=value.get("attempt"),  # type: ignore[arg-type]
        issued_at=value.get("issued_at"),  # type: ignore[arg-type]
        expires_at=value.get("expires_at"),  # type: ignore[arg-type]
        result_binding_sha256=value.get("result_binding_sha256"),  # type: ignore[arg-type]
        idempotency_key_sha256=value.get("idempotency_key_sha256"),  # type: ignore[arg-type]
    )


def _result_from_dict(value: Mapping[str, object]) -> ResultReceipt:
    return ResultReceipt(
        receipt_id=value.get("receipt_id"),  # type: ignore[arg-type]
        work_item_id=value.get("work_item_id"),  # type: ignore[arg-type]
        work_receipt_sha256=value.get("work_receipt_sha256"),  # type: ignore[arg-type]
        project=ProjectScope(value.get("project_id")),  # type: ignore[arg-type]
        coordination_session_id=value.get("coordination_session_id"),  # type: ignore[arg-type]
        submitted_by=_identity_from_dict(_mapping(value.get("submitted_by"))),
        outcome=value.get("outcome"),  # type: ignore[arg-type]
        summary=value.get("summary"),  # type: ignore[arg-type]
        submitted_at=value.get("submitted_at"),  # type: ignore[arg-type]
        role_assignment_sha256=value.get("role_assignment_sha256"),  # type: ignore[arg-type]
        artifact_refs=_string_tuple(value.get("artifact_refs")),
        evidence_refs=_string_tuple(value.get("evidence_refs")),
        result=_mapping(value.get("result")),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RoleAssignmentError("role_assignment_projection_invalid")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RoleAssignmentError("role_assignment_projection_invalid")
    return tuple(str(item) for item in value)


def _canonical_mapping(value: str, code: str) -> Mapping[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RoleAssignmentError(code) from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != value:
        raise RoleAssignmentError(code)
    return payload


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    raw = cursor.fetchone()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(str(column[0]) for column in (cursor.description or ()))
    return [dict(zip(columns, tuple(raw), strict=True)) for raw in cursor.fetchall()]


def _optional_identifier(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, code)


def _active_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ACTIVE_ASSIGNMENT_LIMIT
    ):
        raise RoleAssignmentError("role_assignment_projection_limit_invalid")
    return value


def _basis_state(value: object, allowed: frozenset[str]) -> str:
    state = str(value or "").strip().casefold()
    if state not in allowed:
        raise RoleAssignmentError("role_assignment_basis_invalid")
    return state


__all__ = [
    "ACTIVE_ROLE_ASSIGNMENT_PROJECTION_SCHEMA",
    "DEFAULT_ACTIVE_ASSIGNMENT_LIMIT",
    "DURABLE_ROLE_ASSIGNMENT_BASIS_SCHEMA",
    "DURABLE_ROLE_ASSIGNMENT_REVOCATION_SCHEMA",
    "DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION",
    "MAX_ACTIVE_ASSIGNMENT_LIMIT",
    "ActiveRoleAssignmentProjection",
    "DurableRoleAssignmentRepository",
    "install_schema",
]
