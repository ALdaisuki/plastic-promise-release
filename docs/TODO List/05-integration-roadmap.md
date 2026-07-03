# 05 — Integration Roadmap

> Integration map for remaining roadmap work. Use [README.md](README.md) for current status and this file for implementation sequencing.

## 1. Completed or Partially Completed Retrieval Work

```text
Query expansion
  -> plastic_promise/core/query_expander.py

Reranker
  -> plastic_promise/core/reranker.py
  -> verify provider defaults, fallback, timeouts, and privacy docs

Decay-aware ranking
  -> plastic_promise/core/context_engine.py
  -> plastic_promise/core/decay_engine.py
  -> verify additive and multiplicative effects in ranking

Vector MMR
  -> plastic_promise/core/context_engine.py
  -> plastic_promise/core/lancedb_store.py
  -> verify vector lookup and near-duplicate demotion
```

## 2. Memory Lifecycle Work

```text
Category-aware merge rules
  -> smart extraction / pipeline dedup path
  -> merge, append, supersede, support, contextualize, contradict

Content chunking
  -> new chunker
  -> LanceDB schema migration
  -> parent memory result mapping

Memory compaction
  -> MemoryGC / compactor
  -> cooldown history
  -> archive instead of destructive deletion

Extraction throttling
  -> smart extractor fallback path
  -> sliding-window limiter
```

Integration checkpoints:

- Category merge rules should land before compaction so compaction does not merge records that should remain separate.
- Chunking changes LanceDB schema and should be isolated behind migration tests.
- Compaction should be disabled by default until archive/recovery behavior is proven.

## 3. Infrastructure Safety Work

```text
Session recovery
  -> launcher / MCP server startup
  -> SQLite-LanceDB reconciliation
  -> stale Hunter Guild claim release

Benchmarking
  -> ContextEngine timing instrumentation
  -> benchmark history
  -> system or CLI reporting

Pipeline trace
  -> ContextItem score history
  -> ContextPack audit metadata
  -> disabled by default

Config-driven tier/decay
  -> config schema
  -> env overrides
  -> preserve current defaults
```

Integration checkpoints:

- Recovery must run before daemon scans to avoid compounding stale state.
- Benchmark and trace features should be opt-in to avoid default latency overhead.
- Config-driven decay must keep existing values as defaults.

## 4. Rust Engine Parity Work

```text
Backend path handling
  -> rust/context-engine-core/src/context_engine.rs
  -> verify new_with_backends does not silently use :memory:

Python/Rust supply contract
  -> plastic_promise/core/context_engine.py
  -> verify docstring and actual arguments match

Principle injection
  -> rust/context-engine-core/src/principles.rs
  -> rust context supply result

Graph traversal
  -> serialize Python graph to Rust or load graph from SQLite

Persistent LanceDB backend
  -> replace HashMap placeholder when dependency constraints allow
```

Integration checkpoints:

- Do not present Rust as canonical until parity tests pass against the same memory pool.
- Python context supply remains the reference implementation.
- Every Rust fallback should label degradation explicitly.

## 5. Causal Foundation Work

```text
Event Memory
  -> new event model distinct from ordinary memory

CausalGraph
  -> causal edges with evidence and confidence

Step closure causal sampling
  -> structured event/action/state fields

Causal replay
  -> timeline, causal chain, counterfactuals, preventive rules

Trust attribution
  -> distinguish agent fault, external fault, shared fault, not fault, unknown
```

Integration checkpoints:

- Start with internal PR/CI/task/step events before any vertical domain product.
- Causal claims must carry evidence IDs and confidence.
- Trust adjustment should cite causal evidence, not just outcome.

## 6. Rollout Policy

| Feature class | Default | Reason |
|---|---|---|
| Pure local scoring | On after tests | Low data-boundary risk. |
| Hosted provider calls | Off until configured | May send task or memory text externally. |
| Schema migration | Explicit migration path | Prevent vector/database drift. |
| LLM compaction | Off by default | Risk of information loss without review. |
| Rust acceleration | Optional | Python remains canonical until parity is proven. |
| Causal attribution | Advisory first | Trust penalties should not rely on unverified causal guesses. |

## 7. Testing Strategy

```text
Retrieval:
  - query expansion idempotence
  - reranker fallback
  - decay rank ordering
  - vector MMR duplicate demotion

Memory lifecycle:
  - category merge decisions
  - chunk boundary and parent mapping
  - compaction archive links
  - extraction throttle limits

Infrastructure:
  - server restart recovery
  - benchmark history persistence
  - trace disabled/enabled overhead
  - config validation and bad-config rejection

Rust:
  - same query against Python and Rust paths
  - principle count parity
  - graph node/edge parity
  - explicit fallback labeling

Causal:
  - event model serialization
  - causal edge confidence updates
  - replay for a known task lifecycle
```
