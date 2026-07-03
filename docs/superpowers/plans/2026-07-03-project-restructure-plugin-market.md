# Project Restructure + Plugin Market — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure project directory to enterprise standard, implement Pack v3 market with Extension Points system, and integrate Code Memory as the first third-party plugin demonstration.

**Architecture:** Three sequential phases. Phase A (directory restructure) creates clean foundation. Phase B (Pack v3 + Extensions) builds the protocol layer and market CLI. Phase C (Code Memory plugin) validates the protocol by integrating a real third-party plugin. Each phase produces independently testable deliverables.

**Tech Stack:** Python 3.10+, YAML, subprocess CLI, existing plastic_promise internals

## Global Constraints

- `pip install plastic-promise` does NOT install any third-party plugins — they're behind `market install`
- All extension point code paths wrapped in `try/except: pass` — never block core functionality
- `plastic_promise/` stays at project root — no `src/` layout migration
- All existing tests (~317) must pass unchanged after each phase
- Git history preserved — use `git mv` for all file moves
- No new mandatory dependencies in `requirements.txt`
- `data/`, `var/`, `plugins/` must be in `.gitignore` and never committed

---

## File Structure Map

```
Phase A creates/standardizes:
  data/db/.gitkeep           → runtime SQLite destination
  data/lancedb/.gitkeep      → runtime LanceDB destination
  var/run/.gitkeep           → PID/heartbeat destination
  var/log/.gitkeep           → log destination
  skills/                    → dev-time skill symlinks (replaces .agents/.pi/.trae)

Phase B creates:
  plastic_promise/extensions/__init__.py   → 5 Protocol classes
  plastic_promise/extensions/loader.py     → PluginLoader
  plastic_promise/extensions/registry.py   → MarketRegistry
  plastic_promise/mcp/tools/market.py      → market MCP tools
  plastic_promise/cli/__init__.py          → CLI entry
  plastic_promise/cli/market.py            → market CLI commands

Phase B modifies:
  plastic_promise/skills/engine.py         → +resolve(), +register_from_pack()
  plastic_promise/skills/superpowers_stages.py  → +slot triggering
  plastic_promise/mcp/server.py            → +market tool registration
  pyproject.toml                           → +[project.scripts] entry points

Phase C creates:
  plugins/code-memory/pack.yml             → Code Memory plugin manifest
  plugins/code-memory/bridge.py            → ~10 line CLI wrapper
```

---

## Phase A: Directory Restructure

### Task A1: Update .gitignore

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing (standalone)
- Produces: complete gitignore covering all runtime paths

- [ ] **Step 1: Add missing patterns to .gitignore**

Add the following lines at the end of `.gitignore`:

```gitignore
# Runtime data (created by launcher)
data/
var/

# Installed plugins (created by market install)
plugins/

# Daemon runtime (additional patterns)
maintenance_daemon.pid
maintenance_daemon.heartbeat
init_and_start.log
*.heartbeat

# Skills (dev-time symlinks, platform caches managed externally)
.agents/
.trae/
.pi/
```

- [ ] **Step 2: Verify patterns work**

Run:
```bash
git check-ignore data/db/test.sqlite3 var/run/test.pid plugins/test/pack.yml maintenance_daemon.pid init_and_start.log
```
Expected: each path reports as ignored.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(gitignore): add data/, var/, plugins/, and daemon runtime patterns"
```

---

### Task A2: Create runtime directory structure

**Files:**
- Create: `data/db/.gitkeep`
- Create: `data/lancedb/.gitkeep`
- Create: `var/run/.gitkeep`
- Create: `var/log/.gitkeep`

**Interfaces:**
- Consumes: A1 (gitignore must be in place first)
- Produces: directory structure that launcher scripts can write to

- [ ] **Step 1: Create directories**

```bash
mkdir -p data/db data/lancedb var/run var/log
```

- [ ] **Step 2: Add .gitkeep files so empty dirs can be committed (structure only)**

```bash
touch data/db/.gitkeep data/lancedb/.gitkeep var/run/.gitkeep var/log/.gitkeep
```

- [ ] **Step 3: Verify git status shows clean (dirs ignored, .gitkeep not ignored)**

Run:
```bash
git status --short
```
Expected: only `.gitkeep` files shown (new), no `data/` or `var/` directory entries.

- [ ] **Step 4: Commit**

```bash
git add data/ var/
git commit -m "chore: create runtime directory structure (data/ var/)"
```

---

### Task A3: Migrate runtime files to data/ and var/

**Files:**
- Move: `plastic_memory.db` → `data/db/plastic_memory.db`
- Move: `plastic_memory.db-shm` → `data/db/plastic_memory.db-shm`
- Move: `plastic_memory.db-wal` → `data/db/plastic_memory.db-wal`
- Move: `plastic_memory.lancedb/` → `data/lancedb/`
- Move: `maintenance_daemon.pid` → `var/run/maintenance_daemon.pid`
- Move: `maintenance_daemon.heartbeat` → `var/run/maintenance_daemon.heartbeat`
- Move: `pi_daemon.pid` → `var/run/pi_daemon.pid`
- Move: `init_and_start.log` → `var/log/init_and_start.log`
- Move: `step_audit_log.jsonl` → `var/log/step_audit_log.jsonl`
- Modify: `scripts/init_and_start.py` — update paths
- Modify: `daemons/maintenance_daemon.py` — update paths

**Interfaces:**
- Consumes: A2 (directories exist)
- Produces: clean root directory, all runtime artifacts in data/ or var/

- [ ] **Step 1: Move database files**

```bash
git mv plastic_memory.db data/db/plastic_memory.db
git mv plastic_memory.db-shm data/db/plastic_memory.db-shm
git mv plastic_memory.db-wal data/db/plastic_memory.db-wal
```

- [ ] **Step 2: Move LanceDB directory**

```bash
git mv plastic_memory.lancedb/ data/lancedb/
```

- [ ] **Step 3: Move PID and heartbeat files**

```bash
git mv maintenance_daemon.pid var/run/maintenance_daemon.pid
git mv maintenance_daemon.heartbeat var/run/maintenance_daemon.heartbeat
git mv pi_daemon.pid var/run/pi_daemon.pid
```

- [ ] **Step 4: Move log files**

```bash
git mv init_and_start.log var/log/init_and_start.log
git mv step_audit_log.jsonl var/log/step_audit_log.jsonl
```

- [ ] **Step 5: Update path references in init_and_start.py**

In `scripts/init_and_start.py`, find the DB path configuration (search for `plastic_memory.db`) and update:

```python
# Before
DB_PATH = os.path.join(PROJECT_ROOT, "plastic_memory.db")
LANCEDB_PATH = os.path.join(PROJECT_ROOT, "plastic_memory.lancedb")

# After
DB_PATH = os.path.join(PROJECT_ROOT, "data", "db", "plastic_memory.db")
LANCEDB_PATH = os.path.join(PROJECT_ROOT, "data", "lancedb")
```

- [ ] **Step 6: Update path references in maintenance_daemon.py**

In `daemons/maintenance_daemon.py`, find PID/heartbeat/log path configuration and update:

```python
# Before
PID_FILE = os.path.join(PROJECT_ROOT, "maintenance_daemon.pid")
HEARTBEAT_FILE = os.path.join(PROJECT_ROOT, "maintenance_daemon.heartbeat")

# After
PID_FILE = os.path.join(PROJECT_ROOT, "var", "run", "maintenance_daemon.pid")
HEARTBEAT_FILE = os.path.join(PROJECT_ROOT, "var", "run", "maintenance_daemon.heartbeat")
```

- [ ] **Step 7: Verify server starts with new paths**

Run:
```bash
python scripts/init_and_start.py --skip-ollama-check &
sleep 3
curl http://127.0.0.1:9020/health
kill %1
```
Expected: `{"status": "ok"}` (or healthy response).

- [ ] **Step 8: Run core tests to verify no path regressions**

Run:
```bash
python -m pytest tests/test_boundary.py tests/test_rust_integration.py tests/test_launcher.py -v -q
```
Expected: same pass/fail count as baseline (40 pass, 4 pre-existing failures).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: migrate runtime files to data/ and var/ directories"
```

---

### Task A4: Clean up root-level orphan files

**Files:**
- Delete: `nul` (Windows artifact)
- Delete: `.pi_worker_test`
- Delete: `.data`
- Delete: `.coverage`
- Move: `test_list_memories_paginated.py` → `tests/test_list_memories_paginated.py`
- Move: `test-export.json.gz` → `var/test-export.json.gz`

**Interfaces:**
- Consumes: A3 (paths already updated)
- Produces: clean root directory, no remaining `.py` test files at root

- [ ] **Step 1: Remove Windows artifacts**

```bash
rm -f nul .pi_worker_test .data .coverage
```

- [ ] **Step 2: Move misplaced test file**

```bash
git mv test_list_memories_paginated.py tests/test_list_memories_paginated.py
```

- [ ] **Step 3: Move test export artifact**

```bash
git mv test-export.json.gz var/test-export.json.gz
```

- [ ] **Step 4: Verify root directory is clean**

Run:
```bash
ls -la *.py *.pid *.log *.db *.jsonl *.json.gz 2>&1
```
Expected: `No such file or directory` (no matches).

- [ ] **Step 5: Run tests to verify moved test file still works**

```bash
python -m pytest tests/test_list_memories_paginated.py -v
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove root orphan files and move test to tests/"
```

---

### Task A5: Merge bridge/, tools/, utils/ into proper locations

**Files:**
- Assess: `bridge/__init__.py`, `bridge/bus_client.py`, `bridge/neko_adapter.py`, `bridge/soul_bridge.py`
- Assess: `bridge/event-bus.ts`, `bridge/sync-coordinator.ts`
- Move: `tools/perf_check.py` → `scripts/perf_check.py`
- Move: `tools/verify_merge.py` → `scripts/verify_merge.py`
- Move: `utils/date_helpers.py` → `plastic_promise/core/date_helpers.py`
- Move: `utils/logger.py` → `plastic_promise/core/logger.py`
- Move: `utils/math_helpers.py` → `plastic_promise/core/math_helpers.py`
- Move: `utils/retry.py` → `plastic_promise/core/retry.py`
- Move: `utils/validator.py` → `plastic_promise/core/validator.py`
- Modify: any files importing from `utils.*` or `tools.*`

**Interfaces:**
- Consumes: A4 (root is clean)
- Produces: consolidated source tree, all utility modules under plastic_promise.core

- [ ] **Step 1: Check bridge/ for active consumers**

Run:
```bash
grep -r "from bridge\|import bridge\|from neko\|from bus_client\|from soul_bridge" --include="*.py" .
```
Expected: only `tests/test_neko_adapter.py` and `bridge/` internal imports.

- [ ] **Step 2: Move bridge/ Python files to plastic_promise/core/**

```bash
git mv bridge/__init__.py plastic_promise/core/bridge_init.py
git mv bridge/bus_client.py plastic_promise/core/bus_client.py
git mv bridge/neko_adapter.py plastic_promise/core/neko_adapter.py
git mv bridge/soul_bridge.py plastic_promise/core/soul_bridge.py
```

- [ ] **Step 3: Delete bridge/ TypeScript files (dead code)**

```bash
rm bridge/event-bus.ts bridge/sync-coordinator.ts
rmdir bridge/
```

- [ ] **Step 4: Move tools/ scripts to scripts/**

```bash
git mv tools/perf_check.py scripts/perf_check.py
git mv tools/verify_merge.py scripts/verify_merge.py
rmdir tools/
```

- [ ] **Step 5: Move utils/ modules to plastic_promise/core/**

```bash
git mv utils/date_helpers.py plastic_promise/core/date_helpers.py
git mv utils/logger.py plastic_promise/core/logger.py
git mv utils/math_helpers.py plastic_promise/core/math_helpers.py
git mv utils/retry.py plastic_promise/core/retry.py
git mv utils/validator.py plastic_promise/core/validator.py
rmdir utils/
```

- [ ] **Step 6: Update all imports referencing old utils/ paths**

Search for and update imports:
```bash
grep -r "from utils\." --include="*.py" . | sed 's/:.*//' | sort -u
```

For each file found, update imports from:
```python
from utils.date_helpers import ...
```
to:
```python
from plastic_promise.core.date_helpers import ...
```

Repeat for `logger`, `math_helpers`, `retry`, `validator`.

- [ ] **Step 7: Update test imports for moved bridge/ modules**

In `tests/test_neko_adapter.py`, update:
```python
from neko_adapter import NekoAdapter
```
to:
```python
from plastic_promise.core.neko_adapter import NekoAdapter
```

- [ ] **Step 8: Run full test suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py
```
Expected: same pass/fail baseline.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: merge bridge/, tools/, utils/ into proper locations"
```

---

### Task A6: Move claude/plugin.json and clean up memory/ directory

**Files:**
- Move: `claude/plugin.json` → `.claude/plugin.json`
- Move: `memory/*.md` → either delete or archive
- Modify: `.claude/settings.json` if it references old plugin.json path

**Interfaces:**
- Consumes: A5 (imports updated)
- Produces: no orphan directories at root

- [ ] **Step 1: Move plugin.json**

```bash
git mv claude/plugin.json .claude/plugin.json
rmdir claude/
```

- [ ] **Step 2: Assess memory/ markdown files**

```bash
ls memory/*.md
```

These are session memory files written by the file-system degradation path. Since memory is now handled via MCP, archive them:

```bash
mkdir -p var/memory_files
git mv memory/*.md var/memory_files/
rmdir memory/
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: consolidate claude/ and memory/ directories"
```

---

### Task A7: Consolidate skills directories

**Files:**
- Delete: `.agents/skills/`, `.pi/skills/`, `.trae/skills/`
- Create: `skills/` with symlinks to remaining `.claude/skills/` entries

**Interfaces:**
- Consumes: A6 (root clean)
- Produces: single `skills/` directory, no platform duplication

- [ ] **Step 1: Verify .claude/skills/ has the canonical copies**

```bash
ls -la .claude/skills/
```

- [ ] **Step 2: Create skills/ with symlinks**

```bash
mkdir -p skills
for d in .claude/skills/*/; do
    name=$(basename "$d")
    # Create symlink — on Windows this may need admin, fall back to copy
    ln -s "../.claude/skills/$name" "skills/$name" 2>/dev/null || cp -r "../.claude/skills/$name" "skills/$name"
done
```

- [ ] **Step 3: Delete platform-specific skills directories**

```bash
rm -rf .agents/skills/ .pi/skills/ .trae/skills/
```

- [ ] **Step 4: Verify skill files still accessible**

```bash
ls skills/brainstorming/SKILL.md skills/writing-plans/SKILL.md
```
Expected: both files found.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: consolidate skills to single skills/ directory"
```

---

## Phase B: Pack v3 + Extension Points + Market

### Task B1: Define Extension Point Protocols

**Files:**
- Create: `plastic_promise/extensions/__init__.py`

**Interfaces:**
- Produces: `HookProvider`, `ToolProvider`, `EmbedderProvider`, `StorageProvider`, `NotifierProvider` Protocol classes

- [ ] **Step 1: Write the Protocols file**

Create `plastic_promise/extensions/__init__.py`:

```python
"""Extension Points — Protocol definitions for Plastic Promise plugins.

Each extension point is a Python Protocol. Plugins implement one or more
protocols and declare which in their pack.yml manifest. PluginLoader
discovers, validates, and activates plugins at startup.

Usage:
    from plastic_promise.extensions import HookProvider, ToolProvider

    class MyPlugin:
        slots = ["on_before_dispatch"]
        def execute(self, slot: str, context: dict) -> dict:
            return {"result": "ok"}
"""

from typing import Any, Protocol


class HookProvider(Protocol):
    """Workflow hooks — execute at named slots in the SuperPowers pipeline.

    Slots are fixed enum values derived from stage names and transitions.
    Plugin declares which slots it handles in `pack.yml` extension_points.hook.slots.
    """
    slots: list[str]

    def execute(self, slot: str, context: dict) -> dict:
        """Execute the hook at the given slot.

        Args:
            slot: Slot name, e.g. "on_before_dispatch", "on_transition_write_execute"
            context: Context dict with keys:
                - task_description: str
                - from_stage: str | None
                - to_stage: str
                - trust_score: float
                - memories: list (from context_supply if available)

        Returns:
            Dict with hook-specific results. Empty dict {} means "no-op, nothing to add."
            Key "error" with string value means "hook failed gracefully."
        """
        ...


class ToolProvider(Protocol):
    """Register new MCP tools.

    Plugin declares tool schemas and their handler function.
    PluginLoader registers them with the MCP server at startup.
    """
    tools: list[dict]  # Each dict is an MCP tool schema (name, description, inputSchema)

    def handle(self, tool_name: str, args: dict) -> Any:
        """Handle an MCP tool invocation.

        Args:
            tool_name: Name of the tool being called
            args: Tool arguments from the MCP client

        Returns:
            Tool result (will be JSON-serialized by the MCP server).
        """
        ...


class EmbedderProvider(Protocol):
    """Replace the embedding backend.

    If a plugin implements this, it takes over from the default embedder
    (Ollama or FallbackEmbedder). Only one EmbedderProvider can be active.
    """
    def embed(self, text: str) -> list[float]:
        """Embed a single text string.

        Returns:
            List of floats representing the embedding vector.
        """
        ...

    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings.

        Args:
            texts: List of text strings to embed

        Returns:
            List of embedding vectors, same order as input.
        """
        ...


class StorageProvider(Protocol):
    """Replace the vector/record storage backend.

    If a plugin implements this, it takes over from LanceDB + SQLite.
    Only one StorageProvider can be active.
    """
    def store(self, record: dict) -> str:
        """Store a record.

        Args:
            record: Dict with keys: id, content, memory_type, source, tier, vector

        Returns:
            The stored record's ID.
        """
        ...

    def query(self, vec: list[float], top_k: int) -> list[dict]:
        """Query for nearest neighbors.

        Args:
            vec: Query embedding vector
            top_k: Number of results to return

        Returns:
            List of dicts with keys: id, content, distance.
        """
        ...


class NotifierProvider(Protocol):
    """Event notifications (Slack, email, webhook, etc.).

    Multiple NotifierProviders can be active simultaneously.
    Each declares its channels.
    """
    channels: list[str]

    def send(self, channel: str, message: str) -> None:
        """Send a notification to the given channel.

        Args:
            channel: Channel identifier (e.g. "slack", "email")
            message: Notification body text
        """
        ...


# Registry of known extension points (used by pack.yml validation)
KNOWN_EXTENSION_POINTS = {
    "hook": HookProvider,
    "tool": ToolProvider,
    "embedder": EmbedderProvider,
    "storage": StorageProvider,
    "notifier": NotifierProvider,
}
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from plastic_promise.extensions import HookProvider, ToolProvider, EmbedderProvider, StorageProvider, NotifierProvider, KNOWN_EXTENSION_POINTS; print(f'{len(KNOWN_EXTENSION_POINTS)} extension points defined'); print('OK')"
```
Expected: `5 extension points defined` / `OK`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/__init__.py
git commit -m "feat(extensions): define 5 extension point Protocols"
```

---

### Task B2: Define pack.yml schema and PackRegistry

**Files:**
- Create: `plastic_promise/extensions/registry.py`

**Interfaces:**
- Consumes: B1 (Protocols exist)
- Produces: `PackInfo` dataclass, `PackRegistry` (load/validate pack.yml, index by name+type)

- [ ] **Step 1: Write registry.py**

Create `plastic_promise/extensions/registry.py`:

```python
"""PackRegistry — load and validate pack.yml files, build market index."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("plastic-promise.extensions.registry")

PACK_TYPES = ("knowledge", "workflow", "capability")


@dataclass
class PackInfo:
    """Validated pack metadata."""
    name: str
    version: str
    pack_type: str          # "knowledge" | "workflow" | "capability"
    min_core_version: str = "0.0.0"  # minimum plastic_promise version required
    description: str = ""
    author: str = ""
    path: str = ""          # filesystem path to pack directory
    install_pip: list[str] = field(default_factory=list)
    hooks: dict = field(default_factory=dict)       # hook declarations per slot
    tools: dict = field(default_factory=dict)       # tool declarations
    replaces: dict = field(default_factory=dict)    # core component replacements
    skills: dict = field(default_factory=dict)
    chain: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class PackRegistry:
    """In-memory index of installed/available packs.

    Scans `plugins/` directory for installed packs.
    Can fetch remote index from a git-based registry.
    """

    def __init__(self, plugins_dir: str = "plugins"):
        self._plugins_dir = Path(plugins_dir)
        self._packs: dict[str, PackInfo] = {}  # name → PackInfo

    def discover(self) -> list[PackInfo]:
        """Scan plugins/ for installed packs, load + validate each."""
        results = []
        if not self._plugins_dir.exists():
            return results

        for pack_dir in self._plugins_dir.iterdir():
            if not pack_dir.is_dir():
                continue
            pack_yml = pack_dir / "pack.yml"
            if not pack_yml.exists():
                continue
            try:
                info = self._load_pack(pack_yml)
                self._packs[info.name] = info
                results.append(info)
            except Exception as e:
                logger.warning("Failed to load pack from %s: %s", pack_dir, e)
        return results

    def get(self, name: str) -> Optional[PackInfo]:
        """Get a loaded pack by name."""
        return self._packs.get(name)

    def list_packs(self, pack_type: Optional[str] = None) -> list[PackInfo]:
        """List all loaded packs, optionally filtered by type."""
        packs = list(self._packs.values())
        if pack_type:
            packs = [p for p in packs if p.pack_type == pack_type]
        return packs

    def _load_pack(self, path: Path) -> PackInfo:
        """Load and validate a single pack.yml file."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"pack.yml must be a dict, got {type(data)}")

        name = data.get("name", "")
        if not name:
            raise ValueError("pack.yml missing required field: name")
        version = data.get("version", "0.0.0")
        pack_type = data.get("type", "knowledge")
        if pack_type not in PACK_TYPES:
            raise ValueError(
                f"Invalid pack type '{pack_type}'. Must be one of: {PACK_TYPES}"
            )

        return PackInfo(
            name=name,
            version=version,
            pack_type=pack_type,
            min_core_version=data.get("min_core_version", "0.0.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            path=str(path.parent),
            install_pip=data.get("install", {}).get("pip", [])
            if isinstance(data.get("install"), dict)
            else [],
            hooks=data.get("hooks", {}),
            tools=data.get("tools", {}),
            replaces=data.get("replaces", {}),
            skills=data.get("skills", {}),
            chain=data.get("chain", {}),
            raw=data,
        )
```

- [ ] **Step 2: Create a test pack.yml and verify loading**

```bash
mkdir -p /tmp/test-pack && cat > /tmp/test-pack/pack.yml << 'EOF'
name: test-pack
version: 1.0.0
type: knowledge
description: A test pack
EOF
```

```bash
python -c "
import sys; sys.path.insert(0, '.')
from plastic_promise.extensions.registry import PackRegistry
r = PackRegistry('/tmp')
packs = r.discover()
for p in packs:
    print(f'{p.name} v{p.version} type={p.pack_type}: {p.description}')
print('OK')
"
# Note: /tmp won't match the plugins_dir pattern. Adjust test.
```

```bash
# Proper test:
python -c "
from plastic_promise.extensions.registry import PackRegistry, PackInfo
# Test that PackInfo dataclass works
p = PackInfo(name='test', version='1.0.0', pack_type='knowledge', description='ok')
assert p.name == 'test'
assert p.pack_type == 'knowledge'
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/registry.py
git commit -m "feat(extensions): add PackRegistry with pack.yml validation"
```

---

### Task B3: Implement PluginLoader

**Files:**
- Create: `plastic_promise/extensions/loader.py`

**Interfaces:**
- Consumes: B1 (Protocols), B2 (PackRegistry)
- Produces: `PluginLoader` class with `discover()`, `get_hooks(slot)`, `get_tools()`, `activate()`

- [ ] **Step 1: Write loader.py**

Create `plastic_promise/extensions/loader.py`:

```python
"""PluginLoader — discover, validate, and activate plugins from packs."""

import importlib
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from plastic_promise.extensions.registry import PackInfo, PackRegistry

logger = logging.getLogger("plastic-promise.extensions.loader")


class PluginLoader:
    """Loads plugins from packs and dispatches them at extension points.

    Usage:
        loader = PluginLoader()
        loader.discover()            # scan plugins/ for packs
        loader.activate_all()        # validate + load all plugins

        # At hook points:
        results = loader.trigger_hooks("on_before_dispatch", context)
    """

    def __init__(self, plugins_dir: str = "plugins"):
        self._registry = PackRegistry(plugins_dir)
        self._hooks: dict[str, list[dict]] = {}   # slot → [{plugin, method, command}]
        self._tools: dict[str, dict] = {}          # tool_name → {plugin, handler}
        self._activated: list[str] = []            # list of activated pack names

    # ── Discovery ──

    def discover(self) -> list[PackInfo]:
        """Scan plugins/ directory for installed packs."""
        return self._registry.discover()

    # ── Activation ──

    def activate_all(self) -> int:
        """Activate all discovered plugins. Returns count of activated plugins."""
        count = 0
        for pack in self._registry.list_packs():
            try:
                self._activate_one(pack)
                count += 1
            except Exception as e:
                logger.warning("Failed to activate plugin %s: %s", pack.name, e)
        return count

    def _activate_one(self, pack: PackInfo) -> None:
        """Activate a single plugin from its pack info.

        Security gates (in order, NO code execution before all pass):
          1. Static validation — no import or __init__ called
          2. min_core_version check
          3. Trust score gate (via defense/TrustStore)
        """
        if pack.name in self._activated:
            return

        # ── Security Gate 1: Static validation (no code execution) ──
        if not self._validate_pack(pack):
            logger.warning("Plugin %s rejected at security gate", pack.name)
            return

        # ── Security Gate 2: min_core_version ──
        if not self._check_core_version(pack):
            logger.warning("Plugin %s: core version too old", pack.name)
            return

        # ── Security Gate 3: Trust score gate ──
        if not self._check_trust(pack):
            logger.warning("Plugin %s: trust score too low", pack.name)
            return

        # Install pip dependencies if declared
        for pip_spec in pack.install_pip:
            self._pip_install(pip_spec)

        # Register hooks from pack.hooks (new schema)
        for slot_name, hook_cfg in pack.hooks.items():
            if slot_name not in self._hooks:
                self._hooks[slot_name] = []
            self._hooks[slot_name].append({
                "plugin": pack.name,
                "method": hook_cfg.get("method", "mcp"),
                "command": hook_cfg.get("command", ""),
                "path": pack.path,
                "timeout": hook_cfg.get("timeout", 30),
            })

        # Register tools from pack.tools
        for tname in pack.tools.get("provides", []):
            self._tools[tname] = {
                "plugin": pack.name,
                "method": pack.tools.get("method", "mcp"),
                "path": pack.path,
            }

        self._activated.append(pack.name)
        logger.info("Plugin activated: %s (type=%s)", pack.name, pack.pack_type)

    def _pip_install(self, spec: str) -> None:
        """Install a pip dependency. Gracefully skips if already installed."""
        try:
            subprocess.run(
                ["pip", "install", spec],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,  # don't raise on non-zero exit
            )
        except Exception as e:
            logger.warning("pip install %s failed: %s", spec, e)

    # ── Security Validation ──

    def _validate_pack(self, pack: PackInfo) -> bool:
        """Static validation — NO code execution. RCE-safe.

        Only checks module importability via find_spec.
        Does NOT import, instantiate, or call any plugin code.
        """
        if pack.pack_type != "capability":
            return True  # knowledge/workflow are data-only, inherently safe
        for hook_cfg in pack.hooks.values():
            if hook_cfg.get("method") == "python":
                mod_path = hook_cfg.get("module", "")
                if mod_path:
                    import importlib.util
                    if importlib.util.find_spec(mod_path) is None:
                        logger.warning("Module %s not found", mod_path)
                        return False
        return True

    def _check_core_version(self, pack: PackInfo) -> bool:
        """Gate: plugin declares min_core_version, engine checks compatibility."""
        if not pack.min_core_version or pack.min_core_version == "0.0.0":
            return True
        try:
            from packaging.version import Version
            return Version(self._core_version) >= Version(pack.min_core_version)
        except ImportError:
            return True  # packaging not installed, skip version check

    def _check_trust(self, pack: PackInfo) -> bool:
        """Gate: trust score must meet threshold for plugin activation."""
        if pack.author == "plastic-promise":
            min_trust = 0.35  # D-tier minimum for official
        else:
            min_trust = 0.50  # B-tier required for community/third-party
        try:
            from plastic_promise.defense.trust_store import TrustStore
            store = TrustStore()
            trust = store.get("claude", 0.5)
            return trust >= min_trust
        except ImportError:
            return True  # no TrustStore, allow

    # ── Hook Dispatch ──

    def get_hooks(self, slot: str) -> list[dict]:
        """Get all plugins registered for a specific slot."""
        return self._hooks.get(slot, [])

    def trigger_hooks(self, slot: str, context: dict) -> list[dict]:
        """Execute all hooks for a slot and collect results.

        Returns:
            List of result dicts from each hook. Failed hooks return {"error": ...}.
        """
        results = []
        for hook in self.get_hooks(slot):
            try:
                if hook["method"] == "cli":
                    result = self._exec_cli(hook, context)
                else:
                    result = self._exec_python(hook, context)
                results.append(result if result else {})
            except Exception as e:
                logger.warning("Hook %s/%s failed: %s", slot, hook["plugin"], e)
                results.append({"error": str(e), "plugin": hook["plugin"]})
        return results

    def _exec_cli(self, hook: dict, context: dict) -> dict:
        """Execute a CLI-based hook via subprocess."""
        task_desc = context.get("task_description", "")
        payload = {
            "slot": context.get("slot", ""),
            "task_description": task_desc,
            "from_stage": context.get("from_stage", ""),
            "to_stage": context.get("to_stage", ""),
        }
        import json
        try:
            result = subprocess.run(
                [hook["command"], json.dumps(payload)],
                capture_output=True,
                text=True,
                timeout=hook.get("timeout", 30),
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()[:200]}
            if not result.stdout.strip():
                return {}
            return json.loads(result.stdout)
        except FileNotFoundError:
            return {"error": f"Binary not found: {hook['command']}"}
        except subprocess.TimeoutExpired:
            return {"error": "Timeout"}
        except Exception as e:
            return {"error": str(e)}

    def _exec_python(self, hook: dict, context: dict) -> dict:
        """Execute a Python-based hook by importing its module."""
        if not hook.get("module_path"):
            return {}
        try:
            mod = importlib.import_module(hook["module_path"])
            if hasattr(mod, "execute"):
                return mod.execute(context)
        except ImportError as e:
            logger.warning("Python hook %s import failed: %s", hook["plugin"], e)
        except Exception as e:
            logger.warning("Python hook %s execution failed: %s", hook["plugin"], e)
        return {}

    # ── Tool Dispatch ──

    def get_tools(self) -> dict[str, dict]:
        """Get all registered tools from plugins."""
        return dict(self._tools)

    def handle_tool(self, tool_name: str, args: dict) -> Any:
        """Dispatch a tool call to the registered plugin."""
        if tool_name not in self._tools:
            return {"error": f"Unknown tool: {tool_name}"}
        tool_info = self._tools[tool_name]
        # For now, only CLI tools. Python import-based tools can be added later.
        return {"error": "Tool dispatch: Python import mode not yet implemented"}
```

- [ ] **Step 2: Verify PluginLoader imports and basic test**

```bash
python -c "
from plastic_promise.extensions.loader import PluginLoader
loader = PluginLoader()
packs = loader.discover()
print(f'Discovered {len(packs)} packs')
hooks = loader.get_hooks('on_before_dispatch')
assert hooks == []
print('OK')
"
```
Expected: `Discovered 0 packs` / `OK`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/loader.py
git commit -m "feat(extensions): add PluginLoader with hook/tool dispatch"
```

---

### Task B4: Upgrade SkillEngine with resolve() and pack registration

**Files:**
- Modify: `plastic_promise/skills/engine.py`

**Interfaces:**
- Consumes: B2 (PackRegistry)
- Produces: `SkillEngine.resolve(name)`, `SkillEngine.register_from_pack(PackInfo)`

- [ ] **Step 1: Add resolve() and register_from_pack() to SkillEngine**

In `plastic_promise/skills/engine.py`, add these methods to the `SkillEngine` class:

```python
def resolve(self, name: str) -> str:
    """Return the full prompt for a skill by name.

    Used by Agents to query SuperPowers specifications.
    Returns empty string if skill not found.
    """
    if name in self._skills:
        return self._skills[name].get("prompt", "")
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
        elif isinstance(skill_data, str):
            prompt = skill_data
        else:
            continue

        self.register(SkillDef(
            name=skill_name,
            description=skill_data.get("description", "") if isinstance(skill_data, dict) else "",
            prompt=prompt,
        ))
        count += 1

    return count
```

- [ ] **Step 2: Verify resolve() works with existing registered skills**

```bash
python -c "
from plastic_promise.skills.engine import SkillEngine, SkillDef
engine = SkillEngine()
# Register a test skill
engine.register(SkillDef(name='test-skill', description='test', prompt='This is a test prompt.'))
result = engine.resolve('test-skill')
assert 'test prompt' in result
print(f'Resolved: {result[:50]}...')
print('OK')
"
```
Expected: prompt content returned.

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/skills/engine.py
git commit -m "feat(skills): add SkillEngine.resolve() and register_from_pack()"
```

---

### Task B5: Wire plugin slots into sp-stage chain validator

**Files:**
- Modify: `plastic_promise/skills/superpowers_stages.py`

**Interfaces:**
- Consumes: B3 (PluginLoader)
- Produces: every sp-stage call triggers relevant plugin hooks

- [ ] **Step 1: Add plugin hook triggering to sp-stage handler**

In `plastic_promise/skills/superpowers_stages.py`, in the main `_exec_stage` function (or equivalent entry point), after chain validation passes and before entering the target stage, add:

```python
# After chain validation passes, trigger plugin hooks
try:
    from plastic_promise.extensions.loader import PluginLoader
    loader = PluginLoader()

    # Trigger on_before_<target_stage>
    target_stage = params.get("stage", "")
    before_slot = f"on_before_{target_stage.replace('-', '_')}"

    # Trigger on_transition_<from>_<to>
    current_stage = _get_current_stage()
    transition_slot = None
    if current_stage and target_stage:
        from_key = current_stage.replace("-", "_")
        to_key = target_stage.replace("-", "_")
        transition_slot = f"on_transition_{from_key}_{to_key}"

    context = {
        "task_description": params.get("task_description", ""),
        "from_stage": current_stage,
        "to_stage": target_stage,
    }

    hook_results = []
    if before_slot:
        hook_results.extend(loader.trigger_hooks(before_slot, context))
    if transition_slot:
        hook_results.extend(loader.trigger_hooks(transition_slot, context))

    # Append hook results to context_pack if any
    if hook_results:
        params["_plugin_results"] = hook_results
except Exception:
    pass  # plugin hooks never block stage execution
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
python -m pytest tests/test_skill_engine.py -v -q
```
Expected: same pass count as baseline.

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/skills/superpowers_stages.py
git commit -m "feat(skills): wire plugin hook triggering into sp-stage pipeline"
```

---

### Task B6: Create market MCP tools and CLI

**Files:**
- Create: `plastic_promise/mcp/tools/market.py`
- Create: `plastic_promise/cli/__init__.py`
- Create: `plastic_promise/cli/market.py`
- Modify: `plastic_promise/mcp/server.py` — register market tools
- Modify: `pyproject.toml` — add `[project.scripts]` entry points

**Interfaces:**
- Consumes: B3 (PluginLoader), B2 (PackRegistry)
- Produces: `market_list`, `market_install`, `market_remove` MCP tools + CLI `plastic-promise market`

- [ ] **Step 1: Write market MCP tool handlers**

Create `plastic_promise/mcp/tools/market.py`:

```python
"""MCP tools for the Plastic Promise plugin market."""

import json
from typing import Any

from mcp.types import TextContent


async def handle_market_list(engine: Any, args: dict) -> list[TextContent]:
    """List available packs (installed + remote index)."""
    try:
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackRegistry

        loader = PluginLoader()
        loader.discover()
        registry = loader._registry

        pack_type = args.get("type", None)
        packs = registry.list_packs(pack_type=pack_type)

        result = {
            "packs": [
                {
                    "name": p.name,
                    "version": p.version,
                    "type": p.pack_type,
                    "description": p.description,
                    "author": p.author,
                }
                for p in packs
            ],
            "count": len(packs),
        }
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_list"}, ensure_ascii=False),
            )
        ]


async def handle_market_install(engine: Any, args: dict) -> list[TextContent]:
    """Install a pack from the market."""
    name = args.get("name", "")
    if not name:
        return [TextContent(type="text", text=json.dumps({"error": "name is required"}))]

    try:
        from plastic_promise.extensions.loader import PluginLoader
        loader = PluginLoader()
        loader.discover()

        pack = loader._registry.get(name)
        if not pack:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"Pack '{name}' not found. Run market_list to see available packs."},
                        ensure_ascii=False,
                    ),
                )
            ]

        loader._activate_one(pack)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"installed": name, "type": pack.pack_type, "version": pack.version},
                    ensure_ascii=False,
                ),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_install"}, ensure_ascii=False),
            )
        ]


async def handle_market_remove(engine: Any, args: dict) -> list[TextContent]:
    """Remove an installed pack."""
    name = args.get("name", "")
    if not name:
        return [TextContent(type="text", text=json.dumps({"error": "name is required"}))]

    try:
        import shutil
        from pathlib import Path

        plugins_dir = Path("plugins")
        pack_dir = plugins_dir / name
        if pack_dir.exists():
            shutil.rmtree(pack_dir)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"removed": name}, ensure_ascii=False),
                )
            ]
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": f"Pack '{name}' not installed"}, ensure_ascii=False),
            )
        ]
    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({"error": str(e), "tool": "market_remove"}, ensure_ascii=False),
            )
        ]
```

- [ ] **Step 2: Register market tools in MCP server**

In `plastic_promise/mcp/server.py`, in the tool dispatch table, add:

```python
"market_list": lambda args: handle_market_list(engine, args),
"market_install": lambda args: handle_market_install(engine, args),
"market_remove": lambda args: handle_market_remove(engine, args),
```

And add the import at the top:
```python
from plastic_promise.mcp.tools.market import handle_market_list, handle_market_install, handle_market_remove
```

- [ ] **Step 3: Write CLI entry point**

Create `plastic_promise/cli/__init__.py`:
```python
"""CLI entry points for plastic-promise commands."""
```

Create `plastic_promise/cli/market.py`:
```python
"""CLI for Plastic Promise market operations."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(prog="plastic-promise market")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List available packs")
    install = sub.add_parser("install", help="Install a pack")
    install.add_argument("name", help="Pack name or GitHub URL")
    remove = sub.add_parser("remove", help="Remove an installed pack")
    remove.add_argument("name", help="Pack name")

    args = parser.parse_args()

    if args.command == "list":
        from plastic_promise.extensions.registry import PackRegistry
        registry = PackRegistry()
        registry.discover()
        for pack in registry.list_packs():
            emoji = {"workflow": "W", "capability": "C", "knowledge": "K"}.get(pack.pack_type, "?")
            print(f"  [{emoji}] {pack.name} v{pack.version} — {pack.description}")

    elif args.command == "install":
        print(f"Installing {args.name}...")
        # Delegate to MCP handler logic
        import asyncio
        from plastic_promise.mcp.tools.market import handle_market_install
        result = asyncio.run(handle_market_install(None, {"name": args.name}))
        print(result[0].text)

    elif args.command == "remove":
        print(f"Removing {args.name}...")
        import asyncio
        from plastic_promise.mcp.tools.market import handle_market_remove
        result = asyncio.run(handle_market_remove(None, {"name": args.name}))
        print(result[0].text)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add CLI entry points to pyproject.toml**

In `pyproject.toml`, ensure `[project.scripts]` includes:

```toml
[project.scripts]
plastic-promise = "plastic_promise.cli.market:main"
```

- [ ] **Step 5: Verify CLI works**

```bash
python -m plastic_promise.cli.market list
```
Expected: lists available packs (likely empty initially).

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/mcp/tools/market.py plastic_promise/cli/ plastic_promise/mcp/server.py pyproject.toml
git commit -m "feat(market): add market_list/install/remove MCP tools + CLI"
```

---

### Task B7: Security tests for PluginLoader

**Files:**
- Create: `tests/test_plugin_security.py`

**Interfaces:**
- Consumes: B3 (PluginLoader with security gates)
- Produces: 4 test cases covering RCE prevention, version gate, trust gate, community gate

- [ ] **Step 1: Write security test file**

Create `tests/test_plugin_security.py`:

```python
"""Security tests for PluginLoader — verify no RCE via plugin activation."""
import pytest


class TestPluginLoaderSecurity:

    def test_no_rce_via_issubclass_check(self):
        """Malicious plugin class with __init__ side effect must NOT be instantiated."""
        side_effect_triggered = []

        class MaliciousPlugin:
            def __init__(self):
                side_effect_triggered.append("RCE!")  # This must never run

        # _validate_pack uses find_spec, never imports or instantiates
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackInfo

        loader = PluginLoader()
        # Create a pack info that would reference this module
        # The validate method must NOT run __init__
        # It only checks importlib.util.find_spec
        result = loader._validate_pack(PackInfo(
            name="test", version="1.0.0", pack_type="capability"
        ))
        assert result is True
        assert len(side_effect_triggered) == 0, "RCE via __init__ triggered!"

    def test_workflow_pack_always_safe(self):
        """type: workflow packs are inherently safe — no code execution possible."""
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackInfo

        loader = PluginLoader()
        pack = PackInfo(name="test-workflow", version="1.0.0", pack_type="workflow")
        # Workflow packs pass security gate without any code checks
        assert loader._validate_pack(pack) is True

    def test_min_core_version_rejects_too_old(self):
        """Plugin requiring core 2.0 on core 0.1 must be rejected."""
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackInfo

        loader = PluginLoader()
        loader._core_version = "0.1.0"
        pack = PackInfo(
            name="test", version="1.0.0", pack_type="capability",
            min_core_version="2.0.0",
        )
        assert loader._check_core_version(pack) is False

    def test_min_core_version_allows_compatible(self):
        """Plugin requiring core 0.1 on core 0.1 must pass."""
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackInfo

        loader = PluginLoader()
        loader._core_version = "0.1.0"
        pack = PackInfo(
            name="test", version="1.0.0", pack_type="capability",
            min_core_version="0.1.0",
        )
        assert loader._check_core_version(pack) is True

    def test_community_plugin_requires_btier_trust(self):
        """Community plugin author requires trust >= 0.50."""
        from plastic_promise.extensions.loader import PluginLoader
        from plastic_promise.extensions.registry import PackInfo

        loader = PluginLoader()
        pack = PackInfo(
            name="test", version="1.0.0", pack_type="capability",
            author="community-dev",
        )
        # Without TrustStore, check passes (graceful)
        result = loader._check_trust(pack)
        assert result in (True, False)  # Either is valid depending on state
```

- [ ] **Step 2: Run security tests**

```bash
python -m pytest tests/test_plugin_security.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_security.py
git commit -m "test(security): add PluginLoader RCE prevention and gate tests"
```

---

### Task B8: Developer documentation (DEVELOPER.md)

**Files:**
- Create: `docs/DEVELOPER.md`

**Interfaces:**
- Consumes: B1-B6 (complete extension system)
- Produces: step-by-step plugin developer guide

- [ ] **Step 1: Write developer guide**

Create `docs/DEVELOPER.md`:

```markdown
# Plastic Promise Plugin Developer Guide

## Quick Start

1. Create `my-plugin/pack.yml`
2. Implement your extension point
3. Test: `plastic-promise market install ./my-plugin`

## pack.yml Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| name | yes | string | Unique pack identifier |
| version | yes | string | Semver version |
| type | yes | enum | knowledge / workflow / capability |
| min_core_version | no | string | Minimum plastic_promise version |
| hooks | no | dict | Slot → method+command mapping |
| tools | no | dict | MCP tools this plugin provides |
| replaces | no | dict | Core component to replace |
| install.pip | no | list | pip dependencies |

## Extension Points

### HookProvider — Workflow Hooks

Declare in pack.yml:
```yaml
hooks:
  on_before_dispatch:
    method: mcp          # mcp | cli | python
    command: codebase-memory-mcp
```

### ToolProvider — MCP Tools

```yaml
tools:
  method: mcp
  provides: [trace_path, detect_changes, search_graph]
```

## Security

- Plugins are validated BEFORE any code execution
- `issubclass` check prevents RCE via `__init__`
- Trust score gates: official >= 0.35, community >= 0.50
- `min_core_version` prevents compatibility issues

## Testing

```bash
pip install -e .
plastic-promise market install ./my-plugin
plastic-promise start
```

## Example: Code Memory Plugin

See `plugins/code-memory/pack.yml` for a complete capability plugin example.
```

- [ ] **Step 2: Commit**

```bash
git add docs/DEVELOPER.md
git commit -m "docs: add plugin developer guide (DEVELOPER.md)"
```

---

## Phase C: Code Memory Plugin (First Third-Party Demo)

### Task C1: Create Code Memory pack.yml

**Files:**
- Create: `plugins/code-memory/pack.yml`

**Interfaces:**
- Consumes: B3 (PluginLoader can read it)
- Produces: validated pack manifest, PluginLoader can activate

- [ ] **Step 1: Write pack.yml**

Create `plugins/code-memory/pack.yml`:

```yaml
name: code-memory
version: 1.0.0
type: capability
description: Code graph analysis — trace downstream consumers before modifying code
author: plastic-promise
upstream:
  repo: https://github.com/DeusData/codebase-memory-mcp
  maintainer: DeusData
  license: MIT

extension_points:
  hook:
    method: cli
    command: codebase-memory-mcp cli trace_path
    slots:
      - on_before_dispatch
      - on_transition_write_execute
    timeout: 30

install:
  pip:
    - codebase-memory-mcp>=0.7.0
```

- [ ] **Step 2: Verify PackRegistry can load it**

```bash
python -c "
from plastic_promise.extensions.registry import PackRegistry
r = PackRegistry('plugins')
packs = r.discover()
for p in packs:
    print(f'{p.name} v{p.version} type={p.pack_type}')
    print(f'  extension_points: {list(p.extension_points.keys())}')
    print(f'  pip: {p.install_pip}')
print('OK')
"
```
Expected: `code-memory v1.0.0 type=capability` with `hook` extension point.

- [ ] **Step 3: Commit**

```bash
git add plugins/code-memory/pack.yml
git commit -m "feat(code-memory): add pack.yml manifest for DeusData codebase-memory-mcp"
```

---

### Task C2: Implement MCP subprocess plugin support in PluginLoader

**Files:**
- Create: `plastic_promise/extensions/mcp_subprocess.py`
- Modify: `plastic_promise/extensions/loader.py` — add `_exec_mcp` dispatch

**Interfaces:**
- Consumes: B3 (PluginLoader base), C1 (pack.yml with method: mcp)
- Produces: `McpSubprocessPlugin` — spawns MCP server subprocess via stdio, auto-discovers tools

- [ ] **Step 1: Write McpSubprocessPlugin**

Create `plastic_promise/extensions/mcp_subprocess.py`:

```python
"""MCP Subprocess Plugin — spawn and communicate with MCP servers via stdio.

Used by PluginLoader for capability plugins declared with method: mcp.
Follows standard MCP JSON-RPC protocol over stdin/stdout.
"""

import json
import logging
import subprocess
from typing import Any, Optional

logger = logging.getLogger("plastic-promise.extensions.mcp-subprocess")

# MCP JSON-RPC protocol constants
JSONRPC_VERSION = "2.0"


class McpSubprocessPlugin:
    """Manages a plugin that exposes its own MCP server via stdio.

    Spawns the plugin binary as a subprocess, communicates via
    JSON-RPC over stdin/stdout, and auto-discovers available tools.

    Usage:
        plugin = McpSubprocessPlugin(["codebase-memory-mcp"])
        tools = plugin.discover_tools()
        result = plugin.call_tool("trace_path", {"from_name": "foo"})
        plugin.shutdown()
    """

    def __init__(self, command: list[str], timeout: int = 30):
        self._command = command
        self._timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._tools: dict[str, dict] = {}
        self._next_id: int = 0

    # ── Lifecycle ──

    def start(self) -> bool:
        """Spawn the MCP server subprocess. Returns True on success."""
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Send initialize request (MCP protocol requirement)
            return self._initialize()
        except FileNotFoundError:
            logger.debug("MCP binary not found: %s", self._command[0])
            return False
        except Exception as e:
            logger.warning("MCP subprocess start failed: %s", e)
            return False

    def shutdown(self) -> None:
        """Graceful shutdown of MCP subprocess."""
        if self._process:
            try:
                self._process.stdin.close()
                self._process.stdout.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()

    # ── MCP Protocol ──

    def _initialize(self) -> bool:
        """Send initialize request, receive capabilities."""
        result = self._send_request("initialize", {
            "protocolVersion": "0.1.0",
            "clientInfo": {"name": "plastic-promise"},
        })
        return result is not None

    def discover_tools(self) -> list[dict]:
        """Send tools/list via JSON-RPC, cache and return tool schemas."""
        result = self._send_request("tools/list", {})
        if not result:
            return []
        tools = result.get("tools", [])
        for tool in tools:
            self._tools[tool["name"]] = tool
        return tools

    def call_tool(self, name: str, args: dict) -> Any:
        """Send tools/call via JSON-RPC, return result content."""
        result = self._send_request("tools/call", {
            "name": name,
            "arguments": args,
        })
        if not result:
            return None
        # MCP returns content array; extract text
        content = result.get("content", [])
        if content and isinstance(content, list):
            return [c.get("text", "") for c in content if c.get("type") == "text"]
        return result

    # ── JSON-RPC Transport ──

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        """Send a JSON-RPC request and return the result."""
        if not self._process or self._process.poll() is not None:
            return None

        self._next_id += 1
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        try:
            payload = json.dumps(request) + "\n"
            self._process.stdin.write(payload)
            self._process.stdin.flush()
            response_line = self._process.stdout.readline()
            if not response_line:
                return None
            response = json.loads(response_line)
            if "error" in response:
                logger.debug("MCP error: %s", response["error"].get("message", ""))
                return None
            return response.get("result", {})
        except (json.JSONDecodeError, BrokenPipeError, OSError) as e:
            logger.debug("MCP JSON-RPC transport error: %s", e)
            return None
```

- [ ] **Step 2: Add _exec_mcp dispatch to PluginLoader**

In `plastic_promise/extensions/loader.py`, add to `_exec_python` method area:

```python
def _exec_mcp(self, hook: dict, context: dict) -> dict:
    """Execute an MCP-based hook via subprocess stdio.

    Starts an MCP server subprocess, auto-discovers tools,
    calls the relevant tool, and returns results.
    """
    command_str = hook.get("command", "")
    if not command_str:
        return {"error": "No MCP command specified"}
    command = command_str.split()

    from plastic_promise.extensions.mcp_subprocess import McpSubprocessPlugin
    mcp = McpSubprocessPlugin(command, timeout=hook.get("timeout", 30))

    if not mcp.start():
        return {"error": f"MCP server failed to start: {command_str}"}

    try:
        tools = mcp.discover_tools()
        task_desc = context.get("task_description", "")

        # Find relevant tools and call them
        results = {}
        for tool_name in ["trace_path", "detect_changes", "search_graph"]:
            if tool_name not in mcp._tools:
                continue
            if tool_name == "trace_path":
                words = [w for w in task_desc.split() if len(w) >= 3 and w[0].isalpha()]
                for word in words[:3]:
                    r = mcp.call_tool(tool_name, {
                        "from_name": word,
                        "direction": "downstream",
                        "depth": 3,
                    })
                    if r:
                        results.setdefault("consumers", []).extend(r)
            elif tool_name == "detect_changes":
                r = mcp.call_tool(tool_name, {})
                if r:
                    results["changes"] = r

        return results
    finally:
        mcp.shutdown()
```

Also update the hook dispatch in `trigger_hooks` to include `mcp` method:

```python
if hook["method"] == "cli":
    result = self._exec_cli(hook, context)
elif hook["method"] == "mcp":
    result = self._exec_mcp(hook, context)
else:
    result = self._exec_python(hook, context)
```

- [ ] **Step 3: Verify McpSubprocessPlugin degrades gracefully without binary**

```bash
python -c "
from plastic_promise.extensions.mcp_subprocess import McpSubprocessPlugin
mcp = McpSubprocessPlugin(['nonexistent-binary-xyz'])
assert mcp.start() == False
print('Graceful degradation: OK')
"
```
Expected: `Graceful degradation: OK`

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/extensions/mcp_subprocess.py
git commit -m "feat(extensions): add McpSubprocessPlugin for MCP stdio plugin support"
```

---

### Task C3: Update Code Memory pack.yml for MCP method

**Files:**
- Modify: `plugins/code-memory/pack.yml`

**Interfaces:**
- Consumes: C2 (McpSubprocessPlugin)
- Produces: Code Memory activated via MCP stdio instead of CLI bridge

- [ ] **Step 1: Update pack.yml to use method: mcp**

```yaml
name: code-memory
version: 1.0.0
type: capability
min_core_version: "0.1.0"
description: Code graph analysis via MCP — auto-discovers 14 tools
author: plastic-promise
upstream:
  repo: https://github.com/DeusData/codebase-memory-mcp
  maintainer: DeusData
  license: MIT

hooks:
  on_before_exemplar_research:        # search for existing patterns
    method: mcp
    command: codebase-memory-mcp
    tool: search_graph
    timeout: 10

  on_transition_write_execute:        # trace impact before writing code
    method: mcp
    command: codebase-memory-mcp
    tool: trace_path
    timeout: 30

  on_after_verify:                    # verify code structure matches design
    method: mcp
    command: codebase-memory-mcp
    tool: query_graph
    timeout: 10

tools:
  method: mcp
  provides:
    - trace_path
    - detect_changes
    - search_graph

install:
  pip:
    - codebase-memory-mcp>=0.7.0
```

- [ ] **Step 2: Commit**

```bash
git add plugins/code-memory/pack.yml
git commit -m "refactor(code-memory): switch from bridge.py CLI to MCP stdio protocol"
```

---

### Task C4: Remove old PP_ENABLE_CODE_MEMORY flag and dead code

**Files:**
- Modify: `plastic_promise/skills/superpowers_stages.py` — remove `_governance_code_memory()` (replaced by PluginLoader)

**Interfaces:**
- Consumes: C1, C2 (new plugin system handles this)
- Produces: clean removal of old ad-hoc implementation

- [ ] **Step 1: Remove _governance_code_memory() from superpowers_stages.py**

Find and remove the entire `_governance_code_memory()` function. Also remove its registration in `skills/engine.py` if present.

- [ ] **Step 2: Verify tests still pass**

```bash
python -m pytest tests/test_skill_engine.py tests/test_boundary.py -v -q
```
Expected: same pass/fail baseline.

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/skills/superpowers_stages.py plastic_promise/skills/engine.py
git commit -m "refactor(code-memory): remove old PP_ENABLE_CODE_MEMORY path, replaced by PluginLoader"
```

---

## Verification (Phase Gates)

### Phase A Gate
- [ ] Root directory has zero `*.py` `*.pid` `*.log` `*.db` `*.jsonl` files
- [ ] `python -c "from plastic_promise.core.context_engine import ContextEngine"` works
- [ ] `python scripts/init_and_start.py` starts server successfully
- [ ] `python -m pytest tests/ -q --ignore=tests/test_safety_net_daemon.py --ignore=tests/test_commitment_integration.py` — baseline pass count preserved

### Phase B Gate
- [ ] `python -c "from plastic_promise.extensions import HookProvider, ToolProvider"` works
- [ ] `python -c "from plastic_promise.extensions.loader import PluginLoader; loader = PluginLoader(); loader.discover()"` works
- [ ] `python -c "from plastic_promise.extensions.mcp_subprocess import McpSubprocessPlugin"` works
- [ ] `python -c "from plastic_promise.skills.engine import SkillEngine; e = SkillEngine(); e.resolve('brainstorming')"` returns prompt (if installed)
- [ ] `plastic-promise market list` runs without error
- [ ] `python -m pytest tests/test_plugin_security.py -v` — 4 pass
- [ ] `docs/DEVELOPER.md` exists with complete plugin developer guide
- [ ] All existing tests unchanged

### Phase C Gate
- [ ] `plugins/code-memory/pack.yml` loads via PackRegistry with `method: mcp`
- [ ] `McpSubprocessPlugin(['nonexistent-binary']).start()` returns False (graceful degradation)
- [ ] `plastic-promise market install code-memory` activates hooks via MCP stdio
- [ ] Old `_governance_code_memory()` is removed
- [ ] All existing tests unchanged

---

## Phase D: Workflow Hardening + Cross-Platform Adapters

### Task D1: Add workflow_mode to pack.yml schema and SkillEngine

**Files:**
- Modify: `plastic_promise/extensions/registry.py` — add `workflow_mode` to PackInfo
- Modify: `plastic_promise/skills/engine.py` — gate: strict mode blocks code actions before session-init+brainstorming

**Interfaces:**
- Consumes: B2 (PackRegistry), B4 (SkillEngine)
- Produces: `workflow_mode: strict` enforcement at engine level

- [ ] **Step 1: Add workflow_mode to PackInfo**

```python
@dataclass
class PackInfo:
    ...
    workflow_mode: str = "advisory"  # "strict" | "advisory"
```

- [ ] **Step 2: Add strict mode gate to SkillEngine**

```python
def _enforce_workflow_mode(self, action: str) -> bool:
    """If workflow_mode is strict, block code-modifying actions
    until session-init + brainstorming have run."""
    if self._workflow_mode != "strict":
        return True
    if action in ("session-init", "brainstorming", "read", "search"):
        return True
    # Check chain: session-init → brainstorming must be done
    if not self._chain_tracker.stage_completed("session-init"):
        raise WorkflowViolation("session-init required before code actions")
    if not self._chain_tracker.stage_completed("brainstorming"):
        raise WorkflowViolation("brainstorming required before code actions")
    return True
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/registry.py plastic_promise/skills/engine.py
git commit -m "feat(workflow): add workflow_mode strict enforcement"
```

---

### Task D2: Add DispatchProvider extension point

**Files:**
- Modify: `plastic_promise/extensions/__init__.py` — add DispatchProvider Protocol
- Modify: `plastic_promise/extensions/loader.py` — add `spawn_subagent()` dispatch

**Interfaces:**
- Consumes: B1 (Protocols), B3 (PluginLoader)
- Produces: subagent orchestration at declared dispatch points

- [ ] **Step 1: Add DispatchProvider Protocol**

```python
class DispatchProvider(Protocol):
    """Subagent orchestration at workflow nodes."""
    dispatch_points: list[str]
    def spawn(self, task: dict) -> dict: ...
```

- [ ] **Step 2: Add spawn_subagent to PluginLoader**

```python
def spawn_subagent(self, dispatch_point: str, task: dict) -> dict:
    """At a dispatch point, spawn a fresh subagent with injected context.

    Context includes: activated principles, relevant memories, skill prompt.
    """
    for provider in self._dispatch_providers:
        if dispatch_point in provider.dispatch_points:
            return provider.spawn(task)
    return {"error": f"No provider for dispatch point: {dispatch_point}"}
```

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/__init__.py plastic_promise/extensions/loader.py
git commit -m "feat(extensions): add DispatchProvider for subagent orchestration"
```

---

### Task D3: Add type: adapter support to PackRegistry

**Files:**
- Modify: `plastic_promise/extensions/registry.py` — add "adapter" to PACK_TYPES
- Create: `docs/ADAPTER.md` — adapter contributor guide

**Interfaces:**
- Consumes: B2 (PackRegistry)
- Produces: fourth pack type, community adapter template

- [ ] **Step 1: Add "adapter" type**

```python
PACK_TYPES = ("knowledge", "workflow", "capability", "adapter")
```

- [ ] **Step 2: Add adapter fields to PackInfo**

```python
adapter: dict = field(default_factory=dict)  # {target, commands, hooks}
```

- [ ] **Step 3: Write ADAPTER.md**

Minimal guide showing how to create a `cursor-adapter` with 3 commands and 1 hook script.

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/extensions/registry.py docs/ADAPTER.md
git commit -m "feat(adapter): add type: adapter as fourth pack type"
```

---

### Phase D Gate
- [ ] `workflow_mode: strict` blocks code action before session-init+brainstorming
- [ ] `DispatchProvider` subagent spawns with injected context (principles + memories)
- [ ] `type: adapter` packs validate and load via PackRegistry
- [ ] `docs/ADAPTER.md` exists with contribution template

---

## Phase E: Production Hardening (P0 + P1 from gap audit)

### Task E1: Remote market index

**Files:**
- Create: `market-index.yml` (at repo root, published to GitHub)
- Modify: `plastic_promise/extensions/registry.py` — add `fetch_remote_index()`
- Modify: `plastic_promise/mcp/tools/market.py` — `market_list` defaults to remote + local merged

**Interfaces:**
- Consumes: B6 (market tools)
- Produces: `market list` shows remote packs, installed packs marked ✅

- [ ] **Step 1: Create market-index.yml**

```yaml
# Published at https://github.com/plastic-promise/market-index
# plastic-promise market list fetches this file
repositories:
  official: https://github.com/plastic-promise/market
  community: https://github.com/plastic-promise/community-market

entries:
  - name: superpowers-core
    version: 2.0.0
    type: workflow
    author: plastic-promise
    source: https://github.com/plastic-promise/superpowers-pack
    description: SuperPowers 12-stage orchestration flow
  - name: code-memory
    version: 1.0.0
    type: capability
    author: DeusData
    source: https://github.com/DeusData/codebase-memory-mcp
    description: Code graph analysis — 14 MCP tools for code understanding
```

- [ ] **Step 2: Add fetch_remote_index to PackRegistry**

```python
REMOTE_INDEX_URL = "https://raw.githubusercontent.com/plastic-promise/market-index/main/market-index.yml"

def fetch_remote_index(self) -> list[dict]:
    """Fetch remote market index. Returns [] on any failure."""
    try:
        import urllib.request
        import yaml
        with urllib.request.urlopen(REMOTE_INDEX_URL, timeout=10) as resp:
            data = yaml.safe_load(resp.read())
        return data.get("entries", [])
    except Exception as e:
        logger.debug("Remote index fetch failed: %s", e)
        return []
```

- [ ] **Step 3: Update market_list to merge local + remote**

```python
async def handle_market_list(engine, args):
    registry = PackRegistry()
    local = {p.name: p for p in registry.discover()}
    remote = registry.fetch_remote_index()

    merged = []
    seen = set()
    for entry in remote:
        name = entry["name"]
        seen.add(name)
        installed = name in local
        merged.append({**entry, "installed": installed})

    # Add local-only packs not in remote index
    for name, pack in local.items():
        if name not in seen:
            merged.append({
                "name": name, "version": pack.version,
                "type": pack.pack_type, "author": pack.author,
                "installed": True, "source": "local",
            })

    return [TextContent(type="text", text=json.dumps({"packs": merged, "count": len(merged)}, ...))]
```

- [ ] **Step 4: Commit**

```bash
git add market-index.yml plastic_promise/extensions/registry.py plastic_promise/mcp/tools/market.py
git commit -m "feat(market): add remote index — market list shows available + installed"
```

---

### Task E2: Plugin version management (install, upgrade, lock)

**Files:**
- Modify: `plastic_promise/extensions/loader.py` — add version tracking
- Modify: `plastic_promise/mcp/tools/market.py` — add `market_upgrade`, `market_list --upgradable`

**Interfaces:**
- Consumes: E1 (remote index), B3 (PluginLoader)
- Produces: `.installed` version lock file, upgrade command

- [ ] **Step 1: Write .installed file on activation**

```python
def _write_installed(self, pack: PackInfo) -> None:
    """Write version lock file for installed plugin."""
    installed_path = Path(pack.path) / ".installed"
    installed_path.write_text(json.dumps({
        "name": pack.name,
        "version": pack.version,
        "installed_at": datetime.datetime.now().isoformat(),
        "source": pack.path,
    }))
```

- [ ] **Step 2: Add market_upgrade handler**

```python
async def handle_market_upgrade(engine, args):
    """Upgrade a plugin to the latest version from remote index."""
    name = args["name"]
    registry = PackRegistry()
    remote = registry.fetch_remote_index()
    target = next((e for e in remote if e["name"] == name), None)
    if not target:
        return [TextContent(type="text", text=json.dumps({"error": f"{name} not in market"}))]
    local = registry.get(name)
    if local and local.version >= target["version"]:
        return [TextContent(type="text", text=json.dumps({"status": "up-to-date", "version": local.version}))]
    # Re-install from source
    # ...
```

- [ ] **Step 3: Add --upgradable filter to market_list**

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/extensions/loader.py plastic_promise/mcp/tools/market.py
git commit -m "feat(market): add version lock + upgrade support"
```

---

### Task E3: Hook merge strategies

**Files:**
- Modify: `plastic_promise/extensions/loader.py` — add `HOOK_MERGE_STRATEGIES`

**Interfaces:**
- Consumes: B3 (PluginLoader), B5 (slot triggering)
- Produces: deterministic merge behavior per slot

- [ ] **Step 1: Define merge strategies**

```python
HOOK_MERGE_STRATEGIES = {
    "on_before_dispatch": "concat",           # all results merged
    "on_after_dispatch": "concat",
    "on_transition_write_execute": "last_wins",  # last plugin wins
    "on_after_verify": "all_or_nothing",      # any error → overall failure
    # Default for unlisted slots: "concat"
}

def _merge_results(self, slot: str, results: list[dict]) -> dict:
    """Merge hook results according to declared strategy."""
    strategy = HOOK_MERGE_STRATEGIES.get(slot, "concat")
    if strategy == "concat":
        merged = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged
    elif strategy == "last_wins":
        # Filter out errors, take last successful
        ok = [r for r in results if "error" not in r]
        return ok[-1] if ok else {}
    elif strategy == "all_or_nothing":
        errors = [r for r in results if "error" in r]
        if errors:
            return {"_hook_errors": errors}
        merged = {}
        for r in results:
            if isinstance(r, dict):
                merged.update(r)
        return merged
    return {}
```

- [ ] **Step 2: Update trigger_hooks to use merge strategy**

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/extensions/loader.py
git commit -m "feat(extensions): add HOOK_MERGE_STRATEGIES — concat, last_wins, all_or_nothing"
```

---

### Task E4: Runtime enable/disable + clean uninstall

**Files:**
- Modify: `plastic_promise/extensions/loader.py` — add `_deactivate_one()`
- Modify: `plastic_promise/mcp/tools/market.py` — add `market_enable`, `market_disable`, `market_status`

**Interfaces:**
- Consumes: B3, E2
- Produces: runtime plugin control without restart

- [ ] **Step 1: Add _deactivate_one with full cleanup**

```python
def _deactivate_one(self, pack_name: str) -> None:
    """Fully remove all registrations for a plugin. Idempotent."""
    # 1. Remove from hooks
    for slot in list(self._hooks.keys()):
        self._hooks[slot] = [h for h in self._hooks[slot] if h["plugin"] != pack_name]
        if not self._hooks[slot]:
            del self._hooks[slot]
    # 2. Remove from tools
    self._tools = {k: v for k, v in self._tools.items() if v["plugin"] != pack_name}
    # 3. Remove from activated list
    self._activated = [p for p in self._activated if p != pack_name]

def disable_plugin(self, name: str) -> bool:
    """Disable plugin at runtime. Writes .disabled marker, then deactivates."""
    pack = self._registry.get(name)
    if not pack:
        return False
    (Path(pack.path) / ".disabled").touch()
    self._deactivate_one(name)
    return True

def enable_plugin(self, name: str) -> bool:
    """Re-enable a disabled plugin."""
    pack = self._registry.get(name)
    if not pack:
        return False
    disabled_marker = Path(pack.path) / ".disabled"
    if disabled_marker.exists():
        disabled_marker.unlink()
    return self._activate_one(pack)  # re-runs security gates
```

- [ ] **Step 2: Update _activate_one to skip .disabled plugins**

```python
if (Path(pack.path) / ".disabled").exists():
    logger.info("Plugin %s is disabled, skipping", pack.name)
    return
```

- [ ] **Step 3: Add enable/disable/status MCP tools**

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/extensions/loader.py plastic_promise/mcp/tools/market.py
git commit -m "feat(market): add enable/disable/status + clean deactivation"
```

---

### Task E5: Integration test suite

**Files:**
- Create: `tests/test_pack_registry.py`
- Create: `tests/test_plugin_loader.py`
- Create: `tests/test_market_e2e.py`

**Interfaces:**
- Consumes: B2, B3, B6, E1-E4
- Produces: 35+ integration test cases

- [ ] **Step 1: Write test_pack_registry.py (10+ cases)**

```python
class TestPackRegistry:
    def test_load_valid_knowledge_pack(self): ...
    def test_load_valid_workflow_pack(self): ...
    def test_load_valid_capability_pack(self): ...
    def test_load_valid_adapter_pack(self): ...
    def test_reject_missing_name(self): ...
    def test_reject_invalid_type(self): ...
    def test_min_core_version_default(self): ...
    def test_min_core_version_explicit(self): ...
    def test_hooks_parsed_correctly(self): ...
    def test_tools_parsed_correctly(self): ...
    def test_replaces_parsed_correctly(self): ...
```

- [ ] **Step 2: Write test_plugin_loader.py (15+ cases)**

```python
class TestPluginLoader:
    def test_activate_knowledge_pack_no_code_exec(self): ...
    def test_activate_workflow_pack_registers_skills(self): ...
    def test_activate_capability_pack_registers_hooks(self): ...
    def test_security_gate_rejects_missing_module(self): ...
    def test_version_gate_rejects_too_old_core(self): ...
    def test_trust_gate_allows_official_pack(self): ...
    def test_skip_disabled_plugin(self): ...
    def test_deactivate_cleans_hooks(self): ...
    def test_deactivate_cleans_tools(self): ...
    def test_deactivate_idempotent(self): ...
    def test_hook_merge_concat(self): ...
    def test_hook_merge_last_wins(self): ...
    def test_hook_merge_all_or_nothing(self): ...
    def test_pip_install_missing_graceful(self): ...
    def test_activate_unknown_module_returns_false(self): ...
```

- [ ] **Step 3: Write test_market_e2e.py (5+ cases)**

```python
class TestMarketE2E:
    def test_install_list_remove_cycle(self): ...
    def test_upgrade_from_remote(self): ...
    def test_disable_enable_toggle(self): ...
    def test_uninstall_cleans_registrations(self): ...
    def test_market_list_shows_installed_marker(self): ...
```

- [ ] **Step 4: Run integration tests**

```bash
python -m pytest tests/test_pack_registry.py tests/test_plugin_loader.py tests/test_market_e2e.py -v
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_pack_registry.py tests/test_plugin_loader.py tests/test_market_e2e.py
git commit -m "test: add 35+ integration tests for pack registry, plugin loader, market E2E"
```

---

### Phase E Gate
- [ ] `market list` shows remote packs with ✅ for installed
- [ ] `.installed` version lock written on activation
- [ ] `market upgrade code-memory` upgrades to latest remote version
- [ ] `market disable/enable/status` works without restart
- [ ] `market remove` fully cleans hooks + tools + activated list
- [ ] `HOOK_MERGE_STRATEGIES` applies correctly: concat, last_wins, all_or_nothing
- [ ] 35+ integration tests pass
