# Changelog

All notable changes to Plastic Promise will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.1.2]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/ALdaisuki/plastic-promise-release/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ALdaisuki/plastic-promise-release/releases/tag/v0.1.0
