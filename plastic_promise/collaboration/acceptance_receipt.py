"""Server-issued acceptance decisions for project-scoped Agent work.

``ReviewReceipt`` is a bounded reviewer assertion.  It is deliberately not
authority: a caller can construct and transport it, but it cannot make a work
result accepted.  ``AcceptanceReceiptAuthority`` is the server-only seam that
requires the exact assignment, result, and Agent sessions to exist in a
server-owned source registry, then validates independent sessions, the current
reviewer policy binding, source revision, conflict state, timing, and all
receipt/evidence digests before issuing an ``AcceptanceReceipt``.

The authority depends on the narrow ``AcceptanceAuthorityRepository`` seam.
The in-memory adapter keeps focused contract tests deterministic; the durable
SQLite adapter lives in ``durable_acceptance_store`` and preserves exact
issuance evidence across restarts without turning portable receipt values into
bearer capabilities.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

from .contracts import (
    AgentIdentity,
    AgentSession,
    CollaborationContractError,
    CollaborationEvent,
    ProjectScope,
    ResultReceipt,
    WorkReceipt,
)
from .policy_binding import AgentPolicyBinding, AgentPolicyBindingAuthority
from .role_assignment import (
    ACCEPTANCE_REVIEW_USE,
    RESULT_SUBMISSION_USE,
    ROLE_ASSIGNMENT_POLICY_REVISION,
    WORK_REVIEWER_ROLE,
    WORK_SUBMITTER_ROLE,
    RoleAssignmentAuthority,
    RoleAssignmentError,
    VerifiedRoleAssignment,
)

ACCEPTANCE_RECEIPT_SCHEMA = "collaboration-acceptance/v3"
REVIEW_RECEIPT_SCHEMA = "collaboration-review/v3"
ACCEPTANCE_RECEIPT_ISSUER = "pp-server-backend"
REVIEW_RECEIPT_ISSUER = "reviewer-assertion"

_ACCEPTANCE_DECISIONS = frozenset({"accepted", "rejected"})
_CONFLICT_STATES = frozenset({"none", "resolved", "unresolved"})
REVIEW_CHANNELS = ("standards", "spec", "deepsec")
_REVIEW_CHANNEL_SET = frozenset(REVIEW_CHANNELS)
_EMPTY_SHA256 = "sha256:" + "0" * 64
_DEFAULT_UNION_CONTRACT_REVISION = "union-six-pr-contract/v1"
_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_PINNED_SOURCE_REVISION = re.compile(r"\A[0-9a-f]{40,64}\Z")
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SERVER_AUTHORITY_TOKEN = object()
_SERVER_SOURCE_REGISTRY_TOKEN = object()

_REVIEW_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "authority",
        "issuer",
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
        "evidence_refs",
        "evidence_sha256",
    }
)
_ACCEPTANCE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "authority",
        "issuer",
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
        "review_receipt_bindings",
        "submitter_agent_session_id",
        "submitter_agent_session_sha256",
        "reviewer_agent_session_id",
        "reviewer_agent_session_sha256",
        "submitter_assignment_sha256",
        "reviewer_assignment_sha256",
        "submitted_by",
        "accepted_by",
        "assignment_policy_revision",
        "review_policy_revision",
        "source_revision",
        "diff_digest",
        "requirement_set_digest",
        "union_contract_revision",
        "digests",
        "evidence_refs",
        "evidence_sha256",
        "decision",
        "conflict_state",
        "issued_at_utc",
    }
)
_ACCEPTANCE_DIGEST_FIELDS = frozenset(
    {
        "work",
        "result",
        "submitter_assignment",
        "reviewer_assignment",
        "evidence",
    }
)
_AGENT_IDENTITY_FIELDS = frozenset({"agent_id", "role", "parent_agent_id", "capabilities"})
_REVIEW_BINDING_FIELDS = frozenset({"review_channel", "review_receipt_id", "review_receipt_sha256"})


class AcceptanceReceiptError(ValueError):
    """Stable, non-sensitive error raised by acceptance contracts."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _AcceptanceVerificationToken:
    """Unforgeable-by-value capability owned by one authority instance."""

    __slots__ = ()

    def __init__(self, *, _server_token: object | None = None) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise AcceptanceReceiptError("acceptance_verification_token_server_required")


@runtime_checkable
class AcceptanceAuthorityRepository(Protocol):
    """Canonical source plus append-only receipt seam used by the authority.

    Portable values cross this seam only as evidence.  Implementations must
    resolve canonical work/session/lease/role lineage themselves and may
    append receipts only when handed the private server token.
    """

    def require_canonical_sources(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        submitter_assignment_sha256: str,
        reviewer_assignment_sha256: str,
    ) -> None: ...

    def load_acceptance_by_id(
        self,
        acceptance_receipt_id: str,
    ) -> AcceptanceReceipt | None: ...

    def load_acceptance_by_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        work_item_id: str,
        result_receipt_sha256: str,
    ) -> AcceptanceReceipt | None: ...

    def load_review_by_digest(
        self,
        review_receipt_sha256: str,
    ) -> ReviewReceipt | None: ...

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
    ) -> AcceptanceReceipt: ...


class ServerAcceptanceSourceRegistry:
    """In-memory acceptance repository for focused tests and composition.

    The registry stores immutable source identities and content digests, not
    caller-provided verifier callbacks. Registration is idempotent for the
    exact same value and fails closed on identity reuse with different bytes.
    A result cannot be registered before its exact WorkReceipt source exists.

    Receipt issuance is also retained in memory so this adapter exercises the
    same repository interface as the durable SQLite adapter.  It deliberately
    grants no restart authority.
    """

    __slots__ = (
        "_acceptance_by_binding",
        "_acceptance_by_id",
        "_agent_session_digests",
        "_review_by_binding",
        "_review_by_digest",
        "_review_by_id",
        "_result_receipt_digests",
        "_work_bindings",
        "_work_receipt_digests",
    )

    def __init__(self, *, _server_token: object | None = None) -> None:
        if _server_token is not _SERVER_SOURCE_REGISTRY_TOKEN:
            raise AcceptanceReceiptError("acceptance_source_registry_server_required")
        self._work_receipt_digests: dict[tuple[str, str, str, str], str] = {}
        self._work_bindings: set[tuple[str, str, str, str]] = set()
        self._result_receipt_digests: dict[tuple[str, str, str, str], str] = {}
        self._agent_session_digests: dict[tuple[str, str, str], str] = {}
        self._review_by_digest: dict[str, ReviewReceipt] = {}
        self._review_by_id: dict[str, ReviewReceipt] = {}
        self._review_by_binding: dict[tuple[str, str, str, str, str, str], ReviewReceipt] = {}
        self._acceptance_by_id: dict[str, AcceptanceReceipt] = {}
        self._acceptance_by_binding: dict[tuple[str, str, str, str], AcceptanceReceipt] = {}

    def register(
        self,
        *sources: WorkReceipt | ResultReceipt | AgentSession,
    ) -> None:
        """Atomically register exact canonical values from the server write path."""

        if not sources:
            raise AcceptanceReceiptError("acceptance_source_registry_empty")
        if any(
            type(source) not in {WorkReceipt, ResultReceipt, AgentSession} for source in sources
        ):
            raise AcceptanceReceiptError("acceptance_source_registry_type_invalid")

        work_receipts = dict(self._work_receipt_digests)
        work_bindings = set(self._work_bindings)
        result_receipts = dict(self._result_receipt_digests)
        agent_sessions = dict(self._agent_session_digests)

        for source in sources:
            if type(source) is WorkReceipt:
                self._register_digest(
                    work_receipts,
                    _work_source_key(source),
                    source.content_sha256,
                    "acceptance_work_source_conflict",
                )
                work_bindings.add(
                    (
                        source.project.project_id,
                        source.coordination_session_id,
                        source.work_item_id,
                        source.content_sha256,
                    )
                )

        for source in sources:
            if type(source) is AgentSession:
                self._register_digest(
                    agent_sessions,
                    _agent_session_source_key(source),
                    source.content_sha256,
                    "acceptance_session_source_conflict",
                )

        for source in sources:
            if type(source) is ResultReceipt:
                work_binding = (
                    source.project.project_id,
                    source.coordination_session_id,
                    source.work_item_id,
                    source.work_receipt_sha256,
                )
                if work_binding not in work_bindings:
                    raise AcceptanceReceiptError("acceptance_result_work_source_unregistered")
                self._register_digest(
                    result_receipts,
                    _result_source_key(source),
                    source.content_sha256,
                    "acceptance_result_source_conflict",
                )

        self._work_receipt_digests = work_receipts
        self._work_bindings = work_bindings
        self._result_receipt_digests = result_receipts
        self._agent_session_digests = agent_sessions

    @staticmethod
    def _register_digest(
        records: dict[tuple[str, ...], str],
        key: tuple[str, ...],
        digest: str,
        conflict_code: str,
    ) -> None:
        existing = records.get(key)
        if existing is not None and not hmac.compare_digest(existing, digest):
            raise AcceptanceReceiptError(conflict_code)
        records[key] = digest

    def _contains_work(self, value: WorkReceipt) -> bool:
        return self._contains(
            self._work_receipt_digests,
            _work_source_key(value),
            value.content_sha256,
        )

    def _contains_result(self, value: ResultReceipt) -> bool:
        return self._contains(
            self._result_receipt_digests,
            _result_source_key(value),
            value.content_sha256,
        )

    def _contains_agent_session(self, value: AgentSession) -> bool:
        return self._contains(
            self._agent_session_digests,
            _agent_session_source_key(value),
            value.content_sha256,
        )

    @staticmethod
    def _contains(
        records: dict[tuple[str, ...], str],
        key: tuple[str, ...],
        digest: str,
    ) -> bool:
        existing = records.get(key)
        return existing is not None and hmac.compare_digest(existing, digest)

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
        """Require the exact source values registered by the server write path."""

        del submitter_assignment_sha256, reviewer_assignment_sha256
        for verified, code in (
            (self._contains_work(work), "acceptance_work_source_unverified"),
            (self._contains_result(result), "acceptance_result_source_unverified"),
            (
                self._contains_agent_session(submitter_session),
                "acceptance_submitter_session_source_unverified",
            ),
            (
                self._contains_agent_session(reviewer_session),
                "acceptance_reviewer_session_source_unverified",
            ),
        ):
            if not verified:
                raise AcceptanceReceiptError(code)

    def load_acceptance_by_id(
        self,
        acceptance_receipt_id: str,
    ) -> AcceptanceReceipt | None:
        receipt_id = _safe_identifier(
            acceptance_receipt_id,
            "acceptance_receipt_id_invalid",
        )
        return self._acceptance_by_id.get(receipt_id)

    def load_acceptance_by_binding(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        work_item_id: str,
        result_receipt_sha256: str,
    ) -> AcceptanceReceipt | None:
        key = _acceptance_binding_key(
            project_id=project_id,
            coordination_session_id=coordination_session_id,
            work_item_id=work_item_id,
            result_receipt_sha256=result_receipt_sha256,
        )
        return self._acceptance_by_binding.get(key)

    def load_review_by_digest(
        self,
        review_receipt_sha256: str,
    ) -> ReviewReceipt | None:
        return self._review_by_digest.get(
            _require_sha256(
                review_receipt_sha256,
                "review_receipt_digest_invalid",
            )
        )

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
        """Atomically retain exact review and acceptance receipts in memory."""

        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise AcceptanceReceiptError("acceptance_repository_write_authority_required")
        _require_review_acceptance_pair(reviews, receipt)
        _require_acceptance_source_pair(
            receipt,
            work=work,
            result=result,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
        )
        self.require_canonical_sources(
            work,
            result,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
            submitter_assignment_sha256=receipt.submitter_assignment_sha256,
            reviewer_assignment_sha256=receipt.reviewer_assignment_sha256,
        )

        review_by_digest = dict(self._review_by_digest)
        review_by_id = dict(self._review_by_id)
        review_by_binding = dict(self._review_by_binding)
        acceptance_by_id = dict(self._acceptance_by_id)
        acceptance_by_binding = dict(self._acceptance_by_binding)

        for review in reviews:
            review_key = _review_binding_key(review)
            existing_review = review_by_id.get(review.review_receipt_id)
            if existing_review is None:
                existing_review = review_by_binding.get(review_key)
            if existing_review is None:
                existing_review = review_by_digest.get(review.content_sha256)
            if existing_review is not None and not hmac.compare_digest(
                existing_review.content_sha256,
                review.content_sha256,
            ):
                raise AcceptanceReceiptError("review_receipt_replay_conflict")
            canonical_review = existing_review or review
            review_by_digest[canonical_review.content_sha256] = canonical_review
            review_by_id[canonical_review.review_receipt_id] = canonical_review
            review_by_binding[review_key] = canonical_review

        acceptance_key = _acceptance_binding_key_from_receipt(receipt)
        existing = acceptance_by_id.get(receipt.acceptance_receipt_id)
        binding_existing = acceptance_by_binding.get(acceptance_key)
        if existing is not None and not hmac.compare_digest(
            existing.content_sha256,
            receipt.content_sha256,
        ):
            raise AcceptanceReceiptError("acceptance_receipt_replay_conflict")
        if binding_existing is not None and not hmac.compare_digest(
            binding_existing.content_sha256,
            receipt.content_sha256,
        ):
            if binding_existing.decision != receipt.decision:
                raise AcceptanceReceiptError("acceptance_receipt_decision_conflict")
            raise AcceptanceReceiptError("acceptance_receipt_replay_ambiguous")
        canonical = existing or binding_existing or receipt
        acceptance_by_id[canonical.acceptance_receipt_id] = canonical
        acceptance_by_binding[acceptance_key] = canonical

        self._review_by_digest = review_by_digest
        self._review_by_id = review_by_id
        self._review_by_binding = review_by_binding
        self._acceptance_by_id = acceptance_by_id
        self._acceptance_by_binding = acceptance_by_binding
        return canonical


InMemoryAcceptanceAuthorityRepository = ServerAcceptanceSourceRegistry


@dataclass(frozen=True, slots=True)
class ReviewReceipt:
    """One bounded review assertion; it grants no reviewer authority."""

    review_receipt_id: str
    project: ProjectScope
    coordination_session_id: str
    work_item_id: str
    work_receipt_sha256: str
    result_receipt_sha256: str
    reviewer_assignment_sha256: str
    reviewer_agent_session_id: str
    review_policy_revision: str
    source_revision: str
    decision: str
    conflict_state: str
    reviewed_at_utc: str
    evidence_refs: tuple[str, ...]
    evidence_sha256: str
    review_channel: str = "standards"
    diff_digest: str = _EMPTY_SHA256
    requirement_set_digest: str = _EMPTY_SHA256
    union_contract_revision: str = _DEFAULT_UNION_CONTRACT_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise AcceptanceReceiptError("review_receipt_project_invalid")
        decision = _decision(self.decision, "review_receipt_decision_invalid")
        conflict_state = _conflict_state(
            self.conflict_state,
            "review_receipt_conflict_state_invalid",
        )
        _require_sha256(
            self.work_receipt_sha256,
            "review_receipt_work_digest_invalid",
        )
        _require_sha256(
            self.result_receipt_sha256,
            "review_receipt_result_digest_invalid",
        )
        _require_sha256(
            self.reviewer_assignment_sha256,
            "review_receipt_role_assignment_digest_invalid",
        )
        _require_sha256(
            self.evidence_sha256,
            "review_receipt_evidence_digest_invalid",
        )
        source_revision = _require_pinned_source_revision(self.source_revision)
        review_channel = _review_channel(
            self.review_channel,
            "review_receipt_channel_invalid",
        )
        diff_digest = _require_sha256(
            self.diff_digest,
            "review_receipt_diff_digest_invalid",
        )
        requirement_set_digest = _require_sha256(
            self.requirement_set_digest,
            "review_receipt_requirement_set_digest_invalid",
        )
        union_contract_revision = _safe_identifier(
            self.union_contract_revision,
            "review_receipt_union_contract_revision_invalid",
        )
        probe = _public_probe(
            event_id=self.review_receipt_id,
            project=self.project,
            coordination_session_id=self.coordination_session_id,
            work_item_id=self.work_item_id,
            actor=AgentIdentity("agent:review-receipt-validator", "reviewer"),
            created_at=self.reviewed_at_utc,
            subject_refs=(
                self.reviewer_agent_session_id,
                self.review_policy_revision,
                f"source-revision:{source_revision}",
                f"review-channel:{review_channel}",
                f"diff-digest:{diff_digest}",
                f"requirement-set-digest:{requirement_set_digest}",
                f"union-contract:{union_contract_revision}",
            ),
            evidence_refs=self.evidence_refs,
            error_prefix="review_receipt",
        )
        if not probe.evidence_refs:
            raise AcceptanceReceiptError("review_receipt_evidence_required")
        object.__setattr__(self, "review_receipt_id", probe.event_id)
        object.__setattr__(self, "coordination_session_id", probe.coordination_session_id)
        object.__setattr__(self, "work_item_id", probe.work_item_id)
        object.__setattr__(self, "reviewer_agent_session_id", probe.subject_refs[0])
        object.__setattr__(self, "review_policy_revision", probe.subject_refs[1])
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "review_channel", review_channel)
        object.__setattr__(self, "diff_digest", diff_digest)
        object.__setattr__(self, "requirement_set_digest", requirement_set_digest)
        object.__setattr__(self, "union_contract_revision", union_contract_revision)
        object.__setattr__(self, "reviewed_at_utc", probe.created_at)
        object.__setattr__(self, "evidence_refs", probe.evidence_refs)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "conflict_state", conflict_state)

    @classmethod
    def for_result(
        cls,
        work: WorkReceipt,
        result: ResultReceipt,
        *,
        review_receipt_id: str,
        reviewer_agent_session_id: str,
        reviewer_assignment_sha256: str,
        review_policy_revision: str,
        source_revision: str,
        decision: str,
        conflict_state: str,
        reviewed_at_utc: str,
        evidence_refs: tuple[str, ...],
        review_channel: str = "standards",
        diff_digest: str = _EMPTY_SHA256,
        requirement_set_digest: str = _EMPTY_SHA256,
        union_contract_revision: str = _DEFAULT_UNION_CONTRACT_REVISION,
    ) -> ReviewReceipt:
        """Bind a reviewer assertion to the exact assignment and result digests."""

        _require_work_result_binding(work, result)
        normalized_evidence = _normalize_evidence_refs(
            evidence_refs,
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            work_item_id=work.work_item_id,
            created_at=reviewed_at_utc,
            error_prefix="review_receipt",
        )
        return cls(
            review_receipt_id=review_receipt_id,
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            work_item_id=work.work_item_id,
            work_receipt_sha256=work.content_sha256,
            result_receipt_sha256=result.content_sha256,
            reviewer_assignment_sha256=reviewer_assignment_sha256,
            reviewer_agent_session_id=reviewer_agent_session_id,
            review_policy_revision=review_policy_revision,
            source_revision=source_revision,
            decision=decision,
            conflict_state=conflict_state,
            reviewed_at_utc=reviewed_at_utc,
            evidence_refs=normalized_evidence,
            evidence_sha256=_review_evidence_digest(normalized_evidence),
            review_channel=review_channel,
            diff_digest=diff_digest,
            requirement_set_digest=requirement_set_digest,
            union_contract_revision=union_contract_revision,
        )

    def validate_integrity(self) -> None:
        """Reject evidence-reference tampering after value construction."""

        expected = _review_evidence_digest(self.evidence_refs)
        if not hmac.compare_digest(self.evidence_sha256, expected):
            raise AcceptanceReceiptError("review_receipt_evidence_digest_mismatch")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewReceipt:
        """Strictly rehydrate one portable review assertion."""

        payload = _exact_mapping(
            value,
            fields=_REVIEW_RECEIPT_FIELDS,
            code="review_receipt_projection_invalid",
        )
        if payload["schema_version"] != REVIEW_RECEIPT_SCHEMA:
            raise AcceptanceReceiptError("review_receipt_schema_invalid")
        if payload["authority"] != "reviewer-assertion-only":
            raise AcceptanceReceiptError("review_receipt_authority_invalid")
        if payload["issuer"] != REVIEW_RECEIPT_ISSUER:
            raise AcceptanceReceiptError("review_receipt_issuer_invalid")
        try:
            receipt = cls(
                review_receipt_id=payload["review_receipt_id"],  # type: ignore[arg-type]
                project=ProjectScope(payload["project_id"]),
                coordination_session_id=payload["coordination_session_id"],  # type: ignore[arg-type]
                work_item_id=payload["work_item_id"],  # type: ignore[arg-type]
                work_receipt_sha256=payload["work_receipt_sha256"],  # type: ignore[arg-type]
                result_receipt_sha256=payload["result_receipt_sha256"],  # type: ignore[arg-type]
                reviewer_assignment_sha256=payload["reviewer_assignment_sha256"],  # type: ignore[arg-type]
                reviewer_agent_session_id=payload["reviewer_agent_session_id"],  # type: ignore[arg-type]
                review_policy_revision=payload["review_policy_revision"],  # type: ignore[arg-type]
                source_revision=payload["source_revision"],  # type: ignore[arg-type]
                review_channel=payload["review_channel"],  # type: ignore[arg-type]
                diff_digest=payload["diff_digest"],  # type: ignore[arg-type]
                requirement_set_digest=payload["requirement_set_digest"],  # type: ignore[arg-type]
                union_contract_revision=payload["union_contract_revision"],  # type: ignore[arg-type]
                decision=payload["decision"],  # type: ignore[arg-type]
                conflict_state=payload["conflict_state"],  # type: ignore[arg-type]
                reviewed_at_utc=payload["reviewed_at_utc"],  # type: ignore[arg-type]
                evidence_refs=_strict_string_tuple(
                    payload["evidence_refs"],
                    "review_receipt_projection_invalid",
                ),
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
            )
        except AcceptanceReceiptError:
            raise
        except (CollaborationContractError, TypeError, ValueError) as exc:
            raise AcceptanceReceiptError("review_receipt_projection_invalid") from exc
        receipt.validate_integrity()
        if receipt.to_dict() != dict(payload):
            raise AcceptanceReceiptError("review_receipt_projection_invalid")
        return receipt

    @classmethod
    def from_canonical_json(
        cls,
        value: str,
        *,
        expected_sha256: str | None = None,
    ) -> ReviewReceipt:
        """Decode canonical JSON and optionally bind it to an external digest."""

        payload = _canonical_mapping(value, "review_receipt_json_invalid")
        receipt = cls.from_dict(payload)
        if expected_sha256 is not None and not hmac.compare_digest(
            receipt.content_sha256,
            _require_sha256(expected_sha256, "review_receipt_digest_invalid"),
        ):
            raise AcceptanceReceiptError("review_receipt_digest_mismatch")
        return receipt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_RECEIPT_SCHEMA,
            "authority": "reviewer-assertion-only",
            "issuer": REVIEW_RECEIPT_ISSUER,
            "review_receipt_id": self.review_receipt_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "work_item_id": self.work_item_id,
            "work_receipt_sha256": self.work_receipt_sha256,
            "result_receipt_sha256": self.result_receipt_sha256,
            "reviewer_assignment_sha256": self.reviewer_assignment_sha256,
            "reviewer_agent_session_id": self.reviewer_agent_session_id,
            "review_policy_revision": self.review_policy_revision,
            "source_revision": self.source_revision,
            "review_channel": self.review_channel,
            "diff_digest": self.diff_digest,
            "requirement_set_digest": self.requirement_set_digest,
            "union_contract_revision": self.union_contract_revision,
            "decision": self.decision,
            "conflict_state": self.conflict_state,
            "reviewed_at_utc": self.reviewed_at_utc,
            "evidence_refs": list(self.evidence_refs),
            "evidence_sha256": self.evidence_sha256,
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ReviewReceiptBinding:
    """Canonical channel-to-review binding embedded in an acceptance receipt."""

    review_channel: str
    review_receipt_id: str
    review_receipt_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "review_channel",
            _review_channel(self.review_channel, "acceptance_review_channel_invalid"),
        )
        object.__setattr__(
            self,
            "review_receipt_id",
            _safe_identifier(
                self.review_receipt_id,
                "acceptance_review_receipt_id_invalid",
            ),
        )
        object.__setattr__(
            self,
            "review_receipt_sha256",
            _require_sha256(
                self.review_receipt_sha256,
                "acceptance_review_receipt_digest_invalid",
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> ReviewReceiptBinding:
        payload = _exact_mapping(
            value,
            fields=_REVIEW_BINDING_FIELDS,
            code="acceptance_review_binding_invalid",
        )
        try:
            return cls(
                review_channel=payload["review_channel"],  # type: ignore[arg-type]
                review_receipt_id=payload["review_receipt_id"],  # type: ignore[arg-type]
                review_receipt_sha256=payload["review_receipt_sha256"],  # type: ignore[arg-type]
            )
        except AcceptanceReceiptError:
            raise
        except (TypeError, ValueError) as exc:
            raise AcceptanceReceiptError("acceptance_review_binding_invalid") from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "review_channel": self.review_channel,
            "review_receipt_id": self.review_receipt_id,
            "review_receipt_sha256": self.review_receipt_sha256,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReceipt:
    """Immutable server decision over one exact WorkReceipt and ResultReceipt.

    The value is safe to transport, but possessing or reconstructing it is not
    authority.  Consumers that can mutate state must call the issuing server's
    ``verify_issued`` seam before acting on it.
    """

    acceptance_receipt_id: str
    project: ProjectScope
    coordination_session_id: str
    work_item_id: str
    work_receipt_id: str
    work_receipt_sha256: str
    result_receipt_id: str
    result_receipt_sha256: str
    review_receipt_id: str
    review_receipt_sha256: str
    review_receipt_bindings: tuple[ReviewReceiptBinding, ...]
    submitter_agent_session_id: str
    submitter_agent_session_sha256: str
    reviewer_agent_session_id: str
    reviewer_agent_session_sha256: str
    submitter_assignment_sha256: str
    reviewer_assignment_sha256: str
    submitted_by: AgentIdentity
    accepted_by: AgentIdentity
    assignment_policy_revision: str
    review_policy_revision: str
    source_revision: str
    diff_digest: str
    requirement_set_digest: str
    union_contract_revision: str
    evidence_refs: tuple[str, ...]
    evidence_sha256: str
    decision: str
    conflict_state: str
    issued_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise AcceptanceReceiptError("acceptance_receipt_project_invalid")
        if not isinstance(self.submitted_by, AgentIdentity):
            raise AcceptanceReceiptError("acceptance_receipt_submitter_invalid")
        if not isinstance(self.accepted_by, AgentIdentity):
            raise AcceptanceReceiptError("acceptance_receipt_reviewer_invalid")
        decision = _decision(self.decision, "acceptance_receipt_decision_invalid")
        conflict_state = _conflict_state(
            self.conflict_state,
            "acceptance_receipt_conflict_state_invalid",
        )
        if conflict_state == "unresolved":
            raise AcceptanceReceiptError("acceptance_receipt_conflict_unresolved")
        for value, code in (
            (self.work_receipt_sha256, "acceptance_receipt_work_digest_invalid"),
            (self.result_receipt_sha256, "acceptance_receipt_result_digest_invalid"),
            (self.review_receipt_sha256, "acceptance_receipt_review_digest_invalid"),
            (
                self.submitter_agent_session_sha256,
                "acceptance_receipt_submitter_session_digest_invalid",
            ),
            (
                self.reviewer_agent_session_sha256,
                "acceptance_receipt_reviewer_session_digest_invalid",
            ),
            (
                self.submitter_assignment_sha256,
                "acceptance_receipt_submitter_assignment_digest_invalid",
            ),
            (
                self.reviewer_assignment_sha256,
                "acceptance_receipt_reviewer_assignment_digest_invalid",
            ),
            (self.evidence_sha256, "acceptance_receipt_evidence_digest_invalid"),
        ):
            _require_sha256(value, code)
        bindings = _normalize_review_bindings(self.review_receipt_bindings)
        standards_binding = bindings[0]
        if standards_binding.review_receipt_id != self.review_receipt_id:
            raise AcceptanceReceiptError("acceptance_primary_review_receipt_mismatch")
        if not hmac.compare_digest(
            standards_binding.review_receipt_sha256,
            self.review_receipt_sha256,
        ):
            raise AcceptanceReceiptError("acceptance_primary_review_digest_mismatch")
        diff_digest = _require_sha256(
            self.diff_digest,
            "acceptance_receipt_diff_digest_invalid",
        )
        requirement_set_digest = _require_sha256(
            self.requirement_set_digest,
            "acceptance_receipt_requirement_set_digest_invalid",
        )
        union_contract_revision = _safe_identifier(
            self.union_contract_revision,
            "acceptance_receipt_union_contract_revision_invalid",
        )
        if self.assignment_policy_revision != ROLE_ASSIGNMENT_POLICY_REVISION:
            raise AcceptanceReceiptError("acceptance_assignment_policy_stale")
        source_revision = _require_pinned_source_revision(self.source_revision)
        probe = _public_probe(
            event_id=self.acceptance_receipt_id,
            project=self.project,
            coordination_session_id=self.coordination_session_id,
            work_item_id=self.work_item_id,
            actor=self.accepted_by,
            created_at=self.issued_at_utc,
            subject_refs=(
                self.work_receipt_id,
                self.result_receipt_id,
                self.review_receipt_id,
                self.submitter_agent_session_id,
                self.reviewer_agent_session_id,
                self.assignment_policy_revision,
                self.review_policy_revision,
                f"source-revision:{source_revision}",
                f"diff-digest:{diff_digest}",
                f"requirement-set-digest:{requirement_set_digest}",
                f"union-contract:{union_contract_revision}",
            ),
            evidence_refs=self.evidence_refs,
            error_prefix="acceptance_receipt",
        )
        if not probe.evidence_refs:
            raise AcceptanceReceiptError("acceptance_receipt_evidence_required")
        object.__setattr__(self, "acceptance_receipt_id", probe.event_id)
        object.__setattr__(self, "coordination_session_id", probe.coordination_session_id)
        object.__setattr__(self, "work_item_id", probe.work_item_id)
        object.__setattr__(self, "work_receipt_id", probe.subject_refs[0])
        object.__setattr__(self, "result_receipt_id", probe.subject_refs[1])
        object.__setattr__(self, "review_receipt_id", probe.subject_refs[2])
        object.__setattr__(self, "submitter_agent_session_id", probe.subject_refs[3])
        object.__setattr__(self, "reviewer_agent_session_id", probe.subject_refs[4])
        object.__setattr__(self, "assignment_policy_revision", probe.subject_refs[5])
        object.__setattr__(self, "review_policy_revision", probe.subject_refs[6])
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "diff_digest", diff_digest)
        object.__setattr__(self, "requirement_set_digest", requirement_set_digest)
        object.__setattr__(self, "union_contract_revision", union_contract_revision)
        object.__setattr__(self, "review_receipt_bindings", bindings)
        object.__setattr__(self, "issued_at_utc", probe.created_at)
        object.__setattr__(self, "evidence_refs", probe.evidence_refs)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "conflict_state", conflict_state)

    @property
    def receipt_id(self) -> str:
        """Compatibility alias for existing projection consumers."""

        return self.acceptance_receipt_id

    @property
    def accepted_at(self) -> str:
        """Compatibility alias for existing projection consumers."""

        return self.issued_at_utc

    @property
    def verdict(self) -> str:
        """Compatibility alias; new callers should use ``decision``."""

        return self.decision

    def validate_integrity(self) -> None:
        """Reject evidence-lineage tampering after value construction."""

        expected = _acceptance_evidence_digest(
            review_receipt_bindings=self.review_receipt_bindings,
            result_receipt_sha256=self.result_receipt_sha256,
            submitter_assignment_sha256=self.submitter_assignment_sha256,
            reviewer_assignment_sha256=self.reviewer_assignment_sha256,
            source_revision=self.source_revision,
            diff_digest=self.diff_digest,
            requirement_set_digest=self.requirement_set_digest,
            union_contract_revision=self.union_contract_revision,
            evidence_refs=self.evidence_refs,
        )
        if not hmac.compare_digest(self.evidence_sha256, expected):
            raise AcceptanceReceiptError("acceptance_receipt_evidence_digest_mismatch")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AcceptanceReceipt:
        """Strictly rehydrate one portable server acceptance decision."""

        payload = _exact_mapping(
            value,
            fields=_ACCEPTANCE_RECEIPT_FIELDS,
            code="acceptance_receipt_projection_invalid",
        )
        if payload["schema_version"] != ACCEPTANCE_RECEIPT_SCHEMA:
            raise AcceptanceReceiptError("acceptance_receipt_schema_invalid")
        if payload["authority"] != "server-authenticated-decision":
            raise AcceptanceReceiptError("acceptance_receipt_authority_invalid")
        if payload["issuer"] != ACCEPTANCE_RECEIPT_ISSUER:
            raise AcceptanceReceiptError("acceptance_receipt_issuer_invalid")
        digests = _exact_mapping(
            payload["digests"],
            fields=_ACCEPTANCE_DIGEST_FIELDS,
            code="acceptance_receipt_projection_invalid",
        )
        for nested, top_level in (
            ("work", "work_receipt_sha256"),
            ("result", "result_receipt_sha256"),
            ("submitter_assignment", "submitter_assignment_sha256"),
            ("reviewer_assignment", "reviewer_assignment_sha256"),
            ("evidence", "evidence_sha256"),
        ):
            if digests[nested] != payload[top_level]:
                raise AcceptanceReceiptError("acceptance_receipt_digest_projection_mismatch")
        try:
            receipt = cls(
                acceptance_receipt_id=payload["acceptance_receipt_id"],  # type: ignore[arg-type]
                project=ProjectScope(payload["project_id"]),
                coordination_session_id=payload["coordination_session_id"],  # type: ignore[arg-type]
                work_item_id=payload["work_item_id"],  # type: ignore[arg-type]
                work_receipt_id=payload["work_receipt_id"],  # type: ignore[arg-type]
                work_receipt_sha256=payload["work_receipt_sha256"],  # type: ignore[arg-type]
                result_receipt_id=payload["result_receipt_id"],  # type: ignore[arg-type]
                result_receipt_sha256=payload["result_receipt_sha256"],  # type: ignore[arg-type]
                review_receipt_id=payload["review_receipt_id"],  # type: ignore[arg-type]
                review_receipt_sha256=payload["review_receipt_sha256"],  # type: ignore[arg-type]
                review_receipt_bindings=_review_bindings_from_value(
                    payload["review_receipt_bindings"]
                ),
                submitter_agent_session_id=payload["submitter_agent_session_id"],  # type: ignore[arg-type]
                submitter_agent_session_sha256=payload["submitter_agent_session_sha256"],  # type: ignore[arg-type]
                reviewer_agent_session_id=payload["reviewer_agent_session_id"],  # type: ignore[arg-type]
                reviewer_agent_session_sha256=payload["reviewer_agent_session_sha256"],  # type: ignore[arg-type]
                submitter_assignment_sha256=payload["submitter_assignment_sha256"],  # type: ignore[arg-type]
                reviewer_assignment_sha256=payload["reviewer_assignment_sha256"],  # type: ignore[arg-type]
                submitted_by=_identity_from_dict(
                    payload["submitted_by"],
                    "acceptance_receipt_projection_invalid",
                ),
                accepted_by=_identity_from_dict(
                    payload["accepted_by"],
                    "acceptance_receipt_projection_invalid",
                ),
                assignment_policy_revision=payload["assignment_policy_revision"],  # type: ignore[arg-type]
                review_policy_revision=payload["review_policy_revision"],  # type: ignore[arg-type]
                source_revision=payload["source_revision"],  # type: ignore[arg-type]
                diff_digest=payload["diff_digest"],  # type: ignore[arg-type]
                requirement_set_digest=payload["requirement_set_digest"],  # type: ignore[arg-type]
                union_contract_revision=payload["union_contract_revision"],  # type: ignore[arg-type]
                evidence_refs=_strict_string_tuple(
                    payload["evidence_refs"],
                    "acceptance_receipt_projection_invalid",
                ),
                evidence_sha256=payload["evidence_sha256"],  # type: ignore[arg-type]
                decision=payload["decision"],  # type: ignore[arg-type]
                conflict_state=payload["conflict_state"],  # type: ignore[arg-type]
                issued_at_utc=payload["issued_at_utc"],  # type: ignore[arg-type]
            )
        except AcceptanceReceiptError:
            raise
        except (CollaborationContractError, TypeError, ValueError) as exc:
            raise AcceptanceReceiptError("acceptance_receipt_projection_invalid") from exc
        receipt.validate_integrity()
        if receipt.to_dict() != dict(payload):
            raise AcceptanceReceiptError("acceptance_receipt_projection_invalid")
        return receipt

    @classmethod
    def from_canonical_json(
        cls,
        value: str,
        *,
        expected_sha256: str | None = None,
    ) -> AcceptanceReceipt:
        """Decode canonical JSON and optionally bind it to an external digest."""

        payload = _canonical_mapping(value, "acceptance_receipt_json_invalid")
        receipt = cls.from_dict(payload)
        if expected_sha256 is not None and not hmac.compare_digest(
            receipt.content_sha256,
            _require_sha256(expected_sha256, "acceptance_receipt_digest_invalid"),
        ):
            raise AcceptanceReceiptError("acceptance_receipt_digest_mismatch")
        return receipt

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACCEPTANCE_RECEIPT_SCHEMA,
            "authority": "server-authenticated-decision",
            "issuer": ACCEPTANCE_RECEIPT_ISSUER,
            "acceptance_receipt_id": self.acceptance_receipt_id,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "work_item_id": self.work_item_id,
            "work_receipt_id": self.work_receipt_id,
            "work_receipt_sha256": self.work_receipt_sha256,
            "result_receipt_id": self.result_receipt_id,
            "result_receipt_sha256": self.result_receipt_sha256,
            "review_receipt_id": self.review_receipt_id,
            "review_receipt_sha256": self.review_receipt_sha256,
            "review_receipt_bindings": [
                binding.to_dict() for binding in self.review_receipt_bindings
            ],
            "submitter_agent_session_id": self.submitter_agent_session_id,
            "submitter_agent_session_sha256": self.submitter_agent_session_sha256,
            "reviewer_agent_session_id": self.reviewer_agent_session_id,
            "reviewer_agent_session_sha256": self.reviewer_agent_session_sha256,
            "submitter_assignment_sha256": self.submitter_assignment_sha256,
            "reviewer_assignment_sha256": self.reviewer_assignment_sha256,
            "submitted_by": self.submitted_by.to_dict(),
            "accepted_by": self.accepted_by.to_dict(),
            "assignment_policy_revision": self.assignment_policy_revision,
            "review_policy_revision": self.review_policy_revision,
            "source_revision": self.source_revision,
            "diff_digest": self.diff_digest,
            "requirement_set_digest": self.requirement_set_digest,
            "union_contract_revision": self.union_contract_revision,
            "digests": {
                "work": self.work_receipt_sha256,
                "result": self.result_receipt_sha256,
                "submitter_assignment": self.submitter_assignment_sha256,
                "reviewer_assignment": self.reviewer_assignment_sha256,
                "evidence": self.evidence_sha256,
            },
            "evidence_refs": list(self.evidence_refs),
            "evidence_sha256": self.evidence_sha256,
            "decision": self.decision,
            "conflict_state": self.conflict_state,
            "issued_at_utc": self.issued_at_utc,
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True, init=False)
class VerifiedAcceptanceReceipt:
    """Process-local proof that the issuing authority verified a receipt.

    The wrapper is intentionally not serializable and exposes only the bounded
    receipt fields required by side-effect-free consumers such as the Passive
    Bridge.  A portable ``AcceptanceReceipt`` remains caller-constructible.
    The wrapper is bound to one authority instance and must be resolved through
    that same authority immediately before an authority-gated consumer uses it.
    """

    _receipt: AcceptanceReceipt
    _authority_token: _AcceptanceVerificationToken

    def __init__(
        self,
        receipt: AcceptanceReceipt,
        *,
        _verification_token: _AcceptanceVerificationToken | None = None,
    ) -> None:
        if type(_verification_token) is not _AcceptanceVerificationToken:
            raise AcceptanceReceiptError("acceptance_verified_receipt_authority_required")
        if not isinstance(receipt, AcceptanceReceipt):
            raise AcceptanceReceiptError("acceptance_receipt_invalid")
        object.__setattr__(self, "_receipt", receipt)
        object.__setattr__(self, "_authority_token", _verification_token)

    def __reduce__(self) -> object:
        raise TypeError("verified_acceptance_receipt_not_serializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("verified_acceptance_receipt_not_serializable")

    @property
    def project(self) -> ProjectScope:
        return self._receipt.project

    @property
    def coordination_session_id(self) -> str:
        return self._receipt.coordination_session_id

    @property
    def work_item_id(self) -> str:
        return self._receipt.work_item_id

    @property
    def work_receipt_sha256(self) -> str:
        return self._receipt.work_receipt_sha256

    @property
    def result_receipt_sha256(self) -> str:
        return self._receipt.result_receipt_sha256

    @property
    def accepted_by(self) -> AgentIdentity:
        return self._receipt.accepted_by

    @property
    def accepted_at(self) -> str:
        return self._receipt.issued_at_utc

    @property
    def decision(self) -> str:
        return self._receipt.decision

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return self._receipt.evidence_refs

    @property
    def content_sha256(self) -> str:
        return self._receipt.content_sha256

    def _receipt_for(
        self,
        authority_token: _AcceptanceVerificationToken,
    ) -> AcceptanceReceipt:
        if self._authority_token is not authority_token:
            raise AcceptanceReceiptError("acceptance_verified_receipt_authority_mismatch")
        return self._receipt


class AcceptanceReceiptAuthority:
    """Server issuance seam for exact, repository-backed acceptance decisions."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        reviewer_policy_authority: AgentPolicyBindingAuthority,
        role_assignment_authority: RoleAssignmentAuthority,
        repository: AcceptanceAuthorityRepository,
        current_review_policy_revision: str,
        current_source_revision: str,
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise AcceptanceReceiptError("acceptance_server_authority_required")
        if not isinstance(reviewer_policy_authority, AgentPolicyBindingAuthority):
            raise AcceptanceReceiptError("acceptance_reviewer_policy_authority_invalid")
        if not isinstance(role_assignment_authority, RoleAssignmentAuthority):
            raise AcceptanceReceiptError("acceptance_role_assignment_authority_invalid")
        if not isinstance(repository, AcceptanceAuthorityRepository):
            raise AcceptanceReceiptError("acceptance_repository_invalid")
        self._clock = clock
        self._reviewer_policy_authority = reviewer_policy_authority
        self._role_assignment_authority = role_assignment_authority
        self._repository = repository
        self._current_review_policy_revision = _safe_identifier(
            current_review_policy_revision,
            "acceptance_review_policy_revision_invalid",
        )
        self._current_source_revision = _require_pinned_source_revision(current_source_revision)
        self._verification_token = _AcceptanceVerificationToken(
            _server_token=_SERVER_AUTHORITY_TOKEN
        )

    def issue(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: ReviewReceipt | Iterable[ReviewReceipt],
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        reviewer_policy_binding: AgentPolicyBinding | None,
        acceptance_receipt_id: str | None = None,
    ) -> AcceptanceReceipt:
        """Issue one acceptance decision after validating every trusted input."""

        _require_work_result_binding(work, result)
        if not isinstance(submitter_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_submitter_session_invalid")
        if not isinstance(reviewer_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_reviewer_session_invalid")
        normalized_reviews = _normalize_reviews(reviews)
        primary_review = normalized_reviews[0]
        self._require_review_scope_binding(work, result, normalized_reviews)
        self._require_current_revisions(normalized_reviews)
        generated_id = _deterministic_acceptance_id(
            work=work,
            result=result,
            reviews=normalized_reviews,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
        )
        requested_id = acceptance_receipt_id or generated_id
        replay = self._resolve_replay(
            work,
            result,
            normalized_reviews,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
            requested_id=requested_id,
        )
        if replay is not None:
            return replay
        self._repository.require_canonical_sources(
            work,
            result,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
            submitter_assignment_sha256=result.role_assignment_sha256,
            reviewer_assignment_sha256=primary_review.reviewer_assignment_sha256,
        )
        issued_at = _utc_text(self._clock(), "acceptance_clock_invalid")
        self._require_timing(work, result, normalized_reviews, issued_at=issued_at)
        self._require_sessions(
            work,
            result,
            normalized_reviews,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
            issued_at=issued_at,
        )
        submitter_assignment, reviewer_assignment = self._require_role_assignments(
            work,
            result,
            normalized_reviews,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
        )
        self._require_reviewer_authority(
            work,
            reviewer_session=reviewer_session,
            reviewer_policy_binding=reviewer_policy_binding,
        )
        self._require_decision(result, normalized_reviews)

        review_bindings = tuple(
            ReviewReceiptBinding(
                review_channel=review.review_channel,
                review_receipt_id=review.review_receipt_id,
                review_receipt_sha256=review.content_sha256,
            )
            for review in normalized_reviews
        )
        evidence_refs = _combined_evidence_refs(result, normalized_reviews)
        evidence_sha256 = _acceptance_evidence_digest(
            review_receipt_bindings=review_bindings,
            result_receipt_sha256=result.content_sha256,
            submitter_assignment_sha256=submitter_assignment.assignment_sha256,
            reviewer_assignment_sha256=reviewer_assignment.assignment_sha256,
            source_revision=primary_review.source_revision,
            diff_digest=primary_review.diff_digest,
            requirement_set_digest=primary_review.requirement_set_digest,
            union_contract_revision=primary_review.union_contract_revision,
            evidence_refs=evidence_refs,
        )
        receipt = AcceptanceReceipt(
            acceptance_receipt_id=requested_id,
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            work_item_id=work.work_item_id,
            work_receipt_id=work.receipt_id,
            work_receipt_sha256=work.content_sha256,
            result_receipt_id=result.receipt_id,
            result_receipt_sha256=result.content_sha256,
            review_receipt_id=primary_review.review_receipt_id,
            review_receipt_sha256=primary_review.content_sha256,
            review_receipt_bindings=review_bindings,
            submitter_agent_session_id=submitter_session.session_id,
            submitter_agent_session_sha256=submitter_session.content_sha256,
            reviewer_agent_session_id=reviewer_session.session_id,
            reviewer_agent_session_sha256=reviewer_session.content_sha256,
            submitter_assignment_sha256=submitter_assignment.assignment_sha256,
            reviewer_assignment_sha256=reviewer_assignment.assignment_sha256,
            submitted_by=submitter_session.identity,
            accepted_by=reviewer_session.identity,
            assignment_policy_revision=ROLE_ASSIGNMENT_POLICY_REVISION,
            review_policy_revision=primary_review.review_policy_revision,
            source_revision=primary_review.source_revision,
            diff_digest=primary_review.diff_digest,
            requirement_set_digest=primary_review.requirement_set_digest,
            union_contract_revision=primary_review.union_contract_revision,
            evidence_refs=evidence_refs,
            evidence_sha256=evidence_sha256,
            decision=primary_review.decision,
            conflict_state=primary_review.conflict_state,
            issued_at_utc=issued_at,
        )
        receipt.validate_integrity()
        return self._repository.append_exact(
            normalized_reviews,
            receipt,
            work=work,
            result=result,
            submitter_session=submitter_session,
            reviewer_session=reviewer_session,
            _authority_token=_SERVER_AUTHORITY_TOKEN,
        )

    def _resolve_replay(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        requested_id: str,
    ) -> AcceptanceReceipt | None:
        existing = self._repository.load_acceptance_by_binding(
            project_id=work.project.project_id,
            coordination_session_id=work.coordination_session_id,
            work_item_id=work.work_item_id,
            result_receipt_sha256=result.content_sha256,
        )
        if existing is None:
            existing_id = self._repository.load_acceptance_by_id(requested_id)
            if existing_id is not None:
                raise AcceptanceReceiptError("acceptance_receipt_replay_conflict")
            return None
        if requested_id != existing.acceptance_receipt_id:
            raise AcceptanceReceiptError("acceptance_receipt_replay_ambiguous")
        primary_review = reviews[0]
        if primary_review.decision != existing.decision:
            raise AcceptanceReceiptError("acceptance_receipt_decision_conflict")
        expected_bindings = tuple(
            ReviewReceiptBinding(
                review_channel=review.review_channel,
                review_receipt_id=review.review_receipt_id,
                review_receipt_sha256=review.content_sha256,
            )
            for review in reviews
        )
        if (
            expected_bindings != existing.review_receipt_bindings
            or submitter_session.content_sha256 != existing.submitter_agent_session_sha256
            or reviewer_session.content_sha256 != existing.reviewer_agent_session_sha256
            or primary_review.source_revision != existing.source_revision
            or primary_review.diff_digest != existing.diff_digest
            or primary_review.requirement_set_digest != existing.requirement_set_digest
            or primary_review.union_contract_revision != existing.union_contract_revision
        ):
            raise AcceptanceReceiptError("acceptance_receipt_replay_ambiguous")
        return existing

    def verify_issued(self, receipt: AcceptanceReceipt) -> AcceptanceReceipt:
        """Verify structural integrity and this authority's issuance record."""

        if not isinstance(receipt, AcceptanceReceipt):
            raise AcceptanceReceiptError("acceptance_receipt_invalid")
        receipt.validate_integrity()
        issued = self._repository.load_acceptance_by_id(receipt.acceptance_receipt_id)
        if issued is None:
            raise AcceptanceReceiptError("acceptance_receipt_not_server_issued")
        if not hmac.compare_digest(issued.content_sha256, receipt.content_sha256):
            raise AcceptanceReceiptError("acceptance_receipt_tampered")
        return issued

    def verify_for_consumption(
        self,
        receipt: AcceptanceReceipt,
    ) -> VerifiedAcceptanceReceipt:
        """Return a non-serializable proof for authority-gated consumers."""

        issued = self.verify_issued(receipt)
        return VerifiedAcceptanceReceipt(
            issued,
            _verification_token=self._verification_token,
        )

    def verify_consumption_proof(
        self,
        proof: VerifiedAcceptanceReceipt,
    ) -> AcceptanceReceipt:
        """Resolve only a proof created by this exact issuing authority."""

        if not isinstance(proof, VerifiedAcceptanceReceipt):
            raise AcceptanceReceiptError("acceptance_verified_receipt_invalid")
        return self.verify_issued(proof._receipt_for(self._verification_token))

    def _require_review_scope_binding(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
    ) -> None:
        for review in reviews:
            if review.project != work.project:
                raise AcceptanceReceiptError("acceptance_review_project_mismatch")
            if review.coordination_session_id != work.coordination_session_id:
                raise AcceptanceReceiptError("acceptance_review_session_mismatch")
            if review.work_item_id != work.work_item_id:
                raise AcceptanceReceiptError("acceptance_review_work_item_mismatch")
            if not hmac.compare_digest(review.work_receipt_sha256, work.content_sha256):
                raise AcceptanceReceiptError("acceptance_review_work_digest_mismatch")
            if not hmac.compare_digest(
                review.result_receipt_sha256,
                result.content_sha256,
            ):
                raise AcceptanceReceiptError("acceptance_review_result_digest_mismatch")

    def _require_current_revisions(
        self,
        reviews: tuple[ReviewReceipt, ...],
    ) -> None:
        for review in reviews:
            if review.review_policy_revision != self._current_review_policy_revision:
                raise AcceptanceReceiptError("acceptance_review_policy_stale")
            if review.source_revision != self._current_source_revision:
                raise AcceptanceReceiptError("acceptance_source_revision_stale")

    @staticmethod
    def _require_sessions(
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
        issued_at: str,
    ) -> None:
        if not isinstance(submitter_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_submitter_session_invalid")
        if not isinstance(reviewer_session, AgentSession):
            raise AcceptanceReceiptError("acceptance_reviewer_session_invalid")
        for session, kind in (
            (submitter_session, "submitter"),
            (reviewer_session, "reviewer"),
        ):
            if session.project != work.project:
                raise AcceptanceReceiptError(f"acceptance_{kind}_project_mismatch")
            if session.coordination_session_id != work.coordination_session_id:
                raise AcceptanceReceiptError(f"acceptance_{kind}_session_scope_mismatch")
        if submitter_session.identity != result.submitted_by:
            raise AcceptanceReceiptError("acceptance_submitter_identity_mismatch")
        if submitter_session.identity != work.assigned_agent:
            raise AcceptanceReceiptError("acceptance_submitter_assignment_mismatch")
        for review in reviews:
            if review.reviewer_agent_session_id != reviewer_session.session_id:
                raise AcceptanceReceiptError("acceptance_reviewer_session_mismatch")
        if (
            submitter_session.session_id == reviewer_session.session_id
            or submitter_session.identity.agent_id == reviewer_session.identity.agent_id
        ):
            raise AcceptanceReceiptError("acceptance_independent_reviewer_required")
        submitted_at = _parse_time(result.submitted_at, "acceptance_result_time_invalid")
        if submitted_at < _parse_time(
            submitter_session.started_at,
            "acceptance_submitter_session_time_invalid",
        ):
            raise AcceptanceReceiptError("acceptance_result_before_submitter_session")
        if submitter_session.expires_at is not None and submitted_at >= _parse_time(
            submitter_session.expires_at,
            "acceptance_submitter_session_time_invalid",
        ):
            raise AcceptanceReceiptError("acceptance_result_after_submitter_session")
        for review in reviews:
            reviewed_at = _parse_time(
                review.reviewed_at_utc,
                "acceptance_review_time_invalid",
            )
            if reviewed_at < _parse_time(
                reviewer_session.started_at,
                "acceptance_reviewer_session_time_invalid",
            ):
                raise AcceptanceReceiptError("acceptance_review_before_reviewer_session")
            if reviewer_session.expires_at is not None and reviewed_at >= _parse_time(
                reviewer_session.expires_at,
                "acceptance_reviewer_session_time_invalid",
            ):
                raise AcceptanceReceiptError("acceptance_review_after_reviewer_session")
            if _parse_time(issued_at, "acceptance_issued_at_invalid") < reviewed_at:
                raise AcceptanceReceiptError("acceptance_issued_before_review")

    def _require_role_assignments(
        self,
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
        *,
        submitter_session: AgentSession,
        reviewer_session: AgentSession,
    ) -> tuple[VerifiedRoleAssignment, VerifiedRoleAssignment]:
        if not result.role_assignment_sha256:
            raise AcceptanceReceiptError("acceptance_submitter_assignment_required")
        primary_review = reviews[0]
        if not primary_review.reviewer_assignment_sha256:
            raise AcceptanceReceiptError("acceptance_reviewer_assignment_required")
        try:
            submitter = self._role_assignment_authority.verify_for_use(
                result.role_assignment_sha256,
                use=RESULT_SUBMISSION_USE,
                used_at=result.submitted_at,
            )
        except RoleAssignmentError as exc:
            raise AcceptanceReceiptError("acceptance_submitter_assignment_invalid") from exc
        try:
            reviewer = self._role_assignment_authority.verify_for_use(
                primary_review.reviewer_assignment_sha256,
                use=ACCEPTANCE_REVIEW_USE,
                used_at=primary_review.reviewed_at_utc,
            )
        except RoleAssignmentError as exc:
            raise AcceptanceReceiptError("acceptance_reviewer_assignment_invalid") from exc
        if submitter.assignment_role != WORK_SUBMITTER_ROLE:
            raise AcceptanceReceiptError("acceptance_submitter_assignment_invalid")
        if reviewer.assignment_role != WORK_REVIEWER_ROLE:
            raise AcceptanceReceiptError("acceptance_reviewer_assignment_invalid")
        if (
            submitter.assignment_policy_revision != ROLE_ASSIGNMENT_POLICY_REVISION
            or reviewer.assignment_policy_revision != ROLE_ASSIGNMENT_POLICY_REVISION
        ):
            raise AcceptanceReceiptError("acceptance_assignment_policy_stale")
        for assignment, session, kind in (
            (submitter, submitter_session, "submitter"),
            (reviewer, reviewer_session, "reviewer"),
        ):
            if assignment.project != work.project:
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_project_mismatch")
            if assignment.coordination_session_id != work.coordination_session_id:
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_session_mismatch")
            if assignment.work_item_id != work.work_item_id:
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_work_mismatch")
            if not hmac.compare_digest(
                assignment.work_receipt_sha256,
                work.content_sha256,
            ):
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_work_digest_mismatch")
            if assignment.agent_session_id != session.session_id:
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_agent_session_mismatch")
            if assignment.agent_id != session.identity.agent_id:
                raise AcceptanceReceiptError(f"acceptance_{kind}_assignment_agent_mismatch")
        if reviewer.result_receipt_sha256 != result.content_sha256:
            raise AcceptanceReceiptError("acceptance_reviewer_assignment_result_mismatch")
        if submitter.agent_id == reviewer.agent_id:
            raise AcceptanceReceiptError("acceptance_independent_reviewer_required")
        if hmac.compare_digest(
            submitter.assignment_sha256,
            reviewer.assignment_sha256,
        ):
            raise AcceptanceReceiptError("acceptance_independent_reviewer_required")
        return submitter, reviewer

    def _require_reviewer_authority(
        self,
        work: WorkReceipt,
        *,
        reviewer_session: AgentSession,
        reviewer_policy_binding: AgentPolicyBinding | None,
    ) -> None:
        decision = self._reviewer_policy_authority.authorize_mcp(
            reviewer_session,
            reviewer_policy_binding,
            project=work.project,
            coordination_session_id=work.coordination_session_id,
            policy_revision=self._current_review_policy_revision,
            tool_name="review_run",
            arguments={"action": "evaluate"},
        )
        if decision.allowed:
            return
        if decision.reason == "agent_policy_revision_mismatch":
            raise AcceptanceReceiptError("acceptance_review_policy_stale")
        raise AcceptanceReceiptError("acceptance_reviewer_authority_invalid")

    @staticmethod
    def _require_decision(
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
    ) -> None:
        for review in reviews:
            if review.conflict_state == "unresolved":
                raise AcceptanceReceiptError("acceptance_conflict_unresolved")
            if review.decision != "accepted":
                raise AcceptanceReceiptError("acceptance_review_not_accepted")
        if result.outcome != "completed":
            raise AcceptanceReceiptError("acceptance_result_not_completed")

    @staticmethod
    def _require_timing(
        work: WorkReceipt,
        result: ResultReceipt,
        reviews: tuple[ReviewReceipt, ...],
        *,
        issued_at: str,
    ) -> None:
        work_issued = _parse_time(work.issued_at, "acceptance_work_time_invalid")
        work_expires = _parse_time(work.expires_at, "acceptance_work_time_invalid")
        submitted = _parse_time(result.submitted_at, "acceptance_result_time_invalid")
        issued = _parse_time(issued_at, "acceptance_issued_at_invalid")
        if submitted < work_issued:
            raise AcceptanceReceiptError("acceptance_result_before_work")
        if submitted > work_expires:
            raise AcceptanceReceiptError("acceptance_result_after_work_expiry")
        for review in reviews:
            reviewed = _parse_time(
                review.reviewed_at_utc,
                "acceptance_review_time_invalid",
            )
            if reviewed < submitted:
                raise AcceptanceReceiptError("acceptance_review_before_submission")
        if issued < submitted:
            raise AcceptanceReceiptError("acceptance_issued_before_submission")


def open_server_acceptance_receipt_authority(
    *,
    reviewer_policy_authority: AgentPolicyBindingAuthority,
    role_assignment_authority: RoleAssignmentAuthority,
    repository: AcceptanceAuthorityRepository | None = None,
    source_registry: AcceptanceAuthorityRepository | None = None,
    current_review_policy_revision: str,
    current_source_revision: str,
    clock: Callable[[], datetime] | None = None,
) -> AcceptanceReceiptAuthority:
    """Open the acceptance issuer inside the canonical server runtime only."""

    if repository is not None and source_registry is not None and repository is not source_registry:
        raise AcceptanceReceiptError("acceptance_repository_ambiguous")
    selected = repository if repository is not None else source_registry
    if not isinstance(selected, AcceptanceAuthorityRepository):
        code = (
            "acceptance_source_registry_invalid"
            if source_registry is not None or repository is None
            else "acceptance_repository_invalid"
        )
        raise AcceptanceReceiptError(code)

    return AcceptanceReceiptAuthority(
        clock=clock or (lambda: datetime.now(timezone.utc)),
        reviewer_policy_authority=reviewer_policy_authority,
        role_assignment_authority=role_assignment_authority,
        repository=selected,
        current_review_policy_revision=current_review_policy_revision,
        current_source_revision=current_source_revision,
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


def open_server_acceptance_source_registry() -> ServerAcceptanceSourceRegistry:
    """Create the exact canonical-source adapter inside server wiring."""

    return ServerAcceptanceSourceRegistry(
        _server_token=_SERVER_SOURCE_REGISTRY_TOKEN,
    )


def _require_review_acceptance_pair(
    reviews: tuple[ReviewReceipt, ...],
    receipt: AcceptanceReceipt,
) -> None:
    if not isinstance(receipt, AcceptanceReceipt):
        raise AcceptanceReceiptError("acceptance_receipt_invalid")
    normalized_reviews = _normalize_reviews(reviews)
    receipt.validate_integrity()
    expected_bindings = tuple(
        ReviewReceiptBinding(
            review_channel=review.review_channel,
            review_receipt_id=review.review_receipt_id,
            review_receipt_sha256=review.content_sha256,
        )
        for review in normalized_reviews
    )
    if expected_bindings != receipt.review_receipt_bindings:
        raise AcceptanceReceiptError("acceptance_review_bindings_mismatch")

    primary_review = normalized_reviews[0]
    if primary_review.review_receipt_id != receipt.review_receipt_id:
        raise AcceptanceReceiptError("acceptance_review_receipt_mismatch")
    if not hmac.compare_digest(
        primary_review.content_sha256,
        receipt.review_receipt_sha256,
    ):
        raise AcceptanceReceiptError("acceptance_review_digest_mismatch")

    for review in normalized_reviews:
        checks = (
            (review.project == receipt.project, "acceptance_review_project_mismatch"),
            (
                review.coordination_session_id == receipt.coordination_session_id,
                "acceptance_review_session_mismatch",
            ),
            (
                review.work_item_id == receipt.work_item_id,
                "acceptance_review_work_item_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.work_receipt_sha256,
                    receipt.work_receipt_sha256,
                ),
                "acceptance_review_work_digest_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.result_receipt_sha256,
                    receipt.result_receipt_sha256,
                ),
                "acceptance_review_result_digest_mismatch",
            ),
            (
                review.reviewer_agent_session_id == receipt.reviewer_agent_session_id,
                "acceptance_reviewer_session_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.reviewer_assignment_sha256,
                    receipt.reviewer_assignment_sha256,
                ),
                "acceptance_reviewer_assignment_mismatch",
            ),
            (
                review.review_policy_revision == receipt.review_policy_revision,
                "acceptance_review_policy_mismatch",
            ),
            (
                review.source_revision == receipt.source_revision,
                "acceptance_source_revision_mismatch",
            ),
            (
                hmac.compare_digest(review.diff_digest, receipt.diff_digest),
                "acceptance_review_diff_digest_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.requirement_set_digest,
                    receipt.requirement_set_digest,
                ),
                "acceptance_review_requirement_set_digest_mismatch",
            ),
            (
                review.union_contract_revision == receipt.union_contract_revision,
                "acceptance_review_union_contract_revision_mismatch",
            ),
            (
                review.decision == receipt.decision,
                "acceptance_receipt_decision_conflict",
            ),
            (
                review.conflict_state == receipt.conflict_state,
                "acceptance_conflict_state_mismatch",
            ),
        )
        for accepted, code in checks:
            if not accepted:
                raise AcceptanceReceiptError(code)


def _require_acceptance_source_pair(
    receipt: AcceptanceReceipt,
    *,
    work: WorkReceipt,
    result: ResultReceipt,
    submitter_session: AgentSession,
    reviewer_session: AgentSession,
) -> None:
    if not isinstance(receipt, AcceptanceReceipt):
        raise AcceptanceReceiptError("acceptance_receipt_invalid")
    checks = (
        (receipt.project == work.project, "acceptance_receipt_project_mismatch"),
        (
            receipt.coordination_session_id == work.coordination_session_id,
            "acceptance_receipt_session_mismatch",
        ),
        (receipt.work_item_id == work.work_item_id, "acceptance_receipt_work_item_mismatch"),
        (receipt.work_receipt_id == work.receipt_id, "acceptance_receipt_work_mismatch"),
        (
            hmac.compare_digest(receipt.work_receipt_sha256, work.content_sha256),
            "acceptance_receipt_work_digest_mismatch",
        ),
        (receipt.result_receipt_id == result.receipt_id, "acceptance_receipt_result_mismatch"),
        (
            hmac.compare_digest(receipt.result_receipt_sha256, result.content_sha256),
            "acceptance_receipt_result_digest_mismatch",
        ),
        (
            receipt.submitter_agent_session_id == submitter_session.session_id,
            "acceptance_receipt_submitter_session_mismatch",
        ),
        (
            hmac.compare_digest(
                receipt.submitter_agent_session_sha256,
                submitter_session.content_sha256,
            ),
            "acceptance_receipt_submitter_session_digest_mismatch",
        ),
        (
            receipt.reviewer_agent_session_id == reviewer_session.session_id,
            "acceptance_receipt_reviewer_session_mismatch",
        ),
        (
            hmac.compare_digest(
                receipt.reviewer_agent_session_sha256,
                reviewer_session.content_sha256,
            ),
            "acceptance_receipt_reviewer_session_digest_mismatch",
        ),
        (
            receipt.submitted_by == submitter_session.identity,
            "acceptance_receipt_submitter_identity_mismatch",
        ),
        (
            receipt.accepted_by == reviewer_session.identity,
            "acceptance_receipt_reviewer_identity_mismatch",
        ),
        (
            hmac.compare_digest(
                receipt.submitter_assignment_sha256,
                result.role_assignment_sha256,
            ),
            "acceptance_receipt_submitter_assignment_mismatch",
        ),
    )
    for accepted, code in checks:
        if not accepted:
            raise AcceptanceReceiptError(code)


def _review_binding_key(
    review: ReviewReceipt,
) -> tuple[str, str, str, str, str, str]:
    return (
        review.project.project_id,
        review.coordination_session_id,
        review.work_item_id,
        review.result_receipt_sha256,
        review.reviewer_assignment_sha256,
        review.review_channel,
    )


def _acceptance_binding_key_from_receipt(
    receipt: AcceptanceReceipt,
) -> tuple[str, str, str, str]:
    return _acceptance_binding_key(
        project_id=receipt.project.project_id,
        coordination_session_id=receipt.coordination_session_id,
        work_item_id=receipt.work_item_id,
        result_receipt_sha256=receipt.result_receipt_sha256,
    )


def _acceptance_binding_key(
    *,
    project_id: str,
    coordination_session_id: str,
    work_item_id: str,
    result_receipt_sha256: str,
) -> tuple[str, str, str, str]:
    try:
        project = ProjectScope(project_id).project_id
    except (CollaborationContractError, TypeError, ValueError) as exc:
        raise AcceptanceReceiptError("acceptance_project_invalid") from exc
    return (
        project,
        _safe_identifier(
            coordination_session_id,
            "acceptance_coordination_session_invalid",
        ),
        _safe_identifier(work_item_id, "acceptance_work_item_invalid"),
        _require_sha256(
            result_receipt_sha256,
            "acceptance_result_receipt_digest_invalid",
        ),
    )


def _identity_from_dict(value: object, code: str) -> AgentIdentity:
    payload = _exact_mapping(value, fields=_AGENT_IDENTITY_FIELDS, code=code)
    capabilities = _strict_string_tuple(payload["capabilities"], code)
    parent_agent_id = payload["parent_agent_id"]
    if parent_agent_id is not None and not isinstance(parent_agent_id, str):
        raise AcceptanceReceiptError(code)
    try:
        return AgentIdentity(
            agent_id=payload["agent_id"],  # type: ignore[arg-type]
            role=payload["role"],  # type: ignore[arg-type]
            parent_agent_id=parent_agent_id,
            capabilities=capabilities,
        )
    except (CollaborationContractError, TypeError, ValueError) as exc:
        raise AcceptanceReceiptError(code) from exc


def _strict_string_tuple(value: object, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AcceptanceReceiptError(code)
    return tuple(value)


def _review_bindings_from_value(value: object) -> tuple[ReviewReceiptBinding, ...]:
    if not isinstance(value, list):
        raise AcceptanceReceiptError("acceptance_review_bindings_invalid")
    return _normalize_review_bindings(tuple(ReviewReceiptBinding.from_dict(item) for item in value))


def _normalize_review_bindings(
    value: object,
) -> tuple[ReviewReceiptBinding, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(item, ReviewReceiptBinding) for item in value
    ):
        raise AcceptanceReceiptError("acceptance_review_bindings_invalid")
    if len(value) != len(REVIEW_CHANNELS):
        raise AcceptanceReceiptError("acceptance_review_channels_missing")
    by_channel: dict[str, ReviewReceiptBinding] = {}
    receipt_ids: set[str] = set()
    receipt_digests: set[str] = set()
    for binding in value:
        if binding.review_channel in by_channel:
            raise AcceptanceReceiptError("acceptance_review_channel_duplicate")
        if binding.review_receipt_id in receipt_ids:
            raise AcceptanceReceiptError("acceptance_review_receipt_duplicate")
        if binding.review_receipt_sha256 in receipt_digests:
            raise AcceptanceReceiptError("acceptance_review_digest_duplicate")
        by_channel[binding.review_channel] = binding
        receipt_ids.add(binding.review_receipt_id)
        receipt_digests.add(binding.review_receipt_sha256)
    if set(by_channel) != _REVIEW_CHANNEL_SET:
        raise AcceptanceReceiptError("acceptance_review_channels_missing")
    return tuple(by_channel[channel] for channel in REVIEW_CHANNELS)


def _exact_mapping(
    value: object,
    *,
    fields: frozenset[str],
    code: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AcceptanceReceiptError(code)
    if any(not isinstance(key, str) for key in value):
        raise AcceptanceReceiptError(code)
    return value


def _canonical_mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, str) or value != value.strip():
        raise AcceptanceReceiptError(code)
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise AcceptanceReceiptError(code) from exc
    if not isinstance(payload, Mapping) or _canonical_json(payload) != value:
        raise AcceptanceReceiptError(code)
    return payload


def _work_source_key(value: WorkReceipt) -> tuple[str, str, str, str]:
    return (
        value.project.project_id,
        value.coordination_session_id,
        value.work_item_id,
        value.receipt_id,
    )


def _result_source_key(value: ResultReceipt) -> tuple[str, str, str, str]:
    return (
        value.project.project_id,
        value.coordination_session_id,
        value.work_item_id,
        value.receipt_id,
    )


def _agent_session_source_key(value: AgentSession) -> tuple[str, str, str]:
    return (
        value.project.project_id,
        value.coordination_session_id,
        value.session_id,
    )


def _require_work_result_binding(work: WorkReceipt, result: ResultReceipt) -> None:
    if not isinstance(work, WorkReceipt):
        raise AcceptanceReceiptError("acceptance_work_receipt_invalid")
    if not isinstance(result, ResultReceipt):
        raise AcceptanceReceiptError("acceptance_result_receipt_invalid")
    if result.project != work.project:
        raise AcceptanceReceiptError("acceptance_result_project_mismatch")
    if result.coordination_session_id != work.coordination_session_id:
        raise AcceptanceReceiptError("acceptance_result_session_mismatch")
    if result.work_item_id != work.work_item_id:
        raise AcceptanceReceiptError("acceptance_result_work_item_mismatch")
    if not hmac.compare_digest(result.work_receipt_sha256, work.content_sha256):
        raise AcceptanceReceiptError("acceptance_result_work_digest_mismatch")
    if result.submitted_by != work.assigned_agent:
        raise AcceptanceReceiptError("acceptance_result_submitter_mismatch")


def _public_probe(
    *,
    event_id: str,
    project: ProjectScope,
    coordination_session_id: str,
    work_item_id: str,
    actor: AgentIdentity,
    created_at: str,
    subject_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    error_prefix: str,
) -> CollaborationEvent:
    try:
        return CollaborationEvent(
            event_id=event_id,
            project=project,
            coordination_session_id=coordination_session_id,
            actor=actor,
            event_type="workflow.receipt_submitted",
            summary="Bounded review decision receipt",
            created_at=created_at,
            work_item_id=work_item_id,
            subject_refs=subject_refs,
            evidence_refs=evidence_refs,
        )
    except CollaborationContractError as exc:
        if exc.code == "secret_value_forbidden":
            raise AcceptanceReceiptError("acceptance_secret_value_forbidden") from exc
        raise AcceptanceReceiptError(f"{error_prefix}_field_invalid") from exc


def _normalize_evidence_refs(
    evidence_refs: tuple[str, ...],
    *,
    project: ProjectScope,
    coordination_session_id: str,
    work_item_id: str,
    created_at: str,
    error_prefix: str,
) -> tuple[str, ...]:
    probe = _public_probe(
        event_id=f"event:{error_prefix}:evidence-validator",
        project=project,
        coordination_session_id=coordination_session_id,
        work_item_id=work_item_id,
        actor=AgentIdentity("agent:acceptance-evidence-validator", "reviewer"),
        created_at=created_at,
        subject_refs=(),
        evidence_refs=evidence_refs,
        error_prefix=error_prefix,
    )
    return probe.evidence_refs


def _combined_evidence_refs(
    result: ResultReceipt,
    reviews: tuple[ReviewReceipt, ...],
) -> tuple[str, ...]:
    combined = tuple(
        dict.fromkeys(
            (
                *(ref for review in reviews for ref in review.evidence_refs),
                *result.evidence_refs,
                *result.artifact_refs,
            )
        )
    )
    if len(combined) > 32:
        raise AcceptanceReceiptError("acceptance_evidence_too_many")
    if not combined:
        raise AcceptanceReceiptError("acceptance_evidence_required")
    return combined


def _review_evidence_digest(evidence_refs: tuple[str, ...]) -> str:
    return _sha256({"evidence_refs": list(evidence_refs)})


def _acceptance_evidence_digest(
    *,
    review_receipt_bindings: tuple[ReviewReceiptBinding, ...],
    result_receipt_sha256: str,
    submitter_assignment_sha256: str,
    reviewer_assignment_sha256: str,
    source_revision: str,
    diff_digest: str,
    requirement_set_digest: str,
    union_contract_revision: str,
    evidence_refs: tuple[str, ...],
) -> str:
    return _sha256(
        {
            "review_receipt_bindings": [binding.to_dict() for binding in review_receipt_bindings],
            "result_receipt_sha256": result_receipt_sha256,
            "submitter_assignment_sha256": submitter_assignment_sha256,
            "reviewer_assignment_sha256": reviewer_assignment_sha256,
            "source_revision": source_revision,
            "diff_digest": diff_digest,
            "requirement_set_digest": requirement_set_digest,
            "union_contract_revision": union_contract_revision,
            "evidence_refs": list(evidence_refs),
        }
    )


def _deterministic_acceptance_id(
    *,
    work: WorkReceipt,
    result: ResultReceipt,
    reviews: tuple[ReviewReceipt, ...],
    submitter_session: AgentSession,
    reviewer_session: AgentSession,
) -> str:
    digest = _sha256(
        {
            "project_id": work.project.project_id,
            "coordination_session_id": work.coordination_session_id,
            "work_item_id": work.work_item_id,
            "work_receipt_sha256": work.content_sha256,
            "result_receipt_sha256": result.content_sha256,
            "review_receipts": [
                {
                    "review_channel": review.review_channel,
                    "review_receipt_sha256": review.content_sha256,
                }
                for review in reviews
            ],
            "submitter_assignment_sha256": result.role_assignment_sha256,
            "reviewer_assignment_sha256": reviews[0].reviewer_assignment_sha256,
            "submitter_agent_session_id": submitter_session.session_id,
            "reviewer_agent_session_id": reviewer_session.session_id,
        }
    )
    return f"acceptance:{digest.removeprefix('sha256:')[:40]}"


def _review_channel(value: object, code: str) -> str:
    channel = str(value or "").strip().casefold()
    if channel not in _REVIEW_CHANNEL_SET:
        raise AcceptanceReceiptError(code)
    return channel


def _normalize_reviews(
    value: ReviewReceipt | Iterable[ReviewReceipt],
) -> tuple[ReviewReceipt, ...]:
    if isinstance(value, ReviewReceipt):
        candidates = (value,)
    else:
        try:
            candidates = tuple(value)
        except TypeError as exc:
            raise AcceptanceReceiptError("acceptance_review_receipt_invalid") from exc
    if any(not isinstance(review, ReviewReceipt) for review in candidates):
        raise AcceptanceReceiptError("acceptance_review_receipt_invalid")
    if len(candidates) != len(REVIEW_CHANNELS):
        raise AcceptanceReceiptError("acceptance_review_channels_missing")

    by_channel: dict[str, ReviewReceipt] = {}
    receipt_ids: set[str] = set()
    receipt_digests: set[str] = set()
    for review in candidates:
        review.validate_integrity()
        channel = review.review_channel
        if channel in by_channel:
            raise AcceptanceReceiptError("acceptance_review_channel_duplicate")
        if review.review_receipt_id in receipt_ids:
            raise AcceptanceReceiptError("acceptance_review_receipt_duplicate")
        digest = review.content_sha256
        if digest in receipt_digests:
            raise AcceptanceReceiptError("acceptance_review_digest_duplicate")
        by_channel[channel] = review
        receipt_ids.add(review.review_receipt_id)
        receipt_digests.add(digest)
    if set(by_channel) != _REVIEW_CHANNEL_SET:
        raise AcceptanceReceiptError("acceptance_review_channels_missing")

    reviews = tuple(by_channel[channel] for channel in REVIEW_CHANNELS)
    primary = reviews[0]
    for review in reviews[1:]:
        checks = (
            (review.project == primary.project, "acceptance_review_project_mismatch"),
            (
                review.coordination_session_id == primary.coordination_session_id,
                "acceptance_review_session_mismatch",
            ),
            (
                review.work_item_id == primary.work_item_id,
                "acceptance_review_work_item_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.work_receipt_sha256,
                    primary.work_receipt_sha256,
                ),
                "acceptance_review_work_digest_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.result_receipt_sha256,
                    primary.result_receipt_sha256,
                ),
                "acceptance_review_result_digest_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.reviewer_assignment_sha256,
                    primary.reviewer_assignment_sha256,
                ),
                "acceptance_reviewer_assignment_mismatch",
            ),
            (
                review.reviewer_agent_session_id == primary.reviewer_agent_session_id,
                "acceptance_reviewer_session_mismatch",
            ),
            (
                review.review_policy_revision == primary.review_policy_revision,
                "acceptance_review_policy_mismatch",
            ),
            (
                review.source_revision == primary.source_revision,
                "acceptance_review_source_revision_mismatch",
            ),
            (
                hmac.compare_digest(review.diff_digest, primary.diff_digest),
                "acceptance_review_diff_digest_mismatch",
            ),
            (
                hmac.compare_digest(
                    review.requirement_set_digest,
                    primary.requirement_set_digest,
                ),
                "acceptance_review_requirement_set_digest_mismatch",
            ),
            (
                review.union_contract_revision == primary.union_contract_revision,
                "acceptance_review_union_contract_revision_mismatch",
            ),
            (
                review.decision == primary.decision,
                "acceptance_receipt_decision_conflict",
            ),
            (
                review.conflict_state == primary.conflict_state,
                "acceptance_conflict_state_mismatch",
            ),
        )
        for accepted, code in checks:
            if not accepted:
                raise AcceptanceReceiptError(code)
    return reviews


def _decision(value: object, code: str) -> str:
    decision = str(value or "").strip().casefold()
    if decision not in _ACCEPTANCE_DECISIONS:
        raise AcceptanceReceiptError(code)
    return decision


def _conflict_state(value: object, code: str) -> str:
    conflict_state = str(value or "").strip().casefold()
    if conflict_state not in _CONFLICT_STATES:
        raise AcceptanceReceiptError(code)
    return conflict_state


def _safe_identifier(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SAFE_IDENTIFIER.fullmatch(text) is None:
        raise AcceptanceReceiptError(code)
    probe = _public_probe(
        event_id="event:acceptance:identifier-validator",
        project=ProjectScope("project:acceptance-validator"),
        coordination_session_id="coord:acceptance-validator",
        work_item_id="work:acceptance-validator",
        actor=AgentIdentity("agent:acceptance-identifier-validator", "reviewer"),
        created_at="2000-01-01T00:00:00Z",
        subject_refs=(text,),
        evidence_refs=("evidence:acceptance-identifier-validator",),
        error_prefix="acceptance_receipt",
    )
    return probe.subject_refs[0]


def _require_pinned_source_revision(value: object) -> str:
    text = str(value or "").strip()
    if _PINNED_SOURCE_REVISION.fullmatch(text) is None:
        raise AcceptanceReceiptError("acceptance_source_revision_not_pinned")
    return text


def _require_sha256(value: object, code: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise AcceptanceReceiptError(code)
    return text


def _parse_time(value: object, code: str) -> datetime:
    if not isinstance(value, str) or value != value.strip():
        raise AcceptanceReceiptError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceReceiptError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcceptanceReceiptError(code)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime, code: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceReceiptError(code)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(value: object) -> str:
    encoded = _canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "ACCEPTANCE_RECEIPT_ISSUER",
    "ACCEPTANCE_RECEIPT_SCHEMA",
    "REVIEW_RECEIPT_SCHEMA",
    "REVIEW_RECEIPT_ISSUER",
    "AcceptanceAuthorityRepository",
    "AcceptanceReceipt",
    "AcceptanceReceiptAuthority",
    "AcceptanceReceiptError",
    "InMemoryAcceptanceAuthorityRepository",
    "ReviewReceipt",
    "ServerAcceptanceSourceRegistry",
    "VerifiedAcceptanceReceipt",
    "open_server_acceptance_receipt_authority",
    "open_server_acceptance_source_registry",
]
