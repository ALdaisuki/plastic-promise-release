# Auto Context Inject Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `auto_context_inject` MCP tool — unified automated context injection across Pi Agent, Claude Code Hook, and SoulBridge, with self-feedback loop.

**Architecture:** New handler in `context.py` that chains `skill_session_start` → `SoulLoop.pre_task_v2` → `memory_store` → `skill_session_complete`. Three paths (SoulBridge Python, Claude Hook CLI, Pi Daemon SSE) all call the same MCP tool. Inject records enter memory pool for self-feedback retrieval.

**Tech Stack:** Python 3.10+, Plastic Promise MCP framework, pytest

## Global Constraints

- Zero new storage structures — reuse MemoryRecord + entity graph + tag system
- Graceful degradation: any internal component failure → return partial data, never block
- `auto_inject:` prefix skill_name → domain "reflecting", skip parent validation, skip orphan detection
- Content format: preserve full `task_description` verbatim for self-feedback retrieval
- SoulBridge.pre_task() return structure stays backward-compatible
- Pi Agent scope: "global" (default); Claude Code scope: "agent:claude"
- Principle fallback: when `pre_task_v2` degrades, call `principle_activate("general")`
- MCP tool count: 34 (from 33)

---

### Task 1: skill_tracking.py — auto_inject prefix support

**Files:**
- Modify: `plastic_promise/mcp/tools/skill_tracking.py`

**Interfaces:**
- Produces: `_store_skill_start` handles `auto_inject:` prefix with domain="reflecting", no parent validation, no orphan detection

- [ ] **Step 1: Add auto_inject domain fallback, parent-skip, orphan-skip, and include_auto_inject parameter**

In `plastic_promise/mcp/tools/skill_tracking.py`:

**A. Domain fallback in `_store_skill_start`:** Find `domain = SKILL_DOMAIN_MAP.get(...)` (~line 406), add before it:

```python
    # auto_inject:* prefix → "reflecting" domain (context audit snapshot)
    if skill_name.startswith("auto_inject:"):
        domain = "reflecting"
    else:
        domain = SKILL_DOMAIN_MAP.get(skill_name, "general")
```

**B. Parent validation skip in `_validate_parent`:** (~line 357) add early return:

```python
    # auto_inject: sessions have no parent chain — skip validation
    if skill_name.startswith("auto_inject:"):
        return None
```

**C. Orphan detection skip in `handle_skill_session_trace`:** In the sessions loop:

```python
    # auto_inject: sessions are instant — skip orphan detection
    if s["skill_name"].startswith("auto_inject:"):
        continue
```

**D. Add `include_auto_inject` parameter to `handle_skill_session_trace`:**

```python
async def handle_skill_session_trace(engine: Any, args: dict) -> list[TextContent]:
    # ... existing args parsing ...
    include_auto_inject = args.get("include_auto_inject", False)
    
    # After collecting all sessions, filter if needed:
    if not include_auto_inject:
        sessions = [s for s in sessions if not s["skill_name"].startswith("auto_inject:")]
```

- [ ] **Step 2: Run existing skill_tracking tests to verify no regression**

Run: `python -m pytest tests/test_skill_tracking.py -v`
Expected: 12 PASSED

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/mcp/tools/skill_tracking.py
git commit -m "feat: skill_tracking — auto_inject prefix support (domain=reflecting, skip parent/orphan checks)"
```

---

### Task 2: context.py — handle_auto_context_inject handler + tests

**Files:**
- Modify: `plastic_promise/mcp/tools/context.py` (append handler)
- Create: `tests/test_auto_context_inject.py`

**Interfaces:**
- Produces: `handle_auto_context_inject(engine, args) -> list[TextContent]`
- Consumes: `handle_skill_session_start` (from skill_tracking), `SoulLoop.pre_task_v2`, `handle_memory_store` (from memory), `handle_skill_session_complete` (from skill_tracking)

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_context_inject.py`:

```python
"""Tests for auto_context_inject MCP tool."""
import json
import pytest
from unittest.mock import MagicMock, patch, ANY
from mcp.types import TextContent


class TestAutoContextInject:
    """Tests for handle_auto_context_inject."""

    def test_inject_creates_session_and_returns_context(self):
        """Full inject flow: start → supply → store → complete."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {
            "node_id": "skill_session:skill:auto_inject:claude_code:2026-07-01T18:00:00",
            "type": "skill_session",
            "name": "auto_inject:claude_code",
            "is_new": True,
            "edges_created": 0,
        }

        with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.to_prompt.return_value = "# Context Pack"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.context.handle_memory_store') as mock_store:
                mock_store.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"memory_id": "mem_inject_001", "stored": True})
                )]

                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(
                        type="text",
                        text=json.dumps({
                            "entity_id": "skill:auto_inject:claude_code:2026-07-01T18:00:00",
                            "skill_name": "auto_inject:claude_code",
                            "status": "active",
                            "domain": "reflecting",
                            "activated_principles": [{"id": 2, "name": "全过程可查可透明"}],
                            "related_memories": [],
                            "tags_applied": ["task:active", "skill:auto_inject:claude_code", "domain:reflecting"],
                            "chain_warning": None,
                        })
                    )]

                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(
                            type="text",
                            text=json.dumps({
                                "entity_id": "skill:auto_inject:claude_code:2026-07-01T18:00:00",
                                "status": "done",
                            })
                        )]

                        result = handle_auto_context_inject(engine, {
                            "task_description": "修复 JWT 认证 bug",
                            "task_type": "code_generation",
                            "source": "claude_code",
                        })

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["skill_name"] == "auto_inject:claude_code"
        assert "entity_id" in data
        assert data["inject_memory_id"] == "mem_inject_001"
        assert "principles" in data

    def test_inject_graceful_degradation_when_start_fails(self):
        """Skill session start failure does not block inject."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()

        with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start',
                   side_effect=Exception("MCP unavailable")):
            with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
                mock_loop = MagicMock()
                mock_pack = MagicMock()
                mock_pack.to_prompt.return_value = "# Context Pack"
                mock_loop.pre_task_v2.return_value = mock_pack
                mock_loop_class.return_value = mock_loop

                with patch('plastic_promise.mcp.tools.context.handle_memory_store') as mock_store:
                    mock_store.return_value = [TextContent(
                        type="text",
                        text=json.dumps({"memory_id": "mem_inject_fallback", "stored": True})
                    )]

                    result = handle_auto_context_inject(engine, {
                        "task_description": "修复 bug",
                        "source": "manual",
                    })

        data = json.loads(result[0].text)
        # Should still return context even though tracking failed
        assert "context_pack" in data or "partial" in str(data)

    def test_inject_stores_full_task_description_in_content(self):
        """Content preserves full task_description for self-feedback retrieval."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}
        task_desc = "修复 JWT 认证 bug — token 过期后 refresh 流程异常"

        stored_content = []

        async def capture_store(eng, args):
            stored_content.append(args.get("content", ""))
            return [TextContent(type="text", text=json.dumps({"memory_id": "mem_cap", "stored": True}))]

        with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.core = []
            mock_pack.related = []
            mock_pack.divergent = []
            mock_pack.to_prompt.return_value = "# Context Pack"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.context.handle_memory_store', side_effect=capture_store):
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "activated_principles": [],
                        "related_memories": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        handle_auto_context_inject(engine, {
                            "task_description": task_desc,
                            "source": "manual",
                        })

        assert len(stored_content) == 1
        assert task_desc in stored_content[0]
        assert "[AUTO INJECT]" in stored_content[0]

    def test_inject_principle_fallback_when_supply_fails(self):
        """When pre_task_v2 fails, principle_activate is called as safety net."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            # Simulate pre_task_v2 failure
            mock_loop.pre_task_v2.side_effect = Exception("Embedding service down")
            mock_loop_class.return_value = mock_loop

            fallback_principles = [{"id": 1, "name": "奥卡姆剃刀"}, {"id": 2, "name": "全过程可查可透明"}]
            with patch('plastic_promise.mcp.tools.principles.handle_principle_activate') as mock_pa:
                mock_pa.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"activated": fallback_principles})
                )]

                with patch('plastic_promise.mcp.tools.context.handle_memory_store') as mock_store:
                    mock_store.return_value = [TextContent(
                        type="text",
                        text=json.dumps({"memory_id": "mem_fallback", "stored": True})
                    )]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                        mock_start.return_value = [TextContent(type="text", text=json.dumps({
                            "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                            "skill_name": "auto_inject:manual",
                            "status": "active",
                            "domain": "reflecting",
                            "activated_principles": [],
                            "chain_warning": None,
                        }))]
                        with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                            mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                            result = handle_auto_context_inject(engine, {
                                "task_description": "修复 bug",
                                "source": "manual",
                            })

        data = json.loads(result[0].text)
        # Should have fallback principles
        assert "奥卡姆剃刀" in str(data["principles"])
        assert "errors" in data or "partial" in str(data)

    def test_inject_memory_store_failure_does_not_block(self):
        """memory_store failure returns context_pack anyway."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_pack = MagicMock()
            mock_pack.core = []
            mock_pack.related = []
            mock_pack.divergent = []
            mock_pack.to_prompt.return_value = "# Context"
            mock_loop.pre_task_v2.return_value = mock_pack
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.context.handle_memory_store',
                       side_effect=Exception("Memory store down")) as mock_store:
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:00:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "activated_principles": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        result = handle_auto_context_inject(engine, {
                            "task_description": "修复 bug",
                            "source": "manual",
                        })

        data = json.loads(result[0].text)
        # Should still have context_pack even though store failed
        assert data.get("partial") == True
        assert data["inject_memory_id"] is None

    def test_self_feedback_loop_second_inject_hits_first(self):
        """Second inject with similar task_description retrieves first inject record."""
        from plastic_promise.mcp.tools.context import handle_auto_context_inject

        engine = MagicMock()
        engine.register_entity.return_value = {"node_id": "x", "type": "skill_session", "is_new": True}

        # Simulate existing inject memory in the pool
        first_inject_memory = {
            "id": "mem_first",
            "content": "[AUTO INJECT] 修复 JWT 认证 bug\ncore_items: 3\nactivated_principles: 奥卡姆剃刀, 全过程可查可透明",
            "memory_type": "experience",
            "tags": ["auto_inject", "source:manual", "task:done"],
            "worth_score": 0.72,
        }
        engine._memories = {"mem_first": first_inject_memory}

        # The supply() should find the first inject record
        pack_with_hit = MagicMock()
        core_item = MagicMock()
        core_item.id = "mem_first"
        core_item.content = first_inject_memory["content"]
        core_item.relevance = 0.85
        pack_with_hit.core = [core_item]
        pack_with_hit.related = []
        pack_with_hit.divergent = []
        pack_with_hit.to_prompt.return_value = "# Context with hit"

        with patch('plastic_promise.mcp.tools.context.SoulLoop') as mock_loop_class:
            mock_loop = MagicMock()
            mock_loop.pre_task_v2.return_value = pack_with_hit
            mock_loop_class.return_value = mock_loop

            with patch('plastic_promise.mcp.tools.context.handle_memory_store') as mock_store:
                mock_store.return_value = [TextContent(
                    type="text",
                    text=json.dumps({"memory_id": "mem_second", "stored": True})
                )]
                with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_start') as mock_start:
                    mock_start.return_value = [TextContent(type="text", text=json.dumps({
                        "entity_id": "skill:auto_inject:manual:2026-07-01T18:02:00",
                        "skill_name": "auto_inject:manual",
                        "status": "active",
                        "domain": "reflecting",
                        "related_memories": ["mem_first"],  # Self-feedback hit!
                        "activated_principles": [],
                        "chain_warning": None,
                    }))]
                    with patch('plastic_promise.mcp.tools.skill_tracking.handle_skill_session_complete') as mock_complete:
                        mock_complete.return_value = [TextContent(type="text", text=json.dumps({"status": "done"}))]

                        result = handle_auto_context_inject(engine, {
                            "task_description": "修复 OAuth 认证 bug",  # Similar task
                            "source": "manual",
                        })

        data = json.loads(result[0].text)
        # Second inject's context_pack should have the first inject record in core
        assert data["context_pack"]["core"][0]["id"] == "mem_first"
        assert "JWT" in data["context_pack"]["core"][0]["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_auto_context_inject.py -v`
Expected: FAIL — `ImportError: cannot import name 'handle_auto_context_inject'`

- [ ] **Step 3: Run tests again (still fail — handler not yet implemented)**

Run: `python -m pytest tests/test_auto_context_inject.py -v`
Expected: 6 tests collect, all FAIL with `ImportError`

- [ ] **Step 5: Implement handle_auto_context_inject**

Append to `plastic_promise/mcp/tools/context.py`:

```python
# ---------------------------------------------------------------------------
# auto_context_inject — 统一自动化上下文注入
# ---------------------------------------------------------------------------

async def handle_auto_context_inject(engine: Any, args: dict) -> list[TextContent]:
    """Unified automated context injection across Pi Agent, Claude Code, and SoulBridge.

    Chains: skill_session_start → SoulLoop.pre_task_v2 → memory_store → skill_session_complete.
    Graceful degradation: any internal failure returns partial data, never blocks.

    Args:
        engine: ContextEngine instance.
        args:
            task_description: str (required) — Current task description
            task_type: str — Task type (default "general")
            source: str — "pi_agent" | "claude_code" | "manual" (default "manual")
            scope: str — Retrieval scope (default "global")

    Returns:
        list[TextContent]: entity_id, context_pack, principles, inject_memory_id, stats
    """
    task_description = args.get("task_description", "")
    task_type = args.get("task_type", "general")
    source = args.get("source", "manual")
    scope = args.get("scope", "global")

    skill_name = f"auto_inject:{source}"
    entity_id = None
    context_pack = None
    principles: list[dict] = []
    inject_memory_id = None
    errors: list[str] = []

    # ── Step 1: skill_session_start ──
    try:
        from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_start
        start_result = await handle_skill_session_start(engine, {
            "skill_name": skill_name,
            "task_description": task_description,
            "parent_entity_id": None,
        })
        start_data = json.loads(start_result[0].text)
        entity_id = start_data.get("entity_id")
        principles = start_data.get("activated_principles", [])
    except Exception as e:
        errors.append(f"skill_session_start: {e}")

    # ── Step 2: SoulLoop.pre_task_v2 → ContextEngine.supply() ──
    try:
        from plastic_promise.loop.soul_loop import SoulLoop
        loop = SoulLoop(engine=engine)
        pack = loop.pre_task_v2(task_description, task_type)
        context_pack = {
            "core": [{"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                     for i in getattr(pack, 'core', [])],
            "related": [{"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                        for i in getattr(pack, 'related', [])],
            "divergent": [{"id": i.id, "content": i.content[:200], "relevance": i.relevance}
                          for i in getattr(pack, 'divergent', [])],
        }
        # Extract principles from pack if not already populated
        if not principles:
            pack_principles = getattr(pack, 'activated_principles', [])
            if pack_principles:
                principles = pack_principles
    except Exception as e:
        errors.append(f"pre_task_v2: {e}")
        # Fallback: call principle_activate directly as safety net
        try:
            from plastic_promise.mcp.tools.principles import handle_principle_activate
            pa_result = await handle_principle_activate(engine, {
                "task_type": task_type,
                "task_description": task_description,
            })
            pa_data = json.loads(pa_result[0].text)
            principles = pa_data.get("activated", [])
        except Exception:
            pass

    # ── Step 3: memory_store — inject record into memory pool ──
    try:
        from plastic_promise.mcp.tools.memory import handle_memory_store
        core_count = len(context_pack.get("core", [])) if context_pack else 0
        principle_names = ", ".join(p.get("name", "?") for p in principles[:5])
        content = (
            f"[AUTO INJECT] {task_description}\n"
            f"core_items: {core_count}\n"
            f"activated_principles: {principle_names}"
        )
        tags = [
            "auto_inject",
            f"source:{source}",
            f"skill:{skill_name}",
            "task:done",
        ]
        if entity_id:
            tags.append(f"entity:{entity_id}")
        store_result = await handle_memory_store(engine, {
            "content": content,
            "memory_type": "experience",
            "source": "auto_inject",
            "entity_ids": [entity_id] if entity_id else [],
            "tags": tags,
        })
        store_data = json.loads(store_result[0].text)
        inject_memory_id = store_data.get("memory_id")
    except Exception as e:
        errors.append(f"memory_store: {e}")

    # ── Step 4: skill_session_complete — auto-complete (inject is instant) ──
    if entity_id:
        try:
            from plastic_promise.mcp.tools.skill_tracking import handle_skill_session_complete
            await handle_skill_session_complete(engine, {
                "entity_id": entity_id,
                "outcome": "注入完成",
                "artifacts": [],
            })
        except Exception as e:
            errors.append(f"skill_session_complete: {e}")

    # ── Build response ──
    response = {
        "entity_id": entity_id,
        "skill_name": skill_name,
        "context_pack": context_pack,
        "principles": principles,
        "inject_memory_id": inject_memory_id,
        "errors": errors if errors else None,
        "partial": len(errors) > 0,
    }

    return [TextContent(type="text", text=json.dumps(response, ensure_ascii=False, indent=2))]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auto_context_inject.py -v`
Expected: 6 PASSED

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/mcp/tools/context.py tests/test_auto_context_inject.py
git commit -m "feat: auto_context_inject handler — unified context injection with self-feedback loop"
```

---

### Task 3: server.py — Register auto_context_inject tool

**Files:**
- Modify: `plastic_promise/mcp/server.py`

**Interfaces:**
- Registers: `auto_context_inject` in list_tools() and call_tool()

- [ ] **Step 1: Add tool declaration to list_tools()**

In `list_tools()`, after the context domain tools block, add:

```python
        Tool(
            name="auto_context_inject",
            description="统一自动化上下文注入：skill_session_start → SoulLoop.pre_task_v2 → memory_store → skill_session_complete。覆盖 Pi Agent/Claude Code/SoulBridge 三条路径。",
            inputSchema={
                "type": "object",
                "required": ["task_description"],
                "properties": {
                    "task_description": {"type": "string", "description": "当前任务描述"},
                    "task_type": {"type": "string", "description": "任务类型 (默认 general)"},
                    "source": {"type": "string", "description": "来源: pi_agent/claude_code/manual"},
                    "scope": {"type": "string", "description": "检索范围 (默认 global). Claude Code Hook 传入 agent:claude"},
                },
            },
        ),
```

- [ ] **Step 2: Add routing to call_tool()**

After the existing context domain routing, add:

```python
        elif name == "auto_context_inject":
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            return await handle_auto_context_inject(engine, arguments)
```

- [ ] **Step 3: Verify server loads and tool count**

Run: `python -c "import asyncio; from plastic_promise.mcp.server import list_tools; tools = asyncio.run(list_tools()); print(f'Total tools: {len(tools)}'); assert len(tools) == 34, f'Expected 34, got {len(tools)}'; print('OK')"`
Expected: `Total tools: 34` then `OK`

- [ ] **Step 4: Run all tests to verify no regression**

Run: `python -m pytest tests/test_auto_context_inject.py tests/test_skill_tracking.py -v`
Expected: 18 PASSED (6 new + 12 existing)

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/mcp/server.py
git commit -m "feat: register auto_context_inject — 34th MCP tool, unified injection entry point"
```

---

### Task 4: soul_bridge.py — pre_task() via handler

**Files:**
- Modify: `bridge/soul_bridge.py`

**Interfaces:**
- Modifies: `SoulBridge.pre_task()` to call handler instead of SoulLoop directly

- [ ] **Step 1: Replace pre_task() implementation**

In `soul_bridge.py`, replace the existing `pre_task()` method body (lines 83-151) with:

```python
    async def pre_task(self, task: str, task_type: str = "general") -> Dict[str, Any]:
        """任务执行前管线 — 统一走 auto_context_inject handler。
        
        Returns:
            {ok, blocked, block_reason, context, scarf, trust, layer}
        """
        result: Dict[str, Any] = {
            "ok": True, "blocked": False, "block_reason": None,
            "context": None, "scarf": None, "trust": 0.60, "layer": None,
        }

        if not self._init_modules():
            return result

        # 1. Trust check (unchanged)
        trust_score = self._trust.get()
        result["trust"] = round(trust_score, 2)
        if trust_score < 0.4:
            result["ok"] = False
            result["blocked"] = True
            result["block_reason"] = f"Trust too low ({trust_score:.2f} < 0.40)"
            result["layer"] = "L1"
            return result

        # 2. Defense pre_check (unchanged)
        try:
            defense = self._enforcer.pre_check(task, task_type)
            if defense.get("blocked"):
                result["ok"] = False
                result["blocked"] = True
                result["block_reason"] = defense.get("reason", "Defense blocked")
                result["layer"] = defense.get("layer", "L0")
                return result
        except Exception:
            pass

        # 3. SCARF self-reflection (unchanged)
        try:
            scarf = self._scarf.reflect(task)
            result["scarf"] = scarf
        except Exception:
            result["scarf"] = None

        # 4. Unified context injection via auto_context_inject handler
        try:
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            
            inject_result = await handle_auto_context_inject(self._engine, {
                "task_description": task,
                "task_type": task_type,
                "source": "pi_agent",
            })
            data = json.loads(inject_result[0].text)
            # Backward compatibility: return context_pack dict (not ContextPack object)
            # Existing callers (neko_adapter.py) expect a dict with "summary" key
            if data.get("context_pack"):
                result["context"] = {
                    "summary": str(data["context_pack"])[:200],
                    "inject_memory_id": data.get("inject_memory_id"),
                }
        except Exception:
            result["context"] = None

        return result
```

Note: the old code called `self._soul_loop.pre_task_v2(task, task_type)` which returned a `ContextPack` object. The new code calls `handle_auto_context_inject` which returns a dict. The `result["context"]` field is now `{"summary": str, "inject_memory_id": str}` — backward compatible because existing callers access `result["context"]["summary"]` for display purposes.

- [ ] **Step 2: Verify SoulBridge imports cleanly**

Run: `python -c "from bridge.soul_bridge import SoulBridge; b = SoulBridge(); print('SoulBridge loaded OK')"`
Expected: `SoulBridge loaded OK`

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/test_auto_context_inject.py tests/test_skill_tracking.py -v`
Expected: 15 PASSED

- [ ] **Step 4: Commit**

```bash
git add bridge/soul_bridge.py
git commit -m "refactor: SoulBridge.pre_task() → auto_context_inject handler (unified injection)"
```

---

### Task 5: pi_daemon.py — inject context before execute_task()

**Files:**
- Modify: `pi_daemon.py`

**Interfaces:**
- Adds: `inject_context()` function called before `execute_task()`

- [ ] **Step 1: Add inject_context function and call site**

In `pi_daemon.py`, add the helper function:

```python
async def inject_context(task_content: str, domain: str, role: str = "", mcp_client=None) -> dict | None:
    """Call auto_context_inject via MCP before task execution.
    
    Args:
        task_content: Task description
        domain: Agent domain (building/fixing/reflecting)
        role: Agent role (pi_builder/pi_fixer/pi_reviewer) — included in source for traceability
    """
    try:
        from plastic_promise.core.constants import DOMAIN_TO_TASK_TYPE
        task_type = DOMAIN_TO_TASK_TYPE.get(domain, "general")
        source = f"pi_agent:{role}" if role else "pi_agent"
        
        if mcp_client:
            # Path A: Pi Daemon has an MCP SSE client
            result = await mcp_client.call_tool("auto_context_inject", {
                "task_description": task_content,
                "task_type": task_type,
                "source": source,
            })
            return json.loads(result[0].text) if result else None
        else:
            # Path B: Direct Python call (fallback for testing)
            from plastic_promise.mcp.tools.context import handle_auto_context_inject
            from plastic_promise.core.context_engine import ContextEngine
            engine = ContextEngine()
            result = await handle_auto_context_inject(engine, {
                "task_description": task_content,
                "task_type": task_type,
                "source": source,
            })
            return json.loads(result[0].text)
    except Exception:
        return None  # Graceful degradation
```

Then in the `_run_and_finish` function or main poll loop, add before `execute_task()`:

```python
# Inject context (non-blocking, graceful degradation)
# role is passed so source becomes "pi_agent:builder" / "pi_agent:reviewer" etc.
await inject_context(task_content, domain, role=role, mcp_client=mcp_client)
```

- [ ] **Step 2: Verify pi_daemon.py imports cleanly**

Run: `python -c "import pi_daemon; print('pi_daemon loaded OK')"`
Expected: `pi_daemon loaded OK`

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/test_auto_context_inject.py tests/test_skill_tracking.py -v`
Expected: 15 PASSED

- [ ] **Step 4: Commit**

```bash
git add pi_daemon.py
git commit -m "feat: pi_daemon — auto_context_inject before execute_task()"
```

---

### Task 6: hooks + CLAUDE.md — Claude Code integration

**Files:**
- Modify: `hooks/session-start`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add auto_context_inject call to session-start hook**

In `hooks/session-start`, after the existing SuperPowers bootstrap code, append:

```bash
# ── Auto Context Inject (Plastic Promise unified entry point) ──
# scope: "agent:claude" prevents memory pollution between Pi Agent and Claude Code
claude mcp call plastic-promise auto_context_inject \
  '{"task_description":"会话启动","task_type":"general","scope":"agent:claude","source":"claude_code"}' \
  2>/dev/null || true
```

- [ ] **Step 2: Simplify CLAUDE.md startup sequence**

In `CLAUDE.md`, replace the current 5-step startup section:

```markdown
## 会话启动

每次会话开始，依次执行：

1. `auto_context_inject(task_description="<当前任务>", scope="agent:claude", source="claude_code")` — 统一上下文注入（含原则激活 + 记忆召回 + 实体追踪 + 注入沉淀）
2. `system(action="stats")` — 检查记忆池健康度 + 流水线状态
3. `defense(action="get")` — 信任分 + 防线状态

> auto_context_inject 替代了原有的 principle_activate + memory_recall + memory_store 三步手动调用，同时额外完成 skill_session 追踪和注入记录沉淀。
```

- [ ] **Step 3: Verify changes are syntactically valid**

Run: `python -c "with open('CLAUDE.md', 'r') as f: content = f.read(); print('CLAUDE.md:', len(content.splitlines()), 'lines')"`
Expected: line count should be similar to before (~3 lines shorter due to simplification)

- [ ] **Step 4: Commit**

```bash
git add hooks/session-start CLAUDE.md
git commit -m "feat: Claude Code — auto_context_inject in hook + simplified startup (5→3 steps)"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/test_auto_context_inject.py tests/test_skill_tracking.py tests/test_quality_gate.py tests/test_decay_engine.py tests/test_lancedb_store.py -v`
Expected: 66 PASSED (6 + 12 + 27 + 15 + 6)

- [ ] **Step 2: Verify tool count and uniqueness**

Run: `python -c "import asyncio; from plastic_promise.mcp.server import list_tools; tools = asyncio.run(list_tools()); names = [t.name for t in tools]; print(f'Total: {len(tools)}'); assert len(tools) == 34, f'Expected 34, got {len(tools)}'; assert 'auto_context_inject' in names; print('All checks passed')"`
Expected: `All checks passed`

- [ ] **Step 3: Update GOAL.md**

Add to the "已完成 (2026-07-01)" section:

```markdown
- **Auto Context Inject**: 统一自动化上下文注入 — 1 个 MCP 工具 (auto_context_inject) + SoulBridge/Pi Daemon/Claude Hook 三路径统一 + 自反馈循环 + CLAUDE.md 启动序列简化 (5→3 步)。MCP 工具总数: 34。
```

- [ ] **Step 4: Commit**

```bash
git add GOAL.md
git commit -m "docs: GOAL.md — Auto Context Inject complete (34 MCP tools, 3-path unified injection)"
```

---

### Task Summary

| # | Task | Files | Steps | Dependencies |
|---|------|-------|-------|-------------|
| 1 | auto_inject prefix support | `skill_tracking.py` | 3 | None |
| 2 | handle_auto_context_inject + tests | `context.py`, `test_auto_context_inject.py` | 5 | Task 1 |
| 3 | server registration | `server.py` | 5 | Task 2 |
| 4 | SoulBridge integration | `soul_bridge.py` | 4 | Task 2 |
| 5 | Pi Daemon integration | `pi_daemon.py` | 4 | Task 2 |
| 6 | Hook + CLAUDE.md | `hooks/session-start`, `CLAUDE.md` | 4 | None |
| 7 | Final verification | `GOAL.md`, all tests | 4 | Tasks 1-6 |
