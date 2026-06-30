import importlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable


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

    async def exec(self, skill_name: str, params: dict = None,
                   caller: str = "claude") -> SkillResult:
        """Execute a registered skill.

        Execution flow:
        1. Look up SkillDef -- return failure if unknown
        2. Caller authorization check -- reject if caller not in allowed_callers
        3. skill_session_start -- create tracking entity
        4. For each atom in atoms: call with error handling per degrade_map
        5. Call SkillDef.handler(ctx, params, atom_results)
        6. skill_session_complete -- mark done
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

        # 3. skill_session_start — failure degrades tracking but does not abort
        tracking_degraded = False
        start_handler = self._atoms.get("skill_session_start")
        if start_handler:
            try:
                start_result = await start_handler(self._ctx, {
                    "skill_name": skill_name,
                    "task_description": task_description,
                })
                start_data = json.loads(start_result[0].text)
                entity_id = start_data.get("entity_id", "")
            except json.JSONDecodeError as e:
                tracking_degraded = True
                degrade_log.append(
                    f"skill_session_start: malformed JSON response — "
                    f"session tracking disabled for this execution ({e})"
                )
            except Exception as e:
                tracking_degraded = True
                degrade_log.append(
                    f"skill_session_start: failed — "
                    f"session tracking disabled for this execution ({e})"
                )

        try:
            # 4. Call atoms in order with degradation
            fallback_executed: set[str] = set()  # track atoms already run as fallback
            for atom_name in skill_def.atoms:
                # Skip if this atom was already executed as a fallback for a prior atom
                if atom_name in fallback_executed:
                    degrade_log.append(f"{atom_name}: skip -- already executed as fallback")
                    continue

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
                        degrade_log.append(f"{atom_name}: skip -- {e}")
                        continue
                    elif action == "warn":
                        degrade_log.append(f"{atom_name}: warn -- {e}")
                        continue
                    elif action.startswith("fallback:"):
                        fallback_atom = action[len("fallback:"):]
                        degrade_log.append(f"{atom_name}: fallback to {fallback_atom} -- {e}")
                        try:
                            fb_handler = self._atoms.get(fallback_atom)
                            if fb_handler:
                                fb_result = await fb_handler(self._ctx, params)
                                atom_results[atom_name] = fb_result
                                fallback_executed.add(fallback_atom)
                        except Exception as fb_e:
                            degrade_log.append(f"{atom_name}: fallback {fallback_atom} also failed -- {fb_e}")
                            errors.append(f"{atom_name}: {e}")
                        continue
                    else:  # "abort" (default)
                        errors.append(f"{atom_name}: {e}")
                        degrade_log.append(f"{atom_name}: abort -- {e}")
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
                            degrade_log=degrade_log,
                            audit_trail={"entity_id": entity_id, "tracking_degraded": tracking_degraded},
                            errors=errors,
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
            result.audit_trail = {"entity_id": entity_id, "tracking_degraded": tracking_degraded}
            result.degrade_log = result.degrade_log or degrade_log
            result.errors = result.errors or errors
            return result

        except Exception as e:
            # Handler-level failure -- still attempt session close
            errors.append(f"handler: {e}")
            if entity_id:
                try:
                    complete_handler = self._atoms.get("skill_session_complete")
                    if complete_handler:
                        await complete_handler(self._ctx, {
                            "entity_id": entity_id,
                            "outcome": f"abandoned: handler error -- {e}",
                        })
                except Exception:
                    pass
            return SkillResult(
                skill_name=skill_name, success=False,
                data={}, atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                degrade_log=degrade_log,
                audit_trail={"entity_id": entity_id, "tracking_degraded": tracking_degraded},
                errors=errors,
            )


def _text_or_str(result: list) -> str:
    """Extract text from a list of TextContent, or return str representation."""
    if result and hasattr(result[0], 'text'):
        return result[0].text
    return str(result)
