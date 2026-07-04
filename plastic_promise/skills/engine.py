import asyncio
import importlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class SkillRegistrationError(Exception):
    """Raised when a SkillDef fails validation during registration."""


class WorkflowViolation(Exception):
    """Raised when workflow_mode=strict blocks an action."""

    pass


@dataclass
class SkillDef:
    """Definition of a programmatic skill — atoms + handler + permissions."""

    name: str
    domain: str
    description: str
    tier: str  # "P0" | "P1" | "P2"

    # P0/P1 atom dependencies — Engine calls in order (or concurrently if concurrent=True)
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

    # Performance: run all atoms concurrently via asyncio.gather (default: False = serial)
    concurrent: bool = False
    atom_timeout_seconds: float | None = None
    track_start_memory: bool = True

    # Workflow pack: full skill prompt text (for skill_resolve)
    prompt: str = ""


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
        "memory_recall": ("plastic_promise.mcp.tools.memory", "handle_memory_recall"),
        "memory_store": ("plastic_promise.mcp.tools.memory", "handle_memory_store"),
        "memory_update": ("plastic_promise.mcp.tools.memory", "handle_memory_update"),
        "memory_forget": ("plastic_promise.mcp.tools.memory", "handle_memory_forget"),
        "memory_list": ("plastic_promise.mcp.tools.memory", "handle_memory_list"),
        "memory_gc": ("plastic_promise.mcp.tools.memory", "handle_memory_gc"),
        "memory_correct": ("plastic_promise.mcp.tools.memory", "handle_memory_correct"),
        # === Principle domain (P0 + P1) ===
        "principle_activate": ("plastic_promise.mcp.tools.principles", "handle_principle_activate"),
        "principle_evaluate": ("plastic_promise.mcp.tools.principles", "handle_principle_evaluate"),
        # === Context domain (P0 + P1) ===
        "context_supply": ("plastic_promise.mcp.tools.context", "handle_context_supply"),
        "context_inject": ("plastic_promise.mcp.tools.context", "handle_context_inject"),
        "context_graph": ("plastic_promise.mcp.tools.context", "handle_context_graph"),
        "auto_context_inject": ("plastic_promise.mcp.tools.context", "handle_auto_context_inject"),
        # === Audit & Defense (P0 + P1) ===
        "audit_run": ("plastic_promise.mcp.tools.audit_defense", "handle_audit_run"),
        "audit_pre_check": ("plastic_promise.mcp.tools.audit_defense", "handle_audit_pre_check"),
        "defense": ("plastic_promise.mcp.tools.audit_defense", "handle_defense"),
        # === Reflection domain (P1) ===
        "scarf_reflect": ("plastic_promise.mcp.tools.reflection", "handle_scarf_reflect"),
        "feedback_apply": ("plastic_promise.mcp.tools.reflection", "handle_feedback_apply"),
        # === Domain (P1) ===
        "domain": ("plastic_promise.mcp.tools.domain", "handle_domain"),
        # === Management (P1) ===
        "system": ("plastic_promise.mcp.tools.management", "handle_system"),
        "issue_create": ("plastic_promise.mcp.tools.management", "handle_issue_create"),
        "issue_transition": ("plastic_promise.mcp.tools.management", "handle_issue_transition"),
        "issue_list": ("plastic_promise.mcp.tools.management", "handle_issue_list"),
        "pack_export": ("plastic_promise.mcp.tools.management", "handle_pack_export"),
        "pack_import": ("plastic_promise.mcp.tools.management", "handle_pack_import"),
        # === Skill Tracking (P0 + P1) ===
        "skill_session_start": (
            "plastic_promise.mcp.tools.skill_tracking",
            "handle_skill_session_start",
        ),
        "skill_session_complete": (
            "plastic_promise.mcp.tools.skill_tracking",
            "handle_skill_session_complete",
        ),
        "skill_session_trace": (
            "plastic_promise.mcp.tools.skill_tracking",
            "handle_skill_session_trace",
        ),
        "skill_session_audit": (
            "plastic_promise.mcp.tools.skill_tracking",
            "handle_skill_session_audit",
        ),
        # === Governance Injection (Plastic Promise native) ===
        "step_closure_light": (
            "plastic_promise.skills.superpowers_stages",
            "_governance_step_closure_light",
        ),
        "step_closure_full": (
            "plastic_promise.skills.superpowers_stages",
            "_governance_step_closure_full",
        ),
    }

    @staticmethod
    def build(engine) -> dict[str, "Callable"]:
        """Build a registry of atom_name → async handler(engine, args).

        Lazy-imports handler modules only when build() is called.
        Returns a dict suitable for SkillEngine._atoms.
        """
        registry: dict[str, Callable] = {}
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
        self._atoms: dict[str, Callable] = AtomRegistry.build(engine)
        self._workflow_mode: str = "advisory"
        self._completed_stages: set[str] = set()

    def set_workflow_mode(self, mode: str) -> None:
        """Set workflow mode: 'strict' or 'advisory'."""
        if mode in ("strict", "advisory"):
            self._workflow_mode = mode

    def mark_stage_completed(self, stage_name: str) -> None:
        """Record that a stage has been completed (for strict mode enforcement)."""
        self._completed_stages.add(stage_name)

    def stage_completed(self, stage_name: str) -> bool:
        """Check if a stage has been completed."""
        return stage_name in self._completed_stages

    def enforce_workflow_mode(self, action: str) -> bool:
        """If workflow_mode is strict, block code-modifying actions
        until session-init + brainstorming have run.

        Returns True if action is allowed, False if blocked.
        """
        if self._workflow_mode != "strict":
            return True
        # Read-only and planning actions are always allowed
        if action in (
            "session-init",
            "brainstorming",
            "exemplar-research",
            "read",
            "search",
            "market_list",
            "market_status",
        ):
            return True
        # Code-modifying actions require session-init + brainstorming
        required = {"session-init", "brainstorming"}
        if not required.issubset(self._completed_stages):
            missing = required - self._completed_stages
            raise WorkflowViolation(f"workflow_mode=strict: {missing} required before '{action}'")
        return True

    @staticmethod
    def _build_atom_params(atom_name: str, params: dict) -> dict:
        """Build per-atom params for adapters that need canonical fields."""
        atom_params = dict(params)
        if atom_name == "memory_store" and "content" not in atom_params:
            task_desc = params.get("task_description", "")
            stage = params.get("stage", "")
            atom_params["content"] = f"[{stage}] {task_desc}" if stage else task_desc
        if atom_name == "scarf_reflect" and not atom_params.get("context"):
            atom_params["context"] = params.get("task_description", "")
        return atom_params

    @staticmethod
    def _atom_timeout(skill_def: SkillDef) -> float | None:
        timeout = skill_def.atom_timeout_seconds
        if timeout is None:
            raw = os.environ.get("PP_SKILL_ATOM_TIMEOUT_SEC", "")
            if not raw:
                return None
            try:
                timeout = float(raw)
            except ValueError:
                return None
        return timeout if timeout and timeout > 0 else None

    @classmethod
    def _lifecycle_timeout(cls, skill_def: SkillDef) -> float | None:
        """Timeout for skill start/handler/complete bookkeeping.

        Programmatic skills can keep atoms bounded but still hang in lifecycle
        tracking or handlers. Default to the atom timeout when one is set, and
        allow a separate override for operational tuning.
        """
        raw = os.environ.get("PP_SKILL_LIFECYCLE_TIMEOUT_SEC", "")
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                timeout = None
        else:
            timeout = cls._atom_timeout(skill_def)
        return timeout if timeout and timeout > 0 else None

    async def _call_lifecycle(self, skill_def: SkillDef, coro):
        timeout = self._lifecycle_timeout(skill_def)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)

    async def _call_atom(
        self,
        skill_def: SkillDef,
        atom_handler: Callable,
        atom_params: dict,
    ):
        coro = atom_handler(self._ctx, atom_params)
        timeout = self._atom_timeout(skill_def)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)

    def register(self, skill_def: SkillDef) -> None:
        """Register a skill definition. Validates dependencies and permissions.

        Raises SkillRegistrationError if:
        - skill name already registered
        - any declared atom is not available in the MCP tool set
        - P2 skill allows non-daemon/admin callers
        """
        # 1. Check for duplicate name
        if skill_def.name in self._registry:
            raise SkillRegistrationError(f"Skill '{skill_def.name}' already registered")

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

    def resolve(self, name: str) -> str:
        """Return the full prompt for a skill by name.

        Used by Agents to query SuperPowers specifications.
        Returns empty string if skill not found.
        """
        skill = self._registry.get(name)
        if skill:
            return skill.prompt
        return ""

    def register_from_pack(self, pack) -> int:
        """Register all skills from a workflow-type pack.

        Args:
            pack: PackInfo with pack_type='workflow'

        Returns:
            Number of skills registered.
        """
        if pack.pack_type != "workflow":
            return 0

        count = 0
        for skill_name, skill_data in pack.skills.items():
            if isinstance(skill_data, dict):
                prompt = skill_data.get("prompt", "")
                description = skill_data.get("description", "")
            elif isinstance(skill_data, str):
                prompt = skill_data
                description = ""
            else:
                continue

            self.register(
                SkillDef(
                    name=skill_name,
                    domain="workflow",
                    description=description,
                    tier="P0",
                    prompt=prompt,
                )
            )
            count += 1

        return count

    async def exec(
        self, skill_name: str, params: dict = None, caller: str = "claude"
    ) -> SkillResult:
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
                skill_name=skill_name,
                success=False,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[f"Unknown skill: {skill_name}"],
            )

        # 2. Caller authorization
        if caller not in skill_def.allowed_callers:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                data={},
                atom_results={},
                degrade_log=[],
                audit_trail={},
                errors=[
                    f"Caller '{caller}' not in allowed_callers {skill_def.allowed_callers} for skill '{skill_name}'"
                ],
            )

        atom_results: dict[str, Any] = {}
        degrade_log: list[str] = []
        errors: list[str] = []
        entity_id: str = ""
        task_description = params.get("task_description", skill_def.description)
        track_start_memory = skill_def.track_start_memory

        # 3. skill_session_start — skip if hook already created one (via /api/skill-track)
        tracking_degraded = False
        hook_entity_id: str | None = None
        try:
            from plastic_promise.mcp.tools.skill_tracking import get_current_entity_id

            hook_entity_id = get_current_entity_id()
        except Exception:
            pass

        if hook_entity_id:
            # Hook already created session — reuse it, skip internal start
            entity_id = hook_entity_id
        else:
            start_handler = self._atoms.get("skill_session_start")
            if start_handler:
                try:
                    start_args = {
                        "skill_name": skill_name,
                        "task_description": task_description,
                    }
                    if not track_start_memory:
                        start_args["record_memory"] = False
                    start_result = await self._call_lifecycle(
                        skill_def,
                        start_handler(
                            self._ctx,
                            start_args,
                        ),
                    )
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
            # 4. Call atoms — concurrent or serial based on SkillDef.concurrent
            if skill_def.concurrent:
                atom_results, degrade_log, errors = await self._exec_atoms_concurrent(
                    skill_def, params, atom_results, degrade_log, errors, entity_id
                )
                # Check for abort-level failures from concurrent execution
                for err in errors:
                    if "abort --" in err:
                        if entity_id and track_start_memory:
                            try:
                                complete_handler = self._atoms.get("skill_session_complete")
                                if complete_handler:
                                    await self._call_lifecycle(
                                        skill_def,
                                        complete_handler(
                                            self._ctx,
                                            {
                                                "entity_id": entity_id,
                                                "outcome": f"abandoned: {err}",
                                            },
                                        ),
                                    )
                            except Exception as close_err:
                                degrade_log.append(
                                    f"skill_session_complete also failed during abort: {close_err}"
                                )
                        return SkillResult(
                            skill_name=skill_name,
                            success=False,
                            data={},
                            atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                            degrade_log=degrade_log,
                            audit_trail={
                                "entity_id": entity_id,
                                "tracking_degraded": tracking_degraded,
                                "tracking_persistence": "memory"
                                if track_start_memory
                                else "entity_only",
                            },
                            errors=errors,
                        )
            else:
                atom_results, degrade_log, errors, should_abort = await self._exec_atoms_serial(
                    skill_def, params, atom_results, degrade_log, errors, entity_id
                )
                if should_abort:
                    return SkillResult(
                        skill_name=skill_name,
                        success=False,
                        data={},
                        atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                        degrade_log=degrade_log,
                        audit_trail={
                            "entity_id": entity_id,
                            "tracking_degraded": tracking_degraded,
                            "tracking_persistence": "memory"
                            if track_start_memory
                            else "entity_only",
                        },
                        errors=errors,
                    )

            # 5. Call handler
            result = await self._call_lifecycle(
                skill_def,
                skill_def.handler(self._ctx, params, atom_results),
            )

            # 6. skill_session_complete — skip if hook will handle it
            if not hook_entity_id and track_start_memory:
                complete_handler = self._atoms.get("skill_session_complete")
                if complete_handler and entity_id:
                    try:
                        await self._call_lifecycle(
                            skill_def,
                            complete_handler(
                                self._ctx,
                                {
                                    "entity_id": entity_id,
                                    "outcome": "",
                                },
                            ),
                        )
                    except Exception as e:
                        degrade_log.append(f"skill_session_complete: {e}")

            # 7. Return
            result.audit_trail = {
                "entity_id": entity_id,
                "tracking_degraded": tracking_degraded,
                "tracking_persistence": "memory" if track_start_memory else "entity_only",
            }
            result.degrade_log = result.degrade_log or degrade_log
            result.errors = result.errors or errors
            return result

        except Exception as e:
            # Handler-level failure -- still attempt session close
            errors.append(f"handler: {e}")
            if entity_id and not hook_entity_id and track_start_memory:
                try:
                    complete_handler = self._atoms.get("skill_session_complete")
                    if complete_handler:
                        await self._call_lifecycle(
                            skill_def,
                            complete_handler(
                                self._ctx,
                                {
                                    "entity_id": entity_id,
                                    "outcome": f"abandoned: handler error -- {e}",
                                },
                            ),
                        )
                except Exception as close_err:
                    degrade_log.append(
                        f"skill_session_complete also failed during handler abort: {close_err}"
                    )
            return SkillResult(
                skill_name=skill_name,
                success=False,
                data={},
                atom_results={k: _text_or_str(v) for k, v in atom_results.items()},
                degrade_log=degrade_log,
                audit_trail={
                    "entity_id": entity_id,
                    "tracking_degraded": tracking_degraded,
                    "tracking_persistence": "memory" if track_start_memory else "entity_only",
                },
                errors=errors,
            )

    async def _exec_atoms_serial(
        self,
        skill_def: SkillDef,
        params: dict,
        atom_results: dict,
        degrade_log: list,
        errors: list,
        entity_id: str,
    ) -> tuple:
        """Execute atoms serially with degradation handling. Returns (atom_results, degrade_log, errors, should_abort)."""
        fallback_executed: set[str] = set()
        for atom_name in skill_def.atoms:
            if atom_name in fallback_executed:
                degrade_log.append(f"{atom_name}: skip -- already executed as fallback")
                continue

            atom_handler = self._atoms.get(atom_name)
            if atom_handler is None:
                msg = f"Atom '{atom_name}' not in registry"
                degrade_log.append(msg)
                errors.append(msg)
                continue

            atom_params = self._build_atom_params(atom_name, params)

            try:
                result = await self._call_atom(skill_def, atom_handler, atom_params)
                atom_results[atom_name] = result
            except TimeoutError:
                error_text = f"timed out after {self._atom_timeout(skill_def)}s"
                action = skill_def.degrade_map.get(atom_name, "abort")
                if action == "skip":
                    degrade_log.append(f"{atom_name}: skip -- {error_text}")
                    continue
                elif action == "warn":
                    degrade_log.append(f"{atom_name}: warn -- {error_text}")
                    continue
                errors.append(f"{atom_name}: {error_text}")
                degrade_log.append(f"{atom_name}: abort -- {error_text}")
                if entity_id:
                    try:
                        complete_handler = self._atoms.get("skill_session_complete")
                        if complete_handler:
                            await self._call_lifecycle(
                                skill_def,
                                complete_handler(
                                    self._ctx,
                                    {
                                        "entity_id": entity_id,
                                        "outcome": f"abandoned: atom {atom_name} timed out",
                                    },
                                ),
                            )
                    except Exception as close_err:
                        degrade_log.append(
                            f"skill_session_complete also failed during abort: {close_err}"
                        )
                return atom_results, degrade_log, errors, True
            except Exception as e:
                action = skill_def.degrade_map.get(atom_name, "abort")
                if action == "skip":
                    degrade_log.append(f"{atom_name}: skip -- {e}")
                    continue
                elif action == "warn":
                    degrade_log.append(f"{atom_name}: warn -- {e}")
                    continue
                elif action.startswith("fallback:"):
                    fallback_atom = action[len("fallback:") :]
                    degrade_log.append(f"{atom_name}: fallback to {fallback_atom} -- {e}")
                    try:
                        fb_handler = self._atoms.get(fallback_atom)
                        if fb_handler:
                            fb_result = await self._call_atom(skill_def, fb_handler, params)
                            atom_results[atom_name] = fb_result
                            fallback_executed.add(fallback_atom)
                    except Exception as fb_e:
                        degrade_log.append(
                            f"{atom_name}: fallback {fallback_atom} also failed -- {fb_e}"
                        )
                        errors.append(f"{atom_name}: {e}")
                    continue
                else:
                    errors.append(f"{atom_name}: {e}")
                    degrade_log.append(f"{atom_name}: abort -- {e}")
                    if entity_id and skill_def.track_start_memory:
                        try:
                            complete_handler = self._atoms.get("skill_session_complete")
                            if complete_handler:
                                await self._call_lifecycle(
                                    skill_def,
                                    complete_handler(
                                        self._ctx,
                                        {
                                            "entity_id": entity_id,
                                            "outcome": f"abandoned: atom {atom_name} failed",
                                        },
                                    ),
                                )
                        except Exception as close_err:
                            degrade_log.append(
                                f"skill_session_complete also failed during abort: {close_err}"
                            )
                    return atom_results, degrade_log, errors, True
        return atom_results, degrade_log, errors, False

    async def _exec_atoms_concurrent(
        self,
        skill_def: SkillDef,
        params: dict,
        atom_results: dict,
        degrade_log: list,
        errors: list,
        entity_id: str,
    ) -> tuple:
        """Execute all atoms concurrently via asyncio.gather with per-atom degrade handling."""

        async def _run_one_atom(atom_name: str):
            atom_handler = self._atoms.get(atom_name)
            if atom_handler is None:
                return atom_name, None, f"Atom '{atom_name}' not in registry"
            atom_params = self._build_atom_params(atom_name, params)
            try:
                result = await self._call_atom(skill_def, atom_handler, atom_params)
                return atom_name, result, None
            except TimeoutError:
                return atom_name, None, f"timed out after {self._atom_timeout(skill_def)}s"
            except Exception as e:
                return atom_name, None, str(e)

        tasks = [_run_one_atom(name) for name in skill_def.atoms]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for item in gathered:
            if isinstance(item, Exception):
                errors.append(f"concurrent execution: {item}")
                continue
            atom_name, result, error = item
            if error:
                action = skill_def.degrade_map.get(atom_name, "abort")
                if action == "skip":
                    degrade_log.append(f"{atom_name}: skip -- {error}")
                elif action == "warn":
                    degrade_log.append(f"{atom_name}: warn -- {error}")
                elif action == "abort":
                    errors.append(f"{atom_name}: abort -- {error}")
                    degrade_log.append(f"{atom_name}: abort -- {error}")
                else:
                    degrade_log.append(f"{atom_name}: {action} -- {error}")
            else:
                atom_results[atom_name] = result

        return atom_results, degrade_log, errors


def _text_or_str(result: list) -> str:
    """Extract text from a list of TextContent, or return str representation."""
    if result and hasattr(result[0], "text"):
        return result[0].text
    return str(result)
