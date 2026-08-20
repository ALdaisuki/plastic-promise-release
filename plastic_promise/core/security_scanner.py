"""Bounded security-scanner seam and DeepSec Shield CLI adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

SecurityScanStatus = Literal["clean", "findings", "degraded", "not_applicable"]

_SUPPORTED_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".go",
        ".rb",
        ".php",
        ".cs",
        ".rs",
        ".yaml",
        ".yml",
        ".json",
    }
)
_VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
_VALID_EXIT_CODES = frozenset({0, 2})
_MAX_REPORT_BYTES = 4 * 1024 * 1024
_MAX_FINDINGS = 500
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|passwd|pwd)"
        r"\s*[:=]\s*[\"']?[^\s\"',;]{6,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.IGNORECASE,
    ),
)


def _sha256(value: str | bytes) -> str:
    material = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    return hashlib.sha256(material).hexdigest()


def _bounded_text(value: object, limit: int = 1000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _redacted_text(value: object, limit: int = 1000) -> str:
    text = " ".join(str(value or "").split())
    for pattern in _SECRET_TEXT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _safe_relative_path(value: object) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("security_scan_path_invalid")
    return path.as_posix()


@dataclass(frozen=True)
class ScanRequest:
    project_id: str
    project_root: str
    source_revision: str
    changed_files: tuple[str, ...]
    base_revision: str = ""
    head_revision: str = ""
    scan_id: str = ""
    security_sensitive: bool = False
    timeout_seconds: float = 30.0
    source_sha256: str = ""

    def __post_init__(self) -> None:
        if not str(self.project_id).strip():
            raise ValueError("security_scan_project_required")
        if not str(self.source_revision).strip():
            raise ValueError("security_scan_revision_required")
        if self.timeout_seconds <= 0:
            raise ValueError("security_scan_timeout_invalid")
        object.__setattr__(
            self, "changed_files", tuple(_safe_relative_path(p) for p in self.changed_files)
        )


@dataclass(frozen=True)
class SecurityFinding:
    native_id: str
    rule_id: str
    layer: str
    finding_type: str
    severity: str
    confidence: float
    path: str
    start_line: int | None
    end_line: int | None
    message: str
    suggestion: str = ""
    raw_evidence: str = field(default="", repr=False)
    dismissed: bool = False

    def __post_init__(self) -> None:
        severity = str(self.severity or "info").lower()
        if severity not in _VALID_SEVERITIES:
            raise ValueError("security_finding_severity_invalid")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "path", _safe_relative_path(self.path))
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))

    def public_dict(self, *, source_revision: str, rule_bundle_sha256: str) -> dict:
        evidence_sha256 = _sha256(self.raw_evidence)
        region = {"start_line": self.start_line, "end_line": self.end_line}
        fingerprint = json.dumps(
            {
                "path": self.path,
                "region": region,
                "rule_bundle_sha256": rule_bundle_sha256,
                "rule_id": self.rule_id,
                "source_revision": source_revision,
                "source_span_sha256": evidence_sha256,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return {
            "evidence_id": "security_" + _sha256(fingerprint)[:24],
            "native_id": _redacted_text(self.native_id, 200),
            "rule_id": _redacted_text(self.rule_id, 200),
            "layer": _bounded_text(self.layer, 20),
            "type": _bounded_text(self.finding_type, 100),
            "severity": self.severity,
            "confidence": self.confidence,
            "path": self.path,
            "region": region,
            "message": _redacted_text(self.message),
            "suggestion": _redacted_text(self.suggestion),
            "evidence_sha256": evidence_sha256,
            "dismissed": bool(self.dismissed),
        }


@dataclass(frozen=True)
class SecurityScanResult:
    status: SecurityScanStatus
    scan_id: str
    scanner_name: str
    scanner_version: str
    scanner_revision: str
    rule_bundle_sha256: str
    requested_files: tuple[str, ...]
    scanned_files: tuple[str, ...]
    findings: tuple[SecurityFinding, ...]
    duration_ms: float
    exit_code: int | None
    unsupported_files: tuple[str, ...] = ()
    failure_reason: str = ""
    mode: str = "shadow"

    def __post_init__(self) -> None:
        if self.status not in {"clean", "findings", "degraded", "not_applicable"}:
            raise ValueError("security_scan_status_invalid")
        for field_name in ("requested_files", "scanned_files", "unsupported_files"):
            object.__setattr__(
                self,
                field_name,
                tuple(_safe_relative_path(p) for p in getattr(self, field_name)),
            )

    def public_dict(self, request: ScanRequest) -> dict:
        return {
            "schema_version": "plastic-review-evidence/deepsec-v1",
            "status": self.status,
            "mode": self.mode,
            "scan_id": self.scan_id or request.scan_id,
            "project_id": request.project_id,
            "source_revision": request.source_revision,
            "source_sha256": request.source_sha256,
            "review_range": {"base": request.base_revision, "head": request.head_revision},
            "tool": {
                "name": self.scanner_name,
                "package_version": self.scanner_version,
                "upstream_commit": self.scanner_revision,
                "rule_bundle_sha256": self.rule_bundle_sha256,
            },
            "policy": {
                "layers": ["L1", "L2"],
                "remote_l3": False,
                "network": "denied_by_production_runtime",
            },
            "coverage": {
                "requested_files": list(self.requested_files),
                "scanned_files": list(self.scanned_files),
                "unsupported_files": list(self.unsupported_files),
            },
            "execution": {
                "exit_code": self.exit_code,
                "duration_ms": round(max(0.0, self.duration_ms), 2),
                "failure_reason": _bounded_text(self.failure_reason, 200),
            },
            "findings": [
                finding.public_dict(
                    source_revision=request.source_revision,
                    rule_bundle_sha256=self.rule_bundle_sha256,
                )
                for finding in self.findings
            ],
        }


class SecurityScanner(Protocol):
    def scan(self, request: ScanRequest) -> SecurityScanResult: ...


class DeepSecCliAdapter:
    """Run commit-pinned DeepSec Shield L1/L2 and normalize its JSON result."""

    def __init__(
        self,
        command: tuple[str, ...] | None = None,
        *,
        scanner_version: str = "0.2.0",
        scanner_revision: str = "3742ec0702f6b72956365bee3d23319522db5c40",
        rule_bundle_sha256: str = "unverified",
        mode: str = "shadow",
    ) -> None:
        configured = os.environ.get("PP_DEEPSEC_COMMAND", "deepsec")
        self.command = command or tuple(shlex.split(configured))
        self.scanner_version = scanner_version
        self.scanner_revision = scanner_revision
        self.rule_bundle_sha256 = rule_bundle_sha256
        self.mode = mode if mode in {"shadow", "on"} else "shadow"

    @classmethod
    def from_environment(cls) -> DeepSecCliAdapter:
        return cls(
            scanner_version=os.environ.get("PP_DEEPSEC_VERSION", "0.2.0"),
            scanner_revision=os.environ.get(
                "PP_DEEPSEC_REVISION", "3742ec0702f6b72956365bee3d23319522db5c40"
            ),
            rule_bundle_sha256=os.environ.get("PP_DEEPSEC_RULE_BUNDLE_SHA256", "unverified"),
            mode=os.environ.get("PP_DEEPSEC_REVIEW", "shadow"),
        )

    def scan(self, request: ScanRequest) -> SecurityScanResult:
        started = time.monotonic()
        requested = tuple(dict.fromkeys(request.changed_files))
        supported = tuple(p for p in requested if Path(p).suffix.lower() in _SUPPORTED_SUFFIXES)
        unsupported = tuple(p for p in requested if p not in supported)
        if not supported:
            return self._result(
                request, "not_applicable", requested, (), unsupported, (), started, 0
            )

        root = Path(request.project_root).resolve()
        findings: list[SecurityFinding] = []
        scanned: list[str] = []
        exit_code = 0
        deadline = started + request.timeout_seconds
        try:
            with tempfile.TemporaryDirectory(prefix="pp-deepsec-config-") as config_dir:
                for relative_path in supported:
                    source = (root / relative_path).resolve()
                    if root not in source.parents or not source.is_file():
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    completed = subprocess.run(
                        [
                            *self.command,
                            "shield",
                            "scan",
                            str(source),
                            "--layer",
                            "l1,l2",
                            "--format",
                            "json",
                            "--output",
                            "-",
                        ],
                        cwd=root,
                        env=self._environment(config_dir),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=remaining,
                    )
                    if completed.returncode not in _VALID_EXIT_CODES:
                        return self._degraded(
                            request,
                            requested,
                            tuple(scanned),
                            unsupported,
                            started,
                            completed.returncode,
                            "deepsec_execution_failed",
                        )
                    if len(completed.stdout.encode("utf-8", errors="replace")) > _MAX_REPORT_BYTES:
                        raise ValueError("deepsec_report_too_large")
                    payload = json.loads(completed.stdout)
                    findings.extend(self._parse_findings(payload, relative_path))
                    scanned.append(relative_path)
                    exit_code = max(exit_code, completed.returncode)
        except FileNotFoundError:
            return self._degraded(
                request,
                requested,
                tuple(scanned),
                unsupported,
                started,
                None,
                "deepsec_unavailable",
            )
        except subprocess.TimeoutExpired:
            return self._degraded(
                request,
                requested,
                tuple(scanned),
                unsupported,
                started,
                None,
                "deepsec_timeout",
            )
        except (json.JSONDecodeError, TypeError, ValueError, TimeoutError):
            return self._degraded(
                request,
                requested,
                tuple(scanned),
                unsupported,
                started,
                None,
                "deepsec_output_invalid",
            )

        status: SecurityScanStatus = "findings" if findings else "clean"
        return self._result(
            request,
            status,
            requested,
            tuple(scanned),
            unsupported,
            tuple(findings),
            started,
            exit_code,
        )

    @staticmethod
    def _environment(config_dir: str) -> dict[str, str]:
        result = {
            "PATH": os.environ.get("PATH", ""),
            "DEEPSEC_CONFIG_DIR": config_dir,
            "PYTHONIOENCODING": "utf-8",
        }
        for name in ("LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"):
            if os.environ.get(name):
                result[name] = os.environ[name]
        return result

    def _parse_findings(self, payload: object, relative_path: str) -> list[SecurityFinding]:
        if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
            raise ValueError("deepsec_report_schema_invalid")
        rows = payload["findings"]
        if len(rows) > _MAX_FINDINGS:
            raise ValueError("deepsec_findings_too_many")
        findings = []
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("deepsec_finding_schema_invalid")
            findings.append(
                SecurityFinding(
                    native_id=str(item.get("id") or ""),
                    rule_id=str(item.get("detection_rule") or ""),
                    layer=str(item.get("detection_layer") or ""),
                    finding_type=str(item.get("type") or "other"),
                    severity=str(item.get("severity") or "info"),
                    confidence=float(item.get("confidence", 1.0)),
                    path=relative_path,
                    start_line=_optional_int(item.get("line")),
                    end_line=_optional_int(item.get("end_line") or item.get("endLine")),
                    message=str(item.get("description") or item.get("message") or ""),
                    suggestion=str(item.get("suggestion") or ""),
                    raw_evidence=str(item.get("evidence") or ""),
                    dismissed=bool(item.get("dismissed", False)),
                )
            )
        return findings

    def _degraded(
        self,
        request: ScanRequest,
        requested: tuple[str, ...],
        scanned: tuple[str, ...],
        unsupported: tuple[str, ...],
        started: float,
        exit_code: int | None,
        reason: str,
    ) -> SecurityScanResult:
        return self._result(
            request,
            "degraded",
            requested,
            scanned,
            unsupported,
            (),
            started,
            exit_code,
            reason,
        )

    def _result(
        self,
        request: ScanRequest,
        status: SecurityScanStatus,
        requested: tuple[str, ...],
        scanned: tuple[str, ...],
        unsupported: tuple[str, ...],
        findings: tuple[SecurityFinding, ...],
        started: float,
        exit_code: int | None,
        failure_reason: str = "",
    ) -> SecurityScanResult:
        return SecurityScanResult(
            status=status,
            scan_id=request.scan_id,
            scanner_name="deepsec-shield",
            scanner_version=self.scanner_version,
            scanner_revision=self.scanner_revision,
            rule_bundle_sha256=self.rule_bundle_sha256,
            requested_files=requested,
            scanned_files=scanned,
            unsupported_files=unsupported,
            findings=findings,
            duration_ms=(time.monotonic() - started) * 1000,
            exit_code=exit_code,
            failure_reason=failure_reason,
            mode=self.mode,
        )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("deepsec_finding_region_invalid")
    return int(value)
