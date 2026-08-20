"""Server-owned activity audit and proof-gated WorkItem dispatch.

The coordinator has one narrow authority path:

1. ``audit_activity`` validates a public Agent activity update and gathers one
   independently bound observation from each mandatory evidence adapter.
2. ``dispatch_eligible`` consumes the exact server-issued audit receipt and
   source update before dependency or dispatch callbacks can run.

Agent narrative never proves completion.  Only the ResultReceipt evidence
adapter may do that.  This module grants no role, lease, merge, deployment,
promotion, persistence, tool-policy, or canonical-memory authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .activity_update import (
    ActivityAuditAuthority,
    ActivityAuditReceipt,
    ActivityContractError,
    AgentActivityUpdate,
    open_server_activity_audit_authority,
)
from .canonical_time import canonical_text, server_now_text
from .contracts import ProjectScope
from .lease_contract import AGENT_OWNER_KIND, AGENT_WORK_POLICY, WorkItem

if TYPE_CHECKING:
    from datetime import datetime

AuditStatus = Literal["verified", "mismatch", "overlap", "stale", "blocked"]
EvidenceKind = Literal["lease", "event", "git_diff", "result_receipt"]

AUDIT_STATUSES = frozenset({"verified", "mismatch", "overlap", "stale", "blocked"})
EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    "lease",
    "event",
    "git_diff",
    "result_receipt",
)

_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_COMPLETION_RE = re.compile(
    r"(?:\b(?:complete|completed|completion|done|finish|finished)\b|已完成|完成)",
    re.IGNORECASE,
)
_MAX_WORK_ITEMS = 64
_MAX_REASON_CODES = 24
_MAX_EVIDENCE_PATHS = 64
_MAX_EVIDENCE_PATH_BYTES = 512
_MAX_GENERATION = (1 << 63) - 1
_COORDINATOR_RECEIPT_SCHEMA = "collaboration-coordinator-activity-audit/v3"
_COORDINATOR_RECEIPT_ISSUER = "pp-server-backend"
_RECEIPT_ISSUE_TOKEN = object()
_SERVER_COORDINATOR_AUTHORITY_TOKEN = object()


class CoordinatorAuditError(ValueError):
    """Stable rejection from the coordinator audit seam."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CoordinatorDispatchError(RuntimeError):
    """Stable rejection or failure from proof-gated dispatch."""


@runtime_checkable
class AgentActivityUpdateLike(Protocol):
    """Small structural contract exposed to read-only evidence adapters."""

    @property
    def content_sha256(self) -> str: ...

    @property
    def work_item_id(self) -> str: ...

    @property
    def role_assignment_sha256(self) -> str: ...

    @property
    def cursor(self) -> int: ...

    @property
    def evidence_paths(self) -> tuple[str, ...]: ...

    @property
    def planned_paths(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    """One adapter-owned observation bound to an exact activity update.

    ``observed_paths`` is authoritative only for the ``git_diff`` adapter.
    The coordinator compares it with ``AgentActivityUpdate.evidence_paths``;
    future ``planned_paths`` never reserve files or influence verification.
    """

    status: AuditStatus
    activity_update_sha256: str
    activity_scope_sha256: str
    evidence_sha256: str
    work_item_id: str
    role_assignment_sha256: str
    cursor: int
    observed_paths: tuple[str, ...] = ()
    proves_completion: bool = False

    def __post_init__(self) -> None:
        if self.status not in AUDIT_STATUSES:
            raise CoordinatorAuditError("coordinator_evidence_status_invalid")
        for value, code in (
            (self.activity_update_sha256, "coordinator_evidence_activity_digest_invalid"),
            (self.activity_scope_sha256, "coordinator_evidence_scope_digest_invalid"),
            (self.evidence_sha256, "coordinator_evidence_digest_invalid"),
        ):
            _digest(value, code)
        _identifier(self.work_item_id, "coordinator_evidence_work_item_invalid", optional=True)
        if self.role_assignment_sha256:
            _digest(
                self.role_assignment_sha256,
                "coordinator_evidence_role_assignment_digest_invalid",
            )
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int) or self.cursor < 0:
            raise CoordinatorAuditError("coordinator_evidence_cursor_invalid")
        object.__setattr__(self, "observed_paths", _evidence_paths(self.observed_paths))
        if not isinstance(self.proves_completion, bool):
            raise CoordinatorAuditError("coordinator_evidence_completion_flag_invalid")


@runtime_checkable
class EvidenceAdapter(Protocol):
    """Read-only evidence adapter; it performs no canonical mutation."""

    def inspect(
        self,
        update: AgentActivityUpdateLike,
        receipt: ActivityAuditReceipt,
    ) -> EvidenceObservation | None: ...


@dataclass(frozen=True, slots=True, init=False)
class CoordinatorActivityAuditReceipt:
    """Factory-issued coordinator decision with exact evidence lineage.

    Public JSON is evidence, not authority.  Dispatch additionally requires an
    exact digest match against the issuing authority's current repository head.
    Caller construction, foreign authorities, stale generations, and in-place
    mutation therefore cannot authorize a callback.  Exact canonical receipts
    remain verifiable after a process restart.
    """

    receipt_id: str
    authority_id: str
    audit_generation: int
    activity_receipt: ActivityAuditReceipt
    status: AuditStatus
    evidence_lineage: tuple[tuple[EvidenceKind, str], ...]
    reason_codes: tuple[str, ...]
    completion_verified: bool
    authority_effect: str
    tool_policy_effect: str
    canonical_memory_effect: str
    merge_effect: str
    deploy_effect: str
    persistence_effect: str
    promotion_effect: str

    def __init__(self, *_: object, **__: object) -> None:
        raise CoordinatorAuditError("coordinator_audit_receipt_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        receipt_id: str,
        authority_id: str,
        audit_generation: int,
        activity_receipt: ActivityAuditReceipt,
        status: AuditStatus,
        evidence_lineage: tuple[tuple[EvidenceKind, str], ...],
        reason_codes: tuple[str, ...],
        completion_verified: bool,
        _token: object,
    ) -> CoordinatorActivityAuditReceipt:
        if _token is not _RECEIPT_ISSUE_TOKEN:
            raise CoordinatorAuditError("coordinator_audit_receipt_server_required")
        instance = object.__new__(cls)
        values: dict[str, object] = {
            "receipt_id": receipt_id,
            "authority_id": authority_id,
            "audit_generation": audit_generation,
            "activity_receipt": activity_receipt,
            "status": status,
            "evidence_lineage": evidence_lineage,
            "reason_codes": reason_codes,
            "completion_verified": completion_verified,
            "authority_effect": "none",
            "tool_policy_effect": "none",
            "canonical_memory_effect": "none",
            "merge_effect": "none",
            "deploy_effect": "none",
            "persistence_effect": "none",
            "promotion_effect": "none",
        }
        for field_name, value in values.items():
            object.__setattr__(instance, field_name, value)
        instance.validate_integrity()
        return instance

    @classmethod
    def _rehydrate(
        cls,
        *,
        receipt_id: str,
        authority_id: str,
        audit_generation: int,
        activity_receipt: ActivityAuditReceipt,
        status: AuditStatus,
        evidence_lineage: tuple[tuple[EvidenceKind, str], ...],
        reason_codes: tuple[str, ...],
        completion_verified: bool,
        _authority_token: object | None = None,
    ) -> CoordinatorActivityAuditReceipt:
        """Rebuild one exact receipt for a server-owned repository adapter."""

        if _authority_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_audit_rehydrate_authority_required")
        return cls._issue(
            receipt_id=receipt_id,
            authority_id=authority_id,
            audit_generation=audit_generation,
            activity_receipt=activity_receipt,
            status=status,
            evidence_lineage=evidence_lineage,
            reason_codes=reason_codes,
            completion_verified=completion_verified,
            _token=_RECEIPT_ISSUE_TOKEN,
        )

    def validate_integrity(self) -> None:
        receipt_id = _identifier(self.receipt_id, "coordinator_audit_receipt_id_invalid")
        if not receipt_id.startswith("coordinator-audit:"):
            raise CoordinatorAuditError("coordinator_audit_receipt_id_invalid")
        authority_id = _identifier(self.authority_id, "coordinator_audit_authority_id_invalid")
        if not authority_id.startswith("coordinator-audit-authority:"):
            raise CoordinatorAuditError("coordinator_audit_authority_id_invalid")
        if (
            isinstance(self.audit_generation, bool)
            or not isinstance(self.audit_generation, int)
            or not 1 <= self.audit_generation <= _MAX_GENERATION
        ):
            raise CoordinatorAuditError("coordinator_audit_generation_invalid")
        if not isinstance(self.activity_receipt, ActivityAuditReceipt):
            raise CoordinatorAuditError("coordinator_activity_receipt_invalid")
        try:
            self.activity_receipt.validate_integrity()
        except ActivityContractError as exc:
            raise CoordinatorAuditError(f"coordinator_{exc.code}") from exc
        if self.status not in AUDIT_STATUSES:
            raise CoordinatorAuditError("coordinator_audit_status_invalid")
        _validate_evidence_lineage(self.evidence_lineage)
        if len(self.reason_codes) > _MAX_REASON_CODES or any(
            not isinstance(code, str) or _IDENTIFIER_RE.fullmatch(code) is None
            for code in self.reason_codes
        ):
            raise CoordinatorAuditError("coordinator_reason_codes_invalid")
        if not isinstance(self.completion_verified, bool):
            raise CoordinatorAuditError("coordinator_completion_flag_invalid")
        if self.completion_verified and self.status != "verified":
            raise CoordinatorAuditError("coordinator_completion_status_mismatch")
        if self.status == "verified":
            if self.reason_codes:
                raise CoordinatorAuditError("coordinator_verified_reasons_forbidden")
            if self.evidence_kinds != EVIDENCE_KINDS:
                raise CoordinatorAuditError("coordinator_complete_evidence_required")
        elif not self.reason_codes:
            raise CoordinatorAuditError("coordinator_nonverified_reason_required")
        if any(
            effect != "none"
            for effect in (
                self.authority_effect,
                self.tool_policy_effect,
                self.canonical_memory_effect,
                self.merge_effect,
                self.deploy_effect,
                self.persistence_effect,
                self.promotion_effect,
            )
        ):
            raise CoordinatorAuditError("coordinator_authority_effect_forbidden")
        if self.receipt_id != _coordinator_receipt_id_for(
            authority_id=self.authority_id,
            activity_update_sha256=self.activity_update_sha256,
            generation=self.audit_generation,
        ):
            raise CoordinatorAuditError("coordinator_audit_receipt_tampered")

    @property
    def evidence_kinds(self) -> tuple[EvidenceKind, ...]:
        return tuple(kind for kind, _digest_value in self.evidence_lineage)

    @property
    def evidence_sha256s(self) -> tuple[str, ...]:
        return tuple(digest_value for _kind, digest_value in self.evidence_lineage)

    @property
    def activity_update_sha256(self) -> str:
        return self.activity_receipt.activity_update_sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _COORDINATOR_RECEIPT_SCHEMA,
            "issuer": _COORDINATOR_RECEIPT_ISSUER,
            "receipt_id": self.receipt_id,
            "authority_id": self.authority_id,
            "audit_generation": self.audit_generation,
            "activity_receipt": self.activity_receipt.to_dict(),
            "activity_receipt_sha256": self.activity_receipt.content_sha256,
            "activity_narrative": "omitted",
            "status": self.status,
            "evidence_lineage": [
                {"kind": kind, "evidence_sha256": evidence_sha256}
                for kind, evidence_sha256 in self.evidence_lineage
            ],
            "reason_codes": list(self.reason_codes),
            "completion_verified": self.completion_verified,
            "authority_effect": self.authority_effect,
            "tool_policy_effect": self.tool_policy_effect,
            "canonical_memory_effect": self.canonical_memory_effect,
            "merge_effect": self.merge_effect,
            "deploy_effect": self.deploy_effect,
            "persistence_effect": self.persistence_effect,
            "promotion_effect": self.promotion_effect,
            "verification": "durable-current-head-required",
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_sha256(self) -> str:
        material = self.canonical_json().encode("utf-8")
        return "sha256:" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class CoordinatorAuditRecord:
    """One append-only coordinator generation resolved from canonical storage."""

    project: ProjectScope
    coordination_session_id: str
    receipt: CoordinatorActivityAuditReceipt
    recorded_at_utc: str

    def validate_integrity(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise CoordinatorAuditError("coordinator_audit_record_project_invalid")
        coordination_session_id = _identifier(
            self.coordination_session_id,
            "coordinator_audit_record_session_invalid",
        )
        if not isinstance(self.receipt, CoordinatorActivityAuditReceipt):
            raise CoordinatorAuditError("coordinator_audit_record_receipt_invalid")
        self.receipt.validate_integrity()
        if (
            self.receipt.activity_receipt.scope.project != self.project
            or self.receipt.activity_receipt.scope.coordination_session_id
            != coordination_session_id
        ):
            raise CoordinatorAuditError("coordinator_audit_record_scope_mismatch")
        if self.receipt.authority_id != _coordinator_authority_id(
            self.project, coordination_session_id
        ):
            raise CoordinatorAuditError("coordinator_audit_record_authority_mismatch")
        _canonical_timestamp(
            self.recorded_at_utc,
            "coordinator_audit_recorded_at_invalid",
        )


@dataclass(frozen=True, slots=True)
class CoordinatorAuditConsumption:
    """Append-only proof that one activity update's current audit was consumed."""

    receipt_id: str
    receipt_sha256: str
    activity_update_sha256: str
    audit_generation: int
    consumed_at_utc: str

    def validate_integrity(self) -> None:
        receipt_id = _identifier(
            self.receipt_id,
            "coordinator_consumption_receipt_id_invalid",
        )
        if not receipt_id.startswith("coordinator-audit:"):
            raise CoordinatorAuditError("coordinator_consumption_receipt_id_invalid")
        _digest(
            self.receipt_sha256,
            "coordinator_consumption_receipt_digest_invalid",
        )
        _digest(
            self.activity_update_sha256,
            "coordinator_consumption_activity_digest_invalid",
        )
        _generation(
            self.audit_generation,
            "coordinator_consumption_generation_invalid",
        )
        _canonical_timestamp(
            self.consumed_at_utc,
            "coordinator_consumption_time_invalid",
        )


@runtime_checkable
class CoordinatorAuditRepository(Protocol):
    """Append-only generation/head seam used by the coordinator authority."""

    def load_by_receipt_id(
        self,
        receipt_id: str,
    ) -> CoordinatorAuditRecord | None: ...

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        activity_update_sha256: str,
    ) -> CoordinatorAuditRecord | None: ...

    def append_generation(
        self,
        record: CoordinatorAuditRecord,
        *,
        expected_generation: int,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditRecord: ...

    def load_consumption(
        self,
        activity_update_sha256: str,
    ) -> CoordinatorAuditConsumption | None: ...

    def consume_current(
        self,
        record: CoordinatorAuditRecord,
        *,
        consumed_at_utc: str,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditConsumption: ...


class InMemoryCoordinatorAuditRepository:
    """Process-local adapter for focused tests and isolated composition."""

    def __init__(self) -> None:
        self._by_receipt_id: dict[str, CoordinatorAuditRecord] = {}
        self._current_by_update: dict[tuple[str, str, str], CoordinatorAuditRecord] = {}
        self._consumed_by_update: dict[str, CoordinatorAuditConsumption] = {}

    def load_by_receipt_id(
        self,
        receipt_id: str,
    ) -> CoordinatorAuditRecord | None:
        normalized = _coordinator_receipt_id(receipt_id)
        record = self._by_receipt_id.get(normalized)
        return None if record is None else _copy_coordinator_record(record)

    def load_current(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        activity_update_sha256: str,
    ) -> CoordinatorAuditRecord | None:
        record = self._current_by_update.get(
            _coordinator_head_key(
                project_id=project_id,
                coordination_session_id=coordination_session_id,
                activity_update_sha256=activity_update_sha256,
            )
        )
        return None if record is None else _copy_coordinator_record(record)

    def append_generation(
        self,
        record: CoordinatorAuditRecord,
        *,
        expected_generation: int,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditRecord:
        if _authority_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_audit_repository_write_authority_required")
        _validate_coordinator_record(record)
        prior_generation = _prior_generation(expected_generation)
        if record.receipt.audit_generation != prior_generation + 1:
            raise CoordinatorAuditError("coordinator_audit_generation_invalid")
        existing = self._by_receipt_id.get(record.receipt.receipt_id)
        if existing is not None:
            _require_same_coordinator_receipt(existing, record)
            return _copy_coordinator_record(existing)
        key = _coordinator_head_key(
            project_id=record.project.project_id,
            coordination_session_id=record.coordination_session_id,
            activity_update_sha256=record.receipt.activity_update_sha256,
        )
        current = self._current_by_update.get(key)
        actual_generation = 0 if current is None else current.receipt.audit_generation
        if actual_generation != prior_generation:
            raise CoordinatorAuditError("coordinator_audit_generation_conflict")
        stored = _copy_coordinator_record(record)
        self._by_receipt_id[record.receipt.receipt_id] = stored
        self._current_by_update[key] = stored
        return _copy_coordinator_record(stored)

    def load_consumption(
        self,
        activity_update_sha256: str,
    ) -> CoordinatorAuditConsumption | None:
        consumption = self._consumed_by_update.get(
            _digest(
                activity_update_sha256,
                "coordinator_consumption_activity_digest_invalid",
            )
        )
        return consumption

    def consume_current(
        self,
        record: CoordinatorAuditRecord,
        *,
        consumed_at_utc: str,
        _authority_token: object | None = None,
    ) -> CoordinatorAuditConsumption:
        if _authority_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_consumption_write_authority_required")
        _validate_coordinator_record(record)
        current = self.load_current(
            project_id=record.project.project_id,
            coordination_session_id=record.coordination_session_id,
            activity_update_sha256=record.receipt.activity_update_sha256,
        )
        if current is None or current.receipt.receipt_id != record.receipt.receipt_id:
            raise CoordinatorAuditError("coordinator_audit_receipt_superseded")
        _require_exact_coordinator_record(current, record)
        if record.receipt.activity_update_sha256 in self._consumed_by_update:
            raise CoordinatorAuditError("coordinator_audit_receipt_replayed")
        consumption = CoordinatorAuditConsumption(
            receipt_id=record.receipt.receipt_id,
            receipt_sha256=record.receipt.content_sha256,
            activity_update_sha256=record.receipt.activity_update_sha256,
            audit_generation=record.receipt.audit_generation,
            consumed_at_utc=_canonical_timestamp(
                consumed_at_utc,
                "coordinator_consumption_time_invalid",
            ),
        )
        consumption.validate_integrity()
        self._consumed_by_update[consumption.activity_update_sha256] = consumption
        return consumption


class CoordinatorAuditAuthority:
    """Server issuer/verifier over a repository-backed current-head ledger."""

    def __init__(
        self,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        repository: CoordinatorAuditRepository,
        clock: Callable[[], datetime] | None = None,
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_COORDINATOR_AUTHORITY_TOKEN:
            raise CoordinatorAuditError("coordinator_audit_server_authority_required")
        if not isinstance(project, ProjectScope):
            raise CoordinatorAuditError("coordinator_project_invalid")
        coordination = _identifier(
            coordination_session_id,
            "coordinator_coordination_session_invalid",
        )
        if not isinstance(repository, CoordinatorAuditRepository):
            raise CoordinatorAuditError("coordinator_audit_repository_invalid")
        if clock is not None and not callable(clock):
            raise CoordinatorAuditError("coordinator_audit_clock_invalid")
        self._project = project
        self._coordination_session_id = coordination
        self._repository = repository
        self._clock = clock
        self._authority_id = _coordinator_authority_id(project, coordination)

    @property
    def project(self) -> ProjectScope:
        return self._project

    @property
    def coordination_session_id(self) -> str:
        return self._coordination_session_id

    @property
    def authority_id(self) -> str:
        return self._authority_id

    def issue(
        self,
        *,
        activity_receipt: ActivityAuditReceipt,
        status: AuditStatus,
        evidence_lineage: tuple[tuple[EvidenceKind, str], ...],
        reason_codes: tuple[str, ...],
        completion_verified: bool,
    ) -> CoordinatorActivityAuditReceipt:
        if not isinstance(activity_receipt, ActivityAuditReceipt):
            raise CoordinatorAuditError("coordinator_activity_receipt_invalid")
        activity_receipt.validate_integrity()
        if (
            activity_receipt.scope.project != self._project
            or activity_receipt.scope.coordination_session_id != self._coordination_session_id
        ):
            raise CoordinatorAuditError("coordinator_audit_receipt_scope_mismatch")
        current = self._repository.load_current(
            project_id=self._project.project_id,
            coordination_session_id=self._coordination_session_id,
            activity_update_sha256=activity_receipt.activity_update_sha256,
        )
        expected_generation = 0 if current is None else current.receipt.audit_generation
        generation = expected_generation + 1
        receipt_id = _coordinator_receipt_id_for(
            authority_id=self._authority_id,
            activity_update_sha256=activity_receipt.activity_update_sha256,
            generation=generation,
        )
        receipt = CoordinatorActivityAuditReceipt._issue(
            receipt_id=receipt_id,
            authority_id=self._authority_id,
            audit_generation=generation,
            activity_receipt=activity_receipt,
            status=status,
            evidence_lineage=evidence_lineage,
            reason_codes=reason_codes,
            completion_verified=completion_verified,
            _token=_RECEIPT_ISSUE_TOKEN,
        )
        try:
            recorded_at = server_now_text(self._clock)
        except (TypeError, ValueError) as exc:
            raise CoordinatorAuditError("coordinator_audit_clock_invalid") from exc
        record = CoordinatorAuditRecord(
            project=self._project,
            coordination_session_id=self._coordination_session_id,
            receipt=receipt,
            recorded_at_utc=recorded_at,
        )
        stored = self._repository.append_generation(
            record,
            expected_generation=expected_generation,
            _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
        )
        _require_exact_coordinator_record(stored, record)
        return stored.receipt

    def verify_current(
        self,
        receipt: CoordinatorActivityAuditReceipt,
    ) -> CoordinatorActivityAuditReceipt:
        if not isinstance(receipt, CoordinatorActivityAuditReceipt):
            raise CoordinatorAuditError("coordinator_audit_receipt_invalid")
        receipt.validate_integrity()
        if receipt.authority_id != self._authority_id:
            raise CoordinatorAuditError("coordinator_audit_receipt_foreign_authority")
        stored = self._repository.load_by_receipt_id(receipt.receipt_id)
        if stored is None:
            raise CoordinatorAuditError("coordinator_audit_receipt_not_server_issued")
        stored.validate_integrity()
        if not hmac.compare_digest(
            stored.receipt.content_sha256,
            receipt.content_sha256,
        ):
            raise CoordinatorAuditError("coordinator_audit_receipt_tampered")
        current = self._repository.load_current(
            project_id=self._project.project_id,
            coordination_session_id=self._coordination_session_id,
            activity_update_sha256=receipt.activity_update_sha256,
        )
        if current is None or current.receipt.receipt_id != receipt.receipt_id:
            raise CoordinatorAuditError("coordinator_audit_receipt_superseded")
        _require_exact_coordinator_record(current, stored)
        return stored.receipt

    def is_consumed(self, receipt: CoordinatorActivityAuditReceipt) -> bool:
        canonical = self.verify_current(receipt)
        consumption = self._repository.load_consumption(canonical.activity_update_sha256)
        if consumption is None:
            return False
        consumption.validate_integrity()
        consumed_record = self._repository.load_by_receipt_id(consumption.receipt_id)
        if consumed_record is None:
            raise CoordinatorAuditError("coordinator_consumption_record_corrupt")
        consumed_record.validate_integrity()
        if (
            not hmac.compare_digest(
                consumption.activity_update_sha256,
                canonical.activity_update_sha256,
            )
            or not hmac.compare_digest(
                consumption.activity_update_sha256,
                consumed_record.receipt.activity_update_sha256,
            )
            or not hmac.compare_digest(
                consumption.receipt_sha256,
                consumed_record.receipt.content_sha256,
            )
            or consumption.audit_generation != consumed_record.receipt.audit_generation
        ):
            raise CoordinatorAuditError("coordinator_consumption_record_corrupt")
        return True

    def consume_current(
        self,
        receipt: CoordinatorActivityAuditReceipt,
    ) -> CoordinatorAuditConsumption:
        canonical = self.verify_current(receipt)
        if self.is_consumed(canonical):
            raise CoordinatorAuditError("coordinator_audit_receipt_replayed")
        try:
            consumed_at = server_now_text(self._clock)
        except (TypeError, ValueError) as exc:
            raise CoordinatorAuditError("coordinator_audit_clock_invalid") from exc
        record = self._repository.load_by_receipt_id(canonical.receipt_id)
        if record is None:
            raise CoordinatorAuditError("coordinator_audit_receipt_not_server_issued")
        return self._repository.consume_current(
            record,
            consumed_at_utc=consumed_at,
            _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
        )


def open_server_coordinator_audit_authority(
    *,
    project: ProjectScope,
    coordination_session_id: str,
    repository: CoordinatorAuditRepository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CoordinatorAuditAuthority:
    """Open a server authority over an injected or isolated audit ledger."""

    selected = repository or InMemoryCoordinatorAuditRepository()
    return CoordinatorAuditAuthority(
        project=project,
        coordination_session_id=coordination_session_id,
        repository=selected,
        clock=clock,
        _server_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
    )


DependencyCallback = Callable[[WorkItem], bool]
DispatchCallback = Callable[[WorkItem], object]


class CoordinatorSupervisor:
    """Audit four evidence ports and consume proof before downstream dispatch."""

    def __init__(
        self,
        *,
        project: ProjectScope,
        coordination_session_id: str,
        activity_authority: ActivityAuditAuthority | None = None,
        coordinator_authority: CoordinatorAuditAuthority | None = None,
        lease_adapter: EvidenceAdapter | Callable[..., EvidenceObservation | None] | None = None,
        event_adapter: EvidenceAdapter | Callable[..., EvidenceObservation | None] | None = None,
        git_diff_adapter: EvidenceAdapter | Callable[..., EvidenceObservation | None] | None = None,
        result_receipt_adapter: EvidenceAdapter
        | Callable[..., EvidenceObservation | None]
        | None = None,
        max_work_items: int = _MAX_WORK_ITEMS,
    ) -> None:
        if not isinstance(project, ProjectScope):
            raise CoordinatorAuditError("coordinator_project_invalid")
        self._project = project
        self._coordination_session_id = _identifier(
            coordination_session_id,
            "coordinator_coordination_session_invalid",
        )
        if activity_authority is not None and not isinstance(
            activity_authority,
            ActivityAuditAuthority,
        ):
            raise CoordinatorAuditError("coordinator_activity_authority_invalid")
        self._activity_authority = activity_authority or open_server_activity_audit_authority()
        if coordinator_authority is not None and not isinstance(
            coordinator_authority,
            CoordinatorAuditAuthority,
        ):
            raise CoordinatorAuditError("coordinator_audit_authority_invalid")
        self._coordinator_authority = coordinator_authority or (
            open_server_coordinator_audit_authority(
                project=self._project,
                coordination_session_id=self._coordination_session_id,
            )
        )
        if (
            self._coordinator_authority.project != self._project
            or self._coordinator_authority.coordination_session_id != self._coordination_session_id
        ):
            raise CoordinatorAuditError("coordinator_audit_authority_scope_mismatch")
        self._adapters: tuple[tuple[EvidenceKind, object], ...] = (
            ("lease", _require_adapter("lease", lease_adapter)),
            ("event", _require_adapter("event", event_adapter)),
            ("git_diff", _require_adapter("git_diff", git_diff_adapter)),
            (
                "result_receipt",
                _require_adapter("result_receipt", result_receipt_adapter),
            ),
        )
        if (
            isinstance(max_work_items, bool)
            or not isinstance(max_work_items, int)
            or not 1 <= max_work_items <= _MAX_WORK_ITEMS
        ):
            raise CoordinatorAuditError("coordinator_max_work_items_invalid")
        self._max_work_items = max_work_items

    def audit_activity(self, update: AgentActivityUpdate) -> CoordinatorActivityAuditReceipt:
        """Gather exactly one observation per port and issue an audit receipt."""

        if not isinstance(update, AgentActivityUpdate):
            raise CoordinatorAuditError("coordinator_activity_update_invalid")
        try:
            activity_receipt = self._activity_authority.issue(update)
            activity_receipt = self._activity_authority.verify_issued(
                activity_receipt,
                update=update,
            )
        except ActivityContractError as exc:
            raise CoordinatorAuditError(f"coordinator_{exc.code}") from exc

        reasons: dict[AuditStatus, list[str]] = {
            "verified": [],
            "mismatch": [],
            "overlap": [],
            "stale": [],
            "blocked": [],
        }
        if activity_receipt.scope.project != self._project:
            _add(reasons["mismatch"], "activity_project_mismatch")
        if activity_receipt.scope.coordination_session_id != self._coordination_session_id:
            _add(reasons["mismatch"], "activity_coordination_session_mismatch")

        evidence_lineage: list[tuple[EvidenceKind, str]] = []
        verified_kinds: set[EvidenceKind] = set()
        evidence_digests: set[str] = set()
        completion_verified = False
        for kind, adapter in self._adapters:
            try:
                observation = _inspect(adapter, update, activity_receipt)
            except Exception:
                _add(reasons["blocked"], f"{kind}_adapter_error")
                continue
            if observation is None:
                _add(reasons["blocked"], f"{kind}_evidence_missing")
                continue

            evidence_lineage.append((kind, observation.evidence_sha256))
            binding_verified = True
            if observation.evidence_sha256 in evidence_digests:
                _add(reasons["mismatch"], "evidence_digest_reused")
                binding_verified = False
            evidence_digests.add(observation.evidence_sha256)
            if not hmac.compare_digest(
                observation.activity_update_sha256,
                activity_receipt.activity_update_sha256,
            ):
                _add(reasons["mismatch"], f"{kind}_activity_digest_mismatch")
                binding_verified = False
            if not hmac.compare_digest(
                observation.activity_scope_sha256,
                activity_receipt.activity_scope_sha256,
            ):
                _add(reasons["mismatch"], f"{kind}_scope_digest_mismatch")
                binding_verified = False
            if observation.work_item_id != activity_receipt.work_item_id:
                _add(reasons["mismatch"], f"{kind}_work_item_mismatch")
                binding_verified = False
            if observation.role_assignment_sha256 != activity_receipt.role_assignment_sha256:
                _add(reasons["mismatch"], f"{kind}_role_assignment_digest_mismatch")
                binding_verified = False
            if observation.cursor < activity_receipt.cursor:
                _add(reasons["mismatch"], f"{kind}_cursor_unverified")
                binding_verified = False
            elif observation.cursor > activity_receipt.cursor:
                _add(reasons["stale"], "activity_cursor_stale")
                binding_verified = False
            if kind == "git_diff" and observation.observed_paths != update.evidence_paths:
                _add(reasons["mismatch"], "git_diff_evidence_paths_mismatch")
                binding_verified = False
            if kind != "result_receipt" and observation.proves_completion:
                _add(reasons["mismatch"], f"{kind}_completion_proof_forbidden")
                binding_verified = False

            if observation.status == "verified" and binding_verified:
                verified_kinds.add(kind)
                if kind == "result_receipt" and observation.proves_completion:
                    completion_verified = True
            elif observation.status != "verified":
                _add(reasons[observation.status], f"{kind}_{observation.status}")

        if update.blockers:
            _add(reasons["blocked"], "activity_reported_blockers")
        if _claims_completion(update) and not completion_verified:
            _add(reasons["blocked"], "completion_evidence_required")
        if verified_kinds != set(EVIDENCE_KINDS) and not any(
            reasons[status] for status in ("mismatch", "overlap", "stale", "blocked")
        ):
            _add(reasons["blocked"], "complete_evidence_set_required")

        status = _select_status(reasons, verified_kinds)
        completion_verified = completion_verified and status == "verified"
        return self._issue_receipt(
            activity_receipt=activity_receipt,
            status=status,
            evidence_lineage=tuple(evidence_lineage),
            reason_codes=tuple(reasons[status]),
            completion_verified=completion_verified,
        )

    def dispatch_eligible(
        self,
        *,
        audit_receipt: CoordinatorActivityAuditReceipt,
        source_update: AgentActivityUpdate,
        work_items: Iterable[WorkItem],
        dependency_callback: DependencyCallback,
        dispatch_callback: DispatchCallback,
    ) -> tuple[str, ...]:
        """Consume verified completion proof, then dispatch ready Agent work.

        The receipt is intentionally one-shot.  A second use, even with the
        exact same object, is rejected before either callback can run.
        """

        self._verify_dispatch_proof(audit_receipt, source_update)
        if not callable(dependency_callback):
            raise CoordinatorDispatchError("coordinator_dependency_callback_required")
        if not callable(dispatch_callback):
            raise CoordinatorDispatchError("coordinator_dispatch_callback_required")

        batch = tuple(islice(iter(work_items), self._max_work_items + 1))
        if len(batch) > self._max_work_items:
            raise CoordinatorDispatchError("coordinator_dispatch_batch_too_large")

        candidates: list[WorkItem] = []
        seen: dict[str, str] = {}
        for work_item in batch:
            if not isinstance(work_item, WorkItem):
                raise CoordinatorDispatchError("coordinator_dispatch_work_item_invalid")
            if (
                work_item.project != self._project
                or work_item.coordination_session_id != self._coordination_session_id
                or work_item.owner_kind != AGENT_OWNER_KIND
                or work_item.policy_kind != AGENT_WORK_POLICY
            ):
                continue
            prior_digest = seen.get(work_item.work_item_id)
            if prior_digest is not None:
                code = (
                    "coordinator_dispatch_work_item_duplicate"
                    if hmac.compare_digest(prior_digest, work_item.content_sha256)
                    else "coordinator_dispatch_work_item_conflict"
                )
                raise CoordinatorDispatchError(code)
            seen[work_item.work_item_id] = work_item.content_sha256
            if work_item.work_item_id == audit_receipt.activity_receipt.work_item_id:
                continue
            candidates.append(work_item)

        try:
            self._coordinator_authority.consume_current(audit_receipt)
        except CoordinatorAuditError as exc:
            raise CoordinatorDispatchError(exc.code) from exc

        dispatched: list[str] = []
        for work_item in candidates:
            try:
                ready = dependency_callback(work_item)
            except Exception:
                continue
            if ready is not True:
                continue
            try:
                dispatch_callback(work_item)
            except Exception as exc:
                raise CoordinatorDispatchError("coordinator_dispatch_callback_failed") from exc
            dispatched.append(work_item.work_item_id)
        return tuple(dispatched)

    def _issue_receipt(
        self,
        *,
        activity_receipt: ActivityAuditReceipt,
        status: AuditStatus,
        evidence_lineage: tuple[tuple[EvidenceKind, str], ...],
        reason_codes: tuple[str, ...],
        completion_verified: bool,
    ) -> CoordinatorActivityAuditReceipt:
        return self._coordinator_authority.issue(
            activity_receipt=activity_receipt,
            status=status,
            evidence_lineage=evidence_lineage,
            reason_codes=reason_codes,
            completion_verified=completion_verified,
        )

    def _verify_dispatch_proof(
        self,
        receipt: CoordinatorActivityAuditReceipt,
        update: AgentActivityUpdate,
    ) -> None:
        if not isinstance(receipt, CoordinatorActivityAuditReceipt):
            raise CoordinatorDispatchError("coordinator_audit_receipt_invalid")
        if not isinstance(update, AgentActivityUpdate):
            raise CoordinatorDispatchError("coordinator_source_update_invalid")
        try:
            receipt.validate_integrity()
        except CoordinatorAuditError as exc:
            raise CoordinatorDispatchError(exc.code) from exc
        try:
            issued = self._coordinator_authority.verify_current(receipt)
        except CoordinatorAuditError as exc:
            raise CoordinatorDispatchError(exc.code) from exc
        try:
            activity_receipt = self._activity_authority.verify_issued(
                issued.activity_receipt,
                update=update,
            )
        except ActivityContractError as exc:
            raise CoordinatorDispatchError(f"coordinator_{exc.code}") from exc
        if not hmac.compare_digest(
            activity_receipt.content_sha256,
            receipt.activity_receipt.content_sha256,
        ):
            raise CoordinatorDispatchError("coordinator_activity_receipt_not_exact")
        if (
            update.project != self._project
            or update.coordination_session_id != self._coordination_session_id
        ):
            raise CoordinatorDispatchError("coordinator_source_scope_mismatch")
        if not update.work_item_id or not update.role_assignment_sha256:
            raise CoordinatorDispatchError("coordinator_source_work_authority_required")
        if receipt.status != "verified":
            raise CoordinatorDispatchError("coordinator_verified_audit_required")
        if not receipt.completion_verified:
            raise CoordinatorDispatchError("coordinator_verified_completion_required")
        if receipt.evidence_kinds != EVIDENCE_KINDS:
            raise CoordinatorDispatchError("coordinator_complete_evidence_required")
        try:
            consumed = self._coordinator_authority.is_consumed(receipt)
        except CoordinatorAuditError as exc:
            raise CoordinatorDispatchError(exc.code) from exc
        if consumed:
            raise CoordinatorDispatchError("coordinator_audit_receipt_replayed")


def _inspect(
    adapter: object,
    update: AgentActivityUpdate,
    receipt: ActivityAuditReceipt,
) -> EvidenceObservation | None:
    inspect_method = getattr(adapter, "inspect", None)
    if callable(inspect_method):
        value = inspect_method(update, receipt)
    elif callable(adapter):
        value = adapter(update, receipt)
    else:
        raise CoordinatorAuditError("coordinator_evidence_adapter_invalid")
    if value is None or isinstance(value, EvidenceObservation):
        return value
    if isinstance(value, Mapping):
        return EvidenceObservation(**value)  # type: ignore[arg-type]
    raise CoordinatorAuditError("coordinator_evidence_observation_invalid")


def _require_adapter(kind: EvidenceKind, adapter: object | None) -> object:
    if adapter is None:
        raise CoordinatorAuditError(f"coordinator_{kind}_adapter_required")
    if not callable(adapter) and not callable(getattr(adapter, "inspect", None)):
        raise CoordinatorAuditError(f"coordinator_{kind}_adapter_invalid")
    return adapter


def _claims_completion(update: AgentActivityUpdate) -> bool:
    narratives = [update.summary, update.current.summary]
    if update.previous is not None:
        narratives.append(update.previous.summary)
    return any(_COMPLETION_RE.search(narrative) is not None for narrative in narratives)


def _select_status(
    reasons: Mapping[AuditStatus, list[str]],
    verified_kinds: set[EvidenceKind],
) -> AuditStatus:
    for status in ("mismatch", "overlap", "stale", "blocked"):
        if reasons[status]:
            return status
    return "verified" if verified_kinds == set(EVIDENCE_KINDS) else "blocked"


def _validate_evidence_lineage(
    value: object,
) -> tuple[tuple[EvidenceKind, str], ...]:
    if not isinstance(value, tuple) or len(value) > len(EVIDENCE_KINDS):
        raise CoordinatorAuditError("coordinator_evidence_lineage_invalid")
    result: list[tuple[EvidenceKind, str]] = []
    seen: set[EvidenceKind] = set()
    for entry in value:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise CoordinatorAuditError("coordinator_evidence_lineage_invalid")
        kind, digest_value = entry
        if kind not in EVIDENCE_KINDS or kind in seen:
            raise CoordinatorAuditError("coordinator_evidence_lineage_invalid")
        _digest(digest_value, "coordinator_evidence_digest_invalid")
        seen.add(kind)
        result.append((kind, digest_value))
    canonical_order = tuple(kind for kind in EVIDENCE_KINDS if kind in seen)
    if tuple(kind for kind, _digest_value in result) != canonical_order:
        raise CoordinatorAuditError("coordinator_evidence_lineage_order_invalid")
    return tuple(result)


def _coordinator_authority_id(
    project: ProjectScope,
    coordination_session_id: str,
) -> str:
    if not isinstance(project, ProjectScope):
        raise CoordinatorAuditError("coordinator_project_invalid")
    coordination = _identifier(
        coordination_session_id,
        "coordinator_coordination_session_invalid",
    )
    material = f"{project.project_id}\0{coordination}".encode()
    suffix = hashlib.sha256(material).hexdigest()[:40]
    return f"coordinator-audit-authority:{suffix}"


def _coordinator_receipt_id_for(
    *,
    authority_id: str,
    activity_update_sha256: str,
    generation: int,
) -> str:
    authority = _identifier(
        authority_id,
        "coordinator_audit_authority_id_invalid",
    )
    if not authority.startswith("coordinator-audit-authority:"):
        raise CoordinatorAuditError("coordinator_audit_authority_id_invalid")
    update_digest = _digest(
        activity_update_sha256,
        "coordinator_activity_update_digest_invalid",
    )
    current_generation = _generation(
        generation,
        "coordinator_audit_generation_invalid",
    )
    material = f"{authority}\0{update_digest}\0{current_generation}".encode()
    suffix = hashlib.sha256(material).hexdigest()[:40]
    return f"coordinator-audit:{suffix}:{current_generation}"


def _coordinator_receipt_id(value: object) -> str:
    receipt_id = _identifier(value, "coordinator_audit_receipt_id_invalid")
    if not receipt_id.startswith("coordinator-audit:"):
        raise CoordinatorAuditError("coordinator_audit_receipt_id_invalid")
    return receipt_id


def _coordinator_head_key(
    *,
    project_id: object,
    coordination_session_id: object,
    activity_update_sha256: object,
) -> tuple[str, str, str]:
    try:
        project = ProjectScope(project_id).project_id
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError("coordinator_project_invalid") from exc
    return (
        project,
        _identifier(
            coordination_session_id,
            "coordinator_coordination_session_invalid",
        ),
        _digest(
            activity_update_sha256,
            "coordinator_activity_update_digest_invalid",
        ),
    )


def _validate_coordinator_record(record: object) -> CoordinatorAuditRecord:
    if not isinstance(record, CoordinatorAuditRecord):
        raise CoordinatorAuditError("coordinator_audit_repository_record_invalid")
    record.validate_integrity()
    return record


def _require_exact_coordinator_record(
    actual: CoordinatorAuditRecord,
    expected: CoordinatorAuditRecord,
) -> None:
    current = _validate_coordinator_record(actual)
    target = _validate_coordinator_record(expected)
    if (
        current.project != target.project
        or current.coordination_session_id != target.coordination_session_id
        or current.receipt.receipt_id != target.receipt.receipt_id
        or not hmac.compare_digest(
            current.receipt.content_sha256,
            target.receipt.content_sha256,
        )
        or current.recorded_at_utc != target.recorded_at_utc
    ):
        raise CoordinatorAuditError("coordinator_audit_repository_replay_conflict")


def _require_same_coordinator_receipt(
    actual: CoordinatorAuditRecord,
    expected: CoordinatorAuditRecord,
) -> None:
    current = _validate_coordinator_record(actual)
    target = _validate_coordinator_record(expected)
    if (
        current.project != target.project
        or current.coordination_session_id != target.coordination_session_id
        or current.receipt.receipt_id != target.receipt.receipt_id
        or not hmac.compare_digest(
            current.receipt.content_sha256,
            target.receipt.content_sha256,
        )
    ):
        raise CoordinatorAuditError("coordinator_audit_repository_replay_conflict")


def _copy_coordinator_record(record: CoordinatorAuditRecord) -> CoordinatorAuditRecord:
    source = _validate_coordinator_record(record)
    receipt = source.receipt
    copied_receipt = CoordinatorActivityAuditReceipt._rehydrate(
        receipt_id=receipt.receipt_id,
        authority_id=receipt.authority_id,
        audit_generation=receipt.audit_generation,
        activity_receipt=receipt.activity_receipt,
        status=receipt.status,
        evidence_lineage=receipt.evidence_lineage,
        reason_codes=receipt.reason_codes,
        completion_verified=receipt.completion_verified,
        _authority_token=_SERVER_COORDINATOR_AUTHORITY_TOKEN,
    )
    return CoordinatorAuditRecord(
        project=source.project,
        coordination_session_id=source.coordination_session_id,
        receipt=copied_receipt,
        recorded_at_utc=source.recorded_at_utc,
    )


def _generation(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_GENERATION:
        raise CoordinatorAuditError(code)
    return value


def _prior_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < _MAX_GENERATION:
        raise CoordinatorAuditError("coordinator_audit_expected_generation_invalid")
    return value


def _canonical_timestamp(value: object, code: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise CoordinatorAuditError(code)
    try:
        rendered = canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise CoordinatorAuditError(code) from exc
    if rendered != value:
        raise CoordinatorAuditError(code)
    return rendered


def _evidence_paths(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CoordinatorAuditError("coordinator_evidence_paths_invalid")
    if len(value) > _MAX_EVIDENCE_PATHS:
        raise CoordinatorAuditError("coordinator_evidence_paths_too_many")
    paths: list[str] = []
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or path != path.strip()
            or len(path.encode("utf-8")) > _MAX_EVIDENCE_PATH_BYTES
            or path.startswith(("/", "~"))
            or "\\" in path
            or any(segment in {"", ".", ".."} for segment in path.split("/"))
        ):
            raise CoordinatorAuditError("coordinator_evidence_path_invalid")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise CoordinatorAuditError("coordinator_evidence_paths_duplicate")
    return tuple(sorted(paths))


def _identifier(value: object, code: str, *, optional: bool = False) -> str:
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CoordinatorAuditError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CoordinatorAuditError(code)
    return value


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons and len(reasons) < _MAX_REASON_CODES:
        reasons.append(reason)


__all__ = [
    "AUDIT_STATUSES",
    "EVIDENCE_KINDS",
    "AgentActivityUpdateLike",
    "AuditStatus",
    "CoordinatorActivityAuditReceipt",
    "CoordinatorAuditAuthority",
    "CoordinatorAuditConsumption",
    "CoordinatorAuditError",
    "CoordinatorAuditRecord",
    "CoordinatorAuditRepository",
    "CoordinatorDispatchError",
    "CoordinatorSupervisor",
    "DependencyCallback",
    "DispatchCallback",
    "EvidenceAdapter",
    "EvidenceKind",
    "EvidenceObservation",
    "InMemoryCoordinatorAuditRepository",
    "open_server_coordinator_audit_authority",
]
