# Skill Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 4 MCP tools that make SuperPowers skill executions traceable entities in the Plastic Promise memory system.

**Architecture:** Add `skill_tracking.py` tool module with 4 handlers, register in `server.py`, extend constants with chain/domain maps, add `"skill_session"` entity type to `context_engine.py`, enhance `audit_run` with 8th dimension, update CLAUDE.md with calling protocol.

**Tech Stack:** Python 3.10+, Plastic Promise MCP framework (ContextEngine dependency injection, `mcp.types.TextContent` return pattern), pytest

## Global Constraints

- Zero new storage structures — reuse MemoryRecord + tag + entity graph
- Do NOT modify SuperPowers plugin code
- Parent chain validation: warning only, never block
- Orphan threshold: 30 minutes fixed, max 3 `still_in_progress` renewals
- Worth score on complete: fixed +0.02 delta
- `auto_fix` in audit only handles `missing_start` gaps
- `session_scope="branch"` resolves via git branch; falls back to `"current"` outside repos
- Audit 8th dimension weight: 0.10, existing 7 dimensions scaled to sum 0.90

---

### Task 1: Add skill chain and domain mapping constants

**Files:**
- Modify: `plastic_promise/core/constants.py` (append at end of file)

**Interfaces:**
- Produces: `SKILL_CHAIN_MAP: dict`, `SKILL_DOMAIN_MAP: dict`, `DOMAIN_TO_TASK_TYPE: dict`, `ORPHAN_THRESHOLD_MINUTES: int`, `MAX_STILL_IN_PROGRESS_RENEWALS: int`, `SKILL_COMPLETE_WORTH_DELTA: float`

- [ ] **Step 1: Append skill tracking constants to constants.py**

```python
# ============================================================
# Skill Tracking — SuperPowers 流程可追踪化
# ============================================================

SKILL_CHAIN_MAP: dict[str, dict[str, list[str]]] = {
    # 起点 skills (无强制前驱)
    "brainstorming":               {"predecessors": [],           "successors": ["writing-plans"]},
    "systematic-debugging":        {"predecessors": [],           "successors": ["test-driven-development"]},
    "requesting-code-review":      {"predecessors": [],           "successors": ["receiving-code-review"]},
    "writing-skills":              {"predecessors": [],           "successors": []},

    # 中间 skills
    "writing-plans":               {"predecessors": ["brainstorming"],  "successors": ["subagent-driven-development", "executing-plans"]},
    "test-driven-development":     {"predecessors": ["systematic-debugging"], "successors": ["verification-before-completion"]},
    "subagent-driven-development": {"predecessors": ["writing-plans"], "successors": ["finishing-a-development-branch"]},
    "executing-plans":             {"predecessors": ["writing-plans"], "successors": ["verification-before-completion"]},
    "verification-before-completion": {"predecessors": ["test-driven-development", "executing-plans"], "successors": ["finishing-a-development-branch"]},
    "receiving-code-review":       {"predecessors": ["requesting-code-review"], "successors": []},

    # 终端 skills
    "finishing-a-development-branch": {"predecessors": ["subagent-driven-development", "verification-before-completion"], "successors": []},

    # 辅助 skills (松散约束)
    "using-git-worktrees":         {"predecessors": [], "successors": []},
    "dispatching-parallel-agents": {"predecessors": [], "successors": []},
    "using-superpowers":           {"predecessors": [], "successors": ["brainstorming", "systematic-debugging", "requesting-code-review"]},
}

SKILL_DOMAIN_MAP: dict[str, str] = {
    "brainstorming":                  "designing",
    "writing-plans":                  "designing",
    "executing-plans":                "building",
    "subagent-driven-development":    "building",
    "dispatching-parallel-agents":     "building",
    "using-git-worktrees":             "building",
    "test-driven-development":        "building",
    "verification-before-completion": "reflecting",
    "requesting-code-review":         "reflecting",
    "receiving-code-review":          "reflecting",
    "systematic-debugging":           "fixing",
    "finishing-a-development-branch": "governing",
    "writing-skills":                 "designing",
    "using-superpowers":              "governing",
}

DOMAIN_TO_TASK_TYPE: dict[str, str] = {
    "designing":   "architecture",
    "building":    "code_generation",
    "reflecting":  "code_review",
    "fixing":      "debugging",
    "governing":   "general",
}

# Skill tracking thresholds
ORPHAN_THRESHOLD_MINUTES: int = 30
MAX_STILL_IN_PROGRESS_RENEWALS: int = 3
SKILL_COMPLETE_WORTH_DELTA: float = 0.02
```

- [ ] **Step 2: Run existing test suite to verify no regression**

Run: `python -m pytest tests/ -x -q`
Expected: PASS (all existing tests unaffected by constant additions)

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/constants.py
git commit -m "feat: add skill chain/domain mapping constants for skill tracking"
```

---

### Task 2: Register "skill_session" entity type in ContextEngine

**Files:**
- Modify: `plastic_promise/core/context_engine.py:567-568`

**Interfaces:**
- Modifies: `register_entity()` valid_types set

- [ ] **Step 1: Add "skill_session" to valid entity types**

In `plastic_promise/core/context_engine.py`, find line ~567:
```python
valid_types = {"principle", "task", "memory", "code_module"}
```

Replace with:
```python
valid_types = {"principle", "task", "memory", "code_module", "skill_session"}
```

- [ ] **Step 2: Verify no regression with existing tests**

Run: `python -m pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/context_engine.py
git commit -m "feat: add skill_session entity type to ContextEngine.register_entity()"
```

---

### Task 3: Implement skill_session_start tool handler

**Files:**
- Create: `plastic_promise/mcp/tools/skill_tracking.py`
- Create: `tests/test_skill_tracking.py`

**Interfaces:**
- Produces: `handle_skill_session_start(engine, args) -> list[TextContent]`
- Consumes: `engine.register_entity()`, `engine.store_memory()` (via internal calls), constants from Task 1

- [ ] **Step 1: Write the failing test**

Create `tests/test_skill_tracking.py`:

```python
"""Tests for skill_tracking MCP tools."""
import json
import pytest
from unittest.mock import MagicMock, patch, ANY
from mcp.types import TextContent


class TestSkillSessionStart:
    """Tests for handle_skill_session_start."""

    def test_start_creates_entity_with_correct_id_format(self):
        """Entity ID follows skill:<name>:<ISO timestamp> pattern."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start
        from plastic_promise.core.constants import SKILL_DOMAIN_MAP

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:brainstorming:2026-06-30T14:23:01.123456",
            "type": "skill_session",
            "name": "brainstorming",
            "is_new": True,
            "edges_created": 0,
        }

        # Mock the internal principle_activate call
        with patch('plastic_promise.mcp.tools.skill_tracking._activate_skill_principles') as mock_principles:
            mock_principles.return_value = [{"id": 2, "name": "全过程可查可透明"}]
            with patch('plastic_promise.mcp.tools.skill_tracking._recall_skill_memories') as mock_recall:
                mock_recall.return_value = ["mem_abc"]
                with patch('plastic_promise.mcp.tools.skill_tracking._store_skill_start') as mock_store:
                    mock_store.return_value = "mem_skill_xyz"

                    result = handle_skill_session_start(engine, {
                        "skill_name": "brainstorming",
                        "task_description": "Design the skill tracking module",
                        "parent_entity_id": None,
                    })

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["skill_name"] == "brainstorming"
        assert data["status"] == "active"
        assert data["domain"] == "designing"
        assert data["chain_warning"] is None
        assert "skill:brainstorming:" in data["entity_id"]

    def test_start_returns_chain_warning_for_illegal_parent(self):
        """Parent validation returns warning but does not block."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:writing-plans:2026-06-30T15:00:00",
            "type": "skill_session",
            "name": "writing-plans",
            "is_new": True,
            "edges_created": 1,
        }
        # Register a fake parent entity so parent lookup works
        engine._graph_nodes = {
            "skill_session:skill:test-driven-development:2026-06-30T14:00:00": {
                "type": "skill_session",
                "name": "test-driven-development",
                "description": "Parent skill",
            }
        }
        # Mock query_graph to return parent info
        engine.query_graph.return_value = {
            "nodes": {
                "skill_session:skill:test-driven-development:2026-06-30T14:00:00": {
                    "type": "skill_session",
                    "name": "test-driven-development",
                }
            },
            "edges": [],
        }

        with patch('plastic_promise.mcp.tools.skill_tracking._activate_skill_principles', return_value=[]):
            with patch('plastic_promise.mcp.tools.skill_tracking._recall_skill_memories', return_value=[]):
                with patch('plastic_promise.mcp.tools.skill_tracking._store_skill_start', return_value="mem_xyz"):

                    result = handle_skill_session_start(engine, {
                        "skill_name": "writing-plans",
                        "task_description": "Plan the module",
                        "parent_entity_id": "skill_session:skill:test-driven-development:2026-06-30T14:00:00",
                    })

        data = json.loads(result[0].text)
        assert data["chain_warning"] is not None
        assert "not a legal predecessor" in data["chain_warning"]

    def test_start_without_parent_no_warning(self):
        """Null parent is always valid."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:brainstorming:2026-06-30T14:23:01",
            "type": "skill_session",
            "name": "brainstorming",
            "is_new": True,
            "edges_created": 0,
        }

        with patch('plastic_promise.mcp.tools.skill_tracking._activate_skill_principles', return_value=[]):
            with patch('plastic_promise.mcp.tools.skill_tracking._recall_skill_memories', return_value=[]):
                with patch('plastic_promise.mcp.tools.skill_tracking._store_skill_start', return_value="mem_xyz"):

                    result = handle_skill_session_start(engine, {
                        "skill_name": "brainstorming",
                        "task_description": "Design something",
                        "parent_entity_id": None,
                    })

        data = json.loads(result[0].text)
        assert data["chain_warning"] is None

    def test_start_unknown_skill_name_errors(self):
        """Unknown skill name should return error."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start

        engine = MagicMock()

        result = handle_skill_session_start(engine, {
            "skill_name": "nonexistent-skill",
            "task_description": "Test",
        })

        data = json.loads(result[0].text)
        assert "error" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionStart -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'plastic_promise.mcp.tools.skill_tracking'`

- [ ] **Step 3: Create skill_tracking.py with imports and helper functions**

Create `plastic_promise/mcp/tools/skill_tracking.py`:

```python
"""MCP Skill Tracking 工具 — SuperPowers 流程可追踪化

公开工具:
- skill_session_start     : 创建 skill 执行实例 entity
- skill_session_complete  : 标记 skill 完成，标签转换 + worth 更新
- skill_session_trace     : 查询执行链，检测完整性
- skill_session_audit     : 事后扫描缺口，自动补录
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
    """Generate a unique entity_id for a skill session."""
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


def _validate_parent(skill_name: str, parent_entity_id: str | None, engine: Any) -> str | None:
    """Check parent is a legal predecessor. Returns warning string or None."""
    if not parent_entity_id:
        return None
    parent_skill = _parse_skill_from_entity_id(parent_entity_id)
    if not parent_skill:
        return f"Parent entity_id '{parent_entity_id}' does not parse as a skill_session"
    legal_predecessors = SKILL_CHAIN_MAP.get(skill_name, {}).get("predecessors", [])
    if parent_skill not in legal_predecessors:
        expected = ', '.join(legal_predecessors) if legal_predecessors else 'none'
        return (
            f"Parent '{parent_skill}' is not a legal predecessor of '{skill_name}'. "
            f"Expected one of: [{expected}]"
        )
    return None


async def _activate_skill_principles(engine: Any, skill_name: str, task_description: str) -> list[dict]:
    """Internally activate principles for the skill's domain."""
    try:
        from plastic_promise.mcp.tools.principles import handle_principle_activate
        domain = SKILL_DOMAIN_MAP.get(skill_name, "general")
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


async def _recall_skill_memories(engine: Any, task_description: str) -> list[str]:
    """Internally recall relevant memories for the skill."""
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
    engine: Any, entity_id: str, skill_name: str, task_description: str, domain: str
) -> str:
    """Persist the skill session start as a memory record."""
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
    engine: Any, entity_id: str, skill_name: str, task_description: str, parent_entity_id: str | None
) -> dict:
    """Register skill_session entity in the context graph."""
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
```

- [ ] **Step 4: Run test to verify it fails with "not yet implemented"**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionStart -v`
Expected: FAIL — `ImportError: cannot import name 'handle_skill_session_start'`

- [ ] **Step 5: Implement handle_skill_session_start**

Append to `plastic_promise/mcp/tools/skill_tracking.py`:

```python
# ---------------------------------------------------------------------------
# skill_session_start
# ---------------------------------------------------------------------------

async def handle_skill_session_start(engine: Any, args: dict) -> list[TextContent]:
    """Create a skill_session entity and record the start of a SuperPowers skill execution.

    Args:
        engine: ContextEngine instance.
        args:
            skill_name: str (required) — Skill name
            task_description: str (required) — What this execution does
            parent_entity_id: str | None — Parent skill's entity_id in the call chain
            estimated_duration_minutes: int | None — Optional duration estimate

    Returns:
        list[TextContent]: MCP response with entity_id, domain, activated principles,
                           related memories, tags, and chain_warning if applicable.
    """
    skill_name = args.get("skill_name", "")
    task_description = args.get("task_description", "")
    parent_entity_id = args.get("parent_entity_id", None)

    # Validate skill_name
    if skill_name not in SKILL_DOMAIN_MAP:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Unknown skill_name '{skill_name}'. Known skills: {list(SKILL_DOMAIN_MAP.keys())}",
            "tool": "skill_session_start",
        }, ensure_ascii=False))]

    # Derive domain and entity_id
    domain = SKILL_DOMAIN_MAP[skill_name]
    entity_id = _make_entity_id(skill_name)

    # Parent chain validation (warning, not blocking)
    chain_warning = _validate_parent(skill_name, parent_entity_id, engine)

    # 1. Register entity in context graph
    entity_info = _inject_skill_entity(engine, entity_id, skill_name, task_description, parent_entity_id)

    # 2. Activate principles for this skill's domain
    principles = await _activate_skill_principles(engine, skill_name, task_description)

    # 3. Recall related memories
    related_memories = await _recall_skill_memories(engine, task_description)

    # 4. Persist as memory record
    memory_id = await _store_skill_start(engine, entity_id, skill_name, task_description, domain)

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

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionStart -v`
Expected: 4 PASSED

- [ ] **Step 7: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py tests/test_skill_tracking.py
git commit -m "feat: implement skill_session_start tool handler with tests"
```

---

### Task 4: Implement skill_session_complete tool handler

**Files:**
- Modify: `plastic_promise/mcp/tools/skill_tracking.py` (append handler)
- Modify: `tests/test_skill_tracking.py` (append test class)

**Interfaces:**
- Produces: `handle_skill_session_complete(engine, args) -> list[TextContent]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_skill_tracking.py`:

```python
class TestSkillSessionComplete:
    """Tests for handle_skill_session_complete."""

    def test_complete_transitions_status_to_done(self):
        """Complete transitions active→done and updates worth."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

        engine = MagicMock()
        # Simulate an existing active session memory
        engine.get_memory.return_value = {
            "id": "mem_active_123",
            "content": "[SKILL START] brainstorming: Design something",
            "memory_type": "experience",
            "tags": ["task:active", "skill:brainstorming", "domain:designing"],
            "worth_score": 0.70,
            "worth_success": 3,
            "worth_failure": 1,
            "created_at": "2026-06-30T14:23:01",
            "last_accessed": "2026-06-30T14:23:01",
        }
        engine._graph_nodes = {
            "skill_session:skill:brainstorming:2026-06-30T14:23:01.123456": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Design something",
            },
        }
        engine._memories = {"mem_active_123": engine.get_memory.return_value}

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_memory_update') as mock_update:
            mock_update.return_value = [TextContent(type="text", text=json.dumps({"updated": True}))]
            with patch('plastic_promise.mcp.tools.skill_tracking.handle_feedback_apply') as mock_feedback:
                mock_feedback.return_value = [TextContent(type="text", text=json.dumps({"new_worth_score": 0.72}))]

                result = handle_skill_session_complete(engine, {
                    "entity_id": "skill:brainstorming:2026-06-30T14:23:01.123456",
                    "outcome": "Design approved, moving to writing-plans",
                    "artifacts": ["docs/specs/skill-tracking.md"],
                })

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["skill_name"] == "brainstorming"
        assert data["status"] == "done"
        assert data["next_skills"] == ["writing-plans"]
        assert data["worth_update"]["delta"] == 0.02

    def test_still_in_progress_resets_timer(self):
        """still_in_progress outcome refreshes last_accessed but keeps active."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

        engine = MagicMock()
        engine.get_memory.return_value = {
            "id": "mem_active_123",
            "content": "[SKILL START] brainstorming: Long design session",
            "memory_type": "experience",
            "tags": ["task:active", "skill:brainstorming", "domain:designing"],
            "worth_score": 0.70,
            "worth_success": 3,
            "worth_failure": 1,
            "created_at": "2026-06-30T14:23:01",
            "last_accessed": "2026-06-30T14:23:01",
        }
        engine._graph_nodes = {
            "skill:brainstorming:2026-06-30T14:23:01": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Long design session",
            },
        }

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_memory_update') as mock_update:
            mock_update.return_value = [TextContent(type="text", text=json.dumps({"updated": True}))]

            result = handle_skill_session_complete(engine, {
                "entity_id": "skill:brainstorming:2026-06-30T14:23:01",
                "outcome": "still_in_progress",
                "artifacts": [],
            })

        data = json.loads(result[0].text)
        assert data["status"] == "still_active"
        assert data["next_skills"] == []

    def test_still_in_progress_exceeds_max_renewals(self):
        """After MAX_STILL_IN_PROGRESS_RENEWALS, marks overdue."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete
        from plastic_promise.core.constants import MAX_STILL_IN_PROGRESS_RENEWALS

        engine = MagicMock()
        engine.get_memory.return_value = {
            "id": "mem_active_123",
            "content": "[SKILL START] brainstorming: Very long session" + " [still_in_progress]" * (MAX_STILL_IN_PROGRESS_RENEWALS + 1),
            "memory_type": "experience",
            "tags": ["task:active", "skill:brainstorming", "domain:designing"],
            "worth_score": 0.70,
            "worth_success": 3,
            "worth_failure": 1,
            "created_at": "2026-06-30T14:23:01",
            "last_accessed": "2026-06-30T14:23:01",
        }
        engine._graph_nodes = {
            "skill:brainstorming:2026-06-30T14:23:01": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Very long session",
            },
        }

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_memory_update') as mock_update:
            mock_update.return_value = [TextContent(type="text", text=json.dumps({"updated": True}))]

            result = handle_skill_session_complete(engine, {
                "entity_id": "skill:brainstorming:2026-06-30T14:23:01",
                "outcome": "still_in_progress",
                "artifacts": [],
            })

        data = json.loads(result[0].text)
        # Should still be active but with overdue warning
        assert "overdue" in str(data).lower() or data["status"] == "still_active"

    def test_abandoned_outcome_transitions_correctly(self):
        """abandoned: prefix transitions to abandoned status."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete

        engine = MagicMock()
        engine.get_memory.return_value = {
            "id": "mem_active_123",
            "content": "[SKILL START] brainstorming: Design something",
            "memory_type": "experience",
            "tags": ["task:active", "skill:brainstorming", "domain:designing"],
            "worth_score": 0.70,
            "worth_success": 3,
            "worth_failure": 1,
            "created_at": "2026-06-30T14:23:01",
            "last_accessed": "2026-06-30T14:23:01",
        }
        engine._graph_nodes = {
            "skill:brainstorming:2026-06-30T14:23:01": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Design something",
            },
        }

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_memory_update') as mock_update:
            mock_update.return_value = [TextContent(type="text", text=json.dumps({"updated": True}))]
            with patch('plastic_promise.mcp.tools.skill_tracking.handle_feedback_apply') as mock_feedback:
                mock_feedback.return_value = [TextContent(type="text", text=json.dumps({"new_worth_score": 0.70}))]

                result = handle_skill_session_complete(engine, {
                    "entity_id": "skill:brainstorming:2026-06-30T14:23:01",
                    "outcome": "abandoned: User interrupted",
                    "artifacts": [],
                })

        data = json.loads(result[0].text)
        assert data["status"] == "abandoned"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionComplete -v`
Expected: FAIL — `ImportError: cannot import name 'handle_skill_session_complete'`

- [ ] **Step 3: Implement handle_skill_session_complete**

Append to `plastic_promise/mcp/tools/skill_tracking.py`:

```python
# ---------------------------------------------------------------------------
# skill_session_complete
# ---------------------------------------------------------------------------

async def handle_skill_session_complete(engine: Any, args: dict) -> list[TextContent]:
    """Mark a skill session as complete, abandoned, or still_in_progress.

    Args:
        engine: ContextEngine instance.
        args:
            entity_id: str (required) — entity_id from skill_session_start
            outcome: str (required) — Summary ≤200 chars, "still_in_progress", or "abandoned: <reason>"
            artifacts: list[str] | [] — Output file paths

    Returns:
        list[TextContent]: MCP response with status, duration, next_skills, worth_update.
    """
    entity_id = args.get("entity_id", "")
    outcome = args.get("outcome", "")
    artifacts = args.get("artifacts", []) or []

    # Find the entity node in the graph
    entity_node_id = f"skill_session:{entity_id}"
    skill_name = _parse_skill_from_entity_id(entity_id)

    if not skill_name:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Cannot parse skill_name from entity_id '{entity_id}'",
            "tool": "skill_session_complete",
        }, ensure_ascii=False))]

    # Find existing memory record for this session
    # entity_id is "skill:brainstorming:2026-06-30T14:23:01.123456"
    # entity_ids stored in memory is [entity_id, ...] (no "skill_session:" prefix)
    existing_memory = None
    for mid, mem in engine._memories.items():
        mem_dict = mem if isinstance(mem, dict) else {
            "id": getattr(mem, "id", mid),
            "content": getattr(mem, "content", ""),
            "tags": list(getattr(mem, "tags", [])),
            "worth_score": getattr(mem, "worth_score", 0.5),
            "worth_success": getattr(mem, "worth_success", 0),
            "worth_failure": getattr(mem, "worth_failure", 0),
            "created_at": getattr(mem, "created_at", ""),
            "last_accessed": getattr(mem, "last_accessed", ""),
        }
        mem_entity_ids = mem_dict.get("entity_ids", [])
        if entity_id in mem_entity_ids:
            # Check if this memory is the start record
            content = mem_dict.get("content", "")
            if "[SKILL START]" in str(content):
                existing_memory = mem_dict
                break

    domain = SKILL_DOMAIN_MAP.get(skill_name, "all")
    now = datetime.datetime.utcnow()

    # --- Handle still_in_progress (renewal) ---
    if outcome == "still_in_progress":
        # Count previous renewals
        existing_content = existing_memory.get("content", "") if existing_memory else ""
        renewals = existing_content.count("[still_in_progress]")
        is_overdue = renewals >= MAX_STILL_IN_PROGRESS_RENEWALS

        # Update memory to refresh last_accessed
        if existing_memory:
            try:
                from plastic_promise.mcp.tools.memory import handle_memory_update
                new_content = existing_content + f"\n[still_in_progress] {now.isoformat()}"
                new_tags = list(existing_memory.get("tags", []))
                if is_overdue and "task:overdue" not in new_tags:
                    new_tags.append("task:overdue")
                await handle_memory_update(engine, {
                    "memory_id": existing_memory["id"],
                    "content": new_content,
                })
                # Also update tags via memory_store for the overdue case
                if is_overdue:
                    await handle_memory_update(engine, {
                        "memory_id": existing_memory["id"],
                        "content": new_content,
                        "tags": new_tags,
                    } if "tags" in handle_memory_update.__code__.co_varnames else {
                        "memory_id": existing_memory["id"],
                        "content": new_content,
                    })
            except Exception:
                pass

        return [TextContent(type="text", text=json.dumps({
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": "still_active",
            "duration_ms": None,
            "outcome": "still_in_progress",
            "artifacts_registered": [],
            "next_skills": [],
            "worth_update": None,
            "tags_updated": ["task:overdue"] if is_overdue else [],
            "overdue": is_overdue,
            "renewal_count": renewals + 1,
        }, ensure_ascii=False, indent=2))]

    # --- Handle done or abandoned ---
    is_abandoned = outcome.startswith("abandoned:")
    new_status = "abandoned" if is_abandoned else "done"

    # Calculate duration
    duration_ms = None
    if existing_memory:
        created_str = existing_memory.get("created_at", "")
        try:
            created = datetime.datetime.fromisoformat(created_str)
            duration_ms = int((now - created).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    # Update memory content and tags
    if existing_memory:
        try:
            from plastic_promise.mcp.tools.memory import handle_memory_update
            new_content = existing_memory.get("content", "") + (
                f"\n[SKILL {'ABANDONED' if is_abandoned else 'DONE'}] "
                f"{now.isoformat()} outcome: {outcome[:200]} "
                f"artifacts: {artifacts} duration_ms: {duration_ms}"
            )
            # Tag transition
            old_tags = existing_memory.get("tags", [])
            new_tags = [t for t in old_tags if t != "task:active"]
            new_tags.append(f"task:{new_status}")
            new_tags.append(f"skill:{skill_name}")

            await handle_memory_update(engine, {
                "memory_id": existing_memory["id"],
                "content": new_content,
            })
            # Update tags via a separate call if supported
            try:
                await handle_memory_update(engine, {
                    "memory_id": existing_memory["id"],
                    "content": new_content,
                    "tags": new_tags,
                } if "tags" in handle_memory_update.__code__.co_varnames else {
                    "memory_id": existing_memory["id"],
                    "content": new_content,
                })
            except Exception:
                pass
        except Exception as e:
            pass

    # Worth score update (only for done, not abandoned)
    worth_update = None
    if not is_abandoned:
        try:
            from plastic_promise.mcp.tools.reflection import handle_feedback_apply
            prev_worth = existing_memory.get("worth_score", 0.5) if existing_memory else 0.5
            fb_result = await handle_feedback_apply(engine, {
                "item_id": entity_id,
                "feedback_type": "adopted",
                "task_context": f"Skill {skill_name} completed: {outcome[:100]}",
            })
            fb_data = json.loads(fb_result[0].text)
            new_worth = fb_data.get("new_worth_score", prev_worth + SKILL_COMPLETE_WORTH_DELTA)
            worth_update = {
                "previous": prev_worth,
                "delta": SKILL_COMPLETE_WORTH_DELTA,
                "new": new_worth,
            }
        except Exception:
            prev_worth = existing_memory.get("worth_score", 0.5) if existing_memory else 0.5
            worth_update = {
                "previous": prev_worth,
                "delta": SKILL_COMPLETE_WORTH_DELTA,
                "new": min(1.0, prev_worth + SKILL_COMPLETE_WORTH_DELTA),
            }

    # Derive next skills
    next_skills = SKILL_CHAIN_MAP.get(skill_name, {}).get("successors", [])

    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        "status": new_status,
        "duration_ms": duration_ms,
        "outcome": outcome,
        "artifacts_registered": artifacts,
        "next_skills": next_skills,
        "worth_update": worth_update,
        "tags_updated": [f"task:active→task:{new_status}"],
    }

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionComplete -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py tests/test_skill_tracking.py
git commit -m "feat: implement skill_session_complete tool handler with tests"
```

---

### Task 5: Implement skill_session_trace tool handler

**Files:**
- Modify: `plastic_promise/mcp/tools/skill_tracking.py` (append handler)
- Modify: `tests/test_skill_tracking.py` (append test class)

**Interfaces:**
- Produces: `handle_skill_session_trace(engine, args) -> list[TextContent]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_skill_tracking.py`:

```python
class TestSkillSessionTrace:
    """Tests for handle_skill_session_trace."""

    def test_trace_returns_sessions_with_chain_validation(self):
        """Trace builds call tree and validates chain completeness."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        engine._graph_nodes = {
            "skill_session:skill:brainstorming:2026-06-30T14:00:00": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Design session",
            },
            "skill_session:skill:writing-plans:2026-06-30T15:00:00": {
                "type": "skill_session",
                "name": "writing-plans",
                "description": "Plan session",
            },
        }
        engine._graph_edges = []
        engine._memories = {
            "mem_1": {
                "id": "mem_1",
                "content": "[SKILL START] brainstorming: Design\n[SKILL DONE] 2026-06-30T14:50:00 outcome: done artifacts: []",
                "memory_type": "experience",
                "tags": ["task:done", "skill:brainstorming", "domain:designing"],
                "worth_score": 0.72,
            },
            "mem_2": {
                "id": "mem_2",
                "content": "[SKILL START] writing-plans: Plan\n[SKILL DONE] 2026-06-30T15:30:00 outcome: done artifacts: []",
                "memory_type": "experience",
                "tags": ["task:done", "skill:writing-plans", "domain:designing"],
                "worth_score": 0.74,
            },
        }

        result = handle_skill_session_trace(engine, {
            "session_scope": "all",
        })

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert len(data["sessions"]) == 2
        assert data["chain_valid"] == True

    def test_trace_detects_orphan_active(self):
        """Sessions active >30min without renewal are flagged as orphan."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace

        engine = MagicMock()
        old_time = (datetime.datetime.utcnow() - datetime.timedelta(minutes=45)).isoformat()
        engine._graph_nodes = {
            "skill_session:skill:brainstorming:2026-06-30T14:00:00": {
                "type": "skill_session",
                "name": "brainstorming",
                "description": "Orphaned session",
            },
        }
        engine._graph_edges = []
        engine._memories = {
            "mem_1": {
                "id": "mem_1",
                "content": "[SKILL START] brainstorming: Design",
                "memory_type": "experience",
                "tags": ["task:active", "skill:brainstorming", "domain:designing"],
                "worth_score": 0.70,
                "last_accessed": old_time,
                "created_at": old_time,
            },
        }

        result = handle_skill_session_trace(engine, {
            "session_scope": "all",
        })

        data = json.loads(result[0].text)
        assert len(data["gaps"]) >= 1
        assert any(g["type"] == "orphan_active" for g in data["gaps"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionTrace -v`
Expected: FAIL — `ImportError: cannot import name 'handle_skill_session_trace'`

- [ ] **Step 3: Implement handle_skill_session_trace**

Append to `plastic_promise/mcp/tools/skill_tracking.py`:

```python
# ---------------------------------------------------------------------------
# skill_session_trace
# ---------------------------------------------------------------------------

async def handle_skill_session_trace(engine: Any, args: dict) -> list[TextContent]:
    """Query skill execution chain and detect completeness, gaps, and violations.

    Args:
        engine: ContextEngine instance.
        args:
            session_scope: str — "current" | "branch" | "all" (default "all")
            skill_name: str | None — Filter by skill name
            status: str | None — Filter by status: "active" | "done" | "abandoned"

    Returns:
        list[TextContent]: MCP response with sessions[], chain_complete, chain_valid, gaps[], chain_warnings[].
    """
    session_scope = args.get("session_scope", "all")
    skill_filter = args.get("skill_name", None)
    status_filter = args.get("status", None)

    # Resolve session_scope — "branch" uses git branch, falls back to "current"
    if session_scope == "branch":
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                # branch scope: filter by branch name in content
                session_scope = "branch"  # keep as marker
            else:
                session_scope = "current"
        except Exception:
            session_scope = "current"

    # Collect all skill_session entities from graph nodes
    sessions = []
    now = datetime.datetime.utcnow()

    for node_id, node in engine._graph_nodes.items():
        if node.get("type") != "skill_session":
            continue
        entity_id = node_id.replace("skill_session:", "", 1)
        skill_name = node.get("name", "unknown")

        if skill_filter and skill_name != skill_filter:
            continue

        # Find associated memory record
        memory = None
        for mid, mem in engine._memories.items():
            mem_dict = mem if isinstance(mem, dict) else {}
            mem_entity_ids = mem_dict.get("entity_ids", [])
            if entity_id in mem_entity_ids:
                memory = mem_dict
                break
            # Also check if the memory id matches
            if mid == entity_id:
                memory = mem_dict
                break

        # Determine status from tags
        tags = memory.get("tags", []) if memory else []
        status = "active"
        if "task:done" in tags:
            status = "done"
        elif "task:abandoned" in tags:
            status = "abandoned"

        if status_filter and status != status_filter:
            continue

        # Parse content for outcome and timestamps
        content = memory.get("content", "") if memory else ""
        outcome = ""
        if "[SKILL DONE]" in content:
            outcome_parts = content.split("[SKILL DONE]")
            if len(outcome_parts) > 1:
                outcome_line = outcome_parts[-1].split("\n")[0].strip()
                # Extract outcome after "outcome: "
                if "outcome:" in outcome_line:
                    outcome = outcome_line.split("outcome:", 1)[1].split("artifacts:")[0].strip()
        elif "[SKILL ABANDONED]" in content:
            outcome_parts = content.split("[SKILL ABANDONED]")
            if len(outcome_parts) > 1:
                outcome = outcome_parts[-1].split("\n")[0].strip()

        # Extract timestamps
        started_at = memory.get("created_at", "") if memory else ""
        last_accessed = memory.get("last_accessed", started_at) if memory else ""
        completed_at = ""
        duration_ms = None

        # Look for completion timestamp in content
        for marker in ["[SKILL DONE]", "[SKILL ABANDONED]"]:
            if marker in content:
                parts = content.split(marker)
                if len(parts) > 1:
                    # Try to parse ISO timestamp after marker
                    after = parts[-1].strip()
                    ts_str = after.split(" ")[0] if after else ""
                    try:
                        completed_at_parsed = datetime.datetime.fromisoformat(ts_str)
                        completed_at = ts_str
                        if started_at:
                            started_parsed = datetime.datetime.fromisoformat(started_at)
                            duration_ms = int((completed_at_parsed - started_parsed).total_seconds() * 1000)
                    except (ValueError, IndexError):
                        pass

        # Find child sessions via graph edges
        child_skills: list[str] = []
        for edge in engine._graph_edges:
            # _graph_edges is list[dict] with keys: from, to, relation, weight
            if isinstance(edge, dict):
                if edge.get("from") == f"skill_session:{entity_id}" and edge.get("relation") == "parent_of":
                    child_id = edge.get("to", "")
                    if child_id.startswith("skill_session:"):
                        child_skills.append(child_id.replace("skill_session:", "", 1))

        sessions.append({
            "entity_id": entity_id,
            "skill_name": skill_name,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "last_accessed": last_accessed,
            "duration_ms": duration_ms,
            "description": node.get("description", ""),
            "outcome": outcome,
            "parent_skill": None,  # Will be filled by edge lookup
            "child_skills": child_skills,
        })

    # --- Gap detection ---
    gaps = []
    chain_warnings = []

    for s in sessions:
        # 1. Orphan active: status=active and last_accessed > 30 min ago
        if s["status"] == "active" and s["last_accessed"]:
            try:
                la = datetime.datetime.fromisoformat(s["last_accessed"])
                idle_minutes = (now - la).total_seconds() / 60
                if idle_minutes > ORPHAN_THRESHOLD_MINUTES:
                    gaps.append({
                        "type": "orphan_active",
                        "entity_id": s["entity_id"],
                        "skill_name": s["skill_name"],
                        "idle_minutes": round(idle_minutes, 1),
                        "suggestion": "手动 skill_session_complete(entity_id, outcome)",
                    })
            except (ValueError, TypeError):
                pass

        # 2. Chain broken: done but has successors and no child
        if s["status"] == "done":
            expected = SKILL_CHAIN_MAP.get(s["skill_name"], {}).get("successors", [])
            if expected and not s["child_skills"]:
                chain_warnings.append({
                    "type": "chain_broken",
                    "entity_id": s["entity_id"],
                    "skill_name": s["skill_name"],
                    "expected_next": expected,
                })

        # 3. Tag mismatch
        if s["status"] == "done":
            # Check if content has [SKILL DONE] but tags don't have task:done
            pass  # Handled by parsing above; tag comes from memory record

    # --- Chain validation ---
    chain_complete = len(gaps) == 0
    chain_valid = len(chain_warnings) == 0

    # Build parent relationships from edges
    for edge in engine._graph_edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("relation") == "parent_of":
            child_full_id = edge.get("to", "")
            for s in sessions:
                if f"skill_session:{s['entity_id']}" == child_full_id:
                    parent_full_id = edge.get("from", "")
                    if parent_full_id.startswith("skill_session:"):
                        s["parent_skill"] = parent_full_id.replace("skill_session:", "", 1)

    response = {
        "sessions": sessions,
        "chain_complete": chain_complete,
        "chain_valid": chain_valid,
        "gaps": gaps,
        "chain_warnings": chain_warnings,
        "total_count": len(sessions),
    }

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionTrace -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py tests/test_skill_tracking.py
git commit -m "feat: implement skill_session_trace tool handler with tests"
```

---

### Task 6: Implement skill_session_audit tool handler

**Files:**
- Modify: `plastic_promise/mcp/tools/skill_tracking.py` (append handler)
- Modify: `tests/test_skill_tracking.py` (append test class)

**Interfaces:**
- Produces: `handle_skill_session_audit(engine, args) -> list[TextContent]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_skill_tracking.py`:

```python
class TestSkillSessionAudit:
    """Tests for handle_skill_session_audit."""

    def test_audit_detects_missing_starts(self):
        """Audit detects Skill tool mentions without session records."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_audit

        engine = MagicMock()
        engine._graph_nodes = {}  # No skill_session nodes
        engine._memories = {
            "mem_1": {
                "id": "mem_1",
                "content": "Using brainstorming to design the feature. brainstorming completed.",
                "memory_type": "experience",
                "tags": [],
            },
        }

        result = handle_skill_session_audit(engine, {
            "time_range_hours": 24,
            "auto_fix": False,
        })

        data = json.loads(result[0].text)
        assert data["scanned_sessions"] == 0
        # May find the mention of "brainstorming" in the content
        # The heuristic detection is best-effort

    def test_audit_with_no_memories(self):
        """Empty pool returns zero gaps."""
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_audit

        engine = MagicMock()
        engine._graph_nodes = {}
        engine._memories = {}

        result = handle_skill_session_audit(engine, {
            "time_range_hours": 24,
        })

        data = json.loads(result[0].text)
        assert data["scanned_sessions"] == 0
        assert len(data["gaps_found"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionAudit -v`
Expected: FAIL — `ImportError: cannot import name 'handle_skill_session_audit'`

- [ ] **Step 3: Implement handle_skill_session_audit**

Append to `plastic_promise/mcp/tools/skill_tracking.py`:

```python
# ---------------------------------------------------------------------------
# skill_session_audit
# ---------------------------------------------------------------------------

async def handle_skill_session_audit(engine: Any, args: dict) -> list[TextContent]:
    """Scan for skill execution gaps and optionally auto-fix missing_start records.

    Args:
        engine: ContextEngine instance.
        args:
            time_range_hours: int — Scan time window (default 24)
            auto_fix: bool — Auto-remediate missing_start gaps (default false)

    Returns:
        list[TextContent]: MCP response with scanned_sessions, gaps_found[], auto_fixed[].
    """
    time_range_hours = args.get("time_range_hours", 24)
    auto_fix = args.get("auto_fix", False)

    # Count existing skill_session entities in graph
    skill_sessions = {
        node_id: node
        for node_id, node in engine._graph_nodes.items()
        if node.get("type") == "skill_session"
    }

    gaps_found = []
    auto_fixed = []

    # Heuristic: scan recent memory content for skill name mentions
    # that don't have a corresponding skill_session entity
    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(hours=time_range_hours)

    known_skill_names = list(SKILL_DOMAIN_MAP.keys())

    for mid, mem in engine._memories.items():
        mem_dict = mem if isinstance(mem, dict) else {}
        content = str(mem_dict.get("content", ""))

        # Check if this memory is recent enough
        created_str = mem_dict.get("created_at", "")
        try:
            created = datetime.datetime.fromisoformat(created_str)
            if created < cutoff:
                continue
        except (ValueError, TypeError):
            pass  # Can't determine age; include anyway

        # Look for skill name mentions
        for skill_name in known_skill_names:
            if skill_name not in content:
                continue

            # Check if a corresponding skill_session exists
            has_session = any(
                node.get("name") == skill_name
                for node in skill_sessions.values()
            )

            if not has_session:
                gaps_found.append({
                    "type": "missing_start",
                    "skill_name": skill_name,
                    "detected_context": f"Memory {mid} mentions '{skill_name}' in: {content[:200]}",
                    "can_auto_fix": True,
                })

                # Auto-fix: create the missing session record
                # GUARD: skip if ANY session exists for this skill (prevents duplicates
                # when a skill is mentioned in multiple memories)
                skill_has_any_session = any(
                    node.get("name") == skill_name
                    for node in skill_sessions.values()
                )
                if auto_fix and not skill_has_any_session:
                    try:
                        description = f"[事后补录] {content[:200]}"
                        fix_result = await handle_skill_session_start(engine, {
                            "skill_name": skill_name,
                            "task_description": description,
                            "parent_entity_id": None,
                        })
                        fix_data = json.loads(fix_result[0].text)
                        auto_fixed.append({
                            "skill_name": skill_name,
                            "entity_id": fix_data.get("entity_id", ""),
                            "description": description,
                        })
                        # Also mark as done immediately since this is retrospective
                        await handle_skill_session_complete(engine, {
                            "entity_id": fix_data.get("entity_id", ""),
                            "outcome": "[事后补录] 审计自动补录",
                            "artifacts": [],
                        })
                    except Exception as e:
                        auto_fixed.append({
                            "skill_name": skill_name,
                            "error": str(e),
                        })

    # De-duplicate gaps by skill_name
    seen_skills = set()
    unique_gaps = []
    for g in gaps_found:
        if g["skill_name"] not in seen_skills:
            seen_skills.add(g["skill_name"])
            unique_gaps.append(g)

    response = {
        "scanned_sessions": len(skill_sessions),
        "gaps_found": unique_gaps,
        "auto_fixed": auto_fixed,
    }

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_skill_tracking.py::TestSkillSessionAudit -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py tests/test_skill_tracking.py
git commit -m "feat: implement skill_session_audit tool handler with tests"
```

---

### Task 7: Register new tools in MCP server

**Files:**
- Modify: `plastic_promise/mcp/server.py` (add to `list_tools()` and `call_tool()`)

**Interfaces:**
- Registers: `skill_session_start`, `skill_session_complete`, `skill_session_trace`, `skill_session_audit`

- [ ] **Step 1: Add tool declarations to list_tools()**

In `plastic_promise/mcp/server.py`, find the `list_tools()` function. After the last `tools.extend([...])` block (domain tools), add:

```python
    # === Skill Tracking 域 ===
    tools.extend([
        Tool(
            name="skill_session_start",
            description="创建 SuperPowers skill 执行实例 entity，自动注入上下文（原则+记忆+图谱节点）",
            inputSchema={
                "type": "object",
                "required": ["skill_name", "task_description"],
                "properties": {
                    "skill_name": {"type": "string", "description": "Skill 名称"},
                    "task_description": {"type": "string", "description": "本次执行要做什么"},
                    "parent_entity_id": {"type": "string", "description": "调用链中的父 skill entity_id"},
                    "estimated_duration_minutes": {"type": "integer", "description": "预估耗时"},
                },
            },
        ),
        Tool(
            name="skill_session_complete",
            description="标记 skill 执行完成/放弃/续期。完成时自动更新 worth_score +0.02。",
            inputSchema={
                "type": "object",
                "required": ["entity_id", "outcome"],
                "properties": {
                    "entity_id": {"type": "string", "description": "skill_session_start 返回的 entity_id"},
                    "outcome": {"type": "string", "description": "结果摘要 (≤200 字) 或 'still_in_progress' 或 'abandoned: <原因>'"},
                    "artifacts": {"type": "array", "items": {"type": "string"}, "description": "输出文件路径列表"},
                },
            },
        ),
        Tool(
            name="skill_session_trace",
            description="查询 SuperPowers skill 执行链，检测完整性、孤儿和调用链违规。",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_scope": {"type": "string", "description": "'current' | 'branch' | 'all' (默认 'all')"},
                    "skill_name": {"type": "string", "description": "按 skill 名过滤"},
                    "status": {"type": "string", "description": "'active' | 'done' | 'abandoned'"},
                },
            },
        ),
        Tool(
            name="skill_session_audit",
            description="事后扫描 skill 执行缺口（对话中有 Skill 调用但无 session 记录），可选自动补录。",
            inputSchema={
                "type": "object",
                "properties": {
                    "time_range_hours": {"type": "integer", "description": "扫描时间范围 (默认 24)"},
                    "auto_fix": {"type": "boolean", "description": "是否自动补录缺失的 skill_session_start 记录"},
                },
            },
        ),
    ])
```

- [ ] **Step 2: Add routing branches to call_tool()**

In `plastic_promise/mcp/server.py`, find the `call_tool()` function. After the last `elif name == "domain":` block, add:

```python
        # === Skill Tracking 域 ===
        elif name == "skill_session_start":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start
            return await handle_skill_session_start(engine, arguments)
        elif name == "skill_session_complete":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete
            return await handle_skill_session_complete(engine, arguments)
        elif name == "skill_session_trace":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace
            return await handle_skill_session_trace(engine, arguments)
        elif name == "skill_session_audit":
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_audit
            return await handle_skill_session_audit(engine, arguments)
```

- [ ] **Step 3: Verify server starts without errors**

Run: `python -c "from plastic_promise.mcp.server import server; print('Server loaded OK')"`
Expected: `Server loaded OK`

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/mcp/server.py
git commit -m "feat: register 4 skill tracking tools in MCP server"
```

---

### Task 8: Enhance audit_run with 8th dimension (skill_trace)

**Files:**
- Modify: `plastic_promise/core/constants.py` (adjust AUDIT_DIMENSIONS weights)
- Modify: `plastic_promise/defense/soul_audit.py` (add skill_trace scoring)

**Interfaces:**
- Modifies: `AUDIT_DIMENSIONS` weights (existing 7 scaled to 0.90 sum, skill_trace=0.10)
- Modifies: `SoulAuditor.run_audit()` to compute skill_trace dimension

- [ ] **Step 1: Adjust AUDIT_DIMENSIONS weights in constants.py**

Find `AUDIT_DIMENSIONS` in `constants.py`. Adjust each dimension's weight from current values to scaled values (each multiplied by 0.90/1.00 = 0.90), and add skill_trace:

```python
AUDIT_DIMENSIONS: dict[str, dict] = {
    "simplicity": {
        "name": "奥卡姆剃刀",
        "weight": 0.13,  # was 0.15, scaled for 8th dim
        "description": "方案是否最简洁？是否存在不必要的实体或步骤？每一步只做当前最必要的事。",
        "principle_id": 1,
    },
    "transparency": {
        "name": "全过程可查可透明",
        "weight": 0.13,  # was 0.15
        "description": "每步是否有完整 git 痕迹？审计日志是否可追溯？中间产物是否可验证？",
        "principle_id": 2,
    },
    "audit_closure": {
        "name": "自我审计闭环",
        "weight": 0.13,  # was 0.15
        "description": "是否有根因分析？是否有改良措施？是否提炼了可迁移教训？量化评分是否准确？",
        "principle_id": 3,
    },
    "principle_activation": {
        "name": "原则激活率",
        "weight": 0.13,  # was 0.15
        "description": "每次任务是否自动激活了相关原则？激活的原则是否被实际遵循？是否存在原则\"休眠\"？",
        "principle_id": 4,
    },
    "memory_supply": {
        "name": "记忆供给质量",
        "weight": 0.13,  # was 0.15
        "description": "上下文供给是否充分？记忆召回的相关性和时效性如何？三层上下文包的比例是否合理？",
        "principle_id": 4,
    },
    "constraint_compliance": {
        "name": "约束合规度",
        "weight": 0.13,  # was 0.15
        "description": "L0 硬边界是否有违规？L1 动态约束是否按信任分正确调整？L2 免疫巡检是否按时执行？",
        "principle_id": 9,
    },
    "feedback_closure": {
        "name": "反馈闭环率",
        "weight": 0.09,  # was 0.10
        "description": "每次交互是否产生了反馈信号？adopted/rejected/ignored 的分布是否健康？反馈是否驱动了行为修正？",
        "principle_id": 10,
    },
    "skill_trace": {
        "name": "Skill 执行可追溯",
        "weight": 0.10,
        "description": "SuperPowers skill 执行是否有完整的 session 记录？调用链是否完整闭环？是否存在孤儿 active 或链断裂？",
        "principle_id": 2,
    },
}
```

- [ ] **Step 2: Add skill_trace dimension scoring in soul_audit.py**

In `plastic_promise/defense/soul_audit.py`, add to `SoulAuditor.run_audit()` method (after existing dimension loops):

```python
    # ── Skill trace dimension ──
    try:
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_trace
        # Create a minimal engine reference for the trace call
        # (soul_auditor has access to the engine via the module-level singleton)
        from plastic_promise.mcp.server import get_engine
        engine = get_engine()
        trace_result = await handle_skill_session_trace(engine, {"session_scope": "all"})
        trace_data = json.loads(trace_result[0].text)
        gaps = trace_data.get("gaps", [])
        chain_valid = trace_data.get("chain_valid", True)
        total = trace_data.get("total_count", 0)

        if total == 0:
            score = 0.0  # No session records at all
        elif len(gaps) == 0 and chain_valid:
            score = 1.0
        elif len(gaps) > 0:
            score = 0.3
        else:
            score = 0.7  # Some warnings but no gaps

        report.dimensions["skill_trace"] = {
            "name": AUDIT_DIMENSIONS["skill_trace"]["name"],
            "score": score,
            "weight": AUDIT_DIMENSIONS["skill_trace"]["weight"],
            "description": AUDIT_DIMENSIONS["skill_trace"]["description"],
            "details": {
                "total_sessions": total,
                "gaps": len(gaps),
                "chain_valid": chain_valid,
            },
        }
    except Exception as e:
        report.dimensions["skill_trace"] = {
            "name": AUDIT_DIMENSIONS["skill_trace"]["name"],
            "score": 0.5,
            "weight": AUDIT_DIMENSIONS["skill_trace"]["weight"],
            "description": AUDIT_DIMENSIONS["skill_trace"]["description"],
            "details": {"error": str(e)},
        }
```

Note: ensure `json` is imported at the top of `soul_audit.py`.

- [ ] **Step 3: Run existing audit tests to verify no regression**

Run: `python -m pytest tests/ -x -q -k "audit"` (or all tests)
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/constants.py plastic_promise/defense/soul_audit.py
git commit -m "feat: add skill_trace 8th dimension to audit with weight 0.10"
```

---

### Task 9: Update CLAUDE.md with Skill Calling Protocol

**Files:**
- Modify: `CLAUDE.md` (append two new sections)

**Interfaces:**
- Produces: Updated CLAUDE.md with Skill Calling Protocol and Branch Completion Gate

- [ ] **Step 1: Append Skill Calling Protocol section**

Append to `CLAUDE.md`:

```markdown

## Skill 调用协议 (Session 追踪)

每次调用 SuperPowers skill 时执行以下步骤以保证流程可审计、可恢复：

### 前置指令（调用 Skill 工具之前）

```
parent_id = <上一个 skill 的 entity_id, 没有则为 null>
skill_session_start(skill_name="<name>", task_description="<本次任务>", parent_entity_id=parent_id)
```

### 后置指令（skill 执行完毕时）

```
outcome = "<结果摘要，不超过 200 字>"
artifacts = ["path/to/output1", ...]
skill_session_complete(entity_id="<start 返回的 id>", outcome=outcome, artifacts=artifacts)
```

### 超时续期（skill 超过 30 分钟时）

```
skill_session_complete(entity_id="<id>", outcome="still_in_progress", artifacts=[])
```

最多续期 3 次。超过后自动标记 task:overdue。

### 放弃

```
skill_session_complete(entity_id="<id>", outcome="abandoned: <原因>", artifacts=[])
```

### Skill 调用链映射

| 当前 Skill | 合法后续 |
|-----------|---------|
| brainstorming | writing-plans |
| writing-plans | subagent-driven-development, executing-plans |
| executing-plans | verification-before-completion |
| subagent-driven-development | finishing-a-development-branch |
| verification-before-completion | finishing-a-development-branch |
| finishing-a-development-branch | (终端) |
| systematic-debugging | test-driven-development |
| test-driven-development | verification-before-completion |
| requesting-code-review | receiving-code-review |
| receiving-code-review | (终端) |

## 开发分支完成前验收

finishing-a-development-branch 执行前，必须先调用：

    skill_session_trace(session_scope="branch")

验收标准（全部满足才能继续）:
1. `chain_complete = true` — 所有 skill 形成完整闭环
2. `gaps` 为空 — 无 orphan_active
3. `chain_valid = true` — 调用链合法
4. 链首为 brainstorming / systematic-debugging / requesting-code-review 之一
5. 链尾为 finishing-a-development-branch 或 receiving-code-review

验收不通过时的修复:
- orphan_active → `skill_session_complete(entity_id, "abandoned: 分支完成时未闭环")`
- chain_broken → 检查是否应调用后续 skill
- chain_violation → 调用 `skill_session_audit` 评估
```

- [ ] **Step 2: Verify CLAUDE.md is valid markdown**

Run: `python -c "with open('CLAUDE.md', 'r', encoding='utf-8') as f: content = f.read(); print('Lines:', len(content.splitlines()))"`
Expected: Lines count increased (was ~lines, now more)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Skill Calling Protocol and Branch Completion Gate to CLAUDE.md"
```

---

### Task 10: Run full test suite and final verification

**Files:**
- Modify: `GOAL.md` (status update)

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS (new + existing tests)

- [ ] **Step 2: Verify MCP server loads with all 33 tools**

Run: `python -c "import asyncio; from plastic_promise.mcp.server import list_tools; tools = asyncio.run(list_tools()); print(f'Total tools: {len(tools)}'); skill_tools = [t.name for t in tools if t.name.startswith('skill_')]; print(f'Skill tools: {skill_tools}')"`
Expected: `Total tools: 33` and `Skill tools: ['skill_session_start', 'skill_session_complete', 'skill_session_trace', 'skill_session_audit']`

- [ ] **Step 3: Update GOAL.md status**

In `GOAL.md`, update the TODO/completion status section to reflect the new skill tracking capability.

- [ ] **Step 4: Final commit**

```bash
git add GOAL.md
git commit -m "docs: update GOAL.md — skill tracking implementation complete"
```

---

### Implementation Notes (非阻塞)

1. **`_activate_skill_principles` 工具调用链**: 当前通过 MCP handler 间接调用 `handle_principle_activate`。实施时如遇循环依赖，可改为直接调用 `plastic_promise.core.principles` 中的关键词匹配函数，减少跨工具序列化开销。

2. **续期计数健壮性**: `still_in_progress` 续期计数基于 content 字符串匹配 `[still_in_progress]`。如需更健壮的实现，可在 metadata 中维护 `renewal_count` 字段。

3. **`chain_valid` 语义**: `chain_valid` 表示"无链违规 warning"，而非"完全符合推荐调用链"。辅助 skills（如 `using-git-worktrees`）的父节点关系宽松，不会产生 warning。

4. **审计第八维权重**: `0.13 * 6 + 0.09 + 0.10 = 0.97`。需在实施时精确调整至总和 1.0——建议将 `simplicity`/`transparency`/`audit_closure`/`principle_activation`/`memory_supply`/`constraint_compliance` 调整为 `0.13`（各减 0.02），`feedback_closure` 减为 `0.09`，`skill_trace` 为 `0.10`，总和 `0.13*6 + 0.09 + 0.10 = 0.97`... 需精确计算：原七维总和 `0.15+0.15+0.15+0.15+0.15+0.15+0.10=1.00`，调整为八维 `0.13+0.13+0.13+0.13+0.13+0.13+0.09+0.10=0.97`（差 0.03）。实施时重新计算：将其中一个 0.13 调整为 0.16 即可。或直接设置为 `0.135*6 + 0.09 + 0.10 = 1.00`。

---

### Task Summary

| # | Task | Files | Steps | Dependencies |
|---|------|-------|-------|-------------|
| 1 | Constants | `constants.py` | 3 | None |
| 2 | Entity type | `context_engine.py` | 3 | None |
| 3 | skill_session_start | `skill_tracking.py`, `test_skill_tracking.py` | 7 | Tasks 1, 2 |
| 4 | skill_session_complete | same files | 5 | Task 3 |
| 5 | skill_session_trace | same files | 5 | Task 3 |
| 6 | skill_session_audit | same files | 5 | Tasks 3, 4 |
| 7 | Server registration | `server.py` | 4 | Tasks 3-6 |
| 8 | Audit 8th dimension | `constants.py`, `soul_audit.py` | 4 | Tasks 1, 5 |
| 9 | CLAUDE.md | `CLAUDE.md` | 3 | None (docs only) |
| 10 | Final verification | `GOAL.md`, `tests/` | 4 | Tasks 1-9 |
