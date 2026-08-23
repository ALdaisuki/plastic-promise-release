"""MCP skill tracking tools for the pinned official workflow.

Public tools:
- skill_session_start     : Create a skill execution instance entity
- skill_session_complete  : Mark skill done, tag transition + worth update
- skill_session_trace     : Query execution chain, detect completeness
- skill_session_audit     : Post-hoc gap scan, auto-remediate
"""

import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import re
import threading
import uuid
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.constants import (
    DOMAIN_TO_TASK_TYPE,
    ORPHAN_THRESHOLD_MINUTES,
    SKILL_CHAIN_MAP,
    SKILL_COMPLETE_WORTH_DELTA,
    SKILL_DOMAIN_MAP,
    TRACKABLE_SKILL_DOMAIN_MAP,
    normalize_stage_name,
)
from plastic_promise.core.constants import (
    MAX_STILL_IN_PROGRESS_RENEWALS as _MAX_STILL_IN_PROGRESS_RENEWALS,
)
from plastic_promise.core.official_workflow import COMPOSITE_SKILL_CALLS
from plastic_promise.core.synthesis import synthesis_content_hash
from plastic_promise.core.workflow_state import (
    WorkflowState,
    compose_flow_scope,
    engine_connection,
    load_workflow_state,
    save_workflow_state,
    split_flow_scope,
)

# Kept as a module-level compatibility export for callers and tests.
MAX_STILL_IN_PROGRESS_RENEWALS = _MAX_STILL_IN_PROGRESS_RENEWALS

# ---------------------------------------------------------------------------
# Module-level state — hook 调用间保持调用链
# ---------------------------------------------------------------------------

_skill_state_lock = threading.Lock()
_current_skill: str | None = None
_parent_entity_id: str | None = None
_current_stage: str | None = None  # Last completed official workflow stage
_current_entity_id: str | None = None  # Currently active session entity_id (hook-created)
_DEFAULT_STAGE_SESSION_ID = "default"
_stage_sessions: dict[str, dict[str, Any]] = {}


def _normalize_stage_session_id(stage_session_id: str | None = None) -> str:
    value = str(stage_session_id or "").strip()
    return value or _DEFAULT_STAGE_SESSION_ID


def _safe_agent_name(agent_name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(agent_name or "agent")).strip("-")
    return cleaned.lower() or "agent"


def make_stage_session_id(agent_name: str | None = None) -> str:
    """Allocate an isolated official workflow scope id."""
    return f"stage:{_safe_agent_name(agent_name)}:{uuid.uuid4().hex[:12]}"


def resolve_stage_session_id(args: dict | None = None) -> str:
    """Return caller-provided stage_session_id or allocate a new one."""
    args = args or {}
    explicit = str(args.get("stage_session_id") or args.get("stage_id") or "").strip()
    if explicit:
        return explicit
    return make_stage_session_id(args.get("agent_name") or args.get("agent"))


def _empty_stage_state() -> dict[str, Any]:
    return {
        "current_skill": None,
        "parent_entity_id": None,
        "current_stage": None,
        "current_entity_id": None,
        "route_id": "",
        "current_step_index": -1,
    }


def _load_durable_stage_state(scope_id: str, engine: Any = None) -> dict[str, Any] | None:
    connection = engine_connection(engine)
    if connection is None:
        return None
    persisted = load_workflow_state(connection, scope_id)
    if persisted is None:
        return None
    return {
        "current_skill": persisted.current_entity_id,
        "parent_entity_id": persisted.parent_entity_id,
        "current_stage": persisted.current_stage,
        "current_entity_id": persisted.current_entity_id,
        "route_id": persisted.route_id,
        "current_step_index": persisted.current_step_index,
    }


def _state_for_scope_locked(scope_id: str, engine: Any = None) -> dict[str, Any]:
    """Load one scope while the caller holds ``_skill_state_lock``."""
    connection = engine_connection(engine)
    if connection is not None:
        cache_key = f"engine:{id(engine)}:{scope_id}"
        state = _load_durable_stage_state(scope_id, engine)
        if state is None:
            state = _empty_stage_state()
        _stage_sessions[cache_key] = state
        return state

    # Compatibility callers and test doubles without canonical SQLite state
    # share the process-local scope established by set_current_stage().
    cache_key = scope_id
    state = _stage_sessions.get(cache_key)
    if state is None:
        state = _empty_stage_state()
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            state.update(
                {
                    "current_skill": _current_skill,
                    "parent_entity_id": _parent_entity_id,
                    "current_stage": _current_stage,
                    "current_entity_id": _current_entity_id,
                }
            )
    _stage_sessions[cache_key] = state
    return state


def _sync_default_globals_locked(state: dict[str, Any]) -> None:
    """Keep legacy module exports as mirrors of the durable default scope."""
    global _current_skill, _parent_entity_id, _current_stage, _current_entity_id
    _current_skill = state.get("current_skill")
    _parent_entity_id = state.get("parent_entity_id")
    _current_stage = state.get("current_stage")
    _current_entity_id = state.get("current_entity_id")


def _persist_stage_state(scope_id: str, state: dict[str, Any], engine: Any = None) -> None:
    connection = engine_connection(engine)
    if connection is None:
        return
    stage_session_id, flow_line_id = split_flow_scope(scope_id)
    save_workflow_state(
        connection,
        WorkflowState(
            scope_id=scope_id,
            stage_session_id=stage_session_id,
            flow_line_id=flow_line_id,
            route_id=str(state.get("route_id") or ""),
            current_stage=state.get("current_stage"),
            current_step_index=int(state.get("current_step_index", -1)),
            parent_entity_id=state.get("parent_entity_id"),
            current_entity_id=state.get("current_entity_id"),
        ),
    )


def get_current_stage(stage_session_id: str | None = None, *, engine: Any = None) -> str | None:
    """Return the last completed official stage for a workflow scope."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        return _state_for_scope_locked(scope_id, engine).get("current_stage")


def get_parent_entity_id(stage_session_id: str | None = None, *, engine: Any = None) -> str | None:
    """Return the parent skill entity for the scoped chain."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        return _state_for_scope_locked(scope_id, engine).get("parent_entity_id")


def set_current_stage(
    stage: str | None,
    *,
    stage_session_id: str | None = None,
    parent_entity_id: str | None = None,
    engine: Any = None,
    route_id: str | None = None,
    current_step_index: int | None = None,
) -> None:
    """Record the last completed stage for a scoped official workflow."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    normalized_stage = normalize_stage_name(stage) if stage else None
    with _skill_state_lock:
        state = _state_for_scope_locked(scope_id, engine)
        state["current_stage"] = normalized_stage
        if parent_entity_id is not None:
            state["parent_entity_id"] = parent_entity_id
        if route_id is not None:
            state["route_id"] = str(route_id)
        if current_step_index is not None:
            state["current_step_index"] = int(current_step_index)
        if scope_id == _DEFAULT_STAGE_SESSION_ID:
            _sync_default_globals_locked(state)
        _persist_stage_state(scope_id, state, engine)


def get_current_entity_id(stage_session_id: str | None = None, *, engine: Any = None) -> str | None:
    """Return the active session entity_id for a scoped hook-created session.

    Used by SkillEngine to skip duplicate skill_session_start when hook already created one.
    """
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        return _state_for_scope_locked(scope_id, engine).get("current_entity_id")


def get_stage_chain_state(
    stage_session_id: str | None = None, *, engine: Any = None
) -> dict[str, Any]:
    """Return a copy of scoped chain state for diagnostics."""
    scope_id = _normalize_stage_session_id(stage_session_id)
    with _skill_state_lock:
        state = _state_for_scope_locked(scope_id, engine)
        return {
            "stage_session_id": scope_id,
            "current_skill": state.get("current_skill"),
            "parent_entity_id": state.get("parent_entity_id"),
            "current_stage": state.get("current_stage"),
            "current_entity_id": state.get("current_entity_id"),
            "route_id": state.get("route_id") or "",
            "current_step_index": int(state.get("current_step_index", -1)),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_entity_id(skill_name: str, idempotency_key: str = "") -> str:
    """Generate a unique entity_id for a skill session.

    Format: skill:<skill_name>:<ISO timestamp with microseconds>
    """
    if idempotency_key:
        digest = hashlib.sha256(f"{skill_name}\x1f{idempotency_key}".encode()).hexdigest()[:32]
        return f"skill:{skill_name}:receipt-{digest}"
    ts = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
    return f"skill:{skill_name}:{ts}"


def _skill_start_memory_id(entity_id: str) -> str:
    """Return the deterministic memory id used for a skill start record."""
    return "skill_start_" + entity_id.replace(":", "_")


def _parse_skill_from_entity_id(entity_id: str) -> str | None:
    """Extract skill_name from an id such as ``skill:tdd:2026-...``."""
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
    composite = COMPOSITE_SKILL_CALLS.get(parent_skill)
    if composite is not None and skill_name in {
        *composite["required"],
        *composite["optional"],
    }:
        return None
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

        domain = TRACKABLE_SKILL_DOMAIN_MAP.get(skill_name, "all")
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
    *,
    project_id: str = "",
) -> str:
    """Persist the skill session start as a lightweight memory record.

    Skill startup is on the critical path for session-init and sp-stage.  It
    must not enter the full memory_store quality pipeline because that can
    synchronously invoke extraction, embedding, LanceDB, and reranking work.
    """
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
    _, _, entity_timestamp = entity_id.partition(f"skill:{skill_name}:")
    stable_timestamp = entity_timestamp or "1970-01-01T00:00:00"
    record = {
        "id": memory_id,
        "content": content,
        "memory_type": "experience",
        "source": "skill_session",
        "entity_ids": [entity_id],
        "tags": tags,
        "domain": domain,
        "tier": "L1",
        "category": "skill_session",
        "created_at": stable_timestamp,
        "last_accessed": stable_timestamp,
    }
    if project_id:
        record.update(
            {
                "project_id": project_id,
                "project_policy": "balanced",
                "visibility": "project",
            }
        )
    created_id = await asyncio.to_thread(
        engine.create_ordinary_if_absent,
        record,
    )
    if not isinstance(created_id, str):
        raise TypeError("skill_start_memory_id_invalid")
    created_id = created_id.strip()
    if not created_id:
        raise ValueError("skill_start_memory_id_invalid")
    if created_id != memory_id:
        raise RuntimeError("skill_start_memory_id_mismatch")
    return created_id


def _inject_skill_entity(
    engine: Any,
    entity_id: str,
    skill_name: str,
    task_description: str,
    parent_entity_id: str | None,
    *,
    tracking_persistence: str = "memory",
    tracking_basis: str = "runtime",
    project_id: str = "",
    stage_session_id: str = "",
    flow_line_id: str = "",
) -> dict:
    """Register skill_session entity in the context graph.

    Directly calls engine.register_entity() (sync, no lazy import needed).
    Additionally creates a parent_of edge when parent_entity_id is provided,
    so skill_session_trace can reconstruct the execution chain.
    """
    related = [parent_entity_id] if parent_entity_id else []
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    branch = _get_current_branch()
    tags = ["task:active", f"skill:{skill_name}"]
    if branch:
        tags.append(f"branch:{branch}")
    try:
        result = engine.register_entity(
            entity_type="skill_session",
            entity_id=entity_id,
            entity_name=skill_name,
            entity_description=task_description,
            related_entities=related,
            metadata={
                "lifecycle_status": "active",
                "tracking_persistence": tracking_persistence,
                "tracking_basis": tracking_basis,
                "project_id": project_id,
                "stage_session_id": stage_session_id,
                "flow_line_id": flow_line_id,
                "started_at": started_at,
                "last_accessed": started_at,
                "completed_at": "",
                "duration_ms": None,
                "outcome": "",
                "renewal_count": 0,
                "overdue": False,
                "tags": tags,
            },
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


def _skill_entity_node(engine: Any, entity_id: str) -> dict[str, Any] | None:
    getter = getattr(engine, "get_graph_node", None)
    if not callable(getter):
        return None
    try:
        node = getter(f"skill_session:{entity_id}")
    except Exception:
        return None
    if not isinstance(node, dict) or node.get("type") != "skill_session":
        return None
    return node


def _update_skill_entity_lifecycle(
    engine: Any,
    entity_id: str,
    *,
    status: str,
    outcome: str = "",
    duration_ms: int | None = None,
    renewal_count: int | None = None,
    overdue: bool | None = None,
) -> dict[str, Any] | None:
    node = _skill_entity_node(engine, entity_id)
    if node is None:
        return None
    metadata = dict(node.get("metadata") or {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tags = [str(tag) for tag in metadata.get("tags") or []]
    tags = [tag for tag in tags if tag not in {"task:active", "task:done", "task:abandoned"}]
    tags.append(
        "task:done"
        if status == "done"
        else "task:abandoned"
        if status == "abandoned"
        else "task:active"
    )
    metadata.update(
        {
            "lifecycle_status": status,
            "last_accessed": now,
            "completed_at": now if status in {"done", "abandoned"} else "",
            "duration_ms": duration_ms,
            "outcome": outcome,
            "tags": tags,
        }
    )
    if renewal_count is not None:
        metadata["renewal_count"] = int(renewal_count)
    if overdue is not None:
        metadata["overdue"] = bool(overdue)
    try:
        engine.register_entity(
            entity_type="skill_session",
            entity_id=entity_id,
            entity_name=str(
                node.get("name") or _parse_skill_from_entity_id(entity_id) or "unknown"
            ),
            entity_description=str(node.get("description") or ""),
            metadata=metadata,
            source_kind=str(node.get("source_kind") or ""),
        )
    except Exception:
        return None
    return metadata


def _complete_entity_only_session(
    engine: Any,
    entity_id: str,
    skill_name: str,
    outcome: Any,
) -> list[TextContent]:
    node = _skill_entity_node(engine, entity_id)
    if node is None:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": f"No skill session found for entity_id '{entity_id}'",
                        "tool": "skill_session_complete",
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    metadata = dict(node.get("metadata") or {})
    started_at = str(metadata.get("started_at") or "")
    duration_ms = None
    if started_at:
        try:
            started = datetime.datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=datetime.timezone.utc)
            duration_ms = max(
                0,
                int(
                    (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000
                ),
            )
        except (TypeError, ValueError):
            duration_ms = None

    if outcome == "still_in_progress":
        renewal_count = int(metadata.get("renewal_count") or 0) + 1
        overdue = renewal_count > MAX_STILL_IN_PROGRESS_RENEWALS
        updated = _update_skill_entity_lifecycle(
            engine,
            entity_id,
            status="active",
            outcome="still_in_progress",
            renewal_count=renewal_count,
            overdue=overdue,
        )
        payload = {
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": "still_active",
            "next_skills": [],
            "worth_update": None,
            "memory_id": "",
            "tracking_persistence": "entity_only",
            "renewal_count": renewal_count,
            "overdue": overdue,
        }
    else:
        abandoned = isinstance(outcome, str) and outcome.startswith("abandoned:")
        status = "abandoned" if abandoned else "done"
        normalized_outcome = (
            outcome[len("abandoned:") :].strip() if abandoned else str(outcome or "")
        )
        updated = _update_skill_entity_lifecycle(
            engine,
            entity_id,
            status=status,
            outcome=normalized_outcome,
            duration_ms=duration_ms,
        )
        payload = {
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": status,
            "duration_ms": duration_ms,
            "next_skills": []
            if abandoned
            else SKILL_CHAIN_MAP.get(skill_name, {}).get("successors", []),
            "worth_update": None,
            "memory_id": "",
            "tracking_persistence": "entity_only",
        }
        if abandoned:
            payload["reason"] = normalized_outcome
    if updated is None:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": "skill_session_entity_update_failed", "entity_id": entity_id},
                    ensure_ascii=False,
                ),
            )
        ]
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


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

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    def _graph_nodes() -> list[dict]:
        try:
            nodes = engine.list_graph_nodes()
            if isinstance(nodes, list):
                return nodes
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: public graph node admission failed: %s",
                e,
            )
        return []

    def _graph_edges() -> list[dict]:
        try:
            edges = engine.list_graph_edges()
            if isinstance(edges, list):
                return edges
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: public graph edge admission failed: %s",
                e,
            )
        return []

    def _iter_memories() -> list[Any]:
        try:
            return list(engine.iter_memories())
        except Exception as e:
            logging.getLogger("plastic-promise").warning(
                "skill_session_trace: public memory admission failed: %s", e
            )
        return []

    # Snapshot public graph/memory data once per trace.  Each public graph
    # accessor deep-copies the complete gated graph, so calling it inside the
    # session/edge loops turns a linear scan into an accidental O(N^2) walk.
    graph_nodes = _graph_nodes()
    graph_edges = _graph_edges()
    memories = _iter_memories()

    memory_by_entity_id: dict[str, dict[str, Any]] = {}
    for mem in memories:
        if isinstance(mem, dict):
            mem_dict = mem
        else:
            mem_dict = {key: getattr(mem, key, None) for key in dir(mem) if not key.startswith("_")}
        entity_ids = mem_dict.get("entity_ids", [])
        if not isinstance(entity_ids, list):
            continue
        for entity_id in entity_ids:
            if isinstance(entity_id, str):
                memory_by_entity_id.setdefault(entity_id, mem_dict)

    children_by_parent: dict[str, list[str]] = {}
    parent_by_child: dict[str, str] = {}
    for edge in graph_edges:
        if not isinstance(edge, dict) or edge.get("relation") != "parent_of":
            continue
        parent_id = edge.get("from", "")
        child_id = edge.get("to", "")
        if not isinstance(parent_id, str) or not parent_id.startswith("skill_session:"):
            continue
        if not isinstance(child_id, str) or not child_id.startswith("skill_session:"):
            continue
        raw_parent_id = parent_id[len("skill_session:") :]
        raw_child_id = child_id[len("skill_session:") :]
        children_by_parent.setdefault(raw_parent_id, []).append(raw_child_id)
        parent_by_child[raw_child_id] = raw_parent_id

    # -- Collect skill_session entities from graph nodes --------------------
    sessions: list[dict] = []

    for node in graph_nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id", "")
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
        memory = memory_by_entity_id.get(raw_entity_id)

        # -- Determine status from tags -------------------------------------
        node_metadata = dict(node.get("metadata") or {})
        tags: list[str] = list(memory.get("tags", [])) if memory else []
        if not tags:
            tags = [str(tag) for tag in node_metadata.get("tags") or []]
        metadata_status = str(node_metadata.get("lifecycle_status") or "")
        status: str = (
            metadata_status if metadata_status in {"active", "done", "abandoned"} else "active"
        )
        if not metadata_status:
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
        tracking_persistence = str(node_metadata.get("tracking_persistence") or "") or (
            "memory" if is_skill_start_memory else "entity_only"
        )
        outcome: str = str(node_metadata.get("outcome") or "")
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
        started_at: str = str(
            node_metadata.get("started_at") or (memory.get("created_at", "") if memory else "")
        )
        last_accessed: str = str(
            node_metadata.get("last_accessed")
            or (memory.get("last_accessed", "") if memory else "")
        )
        completed_at: str = str(node_metadata.get("completed_at") or "")
        raw_duration = node_metadata.get("duration_ms")
        duration_ms: int | None = (
            int(raw_duration) if isinstance(raw_duration, (int, float)) else None
        )

        # Extract duration from content if a [SKILL COMPLETE] marker exists
        if "[SKILL COMPLETE]" in content:
            import re as _re

            dur_match = _re.search(r"duration_ms=(\d+)", content)
            if dur_match:
                duration_ms = int(dur_match.group(1))

        # -- Child sessions via graph edges ---------------------------------
        child_skills = list(children_by_parent.get(raw_entity_id, []))

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
                "tracking_basis": str(node_metadata.get("tracking_basis") or "runtime"),
                "parent_skill": parent_by_child.get(raw_entity_id),
                "child_skills": child_skills,
            }
        )

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
            mem_for_session = memory_by_entity_id.get(s["entity_id"])

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


async def handle_skill_session_start(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict | None = None,
) -> list[TextContent]:
    """Create a skill_session entity and record the start of a skill execution.

    Internal steps:
    1. Validate skill_name against the official and native tracking registry
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
    flow_line_id = str(args.get("flow_line_id") or args.get("flow_id") or "").strip()
    project_id = str(args.get("project_id") or "").strip()
    parent_entity_id = args.get("parent_entity_id") or get_parent_entity_id(stage_session_id)
    record_memory = bool(args.get("record_memory", True))
    # A skill session may be tracked as an entity without a durable memory,
    # but durable memory always needs a concrete project scope.  This guard
    # lives in the handler as well as the MCP schema so internal callers and
    # older clients cannot bypass the project-isolation contract.
    project_scope_valid = bool(project_id) and project_id != "project:unknown"
    persistence_warning = ""
    if record_memory and not project_scope_valid:
        record_memory = False
        persistence_warning = "project_scope_required_for_memory_persistence"

    _normalized_name = (
        skill_name
        if str(skill_name).startswith("auto_inject:")
        else normalize_stage_name(str(skill_name))
    )

    # Validate skill_name (auto_inject:* is always allowed)
    if (
        not _normalized_name.startswith("auto_inject:")
        and _normalized_name not in TRACKABLE_SKILL_DOMAIN_MAP
    ):
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": (
                            f"Unknown skill_name '{_normalized_name}' (raw: '{skill_name}'). "
                            f"Known skills: {list(TRACKABLE_SKILL_DOMAIN_MAP.keys())}"
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
        domain = TRACKABLE_SKILL_DOMAIN_MAP.get(_normalized_name, "general")
    # Use the normalized name for entity_id (avoids colons breaking parse)
    # Store original name in the entity description for traceability
    idempotency_key = str(args.get("tracking_idempotency_key") or "").strip()
    tracking_basis = str(args.get("tracking_basis") or "runtime").strip().casefold()
    if tracking_basis not in {"runtime", "execution_receipt", "composite_receipt"}:
        tracking_basis = "runtime"
    entity_id = _make_entity_id(_normalized_name, idempotency_key)
    # Build description with original full name if different
    if _normalized_name != skill_name:
        task_description = f"[{skill_name}] {task_description}"

    # Parent chain validation (warning, not blocking)
    chain_warning = _validate_parent(_normalized_name, parent_entity_id, engine)

    # 1. Register entity in context graph
    entity_result = await asyncio.to_thread(
        _inject_skill_entity,
        engine,
        entity_id,
        _normalized_name,
        task_description,
        parent_entity_id,
        tracking_persistence="memory" if record_memory else "entity_only",
        tracking_basis=tracking_basis,
        project_id=project_id,
        stage_session_id=str(stage_session_id or "").strip(),
        flow_line_id=flow_line_id,
    )
    if entity_result.get("error"):
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "skill_session_entity_start_failed",
                        "reason": str(entity_result["error"]),
                        "tool": "skill_session_start",
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    # 2. Persist as memory record unless the caller explicitly requests
    # entity-only tracking for lightweight bootstrap paths.
    memory_id = ""
    if record_memory:
        memory_id = await _store_skill_start(
            engine,
            entity_id,
            _normalized_name,
            task_description,
            domain,
            project_id=project_id,
        )

    tags_applied = ["task:active", f"skill:{_normalized_name}", f"domain:{domain}"]

    response = {
        "entity_id": entity_id,
        "skill_name": _normalized_name,
        "status": "active",
        "domain": domain,
        "activated_principles": [],  # handled by atoms, not duplicated here
        "related_memories": [],  # callers use explicit memory_recall/context_supply
        "tracking_persistence": "memory" if record_memory else "entity_only",
        "tracking_basis": tracking_basis,
        "stage_session_id": _normalize_stage_session_id(stage_session_id),
        "flow_line_id": flow_line_id,
        "project_id": project_id or "project:unknown",
        "tags_applied": tags_applied,
        "chain_warning": chain_warning,
        "memory_id": memory_id,
    }
    if persistence_warning:
        response["persistence_warning"] = persistence_warning

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


async def record_attested_composite_skills(
    engine: Any,
    *,
    parent_entity_id: str,
    skill_names: list[str],
    task_description: str,
    receipt_id: str,
    project_id: str = "",
    stage_session_id: str = "",
    flow_line_id: str = "",
) -> list[str]:
    """Persist caller-attested composite child calls without moving the route cursor."""
    recorded: list[str] = []
    parent = parent_entity_id
    for index, skill_name in enumerate(skill_names):
        started = await handle_skill_session_start(
            engine,
            {
                "skill_name": skill_name,
                "task_description": task_description,
                "parent_entity_id": parent,
                "record_memory": False,
                "tracking_basis": "composite_receipt",
                "tracking_idempotency_key": f"{receipt_id}:child:{index}:{skill_name}",
                "project_id": project_id,
                "stage_session_id": stage_session_id,
                "flow_line_id": flow_line_id,
            },
        )
        start_data = json.loads(started[0].text)
        if start_data.get("error"):
            raise RuntimeError(f"composite_skill_start_failed:{skill_name}:{start_data['error']}")
        entity_id = str(start_data.get("entity_id") or "")
        if not entity_id:
            raise RuntimeError(f"composite_skill_start_missing_entity:{skill_name}")
        completed = await handle_skill_session_complete(
            engine,
            {
                "entity_id": entity_id,
                "outcome": "attested by the parent composite execution receipt",
            },
        )
        completion_data = json.loads(completed[0].text)
        if completion_data.get("error"):
            raise RuntimeError(
                f"composite_skill_completion_failed:{skill_name}:{completion_data['error']}"
            )
        recorded.append(entity_id)
        parent = entity_id
    return recorded


# ---------------------------------------------------------------------------
# skill_session_complete
# ---------------------------------------------------------------------------


async def handle_skill_session_complete(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict | None = None,
) -> list[TextContent]:
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
    skill_name = _parse_skill_from_entity_id(entity_id) or "unknown"

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
        if not isinstance(mem_data, dict) or "[SKILL START]" not in mem_data.get("content", ""):
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
                    mem_data = {k: getattr(mem, k, None) for k in dir(mem) if not k.startswith("_")}
                break

    if not memory_id:
        return _complete_entity_only_session(engine, entity_id, skill_name, outcome)

    created_at = mem_data.get("created_at", "")

    def _mutation_value(result: Any, field: str, default: Any = None) -> Any:
        if isinstance(result, dict):
            return result.get(field, default)
        return getattr(result, field, default)

    def _mutation_failed(
        reason: str,
        committed_result: Any | None = None,
    ) -> list[TextContent]:
        payload: dict[str, Any] = {
            "updated": False,
            "entity_id": entity_id,
            "skill_name": skill_name,
            "memory_id": memory_id,
            "reason": reason,
            "tool": "skill_session_complete",
        }
        if committed_result is not None:
            payload.update(
                {
                    "committed": True,
                    "partial": True,
                    "operation": str(_mutation_value(committed_result, "operation", "corrected")),
                    "stale_dependents": list(
                        _mutation_value(
                            committed_result,
                            "stale_synthesis_ids",
                            (),
                        )
                    ),
                    "ordinary_index_job_id": str(
                        _mutation_value(
                            committed_result,
                            "ordinary_index_job_id",
                            "",
                        )
                        or ""
                    ),
                    "synthesis_index_job_ids": list(
                        _mutation_value(
                            committed_result,
                            "synthesis_index_job_ids",
                            (),
                        )
                    ),
                }
            )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
            )
        ]

    def _mutate_content(
        new_content: str,
        transition: str,
    ) -> tuple[Any | None, list[TextContent] | None]:
        mutate = getattr(engine, "mutate_ordinary_source", None)
        if not callable(mutate):
            return None, _mutation_failed("ordinary_content_coordinator_unavailable")
        try:
            expected_project_id = str(mem_data.get("project_id") or "").strip()
            expected_tags = mem_data.get("tags")
            mutation_preconditions: dict[str, Any] = {
                "expected_content_hash": synthesis_content_hash(mem_data.get("content", "")),
                "require_source_available": True,
            }
            if expected_project_id:
                mutation_preconditions["expected_project_id"] = expected_project_id
            if isinstance(expected_tags, (list, tuple)):
                mutation_preconditions["expected_source_snapshot"] = {"tags": list(expected_tags)}
            result = mutate(
                memory_id,
                operation="replace_content",
                content=new_content,
                reason=f"skill_session:{transition}",
                actor="skill_tracking",
                call_id=(f"internal:skill_tracking:{transition}:{uuid.uuid4().hex}"),
                **mutation_preconditions,
            )
        except Exception as exc:
            return None, _mutation_failed(str(exc).strip() or "ordinary_content_mutation_failed")
        if result is None or result is False:
            return None, _mutation_failed("ordinary_content_mutation_failed")
        return result, None

    def _metadata_update_failed(
        committed_result: Any | None = None,
    ) -> list[TextContent]:
        return _mutation_failed(
            "ordinary_metadata_update_failed",
            committed_result,
        )

    def _patch_metadata(
        replacements: dict[str, Any],
        *,
        committed_result: Any | None,
        expected_content: str,
    ) -> bool:
        patch = getattr(engine, "patch_ordinary_memory", None)
        if not callable(patch):
            return False
        expected_project_id = str(mem_data.get("project_id") or "").strip()
        expected_tags = mem_data.get("tags")
        if not expected_project_id or not isinstance(expected_tags, (list, tuple)):
            return False
        expected_content_hash = str(
            _mutation_value(committed_result, "current_content_hash", "") or ""
        ).strip()
        if not expected_content_hash:
            expected_content_hash = synthesis_content_hash(expected_content)
        try:
            updated = patch(
                memory_id,
                replacements=replacements,
                expected_project_id=expected_project_id,
                expected_content_hash=expected_content_hash,
                expected_tags=list(expected_tags),
                require_source_available=True,
            )
        except Exception:
            return False
        return bool(updated)

    if outcome and outcome.startswith("abandoned:"):
        reason = outcome[len("abandoned:") :].strip()
        tags: list[str] = list(mem_data.get("tags", []))
        if "task:active" in tags:
            tags.remove("task:active")
        if "task:abandoned" not in tags:
            tags.append("task:abandoned")
        new_content = mem_data.get("content", "") + f"\n[SKILL ABANDONED] {reason}"
        mutation, failure = _mutate_content(new_content, "abandoned")
        if failure is not None:
            return failure
        if not _patch_metadata(
            {"tags": tags},
            committed_result=mutation,
            expected_content=new_content,
        ):
            return _metadata_update_failed(mutation)
        if (
            _skill_entity_node(engine, entity_id) is not None
            and _update_skill_entity_lifecycle(
                engine, entity_id, status="abandoned", outcome=reason
            )
            is None
        ):
            return _mutation_failed("skill_session_entity_update_failed", mutation)
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

    if outcome == "still_in_progress":
        current_content = mem_data.get("content", "")
        renewal_count = current_content.count("[still_in_progress]")
        new_content = current_content + "\n[still_in_progress]"
        tags = list(mem_data.get("tags", []))
        overdue = renewal_count >= MAX_STILL_IN_PROGRESS_RENEWALS
        if overdue and "task:overdue" not in tags:
            tags.append("task:overdue")
        mutation, failure = _mutate_content(new_content, "still_in_progress")
        if failure is not None:
            return failure
        if not _patch_metadata(
            {
                "tags": tags,
                "last_accessed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            committed_result=mutation,
            expected_content=new_content,
        ):
            return _metadata_update_failed(mutation)
        if (
            _skill_entity_node(engine, entity_id) is not None
            and _update_skill_entity_lifecycle(
                engine,
                entity_id,
                status="active",
                outcome="still_in_progress",
                renewal_count=renewal_count + 1,
                overdue=overdue,
            )
            is None
        ):
            return _mutation_failed("skill_session_entity_update_failed", mutation)
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

    current_content = mem_data.get("content", "")
    mutation = None
    if "[SKILL COMPLETE]" not in current_content:
        new_content = current_content + f"\n[SKILL COMPLETE] duration_ms={duration_ms}"
        mutation, failure = _mutate_content(new_content, "complete")
        if failure is not None:
            return failure

    # -- tag transition --
    tags: list[str] = list(mem_data.get("tags", []))
    if "task:active" in tags:
        tags.remove("task:active")
    if "task:done" not in tags:
        tags.append("task:done")
    if not _patch_metadata(
        {"tags": tags},
        committed_result=mutation,
        expected_content=(new_content if mutation is not None else current_content),
    ):
        return _metadata_update_failed(mutation)
    if (
        _skill_entity_node(engine, entity_id) is not None
        and _update_skill_entity_lifecycle(
            engine, entity_id, status="done", duration_ms=duration_ms
        )
        is None
    ):
        return _mutation_failed("skill_session_entity_update_failed", mutation)

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
            _runtime_context={
                "actor": "skill_tracking",
                "call_id": f"internal:skill_tracking:feedback:{uuid.uuid4().hex}",
                "project_id": str(mem_data.get("project_id") or "").strip(),
                "trust_score": 1.0,
                "defense_decision": "allow",
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
            from plastic_promise.core.memory_proposals import trusted_memory_origin
            from plastic_promise.mcp.tools.memory import handle_memory_store

            for art_path in artifacts:
                try:
                    with trusted_memory_origin("skill_session_complete"):
                        art_result = await handle_memory_store(
                            engine,
                            {
                                "content": (f"[SKILL ARTIFACT] {skill_name}: {art_path}"),
                                "memory_type": "code",
                                "source": "skill_session",
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


async def handle_skill_session_audit(
    engine: Any,
    args: dict,
    *,
    _runtime_context: dict | None = None,
) -> list[TextContent]:
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
    """Track an externally executed Skill without advancing its governed route.

    A matching ``sp-stage`` execution receipt is the only operation allowed to
    advance the official workflow cursor. This compatibility endpoint records
    lifecycle identity only.

    **Lightweight design**: Creates only the entity marker without doing
    the full skill_session_start pipeline (no memory_recall, no memory_store).
    The heavy work is deferred to the SkillEngine atoms that run after hooks.

    Args:
        engine: ContextEngine instance.
        args: {"phase": "start"|"complete", "skill_name": str}

    Returns:
        list[TextContent]: tracking status
    """
    phase = args.get("phase", "start")
    skill_name = args.get("skill_name", "")
    lookup_name = normalize_stage_name(skill_name)
    if lookup_name not in SKILL_DOMAIN_MAP:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "unknown_skill",
                        "skill_name": skill_name,
                        "known_skills": sorted(SKILL_DOMAIN_MAP),
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    stage_session_id = args.get("stage_session_id") or args.get("stage_id")
    flow_line_id = args.get("flow_line_id") or args.get("flow_id")
    scope_id = compose_flow_scope(stage_session_id, flow_line_id, args.get("project_id"))

    if phase == "start":
        with _skill_state_lock:
            state = _state_for_scope_locked(scope_id, engine)
            parent_entity_id = state.get("parent_entity_id")
        entity_id = _make_entity_id(lookup_name)

        try:
            entity_result = _inject_skill_entity(
                engine,
                entity_id,
                lookup_name,
                f"auto-tracked: {lookup_name}",
                parent_entity_id,
                tracking_persistence="entity_only",
                project_id=str(args.get("project_id") or "").strip(),
                stage_session_id=str(stage_session_id or "").strip(),
                flow_line_id=str(flow_line_id or "").strip(),
            )
        except Exception as exc:
            entity_result = {"error": str(exc)}
        if entity_result.get("error"):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "skill_tracking_entity_start_failed",
                            "reason": str(entity_result["error"]),
                            "skill_name": lookup_name,
                            "stage_session_id": scope_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        with contextlib.suppress(Exception):
            await _activate_skill_principles(engine, lookup_name, f"auto-tracked: {lookup_name}")

        with _skill_state_lock:
            state = _state_for_scope_locked(scope_id, engine)
            state["current_skill"] = entity_id
            state["current_entity_id"] = entity_id
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                _sync_default_globals_locked(state)
            _persist_stage_state(scope_id, state, engine)
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
            state = _state_for_scope_locked(scope_id, engine)
            eid = state.get("current_skill") or state.get("current_entity_id")
        if eid and _parse_skill_from_entity_id(eid) != lookup_name:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "active_skill_mismatch",
                            "skill_name": lookup_name,
                            "active_entity_id": eid,
                            "stage_session_id": scope_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        if eid:
            try:
                completion = await handle_skill_session_complete(
                    engine,
                    {
                        "entity_id": eid,
                        "outcome": "auto-tracked",
                        "artifacts": [],
                    },
                )
                completion_payload = json.loads(completion[0].text)
            except Exception as exc:
                completion_payload = {"error": str(exc) or "skill_tracking_completion_failed"}
            completion_error = str(completion_payload.get("error") or "")
            if not completion_error and completion_payload.get("updated") is False:
                completion_error = str(
                    completion_payload.get("reason") or "skill_tracking_completion_failed"
                )
            if completion_error:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "skill_tracking_completion_failed",
                                "reason": completion_error,
                                "skill_name": lookup_name,
                                "active_entity_id": eid,
                                "stage_session_id": scope_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
        with _skill_state_lock:
            state = _state_for_scope_locked(scope_id, engine)
            if eid:
                state["parent_entity_id"] = eid
            state["current_skill"] = None
            state["current_entity_id"] = None
            if scope_id == _DEFAULT_STAGE_SESSION_ID:
                _sync_default_globals_locked(state)
            _persist_stage_state(scope_id, state, engine)
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
