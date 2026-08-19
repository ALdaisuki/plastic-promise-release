"""Durable project-scoped DeepSec Shield scan work and evidence versions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from plastic_promise.core.derived_work import (
    DerivedWorkCreateResult,
    DerivedWorkJob,
    DerivedWorkLease,
    DerivedWorkStore,
)
from plastic_promise.core.security_findings import (
    SecurityFinding,
    _contains_secret_value,
    _contains_unredacted_material,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from plastic_promise.core.lancedb_generation import GenerationManager, GenerationManifest

_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_SCAN_REVISION_RE = re.compile(r"\A(?P<prefix>.*?)(?:[:@/_-])v?(?P<number>\d+)\Z")
_SHIELD_JOB_KIND = "security.shield_scan"
_DEFAULT_PROVIDER = "deepsec:shield"
_DEFAULT_BATCH_SIZE = 20
_DEFAULT_MAX_WAIT_SECONDS = 30.0
_CANARY_RECEIPT_METRICS = frozenset(
    {
        "hit_at_1",
        "hit_at_5",
        "mrr",
        "p95_ms",
        "forbidden_hit_rate",
        "conflict_rate",
        "cost_usd",
        "sample_count",
    }
)
_CANARY_RECEIPT_REQUIRED_METRICS = _CANARY_RECEIPT_METRICS


def _text(value: object) -> str:
    return str(value or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scope_required(project_id: str) -> str:
    normalized = _text(project_id)
    if not normalized or normalized == "project:unknown":
        raise ValueError("shield_scan_project_scope_required")
    return normalized


def _scan_revision_is_newer(previous: str, current: str) -> bool:
    """Require an ordered revision suffix rather than mere inequality."""

    previous_match = _SCAN_REVISION_RE.fullmatch(previous)
    current_match = _SCAN_REVISION_RE.fullmatch(current)
    if previous_match is None or current_match is None:
        raise ValueError("rescan_scan_revision_order_unverifiable")
    if previous_match.group("prefix") != current_match.group("prefix"):
        raise ValueError("rescan_scan_revision_prefix_mismatch")
    return int(current_match.group("number")) > int(previous_match.group("number"))


def _validate_scan_identity(value: object, *, code_prefix: str) -> None:
    if _contains_secret_value(value):
        raise ValueError(f"{code_prefix}_secret_detected")
    if _contains_unredacted_material(value):
        raise ValueError(f"{code_prefix}_unredacted_material")


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_receipt_timestamp(value: object, *, field_name: str) -> str:
    normalized = _text(value)
    if not normalized:
        raise ValueError(f"remediation_canary_receipt_{field_name}_required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"remediation_canary_receipt_{field_name}_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"remediation_canary_receipt_{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_receipt_metrics(value: object) -> dict[str, float | int]:
    if not isinstance(value, Mapping):
        raise ValueError("remediation_canary_receipt_metrics_invalid")
    if set(value) != set(_CANARY_RECEIPT_REQUIRED_METRICS):
        raise ValueError("remediation_canary_receipt_metrics_incomplete")
    normalized: dict[str, float | int] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or isinstance(raw, bool):
            raise ValueError("remediation_canary_receipt_metrics_invalid")
        if key == "sample_count":
            if not isinstance(raw, int) or raw <= 0:
                raise ValueError("remediation_canary_receipt_metric_invalid")
            normalized[key] = raw
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("remediation_canary_receipt_metric_invalid") from exc
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("remediation_canary_receipt_metric_invalid")
        if (
            key in {"hit_at_1", "hit_at_5", "mrr", "forbidden_hit_rate", "conflict_rate"}
            and number > 1.0
        ):
            raise ValueError("remediation_canary_receipt_metric_invalid")
        normalized[key] = number
    return normalized


@dataclass(frozen=True)
class ShieldScanRequest:
    """Immutable scan input with no raw path or source-content payload."""

    project_id: str
    commit_sha: str
    scan_revision: str
    request_scope_id: str
    shield_layers: tuple[str, ...] = ("l1", "l2", "l3")
    provider_identity: str = _DEFAULT_PROVIDER
    changed_paths_hash: str = ""
    priority: int = 0
    max_attempts: int = 4

    def __post_init__(self) -> None:
        _scope_required(self.project_id)
        for name in ("commit_sha", "scan_revision", "request_scope_id", "provider_identity"):
            if not _text(getattr(self, name)):
                raise ValueError(f"shield_scan_{name}_required")
            _validate_scan_identity(getattr(self, name), code_prefix="shield_scan")
        layers = tuple(
            sorted({_text(layer).casefold() for layer in self.shield_layers if _text(layer)})
        )
        if not layers or not set(layers).issubset({"l1", "l2", "l3"}):
            raise ValueError("shield_scan_layers_invalid")
        object.__setattr__(self, "shield_layers", layers)
        if self.changed_paths_hash and not _SHA256_RE.fullmatch(self.changed_paths_hash):
            raise ValueError("shield_scan_changed_paths_hash_invalid")
        if self.max_attempts < 1:
            raise ValueError("shield_scan_max_attempts_invalid")

    @property
    def subject_id(self) -> str:
        return f"commit:{self.commit_sha}"

    @property
    def subject_hash(self) -> str:
        return _hash(
            {
                "project_id": self.project_id,
                "commit_sha": self.commit_sha,
                "scan_revision": self.scan_revision,
                "request_scope_id": self.request_scope_id,
                "shield_layers": self.shield_layers,
            }
        )

    @property
    def dedupe_key(self) -> str:
        return _hash(
            {
                "project_id": self.project_id,
                "commit_sha": self.commit_sha,
                "scan_revision": self.scan_revision,
                "request_scope_id": self.request_scope_id,
                "provider_identity": self.provider_identity,
                "shield_layers": self.shield_layers,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "commit_sha": self.commit_sha,
            "scan_revision": self.scan_revision,
            "request_scope_id": self.request_scope_id,
            "shield_layers": list(self.shield_layers),
            "provider_identity": self.provider_identity,
            "changed_paths_hash": self.changed_paths_hash,
        }


@dataclass(frozen=True)
class SecurityFindingVersion:
    version_id: str
    finding_id: str
    project_id: str
    parent_version_id: str
    created_at: str
    finding: SecurityFinding


@dataclass(frozen=True)
class ShadowGenerationBinding:
    generation_id: str
    project_id: str
    status: str
    manifest_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ShadowCanaryReceipt:
    """Project-bound benchmark evidence for one shadow canary window."""

    receipt_id: str
    candidate_id: str
    project_id: str
    generation_id: str
    manifest_hash: str
    benchmark_corpus_sha256: str
    benchmark_cases_sha256: str
    language_split: tuple[str, ...]
    embedding_identity: str
    metrics: dict[str, float | int]
    observed_from: str
    observed_until: str
    created_at: str
    environment_fingerprint: str = ""
    pricing_revision: str = ""
    dependency_digest: str = ""
    source_fingerprint: str = ""
    cost_currency: str = ""


@dataclass(frozen=True)
class RemediationPatternCandidate:
    """A redacted, non-canonical pattern awaiting independent validation."""

    candidate_id: str
    project_id: str
    finding_id: str
    source_version_id: str
    redacted_pattern: str
    severity: str
    status: str
    validation_project_ids: tuple[str, ...]
    shadow_generation: str
    created_at: str
    updated_at: str
    shadow_proposal_id: str = ""
    canary_result: str = ""
    canary_reason: str = ""
    canary_evidence: dict[str, object] = field(default_factory=dict)


class ShieldScanStore:
    """Project-scoped Shield queue and SQLite finding-version repository."""

    def __init__(self, db_path: str | Path, *, derived_work: DerivedWorkStore | None = None):
        self._db_path = Path(db_path)
        self._derived_work = derived_work or DerivedWorkStore(self._db_path)
        self._initialize_findings()

    @property
    def derived_work(self) -> DerivedWorkStore:
        return self._derived_work

    def enqueue(self, request: ShieldScanRequest) -> DerivedWorkCreateResult:
        return self._derived_work.enqueue(
            project_id=_scope_required(request.project_id),
            visibility="project",
            config_revision=request.scan_revision,
            job_kind=_SHIELD_JOB_KIND,
            provider_identity=request.provider_identity,
            subject_id=request.subject_id,
            subject_hash=request.subject_hash,
            dedupe_key=request.dedupe_key,
            payload=request.payload(),
            priority=request.priority,
            max_attempts=request.max_attempts,
        )

    def claim_batch(
        self,
        *,
        project_id: str | None = None,
        limit: int = _DEFAULT_BATCH_SIZE,
        min_batch_size: int = 1,
        max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS,
        lease_seconds: int | None = None,
    ) -> tuple[DerivedWorkLease, ...]:
        if project_id is not None:
            _scope_required(project_id)
        return self._derived_work.claim_batch(
            project_id=project_id,
            visibility="project",
            job_kind=_SHIELD_JOB_KIND,
            limit=limit,
            min_batch_size=min_batch_size,
            max_wait_seconds=max_wait_seconds,
            lease_seconds=lease_seconds,
        )

    def fail(
        self,
        lease: DerivedWorkLease,
        *,
        failure_code: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
    ) -> DerivedWorkJob:
        return self._derived_work.fail(
            job_id=lease.job.job_id,
            project_id=lease.job.project_id,
            lease_token=lease.lease_token,
            fencing_generation=lease.job.fencing_generation,
            failure_code=failure_code,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )

    def complete_scan(
        self,
        lease: DerivedWorkLease,
        findings: Iterable[SecurityFinding],
    ) -> DerivedWorkJob:
        finding_list = tuple(findings)
        payload = dict(lease.job.payload)
        expected_project = _scope_required(lease.job.project_id)
        expected_commit = _text(payload.get("commit_sha"))
        expected_revision = _text(payload.get("scan_revision"))
        expected_scope = _text(payload.get("request_scope_id"))
        if not expected_scope:
            raise ValueError("shield_scan_request_scope_missing")
        for finding in finding_list:
            if finding.project_id != expected_project:
                raise ValueError("shield_finding_project_scope_mismatch")
            if finding.commit_sha != expected_commit:
                raise ValueError("shield_finding_commit_mismatch")
            if finding.scan_revision != expected_revision:
                raise ValueError("shield_finding_scan_revision_mismatch")
            if finding.request_scope_id != expected_scope:
                raise ValueError("shield_finding_request_scope_mismatch")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            versions = []
            for finding in finding_list:
                version_id = self._insert_finding_version(
                    conn,
                    finding,
                    parent_version_id="",
                )
                versions.append(version_id)
            completed = self._derived_work.complete_in_transaction(
                conn,
                job_id=lease.job.job_id,
                project_id=expected_project,
                lease_token=lease.lease_token,
                fencing_generation=lease.job.fencing_generation,
                result={"finding_version_ids": versions},
            )
            conn.commit()
            return completed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_version(
        self,
        finding: SecurityFinding,
        *,
        parent_version_id: str = "",
    ) -> SecurityFindingVersion:
        project_id = _scope_required(finding.project_id)
        normalized_parent = _text(parent_version_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            version_id = self._insert_finding_version(
                conn,
                finding,
                parent_version_id=normalized_parent,
            )
            conn.commit()
            return SecurityFindingVersion(
                version_id=version_id,
                finding_id=finding.finding_id,
                project_id=project_id,
                parent_version_id=normalized_parent,
                created_at=self._version_created_at(conn, version_id),
                finding=finding,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_rescan(
        self,
        *,
        project_id: str,
        parent_version_id: str,
        commit_sha: str,
        scan_revision: str,
        request_scope_id: str,
        finding_present: bool,
        evidence: Mapping[str, object] | None = None,
    ) -> SecurityFindingVersion:
        """Record remediation closure or recurrence from fresh scan evidence.

        A rescan is always a new finding version.  Only a clean rescan can
        produce ``resolved``; a still-present finding becomes ``recurring``.
        The parent, project, commit, scan revision, and request scope are all
        checked before the version is committed.
        """

        normalized_project = _scope_required(project_id)
        normalized_parent = _text(parent_version_id)
        normalized_commit = _text(commit_sha)
        normalized_scan = _text(scan_revision)
        normalized_scope = _text(request_scope_id)
        if not normalized_parent:
            raise ValueError("finding_parent_version_required")
        if not normalized_commit:
            raise ValueError("rescan_commit_sha_required")
        if not normalized_scan:
            raise ValueError("rescan_scan_revision_required")
        if not normalized_scope:
            raise ValueError("rescan_request_scope_id_required")
        if not isinstance(finding_present, bool):
            raise ValueError("rescan_finding_present_invalid")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            parent_row = conn.execute(
                "SELECT * FROM security_finding_versions WHERE version_id = ?",
                (normalized_parent,),
            ).fetchone()
            if parent_row is None:
                raise ValueError("finding_parent_not_found")
            if parent_row["project_id"] != normalized_project:
                raise ValueError("finding_parent_scope_mismatch")

            parent = self._row_to_version(parent_row)
            if not _scan_revision_is_newer(
                parent.finding.scan_revision,
                normalized_scan,
            ):
                raise ValueError("rescan_scan_revision_not_advanced")
            transition = "recurring" if finding_present else "resolved"
            additions = {
                **dict(evidence or {}),
                "commit_sha": normalized_commit,
                "scan_revision": normalized_scan,
                "request_scope_id": normalized_scope,
                "finding_present": finding_present,
            }
            if not finding_present:
                additions["rescan_passed"] = True
            next_finding = parent.finding.transition(transition, evidence=additions)
            version_id = self._insert_finding_version(
                conn,
                next_finding,
                parent_version_id=normalized_parent,
            )
            created_at = self._version_created_at(conn, version_id)
            conn.commit()
            return SecurityFindingVersion(
                version_id=version_id,
                finding_id=next_finding.finding_id,
                project_id=normalized_project,
                parent_version_id=normalized_parent,
                created_at=created_at,
                finding=next_finding,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_remediation_candidate(
        self,
        *,
        project_id: str,
        source_version_id: str,
    ) -> RemediationPatternCandidate:
        """Create an isolated candidate from a clean, low-risk rescan.

        This writes only the security-candidate ledger.  It never creates a
        canonical memory or widens visibility beyond the originating project.
        """

        normalized_project = _scope_required(project_id)
        normalized_source = _text(source_version_id)
        if not normalized_source:
            raise ValueError("remediation_pattern_source_required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM security_finding_versions WHERE version_id = ?",
                (normalized_source,),
            ).fetchone()
            if row is None:
                raise ValueError("remediation_pattern_source_not_found")
            if row["project_id"] != normalized_project:
                raise ValueError("remediation_pattern_source_scope_mismatch")
            finding = self._row_to_version(row).finding
            pattern = _text(finding.remediation_pattern)
            if not pattern:
                raise ValueError("remediation_pattern_missing")
            if finding.security_state != "resolved":
                raise ValueError("remediation_pattern_source_unresolved")
            if finding.evidence.get("rescan_passed") is not True:
                raise ValueError("remediation_pattern_rescan_evidence_required")
            if finding.severity not in {"info", "low", "medium"}:
                raise ValueError("remediation_pattern_risk_too_high")
            if finding.freshness_state not in {"fresh", "aging"}:
                raise ValueError("remediation_pattern_stale")

            candidate_id = (
                "rpc_"
                + hashlib.sha256(
                    _json(
                        {
                            "project_id": normalized_project,
                            "source_version_id": normalized_source,
                            "pattern": pattern,
                        }
                    ).encode("utf-8")
                ).hexdigest()
            )
            now = _utc_now()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS security_remediation_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    source_version_id TEXT NOT NULL,
                    redacted_pattern TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation_projects_json TEXT NOT NULL,
                    shadow_generation TEXT NOT NULL DEFAULT '',
                    shadow_proposal_id TEXT NOT NULL DEFAULT '',
                    canary_result TEXT NOT NULL DEFAULT '',
                    canary_reason TEXT NOT NULL DEFAULT '',
                    canary_evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, source_version_id)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO security_remediation_candidates (
                    candidate_id, project_id, finding_id, source_version_id,
                    redacted_pattern, severity, status,
                    validation_projects_json, shadow_generation, shadow_proposal_id,
                    canary_result, canary_reason, canary_evidence_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_validation', ?, '', '', '', '', '{}', ?, ?)
                """,
                (
                    candidate_id,
                    normalized_project,
                    finding.finding_id,
                    normalized_source,
                    pattern,
                    finding.severity,
                    _json([normalized_project]),
                    now,
                    now,
                ),
            )
            candidate = self._candidate_row(
                conn.execute(
                    "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
            )
            conn.commit()
            return candidate
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_remediation_validation(
        self,
        *,
        candidate_id: str,
        validation_project_id: str,
        validation_version_id: str,
    ) -> RemediationPatternCandidate:
        """Attach one independently validated project to a candidate."""

        normalized_candidate = _text(candidate_id)
        normalized_project = _scope_required(validation_project_id)
        normalized_version = _text(validation_version_id)
        if not normalized_candidate:
            raise ValueError("remediation_candidate_required")
        if not normalized_version:
            raise ValueError("remediation_validation_version_required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                (normalized_candidate,),
            ).fetchone()
            if candidate is None:
                raise ValueError("remediation_candidate_not_found")
            if candidate["status"] != "pending_validation":
                raise ValueError("remediation_candidate_not_pending")
            source_project = str(candidate["project_id"])
            if normalized_project == source_project:
                raise ValueError("remediation_validation_not_independent")
            validation = conn.execute(
                "SELECT * FROM security_finding_versions WHERE version_id = ?",
                (normalized_version,),
            ).fetchone()
            if validation is None:
                raise ValueError("remediation_validation_version_not_found")
            if validation["project_id"] != normalized_project:
                raise ValueError("remediation_validation_scope_mismatch")
            if validation["finding_id"] != candidate["finding_id"]:
                raise ValueError("remediation_validation_finding_mismatch")
            validation_finding = self._row_to_version(validation).finding
            if validation_finding.security_state != "resolved":
                raise ValueError("remediation_validation_unresolved")
            if validation_finding.evidence.get("rescan_passed") is not True:
                raise ValueError("remediation_validation_rescan_required")
            if validation_finding.remediation_pattern != candidate["redacted_pattern"]:
                raise ValueError("remediation_validation_pattern_mismatch")

            projects = list(self._candidate_row(candidate).validation_project_ids)
            if normalized_project not in projects:
                projects.append(normalized_project)
                projects.sort()
                conn.execute(
                    "UPDATE security_remediation_candidates SET "
                    "validation_projects_json = ?, updated_at = ? "
                    "WHERE candidate_id = ?",
                    (_json(projects), _utc_now(), normalized_candidate),
                )
            result = self._candidate_row(
                conn.execute(
                    "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                    (normalized_candidate,),
                ).fetchone()
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_shadow_generation(
        self,
        *,
        project_id: str,
        generation_id: str,
        manifest_hash: str = "",
    ) -> ShadowGenerationBinding:
        """Register an offline shadow generation under one project scope."""

        normalized_project = _scope_required(project_id)
        normalized_generation = _text(generation_id)
        if not normalized_generation:
            raise ValueError("remediation_shadow_generation_required")
        if len(normalized_generation) > 128:
            raise ValueError("remediation_shadow_generation_invalid")
        _validate_scan_identity(
            normalized_generation,
            code_prefix="remediation_shadow_generation",
        )
        normalized_manifest = _text(manifest_hash)
        if normalized_manifest and not re.fullmatch(r"[0-9a-f]{64}", normalized_manifest):
            raise ValueError("remediation_shadow_generation_manifest_invalid")
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM security_shadow_generations WHERE generation_id = ?",
                (normalized_generation,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != normalized_project
                    or str(existing["manifest_hash"] or "") != normalized_manifest
                ):
                    raise ValueError("remediation_shadow_generation_scope_mismatch")
                result = self._shadow_generation_row(existing)
                conn.commit()
                return result
            conn.execute(
                "INSERT INTO security_shadow_generations "
                "(generation_id, project_id, status, manifest_hash, created_at, updated_at) "
                "VALUES (?, ?, 'shadow', ?, ?, ?)",
                (
                    normalized_generation,
                    normalized_project,
                    normalized_manifest,
                    now,
                    now,
                ),
            )
            result = self._shadow_generation_row(
                conn.execute(
                    "SELECT * FROM security_shadow_generations WHERE generation_id = ?",
                    (normalized_generation,),
                ).fetchone()
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_shadow_generation_manifest(
        self,
        *,
        project_id: str,
        manifest: Mapping[str, object],
    ) -> ShadowGenerationBinding:
        """Bind a completed LanceDB manifest to one project's shadow registry.

        The generation manager remains the authority for artifact verification;
        this method records only the verified manifest identity and refuses
        incomplete or unsealed manifest payloads at the security boundary.
        """

        if not isinstance(manifest, Mapping):
            raise ValueError("remediation_shadow_manifest_invalid")
        generation_id = _text(manifest.get("generation_id"))
        manifest_hash = _text(manifest.get("manifest_sha256"))
        if _text(manifest.get("build_status")) != "complete":
            raise ValueError("remediation_shadow_manifest_not_complete")
        if _text(manifest.get("verification_status")) not in {"unverified", "verified"}:
            raise ValueError("remediation_shadow_manifest_invalid")
        if not generation_id or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
            raise ValueError("remediation_shadow_manifest_hash_required")
        return self.register_shadow_generation(
            project_id=project_id,
            generation_id=generation_id,
            manifest_hash=manifest_hash,
        )

    def record_generation_quality_canary(
        self,
        *,
        candidate_id: str,
        manifest: Mapping[str, object],
        conflict_rate: float,
        passed: bool,
        reason: str = "",
    ) -> RemediationPatternCandidate:
        """Adapt one sealed generation quality report into a canary receipt."""

        if not isinstance(manifest, Mapping):
            raise ValueError("remediation_generation_quality_manifest_invalid")
        if _text(manifest.get("build_status")) != "complete":
            raise ValueError("remediation_generation_quality_manifest_not_complete")
        generation_id = _text(manifest.get("generation_id"))
        manifest_hash = _text(manifest.get("manifest_sha256"))
        if not generation_id or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
            raise ValueError("remediation_generation_quality_manifest_invalid")
        report = manifest.get("quality_report")
        if not isinstance(report, Mapping):
            raise ValueError("remediation_generation_quality_report_missing")
        if passed and (
            report.get("degraded") is not False or report.get("publishable_claim") is not True
        ):
            raise ValueError("remediation_generation_quality_report_not_publishable")
        gate = report.get("gate")
        if not isinstance(gate, Mapping):
            raise ValueError("remediation_generation_quality_gate_missing")
        if passed and _text(gate.get("status")) != "pass":
            raise ValueError("remediation_generation_quality_gate_failed")

        corpus = report.get("corpus")
        cases = report.get("cases")
        backend = report.get("backend")
        metrics = report.get("metrics")
        usage = report.get("usage")
        environment = report.get("environment")
        if not all(
            isinstance(value, Mapping)
            for value in (corpus, cases, backend, metrics, usage, environment)
        ):
            raise ValueError("remediation_generation_quality_report_invalid")
        corpus_hash = _text(corpus.get("sha256"))
        cases_hash = _text(cases.get("sha256"))
        if corpus_hash != _text(manifest.get("benchmark_corpus_sha256")) or cases_hash != _text(
            manifest.get("benchmark_cases_sha256")
        ):
            raise ValueError("remediation_generation_quality_benchmark_mismatch")
        if (
            _text(backend.get("model")) != _text(manifest.get("embedding_model"))
            or _text(backend.get("revision")) != _text(manifest.get("model_revision"))
            or backend.get("dimension") != manifest.get("embedding_dimension")
        ):
            raise ValueError("remediation_generation_quality_embedding_mismatch")

        language_metrics = metrics.get("language")
        if not isinstance(language_metrics, Mapping) or not {"en", "zh"}.issubset(
            {_text(key).casefold() for key in language_metrics}
        ):
            raise ValueError("remediation_generation_quality_language_split_invalid")
        hit_at = metrics.get("hit_at")
        if not isinstance(hit_at, Mapping):
            raise ValueError("remediation_generation_quality_metrics_invalid")
        cost_currency = _text(usage.get("cost_currency"))
        cost_usd = usage.get("cost_usd")
        if cost_usd is None:
            if cost_currency.upper() == "USD":
                cost_usd = usage.get("cost")
            else:
                raise ValueError("remediation_generation_quality_cost_currency_unsupported")
        if cost_currency and cost_currency.upper() not in {"USD", "CNY"}:
            raise ValueError("remediation_generation_quality_pricing_invalid")
        if not cost_currency:
            cost_currency = "USD"
        environment_fingerprint = _text(
            environment.get("comparison_environment_fingerprint")
            or environment.get("environment_fingerprint")
        )
        pricing_revision = _text(usage.get("pricing_revision"))
        dependency_digest = _text(environment.get("dependencies_sha256"))
        source_fingerprint = _text(
            environment.get("source_fingerprint")
            or (
                (manifest.get("index_outbox") or {}).get("source_fingerprint")
                if isinstance(manifest.get("index_outbox"), Mapping)
                else ""
            )
        )
        if not re.fullmatch(r"[0-9a-f]{64}", environment_fingerprint):
            raise ValueError("remediation_generation_quality_environment_invalid")
        if not pricing_revision or len(pricing_revision) > 128:
            raise ValueError("remediation_generation_quality_pricing_invalid")
        for value, field_name in (
            (dependency_digest, "dependency_digest"),
            (source_fingerprint, "source_fingerprint"),
        ):
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"remediation_generation_quality_{field_name}_invalid")
        try:
            normalized_conflict_rate = float(conflict_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("remediation_generation_quality_conflict_rate_invalid") from exc
        if (
            not math.isfinite(normalized_conflict_rate)
            or not 0.0 <= normalized_conflict_rate <= 1.0
        ):
            raise ValueError("remediation_generation_quality_conflict_rate_invalid")
        receipt_metrics = {
            "hit_at_1": hit_at.get("1"),
            "hit_at_5": hit_at.get("5"),
            "mrr": metrics.get("mrr"),
            "p95_ms": metrics.get("p95_ms"),
            "forbidden_hit_rate": metrics.get("forbidden_hit_rate"),
            "conflict_rate": normalized_conflict_rate,
            "cost_usd": cost_usd,
            "sample_count": metrics.get("case_count"),
        }
        normalized_metrics = _validate_receipt_metrics(receipt_metrics)
        if passed and float(normalized_metrics["forbidden_hit_rate"]) > 0.0:
            raise ValueError("remediation_generation_quality_forbidden_hit")
        observed_from = manifest.get("created_at")
        observed_until = manifest.get("completed_at")
        embedding_identity = _text(
            (manifest.get("index_outbox") or {}).get("embedding_index_identity")
            if isinstance(manifest.get("index_outbox"), Mapping)
            else ""
        )
        if not embedding_identity:
            embedding_identity = ":".join(
                (
                    _text(manifest.get("embedding_model")),
                    _text(manifest.get("model_revision")),
                    _text(manifest.get("embedding_dimension")),
                )
            )
        return self.record_shadow_canary(
            candidate_id=candidate_id,
            passed=passed,
            reason=reason,
            evidence={
                "quality_gate_status": _text(gate.get("status")),
                "manifest_bound": True,
                "benchmark_corpus_sha256": corpus_hash,
                "benchmark_cases_sha256": cases_hash,
            },
            receipt={
                "generation_id": generation_id,
                "manifest_hash": manifest_hash,
                "benchmark_corpus_sha256": corpus_hash,
                "benchmark_cases_sha256": cases_hash,
                "language_split": ["en", "zh"],
                "embedding_identity": embedding_identity,
                "metrics": normalized_metrics,
                "observed_from": observed_from,
                "observed_until": observed_until,
                "environment_fingerprint": environment_fingerprint,
                "pricing_revision": pricing_revision,
                "dependency_digest": dependency_digest,
                "source_fingerprint": source_fingerprint,
                "cost_currency": cost_currency.upper(),
            },
        )

    def shadow_promote_remediation_candidate(
        self,
        *,
        candidate_id: str,
        shadow_generation: str,
    ) -> RemediationPatternCandidate:
        """Mark a validated pattern as shadow-promotable without canonical write."""

        normalized_candidate = _text(candidate_id)
        normalized_generation = _text(shadow_generation)
        if not normalized_candidate:
            raise ValueError("remediation_candidate_required")
        if not normalized_generation:
            raise ValueError("remediation_shadow_generation_required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                (normalized_candidate,),
            ).fetchone()
            if row is None:
                raise ValueError("remediation_candidate_not_found")
            if row["status"] != "pending_validation":
                raise ValueError("remediation_candidate_not_pending")
            if len(self._candidate_row(row).validation_project_ids) < 2:
                raise ValueError("remediation_validation_required")
            generation = conn.execute(
                "SELECT project_id, status FROM security_shadow_generations "
                "WHERE generation_id = ?",
                (normalized_generation,),
            ).fetchone()
            if generation is None:
                raise ValueError("remediation_shadow_generation_not_found")
            if str(generation["project_id"]) != str(row["project_id"]):
                raise ValueError("remediation_shadow_generation_scope_mismatch")
            if str(generation["status"]) != "shadow":
                raise ValueError("remediation_shadow_generation_not_shadow")
            conn.execute(
                "UPDATE security_remediation_candidates SET status = 'shadowed', "
                "shadow_generation = ?, updated_at = ? WHERE candidate_id = ?",
                (normalized_generation, _utc_now(), normalized_candidate),
            )
            result = self._candidate_row(
                conn.execute(
                    "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                    (normalized_candidate,),
                ).fetchone()
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _normalize_shadow_canary_receipt(
        conn: sqlite3.Connection,
        candidate: sqlite3.Row,
        receipt: Mapping[str, object],
        *,
        allow_closed_generation: bool = False,
    ) -> dict[str, object]:
        if not isinstance(receipt, Mapping):
            raise ValueError("remediation_canary_receipt_invalid")
        generation_id = _text(receipt.get("generation_id"))
        manifest_hash = _text(receipt.get("manifest_hash"))
        if not generation_id or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
            raise ValueError("remediation_canary_receipt_manifest_invalid")
        if generation_id != _text(candidate["shadow_generation"]):
            raise ValueError("remediation_canary_receipt_generation_mismatch")
        generation = conn.execute(
            "SELECT project_id, status, manifest_hash FROM security_shadow_generations "
            "WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if generation is None:
            raise ValueError("remediation_canary_receipt_generation_not_found")
        if str(generation["project_id"]) != str(candidate["project_id"]):
            raise ValueError("remediation_canary_receipt_scope_mismatch")
        allowed_generation_statuses = {"shadow"}
        if allow_closed_generation:
            allowed_generation_statuses.update({"canary_passed", "rolled_back"})
        if str(generation["status"]) not in allowed_generation_statuses:
            raise ValueError("remediation_canary_receipt_generation_not_shadow")
        if str(generation["manifest_hash"] or "") != manifest_hash:
            raise ValueError("remediation_canary_receipt_manifest_mismatch")

        corpus_hash = _text(receipt.get("benchmark_corpus_sha256"))
        cases_hash = _text(receipt.get("benchmark_cases_sha256"))
        if not re.fullmatch(r"[0-9a-f]{64}", corpus_hash) or not re.fullmatch(
            r"[0-9a-f]{64}", cases_hash
        ):
            raise ValueError("remediation_canary_receipt_benchmark_invalid")
        raw_languages = receipt.get("language_split")
        if not isinstance(raw_languages, (list, tuple, set)):
            raise ValueError("remediation_canary_receipt_language_split_invalid")
        languages = tuple(sorted({_text(item).casefold() for item in raw_languages if _text(item)}))
        if languages != ("en", "zh"):
            raise ValueError("remediation_canary_receipt_language_split_invalid")
        embedding_identity = _text(receipt.get("embedding_identity"))
        if not embedding_identity or len(embedding_identity) > 256:
            raise ValueError("remediation_canary_receipt_embedding_invalid")
        _validate_scan_identity(embedding_identity, code_prefix="remediation_canary_receipt")
        environment_fingerprint = _text(receipt.get("environment_fingerprint"))
        pricing_revision = _text(receipt.get("pricing_revision"))
        dependency_digest = _text(receipt.get("dependency_digest"))
        source_fingerprint = _text(receipt.get("source_fingerprint"))
        cost_currency = _text(receipt.get("cost_currency")).upper()
        for value, field_name in (
            (environment_fingerprint, "environment"),
            (pricing_revision, "pricing"),
            (dependency_digest, "dependency_digest"),
            (source_fingerprint, "source_fingerprint"),
        ):
            if len(value) > 256:
                raise ValueError(f"remediation_canary_receipt_{field_name}_invalid")
            if value:
                _validate_scan_identity(value, code_prefix="remediation_canary_receipt")
        if dependency_digest and not re.fullmatch(r"[0-9a-f]{64}", dependency_digest):
            raise ValueError("remediation_canary_receipt_dependency_digest_invalid")
        if source_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
            raise ValueError("remediation_canary_receipt_source_fingerprint_invalid")
        if cost_currency and cost_currency not in {"USD", "CNY"}:
            raise ValueError("remediation_canary_receipt_cost_currency_invalid")
        metrics = _validate_receipt_metrics(receipt.get("metrics"))
        observed_from = _parse_receipt_timestamp(
            receipt.get("observed_from"),
            field_name="observed_from",
        )
        observed_until = _parse_receipt_timestamp(
            receipt.get("observed_until"),
            field_name="observed_until",
        )
        start = datetime.fromisoformat(observed_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(observed_until.replace("Z", "+00:00"))
        if end < start:
            raise ValueError("remediation_canary_receipt_window_invalid")
        binding = {
            "candidate_id": str(candidate["candidate_id"]),
            "project_id": str(candidate["project_id"]),
            "generation_id": generation_id,
            "manifest_hash": manifest_hash,
            "benchmark_corpus_sha256": corpus_hash,
            "benchmark_cases_sha256": cases_hash,
            "language_split": list(languages),
            "embedding_identity": embedding_identity,
            "metrics": metrics,
            "observed_from": observed_from,
            "observed_until": observed_until,
            "environment_fingerprint": environment_fingerprint,
            "pricing_revision": pricing_revision,
            "dependency_digest": dependency_digest,
            "source_fingerprint": source_fingerprint,
            "cost_currency": cost_currency,
        }
        binding["receipt_id"] = "scr_" + hashlib.sha256(_json(binding).encode("utf-8")).hexdigest()
        return binding

    def record_shadow_canary(
        self,
        *,
        candidate_id: str,
        passed: bool,
        reason: str = "",
        evidence: Mapping[str, object] | None = None,
        receipt: Mapping[str, object] | None = None,
    ) -> RemediationPatternCandidate:
        """Close a shadow canary and fail closed on a negative result."""

        normalized_candidate = _text(candidate_id)
        if not normalized_candidate:
            raise ValueError("remediation_candidate_required")
        if not isinstance(passed, bool):
            raise ValueError("remediation_canary_result_invalid")
        normalized_reason = _text(reason)[:256]
        if not passed and not normalized_reason:
            raise ValueError("remediation_canary_failure_reason_required")
        raw_evidence = dict(evidence or {})
        if _contains_secret_value(normalized_reason) or _contains_secret_value(raw_evidence):
            raise ValueError("remediation_canary_secret_detected")
        if _contains_unredacted_material(normalized_reason) or _contains_unredacted_material(
            raw_evidence
        ):
            raise ValueError("remediation_canary_unredacted_material")
        if len(raw_evidence) > 16 or any(
            not isinstance(key, str) or not isinstance(value, (int, float, bool, str))
            for key, value in raw_evidence.items()
        ):
            raise ValueError("remediation_canary_evidence_invalid")
        if any(len(str(key)) > 64 or len(str(value)) > 256 for key, value in raw_evidence.items()):
            raise ValueError("remediation_canary_evidence_invalid")
        bounded_metrics = {
            "hit_at",
            "mrr",
            "forbidden_hit_rate",
            "p95_ms",
            "cost_usd",
            "sample_count",
        }
        for key, value in raw_evidence.items():
            if key not in bounded_metrics:
                continue
            if isinstance(value, bool):
                raise ValueError("remediation_canary_metric_invalid")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("remediation_canary_metric_invalid") from exc
            if key in {"hit_at", "mrr", "forbidden_hit_rate"} and not 0.0 <= number <= 1.0:
                raise ValueError("remediation_canary_metric_invalid")
            if key in {"p95_ms", "cost_usd", "sample_count"} and number < 0.0:
                raise ValueError("remediation_canary_metric_invalid")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                (normalized_candidate,),
            ).fetchone()
            if row is None:
                raise ValueError("remediation_candidate_not_found")
            expected_status = "canary_passed" if passed else "rolled_back"
            if row["status"] != "shadowed":
                if row["status"] != expected_status:
                    raise ValueError("remediation_candidate_not_shadowed")
                if (
                    str(row["canary_reason"] or "") != normalized_reason
                    or json.loads(str(row["canary_evidence_json"] or "{}")) != raw_evidence
                ):
                    raise ValueError("remediation_canary_replay_conflict")
                if receipt is None:
                    conn.commit()
                    return self._candidate_row(row)
                normalized_receipt = self._normalize_shadow_canary_receipt(
                    conn,
                    row,
                    receipt,
                    allow_closed_generation=True,
                )
                stored_receipt = conn.execute(
                    "SELECT receipt_id FROM security_shadow_canary_receipts "
                    "WHERE candidate_id = ? AND receipt_id = ?",
                    (normalized_candidate, normalized_receipt["receipt_id"]),
                ).fetchone()
                if stored_receipt is None:
                    raise ValueError("remediation_canary_replay_conflict")
                conn.commit()
                return self._candidate_row(row)
            normalized_receipt = None
            if receipt is not None:
                normalized_receipt = self._normalize_shadow_canary_receipt(
                    conn,
                    row,
                    receipt,
                )
            status = "canary_passed" if passed else "rolled_back"
            result = "passed" if passed else "failed"
            if normalized_receipt is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO security_shadow_canary_receipts ("
                    "receipt_id, candidate_id, project_id, generation_id, manifest_hash, "
                    "benchmark_corpus_sha256, benchmark_cases_sha256, language_split_json, "
                    "embedding_identity, metrics_json, observed_from, observed_until, "
                    "environment_fingerprint, pricing_revision, dependency_digest, "
                    "source_fingerprint, cost_currency, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        normalized_receipt["receipt_id"],
                        normalized_receipt["candidate_id"],
                        normalized_receipt["project_id"],
                        normalized_receipt["generation_id"],
                        normalized_receipt["manifest_hash"],
                        normalized_receipt["benchmark_corpus_sha256"],
                        normalized_receipt["benchmark_cases_sha256"],
                        _json(normalized_receipt["language_split"]),
                        normalized_receipt["embedding_identity"],
                        _json(normalized_receipt["metrics"]),
                        normalized_receipt["observed_from"],
                        normalized_receipt["observed_until"],
                        normalized_receipt["environment_fingerprint"],
                        normalized_receipt["pricing_revision"],
                        normalized_receipt["dependency_digest"],
                        normalized_receipt["source_fingerprint"],
                        normalized_receipt["cost_currency"],
                        _utc_now(),
                    ),
                )
            conn.execute(
                "UPDATE security_remediation_candidates SET status = ?, "
                "canary_result = ?, canary_reason = ?, canary_evidence_json = ?, "
                "updated_at = ? WHERE candidate_id = ?",
                (
                    status,
                    result,
                    normalized_reason,
                    _json(raw_evidence),
                    _utc_now(),
                    normalized_candidate,
                ),
            )
            conn.execute(
                "UPDATE security_shadow_generations SET status = ?, updated_at = ? "
                "WHERE generation_id = (SELECT shadow_generation FROM "
                "security_remediation_candidates WHERE candidate_id = ?)",
                (status, _utc_now(), normalized_candidate),
            )
            projected = self._candidate_row(
                conn.execute(
                    "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                    (normalized_candidate,),
                ).fetchone()
            )
            conn.commit()
            return projected
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def require_production_canary_receipt(
        self,
        *,
        candidate_id: str,
        project_id: str,
        generation_id: str,
        manifest_hash: str,
    ) -> ShadowCanaryReceipt:
        """Return a complete receipt or fail closed before production promotion.

        The legacy ``record_shadow_canary(..., receipt=None)`` path remains
        available for old local Shield tests.  Production callers must use
        this gate, which requires a project-owned, canary-passed candidate and
        generation plus complete environment/cost provenance and zero
        forbidden hits.
        """

        normalized_project = _scope_required(project_id)
        normalized_candidate = _text(candidate_id)
        normalized_generation = _text(generation_id)
        normalized_manifest = _text(manifest_hash)
        if not normalized_candidate:
            raise ValueError("production_canary_candidate_required")
        if not normalized_generation:
            raise ValueError("production_canary_generation_required")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_manifest):
            raise ValueError("production_canary_manifest_hash_invalid")
        conn = self._connect()
        try:
            candidate = conn.execute(
                "SELECT project_id, status, shadow_generation FROM "
                "security_remediation_candidates WHERE candidate_id = ?",
                (normalized_candidate,),
            ).fetchone()
            if candidate is None:
                raise ValueError("production_canary_candidate_not_found")
            if str(candidate[0]) != normalized_project:
                raise ValueError("production_canary_project_mismatch")
            if str(candidate[1]) != "canary_passed":
                raise ValueError("production_canary_candidate_not_passed")
            if str(candidate[2]) != normalized_generation:
                raise ValueError("production_canary_generation_mismatch")
            generation = conn.execute(
                "SELECT project_id, status, manifest_hash FROM security_shadow_generations "
                "WHERE generation_id = ?",
                (normalized_generation,),
            ).fetchone()
            if generation is None:
                raise ValueError("production_canary_generation_not_found")
            if str(generation[0]) != normalized_project:
                raise ValueError("production_canary_generation_project_mismatch")
            if str(generation[1]) != "canary_passed":
                raise ValueError("production_canary_generation_not_passed")
            if str(generation[2] or "") != normalized_manifest:
                raise ValueError("production_canary_manifest_mismatch")
            receipt = conn.execute(
                "SELECT * FROM security_shadow_canary_receipts "
                "WHERE candidate_id = ? AND project_id = ? AND generation_id = ? "
                "AND manifest_hash = ? ORDER BY created_at DESC LIMIT 1",
                (
                    normalized_candidate,
                    normalized_project,
                    normalized_generation,
                    normalized_manifest,
                ),
            ).fetchone()
            if receipt is None:
                raise ValueError("production_canary_receipt_required")
            required = (
                ("environment_fingerprint", receipt["environment_fingerprint"]),
                ("pricing_revision", receipt["pricing_revision"]),
                ("dependency_digest", receipt["dependency_digest"]),
                ("source_fingerprint", receipt["source_fingerprint"]),
                ("cost_currency", receipt["cost_currency"]),
            )
            if any(not _text(value) for _, value in required):
                raise ValueError("production_canary_receipt_provenance_incomplete")
            metrics = _validate_receipt_metrics(json.loads(str(receipt["metrics_json"])))
            if float(metrics["forbidden_hit_rate"]) != 0.0:
                raise ValueError("production_canary_forbidden_hit")
            return ShadowCanaryReceipt(
                receipt_id=str(receipt["receipt_id"]),
                candidate_id=str(receipt["candidate_id"]),
                project_id=str(receipt["project_id"]),
                generation_id=str(receipt["generation_id"]),
                manifest_hash=str(receipt["manifest_hash"]),
                benchmark_corpus_sha256=str(receipt["benchmark_corpus_sha256"]),
                benchmark_cases_sha256=str(receipt["benchmark_cases_sha256"]),
                language_split=tuple(json.loads(str(receipt["language_split_json"]))),
                embedding_identity=str(receipt["embedding_identity"]),
                metrics=metrics,
                observed_from=str(receipt["observed_from"]),
                observed_until=str(receipt["observed_until"]),
                created_at=str(receipt["created_at"]),
                environment_fingerprint=str(receipt["environment_fingerprint"] or ""),
                pricing_revision=str(receipt["pricing_revision"] or ""),
                dependency_digest=str(receipt["dependency_digest"] or ""),
                source_fingerprint=str(receipt["source_fingerprint"] or ""),
                cost_currency=str(receipt["cost_currency"] or ""),
            )
        finally:
            conn.close()

    def attach_shadow_proposal(
        self,
        *,
        candidate_id: str,
        proposal_id: str,
    ) -> RemediationPatternCandidate:
        """Attach a pending proposal projection without promoting memory."""

        normalized_candidate = _text(candidate_id)
        normalized_proposal = _text(proposal_id)
        if not normalized_candidate:
            raise ValueError("remediation_candidate_required")
        if not normalized_proposal:
            raise ValueError("remediation_shadow_proposal_required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                (normalized_candidate,),
            ).fetchone()
            if row is None:
                raise ValueError("remediation_candidate_not_found")
            if row["status"] != "canary_passed":
                raise ValueError("remediation_candidate_canary_required")
            generation = conn.execute(
                "SELECT project_id, status FROM security_shadow_generations "
                "WHERE generation_id = ?",
                (_text(row["shadow_generation"]),),
            ).fetchone()
            if generation is None:
                raise ValueError("remediation_shadow_generation_not_found")
            if str(generation["project_id"]) != str(row["project_id"]):
                raise ValueError("remediation_shadow_generation_scope_mismatch")
            if str(generation["status"]) != "canary_passed":
                raise ValueError("remediation_shadow_generation_not_canary_passed")
            existing = _text(row["shadow_proposal_id"])
            if existing and existing != normalized_proposal:
                raise ValueError("remediation_shadow_proposal_already_attached")
            proposal_row = conn.execute(
                "SELECT project_id, status, visibility, origin_role, origin_visibility, "
                "metadata_json FROM memory_proposals WHERE proposal_id = ?",
                (normalized_proposal,),
            ).fetchone()
            if proposal_row is None:
                raise ValueError("remediation_shadow_proposal_not_found")
            if (
                str(proposal_row["project_id"]) != str(row["project_id"])
                or str(proposal_row["status"]) != "pending"
                or str(proposal_row["visibility"]) != "project"
                or str(proposal_row["origin_visibility"]) != "project"
                or str(proposal_row["origin_role"]) != "system"
            ):
                raise ValueError("remediation_shadow_proposal_scope_mismatch")
            metadata = json.loads(str(proposal_row["metadata_json"] or "{}"))
            if metadata.get("security_candidate_id") != normalized_candidate:
                raise ValueError("remediation_shadow_proposal_provenance_mismatch")
            if metadata.get("shadow_generation") != _text(row["shadow_generation"]):
                raise ValueError("remediation_shadow_proposal_generation_mismatch")
            conn.execute(
                "UPDATE security_remediation_candidates SET shadow_proposal_id = ?, "
                "updated_at = ? WHERE candidate_id = ?",
                (normalized_proposal, _utc_now(), normalized_candidate),
            )
            projected = self._candidate_row(
                conn.execute(
                    "SELECT * FROM security_remediation_candidates WHERE candidate_id = ?",
                    (normalized_candidate,),
                ).fetchone()
            )
            conn.commit()
            return projected
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_versions(
        self,
        *,
        project_id: str,
        finding_id: str | None = None,
    ) -> tuple[SecurityFindingVersion, ...]:
        normalized_project = _scope_required(project_id)
        conn = self._connect()
        try:
            clauses = ["project_id = ?"]
            values: list[object] = [normalized_project]
            if finding_id is not None:
                clauses.append("finding_id = ?")
                values.append(_text(finding_id))
            rows = conn.execute(
                "SELECT * FROM security_finding_versions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, version_id",
                tuple(values),
            ).fetchall()
            return tuple(self._row_to_version(row) for row in rows)
        finally:
            conn.close()

    def _initialize_findings(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS security_finding_versions (
                    version_id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    parent_version_id TEXT NOT NULL DEFAULT '',
                    commit_sha TEXT NOT NULL,
                    scan_revision TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    security_state TEXT NOT NULL,
                    freshness_state TEXT NOT NULL,
                    request_scope_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    redacted_summary TEXT NOT NULL,
                    remediation_pattern TEXT NOT NULL,
                    accepted_risk_expires_at TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, finding_id, scope_key, security_state, evidence_json)
                );
                CREATE INDEX IF NOT EXISTS idx_security_finding_versions_scope
                ON security_finding_versions(project_id, finding_id, created_at, version_id);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS security_remediation_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    source_version_id TEXT NOT NULL,
                    redacted_pattern TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation_projects_json TEXT NOT NULL,
                    shadow_generation TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, source_version_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS security_shadow_generations (
                    generation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('shadow', 'canary_passed', 'rolled_back')),
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS security_shadow_canary_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    benchmark_corpus_sha256 TEXT NOT NULL,
                    benchmark_cases_sha256 TEXT NOT NULL,
                    language_split_json TEXT NOT NULL,
                    embedding_identity TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    observed_from TEXT NOT NULL,
                    observed_until TEXT NOT NULL,
                    environment_fingerprint TEXT NOT NULL DEFAULT '',
                    pricing_revision TEXT NOT NULL DEFAULT '',
                    dependency_digest TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    cost_currency TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(candidate_id, generation_id, manifest_hash, observed_from, observed_until)
                )
                """
            )
            receipt_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(security_shadow_canary_receipts)")
            }
            for name in (
                "environment_fingerprint",
                "pricing_revision",
                "dependency_digest",
                "source_fingerprint",
                "cost_currency",
            ):
                if name not in receipt_columns:
                    conn.execute(
                        "ALTER TABLE security_shadow_canary_receipts "
                        f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(security_remediation_candidates)")
            }
            for name, definition in (
                ("shadow_proposal_id", "TEXT NOT NULL DEFAULT ''"),
                ("canary_result", "TEXT NOT NULL DEFAULT ''"),
                ("canary_reason", "TEXT NOT NULL DEFAULT ''"),
                ("canary_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if name not in columns:
                    conn.execute(
                        "ALTER TABLE security_remediation_candidates "
                        f"ADD COLUMN {name} {definition}"
                    )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _version_id(finding: SecurityFinding, parent_version_id: str) -> str:
        stable = {
            "finding": finding.to_evidence(),
            "parent_version_id": parent_version_id,
        }
        return "sfv_" + hashlib.sha256(_json(stable).encode("utf-8")).hexdigest()

    def _insert_finding_version(
        self,
        conn: sqlite3.Connection,
        finding: SecurityFinding,
        *,
        parent_version_id: str,
    ) -> str:
        if parent_version_id:
            parent = conn.execute(
                "SELECT * FROM security_finding_versions WHERE version_id = ?",
                (parent_version_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("finding_parent_not_found")
            if (
                parent["project_id"] != finding.project_id
                or parent["finding_id"] != finding.finding_id
            ):
                raise ValueError("finding_parent_scope_mismatch")
            self._row_to_version(parent).finding.validate_transition_to(finding)
        version_id = self._version_id(finding, parent_version_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO security_finding_versions (
                version_id, finding_id, project_id, parent_version_id,
                commit_sha, scan_revision, rule_id, severity, security_state,
                freshness_state, request_scope_id, scope_key, redacted_summary,
                remediation_pattern, accepted_risk_expires_at, evidence_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                finding.finding_id,
                finding.project_id,
                parent_version_id,
                finding.commit_sha,
                finding.scan_revision,
                finding.rule_id,
                finding.severity,
                finding.security_state,
                finding.freshness_state,
                finding.request_scope_id,
                finding.scope_key(),
                finding.redacted_summary,
                finding.remediation_pattern,
                finding.accepted_risk_expires_at,
                _json(finding.evidence),
                _utc_now(),
            ),
        )
        return version_id

    def _version_created_at(self, conn: sqlite3.Connection, version_id: str) -> str:
        row = conn.execute(
            "SELECT created_at FROM security_finding_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        return str(row["created_at"] if row is not None else "")

    @staticmethod
    def _shadow_generation_row(row: sqlite3.Row | None) -> ShadowGenerationBinding:
        if row is None:
            raise ValueError("remediation_shadow_generation_persistence_failed")
        return ShadowGenerationBinding(
            generation_id=str(row["generation_id"]),
            project_id=str(row["project_id"]),
            status=str(row["status"]),
            manifest_hash=str(row["manifest_hash"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> SecurityFindingVersion:
        evidence = json.loads(str(row["evidence_json"] or "{}"))
        finding = SecurityFinding(
            finding_id=str(row["finding_id"]),
            project_id=str(row["project_id"]),
            commit_sha=str(row["commit_sha"]),
            scan_revision=str(row["scan_revision"]),
            rule_id=str(row["rule_id"]),
            severity=str(row["severity"]),
            security_state=str(row["security_state"]),
            freshness_state=str(row["freshness_state"]),
            request_scope_id=str(row["request_scope_id"]),
            redacted_summary=str(row["redacted_summary"]),
            remediation_pattern=str(row["remediation_pattern"]),
            accepted_risk_expires_at=str(row["accepted_risk_expires_at"] or ""),
            evidence=evidence,
        )
        return SecurityFindingVersion(
            version_id=str(row["version_id"]),
            finding_id=str(row["finding_id"]),
            project_id=str(row["project_id"]),
            parent_version_id=str(row["parent_version_id"] or ""),
            created_at=str(row["created_at"]),
            finding=finding,
        )

    @staticmethod
    def _candidate_row(row: sqlite3.Row | None) -> RemediationPatternCandidate:
        if row is None:
            raise ValueError("remediation_pattern_candidate_persistence_failed")
        projects = tuple(
            str(item) for item in json.loads(str(row["validation_projects_json"] or "[]"))
        )
        return RemediationPatternCandidate(
            candidate_id=str(row["candidate_id"]),
            project_id=str(row["project_id"]),
            finding_id=str(row["finding_id"]),
            source_version_id=str(row["source_version_id"]),
            redacted_pattern=str(row["redacted_pattern"]),
            severity=str(row["severity"]),
            status=str(row["status"]),
            validation_project_ids=projects,
            shadow_generation=str(row["shadow_generation"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            shadow_proposal_id=str(row["shadow_proposal_id"] or ""),
            canary_result=str(row["canary_result"] or ""),
            canary_reason=str(row["canary_reason"] or ""),
            canary_evidence=json.loads(str(row["canary_evidence_json"] or "{}")),
        )


def promote_generation_with_security_receipt(
    manager: GenerationManager,
    store: ShieldScanStore,
    *,
    candidate_id: str,
    project_id: str,
    generation_id: str,
    manifest_hash: str,
    connection: sqlite3.Connection | None = None,
) -> GenerationManifest:
    """Promote only after the project-scoped production receipt gate passes.

    ``GenerationManager.promote`` remains usable for generic local generation
    tests. Production orchestration must call this wrapper (or perform the
    equivalent gate) so the legacy no-receipt canary compatibility path cannot
    silently select a production generation.
    """

    store.require_production_canary_receipt(
        candidate_id=candidate_id,
        project_id=project_id,
        generation_id=generation_id,
        manifest_hash=manifest_hash,
    )
    loaded = manager.load_manifest(generation_id)
    if loaded.manifest_sha256 != manifest_hash:
        raise ValueError("production_canary_manager_manifest_mismatch")
    promoted = manager.promote(generation_id, connection=connection)
    if promoted.manifest_sha256 != manifest_hash:
        raise ValueError("production_canary_promoted_manifest_mismatch")
    return promoted
