# Plastic Promise Plugin and Extension Developer Guide

> Practical guide for building optional Plastic Promise packs and extension providers.

## 1. Purpose

Plastic Promise extensions let users add knowledge, workflows, capabilities, or adapters without changing the core runtime. Extension metadata is validated before plugin code is imported, so data-only packs can remain safe and portable.

For the user-facing runtime guide, see [../README.md](../README.md). For architecture, see [architecture/architecture.md](architecture/architecture.md).

## 2. Quick Start

Create a plugin directory:

```bash
mkdir plugins/my-plugin
```

Create `plugins/my-plugin/pack.yml`, then test locally:

```bash
plastic-promise market install ./plugins/my-plugin
plastic-promise market status
```

## 3. pack.yml Reference

| Field | Required | Type | Description |
|---|---|---|---|
| `name` | yes | string | Unique pack identifier. |
| `version` | yes | string | SemVer version such as `1.0.0`. |
| `type` | yes | enum | `knowledge`, `workflow`, `capability`, or `adapter`. |
| `min_core_version` | no | string | Minimum `plastic_promise` version required. |
| `description` | no | string | One-line summary. |
| `author` | no | string | Author identifier. |
| `hooks` | no | dict | Slot name to method config. Capability packs only. |
| `tools` | no | dict | MCP tools declaration. Capability packs only. |
| `replaces` | no | dict | Core component to replace. |
| `install.pip` | no | list | pip dependencies. Capability packs only. |
| `skills` | no | dict | Skill prompt definitions. Workflow packs only. |
| `chain` | no | dict | Stage dependency chain. Workflow packs only. |
| `workflow_mode` | no | string | `strict` or `advisory`. Workflow packs only. |

## 4. Extension Points

### HookProvider — Workflow Hooks

Hook into SuperPowers pipeline stages. Declare in `pack.yml`:

```yaml
hooks:
  on_before_dispatch:
    method: mcp
    command: codebase-memory-mcp
    tool: trace_path
    timeout: 30
  on_transition_write_execute:
    method: cli
    command: my-tool
    timeout: 10
```

Valid slots include:

```text
on_before_<stage>
on_after_<stage>
on_transition_<from>_<to>
```

Common stages include `brainstorming`, `exemplar-research`, `using-git-worktrees`, `writing-plans`, `executing-plans`, `subagent-driven-development`, `test-driven-development`, `verification-before-completion`, and `finishing-a-development-branch`.

### ToolProvider — MCP Tools

```yaml
tools:
  method: mcp
  provides:
    - trace_path
    - detect_changes
    - search_graph
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

## 5. Security Model

- Plugins are validated before any code execution.
- `_validate_pack()` uses discovery checks rather than importing or instantiating plugin code.
- Validation order:
  1. Static metadata validation.
  2. `min_core_version` compatibility check.
  3. Trust score gate: official packs require lower trust than community packs.
- `knowledge` and `workflow` packs should remain data-only.
- Disabled plugins are skipped entirely through the disabled marker mechanism.

## 6. Testing

```bash
plastic-promise market install ./plugins/my-plugin
plastic-promise market status
plastic-promise market remove my-plugin
```

For core changes around extensions, also run:

```bash
pytest
ruff check plastic_promise/
```

## 7. Publishing

1. Push the plugin to a public repository.
2. Add an entry to the market index used by the project.
3. Users install with:

```bash
plastic-promise market install my-plugin
```

Example index entry:

```yaml
- name: my-plugin
  version: 1.0.0
  type: capability
  author: your-name
  source: https://github.com/your-name/my-plugin
  description: What it does
```

## 8. Example

See `plugins/code-memory/pack.yml` for a capability pack that integrates an external code graph tool through MCP stdio.
