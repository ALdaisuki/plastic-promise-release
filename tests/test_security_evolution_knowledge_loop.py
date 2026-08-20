from __future__ import annotations

import json
import subprocess
import tempfile
from typing import TYPE_CHECKING

import pytest

from plastic_promise.core.evolution_evidence import (
    EvolutionEvidence,
    EvolutionEvidenceConflictError,
    EvolutionEvidenceEvent,
)
from plastic_promise.core.evolution_projection import (
    EvolutionProjectionWorker,
    KnowledgeSecurityFindingStore,
)
from plastic_promise.core.review_engine import ReviewEngine
from plastic_promise.core.security_scanner import (
    DeepSecCliAdapter,
    ScanRequest,
    SecurityFinding,
    SecurityScanResult,
)

if TYPE_CHECKING:
    from pathlib import Path


class FakeSecurityScanner:
    def __init__(self, result: SecurityScanResult) -> None:
        self.result = result
        self.requests: list[ScanRequest] = []

    def scan(self, request: ScanRequest) -> SecurityScanResult:
        self.requests.append(request)
        return self.result


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _review_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "review-target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.invalid")
    _git(repo, "config", "user.name", "Review Fixture")
    source = repo / "app.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "base")
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "change")
    return repo


def test_review_prepare_exposes_redacted_security_evidence(tmp_path: Path) -> None:
    repo = _review_repo(tmp_path)
    secret = "sk-live-secret-that-must-not-escape"
    scanner = FakeSecurityScanner(
        SecurityScanResult(
            status="findings",
            scan_id="scan:test-review",
            scanner_name="deepsec-shield",
            scanner_version="0.2.0",
            scanner_revision="3742ec0702f6b72956365bee3d23319522db5c40",
            rule_bundle_sha256="a" * 64,
            requested_files=("app.py",),
            scanned_files=("app.py",),
            findings=(
                SecurityFinding(
                    native_id="ds_native",
                    rule_id="hardcoded-secret",
                    layer="L1",
                    finding_type="hardcoded_secret",
                    severity="critical",
                    confidence=1.0,
                    path="app.py",
                    start_line=2,
                    end_line=2,
                    message=f"Hardcoded provider credential {secret}",
                    suggestion="Load the credential from a protected environment file.",
                    raw_evidence=secret,
                ),
            ),
            duration_ms=5.0,
            exit_code=2,
        )
    )
    review = ReviewEngine(project_root=str(repo), security_scanner=scanner)

    prepared = review.prepare("HEAD~1..HEAD", project_id="project:plastic-promise")

    assert len(scanner.requests) == 1
    assert scanner.requests[0].changed_files == ("app.py",)
    assert prepared["security_scan"]["status"] == "findings"
    assert prepared["security_scan"]["findings"][0]["severity"] == "critical"
    assert prepared["security_scan"]["findings"][0]["evidence_sha256"]
    assert secret not in str(prepared)
    assert "Hardcoded provider credential" in prepared["structured_prompt"]


def test_enforced_critical_security_finding_cannot_be_overridden_by_llm(
    tmp_path: Path,
) -> None:
    repo = _review_repo(tmp_path)
    scanner = FakeSecurityScanner(
        SecurityScanResult(
            status="findings",
            mode="on",
            scan_id="scan:enforced-review",
            scanner_name="deepsec-shield",
            scanner_version="0.2.0",
            scanner_revision="3742ec0702f6b72956365bee3d23319522db5c40",
            rule_bundle_sha256="b" * 64,
            requested_files=("app.py",),
            scanned_files=("app.py",),
            findings=(
                SecurityFinding(
                    native_id="ds_critical",
                    rule_id="command-injection",
                    layer="L2",
                    finding_type="command_injection",
                    severity="critical",
                    confidence=0.99,
                    path="app.py",
                    start_line=2,
                    end_line=2,
                    message="Untrusted input reaches a command sink.",
                    suggestion="Use a fixed argv allow-list.",
                    raw_evidence="subprocess.run(user_input, shell=True)",
                ),
            ),
            duration_ms=8.0,
            exit_code=2,
        )
    )
    review = ReviewEngine(project_root=str(repo), security_scanner=scanner)
    prepared = review.prepare("HEAD~1..HEAD", project_id="project:plastic-promise")

    report = review.evaluate(
        prepared["diff_text"],
        prepared["changed_files"],
        prepared["pre_check_results"],
        '{"status":"pass","findings":[],"recommendation":"approve"}',
        security_scan=prepared["security_scan"],
    )

    assert report.status == "fail"
    assert report.recommendation == "block"
    assert [(item.severity, item.category) for item in report.findings] == [("blocker", "security")]
    assert report.metadata["security_scan"]["status"] == "findings"


def test_shadow_security_finding_is_recorded_without_changing_llm_verdict(
    tmp_path: Path,
) -> None:
    repo = _review_repo(tmp_path)
    scanner = FakeSecurityScanner(
        SecurityScanResult(
            status="findings",
            mode="shadow",
            scan_id="scan:shadow-review",
            scanner_name="deepsec-shield",
            scanner_version="0.2.0",
            scanner_revision="3742ec0702f6b72956365bee3d23319522db5c40",
            rule_bundle_sha256="c" * 64,
            requested_files=("app.py",),
            scanned_files=("app.py",),
            findings=(
                SecurityFinding(
                    native_id="ds_shadow",
                    rule_id="dangerous-call",
                    layer="L2",
                    finding_type="command_execution",
                    severity="critical",
                    confidence=0.95,
                    path="app.py",
                    start_line=2,
                    end_line=2,
                    message="A command execution sink needs review.",
                    raw_evidence="bounded test evidence",
                ),
            ),
            duration_ms=4.0,
            exit_code=2,
        )
    )
    review = ReviewEngine(project_root=str(repo), security_scanner=scanner)
    prepared = review.prepare("HEAD~1..HEAD", project_id="project:plastic-promise")

    report = review.evaluate(
        prepared["diff_text"],
        prepared["changed_files"],
        prepared["pre_check_results"],
        '{"status":"pass","findings":[],"recommendation":"approve"}',
        security_scan=prepared["security_scan"],
    )

    assert report.status == "pass"
    assert report.recommendation == "approve"
    assert [(item.severity, item.category) for item in report.findings] == [("blocker", "security")]
    assert report.metadata["security_scan"]["mode"] == "shadow"


def test_evolution_evidence_submit_is_idempotent_and_queues_projection(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plastic_memory.db"
    event = EvolutionEvidenceEvent(
        project_id="project:plastic-promise",
        causal_scope="scan:review:123",
        origin_kind="security_scanner",
        sensor_name="deepsec-shield",
        sensor_version="0.2.0",
        source_revision="a" * 40,
        source_sha256="b" * 64,
        subject_type="source_file",
        subject_id="app.py",
        rule_id="command-injection",
        payload={
            "severity": "critical",
            "path": "app.py",
            "region": {"start_line": 2, "end_line": 2},
            "message": "Untrusted input reaches a command sink.",
        },
        raw_evidence_sha256="c" * 64,
        idempotency_key="review-scan-123:command-injection:app.py:2",
    )

    first = EvolutionEvidence(database_path).submit(event)
    second = EvolutionEvidence(database_path).submit(event)
    after_restart = EvolutionEvidence(database_path).submit(event)

    assert first.status == "created"
    assert second.status == "deduplicated"
    assert after_restart.status == "deduplicated"
    assert first.evidence_id == second.evidence_id == after_restart.evidence_id
    assert first.independence_group == second.independence_group
    assert first.lifecycle_state == "observed"
    assert first.projection_outbox_id == second.projection_outbox_id
    assert first.reconciliation_outbox_id == second.reconciliation_outbox_id

    conflicting = EvolutionEvidenceEvent(
        **{
            **event.to_dict(),
            "payload": {**event.payload, "severity": "high"},
        }
    )
    with pytest.raises(EvolutionEvidenceConflictError, match="idempotency_key_conflict"):
        EvolutionEvidence(database_path).submit(conflicting)


def test_evolution_projection_reconciles_security_finding_into_knowledge_store(
    tmp_path: Path,
) -> None:
    memory_database = tmp_path / "plastic_memory.db"
    knowledge_database = tmp_path / "plastic_knowledge.db"
    event = EvolutionEvidenceEvent(
        project_id="project:plastic-promise",
        causal_scope="scan:scheduled:456",
        origin_kind="security_scanner",
        sensor_name="deepsec-shield",
        sensor_version="0.2.0",
        source_revision="d" * 40,
        source_sha256="e" * 64,
        subject_type="source_file",
        subject_id="plastic_promise/core/example.py",
        rule_id="unsafe-deserialization",
        payload={
            "severity": "high",
            "type": "unsafe_deserialization",
            "path": "plastic_promise/core/example.py",
            "region": {"start_line": 12, "end_line": 14},
            "message": "Untrusted bytes reach a deserializer.",
            "suggestion": "Use a safe structured decoder.",
        },
        raw_evidence_sha256="f" * 64,
        idempotency_key="scheduled-scan-456:unsafe-deserialization:example.py",
    )
    submission = EvolutionEvidence(memory_database).submit(event)
    worker = EvolutionProjectionWorker(memory_database, knowledge_database)

    first = worker.reconcile(limit=10)
    second = worker.reconcile(limit=10)
    projection = KnowledgeSecurityFindingStore(knowledge_database).get(
        submission.evidence_id,
        project_id="project:plastic-promise",
    )

    assert first.completed == 1
    assert first.failed == 0
    assert first.evidence_ids == (submission.evidence_id,)
    assert second.completed == 0
    assert projection is not None
    assert projection["rule_id"] == "unsafe-deserialization"
    assert projection["severity"] == "high"
    assert projection["path"] == "plastic_promise/core/example.py"
    assert projection["projection_state"] == "quarantined"
    assert "raw_evidence" not in projection


def test_review_prepare_submits_scanner_findings_through_evolution_seam(
    tmp_path: Path,
) -> None:
    repo = _review_repo(tmp_path)
    memory_database = tmp_path / "plastic_memory.db"
    knowledge_database = tmp_path / "plastic_knowledge.db"
    scanner = FakeSecurityScanner(
        SecurityScanResult(
            status="findings",
            mode="shadow",
            scan_id="scan:review-ledger",
            scanner_name="deepsec-shield",
            scanner_version="0.2.0",
            scanner_revision="3742ec0702f6b72956365bee3d23319522db5c40",
            rule_bundle_sha256="1" * 64,
            requested_files=("app.py",),
            scanned_files=("app.py",),
            findings=(
                SecurityFinding(
                    native_id="ds_ledger",
                    rule_id="review-ledger-rule",
                    layer="L1",
                    finding_type="code_smell",
                    severity="medium",
                    confidence=0.8,
                    path="app.py",
                    start_line=2,
                    end_line=2,
                    message="Review finding enters the evidence ledger.",
                    raw_evidence="fixture evidence",
                ),
            ),
            duration_ms=3.0,
            exit_code=0,
        )
    )
    review = ReviewEngine(
        project_root=str(repo),
        security_scanner=scanner,
        evolution_evidence=EvolutionEvidence(memory_database),
    )

    first = review.prepare("HEAD~1..HEAD", project_id="project:plastic-promise")
    second = review.prepare("HEAD~1..HEAD", project_id="project:plastic-promise")
    reconciliation = EvolutionProjectionWorker(memory_database, knowledge_database).reconcile(
        limit=10
    )

    assert first["security_evidence"][0]["status"] == "created"
    assert second["security_evidence"][0]["status"] == "deduplicated"
    assert (
        first["security_evidence"][0]["evidence_id"]
        == second["security_evidence"][0]["evidence_id"]
    )
    assert reconciliation.evidence_ids == (first["security_evidence"][0]["evidence_id"],)


def test_deepsec_cli_adapter_accepts_exit_two_findings_and_isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "scan-target"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")
    request = ScanRequest(
        project_id="project:plastic-promise",
        project_root=str(repo),
        source_revision="a" * 40,
        changed_files=("app.py",),
        scan_id="scan:cli-contract",
    )
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            args,
            2,
            stdout=json.dumps(
                {
                    "findings": [
                        {
                            "id": "native-1",
                            "detection_rule": "command-injection",
                            "detection_layer": "L2",
                            "type": "command_injection",
                            "severity": "high",
                            "confidence": 0.95,
                            "line": 1,
                            "end_line": 1,
                            "description": "Untrusted input reaches a command sink.",
                            "suggestion": "Use a fixed argv allow-list.",
                            "evidence": "bounded fixture evidence",
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", fake_run)
    adapter = DeepSecCliAdapter(command=("deepsec",))
    result = adapter.scan(request)

    assert result.status == "findings"
    assert result.exit_code == 2
    assert result.scanned_files == ("app.py",)
    assert result.findings[0].rule_id == "command-injection"
    assert result.findings[0].severity == "high"
    args = captured["args"]
    assert isinstance(args, list)
    assert "shield" in args and "scan" in args
    assert "--layer" in args and "l1,l2" in args
    assert "--format" in args and "json" in args
    assert "--output" in args and "-" in args
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DEEPSEC_CONFIG_DIR"].startswith(tempfile.gettempdir())
    assert "PYTHONIOENCODING" in env
    for name in ("DEEPSEC_API_KEY", "DEEPSEC_TOKEN", "GITHUB_TOKEN"):
        assert name not in env


def test_deepsec_cli_adapter_clean_exit_zero_and_unsupported_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "scan-target"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "notes.txt").write_text("plain notes", encoding="utf-8")
    request = ScanRequest(
        project_id="project:plastic-promise",
        project_root=str(repo),
        source_revision="b" * 40,
        changed_files=("app.py", "notes.txt"),
        scan_id="scan:cli-clean",
    )
    invoked: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        invoked.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"findings": []}), stderr="")

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", fake_run)
    result = DeepSecCliAdapter(command=("deepsec",)).scan(request)

    assert result.status == "clean"
    assert result.exit_code == 0
    assert result.scanned_files == ("app.py",)
    assert result.unsupported_files == ("notes.txt",)
    assert len(invoked) == 1


def test_deepsec_cli_adapter_degrades_on_invalid_exit_json_and_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "scan-target"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    request = ScanRequest(
        project_id="project:plastic-promise",
        project_root=str(repo),
        source_revision="c" * 40,
        changed_files=("app.py",),
        scan_id="scan:cli-degraded",
    )

    def bad_exit(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", bad_exit)
    degraded = DeepSecCliAdapter(command=("deepsec",)).scan(request)
    assert degraded.status == "degraded"
    assert degraded.failure_reason == "deepsec_execution_failed"
    assert degraded.findings == ()

    def invalid_json(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", invalid_json)
    malformed = DeepSecCliAdapter(command=("deepsec",)).scan(request)
    assert malformed.status == "degraded"
    assert malformed.failure_reason == "deepsec_output_invalid"

    def missing(args: list[str], **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", missing)
    unavailable = DeepSecCliAdapter(command=("deepsec",)).scan(request)
    assert unavailable.status == "degraded"
    assert unavailable.failure_reason == "deepsec_unavailable"


def test_deepsec_cli_adapter_degrades_on_timeout_and_not_applicable_without_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "scan-target"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "notes.txt").write_text("plain notes", encoding="utf-8")
    request = ScanRequest(
        project_id="project:plastic-promise",
        project_root=str(repo),
        source_revision="d" * 40,
        changed_files=("app.py",),
        scan_id="scan:cli-timeout",
    )

    def timed_out(args: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, timeout=float(kwargs.get("timeout", 0)))

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", timed_out)
    result = DeepSecCliAdapter(command=("deepsec",)).scan(request)
    assert result.status == "degraded"
    assert result.failure_reason == "deepsec_timeout"

    invoked: list[list[str]] = []

    def unexpected_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        invoked.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr("plastic_promise.core.security_scanner.subprocess.run", unexpected_run)
    not_applicable = DeepSecCliAdapter(command=("deepsec",)).scan(
        ScanRequest(
            project_id="project:plastic-promise",
            project_root=str(repo),
            source_revision="e" * 40,
            changed_files=("notes.txt",),
            scan_id="scan:cli-na",
        )
    )
    assert not_applicable.status == "not_applicable"
    assert not_applicable.unsupported_files == ("notes.txt",)
    assert invoked == []
