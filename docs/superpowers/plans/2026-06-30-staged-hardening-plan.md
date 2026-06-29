# Staged Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenTelemetry tracing, end-to-end integration tests, performance benchmarks, and RAGAS quality metrics to Plastic Promise.

**Architecture:** Three independent stages: S1 adds `tracing.py` module + manual spans to 8 MCP tools + 3 internal modules + dashboard card; S2 adds session-level integration test fixtures + 5 E2E scenarios; S3 adds `ragas_metrics.py` module + 10-dimension audit + 6 performance benchmarks.

**Tech Stack:** opentelemetry-api >= 1.28.0, opentelemetry-sdk >= 1.28.0, opentelemetry-instrumentation-requests >= 0.49b0, pytest-benchmark

## Global Constraints

- All spans use prefix `plastic.*`
- `PP_TRACING_ENABLED=0` disables all tracing output (test/CI mode)
- RAGAS `compute_context_recall()` returns `None` (not `0.0`) when expected_ids unavailable
- Audit dimension weights sum to exactly 1.000 after adding dimensions 9+10
- Integration tests use session-level fixtures with dynamic ports; Windows-compatible
- Benchmark standard dataset: 1024-dim vectors, SMALL=1000 / LARGE=10000, 3 warmup rounds, 10+ iterations
- `include_ragas=True` on audit_run, callers may explicitly set `False`
- **New dependencies** (add to pyproject.toml or requirements.txt):
  - `opentelemetry-api>=1.28.0`
  - `opentelemetry-sdk>=1.28.0`
  - `opentelemetry-instrumentation-requests>=0.49b0`
  - `opentelemetry-exporter-otlp` (optional, production only)
  - `pytest-benchmark>=5.0.0` (dev dependency)

---

### Task 1: Core Tracing Module

**Files:**
- Create: `plastic_promise/core/tracing.py`
- Create: `tests/test_tracing.py`
- Modify: `plastic_promise/core/__init__.py` (optional, re-export if pattern requires)

**Interfaces:**
- Produces: `init_tracing(service_name, exporter, enabled) -> None`, `get_tracer() -> trace.Tracer | None`, `is_tracing_enabled() -> bool`
- Consumes: nothing (no dependencies on other tasks)

- [ ] **Step 1: Write failing tests for tracing module**

```python
# tests/test_tracing.py
import os
import pytest

# Force clean state before importing tracing
@pytest.fixture(autouse=True)
def clean_tracing_env():
    """Reset tracing state before each test."""
    old_enabled = os.environ.pop("PP_TRACING_ENABLED", None)
    # Force reimport by clearing module cache
    import plastic_promise.core.tracing as tmod
    tmod._tracer = None
    tmod._tracing_enabled = True
    yield
    if old_enabled is not None:
        os.environ["PP_TRACING_ENABLED"] = old_enabled
    else:
        os.environ.pop("PP_TRACING_ENABLED", None)


class TestInitTracing:
    def test_init_console_exporter_creates_tracer(self):
        from plastic_promise.core.tracing import init_tracing, get_tracer, is_tracing_enabled
        init_tracing(service_name="test-svc", exporter="console", enabled=True)
        tracer = get_tracer()
        assert tracer is not None
        assert is_tracing_enabled() is True

    def test_init_disabled_returns_none_tracer(self):
        from plastic_promise.core.tracing import init_tracing, get_tracer, is_tracing_enabled
        init_tracing(service_name="test-svc", exporter="console", enabled=False)
        tracer = get_tracer()
        assert tracer is None
        assert is_tracing_enabled() is False

    def test_env_var_disables_tracing(self, monkeypatch):
        monkeypatch.setenv("PP_TRACING_ENABLED", "0")
        from plastic_promise.core.tracing import init_tracing, get_tracer, is_tracing_enabled
        init_tracing(service_name="test-svc", exporter="console")
        assert get_tracer() is None
        assert is_tracing_enabled() is False


class TestSpanCreation:
    def test_span_created_when_tracing_enabled(self):
        from plastic_promise.core.tracing import init_tracing, get_tracer
        init_tracing(service_name="test-svc", exporter="console", enabled=True)
        tracer = get_tracer()
        assert tracer is not None
        with tracer.start_as_current_span("test.operation") as span:
            span.set_attribute("test.key", "test.value")
        # Span context should have trace_id and span_id
        assert span.get_span_context().trace_id != 0

    def test_no_span_created_when_tracing_disabled(self):
        from plastic_promise.core.tracing import init_tracing, get_tracer
        init_tracing(service_name="test-svc", exporter="console", enabled=False)
        tracer = get_tracer()
        assert tracer is None

    def test_span_attributes_preserved(self):
        from plastic_promise.core.tracing import init_tracing, get_tracer
        init_tracing(service_name="test-svc", exporter="console", enabled=True)
        tracer = get_tracer()
        with tracer.start_as_current_span("plastic.memory.recall") as span:
            span.set_attribute("query", "test query")
            span.set_attribute("max_results", 20)
            span.set_attribute("hit_count", 5)
        assert span.get_span_context().trace_id != 0


class TestRepeatInit:
    def test_double_init_does_not_crash(self):
        from plastic_promise.core.tracing import init_tracing
        init_tracing(service_name="test-svc", exporter="console", enabled=True)
        init_tracing(service_name="test-svc", exporter="console", enabled=True)
        # No exception = pass
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_tracing.py -v
```
Expected: `ModuleNotFoundError: No module named 'plastic_promise.core.tracing'`

- [ ] **Step 3: Write minimal tracing.py implementation**

```python
# plastic_promise/core/tracing.py
"""OpenTelemetry tracing — init, tracer access, enable/disable switch.

Environment:
  PP_TRACING_ENABLED=0  → disable all tracing (test/CI mode)
  PP_SERVICE_NAME       → override service name (default: plastic-promise)
"""

import os
import logging
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

logger = logging.getLogger("plastic-promise.tracing")

_tracer: Optional[trace.Tracer] = None
_tracing_enabled: bool = True


def init_tracing(
    service_name: str = "plastic-promise",
    exporter: str = "console",
    enabled: bool = True,
) -> None:
    """Initialize OpenTelemetry tracing.

    Args:
        service_name: Service name for span attribution.
        exporter: "console" (default, stdout) or "otlp" (OTLP Collector).
        enabled: False to silently skip all span creation (test/CI mode).
    """
    global _tracer, _tracing_enabled

    # Check env override
    env_disabled = os.environ.get("PP_TRACING_ENABLED", "").strip()
    if env_disabled == "0":
        enabled = False

    _tracing_enabled = enabled

    if not enabled:
        _tracer = None
        logger.info("Tracing: disabled (PP_TRACING_ENABLED=0 or enabled=False)")
        return

    service_name = os.environ.get("PP_SERVICE_NAME", service_name)

    provider = TracerProvider()

    if exporter == "otlp":
        otlp_exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("Tracing: OTLP exporter configured")
    else:
        console_exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(console_exporter))
        logger.info("Tracing: Console exporter configured")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    logger.info("Tracing: initialized for service '%s'", service_name)


def get_tracer() -> Optional[trace.Tracer]:
    """Get the current tracer instance, or None if tracing is disabled."""
    return _tracer


def is_tracing_enabled() -> bool:
    """Check whether tracing is currently enabled."""
    return _tracing_enabled and _tracer is not None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_tracing.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/tracing.py tests/test_tracing.py
git commit -m "feat: add tracing.py — OpenTelemetry init with console/otlp exporters and enable/disable switch"
```

---

### Task 2: Internal Module Spans (Embedder + LanceDB + SQLite)

**Files:**
- Modify: `plastic_promise/core/embedder.py`
- Modify: `plastic_promise/core/lancedb_store.py`
- Modify: `plastic_promise/memory/soul_memory.py`

**Interfaces:**
- Consumes: `get_tracer()` from `plastic_promise.core.tracing` (Task 1)
- Produces: spanned methods on `OllamaEmbedder`, `LanceDBStore`, `_SQLiteStorage` (in context_engine.py) — no new public API

- [ ] **Step 1: Add spans to OllamaEmbedder**

```python
# plastic_promise/core/embedder.py — modify OllamaEmbedder.embed() and embed_batch()
# Add import at top of file:
import time
from plastic_promise.core.tracing import get_tracer

# Modify OllamaEmbedder.embed():
def embed(self, text: str) -> list[float]:
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        resp = requests.post(
            f"{self._host}/api/embeddings",
            json={"model": self._model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()["embedding"]
        if tracer:
            span = tracer.start_span("plastic.embedding.encode")
            span.set_attribute("model", self._model)
            span.set_attribute("dim", self.dim)
            span.set_attribute("batch_size", 1)
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()

# Modify OllamaEmbedder.embed_batch():
def embed_batch(self, texts: list[str]) -> list[list[float]]:
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        results = [self.embed(t) for t in texts]
        if tracer:
            span = tracer.start_span("plastic.embedding.batch_encode")
            span.set_attribute("model", self._model)
            span.set_attribute("batch_count", len(texts))
            span.set_attribute("total_tokens", sum(len(t) for t in texts))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return results
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 2: Add spans to LanceDBStore**

```python
# plastic_promise/core/lancedb_store.py — add to LanceDBStore class
# Add import at top of file:
import time
from plastic_promise.core.tracing import get_tracer

# Modify LanceDBStore.search():
def search(self, vector, k=20, scope=None, tier=None):
    if self._table is None:
        return []
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        q = self._table.search(vector).metric("cosine").limit(k)
        raw = q.to_list()
        results = []
        for row in raw:
            dist = row.get("_distance", 0.0)
            sim = 1.0 - (dist / 2.0)
            mid = row["memory_id"]
            if scope and row.get("scope") != scope:
                continue
            if tier and row.get("tier") != tier:
                continue
            results.append((mid, max(0.0, min(1.0, sim)),
                            row.get("text", ""),
                            row.get("tier", "L1"),
                            row.get("scope", "global")))
        if tracer:
            span = tracer.start_span("plastic.lancedb.ann_search")
            span.set_attribute("k", k)
            span.set_attribute("scope", scope or "")
            span.set_attribute("tier", tier or "")
            span.set_attribute("hit_count", len(results))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return results
    except Exception as e:
        logger.error("LanceDB vector search failed: %s", e)
        if span:
            span.set_attribute("error", True)
        return []
    finally:
        if span:
            span.end()

# Modify LanceDBStore.search_similar():
def search_similar(self, vector, k=20, threshold=0.85, scope=None):
    if self._table is None:
        return []
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    results = []
    try:
        raw = self._table.search(vector).metric("cosine").limit(k * 2).to_list()
        for row in raw:
            dist = row.get("_distance", 0.0)
            sim = 1.0 - (dist / 2.0)
            if sim < threshold:
                continue
            if scope and row.get("scope") != scope:
                continue
            results.append((row["memory_id"], sim, row.get("text", ""),
                            row.get("tier", "L1"), row.get("scope", "global")))
        if tracer:
            span = tracer.start_span("plastic.lancedb.similar_search")
            span.set_attribute("k", k)
            span.set_attribute("threshold", threshold)
            span.set_attribute("hit_count", len(results))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
    except Exception as e:
        logger.error("LanceDB similarity search failed: %s", e)
        if span:
            span.set_attribute("error", True)
    finally:
        if span:
            span.end()
    return results

# Modify LanceDBStore.search_fts():
def search_fts(self, query, k=20, scope=None):
    if self._table is None:
        return []
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        if self._fts_ready:
            raw = self._table.search(query, query_type="fts").limit(k).to_list()
        else:
            pattern = f"%{query}%"
            raw = self._table.search().where(
                f"text LIKE '{pattern}'", prefilter=True
            ).limit(k).to_list()
        results = []
        for row in raw:
            mid = row["memory_id"]
            score = row.get("_distance", row.get("_score", 0.5))
            if isinstance(score, (int, float)):
                score = 1.0 - min(float(score), 1.0)
            else:
                score = 0.5
            if scope and row.get("scope") != scope:
                continue
            results.append((mid, max(0.0, min(1.0, score)),
                            row.get("text", ""),
                            row.get("tier", "L1"),
                            row.get("scope", "global")))
        if tracer:
            span = tracer.start_span("plastic.lancedb.fts_search")
            span.set_attribute("query_len", len(query))
            span.set_attribute("fts_ready", self._fts_ready)
            span.set_attribute("hit_count", len(results))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
    except Exception as e:
        logger.warning("LanceDB FTS search failed: %s", e)
        if span:
            span.set_attribute("error", True)
    finally:
        if span:
            span.end()
    return results

# Modify LanceDBStore.upsert():
def upsert(self, memory_id, vector, text, tier="L1", category="general", scope="global"):
    if self._table is None:
        return
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        record = {"memory_id": memory_id, "vector": vector, "text": text,
                  "tier": tier, "category": category, "scope": scope}
        self._table.add([record])
        if tracer:
            span = tracer.start_span("plastic.lancedb.upsert")
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
    except Exception as e:
        logger.error("LanceDB upsert failed: %s", e)
        if span:
            span.set_attribute("error", True)
    finally:
        if span:
            span.end()

# Modify LanceDBStore.delete():
def delete(self, memory_id):
    if self._table is None:
        return
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        self._table.delete(f"memory_id = '{memory_id}'")
        if tracer:
            span = tracer.start_span("plastic.lancedb.delete")
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
    except Exception as e:
        logger.error("LanceDB delete failed: %s", e)
        if span:
            span.set_attribute("error", True)
    finally:
        if span:
            span.end()
```

- [ ] **Step 3: Add spans to _SQLiteStorage**

```python
# plastic_promise/core/context_engine.py — modify _SQLiteStorage class (line ~1030)
# Add import at top of file (if not already present):
import time
from plastic_promise.core.tracing import get_tracer

# Locate _SQLiteStorage.store_memory() and add span wrapper:
# Find the method in the file and wrap the core logic:

# In store_memory(): after the method body's core insert logic, wrap with:
def store_memory(self, memory_data: dict) -> str:
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing insert logic, which assigns memory_id ...
        # (keep existing code, just wrap the final return)
        # After memory_id is determined, before return:
        if tracer:
            span = tracer.start_span("plastic.sqlite.store")
            span.set_attribute("memory_type", memory_data.get("memory_type", ""))
            span.set_attribute("tier", memory_data.get("tier", "L1"))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return memory_id
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()

# In get_memory():
def get_memory(self, memory_id: str):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing lookup logic ...
        # Before return:
        if tracer:
            span = tracer.start_span("plastic.sqlite.get")
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 4: Verify existing tests still pass**

```bash
pytest tests/test_lancedb_store.py tests/test_decay_engine.py tests/test_quality_gate.py -v --timeout=120
```
Expected: all existing tests pass (spans are transparent when tracing not initialized)

- [ ] **Step 5: Manual verify — spans appear when tracing enabled**

```python
# Quick smoke test (run inline, not committed):
from plastic_promise.core.tracing import init_tracing
init_tracing(service_name="test", exporter="console", enabled=True)
from plastic_promise.core.embedder import FallbackEmbedder
e = FallbackEmbedder(dim=4)
e.embed("hello")
# Expected: ConsoleSpanExporter output with "plastic.embedding.encode" span
```

- [ ] **Step 6: Commit**

```bash
git add plastic_promise/core/embedder.py plastic_promise/core/lancedb_store.py plastic_promise/core/context_engine.py
git commit -m "feat: add OTel spans to OllamaEmbedder, LanceDBStore, _SQLiteStorage (in context_engine.py)"
```

---

### Task 3: MCP Tool Spans

**Files:**
- Modify: `plastic_promise/mcp/tools/memory.py`
- Modify: `plastic_promise/mcp/tools/skill_tracking.py`
- Modify: `plastic_promise/mcp/tools/audit_defense.py`
- Modify: `plastic_promise/mcp/tools/principles.py`
- Modify: `plastic_promise/mcp/tools/context.py`
- Modify: `plastic_promise/mcp/tools/management.py`

**Interfaces:**
- Consumes: `get_tracer()` from Task 1
- Produces: each handler function wrapped in `plastic.*` span with tool-specific attributes

- [ ] **Step 1: Add span to handle_memory_recall and handle_memory_store in memory.py**

```python
# plastic_promise/mcp/tools/memory.py
# Add import at top:
import time
from plastic_promise.core.tracing import get_tracer

# In handle_memory_recall, wrap the main logic:
async def handle_memory_recall(engine: Any, args: dict) -> list[TextContent]:
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    query = args.get("query", "")
    try:
        # ... existing logic (keep all current code) ...
        # Before the final return, capture metrics:
        if tracer:
            span = tracer.start_span("plastic.memory.recall")
            span.set_attribute("query", query[:200])
            span.set_attribute("max_results", args.get("max_results", 20))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()

# In handle_memory_store, same pattern:
async def handle_memory_store(engine: Any, args: dict) -> list[TextContent]:
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.memory.store")
            span.set_attribute("memory_type", args.get("memory_type", ""))
            span.set_attribute("tier", args.get("tier", "L1"))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 2: Add spans to skill_tracking.py handlers**

```python
# plastic_promise/mcp/tools/skill_tracking.py
# Add import:
import time
from plastic_promise.core.tracing import get_tracer

# In skill_session_start handler:
async def handle_skill_session_start(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    skill_name = args.get("skill_name", "")
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.skill.start")
            span.set_attribute("skill_name", skill_name)
            span.set_attribute("parent_id", args.get("parent_entity_id", "") or "")
            span.set_attribute("branch", args.get("branch", "") or "")
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()

# In skill_session_complete handler:
async def handle_skill_session_complete(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.skill.complete")
            span.set_attribute("skill_name", args.get("skill_name", ""))
            span.set_attribute("outcome", args.get("outcome", "")[:200])
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

**Note on skill_session_trace:** The handler should accept an `include_auto_inject` parameter (default `False`). When `False`, sessions whose skill_name starts with `"auto_inject:"` are excluded from chain validation and gap detection — this prevents auto-context-inject records from polluting audit scores.

- [ ] **Step 3: Add span to audit_defense.py handlers**

```python
# plastic_promise/mcp/tools/audit_defense.py
import time
from plastic_promise.core.tracing import get_tracer

# In handle_audit_run:
async def handle_audit_run(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.audit.run")
            span.set_attribute("scope", args.get("scope", "global"))
            span.set_attribute("include_ragas", args.get("include_ragas", True))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()

# In handle_issue_transition (in management.py, but handled here for span grouping):
# Add to the appropriate handler file
```

- [ ] **Step 4: Add span to principles.py**

```python
# plastic_promise/mcp/tools/principles.py
import time
from plastic_promise.core.tracing import get_tracer

# In handle_principle_activate:
async def handle_principle_activate(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.principle.activate")
            span.set_attribute("task_type", args.get("task_type", "general"))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 5: Add span to context.py**

```python
# plastic_promise/mcp/tools/context.py
import time
from plastic_promise.core.tracing import get_tracer

# In handle_context_supply:
async def handle_context_supply(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.context.supply")
            span.set_attribute("task_type", args.get("task_type", "general"))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 6: Add span to management.py**

```python
# plastic_promise/mcp/tools/management.py
import time
from plastic_promise.core.tracing import get_tracer

# In handle_issue_transition:
async def handle_issue_transition(engine, args):
    tracer = get_tracer()
    span = None
    start = time.perf_counter()
    try:
        # ... existing logic ...
        if tracer:
            span = tracer.start_span("plastic.issue.transition")
            span.set_attribute("issue_id", args.get("issue_id", ""))
            span.set_attribute("from_state", args.get("from_state", ""))
            span.set_attribute("to_state", args.get("state", ""))
            span.set_attribute("latency_ms", (time.perf_counter() - start) * 1000)
        return result
    except Exception:
        if span:
            span.set_attribute("error", True)
        raise
    finally:
        if span:
            span.end()
```

- [ ] **Step 7: Run existing tests to ensure no regression**

```bash
pytest tests/test_skill_tracking.py tests/test_commitment_integration.py -v --timeout=120
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add plastic_promise/mcp/tools/
git commit -m "feat: add OTel spans to 8 MCP tool handlers (recall, store, skill, audit, principle, context, issue)"
```

---

### Task 4: Server Init + Requests Instrument

**Files:**
- Modify: `plastic_promise/mcp/server.py`
- Modify: `plastic_promise/core/tracing.py` (add `instrument_requests()` helper)

**Interfaces:**
- Consumes: `init_tracing()` from Task 1
- Produces: tracing auto-initialized on server startup; all HTTP calls to Ollama auto-instrumented

- [ ] **Step 1: Add `instrument_requests()` to tracing.py**

```python
# Add to plastic_promise/core/tracing.py:
def instrument_requests() -> None:
    """Auto-instrument the 'requests' library for HTTP call tracing.

    After calling this, all requests.get/post/put/delete calls generate
    OTel spans automatically. Used to capture Ollama API calls.

    Safe to call when tracing is disabled — does nothing.
    """
    if not is_tracing_enabled():
        return
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
        logger.info("Tracing: requests library instrumented")
    except ImportError:
        logger.warning(
            "Tracing: opentelemetry-instrumentation-requests not installed, "
            "HTTP calls will not be auto-traced"
        )
    except Exception as e:
        logger.warning("Tracing: requests instrumentation failed: %s", e)
```

- [ ] **Step 2: Initialize tracing in MCP server startup**

```python
# plastic_promise/mcp/server.py
# Add import near top (after existing imports):
from plastic_promise.core.tracing import init_tracing, instrument_requests

# In the main() function or server startup block, add:
def main():
    # ... existing arg parsing ...
    
    # Initialize tracing (respects PP_TRACING_ENABLED env var)
    init_tracing(service_name="plastic-promise", exporter="console")
    instrument_requests()
    
    # ... rest of existing startup ...
```

- [ ] **Step 3: Verify server starts with tracing**

```bash
python -c "from plastic_promise.mcp.server import main; import sys; sys.exit(0)" 2>&1 | head -20
```
Expected: no crash, tracing init log line visible

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/mcp/server.py plastic_promise/core/tracing.py
git commit -m "feat: init tracing on server startup + auto-instrument requests for Ollama HTTP calls"
```

---

### Task 5: Dashboard Observability Card

**Files:**
- Modify: `plastic_promise/mcp/server.py` (dashboard endpoint)

**Interfaces:**
- Consumes: `get_tracer()` from Task 1 (to read in-memory span stats, or use a simple accumulator)
- Produces: `/dashboard` response includes `observability` section

- [ ] **Step 1: Add in-memory span accumulator to tracing.py**

```python
# Add to plastic_promise/core/tracing.py:
from collections import defaultdict
import threading

_span_stats: dict = {"calls": defaultdict(int), "latencies": defaultdict(list), "errors": defaultdict(int)}
_stats_lock = threading.Lock()

def record_span(span_name: str, latency_ms: float, is_error: bool = False) -> None:
    """Record span metrics for dashboard aggregation. Thread-safe."""
    with _stats_lock:
        _span_stats["calls"][span_name] += 1
        _span_stats["latencies"][span_name].append(latency_ms)
        if is_error:
            _span_stats["errors"][span_name] += 1
        # Keep only last 1000 latencies per span to bound memory
        if len(_span_stats["latencies"][span_name]) > 1000:
            _span_stats["latencies"][span_name] = _span_stats["latencies"][span_name][-1000:]

def get_span_stats() -> dict:
    """Return current span statistics for dashboard display."""
    import statistics
    with _stats_lock:
        result = {}
        for name in _span_stats["calls"]:
            lats = _span_stats["latencies"][name]
            p50 = statistics.median(lats) if lats else 0
            p95 = sorted(lats)[int(len(lats) * 0.95)] if len(lats) >= 20 else (max(lats) if lats else 0)
            result[name] = {
                "call_count": _span_stats["calls"][name],
                "error_count": _span_stats["errors"][name],
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
            }
        return result
```

- [ ] **Step 2: Call record_span() from all existing spans**

In each span-creation site from Tasks 2-3, add one line before `span.end()`:
```python
if tracer:
    record_span("plastic.memory.recall", (time.perf_counter() - start) * 1000)
```

- [ ] **Step 3: Add observability section to dashboard handler**

Locate the dashboard HTML generation in `server.py`. Add a new card after existing cards:

```python
# In the dashboard handler, add to the HTML template:
observability_html = """
<div class="card">
    <h2>📊 可观测性 (最近调用)</h2>
    <table>
        <tr><th>工具</th><th>调用次数</th><th>错误数</th><th>P50</th><th>P95</th></tr>
"""
from plastic_promise.core.tracing import get_span_stats
stats = get_span_stats()
for name, data in sorted(stats.items()):
    error_rate = (data["error_count"] / data["call_count"] * 100) if data["call_count"] else 0
    row_class = "error-row" if error_rate > 10 else ""
    observability_html += (
        f'<tr class="{row_class}">'
        f'<td>{name}</td>'
        f'<td>{data["call_count"]}</td>'
        f'<td>{data["error_count"]} ({error_rate:.1f}%)</td>'
        f'<td>{data["p50_ms"]}ms</td>'
        f'<td>{data["p95_ms"]}ms</td>'
        f'</tr>'
    )
observability_html += "</table></div>"
# Insert into dashboard HTML before the closing body tag
```

- [ ] **Step 4: Commit**

```bash
git add plastic_promise/core/tracing.py plastic_promise/mcp/server.py
git commit -m "feat: dashboard observability card — span call counts, P50/P95 latency, error rates"
```

---

### Task 6: Integration Test Infrastructure + Issue Lifecycle Tests

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_issue_lifecycle.py`

**Interfaces:**
- Produces: `mcp_server` (session fixture), `temp_db` (session fixture), `engine` (function fixture)
- Consumes: ContextEngine, RecMem, SoulAuditor from existing codebase

- [ ] **Step 1: Create empty __init__.py**

```python
# tests/integration/__init__.py
# Integration tests for Plastic Promise multi-agent collaboration
```

- [ ] **Step 2: Write conftest.py with session-level fixtures**

```python
# tests/integration/conftest.py
"""Shared fixtures for Plastic Promise integration tests.

Strategy:
- Session-level MCP server: started once, shared across all tests
- Dynamic port allocation to avoid 9020 conflicts
- Temporary SQLite + LanceDB directories for isolation
- atexit + pytest finalizer for guaranteed cleanup
"""

import os
import sys
import socket
import atexit
import tempfile
import signal
import time
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _find_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def temp_db_dir():
    """Session-scoped temporary directory for SQLite + LanceDB data."""
    tmpdir = tempfile.TemporaryDirectory(prefix="pp_integration_")
    yield tmpdir.name
    tmpdir.cleanup()


@pytest.fixture(scope="session")
def free_port():
    """Session-scoped free port for MCP server."""
    return _find_free_port()


@pytest.fixture(scope="session")
def mcp_server(temp_db_dir, free_port):
    """Start MCP server once for the entire test session.
    
    Uses subprocess to start the server on a random port.
    Registers cleanup via atexit + pytest finalizer.
    """
    import subprocess
    
    env = os.environ.copy()
    env["PP_DB_DIR"] = temp_db_dir
    env["PP_TRACING_ENABLED"] = "0"  # Disable tracing in tests
    env["PP_EMBEDDER_PROVIDER"] = "fallback"  # Use zero-vector fallback
    
    proc = subprocess.Popen(
        [sys.executable, "-m", "plastic_promise.mcp.server", "--http", str(free_port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    # Wait for server to be ready
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://localhost:{free_port}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("MCP server failed to start within 30s")
    
    def _cleanup():
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    
    atexit.register(_cleanup)
    yield {"port": free_port, "process": proc}
    _cleanup()


@pytest.fixture
def engine(temp_db_dir):
    """Function-scoped ContextEngine with fresh state per test.
    
    Uses direct Python API (not subprocess) for speed.
    """
    os.environ["PP_DB_DIR"] = temp_db_dir
    os.environ["PP_TRACING_ENABLED"] = "0"
    os.environ["PP_EMBEDDER_PROVIDER"] = "fallback"
    
    from plastic_promise.core.context_engine import ContextEngine
    eng = ContextEngine()
    yield eng
    # Cleanup: clear test data from SQLite memory pool
    # ContextEngine has no clear_all() — manually purge the internal stores
    eng._memories.clear()
    if hasattr(eng, '_sqlite') and eng._sqlite:
        try:
            eng._sqlite._conn.execute("DELETE FROM memories")
            eng._sqlite._conn.commit()
        except Exception:
            pass  # Table may not exist in test setup
```

- [ ] **Step 3: Write issue lifecycle integration test**

```python
# tests/integration/test_issue_lifecycle.py
"""Integration tests: Issue lifecycle, fix loop, timeout recovery."""

import time
import pytest


class TestIssueLifecycle:
    """Scenario 1: Complete issue lifecycle — Claude → Pi → Reviewer → Claude."""

    def test_full_lifecycle_pending_to_reviewed(self, engine):
        """memory_store(task:pending) → accept → active → done → review → reviewed."""
        from plastic_promise.memory.soul_memory import RecMem
        
        recmem = RecMem(engine=engine)
        
        # Step 1: Claude creates a task
        result = recmem.store(
            content="Implement /dashboard endpoint",
            memory_type="task",
            tags=["task:pending", "assignee:pi_builder", "domain:building"],
        )
        assert result.get("memory_id") is not None
        task_id = result["memory_id"]
        
        # Step 2: Daemon detects and transitions to accepted
        from plastic_promise.issue import transition_issue
        t1 = transition_issue(task_id, "accepted", reason="Daemon assigned")
        assert t1["state"] == "accepted"
        
        # Step 3: Pi Builder transitions to active
        t2 = transition_issue(task_id, "active", reason="Pi started execution")
        assert t2["state"] == "active"
        
        # Step 4: Pi completes → done
        t3 = transition_issue(task_id, "done", reason="Pi completed implementation")
        assert t3["state"] == "done"
        
        # Step 5: Reviewer transitions to review
        t4 = transition_issue(task_id, "review", reason="Reviewer inspection")
        assert t4["state"] == "review"
        
        # Step 6: Claude accepts → reviewed
        t5 = transition_issue(task_id, "reviewed", reason="Claude approved")
        assert t5["state"] == "reviewed"
        
        # Verify memory still accessible
        recalled = recmem.get(task_id)
        assert recalled is not None
        assert "reviewed" in str(recalled.get("tags", ""))


class TestFixLoop:
    """Scenario 2: Review rejection → Fixer repair → re-review."""

    def test_rejected_to_fix_loop(self, engine):
        """task:rejected → accepted → active → done → review → reviewed."""
        from plastic_promise.memory.soul_memory import RecMem
        from plastic_promise.issue import transition_issue
        
        recmem = RecMem(engine=engine)
        
        result = recmem.store(
            content="Fix typo in CLAUDE.md",
            memory_type="task",
            tags=["task:rejected", "assignee:pi_fixer", "domain:fixing"],
        )
        task_id = result["memory_id"]
        
        # Fixer picks up
        t1 = transition_issue(task_id, "accepted", reason="Fixer claimed")
        assert t1["state"] == "accepted"
        
        # Fixer fixes
        t2 = transition_issue(task_id, "active", reason="Fixer working")
        t3 = transition_issue(task_id, "done", reason="Fix applied")
        assert t3["state"] == "done"
        
        # Reviewer re-checks
        t4 = transition_issue(task_id, "review", reason="Re-review after fix")
        t5 = transition_issue(task_id, "reviewed", reason="Claude verified fix")
        assert t5["state"] == "reviewed"


class TestTimeoutRecovery:
    """Scenario 3: Stale task:active reset to task:pending."""

    def test_active_timeout_resets_to_pending(self, engine):
        """A task stuck in active state should be resettable to pending."""
        from plastic_promise.memory.soul_memory import RecMem
        from plastic_promise.issue import transition_issue
        
        recmem = RecMem(engine=engine)
        
        result = recmem.store(
            content="Long-running task simulation",
            memory_type="task",
            tags=["task:active", "assignee:pi_builder", "domain:building"],
        )
        task_id = result["memory_id"]
        
        # Simulate timeout recovery: active → pending
        t1 = transition_issue(task_id, "pending", reason="Timeout: >5min in active state")
        assert t1["state"] == "pending"
        
        # Now it should be reassignable
        t2 = transition_issue(task_id, "accepted", reason="Reassigned by Daemon")
        assert t2["state"] == "accepted"
```

- [ ] **Step 4: Run integration tests**

```bash
pytest tests/integration/test_issue_lifecycle.py -v --timeout=120
```
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add tests/integration/
git commit -m "test: integration tests — Issue lifecycle, fix loop, timeout recovery (scenarios 1-3)"
```

---

### Task 7: Context Flow + Skill Tracking E2E Tests

**Files:**
- Create: `tests/integration/test_context_flow.py`
- Create: `tests/integration/test_skill_tracking_e2e.py`

**Interfaces:**
- Consumes: `engine`, `temp_db_dir` fixtures from conftest.py (Task 6)

- [ ] **Step 1: Write context flow integration test**

```python
# tests/integration/test_context_flow.py
"""Integration tests: Cross-agent context supply flow."""

import os
import pytest


class TestContextFlow:
    """Scenario 4: Context flows between agents via memory pool."""

    def test_context_flows_from_claude_to_pi(self, engine):
        """Claude posts task context → Pi retrieves it via context_supply."""
        from plastic_promise.memory.soul_memory import RecMem
        from plastic_promise.core.context_engine import ContextEngine
        
        recmem = RecMem(engine=engine)
        
        # Claude stores task context
        recmem.store(
            content="Task: Add rate limiting to API. Requirements: 100 req/min per user.",
            memory_type="task",
            tags=["task:pending", "assignee:pi_builder", "domain:building"],
            entity_ids=["entity:task:rate-limit"],
        )
        
        # Claude also stores supporting context
        recmem.store(
            content="Existing API uses Flask middleware. Rate limit should be middleware-based.",
            memory_type="reference",
            tags=["context:api", "domain:building"],
            entity_ids=["entity:task:rate-limit"],
        )
        
        # Pi retrieves context via context_supply
        ctx_engine = ContextEngine()
        pack = ctx_engine.supply(
            task_description="Add rate limiting to API",
            task_type="code_generation",
        )
        
        # Verify context contains the stored memories
        assert pack is not None
        all_items = pack.core + pack.related
        contents = " ".join(str(getattr(item, "content", item)) for item in all_items)
        assert "rate limit" in contents.lower()
        assert "middleware" in contents.lower()

    def test_context_supply_filters_by_domain(self, engine):
        """Context supply respects domain filtering."""
        from plastic_promise.memory.soul_memory import RecMem
        from plastic_promise.core.context_engine import ContextEngine
        
        recmem = RecMem(engine=engine)
        
        # Store memories in different domains
        recmem.store(
            content="Building domain task: implement login",
            memory_type="task",
            tags=["domain:building"],
        )
        recmem.store(
            content="Reflecting domain task: review architecture",
            memory_type="task",
            tags=["domain:reflecting"],
        )
        
        # Supply with building domain hint
        ctx_engine = ContextEngine()
        pack = ctx_engine.supply(
            task_description="implement user authentication",
            task_type="code_generation",
            scope="building",
        )
        
        assert pack is not None
        # Building-domain content should be present
        building_content = False
        for item in pack.core + pack.related:
            text = str(getattr(item, "content", item))
            if "login" in text.lower():
                building_content = True
        assert building_content, "Building-domain content should be in context"
```

- [ ] **Step 2: Write skill tracking E2E test**

```python
# tests/integration/test_skill_tracking_e2e.py
"""Integration tests: Skill tracking end-to-end chain."""

import os
import pytest


class TestSkillTrackingE2E:
    """Scenario 5: Complete skill session chain with audit validation."""

    def test_complete_skill_chain_valid(self, engine):
        """brainstorming → writing-plans → executing-plans → verification → finish."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_start,
            handle_skill_session_complete,
            handle_skill_session_trace,
        )
        
        # Start brainstorming
        r1 = await _call_handler(handle_skill_session_start, engine, {
            "skill_name": "brainstorming",
            "task_description": "Design hardening features",
            "parent_entity_id": None,
        })
        b_id = _extract_entity_id(r1)
        assert b_id is not None
        
        # Complete brainstorming
        await _call_handler(handle_skill_session_complete, engine, {
            "entity_id": b_id,
            "outcome": "Design approved: 3-stage hardening",
            "artifacts": ["docs/specs/hardening-design.md"],
        })
        
        # Start writing-plans (valid next after brainstorming)
        r2 = await _call_handler(handle_skill_session_start, engine, {
            "skill_name": "writing-plans",
            "task_description": "Write hardening implementation plan",
            "parent_entity_id": b_id,
        })
        wp_id = _extract_entity_id(r2)
        
        await _call_handler(handle_skill_session_complete, engine, {
            "entity_id": wp_id,
            "outcome": "Plan complete",
            "artifacts": ["docs/plans/hardening-plan.md"],
        })
        
        # Start executing-plans
        r3 = await _call_handler(handle_skill_session_start, engine, {
            "skill_name": "executing-plans",
            "task_description": "Execute hardening plan",
            "parent_entity_id": wp_id,
        })
        ep_id = _extract_entity_id(r3)
        
        await _call_handler(handle_skill_session_complete, engine, {
            "entity_id": ep_id,
            "outcome": "Executed all tasks",
            "artifacts": [],
        })
        
        # Start verification
        r4 = await _call_handler(handle_skill_session_start, engine, {
            "skill_name": "verification-before-completion",
            "task_description": "Verify hardening changes",
            "parent_entity_id": ep_id,
        })
        v_id = _extract_entity_id(r4)
        
        await _call_handler(handle_skill_session_complete, engine, {
            "entity_id": v_id,
            "outcome": "All tests pass",
            "artifacts": [],
        })
        
        # Trace the chain — exclude auto_inject: records from audit scoring
        r5 = await _call_handler(handle_skill_session_trace, engine, {
            "session_scope": "branch",
            "include_auto_inject": False,  # exclude auto_inject:* sessions from chain validation
        })
        trace_data = _parse_json(r5)
        
        # Assertions
        assert trace_data.get("chain_complete") is True, f"Chain should be complete, got: {trace_data}"
        assert trace_data.get("gaps") == [], f"No orphan sessions, got: {trace_data.get('gaps')}"
        assert trace_data.get("chain_valid") is True, f"Chain should be valid, got: {trace_data}"

    def test_orphan_detection(self, engine):
        """An incomplete skill chain should be detected."""
        from plastic_promise.mcp.tools.skill_tracking import (
            handle_skill_session_start,
            handle_skill_session_trace,
        )
        
        # Start but never complete
        await _call_handler(handle_skill_session_start, engine, {
            "skill_name": "brainstorming",
            "task_description": "Incomplete brainstorming",
            "parent_entity_id": None,
        })
        
        r = await _call_handler(handle_skill_session_trace, engine, {
            "session_scope": "branch",
        })
        trace_data = _parse_json(r)
        
        # Should detect the gap
        assert trace_data.get("chain_complete") is False


# ---- Helpers ----

async def _call_handler(handler, engine, args):
    """Call an async MCP handler and return the result."""
    result = await handler(engine, args)
    return result


def _extract_entity_id(result):
    """Extract entity_id from a skill_session_start result."""
    if not result:
        return None
    import json
    for item in result:
        if hasattr(item, "text"):
            data = json.loads(item.text)
            return data.get("entity_id")
    return None


def _parse_json(result):
    """Parse JSON from MCP TextContent result."""
    import json
    if not result:
        return {}
    for item in result:
        if hasattr(item, "text"):
            return json.loads(item.text)
    return {}
```

- [ ] **Step 3: Run integration tests**

```bash
pytest tests/integration/test_context_flow.py tests/integration/test_skill_tracking_e2e.py -v --timeout=120
```
Expected: 3 passed (2 context flow + 1 skill chain valid; orphan test may need adjustment based on actual skill_tracking behavior)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/
git commit -m "test: integration tests — context flow + skill tracking E2E (scenarios 4-5)"
```

---

### Task 8: RAGAS Metrics Module

**Files:**
- Create: `plastic_promise/core/ragas_metrics.py`
- Create: `tests/test_ragas_metrics.py`

**Interfaces:**
- Produces: `compute_context_precision(retrieved, query_vector, threshold) -> float`, `compute_context_recall(retrieved, expected_ids) -> float | None`, `get_expected_ids(memory_ids, entity_graph) -> set | None`
- Consumes: LanceDBStore for vector similarity; ContextEngine for entity_graph access

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ragas_metrics.py
"""Tests for RAGAS metrics: context precision, recall, expected_ids extraction."""

import pytest
from plastic_promise.core.ragas_metrics import (
    compute_context_precision,
    compute_context_recall,
    get_expected_ids,
)


class TestContextPrecision:
    """Cosine similarity > threshold determines relevance."""

    def test_all_relevant_returns_one(self):
        """When all retrieved items are relevant, precision = 1.0."""
        query_vec = [1.0, 0.0, 0.0]
        retrieved = [
            {"vector": [1.0, 0.0, 0.0], "id": "a"},  # cos_sim = 1.0
            {"vector": [0.99, 0.01, 0.0], "id": "b"},  # cos_sim ≈ 0.99
        ]
        result = compute_context_precision(retrieved, query_vec, threshold=0.7)
        assert result == 1.0

    def test_half_relevant_returns_half(self):
        """Half relevant → 0.5 precision."""
        query_vec = [1.0, 0.0, 0.0]
        retrieved = [
            {"vector": [1.0, 0.0, 0.0], "id": "a"},   # relevant
            {"vector": [0.0, 1.0, 0.0], "id": "b"},   # cos=0, irrelevant
        ]
        result = compute_context_precision(retrieved, query_vec, threshold=0.7)
        assert result == 0.5

    def test_empty_retrieved_returns_zero(self):
        """Empty results → 0.0 precision."""
        result = compute_context_precision([], [1.0, 0.0], threshold=0.7)
        assert result == 0.0

    def test_custom_threshold(self):
        """Higher threshold → fewer items count as relevant."""
        query_vec = [1.0, 0.0]
        retrieved = [
            {"vector": [0.8, 0.6], "id": "a"},  # cos ≈ 0.8
        ]
        result_high = compute_context_precision(retrieved, query_vec, threshold=0.85)
        result_low = compute_context_precision(retrieved, query_vec, threshold=0.5)
        assert result_high == 0.0  # Below 0.85 → not relevant
        assert result_low == 1.0   # Above 0.5 → relevant


class TestContextRecall:
    """Recall based on expected_ids matching."""

    def test_all_found_returns_one(self):
        """All expected IDs in retrieved → recall = 1.0."""
        retrieved = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        result = compute_context_recall(retrieved, expected_ids={"a", "b"})
        assert result == 1.0

    def test_half_found_returns_half(self):
        """Half expected found → 0.5 recall."""
        retrieved = [{"id": "a"}, {"id": "x"}]
        result = compute_context_recall(retrieved, expected_ids={"a", "b"})
        assert result == 0.5

    def test_none_expected_returns_none(self):
        """No ground truth → None, not 0.0."""
        retrieved = [{"id": "a"}, {"id": "b"}]
        result = compute_context_recall(retrieved, expected_ids=None)
        assert result is None

    def test_empty_expected_returns_zero(self):
        """Empty expected_ids set → 0.0 (nothing was expected)."""
        retrieved = [{"id": "a"}]
        result = compute_context_recall(retrieved, expected_ids=set())
        assert result == 0.0

    def test_empty_retrieved_returns_zero(self):
        """Expected but nothing retrieved → 0.0."""
        retrieved = []
        result = compute_context_recall(retrieved, expected_ids={"a", "b"})
        assert result == 0.0


class TestGetExpectedIds:
    """Entity ID extraction from memory records."""

    def test_extracts_from_entity_ids(self):
        """Memory with entity_ids returns those IDs."""
        # Mock entity_graph and memory_ids
        result = get_expected_ids(
            memory_ids=["mem-1", "mem-2"],
            entity_graph=_mock_graph_with_entities({"mem-1": {"e1", "e2"}, "mem-2": {"e2", "e3"}}),
        )
        assert result == {"e1", "e2", "e3"}

    def test_returns_none_when_no_entities(self):
        """Memory without entity_ids → None."""
        result = get_expected_ids(
            memory_ids=["mem-1"],
            entity_graph=_mock_graph_with_entities({}),
        )
        assert result is None


def _mock_graph_with_entities(entity_map):
    """Helper to create a mock entity graph."""
    class MockGraph:
        def get_entities(self, memory_id):
            return entity_map.get(memory_id, set())
    return MockGraph()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_ragas_metrics.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement ragas_metrics.py**

```python
# plastic_promise/core/ragas_metrics.py
"""RAGAS-inspired metrics for memory recall quality evaluation.

Two core metrics:
- Context Precision: fraction of retrieved items that are relevant (cosine > threshold)
- Context Recall: fraction of expected items that were retrieved (requires ground truth)

Key design decision: compute_context_recall() returns None (not 0.0) when expected_ids 
is unavailable — "no data" ≠ "zero recall".
"""

import math
from typing import Optional


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def compute_context_precision(
    retrieved: list[dict],
    query_vector: list[float],
    threshold: float = 0.7,
) -> float:
    """Compute Context Precision: fraction of retrieved items relevant to query.

    Relevance is determined by cosine similarity between the query vector
    and each retrieved item's vector. Items with cos_sim >= threshold are
    considered relevant.

    Args:
        retrieved: List of dicts, each with "vector" and "id" keys.
        query_vector: The embedding vector of the query.
        threshold: Cosine similarity threshold for relevance (default 0.7).

    Returns:
        Float in [0.0, 1.0]. Returns 0.0 if retrieved is empty.
    """
    if not retrieved:
        return 0.0

    relevant_count = 0
    for item in retrieved:
        item_vec = item.get("vector")
        if item_vec is None:
            continue
        sim = _cosine_similarity(query_vector, item_vec)
        if sim >= threshold:
            relevant_count += 1

    return relevant_count / len(retrieved)


def compute_context_recall(
    retrieved: list[dict],
    expected_ids: Optional[set[str]],
) -> Optional[float]:
    """Compute Context Recall: fraction of expected items that were retrieved.

    Args:
        retrieved: List of dicts, each with an "id" key.
        expected_ids: Set of memory IDs that SHOULD have been retrieved.
            If None, returns None (no ground truth available).
            If empty set, returns 0.0 (nothing was expected).

    Returns:
        Float in [0.0, 1.0], or None if expected_ids is None.
    """
    if expected_ids is None:
        return None
    if not expected_ids:
        return 0.0
    if not retrieved:
        return 0.0

    retrieved_ids = {item.get("id") for item in retrieved if "id" in item}
    found = len(retrieved_ids & expected_ids)
    return found / len(expected_ids)


def get_expected_ids(
    memory_ids: list[str],
    entity_graph=None,
) -> Optional[set[str]]:
    """Extract expected memory IDs from an entity graph via entity tag matching.

    Strategy (three-tier fallback):
    1. From entity_graph: traverse entity → memory edges
    2. From memory_ids' own entity_ids tags (if stored in metadata)
    3. Return None if no ground truth available

    Args:
        memory_ids: List of memory IDs to look up entities for.
        entity_graph: EntityGraph instance with get_entities(memory_id) method.

    Returns:
        Set of expected memory IDs, or None if no ground truth is available.
    """
    if entity_graph is None:
        return None

    all_entities = set()
    try:
        for mid in memory_ids:
            entities = entity_graph.get_entities(mid)
            if entities:
                all_entities.update(entities)
    except Exception:
        return None

    if not all_entities:
        return None

    return all_entities
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_ragas_metrics.py -v
```
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/core/ragas_metrics.py tests/test_ragas_metrics.py
git commit -m "feat: ragas_metrics.py — Context Precision/Recall with three-tier expected_ids fallback"
```

---

### Task 9: Audit Dimensions 9+10 + Weight Fix

**Files:**
- Modify: `plastic_promise/core/constants.py`

**Interfaces:**
- Produces: Updated `AUDIT_DIMENSIONS` dict with 10 entries, weights sum to 1.000
- Consumes: nothing new

- [ ] **Step 1: Update AUDIT_DIMENSIONS in constants.py**

Locate `AUDIT_DIMENSIONS` in `plastic_promise/core/constants.py`. Update weights for existing 8 dimensions and add 2 new RAGAS dimensions:

```python
# In plastic_promise/core/constants.py, replace AUDIT_DIMENSIONS:

AUDIT_DIMENSIONS = {
    "simplicity": {
        "name": "奥卡姆剃刀",
        "weight": 0.117,
        "description": "方案是否最简洁？是否存在不必要的实体或步骤？每一步只做当前最必要的事。",
        "principle_id": 1,
    },
    "transparency": {
        "name": "全过程可查可透明",
        "weight": 0.117,
        "description": "每步是否有完整 git 痕迹？审计日志是否可追溯？中间产物是否可验证？",
        "principle_id": 2,
    },
    "audit_closure": {
        "name": "自我审计闭环",
        "weight": 0.117,
        "description": "是否有根因分析？是否有改良措施？是否提炼了可迁移教训？量化评分是否准确？",
        "principle_id": 3,
    },
    "principle_activation": {
        "name": "原则激活率",
        "weight": 0.117,
        "description": "每次任务是否自动激活了相关原则？激活的原则是否被实际遵循？是否存在原则\"休眠\"？",
        "principle_id": 4,
    },
    "memory_supply": {
        "name": "记忆供给质量",
        "weight": 0.117,
        "description": "上下文供给是否充分？记忆召回的相关性和时效性如何？三层上下文包的比例是否合理？",
        "principle_id": 4,
    },
    "constraint_compliance": {
        "name": "约束合规度",
        "weight": 0.117,
        "description": "L0 硬边界是否有违规？L1 动态约束是否按信任分正确调整？L2 免疫巡检是否按时执行？",
        "principle_id": 9,
    },
    "feedback_closure": {
        "name": "反馈闭环率",
        "weight": 0.081,
        "description": "每次交互是否产生了反馈信号？adopted/rejected/ignored 的分布是否健康？反馈是否驱动了行为修正？",
        "principle_id": 10,
    },
    "skill_trace": {
        "name": "Skill 执行可追溯",
        "weight": 0.090,
        "description": "SuperPowers skill 执行是否有完整的 session 记录？调用链是否完整闭环？是否存在孤儿 active 或链断裂？",
        "principle_id": 2,
    },
    "context_precision": {
        "name": "上下文精度 (RAGAS)",
        "weight": 0.050,
        "description": "每次检索返回的相关记忆占比，基于向量相似度自动计算（≥0.7 为相关）",
    },
    "context_recall": {
        "name": "上下文召回率 (RAGAS)",
        "weight": 0.050,
        "description": "应被检索到的记忆实际被检索到的比例，无 ground truth 时跳过（返回 null）",
    },
}
```

- [ ] **Step 2: Verify weight sum equals 1.000**

```bash
python -c "
from plastic_promise.core.constants import AUDIT_DIMENSIONS
total = sum(d['weight'] for d in AUDIT_DIMENSIONS.values())
print(f'Weight sum: {total}')
assert abs(total - 1.0) < 0.001, f'Expected 1.000, got {total}'
print('OK: weights sum to 1.000')
print(f'Dimensions: {len(AUDIT_DIMENSIONS)}')
"
```
Expected: `Weight sum: 1.0` (or `1.000` within float tolerance), `Dimensions: 10`

- [ ] **Step 3: Commit**

```bash
git add plastic_promise/core/constants.py
git commit -m "fix: audit dimensions 9+10 (RAGAS) + weight correction to 1.000"
```

---

### Task 10: Soul Auditor RAGAS Integration

**Files:**
- Modify: `plastic_promise/defense/soul_audit.py`
- Modify: `plastic_promise/mcp/tools/audit_defense.py`

**Interfaces:**
- Consumes: `compute_context_precision`, `compute_context_recall` from Task 8; updated `AUDIT_DIMENSIONS` from Task 9
- Produces: `SoulAuditor.run_audit(include_ragas=True)` computes and includes RAGAS dimensions in report

- [ ] **Step 1: Add RAGAS dimension computation to SoulAuditor**

```python
# plastic_promise/defense/soul_audit.py
# Find SoulAuditor.run_audit() method and add after existing dimension scoring:

# Add import at top:
from plastic_promise.core.ragas_metrics import compute_context_precision, compute_context_recall

# In run_audit(), after computing existing dimensions, add:

def _compute_ragas_dimensions(self, context_engine) -> dict:
    """Compute RAGAS context precision and recall dimensions.
    
    Returns dict with 'context_precision' and 'context_recall' scores.
    context_recall may be None if ground truth unavailable.
    """
    result = {}
    
    # Collect recent query vectors and their retrieved results
    # from the context engine's recall history
    try:
        recall_history = getattr(context_engine, '_recall_history', []) or []
    except Exception:
        recall_history = []
    
    if not recall_history:
        # No recall data → precision unknown (default to neutral)
        result["context_precision"] = 0.5
        result["context_recall"] = None
        return result
    
    precisions = []
    recalls = []
    
    for entry in recall_history[-20:]:  # Last 20 recall operations
        query_vec = entry.get("query_vector")
        retrieved = entry.get("retrieved", [])
        expected = entry.get("expected_ids")
        
        if query_vec and retrieved:
            precisions.append(
                compute_context_precision(retrieved, query_vec, threshold=0.7)
            )
        
        if retrieved:
            recall = compute_context_recall(retrieved, expected)
            if recall is not None:
                recalls.append(recall)
    
    result["context_precision"] = (
        sum(precisions) / len(precisions) if precisions else 0.5
    )
    result["context_recall"] = (
        sum(recalls) / len(recalls) if recalls else None
    )
    
    return result
```

- [ ] **Step 2: Integrate into run_audit() with include_ragas switch**

```python
# In SoulAuditor.run_audit(), update method signature and body:

async def run_audit(self, include_ragas: bool = True) -> AuditReport:
    """Run full audit across all dimensions.
    
    Args:
        include_ragas: If True (default), include RAGAS context precision/recall
                       dimensions. Set False to skip (e.g., when no embedding available).
    """
    report = AuditReport()
    
    # ... existing dimension computations ...
    
    # RAGAS dimensions (only if enabled)
    if include_ragas:
        ragas_scores = self._compute_ragas_dimensions(self._context_engine)
        report.dimensions["context_precision"] = {
            **AUDIT_DIMENSIONS["context_precision"],
            "score": ragas_scores.get("context_precision", 0.5),
        }
        report.dimensions["context_recall"] = {
            **AUDIT_DIMENSIONS["context_recall"],
            "score": ragas_scores.get("context_recall"),  # May be None
        }
    else:
        # Exclude RAGAS dimensions when disabled
        report.dimensions["context_precision"] = {
            **AUDIT_DIMENSIONS["context_precision"],
            "score": None,
            "skipped": True,
        }
        report.dimensions["context_recall"] = {
            **AUDIT_DIMENSIONS["context_recall"],
            "score": None,
            "skipped": True,
        }
    
    # ... existing overall_score calculation, updated to use all 10 (or 8) dims ...
    
    return report
```

- [ ] **Step 3: Pass include_ragas through MCP handler**

```python
# plastic_promise/mcp/tools/audit_defense.py — in handle_audit_run:
async def handle_audit_run(engine, args):
    # ... existing code ...
    include_ragas = args.get("include_ragas", True)
    # Pass to auditor:
    report = await auditor.run_audit(include_ragas=include_ragas)
    # ... rest of handler ...
```

- [ ] **Step 4: Verify audit output includes new dimensions**

```bash
python -c "
from plastic_promise.core.constants import AUDIT_DIMENSIONS
assert 'context_precision' in AUDIT_DIMENSIONS
assert 'context_recall' in AUDIT_DIMENSIONS
assert len(AUDIT_DIMENSIONS) == 10
print('OK: 10 audit dimensions confirmed')
"
```

- [ ] **Step 5: Commit**

```bash
git add plastic_promise/defense/soul_audit.py plastic_promise/mcp/tools/audit_defense.py
git commit -m "feat: integrate RAGAS metrics into SoulAuditor with include_ragas switch"
```

---

### Task 11: Performance Benchmarks

**Files:**
- Create: `tests/benchmarks/__init__.py`
- Create: `tests/benchmarks/conftest.py`
- Create: `tests/benchmarks/test_bench_lancedb.py`
- Create: `tests/benchmarks/test_bench_sqlite.py`
- Create: `tests/benchmarks/test_bench_gc.py`
- Create: `tests/benchmarks/test_bench_recall.py`
- Create: `tests/benchmarks/test_bench_store.py`
- Create: `tests/benchmarks/test_bench_context.py`

**Interfaces:**
- Consumes: LanceDBStore, RecMem, ContextEngine from core
- Produces: pytest-benchmark JSON output files

- [ ] **Step 1: Create __init__.py and conftest.py**

```python
# tests/benchmarks/__init__.py
# Performance benchmarks for Plastic Promise — LanceDB, SQLite, GC, recall, store, context
```

```python
# tests/benchmarks/conftest.py
"""Shared fixtures for Plastic Promise performance benchmarks.

Standard dataset:
  - Vector dim: 1024
  - Scales: SMALL=1000, LARGE=10000
  - 3 warmup rounds before each benchmark
"""

import os
import tempfile
import random
import pytest

# Force tracing off in benchmarks
os.environ["PP_TRACING_ENABLED"] = "0"
os.environ["PP_EMBEDDER_PROVIDER"] = "fallback"

SMALL = 1000
LARGE = 10000
DIM = 1024
WARMUP_ROUNDS = 3
BENCH_ROUNDS = 10


def _random_vector(dim: int = DIM) -> list[float]:
    """Generate a random normalized vector."""
    import math
    vec = [random.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


@pytest.fixture(scope="module")
def temp_lancedb_dir():
    """Module-scoped temporary LanceDB directory."""
    tmpdir = tempfile.TemporaryDirectory(prefix="pp_bench_lancedb_")
    yield tmpdir.name
    tmpdir.cleanup()


@pytest.fixture(scope="module")
def small_dataset(temp_lancedb_dir):
    """1000-vector dataset for fast benchmarks."""
    from plastic_promise.core.lancedb_store import LanceDBStore
    from plastic_promise.core.embedder import FallbackEmbedder
    
    embedder = FallbackEmbedder(dim=DIM)
    store = LanceDBStore(db_path=temp_lancedb_dir, embedder=embedder)
    
    for i in range(SMALL):
        vec = _random_vector()
        store.upsert(
            memory_id=f"bench-small-{i}",
            vector=vec,
            text=f"Benchmark memory record number {i} with some searchable text content.",
            tier="L1" if i % 3 != 0 else "L3",
            category="benchmark",
            scope="global",
        )
    return store


@pytest.fixture(scope="module")
def large_dataset(temp_lancedb_dir):
    """10000-vector dataset for scale benchmarks."""
    from plastic_promise.core.lancedb_store import LanceDBStore
    from plastic_promise.core.embedder import FallbackEmbedder
    
    embedder = FallbackEmbedder(dim=DIM)
    store = LanceDBStore(db_path=temp_lancedb_dir, embedder=embedder)
    
    for i in range(LARGE):
        vec = _random_vector()
        store.upsert(
            memory_id=f"bench-large-{i}",
            vector=vec,
            text=f"Large benchmark record {i} for scalability testing.",
            tier="L1" if i % 3 != 0 else "L3",
            category="benchmark",
            scope="global",
        )
    return store


@pytest.fixture(scope="module")
def engine(temp_lancedb_dir):
    """Module-scoped ContextEngine for benchmarks that need memory pool access."""
    import os
    from plastic_promise.core.context_engine import ContextEngine
    os.environ["PP_DB_DIR"] = temp_lancedb_dir
    os.environ["PP_TRACING_ENABLED"] = "0"
    os.environ["PP_EMBEDDER_PROVIDER"] = "fallback"
    return ContextEngine()
```

- [ ] **Step 2: Write LanceDB benchmark**

```python
# tests/benchmarks/test_bench_lancedb.py
"""LanceDB ANN + FTS search benchmarks."""

import pytest
from tests.benchmarks.conftest import _random_vector, DIM, WARMUP_ROUNDS, BENCH_ROUNDS


class TestLanceDBANN:
    """ANN vector search benchmarks at SMALL and LARGE scale."""

    def test_ann_search_small(self, small_dataset, benchmark):
        """ANN search over 1000 vectors: P95 target < 50ms."""
        store = small_dataset
        query_vec = _random_vector()
        
        # Warmup
        for _ in range(WARMUP_ROUNDS):
            store.search(query_vec, k=20)
        
        def run():
            return store.search(query_vec, k=20)
        
        result = benchmark(run)
        assert len(result) > 0

    def test_ann_search_large(self, large_dataset, benchmark):
        """ANN search over 10000 vectors: P95 target < 200ms."""
        store = large_dataset
        query_vec = _random_vector()
        
        for _ in range(WARMUP_ROUNDS):
            store.search(query_vec, k=20)
        
        def run():
            return store.search(query_vec, k=20)
        
        result = benchmark(run)
        assert len(result) > 0


class TestLanceDBFTS:
    """Full-text search benchmarks."""

    def test_fts_search(self, small_dataset, benchmark):
        """FTS search over 1000 records: target P95 < 100ms."""
        store = small_dataset
        
        for _ in range(WARMUP_ROUNDS):
            store.search_fts("benchmark memory", k=20)
        
        def run():
            return store.search_fts("benchmark memory", k=20)
        
        result = benchmark(run)
        assert len(result) > 0
```

- [ ] **Step 3: Write remaining benchmark files (sqlite, gc, recall, store, context)**

```python
# tests/benchmarks/test_bench_sqlite.py
"""SQLite write throughput benchmark."""
import pytest
from tests.benchmarks.conftest import WARMUP_ROUNDS


def test_bulk_memory_store(benchmark, engine):
    """Batch store 100 memories: target > 50 writes/s."""
    from plastic_promise.memory.soul_memory import RecMem
    recmem = RecMem(engine=engine)
    
    def run():
        for i in range(100):
            recmem.store(
                content=f"Benchmark memory {i}",
                memory_type="benchmark",
                tags=["bench:sqlite"],
            )
    
    benchmark(run)
```

```python
# tests/benchmarks/test_bench_gc.py
"""GC collect benchmark: mark_decaying + merge_similar overhead."""
import pytest
from tests.benchmarks.conftest import WARMUP_ROUNDS


def test_gc_collect_overhead(benchmark, engine):
    """GC over 1000 memories: target < 5s total."""
    from plastic_promise.memory.soul_memory import RecMem
    
    recmem = RecMem(engine=engine)
    # Seed 1000 memories
    for i in range(1000):
        recmem.store(
            content=f"GC benchmark record {i} with some variation in text length.",
            memory_type="benchmark",
            tags=["bench:gc"],
        )
    
    # Trigger decay marking
    recmem.gc.mark_decaying()
    
    def run():
        recmem.gc.merge_similar()
    
    benchmark(run)
```

```python
# tests/benchmarks/test_bench_recall.py
"""memory_recall end-to-end benchmark."""
import pytest
from tests.benchmarks.conftest import _random_vector, WARMUP_ROUNDS


def test_recall_e2e_small(benchmark, engine):
    """End-to-end memory_recall: target P95 < 500ms."""
    from plastic_promise.mcp.tools.memory import handle_memory_recall
    import asyncio
    
    def run():
        return asyncio.run(handle_memory_recall(engine, {
            "query": "benchmark memory search test",
            "max_results": 20,
        }))
    
    benchmark(run)
```

```python
# tests/benchmarks/test_bench_store.py
"""memory_store end-to-end benchmark: embedding + SQLite + LanceDB dual-write."""
import pytest


def test_memory_store_e2e(benchmark, engine):
    """Full memory_store path: target P95 < 800ms."""
    from plastic_promise.mcp.tools.memory import handle_memory_store
    import asyncio
    
    def run():
        return asyncio.run(handle_memory_store(engine, {
            "content": "Benchmark memory for end-to-end store test with embedding and dual write.",
            "memory_type": "benchmark",
            "tags": ["bench:store"],
        }))
    
    benchmark(run)
```

```python
# tests/benchmarks/test_bench_context.py
"""ContextEngine.supply: vector vs text retrieval latency comparison."""
import pytest
from tests.benchmarks.conftest import WARMUP_ROUNDS


def test_vector_vs_text_retrieval(benchmark, engine):
    """Vector retrieval should not exceed text retrieval by more than 3x."""
    from plastic_promise.core.context_engine import ContextEngine
    
    ctx = ContextEngine()
    
    def run():
        return ctx.supply(
            task_description="benchmark retrieval performance test",
            task_type="benchmark",
        )
    
    result = benchmark(run)
    assert result is not None
```

- [ ] **Step 4: Run benchmarks to verify they work**

```bash
pytest tests/benchmarks/ -v --benchmark-only --benchmark-json=benchmark_results.json --timeout=300
```
Expected: 6 benchmark tests execute and produce valid JSON output

- [ ] **Step 5: Commit**

```bash
git add tests/benchmarks/
git commit -m "test: performance benchmarks — LanceDB, SQLite, GC, recall, store, context (6 scenarios)"
```

---

### Task 12: Documentation Update

**Files:**
- Modify: `GOAL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update GOAL.md**

Add new section under "已完成":

```markdown
### 已完成 (2026-06-30)

- **Staged Hardening (S1-S3)**: 
  - S1: OpenTelemetry 可观测性 — `tracing.py` + 8 MCP 工具 Span + 内部关键路径 Span + requests 自动埋点 + 启用/禁用开关 + Dashboard 可观测性卡片
  - S2: 端到端集成测试 — 5 场景 (Issue 生命周期/修复循环/超时恢复/上下文流动/Skill 追踪) + session-level fixture + 动态端口
  - S3: 性能基准 + RAGAS — 6 基准 + `ragas_metrics.py` (Context Precision/Recall) + 审计 10 维
  - 测试总数: 154 → ~180
```

Update 架构 section to mention tracing:

```markdown
基础设施
  SQLite 写穿透 + schema_version 迁移链 ✅
  域联邦（7域 + 自演化 + 联邦信号） ✅
  标签状态机（task:pending→done→reviewed） ✅
  OpenTelemetry 分布式追踪（8 MCP + 内部路径 + Console/OTLP） ✅
  性能基准套件（LanceDB/SQLite/GC/Recall） ✅
```

- [ ] **Step 2: Update CLAUDE.md**

Add observability note:

```markdown
## 可观测性

- `PP_TRACING_ENABLED=0` 禁用追踪（测试/CI）
- Console Exporter 输出 OTel Span 到 stdout
- `/dashboard` 显示调用统计（P50/P95 延迟 + 错误率）
- 所有 MCP 工具 + 内部关键路径（embedding/LanceDB/SQLite）自动 Span
```

- [ ] **Step 3: Commit**

```bash
git add GOAL.md CLAUDE.md
git commit -m "docs: update GOAL.md and CLAUDE.md for Staged Hardening completion"
```

---

### Self-Review Checklist

Before marking this plan complete, verify:

1. **Spec coverage**: Each spec requirement maps to at least one task:
   - S1 tracing.py → Task 1 ✓
   - S1 internal spans → Task 2 ✓
   - S1 MCP spans → Task 3 ✓
   - S1 server init + requests → Task 4 ✓
   - S1 dashboard card → Task 5 ✓
   - S2 conftest + fixtures → Task 6 ✓
   - S2 integration tests → Tasks 6-7 ✓
   - S3 ragas_metrics → Task 8 ✓
   - S3 audit integration → Tasks 9-10 ✓
   - S3 benchmarks → Task 11 ✓
   - Docs → Task 12 ✓

2. **No placeholders**: Every step has concrete code ✓

3. **Type consistency**: `get_tracer()` returns `trace.Tracer | None` — all callers handle `None` ✓
