# Codebase Memory MCP — Optional Plugin Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate codebase-memory-mcp as an optional plugin — zero impact on core users, one env flag to enable for power users.

**Architecture:** A thin `CodebaseMemoryBridge` wraps CLI calls to the binary. When `PP_ENABLE_CODE_MEMORY=1`, `context_supply` injects code graph insights. CLAUDE.md subagent dispatch gains a pre-flight impact analysis step.

**Tech Stack:** Python 3.13, subprocess CLI, codebase-memory-mcp >= 0.7.0

## Global Constraints

- `pip install plastic-promise` does NOT install codebase-memory-mcp — it's behind `[code-memory]` extra
- All code_memory code paths wrapped in `try/except: pass` — never block core functionality
- `PP_ENABLE_CODE_MEMORY=1` is the single on/off switch
- Default is off (`"0"`) — existing users see zero change
- Graceful degradation: binary missing → log warning → return empty results
- All existing tests pass unchanged
- No new dependencies in core requirements.txt

---

## File Structure

| File | Role |
|------|------|
| `pyproject.toml` | **Modify** — add `[project.optional-dependencies] code-memory` group |
| `plastic_promise/code_context/__init__.py` | **Create** — module docstring, public exports |
| `plastic_promise/code_context/bridge.py` | **Create** — `CodebaseMemoryBridge` CLI wrapper |
| `plastic_promise/core/context_engine.py` | **Modify** — `_code_memory_enabled` flag in `__init__` |
| `plastic_promise/core/context_engine.py` | **Modify** — `_inject_code_context()` method |
| `plastic_promise/mcp/tools/context.py` | **Modify** — wire into `context_supply` response |
| `CLAUDE.md` | **Modify** — add Step 0 impact analysis to dispatch protocol |

---

### Task 1: Optional dependency + env flag

**Files:**
- Modify: `pyproject.toml` (line 54, after `rust` group)
- Modify: `plastic_promise/core/context_engine.py` (line ~250, after `_rust_lock` init)

**Interfaces:**
- Produces: `[project.optional-dependencies] code-memory` group in pyproject.toml
- Produces: `self._code_memory_enabled: bool` on ContextEngine instances

- [ ] **Step 1: Add optional dependency group to pyproject.toml**

In `F:/Agent/Memory system/pyproject.toml`, after the `rust` group (line 54-56):

```toml
code-memory = [
    "codebase-memory-mcp>=0.7.0",
]
```

Full context — the section should read:
```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.0",
    "ruff>=0.3.0",
    "mypy>=1.8",
    "pre-commit>=3.6",
]
rust = [
    "maturin>=1.4",
]
code-memory = [
    "codebase-memory-mcp>=0.7.0",
]
```

- [ ] **Step 2: Verify pyproject.toml syntax**

```bash
python -c "import tomllib; tomllib.load(open('F:/Agent/Memory system/pyproject.toml', 'rb')); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Add env flag to ContextEngine.__init__**

In `F:/Agent/Memory system/.claude/worktrees/fix+data-quality-chain/plastic_promise/core/context_engine.py`, in `__init__`, after the `_rust_lock` initialization (around line 241):

```python
# Code Memory — optional codebase-memory-mcp integration
self._code_memory_enabled: bool = os.environ.get("PP_ENABLE_CODE_MEMORY", "0") == "1"
```

- [ ] **Step 4: Verify flag is accessible**

```bash
python -c "from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(use_sqlite=False); print(f'code_memory_enabled={e._code_memory_enabled}')"
```

Expected: `code_memory_enabled=False`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml plastic_promise/core/context_engine.py
git commit -m "feat(code-context): add code-memory optional dep + PP_ENABLE_CODE_MEMORY flag"
```

---

### Task 2: CodebaseMemoryBridge — CLI wrapper

**Files:**
- Create: `plastic_promise/code_context/__init__.py`
- Create: `plastic_promise/code_context/bridge.py`

**Interfaces:**
- Produces: `CodebaseMemoryBridge` class with three methods
- `trace_downstream(function_name: str, depth: int = 3) -> List[dict]`
- `detect_changes() -> List[dict]`
- `search_related(name_pattern: str) -> List[dict]`
- All methods return `[]` on any failure (binary missing, project not indexed, etc.)

- [ ] **Step 1: Create module init**

Create `F:/Agent/Memory system/.claude/worktrees/fix+data-quality-chain/plastic_promise/code_context/__init__.py`:

```python
"""Code Context — optional codebase-memory-mcp integration.

Enabled via:  PP_ENABLE_CODE_MEMORY=1
Install:      pip install plastic-promise[code-memory]
Index:        codebase-memory-mcp cli index_repository '{"path":"."}'

Provides:
  - Impact analysis before modifying functions (trace_downstream)
  - Change blast-radius detection (detect_changes)
  - Pattern-based code search (search_related)

Graceful degradation: if the binary is not installed or the project
is not indexed, all methods return empty results and log warnings.
Never blocks core pipeline.
"""

from plastic_promise.code_context.bridge import CodebaseMemoryBridge

__all__ = ["CodebaseMemoryBridge"]
```

- [ ] **Step 2: Create bridge.py**

Create `F:/Agent/Memory system/.claude/worktrees/fix+data-quality-chain/plastic_promise/code_context/bridge.py`:

```python
"""Thin CLI wrapper around codebase-memory-mcp binary.

All public methods catch every exception and return [] on failure.
The binary is assumed to be on PATH after `pip install codebase-memory-mcp`.
"""

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("plastic-promise.code-context")

_BINARY = "codebase-memory-mcp"


class CodebaseMemoryBridge:
    """Wrapper around codebase-memory-mcp CLI for code graph queries.

    None of the methods raise exceptions — every failure degrades
    gracefully to an empty result list + log warning.
    """

    def trace_downstream(
        self, function_name: str, depth: int = 3
    ) -> List[Dict[str, Any]]:
        """Find all callers and callees of a function.

        Uses trace_path to perform BFS traversal up to `depth` hops.
        Returns a list of nodes with their relationships.
        """
        return self._cli("trace_path", {
            "from_name": function_name,
            "direction": "downstream",
            "depth": depth,
        })

    def detect_changes(self) -> List[Dict[str, Any]]:
        """Analyze uncommitted git diff for blast radius.

        Maps changed symbols to affected callers with risk classification.
        Returns empty list if working tree is clean.
        """
        return self._cli("detect_changes", {})

    def search_related(self, name_pattern: str) -> List[Dict[str, Any]]:
        """Search for functions/classes matching a name pattern.

        Uses search_graph with the given pattern. Useful for finding
        similar implementations or all consumers of an interface.
        """
        return self._cli("search_graph", {
            "name_pattern": name_pattern,
            "limit": 20,
        })

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cli(self, tool: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Execute a codebase-memory-mcp CLI command and parse JSON output.

        Returns [] on any failure — binary missing, project not indexed,
        invalid args, parse error, timeout, etc.
        """
        try:
            result = subprocess.run(
                [_BINARY, "cli", tool, json.dumps(args)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()[:200]
                logger.debug(
                    "codebase-memory-mcp %s failed (exit %d): %s",
                    tool, result.returncode, stderr,
                )
                return []
            if not result.stdout.strip():
                return []
            return json.loads(result.stdout)
        except FileNotFoundError:
            logger.debug(
                "codebase-memory-mcp binary not found — "
                "install with: pip install plastic-promise[code-memory]"
            )
            return []
        except subprocess.TimeoutExpired:
            logger.warning("codebase-memory-mcp %s timed out after 30s", tool)
            return []
        except json.JSONDecodeError as e:
            logger.warning("codebase-memory-mcp %s returned invalid JSON: %s", tool, e)
            return []
        except Exception as e:
            logger.warning("codebase-memory-mcp %s unexpected error: %s", tool, e)
            return []
```

- [ ] **Step 3: Verify bridge import works (without binary)**

```bash
python -c "from plastic_promise.code_context import CodebaseMemoryBridge; b=CodebaseMemoryBridge(); r=b.trace_downstream('test_func'); print(f'Without binary: {r}'); assert r==[]; print('OK')"
```

Expected: `Without binary: []` followed by `OK` (graceful degradation).

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/code_context/
git commit -m "feat(code-context): add CodebaseMemoryBridge CLI wrapper with graceful degradation"
```

---

### Task 3: context_supply integration

**Files:**
- Modify: `plastic_promise/core/context_engine.py` (add `_inject_code_context` method)
- Modify: `plastic_promise/mcp/tools/context.py` (wire into response)

**Interfaces:**
- Consumes: `self._code_memory_enabled` (from Task 1), `CodebaseMemoryBridge` (from Task 2)
- Produces: `_inject_code_context(task_description) -> List[dict]` method on ContextEngine
- Produces: `🟣 Code Context` section in context_supply MCP response

- [ ] **Step 1: Add _inject_code_context() to ContextEngine**

In `F:/Agent/Memory system/.claude/worktrees/fix+data-quality-chain/plastic_promise/core/context_engine.py`, add after `check_rust_health()` public wrapper (around line 1425):

```python
    def _inject_code_context(self, task_description: str) -> list:
        """Inject code graph insights into the context pack.

        Called automatically by context_supply when PP_ENABLE_CODE_MEMORY=1.
        Returns a list of dicts with code structure insights relevant to
        the task. Gracefully returns [] if bridge unavailable.
        """
        if not getattr(self, '_code_memory_enabled', False):
            return []
        try:
            from plastic_promise.code_context.bridge import CodebaseMemoryBridge
            bridge = CodebaseMemoryBridge()
            # Try to find relevant functions by name pattern
            # Extract potential function names from task description
            words = [w for w in task_description.split() if len(w) >= 3 and w[0].isalpha()]
            insights = []
            for word in words[:5]:  # limit to first 5 candidate words
                results = bridge.trace_downstream(word, depth=2)
                if results:
                    insights.extend(results)
            return insights[:20]  # cap at 20 insights
        except Exception:
            return []
```

- [ ] **Step 2: Wire into context_supply MCP handler**

In `F:/Agent/Memory system/.claude/worktrees/fix+data-quality-chain/plastic_promise/mcp/tools/context.py`, in the `handle_context_supply` function, after the existing context pack is built (after the `context_pack` dict is assembled, around line 245):

```python
        # ── 🟣 Code Context (optional — codebase-memory-mcp) ──
        try:
            code_insights = engine._inject_code_context(task_description)
            if code_insights:
                context_pack["code_context"] = code_insights
        except Exception:
            pass  # optional plugin — never block
```

- [ ] **Step 3: Verify with flag off (default)**

```bash
python -c "
from plastic_promise.core.context_engine import ContextEngine
e = ContextEngine(use_sqlite=False)
result = e._inject_code_context('implement new feature')
print(f'Default (off): {result}')
assert result == []
print('OK')
"
```

Expected: `Default (off): []` → `OK`

- [ ] **Step 4: Verify existing tests unchanged**

```bash
python -m pytest tests/test_boundary.py tests/test_rust_integration.py -v -q
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/context_engine.py plastic_promise/mcp/tools/context.py
git commit -m "feat(code-context): wire CodebaseMemoryBridge into context_supply pipeline"
```

---

### Task 4: CLAUDE.md — Impact analysis dispatch step

**Files:**
- Modify: `CLAUDE.md` (subagent dispatch section)

- [ ] **Step 1: Add Step 0 before the existing dispatch protocol**

In `F:/Agent/Memory system/CLAUDE.md`, find the section "## 子 Agent 派发协议". Before the existing 3-step protocol, add:

```markdown
### Step 0: Impact Analysis (if PP_ENABLE_CODE_MEMORY=1)

派发任何修改代码的子 Agent 前，执行：

```
python -c "
from plastic_promise.code_context.bridge import CodebaseMemoryBridge
b = CodebaseMemoryBridge()

# 1. 查目标函数的下游消费者
consumers = b.trace_downstream('<目标函数名>', depth=3)
if consumers:
    print('## Consumers of this interface')
    for c in consumers[:10]:
        print(f'  - {c}')

# 2. 如果有未提交改动，检测影响范围
changes = b.detect_changes()
if changes:
    print('## Change blast radius')
    for c in changes[:10]:
        print(f'  - {c}')
"
```

将输出写入 task brief 的 **"Consumers of this interface"** 段。

如果 `detect_changes()` 返回 HIGH 风险项，扩大 task scope 以包含受影响的消费者。
```

- [ ] **Step 2: Verify CLAUDE.md is valid markdown**

```bash
python -c "open('F:/Agent/Memory system/CLAUDE.md').read(); print('Readable')"
```

Expected: `Readable`

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(code-context): add Step 0 impact analysis to subagent dispatch protocol"
```

---

## Verification (after all tasks)

- [ ] **V1**: `python -c "import tomllib; t=tomllib.load(open('pyproject.toml','rb')); assert 'code-memory' in t['project']['optional-dependencies']; print('OK')"`
- [ ] **V2**: `python -c "from plastic_promise.code_context import CodebaseMemoryBridge; b=CodebaseMemoryBridge(); assert b.trace_downstream('test')==[]; print('OK')"`
- [ ] **V3**: `PP_ENABLE_CODE_MEMORY=0 python -c "from plastic_promise.core.context_engine import ContextEngine; e=ContextEngine(use_sqlite=False); assert e._inject_code_context('test')==[]; print('OK')"`
- [ ] **V4**: `python -m pytest tests/test_boundary.py tests/test_rust_integration.py -v` — all 7 pass
- [ ] **V5**: `python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py` — same 317/29 as baseline
- [ ] **V6**: `pip install -e ".[code-memory]"` — installs codebase-memory-mcp (or confirms it's optional)
