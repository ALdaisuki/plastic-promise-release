from datetime import datetime, timedelta, timezone

import pytest

from plastic_promise.core.security_findings import SecurityFinding


def _finding(**overrides) -> SecurityFinding:
    values = {
        "finding_id": "finding:one",
        "project_id": "project:alpha",
        "commit_sha": "abc123",
        "scan_revision": "scan:one",
        "rule_id": "deepsec.sql.injection",
        "severity": "high",
        "request_scope_id": "project:scope::req:one",
        "redacted_summary": "Parameterized query is required.",
    }
    values.update(overrides)
    return SecurityFinding(**values)


def test_finding_identity_is_project_scoped_and_does_not_expose_project_id():
    first = _finding(project_id="project:alpha")
    second = _finding(project_id="project:beta")

    assert first.scope_key() != second.scope_key()
    assert "project:alpha" not in first.scope_key()
    assert "project:beta" not in second.scope_key()


def test_unknown_project_cannot_create_persistable_finding():
    with pytest.raises(ValueError, match="finding_project_scope_required"):
        _finding(project_id="project:unknown")


def test_security_transition_returns_new_version_and_preserves_original():
    original = _finding()

    remediation = original.transition(
        "remediation_required",
        evidence={"scan_revision": "scan:one"},
    )
    fixed = remediation.transition(
        "fixed",
        evidence={"commit_sha": "def456", "tests": "passed"},
    )

    assert original.security_state == "open"
    assert remediation.security_state == "remediation_required"
    assert fixed.security_state == "fixed"
    assert fixed.commit_sha == "def456"


def test_invalid_security_transition_is_rejected():
    with pytest.raises(ValueError, match="invalid_security_transition"):
        _finding().transition("resolved", evidence={"scan": "missing"})


def test_resolved_requires_a_rescan_evidence_marker():
    fixed = _finding(security_state="fixed")

    with pytest.raises(ValueError, match="resolution_evidence_required"):
        fixed.transition("resolved", evidence={"tests": "passed"})

    resolved = fixed.transition(
        "resolved",
        evidence={"rescan_passed": True, "scan_revision": "scan:two"},
    )

    assert resolved.security_state == "resolved"
    assert resolved.scan_revision == "scan:two"


def test_resolved_finding_cannot_bypass_transition_evidence_validation():
    with pytest.raises(ValueError, match="resolution_evidence_required"):
        _finding(security_state="resolved", evidence={"tests": "passed"})


def test_accepted_risk_has_30_day_default_and_60_day_hard_limit():
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    finding = _finding()

    accepted = finding.transition(
        "accepted_risk",
        evidence={"reason": "mitigation scheduled"},
        accepted_risk_expires_at=(now + timedelta(days=30)).isoformat(),
        now=now,
    )
    assert accepted.security_state == "accepted_risk"

    with pytest.raises(ValueError, match="accepted_risk_expiry_too_far"):
        finding.transition(
            "accepted_risk",
            evidence={"reason": "mitigation scheduled"},
            accepted_risk_expires_at=(now + timedelta(days=61)).isoformat(),
            now=now,
        )


def test_accepted_risk_constructor_requires_reason_and_expiry():
    with pytest.raises(ValueError, match="accepted_risk_expiry_required"):
        _finding(
            security_state="accepted_risk",
            evidence={"reason": "mitigation scheduled"},
        )

    with pytest.raises(ValueError, match="accepted_risk_reason_required"):
        _finding(
            security_state="accepted_risk",
            accepted_risk_expires_at="2026-08-30T00:00:00+00:00",
        )

    with pytest.raises(ValueError, match="accepted_risk_started_at_required"):
        _finding(
            security_state="accepted_risk",
            accepted_risk_expires_at="2026-08-30T00:00:00+00:00",
            evidence={"reason": "mitigation scheduled"},
        )


def test_accepted_risk_constructor_enforces_started_at_hard_limit():
    with pytest.raises(ValueError, match="accepted_risk_expiry_too_far"):
        _finding(
            security_state="accepted_risk",
            accepted_risk_expires_at="2026-11-01T00:00:00+00:00",
            evidence={
                "reason": "mitigation scheduled",
                "accepted_risk_started_at": "2026-08-01T00:00:00+00:00",
            },
        )


def test_freshness_is_independent_from_security_state():
    finding = _finding(
        security_state="resolved",
        freshness_state="fresh",
        evidence={"rescan_passed": True},
    )

    stale = finding.with_freshness("aging").with_freshness("stale")

    assert stale.security_state == "resolved"
    assert stale.freshness_state == "stale"


def test_freshness_cannot_move_backward():
    with pytest.raises(ValueError, match="invalid_freshness_transition"):
        _finding(freshness_state="expired").with_freshness("fresh")


def test_secret_shaped_finding_text_is_rejected():
    with pytest.raises(ValueError, match="finding_secret_detected"):
        _finding(redacted_summary="api_key=sk-test-secret-material-123456")

    with pytest.raises(ValueError, match="finding_secret_detected"):
        _finding(rule_id="api_key=sk-test-secret-material-123456")


def test_finding_identifiers_reject_unredacted_paths():
    with pytest.raises(ValueError, match="finding_unredacted_material"):
        _finding(request_scope_id="/srv/plastic-promise/request-scope")


@pytest.mark.parametrize(
    "field_value",
    [
        "Apply the fix in /srv/app/service.py.",
        "user: paste the complete source file here",
        "prompt: include the original conversation",
    ],
)
def test_unredacted_paths_and_transcript_material_are_rejected(field_value):
    with pytest.raises(ValueError, match="finding_unredacted_material"):
        _finding(remediation_pattern=field_value)
