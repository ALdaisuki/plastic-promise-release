# Plastic Promise — Implementation Notes

> Companion to [architecture.md](architecture.md). Practical steps for operating and extending the runtime.

## 1. Environment Setup

### Prerequisites

- Python 3.10+
- Git
- Optional Rust toolchain for `rust/context-engine-core`
- Optional Ollama for local `mxbai-embed-large` embeddings

### Install

```bash
git clone https://github.com/ALdaisuki/plastic-promise-release.git
cd plastic-promise-release
pip install -e ".[dev]"

# Optional Rust core engine
cd rust/context-engine-core
pip install maturin
maturin develop --release
cd ../..
```

### Verify environment

```bash
python scripts/init_and_start.py --check-only
python scripts/init_and_start.py --skip-ollama-check --check-only
```

## 2. Starting the System

Recommended launcher:

```bash
python scripts/init_and_start.py
```

Fallback when Ollama is unavailable:

```bash
python scripts/init_and_start.py --skip-ollama-check
```

Manual mode:

```bash
# Terminal 1: MCP Server
python -m plastic_promise --sse 9020

# Terminal 2: Maintenance daemon
python daemons/maintenance_daemon.py
```

Health check:

```bash
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
```

## 3. Development Patterns

### Adding a New MCP Tool

1. Define or extend a handler module in `plastic_promise/mcp/tools/`.
2. Register the `Tool(...)` schema in `plastic_promise/mcp/server.py`.
3. Add dispatch logic in the server call handler.
4. Add tests that exercise validation and handler behavior.
5. Update README or architecture docs if the public tool surface changes.

### Adding a New Domain

1. Create or extend a handler module in `plastic_promise/mcp/tools/`.
2. Add domain constants and routing rules where needed.
3. Update domain federation or context graph behavior if the domain participates in retrieval.
4. Run `domain(action="rebuild")` when graph/domain metadata needs rebuilding.

### Memory Pipeline Touch Points

```text
memory_store
  -> smart extraction
  -> category/tier classification
  -> vector deduplication
  -> QualityGate score
  -> RecMem.store
  -> SQLite + LanceDB write
```

To add a new quality dimension, modify `QualityGate` and update the tests that assert admission, low-quality, and discard behavior.

## 4. Agent Operations

### Task lifecycle

```text
task_enqueue -> task_claim -> task_heartbeat -> task_complete -> task_verify
```

Use `task_inbox` to inspect pending or active work. Use `task_abandon` only when work is intentionally given up and should affect trust.

### Trust checks

```text
defense(action="get")
defense(action="history")
defense(action="adjust", delta=+0.02, reason="verified delivery")
```

Write operations should check trust first when following the full Plastic Promise workflow.

## 5. Testing Strategy

```bash
pytest
pytest tests/ -k "memory"
pytest tests/ --cov=plastic_promise --cov-report=term
ruff check plastic_promise/
mypy plastic_promise/ --ignore-missing-imports
```

Make shortcuts:

```bash
make dev-install
make test-fast
make lint
make check
```

## 6. Operational Challenges

| Challenge | Mitigation |
|---|---|
| Ollama unavailable | Start with `--skip-ollama-check` and label fallback embedding behavior. |
| Large memory pool | Run `memory_gc(dry_run=True)` and monitor memory stats before destructive cleanup. |
| Trust score stagnates | Ensure `step-closure` runs after substantive work and review outcomes are recorded. |
| Daemon process drift | Use `scripts/init_and_start.py` so ServiceManager and watchdog own lifecycle. |
| Optional Rust mismatch | Treat Python context supply as canonical until Rust parity is verified for the specific path. |

## 7. Deployment Checklist

- `.env` or environment variables point to the intended SQLite and LanceDB paths.
- `python scripts/init_and_start.py --check-only` passes or known degradations are accepted.
- MCP health endpoint responds on `http://127.0.0.1:9020/health` when SSE mode is used.
- Maintenance daemon is running if task lifecycle scans are required.
- Runtime directories `var/log/` and `var/run/` are writable.
- Trust scores initialize as expected.
- Documentation reflects any public behavior changes.

## 8. Documentation Update Checklist

When implementation changes public behavior, update:

- [../../README.md](../../README.md) for installation, launch, architecture, or public feature changes.
- [../README.zh-CN.md](../README.zh-CN.md) for Chinese quickstart changes.
- [architecture.md](architecture.md) for subsystem or data-flow changes.
- [../TODO List/README.md](../TODO%20List/README.md) when roadmap status changes.
- [../../CHANGELOG.md](../../CHANGELOG.md) before a release.
