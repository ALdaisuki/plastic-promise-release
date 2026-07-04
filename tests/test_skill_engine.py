import asyncio
import json

import pytest
from plastic_promise.skills.engine import SkillDef, SkillResult, SkillRegistrationError


class TestSkillDef:
    def test_minimal_definition(self):
        """Minimum viable SkillDef has name, domain, description, tier."""

        async def noop(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

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
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

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
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

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


# ──────────────────────────────────────────────
# AtomRegistry tests (Task 2)
# ──────────────────────────────────────────────

from unittest.mock import MagicMock


def _make_mock_tool(name: str):
    """Create a minimal mock tool with a .name attribute — mimics MCP Tool objects."""
    tool = MagicMock()
    tool.name = name
    return tool


class TestAtomRegistry:
    def test_build_returns_core_atoms(self):
        """AtomRegistry.build() must include all P0 atoms."""
        from plastic_promise.skills.engine import AtomRegistry

        engine = MagicMock()
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "principle_activate",
                "context_supply",
                "memory_store",
                "memory_recall",
                "memory_stats",
                "defense",
                "domain",
                "system",
                "skill_session_start",
                "skill_session_complete",
                "memory_gc",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert "principle_activate" in registry
        assert "context_supply" in registry
        assert "memory_store" in registry
        assert "memory_recall" in registry
        assert callable(registry["principle_activate"])

    def test_build_includes_p1_p2_atoms(self):
        """All tools from the MCP server tool list must be included."""
        from plastic_promise.skills.engine import AtomRegistry

        engine = MagicMock()
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "audit_run",
                "pack_export",
                "skill_session_trace",
                "memory_gc",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert "audit_run" in registry
        assert "pack_export" in registry
        assert "skill_session_trace" in registry
        assert "memory_gc" in registry

    def test_build_returns_different_callables(self):
        """Each atom callable must be a distinct function."""
        from plastic_promise.skills.engine import AtomRegistry

        engine = MagicMock()
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "principle_activate",
                "memory_store",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert registry["principle_activate"] is not registry["memory_store"]

    def test_unknown_atom_raises(self):
        """Calling an unregistered atom should raise KeyError."""
        from plastic_promise.skills.engine import AtomRegistry

        engine = MagicMock()
        engine.list_tools = MagicMock(return_value=[])
        registry = AtomRegistry.build(engine)
        assert "nonexistent" not in registry


# ──────────────────────────────────────────────
# SkillEngine.register tests (Task 3)
# ──────────────────────────────────────────────


class TestSkillEngineRegister:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        # Mock list_tools to return a tool set that includes the atoms we test with
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "memory_store",
                "memory_recall",
                "principle_activate",
                "context_supply",
                "domain",
                "system",
                "defense",
                "memory_gc",
                "skill_session_start",
                "skill_session_complete",
                "issue_create",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _noop_handler(self):
        async def h(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        return h

    def test_register_valid_skill(self, mock_engine):
        """A valid P0 skill should register without error."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="session-init",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["principle_activate", "context_supply", "memory_store"],
            degrade_map={"domain": "skip"},
            handler=self._noop_handler(),
            allowed_callers=["claude"],
        )
        se.register(sd)  # should not raise
        assert "session-init" in se._registry

    def test_register_duplicate_raises(self, mock_engine):
        """Registering the same skill name twice must raise SkillRegistrationError."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="session-init",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["principle_activate"],
            degrade_map={},
            handler=self._noop_handler(),
            allowed_callers=["claude"],
        )
        se.register(sd)
        with pytest.raises(SkillRegistrationError, match="already registered"):
            se.register(sd)

    def test_register_missing_atom_raises(self, mock_engine):
        """A skill declaring an atom not in MCP tools must raise."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="bad-skill",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["nonexistent_atom"],
            degrade_map={},
            handler=self._noop_handler(),
            allowed_callers=["claude"],
        )
        with pytest.raises(SkillRegistrationError, match="nonexistent_atom"):
            se.register(sd)

    def test_register_p2_with_non_daemon_caller_raises(self, mock_engine):
        """P2 skills must only allow daemon or admin callers."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="scheduled-gc",
            domain="system_health",
            description="GC",
            tier="P2",
            atoms=["memory_gc"],
            degrade_map={"memory_gc": "abort"},
            handler=self._noop_handler(),
            allowed_callers=["claude"],  # invalid for P2
        )
        with pytest.raises(SkillRegistrationError, match="P2"):
            se.register(sd)

    def test_register_p2_with_daemon_succeeds(self, mock_engine):
        """P2 skills with daemon caller must succeed."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="scheduled-gc",
            domain="system_health",
            description="GC",
            tier="P2",
            atoms=["memory_gc"],
            degrade_map={"memory_gc": "abort"},
            handler=self._noop_handler(),
            allowed_callers=["daemon"],
        )
        se.register(sd)  # should not raise
        assert "scheduled-gc" in se._registry

    def test_register_all_superpowers_stages(self, mock_engine):
        """Every SuperPowers stage must declare only registered atoms."""
        from plastic_promise.skills.engine import SkillEngine
        from plastic_promise.skills.superpowers_stages import SKILL_DEFS

        se = SkillEngine(mock_engine)
        for skill_def in SKILL_DEFS.values():
            se.register(skill_def)

        assert set(se._registry) >= {skill_def.name for skill_def in SKILL_DEFS.values()}

    def test_superpowers_stage_atoms_are_registered(self, mock_engine):
        """Stage atom lists must stay in sync with AtomRegistry."""
        from plastic_promise.skills.engine import AtomRegistry
        from plastic_promise.skills.superpowers_stages import STAGE_ATOMS

        registered_atoms = set(AtomRegistry.build(mock_engine))
        stage_atoms = {atom for atoms in STAGE_ATOMS.values() for atom in atoms}

        assert stage_atoms <= registered_atoms


# ──────────────────────────────────────────────
# SkillEngine.exec tests (Task 4)
# ──────────────────────────────────────────────

from mcp.types import TextContent


class TestSkillEngineExec:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [
            _make_mock_tool(n)
            for n in [
                "principle_activate",
                "context_supply",
                "memory_store",
                "memory_recall",
                "domain",
                "system",
                "defense",
                "memory_gc",
                "skill_session_start",
                "skill_session_complete",
            ]
        ]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    @pytest.mark.asyncio
    async def test_exec_successful_skill(self, mock_engine):
        """A simple skill with one atom must execute and return success."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)

        # Override atom with a mock that succeeds
        async def mock_principle_activate(engine, args):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"task_type": "general", "activated": [{"id": 1, "name": "test-principle"}]}
                    ),
                )
            ]

        se._atoms["principle_activate"] = mock_principle_activate

        # Mock skill_session_start/complete to record calls
        session_calls = []

        async def mock_session_start(engine, args):
            session_calls.append(("start", args))
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"entity_id": "skill:test:2026-01-01T00:00:00", "status": "active"}
                    ),
                )
            ]

        async def mock_session_complete(engine, args):
            session_calls.append(("complete", args))
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]

        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atom_results):
            return SkillResult(
                skill_name="test-skill",
                success=True,
                data={"activated": json.loads(atom_results["principle_activate"][0].text)},
                atom_results={k: v[0].text for k, v in atom_results.items()},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="test-skill",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["principle_activate"],
            degrade_map={},
            handler=handler,
            allowed_callers=["claude"],
        )
        se.register(sd)

        result = await se.exec("test-skill", params={"task_description": "test"}, caller="claude")
        assert result.success is True
        assert result.skill_name == "test-skill"
        assert len(session_calls) == 2  # start + complete
        assert session_calls[0][0] == "start"
        assert session_calls[1][0] == "complete"

    @pytest.mark.asyncio
    async def test_exec_unauthorized_caller_blocked(self, mock_engine):
        """Caller not in allowed_callers must be rejected before any atom call."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)

        async def handler(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="daemon-only",
            domain="system_health",
            description="Test",
            tier="P2",
            atoms=["memory_gc"],
            degrade_map={},
            handler=handler,
            allowed_callers=["daemon"],
        )
        se.register(sd)

        result = await se.exec("daemon-only", params={}, caller="claude")
        assert result.success is False
        assert "not in allowed_callers" in result.errors[0]

    @pytest.mark.asyncio
    async def test_exec_unknown_skill_returns_error(self, mock_engine):
        """Calling a non-existent skill must return a failure result."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        result = await se.exec("nonexistent", params={}, caller="claude")
        assert result.success is False
        assert "Unknown skill" in result.errors[0]

    @pytest.mark.asyncio
    async def test_exec_atom_degraded_skip(self, mock_engine):
        """When an atom fails with degrade_map='skip', execution continues."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)

        call_order = []

        async def mock_failing_atom(engine, args):
            call_order.append("failing")
            raise RuntimeError("simulated failure")

        async def mock_ok_atom(engine, args):
            call_order.append("ok")
            return [TextContent(type="text", text=json.dumps({"status": "ok"}))]

        async def mock_session_start(engine, args):
            return [TextContent(type="text", text=json.dumps({"entity_id": "skill:test:..."}))]

        async def mock_session_complete(engine, args):
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]

        se._atoms["atom_a"] = mock_failing_atom
        se._atoms["atom_b"] = mock_ok_atom
        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="degrade-skip-test",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["atom_a", "atom_b"],
            degrade_map={"atom_a": "skip"},
            handler=handler,
            allowed_callers=["claude"],
        )
        se.register(sd)

        result = await se.exec("degrade-skip-test", params={}, caller="claude")
        assert result.success is True
        assert call_order == ["failing", "ok"]  # atom_b executed despite atom_a failure
        assert any("atom_a" in log for log in result.degrade_log)

    @pytest.mark.asyncio
    async def test_exec_atom_timeout_uses_degrade_map(self, mock_engine):
        """A slow async atom must degrade instead of blocking the whole skill."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_slow_atom(engine, args):
            call_order.append("slow")
            await asyncio.sleep(0.2)
            return [TextContent(type="text", text=json.dumps({"status": "slow"}))]

        async def mock_ok_atom(engine, args):
            call_order.append("ok")
            return [TextContent(type="text", text=json.dumps({"status": "ok"}))]

        async def mock_session_start(engine, args):
            return [TextContent(type="text", text=json.dumps({"entity_id": "skill:test:..."}))]

        async def mock_session_complete(engine, args):
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]

        se._atoms["atom_a"] = mock_slow_atom
        se._atoms["atom_b"] = mock_ok_atom
        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="timeout-skip-test",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["atom_a", "atom_b"],
            degrade_map={"atom_a": "skip"},
            handler=handler,
            allowed_callers=["claude"],
            atom_timeout_seconds=0.01,
        )
        se.register(sd)

        result = await se.exec("timeout-skip-test", params={}, caller="claude")
        assert result.success is True
        assert call_order == ["slow", "ok"]
        assert any("atom_a" in log and "timed out" in log for log in result.degrade_log)

    @pytest.mark.asyncio
    async def test_exec_lifecycle_timeout_degrades_completion(self, mock_engine):
        """Slow session completion must not block the whole skill execution."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)

        async def mock_session_start(engine, args):
            return [TextContent(type="text", text=json.dumps({"entity_id": "skill:test:..."}))]

        async def mock_session_complete(engine, args):
            await asyncio.sleep(0.2)
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]

        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={"ok": True},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="complete-timeout-test",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=[],
            degrade_map={},
            handler=handler,
            allowed_callers=["claude"],
            atom_timeout_seconds=0.01,
        )
        se.register(sd)

        result = await se.exec("complete-timeout-test", params={}, caller="claude")

        assert result.success is True
        assert any("skill_session_complete" in log for log in result.degrade_log)

    @pytest.mark.asyncio
    async def test_exec_atom_degraded_abort(self, mock_engine):
        """When an atom fails with default degrade='abort', execution stops."""
        from plastic_promise.skills.engine import SkillEngine

        se = SkillEngine(mock_engine)

        call_order = []

        async def mock_failing_atom(engine, args):
            call_order.append("failing")
            raise RuntimeError("simulated failure")

        async def mock_never_reached(engine, args):
            call_order.append("never")
            return [TextContent(type="text", text="ok")]

        async def mock_session_start(engine, args):
            return [TextContent(type="text", text=json.dumps({"entity_id": "skill:test:..."}))]

        async def mock_session_complete(engine, args):
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]

        se._atoms["atom_a"] = mock_failing_atom
        se._atoms["atom_b"] = mock_never_reached
        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atoms):
            return SkillResult(
                skill_name="test",
                success=True,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[],
            )

        sd = SkillDef(
            name="degrade-abort-test",
            domain="session_lifecycle",
            description="Test",
            tier="P0",
            atoms=["atom_a", "atom_b"],
            degrade_map={},  # atom_a defaults to "abort"
            handler=handler,
            allowed_callers=["claude"],
        )
        se.register(sd)

        result = await se.exec("degrade-abort-test", params={}, caller="claude")
        assert result.success is False
        assert call_order == ["failing"]  # atom_b was never called
        assert "atom_a" in result.errors[0]
