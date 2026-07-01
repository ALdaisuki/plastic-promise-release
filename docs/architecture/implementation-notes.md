# Plastic Promise — Implementation Guide

> Companion to [architecture.md](architecture.md). Practical steps for extending and operating the system.

---

## Phase 1: Environment Setup

### 1. Prerequisites

- Python 3.10+
- Git
- (Optional) Rust toolchain for `rust/context-engine-core`
- (Optional) Ollama for local reranking

### 2. Install

```bash
git clone https://github.com/plastic-promise/plastic-promise.git
cd plastic-promise
pip install -e ".[dev]"

# Optional: Rust core engine
cd rust/context-engine-core && pip install maturin && maturin develop && cd ../..

# Optional: Pre-commit hooks
make pre-commit-install
```

### 3. Verify

```bash
python -c "import plastic_promise; print('OK')"
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health')"
```

---

## Phase 2: Core Development Patterns

### Adding a New MCP Tool

1. **Define the handler** in `plastic_promise/mcp/tools/<domain>.py`:
   ```python
   async def my_new_tool(param1: str, param2: int = 0) -> dict:
       """Tool description."""
       # Implementation
       return {"status": "ok", "result": ...}
   ```

2. **Register the route** in `plastic_promise/mcp/server.py`:
   ```python
   @server.tool()
   async def my_new_tool(param1: str, param2: int = 0) -> dict:
       return await tools.my_new_tool(param1, param2)
   ```

3. **Update CLAUDE.md** tool table with the new tool entry.

### Adding a New Domain

1. Create the handler module in `plastic_promise/mcp/tools/`
2. Add domain constants to `plastic_promise/core/constants.py`
3. Register the domain in `domain_manager.py`
4. Run `domain(action="rebuild")` to update the federation graph

### Memory Pipeline Touch Points

Every `memory_store` call flows through:
```
store_urgent() → smart_extractor (6 categories)
  → vector_dedup (cos≥0.85 → update existing)
  → QualityGate.score (4-dim × 0.25)
  → RecMem.store (decay_init + LanceDB write)
```

To add a new quality dimension, modify `QualityGate.score()` in `quality_gate.py`.

---

## Phase 3: Agent Operations

### Starting the System

```bash
# Terminal 1: MCP Server
python -m plastic_promise.mcp.server --sse 9020

# Terminal 2: Daemon
python daemons/pi_daemon.py

# Or one-click (Windows):
scripts/start-all.bat
```

### Dispatching Tasks

Tasks are dispatched via tags on memory entries:
```python
memory_store(
    content="Implement feature X",
    tags=["task:pending", "assignee:pi_builder", "domain:building"]
)
```

The daemon detects `task:pending` tags and routes to the appropriate Pi agent.

### Monitoring Trust Scores

```python
defense(action="get")        # Current trust score + tier
defense(action="history")    # Trust score change history
defense(action="adjust", delta=+0.02)  # Manual adjustment
```

---

## Potential Challenges

1. **Challenge**: LanceDB index grows large (>1M vectors)
   - **Mitigation**: Weekly `memory_gc` prunes decayed memories; L3 cold storage compresses older vectors
   - **Pattern**: Monitor `memory_stats()` weekly for pool size trends

2. **Challenge**: Trust score stagnation (never moves from 0.6)
   - **Mitigation**: Ensure `step-closure` runs after every substantive step; SCARF < 0.40 triggers -0.02 decay
   - **Pattern**: Check `defense(action="history")` for flat lines

3. **Challenge**: Daemon CPU usage on large memory pools
   - **Mitigation**: Zero-token polling uses SQLite indexes; scanners use LIMIT + OFFSET batching
   - **Pattern**: Set `PP_SCAN_BATCH_SIZE=100` for large pools

4. **Challenge**: Cross-platform path issues (Windows vs Unix)
   - **Mitigation**: Use `pathlib.Path` everywhere; test on both platforms
   - **Pattern**: `scripts/start-all.bat` (Windows) + `scripts/start-all.sh` (Unix)

---

## Testing Strategy

### Unit Tests
```bash
make test                    # Full suite
pytest tests/ -k "memory"   # Memory domain only
pytest tests/ -k "decay"    # Decay engine tests
```

### Integration Tests
- MCP server startup + health check
- memory_store → memory_recall round-trip
- session-init full pipeline
- step-closure 6-link execution

### Performance Benchmarks
- `memory_store`: <50ms (p95, including embedding)
- `memory_recall`: <100ms (p95, ANN + RRF)
- `context_supply`: <1s (p95, including rerank)
- Daemon poll cycle: <10ms (SQLite only)

---

## Deployment Checklist

- [ ] `.env` configured with correct paths
- [ ] LanceDB directory exists and is writable
- [ ] SQLite database created (auto on first run)
- [ ] MCP server reachable at `http://127.0.0.1:9020/health`
- [ ] Daemon running and polling
- [ ] Trust scores initialized (default: 0.6)
- [ ] Pre-commit hooks installed

---

## Cost Optimization Tips

1. **Use light mode** for read-only operations: `step-closure(mode="light")`
2. **Batch memory stores** when possible (single embedding batch)
3. **Cache principle activations** — they change rarely
4. **Use zero-token daemon** — no LLM calls for task routing
5. **Leverage smart_extractor** — handles 90% of extractions without LLM fallback
