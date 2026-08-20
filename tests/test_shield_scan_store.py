from __future__ import annotations

import pytest

from plastic_promise.core.security_findings import SecurityFinding
from plastic_promise.core.shield_scan_store import ShieldScanRequest, ShieldScanStore


def _request(**overrides) -> ShieldScanRequest:
    values = {
        "project_id": "project:alpha",
        "commit_sha": "abc1234",
        "scan_revision": "deepsec-rules:v1",
        "request_scope_id": "scope:alpha:one",
        "changed_paths_hash": "sha256:" + "1" * 64,
    }
    values.update(overrides)
    return ShieldScanRequest(**values)


def _persist_fixed_finding(store: ShieldScanStore):
    opened = SecurityFinding(
        finding_id="finding:sql",
        project_id="project:alpha",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.sql.injection",
        request_scope_id="scope:alpha:initial",
    )
    opened_version = store.append_version(opened)
    remediation = opened.transition(
        "remediation_required",
        evidence={"decision": "patch_required"},
    )
    remediation_version = store.append_version(
        remediation,
        parent_version_id=opened_version.version_id,
    )
    fixed = remediation.transition(
        "fixed",
        evidence={"commit_sha": "def5678", "tests_passed": True},
    )
    return store.append_version(
        fixed,
        parent_version_id=remediation_version.version_id,
    )


def _persist_resolved_pattern(
    store: ShieldScanStore,
    *,
    project_id: str,
    commit_sha: str,
    severity: str = "low",
    pattern: str = "Use parameterized queries for dynamic SQL.",
):
    opened = SecurityFinding(
        finding_id="finding:sql",
        project_id=project_id,
        commit_sha=commit_sha,
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.sql.injection",
        severity=severity,
        request_scope_id=f"scope:{project_id}:initial",
        remediation_pattern=pattern,
    )
    opened_version = store.append_version(opened)
    remediation = opened.transition(
        "remediation_required",
        evidence={"decision": "patch_required"},
    )
    remediation_version = store.append_version(
        remediation,
        parent_version_id=opened_version.version_id,
    )
    fixed = remediation.transition(
        "fixed",
        evidence={"commit_sha": commit_sha, "tests_passed": True},
    )
    fixed_version = store.append_version(
        fixed,
        parent_version_id=remediation_version.version_id,
    )
    return store.record_rescan(
        project_id=project_id,
        parent_version_id=fixed_version.version_id,
        commit_sha=commit_sha,
        scan_revision="deepsec-rules:v2",
        request_scope_id=f"scope:{project_id}:rescan",
        finding_present=False,
    )


def test_shield_scan_enqueue_is_project_scoped_and_idempotent(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")

    first = store.enqueue(_request())
    duplicate = store.enqueue(_request())
    other_project = store.enqueue(_request(project_id="project:beta"))

    assert first.created is True
    assert duplicate.reused is True
    assert duplicate.job.job_id == first.job.job_id
    assert first.job.project_id == "project:alpha"
    assert first.job.job_kind == "security.shield_scan"
    assert first.job.payload["request_scope_id"] == "scope:alpha:one"
    assert other_project.job.job_id != first.job.job_id


def test_shield_scan_scope_is_part_of_dedupe_identity(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")

    first = store.enqueue(_request(request_scope_id="scope:alpha:one"))
    second = store.enqueue(_request(request_scope_id="scope:alpha:two"))

    assert first.created is True
    assert second.created is True
    assert first.job.job_id != second.job.job_id


def test_shield_scan_request_rejects_secret_provider_identity(tmp_path):
    with pytest.raises(ValueError, match="shield_scan_secret_detected"):
        _request(provider_identity="api_key=sk-test-secret-material-123456")


def test_shield_scan_rejects_unknown_project_scope(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")

    with pytest.raises(ValueError, match="shield_scan_project_scope_required"):
        store.enqueue(_request(project_id="project:unknown"))


def test_shadow_generation_registry_is_initialized_and_project_owned(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")

    registered = store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
        manifest_hash="a" * 64,
    )
    assert registered.project_id == "project:alpha"
    assert registered.status == "shadow"
    assert registered.manifest_hash == "a" * 64
    assert (
        store.register_shadow_generation(
            project_id="project:alpha",
            generation_id="shadow:security:v1",
            manifest_hash="a" * 64,
        )
        == registered
    )

    with pytest.raises(ValueError, match="remediation_shadow_generation_scope_mismatch"):
        store.register_shadow_generation(
            project_id="project:beta",
            generation_id="shadow:security:v1",
            manifest_hash="a" * 64,
        )

    with pytest.raises(ValueError, match="remediation_shadow_generation_manifest_invalid"):
        store.register_shadow_generation(
            project_id="project:alpha",
            generation_id="shadow:security:v2",
            manifest_hash="not-a-sha256",
        )


def test_shadow_generation_manifest_binding_requires_complete_sealed_identity(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    manifest = {
        "generation_id": "generation-v2",
        "manifest_sha256": "b" * 64,
        "build_status": "complete",
        "verification_status": "unverified",
    }

    binding = store.register_shadow_generation_manifest(
        project_id="project:alpha",
        manifest=manifest,
    )
    assert binding.generation_id == "generation-v2"
    assert binding.manifest_hash == "b" * 64

    with pytest.raises(ValueError, match="remediation_shadow_manifest_not_complete"):
        store.register_shadow_generation_manifest(
            project_id="project:alpha",
            manifest={**manifest, "build_status": "building"},
        )

    with pytest.raises(ValueError, match="remediation_shadow_manifest_hash_required"):
        store.register_shadow_generation_manifest(
            project_id="project:alpha",
            manifest={**manifest, "manifest_sha256": ""},
        )


def test_shield_claim_batch_never_mixes_project_or_scan_revision(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    store.enqueue(_request(commit_sha="alpha001"))
    store.enqueue(_request(commit_sha="alpha002"))
    store.enqueue(_request(project_id="project:beta", commit_sha="beta0001"))
    store.enqueue(_request(commit_sha="alpha003", scan_revision="deepsec-rules:v2"))

    leases = store.claim_batch(limit=20, min_batch_size=1, max_wait_seconds=0)

    assert len(leases) == 2
    assert {lease.job.project_id for lease in leases} == {"project:alpha"}
    assert {lease.job.config_revision for lease in leases} == {"deepsec-rules:v1"}


def test_complete_scan_atomically_persists_finding_versions_and_job_result(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    queued = store.enqueue(_request())
    lease = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    finding = SecurityFinding(
        finding_id="finding:sql",
        project_id="project:alpha",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.sql.injection",
        severity="high",
        request_scope_id="scope:alpha:one",
        redacted_summary="Parameterized query is required.",
    )

    completed = store.complete_scan(lease, [finding])
    versions = store.list_versions(
        project_id="project:alpha",
        finding_id="finding:sql",
    )

    assert completed.job_id == queued.job.job_id
    assert completed.status == "completed"
    assert len(versions) == 1
    assert versions[0].finding == finding
    assert completed.result == {"finding_version_ids": [versions[0].version_id]}


def test_complete_scan_rolls_back_all_findings_when_one_crosses_scope(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    queued = store.enqueue(_request())
    lease = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    valid = SecurityFinding(
        finding_id="finding:valid",
        project_id="project:alpha",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.valid",
        request_scope_id="scope:alpha:one",
    )
    foreign = SecurityFinding(
        finding_id="finding:foreign",
        project_id="project:beta",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.foreign",
        request_scope_id="scope:beta:one",
    )

    with pytest.raises(ValueError, match="shield_finding_project_scope_mismatch"):
        store.complete_scan(lease, [valid, foreign])

    assert store.list_versions(project_id="project:alpha") == ()
    assert (
        store.derived_work.get(
            job_id=queued.job.job_id,
            project_id="project:alpha",
        ).status
        == "leased"
    )


def test_complete_scan_rejects_finding_request_scope_mismatch(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    queued = store.enqueue(_request(request_scope_id="scope:alpha:one"))
    lease = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    finding = SecurityFinding(
        finding_id="finding:scope",
        project_id="project:alpha",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.scope",
        request_scope_id="scope:alpha:two",
    )

    with pytest.raises(ValueError, match="shield_finding_request_scope_mismatch"):
        store.complete_scan(lease, [finding])

    assert store.list_versions(project_id="project:alpha") == ()
    assert (
        store.derived_work.get(
            job_id=queued.job.job_id,
            project_id="project:alpha",
        ).status
        == "leased"
    )


def test_finding_version_lineage_cannot_cross_project_scope(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    alpha = SecurityFinding(
        finding_id="finding:shared-name",
        project_id="project:alpha",
        commit_sha="alpha001",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.rule",
        request_scope_id="scope:alpha:one",
    )
    beta = SecurityFinding(
        finding_id="finding:shared-name",
        project_id="project:beta",
        commit_sha="beta0001",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.rule",
        request_scope_id="scope:beta:one",
    )
    alpha_version = store.append_version(alpha)

    with pytest.raises(ValueError, match="finding_parent_scope_mismatch"):
        store.append_version(beta, parent_version_id=alpha_version.version_id)


def test_finding_version_lineage_rejects_missing_parent(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    finding = SecurityFinding(
        finding_id="finding:sql",
        project_id="project:alpha",
        commit_sha="alpha001",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.rule",
        request_scope_id="scope:alpha:one",
    )

    with pytest.raises(ValueError, match="finding_parent_not_found"):
        store.append_version(finding, parent_version_id="sfv_missing")


def test_finding_version_lineage_cannot_skip_state_transition(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    opened = SecurityFinding(
        finding_id="finding:skip",
        project_id="project:alpha",
        commit_sha="alpha001",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.rule",
        request_scope_id="scope:alpha:one",
    )
    opened_version = store.append_version(opened)
    fixed = opened.transition(
        "remediation_required",
        evidence={"decision": "patch_required"},
    ).transition("fixed", evidence={"tests_passed": True})

    with pytest.raises(ValueError, match="invalid_security_transition"):
        store.append_version(fixed, parent_version_id=opened_version.version_id)


def test_shield_retry_reclaims_with_new_fence_then_stops_at_attempt_limit(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    store.enqueue(_request(max_attempts=2))

    first = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    waiting = store.fail(
        first,
        failure_code="provider_timeout",
        retryable=True,
    )
    second = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    dead = store.fail(
        second,
        failure_code="provider_timeout",
        retryable=True,
    )

    assert waiting.status == "retry_wait"
    assert second.job.fencing_generation == first.job.fencing_generation + 1
    assert dead.status == "dead"
    assert dead.attempt_count == 2


@pytest.mark.parametrize(
    ("commit_sha", "scan_revision", "error_code"),
    [
        ("wrong-commit", "deepsec-rules:v1", "shield_finding_commit_mismatch"),
        ("abc1234", "deepsec-rules:v2", "shield_finding_scan_revision_mismatch"),
    ],
)
def test_invalid_scan_identity_writes_nothing_and_preserves_lease(
    tmp_path,
    commit_sha,
    scan_revision,
    error_code,
):
    store = ShieldScanStore(tmp_path / "memory.db")
    queued = store.enqueue(_request())
    lease = store.claim_batch(
        project_id="project:alpha",
        min_batch_size=1,
        max_wait_seconds=0,
    )[0]
    finding = SecurityFinding(
        finding_id="finding:identity",
        project_id="project:alpha",
        commit_sha=commit_sha,
        scan_revision=scan_revision,
        rule_id="deepsec.identity",
        request_scope_id="scope:alpha:one",
    )

    with pytest.raises(ValueError, match=error_code):
        store.complete_scan(lease, [finding])

    assert store.list_versions(project_id="project:alpha") == ()
    assert (
        store.derived_work.get(
            job_id=queued.job.job_id,
            project_id="project:alpha",
        ).status
        == "leased"
    )


def test_fixed_finding_closes_only_through_project_scoped_rescan_evidence(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    fixed_version = _persist_fixed_finding(store)

    with pytest.raises(ValueError, match="rescan_scan_revision_not_advanced"):
        store.record_rescan(
            project_id="project:alpha",
            parent_version_id=fixed_version.version_id,
            commit_sha="def5678",
            scan_revision="deepsec-rules:v1",
            request_scope_id="scope:alpha:rescan",
            finding_present=False,
        )

    resolved = store.record_rescan(
        project_id="project:alpha",
        parent_version_id=fixed_version.version_id,
        commit_sha="def5678",
        scan_revision="deepsec-rules:v2",
        request_scope_id="scope:alpha:rescan",
        finding_present=False,
        evidence={"scan_run_id": "scan:two"},
    )

    assert resolved.parent_version_id == fixed_version.version_id
    assert resolved.finding.security_state == "resolved"
    assert resolved.finding.commit_sha == "def5678"
    assert resolved.finding.scan_revision == "deepsec-rules:v2"
    assert resolved.finding.request_scope_id == "scope:alpha:rescan"
    assert resolved.finding.evidence["rescan_passed"] is True


def test_rescan_requires_a_strictly_newer_revision(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    fixed_version = _persist_fixed_finding(store)

    with pytest.raises(ValueError, match="rescan_scan_revision_not_advanced"):
        store.record_rescan(
            project_id="project:alpha",
            parent_version_id=fixed_version.version_id,
            commit_sha="def5678",
            scan_revision="deepsec-rules:v0",
            request_scope_id="scope:alpha:rescan",
            finding_present=False,
        )

    with pytest.raises(ValueError, match="rescan_scan_revision_prefix_mismatch"):
        store.record_rescan(
            project_id="project:alpha",
            parent_version_id=fixed_version.version_id,
            commit_sha="def5678",
            scan_revision="other-rules:v2",
            request_scope_id="scope:alpha:rescan",
            finding_present=False,
        )


def test_rescan_with_finding_present_marks_recurring_without_closure(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    fixed_version = _persist_fixed_finding(store)

    recurring = store.record_rescan(
        project_id="project:alpha",
        parent_version_id=fixed_version.version_id,
        commit_sha="def5678",
        scan_revision="deepsec-rules:v2",
        request_scope_id="scope:alpha:rescan",
        finding_present=True,
        evidence={"scan_run_id": "scan:two"},
    )

    assert recurring.finding.security_state == "recurring"
    assert recurring.finding.evidence["finding_present"] is True
    assert recurring.finding.evidence.get("rescan_passed") is not True


def test_rescan_cannot_use_parent_from_another_project(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    fixed_version = _persist_fixed_finding(store)

    with pytest.raises(ValueError, match="finding_parent_scope_mismatch"):
        store.record_rescan(
            project_id="project:beta",
            parent_version_id=fixed_version.version_id,
            commit_sha="def5678",
            scan_revision="deepsec-rules:v2",
            request_scope_id="scope:beta:rescan",
            finding_present=False,
        )


def test_resolved_low_risk_remediation_pattern_starts_as_noncanonical_candidate(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    opened = SecurityFinding(
        finding_id="finding:sql",
        project_id="project:alpha",
        commit_sha="abc1234",
        scan_revision="deepsec-rules:v1",
        rule_id="deepsec.sql.injection",
        severity="low",
        request_scope_id="scope:alpha:initial",
        remediation_pattern="Use parameterized queries for dynamic SQL.",
    )
    opened_version = store.append_version(opened)
    remediation = opened.transition(
        "remediation_required",
        evidence={"decision": "patch_required"},
    )
    remediation_version = store.append_version(
        remediation,
        parent_version_id=opened_version.version_id,
    )
    fixed = remediation.transition(
        "fixed",
        evidence={"commit_sha": "def5678", "tests_passed": True},
    )
    fixed_version = store.append_version(
        fixed,
        parent_version_id=remediation_version.version_id,
    )
    resolved = store.record_rescan(
        project_id="project:alpha",
        parent_version_id=fixed_version.version_id,
        commit_sha="def5678",
        scan_revision="deepsec-rules:v2",
        request_scope_id="scope:alpha:rescan",
        finding_present=False,
    )

    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=resolved.version_id,
    )

    assert candidate.status == "pending_validation"
    assert candidate.project_id == "project:alpha"
    assert candidate.source_version_id == resolved.version_id
    assert candidate.redacted_pattern == "Use parameterized queries for dynamic SQL."
    assert candidate.validation_project_ids == ("project:alpha",)


def test_remediation_candidate_requires_independent_validation_before_shadow(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _persist_resolved_pattern(
        store,
        project_id="project:alpha",
        commit_sha="alpha5678",
    )
    validation = _persist_resolved_pattern(
        store,
        project_id="project:beta",
        commit_sha="beta5678",
    )
    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )

    with pytest.raises(ValueError, match="remediation_validation_required"):
        store.shadow_promote_remediation_candidate(
            candidate_id=candidate.candidate_id,
            shadow_generation="shadow:security:v1",
        )

    validated = store.record_remediation_validation(
        candidate_id=candidate.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    with pytest.raises(ValueError, match="remediation_shadow_generation_not_found"):
        store.shadow_promote_remediation_candidate(
            candidate_id=candidate.candidate_id,
            shadow_generation="shadow:security:missing",
        )
    store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
    )
    shadowed = store.shadow_promote_remediation_candidate(
        candidate_id=candidate.candidate_id,
        shadow_generation="shadow:security:v1",
    )

    assert validated.validation_project_ids == ("project:alpha", "project:beta")
    assert shadowed.status == "shadowed"
    assert shadowed.shadow_generation == "shadow:security:v1"


def test_shadow_promotion_rejects_generation_from_another_project(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _persist_resolved_pattern(
        store,
        project_id="project:alpha",
        commit_sha="alpha5678",
    )
    validation = _persist_resolved_pattern(
        store,
        project_id="project:beta",
        commit_sha="beta5678",
    )
    candidate = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=candidate.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    store.register_shadow_generation(
        project_id="project:beta",
        generation_id="shadow:security:foreign",
    )

    with pytest.raises(ValueError, match="remediation_shadow_generation_scope_mismatch"):
        store.shadow_promote_remediation_candidate(
            candidate_id=candidate.candidate_id,
            shadow_generation="shadow:security:foreign",
        )


def test_shadow_promotion_rejects_generation_after_canary_closes_it(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _persist_resolved_pattern(
        store,
        project_id="project:alpha",
        commit_sha="alpha5678",
    )
    validation = _persist_resolved_pattern(
        store,
        project_id="project:beta",
        commit_sha="beta5678",
    )
    first = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=first.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )
    store.register_shadow_generation(
        project_id="project:alpha",
        generation_id="shadow:security:v1",
    )
    shadowed = store.shadow_promote_remediation_candidate(
        candidate_id=first.candidate_id,
        shadow_generation="shadow:security:v1",
    )
    store.record_shadow_canary(candidate_id=shadowed.candidate_id, passed=True)

    second_source = _persist_resolved_pattern(
        store,
        project_id="project:alpha",
        commit_sha="alpha9999",
    )
    second = store.create_remediation_candidate(
        project_id="project:alpha",
        source_version_id=second_source.version_id,
    )
    store.record_remediation_validation(
        candidate_id=second.candidate_id,
        validation_project_id="project:beta",
        validation_version_id=validation.version_id,
    )

    with pytest.raises(ValueError, match="remediation_shadow_generation_not_shadow"):
        store.shadow_promote_remediation_candidate(
            candidate_id=second.candidate_id,
            shadow_generation="shadow:security:v1",
        )


def test_high_risk_remediation_pattern_cannot_enter_candidate_ledger(tmp_path):
    store = ShieldScanStore(tmp_path / "memory.db")
    source = _persist_resolved_pattern(
        store,
        project_id="project:alpha",
        commit_sha="alpha5678",
        severity="high",
    )

    with pytest.raises(ValueError, match="remediation_pattern_risk_too_high"):
        store.create_remediation_candidate(
            project_id="project:alpha",
            source_version_id=source.version_id,
        )
