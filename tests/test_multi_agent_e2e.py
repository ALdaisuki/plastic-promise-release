"""Multi-Agent Team E2E — 宪法 + 信任矩阵 + 权限 集成验证"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMultiAgentE2E:
    def test_validator_rejects_incomplete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context

        result = validate_issue_context({"context": {"files": ["a.py"]}})
        assert "error" in result
        assert "interfaces" in result["error"]

    def test_validator_accepts_complete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context

        issue = {"context": {"files": ["a.py"], "interfaces": "f", "acceptance": "pytest"}}
        assert validate_issue_context(issue)["valid"] is True

    def test_trust_tier_boundaries(self):
        from plastic_promise.core.issue_validator import get_tier

        assert get_tier(0.90) == "autonomous"
        assert get_tier(0.80) == "autonomous"
        assert get_tier(0.79) == "standard"
        assert get_tier(0.30) == "restricted"
        assert get_tier(0.29) == "readonly"

    def test_permission_escalation(self):
        from plastic_promise.core.issue_validator import check_permission

        assert check_permission("readonly", "read") == "granted"
        assert check_permission("readonly", "write_file") == "denied"
        assert check_permission("restricted", "write_file") == "needs_review"
        assert check_permission("autonomous", "assign_task") == "granted"
        assert check_permission("standard", "assign_task") == "denied"

    def test_validate_deliverable(self):
        from plastic_promise.core.issue_validator import validate_deliverable

        ok_c = {"files": ["a.py"], "interfaces": "f", "acceptance": "t", "deliverable": ["a.py"]}
        assert validate_deliverable({"context": ok_c})["valid"] is True
        bad_c = {"files": ["a.py"], "interfaces": "f", "acceptance": "t", "deliverable": []}
        assert "error" in validate_deliverable({"context": bad_c})

    def test_get_tier_info(self):
        from plastic_promise.core.issue_validator import get_tier_info

        info = get_tier_info(0.85)
        assert info["tier"] == "autonomous"
        assert info["motto"] == "放手干，结果负责"
