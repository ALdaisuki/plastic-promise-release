"""End-to-end test: Phase 1 skills working together with a real ContextEngine.

Tests the full pipeline: SkillEngine orchestrates skill execution with real
ContextEngine persistence, atoms degrade gracefully, and stored memories are
retrievable via the engine's get_memory() interface.
"""

import json

import pytest
from mcp.types import TextContent

from plastic_promise.core.context_engine import ContextEngine, MemoryRecord
from plastic_promise.skills.engine import SkillEngine
from plastic_promise.skills.session_lifecycle import skill_session_init
from plastic_promise.skills.memory_operations import skill_smart_remember


# ──────────────────────────────────────────────
# Mock helpers
# ──────────────────────────────────────────────


def _tool(name: str):
    """Create a minimal tool-like object with a .name attribute.

    Used to patch ContextEngine.list_tools() so AtomRegistry validation
    passes (the validation checks that declared atoms exist in the tool set).
    """
    return type("Tool", (), {"name": name})()


def _response(data: dict) -> list[TextContent]:
    """Wrap a dict as an MCP TextContent response — matches real atom handler output."""
    return [TextContent(type="text", text=json.dumps(data))]


def _failing_atom(engine, args):
    """Atom that always raises — simulates a degraded component."""
    raise RuntimeError(f"Simulated failure: atom not available in test env")


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine():
    """Real ContextEngine — Python fallback (no Rust required).

    All initialization (DomainManager, LanceDB, embedder) uses try/except
    internally, so it degrades gracefully in test environments where these
    dependencies may be unavailable.
    """
    ctx = ContextEngine(use_sqlite=False)
    # Patch list_tools() to return tool names matching our skill atoms.
    # AtomRegistry validation checks atom names against the MCP tool listing.
    ctx.list_tools = lambda: [
        _tool(n) for n in [
            "principle_activate", "context_supply", "memory_store",
            "memory_recall", "memory_update", "domain", "system",
            "defense", "memory_gc", "skill_session_start",
            "skill_session_complete",
        ]
    ]
    return ctx


@pytest.fixture
def skill_engine(engine):
    """SkillEngine with real ContextEngine + test atom implementations.

    Atoms are overridden with mocks that:
    - Interact with the real ContextEngine (store/retrieve memories)
    - Return proper MCP-style TextContent responses
    - Can simulate failure for degradation testing
    """
    se = SkillEngine(engine)

    # Override skill_session_start/complete with lightweight mocks
    _counter = [0]

    async def mock_session_start(ctx, args):
        _counter[0] += 1
        return _response({
            "entity_id": f"skill:{args.get('skill_name', 'test')}:{_counter[0]}",
            "status": "active",
        })

    async def mock_session_complete(ctx, args):
        return _response({"status": "done"})

    se._atoms["skill_session_start"] = mock_session_start
    se._atoms["skill_session_complete"] = mock_session_complete

    # principle_activate: returns a basic success response
    async def mock_principle_activate(ctx, args):
        return _response({
            "activated": [{"id": 1, "name": "Occam's Razor"}],
            "count": 1,
        })
    se._atoms["principle_activate"] = mock_principle_activate

    # context_supply: returns empty context pack
    async def mock_context_supply(ctx, args):
        return _response({
            "core": [], "related": [], "divergent": [],
        })
    se._atoms["context_supply"] = mock_context_supply

    # memory_store: actually persists to the real ContextEngine
    async def mock_memory_store(ctx, args):
        content = args.get("content", "")
        mtype = args.get("memory_type", "experience")
        source = args.get("source", "user")
        record = MemoryRecord(content=content, memory_type=mtype, source=source)
        mem_id = ctx.store_memory(record)
        return _response({
            "stored": True,
            "memory_id": mem_id,
            "content_preview": content[:200],
        })
    se._atoms["memory_store"] = mock_memory_store

    # memory_recall: returns empty results (no duplicates)
    async def mock_memory_recall(ctx, args):
        return _response({
            "core": [], "related": [], "divergent": [],
        })
    se._atoms["memory_recall"] = mock_memory_recall

    # domain, system, defense, memory_gc: successful mocks
    async def mock_domain(ctx, args):
        return _response({"domains": {"building": {"score": 0.8}}})
    se._atoms["domain"] = mock_domain

    async def mock_system(ctx, args):
        return _response({"memory": {"total": 0, "healthy": 0, "decaying": 0}})
    se._atoms["system"] = mock_system

    async def mock_defense(ctx, args):
        return _response({"trust": 0.75, "tier": "standard"})
    se._atoms["defense"] = mock_defense

    async def mock_memory_gc(ctx, args):
        return _response({"dry_run": True, "candidates_count": 0})
    se._atoms["memory_gc"] = mock_memory_gc

    # Register the two Phase 1 skills
    se.register(skill_session_init)
    se.register(skill_smart_remember)

    return se


# ──────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestPhase1E2E:
    """Phase 1 E2E tests — real ContextEngine, mock atoms, end-to-end pipeline."""

    async def test_session_init_registers(self, skill_engine):
        """session-init must be registered and callable.

        Verifies the 7-atom chain executes: atoms with degrade_map "skip"
        (domain, system, memory_gc) may fail without aborting the skill.
        """
        result = await skill_engine.exec("session-init", params={
            "task_description": "E2E test of session-init",
            "task_type": "general",
        }, caller="claude")

        # Skill execution should record its name
        assert result.skill_name == "session-init"

        # Domain, system, memory_gc may degrade in test env — but the skill
        # MUST still succeed because their degrade_map is "skip"
        assert result.success is True, (
            f"session-init failed. Errors: {result.errors}, "
            f"Degrade log: {result.degrade_log}"
        )

        # Handler-produced data should be present
        assert "context" in result.data
        assert "domain_health" in result.data
        assert "system_stats" in result.data
        assert "trust" in result.data

    async def test_smart_remember_stores_memory(self, skill_engine):
        """smart-remember must store a new memory when no duplicate exists."""
        result = await skill_engine.exec("smart-remember", params={
            "content": "E2E test: the sky is blue",
            "memory_type": "experience",
            "source": "test",
        }, caller="claude")

        assert result.success is True, (
            f"smart-remember failed. Errors: {result.errors}"
        )
        assert result.data["action"] == "stored"
        assert result.data["memory_id"] != ""

    async def test_full_workflow(self, skill_engine):
        """Phase 1 pipeline: init -> remember -> verify in memory.

        Full end-to-end test:
        1. Start a session (session-init)
        2. Store a memory (smart-remember)
        3. Verify the memory is retrievable via ContextEngine.get_memory()
        """
        # 1. Initialize session
        init_result = await skill_engine.exec("session-init", params={
            "task_description": "E2E full workflow test",
        }, caller="claude")
        assert init_result.success is True, (
            f"session-init failed. Errors: {init_result.errors}"
        )

        # 2. Store a memory via smart-remember
        remember_result = await skill_engine.exec("smart-remember", params={
            "content": "E2E workflow: Python is the preferred language",
            "memory_type": "experience",
            "source": "test",
        }, caller="claude")
        assert remember_result.success is True, (
            f"smart-remember failed. Errors: {remember_result.errors}"
        )
        memory_id = remember_result.data["memory_id"]
        assert memory_id != "", "smart-remember did not return a memory_id"

        # 3. Verify memory exists in the real ContextEngine
        mem = skill_engine._ctx.get_memory(memory_id)
        assert mem is not None, (
            f"Memory '{memory_id}' not found in ContextEngine after smart-remember"
        )
        assert mem.content == "E2E workflow: Python is the preferred language"
        assert mem.memory_type == "experience"
        assert mem.source == "test"

    async def test_session_init_degraded_domain_skip(self, skill_engine):
        """When domain atom fails with degrade='skip', session-init must continue.

        This uses the same skill_engine fixture but temporarily re-registers
        the domain atom to simulate failure. The degrade_map says "skip" for
        domain, so execution should proceed past the failure.
        """
        # Replace the domain atom with a failing one
        skill_engine._atoms["domain"] = _failing_atom

        result = await skill_engine.exec("session-init", params={
            "task_description": "E2E degradation test",
            "task_type": "general",
        }, caller="claude")

        # Skill must succeed even though domain atom failed
        assert result.success is True, (
            f"session-init failed despite degrade_map having domain=skip. "
            f"Errors: {result.errors}"
        )
        # The degrade log should record the skipped domain atom
        assert any("domain" in log for log in result.degrade_log), (
            f"Expected domain failure recorded in degrade_log. "
            f"Got: {result.degrade_log}"
        )

    async def test_smart_remember_handles_duplicate(self, skill_engine):
        """smart-remember must update when memory_recall returns a duplicate.

        This test pre-seeds a memory in the engine, then checks that
        smart-remember detects it as a duplicate and reports 'updated'.
        """
        # Pre-seed a memory so recall finds a duplicate
        record = MemoryRecord(
            content="E2E duplicate test: tab over spaces",
            memory_type="experience",
            source="test",
        )
        seed_id = skill_engine._ctx.store_memory(record)

        # Override memory_recall to return the seeded memory as a duplicate
        async def mock_recall_with_dupe(ctx, args):
            return _response({
                "core": [
                    {"id": seed_id, "content": "E2E duplicate test: tab over spaces",
                     "relevance": 0.92},
                ],
                "related": [],
                "divergent": [],
            })
        skill_engine._atoms["memory_recall"] = mock_recall_with_dupe

        result = await skill_engine.exec("smart-remember", params={
            "content": "E2E duplicate test: tab over spaces",
            "memory_type": "experience",
            "source": "test",
        }, caller="claude")

        assert result.success is True, (
            f"smart-remember duplicate handling failed. Errors: {result.errors}"
        )
        assert result.data["action"] == "updated", (
            f"Expected 'updated', got '{result.data.get('action')}'"
        )
        assert result.data.get("duplicate_of") == seed_id

    async def test_unknown_skill_returns_error(self, skill_engine):
        """Calling an unregistered skill returns a failure result."""
        result = await skill_engine.exec("nonexistent-skill", params={}, caller="claude")
        assert result.success is False
        assert "Unknown skill" in result.errors[0]

    async def test_unauthorized_caller_blocked(self, skill_engine):
        """A caller not in allowed_callers must be rejected before atoms run."""
        # smart-remember allows claude/pi; test with "unauthorized"
        result = await skill_engine.exec("smart-remember", params={
            "content": "should not run",
        }, caller="unauthorized")
        assert result.success is False
        assert "not in allowed_callers" in result.errors[0]
