import pytest
from plastic_promise.skills.engine import SkillDef, SkillResult, SkillRegistrationError
from dataclasses import FrozenInstanceError


class TestSkillDef:
    def test_minimal_definition(self):
        """Minimum viable SkillDef has name, domain, description, tier."""
        async def noop(ctx, params, atoms):
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])

        sd = SkillDef(
            name="test-skill",
            domain="session_lifecycle",
            description="A test skill",
            tier="P0",
            atoms=[],
            degrade_map={},
            handler=noop,
            allowed_callers=["claude"],
        )
        assert sd.name == "test-skill"
        assert sd.domain == "session_lifecycle"
        assert sd.tier == "P0"
        assert sd.cross_agent is False
        assert sd.trust_required == 0.0

    def test_cross_agent_skill(self):
        """Cross-agent skills set cross_agent=True and require trust."""
        async def noop(ctx, params, atoms):
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])

        sd = SkillDef(
            name="delegate-to-pi",
            domain="collaboration",
            description="Delegate a task to Pi",
            tier="P0",
            atoms=["memory_store", "issue_create"],
            degrade_map={"issue_create": "abort"},
            handler=noop,
            allowed_callers=["claude", "pi"],
            cross_agent=True,
            trust_required=0.60,
        )
        assert sd.cross_agent is True
        assert sd.trust_required == 0.60

    def test_p2_skill(self):
        """P2 skills must have daemon or admin callers."""
        async def noop(ctx, params, atoms):
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])

        sd = SkillDef(
            name="scheduled-gc",
            domain="system_health",
            description="Scheduled GC run",
            tier="P2",
            atoms=["memory_gc"],
            degrade_map={"memory_gc": "abort"},
            handler=noop,
            allowed_callers=["daemon"],
        )
        assert sd.tier == "P2"
        assert "daemon" in sd.allowed_callers


class TestSkillResult:
    def test_success_result(self):
        result = SkillResult(
            skill_name="session-init",
            success=True,
            data={"context_pack": {"core": []}},
            atom_results={"principle_activate": {"activated": []}},
            degrade_log=[],
            audit_trail={"entity_id": "skill:session-init:2026-..."},
            errors=[],
        )
        assert result.success is True
        assert len(result.errors) == 0

    def test_failure_with_degradation(self):
        result = SkillResult(
            skill_name="session-init",
            success=True,  # partial success — degraded but not failed
            data={},
            atom_results={},
            degrade_log=["domain: skip — DomainManager not available"],
            audit_trail={"entity_id": "skill:session-init:2026-..."},
            errors=[],
        )
        assert result.success is True
        assert "DomainManager" in result.degrade_log[0]


class TestSkillRegistrationError:
    def test_is_exception(self):
        with pytest.raises(SkillRegistrationError) as exc:
            raise SkillRegistrationError("Atom 'nonexistent' not found")
        assert "nonexistent" in str(exc.value)
