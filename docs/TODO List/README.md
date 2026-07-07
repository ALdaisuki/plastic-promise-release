# TODO List — Plastic Promise Roadmap Status

> Current roadmap index for unfinished or partially completed work.
> Baseline comparison source: [CortexReach/memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro), analyzed on 2026-07-03.

This folder now separates dated research from current roadmap status. Older comparison files may describe gaps that have since been implemented or partially implemented. Treat this README as the current index.

## Status Legend

| Status | Meaning |
|---|---|
| Done | Source evidence indicates the item is implemented. |
| Partial | Some implementation exists, but scope is incomplete or needs verification. |
| Planned | No verified implementation yet; still on roadmap. |
| Experimental | Exists but should not be treated as stable/public contract. |
| Needs verification | Documentation or worktree notes claim progress, but current source evidence is insufficient. |

## Roadmap Status

| ID | Area | Status | Source evidence | Next action |
|---|---|---|---|---|
| R1 | Query expansion | Done | `plastic_promise/core/query_expander.py` | Keep tests and docs aligned. |
| R2 | Multi-provider reranker | Partial | `plastic_promise/core/reranker.py` | Verify provider behavior, defaults, timeouts, and privacy wording. |
| R3 | Decay-aware retrieval ranking | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/decay_engine.py` | Verify additive recency and multiplicative decay are both applied in ranking. |
| R4 | Vector MMR diversity | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/lancedb_store.py` | Verify real vector lookup path and chunk interaction. |
| R5 | Pipeline trace / score history | Planned | No verified public trace object in docs pass. | Design low-overhead trace gated by env var. |
| R6 | Real-time tier promotion/demotion | Partial | Context/tier logic exists, but complete demotion/config behavior needs verification. | Confirm thresholds and add tests. |
| R7 | Category-aware merge rules | Planned | No verified category rule engine in docs pass. | Implement merge/update/append rules per memory category. |
| R8 | Content chunking for long memories | Planned | No verified chunk schema migration in docs pass. | Design LanceDB schema migration and parent-memory result mapping. |
| R9 | Memory compaction | Planned | `MemoryGC.merge_similar()` exists, but progressive LLM compaction/cooldown/archive is not verified. | Add compaction design and conservative rollout gate. |
| R10 | Extraction throttling | Planned | No verified sliding-window throttle in docs pass. | Add rate limiter around LLM fallback extraction. |
| R11 | Session recovery | Planned | Launcher cleans stale PID files; no full SQLite/LanceDB recovery pass verified. | Add startup recovery for orphan vectors, missing vectors, and stale claims. |
| R12 | Performance benchmarking | Done | `plastic_promise/core/benchmark.py`, `system(action=benchmark)`, `tests/test_performance_benchmark.py` | Wire release-specific baselines into CI as needed. |
| R13 | Emoji-only noise detection | Needs verification | `plastic_promise/core/noise_filter.py` exists; specific emoji-only behavior was not verified in this pass. | Add explicit tests if missing. |
| R14 | Dual-layer iron rules | Planned | Step closure exists; derived principle extraction is not verified. | Add optional derived-principle layer. |
| R15 | Obsidian vault export | Planned | `pack_export` exists for JSON; markdown vault export not verified. | Design markdown/YAML export command. |
| R16 | Config-driven tier/decay | Planned | Decay constants appear code-based. | Add schema-validated config and env overrides. |
| R17 | Multi-provider embedding and key rotation | Planned | Default local Ollama/fallback path exists; provider/key rotation not verified. | Research provider abstraction without breaking vector dimensions. |
| R18 | Rust principle injection parity | Needs verification | Rust roadmap notes claim worktree fixes; current source not re-verified here. | Verify current Rust path against Python context package. |
| R19 | Rust graph traversal parity | Planned | Rust graph loading parity not verified. | Serialize/load graph or query SQLite from Rust. |
| R20 | Rust backend path handling | Needs verification | Roadmap says fixes existed in a worktree; current source must be checked before closing. | Verify `new_with_backends` and `_supply_rust` current code. |
| R21 | Rust persistent LanceDB backend | Planned | Rust LanceDbStore described as HashMap-backed in roadmap. | Replace placeholder when dependency constraints allow. |
| R22 | Causal world model foundation | Planned | Strategic roadmap only. | Start with event memory and causal graph for internal PR/CI/task events. |

## Files in This Folder

| File | Current role |
|---|---|
| [01-comparison-analysis.md](01-comparison-analysis.md) | Dated baseline comparison against CortexReach; not current truth for completion status. |
| [02-retrieval-enhancement.md](02-retrieval-enhancement.md) | Retrieval roadmap; many items are done or partial and should be read with this index. |
| [03-smart-extraction-upgrade.md](03-smart-extraction-upgrade.md) | Active smart extraction and lifecycle roadmap. |
| [04-infrastructure-gaps.md](04-infrastructure-gaps.md) | Active infrastructure and polish roadmap. |
| [05-integration-roadmap.md](05-integration-roadmap.md) | Integration map; update as implementation status changes. |
| [06-rust-engine-gaps.md](06-rust-engine-gaps.md) | Active Rust parity roadmap. |
| [07-causal-world-model-roadmap.md](07-causal-world-model-roadmap.md) | Strategic causal/event/world-model roadmap. |

## Current Implementation Order

```text
1. Verify completed retrieval claims
   -> query expansion, reranker, decay ranking, vector MMR

2. Finish memory lifecycle quality
   -> category-aware merge, chunking, compaction, extraction throttle

3. Add infrastructure safety
   -> session recovery, benchmarks, trace output, config-driven decay

4. Close Rust parity gaps
   -> backend path handling, principle injection, graph traversal, LanceDB persistence

5. Start causal foundation
   -> event memory, causal graph, replay, trust attribution
```

## Roadmap Policy

- Keep dated research, but mark it as baseline research.
- Do not present worktree-only claims as completed until current source verifies them.
- Use text status markers (`[P0]`, `[P1]`, `Done`, `Partial`) instead of emoji.
- Every closed item should cite source files, tests, or release notes.
- New strategic items should use unique numbering and appear in this README index.
