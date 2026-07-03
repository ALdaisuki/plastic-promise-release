# Exemplar Research — Plugin Market + Extension Points

**Date**: 2026-07-03
**References**: VS Code Extension API, Kong PDK, Homebrew Taps, pip/PyPI

---

## Reference 1: VS Code Extension API

### Q1: What exactly does it do?

VS Code extensions follow a **declarative + imperative** pattern. The `package.json` manifest contains:

- **`activationEvents`** — When to lazy-load the extension (`onCommand`, `onLanguage`, `onStartup`). Extension code is NOT loaded until triggered.
- **`contributes`** — Static declarations of what the extension adds (commands, views, menus, languages, themes, chat participants, MCP servers). VS Code reads these at startup WITHOUT loading extension code.
- **`extension.ts activate(context)`** — Imperative registration function called on activation. Wires up command handlers, tree data providers, etc.

Key design features:
- **Passive activation** — Registry is built from static manifests, not by executing plugins
- **Isolated process** — All extensions run in a separate Extension Host, crash doesn't affect editor
- **40+ contribution point types** — But each is a fixed enum member, not dynamic

**Source**: `microsoft/vscode-docs` — `api/get-started/extension-anatomy.md`

### Q2: How does our context differ?

| Aspect | VS Code | Plastic Promise |
|--------|---------|-----------------|
| Runtime | Electron app, long-lived process | MCP Server, stateless call pattern |
| Plugin isolation | Separate OS process | Subprocess CLI or Python import |
| Registry model | Built-in marketplace (proprietary) | We need git-based decentralized registry |
| Extension types | UI + language + chat | Hook + tool + embedder + storage + notifier |
| Trust model | Marketplace review + user consent | Trust score gating (0.35+ can use, 0.80+ can auto-install) |
| Manifest format | JSON (`package.json`) | YAML (`pack.yml`) |

### Q3: What to adapt vs skip?

**Adapt:**
- **Static manifest + lazy activation** — `pack.yml` contains extension point declarations; PluginLoader reads manifests at startup but only imports plugin code when the extension point is hit. This is VS Code's best pattern and maps directly to our architecture.
- **Fixed enum for extension points** — VS Code has 40+ contribution points but they're all known at compile time. Our 5 `Protocol` classes fill the same role: finite set, plugin declares which it implements.

**Skip:**
- **Marketplace UI** — VS Code has a rich GUI marketplace. We don't need one yet; CLI + MCP tools suffice.
- **Process isolation** — We get it for free via subprocess CLI. Don't need Extension Host equivalent.
- **Activation events based on file types** — VS Code activates on `onLanguage:python`. Our activation is only "when the slot fires."

---

## Reference 2: Kong Plugin Development Kit (PDK)

### Q1: What exactly does it do?

Kong plugins hook into **named lifecycle phases** of the request/response pipeline. Each phase is a function in `handler.lua`:

```
init_worker → certificate → rewrite → access → response → header_filter → body_filter → log
```

Key design features:
- **Phases are Nginx directive wrappers** — Each phase maps to an Nginx processing stage. The names are fixed, the order is fixed.
- **PDK is multi-language** — Lua (native), Go, Python, JavaScript all supported via Plugin Server IPC over Unix sockets
- **Plugin Server sidecar** — Non-Lua plugins run as separate processes, communicate via RPC. Kong core doesn't import them.
- **Static priority** — Every plugin has an integer priority; within the same phase, higher priority runs first.
- **Conditional execution (v3.14+)** — Plugins can declare `condition` expressions evaluated per-request.

**Source**: `docs.konghq.com/gateway/latest/plugin-development/`

### Q2: How does our context differ?

| Aspect | Kong | Plastic Promise |
|--------|------|-----------------|
| Pipeline | Request/response (HTTP lifecycle) | Skill stage transitions (SuperPowers flow) |
| Plugin isolation | Sidecar process + Unix socket | Subprocess CLI or Python import |
| Priority model | Integer priority + dynamic ordering | Order of registration (simpler) |
| Phase granularity | ~15 phases, precise timing constraints | ~30 slots, informational/preventative |
| Language PDK | 4 languages, full SDKs | Python Protocol + CLI protocol |
| Conditional execution | Expression-based per-request | Plugin self-decides based on context dict |

### Q3: What to adapt vs skip?

**Adapt:**
- **Named lifecycle phases as extension points** — Kong's `access`, `log`, etc. → our `on_before_brainstorming`, `on_transition_write_execute`. Same core idea: the host defines the timeline, plugins pick their spots.
- **Plugin Server (CLI mode)** — Kong's non-Lua PDK model (separate process, IPC) → our `method: cli` in `pack.yml`. External plugins don't need to be Python. They just need to speak JSON over stdout.
- **Static priority → registration order** — Kong's priority system is complex (dynamic ordering has known latency issues). We default to "registration order" and only add priority if needed.

**Skip:**
- **Multi-language SDKs** — Kong maintains Go/Python/JS PDK libraries. We only need CLI stdout JSON contract.
- **Conditional expressions** — Kong's `condition` field adds a parser and evaluator. We let the plugin's `execute()` method check context and return `{}` if it wants to skip.
- **Dynamic ordering** — Kong's ordering feature has documented latency issues. Not needed at our scale.

---

## Reference 3: Homebrew Taps

### Q1: What exactly does it do?

Homebrew Taps are **Git repositories as decentralized package registries**. Key design:

- **Tap = Git repo** — `brew tap user/repo` clones `github.com/user/homebrew-repo` to a local directory
- **Formula = Ruby DSL file** — Each package is one `.rb` file in `Formula/` directory
- **No central registry server** — `brew tap` accepts any Git URL, not just GitHub
- **Namespaced resolution** — `brew install user/repo/pkg` for tap packages; `brew install pkg` searches core first
- **Core priority immutable** — A tap CANNOT override a core formula. This is by design for reproducibility.

**Source**: `docs.brew.sh/Taps`

### Q2: How does our context differ?

| Aspect | Homebrew | Plastic Promise |
|--------|----------|-----------------|
| Registry model | Git repos (decentralized) | We need both: git for community + official index |
| Package definition | Ruby DSL (Formula) | YAML (`pack.yml`) |
| Installation | Compile from source or pour bottle | `pip install` for capability, direct import for knowledge/workflow |
| Trust model | User reads formula Ruby code before installing (manual) | Trust score gates installation automatically |
| Search/Discovery | `brew search` via local tap index | `market list` via registry index |

### Q3: What to adapt vs skip?

**Adapt:**
- **Git repo as registry** — This is the killer feature. Any GitHub repo with a `pack.yml` at root is a valid pack. No central approval needed. `plastic-promise market install https://github.com/user/my-pack` just works.
- **Namespaced naming** — `user/pack-name` mirrors Homebrew's `user/repo`. Avoids name squatting without a central authority.
- **Core immutability** — A community pack can't override `superpowers-core`. Built-in packs have naming priority.
- **`tap_migrations.json`** — When packs move between repos, redirect users automatically.

**Redesign:**
- **Formula (.rb) → pack.yml** — Ruby DSL is overkill. YAML with `!include` for skill prompts is simpler and readable by non-programmers.
- **Bottles → wheels** — Homebrew pre-compiles binaries as "bottles". We don't need this; capability plugins use `pip install` for their binary dependencies.

**Skip:**
- **API mode (JSON index)** — Homebrew 4.0 added a cloud-hosted JSON API to avoid cloning core. We start without this; git clone is simple and works offline.

---

## Reference 4: pip/PyPI

### Q1: What exactly does it do?

PyPI is a **centralized package registry** with:

- **Wheel format** — Pre-built distribution, direct install without build step
- **Optional dependencies** — `[project.optional-dependencies]` in `pyproject.toml`, installed with `pip install pkg[extra]`
- **Version pinning** — `package>=1.0,<2.0` syntax
- **`pyproject.toml` as single metadata source** — Build system, dependencies, project metadata in one file

**Source**: `packaging.python.org`

### Q2: How does our context differ?

| Aspect | pip/PyPI | Plastic Promise |
|--------|----------|-----------------|
| Registry | Centralized (pypi.org) | Decentralized (git repos + optional official index) |
| Installation target | Python packages only | Three types: knowledge, workflow, capability |
| Dependency resolution | Complex SAT solver | Plugin independence (no inter-plugin deps) |
| Build step | sdist → wheel build | No build needed for knowledge/workflow; pip for capability |
| Version constraint | Full PEP 440 | Simple semver matching |

### Q3: What to adapt vs skip?

**Adapt:**
- **Optional dependencies (`[code-memory]`)** — We already have this in `pyproject.toml`. Maps directly to `pack.yml` `install.pip` field.
- **`pyproject.toml` as single source** — Our `pack.yml` is the same idea for non-Python packages.
- **Version pinning syntax** — `codebase-memory-mcp>=0.7.0` is perfectly adequate. No need to invent a new version constraint language.

**Skip:**
- **Centralized registry (PyPI)** — We use git + optional index JSON. PyPI's infrastructure (PEP 503 Simple Repository API, warehous) is massive overkill.
- **Build pipeline (sdist → wheel)** — We have no build step for knowledge/workflow packs.
- **Dependency resolver** — pip's SAT solver is 10,000+ lines. We don't allow inter-plugin dependencies, so no resolver needed.

---

## Cross-Reference Synthesis

### What all four get right

1. **Manifest-driven discovery** — VS Code `package.json`, Kong `schema.lua`, Homebrew `Formula/*.rb`, pip `pyproject.toml`. Every mature system uses a declarative manifest that can be read WITHOUT executing code.

2. **Fixed enumeration of extension points** — VS Code contribution points, Kong lifecycle phases. Plugins don't invent new hook types; they declare which of the known hooks they use.

3. **Namespaced naming** — VS Code `publisher.extension`, Homebrew `user/repo/formula`, pip `package[extra]`. Prevents name collisions without central authority.

4. **Graceful failure when plugin is absent** — VS Code disables crashing extensions (extension host restart), Kong skips unhealthy plugins, pip skips unmet optional dependencies. Nobody crashes the host.

### What none of them do (our unique constraints)

1. **Trust-score-gated installation** — Our plugins are gated by `defense(action="get")`. Homebrew's trust model is "read the Ruby source." VS Code has Microsoft's marketplace review. We can do real-time trust gating because we track trust scores.

2. **Memory/principle awareness** — Our plugins execute in a context where `context_supply` has already run. A plugin can use `engine._inject_code_context()` to enrich its output with memories. No reference system couples plugins to a memory system.

3. **Three-type marketplace** — Knowledge/Workflow/Capability in one market. This is genuinely novel. Homebrew only has packages; VS Code only has extensions; pip only has Python packages.

### Direct borrowings

| Pattern | From | Applied to |
|---------|------|------------|
| Static manifest + lazy activation | VS Code | PluginLoader reads `pack.yml` at startup, only imports code when slot fires |
| Named lifecycle slots | Kong PDK | 30 fixed slot names derived from SuperPowers stages |
| Git repo as registry | Homebrew Taps | `market install github.com/user/repo` |
| `pyproject.toml` optional dependencies | pip | `pack.yml` `install.pip` field for capability plugins |
