"""Bounded, secret-free progress updates for Agent collaboration.

An :class:`AgentActivityUpdate` is a public coordination statement.  It can
describe what one Agent previously did, is doing now, plans to do next, and
which blockers remain, but it is deliberately not a command, a role grant, a
lease, an acceptance decision, or canonical memory.

``role`` and ``role_assignment_sha256`` are audit references only.  A caller
cannot gain authority by choosing either value.  ``ActivityAuditAuthority``
is the small server seam that validates an update, issues a narrative-free
receipt, and remembers the exact receipt digest.  Constructing equivalent
JSON or copying a public receipt is never sufficient for server verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .canonical_time import canonical_text, server_now_text
from .contracts import EventCursor, ProjectScope

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime


ACTIVITY_SCOPE_SCHEMA = "collaboration-activity-scope/v1"
ACTIVITY_SLICE_SCHEMA = "collaboration-activity-slice/v1"
AGENT_ACTIVITY_UPDATE_SCHEMA = "collaboration-agent-activity-update/v2"
ACTIVITY_AUDIT_RECEIPT_SCHEMA = "collaboration-activity-audit-receipt/v1"
ACTIVITY_AUDIT_ISSUER = "pp-server-backend"
ACTIVITY_REDACTION_POLICY_REVISION = "activity-public-summary-redaction/v2"

_MAX_IDENTIFIER_BYTES = 256
_MAX_ROLE_BYTES = 64
_MAX_SUMMARY_BYTES = 2 * 1024
_MAX_SLICE_SCOPE_BYTES = 256
_MAX_SLICE_SUMMARY_BYTES = 2 * 1024
_MAX_PATH_BYTES = 512
_MAX_PATHS_PER_SLICE = 32
_MAX_BLOCKER_BYTES = 1024
_MAX_BLOCKERS = 16
_MAX_UPDATE_BYTES = 16 * 1024
_MAX_CURSOR = (1 << 63) - 1

_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SAFE_ROLE = re.compile(r"\A[a-z][a-z0-9_.-]{0,63}\Z")
_SHA256 = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"\A[A-Za-z]:")
_PORTABLE_PATH_SEGMENT = re.compile(r'\A[^\x00-\x1f<>:"\\|?*]+\Z')
_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk|rk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[-_ ]?key|client[-_ ]?secret|password|passwd|private[-_ ]?key|"
        r"access[-_ ]?token|refresh[-_ ]?token)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [^-\r\n]{0,48}PRIVATE KEY-----", re.IGNORECASE),
)
_PRIVATE_REASONING_PATTERNS = (
    re.compile(r"\bchain[ _-]?of[ _-]?thought\b", re.IGNORECASE),
    re.compile(r"\b(?:hidden|private)[ _-]?(?:reasoning|thoughts?)\b", re.IGNORECASE),
    re.compile(r"\binternal[ _-]?monologue\b", re.IGNORECASE),
    re.compile(r"\breasoning[ _-]?(?:trace|transcript)\b", re.IGNORECASE),
    re.compile(r"\b(?:raw|full)[ _-]?prompt\b", re.IGNORECASE),
    re.compile(r"\bprompt[ _-]?transcript\b", re.IGNORECASE),
    re.compile(r"\bscratchpad\b", re.IGNORECASE),
)

_SERVER_AUTHORITY_TOKEN = object()
_RECEIPT_ISSUE_TOKEN = object()


class ActivityContractError(ValueError):
    """Stable, non-sensitive rejection from the activity contract."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Compatibility-friendly names for callers that distinguish update and audit failures.
ActivityUpdateError = ActivityContractError
ActivityAuditError = ActivityContractError


class _ActivityJsonContract:
    """Canonical JSON and digest behavior shared by public activity values."""

    def to_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @property
    def content_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ActivityScope(_ActivityJsonContract):
    """Exact project/session/Agent scope for one public activity stream.

    This is an identity tuple for audit and cursor isolation.  It does not
    authenticate the Agent session and cannot be used as a bearer capability.
    """

    project: ProjectScope
    coordination_session_id: str
    agent_session_id: str
    agent_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectScope):
            raise ActivityContractError("activity_scope_project_invalid")
        object.__setattr__(
            self,
            "coordination_session_id",
            _identifier(
                self.coordination_session_id,
                "activity_scope_coordination_session_invalid",
            ),
        )
        object.__setattr__(
            self,
            "agent_session_id",
            _identifier(self.agent_session_id, "activity_scope_agent_session_invalid"),
        )
        object.__setattr__(
            self,
            "agent_id",
            _identifier(self.agent_id, "activity_scope_agent_id_invalid"),
        )

    def validate_integrity(self) -> None:
        """Revalidate an instance even if Python internals were used to mutate it."""

        if not isinstance(self.project, ProjectScope):
            raise ActivityContractError("activity_scope_project_invalid")
        if (
            _identifier(
                self.coordination_session_id,
                "activity_scope_coordination_session_invalid",
            )
            != self.coordination_session_id
            or _identifier(
                self.agent_session_id,
                "activity_scope_agent_session_invalid",
            )
            != self.agent_session_id
            or _identifier(self.agent_id, "activity_scope_agent_id_invalid") != self.agent_id
        ):
            raise ActivityContractError("activity_scope_tampered")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVITY_SCOPE_SCHEMA,
            "project_id": self.project.project_id,
            "coordination_session_id": self.coordination_session_id,
            "agent_session_id": self.agent_session_id,
            "agent_id": self.agent_id,
            "authority_effect": "none",
            "canonical_memory_effect": "none",
        }

    @property
    def scope_sha256(self) -> str:
        return self.content_sha256


@dataclass(frozen=True, slots=True)
class ActivitySlice(_ActivityJsonContract):
    """One bounded public activity claim with canonical repository paths.

    The containing field supplies temporal meaning: ``previous`` is reported
    work, ``current`` is the active edit set, and ``next`` is planned work.
    Paths are exact repository-relative POSIX paths, not globs, directories,
    absolute paths, or caller-defined JSON.  This keeps Git-diff evidence
    adapters deterministic without giving the narrative any authority.
    """

    scope: str
    paths: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        scope = _public_line(
            self.scope,
            "activity_slice_scope_invalid",
            max_bytes=_MAX_SLICE_SCOPE_BYTES,
        )
        paths = _activity_paths(self.paths)
        summary = _public_text(
            self.summary,
            "activity_slice_summary_invalid",
            max_bytes=_MAX_SLICE_SUMMARY_BYTES,
            required=True,
        )
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "paths", paths)
        object.__setattr__(self, "summary", summary)

    def validate_integrity(self) -> None:
        """Revalidate canonical slice data after crossing a trust seam."""

        if (
            _public_line(
                self.scope,
                "activity_slice_scope_invalid",
                max_bytes=_MAX_SLICE_SCOPE_BYTES,
            )
            != self.scope
        ):
            raise ActivityContractError("activity_slice_tampered")
        if not isinstance(self.paths, tuple) or _activity_paths(self.paths) != self.paths:
            raise ActivityContractError("activity_slice_paths_tampered")
        if (
            _public_text(
                self.summary,
                "activity_slice_summary_invalid",
                max_bytes=_MAX_SLICE_SUMMARY_BYTES,
                required=True,
            )
            != self.summary
        ):
            raise ActivityContractError("activity_slice_tampered")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVITY_SLICE_SCHEMA,
            "scope": self.scope,
            "paths": list(self.paths),
            "summary": self.summary,
            "authority_effect": "none",
            "tool_policy_effect": "none",
            "canonical_memory_effect": "none",
        }

    @property
    def slice_sha256(self) -> str:
        return self.content_sha256


@dataclass(frozen=True, slots=True)
class AgentActivityUpdate(_ActivityJsonContract):
    """One bounded, public progress statement from an Agent.

    ``previous``/``current``/``next`` are typed :class:`ActivitySlice` values,
    never arbitrary mappings, raw prompts, or private reasoning.  The v2 seam
    intentionally rejects v1 strings; callers must select exact public paths
    before coordinator overlap evidence can rely on the report. ``blockers``
    preserves caller order because it is presentation and audit data rather
    than a ranked authority source.
    """

    scope: ActivityScope
    role: str
    summary: str
    previous: ActivitySlice | None
    current: ActivitySlice
    next: ActivitySlice | None
    blockers: tuple[str, ...] = ()
    work_item_id: str = ""
    role_assignment_sha256: str = ""
    cursor: int | EventCursor = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ActivityScope):
            raise ActivityContractError("activity_scope_invalid")
        self.scope.validate_integrity()
        role = _role(self.role)
        summary = _public_text(
            self.summary,
            "activity_summary_invalid",
            max_bytes=_MAX_SUMMARY_BYTES,
            required=True,
        )
        previous = _optional_activity_slice(self.previous, "activity_previous_slice_invalid")
        current = _activity_slice(self.current, "activity_current_slice_invalid")
        next_value = _optional_activity_slice(self.next, "activity_next_slice_invalid")
        blockers = _blockers(self.blockers)
        work_item_id = _optional_identifier(
            self.work_item_id,
            "activity_work_item_invalid",
        )
        role_assignment_sha256 = _optional_digest(
            self.role_assignment_sha256,
            "activity_role_assignment_digest_invalid",
        )
        if role_assignment_sha256 and not work_item_id:
            raise ActivityContractError("activity_role_assignment_work_required")
        cursor = _cursor(self.cursor, self.scope)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "previous", previous)
        object.__setattr__(self, "current", current)
        object.__setattr__(self, "next", next_value)
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "work_item_id", work_item_id)
        object.__setattr__(self, "role_assignment_sha256", role_assignment_sha256)
        object.__setattr__(self, "cursor", cursor)
        _require_size(self.to_dict(), "activity_update_too_large", _MAX_UPDATE_BYTES)

    def validate_integrity(self) -> None:
        """Validate every field and the total projection without trusting construction."""

        if not isinstance(self.scope, ActivityScope):
            raise ActivityContractError("activity_scope_invalid")
        self.scope.validate_integrity()
        if _role(self.role) != self.role:
            raise ActivityContractError("activity_role_tampered")
        if (
            _public_text(
                self.summary,
                "activity_summary_invalid",
                max_bytes=_MAX_SUMMARY_BYTES,
                required=True,
            )
            != self.summary
        ):
            raise ActivityContractError("activity_update_tampered")
        if self.previous is not None:
            if not isinstance(self.previous, ActivitySlice):
                raise ActivityContractError("activity_previous_slice_invalid")
            self.previous.validate_integrity()
        if not isinstance(self.current, ActivitySlice):
            raise ActivityContractError("activity_current_slice_invalid")
        self.current.validate_integrity()
        if self.next is not None:
            if not isinstance(self.next, ActivitySlice):
                raise ActivityContractError("activity_next_slice_invalid")
            self.next.validate_integrity()
        if not isinstance(self.blockers, tuple) or _blockers(self.blockers) != self.blockers:
            raise ActivityContractError("activity_blockers_tampered")
        if (
            _optional_identifier(self.work_item_id, "activity_work_item_invalid")
            != self.work_item_id
        ):
            raise ActivityContractError("activity_work_item_tampered")
        if (
            _optional_digest(
                self.role_assignment_sha256,
                "activity_role_assignment_digest_invalid",
            )
            != self.role_assignment_sha256
        ):
            raise ActivityContractError("activity_role_assignment_tampered")
        if self.role_assignment_sha256 and not self.work_item_id:
            raise ActivityContractError("activity_role_assignment_work_required")
        if _cursor(self.cursor, self.scope) != self.cursor:
            raise ActivityContractError("activity_cursor_tampered")
        _require_size(self.to_dict(), "activity_update_too_large", _MAX_UPDATE_BYTES)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": AGENT_ACTIVITY_UPDATE_SCHEMA,
            "scope": self.scope.to_dict(),
            "role": self.role,
            "summary": self.summary,
            "previous": None if self.previous is None else self.previous.to_dict(),
            "current": self.current.to_dict(),
            "next": None if self.next is None else self.next.to_dict(),
            "blockers": list(self.blockers),
            "work_item_id": self.work_item_id,
            "role_assignment_sha256": self.role_assignment_sha256,
            "cursor": self.cursor,
            "redaction_policy_revision": ACTIVITY_REDACTION_POLICY_REVISION,
            "role_effect": "audit-only",
            "role_assignment_effect": "reference-only",
            "authority_effect": "none",
            "tool_policy_effect": "none",
            "canonical_memory_effect": "none",
        }

    @property
    def update_sha256(self) -> str:
        return self.content_sha256

    @property
    def project(self) -> ProjectScope:
        return self.scope.project

    @property
    def coordination_session_id(self) -> str:
        return self.scope.coordination_session_id

    @property
    def agent_session_id(self) -> str:
        return self.scope.agent_session_id

    @property
    def agent_id(self) -> str:
        return self.scope.agent_id

    @property
    def evidence_paths(self) -> tuple[str, ...]:
        """Canonical paths whose reported edits require independent evidence."""

        previous_paths = () if self.previous is None else self.previous.paths
        return tuple(sorted({*previous_paths, *self.current.paths}))

    @property
    def planned_paths(self) -> tuple[str, ...]:
        """Canonical paths announced for future work, without reserving them."""

        return () if self.next is None else self.next.paths


@dataclass(frozen=True, slots=True, init=False)
class ActivityAuditReceipt(_ActivityJsonContract):
    """Server-validated, narrative-free lineage for one activity update.

    The public receipt remains evidence only.  It grants no Agent role, tool
    policy, work lease, acceptance authority, or canonical-memory status.
    Consumers that care about server issuance must call the exact issuing
    :class:`ActivityAuditAuthority`; JSON shape or a matching digest alone is
    insufficient.
    """

    receipt_id: str
    scope: ActivityScope
    role: str
    work_item_id: str
    role_assignment_sha256: str
    cursor: int
    activity_update_sha256: str
    activity_scope_sha256: str
    validated_at_utc: str

    def __init__(self, *_: object, **__: object) -> None:
        raise ActivityContractError("activity_audit_receipt_factory_required")

    @classmethod
    def _issue(
        cls,
        *,
        receipt_id: str,
        update: AgentActivityUpdate,
        validated_at_utc: str,
        _token: object,
    ) -> ActivityAuditReceipt:
        if _token is not _RECEIPT_ISSUE_TOKEN:
            raise ActivityContractError("activity_audit_receipt_server_required")
        if not isinstance(update, AgentActivityUpdate):
            raise ActivityContractError("activity_update_invalid")
        update.validate_integrity()
        instance = object.__new__(cls)
        values: dict[str, object] = {
            "receipt_id": receipt_id,
            "scope": update.scope,
            "role": update.role,
            "work_item_id": update.work_item_id,
            "role_assignment_sha256": update.role_assignment_sha256,
            "cursor": update.cursor,
            "activity_update_sha256": update.content_sha256,
            "activity_scope_sha256": update.scope.content_sha256,
            "validated_at_utc": validated_at_utc,
        }
        for field_name, value in values.items():
            object.__setattr__(instance, field_name, value)
        instance.validate_integrity(update)
        return instance

    @classmethod
    def _rehydrate(
        cls,
        *,
        receipt_id: str,
        update: AgentActivityUpdate,
        validated_at_utc: str,
        _authority_token: object | None = None,
    ) -> ActivityAuditReceipt:
        """Rebuild one exact receipt only for a server-owned repository adapter."""

        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise ActivityContractError("activity_audit_rehydrate_authority_required")
        return cls._issue(
            receipt_id=receipt_id,
            update=update,
            validated_at_utc=validated_at_utc,
            _token=_RECEIPT_ISSUE_TOKEN,
        )

    def validate_integrity(self, update: AgentActivityUpdate | None = None) -> None:
        """Validate receipt structure and, when supplied, the exact source update."""

        receipt_id = _identifier(self.receipt_id, "activity_audit_receipt_id_invalid")
        if not receipt_id.startswith("activity-audit:"):
            raise ActivityContractError("activity_audit_receipt_id_invalid")
        if not isinstance(self.scope, ActivityScope):
            raise ActivityContractError("activity_audit_scope_invalid")
        self.scope.validate_integrity()
        if _role(self.role) != self.role:
            raise ActivityContractError("activity_audit_role_invalid")
        if (
            _optional_identifier(self.work_item_id, "activity_audit_work_item_invalid")
            != self.work_item_id
        ):
            raise ActivityContractError("activity_audit_work_item_invalid")
        if (
            _optional_digest(
                self.role_assignment_sha256,
                "activity_audit_role_assignment_digest_invalid",
            )
            != self.role_assignment_sha256
        ):
            raise ActivityContractError("activity_audit_role_assignment_digest_invalid")
        if self.role_assignment_sha256 and not self.work_item_id:
            raise ActivityContractError("activity_audit_role_assignment_work_required")
        if _cursor(self.cursor, self.scope) != self.cursor:
            raise ActivityContractError("activity_audit_cursor_invalid")
        _digest(self.activity_update_sha256, "activity_audit_update_digest_invalid")
        _digest(self.activity_scope_sha256, "activity_audit_scope_digest_invalid")
        if not hmac.compare_digest(self.activity_scope_sha256, self.scope.content_sha256):
            raise ActivityContractError("activity_audit_scope_digest_mismatch")
        if _canonical_timestamp(self.validated_at_utc) != self.validated_at_utc:
            raise ActivityContractError("activity_audit_validated_at_invalid")
        if update is not None:
            if not isinstance(update, AgentActivityUpdate):
                raise ActivityContractError("activity_update_invalid")
            update.validate_integrity()
            if not hmac.compare_digest(
                self.activity_update_sha256,
                update.content_sha256,
            ):
                raise ActivityContractError("activity_audit_update_mismatch")
            if (
                self.scope != update.scope
                or self.role != update.role
                or self.work_item_id != update.work_item_id
                or self.role_assignment_sha256 != update.role_assignment_sha256
                or self.cursor != update.cursor
            ):
                raise ActivityContractError("activity_audit_update_scope_mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVITY_AUDIT_RECEIPT_SCHEMA,
            "issuer": ACTIVITY_AUDIT_ISSUER,
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "role": self.role,
            "work_item_id": self.work_item_id,
            "role_assignment_sha256": self.role_assignment_sha256,
            "cursor": self.cursor,
            "activity_update_sha256": self.activity_update_sha256,
            "activity_scope_sha256": self.activity_scope_sha256,
            "validated_at_utc": self.validated_at_utc,
            "redaction": "narrative-omitted",
            "role_effect": "audit-only",
            "role_assignment_effect": "reference-only",
            "authority_effect": "none",
            "tool_policy_effect": "none",
            "canonical_memory_effect": "none",
            "verification": "server-ledger-required",
        }

    @property
    def receipt_sha256(self) -> str:
        return self.content_sha256

    @property
    def activity_sha256(self) -> str:
        return self.activity_update_sha256

    @property
    def update_sha256(self) -> str:
        return self.activity_update_sha256


@dataclass(frozen=True, slots=True)
class ActivityAuditRecord:
    """One exact update/receipt pair resolved from a server-owned ledger."""

    update: AgentActivityUpdate
    receipt: ActivityAuditReceipt
    recorded_at_utc: str

    def validate_integrity(self) -> None:
        if not isinstance(self.update, AgentActivityUpdate):
            raise ActivityContractError("activity_update_invalid")
        if not isinstance(self.receipt, ActivityAuditReceipt):
            raise ActivityContractError("activity_audit_receipt_invalid")
        self.update.validate_integrity()
        self.receipt.validate_integrity(self.update)
        if _canonical_timestamp(self.recorded_at_utc) != self.recorded_at_utc:
            raise ActivityContractError("activity_audit_recorded_at_invalid")


@runtime_checkable
class ActivityAuditRepository(Protocol):
    """Append-only canonical ledger seam used by the activity authority."""

    def load_by_update_digest(
        self,
        update_sha256: str,
    ) -> ActivityAuditRecord | None: ...

    def load_by_receipt_id(self, receipt_id: str) -> ActivityAuditRecord | None: ...

    def load_by_stream_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
        cursor: int,
    ) -> ActivityAuditRecord | None: ...

    def highest_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
    ) -> int | None: ...

    def append_exact(
        self,
        update: AgentActivityUpdate,
        receipt: ActivityAuditReceipt,
        *,
        recorded_at_utc: str,
        _authority_token: object | None = None,
    ) -> ActivityAuditRecord: ...


class InMemoryActivityAuditRepository:
    """Process-local adapter for focused tests and isolated composition."""

    def __init__(self) -> None:
        self._by_update: dict[str, ActivityAuditRecord] = {}
        self._by_receipt_id: dict[str, ActivityAuditRecord] = {}
        self._by_stream_cursor: dict[tuple[str, str, str, str, int], ActivityAuditRecord] = {}
        self._highest_by_stream: dict[tuple[str, str, str, str], int] = {}

    def load_by_update_digest(
        self,
        update_sha256: str,
    ) -> ActivityAuditRecord | None:
        return self._by_update.get(_digest(update_sha256, "activity_audit_update_digest_invalid"))

    def load_by_receipt_id(self, receipt_id: str) -> ActivityAuditRecord | None:
        normalized = _identifier(receipt_id, "activity_audit_receipt_id_invalid")
        if not normalized.startswith("activity-audit:"):
            raise ActivityContractError("activity_audit_receipt_id_invalid")
        return self._by_receipt_id.get(normalized)

    def load_by_stream_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
        cursor: int,
    ) -> ActivityAuditRecord | None:
        return self._by_stream_cursor.get(
            _activity_stream_cursor_key(
                project_id=project_id,
                coordination_session_id=coordination_session_id,
                agent_session_id=agent_session_id,
                work_item_id=work_item_id,
                cursor=cursor,
            )
        )

    def highest_cursor(
        self,
        *,
        project_id: str,
        coordination_session_id: str,
        agent_session_id: str,
        work_item_id: str,
    ) -> int | None:
        return self._highest_by_stream.get(
            _activity_stream_key(
                project_id=project_id,
                coordination_session_id=coordination_session_id,
                agent_session_id=agent_session_id,
                work_item_id=work_item_id,
            )
        )

    def append_exact(
        self,
        update: AgentActivityUpdate,
        receipt: ActivityAuditReceipt,
        *,
        recorded_at_utc: str,
        _authority_token: object | None = None,
    ) -> ActivityAuditRecord:
        if _authority_token is not _SERVER_AUTHORITY_TOKEN:
            raise ActivityContractError("activity_audit_repository_write_authority_required")
        if not isinstance(update, AgentActivityUpdate):
            raise ActivityContractError("activity_update_invalid")
        if not isinstance(receipt, ActivityAuditReceipt):
            raise ActivityContractError("activity_audit_receipt_invalid")
        update.validate_integrity()
        receipt.validate_integrity(update)
        recorded = _canonical_timestamp(recorded_at_utc)
        existing = self._by_update.get(update.update_sha256)
        if existing is not None:
            _require_exact_activity_record(existing, update=update, receipt=receipt)
            return existing
        collision = self._by_receipt_id.get(receipt.receipt_id)
        if collision is not None:
            raise ActivityContractError("activity_audit_receipt_id_conflict")
        cursor_key = _activity_stream_cursor_key(
            project_id=update.project.project_id,
            coordination_session_id=update.coordination_session_id,
            agent_session_id=update.agent_session_id,
            work_item_id=update.work_item_id,
            cursor=update.cursor,
        )
        at_cursor = self._by_stream_cursor.get(cursor_key)
        if at_cursor is not None:
            raise ActivityContractError("activity_audit_cursor_conflict")
        stream = cursor_key[:-1]
        highest = self._highest_by_stream.get(stream)
        if highest is not None and update.cursor < highest:
            raise ActivityContractError("activity_audit_cursor_regression")
        record = ActivityAuditRecord(
            update=update,
            receipt=receipt,
            recorded_at_utc=recorded,
        )
        record.validate_integrity()
        self._by_update[update.update_sha256] = record
        self._by_receipt_id[receipt.receipt_id] = record
        self._by_stream_cursor[cursor_key] = record
        self._highest_by_stream[stream] = max(highest or 0, update.cursor)
        return record


class ActivityAuditAuthority:
    """Server-only issuer and exact-ledger verifier for activity audit receipts."""

    def __init__(
        self,
        *,
        repository: ActivityAuditRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        _server_token: object | None = None,
    ) -> None:
        if _server_token is not _SERVER_AUTHORITY_TOKEN:
            raise ActivityContractError("activity_audit_server_authority_required")
        if not isinstance(repository, ActivityAuditRepository):
            raise ActivityContractError("activity_audit_repository_invalid")
        if clock is not None and not callable(clock):
            raise ActivityContractError("activity_audit_clock_invalid")
        self._repository = repository
        self._clock = clock

    def issue(
        self,
        update: AgentActivityUpdate,
        *,
        receipt_id: str | None = None,
    ) -> ActivityAuditReceipt:
        """Validate one update and issue an idempotent, narrative-free receipt."""

        if not isinstance(update, AgentActivityUpdate):
            raise ActivityContractError("activity_update_invalid")
        update.validate_integrity()
        update_sha256 = update.content_sha256
        existing = self._repository.load_by_update_digest(update_sha256)
        if existing is not None:
            existing.validate_integrity()
            if receipt_id is not None and receipt_id != existing.receipt.receipt_id:
                raise ActivityContractError("activity_audit_replay_ambiguous")
            return existing.receipt

        prior = self._repository.load_by_stream_cursor(
            project_id=update.project.project_id,
            coordination_session_id=update.coordination_session_id,
            agent_session_id=update.agent_session_id,
            work_item_id=update.work_item_id,
            cursor=update.cursor,
        )
        if prior is not None and not hmac.compare_digest(
            prior.update.update_sha256,
            update_sha256,
        ):
            raise ActivityContractError("activity_audit_cursor_conflict")
        highest = self._repository.highest_cursor(
            project_id=update.project.project_id,
            coordination_session_id=update.coordination_session_id,
            agent_session_id=update.agent_session_id,
            work_item_id=update.work_item_id,
        )
        if highest is not None and update.cursor < highest:
            raise ActivityContractError("activity_audit_cursor_regression")

        generated_id = f"activity-audit:{update_sha256.removeprefix('sha256:')[:40]}"
        selected_id = (
            generated_id
            if receipt_id is None
            else _identifier(
                receipt_id,
                "activity_audit_receipt_id_invalid",
            )
        )
        if not selected_id.startswith("activity-audit:"):
            raise ActivityContractError("activity_audit_receipt_id_invalid")
        collision = self._repository.load_by_receipt_id(selected_id)
        if collision is not None:
            raise ActivityContractError("activity_audit_receipt_id_conflict")
        try:
            validated_at_utc = server_now_text(self._clock)
        except (TypeError, ValueError) as exc:
            raise ActivityContractError("activity_audit_clock_invalid") from exc
        receipt = ActivityAuditReceipt._issue(
            receipt_id=selected_id,
            update=update,
            validated_at_utc=validated_at_utc,
            _token=_RECEIPT_ISSUE_TOKEN,
        )
        stored = self._repository.append_exact(
            update,
            receipt,
            recorded_at_utc=validated_at_utc,
            _authority_token=_SERVER_AUTHORITY_TOKEN,
        )
        _require_exact_activity_record(stored, update=update, receipt=receipt)
        return stored.receipt

    def verify_issued(
        self,
        receipt: ActivityAuditReceipt,
        *,
        update: AgentActivityUpdate | None = None,
    ) -> ActivityAuditReceipt:
        """Return the exact canonical receipt or reject forged/tampered input."""

        if not isinstance(receipt, ActivityAuditReceipt):
            raise ActivityContractError("activity_audit_receipt_invalid")
        receipt.validate_integrity(update)
        record = self._repository.load_by_receipt_id(receipt.receipt_id)
        if record is None:
            raise ActivityContractError("activity_audit_receipt_not_server_issued")
        record.validate_integrity()
        issued = record.receipt
        if not hmac.compare_digest(issued.content_sha256, receipt.content_sha256):
            raise ActivityContractError("activity_audit_receipt_tampered")
        if not hmac.compare_digest(
            issued.activity_update_sha256,
            receipt.activity_update_sha256,
        ):
            raise ActivityContractError("activity_audit_receipt_tampered")
        return issued

    def verify(
        self,
        receipt: ActivityAuditReceipt,
        *,
        update: AgentActivityUpdate | None = None,
    ) -> bool:
        """Boolean convenience wrapper that never turns a receipt into authority."""

        try:
            self.verify_issued(receipt, update=update)
        except ActivityContractError:
            return False
        return True


def open_server_activity_audit_authority(
    *,
    repository: ActivityAuditRepository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ActivityAuditAuthority:
    """Open a server issuer over the selected canonical ledger adapter."""

    selected = repository or InMemoryActivityAuditRepository()
    return ActivityAuditAuthority(
        repository=selected,
        clock=clock,
        _server_token=_SERVER_AUTHORITY_TOKEN,
    )


def _require_exact_activity_record(
    record: ActivityAuditRecord,
    *,
    update: AgentActivityUpdate,
    receipt: ActivityAuditReceipt,
) -> None:
    if not isinstance(record, ActivityAuditRecord):
        raise ActivityContractError("activity_audit_repository_record_invalid")
    record.validate_integrity()
    if not hmac.compare_digest(record.update.update_sha256, update.update_sha256):
        raise ActivityContractError("activity_audit_repository_lookup_mismatch")
    if not hmac.compare_digest(record.receipt.receipt_sha256, receipt.receipt_sha256):
        raise ActivityContractError("activity_audit_repository_replay_conflict")
    if record.receipt.receipt_id != receipt.receipt_id:
        raise ActivityContractError("activity_audit_replay_ambiguous")


def _activity_stream_key(
    *,
    project_id: object,
    coordination_session_id: object,
    agent_session_id: object,
    work_item_id: object,
) -> tuple[str, str, str, str]:
    try:
        project = ProjectScope(project_id).project_id
    except ValueError as exc:
        raise ActivityContractError("activity_project_invalid") from exc
    return (
        project,
        _identifier(
            coordination_session_id,
            "activity_scope_coordination_session_invalid",
        ),
        _identifier(agent_session_id, "activity_scope_agent_session_invalid"),
        _optional_identifier(work_item_id, "activity_work_item_invalid"),
    )


def _activity_stream_cursor_key(
    *,
    project_id: object,
    coordination_session_id: object,
    agent_session_id: object,
    work_item_id: object,
    cursor: object,
) -> tuple[str, str, str, str, int]:
    stream = _activity_stream_key(
        project_id=project_id,
        coordination_session_id=coordination_session_id,
        agent_session_id=agent_session_id,
        work_item_id=work_item_id,
    )
    if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= _MAX_CURSOR:
        raise ActivityContractError("activity_cursor_invalid")
    return (*stream, cursor)


def _identifier(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
        or _SAFE_IDENTIFIER.fullmatch(value) is None
    ):
        raise ActivityContractError(code)
    return value


def _optional_identifier(value: object, code: str) -> str:
    if value is None or value == "":
        return ""
    return _identifier(value, code)


def _role(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value.encode("utf-8")) > _MAX_ROLE_BYTES
        or _SAFE_ROLE.fullmatch(value) is None
    ):
        raise ActivityContractError("activity_role_invalid")
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ActivityContractError(code)
    return value


def _optional_digest(value: object, code: str) -> str:
    if value is None or value == "":
        return ""
    return _digest(value, code)


def _public_text(
    value: object,
    code: str,
    *,
    max_bytes: int,
    required: bool,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ActivityContractError(code)
    if required and not value:
        raise ActivityContractError(code)
    if len(value.encode("utf-8")) > max_bytes:
        raise ActivityContractError(code)
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ActivityContractError(code)
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ActivityContractError("activity_secret_forbidden")
    if any(pattern.search(value) for pattern in _PRIVATE_REASONING_PATTERNS):
        raise ActivityContractError("activity_private_reasoning_forbidden")
    return value


def _public_line(value: object, code: str, *, max_bytes: int) -> str:
    line = _public_text(value, code, max_bytes=max_bytes, required=True)
    if "\n" in line or "\t" in line:
        raise ActivityContractError(code)
    return line


def _activity_slice(value: object, code: str) -> ActivitySlice:
    if not isinstance(value, ActivitySlice):
        raise ActivityContractError(code)
    value.validate_integrity()
    return value


def _optional_activity_slice(value: object, code: str) -> ActivitySlice | None:
    if value is None:
        return None
    return _activity_slice(value, code)


def _activity_paths(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ActivityContractError("activity_slice_paths_invalid")
    if not value:
        raise ActivityContractError("activity_slice_paths_empty")
    if len(value) > _MAX_PATHS_PER_SLICE:
        raise ActivityContractError("activity_slice_paths_too_many")
    paths = tuple(_repository_path(path) for path in value)
    if len(paths) != len(set(paths)):
        raise ActivityContractError("activity_slice_paths_duplicate")
    return tuple(sorted(paths))


def _repository_path(value: object) -> str:
    path = _public_text(
        value,
        "activity_slice_path_invalid",
        max_bytes=_MAX_PATH_BYTES,
        required=True,
    )
    if path.startswith(("/", "~")) or _WINDOWS_DRIVE.match(path) is not None or "\\" in path:
        raise ActivityContractError("activity_slice_path_invalid")
    segments = path.split("/")
    if any(
        segment in {"", ".", ".."}
        or segment != segment.strip()
        or segment.endswith(".")
        or _PORTABLE_PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise ActivityContractError("activity_slice_path_invalid")
    return path


def _blockers(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ActivityContractError("activity_blockers_invalid")
    if len(value) > _MAX_BLOCKERS:
        raise ActivityContractError("activity_blockers_too_many")
    blockers = tuple(
        _public_text(
            item,
            "activity_blocker_invalid",
            max_bytes=_MAX_BLOCKER_BYTES,
            required=True,
        )
        for item in value
    )
    if len(blockers) != len(set(blockers)):
        raise ActivityContractError("activity_blockers_duplicate")
    return blockers


def _cursor(value: object, scope: ActivityScope) -> int:
    if isinstance(value, EventCursor):
        if (
            value.project != scope.project
            or value.coordination_session_id != scope.coordination_session_id
        ):
            raise ActivityContractError("activity_cursor_scope_mismatch")
        value = value.sequence
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_CURSOR:
        raise ActivityContractError("activity_cursor_invalid")
    return value


def _canonical_timestamp(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ActivityContractError("activity_audit_validated_at_invalid")
    try:
        canonical = canonical_text(value)
    except (TypeError, ValueError) as exc:
        raise ActivityContractError("activity_audit_validated_at_invalid") from exc
    if not isinstance(canonical, str):
        raise ActivityContractError("activity_audit_validated_at_invalid")
    return canonical


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
        raise ActivityContractError("activity_json_invalid") from exc


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_size(value: object, code: str, max_bytes: int) -> None:
    if len(_canonical_json(value).encode("utf-8")) > max_bytes:
        raise ActivityContractError(code)
