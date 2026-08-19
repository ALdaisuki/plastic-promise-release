"""Project-scoped security finding lifecycle primitives.

This module is intentionally persistence-agnostic.  It defines the safe domain
boundary used by DeepSec adapters before findings are written to SQLite or
queued for asynchronous processing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from plastic_promise.core.memory_proposals import contains_secret

SECURITY_STATES = frozenset(
    {
        "open",
        "remediation_required",
        "fixed",
        "resolved",
        "accepted_risk",
        "false_positive",
        "recurring",
        "needs_revalidation",
    }
)
FRESHNESS_STATES = frozenset({"fresh", "aging", "stale", "expired"})
SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
MAX_ACCEPTED_RISK_DAYS = 60
DEFAULT_ACCEPTED_RISK_DAYS = 30

_SECURITY_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"remediation_required", "accepted_risk", "false_positive", "recurring"}),
    "remediation_required": frozenset({"fixed", "accepted_risk", "false_positive", "recurring"}),
    "fixed": frozenset({"resolved", "needs_revalidation", "recurring"}),
    "resolved": frozenset({"needs_revalidation", "recurring"}),
    "accepted_risk": frozenset({"needs_revalidation", "recurring", "resolved"}),
    "false_positive": frozenset({"needs_revalidation", "recurring"}),
    "recurring": frozenset({"remediation_required", "accepted_risk", "false_positive"}),
    "needs_revalidation": frozenset(
        {"remediation_required", "accepted_risk", "false_positive", "resolved", "recurring"}
    ),
}

_UNREDACTED_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"(?:^|[\s\"'`])/(?:Users|home|private|srv|opt|etc|tmp|var|Volumes)/",
        r"(?:^|[\s\"'`])[A-Z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]+",
        r"(?:^|\s)(?:system|developer|assistant|user|tool)\s*[:：]",
        r"\b(?:prompt|raw[_ -]?content|transcript|conversation)\s*[:=]",
    )
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _contains_secret_value(value: object) -> bool:
    if isinstance(value, str):
        return contains_secret(value)
    if isinstance(value, Mapping):
        return any(_contains_secret_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret_value(item) for item in value)
    return False


def _contains_unredacted_material(value: object) -> bool:
    """Reject raw paths and transcript-shaped material in durable evidence."""

    if isinstance(value, Mapping):
        forbidden_keys = {
            "prompt",
            "raw_content",
            "raw-content",
            "raw_prompt",
            "transcript",
            "conversation",
        }
        return any(
            str(key).strip().casefold() in forbidden_keys
            or _contains_unredacted_material(key)
            or _contains_unredacted_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_unredacted_material(item) for item in value)
    text = str(value or "")
    return any(pattern.search(text) for pattern in _UNREDACTED_TEXT_PATTERNS)


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SecurityFinding:
    """Immutable, project-scoped security evidence version."""

    finding_id: str
    project_id: str
    commit_sha: str
    scan_revision: str
    rule_id: str
    severity: str = "medium"
    security_state: str = "open"
    freshness_state: str = "fresh"
    request_scope_id: str = ""
    redacted_summary: str = ""
    remediation_pattern: str = ""
    accepted_risk_expires_at: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "finding_id",
            "project_id",
            "commit_sha",
            "scan_revision",
            "rule_id",
            "request_scope_id",
        ):
            if not _text(getattr(self, name)):
                raise ValueError(f"finding_{name}_required")
        if self.project_id == "project:unknown":
            raise ValueError("finding_project_scope_required")
        if self.security_state not in SECURITY_STATES:
            raise ValueError("unknown_security_state")
        if self.freshness_state not in FRESHNESS_STATES:
            raise ValueError("unknown_freshness_state")
        if self.severity not in SEVERITIES:
            raise ValueError("unknown_finding_severity")
        identity_values = (
            self.finding_id,
            self.project_id,
            self.commit_sha,
            self.scan_revision,
            self.rule_id,
            self.request_scope_id,
        )
        if any(_contains_secret_value(value) for value in identity_values):
            raise ValueError("finding_secret_detected")
        if any(_contains_unredacted_material(value) for value in identity_values):
            raise ValueError("finding_unredacted_material")
        if _contains_secret_value(self.redacted_summary) or _contains_secret_value(
            self.remediation_pattern
        ):
            raise ValueError("finding_secret_detected")
        if _contains_unredacted_material(self.redacted_summary) or _contains_unredacted_material(
            self.remediation_pattern
        ):
            raise ValueError("finding_unredacted_material")
        evidence = dict(self.evidence or {})
        if _contains_secret_value(evidence):
            raise ValueError("finding_secret_detected")
        if _contains_unredacted_material(evidence):
            raise ValueError("finding_unredacted_material")
        if self.security_state == "resolved" and evidence.get("rescan_passed") is not True:
            raise ValueError("resolution_evidence_required")
        object.__setattr__(self, "evidence", evidence)
        if self.security_state == "accepted_risk":
            if not self.accepted_risk_expires_at:
                raise ValueError("accepted_risk_expiry_required")
            if not _text(evidence.get("reason")):
                raise ValueError("accepted_risk_reason_required")
            if not _text(evidence.get("accepted_risk_started_at")):
                raise ValueError("accepted_risk_started_at_required")
        if self.accepted_risk_expires_at:
            expiry = _parse_timestamp(
                self.accepted_risk_expires_at,
                field_name="accepted_risk_expires_at",
            )
            started_at = _text(evidence.get("accepted_risk_started_at"))
            if started_at:
                started = _parse_timestamp(
                    started_at,
                    field_name="accepted_risk_started_at",
                )
                if expiry < started:
                    raise ValueError("accepted_risk_expiry_invalid")
                if expiry > started + timedelta(days=MAX_ACCEPTED_RISK_DAYS):
                    raise ValueError("accepted_risk_expiry_too_far")

    def scope_key(self) -> str:
        """Return a stable opaque identity for this project's finding scope."""

        stable = "\x1f".join(
            (
                self.project_id,
                self.commit_sha,
                self.scan_revision,
                self.rule_id,
                self.request_scope_id,
            )
        )
        return "finding-scope:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def transition(
        self,
        security_state: str,
        *,
        evidence: Mapping[str, Any] | None = None,
        accepted_risk_expires_at: str = "",
        now: datetime | None = None,
    ) -> SecurityFinding:
        """Create a validated next finding version; never mutate this version."""

        if security_state not in SECURITY_STATES:
            raise ValueError("unknown_security_state")
        if security_state not in _SECURITY_TRANSITIONS[self.security_state]:
            raise ValueError("invalid_security_transition")
        additions = dict(evidence or {})
        if _contains_secret_value(additions):
            raise ValueError("finding_secret_detected")
        if _contains_unredacted_material(additions):
            raise ValueError("finding_unredacted_material")
        if security_state == "resolved" and additions.get("rescan_passed") is not True:
            raise ValueError("resolution_evidence_required")
        next_expiry = self.accepted_risk_expires_at
        if security_state == "accepted_risk":
            current = now or datetime.now(timezone.utc)
            next_expiry = (
                accepted_risk_expires_at
                or (current + timedelta(days=DEFAULT_ACCEPTED_RISK_DAYS)).isoformat()
            )
            expiry = _parse_timestamp(next_expiry, field_name="accepted_risk_expires_at")
            if expiry > current.astimezone(timezone.utc) + timedelta(days=MAX_ACCEPTED_RISK_DAYS):
                raise ValueError("accepted_risk_expiry_too_far")
            if not _text(additions.get("reason")):
                raise ValueError("accepted_risk_reason_required")
            additions.setdefault(
                "accepted_risk_started_at",
                current.astimezone(timezone.utc).isoformat(),
            )
        elif security_state == "needs_revalidation":
            next_expiry = ""

        next_commit = _text(additions.get("commit_sha")) or self.commit_sha
        next_scan = _text(additions.get("scan_revision")) or self.scan_revision
        next_scope = _text(additions.get("request_scope_id")) or self.request_scope_id
        merged_evidence = {**self.evidence, **additions}
        return replace(
            self,
            commit_sha=next_commit,
            scan_revision=next_scan,
            request_scope_id=next_scope,
            security_state=security_state,
            accepted_risk_expires_at=next_expiry,
            evidence=merged_evidence,
        )

    def validate_transition_to(self, next_finding: SecurityFinding) -> None:
        """Validate an already-materialized child version before persistence."""

        if next_finding.project_id != self.project_id:
            raise ValueError("finding_parent_scope_mismatch")
        if next_finding.finding_id != self.finding_id:
            raise ValueError("finding_parent_scope_mismatch")
        if next_finding.security_state not in _SECURITY_TRANSITIONS[self.security_state]:
            raise ValueError("invalid_security_transition")
        if (
            next_finding.security_state == "resolved"
            and next_finding.evidence.get("rescan_passed") is not True
        ):
            raise ValueError("resolution_evidence_required")
        if next_finding.security_state == "accepted_risk":
            if not next_finding.accepted_risk_expires_at:
                raise ValueError("accepted_risk_expiry_required")
            if not _text(next_finding.evidence.get("reason")):
                raise ValueError("accepted_risk_reason_required")
            started_at = _text(next_finding.evidence.get("accepted_risk_started_at"))
            if not started_at:
                raise ValueError("accepted_risk_started_at_required")
            started = _parse_timestamp(started_at, field_name="accepted_risk_started_at")
            expiry = _parse_timestamp(
                next_finding.accepted_risk_expires_at,
                field_name="accepted_risk_expires_at",
            )
            if expiry < started:
                raise ValueError("accepted_risk_expiry_invalid")
            if expiry > started + timedelta(days=MAX_ACCEPTED_RISK_DAYS):
                raise ValueError("accepted_risk_expiry_too_far")

    def with_freshness(self, freshness_state: str) -> SecurityFinding:
        if freshness_state not in FRESHNESS_STATES:
            raise ValueError("unknown_freshness_state")
        allowed = {
            "fresh": frozenset({"fresh", "aging"}),
            "aging": frozenset({"aging", "stale"}),
            "stale": frozenset({"stale", "expired"}),
            "expired": frozenset({"expired"}),
        }
        if freshness_state not in allowed[self.freshness_state]:
            raise ValueError("invalid_freshness_transition")
        return replace(self, freshness_state=freshness_state)

    def to_evidence(self) -> dict[str, Any]:
        """Project only redacted, scope-bound fields for durable evidence."""

        return {
            "finding_id": self.finding_id,
            "project_id": self.project_id,
            "scope_key": self.scope_key(),
            "commit_sha": self.commit_sha,
            "scan_revision": self.scan_revision,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "security_state": self.security_state,
            "freshness_state": self.freshness_state,
            "request_scope_id": self.request_scope_id,
            "redacted_summary": self.redacted_summary,
            "remediation_pattern": self.remediation_pattern,
            "accepted_risk_expires_at": self.accepted_risk_expires_at,
            "evidence": dict(self.evidence),
        }
