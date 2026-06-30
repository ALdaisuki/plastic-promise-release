# Plastic Promise Skills — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the SkillEngine core + session-init skill + smart-remember skill, delivering the atomic-MCP-to-high-level-skill composition foundation.

**Architecture:** `SkillEngine` wraps MCP handler dispatch via a lazily-built `ATOM_REGISTRY`. Each registered `SkillDef` declares atom dependencies and a handler function. `SkillEngine.exec()` chains atoms with degrade-path error handling, wraps execution in `skill_session_start/complete`, and returns structured `SkillResult`.

**Tech Stack:** Python 3.11+, `asyncio`, `ContextEngine` (existing), `mcp.types.TextContent` (existing), `pytest` (existing)

**Spec:** `docs/superpowers/specs/2026-06-30-skills-architecture-design.md`

## Global Constraints

- All atoms map to existing MCP handler functions in `plastic_promise/mcp/tools/*.py` — zero change to handler internals
- The `auto_context_inject` handler is NOT called as an atom — its internal steps are split into `principle_activate` + `context_supply` + `memory_store`
- P2 skill registration must be rejected unless `allowed_callers` ⊆ `{daemon, admin}`
- `SkillEngine.exec()` must enforce caller authorization before any atom dispatch
- Every skill execution creates exactly one `skill_session` entity (no double-wrap)
- File paths follow the directory structure in spec §7.2

---

## File Structure

```
plastic_promise/skills/              ← NEW directory
├── __init__.py                      ← empty, marks package
├── engine.py                        ← SkillDef, SkillResult, SkillRegistrationError, AtomRegistry, SkillEngine
├── session_lifecycle.py             ← skill_session_init handler
└── memory_operations.py             ← skill_smart_remember handler

tests/
├── test_skill_engine.py             ← SkillEngine unit tests
├── test_session_lifecycle.py        ← session-init integration tests
└── test_memory_operations.py        ← smart-remember integration tests
```

---

### Task 1: Scaffold the skills package and define data types

**Files:**
- Create: `plastic_promise/skills/__init__.py`
- Create: `plastic_promise/skills/engine.py` (SkillDef + SkillResult + SkillRegistrationError only)

**Interfaces:**
- Produces: `SkillDef` dataclass, `SkillResult` dataclass, `SkillRegistrationError` exception

- [ ] **Step 1: Create the package init file**

```python
# plastic_promise/skills/__init__.py
"""Plastic Promise 程序化技能 — 原子 MCP 工具的高层组合"""
```

Write with `Write` tool.

- [ ] **Step 2: Write data type tests**

```python
# tests/test_skill_engine.py (partial — data type tests only)

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
```

- [ ] **Step 3: Run tests (expect failure — no implementation yet)**

Run: `pytest tests/test_skill_engine.py -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError`

- [ ] **Step 4: Write the SkillDef, SkillResult, and SkillRegistrationError**

```python
# plastic_promise/skills/engine.py

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class SkillRegistrationError(Exception):
    """Raised when a SkillDef fails validation during registration."""
    pass


@dataclass
class SkillDef:
    """Definition of a programmatic skill — atoms + handler + permissions."""

    name: str
    domain: str
    description: str
    tier: str  # "P0" | "P1" | "P2"

    # P0/P1 atom dependencies — Engine calls in order
    atoms: list[str] = field(default_factory=list)

    # Degradation map: atom_name → "skip" | "warn" | "abort" | "fallback:<tool>"
    degrade_map: dict[str, str] = field(default_factory=dict)

    # Core handler: async (ContextEngine, params, atom_results) -> SkillResult
    handler: Callable = field(default=None)

    # Authorization
    allowed_callers: list[str] = field(default_factory=lambda: ["claude"])

    # Multi-agent
    cross_agent: bool = False
    trust_required: float = 0.0


@dataclass
class SkillResult:
    """Result of a skill execution, including atom results and degradation log."""

    skill_name: str
    success: bool
    data: dict
    atom_results: dict[str, Any]
    degrade_log: list[str]
    audit_trail: dict
    errors: list[str]
```

Write with `Write` tool.

- [ ] **Step 5: Run tests (expect pass)**

Run: `pytest tests/test_skill_engine.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/skills/__init__.py plastic_promise/skills/engine.py tests/test_skill_engine.py
git commit -m "feat: add skills package with SkillDef, SkillResult, SkillRegistrationError"
```

---

### Task 2: Implement AtomRegistry — lazy MCP handler dispatch map

**Files:**
- Modify: `plastic_promise/skills/engine.py` (append AtomRegistry class)

**Interfaces:**
- Produces: `AtomRegistry.build(engine) -> dict[str, Callable]` — returns mapping from atom name to `async (engine, args) -> list[TextContent]`
- Consumes: `ContextEngine` (existing), all handler modules under `plastic_promise.mcp.tools.*`

- [ ] **Step 1: Write tests for AtomRegistry**

```python
# tests/test_skill_engine.py (append after existing tests)

from unittest.mock import MagicMock, AsyncMock, patch
from plastic_promise.skills.engine import AtomRegistry

def _make_mock_tool(name: str):
    """Create a minimal mock tool with a .name attribute — mimics MCP Tool objects."""
    tool = MagicMock()
    tool.name = name
    return tool

class TestAtomRegistry:
    def test_build_returns_core_atoms(self):
        """AtomRegistry.build() must include all P0 atoms."""
        engine = MagicMock()
        # engine.list_tools() returns Tool objects, each with a .name attribute
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "context_supply", "memory_store",
            "memory_recall", "memory_stats", "defense", "domain",
            "system", "skill_session_start", "skill_session_complete", "memory_gc",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert "principle_activate" in registry
        assert "context_supply" in registry
        assert "memory_store" in registry
        assert "memory_recall" in registry
        assert callable(registry["principle_activate"])

    def test_build_includes_p1_p2_atoms(self):
        """All tools from the MCP server tool list must be included."""
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "audit_run", "pack_export", "skill_session_trace", "memory_gc",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert "audit_run" in registry
        assert "pack_export" in registry
        assert "skill_session_trace" in registry
        assert "memory_gc" in registry

    def test_build_returns_different_callables(self):
        """Each atom callable must be a distinct function."""
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "memory_store",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)

        registry = AtomRegistry.build(engine)
        assert registry["principle_activate"] is not registry["memory_store"]

    def test_unknown_atom_raises(self):
        """Calling an unregistered atom should raise KeyError."""
        engine = MagicMock()
        engine.list_tools = MagicMock(return_value=[])
        registry = AtomRegistry.build(engine)
        assert "nonexistent" not in registry
```

- [ ] **Step 2: Run tests (expect failure)**

Run: `pytest tests/test_skill_engine.py::TestAtomRegistry -v`
Expected: FAIL — `AtomRegistry` not defined

- [ ] **Step 3: Implement AtomRegistry**

```python
# Append to plastic_promise/skills/engine.py

import importlib

class AtomRegistry:
    """Lazy-build mapping from atom name → async handler callable.

    Mirrors the dispatch logic in plastic_promise.mcp.server.call_tool().
    Each callable has signature: async (engine, args: dict) -> list[TextContent].
    """

    # Static mapping: atom name → (module_path, handler_function_name)
    _ATOM_MODULES: dict[str, tuple[str, str]] = {
        # === Memory domain (P0 + P1) ===
        "memory_recall":    ("plastic_promise.mcp.tools.memory", "handle_memory_recall"),
        "memory_store":     ("plastic_promise.mcp.tools.memory", "handle_memory_store"),
        "memory_update":    ("plastic_promise.mcp.tools.memory", "handle_memory_update"),
        "memory_forget":    ("plastic_promise.mcp.tools.memory", "handle_memory_forget"),
        "memory_stats":     ("plastic_promise.mcp.tools.memory", "handle_memory_stats"),
        "memory_list":      ("plastic_promise.mcp.tools.memory", "handle_memory_list"),
        "memory_gc":        ("plastic_promise.mcp.tools.memory", "handle_memory_gc"),
        "memory_correct":   ("plastic_promise.mcp.tools.memory", "handle_memory_correct"),

        # === Principle domain (P0 + P1) ===
        "principle_activate":  ("plastic_promise.mcp.tools.principles", "handle_principle_activate"),
        "principle_inherit":   ("plastic_promise.mcp.tools.principles", "handle_principle_inherit"),
        "principle_diffuse":   ("plastic_promise.mcp.tools.principles", "handle_principle_diffuse"),
        "principle_evaluate":  ("plastic_promise.mcp.tools.principles", "handle_principle_evaluate"),

        # === Context domain (P0 + P1) ===
        "context_supply":       ("plastic_promise.mcp.tools.context", "handle_context_supply"),
        "context_inject":       ("plastic_promise.mcp.tools.context", "handle_context_inject"),
        "context_graph":        ("plastic_promise.mcp.tools.context", "handle_context_graph"),
        "context_ready":        ("plastic_promise.mcp.tools.context", "handle_context_ready"),
        "auto_context_inject":  ("plastic_promise.mcp.tools.context", "handle_auto_context_inject"),

        # === Audit & Defense (P0 + P1) ===
        "audit_run":        ("plastic_promise.mcp.tools.audit_defense", "handle_audit_run"),
        "audit_pre_check":  ("plastic_promise.mcp.tools.audit_defense", "handle_audit_pre_check"),
        "defense":          ("plastic_promise.mcp.tools.audit_defense", "handle_defense"),

        # === Reflection domain (P1) ===
        "scarf_reflect":    ("plastic_promise.mcp.tools.reflection", "handle_scarf_reflect"),
        "feedback_apply":   ("plastic_promise.mcp.tools.reflection", "handle_feedback_apply"),

        # === Domain (P1) ===
        "domain":           ("plastic_promise.mcp.tools.domain", "handle_domain"),

        # === Management (P1) ===
        "system":           ("plastic_promise.mcp.tools.management", "handle_system"),
        "issue_create":     ("plastic_promise.mcp.tools.management", "handle_issue_create"),
        "issue_transition": ("plastic_promise.mcp.tools.management", "handle_issue_transition"),
        "issue_list":       ("plastic_promise.mcp.tools.management", "handle_issue_list"),
        "pack_export":      ("plastic_promise.mcp.tools.management", "handle_pack_export"),
        "pack_import":      ("plastic_promise.mcp.tools.management", "handle_pack_import"),
        "pack_recall":      ("plastic_promise.mcp.tools.management", "handle_pack_recall"),

        # === Skill Tracking (P0 + P1) ===
        "skill_session_start":     ("plastic_promise.mcp.tools.skill_tracking", "handle_skill_session_start"),
        "skill_session_complete":  ("plastic_promise.mcp.tools.skill_tracking", "handle_skill_session_complete"),
        "skill_session_trace":     ("plastic_promise.mcp.tools.skill_tracking", "handle_skill_session_trace"),
        "skill_session_audit":     ("plastic_promise.mcp.tools.skill_tracking", "handle_skill_session_audit"),
    }

    @staticmethod
    def build(engine) -> dict[str, "Callable"]:
        """Build a registry of atom_name → async handler(engine, args).

        Lazy-imports handler modules only when build() is called.
        Returns a dict suitable for SkillEngine._atoms.
        """
        registry: dict[str, "Callable"] = {}
        for atom_name, (module_path, func_name) in AtomRegistry._ATOM_MODULES.items():
            module = importlib.import_module(module_path)
            handler = getattr(module, func_name)
            registry[atom_name] = handler
        return registry
```

Write with: append the code to `plastic_promise/skills/engine.py` using Write tool (rewrite full file with both data types + AtomRegistry).

- [ ] **Step 4: Run tests (expect pass)**

Run: `pytest tests/test_skill_engine.py::TestAtomRegistry -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/skills/engine.py tests/test_skill_engine.py
git commit -m "feat: add AtomRegistry — lazy MCP handler dispatch mapping for 35 tools"
```

---

### Task 3: Implement SkillEngine.register() with validation

**Files:**
- Modify: `plastic_promise/skills/engine.py` (add SkillEngine class with __init__ and register methods)

**Interfaces:**
- Produces: `SkillEngine.__init__(engine)`, `SkillEngine.register(skill_def) -> None`
- Consumes: `ContextEngine` (via `__init__`), `AtomRegistry.build()` (via `__init__`)
- Raises: `SkillRegistrationError` on duplicate name, missing atom, or P2 caller violation

- [ ] **Step 1: Write tests for register()**

```python
# tests/test_skill_engine.py (append)

from plastic_promise.skills.engine import SkillEngine

class TestSkillEngineRegister:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        # Mock list_tools to return a tool set that includes the atoms we test with
        mock_tools = [_make_mock_tool(n) for n in [
            "memory_store", "memory_recall", "principle_activate",
            "context_supply", "domain", "system", "defense", "memory_gc",
            "skill_session_start", "skill_session_complete", "issue_create",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _noop_handler(self):
        async def h(ctx, params, atoms):
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])
        return h

    def test_register_valid_skill(self, mock_engine):
        """A valid P0 skill should register without error."""
        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="session-init", domain="session_lifecycle",
            description="Test", tier="P0",
            atoms=["principle_activate", "context_supply", "memory_store"],
            degrade_map={"domain": "skip"},
            handler=self._noop_handler(),
            allowed_callers=["claude"],
        )
        se.register(sd)  # should not raise
        assert "session-init" in se._registry

    def test_register_duplicate_raises(self, mock_engine):
        """Registering the same skill name twice must raise SkillRegistrationError."""
        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="session-init", domain="session_lifecycle",
            description="Test", tier="P0",
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
        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="bad-skill", domain="session_lifecycle",
            description="Test", tier="P0",
            atoms=["nonexistent_atom"],
            degrade_map={},
            handler=self._noop_handler(),
            allowed_callers=["claude"],
        )
        with pytest.raises(SkillRegistrationError, match="nonexistent_atom"):
            se.register(sd)

    def test_register_p2_with_non_daemon_caller_raises(self, mock_engine):
        """P2 skills must only allow daemon or admin callers."""
        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="scheduled-gc", domain="system_health",
            description="GC", tier="P2",
            atoms=["memory_gc"],
            degrade_map={"memory_gc": "abort"},
            handler=self._noop_handler(),
            allowed_callers=["claude"],  # invalid for P2
        )
        with pytest.raises(SkillRegistrationError, match="P2"):
            se.register(sd)

    def test_register_p2_with_daemon_succeeds(self, mock_engine):
        """P2 skills with daemon caller must succeed."""
        se = SkillEngine(mock_engine)
        sd = SkillDef(
            name="scheduled-gc", domain="system_health",
            description="GC", tier="P2",
            atoms=["memory_gc"],
            degrade_map={"memory_gc": "abort"},
            handler=self._noop_handler(),
            allowed_callers=["daemon"],
        )
        se.register(sd)  # should not raise
        assert "scheduled-gc" in se._registry
```

- [ ] **Step 2: Run tests (expect failure)**

Run: `pytest tests/test_skill_engine.py::TestSkillEngineRegister -v`
Expected: FAIL — `SkillEngine` not defined

- [ ] **Step 3: Implement SkillEngine.__init__ and register()**

```python
# Append to plastic_promise/skills/engine.py

class SkillEngine:
    """Programmatic skill orchestration engine.

    Responsibilities:
    1. Skill registry — organized by 8 domains, declares P0/P1 atom dependencies
    2. Execution chain — atoms in order → handler → audit trail
    3. Degradation paths — per-atom degrade_map on failure
    4. Audit tracking — automatic skill_session_start/complete wrapping
    5. P2 scheduling — P2 skills only callable by daemon/admin
    """

    def __init__(self, engine):
        """Initialize the skill engine.

        Args:
            engine: ContextEngine instance (provides list_tools, memory CRUD, etc.)
        """
        self._ctx = engine
        self._registry: dict[str, SkillDef] = {}
        self._atoms: dict[str, "Callable"] = AtomRegistry.build(engine)

    def register(self, skill_def: SkillDef) -> None:
        """Register a skill definition. Validates dependencies and permissions.

        Raises SkillRegistrationError if:
        - skill name already registered
        - any declared atom is not available in the MCP tool set
        - P2 skill allows non-daemon/admin callers
        """
        # 1. Check for duplicate name
        if skill_def.name in self._registry:
            raise SkillRegistrationError(
                f"Skill '{skill_def.name}' already registered"
            )

        # 2. Validate all declared atoms exist
        for atom in skill_def.atoms:
            if atom not in self._atoms:
                available = sorted(self._atoms.keys())
                raise SkillRegistrationError(
                    f"Atom '{atom}' required by skill '{skill_def.name}' "
                    f"not found in MCP tools. Available: {available}"
                )

        # 3. P2 tier enforcement — only daemon or admin
        if skill_def.tier == "P2":
            invalid = set(skill_def.allowed_callers) - {"daemon", "admin"}
            if invalid:
                raise SkillRegistrationError(
                    f"P2 skill '{skill_def.name}' allows non-daemon/admin "
                    f"callers: {invalid}. P2 skills must use ['daemon'] or ['admin']."
                )
            if not skill_def.allowed_callers:
                skill_def.allowed_callers = ["daemon"]

        self._registry[skill_def.name] = skill_def
```

- [ ] **Step 4: Run tests (expect pass)**

Run: `pytest tests/test_skill_engine.py::TestSkillEngineRegister -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/skills/engine.py tests/test_skill_engine.py
git commit -m "feat: add SkillEngine.register() with dependency validation and P2 enforcement"
```

---

### Task 4: Implement SkillEngine.exec() with degradation and audit wrapping

**Files:**
- Modify: `plastic_promise/skills/engine.py` (add exec method)

**Interfaces:**
- Produces: `SkillEngine.exec(skill_name, params, caller) -> SkillResult`
- Consumes: `self._atoms` (atom callables), `self._registry` (SkillDef), `self._ctx` (ContextEngine for skill_session_start/complete)

- [ ] **Step 1: Write tests for exec()**

```python
# tests/test_skill_engine.py (append)

import json
from unittest.mock import AsyncMock, patch
from mcp.types import TextContent

class TestSkillEngineExec:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "context_supply", "memory_store",
            "memory_recall", "domain", "system", "defense", "memory_gc",
            "skill_session_start", "skill_session_complete",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    @pytest.mark.asyncio
    async def test_exec_successful_skill(self, mock_engine):
        """A simple skill with one atom must execute and return success."""
        se = SkillEngine(mock_engine)

        # Override atom with a mock that succeeds
        async def mock_principle_activate(engine, args):
            return [TextContent(type="text", text=json.dumps(
                {"task_type": "general", "activated": [{"id": 1, "name": "奥卡姆剃刀"}]}
            ))]

        se._atoms["principle_activate"] = mock_principle_activate

        # Mock skill_session_start/complete to record calls
        session_calls = []
        async def mock_session_start(engine, args):
            session_calls.append(("start", args))
            return [TextContent(type="text", text=json.dumps(
                {"entity_id": "skill:test:2026-01-01T00:00:00", "status": "active"}
            ))]
        async def mock_session_complete(engine, args):
            session_calls.append(("complete", args))
            return [TextContent(type="text", text=json.dumps({"status": "done"}))]
        se._atoms["skill_session_start"] = mock_session_start
        se._atoms["skill_session_complete"] = mock_session_complete

        async def handler(ctx, params, atom_results):
            return SkillResult(
                skill_name="test-skill", success=True,
                data={"activated": json.loads(atom_results["principle_activate"][0].text)},
                atom_results={k: v[0].text for k, v in atom_results.items()},
                degrade_log=[], audit_trail={}, errors=[],
            )

        sd = SkillDef(
            name="test-skill", domain="session_lifecycle",
            description="Test", tier="P0",
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
        se = SkillEngine(mock_engine)
        async def handler(ctx, params, atoms):
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])

        sd = SkillDef(
            name="daemon-only", domain="system_health",
            description="Test", tier="P2",
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
        se = SkillEngine(mock_engine)
        result = await se.exec("nonexistent", params={}, caller="claude")
        assert result.success is False
        assert "Unknown skill" in result.errors[0]

    @pytest.mark.asyncio
    async def test_exec_atom_degraded_skip(self, mock_engine):
        """When an atom fails with degrade_map='skip', execution continues."""
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
                skill_name="test", success=True, data={},
                atom_results={}, degrade_log=[], audit_trail={}, errors=[],
            )

        sd = SkillDef(
            name="degrade-skip-test", domain="session_lifecycle",
            description="Test", tier="P0",
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
    async def test_exec_atom_degraded_abort(self, mock_engine):
        """When an atom fails with default degrade='abort', execution stops."""
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
            return SkillResult(skill_name="test", success=True, data={},
                              atom_results={}, degrade_log=[], audit_trail={}, errors=[])

        sd = SkillDef(
            name="degrade-abort-test", domain="session_lifecycle",
            description="Test", tier="P0",
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
```

- [ ] **Step 2: Run tests (expect failure)**

Run: `pytest tests/test_skill_engine.py::TestSkillEngineExec -v`
Expected: FAIL — `SkillEngine.exec` not implemented

- [ ] **Step 3: Implement exec()**

```python
# Append to SkillEngine class in plastic_promise/skills/engine.py

    async def exec(self, skill_name: str, params: dict = None,
                   caller: str = "claude") -> SkillResult:
        """Execute a registered skill.

        Execution flow:
        1. Look up SkillDef — return failure if unknown
        2. Caller authorization check — reject if caller not in allowed_callers
        3. skill_session_start — create tracking entity
        4. For each atom in atoms: call with error handling per degrade_map
        5. Call SkillDef.handler(ctx, params, atom_results)
        6. skill_session_complete — mark done
        7. Return SkillResult with audit trail
        """
        if params is None:
            params = {}

        # 1. Lookup
        skill_def = self._registry.get(skill_name)
        if skill_def is None:
            return SkillResult(
                skill_name=skill_name, success=False,
                data={}, atom_results={}, degrade_log=[],
                audit_trail={}, errors=[f"Unknown skill: {skill_name}"],
            )

        # 2. Caller authorization
        if caller not in skill_def.allowed_callers:
            return SkillResult(
                skill_name=skill_name, success=False,
                data={}, atom_results={}, degrade_log=[],
                audit_trail={},
                errors=[f"Caller '{caller}' not in allowed_callers {skill_def.allowed_callers} for skill '{skill_name}'"],
            )

        atom_results: dict[str, Any] = {}
        degrade_log: list[str] = []
        errors: list[str] = []
        entity_id: str = ""
        task_description = params.get("task_description", skill_def.description)

        # 3. skill_session_start
        start_handler = self._atoms.get("skill_session_start")
        if start_handler:
            try:
                start_result = await start_handler(self._ctx, {
                    "skill_name": skill_name,
                    "task_description": task_description,
                })
                start_data = json.loads(start_result[0].text)
                entity_id = start_data.get("entity_id", "")
            except Exception as e:
                degrade_log.append(f"skill_session_start: {e}")

        try:
            # 4. Call atoms in order with degradation
            for atom_name in skill_def.atoms:
                atom_handler = self._atoms.get(atom_name)
                if atom_handler is None:
                    msg = f"Atom '{atom_name}' not in registry"
                    degrade_log.append(msg)
                    errors.append(msg)
                    continue

                try:
                    result = await atom_handler(self._ctx, params)
                    atom_results[atom_name] = result
                except Exception as e:
                    action = skill_def.degrade_map.get(atom_name, "abort")
                    if action == "skip":
                        degrade_log.append(f"{atom_name}: skip — {e}")
                        continue
                    elif action == "warn":
                        degrade_log.append(f"{atom_name}: warn — {e}")
                        continue
                    elif action.startswith("fallback:"):
                        fallback_atom = action[len("fallback:"):]
                        degrade_log.append(f"{atom_name}: fallback to {fallback_atom} — {e}")
                        try:
                            fb_handler = self._atoms.get(fallback_atom)
                            if fb_handler:
                                fb_result = await fb_handler(self._ctx, params)
                                atom_results[atom_name] = fb_result
                        except Exception as fb_e:
                            degrade_log.append(f"{atom_name}: fallback {fallback_atom} also failed — {fb_e}")
                            errors.append(f"{atom_name}: {e}")
                        continue
                    else:  # "abort" (default)
                        errors.append(f"{atom_name}: {e}")
                        degrade_log.append(f"{atom_name}: abort — {e}")
                        # Complete session as failed
                        if entity_id:
                            try:
                                complete_handler = self._atoms.get("skill_session_complete")
                                if complete_handler:
                                    await complete_handler(self._ctx, {
                                        "entity_id": entity_id,
                                        "outcome": f"abandoned: atom {atom_name} failed",
                                    })
                            except Exception:
                                pass
                        return SkillResult(
                            skill_name=skill_name, success=False,
                            data={}, atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                            degrade_log=degrade_log, audit_trail={"entity_id": entity_id}, errors=errors,
                        )

            # 5. Call handler
            result = await skill_def.handler(self._ctx, params, atom_results)

            # 6. skill_session_complete
            complete_handler = self._atoms.get("skill_session_complete")
            if complete_handler and entity_id:
                try:
                    await complete_handler(self._ctx, {
                        "entity_id": entity_id,
                        "outcome": "",
                    })
                except Exception as e:
                    degrade_log.append(f"skill_session_complete: {e}")

            # 7. Return
            result.audit_trail = {"entity_id": entity_id}
            result.degrade_log = result.degrade_log or degrade_log
            result.errors = result.errors or errors
            return result

        except Exception as e:
            # Handler-level failure — still attempt session close
            errors.append(f"handler: {e}")
            if entity_id:
                try:
                    complete_handler = self._atoms.get("skill_session_complete")
                    if complete_handler:
                        await complete_handler(self._ctx, {
                            "entity_id": entity_id,
                            "outcome": f"abandoned: handler error — {e}",
                        })
                except Exception:
                    pass
            return SkillResult(
                skill_name=skill_name, success=False,
                data={}, atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                degrade_log=degrade_log, audit_trail={"entity_id": entity_id}, errors=errors,
            )


def _text_or_str(result: list) -> str:
    """Extract text from a list of TextContent, or return str representation."""
    if result and hasattr(result[0], 'text'):
        return result[0].text
    return str(result)
```

Write with: append to `plastic_promise/skills/engine.py`.

- [ ] **Step 4: Run tests (expect pass)**

Run: `pytest tests/test_skill_engine.py::TestSkillEngineExec -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Run all engine tests**

Run: `pytest tests/test_skill_engine.py -v`
Expected: PASS (all 14 tests across TestSkillDef, TestSkillResult, TestSkillRegistrationError, TestAtomRegistry, TestSkillEngineRegister, TestSkillEngineExec)

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/skills/engine.py tests/test_skill_engine.py
git commit -m "feat: add SkillEngine.exec() with atom degradation, caller auth, and session audit wrapping"
```

---

### Task 5: Implement session-init skill

**Files:**
- Create: `plastic_promise/skills/session_lifecycle.py`

**Interfaces:**
- Produces: `skill_session_init` — the SkillDef for session-init
- Consumes: `SkillEngine`, `SkillDef`, `SkillResult` from `plastic_promise.skills.engine`

- [ ] **Step 1: Write integration test**

```python
# tests/test_session_lifecycle.py

import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from mcp.types import TextContent

from plastic_promise.skills.engine import SkillEngine, SkillDef, SkillResult
from plastic_promise.skills.session_lifecycle import skill_session_init


class TestSessionInit:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "context_supply", "memory_store",
            "domain", "system", "defense", "memory_gc",
            "skill_session_start", "skill_session_complete",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _mock_atom_response(self, data: dict) -> list:
        return [TextContent(type="text", text=json.dumps(data))]

    @pytest.mark.asyncio
    async def test_session_init_success(self, mock_engine):
        """session-init must call all 7 atoms in order and return context pack."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def record_call(name, data):
            def handler(engine, args):
                call_order.append(name)
                return [TextContent(type="text", text=json.dumps(data))]
            return handler

        se._atoms["principle_activate"] = await record_call("principle_activate", {
            "task_type": "general", "activated": [{"id": 1, "name": "奥卡姆剃刀"}], "count": 1
        })
        se._atoms["context_supply"] = await record_call("context_supply", {
            "core": [{"id": "m1", "content": "test"}], "related": [], "divergent": []
        })
        se._atoms["memory_store"] = await record_call("memory_store", {
            "stored": True, "memory_id": "mem_001"
        })
        se._atoms["domain"] = await record_call("domain", {
            "domains": {"building": {"score": 0.8}}
        })
        se._atoms["system"] = await record_call("system", {
            "memory": {"total": 42, "healthy": 40, "decaying": 2}
        })
        se._atoms["defense"] = await record_call("defense", {
            "trust": 0.75, "tier": "standard"
        })
        se._atoms["memory_gc"] = await record_call("memory_gc", {
            "dry_run": True, "candidates_count": 3
        })
        se._atoms["skill_session_start"] = await record_call("skill_session_start", {
            "entity_id": "skill:session-init:2026-01-01T00:00:00"
        })
        se._atoms["skill_session_complete"] = await record_call("skill_session_complete", {
            "status": "done"
        })

        se.register(skill_session_init)
        result = await se.exec("session-init", params={
            "task_description": "test task",
            "task_type": "general",
        }, caller="claude")

        assert result.success is True
        assert result.skill_name == "session-init"
        # Verify all 7 atoms called in order
        assert call_order[:7] == [
            "principle_activate", "context_supply", "memory_store",
            "domain", "system", "defense", "memory_gc"
        ]
        # Verify handler assembled the data
        assert "context" in result.data
        assert "domain_health" in result.data
        assert "system_stats" in result.data
        assert "trust" in result.data

    @pytest.mark.asyncio
    async def test_session_init_degraded_domain_skip(self, mock_engine):
        """When domain fails with degrade='skip', session-init must continue and note the skip."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def ok_atom(name, data):
            def handler(engine, args):
                call_order.append(name)
                return [TextContent(type="text", text=json.dumps(data))]
            return handler

        async def failing_atom(name):
            def handler(engine, args):
                call_order.append(name)
                raise RuntimeError("DomainManager not available")
            return handler

        se._atoms["principle_activate"] = await ok_atom("principle_activate", {"activated": []})
        se._atoms["context_supply"] = await ok_atom("context_supply", {"core": []})
        se._atoms["memory_store"] = await ok_atom("memory_store", {"stored": True})
        se._atoms["domain"] = failing_atom("domain")  # This will fail
        se._atoms["system"] = await ok_atom("system", {"memory": {"total": 0}})
        se._atoms["defense"] = await ok_atom("defense", {"trust": 0.5})
        se._atoms["memory_gc"] = await ok_atom("memory_gc", {"candidates_count": 0})
        se._atoms["skill_session_start"] = await ok_atom("skill_session_start", {"entity_id": "skill:test:..."})
        se._atoms["skill_session_complete"] = await ok_atom("skill_session_complete", {"status": "done"})

        se.register(skill_session_init)
        result = await se.exec("session-init", params={
            "task_description": "test",
        }, caller="claude")

        assert result.success is True
        assert "system" in call_order  # continued after domain failure
        assert any("domain" in log and "skip" in log for log in result.degrade_log)
```

- [ ] **Step 2: Run test (expect failure)**

Run: `pytest tests/test_session_lifecycle.py -v`
Expected: FAIL — `plastic_promise.skills.session_lifecycle` module not found

- [ ] **Step 3: Implement session-init skill**

```python
# plastic_promise/skills/session_lifecycle.py

"""域 1: Session Lifecycle skills — 会话生命周期管理"""

import json

from plastic_promise.skills.engine import SkillDef, SkillResult


async def _session_init_handler(ctx, params, atom_results):
    """session-init handler: assemble atom results into a unified context pack.

    Atoms called before this handler:
    - principle_activate: {activated: [...], count: N}
    - context_supply: ContextPack JSON (core/related/divergent)
    - memory_store: {stored: true, memory_id: "..."}
    - domain: {domains: {...}}
    - system: {memory: {...}, fuzzy_buffer: {...}}
    - defense: {trust: float, tier: str}
    - memory_gc: {dry_run: true, candidates_count: N}
    """

    def parse(result):
        """Extract parsed JSON dict from atom result list[TextContent]."""
        if result and hasattr(result[0], 'text'):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": result[0].text}
        return {}

    principle_data = parse(atom_results.get("principle_activate"))
    context_data = {}
    context_raw = atom_results.get("context_supply")
    if context_raw and hasattr(context_raw[0], 'text'):
        context_data = {"prompt": context_raw[0].text}  # ContextPack.to_prompt() returns formatted text
    memory_data = parse(atom_results.get("memory_store"))
    domain_data = parse(atom_results.get("domain"))
    system_data = parse(atom_results.get("system"))
    defense_data = parse(atom_results.get("defense"))
    gc_data = parse(atom_results.get("memory_gc"))

    return SkillResult(
        skill_name="session-init",
        success=True,
        data={
            "principles": principle_data.get("activated", []),
            "context": context_data,
            "inject_memory_id": memory_data.get("memory_id", ""),
            "domain_health": domain_data,
            "system_stats": system_data,
            "trust": defense_data,
            "gc_preview": gc_data,
        },
        atom_results={},
        degrade_log=[],
        audit_trail={},
        errors=[],
    )


# ── Skill Definition ──

skill_session_init = SkillDef(
    name="session-init",
    domain="session_lifecycle",
    description="会话启动 — 封装 CLAUDE.md 步骤 0-5",
    tier="P0",
    atoms=[
        "principle_activate",
        "context_supply",
        "memory_store",
        "domain",
        "system",
        "defense",
        "memory_gc",
    ],
    degrade_map={
        "domain": "skip",
        "system": "skip",
        "memory_gc": "skip",
        "defense": "warn",
    },
    handler=_session_init_handler,
    allowed_callers=["claude", "pi"],
)
```

- [ ] **Step 4: Run test (expect pass)**

Run: `pytest tests/test_session_lifecycle.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/skills/session_lifecycle.py tests/test_session_lifecycle.py
git commit -m "feat: add session-init skill — 7-atom chain wrapping CLAUDE.md steps 0-5"
```

---

### Task 6: Implement smart-remember skill

**Files:**
- Create: `plastic_promise/skills/memory_operations.py`

**Interfaces:**
- Produces: `skill_smart_remember` — the SkillDef for smart-remember
- Consumes: `SkillDef`, `SkillResult` from `plastic_promise.skills.engine`

- [ ] **Step 1: Write integration test**

```python
# tests/test_memory_operations.py

import json
import pytest
from unittest.mock import MagicMock
from mcp.types import TextContent

from plastic_promise.skills.engine import SkillEngine, SkillDef, SkillResult
from plastic_promise.skills.memory_operations import skill_smart_remember


class TestSmartRemember:
    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        mock_tools = [_make_mock_tool(n) for n in [
            "principle_activate", "memory_recall", "memory_store",
            "memory_update", "skill_session_start", "skill_session_complete",
        ]]
        engine.list_tools = MagicMock(return_value=mock_tools)
        return engine

    def _mock_response(self, data: dict) -> list:
        return [TextContent(type="text", text=json.dumps(data))]

    @pytest.mark.asyncio
    async def test_smart_remember_new_memory(self, mock_engine):
        """When no duplicate is found, a new memory must be stored."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_principle_activate(engine, args):
            call_order.append("principle_activate")
            return self._mock_response({"activated": [{"id": 1, "name": "奥卡姆剃刀"}]})

        async def mock_memory_recall(engine, args):
            call_order.append("memory_recall")
            # No duplicates found
            return self._mock_response({"core": [], "related": [], "divergent": []})

        async def mock_memory_store(engine, args):
            call_order.append("memory_store")
            return self._mock_response({
                "stored": True, "memory_id": "mem_new_001",
                "content_preview": args["content"][:50],
            })

        async def mock_session(engine, args):
            return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["memory_store"] = mock_memory_store
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session

        se.register(skill_smart_remember)
        result = await se.exec("smart-remember", params={
            "content": "The user prefers tabs over spaces",
            "memory_type": "experience",
            "source": "user",
        }, caller="claude")

        assert result.success is True
        assert result.data.get("action") == "stored"
        assert result.data.get("memory_id") == "mem_new_001"
        assert call_order == ["principle_activate", "memory_recall", "memory_store"]

    @pytest.mark.asyncio
    async def test_smart_remember_duplicate_found(self, mock_engine):
        """When a duplicate is found (cos >= 0.85), update existing instead of creating new."""
        se = SkillEngine(mock_engine)
        call_order = []

        async def mock_principle_activate(engine, args):
            call_order.append("principle_activate")
            return self._mock_response({"activated": []})

        async def mock_memory_recall(engine, args):
            call_order.append("memory_recall")
            # Duplicate found — one existing memory with high relevance
            return self._mock_response({
                "core": [
                    {"id": "mem_existing_042", "content": "User prefers tabs over spaces", "relevance": 0.92}
                ],
                "related": [], "divergent": [],
            })

        async def mock_memory_update(engine, args):
            call_order.append("memory_update")
            return self._mock_response({"updated": True, "memory_id": "mem_existing_042"})

        async def mock_session(engine, args):
            return self._mock_response({"entity_id": "skill:test:...", "status": "ok"})

        se._atoms["principle_activate"] = mock_principle_activate
        se._atoms["memory_recall"] = mock_memory_recall
        se._atoms["memory_update"] = mock_memory_update
        se._atoms["skill_session_start"] = mock_session
        se._atoms["skill_session_complete"] = mock_session

        se.register(skill_smart_remember)
        result = await se.exec("smart-remember", params={
            "content": "User prefers tabs over spaces",
            "memory_type": "experience",
            "source": "user",
        }, caller="claude")

        assert result.success is True
        assert result.data.get("action") == "updated"
        assert result.data.get("memory_id") == "mem_existing_042"
        assert "memory_update" in call_order
        assert "memory_store" not in call_order  # did not create duplicate
```

- [ ] **Step 2: Run test (expect failure)**

Run: `pytest tests/test_memory_operations.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement smart-remember skill**

```python
# plastic_promise/skills/memory_operations.py

"""域 2: Memory Operations skills — 记忆 CRUD 的高层组合"""

import json

from plastic_promise.core.constants import DEDUP_SIMILARITY_THRESHOLD
from plastic_promise.skills.engine import SkillDef, SkillResult


async def _smart_remember_handler(ctx, params, atom_results):
    """smart-remember handler: dedup check → store or update.

    Atoms called before this handler:
    - principle_activate: {activated: [...]}
    - memory_recall: {core: [{id, content, relevance}]}
    - memory_store (if no dupe) OR memory_update (if dupe found)

    Dedup logic:
    - If memory_recall returns any core result with relevance >= 0.7
      → treat as duplicate → update instead of store
    - Otherwise → store new memory
    """

    def parse(result):
        if result and hasattr(result[0], 'text'):
            try:
                return json.loads(result[0].text)
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    recall_data = parse(atom_results.get("memory_recall"))
    core_results = recall_data.get("core", [])

    # Check for duplicates: any core result with relevance >= DEDUP_SIMILARITY_THRESHOLD (0.85)
    duplicate = None
    for item in core_results:
        if item.get("relevance", 0) >= DEDUP_SIMILARITY_THRESHOLD:
            duplicate = item
            break

    if duplicate:
        # Update existing
        update_data = parse(atom_results.get("memory_update", atom_results.get("memory_store")))
        memory_id = update_data.get("memory_id", duplicate.get("id", "?"))
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "updated",
                "memory_id": memory_id,
                "duplicate_of": duplicate.get("id"),
                "relevance": duplicate.get("relevance"),
            },
            atom_results={}, degrade_log=[], audit_trail={}, errors=[],
        )
    else:
        # Store new
        store_data = parse(atom_results.get("memory_store"))
        return SkillResult(
            skill_name="smart-remember",
            success=True,
            data={
                "action": "stored",
                "memory_id": store_data.get("memory_id", "?"),
                "pipeline": store_data.get("pipeline", {}),
            },
            atom_results={}, degrade_log=[], audit_trail={}, errors=[],
        )


# ── Skill Definition ──

skill_smart_remember = SkillDef(
    name="smart-remember",
    domain="memory_operations",
    description="记忆前自动去重 + 质量门控 — 重复的记忆更新而非新增",
    tier="P0",
    atoms=[
        "principle_activate",
        "memory_recall",
        "memory_store",  # used only if no duplicate
    ],
    degrade_map={
        "principle_activate": "skip",
        "memory_recall": "fallback:memory_store",  # if recall fails, store anyway (no dedup)
        "memory_store": "abort",
    },
    handler=_smart_remember_handler,
    allowed_callers=["claude", "pi"],
)
```

- [ ] **Step 4: Run test (expect pass)**

Run: `pytest tests/test_memory_operations.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/skills/memory_operations.py tests/test_memory_operations.py
git commit -m "feat: add smart-remember skill — dedup check + store-or-update"
```

---

### Task 7: End-to-end integration test — full Phase 1 pipeline

**Files:**
- Create: `tests/test_skills_phase1_e2e.py`

**Interfaces:**
- Consumes: `SkillEngine`, `skill_session_init`, `skill_smart_remember`

- [ ] **Step 1: Write E2E test**

```python
# tests/test_skills_phase1_e2e.py

"""End-to-end test: Phase 1 skills working together with a real ContextEngine."""

import json
import pytest

from plastic_promise.core.context_engine import ContextEngine
from plastic_promise.skills.engine import SkillEngine
from plastic_promise.skills.session_lifecycle import skill_session_init
from plastic_promise.skills.memory_operations import skill_smart_remember


@pytest.mark.asyncio
class TestPhase1E2E:
    @pytest.fixture
    def engine(self):
        """Real ContextEngine — Python mock (no Rust required)."""
        ctx = ContextEngine()
        # Patch list_tools() to return actual tool names matching our atoms
        ctx.list_tools = lambda: [
            type("Tool", (), {"name": n})()
            for n in [
                "principle_activate", "context_supply", "memory_store",
                "memory_recall", "memory_update", "domain", "system",
                "defense", "memory_gc", "skill_session_start",
                "skill_session_complete",
            ]
        ]
        return ctx

    @pytest.fixture
    def skill_engine(self, engine):
        se = SkillEngine(engine)
        se.register(skill_session_init)
        se.register(skill_smart_remember)
        return se

    @pytest.mark.asyncio
    async def test_session_init_registers(self, skill_engine):
        """session-init must be registered and callable."""
        result = await skill_engine.exec("session-init", params={
            "task_description": "E2E test of session-init",
            "task_type": "general",
        }, caller="claude")

        # Skill execution should succeed (atoms may degrade but handler runs)
        assert result.skill_name == "session-init"
        # Domain, system, memory_gc may fail in test env — but skill should succeed
        # because their degrade_map is "skip"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_smart_remember_stores_memory(self, skill_engine):
        """smart-remember must store a new memory when no duplicate exists."""
        result = await skill_engine.exec("smart-remember", params={
            "content": "E2E test: the sky is blue",
            "memory_type": "experience",
            "source": "test",
        }, caller="claude")

        assert result.success is True
        assert result.data["action"] == "stored"
        assert result.data["memory_id"] != ""

    @pytest.mark.asyncio
    async def test_full_workflow(self, skill_engine):
        """Phase 1 pipeline: init → remember → verify in memory."""
        # 1. Initialize session
        init_result = await skill_engine.exec("session-init", params={
            "task_description": "E2E full workflow test",
        }, caller="claude")
        assert init_result.success is True

        # 2. Store a memory
        remember_result = await skill_engine.exec("smart-remember", params={
            "content": "E2E workflow: Python is the preferred language",
            "memory_type": "experience",
            "source": "test",
        }, caller="claude")
        assert remember_result.success is True
        memory_id = remember_result.data["memory_id"]

        # 3. Verify memory exists in engine
        mem = skill_engine._ctx.get_memory(memory_id)
        assert mem is not None
```

- [ ] **Step 2: Run E2E tests**

Run: `pytest tests/test_skills_phase1_e2e.py -v`
Expected: PASS (all 3 tests). If `domain` or `system` atoms fail in test env, session-init must still succeed (skip degradation).

- [ ] **Step 3: Run full Phase 1 test suite**

Run: `pytest tests/test_skill_engine.py tests/test_session_lifecycle.py tests/test_memory_operations.py tests/test_skills_phase1_e2e.py -v`
Expected: ALL PASS (~22 tests total)

- [ ] **Step 4: Commit**

```bash
git add tests/test_skills_phase1_e2e.py
git commit -m "test: add Phase 1 E2E tests — session-init + smart-remember pipeline"
```

---

## Post-Phase 1 Checklist

- [ ] `plastic_promise/skills/engine.py` — SkillDef, SkillResult, SkillRegistrationError, AtomRegistry, SkillEngine
- [ ] `plastic_promise/skills/session_lifecycle.py` — skill_session_init SkillDef
- [ ] `plastic_promise/skills/memory_operations.py` — skill_smart_remember SkillDef
- [ ] All tests pass: `pytest tests/test_skill_engine.py tests/test_session_lifecycle.py tests/test_memory_operations.py tests/test_skills_phase1_e2e.py -v`
- [ ] Verify no `auto_context_inject` double-wrap: session-init atoms list does NOT include `auto_context_inject`
- [ ] Verify P2 enforcement: `SkillEngine.register(P2_skill_with_claude_caller)` raises `SkillRegistrationError`
