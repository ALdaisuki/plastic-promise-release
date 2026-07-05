"""MCP Skill Tracking tools -- SuperPowers flow traceability

Public tools:
- skill_session_start     : Create a skill execution instance entity
- skill_session_complete  : Mark skill done, tag transition + worth update
- skill_session_trace     : Query execution chain, detect completeness
- skill_session_audit     : Post-hoc gap scan, auto-remediate
"""

import datetime
import json
import logging
import re
import threading
import uuid
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.constants import (
    DOMAIN_TO_TASK_TYPE,
    MAX_STILL_IN_PROGRESS_RENEWALS,
    ORPHAN_THRESHOLD_MINUTES,
    SKILL_CHAIN_MAP,
    SKILL_COMPLETE_WORTH_DELTA,
    SKILL_DOMAIN_MAP,
    normalize_stage_name,
)

# ---------------------------------------------------------------------------
# Module-level state — hook 调用间保持调用链
# ---------------------------------------------------------------------------

_skill_state_lock = threading.Lock()
_current_skill: str | None = None
_parent_entity_id: str | None = None
_current_stage: str | None = None  # Last completed SuperPowers stage name
_current_entity_id: str | None = None  # Currently active session entity_id (hook-created)
_DEFAULT_STAGE_SESSION_ID = "default"
_stage_sessions: dict[str, dict[str, str | None]] = {}


def _normalize_stage_session_id(stage_session_id: str | None = None) -> str:
    value = str(stage_session_id or "").strip()
    return value or _DEFAULT_STAGE_SESSION_ID


def _safe_agent_name(agent_name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(agent_name or "agent")).strip("-")
    return cleaned.lower() or "agent"


def make_stage_session_id(agent_name: str | None = None) -> str:
    """Allocate an isolated SuperPowers chain scope id."""
    return f"stage:{_safe_agent_name(agent_name)}:{uuid.uuid4().hex[:12]}"


def resolve_stage_session_id(args: dict | None = None) -> str:
    """Return caller-provided stage_session_id or allocate a new one."""
    args = args or {}
    explicit = str(args.get("stage_session_id") or args.get("stage_id") or "").strip()
    if explicit:
        return explicit
    return make_stage_session_id(args.get("agent_name") or args.get("agent"))


def _empty_stage_state() -> dict[str, str | None]:
    return {
        "current_skill": None,
        "parent_entity_id": None,
        "current_stage": None,
        "current_entity_id": None,
    }


def get_current_stage(stage_session_id: str | None = None) -> str | None:
    """Return the last completed SuperPowers stage for a chain scope."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            return _current_stage
        return _stage_sessions.get(scope_id, {}).get("current_stage")


def get_parent_entity_id(stage_session_id: str | None = None) -> str | None:
    """Return the parent skill entity for the scoped chain."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            return _parent_entity_id
        return _stage_sessions.get(scope_id, {}).get("parent_entity_id")


def set_current_stage(
    stage: str | None,
    *,
    stage_session_id: str | None = None,
    parent_entity_id: str | None = None,
) -> None:
    """Record the last completed stage for a scoped SuperPowers chain."""
    global _current_stage, _parent_entity_id
    scope_id = _normalize_stage_session_id(stage_session_id)
    normalized_stage = normalize_stage_name(stage) if stage else None
    with _skill_state_lock:
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            _current_stage = normalized_stage
            if parent_entity_id is not None:
                _parent_entity_id = parent_entity_id
            return
        state = _stage_sessions.setdefault(scope_id, _empty_stage_state())
        state["current_stage"] = normalized_stage
        if parent_entity_id is not None:
            state["parent_entity_id"] = parent_entity_id


def get_current_entity_id(stage_session_id: str | None = None) -> str | None:
    """Return the active session entity_id for a scoped hook-created session.

    Used by SkillEngine to skip duplicate skill_session_start when hook already created one.
    """
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            return _current_entity_id
        return _stage_sessions.get(scope_id, {}).get("current_entity_id")


def get_stage_chain_state(stage_session_id: str | None = None) -> dict[str, str | None]:
    """Return a copy of scoped chain state for diagnostics."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            return {
                "stage_session_id": scope_id,
                "current_skill": _current_skill,
                "parent_entity_id": _parent_entity_id,
                "current_stage": _current_stage,
                "current_entity_id": _current_entity_id,
            }
        state = _stage_sessions.get(scope_id, {})
        return {
            "stage_session_id": scope_id,
            "current_skill": state.get("current_skill"),
            "parent_entity_id": state.get("parent_entity_id"),
            "current_stage": state.get("current_stage"),
            "current_entity_id": state.get("current_entity_id"),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_entity_id(skill_name: str) -> str:
    """Generate a unique entity_id for a skill session.

    Format: skill:<skill_name>:<ISO timestamp with microseconds>
    """
    ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()
    return f"skill:{skill_name}:{ts}"


def _skill_start_memory_id(entity_id: str) -> str:
    """Return the deterministic memory id used for a skill start record."""
    return "skill_start_" + entity_id.replace(":", "_")


def _parse_skill_from_entity_id(entity_id: str) -> str | None:
    """Extract skill_name from entity_id like 'skill:brainstorming:2026-...'"""
    parts = entity_id.split(":")
    if len(parts) >= 2 and parts[0] == "skill":
        return parts[1]
    return None


def _get_current_branch() -> str:
    """Detect current git branch name, or return empty string."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _validate_parent(skill_name: str, parent_entity_id: str | None, engine: Any) -> str | None:
    """Check parent is a legal predecessor. Returns warning string or None.

    Never blocks -- always returns None (allowing creation) plus an optional
    warning string that the caller surfaces in chain_warning.
    """
    # auto_inject: sessions have no parent chain — skip validation
    if skill_name.startswith("auto_inject:"):
        return None

    if not parent_entity_id:
        return None
    parent_skill = _parse_skill_from_entity_id(parent_entity_id)
    if not parent_skill:
        return f"Parent entity_id '{parent_entity_id}' does not parse as a skill_session"
    legal_predecessors = SKILL_CHAIN_MAP.get(skill_name, {}).get("predecessors", [])
    if parent_skill not in legal_predecessors:
        expected = ", ".join(legal_predecessors) if legal_predecessors else "none"
        return (
            f"Parent '{parent_skill}' is not a legal predecessor of "
            f"'{skill_name}'. Expected one of: [{expected}]"
        )
    return None


async def _activate_skill_principles(
    engine: Any, skill_name: str, task_description: str
) -> list[dict]:
    """Internally activate principles for the skill's domain.

    Uses a lazy import of handle_principle_activate (matching the pattern
    in server.py) to avoid circular imports at module load time.
    """
    try:
        from plastic_promise.mcp.tools.principles import handle_principle_activate

        domain = SKILL_DOMAIN_MAP.get(skill_name, "all")
        task_type = DOMAIN_TO_TASK_TYPE.get(domain, "general")
        result = await handle_principle_activate(
            engine,
            {
                "task_type": task_type,
                "task_description": task_description,
                "domain_hint": domain,
            },
        )
        data = json.loads(result[0].text)
        return data.get("activated", [])
    except Exception:
        return []


async def _recall_skill_memories(engine: Any, task_description: str) -> list[str]:
    """Internally recall relevant memories for the skill.

    Uses a lazy import of handle_memory_recall, matching server.py pattern.
    """
    try:
        from plastic_promise.mcp.tools.memory import handle_memory_recall

        result = await handle_memory_recall(
            engine,
            {
                "query": task_description,
                "max_results": 10,
            },
        )
        data = json.loads(result[0].text)
        core = data.get("core", [])
        return [item.get("id", "?") for item in core]
    except Exception:
        return []


async def _store_skill_start(
    engine: Any,
    entity_id: str,
    skill_name: str,
    task_description: str,
    domain: str,
) -> str:
    """Persist the skill session start as a lightweight memory record.

    Skill startup is on the critical path for session-init and sp-stage.  It
    must not enter the full memory_store quality pipeline because that can
    synchronously invoke extraction, embedding, LanceDB, and reranking work.
    """
    try:
        content = f"[SKILL START] {skill_name}: {task_description}"
        branch = _get_current_branch()
        tags = [
            "task:active",
            f"skill:{skill_name}",
            f"domain:{domain}",
        ]
        if branch:
            tags.append(f"branch:{branch}")
        memory_id = _skill_start_memory_id(entity_id)
        return engine.register_memory(
            {
                "id": memory_id,
                "content": content,
                "memory_type": "experience",
                "source": "superpowers",
                "entity_ids": [entity_id],
                "tags": tags,
                "domain": domain,
                "tier": "L1",
                "category": "skill_session",
            }
        )
    except Exception:
        return "?"


def _inject_skill_entity(
    engine: Any,
    entity_id: str,
    skill_name: str,
    task_description: str,
    parent_entity_id: str | None,
) -> dict:
    """Register skill_session entity in the context graph.

    Directly calls engine.register_entity() (sync, no lazy import needed).
    Additionally creates a parent_of edge when parent_entity_id is provided,
    so skill_session_trace can reconstruct the execution chain.
    """
    related = [parent_entity_id] if parent_entity_id else []
    try:
        result = engine.register_entity(
            entity_type="skill_session",
            entity_id=entity_id,
            entity_name=skill_name,
            entity_description=task_description,
            related_entities=related,
        )
        # Create explicit parent_of edge for chain traceability
        # register_entity creates "supports" edges (child→parent);
        # skill_session_trace expects "parent_of" edges (parent→child)
        if parent_entity_id:
            child_node = f"skill_session:{entity_id}"
            parent_node = f"skill_session:{parent_entity_id}"
            parent_edge = {
                "from": parent_node,
                "to": child_node,
                "relation": "parent_of",
                "weight": 1.0,
            }
            if not engine.has_graph_edge(parent_edge):
                engine.add_graph_edge(
                    source=parent_edge["from"],
                    target=parent_edge["to"],
                    relation=parent_edge.get("relation", "parent_of"),
                    weight=parent_edge.get("weight", 0.8),
                )
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# skill_session_trace
# ---------------------------------------------------------------------------


async def handle_skill_session_trace(engine: Any, args: dict) -> list[TextContent]:
    """Query skill execution chain and detect completeness, gaps, and violations.

    Collects all skill_session entities from the context graph, finds their
    associated memory records, parses status from tags, and builds the
    parent/child chain from graph edges.  Performs three gap-detection
    passes:

    * orphan_active -- status=active but last_accessed > 30 min ago
    * chain_broken  -- status=done with expected successors but no child
    * tag_mismatch   -- content contains [SKILL COMPLETE] but task:done tag missing

    Args:
        engine: ContextEngine instance (must expose _graph_nodes,
            _graph_edges, and _memories).
        args:
            session_scope: str -- \"current\" | \"branch\" | \"all\" (default \"all\")
            skill_name: str | None -- Filter by skill name
            status: str | None -- Filter by status: \"active\"|\"done\"|\"abandoned\"

    Returns:
        list[TextContent]: MCP response with sessions[], chain_complete,
        chain_valid, gaps[], chain_warnings[], total_count.
    """
    session_scope: str = args.get("session_scope", "all")
    skill_filter: str | None = args.get("skill_name")
    status_filter: str | None = args.get("status")
    include_auto_inject: bool = args.get("include_auto_inject", False)

    # -- Resolve branch name for session_scope "branch" ----------------------
    current_branch: str = ""
    if session_scope == "branch":
        current_branch = _get_current_branch()
        if not current_branch:
            session_scope = "current"  # fallback when not in a git repo

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

    def _graph_nodes() -> list[dict]:
        try:
            nodes = engine.list_graph_nodes()
            if isinstance(nodes, list):
                return nodes
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: list_graph_nodes() failed, falling back to _graph_nodes: %s",
                e,
            )
        raw = getattr(engine, "_graph_nodes", {})
        if isinstance(raw, dict):
            return [dict({"id": node_id}, **data) for node_id, data in raw.items()]
        return []

    def _graph_edges() -> list[dict]:
        try:
            edges = engine.list_graph_edges()
            if isinstance(edges, list):
                return edges
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: list_graph_edges() failed, falling back to _graph_edges: %s",
                e,
            )
        raw = getattr(engine, "_graph_edges", [])
        return raw if isinstance(raw, list) else []

    def _iter_memories() -> list[Any]:
        try:
            memories = engine.iter_memories()
            if isinstance(memories, list):
                return memories
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: iter_memories() failed, falling back to _memories: %s", e
            )
        raw = getattr(engine, "_memories", {})
        if isinstance(raw, dict):
            return list(raw.values())
        return []

    # -- Collect skill_session entities from graph nodes --------------------
    sessions: list[dict] = []

    for node in _graph_nodes():
        node_id = node.get("id", "")
        if not isinstance(node, dict):
            continue
        if node.get("type") != "skill_session":
            continue

        # Strip the "skill_session:" prefix to get the raw entity_id
        raw_entity_id: str = node_id
        if raw_entity_id.startswith("skill_session:"):
            raw_entity_id = raw_entity_id[len("skill_session:") :]
        skill_name: str = node.get("name", "unknown")

        if skill_filter and skill_name != skill_filter:
            continue

        # -- Find associated memory record ----------------------------------
        memory: dict[str, Any] | None = None
        for mem in _iter_memories():
            # Normalize to dict (handle both dict and object memories)
            if isinstance(mem, dict):
                mem_dict = mem
            else:
                mem_dict = {k: getattr(mem, k, None) for k in dir(mem) if not k.startswith("_")}
            mem_entity_ids: list = mem_dict.get("entity_ids", [])
            if not isinstance(mem_entity_ids, list):
                mem_entity_ids = []
            if raw_entity_id in mem_entity_ids:
                memory = mem_dict
                break

        # -- Determine status from tags -------------------------------------
        tags: list[str] = memory.get("tags", []) if memory else []
        status: str = "active"
        if "task:done" in tags:
            status = "done"
        elif "task:abandoned" in tags:
            status = "abandoned"

        if status_filter and status != status_filter:
            continue

        # -- Scope filtering ------------------------------------------------
        if session_scope == "branch" and current_branch:
            branch_tag = f"branch:{current_branch}"
            if branch_tag not in tags:
                continue

        # -- Parse content --------------------------------------------------
        content: str = memory.get("content", "") if memory else ""
        is_skill_start_memory = "[SKILL START]" in content
        tracking_persistence = "memory" if is_skill_start_memory else "entity_only"
        outcome: str = ""
        if "[SKILL COMPLETE]" in content:
            parts = content.split("[SKILL COMPLETE]")
            if len(parts) > 1:
                outcome_line = parts[-1].split("\n")[0].strip()
                outcome = outcome_line
        elif "[SKILL ABANDONED]" in content:
            parts = content.split("[SKILL ABANDONED]")
            if len(parts) > 1:
                outcome = parts[-1].split("\n")[0].strip()

        # -- Timestamps -----------------------------------------------------
        started_at: str = memory.get("created_at", "") if memory else ""
        last_accessed: str = memory.get("last_accessed", "") if memory else ""
        completed_at: str = ""
        duration_ms: int | None = None

        # Extract duration from content if a [SKILL COMPLETE] marker exists
        if "[SKILL COMPLETE]" in content:
            import re as _re

            dur_match = _re.search(r"duration_ms=(\d+)", content)
            if dur_match:
                duration_ms = int(dur_match.group(1))

        # -- Child sessions via graph edges ---------------------------------
        child_skills: list[str] = []
        for edge in _graph_edges():
            if not isinstance(edge, dict):
                continue
            # Edge goes FROM parent TO child with relation "parent_of"
            if (
                edge.get("from") == f"skill_session:{raw_entity_id}"
                and edge.get("relation") == "parent_of"
            ):
                child_id = edge.get("to", "")
                if isinstance(child_id, str) and child_id.startswith("skill_session:"):
                    child_skills.append(child_id[len("skill_session:") :])

        sessions.append(
            {
                "entity_id": raw_entity_id,
                "skill_name": skill_name,
                "status": status,
                "started_at": started_at,
                "completed_at": completed_at,
                "last_accessed": last_accessed,
                "duration_ms": duration_ms,
                "description": node.get("description", ""),
                "outcome": outcome,
                "tracking_persistence": tracking_persistence,
                "parent_skill": None,  # filled below via edge lookup
                "child_skills": child_skills,
            }
        )

    # -- Build parent relationships from edges ------------------------------
    for edge in _graph_edges():
        if not isinstance(edge, dict):
            continue
        if edge.get("relation") == "parent_of":
            child_full_id: str = edge.get("to", "")
            parent_full_id: str = edge.get("from", "")
            for s in sessions:
                if f"skill_session:{s['entity_id']}" == child_full_id:
                    if parent_full_id.startswith("skill_session:"):
                        s["parent_skill"] = parent_full_id[len("skill_session:") :]

    # -- Exclude auto_inject sessions by default ----------------------------
    if not include_auto_inject:
        sessions = [s for s in sessions if not s["skill_name"].startswith("auto_inject:")]

    # -- Gap detection ------------------------------------------------------
    gaps: list[dict] = []
    chain_warnings: list[dict] = []

    for s in sessions:
        # auto_inject: sessions are instant — skip orphan detection
        if s["skill_name"].startswith("auto_inject:"):
            continue

        # 1. orphan_active: active and last_accessed > threshold
        if (
            s["status"] == "active"
            and s["last_accessed"]
            and s.get("tracking_persistence") == "memory"
        ):
            try:
                la = datetime.datetime.fromisoformat(s["last_accessed"])
                if la.tzinfo is not None:
                    la = la.replace(tzinfo=None)
                idle_minutes = (now - la).total_seconds() / 60.0
                if idle_minutes > ORPHAN_THRESHOLD_MINUTES:
                    gaps.append(
                        {
                            "type": "orphan_active",
                            "entity_id": s["entity_id"],
                            "skill_name": s["skill_name"],
                            "idle_minutes": round(idle_minutes, 1),
                            "suggestion": ("手動 skill_session_complete(entity_id, outcome)"),
                        }
                    )
            except (ValueError, TypeError):
                pass

        # 2. chain_broken: done but has successors in SKILL_CHAIN_MAP
        #    and no child sessions recorded
        if s["status"] == "done":
            expected_successors = SKILL_CHAIN_MAP.get(s["skill_name"], {}).get("successors", [])
            if expected_successors and not s["child_skills"]:
                chain_warnings.append(
                    {
                        "type": "chain_broken",
                        "entity_id": s["entity_id"],
                        "skill_name": s["skill_name"],
                        "expected_next": expected_successors,
                    }
                )

        # 3. tag_mismatch: content marks completion but task:done tag missing
        if s["status"] == "done":
            # Re-check original memory for tag integrity
            mem_for_session = None
            for mem in _iter_memories():
                if isinstance(mem, dict):
                    m = mem
                else:
                    m = {k: getattr(mem, k, None) for k in dir(mem) if not k.startswith("_")}
                eids = m.get("entity_ids", [])
                if not isinstance(eids, list):
                    eids = []
                if s["entity_id"] in eids:
                    mem_for_session = m
                    break

            if mem_for_session:
                mem_tags: list[str] = mem_for_session.get("tags", [])
                mem_content: str = mem_for_session.get("content", "")
                has_done_marker = "[SKILL COMPLETE]" in mem_content
                has_done_tag = "task:done" in mem_tags
                if has_done_marker and not has_done_tag:
                    gaps.append(
                        {
                            "type": "tag_mismatch",
                            "entity_id": s["entity_id"],
                            "skill_name": s["skill_name"],
                            "detail": ("Content has [SKILL COMPLETE] but task:done tag is missing"),
                        }
                    )

    # -- Chain validation ---------------------------------------------------
    chain_complete: bool = len(gaps) == 0
    chain_valid: bool = len(chain_warnings) == 0

    response: dict[str, Any] = {
        "sessions": sessions,
        "chain_complete": chain_complete,
        "chain_valid": chain_valid,
        "gaps": gaps,
        "chain_warnings": chain_warnings,
        "total_count": len(sessions),
    }

    return [
        TextContent(
            type="text",
            text=json.dumps(
                response,
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# skill_session_start
# ---------------------------------------------------------------------------


async def handle_skill_session_start(engine: Any, args: dict) -> list[TextContent]:
    """Create a skill_session entity and record the start of a skill execution.

    Internal steps:
    1. Validate skill_name against SKILL_DOMAIN_MAP
    2. Derive domain and generate entity_id
    3. Parent chain validation (warning, never blocking)
    4. Register entity in context graph via engine.register_entity()
    5. Persist a lightweight memory record with tags unless record_memory=False

    Args:
        engine: ContextEngine instance.
        args:
            skill_name: str (required) -- Skill name
            task_description: str (required) -- What this execution does
            parent_entity_id: str | None -- Parent skill's entity_id
            record_memory: bool -- False keeps tracking entity-only
            estimated_duration_minutes: int | None -- Optional estimate

    Returns:
        list[TextContent]: MCP response with entity_id, domain, activated
        principles, related memories, tags, and chain_warning if applicable.
    """
    skill_name = args.get("skill_name", "")
    task_description = args.get("task_description", "")
    stage_session_id = args.get("stage_session_id") or args.get("stage_id")
    parent_entity_id = args.get("parent_entity_id") or get_parent_entity_id(stage_session_id)
    record_memory = bool(args.get("record_memory", True))

    # Normalize: strip plugin namespace prefix (e.g. "superpowers:brainstorming" → "brainstorming")
    _normalized_name = skill_name
    _known_prefixes = ("superpowers:",)
    for prefix in _known_prefixes:
        if skill_name.startswith(prefix):
            _normalized_name = skill_name[len(prefix) :]
            break

    # Validate skill_name (auto_inject:* is always allowed)
    if not _normalized_name.startswith("auto_inject:") and _normalized_name not in SKILL_DOMAIN_MAP:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": (
                            f"Unknown skill_name '{_normalized_name}' (raw: '{skill_name}'). "
                            f"Known skills: {list(SKILL_DOMAIN_MAP.keys())}"
                        ),
                        "tool": "skill_session_start",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    # Derive domain and entity_id (entity_id keeps original full name for traceability)
    # auto_inject:* prefix → "reflecting" domain (context audit snapshot)
    if _normalized_name.startswith("auto_inject:"):
        domain = "reflecting"
    else:
        domain = SKILL_DOMAIN_MAP.get(_normalized_name, "general")
    # Use the normalized name for entity_id (avoids colons breaking parse)
    # Store original name in the entity description for traceability
    entity_id = _make_entity_id(_normalized_name)
    # Build description with original full name if different
    if _normalized_name != skill_name:
        task_description = f"[{skill_name}] {task_description}"

    # Parent chain validation (warning, not blocking)
    chain_warning = _validate_parent(skill_name, parent_entity_id, engine)

    # 1. Register entity in context graph
    _inject_skill_entity(
        engine,
        entity_id,
        skill_name,
        task_description,
        parent_entity_id,
    )

    # 2. Persist as memory record unless the caller explicitly requests
    # entity-only tracking for lightweight bootstrap paths.
    memory_id = ""
    if record_memory:
        memory_id = await _store_skill_start(
            engine,
            entity_id,
            skill_name,
            task_description,
            domain,
        )

    tags_applied = ["task:active", f"skill:{skill_name}", f"domain:{domain}"]

    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        "status": "active",
        "domain": domain,
        "activated_principles": [],  # handled by atoms, not duplicated here
        "related_memories": [],  # callers use explicit memory_recall/context_supply
        "tracking_persistence": "memory" if record_memory else "entity_only",
        "stage_session_id": _normalize_stage_session_id(stage_session_id),
        "tags_applied": tags_applied,
        "chain_warning": chain_warning,
        "memory_id": memory_id,
    }

    return [
        TextContent(
            type="text",
            text=json.dumps(
                response,
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# skill_session_complete
# ---------------------------------------------------------------------------


async def handle_skill_session_complete(engine: Any, args: dict) -> list[TextContent]:
    """Mark a skill session as complete, handling tag transitions and worth updates.

    Three outcomes based on the ``outcome`` argument:

    1. **still_in_progress**: Refresh last_accessed, count ``[still_in_progress]``
       markers in content.  If renewals >= MAX_STILL_IN_PROGRESS_RENEWALS (3),
       add ``task:overdue`` tag.  Status stays ``"still_active"``.  Returns
       ``next_skills: []``, ``worth_update: None``.

    2. **abandoned: <reason>**: Transition to abandoned.  Add ``task:abandoned``
       tag, remove ``task:active``.  No worth update.

    3. **Normal** (outcome is None / empty): Transition to done.  Calculate
       ``duration_ms``.  Add ``task:done`` tag.  Update worth_score via
       ``handle_feedback_apply`` (adopted, +SKILL_COMPLETE_WORTH_DELTA).
       Return ``next_skills`` from ``SKILL_CHAIN_MAP[skill_name].successors``.
       Register artifact memories if provided.

    Args:
        engine: ContextEngine instance (must provide ``_memories`` dict).
        args:
            entity_id: str (required) -- The skill session entity_id.
            outcome: str | None -- ``"still_in_progress"``,
                ``"abandoned: <reason>"``, or omitted for normal completion.
            artifacts: list[str] -- Optional list of artifact paths to register.

    Returns:
        list[TextContent]: MCP response with status, next_skills, worth_update,
        memory_id, and optionally artifact_memory_ids.
    """
    entity_id = args.get("entity_id", "")
    outcome = args.get("outcome")
    artifacts = args.get("artifacts", [])

    # ------------------------------------------------------------------
    # Locate the existing skill-start memory
    # ------------------------------------------------------------------
    memory_id = _skill_start_memory_id(entity_id)
    mem_data = None

    # Fast path: SkillEngine-created sessions use a deterministic id. Avoid
    # scanning the whole memory pool on the sp-stage hot path.
    if hasattr(engine, "get_memory_dict"):
        try:
            mem_data = engine.get_memory_dict(memory_id)
        except Exception:
            mem_data = None
        if not isinstance(mem_data, dict) or "[SKILL START]" not in mem_data.get(
            "content", ""
        ):
            mem_data = None

    # Compatibility fallback for older records or test doubles.
    if mem_data is None:
        memory_id = None
        for mem in engine.iter_memories():
            mid = mem.get("id", "")
            # mem is always a plain dict (register_memory / store_memory both
            # produce dicts).  Normalize defensively in case a MemoryRecord
            # object slips through from older paths.
            if isinstance(mem, dict):
                mem_entity_ids = mem.get("entity_ids", [])
                mem_content = mem.get("content", "")
            else:
                mem_entity_ids = getattr(mem, "entity_ids", [])
                mem_content = getattr(mem, "content", "")

            if entity_id in mem_entity_ids and "[SKILL START]" in mem_content:
                memory_id = mid
                if isinstance(mem, dict):
                    mem_data = dict(mem)  # shallow copy so we can mutate safely
                else:
                    mem_data = {
                        k: getattr(mem, k, None) for k in dir(mem) if not k.startswith("_")
                    }
                break

    if not memory_id:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": (f"No skill session memory found for entity_id '{entity_id}'"),
                        "tool": "skill_session_complete",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    skill_name = _parse_skill_from_entity_id(entity_id) or "unknown"
    created_at = mem_data.get("created_at", "")

    # ------------------------------------------------------------------
    # Outcome: abandoned
    # ------------------------------------------------------------------
    if outcome and outcome.startswith("abandoned:"):
        reason = outcome[len("abandoned:") :].strip()
        tags: list[str] = list(mem_data.get("tags", []))

        if "task:active" in tags:
            tags.remove("task:active")
        if "task:abandoned" not in tags:
            tags.append("task:abandoned")

        new_content = mem_data.get("content", "") + f"\n[SKILL ABANDONED] {reason}"

        # Persist via public API
        engine.update_memory_fields(memory_id, tags=tags, content=new_content)

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "entity_id": entity_id,
                        "skill_name": skill_name,
                        "status": "abandoned",
                        "reason": reason,
                        "next_skills": [],
                        "worth_update": None,
                        "memory_id": memory_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Outcome: still_in_progress
    # ------------------------------------------------------------------
    if outcome == "still_in_progress":
        current_content = mem_data.get("content", "")
        renewal_count = current_content.count("[still_in_progress]")
        new_content = current_content + "\n[still_in_progress]"

        tags: list[str] = list(mem_data.get("tags", []))

        overdue = False
        if renewal_count >= MAX_STILL_IN_PROGRESS_RENEWALS:
            if "task:overdue" not in tags:
                tags.append("task:overdue")
            overdue = True

        # Persist via public API
        engine.update_memory_fields(
            memory_id,
            content=new_content,
            tags=tags,
            last_accessed=datetime.datetime.now(datetime.UTC).isoformat(),
        )

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "entity_id": entity_id,
                        "skill_name": skill_name,
                        "status": "still_active",
                        "next_skills": [],
                        "worth_update": None,
                        "memory_id": memory_id,
                        "renewal_count": renewal_count + 1,
                        "overdue": overdue,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Normal outcome -- transition to done
    # ------------------------------------------------------------------

    # -- duration --
    duration_ms = None
    if created_at:
        try:
            start_dt = datetime.datetime.fromisoformat(created_at)
            # created_at from soul_memory uses local time (no tzinfo).
            # Use local now() to match, then strip tzinfo for safety.
            now_local = datetime.datetime.now().replace(tzinfo=None)
            if start_dt.tzinfo is not None:
                start_dt = start_dt.replace(tzinfo=None)
            delta = now_local - start_dt
            duration_ms = int(delta.total_seconds() * 1000)
        except Exception:
            duration_ms = None

    # -- tag transition --
    tags: list[str] = list(mem_data.get("tags", []))
    if "task:active" in tags:
        tags.remove("task:active")
    if "task:done" not in tags:
        tags.append("task:done")
    engine.update_memory_fields(memory_id, tags=tags)

    # -- content update (guard against duplicate [SKILL COMPLETE] markers) --
    current_content = mem_data.get("content", "")
    if "[SKILL COMPLETE]" not in current_content:
        new_content = current_content + f"\n[SKILL COMPLETE] duration_ms={duration_ms}"
        engine.update_memory_fields(memory_id, content=new_content)
    else:
        # Already completed from a prior call — don't append again
        pass

    # -- worth update via feedback_apply --
    worth_update = None
    try:
        from plastic_promise.mcp.tools.reflection import handle_feedback_apply

        fb_result = await handle_feedback_apply(
            engine,
            {
                "item_id": memory_id,
                "feedback_type": "adopted",
            },
        )
        fb_data = json.loads(fb_result[0].text)
        worth_update = fb_data.get("new_worth_score", SKILL_COMPLETE_WORTH_DELTA)
    except Exception:
        worth_update = SKILL_COMPLETE_WORTH_DELTA

    # -- chain successors --
    next_skills: list[str] = SKILL_CHAIN_MAP.get(skill_name, {}).get("successors", [])

    # -- register artifacts --
    artifact_results: list[str] = []
    if artifacts:
        try:
            from plastic_promise.mcp.tools.memory import handle_memory_store

            for art_path in artifacts:
                try:
                    art_result = await handle_memory_store(
                        engine,
                        {
                            "content": (f"[SKILL ARTIFACT] {skill_name}: {art_path}"),
                            "memory_type": "code",
                            "source": "superpowers",
                            "entity_ids": [entity_id],
                            "tags": ["task:artifact", f"skill:{skill_name}"],
                        },
                    )
                    art_data = json.loads(art_result[0].text)
                    artifact_results.append(art_data.get("memory_id", "?"))
                except Exception:
                    artifact_results.append("?")
        except ImportError:
            pass

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "entity_id": entity_id,
                    "skill_name": skill_name,
                    "status": "done",
                    "duration_ms": duration_ms,
                    "next_skills": next_skills,
                    "worth_update": worth_update,
                    "memory_id": memory_id,
                    "artifact_memory_ids": artifact_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# skill_session_audit
# ---------------------------------------------------------------------------


async def handle_skill_session_audit(engine: Any, args: dict) -> list[TextContent]:
    """Post-hoc gap scan for skill sessions, with optional auto-remediation.

    Scans the context graph for existing skill_session entities, searches
    engine._memories for mentions of known skill names, and reports gaps
    where a skill is mentioned but no session entity exists.

    When ``auto_fix=True``, each gap is auto-remediated by:
    1. Checking ``skill_has_any_session`` (graph nodes by name) to avoid
       creating duplicate sessions when a skill is mentioned multiple times
    2. Calling ``handle_skill_session_start`` with a ``[事后补录]`` description
    3. Immediately calling ``handle_skill_session_complete`` to mark it done

    Args:
        engine: ContextEngine instance (must expose ``_graph_nodes`` and
            ``_memories``).
        args:
            auto_fix: bool -- Auto-create + complete missing sessions
                (default False).
            skill_name: str | None -- Only audit a specific skill.

    Returns:
        list[TextContent]: MCP response with ``scanned_sessions``,
        ``gaps_found[]``, and ``auto_fixed[]``.
    """
    auto_fix: bool = args.get("auto_fix", False)
    skill_filter: str | None = args.get("skill_name")

    known_skill_names: set[str] = set(SKILL_DOMAIN_MAP.keys())

    # ------------------------------------------------------------------
    # 1. Scan existing skill_session entities from graph nodes
    # ------------------------------------------------------------------
    existing_sessions: dict[str, list[str]] = {}  # skill_name -> [entity_ids]
    for node in engine.list_graph_nodes():
        node_id = node.get("id", "")
        if not isinstance(node, dict):
            continue
        if node.get("type") != "skill_session":
            continue
        # Strip the "skill_session:" prefix to get the raw entity_id
        raw_entity_id: str = node_id
        if raw_entity_id.startswith("skill_session:"):
            raw_entity_id = raw_entity_id[len("skill_session:") :]
        skill_name: str = node.get("name", "unknown")
        if skill_name not in existing_sessions:
            existing_sessions[skill_name] = []
        existing_sessions[skill_name].append(raw_entity_id)

    scanned_sessions: int = sum(len(v) for v in existing_sessions.values())

    # ------------------------------------------------------------------
    # 2. Scan engine._memories for mentions of known skill names
    # ------------------------------------------------------------------
    mentioned_skills: set[str] = set()
    for mem in engine.iter_memories():
        # Normalize to dict (handle both dict and object memories)
        if isinstance(mem, dict):
            content: str = mem.get("content", "")
        else:
            content = getattr(mem, "content", "")
        if not content:
            continue
        for skill_name in known_skill_names:
            if skill_filter and skill_name != skill_filter:
                continue
            # Best-effort heuristic: substring match of skill name in content
            if skill_name in content:
                mentioned_skills.add(skill_name)

    # ------------------------------------------------------------------
    # 3. Detect gaps — mentioned skills without sessions
    #    De-duplicated by skill_name (set iteration)
    # ------------------------------------------------------------------
    gaps: list[dict] = []
    for skill_name in sorted(mentioned_skills):
        if skill_name not in existing_sessions:
            gaps.append(
                {
                    "type": "missing_start",
                    "skill_name": skill_name,
                    "domain": SKILL_DOMAIN_MAP.get(skill_name, "unknown"),
                }
            )

    # ------------------------------------------------------------------
    # 4. Auto-fix mode
    # ------------------------------------------------------------------
    auto_fixed: list[dict] = []
    if auto_fix and gaps:
        for gap in gaps:
            skill_name = gap["skill_name"]

            # ---------- skill_has_any_session guard ----------
            # Re-check graph nodes by name (not entity_id) to prevent
            # creating duplicates when a skill is mentioned multiple times
            # and another auto_fix iteration already created one.
            skill_has_any_session: bool = False
            for node in engine.list_graph_nodes():
                if not isinstance(node, dict):
                    continue
                if node.get("type") != "skill_session":
                    continue
                if node.get("name") == skill_name:
                    skill_has_any_session = True
                    break

            if skill_has_any_session:
                auto_fixed.append(
                    {
                        "skill_name": skill_name,
                        "status": "skipped",
                        "reason": "session_already_exists",
                    }
                )
                continue

            try:
                # Create session with [事后补录] description
                start_result = await handle_skill_session_start(
                    engine,
                    {
                        "skill_name": skill_name,
                        "task_description": f"[事后补录] {skill_name}",
                        "parent_entity_id": None,
                    },
                )
                start_data = json.loads(start_result[0].text)

                if "error" in start_data:
                    auto_fixed.append(
                        {
                            "skill_name": skill_name,
                            "status": "failed",
                            "reason": start_data["error"],
                        }
                    )
                    continue

                entity_id: str = start_data["entity_id"]

                # Immediately mark as done
                complete_result = await handle_skill_session_complete(
                    engine,
                    {
                        "entity_id": entity_id,
                    },
                )
                complete_data = json.loads(complete_result[0].text)

                auto_fixed.append(
                    {
                        "skill_name": skill_name,
                        "status": "fixed",
                        "entity_id": entity_id,
                        "memory_id": complete_data.get("memory_id", "?"),
                    }
                )
            except Exception as exc:
                auto_fixed.append(
                    {
                        "skill_name": skill_name,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )

    response: dict[str, Any] = {
        "scanned_sessions": scanned_sessions,
        "gaps_found": gaps,
        "auto_fixed": auto_fixed,
    }

    return [
        TextContent(
            type="text",
            text=json.dumps(
                response,
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# skill_auto_track — hook 调用的自动 Skill 追踪
# ---------------------------------------------------------------------------


async def handle_skill_auto_track(engine: Any, args: dict) -> list[TextContent]:
    """Auto-track Skill calls via PreToolUse/PostToolUse hooks.

    Called by hook system — no manual invocation needed.
    Manages a linear skill chain via module-level state.

    **Lightweight design**: Creates only the entity marker without doing
    the full skill_session_start pipeline (no memory_recall, no memory_store).
    The heavy work is deferred to the SkillEngine atoms that run after hooks.

    Args:
        engine: ContextEngine instance.
        args: {"phase": "start"|"complete", "skill_name": str}

    Returns:
        list[TextContent]: tracking status
    """
    global _current_skill, _parent_entity_id, _current_stage, _current_entity_id
    phase = args.get("phase", "start")
    skill_name = args.get("skill_name", "")
    stage_session_id = args.get("stage_session_id") or args.get("stage_id")
    scope_id = _normalize_stage_session_id(stage_session_id)

    if phase == "start":
        with _skill_state_lock:
            # ── Lightweight start: just create entity + activate principles ──
            lookup_name = normalize_stage_name(skill_name)
            entity_id = _make_entity_id(lookup_name)
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                parent_entity_id = _parent_entity_id
            else:
                state = _stage_sessions.setdefault(scope_id, _empty_stage_state())
                parent_entity_id = state.get("parent_entity_id")

            # Register entity in context graph (fast, in-memory)
            # Use _parent_entity_id to link to the previous skill in the chain
            try:
                _inject_skill_entity(
                    engine,
                    entity_id,
                    lookup_name,
                    f"auto-tracked: {lookup_name}",
                    parent_entity_id,
                )
            except Exception:
                pass

            # Activate principles (fast, in-memory)
            try:
                await _activate_skill_principles(
                    engine, lookup_name, f"auto-tracked: {lookup_name}"
                )
            except Exception:
                pass

            # NOTE: memory_recall and memory_store are intentionally SKIPPED here.
            # The SkillEngine atoms (principle_activate + memory_store) run after
            # hooks and handle the heavy work. Doing it here would double the cost.
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                _current_skill = entity_id
                _current_entity_id = entity_id
            else:
                state["current_skill"] = entity_id
                state["current_entity_id"] = entity_id
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "entity_id": entity_id,
                        "status": "tracking",
                        "phase": "start",
                        "stage_session_id": scope_id,
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    elif phase == "complete":
        with _skill_state_lock:
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                eid = _current_skill
            else:
                state = _stage_sessions.setdefault(scope_id, _empty_stage_state())
                eid = state.get("current_skill")
            if eid:
                try:
                    # Lightweight complete: run the session_complete handler
                    await handle_skill_session_complete(
                        engine,
                        {
                            "entity_id": eid,
                            "outcome": "auto-tracked",
                            "artifacts": [],
                        },
                    )
                except Exception:
                    pass
            completed_stage = normalize_stage_name(skill_name)
            if completed_stage in SKILL_CHAIN_MAP:
                if scope_id == _DEFAULT_STAGE_SESSION_ID:
                    _parent_entity_id = eid
                    _current_stage = completed_stage  # Track last completed SuperPowers stage
                else:
                    state["parent_entity_id"] = eid
                    state["current_stage"] = completed_stage
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                _current_skill = None
                _current_entity_id = None  # Clear hook session marker
                next_parent = _parent_entity_id
                current_stage = _current_stage
            else:
                state["current_skill"] = None
                state["current_entity_id"] = None
                next_parent = state.get("parent_entity_id")
                current_stage = state.get("current_stage")
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "tracked",
                        "phase": "complete",
                        "stage_session_id": scope_id,
                        "next_parent": next_parent,
                        "current_stage": current_stage,
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    return [
        TextContent(
            type="text", text=json.dumps({"error": f"Unknown phase: {phase!r}"}, ensure_ascii=False)
        )
    ]
