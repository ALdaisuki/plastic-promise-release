# Changelog

All notable changes to Plastic Promise will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Released version: `0.1.17`.

## [0.1.17] - 2026-07-18

### Added

- Added feature-gated structure-aware embedding chunking for Markdown headings,
  paragraphs, fenced code, lists, and tables.
- Added a read-only shadow benchmark that reports legacy truncation and
  structure-aware coverage without calling an embedding model or writing an index.

### Changed

- `PP_MEMORY_CHUNKING=shadow` preserves legacy embedding requests and index
  identity while emitting candidate diagnostics; `structure-v1` is opt-in and
  binds its configuration into the persisted embedding model identity.
- Structure-aware mode preserves the tail within a bounded request budget and
  reports middle coverage when the budget is exceeded instead of silently
  pretending the full source was embedded.
- Pipeline and LanceDB repair paths now bind the complete chunking identity and
  migrate existing same-model-family material before replacing derived vectors.
- Opaque held-out report fingerprints now normalize text line endings, keeping
  frozen recall contracts stable across Windows `autocrlf` without parsing or
  exposing held-out content.

### Verification

- Default behavior remains legacy-compatible and is unchanged unless the new
  feature flag is explicitly enabled.
- Long-text validation uses a real local Ollama embedding sample; publishable
  recall claims still require the versioned real-model benchmark and store to
  recall to context smoke.
- Rollback keeps `PP_MEMORY_CHUNKING=off` and requires rebuilding the derived
  LanceDB index after the flag change; SQLite remains the canonical source.
- Release status for `0.1.17` is **audited and approved**. Final whole-repository verification and mandatory high-risk review completed before release synchronization. Release-specific benchmark and runtime evidence are recorded in the release notes.

## [0.1.16] - Draft (unreleased)

### Fixed

- Isolated synchronous `context_supply` work behind a bounded executor with
  configurable embedding and supply timeouts, so a slow retrieval path cannot
  block the MCP HTTP event loop and instead returns an auditable degraded result.
- Replaced per-memory LanceDB vector lookups in the Rust snapshot path with
  bounded batch queries filtered to admitted memory IDs, eliminating the N+1
  query pattern without crossing the canonical admission boundary.

### Verification

- Overall release status is **Draft/BLOCK**. Targeted context, LanceDB, Rust,
  canonical-admission, live HTTP, restart, and release-sync gates must pass
  before publishing `v0.1.16`.

## [0.1.15] - 2026-07-13

### Added

- Added opt-in governed synthesis and memory proposals with canonical SQLite
  lifecycle, provenance, review evidence, and fail-closed retrieval admission.
- Added durable checked `memory-index/v3` upsert/delete replay so LanceDB remains
  a rebuildable projection of canonical memory state.

### Changed

- Routed retrieval-visible ordinary-memory content and availability changes
  through one field-scoped transaction boundary. Source lineage, dependent
  synthesis invalidation, the canonical memory version, and checked index jobs
  are now committed together before derived-index repair.
- Kept ordinary metadata-only changes narrow so updates do not overwrite
  project, provenance, summary, index-material, or lifecycle fields owned by
  other subsystems.
- Raised the minimum supported LanceDB dependency to `0.34.0` for the governed
  retrieval and compact index contracts.

### Fixed

- Enforced project equality during GC candidate discovery, before a merge
  transaction, and again against canonical source and peer rows inside the
  transaction. Empty, cross-project, and spoofed peer identities fail without
  changing memory, lineage, outbox, version, or cache state.
- Made both `smart-remember` aliases use server-owned `memory_update` authority
  and overwrite caller-supplied runtime identity before canonical reads or
  mutations.
- Routed public update/correct/forget, feedback, skill completion, RecMem,
  lifecycle scans, duplicate cleanup, GC merge, and audit rollover through
  canonical patch or source-mutation paths.
- Removed every case-insensitive `cat:*` tag during LLM reclassification instead
  of leaving differently cased stale category tags behind.
- Made ordinary `memory_store` return the durable canonical survivor after
  vector deduplication, and made restart-recovery source seeding deterministic
  so model similarity cannot collapse its two independent source records.
- Bound vector-dedup reinforcement to an exact project, visibility, source
  class, and memory-type match, and made API/SSE metadata come from the
  canonical survivor instead of the submitted candidate.

### Security

- Enforced direct tool-manifest trust checks, retained public `memory_forget` as
  a critical `0.80` operation, and added the internal-only `audit_rollover`
  capability at `0.60` without weakening public delete authority.
- Bound public mutation actor, call, project, and trust evidence to server-owned
  runtime context; caller-declared identity remains audit-only.

### Upgrade Notes

- The synthesis, synthesis-retrieval, proposal, and compact index-text gates
  remain off by default, so existing deployments keep legacy retrieval behavior
  until each gate is explicitly enabled.
- The governed retrieval path requires LanceDB `>=0.34.0`. Environments pinned
  to an older LanceDB must upgrade that dependency before restarting services.
- The public release repository contains a historical `v0.2.14` tag, which
  SemVer sorts above the active `main` package line (`0.1.14`) and this prepared
  `0.1.15` version. The historical tag remains untouched and `0.1.15` must not
  be marked as latest; pure SemVer selectors may therefore continue to prefer
  `v0.2.14`.
- Restart the MCP server and Maintenance Daemon together after upgrading; mixed
  process versions do not share one mutation contract. Then run
  `python scripts/smoke_http_mcp.py --expected-version 0.1.15 --expected-mode rust-full`
  against the live Streamable HTTP endpoint.
- Fusion rollback restores `legacy-auto`, unsets the candidate RRF environment,
  retains SQLite/outbox evidence, and replays checked index jobs before smoke.
- Maintenance one-shot JSON output, PID-bound `maintenance-heartbeat/v1`, and
  the cross-process restart-recovery smoke are the release recovery evidence.
- Existing valid `memory-index/v2` upserts remain replay-compatible; all new
  checked ordinary index upserts and deletes use `memory-index/v3`.
- No public MCP tool or parameter was removed. Existing SQLite memory remains
  canonical; LanceDB is derived and can be repaired from durable checked jobs.

### Verification

- Historical Tasks 1-5 slice evidence remains: the stable 17-file matrix passed
  `688` tests twice, a read-only reviewer ran `95` focused tests, and the local
  audit fallback scored `0.6987` against its `0.60` slice gate.
- Release status for `0.1.15` is **audited and approved**. The one-shot public
  calibration produced no eligible WRRF candidate, so held-out queries remained
  unopened and `legacy-auto` was the released policy.

## [0.1.14] - 2026-07-09

### Changed

- Defaulted local runtime startup to `EMBEDDER_TIMEOUT=30` unless explicitly
  overridden, reducing false MCP smoke failures during cold Ollama embedding
  calls.
- Documented MCP connection troubleshooting: use `/health` for probes, reserve
  `/mcp` for MCP clients, and refresh Codex sessions after MCP restarts when
  dynamic tool handles are stale.

### Fixed

- Scoped Windows `scripts/init_and_start.py --stop` fallback to Plastic Promise
  MCP and `maintenance_daemon.py` command lines instead of terminating every
  `python.exe` process.
- Suppressed benign Windows Proactor `ConnectionResetError [WinError 10054]`
  tracebacks when MCP HTTP clients close long-lived connections.

## [0.1.13] - 2026-07-09

### Added

- Added deterministic Rust/Python snapshot parity fixtures for project
  isolation, English and Chinese BM25, source penalty/exclusion, MMR, noise
  filtering, and hard minimum score behavior.
- Added a `rust_snapshot_supply` benchmark path with p50/p95 gate coverage for
  the Python-to-Rust snapshot boundary.

### Changed

- Expanded Rust snapshot debug output with per-stage filter counts, stage timing,
  fallback reason, hard score floor, and per-item keep/drop reasons.
- Clarified that the current Rust vector/FTS store is a snapshot-fed in-memory
  adapter while Python remains the persistent LanceDB authority.

### Fixed

- Isolated Rust integration tests from repository-wide code-memory scans by
  disabling code-memory by default and using temporary DB/LanceDB roots.

## [0.1.12] - 2026-07-09

### Changed

- Tightened the `PP_MEMORY_SUMMARY_INDEX=1` write path so embedding input is
  summary-only (L0/L1). Raw source text and L2 narrative remain persisted in
  SQLite but are no longer included in the gated vector embedding document.

## [0.1.11] - 2026-07-09

### Added

- Added a feature-gated memory summary index layer behind
  `PP_MEMORY_SUMMARY_INDEX=1`, persisting `raw_content`, `l0_abstract`,
  `l1_summary`, `l2_content`, `embedding_text`, and `embedding_hash` in SQLite.
- Added regression coverage for summary-index field construction, gated
  `embedding_text` usage, compact LanceDB `search_text`, legacy gate-off
  behavior, and SQLite round-tripping of the new memory index fields.
- Added design, implementation-plan, and exemplar-research notes for the
  SQLite-truth / LanceDB-derived-index memory contract.

### Changed

- When `PP_MEMORY_SUMMARY_INDEX=1`, memory pipeline embeddings use deterministic
  `embedding_text`, while LanceDB stores compact index text instead of raw or
  L2 memory content. The default gate-off path preserves existing behavior.

## [0.1.10] - 2026-07-08

### Changed

- Added chunked Ollama embedding for long memory text, controlled by
  `EMBEDDER_CHUNK_CHARS` and `EMBEDDER_MAX_CHUNKS`, then mean-pooled and
  normalized so review/audit records can be indexed without exceeding the
  local embedding context window.
- Changed the default local Ollama rerank model to `qwen2.5:3b`, keeping
  `mxbai-embed-large` as the default embedding model instead of using it for
  `/api/generate`.

### Fixed

- Prevented launcher warmup and LanceDB backfill from surfacing Ollama 500
  errors for long review memories that exceeded `/api/embeddings` input limits.
- Normalized `OLLAMA_HOST=0.0.0.0` to `127.0.0.1` for local rerank client calls.
- Hardened local rerank parsing when small generation models return score arrays
  with non-strict JSON such as ellipses.

## [0.1.9] - 2026-07-08

### Added

- Added debug-only canonical hot lookup and ContextGate telemetry for the Python
  context supply path behind feature flags, including audit metadata and per-item
  gate stats without changing prompt layers unless explicit enforcement flags are
  enabled.
- Added Codex MCP schema/encoding exemplar research and release planning docs for
  the Engram hot-memory and MCP debug contract slice.

### Changed

- `context_supply(debug=true)` now follows the same MCP schema/handler contract
  pattern as `memory_recall(debug=true)`: normal calls keep prompt output, while
  debug calls return structured prompt, layer, audit, pipeline, and per-item data.
- Canonical hot lookup now respects `PP_CODE_MEMORY_ENABLED=0` before consulting
  the code-memory index and safely falls back to the default hot lookup limit when
  `PP_CANONICAL_HOT_LIMIT` is invalid.

### Fixed

- Exposed `debug` and `retrieval_mode` in the MCP `context_supply` schema so
  Codex deferred-tool validation no longer rejects diagnostic calls.
- Added UTF-8-clean MCP initialization instructions with the Codex bootstrap
  contract, matching the official server-wide guidance path.
- Added a public MCP description regression guard for common mojibake markers,
  confirming that observed event-stream mojibake was a probe display issue rather
  than corrupted source metadata.

## [0.1.8] - 2026-07-08

### Fixed

- Enriched Rust-primary `context_supply` results with read-only `code_memory` evidence and excluded local worktrees from the code index.
- Added a launcher watchdog grace window so long MCP tool calls do not get mistaken for crashed child processes while preserving immediate restart for exited processes.
- Initialized LanceDB/domain heavy backends at the `ContextEngine.supply()` boundary before Rust snapshot retrieval, even when MCP callers provide a precomputed task vector.
- Resolved implicit project-context degradation by inferring `PLASTIC_PROJECT_ID` / `PP_PROJECT_ID` and setting `project:plastic-promise` defaults for both launcher-managed and direct MCP starts.
- Kept `rust-full` request-process startup responsive by leaving LanceDB backfill/rebuild to launcher warmup maintenance instead of per-process runtime mode env.

### Changed

- Documented default project identity behavior for launcher and direct MCP starts.
- Clarified release-facing runtime docs so `full` / `rust-full` LanceDB backfill and rebuild are startup warmup work, not request-time heavy initialization.

## [0.1.7] - 2026-07-08

### Fixed

- Honored explicit SQLite paths in the Rust `ContextEngine::new_with_backends` constructor while preserving `:memory:` for isolated tests and release smoke checks.
- Preserved `PLASTIC_DB_PATH=":memory:"` across the Python `_supply_rust` boundary before dispatching to the Rust backend constructor.

### Changed

- Clarified Rust parity roadmap status: R18 remains partial for principle set/content parity, R19 remains planned, and R20 backend path handling is done with source, Python boundary, and release import evidence.

## [0.1.6] - 2026-07-07

### Added

- Added project-aware memory metadata, recall filtering, and request-scope trace fields so multi-project diffs can keep project context separate while retaining global divergent context.
- Added traceability and degradation helpers that expose call provenance, warning envelopes, fallback paths, and minimum runnable results across MCP memory/context flows.
- Added release-grade resilience coverage for project memory schema, recall isolation, traceability degradation, commercial audit export, task recovery, and launcher startup behavior.

### Changed

- Extended principle activation with project overlay metadata while keeping global principles immutable.
- Hardened launcher startup, maintenance daemon recovery, and release sync validation paths for commercial release handoff.

### Fixed

- Preserved legacy `memory_reclassify` and `memory_sync_files` wrapper imports for older clients.
- Kept `memory_store` responses explicit when durable storage degrades or produces no persisted memory.

## [0.1.5] - 2026-07-06

### Fixed

- Kept `memory_recall(debug=true)` on the Rust snapshot hot path when Rust is healthy and preferred, preventing debug recall from forcing the MCP server into the slower Python full pipeline under `rust-full`.
- Added regression coverage proving `debug=True` uses `_supply_rust()` instead of `_supply_python()` in Rust-preferred mode, while Rust debug counters remain visible through `pipeline_stats` and `per_item_stats`.

## [0.1.4] - 2026-07-06

### Fixed

- Bootstrapped the Maintenance Daemon import path for both launcher-managed startup and direct `python daemons/maintenance_daemon.py` usage, preventing `ModuleNotFoundError: No module named 'plastic_promise'` after service restarts.
- Prepended the project root to child-process `PYTHONPATH` in the launcher `ServiceManager`, preserving existing `PYTHONPATH` while making script-based services importable from hidden Windows subprocesses.
- Updated the one-click launcher banner to report the package version instead of the stale `0.1.0` label.

## [0.1.3] - 2026-07-06

### Fixed

- Excluded daemon audit telemetry from Rust snapshot ingestion before BM25, FTS, vector, and item lookup construction, so `AUDIT trust=...` rows cannot score or leak through the Rust hot path.
- Added a Python conversion-boundary filter for Rust `ContextPack` results, preventing stale or mismatched native extensions from returning audit telemetry into `memory_recall` or `context_supply`.
- Aligned MCP server and dashboard health version reporting with `plastic_promise.__version__` instead of the stale hardcoded `0.1.0`.

## [0.1.2] - 2026-07-06

### Fixed

- Filtered prefixed daemon audit telemetry such as `[maintenance_daemon] AUDIT trust=...` and `[0.70] [maintenance_daemon] AUDIT trust=...` before Python `context_supply` layering, preventing recovered-task audit rows from reappearing in related or divergent context.

## [0.1.1] - 2026-07-06

### Fixed

- Added request-scope isolation for heavy `memory_recall` and `context_supply` calls using `stage_session_id`, `flow_line_id`, and `request_id`, with derived `request_scope_id` metadata and visible `context_supply` trace output for cache isolation and auditability.
- Kept Python `ContextEngine` request state local to each supply call to avoid cross-talk between concurrent domain-scoped requests.
- Brought the Rust context hot path in line with Python recall filtering so `maintenance_daemon` audit telemetry is filtered or penalized before it can dominate core or related context.
- Exposed the new request-scope fields in the MCP schemas for `memory_recall` and `context_supply`, preventing Codex deferred-tool validation from rejecting isolated heavy requests.

### Changed

- Hardened `scripts/release-sync.py` so release synchronization does not duplicate an existing changelog entry for the target version.
- Added regression coverage for request-scope defaults, cache-key isolation, context audit metadata, MCP schema exposure, and Rust audit-telemetry filtering.

### Changed

- Reworked the public README into an English-first release entrypoint with a Chinese companion guide at `docs/README.zh-CN.md`.
- Added a compact ASCII architecture map and SVG flow graphic for release documentation.
- Refreshed architecture docs around the current local-first MCP runtime, explicit degraded mode, Maintenance Daemon, and optional Rust accelerator status.
- Reconciled public documentation with source truth for version `0.1.0`, the `maintenance_daemon.py` entrypoint, and the MCP tool surface declared in `plastic_promise/mcp/server.py`.
- Updated TODO documentation policy so roadmap pages distinguish completed, partial, planned, experimental, and needs-verification work.

### Documentation

- Added clearer privacy wording: local-first by default, with possible external calls only when optional providers or agents are configured.
- Added a current status matrix for MCP server, memory pipeline, context supply, Hunter Guild, SuperPowers, extension market, release pipeline, and documentation.
- Added contribution notes for small logical PRs, verification notes, and the rule that pull requests must not be merged without explicit maintainer authorization.

## [0.1.0] — 2026-07-01

### Added

- Initial public package metadata for Plastic Promise.
- MCP server with memory, principles, context, audit/defense, reflection, system, pack, domain, skill, dispatch, review, market, and SuperPowers tool groups.
- Dual storage design: SQLite WAL for structured state and LanceDB for vector/search state.
- Memory quality pipeline: extraction, classification, embedding, deduplication, quality gate, decay initialization, and dual write.
- Context supply engine combining memory retrieval, graph signals, principles, ranking, and layered context packaging.
- 12 core operating principles and principle activation/evaluation flows.
- Trust-driven defense model with persisted trust history.
- Hunter Guild task lifecycle: enqueue, claim, heartbeat, complete, verify, inbox, and abandon.
- Programmatic skills: `session-init`, `smart-remember`, `step-closure`, and `sp-stage`.
- One-click launcher for MCP server, Maintenance Daemon, and watchdog.
- Optional Rust `context-engine-core` accelerator path.
- Web dashboard and local health endpoints.
- Security policy, contributing guide, release governance scaffolding, and baseline architecture documentation.

### Changed

- Rebuilt the memory system from in-memory prototypes toward SQLite + LanceDB persistence.
- Extended the project from memory-only behavior toward a local governance runtime with audit, trust, skills, and task dispatch.

### Fixed

- Import path issues around MCP server startup.
- Duplicate memory handling through vector similarity and quality gates.
- LanceDB/SQLite consistency paths for common memory operations.

[0.1.17]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ALdaisuki/plastic-promise-release/releases/tag/v0.1.0
