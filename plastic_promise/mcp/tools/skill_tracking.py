"""MCP Skill Tracking tools -- SuperPowers flow traceability

Public tools:
- skill_session_start     : Create a skill execution instance entity
- skill_session_complete  : Mark skill done, tag transition + worth update
- skill_session_trace     : Query execution chain, detect completeness
- skill_session_audit     : Post-hoc gap scan, auto-remediate
"""

import json
import datetime
from typing import Any

from mcp.types import TextContent

from plastic_promise.core.constants import (
    SKILL_CHAIN_MAP,
    SKILL_DOMAIN_MAP,
    DOMAIN_TO_TASK_TYPE,
    ORPHAN_THRESHOLD_MINUTES,
    MAX_STILL_IN_PROGRESS_RENEWALS,
    SKILL_COMPLETE_WORTH_DELTA,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_entity_id(skill_name: str) -> str:
    """Generate a unique entity_id for a skill session.

    Format: skill:<skill_name>:<ISO timestamp with microseconds>
    """
    ts = datetime.datetime.utcnow().isoformat()
    return f"skill:{skill_name}:{ts}"


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
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _validate_parent(
    skill_name: str, parent_entity_id: str | None, engine: Any
) -> str | None:
    """Check parent is a legal predecessor. Returns warning string or None.

    Never blocks -- always returns None (allowing creation) plus an optional
    warning string that the caller surfaces in chain_warning.
    """
    if not parent_entity_id:
        return None
    parent_skill = _parse_skill_from_entity_id(parent_entity_id)
    if not parent_skill:
        return (
            f"Parent entity_id '{parent_entity_id}' does not parse "
            f"as a skill_session"
        )
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
        result = await handle_principle_activate(engine, {
            "task_type": task_type,
            "task_description": task_description,
            "domain_hint": domain,
        })
        data = json.loads(result[0].text)
        return data.get("activated", [])
    except Exception:
        return []


async def _recall_skill_memories(
    engine: Any, task_description: str
) -> list[str]:
    """Internally recall relevant memories for the skill.

    Uses a lazy import of handle_memory_recall, matching server.py pattern.
    """
    try:
        from plastic_promise.mcp.tools.memory import handle_memory_recall
        result = await handle_memory_recall(engine, {
            "query": task_description,
            "max_results": 10,
        })
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
    """Persist the skill session start as a memory record.

    Uses lazy import of handle_memory_store, matching server.py pattern.
    Adds branch tag when inside a git repository.
    """
    try:
        from plastic_promise.mcp.tools.memory import handle_memory_store
        content = f"[SKILL START] {skill_name}: {task_description}"
        branch = _get_current_branch()
        tags = [
            "task:active",
            f"skill:{skill_name}",
            f"domain:{domain}",
        ]
        if branch:
            tags.append(f"branch:{branch}")
        result = await handle_memory_store(engine, {
            "content": content,
            "memory_type": "experience",
            "source": "superpowers",
            "entity_ids": [entity_id],
            "tags": tags,
        })
        data = json.loads(result[0].text)
        return data.get("memory_id", "?")
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
    """
    related = [parent_entity_id] if parent_entity_id else []
    try:
        return engine.register_entity(
            entity_type="skill_session",
            entity_id=entity_id,
            entity_name=skill_name,
            entity_description=task_description,
            related_entities=related,
        )
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# skill_session_trace
# ---------------------------------------------------------------------------

async def handle_skill_session_trace(
    engine: Any, args: dict
) -> list[TextContent]:
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
    skill_filter: str | None = args.get("skill_name", None)
    status_filter: str | None = args.get("status", None)

    # -- Resolve branch name for session_scope "branch" ----------------------
    current_branch: str = ""
    if session_scope == "branch":
        current_branch = _get_current_branch()
        if not current_branch:
            session_scope = "current"  # fallback when not in a git repo

    now = datetime.datetime.utcnow()

    # -- Collect skill_session entities from graph nodes --------------------
    sessions: list[dict] = []

    for node_id, node in engine._graph_nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("type") != "skill_session":
            continue

        # Strip the "skill_session:" prefix to get the raw entity_id
        raw_entity_id: str = node_id
        if raw_entity_id.startswith("skill_session:"):
            raw_entity_id = raw_entity_id[len("skill_session:"):]
        skill_name: str = node.get("name", "unknown")

        if skill_filter and skill_name != skill_filter:
            continue

        # -- Find associated memory record ----------------------------------
        memory: dict[str, Any] | None = None
        for mid, mem in engine._memories.items():
            # Normalize to dict (handle both dict and object memories)
            if isinstance(mem, dict):
                mem_dict = mem
            else:
                mem_dict = {
                    k: getattr(mem, k, None)
                    for k in dir(mem) if not k.startswith("_")
                }
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
        last_accessed: str = (
            memory.get("last_accessed", "") if memory else ""
        )
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
        for edge in engine._graph_edges:
            if not isinstance(edge, dict):
                continue
            # Edge goes FROM parent TO child with relation "parent_of"
            if (edge.get("from") == f"skill_session:{raw_entity_id}"
                    and edge.get("relation") == "parent_of"):
                child_id = edge.get("to", "")
                if isinstance(child_id, str) and child_id.startswith(
                    "skill_session:"
                ):
                    child_skills.append(
                        child_id[len("skill_session:"):]
                    )

        sessions.append({
            "entity_id": raw_entity_id,
            "skill_name": skill_name,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "last_accessed": last_accessed,
            "duration_ms": duration_ms,
            "description": node.get("description", ""),
            "outcome": outcome,
            "parent_skill": None,  # filled below via edge lookup
            "child_skills": child_skills,
        })

    # -- Build parent relationships from edges ------------------------------
    for edge in engine._graph_edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("relation") == "parent_of":
            child_full_id: str = edge.get("to", "")
            parent_full_id: str = edge.get("from", "")
            for s in sessions:
                if f"skill_session:{s['entity_id']}" == child_full_id:
                    if parent_full_id.startswith("skill_session:"):
                        s["parent_skill"] = parent_full_id[
                            len("skill_session:"):
                        ]

    # -- Gap detection ------------------------------------------------------
    gaps: list[dict] = []
    chain_warnings: list[dict] = []

    for s in sessions:
        # 1. orphan_active: active and last_accessed > threshold
        if s["status"] == "active" and s["last_accessed"]:
            try:
                la = datetime.datetime.fromisoformat(s["last_accessed"])
                idle_minutes = (now - la).total_seconds() / 60.0
                if idle_minutes > ORPHAN_THRESHOLD_MINUTES:
                    gaps.append({
                        "type": "orphan_active",
                        "entity_id": s["entity_id"],
                        "skill_name": s["skill_name"],
                        "idle_minutes": round(idle_minutes, 1),
                        "suggestion": (
                            "手動 skill_session_complete(entity_id, outcome)"
                        ),
                    })
            except (ValueError, TypeError):
                pass

        # 2. chain_broken: done but has successors in SKILL_CHAIN_MAP
        #    and no child sessions recorded
        if s["status"] == "done":
            expected_successors = SKILL_CHAIN_MAP.get(
                s["skill_name"], {}
            ).get("successors", [])
            if expected_successors and not s["child_skills"]:
                chain_warnings.append({
                    "type": "chain_broken",
                    "entity_id": s["entity_id"],
                    "skill_name": s["skill_name"],
                    "expected_next": expected_successors,
                })

        # 3. tag_mismatch: content marks completion but task:done tag missing
        if s["status"] == "done":
            # Re-check original memory for tag integrity
            mem_for_session = None
            for mid, mem in engine._memories.items():
                if isinstance(mem, dict):
                    m = mem
                else:
                    m = {
                        k: getattr(mem, k, None)
                        for k in dir(mem) if not k.startswith("_")
                    }
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
                    gaps.append({
                        "type": "tag_mismatch",
                        "entity_id": s["entity_id"],
                        "skill_name": s["skill_name"],
                        "detail": (
                            "Content has [SKILL COMPLETE] but "
                            "task:done tag is missing"
                        ),
                    })

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

    return [TextContent(type="text", text=json.dumps(
        response, ensure_ascii=False, indent=2,
    ))]


# ---------------------------------------------------------------------------
# skill_session_start
# ---------------------------------------------------------------------------

async def handle_skill_session_start(
    engine: Any, args: dict
) -> list[TextContent]:
    """Create a skill_session entity and record the start of a skill execution.

    Internal steps:
    1. Validate skill_name against SKILL_DOMAIN_MAP
    2. Derive domain and generate entity_id
    3. Parent chain validation (warning, never blocking)
    4. Register entity in context graph via engine.register_entity()
    5. Activate principles for this skill's domain
    6. Recall related memories
    7. Persist as memory record with tags

    Args:
        engine: ContextEngine instance.
        args:
            skill_name: str (required) -- Skill name
            task_description: str (required) -- What this execution does
            parent_entity_id: str | None -- Parent skill's entity_id
            estimated_duration_minutes: int | None -- Optional estimate

    Returns:
        list[TextContent]: MCP response with entity_id, domain, activated
        principles, related memories, tags, and chain_warning if applicable.
    """
    skill_name = args.get("skill_name", "")
    task_description = args.get("task_description", "")
    parent_entity_id = args.get("parent_entity_id", None)

    # Validate skill_name
    if skill_name not in SKILL_DOMAIN_MAP:
        return [TextContent(type="text", text=json.dumps({
            "error": (
                f"Unknown skill_name '{skill_name}'. "
                f"Known skills: {list(SKILL_DOMAIN_MAP.keys())}"
            ),
            "tool": "skill_session_start",
        }, ensure_ascii=False))]

    # Derive domain and entity_id
    domain = SKILL_DOMAIN_MAP[skill_name]
    entity_id = _make_entity_id(skill_name)

    # Parent chain validation (warning, not blocking)
    chain_warning = _validate_parent(skill_name, parent_entity_id, engine)

    # 1. Register entity in context graph
    entity_info = _inject_skill_entity(
        engine, entity_id, skill_name, task_description, parent_entity_id,
    )

    # 2. Activate principles for this skill's domain
    principles = await _activate_skill_principles(
        engine, skill_name, task_description,
    )

    # 3. Recall related memories
    related_memories = await _recall_skill_memories(engine, task_description)

    # 4. Persist as memory record
    memory_id = await _store_skill_start(
        engine, entity_id, skill_name, task_description, domain,
    )

    tags_applied = ["task:active", f"skill:{skill_name}", f"domain:{domain}"]

    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        "status": "active",
        "domain": domain,
        "activated_principles": principles,
        "related_memories": related_memories,
        "tags_applied": tags_applied,
        "chain_warning": chain_warning,
        "memory_id": memory_id,
    }

    return [TextContent(type="text", text=json.dumps(
        response, ensure_ascii=False, indent=2,
    ))]


# ---------------------------------------------------------------------------
# skill_session_complete
# ---------------------------------------------------------------------------

async def handle_skill_session_complete(
    engine: Any, args: dict
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
    outcome = args.get("outcome", None)
    artifacts = args.get("artifacts", [])

    # ------------------------------------------------------------------
    # Locate the existing skill-start memory
    # ------------------------------------------------------------------
    memory_id = None
    mem_data = None
    for mid, mem in engine._memories.items():
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
                    k: getattr(mem, k, None)
                    for k in dir(mem) if not k.startswith("_")
                }
            break

    if not memory_id:
        return [TextContent(type="text", text=json.dumps({
            "error": (
                f"No skill session memory found for entity_id '{entity_id}'"
            ),
            "tool": "skill_session_complete",
        }, ensure_ascii=False))]

    skill_name = _parse_skill_from_entity_id(entity_id) or "unknown"
    created_at = mem_data.get("created_at", "")

    # ------------------------------------------------------------------
    # Outcome: abandoned
    # ------------------------------------------------------------------
    if outcome and outcome.startswith("abandoned:"):
        reason = outcome[len("abandoned:"):].strip()
        tags: list[str] = list(mem_data.get("tags", []))

        if "task:active" in tags:
            tags.remove("task:active")
        if "task:abandoned" not in tags:
            tags.append("task:abandoned")

        new_content = (
            mem_data.get("content", "")
            + f"\n[SKILL ABANDONED] {reason}"
        )

        # Persist to engine._memories dict
        engine._memories[memory_id]["tags"] = tags
        engine._memories[memory_id]["content"] = new_content

        return [TextContent(type="text", text=json.dumps({
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": "abandoned",
            "reason": reason,
            "next_skills": [],
            "worth_update": None,
            "memory_id": memory_id,
        }, ensure_ascii=False, indent=2))]

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

        # Persist directly to engine._memories dict
        engine._memories[memory_id]["content"] = new_content
        engine._memories[memory_id]["tags"] = tags
        engine._memories[memory_id]["last_accessed"] = (
            datetime.datetime.utcnow().isoformat()
        )

        return [TextContent(type="text", text=json.dumps({
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": "still_active",
            "next_skills": [],
            "worth_update": None,
            "memory_id": memory_id,
            "renewal_count": renewal_count + 1,
            "overdue": overdue,
        }, ensure_ascii=False, indent=2))]

    # ------------------------------------------------------------------
    # Normal outcome -- transition to done
    # ------------------------------------------------------------------

    # -- duration --
    duration_ms = None
    if created_at:
        try:
            start_dt = datetime.datetime.fromisoformat(created_at)
            now = datetime.datetime.utcnow()
            # start_dt may be offset-aware or naive; strip tzinfo for safety
            if start_dt.tzinfo is not None:
                start_dt = start_dt.replace(tzinfo=None)
            duration_ms = int(
                (now - start_dt).total_seconds() * 1000
            )
        except Exception:
            duration_ms = None

    # -- tag transition --
    tags: list[str] = list(mem_data.get("tags", []))
    if "task:active" in tags:
        tags.remove("task:active")
    if "task:done" not in tags:
        tags.append("task:done")
    engine._memories[memory_id]["tags"] = tags

    # -- content update --
    new_content = (
        mem_data.get("content", "")
        + f"\n[SKILL COMPLETE] duration_ms={duration_ms}"
    )
    engine._memories[memory_id]["content"] = new_content

    # -- worth update via feedback_apply --
    worth_update = None
    try:
        from plastic_promise.mcp.tools.reflection import handle_feedback_apply
        fb_result = await handle_feedback_apply(engine, {
            "item_id": memory_id,
            "feedback_type": "adopted",
        })
        fb_data = json.loads(fb_result[0].text)
        worth_update = fb_data.get(
            "new_worth_score", SKILL_COMPLETE_WORTH_DELTA
        )
    except Exception:
        worth_update = SKILL_COMPLETE_WORTH_DELTA

    # -- chain successors --
    next_skills: list[str] = SKILL_CHAIN_MAP.get(
        skill_name, {}
    ).get("successors", [])

    # -- register artifacts --
    artifact_results: list[str] = []
    if artifacts:
        try:
            from plastic_promise.mcp.tools.memory import handle_memory_store
            for art_path in artifacts:
                try:
                    art_result = await handle_memory_store(engine, {
                        "content": (
                            f"[SKILL ARTIFACT] {skill_name}: {art_path}"
                        ),
                        "memory_type": "code",
                        "source": "superpowers",
                        "entity_ids": [entity_id],
                        "tags": ["task:artifact", f"skill:{skill_name}"],
                    })
                    art_data = json.loads(art_result[0].text)
                    artifact_results.append(
                        art_data.get("memory_id", "?")
                    )
                except Exception:
                    artifact_results.append("?")
        except ImportError:
            pass

    return [TextContent(type="text", text=json.dumps({
        "entity_id": entity_id,
        "skill_name": skill_name,
        "status": "done",
        "duration_ms": duration_ms,
        "next_skills": next_skills,
        "worth_update": worth_update,
        "memory_id": memory_id,
        "artifact_memory_ids": artifact_results,
    }, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# skill_session_audit
# ---------------------------------------------------------------------------

async def handle_skill_session_audit(
    engine: Any, args: dict
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
    skill_filter: str | None = args.get("skill_name", None)

    known_skill_names: set[str] = set(SKILL_DOMAIN_MAP.keys())

    # ------------------------------------------------------------------
    # 1. Scan existing skill_session entities from graph nodes
    # ------------------------------------------------------------------
    existing_sessions: dict[str, list[str]] = {}  # skill_name -> [entity_ids]
    for node_id, node in engine._graph_nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("type") != "skill_session":
            continue
        # Strip the "skill_session:" prefix to get the raw entity_id
        raw_entity_id: str = node_id
        if raw_entity_id.startswith("skill_session:"):
            raw_entity_id = raw_entity_id[len("skill_session:"):]
        skill_name: str = node.get("name", "unknown")
        if skill_name not in existing_sessions:
            existing_sessions[skill_name] = []
        existing_sessions[skill_name].append(raw_entity_id)

    scanned_sessions: int = sum(len(v) for v in existing_sessions.values())

    # ------------------------------------------------------------------
    # 2. Scan engine._memories for mentions of known skill names
    # ------------------------------------------------------------------
    mentioned_skills: set[str] = set()
    for mid, mem in engine._memories.items():
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
            gaps.append({
                "type": "missing_start",
                "skill_name": skill_name,
                "domain": SKILL_DOMAIN_MAP.get(skill_name, "unknown"),
            })

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
            for node_id, node in engine._graph_nodes.items():
                if not isinstance(node, dict):
                    continue
                if node.get("type") != "skill_session":
                    continue
                if node.get("name") == skill_name:
                    skill_has_any_session = True
                    break

            if skill_has_any_session:
                auto_fixed.append({
                    "skill_name": skill_name,
                    "status": "skipped",
                    "reason": "session_already_exists",
                })
                continue

            try:
                # Create session with [事后补录] description
                start_result = await handle_skill_session_start(engine, {
                    "skill_name": skill_name,
                    "task_description": f"[事后补录] {skill_name}",
                    "parent_entity_id": None,
                })
                start_data = json.loads(start_result[0].text)

                if "error" in start_data:
                    auto_fixed.append({
                        "skill_name": skill_name,
                        "status": "failed",
                        "reason": start_data["error"],
                    })
                    continue

                entity_id: str = start_data["entity_id"]

                # Immediately mark as done
                complete_result = await handle_skill_session_complete(engine, {
                    "entity_id": entity_id,
                })
                complete_data = json.loads(complete_result[0].text)

                auto_fixed.append({
                    "skill_name": skill_name,
                    "status": "fixed",
                    "entity_id": entity_id,
                    "memory_id": complete_data.get("memory_id", "?"),
                })
            except Exception as exc:
                auto_fixed.append({
                    "skill_name": skill_name,
                    "status": "failed",
                    "reason": str(exc),
                })

    response: dict[str, Any] = {
        "scanned_sessions": scanned_sessions,
        "gaps_found": gaps,
        "auto_fixed": auto_fixed,
    }

    return [TextContent(type="text", text=json.dumps(
        response, ensure_ascii=False, indent=2,
    ))]
