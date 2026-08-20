# TODO List — Plastic Promise Roadmap Status

> Current roadmap index for unfinished or partially completed work.
> Baseline comparison source: [CortexReach/memory-lancedb-pro](https://github.com/CortexReach/memory-lancedb-pro), analyzed on 2026-07-03.
> Status updated: 2026-08-14.

> The [Union Six-PR Contract](../standards/union-six-pr-contract.json), revision
> `2026-08-18.1`, is the
> authority for the integrated composable-deployment and project-collaboration
> line. A PR is complete only when every `delivery_scope`,
> `collaboration_scope`, and `required_evidence` item passes; one-sided
> completion is not PR completion. This TODO index reports work and evidence
> status but cannot redefine that scope or prove runtime/production state.

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
| R2 | Multi-provider reranker | Partial | `plastic_promise/core/reranker.py`, `tests/test_vertical_slice_units.py` | Provider order, local default model, host normalization, and fallback parsing are covered; continue privacy/docs work for hosted providers. |
| D1 | Three-endpoint composable deployment | In progress | `docs/architecture/three-endpoint-deployment/`, `docs/roadmap/composable-deployment.md`, `plastic_promise/deployment/endpoint_contract.py`, `plastic_promise/deployment/container_artifacts.py`, `plastic_promise/deployment/migration_journal.py`, `plastic_promise/local_inference_node/`, `plastic_promise/core/node_governance.py` | PR 1 provides the stacked routing seam; PR 2 provides the V2 endpoint contract; PR 3 provides `ContainerArtifactCompiler` and protected no-push OCI build-verification; PR 4 provides the read-only Deployment Center plus source-level working-set/awareness projections. Current PR 5 source adds backup-gated migration contracts, durable collaboration stores, restart-safe Hook continuation, server-owned work issuance, ordinary tool reconcile, bounded Stop progress/submitted events, Maintenance composition, shadow/inject awareness, a read-only collaboration Dashboard, and atomic pending-only promotion enqueue. PR 6 remains the target release, installer, upgrade, rollback, and final cross-endpoint evidence slice. Live mutable migration adapters, real browser/runtime evidence, production activation, and publication remain target or unverified. The current stack makes no claim that a live production migration, LanceDB promotion, Maintenance transition, MCP restart, registry publication, or stable release has occurred. SQLite remains solely owned by `pp-server-backend`, LanceDB remains derived, and deployment interaction remains on `pp-local-edge`. |
| D2 | Maintainer Release Builder | In progress | `docs/release-builder.md`, `docs/adr/0006-maintainer-request-triggered-release-builder.md` | Implement the six request/receipt, Windows worker, evidence, deployment, synchronization, and documentation slices.  Keep `Project Steward / user-project build adapters` as a future-only goal. |
| C1 | Project-scoped multi-Agent collaboration | In progress / evidence pending | `plastic_promise/collaboration/`, `plastic_promise/mcp/server.py`, `plastic_promise/passive_memory/codex_hook.py`, `plastic_promise/mcp/dashboard_v2/`, `daemons/maintenance_daemon.py` | Current source contains the PR1–PR4 foundations plus PR5 durable stores, restart-safe authenticated Hook continuation, server-owned bounded work issuance/operations, ordinary tool reconcile, bounded Stop progress/submitted events, typed stage/result receipts, Maintenance composition, shadow/inject awareness, read-only topology/work/timeline projections, and atomic accepted-result-to-pending-only promotion enqueue. It does not establish whole-PR completion. Real browser/runtime smoke, production activation, migration execution, and governed review/runtime/production receipts remain open. PR6 owns final cross-Agent E2E and shadow-only Workflow Composer evidence. Status remains governed by the union evidence ledger. |
| R3 | Decay-aware retrieval ranking | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/decay_engine.py` | Verify additive recency and multiplicative decay are both applied in ranking. |
| R4 | Vector MMR diversity | Partial | `plastic_promise/core/context_engine.py`, `plastic_promise/core/lancedb_store.py` | Verify real vector lookup path and chunk interaction. |
| R5 | Pipeline trace / score history | Planned | No verified public trace object in docs pass. | Design low-overhead trace gated by env var. |
| R6 | Real-time tier promotion/demotion | Partial | Context/tier logic exists, but complete demotion/config behavior needs verification. | Confirm thresholds and add tests. |
| R7 | Category-aware merge rules | Planned | No verified category rule engine in docs pass. | Implement merge/update/append rules per memory category. |
| R8 | Content chunking for long memories | Partial | `plastic_promise/core/embedder.py`, `tests/test_embedder.py` | Embedding-request chunking is implemented; LanceDB parent/child chunk schema migration remains planned. |
| R9 | Memory compaction | Planned | `MemoryGC.merge_similar()` exists, but progressive LLM compaction/cooldown/archive is not verified. | Add compaction design and conservative rollout gate. |
| R10 | Extraction throttling | Planned | No verified sliding-window throttle in docs pass. | Add rate limiter around LLM fallback extraction. |
| R11 | Session recovery | Partial | Launcher stale-claim recovery now has explicit project or system authority in the PR 1 worktree; orphan/missing vector recovery is still unverified. | Complete focused stale-claim migration/recovery review, then add orphan-vector and missing-vector reconcile in the later storage slices. |
| R12 | Performance benchmarking | Done | `plastic_promise/core/benchmark.py`, `system(action=benchmark)`, `tests/test_performance_benchmark.py` | Wire release-specific baselines into CI as needed. |
| R13 | Emoji-only noise detection | Done | `plastic_promise/core/noise_filter.py`, `tests/test_recall_quality_quick_fixes.py`, `tests/test_vertical_slice_units.py` | Verified emoji-only, emoji+whitespace, reaction wrapper, and mixed meaningful text behavior. |
| R14 | Dual-layer iron rules | Planned | Step closure exists; derived principle extraction is not verified. | Add optional derived-principle layer. |
| R15 | Obsidian vault export | Planned | `pack_export` exists for JSON; markdown vault export not verified. | Design markdown/YAML export command. |
| R16 | Config-driven tier/decay | Planned | Decay constants appear code-based. | Add schema-validated config and env overrides. |
| R17 | Multi-provider embedding and key rotation | Planned | Default local Ollama/fallback path exists; provider/key rotation not verified. | Research provider abstraction without breaking vector dimensions. |
| R18 | Rust principle injection parity | Partial | Verified by `cargo test --manifest-path rust/context-engine-core/Cargo.toml` and `python -B -m pytest -p no:cacheprovider tests/test_rust_release_import.py::test_release_context_engine_core_import_contract -q`; evidence shows non-empty activated principles and matching `principle_injection_count`, not full principle set/content parity. | Compare Rust activation against the canonical Python task-type mapping before closing R18. |
| R19 | Rust graph traversal parity | Planned | Rust graph loading parity not verified. | Serialize/load graph or query SQLite from Rust. |
| R20 | Rust backend path handling | Done | Verified by `cargo test --manifest-path rust/context-engine-core/Cargo.toml` and `python -B -m pytest -p no:cacheprovider tests/test_rust_integration.py::test_supply_rust_preserves_memory_db_path_for_new_with_backends tests/test_rust_integration.py::test_supply_rust_uses_new_with_backends_and_project_context tests/test_rust_integration.py::test_debug_supply_uses_rust_path_when_rust_is_preferred -q`; evidence covers `new_with_backends` path handling, `_supply_rust` preserving `:memory:`, and project-aware snapshot context. | None. |
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

4. Close remaining Rust parity gaps
   -> graph traversal, LanceDB persistence

5. Start causal foundation
   -> event memory, causal graph, replay, trust attribution
```

## Roadmap Policy

- Keep dated research, but mark it as baseline research.
- Do not present worktree-only claims as completed until current source verifies them.
- Use text status markers (`[P0]`, `[P1]`, `Done`, `Partial`) instead of emoji.
- Every closed item should cite source files, tests, or release notes.
- New strategic items should use unique numbering and appear in this README index.
