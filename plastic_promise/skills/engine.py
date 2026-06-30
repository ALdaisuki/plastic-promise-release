import importlib
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
