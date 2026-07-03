# Plastic Promise Plugin Developer Guide

## Quick Start

1. Create a directory for your plugin:

```bash
mkdir plugins/my-plugin
```

2. Create `plugins/my-plugin/pack.yml` (see reference below)

3. Test locally:

```bash
plastic-promise market install ./plugins/my-plugin
plastic-promise market status
```

## pack.yml Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| name | yes | string | Unique pack identifier |
| version | yes | string | Semver version (e.g. "1.0.0") |
| type | yes | enum | knowledge / workflow / capability / adapter |
| min_core_version | no | string | Minimum `plastic_promise` version required |
| description | no | string | One-line summary |
| author | no | string | Author identifier |
| hooks | no | dict | Slot name → method config (capability only) |
| tools | no | dict | MCP tools declaration (capability only) |
| replaces | no | dict | Core component to replace |
| install.pip | no | list | pip dependencies (capability only) |
| skills | no | dict | Skill prompt definitions (workflow only) |
| chain | no | dict | Stage dependency chain (workflow only) |
| workflow_mode | no | string | strict / advisory (workflow only) |

## Extension Points

### HookProvider — Workflow Hooks

Hook into SuperPowers pipeline stages. Declare in pack.yml:

```yaml
hooks:
  on_before_dispatch:
    method: mcp          # mcp | cli | python
    command: codebase-memory-mcp
    tool: trace_path
    timeout: 30
  on_transition_write_execute:
    method: cli
    command: my-tool
    timeout: 10
```

Valid slots: `on_before_<stage>`, `on_after_<stage>`, `on_transition_<from>_<to>`
where stages are: brainstorming, exemplar-research, using-git-worktrees, writing-plans,
executing-plans, subagent-driven-development, test-driven-development,
verification-before-completion, finishing-a-development-branch

### ToolProvider — MCP Tools

```yaml
tools:
  method: mcp
  provides: [trace_path, detect_changes, search_graph]
```

### EmbedderProvider — Replace Embedding Backend

```python
from plastic_promise.extensions import EmbedderProvider

class MyEmbedder:
    def embed(self, text: str) -> list[float]:
        ...
    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        ...
```

### StorageProvider — Replace Storage Backend

```python
from plastic_promise.extensions import StorageProvider

class MyStorage:
    def store(self, record: dict) -> str:
        ...
    def query(self, vec: list[float], top_k: int) -> list[dict]:
        ...
```

## Security Model

- Plugins are validated BEFORE any code execution
- `_validate_pack()` uses `find_spec` — NEVER imports or instantiates plugin code
- Three security gates (in order):
  1. Static validation (Protocol check, no code execution)
  2. `min_core_version` compatibility check
  3. Trust score gate: official packs >= 0.35, community >= 0.50
- `type: knowledge` and `type: workflow` packs are data-only — no code paths exist
- Disabled plugins (`.disabled` marker file) are skipped entirely

## Testing

```bash
# Test local pack
plastic-promise market install ./plugins/my-plugin

# Verify activation
plastic-promise market status

# Remove cleanly
plastic-promise market remove my-plugin
```

## Publishing

1. Push your plugin to a public GitHub repository
2. Add an entry to the market index:

```yaml
# Submit PR to plastic-promise/market-index
- name: my-plugin
  version: 1.0.0
  type: capability
  author: your-name
  source: https://github.com/your-name/my-plugin
  description: What it does
```

3. Users install with: `plastic-promise market install my-plugin`

## Example: Code Memory Plugin

See `plugins/code-memory/pack.yml` for a complete `type: capability` example
using MCP stdio protocol for code graph analysis.

## Community

- GitHub: https://github.com/plastic-promise
- Plugin index: https://github.com/plastic-promise/market-index
