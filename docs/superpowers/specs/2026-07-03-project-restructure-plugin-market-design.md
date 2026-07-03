# Project Restructure + Plugin Market — Design Spec

**Date**: 2026-07-03
**Status**: Design — approved
**Scope**: Directory restructuring, Pack v3 market, Extension Points system, Code Memory plugin integration

## Context

Plastic Promise started as a memory system for MCP, grew into a multi-agent governance framework. Current directory structure reflects accretion, not intention. Meanwhile, the codebase-memory-mcp optional plugin (PP_ENABLE_CODE_MEMORY=1) was designed but only 1/4 implemented. These two problems share a root cause: no extension architecture. This design addresses both.

## Design Principles

1. **Extension points, not adapters** — Define protocols once; plugins implement them. Never write per-plugin glue code.
2. **Pack is the universal distribution unit** — Memories, skills, and capabilities all travel through the same pack market.
3. **Graceful degradation everywhere** — Missing plugin, broken binary, unindexed project → silent empty result, never crash.
4. **Core ships lean** — `pip install plastic-promise` gives engine + built-in skills only. Everything else via `market install`.

---

## Part A: Directory Restructuring

### Current Problems

- Runtime artifacts pollute root: `plastic_memory.db`, `*.pid`, `*.heartbeat`, `*.log`, `*.dll`, `*.pyd`
- Skills duplicated across `.agents/skills/`, `.pi/skills/`, `.trae/skills/` (physical copies)
- Unclear-purpose directories: `bridge/` (mixed Python/TS), `claude/` (one file), `tools/` (2 scripts), `utils/` (5 modules), `memory/` (markdown files)
- `.gitignore` incomplete — `maintenance_daemon.pid`, `*.heartbeat`, `init_and_start.log` not covered

### Target Structure

```
plastic-promise/
├── plastic_promise/          # Main package — all Python source
│   ├── core/                 # context_engine, decay, quality_gate, domain_manager
│   ├── memory/               # soul_memory, pipeline, memory_gc
│   ├── mcp/                  # MCP server + tool handlers
│   │   └── tools/
│   ├── skills/               # SkillEngine + superpowers_stages
│   │   └── prompts/          # SuperPowers 14 skill prompts (from pack cache)
│   ├── principles/           # Principle engine
│   ├── reflection/           # SCARF + feedback
│   ├── defense/              # Defense + trust scores
│   ├── loop/                 # SoulLoop + step-closure
│   ├── growth/               # Hormones + classifiers
│   ├── launcher/             # Service launcher
│   ├── extensions/           # ★ Extension Points (NEW)
│   │   ├── __init__.py       #    Protocol definitions
│   │   ├── loader.py         #    PluginLoader — discover, validate, activate
│   │   └── registry.py       #    Market index (local + remote)
│   └── lib/                  # Embedded binaries
│       ├── context_engine_core.dll
│       └── context_engine_core.pyd
│
├── daemons/                  # Daemon processes
│   └── maintenance_daemon.py
│
├── scripts/                  # DevOps / one-shot scripts
│   ├── bootstrap.py
│   ├── init_and_start.py
│   ├── rebuild_lancedb.py
│   ├── repair_zero_vectors.py
│   ├── system_audit.py
│   ├── eco.py
│   └── start-all.sh
│
├── docs/                     # All documentation
│   ├── architecture/
│   ├── superpowers/
│   │   ├── specs/
│   │   └── plans/
│   └── GOAL.md               # moved from root
│
├── data/                     # Runtime data (gitignored)
│   ├── db/                   # plastic_memory.db + WAL/SHM
│   └── lancedb/              # plastic_memory.lancedb/
│
├── var/                      # Runtime temp files (gitignored)
│   ├── run/                  # *.pid, *.heartbeat
│   └── log/                  # *.log, step_audit_log.jsonl
│
├── skills/                   # Dev-time skill references (symlinks to pack cache)
├── plugins/                  # Installed third-party plugin cache
├── rust/                     # Rust extension (independent cargo workspace)
├── tests/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
├── Makefile
└── .gitignore
```

### Removed / Merged Directories

| Directory | Destination | Reason |
|-----------|------------|--------|
| `bridge/` | Python → `plastic_promise/core/`, TS → deleted | Mixed-language dead code; only `neko_adapter.py` has test references |
| `claude/` | `plugin.json` → `.claude/` | Single config file doesn't need a directory |
| `memory/` | markdown files → `var/memory_files/` or DB-managed | Not source code; runtime memory artifacts |
| `tools/` | scripts → `scripts/` | `perf_check.py`, `verify_merge.py` are DevOps scripts |
| `utils/` | modules → `plastic_promise/core/` | `date_helpers`, `logger`, `math_helpers`, `retry`, `validator` are core utilities |
| `.agents/skills/` | → `skills/` (symlinks) | Eliminate physical duplication |
| `.pi/skills/` | → `skills/` (symlinks) | Eliminate physical duplication |
| `.trae/skills/` | → `skills/` (symlinks) | Eliminate physical duplication |

### Impact Analysis

| Scope | Count | Change |
|-------|-------|--------|
| Python import paths | ~60 files | **No change** — `plastic_promise/` stays at root |
| Config file paths | ~5 files | Update DB/log/PID paths to `data/` and `var/` |
| `.gitignore` | 1 file | Add `maintenance_daemon.*`, `*.heartbeat`, `init_and_start.log`, `var/`, `plugins/` |
| `pyproject.toml` | 1 file | Add `[project.scripts]` entry points |
| Tests | ~317 | Run full suite; no import changes needed |

---

## Part B: Pack v3 + Extension Points + Market

### Pack v3 Format (`pack.yml`)

```yaml
name: superpowers-core
version: 2.0.0
type: workflow              # knowledge | workflow | capability | adapter
description: SuperPowers 12-stage orchestration flow
author: plastic-promise

# ── workflow type fields ──
workflow_mode: strict           # strict | advisory
                                # strict: session-init → brainstorming
                                #   MUST run before any code-modifying action
                                # advisory: skills are suggested but skippable
skills:
  brainstorming: !include prompts/brainstorming.md
  writing-plans: !include prompts/writing-plans.md
  # ... all 14 skills

chain:
  brainstorming: [using-git-worktrees]
  using-git-worktrees: [writing-plans]
  writing-plans: [executing-plans, subagent-driven-development]
  # ...

# ── capability type fields ──
extension_points:
  hook:
    slots: [on_before_dispatch, on_transition_write_execute]
  tool:
    provides: [git_blame]
  # Other extension points: embedder, storage, notifier, ...

# ── optional install dependency ──
install:
  pip: codebase-memory-mcp>=0.7.0
```

### Three Pack Types → One Entry → Respective Engines

```
market install <name>
       │
       ▼
  MarketRegistry.lookup(name)
       │
       ├─ type: knowledge   → pack_import_with_strategy() → Memory pool
       ├─ type: workflow    → SkillEngine.register()       → skill_resolve available
       ├─ type: capability  → PluginLoader.activate()      → Hooks/Tools/Storage live
       └─ type: adapter     → PlatformBridge.activate()    → /market install for target
```

### Pack v3 Format (updated with security + versioning)

```yaml
name: code-memory
version: 1.0.0
type: capability              # knowledge | workflow | capability
min_core_version: "0.1.0"    # ★ minimum plastic_promise version required
description: Code graph analysis
author: plastic-promise

# ── capability type: hooks ──
hooks:                        # ★ declares what this plugin provides
  on_before_dispatch:
    method: mcp               # mcp | cli | python
    # For mcp: PluginLoader spawns MCP server subprocess via stdio
    # For cli: subprocess.run(command, json.dumps(payload))
    # For python: import module, call execute(context)

# ── capability type: tools ──
tools:
  provides: [trace_path, detect_changes, search_graph]

# ── capability type: replaces core components ──
replaces:                     # optional — at most one plugin per replace target
  embedder: ollama-mxbai
  # storage: postgres-vector

install:
  pip: codebase-memory-mcp>=0.7.0
```

### Extension Point Protocols

Defined in `plastic_promise/extensions/__init__.py`:

```python
from typing import Protocol, Any

class HookProvider(Protocol):
    """Workflow hooks — execute at named points in the SuperPowers pipeline."""
    slots: list[str]
    def execute(self, slot: str, context: dict) -> dict: ...

class ToolProvider(Protocol):
    """Register new MCP tools via MCP stdio subprocess."""
    tools: list[dict]          # MCP tool schemas
    def handle(self, tool_name: str, args: dict) -> Any: ...

class EmbedderProvider(Protocol):
    """Replace the embedding backend."""
    def embed(self, text: str) -> list[float]: ...
    def batch_embed(self, texts: list[str]) -> list[list[float]]: ...

class StorageProvider(Protocol):
    """Replace the vector/record storage backend."""
    def store(self, record: dict) -> str: ...
    def query(self, vec: list[float], top_k: int) -> list[dict]: ...

class NotifierProvider(Protocol):
    """Event notifications (slack, email, webhook, etc.)."""
    channels: list[str]
    def send(self, channel: str, message: str) -> None: ...

class DispatchProvider(Protocol):
    """Subagent orchestration — spawn independent agents at workflow nodes.

    Used by workflow packs to implement subagent-driven development:
    the plugin declares a dispatch point, and PluginLoader spawns a
    fresh subagent with injected context at that point.
    """
    dispatch_points: list[str]     # slot names where subagents launch
    def spawn(self, task: dict) -> dict:
        """Spawn a subagent for the given task.

        Args:
            task: dict with keys — description, context (memories+principles),
                  schema (optional JSON Schema for structured output)

        Returns:
            dict with subagent result or error.
        """
        ...
```

### Platform Adapter (type: adapter)

```yaml
# pack.yml for a cross-platform adapter
name: cursor-adapter
version: 1.0.0
type: adapter
description: Plastic Promise integration for Cursor IDE
min_core_version: "0.1.0"
author: community-contributor

adapter:
  target: cursor              # target platform identifier
  commands:                   # maps market commands to platform-native syntax
    market_list: "/plastic-promise market list"
    market_install: "/plastic-promise market install {name}"
    session_init: "/plastic-promise session-init"
  hooks:                      # platform-specific hook scripts
    startup: cursor_hook.sh   # injects using-superpowers at IDE startup
```

Community members can contribute `cursor-adapter`, `copilot-adapter`, `kimi-adapter` etc. Each adapter is a small pack (no code execution) that maps our MCP tools to the target platform's native command syntax.

### PluginLoader Security Model

```python
# PluginLoader validates BEFORE any code execution:

def _validate_plugin(self, plugin_class) -> bool:
    """Static check — no instantiation. No RCE surface."""
    # 1. issubclass check (does not call __init__)
    if not issubclass(plugin_class, HookProvider):
        return False

    # 2. Trust score gate
    trust = defense(action="get")
    if trust < 0.35:
        return False  # below D-tier, plugins disabled

    # 3. min_core_version check
    if pack.min_core_version:
        if not self._version_satisfies(pack.min_core_version):
            return False  # engine too old

    # 4. Source trust
    if pack.source == "community" and trust < 0.50:
        return False  # community plugins require B-tier+

    return True
```

### Slot Names — Complete Enumeration

Derived from SuperPowers pipeline stages. All are `on_before_<stage>`, `on_after_<stage>`, or `on_transition_<from>_<to>`.

```
session-init:
  on_before_session_init
  on_after_session_init

brainstorming:
  on_before_brainstorming
  on_after_brainstorming
  on_transition_brainstorm_research      # → exemplar-research

exemplar-research:
  on_before_exemplar_research
  on_after_exemplar_research
  on_transition_research_worktrees       # → using-git-worktrees

using-git-worktrees:
  on_before_git_worktrees
  on_after_git_worktrees
  on_transition_worktrees_plans          # → writing-plans

writing-plans:
  on_before_writing_plans
  on_after_writing_plans
  on_transition_write_execute            # → executing-plans
  on_transition_write_subagent           # → subagent-driven-development

executing-plans / subagent-driven-development:
  on_before_dispatch                     # ★ Before subagent spawn
  on_after_dispatch                      # ★ After subagent returns

test-driven-development:
  on_after_verify                        # ★ After tests pass

finishing-a-development-branch:
  on_before_finish                       # ★ Before branch finalization
  on_after_finish
```

### SkillEngine Upgrade

`SkillEngine` becomes the single source of truth for skill prompts:

```python
# Before: prompts live in platform plugin caches
# After: prompts live in SkillEngine registry (from packs)

engine.resolve("brainstorming")
# → Returns full SKILL.md prompt content
# → Any Agent can query via MCP tool: skill_resolve(name)

# sp-stage no longer holds prompt content — it does:
#   1. Chain validation (via existing SKILL_CHAIN_MAP)
#   2. Execution tracking (skill_session_start/complete)
#   3. Plugin slot triggering (via PluginLoader)
```

### Chain Validator as Plugin Trigger

The existing chain validator (`plastic_promise/skills/superpowers_stages.py`) doubles as the slot dispatcher:

```
sp-stage("executing-plans", task_description)
  │
  ├─ 1. Chain validator: current=writing-plans, target=executing-plans ✓
  │
  ├─ 2. PluginLoader.get_hooks("on_before_executing")
  │     + PluginLoader.get_hooks("on_transition_write_execute")
  │     → Execute each HookProvider in order
  │     → Results appended to context_pack
  │
  └─ 3. Enter executing-plans stage
```

No new event bus. No plugin priority system. The validator already has all the state needed (from_stage, to_stage, task_description); plugins just read from that.

### Market

```bash
plastic-promise market list
# Available packs:
#   superpowers-core    workflow   ⭐official   SuperPowers 12-stage flow
#   code-memory         capability ⭐recommended DeusData · Code graph analysis
#   python-rules        knowledge  🌐community  Team Python coding standards

plastic-promise market install code-memory
# → Identifies type: capability
# → PluginLoader reads extension_points from pack.yml
# → pip install codebase-memory-mcp>=0.7.0 (optional)
# → HookProvider registered to on_before_dispatch
# → Done — zero core code changes
```

---

## Part C: Code Memory Plugin Relationship

### Boundary

```
DeusData/codebase-memory-mcp              plastic-promise
     (Independent project)                  (This project)
┌─────────────────────────┐           ┌─────────────────────────┐
│ MCP Server (stdio)      │◄──────────│ PluginLoader            │
│ 14 MCP tools            │  spawns   │   spawns MCP subprocess │
│ Own database            │  subprocess│   auto-discovers tools  │
│ Own release cycle       │           │   hooks: on_before_xxx  │
│ Own community           │           │                         │
└─────────────────────────┘           └─────────────────────────┘

We DO NOT own Code Memory code.
We DO NOT ship it in our wheel.
We DO NOT write adapter bridges.

We DO:
  - Maintain pack.yml (metadata + hooks declaration)
  - PluginLoader spawns codebase-memory-mcp as MCP stdio subprocess
  - Auto-discover its 14 MCP tools via tools/list
  - Register discovered tools into our MCP server's tool table
```

### User Experience

```bash
# Without Code Memory — zero impact
pip install plastic-promise
plastic-promise start
# → Full SuperPowers flow, no code graph features

# With Code Memory
plastic-promise market install code-memory
# → pip install codebase-memory-mcp
# → PluginLoader spawns MCP server subprocess via stdio
# → Auto-discovers 14 tools (trace_path, detect_changes, search_graph, ...)
# → Registers them as MCP tools
# → Hooks fire at on_before_dispatch automatically
```

### PluginLoader MCP Subprocess Management

```python
class McpSubprocessPlugin:
    """Manages a plugin that exposes its own MCP server via stdio."""

    def __init__(self, command: list[str]):
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def discover_tools(self) -> list[dict]:
        """Send tools/list via JSON-RPC, return tool schemas."""
        # Standard MCP protocol: JSON-RPC over stdio
        ...

    def call_tool(self, name: str, args: dict) -> Any:
        """Send tools/call via JSON-RPC, return result."""
        ...

    def shutdown(self):
        """Graceful shutdown of MCP subprocess."""
        ...
```

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `bridge/neko_adapter.py` removal breaks tests | Medium | `tests/test_neko_adapter.py` references it; audit before removal, either delete tests or extract adapter into `plastic_promise/` |
| `.gitignore` changes leave tracked runtime files | Low | `git rm --cached` for files now under `data/`/`var/` before moving |
| Pack v3 format breaks existing pack exports | Low | Backward-compatible reader; v2 packs import as `type: knowledge` (default) |
| PluginLoader import overhead at startup | Low | Lazy import — only load plugins when their extension point is invoked |
| `C:\Users\...\.claude\skills\` symlinks break on Windows | Medium | Keep `.claude/skills/` as-is for Claude Code; `skills/` at project root is a separate dev-time reference |

## Skill vs Plugin Boundary

| | SkillEngine | PluginLoader |
|---|---|---|
| **Pack type** | `type: workflow` | `type: capability` |
| **What it handles** | Workflow orchestration (prompts, chain, stages) | Capability extension (hooks, tools, storage, embedder, notifier) |
| **What it registers** | SkillDef → prompt lookup | HookProvider → slot dispatch, ToolProvider → MCP tools |
| **Entry point** | `skill_resolve(name)` | `trigger_hooks(slot, context)` |
| **Activation** | `SkillEngine.register_from_pack()` | `PluginLoader.activate()` |
| **Security** | None needed (prompts are passive text) | `issubclass` + trust gate + min_core_version |

**Rule**: `type: workflow` packs NEVER execute code. Only `type: capability` packs can. This means skills are safe by construction — a malicious `type: workflow` pack can have bad advice in its prompt, but it cannot run code.

## Developer Documentation (required)

Every pack must include or link to a `DEVELOPER.md` covering:
- Step-by-step guide to create a pack.yml
- How to implement each extension point Protocol
- How to test with `plastic-promise market install ./my-pack`
- Security: `issubclass` validation, trust score gating, min_core_version
- Example: Code Memory pack as reference implementation

## Non-Goals

- No plugin-to-plugin communication (plugins are independent)
- No hot-reload (discover once at startup)
- No dependency resolution between plugins
- No sandboxing beyond subprocess isolation
- No graphical marketplace UI (CLI + MCP tools only)

## Future Roadmap (not in this plan)

| Feature | Inspiration | Priority |
|---------|------------|----------|
| Team shared code graph (export index → commit to repo) | codebase-memory-mcp `export` | P2 |
| Cypher-like graph queries for architecture validation | codebase-memory-mcp `query_graph` | P2 |
| Single-binary plugin distribution (compile to static binary) | codebase-memory-mcp binary model | P3 |
| Infrastructure-as-code indexing (K8s, Dockerfile, Terraform) | codebase-memory-mcp parser | P3 |
| 11-platform auto-detection for adapter packs | codebase-memory-mcp `install --auto` | P2 |
| Skill behavior eval framework (validate workflow prompts) | superpowers-evals | P2 |
| Community Discord + plugin registry index | superpowers community | P3 |
| entry_point flexibility (Python class path, MCP auto-discover) | audit finding #6 | P2 |
| workflow_mode extensibility (custom guardrails per pack) | audit finding #7 | P2 |
| adapter activation mechanism (PlatformBridge) | audit finding #8 | P2 |
| Structured plugin event logging (var/log/plugin_events.jsonl) | audit finding #10 | P2 |

---

## Verification

### Directory Restructure
- `git status` shows only moved files (detected as renames)
- `python -c "from plastic_promise.core.context_engine import ContextEngine"` works
- `python -m pytest tests/ -q` — same pass count as baseline
- `python scripts/init_and_start.py` — server starts, no path errors
- `data/`, `var/`, `plugins/` directories in `.gitignore`, not tracked

### Pack v3 + Market
- `plastic-promise market list` — shows available packs
- `plastic-promise market install superpowers-core` — skills registered, `skill_resolve("brainstorming")` returns prompt
- `plastic-promise market install code-memory` — hooks active, subagent dispatch includes trace results
- `PP_ENABLE_CODE_MEMORY=1` removed — no env var, market install is the mechanism
- Uninstall → hooks cleared, core unaffected

### Extension Points
- Plugin implementing `HookProvider` → loaded and executed at declared slots
- Plugin implementing `ToolProvider` → MCP tools registered and callable
- Plugin missing optional dependency → graceful degradation, logged warning
- Malformed `pack.yml` → rejected at load with clear error
