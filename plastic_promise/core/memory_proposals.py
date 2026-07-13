"""Bounded, approval-gated user-memory proposals backed by canonical SQLite."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from plastic_promise.core.synthesis import VISIBILITY_RANK, visibility_allows

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


UTC = timezone.utc
PROPOSAL_CATEGORIES = frozenset({"fact", "preference", "decision"})
PROPOSAL_STATUSES = frozenset({"pending", "adopted", "rejected", "expired"})
MAX_CANDIDATE_LENGTH = 500
MAX_CANDIDATES = 5
MAX_METADATA_BYTES = 4096
DEFAULT_EXPIRY_DAYS = 7
SECRET_PATTERN_ENV = "PP_MEMORY_PROPOSAL_SECRET_PATTERNS"
REJECTION_REASON_CODES = frozenset(
    {
        "duplicate",
        "incorrect",
        "not_durable",
        "not_reusable",
        "outdated",
        "policy_rejected",
        "rejected",
        "reviewer_rejected",
    }
)
TRUSTED_INTERNAL_MEMORY_ROUTES = frozenset(
    {
        "audit",
        "audit_run",
        "auto_context_inject",
        "commercial_audit_export",
        "memory_sync_files",
        "review_run",
        "session-init",
        "session_init",
        "skill_auto_track",
        "skill_session_complete",
        "skill_session_start",
        "sp-stage",
        "sp_stage",
        "step-closure",
        "step_closure",
        "system",
    }
)
FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "assistant",
        "assistant_text",
        "content",
        "conversation",
        "messages",
        "prompt",
        "raw_content",
        "transcript",
    }
)

_TRANSCRIPT_ROLE_RE = re.compile(
    r"(?:^|\s)(?:assistant|system|developer|tool|user)\s*:",
    re.IGNORECASE,
)
_COMMON_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\b(?:password|passwd|pwd|secret)\s*[:=]\s*[\"']?[^\s\"',;]{6,}",
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
        r"[^/\s:@]+:[^@\s/]+@",
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
    )
)
_RUNTIME_MEMORY_ROUTE: ContextVar[str] = ContextVar(
    "plastic_promise_runtime_memory_route",
    default="",
)

_ROW_COLUMNS = (
    "proposal_id",
    "project_id",
    "visibility",
    "origin_visibility",
    "content",
    "content_hash",
    "category",
    "origin_role",
    "origin_turn_hash",
    "origin_call_id",
    "status",
    "approval_actor",
    "approval_call_id",
    "promoted_memory_id",
    "rejection_reason",
    "metadata_json",
    "expires_at",
    "redacted_at",
    "created_at",
    "updated_at",
)


class ProposalPolicyError(RuntimeError):
    """A stable, content-free proposal policy failure."""


@dataclass(frozen=True)
class ProposalCandidate:
    content: str
    category: str
    project_id: str
    visibility: str
    origin_role: str
    origin_turn_hash: str
    origin_call_id: str = ""
    origin_visibility: str = "project"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProposalClassification:
    decision: str
    candidates: tuple[ProposalCandidate, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"propose", "reject"}:
            raise ValueError("invalid_proposal_decision")
        if self.decision == "propose" and (not self.candidates or self.reason_codes):
            raise ValueError("invalid_proposal_classification")
        if self.decision == "reject" and (self.candidates or not self.reason_codes):
            raise ValueError("invalid_proposal_classification")


@dataclass(frozen=True)
class PromotionResult:
    proposal_id: str
    status: str
    memory_id: str
    created: bool
    index_job_id: str = ""


def proposal_mode() -> str:
    mode = os.environ.get("PP_MEMORY_PROPOSALS", "off").strip().casefold()
    if mode not in {"off", "shadow", "on"}:
        raise ProposalPolicyError("unknown_proposal_mode")
    return mode


def set_runtime_memory_route(route: object) -> Token[str]:
    """Set server-owned tool provenance for nested memory writes."""
    return _RUNTIME_MEMORY_ROUTE.set(str(route or "").strip())


def reset_runtime_memory_route(token: Token[str]) -> None:
    _RUNTIME_MEMORY_ROUTE.reset(token)


@contextmanager
def trusted_memory_origin(route: object):
    token = set_runtime_memory_route(route)
    try:
        yield
    finally:
        reset_runtime_memory_route(token)


def has_trusted_internal_origin(args: Mapping[str, Any] | None = None) -> bool:
    """Resolve internal origin from runtime provenance, never caller categories."""
    values = args if isinstance(args, Mapping) else {}
    source = str(values.get("source") or "").strip().casefold()
    origin_kind = str(values.get("origin_kind") or "").strip().casefold()
    origin_uri = str(values.get("origin_uri") or "").strip().casefold()
    if source in {"user", "conversation", "session_hook", "session-hook"}:
        return False
    if origin_kind in {"conversation", "session_hook", "session-hook", "user_turn"}:
        return False
    if origin_uri.startswith(("conversation://", "session-hook://", "session_hook://")):
        return False
    return _RUNTIME_MEMORY_ROUTE.get() in TRUSTED_INTERNAL_MEMORY_ROUTES


def contains_secret(content: object) -> bool:
    text = str(content or "")
    if any(pattern.search(text) for pattern in _COMMON_SECRET_PATTERNS):
        return True

    for configured in _configured_secret_patterns():
        try:
            if re.search(configured, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            return True
    return False


def _create_memory_proposal_table(conn: Any, table_name: str) -> None:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
        raise ProposalPolicyError("invalid_proposal_table_name")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            proposal_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'project',
            origin_visibility TEXT NOT NULL DEFAULT 'project',
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            category TEXT NOT NULL,
            origin_role TEXT NOT NULL,
            origin_turn_hash TEXT NOT NULL,
            origin_call_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            approval_actor TEXT NOT NULL DEFAULT '',
            approval_call_id TEXT NOT NULL DEFAULT '',
            promoted_memory_id TEXT NOT NULL DEFAULT '',
            rejection_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            expires_at TEXT NOT NULL,
            redacted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, origin_turn_hash, content_hash),
            CHECK(origin_role = 'user'),
            CHECK(category IN ('fact', 'preference', 'decision')),
            CHECK(visibility IN ('private', 'project', 'shared', 'global')),
            CHECK(origin_visibility IN ('private', 'project', 'shared', 'global')),
            CHECK(status IN ('pending', 'adopted', 'rejected', 'expired')),
            CHECK(
                status NOT IN ('adopted', 'rejected')
                OR (length(trim(approval_actor)) > 0 AND length(trim(approval_call_id)) > 0)
            ),
            CHECK(status != 'adopted' OR length(trim(promoted_memory_id)) > 0),
            CHECK(status != 'rejected' OR length(trim(rejection_reason)) > 0),
            CHECK(
                (status IN ('pending', 'adopted') AND length(content) BETWEEN 1 AND 500)
                OR (status IN ('rejected', 'expired') AND content = '')
            )
        )
        """
    )


def ensure_memory_proposal_schema(conn: Any) -> None:
    _create_memory_proposal_table(conn, "memory_proposals")
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(memory_proposals)").fetchall()
    }
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_proposals'"
    ).fetchone()
    table_sql = str(table_row[0] or "") if table_row else ""
    if not _memory_proposal_schema_current(columns, table_sql):
        _rebuild_memory_proposal_table(conn, columns)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposals_fingerprint
        ON memory_proposals(project_id, origin_turn_hash, content_hash)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_proposals_pending_expiry
        ON memory_proposals(status, expires_at, proposal_id)
        """
    )


def _memory_proposal_schema_current(columns: set[str], table_sql: str) -> bool:
    normalized = " ".join(str(table_sql or "").lower().split())
    required_fragments = (
        "origin_visibility",
        "unique(project_id, origin_turn_hash, content_hash)",
        "status not in ('adopted', 'rejected')",
        "status != 'adopted' or length(trim(promoted_memory_id)) > 0",
        "between 1 and 500",
    )
    return "origin_visibility" in columns and all(
        fragment in normalized for fragment in required_fragments
    )


def _rebuild_memory_proposal_table(conn: Any, columns: set[str]) -> None:
    ordered_columns = [
        str(row[1]) for row in conn.execute("PRAGMA table_info(memory_proposals)").fetchall()
    ]
    rows = conn.execute("SELECT * FROM memory_proposals").fetchall()
    legacy_name = f"memory_proposals_legacy_{secrets.token_hex(6)}"
    savepoint = f"memory_proposals_migrate_{secrets.token_hex(6)}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(f"ALTER TABLE memory_proposals RENAME TO {legacy_name}")
        _create_memory_proposal_table(conn, "memory_proposals")
        placeholders = ", ".join("?" for _ in _ROW_COLUMNS)
        for raw_row in rows:
            source = dict(zip(ordered_columns, tuple(raw_row), strict=True))
            normalized = _normalize_legacy_proposal_row(source, columns)
            conn.execute(
                f"INSERT OR IGNORE INTO memory_proposals "
                f"({', '.join(_ROW_COLUMNS)}) VALUES ({placeholders})",
                tuple(normalized[column] for column in _ROW_COLUMNS),
            )
        conn.execute(f"DROP TABLE {legacy_name}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _normalize_legacy_proposal_row(source: Mapping[str, Any], columns: set[str]) -> dict[str, Any]:
    now_text = _utc_text(datetime.now(UTC))
    content = _normalize_whitespace(source.get("content"))
    content_is_safe = bool(content) and len(content) <= MAX_CANDIDATE_LENGTH
    content_is_safe = content_is_safe and not contains_secret(content)
    category = str(source.get("category") or "").strip().casefold()
    if category not in PROPOSAL_CATEGORIES:
        category = "fact"
        content_is_safe = False
    origin_role = str(source.get("origin_role") or "").strip().casefold()
    if origin_role != "user":
        origin_role = "user"
        content_is_safe = False
    project_id = str(source.get("project_id") or "project:unknown").strip()
    visibility = str(source.get("visibility") or "project").strip().casefold()
    if visibility not in VISIBILITY_RANK:
        visibility = "project"
    origin_visibility = (
        str(source.get("origin_visibility") or visibility).strip().casefold()
        if "origin_visibility" in columns
        else visibility
    )
    if origin_visibility not in VISIBILITY_RANK or not visibility_allows(
        visibility, [origin_visibility]
    ):
        origin_visibility = visibility

    content_hash = str(source.get("content_hash") or "").strip() or _content_hash(content)
    origin_turn_hash = str(source.get("origin_turn_hash") or "").strip() or content_hash
    proposal_id = str(source.get("proposal_id") or "").strip() or _proposal_id(
        project_id, origin_turn_hash, content_hash
    )
    actor = str(source.get("approval_actor") or "").strip()
    approval_call = str(source.get("approval_call_id") or "").strip()
    promoted_memory_id = str(source.get("promoted_memory_id") or "").strip()
    status = str(source.get("status") or "pending").strip().casefold()
    if status not in PROPOSAL_STATUSES:
        status = "expired"
    if status == "adopted" and not (actor and approval_call and promoted_memory_id):
        status = "pending" if content_is_safe else "expired"
        actor = approval_call = promoted_memory_id = ""
    if status == "pending" and not content_is_safe:
        status = "expired"
    rejection_reason = _normalize_rejection_reason(source.get("rejection_reason"))
    if status == "rejected" and not (actor and approval_call):
        status = "expired"
    if status in {"rejected", "expired"}:
        content = ""
    if status != "rejected":
        rejection_reason = ""
    redacted_at = str(source.get("redacted_at") or "").strip()
    if status in {"rejected", "expired"} and not redacted_at:
        redacted_at = now_text

    metadata_value: object = source.get("metadata_json", "{}")
    try:
        if isinstance(metadata_value, str):
            metadata_value = json.loads(metadata_value or "{}")
        metadata_json = _metadata_json(metadata_value)
    except (ProposalPolicyError, TypeError, ValueError):
        metadata_json = "{}"
    return {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "visibility": visibility,
        "origin_visibility": origin_visibility,
        "content": content,
        "content_hash": content_hash,
        "category": category,
        "origin_role": origin_role,
        "origin_turn_hash": origin_turn_hash,
        "origin_call_id": str(source.get("origin_call_id") or "").strip(),
        "status": status,
        "approval_actor": actor,
        "approval_call_id": approval_call,
        "promoted_memory_id": promoted_memory_id,
        "rejection_reason": rejection_reason,
        "metadata_json": metadata_json,
        "expires_at": str(source.get("expires_at") or now_text),
        "redacted_at": redacted_at,
        "created_at": str(source.get("created_at") or now_text),
        "updated_at": str(source.get("updated_at") or now_text),
    }


def classify_proposal_candidates(
    conversation: object,
    *,
    extract: Callable[[str], Iterable[Any]] | None = None,
    project_id: str = "project:unknown",
    visibility: str = "project",
    origin_role: str = "user",
    origin_turn_hash: str = "",
    origin_call_id: str = "",
    origin_visibility: str = "project",
    metadata: Mapping[str, Any] | None = None,
) -> ProposalClassification:
    text = str(conversation or "")
    if contains_secret(text):
        return _rejected("secret_detected")

    if extract is None:
        from plastic_promise.smart_extractor import extract_memories

        extract = extract_memories

    try:
        extracted_items = list(extract(text) or [])
    except Exception:
        return _rejected("proposal_classification_uncertain")
    if not extracted_items:
        return _rejected("proposal_classification_uncertain")

    turn_hash = str(origin_turn_hash or "").strip() or _content_hash(text)
    candidates: list[ProposalCandidate] = []
    seen_hashes: set[str] = set()
    for item in extracted_items:
        category = str(_field(item, "category") or "").strip().casefold()
        content = _extracted_content(item)
        try:
            confidence = float(_field(item, "confidence"))
        except (TypeError, ValueError):
            return _rejected("proposal_classification_uncertain")
        atomic = _field(item, "is_atomic", _field(item, "atomic", True))
        if (
            category not in PROPOSAL_CATEGORIES
            or not math.isfinite(confidence)
            or not 0.5 <= confidence <= 1.0
            or atomic is False
        ):
            return _rejected("proposal_classification_uncertain")
        if _TRANSCRIPT_ROLE_RE.search(content):
            return _rejected("proposal_classification_uncertain")

        candidate = ProposalCandidate(
            content=content,
            category=category,
            project_id=project_id,
            visibility=visibility,
            origin_role=origin_role,
            origin_turn_hash=turn_hash,
            origin_call_id=origin_call_id,
            origin_visibility=origin_visibility,
            metadata=dict(metadata or {}),
        )
        try:
            normalized = _validate_candidate(candidate)
        except ProposalPolicyError:
            return _rejected("proposal_classification_uncertain")
        fingerprint = _content_hash(normalized.content)
        if fingerprint in seen_hashes:
            continue
        seen_hashes.add(fingerprint)
        candidates.append(normalized)

    if not candidates or len(candidates) > MAX_CANDIDATES:
        return _rejected("proposal_classification_uncertain")
    return ProposalClassification(decision="propose", candidates=tuple(candidates))


class MemoryProposalStore:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        ensure_memory_proposal_schema(conn)

    def create_many(
        self,
        candidates: Iterable[ProposalCandidate],
        *,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        normalized: list[ProposalCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            validated = _validate_candidate(candidate)
            content_hash = _content_hash(validated.content)
            fingerprint = (
                validated.project_id,
                validated.origin_turn_hash,
                content_hash,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            normalized.append(validated)

        if not normalized:
            raise ProposalPolicyError("no_candidates")
        if len(normalized) > MAX_CANDIDATES:
            raise ProposalPolicyError("too_many_candidates")

        created = _as_utc(now)
        created_at = _utc_text(created)
        expires_at = _utc_text(created + timedelta(days=DEFAULT_EXPIRY_DAYS))
        rows: list[dict[str, Any]] = []
        with self._transaction():
            for candidate in normalized:
                content_hash = _content_hash(candidate.content)
                proposal_id = _proposal_id(
                    candidate.project_id,
                    candidate.origin_turn_hash,
                    content_hash,
                )
                metadata_json = _metadata_json(candidate.metadata)
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_proposals (
                        proposal_id, project_id, visibility, origin_visibility,
                        content, content_hash, category, origin_role,
                        origin_turn_hash, origin_call_id, status,
                        metadata_json, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        candidate.project_id,
                        candidate.visibility,
                        candidate.origin_visibility,
                        candidate.content,
                        content_hash,
                        candidate.category,
                        candidate.origin_role,
                        candidate.origin_turn_hash,
                        candidate.origin_call_id,
                        metadata_json,
                        expires_at,
                        created_at,
                        created_at,
                    ),
                )
                row = self._get_by_fingerprint(
                    candidate.project_id,
                    candidate.origin_turn_hash,
                    content_hash,
                )
                if row is None:
                    raise ProposalPolicyError("proposal_persistence_failed")
                rows.append(row)
        return rows

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {', '.join(_ROW_COLUMNS)} FROM memory_proposals WHERE proposal_id = ?",
            (str(proposal_id or "").strip(),),
        ).fetchone()
        return _row_dict(row)

    def adopt_prepared(
        self,
        proposal_id: str,
        promoted_memory_id: str = "",
        *,
        actor: str,
        call_id: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        actor_value, call_value = _review_evidence(actor, call_id)
        memory_id = str(promoted_memory_id or "").strip()
        if not memory_id:
            raise ProposalPolicyError("promoted_memory_id_required")
        now_value = _as_utc(now)
        now_text = _utc_text(now_value)
        expired = False
        with self._transaction():
            row = self._require(proposal_id)
            if row["status"] == "adopted":
                return _validate_adopted_row(row)
            if row["status"] == "expired":
                raise ProposalPolicyError("proposal_expired")
            if row["status"] != "pending":
                raise ProposalPolicyError("proposal_not_pending")
            if _parse_utc(row["expires_at"]) <= now_value:
                self._expire_one(row["proposal_id"], now_text)
                expired = True
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE memory_proposals
                    SET status = 'adopted', approval_actor = ?, approval_call_id = ?,
                        promoted_memory_id = ?, updated_at = ?
                    WHERE proposal_id = ? AND status = 'pending' AND expires_at > ?
                    """,
                    (
                        actor_value,
                        call_value,
                        memory_id,
                        now_text,
                        row["proposal_id"],
                        now_text,
                    ),
                )
                if cursor.rowcount != 1:
                    concurrent = self._require(row["proposal_id"])
                    if concurrent["status"] == "adopted":
                        return _validate_adopted_row(concurrent)
                    raise ProposalPolicyError("proposal_transition_conflict")
        if expired:
            raise ProposalPolicyError("proposal_expired")
        return self._require(proposal_id)

    def reject(
        self,
        proposal_id: str,
        *,
        actor: str,
        call_id: str,
        reason: str = "rejected",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        actor_value, call_value = _review_evidence(actor, call_id)
        reason_value = _normalize_rejection_reason(reason)
        now_value = _as_utc(now)
        now_text = _utc_text(now_value)
        expired = False
        with self._transaction():
            row = self._require(proposal_id)
            if row["status"] in {"rejected", "expired"}:
                return row
            if row["status"] != "pending":
                raise ProposalPolicyError("proposal_not_pending")
            if _parse_utc(row["expires_at"]) <= now_value:
                self._expire_one(row["proposal_id"], now_text)
                expired = True
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE memory_proposals
                    SET status = 'rejected', content = '', approval_actor = ?,
                        approval_call_id = ?, rejection_reason = ?, redacted_at = ?,
                        updated_at = ?
                    WHERE proposal_id = ? AND status = 'pending' AND expires_at > ?
                    """,
                    (
                        actor_value,
                        call_value,
                        reason_value,
                        now_text,
                        now_text,
                        row["proposal_id"],
                        now_text,
                    ),
                )
                if cursor.rowcount != 1:
                    concurrent = self._require(row["proposal_id"])
                    if concurrent["status"] in {"rejected", "expired"}:
                        return concurrent
                    raise ProposalPolicyError("proposal_transition_conflict")
        if expired:
            raise ProposalPolicyError("proposal_expired")
        return self._require(proposal_id)

    def expire_and_redact(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> int:
        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ProposalPolicyError("invalid_expiry_limit") from exc
        if bounded_limit < 0 or bounded_limit > 1000:
            raise ProposalPolicyError("invalid_expiry_limit")
        if bounded_limit == 0:
            return 0

        now_text = _utc_text(_as_utc(now))
        with self._transaction():
            proposal_ids = [
                str(row[0])
                for row in self.conn.execute(
                    """
                    SELECT proposal_id FROM memory_proposals
                    WHERE status = 'pending' AND expires_at <= ?
                    ORDER BY expires_at, proposal_id
                    LIMIT ?
                    """,
                    (now_text, bounded_limit),
                ).fetchall()
            ]
            if not proposal_ids:
                return 0
            placeholders = ", ".join("?" for _ in proposal_ids)
            cursor = self.conn.execute(
                f"""
                UPDATE memory_proposals
                SET status = 'expired', content = '', redacted_at = ?, updated_at = ?
                WHERE status = 'pending' AND proposal_id IN ({placeholders})
                """,
                (now_text, now_text, *proposal_ids),
            )
            return int(cursor.rowcount)

    def _get_by_fingerprint(
        self,
        project_id: str,
        origin_turn_hash: str,
        content_hash: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"""
            SELECT {", ".join(_ROW_COLUMNS)} FROM memory_proposals
            WHERE project_id = ? AND origin_turn_hash = ? AND content_hash = ?
            """,
            (project_id, origin_turn_hash, content_hash),
        ).fetchone()
        return _row_dict(row)

    def _require(self, proposal_id: str) -> dict[str, Any]:
        row = self.get(proposal_id)
        if row is None:
            raise ProposalPolicyError("proposal_not_found")
        return row

    def _expire_one(self, proposal_id: str, now_text: str) -> None:
        self.conn.execute(
            """
            UPDATE memory_proposals
            SET status = 'expired', content = '', redacted_at = ?, updated_at = ?
            WHERE proposal_id = ? AND status = 'pending'
            """,
            (now_text, now_text, proposal_id),
        )

    @contextmanager
    def _transaction(self):
        savepoint = f"memory_proposal_{secrets.token_hex(6)}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def _validate_candidate(candidate: ProposalCandidate) -> ProposalCandidate:
    if not isinstance(candidate, ProposalCandidate):
        raise ProposalPolicyError("invalid_candidate")
    if not isinstance(candidate.content, str):
        raise ProposalPolicyError("invalid_content")
    content = _normalize_whitespace(candidate.content)
    if not content:
        raise ProposalPolicyError("empty_content")
    if len(content) > MAX_CANDIDATE_LENGTH:
        raise ProposalPolicyError("content_too_long")
    if _TRANSCRIPT_ROLE_RE.search(content):
        raise ProposalPolicyError("transcript_content_rejected")
    if contains_secret(content):
        raise ProposalPolicyError("secret_detected")

    category = str(candidate.category or "").strip().casefold()
    if category not in PROPOSAL_CATEGORIES:
        raise ProposalPolicyError("unknown_category")
    origin_role = str(candidate.origin_role or "").strip().casefold()
    if origin_role != "user":
        raise ProposalPolicyError("user_origin_required")
    project_id = str(candidate.project_id or "").strip()
    if not project_id:
        raise ProposalPolicyError("project_id_required")
    origin_turn_hash = str(candidate.origin_turn_hash or "").strip()
    if not origin_turn_hash:
        raise ProposalPolicyError("origin_turn_hash_required")

    visibility = str(candidate.visibility or "").strip().casefold()
    origin_visibility = str(candidate.origin_visibility or "").strip().casefold()
    if visibility not in VISIBILITY_RANK or origin_visibility not in VISIBILITY_RANK:
        raise ProposalPolicyError("invalid_visibility")
    if not visibility_allows(visibility, [origin_visibility]):
        raise ProposalPolicyError("visibility_widening")
    metadata = _metadata_mapping(candidate.metadata)
    return ProposalCandidate(
        content=content,
        category=category,
        project_id=project_id,
        visibility=visibility,
        origin_role=origin_role,
        origin_turn_hash=origin_turn_hash,
        origin_call_id=str(candidate.origin_call_id or "").strip(),
        origin_visibility=origin_visibility,
        metadata=metadata,
    )


def _configured_secret_patterns() -> tuple[str, ...]:
    raw = os.environ.get(SECRET_PATTERN_ENV, "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return tuple(line.strip() for line in raw.splitlines() if line.strip())
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return ("(",)
    return tuple(item for item in parsed if item)


def _normalize_whitespace(content: object) -> str:
    return " ".join(str(content or "").split())


def _content_hash(content: object) -> str:
    return "sha256:" + hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _proposal_id(project_id: str, origin_turn_hash: str, content_hash: str) -> str:
    payload = "\x1f".join((project_id, origin_turn_hash, content_hash))
    return "proposal_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _metadata_mapping(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProposalPolicyError("invalid_metadata")
    try:
        mapped = dict(value)
        encoded = json.dumps(mapped, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProposalPolicyError("invalid_metadata") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ProposalPolicyError("metadata_too_large")
    if _contains_forbidden_metadata_payload(mapped):
        raise ProposalPolicyError("metadata_payload_rejected")
    if contains_secret(encoded):
        raise ProposalPolicyError("secret_detected")
    return mapped


def _metadata_json(value: object) -> str:
    return json.dumps(
        _metadata_mapping(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _contains_forbidden_metadata_payload(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().casefold() in FORBIDDEN_METADATA_KEYS:
                return True
            if _contains_forbidden_metadata_payload(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_metadata_payload(item) for item in value)
    elif isinstance(value, str):
        return _TRANSCRIPT_ROLE_RE.search(value) is not None
    return False


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        return _parse_utc(value)
    if not isinstance(value, datetime):
        raise ProposalPolicyError("invalid_timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ProposalPolicyError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ProposalPolicyError("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _review_evidence(actor: object, call_id: object) -> tuple[str, str]:
    actor_value = str(actor or "").strip()
    call_value = str(call_id or "").strip()
    if not actor_value:
        raise ProposalPolicyError("review_actor_required")
    if not call_value:
        raise ProposalPolicyError("review_call_id_required")
    return actor_value, call_value


def _normalize_rejection_reason(reason: object) -> str:
    normalized = str(reason or "rejected").strip().casefold().replace("-", "_")
    return normalized if normalized in REJECTION_REASON_CODES else "reviewer_rejected"


def _validate_adopted_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not all(
        str(row.get(field) or "").strip()
        for field in ("approval_actor", "approval_call_id", "promoted_memory_id")
    ):
        raise ProposalPolicyError("invalid_adopted_proposal")
    return dict(row)


def _field(item: object, name: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _extracted_content(item: object) -> str:
    for name in ("source_segment", "l2_content", "content"):
        value = _field(item, name)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _rejected(reason: str) -> ProposalClassification:
    return ProposalClassification(decision="reject", reason_codes=(reason,))


def _row_dict(row: object) -> dict[str, Any] | None:
    if row is None:
        return None
    values = tuple(row)  # type: ignore[arg-type]
    result = dict(zip(_ROW_COLUMNS, values, strict=True))
    raw_metadata = result.pop("metadata_json", "{}")
    try:
        parsed = json.loads(str(raw_metadata or "{}"))
    except (TypeError, ValueError):
        parsed = {}
    metadata = parsed if isinstance(parsed, dict) else {}
    result["metadata_json"] = metadata
    result["metadata"] = dict(metadata)
    return result


def _canonical_connection(engine: Any):
    state = getattr(engine, "__dict__", {})
    sqlite_store = state.get("_sqlite") if isinstance(state, dict) else None
    if sqlite_store is None:
        sqlite_store = getattr(engine, "_sqlite", None)
    return getattr(sqlite_store, "_conn", None)


def _deterministic_memory_id(proposal_id: str) -> str:
    digest = hashlib.sha256(str(proposal_id).encode("utf-8")).hexdigest()[:20]
    return f"proposal_mem_{digest}"


def _prepared_metadata(prepared: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    from plastic_promise.core.memory_index import metadata_with_index_material

    metadata = dict(getattr(prepared, "metadata", {}) or {})
    metadata.update(
        {
            "proposal_id": row["proposal_id"],
            "proposal_content_hash": row["content_hash"],
            "proposal_origin_turn_hash": row["origin_turn_hash"],
            "proposal_origin_call_id": row["origin_call_id"],
        }
    )
    return metadata_with_index_material(metadata, prepared.index_material)


def _insert_prepared_memory(
    conn: Any,
    *,
    memory_id: str,
    row: Mapping[str, Any],
    prepared: Any,
    actor: str,
    call_id: str,
    now_text: str,
) -> None:
    metadata = _prepared_metadata(prepared, row)
    domain = str(metadata.get("domain") or "uncategorized")
    try:
        importance = float(metadata.get("importance", 0.7))
    except (TypeError, ValueError):
        importance = 0.7
    source_class = "user_fact" if prepared.category == "fact" else prepared.category
    conn.execute(
        """
        INSERT INTO memories (
            id, content, memory_type, source, owner, tier, scope, category,
            tags, domain, importance, entity_ids, created_at, access_count,
            worth_success, worth_failure, activation_weight, decay_multiplier,
            effective_half_life, last_accessed, project_id, visibility,
            source_class, created_by_call_id, origin_kind, origin_uri,
            origin_ref, origin_hash, parent_memory_ids, metadata_json,
            raw_content, l0_abstract, l1_summary, l2_content,
            embedding_text, embedding_hash, search_text
        ) VALUES (?, ?, 'experience', 'user', ?, ?, 'global', ?, ?, ?, ?, '[]',
                  ?, 0, 0, 0, 0.5, 1.0, 3.0, ?, ?, ?, ?, ?,
                  'memory_proposal', ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            prepared.content,
            actor,
            prepared.tier,
            prepared.category,
            json.dumps(list(prepared.tags), ensure_ascii=False),
            domain,
            importance,
            now_text,
            now_text,
            row["project_id"],
            row["visibility"],
            source_class,
            call_id,
            f"proposal://{row['proposal_id']}",
            row["proposal_id"],
            row["origin_turn_hash"],
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            str(metadata.get("raw_content") or prepared.content),
            str(metadata.get("l0_abstract") or ""),
            str(metadata.get("l1_summary") or ""),
            str(metadata.get("l2_content") or prepared.content),
            prepared.index_material.vector_text,
            prepared.index_material.embedding_hash,
            prepared.index_material.search_text,
        ),
    )


def _refresh_engine_after_promotion(engine: Any) -> None:
    refresh = getattr(engine, "_refresh_canonical_cache_if_changed", None)
    if callable(refresh):
        refresh(force=True)


def _enqueue_promoted_index(
    conn: Any,
    *,
    memory_id: str,
    call_id: str,
) -> str:
    from plastic_promise.core.traceability import enqueue_memory_index_upsert

    row = conn.execute(
        "SELECT project_id, embedding_hash FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        raise ProposalPolicyError("promoted_memory_missing")
    return enqueue_memory_index_upsert(
        conn,
        memory_id=memory_id,
        project_id=str(row[0] or ""),
        embedding_hash=str(row[1] or ""),
        call_id=call_id,
    )


def _adopted_result(
    engine: Any,
    row: Mapping[str, Any],
    *,
    call_id: str,
    created: bool,
    index_job_id: str = "",
) -> PromotionResult:
    row = _validate_adopted_row(row)
    conn = _canonical_connection(engine)
    if conn is None:
        raise ProposalPolicyError("canonical_store_unavailable")
    memory_id = str(row.get("promoted_memory_id") or "")
    if not memory_id:
        raise ProposalPolicyError("promoted_memory_id_required")
    if not index_job_id:
        index_job_id = _enqueue_promoted_index(
            conn,
            memory_id=memory_id,
            call_id=call_id,
        )
    return PromotionResult(
        proposal_id=str(row["proposal_id"]),
        status="adopted",
        memory_id=memory_id,
        created=created,
        index_job_id=index_job_id,
    )


def promote_memory_proposal(
    engine: Any,
    proposal_id: str,
    *,
    actor: str,
    call_id: str,
) -> PromotionResult:
    """Atomically promote one approved proposal into canonical SQLite."""
    actor_value, call_value = _review_evidence(actor, call_id)
    conn = _canonical_connection(engine)
    if conn is None:
        raise ProposalPolicyError("canonical_store_unavailable")
    if conn.in_transaction:
        raise ProposalPolicyError("canonical_transaction_open")
    store = MemoryProposalStore(conn)
    row = store._require(proposal_id)
    if row["status"] == "adopted":
        return _adopted_result(engine, row, call_id=call_value, created=False)
    if row["status"] == "expired":
        raise ProposalPolicyError("proposal_expired")
    if row["status"] != "pending":
        raise ProposalPolicyError("proposal_not_pending")

    now_value = datetime.now(UTC)
    now_text = _utc_text(now_value)
    if _parse_utc(row["expires_at"]) <= now_value:
        with store._transaction():
            store._expire_one(row["proposal_id"], now_text)
        raise ProposalPolicyError("proposal_expired")

    from plastic_promise.mcp.tools.memory import _get_fuzzy_buffer

    pipeline = _get_fuzzy_buffer(engine)
    prepared = pipeline.prepare_approved_candidate(
        row["content"],
        category=row["category"],
        source="user",
        source_class="user_fact" if row["category"] == "fact" else row["category"],
        project_id=row["project_id"],
        visibility=row["visibility"],
        created_by_call_id=call_value,
        origin_kind="memory_proposal",
        origin_uri=f"proposal://{row['proposal_id']}",
        origin_ref=row["proposal_id"],
        origin_hash=row["origin_turn_hash"],
        metadata_json=row.get("metadata_json", {}),
    )
    memory_id = _deterministic_memory_id(row["proposal_id"])
    created = False
    expired = False
    index_job_id = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = store._require(row["proposal_id"])
        if current["status"] == "adopted":
            conn.rollback()
            return _adopted_result(engine, current, call_id=call_value, created=False)
        if current["status"] == "expired":
            conn.rollback()
            raise ProposalPolicyError("proposal_expired")
        if current["status"] != "pending":
            conn.rollback()
            raise ProposalPolicyError("proposal_not_pending")
        if _parse_utc(current["expires_at"]) <= now_value:
            store._expire_one(current["proposal_id"], now_text)
            conn.commit()
            expired = True
        else:
            existing = conn.execute(
                "SELECT id FROM memories "
                "WHERE project_id = ? AND content = ? AND origin_hash = ? "
                "ORDER BY id LIMIT 1",
                (
                    current["project_id"],
                    prepared.content,
                    current["origin_turn_hash"],
                ),
            ).fetchone()
            if existing is None:
                _insert_prepared_memory(
                    conn,
                    memory_id=memory_id,
                    row=current,
                    prepared=prepared,
                    actor=actor_value,
                    call_id=call_value,
                    now_text=now_text,
                )
                created = True
            else:
                memory_id = str(existing[0])

            cursor = conn.execute(
                """
                UPDATE memory_proposals
                SET status = 'adopted', approval_actor = ?, approval_call_id = ?,
                    promoted_memory_id = ?, updated_at = ?
                WHERE proposal_id = ? AND status = 'pending' AND expires_at > ?
                """,
                (
                    actor_value,
                    call_value,
                    memory_id,
                    now_text,
                    current["proposal_id"],
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise ProposalPolicyError("proposal_transition_conflict")

            from plastic_promise.core import traceability

            traceability.record_memory_lineage(
                conn,
                memory_id=memory_id,
                parent_memory_id=current["proposal_id"],
                relation="promoted_from_proposal",
                call_id=call_value,
                metadata={"content_hash": current["content_hash"]},
            )
            version_cursor = conn.execute(
                "UPDATE memory_version SET version = version + 1 WHERE singleton = 1"
            )
            if version_cursor.rowcount != 1:
                raise ProposalPolicyError("memory_version_unavailable")
            index_job_id = _enqueue_promoted_index(
                conn,
                memory_id=memory_id,
                call_id=call_value,
            )
            conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

    if expired:
        raise ProposalPolicyError("proposal_expired")
    _refresh_engine_after_promotion(engine)
    adopted = store._require(row["proposal_id"])
    return _adopted_result(
        engine,
        adopted,
        call_id=call_value,
        created=created,
        index_job_id=index_job_id,
    )


def reject_memory_proposal(
    engine: Any,
    proposal_id: str,
    *,
    actor: str,
    call_id: str,
    reason: str,
) -> dict[str, Any]:
    conn = _canonical_connection(engine)
    if conn is None:
        raise ProposalPolicyError("canonical_store_unavailable")
    return MemoryProposalStore(conn).reject(
        proposal_id,
        actor=actor,
        call_id=call_id,
        reason=reason,
    )


def expire_memory_proposals(engine: Any, *, limit: int = 100) -> dict[str, int]:
    conn = _canonical_connection(engine)
    if conn is None:
        raise ProposalPolicyError("canonical_store_unavailable")
    bounded_limit = max(0, min(int(limit), 1000))
    expired = MemoryProposalStore(conn).expire_and_redact(limit=bounded_limit)
    return {"expired": expired, "limit": bounded_limit}
