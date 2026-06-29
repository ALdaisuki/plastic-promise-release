"""Multi-Agent Team E2E — constitution + trust + permissions + identity + supervisor"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMultiAgentE2E:
    def test_validator_rejects_incomplete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context
        result = validate_issue_context({"context": {"files": ["a.py"]}})
        assert "error" in result
        assert "interfaces" in result["error"]
        assert "acceptance" in result["error"]

    def test_validator_accepts_complete_issue(self):
        from plastic_promise.core.issue_validator import validate_issue_context
        issue = {"context": {
            "files": ["a.py"], "interfaces": "def f():", "acceptance": "pytest"
        }}
        assert validate_issue_context(issue)["valid"] is True

    def test_trust_tier_boundaries(self):
        from plastic_promise.core.issue_validator import get_tier
        assert get_tier(0.90) == "autonomous"
        assert get_tier(0.80) == "autonomous"
        assert get_tier(0.79) == "standard"
        assert get_tier(0.60) == "standard"
        assert get_tier(0.59) == "restricted"
        assert get_tier(0.30) == "restricted"
        assert get_tier(0.29) == "readonly"

    def test_permission_escalation(self):
        from plastic_promise.core.issue_validator import check_permission
        assert check_permission("readonly", "read") == "granted"
        assert check_permission("readonly", "write_file") == "denied"
        assert check_permission("restricted", "write_file") == "needs_review"
        assert check_permission("standard", "write_file") == "granted"
        assert check_permission("standard", "assign_task") == "denied"
        assert check_permission("autonomous", "assign_task") == "granted"

    def test_validate_deliverable(self):
        from plastic_promise.core.issue_validator import validate_deliverable
        ok = {"context": {"files": ["a.py"], "interfaces": "f", "acceptance": "t", "deliverable": ["a.py"]}}
        assert validate_deliverable(ok)["valid"] is True
        bad = {"context": {"files": ["a.py"], "interfaces": "f", "acceptance": "t", "deliverable": []}}
        assert "error" in validate_deliverable(bad)

    def test_builder_identity(self):
        """验证 AGENT_OWNER→ROLE/DOMAIN 映射（模块级常量，用 reload 避免缓存）"""
        import importlib, plastic_promise.agent as mod
        os.environ["AGENT_OWNER"] = "pi_builder"
        importlib.reload(mod)
        assert mod.ROLE == "pi_builder"
        assert mod.DOMAIN == "building"

    def test_fixer_identity(self):
        import importlib, plastic_promise.agent as mod
        os.environ["AGENT_OWNER"] = "pi_fixer"
        importlib.reload(mod)
        assert mod.ROLE == "pi_fixer"
        assert mod.DOMAIN == "fixing"

    def test_reviewer_identity(self):
        import importlib, plastic_promise.agent as mod
        os.environ["AGENT_OWNER"] = "pi_reviewer"
        importlib.reload(mod)
        assert mod.ROLE == "pi_reviewer"
        assert mod.DOMAIN == "reflecting"

    def test_supervisor_status(self):
        from agent_supervisor import AgentSupervisor
        sup = AgentSupervisor()
        status = sup.status()
        for name in ["pi_builder", "pi_fixer", "pi_reviewer"]:
            assert name in status
            assert status[name]["status"] == "stopped"
