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

- [ ] **Step 1: Add auto_inject domain fallback and parent-skip logic**

In `plastic_promise/mcp/tools/skill_tracking.py`, find the `_store_skill_start` function (~line 406). Before the `domain = SKILL_DOMAIN_MAP.get(...)` line, add:

```python
    domain = SKILL_DOMAIN_MAP.get(skill_name, "general")
    # auto_inject:* prefix → "reflecting" domain (context audit snapshot)
    if skill_name.startswith("auto_inject:"):
        domain = "reflecting"
```

In `_validate_parent` (~line 357), add early return:

```python
def _validate_parent(skill_name: str, parent_entity_id: str | None, engine: Any) -> str | None:
    # auto_inject: sessions have no parent chain — skip validation
    if skill_name.startswith("auto_inject:"):
        return None
    if not parent_entity_id:
        return None
    ...
```

In `handle_skill_session_trace` orphan detection (~line that checks `orphan_active`), add skip:

```python
for s in sessions:
    # auto_inject: sessions are instant — skip orphan detection
    if s["skill_name"].startswith("auto_inject:"):
        continue
    # 1. Orphan active: ...
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_auto_context_inject.py -v`
Expected: FAIL — `ImportError: cannot import name 'handle_auto_context_inject'`

- [ ] **Step 3: Implement handle_auto_context_inject**

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
Expected: 3 PASSED

- [ ] **Step 5: Commit**

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
Expected: 15 PASSED (3 new + 12 existing)

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
            if data.get("context_pack"):
                result["context"] = {"summary": str(data["context_pack"])[:200]}
        except Exception:
            result["context"] = None

        return result
```

Note: the old code directly called `self._soul_loop.pre_task_v2(task, task_type)`. The new code calls `handle_auto_context_inject` which internally calls `SoulLoop.pre_task_v2()`, so the dependency on `self._soul_loop` can be removed or kept for backward compatibility.

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
async def inject_context(task_content: str, domain: str, mcp_client=None) -> dict | None:
    """Call auto_context_inject via MCP before task execution."""
    try:
        from plastic_promise.core.constants import DOMAIN_TO_TASK_TYPE
        task_type = DOMAIN_TO_TASK_TYPE.get(domain, "general")
        
        if mcp_client:
            # Path A: Pi Daemon has an MCP SSE client
            result = await mcp_client.call_tool("auto_context_inject", {
                "task_description": task_content,
                "task_type": task_type,
                "source": "pi_agent",
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
                "source": "pi_agent",
            })
            return json.loads(result[0].text)
    except Exception:
        return None  # Graceful degradation
```

Then in the `_run_and_finish` function or main poll loop, add before `execute_task()`:

```python
# Inject context (non-blocking, graceful degradation)
await inject_context(task_content, domain, mcp_client)
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
Expected: 63 PASSED (3 + 12 + 27 + 15 + 6)

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
