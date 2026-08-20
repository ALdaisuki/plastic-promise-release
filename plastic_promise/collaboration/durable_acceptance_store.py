"""Durable SQLite adapter for server-owned acceptance authority.

The module is deliberately a deep adapter behind
``AcceptanceAuthorityRepository``.  It receives the canonical server SQLite
connection and the server's single-writer transaction factory; it never opens
another database and never creates, repairs, or migrates schema at runtime.

Issuance validates canonical WorkReceipt, ResultReceipt, AgentSession,
WorkLease, and durable role-assignment lineage directly from their owner
tables.  In particular, the persisted ``lease_binding`` that accompanies a
ResultReceipt is verified even though that binding is intentionally outside
the portable ResultReceipt digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .acceptance_receipt import (
    _SERVER_AUTHORITY_TOKEN,
    REVIEW_CHANNELS,
    AcceptanceReceipt,
    AcceptanceReceiptError,
    ReviewReceipt,
    _acceptance_binding_key,
    _canonical_json,
    _normalize_reviews,
    _require_acceptance_source_pair,
    _require_review_acceptance_pair,
    _require_sha256,
    _safe_identifier,
)
from .contracts import AgentSession, ProjectScope, ResultReceipt, WorkReceipt
from .durable_role_store import (
    DURABLE_ROLE_ASSIGNMENT_BASIS_SCHEMA,
    DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION,
)
from .role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA,
    ROLE_ASSIGNMENT_ISSUER,
    ROLE_ASSIGNMENT_POLICY_REVISION,
    ROLE_ASSIGNMENT_SCHEMA,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    RoleAssignmentBindingState,
    RoleAssignmentError,
    RoleAssignmentReceipt,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


DURABLE_ACCEPTANCE_SCHEMA_REVISION = "collaboration-acceptance/sqlite-v2"

_SCHEMA_TABLE = "collaboration_acceptance_schema"
_REVIEW_TABLE = "collaboration_review_receipts"
_ACCEPTANCE_TABLE = "collaboration_acceptance_receipts"
_ACCEPTANCE_REVIEW_BINDING_TABLE = "collaboration_acceptance_review_bindings"

_REQUIRED_INDEXES = frozenset(
    {
        "idx_collaboration_review_receipts_scope",
        "idx_collaboration_acceptance_receipts_scope",
    }
)
_REQUIRED_TRIGGERS = frozenset(
    {
        "collaboration_review_receipts_no_update",
        "collaboration_review_receipts_no_delete",
        "collaboration_acceptance_receipts_no_update",
        "collaboration_acceptance_receipts_no_delete",
        "collaboration_acceptance_review_bindings_no_update",
        "collaboration_acceptance_review_bindings_no_delete",
    }
)
_INDEX_COLUMNS = {
    "idx_collaboration_review_receipts_scope": (
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "reviewed_at_utc",
    ),
    "idx_collaboration_acceptance_receipts_scope": (
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "issued_at_utc",
    ),
}
_UNIQUE_COLUMN_SETS = {
    _REVIEW_TABLE: (
        ("review_receipt_sha256",),
        ("review_receipt_id",),
        (
            "project_id",
            "coordination_session_id",
            "work_item_id",
            "result_receipt_sha256",
            "reviewer_assignment_sha256",
            "review_channel",
        ),
    ),
    _ACCEPTANCE_TABLE: (
        ("acceptance_receipt_sha256",),
        ("acceptance_receipt_id",),
        (
            "project_id",
            "coordination_session_id",
            "work_item_id",
            "result_receipt_sha256",
        ),
    ),
    _ACCEPTANCE_REVIEW_BINDING_TABLE: (
        ("acceptance_receipt_sha256", "review_channel"),
        ("acceptance_receipt_sha256", "review_receipt_sha256"),
    ),
}
_TRIGGER_FRAGMENTS = {
    "collaboration_review_receipts_no_update": (
        "BEFORE UPDATE ON COLLABORATION_REVIEW_RECEIPTS",
        "RAISE(ABORT, 'COLLABORATION_REVIEW_RECEIPT_APPEND_ONLY')",
    ),
    "collaboration_review_receipts_no_delete": (
        "BEFORE DELETE ON COLLABORATION_REVIEW_RECEIPTS",
        "RAISE(ABORT, 'COLLABORATION_REVIEW_RECEIPT_APPEND_ONLY')",
    ),
    "collaboration_acceptance_receipts_no_update": (
        "BEFORE UPDATE ON COLLABORATION_ACCEPTANCE_RECEIPTS",
        "RAISE(ABORT, 'COLLABORATION_ACCEPTANCE_RECEIPT_APPEND_ONLY')",
    ),
    "collaboration_acceptance_receipts_no_delete": (
        "BEFORE DELETE ON COLLABORATION_ACCEPTANCE_RECEIPTS",
        "RAISE(ABORT, 'COLLABORATION_ACCEPTANCE_RECEIPT_APPEND_ONLY')",
    ),
    "collaboration_acceptance_review_bindings_no_update": (
        "BEFORE UPDATE ON COLLABORATION_ACCEPTANCE_REVIEW_BINDINGS",
        "RAISE(ABORT, 'COLLABORATION_ACCEPTANCE_REVIEW_BINDING_APPEND_ONLY')",
    ),
    "collaboration_acceptance_review_bindings_no_delete": (
        "BEFORE DELETE ON COLLABORATION_ACCEPTANCE_REVIEW_BINDINGS",
        "RAISE(ABORT, 'COLLABORATION_ACCEPTANCE_REVIEW_BINDING_APPEND_ONLY')",
    ),
}
_RESULT_LEASE_BINDING_FIELDS = frozenset(
    {"lease_id", "lease_sha256", "fencing_generation", "result_binding_sha256"}
)
_ROLE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "issuer",
        "assignment_id",
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "work_receipt_sha256",
        "lease_id",
        "lease_sha256",
        "fencing_generation",
        "binding_generation",
        "agent_session_id",
        "agent_session_sha256",
        "agent_id",
        "assignment_role",
        "use",
        "workflow_stage",
        "intent_event_id",
        "intent_event_sha256",
        "result_receipt_sha256",
        "assignment_policy_revision",
        "issued_at_utc",
        "expires_at_utc",
        "idempotency_sha256",
        "authority_effect",
        "tool_policy_effect",
        "canonical_memory_effect",
        "verification",
    }
)
_ROLE_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "assignment_sha256",
        "server_basis_sha256",
        "binding_generation",
        "revoked",
        "revoked_at_utc",
    }
)
_ROLE_BASIS_FIELDS = frozenset(
    {
        "schema_version",
        "use",
        "session",
        "work",
        "lease",
        "intent_event",
        "workflow_stage",
        "work_state",
        "lease_state",
        "result",
        "submitter_agent_session_id",
        "authority_effect",
        "canonical_memory_effect",
    }
)

_REQUIRED_COLUMNS = {
    _SCHEMA_TABLE: {"singleton", "schema_revision", "installed_at_utc"},
    _REVIEW_TABLE: {
        "review_receipt_sha256",
        "review_receipt_id",
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "work_receipt_sha256",
        "result_receipt_sha256",
        "reviewer_assignment_sha256",
        "reviewer_agent_session_id",
        "review_policy_revision",
        "source_revision",
        "review_channel",
        "diff_digest",
        "requirement_set_digest",
        "union_contract_revision",
        "decision",
        "conflict_state",
        "reviewed_at_utc",
        "receipt_json",
    },
    _ACCEPTANCE_TABLE: {
        "acceptance_receipt_sha256",
        "acceptance_receipt_id",
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "work_receipt_id",
        "work_receipt_sha256",
        "result_receipt_id",
        "result_receipt_sha256",
        "review_receipt_id",
        "review_receipt_sha256",
        "submitter_agent_session_id",
        "submitter_agent_session_sha256",
        "reviewer_agent_session_id",
        "reviewer_agent_session_sha256",
        "submitter_assignment_sha256",
        "reviewer_assignment_sha256",
        "assignment_policy_revision",
        "review_policy_revision",
        "source_revision",
        "diff_digest",
        "requirement_set_digest",
        "union_contract_revision",
        "decision",
        "conflict_state",
        "issued_at_utc",
        "receipt_json",
    },
    _ACCEPTANCE_REVIEW_BINDING_TABLE: {
        "acceptance_receipt_sha256",
        "review_channel",
        "review_receipt_sha256",
        "source_revision",
        "diff_digest",
        "requirement_set_digest",
        "union_contract_revision",
    },
    "collaboration_work_items": {
        "work_item_id",
        "project_id",
        "coordination_session_id",
        "work_receipt_json",
        "work_receipt_sha256",
        "assigned_agent_id",
    },
    "collaboration_results": {
        "receipt_id",
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "result_json",
        "result_sha256",
        "submitted_at",
        "outcome",
    },
    "collaboration_agent_sessions": {
        "session_id",
        "project_id",
        "agent_id",
        "coordination_session_id",
        "session_json",
        "session_sha256",
    },
    "collaboration_work_leases": {
        "lease_id",
        "work_item_id",
        "project_id",
        "coordination_session_id",
        "owner_id",
        "owner_session_id",
        "lease_json",
        "lease_sha256",
        "fencing_generation",
        "state",
    },
    "collaboration_role_assignment_schema": {
        "singleton",
        "schema_revision",
        "installed_at",
    },
    "collaboration_role_assignment_receipts": {
        "assignment_sha256",
        "assignment_id",
        "project_id",
        "coordination_session_id",
        "work_item_id",
        "agent_session_id",
        "agent_id",
        "assignment_role",
        "use",
        "workflow_stage",
        "binding_generation",
        "receipt_json",
    },
    "collaboration_role_assignment_bindings": {
        "assignment_sha256",
        "server_basis_sha256",
        "binding_generation",
        "revoked_at_utc",
        "binding_json",
        "binding_sha256",
    },
    "collaboration_role_assignment_basis_current": {
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
    },
    "collaboration_role_assignment_basis_snapshots": {
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
        "basis_json",
    },
}


@dataclass(frozen=True, slots=True)
class _LeaseLineage:
    lease_id: str
    lease_sha256: str
    fencing_generation: int
    owner_id: str
    owner_session_id: str
    projection: Mapping[str, object]


class DurableAcceptanceAuthorityRepository:
    """Canonical SQLite adapter satisfying ``AcceptanceAuthorityRepository``."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_factory: Callable[[], AbstractContextManager[None]],
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise AcceptanceReceiptError("acceptance_durable_connection_invalid")
        if not callable(transaction_factory):
            raise AcceptanceReceiptError("acceptance_durable_writer_required")
        self._connection = connection
        self._transaction_factory = transaction_factory
        self._verify_schema()

    def require_canonical_sources(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        submitter_assignment_sha256: str,
        reviewer_assignment_sha256: str,
    ) -> None:
        """Verify the exact committed canonical lineage needed for issuance."""

        if not isinstance(work, WorkReceipt):
            raise AcceptanceReceiptError("acceptance_work_receipt_invalid")
        if not isinstance(result, ResultReceipt):
            raise AcceptanceReceiptError("acceptance_result_receipt_invalid")
        if not isinstance(submitter_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_submitter_session_invalid")
        if not isinstance(reviewer_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_reviewer_session_invalid")
        submitter_digest = _require_sha256(
            submitter_assignment_sha256,
            "acceptance_submitter_assignment_digest_invalid",
        )
        reviewer_digest = _require_sha256(
            reviewer_assignment_sha256,
            "acceptance_reviewer_assignment_digest_invalid",
        )

        self._require_work(work)
        self._require_session(submitter_session, kind="submitter")
        self._require_session(reviewer_session, kind="reviewer")
        lease = self._require_result(
            result,
            work=work,
            submitter_session_id=submitter_session.session_id,
        )
        submitter_assignment = self._require_role_lineage(
            submitter_digest,
            expected_use=RESULT_SUBMISSION_USE,
            expected_role=WORK_SUBMITTER_ROLE,
            session=submitter_session,
            work=work,
            result=result,
            lease=lease,
            submitter_session_id=submitter_session.session_id,
        )
        reviewer_assignment = self._require_role_lineage(
            reviewer_digest,
            expected_use=ACCEPTANCE_REVIEW_USE,
            expected_role=WORK_REVIEWER_ROLE,
            session=reviewer_session,
            work=work,
            result=result,
            lease=lease,
            submitter_session_id=submitter_session.session_id,
        )
        if submitter_assignment.agent_id == reviewer_assignment.agent_id:
            raise AcceptanceReceiptError("acceptance_independent_reviewer_required")

    def load_acceptance_by_id(
        self,
        acceptance_receipt_id: str,
    ) -> AcceptanceReceipt | None:
        receipt_id = _safe_identifier(
            acceptance_receipt_id,
            "acceptance_receipt_id_invalid",
        )
        row = self._fetchone(
            f"SELECT * FROM {_ACCEPTANCE_TABLE} WHERE acceptance_receipt_id=?",
            (receipt_id,),
        )
        if row is None:
            return None
        receipt = self._acceptance_from_row(row)
        if receipt.acceptance_receipt_id != receipt_id:
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        return receipt

    def load_acceptance_by_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        work_item_id: str,
        result_receipt_sha256: str,
    ) -> AcceptanceReceipt | None:
        binding = _acceptance_binding_key(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            work_item_id=work_item_id,
            result_receipt_sha256=result_receipt_sha256,
        )
        row = self._fetchone(
            f"""
            SELECT * FROM {_ACCEPTANCE_TABLE}
             WHERE project_id=? AND coordination_session_id=?
               AND work_item_id=? AND result_receipt_sha256=?
            """,
            binding,
        )
        if row is None:
            return None
        receipt = self._acceptance_from_row(row)
        observed = (
            receipt.project.project_id,
            receipt.coordination_session_id,
            receipt.work_item_id,
            receipt.result_receipt_sha256,
        )
        if observed != binding:
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        return receipt

    def load_review_by_digest(
        self,
        review_receipt_sha256: str,
    ) -> ReviewReceipt | None:
        digest = _require_sha256(
            review_receipt_sha256,
            "review_receipt_digest_invalid",
        )
        row = self._fetchone(
            f"SELECT * FROM {_REVIEW_TABLE} WHERE review_receipt_sha256=?",
            (digest,),
        )
        if row is None:
            return None
        review = self._review_from_row(row)
        if not hmac.compare_digest(review.content_sha256, digest):
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        return review

    def load_review_by_id(self, review_receipt_id: str) -> ReviewReceipt | None:
        receipt_id = _safe_identifier(review_receipt_id, "review_receipt_id_invalid")
        row = self._fetchone(
            f"SELECT * FROM {_REVIEW_TABLE} WHERE review_receipt_id=?",
            (receipt_id,),
        )
        if row is None:
            return None
        review = self._review_from_row(row)
        if review.review_receipt_id != receipt_id:
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        return review

    def append_exact(
        self,
        reviews: tuple[ReviewReceipt, ...],
        receipt: AcceptanceReceipt,
        *,
        work: WorkReceipt,
        result: ResultReceipt,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        _authority_token: object | None = None,
    ) -> AcceptanceReceipt:
        """Persist exact three-channel reviews and acceptance atomically."""

        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise AcceptanceReceiptError("acceptance_repository_write_authority_required")
        normalized_reviews = _normalize_reviews(reviews)
        _require_review_acceptance_pair(normalized_reviews, receipt)
        _require_acceptance_source_pair(
            receipt,
            work=work,
            result=result,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
        )
        with self._transaction_factory():
            self.require_canonical_sources(
                work,
                result,
                submitter_session=submitter_session,
                reviewer_session=reviewer_session,
                submitter_assignment_sha256=receipt.submitter_assignment_sha256,
                reviewer_assignment_sha256=receipt.reviewer_assignment_sha256,
            )
            canonical_reviews = tuple(self._append_review(review) for review in normalized_reviews)
            canonical_acceptance = self._append_acceptance(receipt)
            self._append_acceptance_review_bindings(
                canonical_reviews,
                canonical_acceptance,
            )
            return canonical_acceptance

    def _require_work(self, work: WorkReceipt) -> None:
        row = self._fetchone(
            "SELECT * FROM collaboration_work_items WHERE work_item_id=?",
            (work.work_item_id,),
        )
        if row is None:
            raise AcceptanceReceiptError("acceptance_work_source_unverified")
        if (
            str(row["project_id"]) != work.project.project_id
            or str(row["coordination_session_id"]) != work.coordination_session_id
            or str(row["assigned_agent_id"]) != work.assigned_agent.agent_id
            or not hmac.compare_digest(str(row["work_receipt_sha256"]), work.content_sha256)
        ):
            raise AcceptanceReceiptError("acceptance_work_source_unverified")
        payload = self._canonical_payload(
            row["work_receipt_json"],
            "acceptance_work_source_corrupt",
        )
        if payload != work.to_dict() or not hmac.compare_digest(
            _sha256(payload),
            str(row["work_receipt_sha256"]),
        ):
            raise AcceptanceReceiptError("acceptance_work_source_corrupt")

    def _require_session(self, session: AgentSession, *, kind: str) -> None:
        row = self._fetchone(
            "SELECT * FROM collaboration_agent_sessions WHERE session_id=?",
            (session.session_id,),
        )
        if row is None:
            raise AcceptanceReceiptError(f"acceptance_{kind}_session_source_unverified")
        if (
            str(row["project_id"]) != session.project.project_id
            or str(row["coordination_session_id"]) != session.coordination_session_id
            or str(row["agent_id"]) != session.identity.agent_id
            or not hmac.compare_digest(str(row["session_sha256"]), session.content_sha256)
        ):
            raise AcceptanceReceiptError(f"acceptance_{kind}_session_source_unverified")
        payload = self._canonical_payload(
            row["session_json"],
            f"acceptance_{kind}_session_source_corrupt",
        )
        if payload != session.to_dict() or not hmac.compare_digest(
            _sha256(payload),
            str(row["session_sha256"]),
        ):
            raise AcceptanceReceiptError(f"acceptance_{kind}_session_source_corrupt")

    def _require_result(
        self,
        result: ResultReceipt,
        *,
        work: WorkReceipt,
        submitter_session_id: str,
    ) -> _LeaseLineage:
        row = self._fetchone(
            "SELECT * FROM collaboration_results WHERE receipt_id=?",
            (result.receipt_id,),
        )
        if row is None:
            raise AcceptanceReceiptError("acceptance_result_source_unverified")
        if (
            str(row["project_id"]) != result.project.project_id
            or str(row["coordination_session_id"]) != result.coordination_session_id
            or str(row["work_item_id"]) != result.work_item_id
            or str(row["submitted_at"]) != result.submitted_at
            or str(row["outcome"]) != result.outcome
            or not hmac.compare_digest(str(row["result_sha256"]), result.content_sha256)
        ):
            raise AcceptanceReceiptError("acceptance_result_source_unverified")
        payload = self._canonical_payload(
            row["result_json"],
            "acceptance_result_source_corrupt",
        )
        expected_fields = set(result.to_dict()) | {"lease_binding"}
        if set(payload) != expected_fields:
            raise AcceptanceReceiptError("acceptance_result_lease_binding_missing")
        portable = dict(payload)
        raw_binding = portable.pop("lease_binding")
        if portable != result.to_dict() or not hmac.compare_digest(
            _sha256(portable),
            str(row["result_sha256"]),
        ):
            raise AcceptanceReceiptError("acceptance_result_source_corrupt")
        binding = _exact_mapping(
            raw_binding,
            fields=_RESULT_LEASE_BINDING_FIELDS,
            code="acceptance_result_lease_binding_corrupt",
        )
        lease_id = _safe_identifier(
            binding["lease_id"],
            "acceptance_result_lease_binding_corrupt",
        )
        lease_sha256 = _require_sha256(
            binding["lease_sha256"],
            "acceptance_result_lease_binding_corrupt",
        )
        generation = binding["fencing_generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise AcceptanceReceiptError("acceptance_result_lease_binding_corrupt")
        if binding["result_binding_sha256"] != result.work_receipt_sha256:
            raise AcceptanceReceiptError("acceptance_result_lease_binding_mismatch")

        lease_row = self._fetchone(
            "SELECT * FROM collaboration_work_leases WHERE lease_id=?",
            (lease_id,),
        )
        if lease_row is None:
            raise AcceptanceReceiptError("acceptance_result_lease_source_unverified")
        if (
            str(lease_row["work_item_id"]) != result.work_item_id
            or str(lease_row["project_id"]) != result.project.project_id
            or str(lease_row["coordination_session_id"]) != result.coordination_session_id
            or str(lease_row["owner_id"]) != result.submitted_by.agent_id
            or str(lease_row["owner_session_id"] or "") != submitter_session_id
            or str(lease_row["state"]) not in {"active", "completed"}
            or int(lease_row["fencing_generation"]) != generation
            or not hmac.compare_digest(str(lease_row["lease_sha256"]), lease_sha256)
        ):
            raise AcceptanceReceiptError("acceptance_result_lease_binding_mismatch")
        lease_projection = self._canonical_payload(
            lease_row["lease_json"],
            "acceptance_result_lease_source_corrupt",
        )
        if not hmac.compare_digest(_sha256(lease_projection), lease_sha256):
            raise AcceptanceReceiptError("acceptance_result_lease_source_corrupt")
        if (
            lease_projection.get("lease_id") != lease_id
            or lease_projection.get("project_id") != work.project.project_id
            or lease_projection.get("owner_id") != result.submitted_by.agent_id
            or lease_projection.get("fencing_generation") != generation
            or lease_projection.get("result_binding_sha256") != work.content_sha256
        ):
            raise AcceptanceReceiptError("acceptance_result_lease_binding_mismatch")
        return _LeaseLineage(
            lease_id=lease_id,
            lease_sha256=lease_sha256,
            fencing_generation=generation,
            owner_id=result.submitted_by.agent_id,
            owner_session_id=str(lease_row["owner_session_id"] or ""),
            projection=lease_projection,
        )

    def _require_role_lineage(
        self,
        assignment_sha256: str,
        *,
        expected_use: str,
        expected_role: str,
        session: AgentSession,
        work: WorkReceipt,
        result: ResultReceipt,
        lease: _LeaseLineage,
        submitter_session_id: str,
    ) -> RoleAssignmentReceipt:
        row = self._fetchone(
            """
            SELECT receipts.*, bindings.server_basis_sha256,
                   bindings.revoked_at_utc, bindings.binding_json,
                   bindings.binding_sha256
              FROM collaboration_role_assignment_receipts AS receipts
              JOIN collaboration_role_assignment_bindings AS bindings
                ON bindings.assignment_sha256 = receipts.assignment_sha256
             WHERE receipts.assignment_sha256=?
            """,
            (assignment_sha256,),
        )
        if row is None:
            raise AcceptanceReceiptError("acceptance_role_assignment_source_unverified")
        receipt = _role_receipt_from_row(row)
        expected_result_digest = (
            result.content_sha256 if expected_use == ACCEPTANCE_REVIEW_USE else ""
        )
        checks = (
            (receipt.assignment_sha256 == assignment_sha256),
            (receipt.use == expected_use),
            (receipt.assignment_role == expected_role),
            (receipt.assignment_policy_revision == ROLE_ASSIGNMENT_POLICY_REVISION),
            (receipt.project == work.project),
            (receipt.coordination_session_id == work.coordination_session_id),
            (receipt.work_item_id == work.work_item_id),
            hmac.compare_digest(receipt.work_receipt_sha256, work.content_sha256),
            (receipt.agent_session_id == session.session_id),
            hmac.compare_digest(receipt.agent_session_sha256, session.content_sha256),
            (receipt.agent_id == session.identity.agent_id),
            (receipt.lease_id == lease.lease_id),
            hmac.compare_digest(receipt.lease_sha256, lease.lease_sha256),
            (receipt.fencing_generation == lease.fencing_generation),
            (receipt.result_receipt_sha256 == expected_result_digest),
        )
        if not all(checks):
            raise AcceptanceReceiptError("acceptance_role_assignment_lineage_mismatch")
        binding = _role_binding_from_row(row)
        if (
            binding.binding_generation != receipt.binding_generation
            or not hmac.compare_digest(binding.assignment_sha256, receipt.assignment_sha256)
            or binding.revoked
        ):
            raise AcceptanceReceiptError("acceptance_role_assignment_lineage_mismatch")
        basis = self._require_role_basis(receipt)
        expected_basis_digest = _server_basis_sha256(
            basis,
            receipt=receipt,
            session=session,
            work=work,
            result=result if expected_use == ACCEPTANCE_REVIEW_USE else None,
            lease=lease,
            submitter_session_id=(
                submitter_session_id if expected_use == ACCEPTANCE_REVIEW_USE else ""
            ),
        )
        if not hmac.compare_digest(binding.server_basis_sha256, expected_basis_digest):
            raise AcceptanceReceiptError("acceptance_role_assignment_lineage_mismatch")
        return receipt

    def _require_role_basis(self, receipt: RoleAssignmentReceipt) -> Mapping[str, object]:
        row = self._fetchone(
            """
            SELECT current.basis_key_sha256 AS current_basis_key_sha256,
                   current.basis_sha256 AS current_basis_sha256,
                   snapshots.*
              FROM collaboration_role_assignment_basis_current AS current
              JOIN collaboration_role_assignment_basis_snapshots AS snapshots
                ON snapshots.snapshot_id = current.snapshot_id
             WHERE current.use=? AND current.agent_session_id=?
               AND current.work_item_id=? AND current.lease_id=?
               AND current.intent_event_id=?
            """,
            (
                receipt.use,
                receipt.agent_session_id,
                receipt.work_item_id,
                receipt.lease_id,
                receipt.intent_event_id,
            ),
        )
        if row is None:
            raise AcceptanceReceiptError("acceptance_role_assignment_basis_unverified")
        payload = self._canonical_payload(
            row["basis_json"],
            "acceptance_role_assignment_basis_corrupt",
        )
        if set(payload) != _ROLE_BASIS_FIELDS:
            raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
        if (
            payload["schema_version"] != DURABLE_ROLE_ASSIGNMENT_BASIS_SCHEMA
            or payload["authority_effect"] != "none"
            or payload["canonical_memory_effect"] != "none"
        ):
            raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
        if (
            str(row["current_basis_key_sha256"]) != str(row["basis_key_sha256"])
            or str(row["current_basis_sha256"]) != str(row["basis_sha256"])
            or not hmac.compare_digest(_sha256(payload), str(row["basis_sha256"]))
            or str(row["use"]) != receipt.use
            or str(row["project_id"]) != receipt.project.project_id
            or str(row["coordination_session_id"]) != receipt.coordination_session_id
            or str(row["agent_session_id"]) != receipt.agent_session_id
            or str(row["work_item_id"]) != receipt.work_item_id
            or str(row["lease_id"]) != receipt.lease_id
            or str(row["intent_event_id"]) != receipt.intent_event_id
            or not hmac.compare_digest(
                str(row["intent_event_sha256"]),
                receipt.intent_event_sha256,
            )
        ):
            raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
        return payload

    def _append_review(self, review: ReviewReceipt) -> ReviewReceipt:
        existing_by_id = self.load_review_by_id(review.review_receipt_id)
        existing_by_digest = self.load_review_by_digest(review.content_sha256)
        existing_by_binding = self._load_review_by_binding(review)
        existing = existing_by_id or existing_by_digest or existing_by_binding
        for candidate in (existing_by_id, existing_by_digest, existing_by_binding):
            if candidate is not None and not hmac.compare_digest(
                candidate.content_sha256,
                review.content_sha256,
            ):
                raise AcceptanceReceiptError("review_receipt_replay_conflict")
        if existing is not None:
            return existing
        try:
            self._connection.execute(
                f"""
                INSERT INTO {_REVIEW_TABLE} (
                    review_receipt_sha256, review_receipt_id, project_id,
                    coordination_session_id, work_item_id, work_receipt_sha256,
                    result_receipt_sha256, reviewer_assignment_sha256,
                    reviewer_agent_session_id, review_policy_revision,
                    source_revision, review_channel, diff_digest,
                    requirement_set_digest, union_contract_revision,
                    decision, conflict_state, reviewed_at_utc, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.content_sha256,
                    review.review_receipt_id,
                    review.project.project_id,
                    review.coordination_session_id,
                    review.work_item_id,
                    review.work_receipt_sha256,
                    review.result_receipt_sha256,
                    review.reviewer_assignment_sha256,
                    review.reviewer_agent_session_id,
                    review.review_policy_revision,
                    review.source_revision,
                    review.review_channel,
                    review.diff_digest,
                    review.requirement_set_digest,
                    review.union_contract_revision,
                    review.decision,
                    review.conflict_state,
                    review.reviewed_at_utc,
                    _canonical_json(review.to_dict()),
                ),
            )
        except sqlite3.IntegrityError as exc:
            winner = self.load_review_by_id(review.review_receipt_id)
            if winner is None:
                winner = self._load_review_by_binding(review)
            if winner is not None and hmac.compare_digest(
                winner.content_sha256,
                review.content_sha256,
            ):
                return winner
            raise AcceptanceReceiptError("review_receipt_replay_conflict") from exc
        return review

    def _append_acceptance(self, receipt: AcceptanceReceipt) -> AcceptanceReceipt:
        existing_by_id = self.load_acceptance_by_id(receipt.acceptance_receipt_id)
        existing_by_binding = self.load_acceptance_by_binding(
            project_id=receipt.project.project_id,
            coordination_session_id=receipt.coordination_session_id,
            work_item_id=receipt.work_item_id,
            result_receipt_sha256=receipt.result_receipt_sha256,
        )
        if existing_by_id is not None and not hmac.compare_digest(
            existing_by_id.content_sha256,
            receipt.content_sha256,
        ):
            raise AcceptanceReceiptError("acceptance_receipt_replay_conflict")
        if existing_by_binding is not None and not hmac.compare_digest(
            existing_by_binding.content_sha256,
            receipt.content_sha256,
        ):
            if existing_by_binding.decision != receipt.decision:
                raise AcceptanceReceiptError("acceptance_receipt_decision_conflict")
            raise AcceptanceReceiptError("acceptance_receipt_replay_ambiguous")
        existing = existing_by_id or existing_by_binding
        if existing is not None:
            return existing
        for binding in receipt.review_receipt_bindings:
            persisted_review = self.load_review_by_digest(binding.review_receipt_sha256)
            if (
                persisted_review is None
                or persisted_review.review_channel != binding.review_channel
                or persisted_review.review_receipt_id != binding.review_receipt_id
            ):
                raise AcceptanceReceiptError("acceptance_review_receipt_unpersisted")
        try:
            self._connection.execute(
                f"""
                INSERT INTO {_ACCEPTANCE_TABLE} (
                    acceptance_receipt_sha256, acceptance_receipt_id, project_id,
                    coordination_session_id, work_item_id, work_receipt_id,
                    work_receipt_sha256, result_receipt_id, result_receipt_sha256,
                    review_receipt_id, review_receipt_sha256,
                    submitter_agent_session_id, submitter_agent_session_sha256,
                    reviewer_agent_session_id, reviewer_agent_session_sha256,
                    submitter_assignment_sha256, reviewer_assignment_sha256,
                    assignment_policy_revision, review_policy_revision,
                    source_revision, diff_digest, requirement_set_digest,
                    union_contract_revision, decision, conflict_state,
                    issued_at_utc, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.content_sha256,
                    receipt.acceptance_receipt_id,
                    receipt.project.project_id,
                    receipt.coordination_session_id,
                    receipt.work_item_id,
                    receipt.work_receipt_id,
                    receipt.work_receipt_sha256,
                    receipt.result_receipt_id,
                    receipt.result_receipt_sha256,
                    receipt.review_receipt_id,
                    receipt.review_receipt_sha256,
                    receipt.submitter_agent_session_id,
                    receipt.submitter_agent_session_sha256,
                    receipt.reviewer_agent_session_id,
                    receipt.reviewer_agent_session_sha256,
                    receipt.submitter_assignment_sha256,
                    receipt.reviewer_assignment_sha256,
                    receipt.assignment_policy_revision,
                    receipt.review_policy_revision,
                    receipt.source_revision,
                    receipt.diff_digest,
                    receipt.requirement_set_digest,
                    receipt.union_contract_revision,
                    receipt.decision,
                    receipt.conflict_state,
                    receipt.issued_at_utc,
                    _canonical_json(receipt.to_dict()),
                ),
            )
        except sqlite3.IntegrityError as exc:
            winner = self.load_acceptance_by_id(receipt.acceptance_receipt_id)
            if winner is None:
                winner = self.load_acceptance_by_binding(
                    project_id=receipt.project.project_id,
                    coordination_session_id=receipt.coordination_session_id,
                    work_item_id=receipt.work_item_id,
                    result_receipt_sha256=receipt.result_receipt_sha256,
                )
            if winner is not None and hmac.compare_digest(
                winner.content_sha256,
                receipt.content_sha256,
            ):
                return winner
            raise AcceptanceReceiptError("acceptance_receipt_replay_conflict") from exc
        return receipt

    def _append_acceptance_review_bindings(
        self,
        reviews: tuple[ReviewReceipt, ...],
        receipt: AcceptanceReceipt,
    ) -> None:
        normalized_reviews = _normalize_reviews(reviews)
        _require_review_acceptance_pair(normalized_reviews, receipt)
        existing_rows = self._acceptance_review_binding_rows(receipt.content_sha256)
        if existing_rows:
            persisted_reviews = self._reviews_from_binding_rows(
                receipt,
                existing_rows,
            )
            if persisted_reviews != normalized_reviews:
                raise AcceptanceReceiptError("acceptance_review_bindings_mismatch")
            return

        try:
            self._connection.executemany(
                f"""
                INSERT INTO {_ACCEPTANCE_REVIEW_BINDING_TABLE} (
                    acceptance_receipt_sha256, review_channel,
                    review_receipt_sha256, source_revision, diff_digest,
                    requirement_set_digest, union_contract_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    (
                        receipt.content_sha256,
                        review.review_channel,
                        review.content_sha256,
                        review.source_revision,
                        review.diff_digest,
                        review.requirement_set_digest,
                        review.union_contract_revision,
                    )
                    for review in normalized_reviews
                ),
            )
        except sqlite3.IntegrityError as exc:
            rows = self._acceptance_review_binding_rows(receipt.content_sha256)
            if rows:
                persisted_reviews = self._reviews_from_binding_rows(receipt, rows)
                if persisted_reviews == normalized_reviews:
                    return
            raise AcceptanceReceiptError("acceptance_review_binding_replay_conflict") from exc

        persisted_reviews = self._reviews_for_acceptance(receipt)
        if persisted_reviews != normalized_reviews:
            raise AcceptanceReceiptError("acceptance_review_bindings_mismatch")

    def _reviews_for_acceptance(
        self,
        receipt: AcceptanceReceipt,
    ) -> tuple[ReviewReceipt, ...]:
        rows = self._acceptance_review_binding_rows(receipt.content_sha256)
        if not rows:
            raise AcceptanceReceiptError("acceptance_review_bindings_unpersisted")
        return self._reviews_from_binding_rows(receipt, rows)

    def _acceptance_review_binding_rows(
        self,
        acceptance_receipt_sha256: str,
    ) -> tuple[dict[str, Any], ...]:
        digest = _require_sha256(
            acceptance_receipt_sha256,
            "acceptance_receipt_digest_invalid",
        )
        cursor = self._connection.execute(
            f"""
            SELECT * FROM {_ACCEPTANCE_REVIEW_BINDING_TABLE}
             WHERE acceptance_receipt_sha256=?
            """,
            (digest,),
        )
        columns = tuple(str(column[0]) for column in (cursor.description or ()))
        return tuple(dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall())

    def _reviews_from_binding_rows(
        self,
        receipt: AcceptanceReceipt,
        rows: tuple[Mapping[str, object], ...],
    ) -> tuple[ReviewReceipt, ...]:
        if len(rows) != len(REVIEW_CHANNELS):
            raise AcceptanceReceiptError("acceptance_review_bindings_incomplete")
        row_by_channel: dict[str, Mapping[str, object]] = {}
        for row in rows:
            channel = str(row["review_channel"])
            if channel not in REVIEW_CHANNELS or channel in row_by_channel:
                raise AcceptanceReceiptError("acceptance_review_binding_channel_invalid")
            row_by_channel[channel] = row
        if (
            tuple(channel for channel in REVIEW_CHANNELS if channel in row_by_channel)
            != REVIEW_CHANNELS
        ):
            raise AcceptanceReceiptError("acceptance_review_bindings_incomplete")

        binding_by_channel = {
            binding.review_channel: binding for binding in receipt.review_receipt_bindings
        }
        reviews: list[ReviewReceipt] = []
        for channel in REVIEW_CHANNELS:
            row = row_by_channel[channel]
            binding = binding_by_channel.get(channel)
            if binding is None:
                raise AcceptanceReceiptError("acceptance_review_bindings_mismatch")
            row_checks = (
                hmac.compare_digest(
                    str(row["acceptance_receipt_sha256"]),
                    receipt.content_sha256,
                ),
                hmac.compare_digest(
                    str(row["review_receipt_sha256"]),
                    binding.review_receipt_sha256,
                ),
                str(row["source_revision"]) == receipt.source_revision,
                hmac.compare_digest(
                    str(row["diff_digest"]),
                    receipt.diff_digest,
                ),
                hmac.compare_digest(
                    str(row["requirement_set_digest"]),
                    receipt.requirement_set_digest,
                ),
                str(row["union_contract_revision"]) == receipt.union_contract_revision,
            )
            if not all(row_checks):
                raise AcceptanceReceiptError("acceptance_review_bindings_mismatch")
            review = self.load_review_by_digest(binding.review_receipt_sha256)
            if (
                review is None
                or review.review_channel != channel
                or review.review_receipt_id != binding.review_receipt_id
            ):
                raise AcceptanceReceiptError("acceptance_review_receipt_unpersisted")
            reviews.append(review)
        return _normalize_reviews(tuple(reviews))

    def _load_review_by_binding(self, review: ReviewReceipt) -> ReviewReceipt | None:
        row = self._fetchone(
            f"""
            SELECT * FROM {_REVIEW_TABLE}
             WHERE project_id=? AND coordination_session_id=?
               AND work_item_id=? AND result_receipt_sha256=?
               AND reviewer_assignment_sha256=? AND review_channel=?
            """,
            (
                review.project.project_id,
                review.coordination_session_id,
                review.work_item_id,
                review.result_receipt_sha256,
                review.reviewer_assignment_sha256,
                review.review_channel,
            ),
        )
        return None if row is None else self._review_from_row(row)

    def _review_from_row(self, row: Mapping[str, object]) -> ReviewReceipt:
        review = ReviewReceipt.from_canonical_json(
            str(row["receipt_json"]),
            expected_sha256=str(row["review_receipt_sha256"]),
        )
        checks = (
            (review.review_receipt_id, row["review_receipt_id"]),
            (review.project.project_id, row["project_id"]),
            (review.coordination_session_id, row["coordination_session_id"]),
            (review.work_item_id, row["work_item_id"]),
            (review.work_receipt_sha256, row["work_receipt_sha256"]),
            (review.result_receipt_sha256, row["result_receipt_sha256"]),
            (review.reviewer_assignment_sha256, row["reviewer_assignment_sha256"]),
            (review.reviewer_agent_session_id, row["reviewer_agent_session_id"]),
            (review.review_policy_revision, row["review_policy_revision"]),
            (review.source_revision, row["source_revision"]),
            (review.review_channel, row["review_channel"]),
            (review.diff_digest, row["diff_digest"]),
            (review.requirement_set_digest, row["requirement_set_digest"]),
            (review.union_contract_revision, row["union_contract_revision"]),
            (review.decision, row["decision"]),
            (review.conflict_state, row["conflict_state"]),
            (review.reviewed_at_utc, row["reviewed_at_utc"]),
        )
        if any(str(expected) != str(observed) for expected, observed in checks):
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        return review

    def _acceptance_from_row(self, row: Mapping[str, object]) -> AcceptanceReceipt:
        receipt = AcceptanceReceipt.from_canonical_json(
            str(row["receipt_json"]),
            expected_sha256=str(row["acceptance_receipt_sha256"]),
        )
        checks = (
            (receipt.acceptance_receipt_id, row["acceptance_receipt_id"]),
            (receipt.project.project_id, row["project_id"]),
            (receipt.coordination_session_id, row["coordination_session_id"]),
            (receipt.work_item_id, row["work_item_id"]),
            (receipt.work_receipt_id, row["work_receipt_id"]),
            (receipt.work_receipt_sha256, row["work_receipt_sha256"]),
            (receipt.result_receipt_id, row["result_receipt_id"]),
            (receipt.result_receipt_sha256, row["result_receipt_sha256"]),
            (receipt.review_receipt_id, row["review_receipt_id"]),
            (receipt.review_receipt_sha256, row["review_receipt_sha256"]),
            (receipt.submitter_agent_session_id, row["submitter_agent_session_id"]),
            (
                receipt.submitter_agent_session_sha256,
                row["submitter_agent_session_sha256"],
            ),
            (receipt.reviewer_agent_session_id, row["reviewer_agent_session_id"]),
            (
                receipt.reviewer_agent_session_sha256,
                row["reviewer_agent_session_sha256"],
            ),
            (receipt.submitter_assignment_sha256, row["submitter_assignment_sha256"]),
            (receipt.reviewer_assignment_sha256, row["reviewer_assignment_sha256"]),
            (receipt.assignment_policy_revision, row["assignment_policy_revision"]),
            (receipt.review_policy_revision, row["review_policy_revision"]),
            (receipt.source_revision, row["source_revision"]),
            (receipt.diff_digest, row["diff_digest"]),
            (receipt.requirement_set_digest, row["requirement_set_digest"]),
            (receipt.union_contract_revision, row["union_contract_revision"]),
            (receipt.decision, row["decision"]),
            (receipt.conflict_state, row["conflict_state"]),
            (receipt.issued_at_utc, row["issued_at_utc"]),
        )
        if any(str(expected) != str(observed) for expected, observed in checks):
            raise AcceptanceReceiptError("acceptance_repository_lookup_mismatch")
        persisted_reviews = self._reviews_for_acceptance(receipt)
        _require_review_acceptance_pair(persisted_reviews, receipt)
        return receipt

    def _canonical_payload(self, value: object, code: str) -> Mapping[str, object]:
        if not isinstance(value, str):
            raise AcceptanceReceiptError(code)
        try:
            payload = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise AcceptanceReceiptError(code) from exc
        if not isinstance(payload, Mapping) or _canonical_json(payload) != value:
            raise AcceptanceReceiptError(code)
        return payload

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> dict[str, Any] | None:
        cursor = self._connection.execute(sql, parameters)
        columns = tuple(str(column[0]) for column in (cursor.description or ()))
        raw = cursor.fetchone()
        if raw is None:
            return None
        return dict(zip(columns, tuple(raw), strict=True))

    def _verify_schema(self) -> None:
        rows = self._connection.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index','trigger')"
        ).fetchall()
        objects = {(str(row[0]), str(row[1])) for row in rows}
        table_names = {name for kind, name in objects if kind == "table"}
        if not set(_REQUIRED_COLUMNS).issubset(table_names):
            raise AcceptanceReceiptError("acceptance_durable_schema_missing")
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not required.issubset(columns):
                raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        if not _REQUIRED_INDEXES.issubset(
            {name for kind, name in objects if kind == "index"}
        ) or not _REQUIRED_TRIGGERS.issubset({name for kind, name in objects if kind == "trigger"}):
            raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        for index, expected_columns in _INDEX_COLUMNS.items():
            observed_columns = tuple(
                str(row[2])
                for row in self._connection.execute(f"PRAGMA index_info({index})").fetchall()
            )
            if observed_columns != expected_columns:
                raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        for table, required_sets in _UNIQUE_COLUMN_SETS.items():
            observed_unique_sets = {
                tuple(
                    str(column[2])
                    for column in self._connection.execute(
                        f"PRAGMA index_info({row[1]})"
                    ).fetchall()
                )
                for row in self._connection.execute(f"PRAGMA index_list({table})").fetchall()
                if int(row[2]) == 1
            }
            if not set(required_sets).issubset(observed_unique_sets):
                raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        for trigger, fragments in _TRIGGER_FRAGMENTS.items():
            row = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger,),
            ).fetchone()
            normalized = " ".join(str(row[0] if row else "").upper().split())
            if any(fragment not in normalized for fragment in fragments):
                raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        marker = self._fetchone(f"SELECT schema_revision FROM {_SCHEMA_TABLE} WHERE singleton=1")
        if marker is None:
            raise AcceptanceReceiptError("acceptance_durable_schema_missing")
        if str(marker["schema_revision"]) != DURABLE_ACCEPTANCE_SCHEMA_REVISION:
            raise AcceptanceReceiptError("acceptance_durable_schema_stale")
        role_marker = self._fetchone(
            """
            SELECT schema_revision
              FROM collaboration_role_assignment_schema
             WHERE singleton=1
            """
        )
        if role_marker is None:
            raise AcceptanceReceiptError("acceptance_durable_schema_missing")
        if str(role_marker["schema_revision"]) != DURABLE_ROLE_ASSIGNMENT_SCHEMA_REVISION:
            raise AcceptanceReceiptError("acceptance_durable_schema_stale")


def _role_receipt_from_row(row: Mapping[str, object]) -> RoleAssignmentReceipt:
    payload = _canonical_mapping(
        row["receipt_json"],
        fields=_ROLE_RECEIPT_FIELDS,
        code="acceptance_role_assignment_source_corrupt",
    )
    if payload["schema_version"] != ROLE_ASSIGNMENT_SCHEMA:
        raise AcceptanceReceiptError("acceptance_role_assignment_source_corrupt")
    if payload["issuer"] != ROLE_ASSIGNMENT_ISSUER:
        raise AcceptanceReceiptError("acceptance_role_assignment_source_corrupt")
    try:
        receipt = RoleAssignmentReceipt(
            assignment_id=payload["assignment_id"],  # type: ignore[arg-type]
            project=ProjectScope(payload["project_id"]),
            coordination_session_id=payload["coordination_session_id"],  # type: ignore[arg-type]
            work_item_id=payload["work_item_id"],  # type: ignore[arg-type]
            work_receipt_sha256=payload["work_receipt_sha256"],  # type: ignore[arg-type]
            lease_id=payload["lease_id"],  # type: ignore[arg-type]
            lease_sha256=payload["lease_sha256"],  # type: ignore[arg-type]
            fencing_generation=payload["fencing_generation"],  # type: ignore[arg-type]
            binding_generation=payload["binding_generation"],  # type: ignore[arg-type]
            agent_session_id=payload["agent_session_id"],  # type: ignore[arg-type]
            agent_session_sha256=payload["agent_session_sha256"],  # type: ignore[arg-type]
            agent_id=payload["agent_id"],  # type: ignore[arg-type]
            assignment_role=payload["assignment_role"],  # type: ignore[arg-type]
            use=payload["use"],  # type: ignore[arg-type]
            workflow_stage=payload["workflow_stage"],  # type: ignore[arg-type]
            intent_event_id=payload["intent_event_id"],  # type: ignore[arg-type]
            intent_event_sha256=payload["intent_event_sha256"],  # type: ignore[arg-type]
            result_receipt_sha256=payload["result_receipt_sha256"],  # type: ignore[arg-type]
            assignment_policy_revision=payload["assignment_policy_revision"],  # type: ignore[arg-type]
            issued_at_utc=payload["issued_at_utc"],  # type: ignore[arg-type]
            expires_at_utc=payload["expires_at_utc"],  # type: ignore[arg-type]
            idempotency_sha256=payload["idempotency_sha256"],  # type: ignore[arg-type]
        )
    except (RoleAssignmentError, TypeError, ValueError) as exc:
        raise AcceptanceReceiptError("acceptance_role_assignment_source_corrupt") from exc
    if receipt.to_dict() != dict(payload):
        raise AcceptanceReceiptError("acceptance_role_assignment_source_corrupt")
    column_checks = (
        (receipt.assignment_sha256, row["assignment_sha256"]),
        (receipt.assignment_id, row["assignment_id"]),
        (receipt.project.project_id, row["project_id"]),
        (receipt.coordination_session_id, row["coordination_session_id"]),
        (receipt.work_item_id, row["work_item_id"]),
        (receipt.agent_session_id, row["agent_session_id"]),
        (receipt.agent_id, row["agent_id"]),
        (receipt.assignment_role, row["assignment_role"]),
        (receipt.use, row["use"]),
        (receipt.workflow_stage, row["workflow_stage"]),
        (receipt.binding_generation, row["binding_generation"]),
    )
    if any(str(expected) != str(observed) for expected, observed in column_checks):
        raise AcceptanceReceiptError("acceptance_role_assignment_source_corrupt")
    return receipt


def _role_binding_from_row(row: Mapping[str, object]) -> RoleAssignmentBindingState:
    payload = _canonical_mapping(
        row["binding_json"],
        fields=_ROLE_BINDING_FIELDS,
        code="acceptance_role_assignment_binding_corrupt",
    )
    if payload["schema_version"] != ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA:
        raise AcceptanceReceiptError("acceptance_role_assignment_binding_corrupt")
    try:
        state = RoleAssignmentBindingState(
            assignment_sha256=payload["assignment_sha256"],  # type: ignore[arg-type]
            server_basis_sha256=payload["server_basis_sha256"],  # type: ignore[arg-type]
            binding_generation=payload["binding_generation"],  # type: ignore[arg-type]
            revoked_at_utc=payload["revoked_at_utc"],  # type: ignore[arg-type]
        )
    except (RoleAssignmentError, TypeError, ValueError) as exc:
        raise AcceptanceReceiptError("acceptance_role_assignment_binding_corrupt") from exc
    if state.to_dict() != dict(payload):
        raise AcceptanceReceiptError("acceptance_role_assignment_binding_corrupt")
    if (
        not hmac.compare_digest(_sha256(payload), str(row["binding_sha256"]))
        or not hmac.compare_digest(state.assignment_sha256, str(row["assignment_sha256"]))
        or not hmac.compare_digest(
            state.server_basis_sha256,
            str(row["server_basis_sha256"]),
        )
        or state.binding_generation != int(row["binding_generation"])
        or state.revoked_at_utc != str(row["revoked_at_utc"])
    ):
        raise AcceptanceReceiptError("acceptance_role_assignment_binding_corrupt")
    return state


def _server_basis_sha256(
    basis: Mapping[str, object],
    *,
    receipt: RoleAssignmentReceipt,
    session: AgentSession,
    work: WorkReceipt,
    result: ResultReceipt | None,
    lease: _LeaseLineage,
    submitter_session_id: str,
) -> str:
    session_projection = _exact_mapping(
        basis["session"],
        fields=frozenset(session.to_dict()),
        code="acceptance_role_assignment_basis_corrupt",
    )
    work_projection = _exact_mapping(
        basis["work"],
        fields=frozenset(work.to_dict()),
        code="acceptance_role_assignment_basis_corrupt",
    )
    lease_projection = _mapping(
        basis["lease"],
        "acceptance_role_assignment_basis_corrupt",
    )
    intent_projection = _mapping(
        basis["intent_event"],
        "acceptance_role_assignment_basis_corrupt",
    )
    if (
        dict(session_projection) != session.to_dict()
        or dict(work_projection) != work.to_dict()
        or dict(lease_projection) != dict(lease.projection)
        or not hmac.compare_digest(_sha256(lease_projection), receipt.lease_sha256)
        or not hmac.compare_digest(_sha256(intent_projection), receipt.intent_event_sha256)
    ):
        raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
    expected_result = result.to_dict() if result is not None else None
    if basis["result"] != expected_result:
        raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
    if basis["use"] != receipt.use or basis["workflow_stage"] != receipt.workflow_stage:
        raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
    if str(basis["submitter_agent_session_id"] or "") != submitter_session_id:
        raise AcceptanceReceiptError("acceptance_role_assignment_basis_corrupt")
    intent_actor = _mapping(
        intent_projection.get("actor"),
        "acceptance_role_assignment_basis_corrupt",
    )
    return _sha256(
        {
            "schema_version": ROLE_ASSIGNMENT_BINDING_STATE_SCHEMA,
            "assignment_policy_revision": ROLE_ASSIGNMENT_POLICY_REVISION,
            "use": receipt.use,
            "assignment_role": receipt.assignment_role,
            "workflow_stage": receipt.workflow_stage,
            "work_state": str(basis["work_state"] or "").strip().casefold(),
            "lease_state": str(basis["lease_state"] or "").strip().casefold(),
            "project_id": work.project.project_id,
            "coordination_session_id": work.coordination_session_id,
            "agent_session_id": session.session_id,
            "agent_session_sha256": session.content_sha256,
            "agent_id": session.identity.agent_id,
            "work_item_id": work.work_item_id,
            "work_receipt_sha256": work.content_sha256,
            "work_assigned_agent_id": work.assigned_agent.agent_id,
            "lease_id": lease.lease_id,
            "lease_sha256": lease.lease_sha256,
            "lease_owner_id": lease.owner_id,
            "fencing_generation": lease.fencing_generation,
            "intent_event_id": receipt.intent_event_id,
            "intent_event_sha256": receipt.intent_event_sha256,
            "intent_actor_agent_id": str(intent_actor.get("agent_id") or ""),
            "result_receipt_sha256": result.content_sha256 if result is not None else "",
            "result_submitter_agent_id": (
                result.submitted_by.agent_id if result is not None else ""
            ),
            "submitter_agent_session_id": submitter_session_id,
        }
    )


def _canonical_mapping(
    value: object,
    *,
    fields: frozenset[str],
    code: str,
) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise AcceptanceReceiptError(code)
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceReceiptError(code) from exc
    exact = _exact_mapping(payload, fields=fields, code=code)
    if _canonical_json(exact) != value:
        raise AcceptanceReceiptError(code)
    return exact


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AcceptanceReceiptError(code)
    return value


def _exact_mapping(
    value: object,
    *,
    fields: frozenset[str],
    code: str,
) -> Mapping[str, object]:
    payload = _mapping(value, code)
    if set(payload) != fields or any(not isinstance(key, str) for key in payload):
        raise AcceptanceReceiptError(code)
    return payload


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "DURABLE_ACCEPTANCE_SCHEMA_REVISION",
    "DurableAcceptanceAuthorityRepository",
]
