"""Issue Validator + Trust-Freedom Matrix 测试"""

import pytest
from plastic_promise.core.issue_validator import (
    validate_issue_context,
    validate_deliverable,
    get_tier,
    check_permission,
    REQUIRED_CONTEXT,
)


class TestIssueValidator:
    def test_valid_context_passes(self):
        issue = {"context": {"files": ["a.py"], "interfaces": "def f():", "acceptance": "pytest"}}
        result = validate_issue_context(issue)
        assert result["valid"] is True

    def test_missing_context_rejected(self):
        issue = {"context": {"files": ["a.py"]}}
        result = validate_issue_context(issue)
        assert "error" in result
        assert "NEEDS_CONTEXT" in result["error"]
        assert "interfaces" in result["error"]

    def test_empty_context_rejected(self):
        issue = {"context": {}}
        result = validate_issue_context(issue)
        assert "error" in result

    def test_get_tier_autonomous(self):
        assert get_tier(0.90) == "autonomous"
        assert get_tier(0.80) == "autonomous"

    def test_get_tier_standard(self):
        assert get_tier(0.70) == "standard"
        assert get_tier(0.60) == "standard"

    def test_get_tier_restricted(self):
        assert get_tier(0.50) == "restricted"
        assert get_tier(0.30) == "restricted"

    def test_get_tier_readonly(self):
        assert get_tier(0.20) == "readonly"
        assert get_tier(0.0) == "readonly"

    def test_permission_autonomous_can_assign(self):
        assert check_permission("autonomous", "assign_task") == "granted"

    def test_permission_standard_cannot_assign(self):
        assert check_permission("standard", "assign_task") == "denied"

    def test_permission_restricted_needs_review_for_write(self):
        assert check_permission("restricted", "write_file") == "needs_review"

    def test_permission_readonly_cannot_write(self):
        assert check_permission("readonly", "write_file") == "denied"

    def test_validate_deliverable_valid_passes(self):
        issue = {
            "context": {
                "files": ["a.py"],
                "interfaces": "def f():",
                "acceptance": "pytest",
                "deliverable": [
                    "plastic_promise/core/issue_validator.py",
                    "tests/test_issue_validator.py",
                ],
            }
        }
        result = validate_deliverable(issue)
        assert result["valid"] is True

    def test_validate_deliverable_missing_rejected(self):
        issue = {
            "context": {
                "files": ["a.py"],
                "interfaces": "def f():",
                "acceptance": "pytest",
            }
        }
        result = validate_deliverable(issue)
        assert "error" in result
        assert "NEEDS_DELIVERABLE" in result["error"]
